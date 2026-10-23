"""NavPage Agent：把本机 Docker 容器清单推送到面板。

零依赖（只用标准库），两种跑法都行：

1) 容器（推荐，远端只要有 docker）：
     docker run -d --name navpage-agent --restart unless-stopped \
       -v /var/run/docker.sock:ro \
       -e PANEL_URL=http://<面板地址>:8095 \
       -e AGENT_TOKEN=npk_xxxxxxxx \
       -e INTERVAL=300 \
       yuwancumian2009/navpage:latest python -m app.agent

2) 主机直接跑（有 python3、不想额外拉镜像）：
     PANEL_URL=... AGENT_TOKEN=... python3 agent.py

纯出站：只向面板 POST，不监听端口、不接受任何指令。

环境变量：
  PANEL_URL    面板地址（必填），如 http://127.0.0.1:8095
  AGENT_TOKEN  面板「采集源」里生成的令牌（必填）
  INTERVAL     上报间隔秒，默认 300
  DOCKER_HOST  默认 unix:///var/run/docker.sock，也支持 tcp:// 和 http(s)://
  HOST_NAME    显示用主机名，默认 hostname
  AGENT_ID     主机唯一 ID，默认持久化到 <NAVPAGE_DATA 或 /tmp>/agent.id
"""
import http.client
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# 常见 Web 端口优先（和面板 app/dockerapi.py 里的 WEB_PRIVATE 保持一致）
WEB_PRIVATE = {80, 443, 3000, 5000, 5001, 8000, 8001, 8080, 8081, 8088, 8090,
               8096, 8443, 8888, 9000, 9090, 25600, 32400}
KEEP_LABEL_PREFIX = ("nav.", "com.docker.compose.project", "com.docker.compose.service")


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=15):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def _get_json(docker_host, path):
    """最小 Docker API 客户端：unix socket / tcp / http(s) 三种都支持。"""
    if docker_host.startswith("unix://"):
        conn = _UnixConnection(docker_host[len("unix://"):])
        try:
            conn.request("GET", path)
            r = conn.getresponse()
            body = r.read()
            if r.status >= 400:
                raise RuntimeError(f"Docker API {r.status}: {body[:200]!r}")
            return json.loads(body or b"{}")
        finally:
            conn.close()
    u = urllib.parse.urlparse(docker_host if "://" in docker_host else "http://" + docker_host)
    if u.scheme in ("tcp", "http", "https"):
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = cls(u.hostname, u.port or (2375 if u.scheme == "tcp" else 80), timeout=15)
        try:
            conn.request("GET", path)
            r = conn.getresponse()
            body = r.read()
            if r.status >= 400:
                raise RuntimeError(f"Docker API {r.status}: {body[:200]!r}")
            return json.loads(body or b"{}")
        finally:
            conn.close()
    raise RuntimeError(f"无法识别的 DOCKER_HOST: {docker_host}")


def normalize(raw):
    """把 /containers/json 结果整理成面板要的结构（含 nav.* 标签）。"""
    out = []
    for c in raw or []:
        names = [n.lstrip("/") for n in (c.get("Names") or [])]
        name = names[0] if names else (c.get("Id") or "")[:12]
        labels = c.get("Labels") or {}
        ports = [{"private": p.get("PrivatePort") or 0, "public": p.get("PublicPort") or 0,
                  "type": p.get("Type") or "tcp", "ip": p.get("IP") or ""}
                 for p in (c.get("Ports") or [])]
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
            "image_id": (c.get("ImageID") or "").replace("sha256:", ""),
            "state": c.get("State") or "",
            "status": c.get("Status") or "",
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
            "labels": {k: v for k, v in labels.items() if k.startswith(KEEP_LABEL_PREFIX)},
        })
    out.sort(key=lambda x: (x["compose_project"] or "~", x["name"]))
    return out


def _agent_id():
    path = os.environ.get("AGENT_ID") or os.path.join(
        os.environ.get("NAVPAGE_DATA") or "/tmp", "navpage-agent.id")
    try:
        if os.path.exists(path):
            v = open(path, encoding="utf-8").read().strip()
            if v:
                return v
    except Exception:
        pass
    v = uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write(v)
    except Exception:
        pass
    return v


def build_payload(docker_host):
    ver = {}
    try:
        ver = _get_json(docker_host, "/version")
    except Exception as e:
        ver = {"_error": f"{type(e).__name__}: {e}"}
    raw = _get_json(docker_host, "/containers/json?all=true")
    return {
        "host_id": _agent_id(),
        "host_name": os.environ.get("HOST_NAME") or socket.gethostname(),
        "sent_at": int(time.time()),
        "docker": {"version": ver.get("Version") or "", "api": ver.get("ApiVersion") or "",
                   "ok": not ver.get("_error"), "error": ver.get("_error") or ""},
        "containers": normalize(raw),
    }


def push_once(panel, token, docker_host):
    payload = build_payload(docker_host)
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(panel.rstrip("/") + "/api/agent/inventory", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Agent-Token": token})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return json.loads(r.read() or b"{}"), len(payload["containers"])
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}")


def main():
    panel = os.environ.get("PANEL_URL", "").strip()
    token = os.environ.get("AGENT_TOKEN", "").strip()
    if not panel or not token:
        print("PANEL_URL 和 AGENT_TOKEN 必须设置", flush=True)
        return 2
    docker_host = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    interval = max(30, int(os.environ.get("INTERVAL", "300") or 300))
    print(f"[navpage-agent] -> {panel}  每 {interval}s 上报  docker={docker_host} "
          f"id={_agent_id()}", flush=True)
    once = bool(os.environ.get("ONCE"))  # 只上报一次就退出：装完自测用
    fails = 0
    while True:
        t0 = time.time()
        try:
            res, n = push_once(panel, token, docker_host)
            if fails:
                print(f"[navpage-agent] 恢复正常（之前连续失败 {fails} 次）", flush=True)
            fails = 0
            print(f"[navpage-agent] ok 上报 {n} 个容器 新增 {res.get('imported', 0)} "
                  f"更新 {res.get('updated', 0)} 消失 {res.get('missing', 0)}", flush=True)
            if once:
                return 0
            time.sleep(max(5, interval - int(time.time() - t0)))
        except Exception as e:
            fails += 1
            print(f"[navpage-agent] 失败（连续 {fails} 次）：{type(e).__name__}: {e}", flush=True)
            if once:
                return 1
            time.sleep(min(60, 5 * fails))


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)
