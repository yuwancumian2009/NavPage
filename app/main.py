"""NavPage — FastAPI 应用。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8080
"""
import os
import re
import random
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import time
import json
import hashlib
import ipaddress
import secrets
import uuid
import warnings
import concurrent.futures
import asyncio
from urllib.parse import quote, urlsplit
from concurrent.futures import as_completed

warnings.filterwarnings("ignore")

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Depends
from fastapi.responses import JSONResponse, FileResponse, Response, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from starlette.background import BackgroundTask
from pydantic import BaseModel

import httpx

from . import db, netutil, dockerapi, icons, iconlib, widgets, registry, cron, version, gately
from . import gw as gw_mod
from . import compose as compose_mod
from . import backup as backup_mod

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="NavPage", docs_url=None, redoc_url=None)

# 启动时记录主事件循环，供线程池里的同步端点提交后台任务（见 _run_bg）
_MAIN_LOOP = None

# 静态资源打包后 1.5MB（element-plus 一个就 936KB），不压缩的话外网/frp 首屏很慢。
# 实测 gzip 后 1585KB → 473KB（-70%），CPU 开销可忽略（服务端只压一次就进浏览器缓存）。
# minimum_size 取 512：小 JSON 接口压了反而更大。
app.add_middleware(GZipMiddleware, minimum_size=512)


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

    url_lan / url_wan / url_frp 里可以写 `{host}` `{scheme}` 占位符，按本次请求的
    实际访问地址展开，这样同一个条目在局域网和公网访问时都能拿到正确链接。

    2.0.0 起多了两件事：
      * 第三种地址 `url_frp`（frp 隧道域名），所以 mode 多了 "frp"；
      * 单卡可以指定 `addr_pref`（inherit/auto/lan/wan/frp），优先于右上角的全局开关。
        这就是「这张卡走 frp、那张卡走反代」的实现方式，不用来回切全局开关。
    """
    ctx = ctx or {}
    lan_raw = (item.get("url_lan") or "").strip()
    wan_raw = (item.get("url_wan") or "").strip()
    frp_raw = (item.get("url_frp") or "").strip()

    lan = netutil.expand_url(lan_raw, "lan", ctx) if lan_raw else ""
    wan = netutil.expand_url(wan_raw, "wan", ctx) if wan_raw else ""
    frp = netutil.expand_url(frp_raw, "wan", ctx) if frp_raw else ""

    # 某一侧没配时，用另一侧的模板换主机名兜底（frp 是完整域名，不参与兜底）
    if not wan and "{host}" in lan_raw:
        wan = netutil.expand_url(lan_raw, "wan", ctx)
    if not lan and "{host}" in wan_raw:
        lan = netutil.expand_url(wan_raw, "lan", ctx)

    pref = (item.get("addr_pref") or "inherit").strip().lower()
    want = mode if pref in ("inherit", "auto", "") else pref
    if want not in ("lan", "wan", "frp"):
        want = "lan"

    chains = {
        "lan": [(lan, "lan"), (wan, "wan"), (frp, "frp")],
        "wan": [(wan, "wan"), (frp, "frp"), (lan, "lan")],
        "frp": [(frp, "frp"), (wan, "wan"), (lan, "lan")],
    }
    url, prefer = "", "none"
    for val, kind in chains[want]:
        if val:
            url, prefer = val, kind
            break

    avail = []
    if lan:
        avail.append("lan")
    if wan:
        avail.append("wan")
    if frp:
        avail.append("frp")
    out = dict(item)
    out["url"] = url
    out["url_lan_view"] = lan
    out["url_wan_view"] = wan
    out["url_frp_view"] = frp
    out["used"] = prefer
    out["wanted"] = want
    out["availability"] = "+".join(avail) if avail else "none"
    out["has_frp"] = bool(frp)
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
            raw = netutil.normalize_url(raw)   # ':9091' 这种残缺值补全模板再展开
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
    eff = mode if mode in ("lan", "wan", "frp") else auto_mode

    authed = is_authed(request)
    if s.get("require_login") and not authed:
        # 未登录：不下发任何条目，前端只渲染登录页
        return {
            "ok": True,
            "locked": True,
            "authed": False,
            # 版本/项目地址等公开元信息：登录门禁页的「关于」也要显示
            "app": version.meta(),
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
        "app": version.meta(),
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
    _WIDGET_LAST_HIT["t"] = time.time()
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



# ---------------------------------------------------------------- 远端壁纸中转
_RELAY_POOL = 10           # 每个壁纸 URL 本地常备几张（连刷十几次都不用等）
_RELAY_BUSY = {}           # key -> Lock，保证同一 URL 只有一个线程在补池子
_RELAY_LAST = {}           # key -> 上一次发出去的文件名（避免连续两次同一张）
_RELAY_LOCK = threading.Lock()


def _relay_dir():
    d = os.path.join(db.WALLPAPER_DIR, "_relay")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _relay_key(u):
    return hashlib.sha1(u.encode("utf-8")).hexdigest()[:20]


def _relay_pool(d, key):
    """该 URL 已缓存的图，新的在前。"""
    try:
        fs = [f for f in os.listdir(d)
              if f.startswith(key + "_") and not f.startswith("tmp_")
              and "_live" not in f]
    except Exception:
        return []
    def ts(f):
        try:
            return int(f.rsplit("_", 1)[1].split(".")[0])
        except Exception:
            return 0
    return sorted(fs, key=ts, reverse=True)


def _relay_live(d, key):
    """上一次发出去的那张（池子被取空时拿它兜底，不让用户干等公网）。"""
    try:
        for f in os.listdir(d):
            if f.startswith(key + "_live"):
                return os.path.join(d, f)
    except Exception:
        pass
    return None


def _relay_fetch(u, key, d):
    """拉一张远端壁纸，压到 1920px webp 落盘，返回文件名；失败返回 None。"""
    try:
        with httpx.Client(timeout=20, verify=False, follow_redirects=True) as c:
            r = c.get(u, headers={"User-Agent": "Mozilla/5.0 NavPage"})
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                return None
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                   "image/gif": "gif", "image/avif": "avif"}.get(ctype, "jpg")
            name = "%s_%d.%s" % (key, int(time.time() * 1000), ext)
            tmp = os.path.join(d, "tmp_" + name)
            with open(tmp, "wb") as f:
                f.write(r.content)
            # 壁纸源常给 4K 原图（实测一张 7.9MB），浏览器只为铺满屏幕，
            # 压到 1920px webp 体积能掉 90%+。
            try:
                from PIL import Image
                im = Image.open(tmp)
                # draft() 让 JPEG 解码时直接按 1/2、1/4 降采样，4K 原图解码从
                # 一两秒掉到几十毫秒（解码是 C 代码、吃 GIL，会拖慢同时进来的请求）。
                try:
                    im.draft("RGB", (1920, 1920))
                except Exception:
                    pass
                if max(im.size) > 1920:
                    im.thumbnail((1920, 1920), Image.LANCZOS)
                wt = os.path.join(d, "tmpw_" + name)
                im.convert("RGB").save(wt, "WEBP", quality=80, method=4)
                if os.path.getsize(wt) < os.path.getsize(tmp):
                    os.unlink(tmp)
                    name = name.rsplit(".", 1)[0] + ".webp"
                    os.replace(wt, os.path.join(d, name))
                else:
                    os.unlink(wt)
                    os.replace(tmp, os.path.join(d, name))
            except Exception:
                os.replace(tmp, os.path.join(d, name))
            return name
    except Exception:
        return None


def _relay_prune(d, key):
    for f in _relay_pool(d, key)[_RELAY_POOL:]:
        try:
            os.unlink(os.path.join(d, f))
        except Exception:
            pass
    try:
        for f in os.listdir(d):
            fp = os.path.join(d, f)
            try:
                if (f.startswith("tmp") and time.time() - os.path.getmtime(fp) > 600) or \
                        time.time() - os.path.getmtime(fp) > 7 * 86400:
                    os.unlink(fp)
            except Exception:
                pass
    except Exception:
        pass


def _relay_topup(u, key, d):
    """把池子补到 _RELAY_POOL 张（后台线程调，同一 URL 同时只跑一个）。

    单张要 3~5 秒，串行补 6 张太慢 → 3 路并行。
    """
    lock = _RELAY_BUSY.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        need = _RELAY_POOL - len(_relay_pool(d, key))
        while need > 0:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, need)) as ex:
                futs = [ex.submit(_relay_fetch, u, key, d) for _ in range(min(3, need))]
                got = sum(1 for f in as_completed(futs) if f.result())
            if not got:
                break
            need -= got
        _relay_prune(d, key)
    finally:
        lock.release()


@app.get("/api/wprelay")
def wallpaper_relay(u: str):
    """远端壁纸本地中转：本地存一池子图，每次换一张，取走一张后台补一张。

    壁纸可以填外部 URL（例：https://pic.o.hhxin.top/api/random），浏览器直接去拉
    每次要绕公网、实测 3~5 秒，而且原图常有 7~9MB。这里由 NAS 先拉好并压到 1920px
    webp，浏览器拿到的是本地几十毫秒、几百 KB。

    注意**不能**给浏览器长缓存：用户要的是「每次刷新换一张随机壁纸」。所以响应
    no-store，服务端每次从池子里随机挑一张（并把它删掉，让后台补新的），既保证随机
    又不用等公网。
    """
    u = (u or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "bad url")
    d = _relay_dir()
    key = _relay_key(u)
    pool = _relay_pool(d, key)
    if not pool:
        # 池子空（第一次访问 / 刚被取空）：同步拉一张顶住，剩下的交给后台补。
        name = _relay_fetch(u, key, d)
        if not name:
            raise HTTPException(502, "壁纸源取不到")
        pool = [name]
    with _RELAY_LOCK:
        pool = _relay_pool(d, key)
        live = _relay_live(d, key)
        if pool:
            last = _RELAY_LAST.get(key)
            cands = [f for f in pool if f != last] or pool
            pick = random.choice(cands)
            _RELAY_LAST[key] = pick
            # 发出去的这张挪成 live，池子里就没有它了 → 后台再拉一张新的补上，
            # 所以每次刷新拿到的都是没见过的图。
            try:
                newlive = os.path.join(d, "%s_live%s" % (key, os.path.splitext(pick)[1]))
                if live and live != newlive:
                    try:
                        os.unlink(live)
                    except Exception:
                        pass
                os.replace(os.path.join(d, pick), newlive)
                live = newlive
            except Exception:
                live = os.path.join(d, pick)
    if not live:
        name = _relay_fetch(u, key, d)
        if not name:
            raise HTTPException(502, "壁纸源取不到")
        live = os.path.join(d, name)
    threading.Thread(target=_relay_topup, args=(u, key, d), daemon=True).start()
    return FileResponse(live, headers={
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
    })

_WP_WARMED = {"t": 0.0}


def _wallpaper_warmer():
    """后台把远端壁纸池子一直保持在满的状态。

    远端壁纸源（pic.o.hhxin.top）单张要 3~5 秒，池子里没货时第一个打开导航页的人
    就得干等。这里趁没人访问时提前备好 4 张，用户每次刷新直接拿现成的、且每张都不一样。
    """
    first = True
    while True:
        try:
            time.sleep(30 if first else 600)
            first = False
            st = db.get_settings() or {}
            u = (st.get("wallpaper") or "").strip()
            if not u.lower().startswith(("http://", "https://")):
                continue
            urls = [u]
            if "/random" in u:
                urls += [u.replace("/random", "/pc"), u.replace("/random", "/mobile")]
            d = _relay_dir()
            for c in urls:
                try:
                    _relay_topup(c, _relay_key(c), d)
                except Exception:
                    pass
        except Exception:
            time.sleep(60)


_WIDGET_LAST_HIT = {"t": 0.0}


def _widgets_warmer():
    """空跑预热小组件缓存。

    冷缓存时一次 /api/widgets 要把 20 个面板全问一遍（实测 9.5 秒），第一个打开页面的人
    就得盯着空白卡片。这里在后台先跑一遍；之后只在「最近 10 分钟有人看过页面」时才继续热，
    没人用就安静待着，不去打扰远端面板。
    """
    first = True
    while True:
        try:
            time.sleep(15 if first else 55)
            warm = first or (time.time() - _WIDGET_LAST_HIT["t"] < 600)
            first = False
            if not warm:
                continue
            st = db.get_settings() or {}
            if not st.get("widgets_enabled"):
                continue
            items = [i for i in db.list_items(include_hidden=False) if i.get("widget")]
            if items:
                widgets.fetch_many(items, ttl=max(5, int(st.get("widget_interval") or 60)))
        except Exception:
            time.sleep(30)


@app.on_event("startup")
def _start_widgets_warmer():
    threading.Thread(target=_widgets_warmer, daemon=True).start()


@app.on_event("startup")
def _start_wallpaper_warmer():
    threading.Thread(target=_wallpaper_warmer, daemon=True).start()


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


def _icon_response(name):
    """图标统一出口：大于 48KB 的原图按需转 128px webp 再发。

    卡片上的图标只有 32~48px，直接把用户上传的原图丢给浏览器很亏：
    实测首屏 3.0MB 里 2.4MB 是图标，其中一张 1.77MB 的 PNG 就占了一半以上。
    """
    p = icons.thumb_path(name)
    if not p:
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type=icons.guess_mime(os.path.basename(p)),
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/iconfile/{name}")
def iconfile(name: str):
    return _icon_response(name)


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
        return _icon_response(hit)
    name = fetch_and_cache_icon(url)
    if not name:
        raise HTTPException(404, "icon not available")
    return _icon_response(name)


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
    docker_host: str | None = None
    missing: bool | None = None
    widget: dict | None = None
    url_visible: int | None = None
    card_size: str | None = None
    auto_created: bool | None = None
    # 2.0.0 内置网关：FRP 地址 / 反代绑定的域名 / 这张卡片用哪个地址
    url_frp: str | None = None
    proxy_domain: str | None = None
    addr_pref: str | None = None


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
def delete_item(iid: int, purge: bool = False, _=Depends(require_admin)):
    """删卡片。容器卡片不是真删，而是留一条隐藏墓碑。

    远端采集源每 5 分钟上报一次，真把行删掉的话下一次上报就把它原样重建出来
    （用户会以为「删不掉」）。墓碑 = hidden=1 + 保留 container_name/docker_host：
    首页不渲染、扫描不算「新发现」、自动建卡也认为它已存在，只在「项目」页里
    以灰色「已排除」出现，想恢复点一下就行。
    """
    it = db.get_item(iid)
    if not it:
        return _err("条目不存在", 404)
    # 「已消失」列表里点删除 → 前端带 purge=1，直接真删。
    # 原来一律留墓碑，结果已消失列表永远挂着它，点了删除也像没反应（2026-10-26 用户反馈）。
    if purge or it.get("missing"):
        db.delete_item(iid)
        return {"ok": True, "kept_as_excluded": False, "deleted": True}
    if (it.get("container_name") or "").strip() and it.get("source") == "docker":
        db.update_item(iid, {"hidden": 1, "missing": 1})
        return {"ok": True, "kept_as_excluded": True}
    db.delete_item(iid)
    return {"ok": True, "kept_as_excluded": False}


class ExcludeBody(BaseModel):
    excluded: bool = True


@app.post("/api/admin/items/{iid}/exclude")
def exclude_item(iid: int, body: ExcludeBody, _=Depends(require_admin)):
    """排除 / 恢复一个条目。

    排除要同时做两件事，缺一不可：
      1. `hidden=1` → 首页不再渲染这张卡片（bootstrap 用 include_hidden=False）
      2. 容器名写进 `scan_exclude` → 重新扫描 / 全量同步时不会又把它导入回来

    只做第 1 步的话，下次点「全量同步」它就复活了；只做第 2 步的话卡片还挂在首页上。
    手建条目（没有 container_name）只做第 1 步；扫描判定「已消失」的卡片直接真删。
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


def _img_checked_at(state=None):
    """上次真正联网检查成功的时间戳（取各镜像 ts 的最大值），0 = 从来没查过。"""
    st = state if state is not None else _img_state_load()
    ts = [float(v.get("ts") or 0) for v in st.values() if isinstance(v, dict) and v.get("ts")]
    return int(max(ts)) if ts else 0


def _img_rows_from_state():
    """只用落盘的「上次检查结果」拼行，**全程不联网**。

    2026-10-18 用户要求：打开镜像页默认显示上一次的结果，不要自动去查最新。
    这样点开页面是秒开的，也不会碰 Docker Hub 的匿名配额；要最新就手动点「检查更新」。
    """
    state = _img_state_load()
    try:
        cs = dockerapi.containers(all_containers=True)
    except Exception as e:
        return [], f"读不到容器列表：{e}"
    rows = []
    for c in cs:
        img = c.get("image") or ""
        st = state.get(img) or {}
        status = st.get("status") or "unknown"
        rows.append({
            "container": c.get("name"), "id": c.get("id"), "image": img,
            "state": c.get("state"), "project": c.get("compose_project"),
            "status": status,
            "local_digest": (st.get("local") or "")[:19],
            "remote_digest": (st.get("remote") or "")[:19],
            "has_repo_digest": bool(st.get("local")),
            "is_local_build": bool(st.get("is_local_build")) or st.get("status") == "local",
            "digest_missing": bool(st.get("digest_missing")),
            "error": "" if st else "还没有检查记录，点「检查更新」拉一次",
        })
    rows.sort(key=lambda r: ({"outdated": 0, "unknown": 1, "current": 2, "local": 3}.get(r["status"], 1),
                             r.get("project") or "~", r.get("container") or ""))
    return rows, ""


_TRANSIENT_ERR = ("ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
                  "RemoteProtocolError", "ServerDisconnected", "Network is unreachable",
                  "timed out", "502", "503", "504")


def _transient_err(err):
    """registry 查询失败的文案是否属于「等会儿再试可能就好了」这类。"""
    s = str(err or "")
    return any(k in s for k in _TRANSIENT_ERR)


def _image_rows(force=False):
    """比对本地镜像摘要与 registry 最新摘要。结果按 image_check_cache 秒缓存。"""
    s = db.get_settings()
    ttl = int(s.get("image_check_cache") or 3600)
    now = time.time()
    if not force and _img_cache["rows"] and (now - _img_cache["ts"]) < ttl:
        return _img_cache["rows"], _img_cache["err"], True

    proxy = (s.get("image_proxy") or "").strip() or None
    # 镜像源：判断 Docker Hub 镜像「是否最新」优先走它（直连、不占匿名配额）
    mirror = (s.get("image_mirror") or "").strip() or None
    state = _img_state_load()
    try:
        local, local_by_id = dockerapi.image_index()
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
        rd, e = registry.remote_digest(img, proxy, mirror)
        # 镜像源/代理偶发抽风（连接失败、5xx、超时）会把「其实可更新」的镜像判成「未知」，
        # 而未知状态的镜像永远不会被自动更新 —— 用户观感就是「勾了自动更新但它不动」。
        # 只对可判定的瞬时错误补一次重试，401/404 这类确定性失败不浪费配额。
        if not rd and _transient_err(e):
            time.sleep(1.0)
            rd2, e2 = registry.remote_digest(img, proxy, mirror)
            if rd2:
                return img, (rd2, "")
        return img, (rd, e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        for img, res in ex.map(_one, need):
            remote[img] = res

    rows = []
    for c in cs:
        img = c.get("image") or ""
        li = local_by_id.get(c.get("image_id") or "") or local.get(img) or {}
        ld = registry.local_digest_for(li.get("digests"), img)
        rd, err = remote.get(img, ("", "未检查"))
        row_err = err
        if img.startswith("sha256:") and li.get("digests"):
            # tag 被别的容器/一次 pull 挪到新镜像后，Docker 会把这个字段降级成裸镜像 ID。
            # 原 tag 已经查不到了，硬猜（比如当成 :latest）可能把别的 tag 的容器更新错，
            # 所以只如实说明，交给用户重新创建容器。
            rows.append({
                "container": c.get("name"), "id": c.get("id"), "image": img,
                "state": c.get("state"), "project": c.get("compose_project"),
                "status": "unknown", "local_digest": "", "remote_digest": "",
                "error": "容器的镜像 tag 已被其他镜像占用（被别的容器或一次 pull 挪走了），无法比对远端；重新创建该容器即可恢复",
                "has_repo_digest": bool(li.get("digests")), "is_local_build": False,
                "digest_missing": False,
            })
            continue
        if not ld:
            # RepoDigests 为空 **不等于** 本地构建：飞牛面板装的应用、离线包导入的镜像
            # 都来自 registry，只是 daemon 没留下摘要（用户反馈 openlist 被误判成
            # 「本地镜像」就是这个原因）。带命名空间的引用（有 "/"）去 registry 取
            # config digest —— 它和本地镜像 ID 是同一个东西，可以直接比，不用先 pull。
            cfg, cfg_err = "", ""
            if "/" in img:
                cfg, cfg_err = registry.remote_config_digest(img, proxy, mirror)
            if cfg:
                local_id = "sha256:" + (li.get("full_id") or "")
                status = "current" if local_id == cfg else "outdated"
                state[img] = {"status": status, "ts": time.time(), "local": local_id,
                              "remote": registry.normalize(cfg), "digest_missing": True,
                              "is_local_build": False}
                rows.append({
                    "container": c.get("name"), "id": c.get("id"), "image": img,
                    "state": c.get("state"), "project": c.get("compose_project"),
                    "status": status, "local_digest": local_id[7:26],
                    "remote_digest": registry.normalize(cfg)[7:26],
                    "error": "", "has_repo_digest": False, "is_local_build": False,
                    "digest_missing": True,
                })
                continue
            # 取不到：registry 明说没这个仓库 -> 真·本地构建；其它情况（网络/限流）-> 未知
            low = (cfg_err or "").lower()
            is_local = ("没有" in (cfg_err or "")) or ("404" in low) or ("does not exist" in low)
            if is_local:
                status = "local"
                row_err = ""
            else:
                status = "unknown"
                row_err = cfg_err or "取不到远程摘要"
            if is_local:
                state[img] = {"status": "local", "ts": time.time(), "is_local_build": True}
            rows.append({
                "container": c.get("name"), "id": c.get("id"), "image": img,
                "state": c.get("state"), "project": c.get("compose_project"),
                "status": status, "local_digest": "", "remote_digest": "",
                "error": row_err, "has_repo_digest": False, "is_local_build": is_local,
                "digest_missing": False,
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
            "is_local_build": False,
            "digest_missing": False,
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
def admin_images(refresh: int = 0, cached: int = 0, auto_apply: int = 0, _=Depends(require_admin)):
    s = db.get_settings()
    if cached and not refresh:
        # 打开页面默认走这里：只读上次结果，不碰 registry（2026-10-18 用户要求）
        rows, err = _img_rows_from_state()
        is_cached = True
    else:
        rows, err, is_cached = _image_rows(force=bool(refresh))
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
        # 真·本地构建（registry 上没有这个仓库）才显示「本地镜像 / 重建」；
        # 没有 RepoDigests 但 registry 上确实有的（飞牛面板装的 openlist 这类），
        # 走 config digest 比对，按正常的「已最新 / 可更新」展示。
        r["local_only"] = bool(r.get("is_local_build")) or dockerapi.is_local_only(r.get("image") or "")
    n_out = sum(1 for r in rows if r["status"] == "outdated")
    # 点「检查更新」时顺手执行自动更新（2026-10-22 用户反馈：勾了自动更新，
    # 检查到有更新却不动 —— 以前只有 cron 到点才更新，手动检查纯看不做）。
    applied = []
    if refresh and auto_apply:
        applied = _apply_updates(_auto_targets(rows, s), "手动检查")
    hist = _hist_list()
    return {"ok": True, "rows": rows, "error": err, "cached": is_cached,
            "auto_applied": applied,
            "checked_at": _img_checked_at(),
            "outdated": n_out, "last": (hist[0] if hist else None),
            "allow_update": bool(s.get("allow_image_update")),
            "proxy": bool((s.get("image_proxy") or "").strip()),
            "mirror": (s.get("image_mirror") or "").strip(),
            "auto": _auto_status(s)}


@app.get("/api/admin/images/badge")
def admin_images_badge(_=Depends(require_admin)):
    """顶部「可更新镜像」角标用的轻量接口：只读上次结果，绝不联网。

    2026-10-18 用户要求：导航页顶栏汉堡菜单左边显示可更新镜像数量。
    这里不触发检查，所以刷新页面不会打 registry；数量随「检查更新」/定时任务更新。
    """
    rows, _err = _img_rows_from_state()
    return {"ok": True,
            "outdated": sum(1 for r in rows if r.get("status") == "outdated"),
            "total": len(rows),
            "checked_at": _img_checked_at()}


# ---------------------------------------------------------------- Compose 管理
# 2026-10-19 用户要求：能读取飞牛上每个 compose 的 YAML，在面板里改完直接重构。
# 文件读写走 bind mount（宿主机同路径挂载），执行走 compose CLI + socket-proxy。
class ComposeSaveBody(BaseModel):
    path: str
    content: str
    restart: bool = False
    pull: bool = False


class ComposeUpBody(BaseModel):
    path: str
    pull: bool = False


class ComposeRebuildBody(BaseModel):
    path: str
    build: bool = True


class ComposeNewBody(BaseModel):
    name: str
    content: str
    up: bool = False
    pull: bool = False


@app.get("/api/admin/compose")
def admin_compose_list(_=Depends(require_admin)):
    try:
        rows = compose_mod.projects()
    except Exception as e:
        return _err(f"扫描 compose 失败：{e}")
    return {"ok": True, "root": compose_mod.COMPOSE_ROOT, "rows": rows}


@app.get("/api/admin/compose/file")
def admin_compose_file(path: str, _=Depends(require_admin)):
    try:
        return {"ok": True, **compose_mod.read_file(path)}
    except Exception as e:
        return _err(str(e))


@app.post("/api/admin/compose/save")
def admin_compose_save(body: ComposeSaveBody, _=Depends(require_admin)):
    try:
        res = compose_mod.save(body.path, body.content,
                               restart=bool(body.restart), pull=bool(body.pull))
        return dict(res, ok=bool(res.get("ok")))
    except Exception as e:
        return _err(str(e))


@app.post("/api/admin/compose/up")
def admin_compose_up(body: ComposeUpBody, _=Depends(require_admin)):
    try:
        ok, out, detached = compose_mod.up(body.path, pull=bool(body.pull))
        return {"ok": ok, "message": out, "detached": detached}
    except Exception as e:
        return _err(str(e))


@app.post("/api/admin/compose/rebuild")
def admin_compose_rebuild(body: ComposeRebuildBody, _=Depends(require_admin)):
    """强制重建：--pull always 回源拉镜像 + --build 重编本地镜像 + --force-recreate 重建容器。"""
    try:
        ok, out, detached = compose_mod.rebuild(body.path, build=bool(body.build))
        return {"ok": ok, "message": out, "detached": detached}
    except Exception as e:
        return _err(str(e))


@app.post("/api/admin/compose/new")
def admin_compose_new(body: ComposeNewBody, _=Depends(require_admin)):
    try:
        res = compose_mod.create(body.name, body.content,
                                 up_after=bool(body.up), pull=bool(body.pull))
        return dict(res, ok=bool(res.get("ok")))
    except Exception as e:
        return _err(str(e))


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


class AutoApplyBody(BaseModel):
    names: list = []          # 空 = 全部勾选的；传了就只处理这几个（勾选框点了立刻生效用）


@app.post("/api/admin/images/auto/apply")
def admin_images_auto_apply(body: AutoApplyBody, _=Depends(require_admin)):
    """立刻执行一次「检查 + 自动更新」（勾选自动更新后马上生效）。

    2026-10-22 用户反馈：勾了「自动更新」的镜像，检查到有更新却不动。
    根因是自动更新只在 cron 到点时跑，手动检查/勾选都不会触发；这里补上第三个入口。
    """
    s = db.get_settings()
    if not s.get("allow_image_update"):
        return _err("面板的「允许从面板更新镜像」没打开（需要 socket-proxy 放开 POST）")
    if not s.get("image_auto_update"):
        return _err("还没打开「检查到更新就自动更新」开关")
    rows, err, _cached = _image_rows(force=True)
    want = {str(x) for x in (body.names or [])}
    targets = [r for r in _auto_targets(rows, s) if not want or r.get("container") in want]
    if not targets:
        why = "勾选的镜像当前都是最新的（没有需要更新的）"
        if want:
            st = {r.get("container"): r.get("status") for r in rows if r.get("container") in want}
            bad = [k for k, v in st.items() if v != "outdated"]
            if bad:
                why = "、".join(bad) + " 当前不需要更新（或取不到远端摘要）"
        return {"ok": True, "targets": [], "reason": why}
    started = _apply_updates(targets, "勾选自动更新")
    return {"ok": True, "targets": started, "reason": ""}


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


# ---------------------------------------------------------------- 备份与恢复（2.2.0）

def _bak_json(**kw):
    d = {"ok": True}
    d.update(kw)
    return d


@app.get("/api/admin/backups")
def admin_backups(_=Depends(require_admin)):
    """服务器端快照列表（存在 /data/backups，容器重建也在）。"""
    s = db.get_settings()
    return _bak_json(dir=backup_mod.BACKUP_DIR, files=backup_mod.list_backups(),
                     auto=bool(s.get("backup_auto", True)),
                     keep=int(s.get("backup_keep") or backup_mod.MAX_KEEP),
                     data_dir=db.DATA_DIR)


@app.get("/api/admin/backup/export")
def admin_backup_export(_=Depends(require_admin)):
    """现打一个包直接下载（不留服务器副本）。"""
    import tempfile
    name = backup_mod._ts_name()
    tmp = os.path.join(tempfile.gettempdir(), name)
    try:
        info = backup_mod.build_zip(tmp)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"打包失败：{e}"}, status_code=500)
    return FileResponse(tmp, filename=name, media_type="application/zip",
                        background=BackgroundTask(lambda: os.path.exists(tmp) and os.remove(tmp)))


@app.post("/api/admin/backup/snapshot")
def admin_backup_snapshot(_=Depends(require_admin)):
    """在服务器上留一份快照（就是「备份到 /data/backups」，方便新容器直接用）。"""
    try:
        info = backup_mod.snapshot(kind="manual")
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"备份失败：{e}"}, status_code=500)
    return _bak_json(**info, files_list=backup_mod.list_backups())


@app.get("/api/admin/backups/{name}")
def admin_backup_download(name: str, _=Depends(require_admin)):
    name = os.path.basename(name)
    p = os.path.join(backup_mod.BACKUP_DIR, name)
    if not name.endswith(".zip") or not os.path.isfile(p):
        raise HTTPException(404, "备份不存在")
    return FileResponse(p, filename=name, media_type="application/zip")


@app.delete("/api/admin/backups/{name}")
def admin_backup_delete(name: str, _=Depends(require_admin)):
    name = os.path.basename(name)
    p = os.path.join(backup_mod.BACKUP_DIR, name)
    if name.endswith(".zip") and os.path.isfile(p):
        os.remove(p)
    return _bak_json(files_list=backup_mod.list_backups())


async def _after_restore():
    """恢复完把网关/FRP 按新数据重新对齐（不签证书，避免恢复时触发一堆 ACME）。"""
    try:
        s = db.get_settings()
        if _gw_should_sync(s):
            await _gw_sync_all(issue=False)
        svc = gw_mod.instance()
        if svc and s.get("frp_enabled"):
            svc.frp.apply(svc.cfg(), svc.frp_proxies())
    except Exception as e:
        print(f"[navpage] 恢复后重载网关/FRP 失败（可手动点同步）：{e}")


class BackupRestoreBody(BaseModel):
    name: str = ""
    restore_password: bool = False


@app.post("/api/admin/backup/restore")
async def admin_backup_restore(file: UploadFile = File(...), restore_password: bool = False,
                               _=Depends(require_admin)):
    """上传备份包 → 整体恢复。恢复前会自动给当前状态留一份 prerestore 快照。"""
    import tempfile
    raw = await file.read()
    if not raw:
        return _err("备份包是空的")
    if len(raw) > 200 * 1024 * 1024:
        return _err("备份包不能超过 200MB")
    tmp = os.path.join(tempfile.gettempdir(), "np-restore-" + str(int(time.time())) + ".zip")
    with open(tmp, "wb") as f:
        f.write(raw)
    try:
        res = backup_mod.restore_zip(tmp, restore_password=bool(restore_password))
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"恢复失败：{e}"}, status_code=500)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    await _after_restore()
    return _bak_json(**res, files_list=backup_mod.list_backups())


@app.post("/api/admin/backup/restore-local")
async def admin_backup_restore_local(body: BackupRestoreBody, _=Depends(require_admin)):
    """用服务器上已有的快照恢复（新容器里最常用：同一个 /data 卷，直接选一份恢复）。"""
    name = os.path.basename(body.name or "")
    p = os.path.join(backup_mod.BACKUP_DIR, name)
    if not name.endswith(".zip") or not os.path.isfile(p):
        return _err("找不到这份快照")
    try:
        res = backup_mod.restore_zip(p, restore_password=bool(body.restore_password))
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"恢复失败：{e}"}, status_code=500)
    await _after_restore()
    return _bak_json(**res, files_list=backup_mod.list_backups())


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


def _candidate(c, s, src=None):
    """生成候选条目。

    本机（Docker socket）继续用 `{scheme}://{host}:{port}` 模板：容器里拿不到
    局域网 IP，模板在请求时按浏览器 Host 头展开。
    远端采集源不适用这个前提（端口开在另一台机器上），改用该源配置的地址前缀
    拼绝对地址；容器自带 nav.url.lan 标签时以标签为准。
    """
    src = src or LOCAL_SOURCE
    if (src.get("kind") or "local") == "agent":
        prefix = (src.get("prefix") or "").strip()
        port = c.get("primary_port")
        lan = c.get("nav_url_lan") or ""
        if not lan and prefix and port:
            lan = (prefix if prefix.endswith(":") else prefix.rstrip("/") + ":") + str(port)
        # 远端主机（云主机 / 另一网段）通常内网外网同一个地址，外网缺省跟随内网，
        # 否则切到「外网」模式时这些卡片会变成没有地址。
        wan = c.get("nav_url_wan") or lan
    else:
        lan = c["nav_url_lan"] or netutil.template_url(c["primary_port"], "http")
        wan = c["nav_url_wan"]
        # 没端口时不要用 host_wan 硬拼，否则会得到 https://<域名>（指向导航页本身）
        if not wan and s.get("host_wan") and c["primary_port"]:
            wan = netutil.build_url(s["host_wan"], c["primary_port"], s.get("wan_scheme") or "https")
    return {
        "name": c.get("nav_name") or c["name"],
        "url_lan": lan,
        "url_wan": wan,
        "icon": c.get("nav_icon") or "",
        "category": c.get("nav_category") or "",
        "auto_category": c.get("nav_category") or auto_category({
            "name": c.get("nav_name") or c["name"], "container_name": c["name"],
            "container_image": c.get("image") or ""}),
        "container_name": c["name"],
        "container_image": c.get("image") or "",
        "container_port": c.get("primary_port"),
        "docker_host": str(src.get("id") or ""),
        "source_name": src.get("name") or "本机",
    }


# ---------------------------------------------------------------- 采集源
# 本机（Docker socket）是隐式的：items.docker_host='' 即代表本机。
# 远端主机跑一个 agent 容器，每 5 分钟把容器清单 POST 到 /api/agent/inventory。
LOCAL_SOURCE = {"id": "", "name": "本机", "kind": "local", "prefix": "", "enabled": True,
                "auto_import": False, "last_seen": 0, "last_error": "", "host_name": ""}


def _source_by_id(sid):
    sid = str(sid or "")
    if sid == "":
        return dict(LOCAL_SOURCE)
    return db.get_source(sid)


def _sources():
    return [dict(LOCAL_SOURCE)] + db.list_sources(only_enabled=False)


def _source_view(src, now=None):
    """给前端看的采集源（永远不含令牌哈希），带在线状态和「多久前上报」。"""
    now = now or int(time.time())
    d = {k: v for k, v in src.items() if k not in ("token_hash", "containers", "token")}
    d["container_count"] = len(src.get("containers") or []) if src.get("kind") == "agent" else 0
    d["id"] = "" if src.get("kind") == "local" else str(src.get("id") or "")
    last = int(src.get("last_seen") or 0)
    # 5 分钟一次上报，给到 15 分钟容差；本机永远算在线（读不到 Docker 会单独报错）
    d["online"] = (src.get("kind") == "local") or bool(last and now - last < 900)
    d["ago"] = (now - last) if last else None
    return d


def _url_netloc(u):
    try:
        return urllib.parse.urlparse((u or "").strip()).netloc.lower()
    except Exception:
        return ""


def _name_key(u):
    return "".join(ch for ch in (u or "").casefold() if ch.isalnum())


def _find_similar_item(cand):
    """找一张「看起来就是同一个服务」的已有卡片（用户手工建过的那种）。

    远端采集源是新功能，很多服务用户早就在导航页里手工放过卡片了（甚至已经
    分好组、配好域名）。自动建卡如果只管 (docker_host, container_name)，
    就会在旁边再长出一张一模一样的卡片 —— 首页重复、看着像 bug。
    这里按两件事认领：地址主机相同（域名/端口对得上），或者名字几乎一样。
    """
    hosts = {h for h in (_url_netloc(cand.get("url_lan")), _url_netloc(cand.get("url_wan"))) if h}
    nk = _name_key(cand.get("name"))
    cname = (cand.get("container_name") or "").strip()
    best = None
    for it in db.list_items():
        if it.get("hidden"):
            continue
        # 容器名一致 = 同一个容器（用户可能把卡片改名了，容器名不会变）
        if cname and (it.get("container_name") or "").strip() == cname:
            return it
        if hosts:
            for f in ("url_lan", "url_wan"):
                if _url_netloc(it.get(f)) in hosts:
                    return it
        # 名字只做「完全相同」匹配：包含关系会把 navpage 认成 navpage-agent-once
        ik = _name_key(it.get("name"))
        if nk and ik and nk == ik:
            best = best or it
    return best


def _adopt(it, cand, host, c):
    """把已有卡片接管成本采集源的容器卡片：只补容器信息，名称/分组/地址都不动。"""
    patch = {"docker_host": host, "source": "docker", "container_name": c["name"], "missing": False}
    if c.get("image"):
        patch["container_image"] = c["image"]
    if c.get("primary_port"):
        patch["container_port"] = int(c["primary_port"])
    # 只在卡片完全没有地址时才补：已有地址（哪怕只是 {scheme}://{host} 模板）
    # 就说明用户自己安排过，别把「常用」里的卡片也改成域名，那样会和
    # 「甲骨文」分组里的同名卡片在首页撞成两张一样的链接。
    if not (it.get("url_lan") or "").strip() and not (it.get("url_wan") or "").strip():
        if cand.get("url_wan"):
            patch["url_wan"] = cand["url_wan"]
        if cand.get("url_lan"):
            patch["url_lan"] = cand["url_lan"]
    db.update_item(it["id"], patch)
    return it["id"]


def _collect(s=None, ok_hosts=None):
    """把所有采集源的容器摊平成 [(source, container)]。

    本机实时读 Docker；远端用最近一次上报的快照（离线也能看历史清单）。
    ok_hosts 用来判断「这次谁真的报了数据」，避免把离线源的卡片误标成已消失。
    """
    s = s or db.get_settings()
    out, errors = [], []
    local_ok = False
    try:
        ok, info = dockerapi.available()
        if ok:
            local_ok = True
            for c in dockerapi.containers():
                out.append((dict(LOCAL_SOURCE), c))
        else:
            errors.append({"source": "本机", "error": info.get("error") or "Docker 不可用"})
    except Exception as e:
        errors.append({"source": "本机", "error": f"{type(e).__name__}: {e}"})
    if ok_hosts is not None and local_ok:
        ok_hosts.add("")
    for src in db.list_sources(only_enabled=True):
        for c in (src.get("containers") or []):
            out.append((src, c))
        if ok_hosts is not None:
            ok_hosts.add(str(src["id"]))
    return out, errors


def _create_from_candidate(cand, host, cat_name=None):
    return db.create_item({
        "name": cand["name"], "url_lan": cand["url_lan"], "url_wan": cand["url_wan"],
        "icon": cand["icon"], "source": "docker",
        "category_id": db.get_or_create_category(cat_name) if cat_name else None,
        "container_name": cand["container_name"], "container_image": cand["container_image"],
        "container_port": cand["container_port"], "docker_host": host,
        "auto_created": True,
    })


# ---------------------------------------------------------------- Agent 上报
_AGENT_HITS = {}
_AGENT_LOCK = threading.Lock()
AGENT_MAX_BODY = 512 * 1024
AGENT_RATE = (30, 60)  # 每令牌每 60 秒最多 30 次


def _agent_rate_ok(sid):
    now = time.time()
    with _AGENT_LOCK:
        q = [t for t in _AGENT_HITS.get(sid, []) if now - t < AGENT_RATE[1]]
        if len(q) >= AGENT_RATE[0]:
            _AGENT_HITS[sid] = q
            return False
        q.append(now)
        _AGENT_HITS[sid] = q
        return True


def _agent_auto_import(src, s):
    """把上报的容器写进卡片表：新容器自动建卡，已存在的刷新镜像/端口并清 missing。"""
    host = str(src["id"])
    imported = updated = 0
    live = set()
    for c in (src.get("containers") or []):
        name = (c.get("name") or "").strip()
        if not name:
            continue
        live.add(name)
        if not _eligible(c, s):
            continue
        cand = _candidate(c, s, src)
        old = db.find_item_by_container(name, host)
        if not old:
            sim = _find_similar_item(cand)
            if sim:
                _adopt(sim, cand, host, c)
                updated += 1
                continue
        if old:
            patch = {"missing": False}
            if c.get("image") and c["image"] != old.get("container_image"):
                patch["container_image"] = c["image"]
            if c.get("primary_port") and int(c["primary_port"]) != int(old.get("container_port") or 0):
                patch["container_port"] = int(c["primary_port"])
            # 地址还是空/模板的，顺手补成该源的绝对地址
            old_lan = old.get("url_lan") or ""
            if cand["url_lan"] and (not old_lan or "{host}" in old_lan):
                patch["url_lan"] = cand["url_lan"]
            db.update_item(old["id"], patch)
            updated += 1
            continue
        if not src.get("auto_import"):
            continue
        _create_from_candidate(cand, host, cand["auto_category"])
        imported += 1
    missing = db.mark_missing(live, host)
    return {"imported": imported, "updated": updated, "missing": missing, "live": len(live)}


@app.post("/api/agent/inventory")
async def agent_inventory(request: Request):
    raw = await request.body()
    if len(raw) > AGENT_MAX_BODY:
        return _err("上报内容过大（>512KB）", 413)
    token = (request.headers.get("x-agent-token") or "").strip()
    if not token:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        return _err("缺少令牌", 401)
    src = db.source_by_token_hash(hashlib.sha256(token.encode()).hexdigest())
    if not src:
        return _err("令牌无效或采集源已停用", 401)
    if not _agent_rate_ok(src["id"]):
        return _err("上报太频繁，请稍后重试", 429)
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        return _err("JSON 解析失败")
    cs = payload.get("containers")
    if not isinstance(cs, list):
        return _err("containers 必须是数组")
    cs = [c for c in cs if isinstance(c, dict) and c.get("name")][:500]
    db.record_inventory(src["id"], cs, str(payload.get("host_name") or "")[:64])
    src = db.get_source(src["id"]) or src
    try:
        res = _agent_auto_import(src, db.get_settings())
    except Exception as e:
        db.set_source_error(src["id"], f"{type(e).__name__}: {e}")
        res = {"imported": 0, "updated": 0, "missing": 0, "live": 0}
    return {"ok": True, "server_time": int(time.time()), **res}


# ---------------------------------------------------------------- Agent 安装地址（2.2.6）
# 为什么需要它：agent 安装命令是拿到**对端 VPS** 上执行的，命令里的 PANEL_URL 必须
# 是对端能访问到的地址。以前前端直接写 location.origin —— 从内网 IP 打开面板时，
# 生成的命令就带着 192.168.x.x，粘到 VPS 上必然连不上，只能手工改。
# 现在由服务端把所有可能的面板地址排好序给前端，前端做成下拉（也可手填）。
_PUBLIC_BASE_CACHE = {"value": None}


def _req_origin(request: Request) -> str:
    s = db.get_settings()
    trust = bool(s.get("trust_proxy"))
    # X-Forwarded-Proto 一律采用：反代/隧道（frps、nginx）都会带，它才是浏览器看到的协议。
    # 只有 X-Forwarded-Host 需要开「信任反代」才认——那个头会决定卡片的内外网归属。
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    host = ""
    if trust:
        host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    proto = proto or request.url.scheme or "http"
    host = host or request.headers.get("host") or ""
    return (f"{proto}://{host}").rstrip("/") if host else ""


def _host_is_private(host_header: str) -> bool:
    """内网 / 本机地址判定。域名一律视为公网入口。"""
    host = netutil._hostname_of(host_header)
    if not host or host.lower() == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except Exception:
        return False
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local)


@app.get("/api/admin/agent/base_urls")
def agent_base_urls(request: Request, _=Depends(require_admin)):
    s = db.get_settings()
    out, seen = [], set()

    def add(url, label, public):
        url = (url or "").strip().rstrip("/")
        if not url.startswith("http") or url in seen:
            return
        seen.add(url)
        out.append({"url": url, "label": label, "public": bool(public)})

    add(s.get("agent_public_base") or "", "上次的公网访问地址", True)
    wan = (s.get("host_wan") or "").strip()
    if wan:
        add(netutil.build_url(wan, 0, s.get("wan_scheme") or "https"), "设置里的「外网地址」", True)
    ddns = (s.get("ddns_record") or "").strip()
    if ddns:
        add("https://" + ddns, "DDNS 域名", True)
    cur = _req_origin(request)
    if cur:
        add(cur, "当前访问地址", not _host_is_private(request.headers.get("host") or ""))

    suggested = next((c["url"] for c in out if c["public"]), cur)
    return {"ok": True, "candidates": out, "suggested": suggested, "current": cur}


@app.middleware("http")
async def _learn_public_base(request, call_next):
    """记住「用公网域名访问过面板」的那个地址，下次生成 agent 命令直接用它。

    只对 API 请求记（静态资源不记），且值变了才落库，正常运行时几乎不产生写。
    """
    resp = await call_next(request)
    try:
        if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/health"):
            host = request.headers.get("host") or ""
            if host and not _host_is_private(host):
                origin = _req_origin(request)
                if origin and origin != _PUBLIC_BASE_CACHE["value"]:
                    _PUBLIC_BASE_CACHE["value"] = origin
                    if (db.get_settings().get("agent_public_base") or "") != origin:
                        db.update_settings({"agent_public_base": origin})
    except Exception:
        pass
    return resp


# ---------------------------------------------------------------- 采集源管理
@app.get("/api/admin/sources")
def sources_list(_=Depends(require_admin)):
    now = int(time.time())
    return {"ok": True, "sources": [_source_view(x, now) for x in _sources()]}


class SourceBody(BaseModel):
    name: str = ""
    prefix: str = ""
    auto_import: bool = True
    enabled: bool = True


@app.post("/api/admin/sources")
def sources_create(body: SourceBody, _=Depends(require_admin)):
    name = (body.name or "").strip()
    if not name:
        return _err("名称不能为空")
    sid = db.create_source(name, body.prefix, body.auto_import)
    token = "npk_" + secrets.token_hex(16)
    db.set_source_token(sid, hashlib.sha256(token.encode()).hexdigest(), token[-6:])
    return {"ok": True, "id": sid, "token": token}


@app.patch("/api/admin/sources/{sid}")
def sources_update(sid: str, body: SourceBody, _=Depends(require_admin)):
    if not db.get_source(sid):
        return _err("采集源不存在", 404)
    db.update_source(sid, body.dict(exclude_unset=True))
    return {"ok": True}


@app.post("/api/admin/sources/{sid}/rotate")
def sources_rotate(sid: str, _=Depends(require_admin)):
    if not db.get_source(sid):
        return _err("采集源不存在", 404)
    token = "npk_" + secrets.token_hex(16)
    db.set_source_token(sid, hashlib.sha256(token.encode()).hexdigest(), token[-6:])
    return {"ok": True, "token": token}


@app.delete("/api/admin/sources/{sid}")
def sources_delete(sid: str, keep_manual: bool = False, _=Depends(require_admin)):
    """删采集源。

    keep_manual=false（默认）：该源自动建的卡片一起删掉；
      手工建/被认领的卡片保留，只解除绑定。
    keep_manual=true：所有卡片都保留，只解除绑定。
    """
    if not db.get_source(sid):
        return _err("采集源不存在", 404)
    res = db.delete_source(sid, keep_manual=keep_manual)
    return {"ok": True, **res}


# ---------------------------------------------------------------- 扫描 / 导入
@app.get("/api/admin/scan")
def scan(_=Depends(require_admin)):
    s = db.get_settings()
    ok, info = dockerapi.available()
    ok_hosts = set()
    pairs, errors = _collect(s, ok_hosts)
    _items = db.list_items()
    known = {(str(i.get("docker_host") or ""), i.get("container_name") or "")
             for i in _items if i.get("container_name")}
    # 名字兜底：采集源删掉后卡片会只剩 container_name（docker_host 清空），
    # 这些容器不该再算「新发现」，否则重扫一遍还是整排冒出来。
    known_names = {i["container_name"] for i in _items if i.get("container_name")}
    live, new, existing, skipped = set(), [], [], []
    for src, c in pairs:
        key = (str(src.get("id") or ""), c.get("name") or "")
        live.add(key)
        if not _eligible(c, s):
            reason = _skip_reason(c, s)
            if reason != "已排除":      # 已排除的有专门的「排除名单」区块，不重复堆在已跳过里
                skipped.append({"name": c.get("name"), "container_name": key[1],
                                "source_name": src.get("name") or "本机", "reason": reason})
            continue
        cand = _candidate(c, s, src)
        cand.update({"id": c.get("id", ""), "image": c.get("image", ""),
                     "status": c.get("status", ""), "public_ports": c.get("public_ports") or [],
                     "project": c.get("compose_project", ""),
                     "no_port": not c.get("primary_port"), "state": c.get("state", "")})
        (existing if (key in known or key[1] in known_names) else new).append(cand)

    missing = []
    for i in db.list_items():
        if i.get("source") != "docker" or not i.get("container_name"):
            continue
        host = str(i.get("docker_host") or "")
        if i.get("hidden"):
            continue          # 已被排除/删除的卡片不用再提示「已消失」
        if host in ok_hosts and (host, i["container_name"]) not in live:
            missing.append({"name": i["name"], "container_name": i["container_name"],
                            "id": i["id"], "docker_host": host,
                            "source_name": (_source_by_id(host) or {}).get("name") or "本机"})

    views = [_source_view(x) for x in _sources()]
    for v in views:                      # 本机没有上报数，用实时扫到的容器数补上，别显示 0
        if (v.get("kind") or "local") == "local":
            v["container_count"] = sum(1 for k in live if k[0] == str(v.get("id") or ""))
    # 排除名单要能在界面上看见，否则「被排除了」这件事对用户是隐形的：
    # 容器不出现、也不在新发现里，只有「已跳过」里一行灰字（2026-10-26 用户报 gately 凭空消失）。
    excl_names = [x.strip() for x in (s.get("scan_exclude") or "").split(",") if x.strip()]
    live_names = {c.get("name") for _src, c in pairs}
    excluded = [{"name": n, "exists": n in live_names} for n in excl_names]
    return {"ok": True, "docker": info, "new": new, "existing": existing,
            "excluded": excluded,
            "missing": missing, "skipped": skipped, "total": len(live),
            "errors": errors, "sources": views, "auto": _auto_scan_info()}


class ExcludeBody(BaseModel):
    names: list = []
    excluded: bool = True


@app.post("/api/admin/scan/exclude")
def scan_exclude(body: ExcludeBody, _=Depends(require_admin)):
    """把「新发现」里的候选直接排除掉（写 scan_exclude，重扫也不再冒出来）。

    候选还没建卡片，没法走 items 的排除接口，所以这里按容器名写设置。
    """
    s = db.get_settings()
    cur = [x.strip() for x in (s.get("scan_exclude") or "").split(",") if x.strip()]
    for n in body.names:
        n = (n or "").strip()
        if not n:
            continue
        if body.excluded and n not in cur:
            cur.append(n)
        if not body.excluded and n in cur:
            cur.remove(n)
    db.update_settings({"scan_exclude": ",".join(cur)})
    return {"ok": True, "scan_exclude": ",".join(cur)}


class ImportBody(BaseModel):
    names: list = []
    items: list = []
    host: str = ""
    category_from_project: bool = True


@app.post("/api/admin/scan/import")
def scan_import(request: Request, body: ImportBody, _=Depends(require_admin)):
    s = db.get_settings()
    ctx = netutil.host_context(request, s)
    pairs, _errors = _collect(s)
    idx = {(str(src.get("id") or ""), c.get("name") or ""): (src, c) for src, c in pairs}
    wanted = []
    for it in (body.items or []):
        if isinstance(it, dict) and it.get("name"):
            wanted.append((str(it.get("host") or ""), it["name"]))
    if not wanted:
        wanted = [(str(body.host or ""), n) for n in (body.names or [])]
    added, updated = 0, 0
    for host, name in wanted:
        pair = idx.get((host, name))
        if not pair:
            continue
        src, c = pair
        if not _eligible(c, s):
            continue
        cand = _candidate(c, s, src)
        cat_name = cand["auto_category"] if body.category_from_project else cand["category"]
        old = db.find_item_by_container(name, host)
        if old:
            # 模板地址可以随端口变化刷新；用户手填的绝对地址不动
            patch = {"missing": False}
            if cat_name and not old.get("category_id"):
                patch["category_id"] = db.get_or_create_category(cat_name)
            if cand["url_lan"] and (not old.get("url_lan") or "{host}" in (old.get("url_lan") or "")):
                patch["url_lan"] = cand["url_lan"]
            if cand["url_wan"] and not old.get("url_wan"):
                patch["url_wan"] = cand["url_wan"]
            db.update_item(old["id"], patch)
            updated += 1
        else:
            sim = _find_similar_item(cand)
            if sim:
                _adopt(sim, cand, host, c)
                updated += 1
                continue
            icon = cand["icon"] or ""
            # 远端主机取不到 favicon（网络不一定通），只在图标标签缺失时给本机试
            if not icon and host == "":
                f, _why = icons.fetch_favicon(_fetchable_url(cand["url_lan"], ctx))
                icon = f or ""
            cand["icon"] = icon
            _create_from_candidate(cand, host, cat_name)
            added += 1
    proxied, proxy_err = 0, ""      # Gately 面板已下线（2.0.4），反代走内置网关
    # 2.0.0 内置网关：开了「自动反代」就把新卡片的域名/地址补上，
    # 证书在后台签（不能让用户对着「正在导入」转圈等 40 秒）
    gw_auto = False
    if s.get("gw_auto_proxy") and s.get("gw_enabled") and gw_mod.instance():
        gw_auto = bool(_run_bg(_gw_sync_all(issue=bool(s.get("gw_auto_cert", True)))))
    return {"ok": True, "added": added, "updated": updated,
            "proxied": proxied, "proxy_error": proxy_err, "gw_auto": gw_auto}


@app.post("/api/admin/scan/sync")
async def scan_sync(request: Request, _=Depends(require_admin)):
    s = db.get_settings()
    ctx = netutil.host_context(request, s)
    r = _sync_containers(ctx)
    # 全量同步也要补反代 / FRP 地址（2026-10-26 修：以前只有「导入选中」才补，
    # 用户点全量同步后新卡片的外网地址一直是空的）。
    gw_auto = False
    if _gw_should_sync(s):
        gw_auto = bool(_run_bg(_gw_sync_all(issue=bool(s.get("gw_auto_cert", True)))))
    return {"ok": True, "added": r["added"], "updated": r["updated"], "gw_auto": gw_auto}


def _need_addr_sync(s) -> bool:
    """还有卡片缺「该有的」自动地址吗？恢复卡片、手建卡片、改过端口的卡片都靠它兜住。

    以前自动扫描只在 `added > 0` 时才补 FRP 地址，于是「从排除名单恢复 / 手工新建」
    的卡片永远拿不到 url_frp（2026-10-26 用户报 gately 没有自动生成 FRP 地址）。
    """
    wants_frp = bool(s.get("frp_enabled"))
    wants_proxy = bool(s.get("gw_enabled")) and bool(s.get("gw_auto_proxy"))
    if not (wants_frp or wants_proxy):
        return False
    for it in db.list_items(include_hidden=True):
        if not (it.get("url_lan") or "").strip():
            continue
        if wants_frp and not (it.get("url_frp") or "").strip():
            return True
        if wants_proxy and not (it.get("proxy_domain") or "").strip():
            return True
    return False


def _gw_should_sync(s) -> bool:
    """扫描/同步后要不要顺带补反代与 FRP 地址。"""
    if s.get("gw_enabled") and s.get("gw_auto_proxy") and gw_mod.instance():
        return True
    # 只开了 FRP 没开内置反代：也要把 FRP 地址写回卡片
    return bool(s.get("frp_enabled")) and bool(gw_mod.instance())


def _sync_containers(ctx):
    """容器同步主体：新容器建卡、已有卡片刷新模板地址与「已消失」标记。

    手动「全量同步」和后台「自动扫描」共用这一份逻辑，避免两边行为不一致。
    """
    s = db.get_settings()
    pairs, _errors = _collect(s)
    added, updated = 0, 0
    names_by_host = {}
    for src, c in pairs:
        host = str(src.get("id") or "")
        names_by_host.setdefault(host, set()).add(c.get("name"))
        if not _eligible(c, s):
            continue
        cand = _candidate(c, s, src)
        cat_name = cand["auto_category"]
        old = db.find_item_by_container(c["name"], host)
        if old:
            # 保留用户改过的名称/图标/描述；只刷新模板地址与存在性
            # 端口要尊重卡片现有值：一个容器常同时发布多个端口（nezha 的 5555 和
            # 8008、navpage 的 8081/18443/8095），按 docker 返回顺序挑「主端口」
            # 会来回抖，把卡片上本来对的端口覆盖成对外根本不通的那个。
            old_lan = old.get("url_lan") or ""
            card_port = 0
            try:
                _u = urlsplit(old_lan.replace("{scheme}", "http").replace("{host}", "h"))
                card_port = int(_u.port or 0)
            except Exception:
                card_port = 0
            published = set(c.get("public_ports") or [])
            # host 网络容器（gately / 家里那些 network_mode: host 的）没有 ports 映射，
            # published 是空集：这时不能把卡片上填的端口冲成 0，否则 FRP 目标端口就丢了
            # （2026-10-26 实测：gately 的 16680 被冲成 0，卡片一直没 FRP 地址）。
            keep_port = card_port if (card_port and (card_port in published or not published)) else 0
            patch = {"missing": False, "container_image": c.get("image") or "",
                     "container_port": keep_port or (c.get("primary_port") or 0)}
            if cat_name and not old.get("category_id"):
                patch["category_id"] = db.get_or_create_category(cat_name)
            if cand["url_lan"] and not keep_port and (not old_lan or "{host}" in old_lan) and cand["url_lan"] != old_lan:
                patch["url_lan"] = cand["url_lan"]
            if cand["url_wan"] and not old.get("url_wan"):
                patch["url_wan"] = cand["url_wan"]
            db.update_item(old["id"], patch)
            updated += 1
        else:
            icon = cand["icon"] or ""
            if not icon and host == "":
                f, _why = icons.fetch_favicon(_fetchable_url(cand["url_lan"], ctx))
                icon = f or ""
            cand["icon"] = icon
            _create_from_candidate(cand, host, cat_name)
            added += 1
    for host, names in names_by_host.items():
        db.mark_missing(names, host)
    return {"added": added, "updated": updated}


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
    explicit = bool(body.ids)          # 勾选了具体条目 = 用户明确要重抓，允许覆盖
    if explicit:
        want = {int(x) for x in body.ids}
        items = [i for i in items if i["id"] in want]
    total = len(items)
    if not explicit:
        # 先把已有图标的一律剔掉再抓，别让它们白占网络请求（之前会先抓完再跳过）。
        items = [i for i in items if not (i.get("icon") or "").strip()]
    pre_skipped = total - len(items)
    # 并发 8 路：单个站点最坏要等十几秒（被墙站点要先超时再兜底），
    # 70 多条串行会等到浏览器先超时。
    online_ok = bool(s.get("icon_online_fallback", True))

    letter_ok = bool(s.get("icon_letter_fallback", True))

    def one(it):
        base = _fetchable_url(it.get("url_lan"), ctx) or _fetchable_url(it.get("url_wan"), ctx)
        if not base:
            # 连地址都没有（纯内网/无前端服务）：照样给一张首字母图
            if letter_ok:
                return it, icons.make_letter_icon(it.get("name") or ""), "首字母占位图（无可用地址）", True
            return it, None, "地址展开不出来（模板占位符没配）", False
        f, why = icons.fetch_favicon(base, online=online_ok)
        if not f and letter_ok:
            return it, icons.make_letter_icon(it.get("name") or "", base), "首字母占位图（" + why[:60] + "）", True
        return it, f, why, False

    done, fail, skipped = 0, 0, 0
    misses = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for it, f, why, is_letter in pool.map(one, items):
            has_icon = bool((it.get("icon") or "").strip())
            # 首字母图只是兜底：条目本来就有（真）图标时绝不覆盖，避免批量重抓把好图标冲掉
            if is_letter and has_icon:
                skipped += 1
                continue
            # 未勾选具体条目（「补齐缺失图标」）时，已有图标的一律不动 ——
            # 之前全量重抓把图标库里的矢量图标换成了站点小 favicon，视觉反而变差。
            if has_icon and not explicit:
                skipped += 1
                continue
            if f:
                db.update_item(it["id"], {"icon": f})
                done += 1
            else:
                fail += 1
                misses.append({"name": it.get("name") or str(it["id"]), "why": why})
    return {"ok": True, "done": done, "fail": fail, "skipped": skipped + pre_skipped,
            "misses": misses[:8]}




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


def _self_container():
    """面板自身容器的名字。更新它会把本进程连同调度线程一起杀掉。"""
    return (os.environ.get("NAVPAGE_SELF_NAME") or "navpage").strip()


def _auto_targets(rows, s=None):
    """从检查结果里挑「勾了自动更新 ∩ 状态 outdated ∩ 不是本地构建」的容器，自身排最后。

    为什么自身必须排最后（2026-10-22 用户反馈「勾选了自动更新却没更新」的真凶之一）：
    navpage 更新自身时容器被重建，跑循环的这个进程直接消失 —— 排在前面的话，
    同一批里后面的目标一个都不会更新，用户看到的就是「只更新了一个/一个都没更新」。
    """
    s = s or db.get_settings()
    names = {str(x) for x in (s.get("image_auto_names") or [])}
    out = [r for r in rows
           if r.get("status") == "outdated"
           and r.get("container") in names
           and not r.get("local_only") and not r.get("is_local_build")]
    me = _self_container()
    out.sort(key=lambda r: 1 if r.get("container") == me else 0)
    return out


def _apply_updates(targets, source):
    """后台串行更新一批容器（一次一个，等上一个跑完），每个结果都落更新历史。

    抽出来是因为三个入口共用：定时检查、手动「检查更新」、勾选「自动更新」。
    之前只有定时检查会真的更新 —— 手动点「检查更新」看到「可更新」却什么都不发生，
    就成了「勾了自动更新，检查到更新也不更新」（2026-10-22 用户反馈）。
    """
    if not targets:
        return []

    def _worker():
        for r in targets:
            name = r.get("container")
            ok, t = _spawn_update(name, pull=True)
            if not ok:
                _hist_add({"ts": int(time.time()), "container": name,
                           "image": r.get("image") or "", "success": False,
                           "elapsed": None, "local_only": False,
                           "message": "%s：没能发起更新（%s）" % (source, t), "log": []})
                continue
            # 等这一个跑完再动下一个：同时更新多个容器会一起停服务，日志也糊成一团
            deadline = time.time() + 1800
            while t.running and time.time() < deadline:
                time.sleep(1)

    threading.Thread(target=_worker, name="navpage-auto-apply", daemon=True).start()
    # 立刻返回「这一批要更新谁」：真实结果看更新历史（更新自身时进程会重启，收尾写不进来）
    return [r.get("container") for r in targets]


def _auto_run():
    """到点了：查一遍更新；该自动更新的按顺序更新（一次一个容器）。"""
    s = db.get_settings()
    names = [str(x) for x in (s.get("image_auto_names") or [])]
    rows, err, _cached = _image_rows(force=True)
    outdated = [r for r in rows if r.get("status") == "outdated"]
    targets = _auto_targets(rows, s)
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
    name: str = ""          # 抓不到 favicon 时用名称首字母生成占位图


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
    raw_lan = netutil.normalize_url(raw_lan)
    raw_wan = netutil.normalize_url(raw_wan)
    if not raw_lan and not raw_wan:
        return _err("先在地址栏填一个网址，再来抓图标")
    base = _fetchable_url(raw_lan, ctx) or _fetchable_url(raw_wan, ctx)
    if not base:
        return _err("地址里还有 {host} 占位符，但「设置 → 网络」里没填内网地址/外网域名，"
                    "展开不出真实网址")
    if not (base.startswith("http://") or base.startswith("https://")):
        return _err("地址要以 http:// 或 https:// 开头（当前是 " + base[:40] + "）")
    f, why = icons.fetch_favicon(base, online=bool(s.get("icon_online_fallback", True)))
    letter = False
    if not f and s.get("icon_letter_fallback", True):
        # 纯 API / 没有前端的容器根本没有 favicon，用名称首字母生成占位图，别让卡片空着
        label = (body.name or "").strip() or base.split("://", 1)[-1].split("/")[0]
        f = icons.make_letter_icon(label, base)
        letter = True
        if f:
            return {"ok": True, "icon": f, "from": base,
                    "source": "首字母占位图（" + why[:80] + "）", "letter": True}
    if not f:
        return _err(f"没抓到图标：{why}。可手动上传图标，或打开「首字母占位」")
    return {"ok": True, "icon": f, "from": base, "source": why, "letter": letter}


# ---------------------------------------------------------------- 反代面板联动（Gately）
#
# 与上面「粘贴导入」的区别：Gately 有公开 REST API，规则是明文 JSON，所以这里能直接
# 读规则映射成卡片外网地址，也能反过来给卡片建规则——「部署容器 → 自动反代 → 卡片
# 自动带上外网地址」这条链路就是靠这里闭合的。Lucky 做不到（规则被它自己加密）。

class GatelyProvisionBody(BaseModel):
    item_id: int
    subdomain: str = ""


def _gately_cfg(s=None):
    s = s or db.get_settings()
    return (s.get("gately_url") or "").strip(), (s.get("gately_token") or "").strip()


def _gately_client(s=None):
    s = s or db.get_settings()
    url, tok = _gately_cfg(s)
    if not url or not tok:
        raise HTTPException(400, "还没配置 Gately 地址和 API 令牌（设置 → 反代面板）")
    return gately.Client(url, tok)


def _gately_listen(s):
    return int(s.get("gately_listener_port") or 443), bool(s.get("gately_tls", True))


def _expand_lan(url, s, host=""):
    """把卡片内网地址展开成绝对地址（模板里的 {host}/{scheme} 换成真实值）。"""
    h = host or (s.get("host_lan_ip") or "").strip() or "127.0.0.1"
    return (url or "").strip().replace("{host}", h).replace("{scheme}", "http")


def _item_target(it, s):
    """卡片 → Gately 后端地址（http://host:port）。

    Gately 与端口在同一台机器时用 127.0.0.1 最稳（容器里也通）；配置了内网地址优先用它。
    """
    host = (s.get("host_lan_ip") or "").strip() or "127.0.0.1"
    port = int(it.get("container_port") or 0)
    lan = _expand_lan(it.get("url_lan"), s)
    if lan:
        u = lan if "://" in lan else "http://" + lan
        parts = urllib.parse.urlsplit(u)
        p = parts.port or port or (443 if parts.scheme == "https" else 80)
        return "http://%s:%d" % (parts.hostname or host, p)
    if not port:
        raise HTTPException(400, "这张卡片没有内网端口，先在「编辑」里补上端口再生成反代")
    return "http://%s:%d" % (host, port)


def _match_gately(row, items):
    """Gately 的一条规则 → 卡片。先比后端 host:port 与卡片内网地址，再退到端口 / 名称。"""
    host = (row.get("backend_host") or "").lower()
    port = int(row.get("backend_port") or 0)
    if port:
        same = []
        for it in items:
            lan = _expand_lan(it.get("url_lan"), db.get_settings(), "127.0.0.1")
            if not lan:
                continue
            u = lan if "://" in lan else "http://" + lan
            parts = urllib.parse.urlsplit(u)
            ih, ip = (parts.hostname or "").lower(), parts.port or 0
            if ip == port:
                same.append((it, ih))
        exact = [it for it, ih in same if host in (ih, "") or ih in ("127.0.0.1", "localhost", host)]
        if len(exact) == 1:
            return exact[0], "后端地址"
        cands = [it for it, _ih in same] or [i for i in items if int(i.get("container_port") or 0) == port]
        if len(cands) == 1:
            return cands[0], "端口"
        if cands:
            lab = netutil.norm_name(row.get("label") or "")
            for i in cands:
                if lab and lab in (netutil.norm_name(i.get("name")),
                                   netutil.norm_name(i.get("container_name") or "")):
                    return i, "端口+名称"
    lab = netutil.norm_name(row.get("site_name") or row.get("label") or "")
    if lab:
        for it in items:
            if lab == netutil.norm_name(it.get("name")) or lab == netutil.norm_name(it.get("container_name") or ""):
                return it, "名称"
    return None, ""


@app.get("/api/admin/gately/state")
def admin_gately_state(_=Depends(require_admin)):
    """设置页用：是否配置好、能不能通、版本、现有规则条数。"""
    s = db.get_settings()
    url, tok = _gately_cfg(s)
    port, tls = _gately_listen(s)
    out = {"configured": bool(url and tok), "url": url, "domain": (s.get("gately_domain") or "").strip(),
           "listener_port": port, "tls": tls, "auto": bool(s.get("gately_auto")),
           "ok": False, "version": "", "error": "", "rules": 0}
    if out["configured"]:
        try:
            c = gately.Client(url, tok)
            out["version"] = str((c.info() or {}).get("version") or "")
            out["rules"] = len(gately.rows_from_listeners(c.listeners()))
            out["ok"] = True
        except gately.GatelyError as e:
            out["error"] = str(e)
    return out


@app.post("/api/admin/gately/preview")
def admin_gately_preview(_=Depends(require_admin)):
    """把 Gately 现有规则拉下来 → 逐条匹配卡片（不落库，给前端预览）。"""
    s = db.get_settings()
    c = _gately_client(s)
    try:
        rows = gately.rows_from_listeners(c.listeners())
    except gately.GatelyError as e:
        raise HTTPException(400, "拉取失败：%s" % e)
    items = db.list_items(include_hidden=True)
    out = []
    for r in rows:
        it, how = _match_gately(r, items)
        out.append({
            **r,
            "domain": _wan_host(r["domain"], r["scheme"], r.get("listener_port")),
            "url": "%s://%s" % (r["scheme"], _wan_host(r["domain"], r["scheme"], r.get("listener_port"))),
            "item_id": it["id"] if it else None,
            "item_name": (it or {}).get("name") or "",
            "current_wan": (it or {}).get("url_wan") or "",
            "match_by": how,
        })
    return {
        "ok": True,
        "rows": out,
        "items": [{"id": i["id"], "name": i["name"], "port": int(i.get("container_port") or 0),
                   "url_wan": i.get("url_wan") or ""} for i in items],
    }


def _wan_host(domain, scheme, port):
    """外网地址里的 host 部分：监听口不是 80/443 时必须带端口，否则写出来的地址打不开。"""
    if not port:
        return domain
    if (scheme == "https" and int(port) == 443) or (scheme == "http" and int(port) == 80):
        return domain
    return "%s:%d" % (domain, int(port))


# 子域名智能缩写：默认只取 2-4 个字母（Lucky 时代项目名太长，外网地址不好记）
_ABBR_ALIAS = {
    "emby": "emby", "jellyfin": "jf", "jelly": "jf", "plex": "plex",
    "qbittorrent": "qb", "qbit": "qb", "transmission": "tr", "deluge": "dg",
    "moviepilot": "mp", "moviepilotv2": "mp", "moviepilotv3": "mp",
    "navpage": "np", "gately": "gt", "lucky": "lucky",
    "familytree": "ft", "liveposter": "lp",
    "xhsautodeliver": "xhs", "wechatautopost": "wx",
    "autospeed": "spd", "speedtest": "spd", "sunpanel": "sun",
    "lunatvdl": "ltv", "lunatv": "ltv", "qinglong": "ql",
    "wbapihub": "wb", "alist": "al", "openlist": "ol",
    "immich": "im", "photoprism": "pp", "navidrome": "nd",
    "audiobookshelf": "ab", "homeassistant": "ha", "vaultwarden": "vw",
    "portainer": "pt", "code-server": "cs", "uptimekuma": "uk",
    "syncthing": "st", "filebrowser": "fb", "cloudreve": "cv",
}
_ABBR_NOISE = {"auto", "web", "app", "apps", "server", "srv", "panel", "service",
               "docker", "ui", "pro", "hub", "box", "dl", "the", "my", "new", "test"}


def _slug_wan_host(url_wan):
    """从卡片外网地址里抠出子域名（用于撞名判断）。"""
    u = (url_wan or "").strip()
    if not u:
        return ""
    u = re.sub(r"^\w+://", "", u).split("/")[0].split(":")[0]
    return u.split(".")[0].lower()


def _abbr(text):
    """把容器/项目名压成 2-4 个字母：family-tree→ft，qbittorrent→qb，moviepilotv2→mp。"""
    # 注意：不能用 netutil.norm_name —— 它会去掉连字符，family-tree 就拆不开段了
    t = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    if not t:
        return ""
    key = t.replace("-", "")
    for cand in (t, re.sub(r"-?v?\d+$", "", t), key, re.sub(r"v?\d+$", "", key)):
        if cand and cand in _ABBR_ALIAS:
            return _ABBR_ALIAS[cand]
    toks = [x for x in t.split("-") if x and x not in _ABBR_NOISE and not re.fullmatch(r"v?\d+", x)]
    if not toks:
        toks = [x for x in t.split("-") if x]
    if not toks:
        return ""
    if len(toks) >= 2:
        # 优先用里面有意义的别名段：amilys-emby → emby、lunatv-core → ltv
        for x in reversed(toks):
            if x in _ABBR_ALIAS:
                return _ABBR_ALIAS[x]
        out = "".join(x[0] for x in toks)
    else:
        w = toks[0]
        if len(w) <= 4:
            out = w
        else:
            # 长单词用辅音骨架，避免 peerbanhelper→pee 这类尴尬缩写
            out = w[0] + re.sub(r"[aeiou]", "", w[1:])
            out = out[:3] if len(out) >= 2 else w[:3]
    out = re.sub(r"[^a-z0-9]", "", out)[:4]
    return out or toks[0][:3]


def _slug(text):
    t = netutil.norm_name(text or "")
    t = re.sub(r"[^a-z0-9-]+", "-", t).strip("-")
    return t[:40]


def _provision(it, s=None, subdomain=""):
    """给一张卡片在 Gately 上建/更新反代规则，并把外网地址写回卡片。"""
    s = s or db.get_settings()
    base = (s.get("gately_domain") or "").strip().strip(".")
    if not base:
        raise HTTPException(400, "先在设置 → 反代面板里填「根域名」")
    if subdomain and subdomain.strip():
        sub = _slug(subdomain)
    else:
        base_name = it.get("container_name") or it.get("name") or ""
        sub = _abbr(base_name) or _slug(base_name)
    if not sub:
        raise HTTPException(400, "生成不出子域名，请手动填写")
    # 撞名保护：同一个根域名下已有卡片占用，就依次加 2、3…
    taken = {(_slug_wan_host(x.get("url_wan"))) for x in db.list_items(include_hidden=True) if int(x["id"]) != int(it["id"])}
    if sub in taken:
        for n in range(2, 10):
            if "%s%d" % (sub, n) not in taken:
                sub = "%s%d" % (sub, n)
                break
    port, tls = _gately_listen(s)
    domain = "%s.%s" % (sub, base)
    target = _item_target(it, s)
    c = _gately_client(s)
    try:
        res = gately.ensure_site(c, port, tls, domain, target, name=it.get("name") or sub)
    except gately.GatelyError as e:
        raise HTTPException(400, "Gately 写入失败：%s" % e)
    url = "%s://%s" % ("https" if tls else "http", _wan_host(domain, "https" if tls else "http", port))
    db.update_item(int(it["id"]), {"url_wan": url})
    return {"ok": True, "action": res["action"], "domain": domain, "url": url, "target": target}


@app.post("/api/admin/gately/provision")
def admin_gately_provision(body: GatelyProvisionBody, _=Depends(require_admin)):
    it = db.get_item(int(body.item_id))
    if not it:
        raise HTTPException(404, "卡片不存在")
    return _provision(it, subdomain=body.subdomain)



# ---------------------------------------------------------------- 2.0.0 内置网关
# 反代 / 证书 / DDNS / FRP 都在本进程里，不再依赖 Lucky 或 Gately。
# 卡片即规则：routes 每次请求都实时从 items 推，所以改卡片不需要 reload 反代。

def _gw_cfg():
    return db.get_settings()


def _gw_items():
    return db.list_items(include_hidden=False)


def _gw_items_all():
    """含隐藏卡片的全集：FRP 隧道的生成用它，首页渲染仍用 _gw_items。"""
    return db.list_items(include_hidden=True)


@app.on_event("startup")
async def _gw_startup():
    """网关晚于数据库初始化启动；起不来也不能拖垮整个面板。"""
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    try:
        svc = gw_mod.configure(db.DATA_DIR, _gw_cfg, _gw_items, items_all_fn=_gw_items_all)
        await svc.start()
    except Exception as e:
        print(f"[navpage] 网关启动失败（面板其他功能不受影响）：{e}")


_AUTO_SCAN = {"ts": 0.0, "added": 0, "updated": 0, "error": ""}


def _bg_host_ctx(s):
    """后台任务没有请求头，用设置里的内网地址兜底；只影响 favicon 抓取。"""
    lan = (s.get("host_lan_ip") or "").strip()
    return {"host": lan, "host_is_ip": True, "host_private": True, "lan_base": lan,
            "wan_base": (s.get("host_wan") or "").strip(),
            "wan_scheme": s.get("wan_scheme") or "https"}


def _auto_scan_info():
    s = db.get_settings()
    mins = int(s.get("auto_scan_min") or 0)
    return {"min": mins, "last": _AUTO_SCAN["ts"], "added": _AUTO_SCAN["added"],
            "updated": _AUTO_SCAN["updated"], "error": _AUTO_SCAN["error"]}


async def _auto_scan_loop():
    """「自动扫描」：按设置间隔跑一次容器同步，新容器自动建卡并补地址。

    2026-10-26 用户问「新容器会不会自己冒出来」——以前只有手动点扫描才会，
    这里补上后台循环。默认关闭（auto_scan_min=0），设置里填分钟数即启用。
    """
    await asyncio.sleep(30)          # 让网关和数据库先就绪
    loop = asyncio.get_running_loop()
    while True:
        try:
            s = db.get_settings()
            mins = int(s.get("auto_scan_min") or 0)
            if mins > 0 and (time.time() - _AUTO_SCAN["ts"]) >= mins * 60:
                _AUTO_SCAN["ts"] = time.time()
                _AUTO_SCAN["error"] = ""
                r = await loop.run_in_executor(None, _sync_containers, _bg_host_ctx(s))
                _AUTO_SCAN["added"], _AUTO_SCAN["updated"] = r["added"], r["updated"]
                # 补地址 ≠ 签证书：只有真的新增了卡片才走 ACME（否则僵尸卡会让每一轮
                # 都去重试签发，日志全是失败）。补地址本身很便宜，缺就补。
                if (r["added"] or _need_addr_sync(s)) and _gw_should_sync(s):
                    await _gw_sync_all(issue=bool(r["added"] and s.get("gw_auto_cert", True)))
                if r["added"]:
                    print(f"[navpage] 自动扫描：新增 {r['added']} 张卡片")
        except Exception as e:
            _AUTO_SCAN["error"] = str(e)
            print(f"[navpage] 自动扫描失败（不影响面板）：{e}")
        # 每天一次全量快照：靠已有快照的 mtime 判断，重启也不会重复拍
        try:
            s = db.get_settings()
            if s.get("backup_auto", True):
                autos = [b for b in backup_mod.list_backups() if b["kind"] == "auto"]
                if not autos or (time.time() - max(b["mtime"] for b in autos)) >= 86400:
                    info = await loop.run_in_executor(None, backup_mod.snapshot, "auto")
                    if info.get("skipped"):
                        print(f"[navpage] 自动备份跳过：{info['reason']}")
                    else:
                        print(f"[navpage] 自动备份完成：{info['name']}（{info['size'] // 1024} KB）")
                    backup_mod.prune(int(s.get("backup_keep") or backup_mod.MAX_KEEP))
        except Exception as e:
            print(f"[navpage] 自动备份失败（不影响面板）：{e}")
        await asyncio.sleep(60)


@app.on_event("startup")
async def _auto_scan_startup():
    try:
        asyncio.create_task(_auto_scan_loop())
    except Exception as e:
        print(f"[navpage] 自动扫描任务没起来：{e}")


@app.on_event("shutdown")
async def _gw_shutdown():
    svc = gw_mod.instance()
    if svc:
        try:
            await svc.stop()
        except Exception:
            pass


def _run_bg(coro):
    """在后台跑协程。

    FastAPI 的同步端点跑在线程池里，拿不到主事件循环，直接 asyncio.create_task
    会抛 RuntimeError。这里用启动时记下的 loop 提交，扫完容器自动反代这类
    「不该让用户等」的活儿都走这条路。
    """
    try:
        if _MAIN_LOOP:
            asyncio.run_coroutine_threadsafe(coro, _MAIN_LOOP)
            return True
    except Exception:
        pass
    return False


def _gw_or_404():
    svc = gw_mod.instance()
    if not svc:
        raise HTTPException(503, "网关未启动")
    return svc


@app.get("/api/admin/gw/status")
def gw_status(_=Depends(require_admin)):
    return {"ok": True, **gw_status_only()}


def gw_status_only():
    svc = _gw_or_404()
    st = svc.status()
    st["host_ip"] = db.detect_host_ip()
    st["frp_binary"] = svc.frp.status().get("binary") or ""
    return st


class GwSettingsBody(BaseModel):
    patch: dict = {}


@app.put("/api/admin/gw/settings")
def gw_save_settings(body: GwSettingsBody, _=Depends(require_admin)):
    """保存网关相关设置并立刻生效（端口变化会重启监听）。"""
    allowed = {k for k in db.DEFAULT_SETTINGS if k.startswith(("gw_", "waf_", "dns_", "ddns_", "frp_"))}
    allowed.add("auto_scan_min")      # 「扫描行为」里的自动扫描间隔也在这一页保存
    patch = {k: v for k, v in (body.patch or {}).items() if k in allowed}
    if not patch:
        return _err("没有可保存的网关设置")
    db.update_settings(patch)
    _run_bg(_gw_or_404().reload_all())
    return {"ok": True, "saved": list(patch.keys())}


@app.post("/api/admin/gw/reload")
def gw_reload(_=Depends(require_admin)):
    _run_bg(_gw_or_404().reload_all())
    return {"ok": True}


@app.get("/api/admin/gw/routes")
def gw_routes(_=Depends(require_admin)):
    svc = _gw_or_404()
    return {"ok": True, "routes": svc.routes(), "frp": svc.frp_proxies(),
            "target_host": svc.target_host()}


@app.get("/api/admin/gw/waf")
def gw_waf(_=Depends(require_admin)):
    """WAF 状态 + 最近的拦截/观察记录。"""
    svc = _gw_or_404()
    return {"ok": True, **svc.waf.status()}


@app.post("/api/admin/gw/waf/clear")
def gw_waf_clear(_=Depends(require_admin)):
    svc = _gw_or_404()
    return {"ok": True, "cleared": svc.waf.clear()}


@app.post("/api/admin/gw/waf/test")
async def gw_waf_test(request: Request, _=Depends(require_admin)):
    """拿一条构造出来的假请求跑一遍规则，让人先看清「会不会误伤」再开。

    body: {host, path, ua, ip, method}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    svc = _gw_or_404()

    class _Fake:
        def __init__(self, b):
            self.path = b.get("path") or "/"
            self.method = (b.get("method") or "GET").upper()
            self.headers = {"User-Agent": b.get("ua") or "Mozilla/5.0 (NavPage WAF test)"}
            self.remote = b.get("ip") or "203.0.113.7"

    hit = svc.waf.check(_Fake(body or {}), body.get("host") or "test.local", record=False)
    return {"ok": True, "hit": hit, "verdict": (hit or {}).get("action") or "allow"}



async def _gw_sync_all(issue: bool = True) -> dict:
    """把「反代域名 / 外网地址 / FRP 地址」写回所有卡片。

    签名证书很慢（DNS-01 要等 TXT 生效，单个域名 10~40s），所以这里只对
    **还没有可用证书**的域名签，其余复用通配符；扫描导入后是在后台跑的。
    """
    svc = gw_mod.instance()
    if not svc:
        return {"ok": False, "error": "网关未启动", "updated": 0}
    items = db.list_items(include_hidden=True)
    n, certs = 0, []
    # 用 sync_item 逐个算 patch：它只补空字段，不会覆盖手填的外网地址（见 service.py 注释）
    for it in items:
        r = await svc.sync_item(it, issue=issue)
        if r.get("patch"):
            db.update_item(it["id"], r["patch"])
            n += 1
        if r.get("cert"):
            certs.append(r["cert"])
    svc.frp.apply(svc.cfg(), svc.frp_proxies())
    return {"ok": True, "updated": n, "total": len(items), "certs": certs}


@app.post("/api/admin/gw/sync")
def gw_sync(body: dict | None = None, _=Depends(require_admin)):
    """给所有卡片补齐 反代域名 / 外网地址 / FRP 地址（可选顺带签证书）。"""
    _gw_or_404()
    issue = True if not body else bool(body.get("issue", True))
    fut = asyncio.run_coroutine_threadsafe(_gw_sync_all(issue=issue), _MAIN_LOOP)
    try:
        return fut.result(timeout=600)
    except Exception as e:
        raise HTTPException(400, f"同步失败：{e}")


def _norm_domain(raw: str) -> str:
    """把用户填的东西洗成裸域名：去掉协议、路径、端口、首尾点。"""
    d = (raw or "").strip().lower()
    d = re.sub(r"^[a-z][a-z0-9+.-]*://", "", d)
    d = d.split("/")[0].split("?")[0].split(":")[0].strip().strip(".")
    if not d:
        return ""
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+", d):
        raise HTTPException(400, "域名格式不对（示例：emby.example.com）")
    return d


@app.post("/api/admin/gw/item/{iid}/proxy")
def gw_item_proxy(iid: int, body: dict | None = None, _=Depends(require_admin)):
    """给单张卡片配反代：可手填域名，写回卡片后立刻重载路由 / 隧道 / 证书。

    body: {domain?: "emby.example.com"（留空=按规则自动推导）, issue?: bool}
    """
    it = db.get_item(iid)
    if not it:
        raise HTTPException(404, "卡片不存在")
    svc = _gw_or_404()
    body = body or {}

    # 1) 手填域名优先（卡片上的 proxy_domain 就是规则本身，domain_for() 会先认它）
    if "domain" in body:
        custom = _norm_domain(str(body.get("domain") or ""))
        db.update_item(iid, {"proxy_domain": custom})
        it = db.get_item(iid) or it

    addr = svc.item_addresses(it)
    if not addr["proxy_domain"]:
        return _err("先在网关设置里填「根域名」，或者在这张卡片上手动填反代域名")
    host, port = svc.item_target(it)
    if not (host and port):
        return _err("这张卡片没有内网地址（或端口），反代不知道转发到哪")

    patch = {"proxy_domain": addr["proxy_domain"]}
    if addr["url_wan"]:
        patch["url_wan"] = addr["url_wan"]
    if addr["url_frp"]:
        patch["url_frp"] = addr["url_frp"]
    db.update_item(iid, patch)

    # 2) 签发/续期证书（没配 DNS 凭据时不要整单失败，把错误带回去给前端显示）
    cert, cert_err = None, ""
    if bool(body.get("issue", True)):
        try:
            cert = asyncio.run_coroutine_threadsafe(
                svc.ensure_cert_for(addr["proxy_domain"]), _MAIN_LOOP).result(timeout=300)
        except Exception as e:
            cert_err = str(e)[:300]

    # 3) 路由 / frpc 立刻按新域名重来（不重载的话新域名要等重启才生效）
    try:
        asyncio.run_coroutine_threadsafe(svc.reload_all(), _MAIN_LOOP).result(timeout=90)
    except Exception as e:
        cert_err = cert_err or ("重载网关失败：" + str(e)[:200])

    return {"ok": True, "id": iid, "domain": addr["proxy_domain"], "url_wan": addr["url_wan"],
            "url_frp": addr["url_frp"], "target": f"{host}:{port}", "cert": cert,
            "cert_error": cert_err, "gw_enabled": bool(svc.cfg().get("gw_enabled"))}


class GwCertBody(BaseModel):
    domains: list[str] | None = None
    name: str = ""
    force: bool = False


@app.post("/api/admin/gw/cert/issue")
def gw_cert_issue(body: GwCertBody, _=Depends(require_admin)):
    svc = _gw_or_404()
    s = db.get_settings()
    domains = body.domains or []
    if not domains:
        root = (s.get("gw_root_domain") or "").strip().strip(".")
        if not root:
            return _err("没有域名可签：先在网关设置里填根域名")
        domains = [f"*.{root}", root] if s.get("gw_wildcard", True) else [root]
    fut = asyncio.run_coroutine_threadsafe(
        svc.issue_cert(domains, name=body.name, force=bool(body.force)), _MAIN_LOOP)
    try:
        res = fut.result(timeout=300)
    except Exception as e:
        return _err(f"签发失败：{e}", 400)
    # 把结果平铺返回：前端要的是 {ok, domains, days_left, error}
    out = dict(res or {})
    out.setdefault("ok", True)
    return out


@app.delete("/api/admin/gw/cert/{name}")
def gw_cert_delete(name: str, _=Depends(require_admin)):
    svc = _gw_or_404()
    ok = svc.certs.remove(name)
    return {"ok": ok}


@app.get("/api/admin/gw/cert/{name}/pem")
def gw_cert_pem(name: str, _=Depends(require_admin)):
    svc = _gw_or_404()
    cp, _kp = svc.certs.cert_paths(name)
    if not os.path.exists(cp):
        raise HTTPException(404, "证书不存在")
    return FileResponse(cp, media_type="application/x-pem-file", filename=f"{name}.pem")


@app.post("/api/admin/gw/ddns/tick")
def gw_ddns_tick(_=Depends(require_admin)):
    svc = _gw_or_404()
    fut = asyncio.run_coroutine_threadsafe(svc.ddns.tick(force=True), _MAIN_LOOP)
    try:
        return {"ok": True, "state": fut.result(timeout=120)}
    except Exception as e:
        return _err(f"DDNS 更新失败：{e}", 400)


@app.get("/api/admin/gw/nics")
def gw_nics(_=Depends(require_admin)):
    """本机网卡清单，DDNS「指定网卡」下拉用。"""
    info = netutil.container_net()
    return {"ok": True, "nics": netutil.list_ifaces(), **info}


@app.get("/api/admin/gw/ip")
def gw_public_ip(mode: str = "", nic: str = "", static: str = "", _=Depends(require_admin)):
    """试取 IP。带 mode/nic/static 时按前端当前选择试，不带就用已保存的设置。"""
    cfg = dict(db.get_settings())
    if mode:
        cfg["ddns_ip_mode"] = mode
        cfg["ddns_nic"] = nic or cfg.get("ddns_nic") or ""
        cfg["ddns_ip_static"] = static or cfg.get("ddns_ip_static") or ""
    ip, src = gw_mod.ddns.detect_ip(cfg)
    return {"ok": bool(ip), "ip": ip, "source": src}


class GwDnsTestBody(BaseModel):
    provider: str = ""
    creds: dict | None = None


@app.post("/api/admin/gw/dns/test")
def gw_dns_test(body: GwDnsTestBody, _=Depends(require_admin)):
    s = db.get_settings()
    provider = body.provider or s.get("dns_provider") or ""
    creds = body.creds if body.creds is not None else (s.get("dns_creds") or {})
    try:
        prov = gw_mod.dnsprov.get_provider(provider, creds)
        msg = prov.test()
        return {"ok": True, "message": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.post("/api/admin/gw/frp/apply")
def gw_frp_apply(_=Depends(require_admin)):
    svc = _gw_or_404()
    st = svc.frp.apply(svc.cfg(), svc.frp_proxies())
    return {"ok": True, "frp": st}


# ---------------------------------------------------------------- 远程 FRP 节点（2.1.0）
# 把 frps（+ 可选 Caddy）部署到任意裸 VPS：SSH + Docker，不依赖 1Panel/宝塔。
from .gw import node as node_mod  # noqa: E402


class NodeBody(BaseModel):
    node: dict = {}


def _node_norm(raw: dict) -> dict:
    """补齐默认值 + 生成 token，避免前端漏字段。"""
    n = dict(raw or {})
    n["host"] = (n.get("host") or "").strip()
    n["ssh_user"] = (n.get("ssh_user") or "root").strip()
    n["ssh_port"] = int(n.get("ssh_port") or 22)
    n["mode"] = (n.get("mode") or "caddy").lower()
    if n["mode"] not in ("http", "caddy"):
        n["mode"] = "caddy"
    n["root_domain"] = (n.get("root_domain") or "").strip().strip(".").lower()
    n["frps_bind_port"] = int(n.get("frps_bind_port") or 7000)
    n["vhost_port"] = int(n.get("vhost_port") or 0)
    n["https_port"] = int(n.get("https_port") or 0)
    n["auth"] = "key" if (n.get("key") or "").strip() else "password"
    if not (n.get("token") or "").strip() or n.get("token") == "***":
        old = node_mod.get_node(n.get("id") or "")
        n["token"] = (old or {}).get("token") or uuid.uuid4().hex[:24]
    return n


def _node_cfg(raw: dict) -> dict:
    """带密钥的完整节点：前端传 *** 时用库里存的真值。"""
    n = _node_norm(raw)
    old = node_mod.get_node(n.get("id") or "")
    if old:
        for k in ("password", "key", "token"):
            if n.get(k) in (None, "", "***"):
                n[k] = old.get(k, "")
    return n


@app.get("/api/admin/gw/nodes")
def gw_nodes(_=Depends(require_admin)):
    svc = _gw_or_404()
    cfg = svc.cfg()
    nodes = [node_mod.mask_node(n) for n in node_mod.load_nodes()]
    for n in nodes:
        n["ports"] = node_mod.node_ports(n)
    return {"ok": True, "nodes": nodes,
            "dns_provider": cfg.get("dns_provider") or "",
            "acme_email": cfg.get("gw_acme_email") or "",
            "acme_env": cfg.get("gw_acme_env") or "letsencrypt",
            "frp": {"server": cfg.get("frp_server") or "", "port": cfg.get("frp_port"),
                    "protocol": cfg.get("frp_protocol") or "", "domain": cfg.get("frp_domain") or ""}}


@app.post("/api/admin/gw/nodes/test")
def gw_node_test(body: NodeBody, _=Depends(require_admin)):
    try:
        node = _node_cfg(body.node)
        info = node_mod.preflight(node)
        return {"ok": True, **info}
    except node_mod.NodeError as e:
        return {"ok": False, "error": str(e)[:400], "checks": []}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:400], "checks": []}


@app.post("/api/admin/gw/nodes/deploy")
def gw_node_deploy(body: NodeBody, _=Depends(require_admin)):
    """体检 → 上传 → compose up → 回环验证 → 补 DNS → 签证书推证书。"""
    svc = _gw_or_404()
    steps: list[dict] = []
    try:
        node = _node_norm(_node_cfg(body.node))
        if not node["root_domain"]:
            return {"ok": False, "error": "必须填 FRP 域名后缀，例如 frp.example.com", "steps": steps}
        node = node_mod.upsert_node(node)

        info = node_mod.preflight(node)
        steps += [{"step": "体检 · " + c["name"], "ok": c["ok"], "msg": c["msg"]} for c in info["checks"]]
        if not info["ok"]:
            bad = [c for c in info["checks"] if not c["ok"] and c["name"] in ("Docker", "docker compose")]
            if bad:
                return {"ok": False, "error": "目标机不满足前置条件：" + bad[0]["msg"], "steps": steps,
                        "checks": info["checks"]}

        res = node_mod.deploy(node)
        steps += res["steps"]
        host_ip = res.get("host_ip") or info.get("host_ip") or ""

        dns = node_mod.ensure_wildcard_dns(node, host_ip)
        steps.append({"step": "DNS 通配记录", "ok": bool(dns.get("ok")),
                      "msg": dns.get("msg") or "、".join(f"{k} → {v}" for k, v in (dns.get("records") or {}).items())})

        cert = {"ok": False, "steps": []}
        try:
            cert = node_mod.sync_cert(node, _gw_or_404())
            steps += cert.get("steps") or []
        except Exception as e:
            steps.append({"step": "证书", "ok": False, "msg": f"{type(e).__name__}: {e}"[:300]})

        ok = all(s["ok"] for s in steps if s["step"] in
                 ("拉起容器", "frps vhost 应答")) and bool(res.get("ok"))
        node = node_mod.get_node(node["id"]) or node
        node.update(res.get("node_patch") or {})
        node["last"] = {"at": int(time.time()), "ok": ok, "host_ip": host_ip,
                        "cert_ok": bool(cert.get("ok")), "mode": res["ports"]["mode"]}
        node_mod.upsert_node(node)

        return {"ok": ok, "steps": steps, "host_ip": host_ip,
                "suggest": res["suggest"], "node": node_mod.mask_node(node),
                "cert_domains": cert.get("domains") or []}
    except node_mod.NodeError as e:
        return {"ok": False, "error": str(e)[:400], "steps": steps}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:400], "steps": steps}


@app.post("/api/admin/gw/nodes/{nid}/cert")
def gw_node_cert(nid: str, _=Depends(require_admin)):
    try:
        node = node_mod.get_node(nid)
        if not node:
            return {"ok": False, "error": "节点不存在"}
        res = node_mod.sync_cert(node, _gw_or_404(), force=True)
        return {"ok": bool(res.get("ok")), "steps": res.get("steps") or [],
                "error": "" if res.get("ok") else "签发或推送失败，看步骤日志"}
    except node_mod.NodeError as e:
        return {"ok": False, "error": str(e)[:400], "steps": []}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:400], "steps": []}


@app.post("/api/admin/gw/nodes/{nid}/dns")
def gw_node_dns(nid: str, _=Depends(require_admin)):
    node = node_mod.get_node(nid)
    if not node:
        return {"ok": False, "error": "节点不存在"}
    ip = ((node.get("last") or {}).get("host_ip") or "").strip()
    res = node_mod.ensure_wildcard_dns(node, ip)
    return {"ok": bool(res.get("ok")), **res}


@app.post("/api/admin/gw/nodes/{nid}/use")
def gw_node_use(nid: str, _=Depends(require_admin)):
    """把某个节点的参数写进 FRP 客户端设置，并立刻应用。"""
    svc = _gw_or_404()
    node = node_mod.get_node(nid)
    if not node:
        return {"ok": False, "error": "节点不存在"}
    sug = node_mod.suggest_client(node, ((node.get("last") or {}).get("host_ip") or ""))
    db.update_settings(sug)
    _run_bg(svc.reload_all())
    return {"ok": True, "saved": sug}


@app.delete("/api/admin/gw/nodes/{nid}")
def gw_node_del(nid: str, _=Depends(require_admin)):
    return {"ok": node_mod.delete_node(nid)}


# ---------------------------------------------------------------- static (must be last)

if os.path.isdir(STATIC_DIR):
    def _asset_ver(rel):
        """静态资源版本号 = 文件内容哈希，改了就变，不用再手改 index.html 里的 ?v=N。"""
        try:
            with open(os.path.join(STATIC_DIR, rel), "rb") as f:
                return hashlib.md5(f.read()).hexdigest()[:8]
        except OSError:
            return ""

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def _index_html():
        try:
            html = open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8").read()
        except OSError:
            raise HTTPException(404, "index.html missing")
        for rel in ("js/app.js", "css/app.css"):
            v = _asset_ver(rel)
            if v:
                html = re.sub(r"(/%s\?v=)[0-9a-f]+" % re.escape(rel), r"\g<1>" + v, html)
        return HTMLResponse(html)

    # 2.0.0 起设置不再独立成页（是主页面里居中的整块页面），旧链接跳过去自动打开
    @app.get("/settings", include_in_schema=False)
    def _settings_redirect(q: str = ""):
        return RedirectResponse("/?settings=1" + (("&tab=" + q) if q else ""), status_code=307)

    # index.html 自身没有版本号：被浏览器缓存会导致「服务端已更新、用户看到的还是旧界面」
    # （2026-10-16 实际踩到）。html 一律 no-store，app.js/app.css 靠 ?v=N 区分，不禁。
    @app.middleware("http")
    async def _cache_headers(request, call_next):
        resp = await call_next(request)
        p = request.url.path
        if p.endswith(".html") or p in ("", "/", "/settings"):
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
        elif p.startswith(("/vendor/", "/js/", "/css/", "/icons/")) or p.endswith(
                (".js", ".css", ".woff2", ".ico", ".webmanifest")):
            # 静态资源固定 7 天强缓存：之前一个 Cache-Control 都没有，每次打开都要拿 etag
            # 回源重验证（14 个请求全变 304，外网隧道下白等一圈）。
            # app.js / app.css 带 ?v=N，升级时 URL 变了，不会被这份 7 天缓存挡住。
            resp.headers["Cache-Control"] = "public, max-age=604800"
        return resp

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
