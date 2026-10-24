"""NavPage 存储层：SQLite + 设置默认值。

所有配置存在 settings 表，值统一 JSON 编码，读出来保持原类型。
"""
import os
import json
import time
import sqlite3
import hashlib
import hmac
import base64
import secrets
import threading

from . import netutil

DATA_DIR = os.environ.get("NAVPAGE_DATA", "/data")
DB_PATH = os.path.join(DATA_DIR, "navpage.db")
WALLPAPER_DIR = os.path.join(DATA_DIR, "wallpapers")
ICON_DIR = os.path.join(DATA_DIR, "icons")
LOGO_DIR = os.path.join(DATA_DIR, "logo")

DEFAULT_LAN_CIDRS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,"
    "169.254.0.0/16,100.64.0.0/10"
)

DEFAULT_ENGINES = [
    {"name": "必应", "url": "https://www.bing.com/search?q="},
    {"name": "百度", "url": "https://www.baidu.com/s?wd="},
    {"name": "Google", "url": "https://www.google.com/search?q="},
]

# 全新部署的初始管理员密码，从环境变量 NAVPAGE_DEFAULT_PASSWORD 读。
# 首次 init() 时如果库里还没有密码，就用它，并且强制 require_login，
# 避免"默认密码形同虚设"；环境变量没设就随机生成一个并打印到启动日志。
# 这里刻意**不写死**任何口令：本仓库的镜像会发布到 Docker Hub，
# 镜像里带固定默认密码等于把所有部署者的初始口令一起发出去。
DEFAULT_PASSWORD = (os.environ.get("NAVPAGE_DEFAULT_PASSWORD") or "").strip()

# 允许写入的键白名单（防止前端塞垃圾）
DEFAULT_SETTINGS = {
    "site_title": "我的导航",
    "site_subtitle": "NAS 服务聚合面板",
    # 左上角图标：留空 = 用内置的 ◆ 字形；可以是上传后的名字（/api/logo/<name>）或外链 URL
    "site_logo": "",
    "wallpaper": "",
    "wallpaper_blur": 0,
    "wallpaper_dim": 25,
    "wallpaper_random": False,
    "card_theme": "glass",      # glass | solid | dark
    "card_size": "lg",          # lg=大（沿用原大卡）| xs=小（纯图标方形）
    "columns": 1,               # 每行分组数：1=每个分类独占一行（0=自适应流式打包）
    "show_desc": True,
    "host_lan_ip": "",
    "host_wan": "",
    "wan_scheme": "https",
    "lan_cidrs": DEFAULT_LAN_CIDRS,
    "trust_proxy": False,
    "auto_switch": True,
    "default_mode": "auto",     # auto | lan | wan
    "wan_hosts": "",            # 命中这些主机名/域名即视为外网访问，逗号分隔，支持 *.x.com
    "domain_means_wan": True,   # 用域名访问（且没配进 wan_hosts）时也算外网
    "frp_header_name": "",      # frp requestHeaders.set 注入的头名，如 X-Navpage-Net
    "frp_header_value": "",     # 该头的期望值，留空表示只看头是否存在
    "widgets_enabled": True,    # 卡片上显示服务状态小组件
    "widget_interval": 30,      # 前端刷新间隔（秒）
    "widget_style": "rich",     # rich = homepage 风格（多指标+进度条）| plain = 单行文字
    "show_url": True,           # 卡片上是否显示服务地址
    "status_dot": True,         # 容器卡片图标右下角的状态灯：绿=正常/黄=延迟高/红=访问不了
    "status_slow_ms": 1000,     # 探测延迟超过该毫秒数算「延迟高」，显示黄点
    "allow_image_update": False,  # 是否允许从面板拉取并重建容器（需 socket-proxy 放开 POST）
    "image_check_cache": 3600,    # 镜像更新检查缓存秒数（打外网 registry）；2026-10-18 用户要求默认 1 小时
    # 定时自动检查 / 自动更新（2026-10-16 用户要求）。
    # image_auto_cron 是 5 段 cron（分 时 日 月 周），只认这一种写法，解析见 app/cron.py。
    # image_auto_names 里列的是**容器名**：只有这些容器会在检查到更新时被自动更新。
    "image_auto_check": False,
    "image_auto_cron": "0 3 * * *",
    "image_auto_update": False,
    "image_auto_names": [],
    "image_proxy": "",            # 检查镜像更新走的外网代理，如 http://user:pass@192.168.1.10:7893
    # 检查「是否最新」用的国内镜像站（只对 Docker Hub 镜像生效）。为什么需要它：
    # Docker Hub 按 IP 对匿名请求限流，面板一次要查几十个镜像，很容易整片 HTTP 429，
    # 表现就是「一直显示不出来更新」。走镜像站直连、不占配额、通常 1~3 秒返回。
    # 例：https://docker.1panel.live（2026-10 实测摘要与上游一致）
    "image_mirror": "",
    # 定时清理未使用镜像（2026-10-16 用户要求）。DELETE 不需要改 socket-proxy：
    # 代理规则是「deny unless METH_GET || env(POST)」，POST=1 已经放行所有非 GET 方法。
    #   image_prune_mode: dangling = 只清无标签的悬空镜像（最安全）
    #                     unused   = 清掉所有没被任何容器（含已停止）引用的镜像
    #   image_prune_keep: 永不清理的镜像，写 repo:tag 或镜像 ID 前缀，支持 * 通配
    "image_auto_prune": False,
    "image_auto_prune_cron": "0 3 * * 0",
    "image_prune_mode": "dangling",
    "image_prune_keep": [],
    # 在线图标库：可配多个 GitHub 公开图标仓库（仓库地址 / 分支 / 备注名 / 子目录）。
    # 首项是内置默认库，与 app/iconlib.py 的 DEFAULT_LIB 保持一致。
    # 增删改走 /api/admin/iconlibraries，别的手改这个键。
    "icon_libs": [{"repo": "homarr-labs/dashboard-icons", "branch": "main",
                   "label": "Dashboard Icons", "sub": ""}],
    "require_login": True,
    "admin_password_hash": "",
    "secret": "",
    "search_engines": DEFAULT_ENGINES,
    "search_engine_default": 0,
    "scan_exclude": "",
    # 无端口映射的容器（host 网络 / 纯内网服务）默认也建卡片，地址留空由用户手填。
    # 置 true 则回到「跳过它们」的旧行为。注意：前端 saveSettings() 不带参数时会提交
    # 整个设置对象，所以这个键在存量实例里可能已被写进 DB，改默认值对它们无效。
    "scan_hide_no_port": False,
    "footer_text": "",
    # ---------------- 反代面板联动（Gately）----------------
    # gately_url 例：http://192.168.1.109:16680；gately_token 在面板「API 令牌」页建，
    # 只需 webservice:read + webservice:write（读规则、建规则）。
    # gately_domain 是根域名：给卡片生成反代时拼成 <子域名>.<根域名>。
    # gately_auto：容器带标签 nav.proxy.domain=<子域名> 时，扫描导入/同步自动建反代并写外网地址。
    "gately_url": "",
    "gately_token": "",
    "gately_domain": "",
    "gately_listener_port": 443,
    "gately_tls": True,
    "gately_auto": False,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  icon TEXT DEFAULT '',
  sort INTEGER DEFAULT 0,
  created_at INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  url_lan TEXT DEFAULT '',
  url_wan TEXT DEFAULT '',
  icon TEXT DEFAULT '',
  category_id INTEGER,
  sort INTEGER DEFAULT 0,
  hidden INTEGER DEFAULT 0,
  source TEXT DEFAULT 'manual',
  container_name TEXT DEFAULT '',
  container_image TEXT DEFAULT '',
  container_port INTEGER DEFAULT 0,
  docker_host TEXT DEFAULT '',
  missing INTEGER DEFAULT 0,
  widget TEXT DEFAULT '',
  created_at INTEGER DEFAULT 0,
  updated_at INTEGER DEFAULT 0
);
-- 唯一索引 idx_items_container_host 在 _migrate() 里建：
-- 老库必须先补 docker_host 列，否则 executescript 直接报 no such column。
CREATE TABLE IF NOT EXISTS docker_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT DEFAULT 'agent',
  prefix TEXT DEFAULT '',
  token_hash TEXT DEFAULT '',
  token_hint TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1,
  auto_import INTEGER DEFAULT 1,
  containers TEXT DEFAULT '[]',
  host_name TEXT DEFAULT '',
  last_seen INTEGER DEFAULT 0,
  last_error TEXT DEFAULT '',
  created_at INTEGER DEFAULT 0,
  updated_at INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

_lock = threading.RLock()
_conn = None


def conn():
    global _conn
    with _lock:
        if _conn is None:
            for d in (DATA_DIR, WALLPAPER_DIR, ICON_DIR, LOGO_DIR):
                os.makedirs(d, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=5000")
        return _conn


def init():
    c = conn()
    with _lock:
        c.executescript(SCHEMA)
        c.commit()
        _migrate(c)
    s = get_settings()
    if not s.get("secret"):
        update_settings({"secret": secrets.token_hex(32)})
    if not s.get("admin_password_hash"):
        # 首次部署：写入初始密码，同时要求登录
        pw = DEFAULT_PASSWORD or secrets.token_urlsafe(12)
        update_settings({"admin_password_hash": _hash_password(pw),
                         "require_login": True})
        if DEFAULT_PASSWORD:
            print("[navpage] 首次启动：管理员密码取自 NAVPAGE_DEFAULT_PASSWORD。", flush=True)
        else:
            print("[navpage] 首次启动：未设置 NAVPAGE_DEFAULT_PASSWORD，"
                  f"已随机生成管理员密码：{pw}", flush=True)
            print("[navpage] 请登录后在「网络与安全」页签修改，"
                  "或用 NAVPAGE_DEFAULT_PASSWORD 指定初始密码。", flush=True)


def detect_host_ip():
    """容器内拿不到宿主机 LAN IP，这里只是「本容器出口地址」，仅供排查显示。

    真正的内网地址靠请求 Host 头 + {host} 占位符展开，见 netutil.host_context。
    """
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- settings

def get_settings():
    c = conn()
    with _lock:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    out = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except Exception:
            out[r["key"]] = r["value"]
    # 尺寸模式重构（2026-10-16）：旧的 sm/md 已废弃（只剩 lg 大 / xs 小），统一迁移到 lg
    if out.get("card_size") not in ("lg", "xs"):
        out["card_size"] = "lg"
    return out


def update_settings(patch):
    c = conn()
    with _lock:
        for k, v in (patch or {}).items():
            if k not in DEFAULT_SETTINGS:
                continue
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v, ensure_ascii=False)),
            )
        c.commit()
    return get_settings()


def public_settings():
    s = get_settings()
    return {
        "site_title": s["site_title"],
        "site_subtitle": s["site_subtitle"],
        "site_logo": s["site_logo"],
        "wallpaper": s["wallpaper"],
        "wallpaper_blur": s["wallpaper_blur"],
        "wallpaper_dim": s["wallpaper_dim"],
        "wallpaper_random": s["wallpaper_random"],
        "card_theme": s["card_theme"],
        "card_size": s["card_size"],
        "columns": s["columns"],
        "show_desc": s["show_desc"],
        "auto_switch": s["auto_switch"],
        "default_mode": s["default_mode"],
        "widgets_enabled": s["widgets_enabled"],
        "widget_interval": s["widget_interval"],
        "widget_style": s["widget_style"],
        "show_url": s["show_url"],
        "status_dot": s["status_dot"],
        "status_slow_ms": s["status_slow_ms"],
        # image_proxy 含代理密码，只在 /api/admin/state 下发，不进公开设置
        "require_login": s["require_login"],
        "search_engines": s["search_engines"],
        "search_engine_default": s["search_engine_default"],
        "footer_text": s["footer_text"],
        "has_password": bool(s["admin_password_hash"]),
    }


# ---------------------------------------------------------------- categories

def list_categories():
    c = conn()
    with _lock:
        rows = c.execute(
            "SELECT * FROM categories ORDER BY sort, id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_or_create_category(name, icon=""):
    name = (name or "").strip()
    if not name:
        return None
    c = conn()
    with _lock:
        row = c.execute("SELECT * FROM categories WHERE name=?", (name,)).fetchone()
        if row:
            return row["id"]
        max_sort = c.execute("SELECT COALESCE(MAX(sort),0) m FROM categories").fetchone()["m"]
        cur = c.execute(
            "INSERT INTO categories(name, icon, sort, created_at) VALUES(?,?,?,?)",
            (name, icon, max_sort + 10, int(time.time())),
        )
        c.commit()
        return cur.lastrowid


def create_category(name, icon="", sort=None):
    c = conn()
    with _lock:
        if sort is None:
            sort = c.execute("SELECT COALESCE(MAX(sort),0) m FROM categories").fetchone()["m"] + 10
        cur = c.execute(
            "INSERT INTO categories(name, icon, sort, created_at) VALUES(?,?,?,?)",
            ((name or "").strip(), icon or "", int(sort), int(time.time())),
        )
        c.commit()
        return cur.lastrowid


def update_category(cid, patch):
    fields, vals = [], []
    for k in ("name", "icon", "sort", "card_size"):
        if k in patch and patch[k] is not None:
            fields.append(f"{k}=?")
            vals.append(patch[k])
    if not fields:
        return
    vals.append(cid)
    c = conn()
    with _lock:
        c.execute(f"UPDATE categories SET {', '.join(fields)} WHERE id=?", vals)
        c.commit()


def delete_category(cid):
    c = conn()
    with _lock:
        c.execute("UPDATE items SET category_id=NULL WHERE category_id=?", (cid,))
        c.execute("DELETE FROM categories WHERE id=?", (cid,))
        c.commit()


def delete_categories(cids):
    """批量删分类：**一个事务**里把其下条目的 category_id 置空再删分类。
    返回 (删掉的分类数, 受影响的条目数)。空列表直接返回 (0, 0)。"""
    ids = []
    for x in (cids or []):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not ids:
        return 0, 0
    ph = ",".join("?" * len(ids))
    c = conn()
    with _lock:
        n_items = c.execute(
            f"SELECT COUNT(*) n FROM items WHERE category_id IN ({ph})", ids
        ).fetchone()["n"]
        c.execute(f"UPDATE items SET category_id=NULL WHERE category_id IN ({ph})", ids)
        cur = c.execute(f"DELETE FROM categories WHERE id IN ({ph})", ids)
        c.commit()
        return cur.rowcount, n_items


# ---------------------------------------------------------------- items

ITEM_FIELDS = (
    "name", "description", "url_lan", "url_wan", "icon", "category_id",
    "sort", "hidden", "source", "container_name", "container_image",
    "container_port", "docker_host", "missing", "widget", "url_visible", "card_size",
    "auto_created",
)

# 后加的列：老库启动时自动补齐，避免用户手动迁移
MIGRATIONS = {
    "items": [("widget", "TEXT DEFAULT ''"), ("url_visible", "INTEGER DEFAULT -1"),
              ("card_size", "TEXT DEFAULT ''"), ("docker_host", "TEXT DEFAULT ''"),
              ("auto_created", "INTEGER DEFAULT 0")],
    "categories": [("card_size", "TEXT DEFAULT ''")],
}
# card_size: '' = 跟随上级（条目→分类→全局），lg = 大卡，xs = 小卡（纯图标方形）

# url_visible: -1 跟随全局设置，0 强制隐藏地址，1 强制显示
TRI_FIELDS = ("url_visible",)


def _enc_tri(v):
    """三态字段：None/'' 当 -1（跟随全局）。"""
    if v in ("", None):
        return -1
    try:
        return max(-1, min(1, int(v)))
    except Exception:
        return -1


def _migrate(c):
    for table, cols in MIGRATIONS.items():
        try:
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        except Exception:
            continue
        for name, decl in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # 存量数据修复：早期「只填端口」被存成了残缺值 ':9091'（缺 {scheme}://{host} 前缀），
    # 这里补全成模板，让老卡片在管理台里也显示正确的占位符地址。幂等，可反复执行。
    for col in ("url_lan", "url_wan"):
        c.execute(f"UPDATE items SET {col} = '{{scheme}}://{{host}}' || {col} "
                  f"WHERE {col} LIKE ':%'")
    # 2026-10-18：检查缓存默认值从 30 分钟(1800) 改成 1 小时(3600)。
    # 存量库里 1800 是**旧默认值**被设置页存下来的，不是用户特意选的，一起迁到 3600；
    # 之后用户如果想改回半小时，在设置里改完会写 1800 —— 但这行每次启动都会再迁一次，
    # 所以用标记位保证只迁一次。
    if not c.execute("SELECT 1 FROM settings WHERE key='migrated_cache_1h'").fetchone():
        c.execute("UPDATE settings SET value='3600' WHERE key='image_check_cache' AND value='1800'")
        c.execute("INSERT INTO settings(key, value) VALUES('migrated_cache_1h', '1')")
    # 2026-10-22：多采集源（本机 + 远端 agent）后，容器名会在不同主机重复，
    # 唯一索引必须从 container_name 单列换成 (docker_host, container_name)。
    c.execute("DROP INDEX IF EXISTS idx_items_container")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_container_host "
              "ON items(docker_host, container_name) WHERE container_name <> ''")
    c.commit()


def _row_to_item(r):
    d = dict(r)
    d["hidden"] = bool(d.get("hidden"))
    d["missing"] = bool(d.get("missing"))
    d["widget"] = _dec_widget(d.get("widget"))
    return d


def _enc_widget(v):
    """小组件配置统一存 JSON 字符串。没有 type 就当作未配置。"""
    if v is None:
        return ""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return ""
    if not isinstance(v, dict) or not v.get("type"):
        return ""
    return json.dumps(v, ensure_ascii=False)


def _dec_widget(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def list_items(include_hidden=True):
    c = conn()
    sql = "SELECT * FROM items"
    if not include_hidden:
        sql += " WHERE hidden=0"
    sql += " ORDER BY sort, id"
    with _lock:
        rows = c.execute(sql).fetchall()
    return [_row_to_item(r) for r in rows]


def get_item(iid):
    c = conn()
    with _lock:
        r = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    return _row_to_item(r) if r else None


def find_item_by_container(name, host=""):
    """按 (采集源, 容器名) 找卡片。host='' 是本机，远端采集源传 str(source_id)。

    精确匹配不到时，退一步按容器名找「已经解除绑定」的卡片（docker_host=''）。
    删采集源会把卡片解绑但保留 container_name —— 重扫/全量同步要是认不出来，
    就会给同一个容器再建一张重复卡片，或者在「新发现」里一直冒出来。
    """
    c = conn()
    with _lock:
        r = c.execute("SELECT * FROM items WHERE container_name=? AND COALESCE(docker_host,'')=?",
                      (name, str(host or ""))).fetchone()
        if not r:
            r = c.execute("SELECT * FROM items WHERE container_name=? AND COALESCE(docker_host,'')=''"
                          " ORDER BY (source='manual') DESC, id LIMIT 1", (name,)).fetchone()
    return _row_to_item(r) if r else None


def create_item(data):
    now = int(time.time())
    d = {k: data.get(k) for k in ITEM_FIELDS}
    d["name"] = (d.get("name") or "未命名").strip()
    d["description"] = d.get("description") or ""
    d["url_lan"] = netutil.normalize_url(d.get("url_lan"))
    d["url_wan"] = netutil.normalize_url(d.get("url_wan"))
    d["icon"] = d.get("icon") or ""
    d["source"] = d.get("source") or "manual"
    d["container_name"] = d.get("container_name") or ""
    d["container_image"] = d.get("container_image") or ""
    d["container_port"] = int(d.get("container_port") or 0)
    d["docker_host"] = str(d.get("docker_host") or "")
    d["auto_created"] = 1 if d.get("auto_created") else 0
    d["hidden"] = 1 if d.get("hidden") else 0
    d["missing"] = 1 if d.get("missing") else 0
    d["url_visible"] = _enc_tri(d.get("url_visible"))
    d["widget"] = _enc_widget(d.get("widget"))
    if d.get("category_id") in ("", None):
        d["category_id"] = None
    if d.get("sort") in ("", None):
        c = conn()
        with _lock:
            d["sort"] = c.execute("SELECT COALESCE(MAX(sort),0) m FROM items").fetchone()["m"] + 10
    c = conn()
    cols = list(ITEM_FIELDS)
    with _lock:
        cur = c.execute(
            f"INSERT INTO items({', '.join(cols)}, created_at, updated_at) "
            f"VALUES({', '.join('?' * len(cols))}, ?, ?)",
            [d[k] for k in cols] + [now, now],
        )
        c.commit()
        return cur.lastrowid


def update_item(iid, patch):
    fields, vals = [], []
    for k in ITEM_FIELDS:
        if k in patch:
            v = patch[k]
            if k == "category_id" and v in ("", None):
                v = None
            if k in ("hidden", "missing", "auto_created"):
                v = 1 if v else 0
            if k == "container_port":
                v = int(v or 0)
            if k in ("url_lan", "url_wan"):
                v = netutil.normalize_url(v)
            if k == "widget":
                v = _enc_widget(v)
            if k in TRI_FIELDS:
                v = _enc_tri(v)
            fields.append(f"{k}=?")
            vals.append(v)
    if not fields:
        return
    fields.append("updated_at=?")
    vals.append(int(time.time()))
    vals.append(iid)
    c = conn()
    with _lock:
        c.execute(f"UPDATE items SET {', '.join(fields)} WHERE id=?", vals)
        c.commit()


def delete_item(iid):
    c = conn()
    with _lock:
        c.execute("DELETE FROM items WHERE id=?", (iid,))
        c.commit()


def reorder_items(order):
    """order: [[id, sort], ...]"""
    c = conn()
    with _lock:
        for iid, sort in order:
            c.execute("UPDATE items SET sort=? WHERE id=?", (int(sort), int(iid)))
        c.commit()


def apply_layout(rows):
    """拖拽布局落库。rows: [{"id": 1, "category_id": 2, "sort": 10}, ...]"""
    c = conn()
    with _lock:
        for r in rows:
            try:
                iid = int(r.get("id"))
            except Exception:
                continue
            cid = r.get("category_id")
            cid = int(cid) if cid not in (None, "", "null") else None
            sort = r.get("sort")
            sort = int(sort) if sort not in (None, "") else 0
            c.execute("UPDATE items SET category_id=?, sort=?, updated_at=? WHERE id=?",
                      (cid, sort, int(__import__("time").time()), iid))
        c.commit()


def reorder_categories(order):
    c = conn()
    with _lock:
        for cid, sort in order:
            c.execute("UPDATE categories SET sort=? WHERE id=?", (int(sort), int(cid)))
        c.commit()


def mark_missing(container_names, host=""):
    """把该采集源里不在 container_names 的 docker 卡片标记 missing=1。

    host='' 是本机；远端采集源传 str(source_id)，只影响该来源的卡片，
    不会把别的主机上同名容器误标成「已消失」。
    """
    names = set(container_names)
    c = conn()
    with _lock:
        rows = c.execute(
            "SELECT id, container_name FROM items WHERE source='docker' AND container_name<>'' "
            "AND COALESCE(docker_host,'')=?", (str(host or ""),)
        ).fetchall()
        changed = 0
        for r in rows:
            want = 0 if r["container_name"] in names else 1
            if want:
                changed += 1
            c.execute("UPDATE items SET missing=? WHERE id=?", (want, r["id"]))
        c.commit()
        return changed


# ---------------------------------------------------------------- auth

def _hash_password(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000).hex()
    return f"{salt}${h}"


def set_password(pw):
    update_settings({"admin_password_hash": _hash_password(pw)})


def check_password(pw):
    stored = get_settings().get("admin_password_hash") or ""
    if not stored or "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(_hash_password(pw, salt), stored)


def make_token(days=30):
    secret = get_settings().get("secret") or ""
    exp = int(time.time()) + days * 86400
    sig = hmac.new(secret.encode(), str(exp).encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{exp}.{sig}".encode()).decode()


def verify_token(token):
    secret = get_settings().get("secret") or ""
    if not token or not secret:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        exp_s, sig = raw.split(".", 1)
        exp = int(exp_s)
    except Exception:
        return False
    if exp < time.time():
        return False
    good = hmac.new(secret.encode(), exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)

# ---------------------------------------------------------------- 采集源
# 本机（Docker socket）是隐式的，不在表里：items.docker_host='' 就代表本机。
# 这里只存远端 agent 采集源。

SOURCE_FIELDS = ("name", "kind", "prefix", "enabled", "auto_import")


def list_sources(only_enabled=False):
    c = conn()
    with _lock:
        sql = "SELECT * FROM docker_sources"
        if only_enabled:
            sql += " WHERE enabled=1"
        rows = c.execute(sql + " ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        d["auto_import"] = bool(d.get("auto_import"))
        d["containers"] = json.loads(d.get("containers") or "[]")
        d["token"] = ""  # 明文令牌只在校验时用，永不回传
        out.append(d)
    return out


def get_source(sid):
    for x in list_sources():
        if str(x["id"]) == str(sid):
            return x
    return None


def create_source(name, prefix="", auto_import=True, kind="agent"):
    now = int(time.time())
    c = conn()
    with _lock:
        cur = c.execute(
            "INSERT INTO docker_sources(name, kind, prefix, enabled, auto_import, containers,"
            " created_at, updated_at) VALUES(?,?,?,1,?,'[]',?,?)",
            (name, kind, prefix or "", 1 if auto_import else 0, now, now))
        c.commit()
        return cur.lastrowid


def update_source(sid, patch):
    allow = {"name", "prefix", "enabled", "auto_import"}
    sets, vals = [], []
    for k, v in (patch or {}).items():
        if k not in allow:
            continue
        if k in ("enabled", "auto_import"):
            v = 1 if v else 0
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.extend([int(time.time()), sid])
    c = conn()
    with _lock:
        c.execute(f"UPDATE docker_sources SET {', '.join(sets)} WHERE id=?", vals)
        c.commit()


def delete_source(sid, keep_manual=False):
    """删源并清理它的卡片。

    keep_manual=False（默认）：该源自动建的卡片直接删掉（源没了，卡片就是垃圾）；
      用户自己建、后来被「认领」的卡片保留，但解除绑定（docker_host/container_name
      一起清空，否则占着 (docker_host, container_name) 唯一索引，同名卡片以后
      再也建不出来）。
    keep_manual=True：只解除绑定，一张都不删。

    返回 {"removed": 删掉的卡片数, "released": 保留并解除绑定的卡片数}。
    """
    sid = str(sid)
    removed = released = 0
    c = conn()
    with _lock:
        rows = c.execute(
            "SELECT id, auto_created FROM items WHERE COALESCE(docker_host,'')=?",
            (sid,)).fetchall()
        for r in rows:
            if r["auto_created"] and not keep_manual:
                c.execute("DELETE FROM items WHERE id=?", (r["id"],))
                removed += 1
            else:
                # 只清 docker_host，container_name 必须留着：扫描/同步靠它认出
                # 「这个容器已经有卡片了」，清掉的话重扫会把它当成新发现再建一张。
                c.execute("UPDATE items SET source='manual', missing=0, docker_host=''"
                          " WHERE id=?", (r["id"],))
                released += 1
        c.execute("DELETE FROM docker_sources WHERE id=?", (sid,))
        c.commit()
    return {"removed": removed, "released": released}


def set_source_token(sid, token_hash, hint=""):
    c = conn()
    with _lock:
        c.execute("UPDATE docker_sources SET token_hash=?, token_hint=?, updated_at=? WHERE id=?",
                  (token_hash, hint, int(time.time()), sid))
        c.commit()


def source_by_token_hash(h):
    c = conn()
    with _lock:
        r = c.execute("SELECT * FROM docker_sources WHERE token_hash=? AND enabled=1",
                      (h,)).fetchone()
    return dict(r) if r else None


def record_inventory(sid, containers, host_name=""):
    c = conn()
    with _lock:
        c.execute("UPDATE docker_sources SET containers=?, host_name=?, last_seen=?, last_error='',"
                  " updated_at=? WHERE id=?",
                  (json.dumps(containers, ensure_ascii=False), host_name or "",
                   int(time.time()), int(time.time()), sid))
        c.commit()


def set_source_error(sid, err):
    c = conn()
    with _lock:
        c.execute("UPDATE docker_sources SET last_error=? WHERE id=?", ((err or "")[:300], sid))
        c.commit()
