"""全量备份与恢复（2.2.0）

一个 zip = 数据库 5 张表 + /data 下所有「配置资产」文件（图标、壁纸、logo、网关证书 / frpc 配置）。

* 备份：`dump()` 把表按列导出成 JSON，`build_zip()` 连文件一起打包；
  图标既是卡片 icon 字段引用的文件名，也是磁盘上的真实文件，所以必须一起带走，
  否则恢复后满屏都是「找不到图标」。
* 恢复：整体替换。先自动给当前状态拍一份快照（失败可回滚），再清表重建。
  管理员密码 / 会话密钥默认**保留当前值**，避免恢复完把自己锁在门外。
"""
import json
import os
import re
import shutil
import time
import zipfile

from . import db

MANIFEST = "manifest.json"
FORMAT = "navpage-backup/1"
BACKUP_DIR = os.path.join(db.DATA_DIR, "backups")
TABLES = ["categories", "items", "docker_sources", "settings", "gw_certs"]
# 打进包里的数据目录（相对 /data）
FILE_DIRS = ["icons", "wallpapers", "logo", "gw"]
SKIP_EXT = (".log",)
# 恢复时保留当前值、不从备份覆盖的设置项（否则恢复完就登不进去了）
KEEP_SETTINGS = ("admin_password_hash", "secret")
MAX_KEEP = 10


def _now():
    return int(time.time())


def _ts_name(prefix="navpage-backup"):
    return f"{prefix}_{time.strftime('%Y%m%d-%H%M%S')}.zip"


def _table_rows(c, table):
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    if not cols:
        return [], []
    rows = [dict(zip(cols, tuple(r))) for r in c.execute(f"SELECT * FROM {table}").fetchall()]
    return cols, rows


def _iter_files():
    """列出要备份的文件（相对 /data 的路径）。日志和缩略图缓存不备份。"""
    out = []
    for d in FILE_DIRS:
        base = os.path.join(db.DATA_DIR, d)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if x not in ("__pycache__",)]
            for f in files:
                if f.endswith(SKIP_EXT):
                    continue
                full = os.path.join(root, f)
                if not os.path.isfile(full):
                    continue
                out.append(os.path.relpath(full, db.DATA_DIR))
    return out


def dump():
    """当前状态快照（只有数据，没有二进制）。"""
    c = db.conn()
    tables, cols = {}, {}
    for t in TABLES:
        cs, rows = _table_rows(c, t)
        cols[t] = cs
        tables[t] = rows
    files = []
    for rel in _iter_files():
        full = os.path.join(db.DATA_DIR, rel)
        try:
            files.append({"path": rel, "size": os.path.getsize(full)})
        except OSError:
            continue
    from . import version
    return {
        "format": FORMAT,
        "app_version": getattr(version, "VERSION", ""),
        "created_at": _now(),
        "created_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {k: len(v) for k, v in tables.items()},
        "columns": cols,
        "tables": tables,
        "files": files,
    }


def build_zip(dest):
    """打包成 zip，返回统计信息。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    man = dump()
    tmp = dest + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr(MANIFEST, json.dumps(man, ensure_ascii=False, indent=1))
        for it in man["files"]:
            full = os.path.join(db.DATA_DIR, it["path"])
            try:
                z.write(full, "files/" + it["path"])
            except OSError:
                continue
    os.replace(tmp, dest)
    return {"path": dest, "size": os.path.getsize(dest), "counts": man["counts"],
            "files": len(man["files"])}


def snapshot(kind="manual", prefix=None):
    """在服务器上留一份快照（/data/backups）。kind 只用于命名，便于区分。

    空实例不自动备份：新建容器首启时库是空的，这时拍快照只会往备份目录塞一个
    0KB 垃圾文件，还把真实的旧备份挤出保留名额。
    """
    if kind == "auto" and not prefix:
        try:
            with db.conn() as c:
                if not c.execute("SELECT 1 FROM items LIMIT 1").fetchone():
                    return {"skipped": True, "reason": "空实例，跳过自动备份"}
        except Exception:
            pass
    pfx = prefix or f"navpage-{kind}"
    dest = os.path.join(BACKUP_DIR, _ts_name(pfx))
    info = build_zip(dest)
    info["name"] = os.path.basename(dest)
    info["kind"] = kind
    prune()
    return info


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not f.endswith(".zip"):
            continue
        p = os.path.join(BACKUP_DIR, f)
        if not os.path.isfile(p):
            continue
        kind = "manual"
        if f.startswith("navpage-auto"):
            kind = "auto"
        elif f.startswith("navpage-prerestore"):
            kind = "prerestore"
        out.append({"name": f, "size": os.path.getsize(p), "mtime": int(os.path.getmtime(p)),
                    "kind": kind})
    return out


def prune(keep=MAX_KEEP):
    """按类型各自只留最近 keep 份，避免自动备份把盘吃满。"""
    groups = {}
    for b in list_backups():
        groups.setdefault(b["kind"], []).append(b)
    removed = []
    for kind, arr in groups.items():
        arr.sort(key=lambda x: x["mtime"], reverse=True)
        for old in arr[keep:]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old["name"]))
                removed.append(old["name"])
            except OSError:
                pass
    return removed


def read_manifest(path):
    with zipfile.ZipFile(path) as z:
        try:
            raw = z.read(MANIFEST)
        except KeyError:
            raise ValueError("不是导航页的备份包：缺少 manifest.json")
    man = json.loads(raw.decode("utf-8"))
    if not str(man.get("format", "")).startswith("navpage-backup/"):
        raise ValueError("备份包格式不认识：" + str(man.get("format")))
    return man


def _apply_tables(c, man, keep_settings=KEEP_SETTINGS):
    """清空后按 id 原样写回：id 必须保留，否则 category_id 会指错分类。"""
    cur = db.get_settings() if keep_settings else {}
    stat = {}
    for t in TABLES:
        rows = man.get("tables", {}).get(t) or []
        cols_now = [r[1] for r in c.execute(f"PRAGMA table_info({t})").fetchall()]
        if not cols_now:
            continue
        c.execute(f"DELETE FROM {t}")
        n = 0
        for row in rows:
            use = {k: v for k, v in row.items() if k in cols_now}
            if t == "settings" and keep_settings:
                k = use.get("key")
                if k in keep_settings and cur.get(k) not in (None, ""):
                    use["value"] = json.dumps(cur[k], ensure_ascii=False)
            if not use:
                continue
            keys = list(use.keys())
            c.execute(
                f"INSERT INTO {t} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                [use[k] for k in keys],
            )
            n += 1
        stat[t] = n
        # AUTOINCREMENT 序列跟着最大 id 走，避免恢复后新增撞 id
        try:
            mx = c.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {t}").fetchone()[0]
            c.execute("DELETE FROM sqlite_sequence WHERE name=?", (t,))
            if t != "settings" and mx:
                c.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?,?)", (t, mx))
        except Exception:
            pass
    return stat


def _replace_dir(base):
    """把 base 清空（保留目录本身）。"""
    if not os.path.isdir(base):
        os.makedirs(base, exist_ok=True)
        return
    for name in os.listdir(base):
        p = os.path.join(base, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass


def restore_zip(path, restore_password=False):
    """整体恢复到备份状态。返回统计。恢复前自动给当前状态拍快照。"""
    man = read_manifest(path)
    pre = None
    try:
        s = db.get_settings()
        if db.list_items(include_hidden=True) or s.get("site_title"):
            pre = snapshot(kind="prerestore")
    except Exception:
        pre = None

    keep = () if restore_password else KEEP_SETTINGS
    with zipfile.ZipFile(path) as z:
        c = db.conn()
        with db._lock:
            try:
                stat = _apply_tables(c, man, keep_settings=keep)
                c.commit()
            except Exception:
                c.rollback()
                raise
        # 文件：先清空再铺开（只碰备份里带的目录）
        names = z.namelist()
        for d in FILE_DIRS:
            if not any(n.startswith("files/" + d + "/") for n in names):
                continue
            base = os.path.join(db.DATA_DIR, d)
            _replace_dir(base)
            for n in names:
                if not n.startswith("files/" + d + "/") or n.endswith("/"):
                    continue
                rel = n[len("files/"):]
                # 防 zip slip：拼出来的路径必须还在 /data 里
                dest = os.path.normpath(os.path.join(db.DATA_DIR, rel))
                if not dest.startswith(os.path.normpath(db.DATA_DIR) + os.sep):
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(n) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    clear_caches()
    return {"ok": True, "counts": stat, "from_version": man.get("app_version", ""),
            "created_text": man.get("created_text", ""),
            "pre_restore": (pre or {}).get("name", "")}


def clear_caches():
    """恢复后内存里全是旧数据的缓存，必须清掉，否则页面还是旧的。"""
    import importlib
    for mod, attrs in (("main", ["_STATUS_CACHE", "_img_cache"]),
                       ("widgets", ["_cache"]),
                       ("iconlib", ["_cache", "_tree_cache", "_libs_cache"]),
                       ("gw.certs", ["_cache"]),
                       ("icons", ["_thumb_memo"])):
        try:
            m = importlib.import_module("app." + mod)
        except Exception:
            continue
        for a in attrs:
            v = getattr(m, a, None)
            if isinstance(v, dict):
                try:
                    v.clear()
                except Exception:
                    pass
            elif isinstance(v, list):
                try:
                    del v[:]
                except Exception:
                    pass
    # 缩略图按原图 mtime 生成，图标被整体替换后一律重建
    try:
        shutil.rmtree(os.path.join(db.DATA_DIR, "iconthumbs"), ignore_errors=True)
    except Exception:
        pass
