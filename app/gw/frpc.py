"""FRP 客户端管理：让没有公网 IP 的机器也能把卡片暴露出去。

镜像里内置官方 frpc（见 Dockerfile，按 TARGETARCH 下载），这里只做三件事：
  1. 按当前卡片生成 frpc.toml；
  2. 起了/停了/配置变了就重启进程（frpc 本身很轻，重启 <1s）；
  3. 留着日志尾巴，设置页里能直接看到「连上 frps 没有、哪个 proxy 失败了」。

为什么不用 docker 跑 frpc：容器里已经有 frpc 二进制，再起一个容器既需要 docker.sock，
又会让「卡片改了但 frp 没变」这种不一致有机会发生。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

BIN_CANDIDATES = ["/usr/local/bin/frpc", "/app/frpc", "frpc"]


def find_binary() -> str:
    env = os.environ.get("NAVPAGE_FRPC", "")
    if env and os.path.exists(env):
        return env
    for c in BIN_CANDIDATES:
        p = shutil.which(c) if "/" not in c else c
        if p and os.path.exists(p):
            return p
    return ""


def toml_escape(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


class FrpManager:
    def __init__(self, data_dir: str, log=None):
        self.dir = os.path.join(data_dir, "gw")
        os.makedirs(self.dir, exist_ok=True)
        self.cfg_path = os.path.join(self.dir, "frpc.toml")
        self.log_path = os.path.join(self.dir, "frpc.log")
        self.log = log or (lambda *a: None)
        self.proc: subprocess.Popen | None = None
        self.last_cfg = ""
        self.last_cfg_obj: dict = {}
        self.started_at = 0
        self.error = ""

    # ---------------------------------------------------------------- 配置
    def build_config(self, cfg: dict, proxies: list[dict]) -> str:
        lines = [
            f'serverAddr = "{toml_escape(cfg.get("frp_server") or "")}"',
            f'serverPort = {int(cfg.get("frp_port") or 7000)}',
        ]
        # frps 挂在 443 后面（OpenResty/nginx 终结 TLS）时必须走 wss，否则明文 TCP 直接连不上。
        proto = (cfg.get("frp_protocol") or "tcp").strip().lower()
        if proto and proto != "tcp":
            lines.append(f'transport.protocol = "{toml_escape(proto)}"')
        lines += [
            'auth.method = "token"',
            f'auth.token = "{toml_escape(cfg.get("frp_token") or "")}"',
            'transport.tls.enable = true',
            'transport.tcpMux = true',
            # 偶发失败（frps 重启、网络抖动）自动重试，而不是第一次登录失败就退出。
            'loginFailExit = false',
            f'log.to = "{toml_escape(self.log_path)}"',
            f'log.level = "{toml_escape(cfg.get("frp_log_level") or "info")}"',
            f'log.maxDays = 3',
            'webServer.addr = "127.0.0.1"',
            'webServer.port = 7400',
        ]
        for p in proxies:
            lines += [
                "",
                "[[proxies]]",
                f'name = "{toml_escape(p["name"])}"',
                'type = "http"',
                f'localIP = "{toml_escape(p["local_ip"])}"',
                f'localPort = {int(p["local_port"])}',
            ]
            if p.get("subdomain"):
                lines.append(f'subdomain = "{toml_escape(p["subdomain"])}"')
            if p.get("domain"):
                lines.append(f'customDomains = ["{toml_escape(p["domain"])}"]')
            if p.get("http_user"):
                lines.append(f'httpUser = "{toml_escape(p["http_user"])}"')
                lines.append(f'httpPassword = "{toml_escape(p.get("http_password") or "")}"')
        return "\n".join(lines) + "\n"

    def apply(self, cfg: dict, proxies: list[dict]) -> dict:
        """写配置并在必要时（重）启进程。返回状态。"""
        enabled = bool(cfg.get("frp_enabled"))
        if not enabled:
            self.stop()
            self.error = ""
            return self.status()
        if not (cfg.get("frp_server") or "").strip():
            self.stop()
            self.error = "没填 frps 服务器地址"
            return self.status()
        bin_path = find_binary()
        if not bin_path:
            self.error = "镜像里没有 frpc 二进制（构建时未下载成功）"
            return self.status()
        self.last_cfg_obj = dict(cfg)
        text = self.build_config(cfg, proxies)
        # 判「变没变」要比磁盘上的真内容，不能只比内存里的 last_cfg：
        # 容器重建后拿到的可能是上一轮留在卷里的旧配置（端口是错的），
        # 而 last_cfg 是空的/旧的，只比内存会得出「没变」→ 永远不修。
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                disk = f.read()
        except Exception:
            disk = ""
        changed = text != disk or not self._alive()
        if changed:
            try:
                with open(self.cfg_path, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception as e:
                self.error = f"写配置失败：{e}"
                return self.status()
            self._restart(bin_path)
            self.last_cfg = text
        self.error = ""
        return self.status()

    # ---------------------------------------------------------------- 进程
    def _alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def _restart(self, bin_path: str):
        self.stop()
        try:
            open(self.log_path, "a").write(f"\n===== {time.strftime('%F %T')} 启动 frpc =====\n")
            f = open(self.log_path, "ab", buffering=0)
            # 关键坑（2026-10-25 实测）：frp v0.6x 会读 HTTP_PROXY/ALL_PROXY，
            # 把 frpc→frps 的 TCP 连接也塞进上网代理，表现是「try to connect… 3 秒后 EOF」，
            # frps 侧一条日志都没有。容器里常带代理变量，所以给子进程一个干净环境。
            env = {k: v for k, v in os.environ.items()
                   if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
            self.proc = subprocess.Popen([bin_path, "-c", self.cfg_path],
                                         stdout=f, stderr=subprocess.STDOUT,
                                         cwd=self.dir, start_new_session=False, env=env)
            self.started_at = int(time.time())
            self.log(f"frpc 已启动（pid {self.proc.pid}）")
        except Exception as e:
            self.error = f"启动 frpc 失败：{e}"
            self.log(self.error)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    def tail(self, n: int = 25) -> list[str]:
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8000))
                text = f.read().decode("utf-8", "replace")
            return text.splitlines()[-n:]
        except Exception:
            return []

    def _hint(self) -> str:
        """把 frpc 日志里的失败翻成人话。

        注意：加了 loginFailExit = false 之后，登录失败时进程仍然活着（每 5 秒重试），
        所以不能只看「进程是否存活」，要看日志尾巴里有没有失败痕迹。
        """
        if not self.last_cfg:
            return ""
        log = self.tail(40)
        # 只看「本次启动」之后的日志，否则改好配置后会残留上一次的报错
        for i in range(len(log) - 1, -1, -1):
            if "启动 frpc" in log[i]:
                log = log[i + 1:]
                break
        marks = ("connect to server error", "login to the server failed",
                 "already exists", "token in login", "unexpected EOF")
        if not any(m in "\n".join(log) for m in marks):
            return ""
        return explain_failure(log, self.last_cfg_obj or {})

    def status(self) -> dict:
        return {
            "running": self._alive(),
            "pid": self.proc.pid if self._alive() else 0,
            "since": self.started_at,
            "binary": find_binary(),
            "error": self.error,
            "hint": self._hint(),
            "log": self.tail(12),
        }

# FRP 的报错原名很含糊（EOF / session shutdown），这里翻成人话。
# 每条都来自 2026-10-26 用真实 frp 0.61.1 二进制逐个复现的结果。
def explain_failure(log: list[str], cfg: dict) -> str:
    text = "\n".join(log[-14:])
    port = str(cfg.get("frp_port") or "")
    proto = (cfg.get("frp_protocol") or "tcp").strip().lower()
    if "unexpected EOF" in text:
        return ("连接被中途掐断：选了 websocket/wss，但服务端这个端口不认 ws。"
                "直连 frps 请选 tcp 并填 bindPort（默认 7000）；只有前置反代时（wss + 443）才会看到这个错。")
    if "EOF" in text:
        if proto == "tcp" and port in ("80", "443", "8080", "8443"):
            return (f"端口填的是 {port}（网站端口），tcp 直连连不上它。"
                    "tcp 要填 frps 的 bindPort（默认 7000）；只有 wss 才填 443。")
        return ("TCP 能连上但对方立刻断开 —— 这个端口上跑的不是 frps。常见三种："
                "① 填了 80/443（nginx、Caddy、OpenResty）；② 填了 7500（frps 仪表盘）；"
                "③ VPS 上另有一套 frps 且版本/配置不同。确认填的是 frps 的 bindPort（本项目默认 7000）。")
    if "connection refused" in text:
        return ("端口没有服务监听：frps 没启动、端口写错，或 VPS 安全组/防火墙没放行这个端口。")
    if "i/o timeout" in text or "context deadline exceeded" in text or "no route to host" in text:
        return ("连接超时：VPS 安全组或本机防火墙没放行这个端口（有的是机房拦截了非标准端口）。")
    if "token in login doesn't match" in text:
        return "token 和 frps 不一致：把 VPS 上的 auth.token 和这里填的改成同一个。"
    if "session shutdown" in text:
        return ("服务端主动关了会话：token 不一致，或两端 frp 版本差太多"
                "（本项目 frps 镜像固定 v0.61.1，自己另装的建议也升到 0.61.x）。")
    if "already exists" in text:
        return "隧道名和同一个 frps 上其他客户端重名了：给「隧道名前缀」填个独一无二的值。"
    return ""
