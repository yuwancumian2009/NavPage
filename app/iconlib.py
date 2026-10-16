"""在线图标库 —— **可配置多个** GitHub 公开图标仓库（默认内置 homarr-labs/dashboard-icons）。

四件事：

  1) list_libs()   读配置（settings.icon_libs）+ 各自的清单缓存状态
  2) refresh()     拉某个库的全量清单（GitHub tree API）→ 缓存到 /data/iconlib_cache/<key>.json
  3) search()      按关键词在选定库里匹配，返回可直接预览的 URL（默认只查缓存，不联网）
  4) download()    选中的图标抓到 /data/icons/lib_<名字>.<ext>，所有卡片可复用

库地址支持这几种写法（见 parse_lib_input）：

    homarr-labs/dashboard-icons
    https://github.com/homarr-labs/dashboard-icons
    https://github.com/homarr-labs/dashboard-icons/tree/main
    https://github.com/owner/repo/tree/分支/子目录

依赖容器能直连 api.github.com / cdn.jsdelivr.net / raw.githubusercontent.com，
直连被限流或没有外网出口时自动回退面板里的 image_proxy。清单走磁盘缓存（7 天过期）。
"""
import os
import re
import json
import time
import threading
from urllib.parse import quote

import httpx

from . import db, icons

# 内置默认库（db.DEFAULT_SETTINGS["icon_libs"] 里那一项要与这里保持一致）
DEFAULT_LIB = {"repo": "homarr-labs/dashboard-icons", "branch": "main", "label": "Dashboard Icons"}

GH_TREE = "https://api.github.com/repos/%s/git/trees/%s?recursive=1"
GH_REPO = "https://api.github.com/repos/%s"
CDN_BASE = "https://cdn.jsdelivr.net/gh/%s@%s"          # 预览用（jsdelivr 有缓存、国内通常可达）
RAW_BASE = "https://raw.githubusercontent.com/%s/%s"    # 兜底（jsdelivr 不收录时）
CACHE_DIR = os.path.join(db.DATA_DIR, "iconlib_cache")
TTL = 7 * 86400
UA = {"User-Agent": "navpage-iconlib/1.1"}
EXTS = ("svg", "png", "webp", "ico", "jpg", "jpeg", "gif", "avif")
# 变体优先顺序（同一个名字有多个格式时，默认挑前面的）
VARIANTS = ("svg", "webp", "png", "ico")
# 明显不是图标的目录，直接跳过（隐藏目录另外按 "." 前缀跳）
SKIP_DIRS = {"node_modules", "test", "tests", "docs", "doc", "spec", "__pycache__"}
MAX_ITEMS = 40000        # 单个库最多收这么多图标（防止超大仓库撑爆缓存）

_lock = threading.Lock()
_mem = {}                # {key: {"at": ts, "items": [...]}}

# 没输关键词时优先展示的常用图标（顺序即展示顺序；不在库里的自动跳过）。
# 分了几组，纯粹为了维护时好找 —— 前 60 个就是打开面板第一屏看到的。
POPULAR = [
    # 影音
    "plex", "jellyfin", "emby", "kodi", "stremio", "vlc", "mpv", "potplayer",
    "navidrome", "airsonic", "funkwhale", "ampache", "koel", "mstream", "xiaomusic", "music-tag-web",
    "audiobookshelf", "calibre", "calibre-web", "kavita", "komga", "bookstack", "readarr",
    "sonarr", "radarr", "lidarr", "prowlarr", "bazarr", "jackett", "nzbhydra", "sabnzbd", "nzbget",
    "overseerr", "jellyseerr", "ombi", "tautulli", "moviepilot", "flexget", "flaresolverr",
    "handbrake", "ffmpeg", "tdarr", "unmanic", "fileflows", "makemkv", "tiny-media-manager",
    # 下载 / 网盘 / 文件
    "qbittorrent", "transmission", "deluge", "rtorrent", "aria2", "motrix", "jdownloader", "alist",
    "filebrowser", "filegator", "cloudreve", "seafile", "nextcloud", "owncloud", "syncthing",
    "rclone", "duplicati", "restic", "borg", "kopia", "urbackup", "proxmox-backup-server",
    "minio", "ceph", "seaweedfs", "garage", "openlist",
    # 家庭自动化 / 影音识别
    "home-assistant", "esphome", "zigbee2mqtt", "mosquitto", "openhab", "domoticz", "homebridge",
    "frigate", "motioneye", "shinobi", "zoneminder", "viseron", "double-take", "scrypted", "deepstack",
    # 照片 / 笔记 / 阅读
    "immich", "photoprism", "photoview", "piwigo", "lychee", "memos", "shiori", "linkding", "wallabag",
    "freshrss", "miniflux", "rsshub", "changedetection", "vaultwarden", "bitwarden", "keepass",
    # 运维 / 监控
    "portainer", "docker", "kubernetes", "rancher", "podman", "casaos", "cockpit", "semaphore",
    "watchtower", "diun", "uptime-kuma", "grafana", "prometheus", "influxdb", "telegraf", "netdata",
    "glances", "cadvisor", "zabbix", "nagios", "librespeed", "speedtest-tracker", "smokeping", "netbox",
    "nezha", "autospeed", "flame", "homepage", "dashy", "heimdall", "gethomepage",
    # 网络 / 网关 / DNS
    "nginx", "nginx-proxy-manager", "traefik", "caddy", "haproxy", "apache", "httpd", "frp",
    "cloudflared", "ddns-go", "lucky", "nps", "wireguard", "tailscale", "zerotier", "netbird",
    "openvpn", "adguard-home", "pi-hole", "unbound", "dnsmasq", "coredns", "blocky", "openwrt",
    "ikuai", "routeros", "wireguard-ui",
    # 数据库 / 中间件
    "mysql", "mariadb", "postgresql", "redis", "mongodb", "clickhouse", "sqlite", "adminer",
    "phpmyadmin", "pgadmin", "dbeaver", "elasticsearch", "rabbitmq", "kafka", "etcd",
    # 开发 / CI / 自动化
    "gitea", "forgejo", "gitlab", "github", "gitee", "jenkins", "drone", "woodpecker", "argocd",
    "sonarqube", "code-server", "openvscode-server", "jupyter", "jupyterlab", "rstudio", "kasm",
    "n8n", "node-red", "homeassistant", "nodered", "hass", "openai", "ollama", "hermes", "lobe-chat",
    "chatgpt", "claude", "stable-diffusion", "comfyui", "automatic1111", "open-webui", "new-api",
    # 面板 / 账号 / 通讯
    "authentik", "authelia", "keycloak", "casdoor", "ldap", "matrix", "element", "rocket-chat",
    "mattermost", "discord", "telegram", "slack", "wechat", "qq", "mastodon", "misskey", "lemmy",
    "bluesky", "plexamp", "synology", "truenas", "openmediavault", "unraid", "freenas", "omv",
    "proxmox", "esxi", "vmware", "virtualbox", "linux", "ubuntu", "debian", "centos", "docker-compose",
    # 游戏 / 娱乐
    "steam", "steamdeck", "lutris", "pterodactyl", "minecraft", "valheim", "terraria", "factorio",
    "palworld", "romm", "emulatorjs", "playnite", "epicgames",
]
# 允许上面手写时重复（同一个名字出现在两组里很正常），去重但保持首次出现的顺序
POPULAR = list(dict.fromkeys(POPULAR))


def _safe_name(name):
    """图标名 → 安全 slug（只允许字母数字和 . _ -）"""
    name = (name or "").strip().lower()
    name = re.sub(r"\.(svg|png|webp|ico|jpe?g|gif|avif)$", "", name)
    name = re.sub(r"[^a-z0-9._-]", "-", name).strip("-")
    return name[:80]


def _why(e):
    """异常 → 给用户看的一句话原因"""
    s = str(e) or ""
    m = re.search(r"HTTP (\d+)", s)
    if m:
        code = m.group(1)
        if code == "404":
            return "仓库或分支不存在（HTTP 404）"
        if code in ("403", "429"):
            return "GitHub 接口被限流（HTTP %s），过一会儿再试" % code
        return "图标源返回 HTTP %s" % code
    n = type(e).__name__
    if "Timeout" in n or "timeout" in s.lower():
        return "连接超时（容器可能没有外网出口）"
    if "Connect" in n:
        return "连接不上（容器可能没有外网出口，可在面板里配 image_proxy）"
    return s or n


# ---------------------------------------------------------------- 库地址解析 / 配置

def parse_lib_input(text, label="", branch=""):
    """把用户输入解析成 {"repo","branch","label","sub"}。

    支持：owner/repo、https://github.com/owner/repo、.../tree/分支[/子目录]。
    分支留空表示「用仓库默认分支」，由 default_branch() 查出来。
    非法输入抛 ValueError（消息直接给前端看）。
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("请输入仓库地址，如 homarr-labs/dashboard-icons")
    s = re.sub(r"^git\+", "", s)
    s = re.sub(r"\.git$", "", s.rstrip("/"))
    owner = name = ""
    sub = ""
    url_branch = ""
    m = re.match(
        r"^(?:https?://)?(?:www\.)?github\.com/([^/?#]+)/([^/?#]+)"
        r"(?:/(?:tree|blob)/([^/?#]+)(?:/(.*))?)?", s, re.I)
    if m:
        owner, name = m.group(1), m.group(2)
        url_branch = (m.group(3) or "")
        sub = (m.group(4) or "")
    else:
        m2 = re.match(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$", s)
        if not m2:
            raise ValueError("格式应为 owner/repo 或 https://github.com/owner/repo")
        owner, name = m2.group(1), m2.group(2)
    if not owner or not name:
        raise ValueError("没能从地址里解析出 owner/repo")
    repo = "%s/%s" % (owner, name)
    return {
        "repo": repo,
        # 分支：显式传的 > 地址里带的（.../tree/分支）> 留空（由 default_branch() 查默认分支）
        "branch": (branch or url_branch or "").strip(),
        "label": (label or "").strip()[:40] or name,
        "sub": (sub or "").strip().strip("/"),
    }


def default_branch(repo, timeout=20.0):
    """问 GitHub 仓库的默认分支（用户没写分支时用，别硬套 main）。查不到就回 main。"""
    try:
        d = _get_json(GH_REPO % repo, timeout)
        b = (d.get("default_branch") or "").strip()
        if b:
            return b
    except Exception:
        pass
    return "main"


def get_libs():
    """配置里的库（settings.icon_libs），逐项校验补全；非法项丢弃。"""
    try:
        raw = db.get_settings().get("icon_libs")
    except Exception:
        raw = None
    if not isinstance(raw, list):
        raw = [dict(DEFAULT_LIB)]
    out = []
    seen = set()
    for it in raw:
        if isinstance(it, str):
            try:
                it = parse_lib_input(it)
            except ValueError:
                continue
        if not isinstance(it, dict):
            continue
        repo = (it.get("repo") or "").strip().strip("/")
        if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
            continue
        if repo.lower() in seen:
            continue
        seen.add(repo.lower())
        out.append({
            "repo": repo,
            "branch": (it.get("branch") or "main").strip() or "main",
            "label": (it.get("label") or repo.split("/")[-1]).strip()[:40],
            "sub": (it.get("sub") or "").strip("/"),
        })
    return out


def save_libs(libs):
    """写回 settings.icon_libs（只留合法字段）。"""
    clean = []
    for l in (libs or []):
        clean.append({
            "repo": l["repo"], "branch": l.get("branch") or "main",
            "label": l.get("label") or l["repo"].split("/")[-1],
            "sub": l.get("sub") or "",
        })
    db.update_settings({"icon_libs": clean})
    return get_libs()


def _key(lib):
    s = "%s@%s" % (lib.get("repo", ""), lib.get("branch") or "main")
    if lib.get("sub"):
        s += "#" + lib["sub"]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:120]


# ---------------------------------------------------------------- 清单抓取 / 解析

def _proxy_url():
    """面板设置里的镜像代理（image_proxy）。容器直连 GitHub 被限流时兜底用。"""
    try:
        v = (db.get_settings().get("image_proxy") or "").strip()
    except Exception:
        return ""
    return v if v.startswith("http") else ""


def _client(timeout, proxy=None):
    kw = {"timeout": timeout, "headers": UA, "follow_redirects": True}
    if proxy:
        kw["proxy"] = proxy
    return httpx.Client(**kw)


def _get(url, timeout=40.0):
    """GET 一个 URL（要求 200）。优先直连，失败/限流再试面板里配的代理。"""
    tries = [None]
    p = _proxy_url()
    if p:
        tries.append(p)
    last = None
    for proxy in tries:
        try:
            with _client(timeout, proxy) as c:
                r = c.get(url)
            if r.status_code == 200:
                return r
            last = RuntimeError("HTTP %d" % r.status_code)
        except Exception as e:
            last = e
    raise last or RuntimeError("请求失败")


def _get_json(url, timeout=30.0):
    return _get(url, timeout).json()


def _parse_tree(paths, repo, branch="main", sub=""):
    """tree 路径列表 → [{name, variants, urls}]。

    变体名取「一级目录名」（dashboard-icons 的 svg/ png/ webp/、别的库的 icons/、
    border-radius/ 都行），根目录下的文件用扩展名当变体。
    同一个名字同一变体只留第一个；同一个名字多个变体合并成一项。
    """
    cdn = CDN_BASE % (repo, branch)
    raw = RAW_BASE % (repo, branch)
    pre = (sub.strip("/") + "/") if sub else ""
    bag = {}
    for p in paths:
        if pre:
            if not p.startswith(pre):
                continue
            rel = p[len(pre):]
        else:
            rel = p
        parts = rel.split("/")
        if not parts or any(x.startswith(".") for x in parts):
            continue
        fname = parts[-1]
        stem, _, ext = fname.rpartition(".")
        ext = ext.lower()
        if not stem or ext not in EXTS:
            continue
        variant = parts[0].lower() if len(parts) > 1 else ext
        if variant in SKIP_DIRS:
            continue
        name = _safe_name(stem)
        if not name:
            continue
        rec = bag.setdefault(name, {})
        if variant not in rec:
            rec[variant] = {"cdn": "%s/%s" % (cdn, p), "raw": "%s/%s" % (raw, p), "ext": ext}
    out = []
    for name, vs in bag.items():
        order = [v for v in VARIANTS if v in vs] + sorted(v for v in vs if v not in VARIANTS)
        out.append({"name": name, "variants": order, "urls": vs})
    out.sort(key=lambda x: x["name"])
    return out[:MAX_ITEMS]


def _load_disk(key):
    p = os.path.join(CACHE_DIR, key + ".json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("items"), list) and d["items"]:
            return int(d.get("at") or 0), d["items"]
    except Exception:
        pass
    return 0, []


def _save_disk(key, at, items):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        p = os.path.join(CACHE_DIR, key + ".json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": at, "items": items}, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def refresh(lib, timeout=60.0):
    """拉某个库的全量清单并落盘。返回 (items, at)。失败抛 RuntimeError（消息可直接展示）。"""
    repo, branch = lib["repo"], lib.get("branch") or "main"
    try:
        d = _get_json(GH_TREE % (repo, quote(branch, safe="")), timeout)
    except Exception as e:
        raise RuntimeError(_why(e))
    paths = [t["path"] for t in (d.get("tree") or []) if t.get("type") == "blob"]
    if not paths:
        raise RuntimeError("仓库里没读到文件（分支 %s 对不对？）" % branch)
    items = _parse_tree(paths, repo, branch, lib.get("sub") or "")
    if not items:
        raise RuntimeError("这个仓库里没找到图标文件（支持 svg/png/webp/ico/jpg/gif，可指定子目录）")
    at = int(time.time())
    key = _key(lib)
    _mem[key] = {"at": at, "items": items}
    _save_disk(key, at, items)
    return items, at


def get_items(lib, ensure=True, force=False):
    """返回 (items, cached_at)。内存 → 磁盘 → 联网三级，TTL 内不联网。

    ensure=False 时只读缓存（不联网）；缺缓存会抛 RuntimeError。
    """
    key = _key(lib)
    now = int(time.time())
    hit = _mem.get(key)
    if hit and hit["items"] and (not force) and now - hit["at"] < TTL:
        return hit["items"], hit["at"]
    with _lock:
        hit = _mem.get(key)
        if hit and hit["items"] and (not force) and now - hit["at"] < TTL:
            return hit["items"], hit["at"]
        at, items = _load_disk(key)
        if items and (not force) and now - at < TTL:
            _mem[key] = {"at": at, "items": items}
            return items, at
        if not ensure and not items:
            raise RuntimeError("这个库还没拉过清单，点一下「更新清单」")
        try:
            return refresh(lib)
        except Exception:
            # 联网失败就退回旧缓存（哪怕过期），总比空手强
            if items:
                _mem[key] = {"at": at, "items": items}
                return items, at
            raise


def list_libs(ensure=False):
    """配置的库 + 各自的缓存状态（count / cached_at / error），给管理界面用。"""
    now = int(time.time())
    out = []
    for lib in get_libs():
        key = _key(lib)
        at, items = _load_disk(key)
        err = ""
        if ensure and (not items or now - at >= TTL):
            try:
                items, at = refresh(lib)
            except Exception as e:
                err = str(e) if isinstance(e, RuntimeError) else _why(e)
                at, items = _load_disk(key)
        out.append({
            "id": lib["repo"], "repo": lib["repo"], "branch": lib["branch"],
            "label": lib["label"], "sub": lib["sub"],
            "count": len(items), "cached_at": at, "error": err,
        })
    return out


# ---------------------------------------------------------------- 搜索 / 选用

def _score(name, q):
    if name == q:
        return 0
    if name.startswith(q):
        return 1
    if re.search(r"(^|[-_.])" + re.escape(q), name):
        return 2
    if q in name:
        return 3
    return 99


def search(q="", limit=60, variant="", lib_id="", offset=0, ensure=True):
    """在（选定的）库里搜索。返回 dict：items / total / cached_at / libs / errors。

    q 为空时：先是内置常用表里能匹配上的（第一屏），后面接库里其余全部图标（可按「加载更多」翻）。
    每个 item 带来源库 lib / lib_label，方便前端打标签、按库筛选。
    """
    libs = [l for l in get_libs() if not lib_id or l["repo"] == lib_id]
    errors = []
    ats = []
    by_name = {}
    for lib in libs:
        try:
            items, at = get_items(lib, ensure=ensure)
        except Exception as e:
            errors.append("%s：%s" % (lib["label"], str(e) if isinstance(e, RuntimeError) else _why(e)))
            continue
        ats.append(at)
        for it in items:
            # 同名图标多个库都有时，先配的库优先（默认那个库排最前）
            by_name.setdefault(it["name"], (lib, it))
    q = (q or "").strip().lower()
    flat = []
    if not q:
        # 空关键词：先用内置常用表（用户第一屏看到的就是这些），后面接库里其余全部图标，
        # 按名字排序 —— 配合 offset/limit，「加载更多」可以一路翻完整个库。
        seen = set()
        for nm in POPULAR:
            hit = by_name.get(nm)
            if hit and nm not in seen:
                seen.add(nm)
                flat.append((len(flat), hit))
        for nm in sorted(by_name.keys()):
            if nm not in seen:
                seen.add(nm)
                flat.append((0, by_name[nm]))
    else:
        for nm, hit in by_name.items():
            s = _score(nm, q)
            if s < 99:
                flat.append((s * 1000 + len(nm), hit))
        flat.sort(key=lambda x: (x[0], x[1][1]["name"]))
    total = len(flat)
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 60), 200))
    out = []
    for _, (lib, it) in flat[offset:offset + limit]:
        vs = it["variants"]
        var = variant if variant in vs else (vs[0] if vs else "")
        rec = it["urls"].get(var) or {}
        out.append({
            "name": it["name"], "variant": var, "variants": vs,
            "lib": lib["repo"], "lib_label": lib["label"],
            "cdn": rec.get("cdn", ""),
            # 浏览器直连 CDN 拿不到时（内网无外网出口 / jsdelivr 不收录）退回本机代理，
            # 服务端抓取并缓存 —— 优先用 raw.githubusercontent，绕开 jsdelivr 的限制
            "proxy": "/api/icon?url=" + quote(rec.get("raw") or rec.get("cdn") or "", safe=""),
        })
    return {
        "items": out, "total": total,
        "cached_at": (min(ats) if ats else 0),
        "libs": [{"id": l["repo"], "label": l["label"]} for l in libs],
        "errors": errors,
    }


def download(name, variant="", lib_id=""):
    """把图标抓到 /data/icons/lib_<名字>.<ext>。返回 (文件名, 错误原因)。"""
    slug = _safe_name(name)
    if not slug:
        return None, "图标名非法"
    hit = None
    for lib in [l for l in get_libs() if not lib_id or l["repo"] == lib_id]:
        try:
            items, _ = get_items(lib)
        except Exception:
            continue
        for it in items:
            if it["name"] == slug:
                hit = (lib, it)
                break
        if hit:
            break
    if not hit:
        return None, "在线库里找不到「%s」，先「更新清单」再试" % slug
    _lib, it = hit
    vs = it["variants"]
    order = ([variant] if variant in vs else []) + [v for v in vs if v != variant]
    last = None
    for v in order:
        rec = it["urls"].get(v) or {}
        for url in (rec.get("cdn"), rec.get("raw")):
            if not url:
                continue
            try:
                r = _get(url, 20.0)
            except Exception as e:
                last = "下载失败：%s" % _why(e)
                continue
            data = r.content
            if not data:
                last = "图标内容为空"
                continue
            if len(data) > icons.MAX_BYTES:
                last = "图标文件过大（>%dKB）" % (icons.MAX_BYTES // 1024)
                continue
            ext = icons._detect_ext(data, r.headers.get("content-type")) or rec.get("ext") or "svg"
            os.makedirs(icons.ICON_DIR, exist_ok=True)
            fname = "lib_%s.%s" % (slug, ext)
            with open(os.path.join(icons.ICON_DIR, fname), "wb") as f:
                f.write(data)
            return fname, ""
    return None, last or "图标下载失败"


def kind_of(name):
    """本地图标的来源分类，前端用来分组/标注"""
    n = (name or "")
    if n.startswith("up_"):
        return "upload"
    if n.startswith("lib_"):
        return "library"
    return "cache"
