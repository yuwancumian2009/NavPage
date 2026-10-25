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
        text = self.build_config(cfg, proxies)
        changed = text != self.last_cfg or not self._alive()
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

    def status(self) -> dict:
        return {
            "running": self._alive(),
            "pid": self.proc.pid if self._alive() else 0,
            "since": self.started_at,
            "binary": find_binary(),
            "error": self.error,
            "log": self.tail(12),
        }
