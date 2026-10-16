"""NavPage — FastAPI 应用。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8080
"""
import os
import re
import socket
import threading
import urllib.parse
import time
import json
import hashlib
import warnings
import concurrent.futures
from urllib.parse import quote

warnings.filterwarnings("ignore")

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Depends
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import httpx

from . import db, netutil, dockerapi, icons, iconlib, widgets, registry, cron

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="NavPage", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------- helpers

def _now():
    return int(time.time())


def icon_url_for(item):
    raw = (item.get("icon") or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return "/api/icon?url=" + quote(raw, safe="")
    return "/api/iconfile/" + quote(os.path.basename(raw), safe="")


def resolve_item(item, mode, ctx=None):
    """按当前模式挑地址，返回附加 url 字段的条目。

    url_lan / url_wan 里可以写 `{host}` `{scheme}` 占位符，按本次请求的实际
    访问地址展开，这样同一个条目在局域网和公网访问时都能拿到正确链接。
    """
    ctx = ctx or {}
    lan_raw = (item.get("url_lan") or "").strip()
    wan_raw = (item.get("url_wan") or "").strip()

    lan = netutil.expand_url(lan_raw, "lan", ctx) if lan_raw else ""
    wan = netutil.expand_url(wan_raw, "wan", ctx) if wan_raw else ""

    # 某一侧没配时，用另一侧的模板换主机名兜底
    if not wan and "{host}" in lan_raw:
        wan = netutil.expand_url(lan_raw, "wan", ctx)
    if not lan and "{host}" in wan_raw:
        lan = netutil.expand_url(wan_raw, "lan", ctx)

    if mode == "wan":
        url = wan or lan
        prefer = "wan" if wan else ("lan" if lan else "none")
    else:
        url = lan or wan
        prefer = "lan" if lan else ("wan" if wan else "none")

    if lan and wan:
        availability = "both"
    elif lan:
        availability = "lan"
    elif wan:
        availability = "wan"
    else:
        availability = "none"
    out = dict(item)
    out["url"] = url
    out["url_lan_view"] = lan
    out["url_wan_view"] = wan
    out["used"] = prefer
    out["availability"] = availability
    out["icon_url"] = icon_url_for(item)
    # 小组件配置里含密码 / API Key，只把类型下发给前端，凭据留在服务端
    w = item.get("widget") or {}
    out["has_widget"] = bool(w.get("type"))
    out["widget_type"] = w.get("type") or ""
    out.pop("widget", None)
    return out


def auth_token(request: Request):
    auth = request.headers.get("authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def is_authed(request: Request):
    return bool(db.verify_token(auth_token(request)))


def require_admin(request: Request):
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return True


def _err(msg, code=400):
    return JSONResponse(status_code=code, content={"ok": False, "error": msg})


def _fetchable_url(raw, ctx):
    """把 {host} 模板展开成真实可抓取的地址（内网优先），用于抓 favicon。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "{host}" not in raw and "{scheme}" not in raw:
        return raw
    return netutil.expand_url(raw, "lan", ctx) or netutil.expand_url(raw, "wan", ctx)


# ---------------------------------------------------------------- public

@app.on_event("startup")
def _startup():
    db.init()
    # 镜像定时自动检查/自动更新（设置项 image_auto_check）。单进程 uvicorn，不会重复起。
    threading.Thread(target=_auto_scheduler_loop, name="navpage-auto-check", daemon=True).start()


@app.get("/api/health")
def health():
    ok, info = dockerapi.available()
    return {"ok": True, "docker": ok, "docker_info": info, "time": _now()}


LAN_PROBE_TOKEN = "navpage-lan-probe-v1"


@app.get("/api/lanprobe")
def api_lanprobe():
    """前端「内网可达性探测」的靶子：极轻、无鉴权、无信息。

    frp/反代 TCP 直通时服务端看不到手机的真实网络（隧道里 client.host 恒为 frpc 私网地址），
    所以改由浏览器探：够得到内网地址 = 在家，够不到 / 超时 = 在外（逻辑在 static/js/app.js）。
    返回带标记的 JSON 而非 204 空响应，是为了让前端能校验应答来源，排除代理 / 门户页的假阳性。
    """
    return JSONResponse({"probe": LAN_PROBE_TOKEN}, headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store, no-cache, must-revalidate",
    })


# ---------------------------------------------------------------- 容器在线状态灯
# 只探「容器卡片」（container_name 非空）——网站收藏卡片不参与。
# 探的是条目自己的内网直达地址（在 NAS 上发请求，跟访客此刻在内网还是外网无关）；
# 没配内网地址就退回外网地址。状态：ok=正常 / slow=延迟高 / down=连不上。
_STATUS_CACHE = {}          # item_id -> (缓存时间, 结果)
_STATUS_LOCK = threading.Lock()
_STATUS_POOL = None
_STATUS_TTL = 8             # 秒：多个访客/多次轮询共用，别把几十个服务反复打


def _status_probe(url, slow_ms):
    """探测一个地址 -> (state, ms, code, kind)。state: ok / slow / down。

    先试 HTTP(S)：能拿到响应码，耗时也最接近用户真实感受。
    但如果 HTTP 拿不到响应（gRPC 口、只收 TLS、非 HTTP 协议会直接把请求断掉），
    不代表服务死了 —— 再用裸 TCP 连一次，端口能建连就说明服务在监听。
    只有 TCP 也连不上，才算「不可访问」（红点）。

    2026-10-16 为什么要这样：只按 HTTP 判定时，nezha-dashboard(5555 是 gRPC 口)、
    biliup、transmission、amilys-emby 全被判成红点，实测 docker ps 里它们都是 Up、
    TCP 也能连上 —— 假红点比没有点更误导人。
    """
    u = urllib.parse.urlparse(url)
    host = u.hostname or ""
    port = u.port or (443 if u.scheme == "https" else 80)
    t0 = time.monotonic()
    try:
        # trust_env=False：忽略容器里的 HTTP_PROXY 等环境变量，探测直连内网
        with httpx.Client(timeout=httpx.Timeout(2.0, connect=1.2),
                          follow_redirects=True, verify=False, trust_env=False) as c:
            r = c.get(url)
        ms = int((time.monotonic() - t0) * 1000)
        # 5xx = 服务在但报错：给黄点，不是「访问不了」
        if r.status_code >= 500:
            return "slow", ms, r.status_code, "http"
        return ("slow" if ms > slow_ms else "ok"), ms, r.status_code, "http"
    except Exception:
        pass
    t1 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=1.5):
            pass
        ms = int((time.monotonic() - t1) * 1000)
        return ("slow" if ms > slow_ms else "ok"), ms, 0, "tcp"
    except Exception:
        return "down", int((time.monotonic() - t1) * 1000), 0, "none"


@app.get("/api/status")
def api_status(request: Request, ids: str = ""):
    """批量探测容器卡片是否在线。前端按刷新间隔轮询。"""
    s = db.get_settings()
    if s.get("require_login") and not is_authed(request):
        return {"ok": True, "status": {}}
    if not s.get("status_dot"):
        return {"ok": True, "status": {}, "off": True}

    want = [x.strip() for x in (ids or "").split(",") if x.strip().isdigit()]
    if not want:
        return {"ok": True, "status": {}}
    allitems = {str(i["id"]): i for i in db.list_items(include_hidden=False)}
    ctx = netutil.host_context(request, s)
    slow_ms = int(s.get("status_slow_ms") or 1000)

    targets = {}
    for iid in want[:120]:
        it = allitems.get(iid)
        if not it or not (it.get("container_name") or "").strip():
            continue                                  # 网站收藏卡片：不探
        url = ""
        for key, mode in (("url_lan", "lan"), ("url_wan", "wan")):
            raw = (it.get(key) or "").strip()
            if not raw:
                continue
            if raw.startswith(":"):          # ':9091' 这种只写了端口的残缺值，补全模板再展开
                raw = "{scheme}://{host}" + raw
            u = netutil.expand_url(raw, mode, ctx)
            if u.startswith("http://") or u.startswith("https://"):
                url = u
                break
        if url:
            targets[iid] = url

    now = time.time()
    out, todo = {}, []
    with _STATUS_LOCK:
        for iid, url in targets.items():
            hit = _STATUS_CACHE.get(iid)
            if hit and now - hit[0] < _STATUS_TTL and hit[1].get("_url") == url:
                d = dict(hit[1]); d.pop("_url", None); out[iid] = d
            else:
                todo.append((iid, url))

    if todo:
        global _STATUS_POOL
        if _STATUS_POOL is None:
            _STATUS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=24)
        futs = {_STATUS_POOL.submit(_status_probe, u, slow_ms): i for i, u in todo}
        try:
            for f in concurrent.futures.as_completed(futs, timeout=12):
                iid = futs[f]
                try:
                    state, ms, code, kind = f.result()
                except Exception:
                    state, ms, code, kind = "down", 0, 0, "none"
                d = {"state": state, "ms": ms, "code": code, "kind": kind}
                out[iid] = d
                with _STATUS_LOCK:
                    _STATUS_CACHE[iid] = (time.time(), dict(d, _url=targets[iid]))
        except concurrent.futures.TimeoutError:
            pass          # 个别慢的这轮先不给结果，下轮再说

    return {"ok": True, "status": out, "slow_ms": slow_ms, "checked": len(out)}


@app.get("/api/bootstrap")
def bootstrap(request: Request, mode: str = ""):
    s = db.get_settings()
    auto_mode, cip, src = netutil.resolve_mode(request, s)

    # mode 只影响「用内网还是外网链接」，与鉴权无关
    eff = mode if mode in ("lan", "wan") else auto_mode

    authed = is_authed(request)
    if s.get("require_login") and not authed:
        # 未登录：不下发任何条目，前端只渲染登录页
        return {
            "ok": True,
            "locked": True,
            "authed": False,
            "settings": db.public_settings(),
            "network": {
                "mode": eff,
                "auto_mode": auto_mode,
                "client_ip": cip,
                "source": src,
                "host": netutil.host_of(request, bool(s.get("trust_proxy"))),
            },
        }

    cats = db.list_categories()
    ctx = netutil.host_context(request, s)
    items = [resolve_item(i, eff, ctx) for i in db.list_items(include_hidden=False)]
    by_cat = {}
    for it in items:
        by_cat.setdefault(it.get("category_id"), []).append(it)

    groups = []
    for c in cats:
        arr = by_cat.pop(c["id"], [])
        # 空分类也要带出去（2026-10-16 用户反馈「新建的没有卡片的分类在导航页看不见，
        # 没法拖卡片进去」）：原来 `if arr` 直接把空分类丢掉，前端连拖拽落点都没有。
        # 只对已登录的管理员返回，访客页面不出现空壳分组。
        if arr or authed:
            groups.append({"id": c["id"], "name": c["name"], "icon": c["icon"],
                           "card_size": c.get("card_size") or "", "items": arr})
    leftovers = []
    for cid, arr in by_cat.items():
        leftovers.extend(arr)
    if leftovers:
        groups.append({"id": None, "name": "未分类", "icon": "", "items": leftovers})

    return {
        "ok": True,
        "locked": False,
        "authed": authed,
        "settings": db.public_settings(),
        "network": {
            "mode": eff,
            "auto_mode": auto_mode,
            "client_ip": cip,
            "source": src,
            "lan_cidrs": s.get("lan_cidrs"),
            "auto": bool(s.get("auto_switch")),
            "host": ctx.get("host"),
            "host_private": ctx.get("host_private"),
            "lan_base": ctx.get("lan_base"),
            "wan_base": ctx.get("wan_base"),
        },
        "groups": groups,
        "count": len(items),
    }


@app.get("/api/widgets")
def api_widgets(request: Request, ids: str = ""):
    """批量取卡片小组件数据。需要登录（否则会泄露媒体库/下载统计）。"""
    if not is_authed(request):
        raise HTTPException(401, "未登录")
    s = db.get_settings()
    if not s.get("widgets_enabled"):
        return {"ok": True, "widgets": {}, "disabled": True}

    want = None
    if ids.strip():
        try:
            want = {int(x) for x in ids.split(",") if x.strip()}
        except ValueError:
            want = None

    items = [i for i in db.list_items(include_hidden=False) if i.get("widget")]
    if want is not None:
        items = [i for i in items if i["id"] in want]

    ttl = max(5, int(s.get("widget_interval") or 60))
    return {"ok": True, "widgets": widgets.fetch_many(items, ttl=ttl)}


@app.post("/api/admin/widget/test")
def admin_widget_test(body: dict, _=Depends(require_admin)):
    """管理界面「测试连接」：不走缓存，立刻返回真实结果。"""
    cfg = body.get("widget") if isinstance(body, dict) else None
    if not isinstance(cfg, dict):
        return _err("缺少 widget 配置")
    return {"ok": True, "result": widgets.test(cfg)}


@app.get("/api/admin/widget/providers")
def admin_widget_providers(_=Depends(require_admin)):
    return {"ok": True, "providers": widgets.provider_list()}


@app.get("/api/wallpaper/{name}")
def wallpaper(name: str):
    name = os.path.basename(name)
    path = os.path.join(db.WALLPAPER_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/logo/{name}")
def logo_file(name: str):
    """左上角自定义图标（外观页上传）。故意不鉴权：登录页上也要显示它。"""
    name = os.path.basename(name)
    path = os.path.join(db.LOGO_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/wallpapers")
def wallpapers():
    d = db.WALLPAPER_DIR
    if not os.path.isdir(d):
        return {"ok": True, "files": []}
    files = []
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if os.path.isfile(p):
            files.append({"name": f, "size": os.path.getsize(p),
                          "mtime": int(os.path.getmtime(p)),
                          "url": "/api/wallpaper/" + quote(f)})
    return {"ok": True, "files": files}


@app.get("/api/iconfile/{name}")
def iconfile(name: str):
    p = icons.icon_path(name)
    if not p:
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type=icons.guess_mime(name),
                        headers={"Cache-Control": "public, max-age=604800"})


def _cached_by_url(url):
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    d = db.ICON_DIR
    if not os.path.isdir(d):
        return None
    for f in os.listdir(d):
        if f.startswith(key + "."):
            return f
    return None


def fetch_and_cache_icon(url, force=False):
    if not force:
        hit = _cached_by_url(url)
        if hit:
            return hit
    f, why = icons.fetch_icon_url(url)
    if not f:
        return None
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    ext = f.rsplit(".", 1)[-1]
    new = key + "." + ext
    src = os.path.join(db.ICON_DIR, f)
    dst = os.path.join(db.ICON_DIR, new)
    try:
        os.replace(src, dst)
    except Exception:
        return None
    return new


@app.get("/api/icon")
def icon_proxy(url: str):
    if not url:
        raise HTTPException(400, "missing url")
    hit = _cached_by_url(url)
    if hit:
        return FileResponse(os.path.join(db.ICON_DIR, hit),
                            media_type=icons.guess_mime(hit),
                            headers={"Cache-Control": "public, max-age=604800"})
    name = fetch_and_cache_icon(url)
    if not name:
        raise HTTPException(404, "icon not available")
    return FileResponse(os.path.join(db.ICON_DIR, name),
                        media_type=icons.guess_mime(name),
                        headers={"Cache-Control": "public, max-age=604800"})


# ---------------------------------------------------------------- auth

class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginBody):
    s = db.get_settings()
    if not s.get("admin_password_hash"):
        raise HTTPException(400, "尚未设置管理员密码")
    if not db.check_password(body.password):
        raise HTTPException(401, "密码错误")
    return {"ok": True, "token": db.make_token()}


@app.post("/api/admin/password")
def set_password(request: Request, body: LoginBody):
    """首次设置密码（无密码时无需鉴权），或已登录后修改。"""
    s = db.get_settings()
    if s.get("admin_password_hash"):
        require_admin(request)
    if len(body.password or "") < 4:
        raise HTTPException(400, "密码至少 4 位")
    db.set_password(body.password)
    return {"ok": True, "token": db.make_token()}


# ---------------------------------------------------------------- admin: state

@app.get("/api/admin/state")
def admin_state(request: Request, _=Depends(require_admin)):
    s = db.get_settings()
    ok, info = dockerapi.available()
    safe = dict(s)
    safe.pop("admin_password_hash", None)
    safe.pop("secret", None)
    ctx = netutil.host_context(request, s)
    return {
        "ok": True,
        "items": [{**resolve_item(i, "lan", ctx), "widget": i.get("widget") or {}}
                  for i in db.list_items(include_hidden=True)],
        "categories": db.list_categories(),
        "settings": safe,
        "docker": {"ok": ok, **info},
        "icons": icons.list_icons(),
        "net": {
            "host": ctx.get("host"),
            "lan_base": ctx.get("lan_base"),
            "wan_base": ctx.get("wan_base"),
            "container_ip": db.detect_host_ip(),
        },
    }


# ---------------------------------------------------------------- admin: items

class ItemBody(BaseModel):
    name: str | None = None
    description: str | None = None
    url_lan: str | None = None
    url_wan: str | None = None
    icon: str | None = None
    category_id: int | None = None
    sort: int | None = None
    hidden: bool | None = None
    source: str | None = None
    container_name: str | None = None
    container_image: str | None = None
    container_port: int | None = None
    widget: dict | None = None
    url_visible: int | None = None
    card_size: str | None = None


@app.post("/api/admin/items")
def create_item(body: ItemBody, _=Depends(require_admin)):
    data = body.model_dump(exclude_unset=True)
    if not data.get("name"):
        return _err("名称不能为空")
    iid = db.create_item(data)
    return {"ok": True, "id": iid, "item": resolve_item(db.get_item(iid), "lan")}


@app.put("/api/admin/items/{iid}")
def update_item(iid: int, body: ItemBody, _=Depends(require_admin)):
    if not db.get_item(iid):
        return _err("条目不存在", 404)
    db.update_item(iid, body.model_dump(exclude_unset=True))
    return {"ok": True, "item": resolve_item(db.get_item(iid), "lan")}


@app.delete("/api/admin/items/{iid}")
def delete_item(iid: int, _=Depends(require_admin)):
    db.delete_item(iid)
    return {"ok": True}


class ExcludeBody(BaseModel):
    excluded: bool = True


@app.post("/api/admin/items/{iid}/exclude")
def exclude_item(iid: int, body: ExcludeBody, _=Depends(require_admin)):
    """排除 / 恢复一个条目。

    排除要同时做两件事，缺一不可：
      1. `hidden=1` → 首页不再渲染这张卡片（bootstrap 用 include_hidden=False）
      2. 容器名写进 `scan_exclude` → 重新扫描 / 全量同步时不会又把它导入回来

    只做第 1 步的话，下次点「全量同步」它就复活了；只做第 2 步的话卡片还挂在首页上。
    手建条目（没有 container_name）只做第 1 步。
    """
    it = db.get_item(iid)
    if not it:
        return _err("条目不存在", 404)
    s = db.get_settings()
    names = [x.strip() for x in (s.get("scan_exclude") or "").split(",") if x.strip()]
    cname = (it.get("container_name") or "").strip()
    if body.excluded:
        db.update_item(iid, {"hidden": True})
        if cname and cname not in names:
            names.append(cname)
    else:
        db.update_item(iid, {"hidden": False})
        if cname and cname in names:
            names.remove(cname)
    db.update_settings({"scan_exclude": ",".join(names)})
    return {"ok": True, "excluded": bool(body.excluded),
            "item": resolve_item(db.get_item(iid), "lan"),
            "scan_exclude": ",".join(names)}


class OrderBody(BaseModel):
    order: list


@app.post("/api/admin/items/reorder")
def reorder(body: OrderBody, _=Depends(require_admin)):
    db.reorder_items(body.order or [])
    return {"ok": True}


# ---------------------------------------------------------------- admin: categories

class CategoryBody(BaseModel):
    name: str | None = None
    icon: str | None = None
    sort: int | None = None
    card_size: str | None = None


@app.post("/api/admin/categories")
def create_cat(body: CategoryBody, _=Depends(require_admin)):
    if not (body.name or "").strip():
        return _err("分类名不能为空")
    cid = db.create_category(body.name, body.icon or "", body.sort)
    return {"ok": True, "id": cid}


@app.put("/api/admin/categories/{cid}")
def update_cat(cid: int, body: CategoryBody, _=Depends(require_admin)):
    db.update_category(cid, {k: v for k, v in body.dict().items() if v is not None})
    return {"ok": True}


@app.delete("/api/admin/categories/{cid}")
def delete_cat(cid: int, _=Depends(require_admin)):
    db.delete_category(cid)
    return {"ok": True}


class CatBulkBody(BaseModel):
    ids: list[int] | None = None


@app.post("/api/admin/categories/bulk_delete")
def bulk_delete_cats(body: CatBulkBody, _=Depends(require_admin)):
    """批量删除分类。其下条目不会被删，只会变成「未分类」。"""
    ids = [int(x) for x in (body.ids or [])]
    if not ids:
        return _err("未选择分类")
    n_cat, n_item = db.delete_categories(ids)
    return {"ok": True, "deleted": n_cat, "unassigned": n_item}


@app.post("/api/admin/categories/reorder")
def reorder_cats(body: OrderBody, _=Depends(require_admin)):
    db.reorder_categories(body.order or [])
    return {"ok": True}


# ---------------------------------------------------------------- admin: 拖拽布局

class LayoutBody(BaseModel):
    rows: list


@app.post("/api/admin/layout")
def save_layout(body: LayoutBody, _=Depends(require_admin)):
    """卡片拖拽后的落库：位置 + 所属分组一次写完。"""
    db.apply_layout(body.rows or [])
    return {"ok": True, "n": len(body.rows or [])}


# ---------------------------------------------------------------- admin: 镜像更新

_img_cache = {"ts": 0.0, "rows": [], "err": ""}


# 上次成功查到的「是否最新」结果。为什么留它：Docker Hub 按 IP 限流，
# 一次查几十个镜像很容易整片 429；那时若把状态抹成「未知」，用户看到的就是
# 「一直显示不出来更新」（2026-10-15 实测 40/57 被限流）。有这份记录就能沿用上次结果。
_IMG_STATE_FILE = os.path.join(db.DATA_DIR, "image_state.json")


def _img_state_load():
    try:
        with open(_IMG_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _img_state_save(d):
    try:
        tmp = _IMG_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _IMG_STATE_FILE)
    except Exception:
        pass


def _image_rows(force=False):
    """比对本地镜像摘要与 registry 最新摘要。结果按 image_check_cache 秒缓存。"""
    s = db.get_settings()
    ttl = int(s.get("image_check_cache") or 1800)
    now = time.time()
    if not force and _img_cache["rows"] and (now - _img_cache["ts"]) < ttl:
        return _img_cache["rows"], _img_cache["err"], True

    proxy = (s.get("image_proxy") or "").strip() or None
    # 镜像源：判断 Docker Hub 镜像「是否最新」优先走它（直连、不占匿名配额）
    mirror = (s.get("image_mirror") or "").strip() or None
    state = _img_state_load()
    try:
        local = dockerapi.image_digests()
    except Exception as e:
        return [], f"读不到本地镜像列表：{e}", False

    try:
        cs = dockerapi.containers(all_containers=True)
    except Exception as e:
        return [], f"读不到容器列表：{e}", False

    images = sorted({c.get("image") or "" for c in cs if c.get("image")})
    remote = {}

    # 本地 build 出来的镜像（从没进过 registry → RepoDigests 为空）不可能「有新版本」，
    # 别去 registry 白问：既浪费 Docker Hub 的匿名配额，又会让它们显示成「未知/错误」
    #（2026-10-15 实测：18 个本地镜像都在撞 401/403）。
    need = [img for img in images if (local.get(img) or {}).get("digests")]

    def _one(img):
        return img, registry.remote_digest(img, proxy, mirror)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        for img, res in ex.map(_one, need):
            remote[img] = res

    rows = []
    for c in cs:
        img = c.get("image") or ""
        li = local.get(img) or {}
        ld = registry.local_digest_for(li.get("digests"), img)
        rd, err = remote.get(img, ("", "未检查"))
        row_err = err
        if not ld:
            # 本地构建的镜像：状态单列一档，前端据此显示「本地镜像 / 重建」
            status = "local"
            row_err = ""
            rows.append({
                "container": c.get("name"), "id": c.get("id"), "image": img,
                "state": c.get("state"), "project": c.get("compose_project"),
                "status": status, "local_digest": "", "remote_digest": "",
                "error": "", "has_repo_digest": False,
            })
            continue
        if err:
            # 查不到时别把状态抹成「未知」：沿用上次成功的结果，只在行里标注本次失败。
            prev = state.get(img) or {}
            if prev.get("status") in ("current", "outdated") and prev.get("local"):
                status = prev["status"]
                ld = prev.get("local") or ld
                rd = prev.get("remote") or rd
                row_err = f"本次检查失败（{err}）；以下是上次结果"
            else:
                status = "unknown"
        elif not ld:
            status = "unknown"
            row_err = ""
        elif registry.normalize(rd) == ld:
            status = "current"
            row_err = ""
        else:
            status = "outdated"
            row_err = ""
        if not err and status in ("current", "outdated"):
            state[img] = {"status": status, "ts": time.time(),
                          "local": ld, "remote": registry.normalize(rd)}
        rows.append({
            "container": c.get("name"),
            "id": c.get("id"),
            "image": img,
            "state": c.get("state"),
            "project": c.get("compose_project"),
            "status": status,
            "local_digest": ld[:19],
            "remote_digest": registry.normalize(rd)[:19],
            "has_repo_digest": True,
            "error": row_err,
        })
    rows.sort(key=lambda r: ({"outdated": 0, "unknown": 1, "current": 2, "local": 3}[r["status"]],
                             r.get("project") or "~", r.get("container") or ""))
    _img_state_save(state)
    err_all = ""
    n_bad = sum(1 for r in rows if r.get("error"))
    if rows and n_bad >= max(3, len(rows) // 2):
        blob = " ".join(str(r.get("error") or "") for r in rows)
        if "429" in blob:
            if (s.get("image_mirror") or "").strip():
                err_all = ("Docker Hub 在限流（HTTP 429）：匿名查询配额用完了，"
                           "填的镜像源也没能兜住。换一个镜像源，或等几小时再检查。")
            else:
                err_all = ("Docker Hub 在限流（HTTP 429）：这台机器的匿名查询配额用完了。"
                           "去「设置 → 镜像」填一个国内镜像源（如 https://docker.1panel.live），"
                           "判断「是否最新」会直连镜像站、不占配额，通常 1~3 秒返回。")
        else:
            err_all = ("大部分镜像都查不到，通常是外网不通。"
                       "去「设置 → 镜像」填一个代理或镜像源（宿主机的 docker 也可能走代理）。")
    _img_cache.update({"ts": now, "rows": rows, "err": err_all})
    return rows, err_all, False


@app.get("/api/admin/images")
def admin_images(refresh: int = 0, _=Depends(require_admin)):
    s = db.get_settings()
    rows, err, cached = _image_rows(force=bool(refresh))
    # 行是按 image_check_cache 秒（默认 1800）缓存的：这期间容器被删掉/重建，
    # 缓存里就会留一条「幽灵行」——点它的「更新」只会得到「容器不存在」，
    # 看着就像功能坏了（2026-10-15 自查时踩到）。所以每次响应都按实时容器列表过一遍。
    try:
        live = {c.get("name") for c in dockerapi.containers(all_containers=True)}
        rows = [r for r in rows if r.get("container") in live]
    except Exception:
        pass  # 拿不到实时列表就退回缓存，别把整个页面弄挂
    # local_only 每次响应都现算：它是进程内记忆（第一次拉取撞 404 才学到），
    # 塞进被缓存 30 分钟的 rows 里会长时间显示不出来。
    for r in rows:
        # 本地构建 = 没有 RepoDigests（权威判据，第一次就准），或之前撞过 404 记住的
        r["local_only"] = (not r.get("has_repo_digest", True)) or dockerapi.is_local_only(r.get("image") or "")
    n_out = sum(1 for r in rows if r["status"] == "outdated")
    hist = _hist_list()
    return {"ok": True, "rows": rows, "error": err, "cached": cached,
            "outdated": n_out, "last": (hist[0] if hist else None),
            "allow_update": bool(s.get("allow_image_update")),
            "proxy": bool((s.get("image_proxy") or "").strip()),
            "mirror": (s.get("image_mirror") or "").strip(),
            "auto": _auto_status(s)}


# ---------------------------------------------------------------- 更新历史
# 以前更新失败了刷新一下什么都不剩，用户只能说「还是有问题」而拿不到原因
# （2026-10-15 用户反馈）。现在把最近 30 次结果落盘：
# 「镜像」页显示一句「上次：xxx 成功/失败」，排查时 GET /api/admin/images/history 看全文。
_HIST_FILE = os.path.join(db.DATA_DIR, "update_history.json")


def _hist_add(rec):
    try:
        arr = []
        if os.path.exists(_HIST_FILE):
            with open(_HIST_FILE, "r", encoding="utf-8") as f:
                arr = json.load(f) or []
        arr.insert(0, rec)
        with open(_HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(arr[:30], f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _hist_list():
    try:
        if os.path.exists(_HIST_FILE):
            with open(_HIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or []
    except Exception:
        pass
    return []


class ImageUpdateBody(BaseModel):
    name: str
    pull: bool = True


# 最近一次更新任务（单用户面板，只留一个够用）。
# 为什么改成后台线程 + 轮询：拉镜像可能要几分钟，同步请求期间前端完全看不到进展
# （用户 2026-10-15 的诉求）。后台跑还有个附带好处：浏览器关掉更新也不会中断。
_TASK_LOCK = threading.Lock()
_CUR_TASK = {"t": None}


def _run_update_task(t, name, pull):
    """真正干活的那段（跑在自己的线程里）。手动点更新和定时自动更新共用。"""
    ok, msg = False, ""
    try:
        ok, msg, _log = dockerapi.recreate_container(name, task=t, do_pull=bool(pull))
    except Exception as e:
        msg = f"内部错误：{type(e).__name__}: {e}"
        t.line(msg)
    t.finish(ok, msg)
    _img_cache["ts"] = 0.0   # 更新完失效镜像缓存，下次「检查更新」重新比对
    _hist_add({"ts": int(time.time()), "container": name, "image": t.image or "",
               "success": bool(ok), "message": msg,
               "elapsed": round(time.time() - t.started, 1),
               "local_only": dockerapi.is_local_only(t.image or ""),
               "log": list(t.log)[-14:]})


def _spawn_update(name, pull=True):
    """起一个更新任务 → (ok, Task 或错误文案)。同一时刻全局只允许一个任务。

    先落一条「已开始」：更新面板自身时进程会被重建，收尾那条永远写不进去，
    前端就只能看到一个连不上的进度接口（用户 2026-10-15 反馈的那类「有问题」）。
    """
    name = (name or "").strip()
    if not name:
        return False, "缺容器名"
    with _TASK_LOCK:
        cur = _CUR_TASK["t"]
        if cur is not None and cur.running:
            return False, f"{cur.container} 正在更新中（{cur.step}），等它结束再试"
        t = dockerapi.Task(name)
        _CUR_TASK["t"] = t
    _hist_add({"ts": int(time.time()), "container": name, "image": "",
               "success": None, "message": "已开始，尚未收到结果", "elapsed": None,
               "local_only": False, "log": []})
    threading.Thread(target=_run_update_task, args=(t, name, bool(pull)),
                     name="navpage-image-update", daemon=True).start()
    return True, t


@app.post("/api/admin/images/update")
def admin_image_update(body: ImageUpdateBody, _=Depends(require_admin)):
    """只负责「发起」：起线程干活，立刻返回。进度走 /update/status 轮询。"""
    s = db.get_settings()
    if not s.get("allow_image_update"):
        return _err("镜像更新开关没打开。去「镜像」标签页打开（需要 socket-proxy 放开 POST 权限）")
    ok, t = _spawn_update(body.name, pull=bool(body.pull))
    if not ok:
        return _err(str(t))
    return {"ok": True, "started": True, "container": t.container}


# ---------------------------------------------------------------- 清理未使用镜像
# 2026-10-16 用户要求：镜像页能定时清理未使用镜像（cron）。设计取舍：
#  - 默认只清「悬空镜像」（无 RepoTags 的 <none>，构建/拉取留下的中间层），最安全，
#    docker 官方 `image prune` 默认也是这个范围。
#  - 可切「未被容器使用的镜像」（有标签但没有任何容器用），等价 `image prune -a`：
#    会删掉「拉过还没跑」和「上一版备份」，所以必须配预览 + 白名单。
#  - 「被使用」只认 /containers/json?all=true 的 ImageID（含已停止容器）。不用镜像
#    自带的 containers 字段 —— 那只算运行中的，会把「被停止容器占着」的镜像误判成可删，
#    结果预览说能删、执行全 409。

_PRUNE_LOCK = threading.Lock()


def _prune_keep_patterns(s=None):
    s = s or db.get_settings()
    return [str(x).strip() for x in (s.get("image_prune_keep") or []) if str(x).strip()]


def _keep_hit(img, pats):
    """白名单命中就返回命中的那条（用于提示），否则空串。

    支持：repo:tag 精确/前缀、镜像 ID 前缀、以及带 * 的 glob。
    """
    tags = img.get("tags") or []
    iid = (img.get("id") or "").replace("sha256:", "")
    for pat in pats:
        low = pat.lower()
        if "*" in low:
            rx = re.compile("^" + re.escape(low).replace(r"\*", ".*") + "$")
            if any(rx.match(t.lower()) for t in tags) or rx.match(iid):
                return pat
            continue
        for t in tags:
            if t.lower() == low or t.lower().startswith(low):
                return pat
        if iid.startswith(low):
            return pat
    return ""


def _prune_candidates(mode=None, s=None):
    """-> (items, err, mode)。items: [{id, tags, size, created, dangling, reason}]"""
    s = s or db.get_settings()
    mode = (mode or s.get("image_prune_mode") or "dangling").strip()
    if mode not in ("dangling", "unused"):
        mode = "dangling"
    try:
        imgs = dockerapi.images_all()
        used = dockerapi.container_image_ids()
    except Exception as e:
        return [], f"读不到镜像或容器列表：{type(e).__name__}: {e}", mode
    pats = _prune_keep_patterns(s)
    out = []
    for im in imgs:
        if im["id"] in used:
            continue                              # 有容器（含已停止）在用
        if mode == "dangling" and not im["dangling"]:
            continue                              # 只清悬空：有标签的一律留着
        if _keep_hit(im, pats):
            continue
        out.append(dict(im, reason="无标签悬空镜像" if im["dangling"] else "没有容器使用"))
    out.sort(key=lambda x: -x["size"])
    return out, "", mode


def _prune_summary(rec):
    parts = []
    if rec.get("error"):
        parts.append("出错：" + str(rec["error"])[:160])
    parts.append("清理 %d 个镜像，释放 %s" % (len(rec.get("removed") or []),
                                            _fmt_size(rec.get("freed") or 0)))
    if rec.get("failed"):
        parts.append("失败 %d 个" % len(rec["failed"]))
    if rec.get("skipped"):
        parts.append(rec["skipped"])
    return "；".join(parts)


def _fmt_size(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%.0f%s" % (n, u)) if u == "B" else ("%.1f%s" % (n, u))
        n /= 1024


def _prune_run(mode=None, force=False, auto=False):
    """跑一次清理 -> 结果 dict。手动按钮和定时任务共用，结果都落 _SCHED + 历史。"""
    t0 = time.time()
    rec = {"ts": int(time.time()), "mode": "", "auto": bool(auto), "ok": True,
           "candidates": 0, "removed": [], "failed": [], "freed": 0,
           "error": "", "elapsed": 0, "skipped": ""}
    if not _PRUNE_LOCK.acquire(blocking=False):
        rec["ok"] = False
        rec["error"] = "上一次清理还在跑，稍后再试"
        return rec
    try:
        try:
            before = sum(i["size"] for i in dockerapi.images_all())
        except Exception:
            before = None
        items, err, mode = _prune_candidates(mode)
        rec["mode"] = mode
        rec["candidates"] = len(items)
        if err:
            rec["ok"] = False
            rec["error"] = err
        elif not items:
            rec["skipped"] = "没有可清理的镜像（%s）" % ("悬空镜像" if mode == "dangling" else "未被容器使用的镜像")
        else:
            for im in items:
                label = im["tags"][0] if im["tags"] else (im["id"] or "")[:19]
                ok, msg = dockerapi.image_remove(im["id"], force=force)
                if ok:
                    rec["removed"].append({"name": label, "size": im["size"], "msg": msg})
                else:
                    rec["failed"].append("%s（%s）" % (label, msg))
            # 释放量用「清理前后镜像总字节差」算，比把删除项体积相加准
            # （镜像共享层，逐个相加会高估）。
            try:
                after = sum(i["size"] for i in dockerapi.images_all())
                rec["freed"] = max(0, before - after) if before is not None else 0
            except Exception:
                rec["freed"] = sum(r["size"] for r in rec["removed"])
            rec["ok"] = not rec["failed"]
        rec["freed_text"] = _fmt_size(rec["freed"])
    except Exception as e:
        rec["ok"] = False
        rec["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        rec["elapsed"] = round(time.time() - t0, 1)
        try:
            _PRUNE_LOCK.release()
        except Exception:
            pass
    with _SCHED_LOCK:
        _SCHED["last_prune"] = rec
    _hist_add({"ts": rec["ts"], "container": "(定时清理镜像)" if auto else "(手动清理镜像)",
               "image": "", "success": bool(rec["ok"]), "elapsed": rec["elapsed"],
               "local_only": False, "message": _prune_summary(rec), "log": []})
    return rec


@app.get("/api/admin/images/unused")
def admin_images_unused(mode: str = "", _=Depends(require_admin)):
    """预览「会被清理哪些镜像」，不会真删。前端清理前的确认清单就用它。"""
    s = db.get_settings()
    items, err, mode = _prune_candidates(mode)
    if err:
        return _err(err)
    return {"ok": True, "mode": mode, "items": items, "count": len(items),
            "bytes": sum(i["size"] for i in items),
            "bytes_text": _fmt_size(sum(i["size"] for i in items)),
            "keep": _prune_keep_patterns(s),
            "allow": bool(s.get("allow_image_update"))}


class PruneBody(BaseModel):
    mode: str = ""
    force: bool = False


@app.post("/api/admin/images/prune")
def admin_images_prune(body: PruneBody, _=Depends(require_admin)):
    """立即清理一次（按钮用）。定时清理走 _auto_scheduler_loop → _prune_run(auto=True)。"""
    s = db.get_settings()
    if not s.get("allow_image_update"):
        return _err("面板的「允许从面板更新镜像 / 管理容器」还关着：清理镜像要发 DELETE，"
                    "先把它打开（同一个 socket-proxy 开关，不需要额外改代理配置）。")
    rec = _prune_run(mode=body.mode, force=body.force)
    return {"ok": not rec.get("error"), "error": rec.get("error") or "", "result": rec}


@app.get("/api/admin/images/auto")
def admin_images_auto(_=Depends(require_admin)):
    """定时检查的状态：开关 / cron / 下次运行 / 上次结果（设置页显示用）。"""
    return {"ok": True, "auto": _auto_status()}


@app.get("/api/admin/images/history")
def admin_image_history(_=Depends(require_admin)):
    """最近 30 次更新结果（含失败原因全文），刷新页面也不会丢。"""
    return {"ok": True, "items": _hist_list()}


@app.get("/api/admin/images/update/status")
def admin_image_update_status(since: int = 0, _=Depends(require_admin)):
    """轮询取进度。since=已收到的日志行数，只回增量。"""
    with _TASK_LOCK:
        t = _CUR_TASK["t"]
    if t is None:
        return {"ok": True, "idle": True, "running": False, "log": [], "n": 0}
    snap = t.snapshot(since)
    snap["ok"] = True
    snap["idle"] = False
    return snap


# ---------------------------------------------------------------- admin: settings

class SettingsBody(BaseModel):
    patch: dict


@app.put("/api/admin/settings")
def update_settings(body: SettingsBody, _=Depends(require_admin)):
    s = db.update_settings(body.patch or {})
    safe = dict(s)
    safe.pop("admin_password_hash", None)
    safe.pop("secret", None)
    return {"ok": True, "settings": safe}


@app.post("/api/admin/wallpaper")
async def upload_wallpaper(file: UploadFile = File(...), _=Depends(require_admin)):
    raw = await file.read()
    if len(raw) > 25 * 1024 * 1024:
        return _err("图片不能超过 25MB")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"):
        return _err("仅支持 jpg/png/webp/gif/avif/bmp")
    os.makedirs(db.WALLPAPER_DIR, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(file.filename or "wall")[0])[:60]
    name = f"{safe_name}_{int(time.time())}{ext}"
    with open(os.path.join(db.WALLPAPER_DIR, name), "wb") as f:
        f.write(raw)
    return {"ok": True, "name": name, "url": "/api/wallpaper/" + quote(name)}


@app.post("/api/admin/logo")
async def upload_logo(file: UploadFile = File(...), _=Depends(require_admin)):
    """上传左上角图标。存数据目录的 logo/（compose 挂载的 ./data），重建镜像不会丢。

    只保存，不改设置：URL 回给前端，由前端写进 site_logo（和壁纸上传一致的做法），
    这样「上传了但没保存」不会把线上图标偷偷换掉。
    """
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        return _err("图标不能超过 5MB")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".avif", ".bmp"):
        return _err("仅支持 png / jpg / webp / gif / svg / ico / avif / bmp")
    os.makedirs(db.LOGO_DIR, exist_ok=True)
    # 同名文件反复上传会越堆越多，这里每次覆盖同一个名字（浏览器靠 ?v= 刷新）
    name = "logo" + ext
    with open(os.path.join(db.LOGO_DIR, name), "wb") as f:
        f.write(raw)
    return {"ok": True, "name": name, "url": "/api/logo/" + quote(name)}


@app.delete("/api/admin/wallpaper/{name}")
def delete_wallpaper(name: str, _=Depends(require_admin)):
    name = os.path.basename(name)
    p = os.path.join(db.WALLPAPER_DIR, name)
    if os.path.isfile(p):
        os.remove(p)
    s = db.get_settings()
    if s.get("wallpaper") == name:
        db.update_settings({"wallpaper": ""})
    return {"ok": True}


# ---------------------------------------------------------------- 图标：本地库 + 在线库

class IconUseBody(BaseModel):
    name: str = ""
    variant: str = "svg"
    lib: str = ""          # 来源库（owner/repo），留空则跨所有库找


class IconLibBody(BaseModel):
    url: str = ""          # owner/repo 或 GitHub 仓库/目录地址
    label: str = ""        # 备注名（可选）
    branch: str = ""       # 分支（可选，留空自动取仓库默认分支）


class IconLibIdBody(BaseModel):
    id: str = ""           # owner/repo；留空表示「全部库」


@app.get("/api/admin/icons")
def admin_icons(_=Depends(require_admin)):
    """本地图标库：/data/icons 下的全部文件（上传的 + 从在线库下载的 + 自动抓的 favicon）。"""
    d = db.ICON_DIR
    files = []
    if os.path.isdir(d):
        for f in os.listdir(d):
            p = os.path.join(d, f)
            if not os.path.isfile(p):
                continue
            files.append({
                "name": f,
                "size": os.path.getsize(p),
                "mtime": int(os.path.getmtime(p)),
                "kind": iconlib.kind_of(f),
                "url": "/api/iconfile/" + quote(f, safe=""),
            })
    # 上传的排最前，其次从在线库下载的，哈希命名的自动缓存垫底
    order = {"upload": 0, "library": 1, "cache": 2}
    files.sort(key=lambda x: (order.get(x["kind"], 3), -x["mtime"]))
    return {"ok": True, "files": files, "count": len(files)}


@app.post("/api/admin/icon")
async def upload_icon(file: UploadFile = File(...), _=Depends(require_admin)):
    """上传图标 → /data/icons/up_<名字>_<时间戳>.<ext>。

    落在数据目录（compose 挂载的 ./data）里，重建镜像也不会丢，
    所有卡片的图标选择器都能看到并复用。
    """
    raw = await file.read()
    if not raw:
        return _err("文件为空")
    if len(raw) > 2 * 1024 * 1024:
        return _err("图标不能超过 2MB")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".bmp", ".avif"):
        return _err("仅支持 png/jpg/webp/gif/svg/ico/bmp/avif")
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(file.filename or "icon")[0])[:48] or "icon"
    name = "up_%s_%d%s" % (stem, int(time.time()), ext)
    os.makedirs(db.ICON_DIR, exist_ok=True)
    with open(os.path.join(db.ICON_DIR, name), "wb") as f:
        f.write(raw)
    return {"ok": True, "name": name, "url": "/api/iconfile/" + quote(name, safe="")}


@app.delete("/api/admin/icon/{name}")
def delete_icon(name: str, _=Depends(require_admin)):
    """删除本地图标文件。引用它的卡片不受影响，只是下次渲染退回首字母占位。"""
    name = os.path.basename(name or "")
    if not name:
        return _err("缺少文件名")
    p = os.path.join(db.ICON_DIR, name)
    if os.path.isfile(p):
        os.remove(p)
    return {"ok": True}


@app.get("/api/admin/iconlibraries")
def admin_icon_libraries(ensure: int = 0, _=Depends(require_admin)):
    """已配置的在线图标库（GitHub 公开仓库，可多个）+ 各自的清单缓存状态。

    ensure=1 时顺手把缺清单 / 过期的库拉一遍（前端打开面板时用，别让首屏空着）。
    """
    return {"ok": True, "items": iconlib.list_libs(ensure=bool(ensure))}


@app.post("/api/admin/iconlibraries")
def admin_icon_library_add(body: IconLibBody, _=Depends(require_admin)):
    """添加一个在线图标库。

    地址支持 owner/repo、https://github.com/owner/repo、.../tree/分支[/子目录]。
    加完立刻拉一次清单，用户马上就能搜到；拉不动也照样保存（给出 warning，可稍后重试）。
    """
    try:
        lib = iconlib.parse_lib_input(body.url, body.label, body.branch)
    except ValueError as e:
        return _err(str(e))
    libs = iconlib.get_libs()
    if any(l["repo"].lower() == lib["repo"].lower() for l in libs):
        return _err("这个仓库已经添加过了：%s" % lib["repo"])
    if not lib["branch"]:
        # 没写分支就问一下仓库默认分支，别硬套 main（master 的仓库不少）
        lib["branch"] = iconlib.default_branch(lib["repo"])
    libs.append(lib)
    iconlib.save_libs(libs)
    warn = ""
    try:
        iconlib.refresh(lib)
    except Exception as e:
        warn = str(e) if isinstance(e, RuntimeError) else "清单拉取失败：%s" % e
    return {"ok": True, "items": iconlib.list_libs(), "added": lib["repo"], "warning": warn}


@app.delete("/api/admin/iconlibraries")
def admin_icon_library_del(id: str = "", _=Depends(require_admin)):
    """移除一个在线图标库（本地已下载的图标不受影响，仍留在 data/icons/）。"""
    rid = (id or "").strip()
    if not rid:
        return _err("缺少库标识（owner/repo）")
    libs = iconlib.get_libs()
    keep = [l for l in libs if l["repo"].lower() != rid.lower()]
    if len(keep) == len(libs):
        return _err("没有配置这个图标库：%s" % rid)
    iconlib.save_libs(keep)
    return {"ok": True, "items": iconlib.list_libs()}


@app.post("/api/admin/iconlibraries/refresh")
def admin_icon_library_refresh(body: IconLibIdBody, _=Depends(require_admin)):
    """重拉清单（id 留空 = 全部库）。清单缓存 7 天过期，平时按需自动拉。"""
    rid = (body.id or "").strip()
    libs = [l for l in iconlib.get_libs() if not rid or l["repo"].lower() == rid.lower()]
    if not libs:
        return _err("没有配置这个图标库：%s" % (rid or "（空）"))
    counts, errs = {}, []
    for l in libs:
        try:
            items, _at = iconlib.refresh(l)
            counts[l["repo"]] = len(items)
        except Exception as e:
            errs.append("%s：%s" % (l["label"], str(e) if isinstance(e, RuntimeError) else e))
    if errs and not counts:
        return _err("；".join(errs))
    return {"ok": True, "items": iconlib.list_libs(), "counts": counts, "warning": "；".join(errs)}


@app.get("/api/admin/iconlibrary")
def icon_library(q: str = "", limit: int = 60, variant: str = "svg", offset: int = 0,
                 lib: str = "", refresh: int = 0, _=Depends(require_admin)):
    """搜索在线图标库（可配多个 GitHub 公开仓库，默认 homarr-labs/dashboard-icons）。

    - q 为空：先给内置常用表里的图标，后面接库里其余全部图标（配 offset 可「加载更多」翻完）；
    - lib：只搜某一个库（owner/repo），留空 = 全部库；
    - 清单走磁盘缓存（7 天过期），缺缓存时按需拉一次；refresh=1 强制重拉，失败就退回旧缓存。
    """
    try:
        if refresh:
            for l in [x for x in iconlib.get_libs() if not lib or x["repo"] == lib]:
                try:
                    iconlib.refresh(l)
                except Exception:
                    pass       # 拉不动就退回旧缓存，别让面板变空白
        r = iconlib.search(q, limit=limit, variant=variant, lib_id=lib, offset=offset)
    except Exception as e:
        return _err("图标库清单获取失败：%s" % (str(e) if isinstance(e, RuntimeError) else e))
    r["ok"] = True
    r["query"] = q
    return r


@app.post("/api/admin/iconlibrary/use")
def icon_library_use(body: IconUseBody, _=Depends(require_admin)):
    """把在线图标抓到本地（lib_<名字>.<ext>），返回可直接写进卡片 icon 字段的文件名。"""
    f, why = iconlib.download(body.name, body.variant, lib_id=body.lib)
    if not f:
        return _err(why or "图标下载失败")
    return {"ok": True, "name": f, "url": "/api/iconfile/" + quote(f, safe="")}


# ---------------------------------------------------------------- admin: docker scan

def _eligible(c, s):
    # 无端口映射的容器（host 网络 / 只监听内网的服务）默认也纳入候选，卡片照样建出来，
    # 只是地址留空，等用户在「项目」页手动把端口补上。scan_hide_no_port=true 回到旧行为。
    if not c.get("primary_port") and s.get("scan_hide_no_port", False):
        return False
    if c["nav_hide"]:
        return False
    excl = {x.strip() for x in (s.get("scan_exclude") or "").split(",") if x.strip()}
    if c["name"] in excl:
        return False
    return True


def _skip_reason(c, s):
    """给扫描结果里被跳过的容器一个准确的原因，别一律写成「无端口映射」。"""
    if not c.get("primary_port") and s.get("scan_hide_no_port", False):
        return "无端口映射（已按设置跳过）"
    if c["nav_hide"]:
        return "容器标了 nav.hide"
    return "已排除"


# ------------------------------------------------------- 扫描导入：按服务类型自动归类
# 用户手填的容器标签 nav.category 优先级最高；其次是按名称/镜像关键词猜服务类型。
# 顺序即优先级（先匹配先得），猜不到返回空串 → 保持「未分类」，不硬塞。
AUTO_CATEGORY_RULES = [
    ("数据服务", ("postgres", "mysql", "mariadb", "redis", "mongo", "influx", "clickhouse", "sqlite", "minio")),
    ("智能家居", ("homeassistant", "home-assistant", "xiaomusic", "sgcc", "electricity", "tuya", "miio", "zigbee", "esphome")),
    ("运维监控", ("nezha", "dashboard", "uptime", "grafana", "prometheus", "portainer", "speedtest", "autospeed", "diancup", "watchdog")),
    ("网络工具", ("mihomo", "clash", "proxy", "frp", "tunnel", "adguard", "dns", "vpn", "wireguard", "xray", "v2ray", "sing-box", "nginx", "caddy", "socket")),
    ("网盘存储", ("clouddrive", "openlist", "alist", "litepan", "panhub", "flatnas", "cloudreve", "webdav", "sftp")),
    ("影音照片", ("emby", "jellyfin", "plex", "komga", "navidrome", "audiobook", "music", "lyric", "lrc", "subtitle", "biliup", "bililive", "biliboli", "moviepilot", "lunatv", "tunescout", "mediaflow", "live-poster", "idphoto", "mtphoto", "photo", "immich", "stream", "player")),
    ("下载工具", ("qbittorrent", "qbit", "transmission", "aria2", "iyuu", "peerban", "pt-monitor", "ptmonitor", "ximalaya", "download")),
    ("效率工具", ("memos", "vaultwarden", "wechat", "wps", "qinglong", "family-tree", "hermes", "navpage", "bestnav", "chromium", "browser", "dota2", "code-server", "vscode", "gitlab", "jenkins")),
]


def auto_category(cand):
    """按容器名 / 镜像名猜服务类型；猜不到返回 ""（留空 = 未分类）。"""
    hay = " ".join(str(x or "") for x in (
        cand.get("name"), cand.get("container_name"), cand.get("container_image"))).lower()
    for name, kws in AUTO_CATEGORY_RULES:
        if any(k in hay for k in kws):
            return name
    return ""


def _candidate(c, s):
    """生成候选条目。

    内网地址写成 `{scheme}://{host}:{port}` 模板：容器里拿不到 NAS 的局域网 IP，
    模板在请求时按浏览器实际访问的 Host 头展开，内网/公网访问各自得到正确链接。

    没有端口映射的容器拿不到端口，模板为空 —— 卡片会显示「未设置地址」，
    用户在面板里手填即可（硬凑一个不带端口的地址只会指向导航页本身，更误导）。
    """
    lan = c["nav_url_lan"] or netutil.template_url(c["primary_port"], "http")
    wan = c["nav_url_wan"]
    # 没端口时不要用 host_wan 硬拼，否则会得到 https://<域名>（指向导航页本身）
    if not wan and s.get("host_wan") and c["primary_port"]:
        wan = netutil.build_url(s["host_wan"], c["primary_port"], s.get("wan_scheme") or "https")
    return {
        "name": c["nav_name"] or c["name"],
        "url_lan": lan,
        "url_wan": wan,
        "icon": c["nav_icon"],
        "category": c["nav_category"] or "",
        "auto_category": c["nav_category"] or auto_category({
            "name": c["nav_name"] or c["name"], "container_name": c["name"],
            "container_image": c["image"]}),
        "container_name": c["name"],
        "container_image": c["image"],
        "container_port": c["primary_port"],
    }


@app.get("/api/admin/scan")
def scan(_=Depends(require_admin)):
    s = db.get_settings()
    ok, info = dockerapi.available()
    if not ok:
        return {"ok": False, "error": "Docker 不可用", "docker": info, "new": [], "existing": [], "missing": []}
    try:
        cs = dockerapi.containers()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "new": [], "existing": [], "missing": []}

    known = {i["container_name"] for i in db.list_items() if i.get("container_name")}
    new, existing, skipped = [], [], []
    for c in cs:
        if not _eligible(c, s):
            skipped.append({"name": c["name"], "reason": _skip_reason(c, s)})
            continue
        cand = _candidate(c, s)
        cand.update({"id": c["id"], "image": c["image"], "status": c["status"],
                     "public_ports": c["public_ports"], "project": c["compose_project"],
                     "no_port": not c["primary_port"]})
        (existing if c["name"] in known else new).append(cand)

    missing = []
    for i in db.list_items():
        if i.get("source") == "docker" and i.get("container_name") and \
                i["container_name"] not in {c["name"] for c in cs}:
            missing.append({"name": i["name"], "container_name": i["container_name"], "id": i["id"]})

    return {"ok": True, "docker": info, "new": new, "existing": existing,
            "missing": missing, "skipped": skipped, "total": len(cs)}


class ImportBody(BaseModel):
    names: list
    category_from_project: bool = True


@app.post("/api/admin/scan/import")
def scan_import(request: Request, body: ImportBody, _=Depends(require_admin)):
    s = db.get_settings()
    ctx = netutil.host_context(request, s)
    try:
        cs = {c["name"]: c for c in dockerapi.containers()}
    except Exception as e:
        return _err(f"扫描失败: {e}", 502)
    added, updated = 0, 0
    for name in body.names or []:
        c = cs.get(name)
        if not c or not _eligible(c, s):
            continue
        cand = _candidate(c, s)
        cat_name = cand["auto_category"] if body.category_from_project else cand["category"]
        old = db.find_item_by_container(name)
        if old:
            # 模板地址可以随端口变化刷新；用户手填的绝对地址不动
            patch = {"missing": False}
            # 用户自己分过的类不动；从没分过类的（早期导入的老卡片）按服务类型补一次
            if cat_name and not old.get("category_id"):
                patch["category_id"] = db.get_or_create_category(cat_name)
            if cand["url_lan"] and (not old.get("url_lan") or "{host}" in (old.get("url_lan") or "")):
                patch["url_lan"] = cand["url_lan"]
            if cand["url_wan"] and not old.get("url_wan"):
                patch["url_wan"] = cand["url_wan"]
            db.update_item(old["id"], patch)
            updated += 1
        else:
            icon = cand["icon"] or ""
            if not icon:
                f, _why = icons.fetch_favicon(_fetchable_url(cand["url_lan"], ctx))
                icon = f or ""
            db.create_item({
                "name": cand["name"], "url_lan": cand["url_lan"], "url_wan": cand["url_wan"],
                "icon": icon, "source": "docker",
                "category_id": db.get_or_create_category(cat_name) if cat_name else None,
                "container_name": cand["container_name"], "container_image": cand["container_image"],
                "container_port": cand["container_port"],
            })
            added += 1
    try:
        dockerapi_names = list(cs.keys())
        db.mark_missing(dockerapi_names)
    except Exception:
        pass
    return {"ok": True, "added": added, "updated": updated}


@app.post("/api/admin/scan/sync")
def scan_sync(request: Request, _=Depends(require_admin)):
    s = db.get_settings()
    ctx = netutil.host_context(request, s)
    ok, info = dockerapi.available()
    if not ok:
        return _err("Docker 不可用: " + json.dumps(info, ensure_ascii=False), 502)
    cs = dockerapi.containers()
    added = updated = 0
    for c in cs:
        if not _eligible(c, s):
            continue
        cand = _candidate(c, s)
        old = db.find_item_by_container(c["name"])
        cat_name = cand["auto_category"]
        if old:
            # 保留用户改过的名称/图标/描述；只刷新模板地址与存在性
            patch = {"missing": False, "container_image": c["image"],
                     "container_port": c["primary_port"]}
            if cat_name and not old.get("category_id"):
                patch["category_id"] = db.get_or_create_category(cat_name)
            old_lan = old.get("url_lan") or ""
            if cand["url_lan"] and (not old_lan or "{host}" in old_lan) and cand["url_lan"] != old_lan:
                patch["url_lan"] = cand["url_lan"]
            if cand["url_wan"] and not old.get("url_wan"):
                patch["url_wan"] = cand["url_wan"]
            db.update_item(old["id"], patch)
            updated += 1
        else:
            cat_id = db.get_or_create_category(cat_name) if cat_name else None
            icon = cand["icon"] or ""
            if not icon:
                f, _why = icons.fetch_favicon(_fetchable_url(cand["url_lan"], ctx))
                icon = f or ""
            db.create_item({
                "name": cand["name"], "url_lan": cand["url_lan"], "url_wan": cand["url_wan"],
                "icon": icon, "category_id": cat_id, "source": "docker",
                "container_name": c["name"], "container_image": c["image"],
                "container_port": c["primary_port"],
            })
            added += 1
    db.mark_missing([c["name"] for c in cs])
    return {"ok": True, "added": added, "updated": updated}


@app.post("/api/admin/scan/autocat")
def scan_autocat(_=Depends(require_admin)):
    """把扫描进来、还没分类的条目按「服务类型」归类；用户自己分过的类一概不动。"""
    done, skipped, by_cat = 0, 0, {}
    for it in db.list_items():
        if (it.get("source") or "") != "docker" or it.get("category_id"):
            continue
        name = auto_category({"name": it.get("name"), "container_name": it.get("container_name"),
                              "container_image": it.get("container_image")})
        if not name:
            skipped += 1
            continue
        db.update_item(it["id"], {"category_id": db.get_or_create_category(name)})
        by_cat[name] = by_cat.get(name, 0) + 1
        done += 1
    return {"ok": True, "done": done, "skipped": skipped, "by_category": by_cat}


class RefreshIconsBody(BaseModel):
    ids: list | None = None


@app.post("/api/admin/refresh-icons")
def refresh_icons(request: Request, body: RefreshIconsBody, _=Depends(require_admin)):
    s = db.get_settings()
    ctx = netutil.host_context(request, s)
    items = db.list_items()
    if body.ids:
        want = {int(x) for x in body.ids}
        items = [i for i in items if i["id"] in want]
    done, fail = 0, 0
    for it in items:
        base = _fetchable_url(it.get("url_lan"), ctx) or _fetchable_url(it.get("url_wan"), ctx)
        if not base:
            fail += 1
            continue
        f, why = icons.fetch_favicon(base)
        if f:
            db.update_item(it["id"], {"icon": f})
            done += 1
        else:
            fail += 1
    return {"ok": True, "done": done, "fail": fail}




# ---------------------------------------------------------------- 镜像定时自动检查 / 自动更新
# 2026-10-16 用户要求：镜像页要能设一个 cron，到点自动检查更新；
# 检查到更新时，把「被勾选为自动更新」的那些镜像自动更新掉。
# 实现放在面板进程内的一个守护线程里（单进程 uvicorn，不会重复起），
# 不开就是完全不跑 —— 定时任务也要能一眼看出「上次什么时候跑的、结果如何」。
_SCHED = {"minute": "", "last": None, "prune_minute": "", "last_prune": None}
_SCHED_LOCK = threading.Lock()


def _auto_status(s=None):
    s = s or db.get_settings()
    expr = (s.get("image_auto_cron") or "").strip()
    on = bool(s.get("image_auto_check"))
    return {
        "check": on,                     # 定时检查开关
        "cron": expr,
        "valid": cron.ok(expr),          # 表达式能不能认
        "desc": cron.describe(expr),     # 预设的人话
        "update": bool(s.get("image_auto_update")),   # 检查到更新就自动更新
        "names": [str(x) for x in (s.get("image_auto_names") or [])],
        "next": (cron.next_run(expr) if on else None),
        "last": _SCHED.get("last"),
        "allow": bool(s.get("allow_image_update")),
        "prune": _prune_status(s),
    }


def _prune_status(s=None):
    """定时清理的展示状态（设置页用）。"""
    s = s or db.get_settings()
    expr = (s.get("image_auto_prune_cron") or "").strip()
    on = bool(s.get("image_auto_prune"))
    return {
        "on": on,
        "cron": expr,
        "valid": cron.ok(expr),
        "desc": cron.describe(expr),
        "next": (cron.next_run(expr) if on else None),
        "mode": (s.get("image_prune_mode") or "dangling"),
        "keep": [str(x) for x in (s.get("image_prune_keep") or [])],
        "last": _SCHED.get("last_prune"),
    }


def _auto_summary(rec):
    parts = [f"定时检查：{rec.get('checked', 0)} 个容器，{len(rec.get('outdated') or [])} 个可更新"]
    if rec.get("updated"):
        parts.append("已自动更新 " + "、".join(rec["updated"]))
    if rec.get("failed"):
        parts.append("失败 " + "；".join(rec["failed"]))
    if rec.get("skipped"):
        parts.append(rec["skipped"])
    if rec.get("error"):
        parts.append(str(rec["error"])[:120])
    return "；".join(parts)


def _auto_run():
    """到点了：查一遍更新；该自动更新的按顺序更新（一次一个容器）。"""
    s = db.get_settings()
    names = [str(x) for x in (s.get("image_auto_names") or [])]
    rows, err, _cached = _image_rows(force=True)
    outdated = [r for r in rows if r.get("status") == "outdated"]
    targets = [r for r in outdated
               if r.get("container") in names and not r.get("local_only")]
    rec = {"ts": int(time.time()), "checked": len(rows),
           "outdated": [r.get("container") for r in outdated],
           "updated": [], "failed": [], "skipped": "", "error": err or ""}
    if not outdated:
        rec["skipped"] = "没有需要更新的镜像"
    elif not targets:
        # 区分两种情况：压根没勾 vs 勾了但那些镜像当前不需要更新
        # （第一版只有一句「没有勾选」，实测勾了一个 current 的镜像时提示会误导）
        if not names:
            rec["skipped"] = "有 %d 个镜像可更新，但没有勾选任何「自动更新」" % len(outdated)
        else:
            rec["skipped"] = ("勾选的 %d 个镜像当前都不需要更新（可更新的 %d 个都没勾）"
                              % (len(names), len(outdated)))
    elif not s.get("image_auto_update"):
        rec["skipped"] = "未开启「检查到更新时自动更新」"
    elif not s.get("allow_image_update"):
        rec["skipped"] = "面板的「允许从面板更新镜像」没打开"
    else:
        for r in targets:
            ok, t = _spawn_update(r["container"], pull=True)
            if not ok:
                rec["failed"].append(f"{r['container']}（{t}）")
                continue
            # 等这一个跑完再动下一个：同时更新多个容器会一起停服务，日志也糊成一团
            deadline = time.time() + 1800
            while t.running and time.time() < deadline:
                time.sleep(1)
            if t.success:
                rec["updated"].append(r["container"])
            else:
                rec["failed"].append(f"{r['container']}（{t.message}）")
    with _SCHED_LOCK:
        _SCHED["last"] = rec
    _hist_add({"ts": rec["ts"], "container": "(定时检查)", "image": "",
               "success": not rec["failed"], "elapsed": None, "local_only": False,
               "message": _auto_summary(rec), "log": []})
    return rec


def _auto_scheduler_loop():
    """每 20 秒醒一次：命中 cron 且这一分钟还没跑过，就执行一次。

    两个任务共用这个循环，各自有独立开关、cron 和「这一分钟跑过没」标记：
      1) 定时检查 / 自动更新镜像（image_auto_check）
      2) 定时清理未使用镜像（image_auto_prune）
    2026-10-16 为什么要拆开：原来是一串 `continue`，第一个开关关掉就直接跳过整个循环，
    后面再加任务永远不会执行。
    """
    while True:
        try:
            time.sleep(20)
            s = db.get_settings()
            tm = time.localtime()
            stamp = time.strftime("%Y-%m-%d %H:%M", tm)

            expr = (s.get("image_auto_cron") or "").strip()
            if s.get("image_auto_check") and cron.ok(expr):
                with _SCHED_LOCK:
                    done = _SCHED["minute"] == stamp
                if not done and cron.matches(expr, tm):
                    with _SCHED_LOCK:
                        _SCHED["minute"] = stamp
                    _auto_run()

            pexpr = (s.get("image_auto_prune_cron") or "").strip()
            if s.get("image_auto_prune") and cron.ok(pexpr):
                with _SCHED_LOCK:
                    pdone = _SCHED.get("prune_minute") == stamp
                if not pdone and cron.matches(pexpr, tm):
                    with _SCHED_LOCK:
                        _SCHED["prune_minute"] = stamp
                    _prune_run(auto=True)
        except Exception as e:
            try:
                with _SCHED_LOCK:
                    _SCHED["last"] = {"ts": int(time.time()), "checked": 0,
                                      "updated": [], "failed": [],
                                      "skipped": "", "outdated": [],
                                      "error": f"{type(e).__name__}: {e}"}
            except Exception:
                pass


class ContainerActionBody(BaseModel):
    name: str
    action: str          # start | stop | restart


@app.post("/api/admin/container/action")
def admin_container_action(body: ContainerActionBody, _=Depends(require_admin)):
    """启动 / 停止 / 重启容器（卡片右键菜单用）。

    和「更新镜像」共用同一把面板开关 + 同一套 socket-proxy 权限：
    面板没挂 docker.sock，非 GET 请求必须由代理放行 POST + ALLOW_START/ALLOW_STOP。
    """
    s = db.get_settings()
    if not s.get("allow_image_update"):
        return _err("容器操作开关没打开：去「镜像」页打开「允许从面板更新镜像 / 管理容器」，"
                    "并给 navpage-socket-proxy 放开 POST=1 ALLOW_START=1 ALLOW_STOP=1")
    action = (body.action or "").strip().lower()
    if action not in ("start", "stop", "restart"):
        return _err("不支持的操作：" + (body.action or ""))
    name = (body.name or "").strip()
    if not name:
        return _err("缺容器名")
    cid = dockerapi.find_container(name)
    if not cid:
        return _err(f"找不到名为 {name} 的容器：可能已被删掉或改过名")
    try:
        r = dockerapi._post(f"/containers/{cid}/{action}", timeout=120)
    except Exception as e:
        return _err(f"请求 Docker 失败：{type(e).__name__}: {e}")
    if dockerapi.blocked_by_proxy(r):
        return _err(dockerapi.ACL_HINT)
    if r.status_code in (204, 304):
        word = {"start": "已启动", "stop": "已停止", "restart": "已重启"}[action]
        extra = "（本来就是这个状态）" if r.status_code == 304 else ""
        state = ""
        try:
            state = (dockerapi.container_inspect(cid).get("State") or {}).get("Status") or ""
        except Exception:
            pass
        return {"ok": True, "message": f"{word} {name}{extra}", "state": state}
    body_txt = (r.text or "")[:160]
    return _err(f"{action} {name} 失败：HTTP {r.status_code} {body_txt}")


class ItemIconBody(BaseModel):
    url_lan: str = ""
    url_wan: str = ""
    item_id: int = 0


@app.post("/api/admin/item/icon")
def admin_item_icon(request: Request, body: ItemIconBody, _=Depends(require_admin)):
    """编辑弹窗的「抓取图标」：按地址抓一个网页图标存进本地图标库，返回文件名。

    和「刷新图标」的区别：这个**不写库**，只把文件名回给前端填进输入框 ——
    用户点「保存」才真正生效，所以新增条目（还没有 id）时也能用。
    """
    s = db.get_settings()
    ctx = netutil.host_context(request, s)
    raw_lan = (body.url_lan or "").strip()
    raw_wan = (body.url_wan or "").strip()
    if not raw_lan and not raw_wan and body.item_id:
        it = next((i for i in db.list_items() if i["id"] == body.item_id), None)
        if it:
            raw_lan = (it.get("url_lan") or "").strip()
            raw_wan = (it.get("url_wan") or "").strip()
    # ':8096' 这种只填了端口的残缺值：补成模板再展开
    if raw_lan.startswith(":"):
        raw_lan = "{scheme}://{host}" + raw_lan
    if raw_wan.startswith(":"):
        raw_wan = "{scheme}://{host}" + raw_wan
    if not raw_lan and not raw_wan:
        return _err("先在地址栏填一个网址，再来抓图标")
    base = _fetchable_url(raw_lan, ctx) or _fetchable_url(raw_wan, ctx)
    if not base:
        return _err("地址里还有 {host} 占位符，但「设置 → 网络」里没填内网地址/外网域名，"
                    "展开不出真实网址")
    if not (base.startswith("http://") or base.startswith("https://")):
        return _err("地址要以 http:// 或 https:// 开头（当前是 " + base[:40] + "）")
    f, why = icons.fetch_favicon(base)
    if not f:
        return _err(f"没抓到图标（{why}）：站点可能没有 favicon，或要登录才能访问")
    return {"ok": True, "icon": f, "from": base}


# ---------------------------------------------------------------- static (must be last)

if os.path.isdir(STATIC_DIR):
    # index.html 自身没有版本号：被浏览器缓存会导致「服务端已更新、用户看到的还是旧界面」
    # （2026-10-16 实际踩到）。html 一律 no-store，app.js/app.css 靠 ?v=N 区分，不禁。
    @app.middleware("http")
    async def _no_cache_html(request, call_next):
        resp = await call_next(request)
        p = request.url.path
        if p.endswith(".html") or p in ("", "/"):
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
        return resp

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
