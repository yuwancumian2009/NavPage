"""Docker API 客户端。

DOCKER_HOST 支持：
  unix:///var/run/docker.sock   （直接挂载，不推荐）
  tcp://navpage-socket-proxy:2375 （socket-proxy，推荐）
只用到只读端点：/version /containers/json /networks
"""
import json
import os
import threading
import time

import httpx

DOCKER_HOST = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
TIMEOUT = float(os.environ.get("NAVPAGE_DOCKER_TIMEOUT", "10"))

# 常见 Web 端口的优先顺序（私有端口侧判断）
WEB_PRIVATE = {80, 443, 3000, 5000, 5001, 8000, 8001, 8080, 8081, 8088, 8090,
               8096, 8443, 8888, 9000, 9090, 25600, 32400}


def _client():
    if DOCKER_HOST.startswith("unix://"):
        path = DOCKER_HOST[len("unix://"):]
        transport = httpx.HTTPTransport(uds=path)
        return httpx.Client(transport=transport, base_url="http://docker", timeout=TIMEOUT)
    if DOCKER_HOST.startswith("tcp://"):
        return httpx.Client(base_url="http://" + DOCKER_HOST[len("tcp://"):], timeout=TIMEOUT)
    if DOCKER_HOST.startswith("http"):
        return httpx.Client(base_url=DOCKER_HOST, timeout=TIMEOUT)
    raise RuntimeError(f"无法识别的 DOCKER_HOST: {DOCKER_HOST}")


def _get(path):
    with _client() as c:
        r = c.get(path)
        r.raise_for_status()
        return r.json()


def available():
    """返回 (ok, detail)"""
    try:
        v = _get("/version")
        return True, {
            "host": DOCKER_HOST,
            "version": v.get("Version"),
            "api_version": v.get("ApiVersion"),
        }
    except Exception as e:
        return False, {"host": DOCKER_HOST, "error": f"{type(e).__name__}: {e}"}


def _post(path, params=None, json_body=None, timeout=None):
    with _client() as c:
        r = c.post(path, params=params, json=json_body,
                   timeout=timeout or TIMEOUT)
        return r


def _delete(path, params=None):
    with _client() as c:
        return c.delete(path, params=params)


# ------------------------------------------------------------------ 镜像更新

def image_inspect(image):
    """GET /images/{name}/json -> {Id, RepoDigests, Created}"""
    return _get(f"/images/{image}/json")


def image_index():
    """一次 /images/json 同时建两个索引：(按 tag, 按镜像 ID)。

    返回 ({tag: info}, {镜像完整ID: info})。

    by_id 里包含**没有 tag 的镜像** —— 容器跑的可能就是这种：tag 已经被别的容器
    或一次 pull 挪到新镜像上了，只按 tag 比对会把「还在跑旧镜像」的容器误判成
    「最新」或「未知」，用户勾了自动更新也永远等不到更新。
    """
    raw = _get("/images/json?all=false")
    by_tag, by_id = {}, {}
    for im in raw or []:
        tags = [t for t in (im.get("RepoTags") or []) if t and t != "<none>:<none>"]
        info = {
            "id": (im.get("Id") or "").replace("sha256:", "")[:12],
            # 完整 ID（= 镜像 config digest）。有些镜像没有 RepoDigests（飞牛面板/
            # 离线包导入的），只能拿它跟 registry 的 config.digest 比。
            "full_id": (im.get("Id") or "").replace("sha256:", ""),
            "digests": [d for d in (im.get("RepoDigests") or []) if d],
            "created": im.get("Created") or 0,
            "size": im.get("Size") or 0,
        }
        if info["full_id"]:
            by_id[info["full_id"]] = info
        for t in tags:
            by_tag[t] = info
    return by_tag, by_id


def image_digests():
    """向后兼容：只要 {镜像名: 摘要信息}。"""
    return image_index()[0]


# socket-proxy 拒绝非 GET 时返回的是 nginx 风格的 403，跟 Docker 自己的 403 不一样，
# 用 "administrative rules" 这个特征串区分。这条提示要能直接告诉用户改哪里。
ACL_HINT = (
    "被 docker-socket-proxy 拦住了（403 Forbidden by administrative rules）。"
    "面板里的开关只是「允许面板发起」，真正的权限在 socket-proxy 的容器环境变量上："
    "给它加 POST=1、ALLOW_START=1、ALLOW_STOP=1 再重启它，更新功能才会生效。"
    "代价是面板将获得创建容器的能力（≈宿主机 root），请确认接受再开。"
)


# ------------------------------------------------------------------ 镜像清理
# 2026-10-16 用户要求「定时清理未使用镜像」。DELETE 不需要改 socket-proxy：
# 代理里写的是 `http-request deny unless METH_GET || { env(POST) -m bool }`，
# 也就是 POST=1 已经放行全部非 GET 方法（实测 /images 的 DELETE 能穿过，非 403）。

def images_all():
    """全部本地镜像（含 <none> 悬空镜像）。

    /images/json 默认不返回悬空镜像，必须 all=true。
    返回 [{id, tags, size, created, containers}]，size 单位字节。
    """
    out = []
    for im in _get("/images/json?all=true"):
        tags = [t for t in (im.get("RepoTags") or []) if t and t != "<none>:<none>"]
        out.append({
            "id": im.get("Id") or "",
            "tags": tags,
            "size": int(im.get("Size") or 0),
            "created": int(im.get("Created") or 0),
            "containers": int(im.get("Containers") or 0),
            "dangling": not tags,
        })
    return out


def container_image_ids():
    """所有容器（**含已停止**）引用的镜像 ID 集合。

    为什么不能用 /images/json 里的 containers 字段：那个只统计运行中的容器。
    已停止的容器照样占着镜像，删它会 409 conflict —— 用它当依据就会「预览说能删、
    执行时全失败」。
    """
    ids = set()
    for c in _get("/containers/json?all=true"):
        iid = c.get("ImageID") or ""
        if iid:
            ids.add(iid)
    return ids


def image_remove(iid, force=False):
    """删除一个镜像 -> (ok, message)。返回体是 [{"Deleted": ...}] 或 [{"Untagged": ...}]。

    force=False 时被容器引用的镜像会 409；我们本来就先过滤掉了，这里再兜一层。
    """
    try:
        r = _delete(f"/images/{iid}", params={"force": "1" if force else "0"})
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if r.status_code in (200, 204):
        try:
            items = r.json()
            acts = []
            for it in items if isinstance(items, list) else []:
                for k, v in (it or {}).items():
                    acts.append(f"{k} {v}")
            return True, "；".join(acts)[:200] or "已删除"
        except Exception:
            return True, "已删除"
    if blocked_by_proxy(r):
        return False, ACL_HINT
    if r.status_code == 409:
        return False, "镜像正被容器使用（或存在依赖它的子镜像），跳过"
    return False, f"HTTP {r.status_code}：{(r.text or '')[:160]}"


def blocked_by_proxy(r):
    """判断这个响应是不是被 socket-proxy 的 ACL 拦的（而不是 Docker 自身的 403）。"""
    if r is None or getattr(r, "status_code", None) != 403:
        return False
    return "administrative rules" in (r.text or "").lower()


class Task:
    """一次更新的进度黑板：后台线程写，HTTP 轮询读（2026-10-15 用户要求看实时日志/进度）。

    字段：step=当前正在做的事（人话）、pct=拉取百分比 0~100、phase=pull/recreate、
    log=逐行日志、running/success/message=结束状态。
    只保留「最近一次」任务，够用（面板是单用户；批量更新时前端逐个发起）。
    """

    def __init__(self, container, image=""):
        self.container = container
        self.image = image
        self.step = "准备中"
        self.pct = 0
        self.phase = "prepare"
        self.log = []
        self.running = True
        self.success = None
        self.message = ""
        self.started = time.time()
        self.finished = 0.0
        self._lock = threading.Lock()

    def line(self, s):
        with self._lock:
            self.log.append(str(s))

    def set(self, step=None, pct=None, phase=None):
        with self._lock:
            if step is not None:
                self.step = step
            if pct is not None:
                self.pct = pct
            if phase is not None:
                self.phase = phase

    def finish(self, success, message):
        with self._lock:
            self.success = bool(success)
            self.message = message or ""
            self.step = "完成" if success else "失败"
            took = time.time() - self.started
            # 日志尾部给一句明确结论（成功与否 + 耗时），不用用户自己拼
            self.log.append("✅ 更新成功（用时 %.1fs）" % took if success
                            else "❌ 更新失败：%s（用时 %.1fs）" % (self.message or "未知原因", took))
            self.finished = time.time()
            self.running = False

    def snapshot(self, since=0):
        with self._lock:
            since = max(0, int(since or 0))
            return {
                "container": self.container, "image": self.image, "step": self.step,
                "pct": int(self.pct), "phase": self.phase, "running": self.running,
                "success": self.success, "message": self.message,
                "n": len(self.log), "log": self.log[since:],
                "elapsed": round((self.finished or time.time()) - self.started, 1),
            }


class TaskLog(list):
    """list 子类：老的 log.append(...) 写法一字不改，新行自动推给 Task（日志实时可见）。"""

    def __init__(self, task=None):
        super().__init__()
        self._task = task

    def append(self, s):
        super().append(s)
        if self._task is not None:
            self._task.line(s)


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


def _consume_pull_stream(r, task=None):
    """解析 Docker 拉取的 NDJSON 流，边读边报「当前操作」+ 百分比。

    进度口径：每个 layer 取 frac = max(之前, current/total)，整体 = 各 layer frac 的均值。
    下载→解压阶段同一 layer 会重置 current，用 max() 保证百分比只涨不跌（实测第一版会往回跳）。
    返回 (ok, message)。
    """
    layers = {}      # id -> {"total":n,"frac":f,"said_dl":b,"said_done":b}
    buf = ""
    done_msg = ""
    last_pct = -1
    t0 = time.time()
    for chunk in r.iter_text():
        buf += chunk
        while "\n" in buf:
            raw, buf = buf.split("\n", 1)
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                if task:
                    task.line("[docker] " + raw[:200])
                continue
            if isinstance(ev, dict) and ev.get("error"):
                return False, str(ev.get("error"))[:300]
            if not isinstance(ev, dict):
                continue
            st = ev.get("status") or ""
            lid = ev.get("id") or ""
            pd = ev.get("progressDetail") or {}
            if st.startswith("Pulling from"):
                # 这行的 id 是 tag（如 12-slim）不是 layer，别当层统计
                if task:
                    task.line("开始拉取 %s" % st.split(" from ")[-1])
                continue
            if lid:
                # 任何带 id 的事件都登记为 layer：只有「需要下载」的层才会收到
                # "Pulling fs layer"，缓存命中时 daemon 直接给 "Already exists"（实测 2026-10-15），
                # 先只认前者会漏统计、进度也永远是 0。
                e = layers.setdefault(lid, {"total": 0, "frac": 0.0})
                total, cur = pd.get("total") or e["total"], pd.get("current") or 0
                if total:
                    e["total"] = total
                    e["frac"] = max(e["frac"], min(1.0, cur / float(total)))
                if st.startswith("Download complete"):
                    if not e.get("said_dl"):
                        e["said_dl"] = True
                        if task:
                            task.line("层 %s 下载完成 %s"
                                      % (lid[:12], _human(e["total"]) if e["total"] else ""))
                elif st.startswith("Pull complete") or st.startswith("Already exists"):
                    if not e.get("said_done"):
                        e["said_done"] = True
                        e["frac"] = 1.0
                        if task:
                            task.line("层 %s %s" % (
                                lid[:12],
                                "本地已有缓存" if st.startswith("Already") else "解压完成"))
            elif st.startswith("Status:"):
                done_msg = st
                if task:
                    task.line(st)
            if task and layers:
                pct = int(round(100.0 * sum(v["frac"] for v in layers.values()) / len(layers)))
                if pct != last_pct:
                    last_pct = pct
                    mb = sum(v["total"] for v in layers.values() if v["total"])
                    task.set(step="拉取镜像 %s · %d 层 · %s%s"
                                  % (task.image or "", len(layers), _human(mb) if mb else "",
                                     "（%d%%）" % pct if pct < 100 else ""),
                             pct=pct, phase="pull")
    if task:
        n = len(layers)
        dl = sum(1 for v in layers.values() if v.get("said_dl"))
        mb = sum(v["total"] for v in layers.values() if v["total"])
        if "up to date" in done_msg:
            step = "镜像已是最新，无需下载"
        elif dl:
            step = "拉取完成：新下载 %d/%d 层 · %s" % (dl, n, _human(mb) if mb else "")
        elif n:
            step = "拉取完成：%d 层全部命中本地缓存" % n
        else:
            step = "拉取完成"
        task.set(step=step, pct=100, phase="pull")
        task.line("拉取结束：%s（%.1fs）" % (done_msg or "完成", time.time() - t0))
    return True, "拉取完成"


# 本地构建的镜像（docker build 出来的 navpage / hermes-webui 这种）在 Hub 上必然 404
# 「pull access denied / repository does not exist」。第一次撞上就记下来，以后不再白等
# 十几秒去拉，直接走「用本地镜像重建」。
# 2026-10-15 用户反馈「更新镜像还是有问题」正是这一类：面板给每个容器都画了「更新」按钮，
# 而 39 个容器的镜像是自建的，点了必然失败，日志里只有一行 404，看着就像功能坏了。
LOCAL_ONLY_HINT = "本地构建镜像（Hub 上没有这个仓库）"
_LOCAL_ONLY = set()
_LOCAL_ONLY_LOCK = threading.Lock()


def _split_ref(image):
    """拆 image 引用 -> (repo, tag)。host:port/repo:tag 也能正确切分。"""
    if ":" in image.rsplit("/", 1)[-1]:
        return image.rsplit(":", 1)
    return image, "latest"


def _ref_key(repo, tag):
    return f"{repo}:{tag}"


def has_registry_host(ref):
    """image 引用第一段是否带 registry 主机名（含 . 或 : 或 localhost）。

    bare 名字（navpage / redis）既可能来自 Hub 也可能是本地构建；
    带主机的（ghcr.io/x/y、192.168.1.10:5000/x）404 只可能是认证/网络问题，
    绝不能当成本地镜像吞掉。
    """
    if "/" not in (ref or ""):
        # 没有斜杠就是没有 registry 主机 —— "navpage:latest" / "redis:alpine" 里那个冒号是 tag。
        # （第一版漏了这条，把 tag 当成了 host:port，本地镜像判定直接失效）
        return False
    first = ref.split("/")[0]
    return bool(first) and ("." in first or ":" in first or first == "localhost")


def is_local_only(image):
    """该镜像引用是否已知「本地构建、无远程仓库」（进程内记忆，重启即忘，会重新学到）。"""
    repo, tag = _split_ref(image or "")
    with _LOCAL_ONLY_LOCK:
        return _ref_key(repo, tag) in _LOCAL_ONLY


def pull_image(image, tag=None, task=None):
    """POST /images/create（流式）。带 task 时实时上报进度。返回 (ok, message)。

    返回 (True, LOCAL_ONLY_HINT) 表示「这是本地构建镜像，没有远程仓库，不用拉」——
    调用方应当继续用本地镜像重建，而不是当成失败。
    """
    if tag is None:
        image, tag = _split_ref(image)
    _key = _ref_key(image, tag)
    with _LOCAL_ONLY_LOCK:
        _known_local = _key in _LOCAL_ONLY
    if _known_local:
        if task:
            task.set(step="本地构建镜像，跳过拉取（用本地镜像重建）", phase="pull")
        return True, LOCAL_ONLY_HINT
    if task:
        task.set(step="拉取镜像 %s:%s" % (image, tag), pct=0, phase="pull")
    try:
        with _client() as c:
            with c.stream("POST", "/images/create",
                          params={"fromImage": image, "tag": tag},
                          timeout=httpx.Timeout(1800.0, connect=10.0)) as r:
                if r.status_code not in (200, 201):
                    body = r.read().decode("utf-8", "replace")
                    if r.status_code == 403 and "administrative rules" in body.lower():
                        return False, ACL_HINT
                    low = body.lower()
                    # registry 说「没这个仓库 / 没权限」，且引用是 bare 名字（没有 registry 主机）
                    # -> 判为本地构建镜像，跳过拉取、用本地镜像继续重建。
                    if (r.status_code in (401, 404) and not has_registry_host(image)
                            and ("pull access denied" in low or "repository does not exist" in low
                                 or "denied" in low)):
                        with _LOCAL_ONLY_LOCK:
                            _LOCAL_ONLY.add(_key)
                        if task:
                            task.set(step="本地构建镜像，跳过拉取（用本地镜像重建）", phase="pull")
                        return True, LOCAL_ONLY_HINT
                    if task:
                        task.line("拉取原始错误: " + (body or "")[:300])
                    return False, _friendly_pull_err(r.status_code, body)
                return _consume_pull_stream(r, task)
    except Exception as e:
        txt = f"{type(e).__name__}: {e}"
        if any(h in txt.lower() for h in _NET_HINTS):
            return False, _friendly_pull_err(0, txt)
        return False, txt


# registry 连不上时的特征串：daemon 出网走代理，代理一抖就是这样一串 EOF / timeout
_NET_HINTS = ("eof", "dial tcp", "i/o timeout", "proxyconnect", "connection reset",
              "tls handshake", "no such host", "connection refused", "connection timed out",
              "timeout awaiting response", "http2", "unexpected eof")


def _friendly_pull_err(status, body):
    """把拉取失败的原始报文压成一句能行动的话（详情仍由调用方写进日志）。"""
    low = (body or "").lower()
    if any(h in low for h in _NET_HINTS):
        return ("连不上镜像仓库（NAS 的 docker 要经代理出网，多半是代理不稳/不通）。"
                "稍后重试，或改用「仅重建」用本地现有镜像重建容器。")
    if status in (401, 404):
        return f"仓库里没有这个镜像或没权限（HTTP {status}）"
    return f"HTTP {status}: {(body or '')[:200]}"


def container_inspect(ref):
    """ref 可以是容器名或容器 ID —— 本模块内部一律传 ID。"""
    return _get(f"/containers/{ref}/json")


def find_container(name):
    """按**精确名字**找容器，返回完整容器 ID；找不到返回 None。

    为什么不用 /containers/{name}/json：Docker 按名字找容器是**前缀匹配**的。
    一旦存在 `X__navpage_old` 这种残留，拿 `X` 去 stop / rename 会模糊命中那个残留容器，
    Docker 便回「Renaming a container with the same name as its current name」——
    2026-10-15 用户更新 hermes 时踩到的就是这个报错（现象完全对得上，SKILL.md 有记录）。
    所以所有写操作一律走这里精确解析出来的容器 ID，绝不把裸名字拼进路径。
    """
    for c in _get("/containers/json?all=true") or []:
        if name in [n.lstrip("/") for n in (c.get("Names") or [])]:
            return c.get("Id")
    return None


def _backup_name(name):
    return f"{name}__navpage_old"


def _heal_backup(name, log):
    """收拾上一次更新残留的 `{name}__navpage_old`，返回是否真的动了手。

    更新中途挂掉（面板被重启 / 请求被掐断）会留下两种残留：
      - 原容器还在 → 残留是「新容器已建好、旧壳没删掉」，直接删掉
      - 原容器不在 → 上次卡在「改完名、还没新建」，把残留改回原名并启动，先把服务救回来
    """
    backup = _backup_name(name)
    bid = find_container(backup)
    if not bid:
        return False
    if find_container(name):
        try:
            _delete(f"/containers/{bid}", params={"force": 1, "v": 1})
            log.append(f"清理上次更新遗留的旧容器 {backup}")
        except Exception as e:
            log.append(f"清理遗留容器 {backup} 失败（可在容器页手动删）: {e}")
    else:
        try:
            _post(f"/containers/{bid}/rename", params={"name": name})
            _post(f"/containers/{bid}/start", timeout=90.0)
            log.append(f"上次更新中断在改名之后，已把 {backup} 改回 {name} 并启动")
        except Exception as e:
            log.append(f"恢复 {backup} → {name} 失败: {e}")
    return True


# 同一容器同时只允许一次更新。FastAPI 的同步路由跑在线程池里，
# 双击「更新」、或批量更新期间再点单个，会真的并发跑两遍，
# 互相把对方的改名 / 新建搅乱（用户遇到的那次就是两遍并发）。
_BUSY = set()
_BUSY_LOCK = threading.Lock()


def recreate_container(name, image=None, do_pull=True, task=None):
    """拉新镜像 + 用原配置重建容器（Watchtower 的套路）。

    失败会尽量把旧容器恢复起来。返回 (ok, message, log)。
    task：dockerapi.Task，给了就实时上报「当前操作」+ 拉取进度 + 逐行日志。
    """
    name = (name or "").strip()
    if not name:
        return False, "缺容器名", []
    with _BUSY_LOCK:
        if name in _BUSY:
            return False, f"{name} 正在更新中（上一次还没结束），这次请求已跳过", \
                [f"{name} 正在更新中：同一容器不接受并发更新，已跳过这次重复请求"]
        _BUSY.add(name)
    try:
        return _recreate(name, image, do_pull, task)
    finally:
        with _BUSY_LOCK:
            _BUSY.discard(name)


def _recreate(name, image=None, do_pull=True, task=None):
    # TaskLog 是个 list：老的 log.append(...) 写法原样保留，同时把每行实时推给前端
    log = TaskLog(task)
    log.append(f"目标容器: {name}")
    if task:
        task.set(step=f"检查残留容器（{name}）", phase="prepare")

    # 0) 先收拾上次残留（存在 {name}__navpage_old 时）
    _heal_backup(name, log)

    if task:
        task.set(step="读取容器配置", phase="prepare")
    cid = find_container(name)
    if not cid:
        log.append(f"没找到名为 {name} 的容器")
        return False, f"找不到名为 {name} 的容器：可能已被删掉或改过名", log
    try:
        old = container_inspect(cid)
    except Exception as e:
        return False, f"读不到容器配置: {e}", log

    img = image or (old.get("Config") or {}).get("Image") or ""
    if not img:
        return False, "容器没有镜像信息", log

    try:
        _hc = old.get("HostConfig") or {}
        _np = len((_hc.get("PortBindings") or {}))
        _nm = len(_hc.get("Binds") or [])
        log.append(f"配置已读取：镜像 {img}，端口映射 {_np} 个，挂载 {_nm} 处，"
                   f"重启策略 {(old.get('HostConfig') or {}).get('RestartPolicy', {}).get('Name') or 'no'}")
    except Exception:
        log.append(f"配置已读取：镜像 {img}")
    if task:
        task.set(step=f"准备拉取 {img}", pct=0, phase="pull")
    pulled = True
    if do_pull:
        if task:
            task.image = img
        log.append(f"开始拉取镜像 {img} …")
        ok, msg = pull_image(img, task=task)
        if ok and msg == LOCAL_ONLY_HINT:
            # 自建镜像：没有远程仓库可拉，直接用本地镜像重建（等价于「重建容器」）
            pulled = False
            log.append(f"{img}：{LOCAL_ONLY_HINT}，跳过拉取，直接用本地镜像重建")
        else:
            # ACL_HINT 会由下面的 message 完整带出，日志行只留一句摘要，避免同一段话显示两遍
            log.append(f"拉取 {img}: {'完成' if ok else ('失败' if msg == ACL_HINT else msg)}")
        if not ok:
            return False, f"拉取镜像失败：{msg}", log

    body = _create_body(old, img)
    backup = _backup_name(name)

    # 优雅退出等待时间：优先用容器自己配的 StopTimeout，没配就 30s
    # （原来硬编码 30，且这段时间面板一片沉默，用户以为卡死了 —— 2026-10-15 改成带倒计时）
    stop_wait = 30
    try:
        _st = (old.get("HostConfig") or {}).get("StopTimeout")
        if isinstance(_st, int) and _st > 0:
            stop_wait = _st
    except Exception:
        pass
    log.append(f"停止旧容器（最多等 {stop_wait}s 优雅退出）")
    if task:
        task.set(step=f"停止旧容器（{name}，最多等 {stop_wait}s 优雅退出）", phase="recreate")
    # 1) 停旧容器（用 ID：裸名字可能前缀匹配到残留容器）
    #    放在线程里发请求，主线程每秒刷一次「已等 Ns」，避免长静默
    _box = {}

    def _do_stop():
        try:
            _box["r"] = _post(f"/containers/{cid}/stop", params={"t": stop_wait},
                              timeout=float(stop_wait) + 60.0)
        except Exception as _e:  # noqa
            _box["e"] = _e

    _th = threading.Thread(target=_do_stop, daemon=True)
    _th.start()
    _waited = 0
    while _th.is_alive() and _waited < stop_wait + 30:
        _th.join(1.0)
        _waited += 1
        if task and _th.is_alive():
            task.set(step=f"停止旧容器（已等 {_waited}s / 最多 {stop_wait}s）")
    if "e" in _box:
        log.append(f"停止旧容器异常（继续）: {_box['e']}")
    else:
        r = _box.get("r")
        if r is None:
            log.append("停止旧容器超时（继续）")
        elif blocked_by_proxy(r):
            log.append("停止旧容器: 被 socket-proxy 拦（403）")
            return False, "停不下旧容器：" + ACL_HINT, log
        else:
            log.append(f"停止旧容器: HTTP {r.status_code}（等了 {_waited}s）")

    if task:
        task.set(step=f"旧容器改名让位 → {backup}", phase="recreate")
    # 2) 改名让位（同样用 ID）
    try:
        r = _post(f"/containers/{cid}/rename", params={"name": backup})
        if blocked_by_proxy(r):
            return False, "改不了旧容器名：" + ACL_HINT, log
        if r.status_code >= 400:
            return False, f"重命名旧容器失败 HTTP {r.status_code}: {r.text[:160]}", log
        log.append(f"旧容器改名 -> {backup}")
    except Exception as e:
        return False, f"重命名旧容器失败: {e}", log

    log.append("按原配置创建新容器 …")
    if task:
        task.set(step="按原配置创建新容器", phase="recreate")
    # 3) 按原配置新建（名字沿用原名）
    new_id = None
    try:
        r = _post("/containers/create", params={"name": name}, json_body=body,
                  timeout=120.0)
        if r.status_code in (200, 201):
            new_id = (r.json() or {}).get("Id")
            log.append(f"新容器已创建 {str(new_id)[:12]}")
        elif blocked_by_proxy(r):
            raise RuntimeError(ACL_HINT)
        else:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        log.append(f"创建新容器失败: {e}")
        _rollback(cid, name, log)
        return False, f"创建新容器失败，已回滚旧容器：{e}", log

    if task:
        task.set(step="启动新容器", phase="recreate")
    # 4) 启动新容器
    try:
        r = _post(f"/containers/{new_id}/start", timeout=90.0)
        if r.status_code not in (204, 304):
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        log.append("新容器已启动")
    except Exception as e:
        log.append(f"启动新容器失败: {e}")
        try:
            _delete(f"/containers/{new_id}", params={"force": 1})
        except Exception:
            pass
        _rollback(cid, name, log)
        return False, f"启动新容器失败，已回滚旧容器：{e}", log

    if task:
        task.set(step="清理旧容器", phase="recreate")
    # 5) 删掉旧容器（用它自己的 ID：改名不影响 ID，不必再查名字）
    try:
        r = _delete(f"/containers/{cid}", params={"force": 1, "v": 1})
        log.append(f"清理旧容器: HTTP {r.status_code}")
    except Exception as e:
        log.append(f"清理旧容器失败（可手动删掉 {backup}）: {e}")

    if pulled:
        return True, "更新完成", log
    return True, "重建完成（本地构建镜像，未拉取）", log


def _rollback(cid, name, log):
    """把改名后的旧容器改回来并启动。cid 是旧容器 ID（改名不影响 ID）。"""
    if isinstance(log, TaskLog) and log._task is not None:
        log._task.set(step="出错，正在回滚旧容器", phase="rollback")
    try:
        r = _post(f"/containers/{cid}/rename", params={"name": name})
        log.append(f"回滚改名: HTTP {r.status_code}")
    except Exception as e:
        log.append(f"回滚改名失败: {e}")
    try:
        r = _post(f"/containers/{cid}/start", timeout=90.0)
        log.append(f"回滚启动: HTTP {r.status_code}")
    except Exception as e:
        log.append(f"回滚启动失败: {e}")


def _create_body(old, image):
    cfg = old.get("Config") or {}
    nets = (old.get("NetworkSettings") or {}).get("Networks") or {}
    endpoints = {}
    for net, v in nets.items():
        ep = {}
        if v.get("Aliases"):
            ep["Aliases"] = v["Aliases"]
        if v.get("IPAMConfig"):
            ep["IPAMConfig"] = v["IPAMConfig"]
        endpoints[net] = ep
    return {
        "Image": image,
        "Hostname": cfg.get("Hostname") or "",
        "Domainname": cfg.get("Domainname") or "",
        "User": cfg.get("User") or "",
        "Env": cfg.get("Env") or [],
        "Cmd": cfg.get("Cmd") or [],
        "Entrypoint": cfg.get("Entrypoint") or [],
        "WorkingDir": cfg.get("WorkingDir") or "",
        "Labels": cfg.get("Labels") or {},
        "ExposedPorts": cfg.get("ExposedPorts") or {},
        "Tty": bool(cfg.get("Tty")),
        "OpenStdin": bool(cfg.get("OpenStdin")),
        "HostConfig": old.get("HostConfig") or {},
        "NetworkingConfig": {"EndpointsConfig": endpoints},
    }


def containers(all_containers=False):
    """返回规范化后的容器列表。"""
    raw = _get(f"/containers/json?all={'true' if all_containers else 'false'}")
    out = []
    for c in raw:
        names = [n.lstrip("/") for n in (c.get("Names") or [])]
        name = names[0] if names else (c.get("Id") or "")[:12]
        labels = c.get("Labels") or {}
        ports = []
        for p in c.get("Ports") or []:
            ports.append({
                "private": p.get("PrivatePort") or 0,
                "public": p.get("PublicPort") or 0,
                "type": p.get("Type") or "tcp",
                "ip": p.get("IP") or "",
            })
        pub = [p for p in ports if p["type"] == "tcp" and p["public"]]
        primary = None
        for p in pub:
            if p["private"] in WEB_PRIVATE:
                primary = p["public"]
                break
        if primary is None and pub:
            primary = pub[0]["public"]
        out.append({
            "id": (c.get("Id") or "")[:12],
            "name": name,
            "image": c.get("Image") or "",
            # 容器真正跑的那个镜像 ID：tag 可能已经被别的容器/一次 pull 挪到新镜像上了，
            # 只按 tag 比对会把「还在跑旧镜像」的容器误判成「最新」（2026-10-22 修）。
            "image_id": (c.get("ImageID") or "").replace("sha256:", ""),
            "state": c.get("State") or "",
            "status": c.get("Status") or "",
            "labels": labels,
            "ports": ports,
            "public_ports": sorted({p["public"] for p in pub}),
            "primary_port": primary,
            "compose_project": labels.get("com.docker.compose.project", ""),
            "compose_service": labels.get("com.docker.compose.service", ""),
            "nav_name": labels.get("nav.name", ""),
            "nav_icon": labels.get("nav.icon", ""),
            "nav_url_lan": labels.get("nav.url.lan", ""),
            "nav_url_wan": labels.get("nav.url.wan", ""),
            "nav_category": labels.get("nav.category", ""),
            "nav_hide": str(labels.get("nav.hide", "")).lower() in ("1", "true", "yes"),
        })
    out.sort(key=lambda x: (x["compose_project"] or "~", x["name"]))
    return out
