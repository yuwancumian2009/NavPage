"""服务状态小组件：从各服务自己的 API 取运行时信息，显示在导航卡片上。

设计要点：
  * 每个 provider 是一个纯函数 (cfg) -> {"ok":bool, "text":str, "detail":[..], "error":str}
  * 结果按配置指纹缓存 TTL 秒，避免每张卡片刷新都去打上游 API
  * 认证失败要给出人能看懂的提示（「认证失败，请检查 API Key」），不要抛原始异常
  * LAN 里自签证书很常见，一律 verify=False

配置形状（存在 items.widget，JSON）：
    {"type": "qbittorrent", "url": "http://192.168.1.10:8889",
     "username": "admin", "password": "***"}
"""
import base64
import hashlib
import json
import threading
import time

import httpx

TIMEOUT = 7.0
CACHE_TTL = 20

_cache = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------- 小工具

def fmt_bytes(n):
    try:
        n = float(n or 0)
    except Exception:
        return "0 B"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_speed(n):
    return fmt_bytes(n) + "/s"


def fmt_dur(seconds):
    try:
        s = int(seconds or 0)
    except Exception:
        s = 0
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


def _base(cfg):
    return (cfg.get("url") or "").strip().rstrip("/")


def _client(**kw):
    kw.setdefault("timeout", TIMEOUT)
    kw.setdefault("verify", False)
    kw.setdefault("follow_redirects", True)
    return httpx.Client(**kw)


def _err(msg):
    return {"ok": False, "error": str(msg)[:160]}


def _auth_err():
    return {"ok": False, "error": "认证失败，请检查用户名/密码或 API Key"}


def _rich(text, stats, detail=None, progress=None):
    """homepage 风格输出：text 是单行兜底，stats 是多指标小方块，progress 是进度条。

    stats: [{"label": "订阅", "value": "43", "tone": "good|warn|bad|muted"}]
    progress: {"value": 0.62, "label": "平均进度 62%"}
    """
    out = {"ok": True, "text": text, "stats": stats}
    if detail:
        out["detail"] = detail
    if progress:
        out["progress"] = progress
    return out


def _pct(v, digits=0):
    try:
        return f"{float(v) * 100:.{digits}f}%"
    except Exception:
        return "0%"


# ---------------------------------------------------------------- 下载器

def qbittorrent(cfg):
    base = _base(cfg)
    if not base:
        return _err("未填地址")
    try:
        with _client() as c:
            r = c.post(base + "/api/v2/auth/login",
                       data={"username": cfg.get("username") or "",
                             "password": cfg.get("password") or ""})
            if r.status_code == 403:
                return _err("IP 被 qBittorrent 封禁，或未开「跳过本地认证」")
            if "Ok." not in (r.text or ""):
                return _err("用户名或密码不对（qBittorrent 默认用户名 admin）")
            t = c.get(base + "/api/v2/transfer/info").json()
            tl = c.get(base + "/api/v2/torrents/info").json()
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")

    dl = t.get("dl_info_speed", 0)
    up = t.get("up_info_speed", 0)
    DL_STATES = ("downloading", "stalledDL", "metaDL", "forcedDL", "allocating",
                 "checkingDL", "queuedDL")
    UP_STATES = ("uploading", "stalledUP", "forcedUP", "checkingUP", "queuedUP")
    states, prog, eta = {}, [], 0
    for x in tl or []:
        s = x.get("state") or ""
        states[s] = states.get(s, 0) + 1
        if s in DL_STATES:
            try:
                prog.append(float(x.get("progress") or 0))
            except Exception:
                pass
            e = x.get("eta") or 0
            if isinstance(e, (int, float)) and e > 0 and (eta == 0 or e < eta):
                eta = int(e)
    downloading = sum(v for k, v in states.items() if k in DL_STATES)
    seeding = sum(v for k, v in states.items() if k in UP_STATES)
    # 完成 = 下载已跑完（progress>=99.9%，qB 没有独立的 completed 状态，跑完的会留在做种）
    completed = 0
    for x in tl or []:
        try:
            if float(x.get("progress") or 0) >= 0.999:
                completed += 1
        except Exception:
            pass
    avg = (sum(prog) / len(prog)) if prog else 0.0
    # 用户要求四项：下载 / 上传 / 完成 / 任务（速度放在摘要行，不重复）
    stats = [
        {"label": "下载", "value": str(downloading), "tone": "warn" if downloading else "muted"},
        {"label": "上传", "value": str(seeding)},
        {"label": "完成", "value": str(completed)},
        {"label": "任务", "value": str(len(tl or []))},
    ]
    res = _rich(f"↓{fmt_speed(dl)} ↑{fmt_speed(up)}",
                stats,
                [["下载速度", fmt_speed(dl)], ["上传速度", fmt_speed(up)],
                 ["任务总数", str(len(tl or []))],
                 ["下载中", str(downloading)], ["做种中", str(seeding)],
                 ["已完成", str(completed)],
                 ["累计上传", fmt_bytes(t.get("up_info_data", 0))]])
    if downloading:
        lbl = "平均进度 " + _pct(avg, 1)
        if eta:
            lbl += " · 最快 " + fmt_dur(eta) + " 后完成"
        res["progress"] = {"value": avg, "label": lbl}
    return res


def transmission(cfg):
    base = _base(cfg)
    if not base:
        return _err("未填地址")
    rpc = base + "/transmission/rpc"
    auth = None
    if cfg.get("username"):
        auth = (cfg["username"], cfg.get("password") or "")
    def _rpc(c, payload, sid=None):
        h = {"X-Transmission-Session-Id": sid} if sid else {}
        return c.post(rpc, json=payload, headers=h)

    a, tl = {}, []
    try:
        with _client(auth=auth) as c:
            r = _rpc(c, {"method": "session-stats"})
            sid = None
            if r.status_code == 409:
                sid = r.headers.get("x-transmission-session-id")
                r = _rpc(c, {"method": "session-stats"}, sid)
            if r.status_code in (401, 403):
                return _auth_err()
            r.raise_for_status()
            a = (r.json() or {}).get("arguments") or {}
            # 再取一次任务列表算平均进度（session-stats 不含逐任务进度）
            try:
                r2 = _rpc(c, {"method": "torrent-get",
                              "arguments": {"fields": ["status", "percentDone", "eta",
                                                       "rateDownload", "rateUpload", "error"]}}, sid)
                if r2.status_code == 200:
                    tl = ((r2.json() or {}).get("arguments") or {}).get("torrents") or []
            except Exception:
                tl = []
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")

    dl = a.get("downloadSpeed", 0)
    up = a.get("uploadSpeed", 0)
    total = int(a.get("torrentCount", 0) or 0)
    paused = int(a.get("pausedTorrentCount", 0) or 0)
    # ⚠ 「活动」不能直接用 session-stats 的 activeTorrentCount：
    #   TR 把「运行中」全部算作 active —— 生产实测 12263 个任务全是「做种中」，
    #   activeTorrentCount 也返回 12263，而真正在传数据的只有 1 个（用户界面上看到的也是 1）。
    #   TR WebUI 的「活动」= 正在传输，所以这里按「正在下载(4) + 正在上传(有上传速率)」自己算。
    active = 0
    errors = 0
    for x in tl:
        if (x.get("status") == 4) or ((x.get("rateUpload") or 0) > 0):
            active += 1
        if (x.get("error") or 0) > 0:
            errors += 1
    if not tl:
        # 取不到任务列表时退回 session-stats 的口径（宁可显示 TR 的原始值，也不要显示 0）
        active = int(a.get("activeTorrentCount", 0) or 0)
    # TR status: 4 = 下载中
    prog = [float(x.get("percentDone") or 0) for x in tl if x.get("status") == 4]
    downloading = len(prog)
    eta = min([int(x.get("eta") or 0) for x in tl
               if x.get("status") == 4 and (x.get("eta") or 0) > 0] or [0])
    avg = (sum(prog) / len(prog)) if prog else 0.0
    # 用户要求四项：任务 / 活动 / 暂停 / 错误
    res = _rich(f"↓{fmt_speed(dl)} ↑{fmt_speed(up)}",
                [{"label": "任务", "value": str(total)},
                 {"label": "活动", "value": str(active),
                  "tone": "good" if active else "muted"},
                 {"label": "暂停", "value": str(paused),
                  "tone": "muted" if paused else "default"},
                 {"label": "错误", "value": str(errors),
                  "tone": "bad" if errors else "default"}],
                [["下载速度", fmt_speed(dl)], ["上传速度", fmt_speed(up)],
                 ["任务总数", str(total)], ["活动中", str(active)],
                 ["正在下载", str(downloading)],
                 ["暂停", str(paused)], ["错误", str(errors)]])
    if downloading:
        lbl = "平均进度 " + _pct(avg, 1)
        if eta:
            lbl += " · 最快 " + fmt_dur(eta) + " 后完成"
        res["progress"] = {"value": avg, "label": lbl}
    return res


# ---------------------------------------------------------------- 媒体服务器

def _emby_like(cfg, prefix):
    base = _base(cfg)
    if not base:
        return _err("未填地址")
    key = (cfg.get("apikey") or "").strip()
    if not key:
        return _err("未填 API Key")
    h = {"X-Emby-Token": key, "Accept": "application/json"}
    try:
        with _client(headers=h) as c:
            rc = c.get(base + prefix + "/Items/Counts")
            if rc.status_code in (401, 403):
                return _auth_err()
            rc.raise_for_status()
            counts = rc.json() or {}
            try:
                sess = c.get(base + prefix + "/Sessions").json() or []
            except Exception:
                sess = []
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")

    playing = []
    for s in sess:
        if s.get("NowPlayingItem"):
            u = (s.get("UserName") or "").strip()
            it = (s.get("NowPlayingItem") or {}).get("Name") or ""
            playing.append(f"{u}: {it}" if u else it)
    mv = counts.get("MovieCount", 0)
    tv = counts.get("SeriesCount", 0)
    ep = counts.get("EpisodeCount", 0)
    txt = f"{len(playing)} 个会话正在播放" if playing else "当前无播放"
    stats = [{"label": "电影", "value": str(mv)}, {"label": "剧集", "value": str(tv)},
             {"label": "单集", "value": str(ep)}]
    if playing:
        stats.insert(0, {"label": "播放中", "value": str(len(playing)), "tone": "good"})
    return _rich(txt, stats,
                 [["正在播放", "、".join(playing) if playing else "无"],
                  ["电影", str(mv)], ["剧集", str(tv)], ["单集", str(ep)],
                  ["音乐", str(counts.get("SongCount", 0))]])


def emby(cfg):
    return _emby_like(cfg, "/emby")


def jellyfin(cfg):
    return _emby_like(cfg, "")


def plex(cfg):
    base = _base(cfg)
    if not base:
        return _err("未填地址")
    key = (cfg.get("apikey") or "").strip()
    if not key:
        return _err("未填 X-Plex-Token")
    h = {"X-Plex-Token": key, "Accept": "application/json"}
    try:
        with _client(headers=h) as c:
            r = c.get(base + "/library/sections")
            if r.status_code in (401, 403):
                return _auth_err()
            r.raise_for_status()
            secs = ((r.json() or {}).get("MediaContainer") or {}).get("Directory") or []
            try:
                st = c.get(base + "/status/sessions").json() or {}
                playing = ((st.get("MediaContainer") or {}).get("size")) or 0
            except Exception:
                playing = 0
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    return _rich(f"{playing} 个会话正在播放" if playing else "当前无播放",
                 [{"label": "媒体库", "value": str(len(secs))}],
                 [["正在播放", str(playing)], ["媒体库", str(len(secs))],
                  ["库名", "、".join([s.get("title", "") for s in secs[:6]])]])


def audiobookshelf(cfg):
    base = _base(cfg)
    if not base:
        return _err("未填地址")
    key = (cfg.get("apikey") or "").strip()
    h = {"Authorization": "Bearer " + key} if key else {}
    try:
        with _client(headers=h) as c:
            r = c.get(base + "/api/libraries")
            if r.status_code in (401, 403):
                return _auth_err()
            r.raise_for_status()
            libs = ((r.json() or {}).get("libraries")) or []
            # /api/libraries 本身不带 numItems，条目数只能逐库问 /items 的 total。
            # 顺手按 mediaType 归类（book / podcast），指标格要分开显示有声书和播客。
            total, counted, by_type = 0, 0, {}
            for lib in libs[:8]:
                if not lib.get("id"):
                    continue
                try:
                    j = c.get(base + "/api/libraries/%s/items" % lib["id"],
                              params={"limit": 1}).json() or {}
                    n = int(j.get("total") or 0)
                    total += n
                    counted += 1
                    mt = str(lib.get("mediaType") or "book").lower()
                    by_type[mt] = by_type.get(mt, 0) + n
                except Exception:
                    pass
            listen, finished, doing = 0.0, 0, 0
            try:
                me = c.get(base + "/api/me").json() or {}
                for x in me.get("mediaProgress") or []:
                    # currentTime = 实际听过的秒数；duration 是整本时长，加它会得出天文数字
                    cur = float(x.get("currentTime") or 0)
                    listen += cur
                    if x.get("isFinished"):
                        finished += 1
                    elif cur > 0:
                        doing += 1
            except Exception:
                pass
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")

    # 摘要行只放「状态」，且必须单行放得下：手机端卡内可用宽约 145px，
    # 折成两行会把整个网格行撑高 17.6px（实测卡高 207.1 vs 别的 189.5），同排另一张卡也被带高。
    # 所以摘要只留已听时长，「读完」挪到指标格（数字不与摘要重复）。
    txt = f"已听 {fmt_dur(listen)}" if listen else "有声书库正常"
    # 指标块固定 2×2 四格，和 qb/tr/host 那些卡片保持一致的版式
    stats = [
        {"label": "媒体库", "value": str(len(libs))},
        {"label": "有声书", "value": str(by_type.get("book", 0)) if counted else "—"},
        {"label": "进行中", "value": str(doing) if listen else "—"},
        {"label": "读完", "value": str(finished) if listen else "—"},
    ]
    d = [["媒体库", str(len(libs))]]
    if counted:
        suffix = "" if counted == len(libs) else f"（只统计了 {counted} 个库）"
        d.append(["条目总数", f"{total}{suffix}"])
        d.append(["有声书 / 播客", "%d / %d" % (by_type.get("book", 0), by_type.get("podcast", 0))])
    d.append(["库名", "、".join([l.get("name", "") for l in libs[:6]])])
    if listen:
        d.append(["已听时长", fmt_dur(listen)])
        d.append(["读完", f"{finished} 本"])
        d.append(["进行中", f"{doing} 本"])
    return _rich(txt, stats, d)


# Navidrome 自己 REST API 的登录 token（15 天有效）：进程内缓存，别每次刷卡片都重登。
# key = (base, 用户名) -> (token, 取到的时间)
_nd_tokens = {}


def _nd_token(c, base, user, pwd):
    hit = _nd_tokens.get((base, user))
    if hit and time.time() - hit[1] < 3600:
        return hit[0]
    r = c.post(base + "/auth/login", json={"username": user, "password": pwd})
    if r.status_code == 404:
        raise PermissionError("这个 Navidrome 没有 /auth/login（版本太老，需 0.49+）")
    if r.status_code in (401, 403):
        raise PermissionError("用户名或密码不对")
    r.raise_for_status()
    tok = str((r.json() or {}).get("token") or "")
    if tok:
        _nd_tokens[(base, user)] = (tok, time.time())
    return tok


def _nd_total(c, base, tok, path):
    """取 Navidrome 某个集合的精确总数。

    它自己的 Web UI 就是这么数的：GET /api/<集合>?_start=0&_end=1，
    总数在 X-Total-Count 响应头里，响应体只有分页后的那一条。
    （Subsonic API 拿不到精确总数：getAlbumList2 得一直翻页。）
    """
    r = c.get(base + path, params={"_start": 0, "_end": 1},
              headers={"X-ND-Authorization": "Bearer " + tok})
    if r.status_code in (401, 403):
        raise PermissionError("登录态失效")
    r.raise_for_status()
    n = str(r.headers.get("X-Total-Count") or "").strip()
    return int(n) if n.isdigit() else None


def navidrome(cfg):
    base = _base(cfg)
    if not base:
        return _err("未填地址")
    u, p = (cfg.get("username") or "").strip(), cfg.get("password") or ""
    if not (u and p):
        return _err("未填用户名 / 密码")
    try:
        with _client() as c:
            tok = _nd_token(c, base, u, p)
            if not tok:
                return _auth_err()
            albums = _nd_total(c, base, tok, "/api/album")
            artists = _nd_total(c, base, tok, "/api/artist")
            songs = _nd_total(c, base, tok, "/api/song")
            lists = _nd_total(c, base, tok, "/api/playlist")
    except PermissionError as e:
        _nd_tokens.pop((base, u), None)
        return _err(str(e))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")

    def _n(v):
        return "—" if v is None else str(v)

    # 2×2 四格：专辑 / 艺术家 / 歌曲 / 歌单
    stats = [{"label": "专辑", "value": _n(albums)},
             {"label": "艺术家", "value": _n(artists)},
             {"label": "歌曲", "value": _n(songs)},
             {"label": "歌单", "value": _n(lists)}]
    empty = (songs in (0, None)) and (albums in (0, None))
    d = [["专辑", _n(albums)], ["艺术家", _n(artists)],
         ["歌曲", _n(songs)], ["歌单", _n(lists)]]
    return _rich("音乐库为空" if empty else "音乐库正常", stats, d)


# ---------------------------------------------------------------- MoviePilot

def _mp_login_token(c, base, cfg):
    """用用户名+密码换 access_token。"""
    u = (cfg.get("username") or "").strip()
    p = cfg.get("password") or ""
    if not (u and p):
        return ""
    try:
        r = c.post(base + "/api/v1/login/access-token",
                   data={"username": u, "password": p})
        if r.status_code >= 400:
            return ""
        return str((r.json() or {}).get("access_token") or "").strip()
    except Exception:
        return ""


# MoviePilot 订阅状态位（实测线上只有 R / P，其余按官方枚举兜底）
MP_STATE = {"N": "待定", "R": "订阅中", "P": "暂停", "S": "已停止",
            "C": "已完成", "E": "已完结", "D": "已删除"}


def _mp_auth_variants(c, base, cfg):
    """MP 的 /api/v1 支持三种鉴权：?token=<API_TOKEN>、?apikey=<用户key>、Bearer JWT。

    token 是 app.env 里的 API_TOKEN（注意 app.env 里带引号，要去掉）。
    """
    tok = (cfg.get("apikey") or "").strip().strip('"').strip("'")
    out = []
    if tok:
        out.append({"params": {"token": tok}})
        out.append({"params": {"apikey": tok}})
        out.append({"headers": {"Authorization": "Bearer " + tok}})
    if (cfg.get("username") or "").strip() and cfg.get("password"):
        lt = _mp_login_token(c, base, cfg)
        if lt:
            out.append({"headers": {"Authorization": "Bearer " + lt}})
    return out


def moviepilot(cfg):
    """显示订阅概况：总数 / 追更中 / 待更新缺集 / 剧集完成度。"""
    base = _base(cfg)
    if not base:
        return _err("未填地址")
    has_tok = bool((cfg.get("apikey") or "").strip())
    has_pw = bool((cfg.get("username") or "").strip() and cfg.get("password"))
    if not has_tok and not has_pw:
        return _err("填 MoviePilot 的 API Token，或填面板登录用户名+密码")

    subs, last = None, ""
    try:
        with _client() as c:
            variants = _mp_auth_variants(c, base, cfg)
            if not variants:
                return _err("用户名或密码不对（或该账号没有 API 权限）")
            for kw in variants:
                r = c.get(base + "/api/v1/subscribe/", **kw)
                if r.status_code in (401, 403):
                    try:
                        last = (r.json() or {}).get("message") or ""
                    except Exception:
                        last = ""
                    continue
                r.raise_for_status()
                j = r.json()
                subs = j if isinstance(j, list) else ((j or {}).get("data") or [])
                break
            if subs is None:
                return _err("Token 校验不通过" + (f"（{last}）" if last else ""))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")

    if not isinstance(subs, list):
        return _err("订阅接口返回格式不认识，请把响应发我")

    total = len(subs)
    by_state, movies, tvs = {}, 0, 0
    lack_list, done_ep, total_ep = [], 0, 0
    for s in subs:
        st = str(s.get("state") or "?").upper()
        by_state[st] = by_state.get(st, 0) + 1
        ty = s.get("type") or ""
        if ty == "电影":
            movies += 1
        elif ty in ("电视剧", "动漫"):
            tvs += 1
        te = int(s.get("total_episode") or 0)
        le = int(s.get("lack_episode") or 0)
        if st == "R":
            if te > 0:
                total_ep += te
                done_ep += max(0, te - le)
            if le > 0:
                lack_list.append((le, s.get("name") or ""))

    running = by_state.get("R", 0)
    paused = by_state.get("P", 0)
    lack_n = len(lack_list)
    lack_list.sort(reverse=True)

    # 摘要行给"状态"，数量交给指标块
    if lack_n:
        text = f"{lack_n} 部有新集待更新"
    elif running:
        text = "订阅已全部追平"
    else:
        text = "暂无追更中的订阅"

    stats = [{"label": "订阅", "value": str(total)},
             {"label": "追更中", "value": str(running), "tone": "good" if running else "muted"}]
    if lack_n:
        stats.append({"label": "待更新", "value": str(lack_n), "tone": "warn"})
    if paused:
        stats.append({"label": "暂停", "value": str(paused), "tone": "muted"})

    detail = [["订阅总数", str(total)], ["订阅中", str(running)], ["暂停", str(paused)]]
    for st, n in sorted(by_state.items(), key=lambda x: -x[1]):
        if st not in ("R", "P"):
            detail.append([MP_STATE.get(st, st), str(n)])
    detail.append(["电影", str(movies)])
    detail.append(["电视剧/动漫", str(tvs)])
    if lack_list:
        detail.append(["缺集最多", "、".join(f"{n}(缺{l})" for l, n in lack_list[:3])])
    recent = sorted([s for s in subs if s.get("date")],
                    key=lambda x: str(x.get("date")), reverse=True)[:3]
    if recent:
        detail.append(["最近订阅", "、".join(str(s.get("name")) for s in recent)])

    res = _rich(text, stats, detail)
    if total_ep:
        res["progress"] = {"value": done_ep / total_ep,
                           "label": f"追更剧集 {done_ep}/{total_ep}"}
    return res


# ---------------------------------------------------------------- 本机 / NAS

_cpu_lock = threading.Lock()
_prev_cpu = None   # 上一次 /proc/stat 采样，用于算占用率


def _cpu_ticks():
    with open("/proc/stat") as f:
        parts = [int(x) for x in f.readline().split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)   # idle + iowait
    return idle, sum(parts)


def cpu_percent():
    """CPU 占用率。

    容器里读 /proc/stat 拿到的就是**宿主的**数据（/proc/stat 不做命名空间隔离），
    所以不需要把宿主 /proc 挂进来。占用率必须靠两次采样的差值，
    首次调用没有基线时现场等 0.3s 补一个基线。
    """
    global _prev_cpu
    with _cpu_lock:
        now = _cpu_ticks()
        prev = _prev_cpu
        _prev_cpu = now
        if prev is None:
            time.sleep(0.3)
            prev, now = now, _cpu_ticks()
            _prev_cpu = now
    di, dt = now[0] - prev[0], now[1] - prev[1]
    if dt <= 0:
        return None
    return max(0.0, min(100.0, (1 - di / float(dt)) * 100))


def read_mem():
    """(总, 可用, 已用%)，同样来自宿主的 /proc/meminfo。"""
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int((v.strip().split() or ["0"])[0])
    except Exception:
        return 0, 0, None
    total = info.get("MemTotal", 0) * 1024
    avail = info.get("MemAvailable", info.get("MemFree", 0)) * 1024
    used_pct = None
    if total:
        used_pct = max(0.0, min(100.0, (total - avail) / float(total) * 100))
    return total, avail, used_pct


def read_temps():
    """扫 /sys/class/hwmon：返回 (cpu温度, [(盘名, 温度), ...], 其它温度)。

    coretemp/k10temp/zenpower = CPU 封装温度；
    drivetemp = SATA 盘（需宿主 `modprobe drivetemp`），nvme = NVMe 盘。
    """
    cpu = None
    disks = []
    others = []
    try:
        import glob
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            try:
                with open(d + "/name") as f:
                    nm = (f.read() or "").strip().lower()
            except Exception:
                continue
            vals = []
            for t in sorted(glob.glob(d + "/temp*_input")):
                try:
                    with open(t) as f:
                        vals.append(int(f.read().strip()) / 1000.0)
                except Exception:
                    pass
            if not vals:
                continue
            # drivetemp 一个盘一个 hwmon；用 device/block 反查盘名（sda/sdb…）
            dev = ""
            try:
                for b in glob.glob(d + "/device/block/*"):
                    dev = b.rsplit("/", 1)[-1]
                    break
            except Exception:
                dev = ""
            if nm in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
                cpu = max(vals)
            elif nm in ("drivetemp", "nvme", "sd"):
                disks.append((dev or nm, max(vals)))
            else:
                others.append((nm, max(vals)))
    except Exception:
        pass
    return cpu, disks, others


def _uptime():
    try:
        with open("/proc/uptime") as f:
            return float((f.read() or "0").split()[0])
    except Exception:
        return 0.0


def host(cfg):
    """本机（飞牛 NAS）监控：CPU 占用 / 内存 / CPU 温度 / 硬盘温度 / 运行天数。

    数据全部来自容器内可见的 /proc 与 /sys（都是宿主视角，无需额外挂载）。
    """
    cpu_pct = cpu_percent()
    total, avail, mem_pct = read_mem()
    ctemp, disks, others = read_temps()
    up = _uptime()

    # 指标格实测只有 92px 宽：4 字标签（"CPU温度 58°C"）必然折行，而 .np-wstats 是
    # grid-auto-rows:1fr —— 一个格子折行会把整行 4 个格子一起从 24px 撑到 60px，
    # 卡片就比别的小组件「胖」一大截（2026-10-16 用户报「内容太多，要和别的一致」）。
    # 所以：标签压到 3 字（CPU温 / 硬盘温，实测单行放得下），"（N 块最高）" 挪进 detail。
    dmax = max((d[1] for d in disks), default=None)
    stats = [
        {"label": "CPU", "value": ("%.0f%%" % cpu_pct) if cpu_pct is not None else "—",
         "tone": "bad" if (cpu_pct or 0) >= 90 else ("warn" if (cpu_pct or 0) >= 70 else "good")},
        {"label": "内存", "value": ("%.0f%%" % mem_pct) if mem_pct is not None else "—",
         "tone": "bad" if (mem_pct or 0) >= 90 else ("warn" if (mem_pct or 0) >= 80 else "good")},
        {"label": "CPU温", "value": ("%.0f°C" % ctemp) if ctemp is not None else "—",
         "tone": "bad" if (ctemp or 0) >= 85 else ("warn" if (ctemp or 0) >= 70 else "default")},
        {"label": "硬盘温", "value": ("%.0f°C" % dmax) if dmax is not None else "—",
         "tone": "bad" if (dmax or 0) >= 55 else "default"},
    ]
    detail = [["CPU 占用", ("%.1f%%" % cpu_pct) if cpu_pct is not None else "取不到"],
              ["内存", ("%s / %s（%.0f%%）" % (fmt_bytes(total - avail), fmt_bytes(total), mem_pct or 0))
               if total else "取不到"],
              ["运行时长", fmt_dur(up) + f"（{up / 86400:.1f} 天）"]]
    if ctemp is not None:
        detail.append(["CPU 温度", "%.1f°C" % ctemp])
    for nm, v in disks:
        detail.append(["硬盘 %s" % nm, "%.1f°C" % v])
    for nm, v in others:
        detail.append([nm, "%.1f°C" % v])

    if disks:
        detail.insert(3, ["硬盘块数", "%d 块（最高 %.1f°C）" % (len(disks), dmax)])
    # 摘要行必须单行：别的小组件是 "↓0 B/s ↑1.1 MB/s" / "当前无播放" 这种 18px 一行，
    # 之前写成 "CPU 15% · 内存 42% · 已运行 5.3 天" 会折成两行（35px）→ 卡片比别的胖。
    # 指标格已经放了 CPU/内存/温度，摘要行留给「运行天数」这一条。
    if up:
        txt = "已运行 %s" % (("%.1f 天" % (up / 86400.0)) if up >= 86400 else fmt_dur(up))
    else:
        txt = "运行时长取不到"
    return _rich(txt, stats, detail)


# ---------------------------------------------------------------- 哪吒探针 (Nezha v1)

def _nz_get(base, path, token):
    with _client() as c:
        r = c.get(base.rstrip("/") + path,
                  headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    if r.status_code in (401, 403):
        raise PermissionError("令牌被拒（HTTP %d）" % r.status_code)
    r.raise_for_status()
    return r.json()


def _nz_walk_servers(data):
    """v1 的响应形状会嵌套（server-group -> servers -> state），把带 id+name 的都挖出来。"""
    out = []

    def walk(x):
        if isinstance(x, dict):
            if "id" in x and "name" in x and ("uuid" in x or "state" in x or "State" in x):
                out.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    return out


def _nz_pick(st, *keys):
    """状态字段在不同版本里命名不一，按候选键依次取。"""
    for k in keys:
        if isinstance(st, dict) and st.get(k) not in (None, ""):
            return st[k]
    return None


def fmt_compact(n):
    """流量用的紧凑写法：1.2T / 340G / 950K（指标格只有 92px 宽，放不下 "340.0 GB"）。

    ≥100 时不带小数（340G 而不是 340.0G），因为摘要行要跟 qb/tr 的 "↓x ↑y" 一样压在一行。
    """
    try:
        n = float(n or 0)
    except Exception:
        return "0B"
    if n < 1024:
        return "%.0fB" % n
    for u in ("K", "M", "G"):
        n /= 1024.0
        if n < 1024:
            return ("%.0f%s" % (n, u)) if n >= 100 else ("%.1f%s" % (n, u))
    return "%.1fT" % (n / 1024.0)


def nezha(cfg):
    base = (cfg.get("url") or "http://192.168.1.10:8008").strip()
    token = (cfg.get("token") or "").strip()
    if not token:
        return {"ok": False, "error": "没填 API 令牌：哪吒面板 → 设置 → API 令牌，"
                                     "权限至少要 nezha:inventory:read"}
    want = (cfg.get("server") or "").strip()

    # 实时状态只在 /api/v1/server-group 的 server.state 里（/api/v1/server 只回元数据，
    # state 恒为 {}）；/api/v1/server/{id}/metrics 走 TSDB，本机 TSDB 是关的，取不到。
    # 字段名取自面板自身前端 bundle 的权威定义（2026-10-16 实测核对）：
    #   state.cpu(百分数) / state.mem_used / state.swap_used / state.disk_used
    #   state.net_in_speed / net_out_speed(字节/秒) / net_in_transfer / net_out_transfer(累计字节)
    #   state.uptime(秒) / state.load_1 / state.process_count
    #   host.mem_total / host.disk_total / host.platform / host.version
    servers, err = [], None
    for path in ("/api/v1/server-group", "/api/v1/server"):
        try:
            j = _nz_get(base, path, token)
        except PermissionError as e:
            err = str(e)
            continue
        except Exception as e:
            err = "连不上哪吒面板：%s" % str(e)[:70]
            break
        servers = _nz_walk_servers(j.get("data") if isinstance(j, dict) else j)
        if servers:
            break

    if not servers:
        if err:
            return _err("%s。令牌需要同时勾选 nezha:inventory:read（服务器列表）"
                        "和 nezha:server:read（实时状态）" % err)
        return _err("令牌能连上面板，但读不到服务器：请确认令牌同时勾了 "
                    "nezha:inventory:read 与 nezha:server:read")

    cur = servers[0]
    if want:
        for s in servers:
            if want in (str(s.get("id")), str(s.get("name"))):
                cur = s
                break

    st = cur.get("state") or {}
    hostinfo = cur.get("host") or {}
    name = str(cur.get("name") or "云服务器")
    if not st:
        return _err("服务器「%s」没有实时数据：哪吒面板认为它未上报（agent 没连上面板）。"
                    "先在面板确认这台机器在线，再回来刷新。" % name)

    cpu = st.get("cpu")
    mu, mt = st.get("mem_used"), hostinfo.get("mem_total")
    ni, no = st.get("net_in_transfer"), st.get("net_out_transfer")
    si, so = st.get("net_in_speed"), st.get("net_out_speed")
    up = st.get("uptime")

    mem_pct = None
    try:
        if mu is not None and mt:
            mem_pct = float(mu) / float(mt) * 100
    except Exception:
        mem_pct = None

    # 四项口径与别的小组件一致：三个指标格 + 摘要行一条
    stats = [
        {"label": "CPU", "value": ("%.0f%%" % float(cpu)) if cpu is not None else "—",
         "tone": "bad" if (cpu or 0) >= 90 else ("warn" if (cpu or 0) >= 75 else "good")},
        {"label": "内存", "value": ("%.0f%%" % mem_pct) if mem_pct is not None else "—",
         "tone": "bad" if (mem_pct or 0) >= 90 else ("warn" if (mem_pct or 0) >= 80 else "good")},
        {"label": "运行", "value": fmt_dur(up) if up else "—"},
    ]
    # 摘要行放上下行累计流量（沿用 qb/tr 的 "↓x ↑y" 写法，保持观感统一）
    if ni is not None or no is not None:
        txt = "↓%s ↑%s" % (fmt_compact(ni or 0), fmt_compact(no or 0))
    elif up:
        txt = "已运行 %s" % (("%.1f 天" % (up / 86400.0)) if up >= 86400 else fmt_dur(up))
    else:
        txt = name

    detail = []
    if si is not None or so is not None:
        detail.append(["实时速率", "↓%s ↑%s" % (fmt_speed(si or 0), fmt_speed(so or 0))])
    if ni is not None or no is not None:
        detail.append(["累计流量", "↓%s ↑%s" % (fmt_compact(ni or 0), fmt_compact(no or 0))])
    if mem_pct is not None:
        detail.append(["内存", "%s / %s（%.0f%%）" % (fmt_bytes(mu), fmt_bytes(mt), mem_pct)])
    if up:
        detail.append(["运行时长", "%s（%.1f 天）" % (fmt_dur(up), up / 86400.0)])
    if st.get("load_1") is not None:
        detail.append(["系统负载", "%.2f" % float(st["load_1"])])
    if hostinfo.get("platform"):
        detail.append(["系统", str(hostinfo["platform"])])
    if st.get("process_count") is not None:
        detail.append(["进程数", str(st["process_count"])])
    return _rich(txt, stats, detail)


# ---------------------------------------------------------------- mihomo / Clash

# 策略组类型：这些组才有「当前选了谁」，直连/节点本身没有
_MH_GROUP_TYPES = ("Selector", "URLTest", "Fallback", "LoadBalance", "Relay")
# 订阅型 provider 的 vehicleType（真正的「机场」）；Compatible 是策略组伪装成的 provider
_MH_SUB_VEHICLES = ("HTTP", "File")

_mh_last = {}          # base -> (ts, down_total, up_total)，/traffic 不可用时算速率用


def _mh_get(c, base, path, secret):
    h = {}
    if secret:
        h["Authorization"] = "Bearer " + secret
    r = c.get(base + path, headers=h)
    if r.status_code in (401, 403):
        raise PermissionError("mihomo 需要密钥：请在小组件里填 secret")
    r.raise_for_status()
    return r.json()


def _mh_speed(c, base, secret):
    """/traffic 是长连接逐秒吐 {"up":x,"down":y}，读第一行就断开。失败返回 None。"""
    h = {}
    if secret:
        h["Authorization"] = "Bearer " + secret
    try:
        with c.stream("GET", base + "/traffic", headers=h, timeout=6.0) as r:
            if r.status_code >= 400:
                return None
            for line in r.iter_lines():
                line = (line or "").strip()
                if line.startswith("{"):
                    j = json.loads(line)
                    return int(j.get("down") or 0), int(j.get("up") or 0)
    except Exception:
        return None
    return None


def _mh_rate(base, dl, ul):
    now = time.time()
    prev = _mh_last.get(base)
    _mh_last[base] = (now, dl, ul)
    if not prev:
        return 0, 0
    dt = max(0.001, now - prev[0])
    return max(0, int((dl - prev[1]) / dt)), max(0, int((ul - prev[2]) / dt))


def _mh_delay_of(p):
    hist = (p or {}).get("history") or []
    for h in reversed(hist):
        d = h.get("delay")
        if isinstance(d, (int, float)) and d:
            return int(d)
    return None


def _mh_resolve(proxies, nodes, name):
    """策略组可能套策略组（AI平台 → 故障转移 → 香港策略 → 香港自动 → 节点）。

    节点数据不在 /proxies 里（那边只给策略组和 DIRECT/REJECT），
    必须去 /providers/proxies 的 nodes 表里查，否则延迟永远是空的。

    返回 (路径[除起点的名字...], 延迟毫秒或 None)；沿途任一层有测速结果就先用它兜底。
    """
    path, delay = [], None
    cur = name
    for _ in range(6):
        p = proxies.get(cur) or nodes.get(cur)
        if not p:
            return path, delay
        d = _mh_delay_of(p)
        if d:
            delay = d
        if p.get("type") not in _MH_GROUP_TYPES:
            return path, delay
        nxt = p.get("now")
        if not nxt or nxt == cur:
            return path, delay
        path.append(nxt)
        cur = nxt
    return path, delay


def mihomo(cfg):
    base = _base(cfg)
    if not base:
        return _err("未填外部控制地址（如 http://192.168.1.109:9090）")
    secret = (cfg.get("secret") or "").strip()
    try:
        # 不能吃环境里的 HTTP(S)_PROXY：mihomo 自己就常被设成系统代理，
        # 走代理再去连 9090 会自己转发自己（实测 502）
        with _client(trust_env=False) as c:
            ver = _mh_get(c, base, "/version", secret)
            conn = _mh_get(c, base, "/connections", secret)
            proxies = (_mh_get(c, base, "/proxies", secret) or {}).get("proxies") or {}
            providers = (_mh_get(c, base, "/providers/proxies", secret) or {}).get("providers") or {}
            live = _mh_speed(c, base, secret)
    except PermissionError as e:
        return _err(str(e))
    except Exception as e:
        return _err("连不上 mihomo：%s" % str(e)[:90])

    n_conn = len(conn.get("connections") or [])
    dl = int(conn.get("downloadTotal") or 0)
    ul = int(conn.get("uploadTotal") or 0)
    down, up = live if live else _mh_rate(base, dl, ul)

    subs = [k for k, v in providers.items() if (v or {}).get("vehicleType") in _MH_SUB_VEHICLES]
    n_nodes = sum(len((providers.get(k) or {}).get("proxies") or []) for k in subs)
    n_air = len(subs)
    if not n_air:      # 全是 Compatible（策略组伪装）时，退一步按非 Compatible 数算
        n_air = len([1 for v in providers.values()
                     if (v or {}).get("vehicleType") not in (None, "", "Compatible")])

    stats = [
        {"label": "连接", "value": str(n_conn)},
        {"label": "↓", "value": fmt_speed(down), "tone": "good" if down else "muted"},
        {"label": "↑", "value": fmt_speed(up), "tone": "good" if up else "muted"},
        {"label": "机场", "value": str(n_air)},
    ]
    text = "累计 ↓%s ↑%s" % (fmt_compact(dl), fmt_compact(ul))

    detail = [
        ["累计流量", "↓%s ↑%s" % (fmt_compact(dl), fmt_compact(ul))],
        ["机场订阅", "%d 个 · %d 节点" % (n_air, n_nodes) if n_air else "—"],
    ]
    if ver.get("version"):
        detail.append(["内核", str(ver["version"])])

    # 节点（含延迟）只在 /providers/proxies 里，先摊平成一张名字表
    nodes = {}
    for v in providers.values():
        for it in (v.get("proxies") or []):
            nm = (it or {}).get("name")
            if nm:
                nodes[nm] = it

    groups = [(k, v) for k, v in proxies.items()
              if (v or {}).get("type") in _MH_GROUP_TYPES and k != "GLOBAL"]
    for name, g in groups[:40]:
        now = g.get("now") or ""
        if not now:
            detail.append([name, "未选择"])
            continue
        path, delay = _mh_resolve(proxies, nodes, name)
        chain = " → ".join(path[-2:]) if path else now      # 最多显示两跳，太长弹层放不下
        if delay:
            chain += " · %dms" % delay
        detail.append([name, chain])
    if len(groups) > 40:
        detail.append(["……", "还有 %d 个策略组" % (len(groups) - 40)])

    return _rich(text, stats, detail)


# ---------------------------------------------------------------- 通用

def generic(cfg):
    """自定义：给一个返回 JSON 的地址，用点号路径取一个数值/字符串。"""
    base = _base(cfg)
    if not base:
        return _err("未填地址")
    h = {}
    if cfg.get("apikey"):
        h["Authorization"] = "Bearer " + cfg["apikey"]
    try:
        with _client(headers=h) as c:
            r = c.get(base)
            r.raise_for_status()
            j = r.json()
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    path = (cfg.get("path") or "").strip()
    val = j
    for seg in [s for s in path.split(".") if s]:
        if isinstance(val, list):
            try:
                val = val[int(seg)]
            except Exception:
                return _err(f"路径 {path} 取不到值")
        elif isinstance(val, dict):
            val = val.get(seg)
        else:
            return _err(f"路径 {path} 取不到值")
    if val is None:
        return _err(f"路径 {path} 取不到值")
    unit = (cfg.get("unit") or "").strip()
    label = (cfg.get("label") or "").strip() or (path.split(".")[-1] if path else "值")
    text = f"{label} {val}{unit}"
    return _rich(text, [{"label": label, "value": f"{val}{unit}"}],
                 [[label, f"{val}{unit}"]])


PROVIDERS = {
    "qbittorrent": {
        "label": "qBittorrent", "icon": "⚡",
        "fields": ["url", "username", "password"],
        "hint": "地址填 qBittorrent 的 WebUI，如 http://192.168.1.10:8889",
        "fetch": qbittorrent,
    },
    "transmission": {
        "label": "Transmission", "icon": "🌀",
        "fields": ["url", "username", "password"],
        "hint": "地址填 TR 的 RPC 根地址，如 http://192.168.1.10:9091",
        "fetch": transmission,
    },
    "moviepilot": {
        "label": "MoviePilot", "icon": "🎬",
        "fields": ["url", "apikey", "username", "password"],
        "hint": "API Token 在 MP 的 app.env 里（API_TOKEN，注意去掉引号）；也可以直接填面板登录用户名+密码。显示订阅总数/追更中/待更新缺集。",
        "fetch": moviepilot,
    },
    "emby": {
        "label": "Emby", "icon": "📺",
        "fields": ["url", "apikey"],
        "hint": "API Key 在 Emby 控制台 → 高级 → API 密钥里新建",
        "fetch": emby,
    },
    "jellyfin": {
        "label": "Jellyfin", "icon": "📺",
        "fields": ["url", "apikey"],
        "hint": "API Key 在 Jellyfin 控制台 → API 密钥里新建",
        "fetch": jellyfin,
    },
    "plex": {
        "label": "Plex", "icon": "🎞️",
        "fields": ["url", "apikey"],
        "hint": "X-Plex-Token 在 Plex 的 XML / 账号页里找",
        "fetch": plex,
    },
    "audiobookshelf": {
        "label": "Audiobookshelf", "icon": "🎧",
        "fields": ["url", "apikey"],
        "hint": "API Token 在 ABS 用户设置里（Settings → Users）",
        "fetch": audiobookshelf,
    },
    "navidrome": {
        "label": "Navidrome", "icon": "🎵",
        "fields": ["url", "username", "password"],
        "hint": "地址填 Navidrome 根地址，如 http://192.168.1.10:4533；用户名/密码填 Navidrome 的登录账号"
                "（读它自己的 API 拿精确总数，显示专辑 / 艺术家 / 歌曲 / 歌单）。",
        "fetch": navidrome,
    },
    "host": {
        "label": "本机 / NAS", "icon": "🖥️",
        "fields": [],
        "hint": "监控本机（飞牛 NAS）：CPU 占用 / 内存 / CPU 温度 / 硬盘温度 / 运行天数。"
                "数据直接读容器内可见的宿主 /proc 与 /sys，不用填任何东西。"
                "SATA 盘温度需要宿主先 `modprobe drivetemp`（NVMe 与 CPU 温度本来就有）。",
        "fetch": host,
    },
    "nezha": {
        "label": "哪吒探针 / 云服务器", "icon": "🌐",
        "fields": [
            {"key": "url", "label": "面板地址", "ph": "http://192.168.1.10:8008",
             "hint": "哪吒 v1 面板地址（8008 是面板，5555 是 agent 端口，别填错）"},
            {"key": "token", "label": "API 令牌", "ph": "nzp_xxx",
             "hint": "面板 → 设置 → API 令牌，权限至少 nezha:inventory:read"},
            {"key": "server", "label": "服务器（可选）", "ph": "留空 = 第一台",
             "hint": "填服务器名或 id，多台时指定显示哪台"},
        ],
        "hint": "显示云服务器的 CPU / 内存 / 上下行累计流量 / 运行时长（数据来自哪吒 v1 面板 API）。",
        "fetch": nezha,
    },
    "mihomo": {
        "label": "mihomo / Clash", "icon": "🐱",
        "fields": [
            {"key": "url", "label": "外部控制地址", "ph": "http://192.168.1.109:9090",
             "hint": "mihomo 的 external-controller 地址（docker 里映射的 9090 端口），不是订阅链接"},
            {"key": "secret", "label": "密钥（没设就留空）", "ph": "config.yaml 里的 secret",
             "hint": "外部控制密码；mihomo 没配 secret 时留空即可"},
        ],
        "hint": "显示连接数 / 上下行速率 / 机场订阅数；鼠标悬停卡片看每个策略组当前走的线路和延迟。",
        "fetch": mihomo,
    },
    "generic": {
        "label": "自定义 JSON", "icon": "🔧",
        "fields": ["url", "apikey", "path", "label", "unit"],
        "hint": "给一个返回 JSON 的地址，path 用点号取字段，如 data.count",
        "fetch": generic,
    },
}


def provider_list():
    out = []
    for k, v in PROVIDERS.items():
        out.append({"type": k, "label": v["label"], "icon": v["icon"],
                    "fields": v["fields"], "hint": v["hint"]})
    return out


# ---------------------------------------------------------------- 取数 + 缓存

def _key(cfg):
    raw = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()


def fetch(cfg, use_cache=True, ttl=CACHE_TTL):
    """取单个小组件的数据。失败也缓存一小会儿，避免失败风暴。"""
    if not cfg or not cfg.get("type"):
        return {"ok": False, "error": "未配置"}
    p = PROVIDERS.get(cfg["type"])
    if not p:
        return {"ok": False, "error": f"未知类型 {cfg['type']}"}

    k = _key(cfg)
    now = time.time()
    if use_cache:
        with _cache_lock:
            hit = _cache.get(k)
        if hit and now - hit[0] < ttl:
            return hit[1]

    try:
        res = p["fetch"](cfg)
    except Exception as e:
        res = _err(f"{type(e).__name__}: {e}")
    if not isinstance(res, dict):
        res = _err("provider 返回异常")
    res.setdefault("ok", False)
    res["ts"] = int(now)

    with _cache_lock:
        _cache[k] = (now, res)
        if len(_cache) > 500:
            for kk in sorted(_cache, key=lambda x: _cache[x][0])[:200]:
                _cache.pop(kk, None)
    return res


def fetch_many(items, ttl=CACHE_TTL):
    """并发取多个条目的小组件。返回 {item_id: result}。"""
    jobs = [(it["id"], it.get("widget")) for it in items if it.get("widget")]
    if not jobs:
        return {}
    out = {}
    lock = threading.Lock()

    def run(iid, cfg):
        r = fetch(cfg, ttl=ttl)
        with lock:
            out[iid] = r

    threads = []
    for iid, cfg in jobs[:60]:
        t = threading.Thread(target=run, args=(iid, cfg), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=TIMEOUT + 3)
    return out


def test(cfg):
    """管理界面「测试」按钮用：不走缓存。"""
    return fetch(cfg, use_cache=False)
