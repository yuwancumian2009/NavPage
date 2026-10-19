"""Compose 项目管理：扫描 compose 文件、读写 YAML、用 compose CLI 重构。

为什么要调 CLI 而不是裸 Docker API：compose 的多服务依赖、网络、命名卷、
build 上下文没法用 API 等价复现。CLI 走的是 DOCKER_HOST（socket-proxy），
面板本身不用挂 docker.sock；但代理里必须放开 SESSION 和 VOLUMES，
否则 compose v5 启动时开的 BuildKit session 会被 403 挡掉（实测）。

路径约定（很重要）：compose 目录必须以**宿主机同样的绝对路径**挂进容器
（/vol1/1000/docker:/vol1/1000/docker）。否则 YAML 里的相对路径 ./data
会被解析成容器内路径，daemon 会照着建出错误的挂载点。
"""
import os
import re
import shutil
import subprocess
import time

from . import dockerapi

COMPOSE_ROOT = os.environ.get("NAVPAGE_COMPOSE_ROOT", "/vol1/1000/docker")
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
MAX_DEPTH = 3
RUN_TIMEOUT = 600
BACKUP_KEEP = 10

try:
    import yaml
except Exception:            # 镜像里没装 PyYAML 也要能启动，只是解析退化成正则
    yaml = None


# ------------------------------------------------------------------ 扫描

def _walk(root, depth=0):
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except Exception:
        return
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                if depth + 1 > MAX_DEPTH:
                    continue
                if e.name.startswith(".") or e.name in ("data", "node_modules"):
                    continue
                yield from _walk(e.path, depth + 1)
            elif e.is_file() and e.name in COMPOSE_NAMES:
                yield e.path
        except Exception:
            continue


def _load_yaml(path):
    """读 compose 文件 -> dict。解析失败返回 {}（列表页不该因为一个坏文件整页挂掉）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except Exception:
        return {}, ""
    if yaml is None:
        # 退路：只抓 services 下一层的键名
        names = re.findall(r"^  ([A-Za-z0-9_.-]+):\s*$", txt, re.M)
        return {"services": {n: {} for n in names}}, txt
    try:
        doc = yaml.safe_load(txt) or {}
        return (doc if isinstance(doc, dict) else {}), txt
    except Exception:
        return {}, txt


def _containers_by_project():
    """{project: [{name, state, service, config_files}]}"""
    out = {}
    try:
        cs = dockerapi.containers(all_containers=True)
    except Exception:
        return out
    for c in cs:
        proj = c.get("compose_project") or ""
        if not proj:
            continue
        out.setdefault(proj, []).append({
            "name": c.get("name"),
            "state": c.get("state"),
            "service": c.get("compose_service") or "",
            "config_files": (c.get("labels") or {}).get("com.docker.compose.project.config_files") or "",
            "id": c.get("id") or "",
        })
    return out


def _clean_project(name):
    n = re.sub(r"[^a-z0-9_-]+", "", (name or "").lower())
    return n.strip("-_") or "default"


def _project_for(path, doc, by_proj):
    """项目名优先级：YAML 里的 name: > 容器标签反查 > 目录名。"""
    explicit = (doc or {}).get("name")
    if isinstance(explicit, str) and explicit.strip():
        return _clean_project(explicit)
    real = os.path.realpath(path)
    for proj, cs in by_proj.items():
        for c in cs:
            files = [f.strip() for f in (c.get("config_files") or "").split(",") if f.strip()]
            if any(os.path.realpath(f) == real for f in files):
                return proj
    return _clean_project(os.path.basename(os.path.dirname(path)))


def projects():
    """列出全部 compose 项目。"""
    by_proj = _containers_by_project()
    out = []
    for path in _walk(COMPOSE_ROOT):
        doc, _txt = _load_yaml(path)
        svcs = (doc.get("services") or {}) if isinstance(doc, dict) else {}
        if not isinstance(svcs, dict):
            svcs = {}
        proj = _project_for(path, doc, by_proj)
        cs = by_proj.get(proj) or []
        try:
            st = os.stat(path)
            mtime, size = int(st.st_mtime), st.st_size
        except Exception:
            mtime, size = 0, 0
        out.append({
            "path": path,
            "dir": os.path.dirname(path),
            "project": proj,
            "services": [s for s in svcs.keys()],
            "images": sorted({(v or {}).get("image") for v in svcs.values() if (v or {}).get("image")}),
            "containers": [{"name": c["name"], "state": c["state"], "service": c["service"]} for c in cs],
            "running": sum(1 for c in cs if c["state"] == "running"),
            "mtime": mtime,
            "size": size,
            "parse_error": bool(_txt) and not svcs,
        })
    out.sort(key=lambda r: (r["project"] or "~"))
    return out


# ------------------------------------------------------------------ 路径安全

def safe_path(path):
    """只允许 COMPOSE_ROOT 下的 compose 文件，挡掉 ../ 穿越和任意文件读写。"""
    p = (path or "").strip()
    if not p:
        raise ValueError("路径为空")
    real = os.path.realpath(p)
    root = os.path.realpath(COMPOSE_ROOT)
    if not (real == root or real.startswith(root + os.sep)):
        raise ValueError("路径不在 compose 目录内")
    if os.path.basename(real) not in COMPOSE_NAMES:
        raise ValueError("不是 compose 文件")
    return real


def read_file(path):
    real = safe_path(path)
    with open(real, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    doc, _ = _load_yaml(real)
    by_proj = _containers_by_project()
    proj = _project_for(real, doc, by_proj)
    return {
        "path": real,
        "project": proj,
        "content": content,
        "mtime": int(os.path.getmtime(real)),
        "services": list(((doc.get("services") or {}) or {}).keys()),
        "containers": [{"name": c["name"], "state": c["state"]} for c in (by_proj.get(proj) or [])],
    }


# ------------------------------------------------------------------ 执行

def _run(cmd, timeout=RUN_TIMEOUT):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, out[-6000:]
    except subprocess.TimeoutExpired:
        return False, f"执行超时（{timeout}s）"
    except FileNotFoundError:
        return False, "容器里没有 docker CLI（镜像需要带 compose 插件）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def validate(path, content):
    """用 `docker compose config -q` 校验内容。临时文件放在同目录，保证相对路径解析一致。"""
    real = safe_path(path)
    tmp = os.path.join(os.path.dirname(real), ".navpage-check-" + str(int(time.time() * 1000)) + ".yml")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content or "")
        ok, out = _run(["docker", "compose", "-f", tmp, "config", "-q"], timeout=60)
        return ok, out
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _backup(real):
    bak = real + ".bak-" + time.strftime("%Y%m%d%H%M%S")
    try:
        shutil.copy2(real, bak)
    except Exception:
        return ""
    # 只留最近 N 份，别把目录塞满
    try:
        d = os.path.dirname(real)
        base = os.path.basename(real) + ".bak-"
        olds = sorted([f for f in os.listdir(d) if f.startswith(base)])
        for f in olds[:-BACKUP_KEEP]:
            os.remove(os.path.join(d, f))
    except Exception:
        pass
    return bak


def _is_self(project):
    """该项目里有没有「面板自己」这个容器 —— 有的话 up 会把面板重启，请求会被掐断。"""
    me = (os.environ.get("HOSTNAME") or "")[:12]
    if not me:
        return False
    try:
        for c in dockerapi.containers(all_containers=True):
            if (c.get("compose_project") or "") != project:
                continue
            cid = (c.get("id") or "")[:12]
            if cid and (cid == me or me.startswith(cid) or cid.startswith(me)):
                return True
    except Exception:
        pass
    return False


def up(path, pull=False):
    """重构：docker compose up -d。返回 (ok, 输出, detached)。"""
    real = safe_path(path)
    doc, _ = _load_yaml(real)
    proj = _project_for(real, doc, _containers_by_project())
    cmd = ["docker", "compose", "-f", real, "-p", proj, "up", "-d"]
    if pull:
        cmd += ["--pull", "always"]
    if _is_self(proj):
        # 面板自己：先返回再执行，否则容器一停请求就断，前端只能看到「失败」
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True, "已提交，面板容器正在重建，几秒后自动刷新", True
        except Exception as e:
            return False, f"{type(e).__name__}: {e}", False
    ok, out = _run(cmd)
    return ok, out, False


def save(path, content, restart=False, pull=False):
    """保存 YAML：校验 -> 备份 -> 原子写 -> （可选）重构。"""
    real = safe_path(path)
    ok, msg = validate(real, content)
    if not ok:
        return {"ok": False, "stage": "validate", "message": msg}
    bak = _backup(real)
    tmp = real + ".navpage-tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content or "")
        os.replace(tmp, real)
    except Exception as e:
        return {"ok": False, "stage": "write", "message": f"{type(e).__name__}: {e}"}
    res = {"ok": True, "stage": "save", "backup": bak, "message": "已保存"}
    if restart:
        up_ok, out, detached = up(real, pull=pull)
        res.update({"ok": up_ok, "stage": "up", "message": out, "detached": detached})
    return res
