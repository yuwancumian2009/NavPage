"""远程 FRP 节点：把 frps（可选 + Caddy 终止 TLS）部署到任意裸 VPS。

为什么要有这个模块
------------------
导航页的 FRP 客户端只要求「有个 frps 能连、80/443 上有个按 Host 转发的入口」。
但让用户自己去 VPS 上手写 frps.toml / nginx / 证书，是这个功能最大的门槛。
这里把「服务端那半边」也自动化：

  1. SSH 进去做体检（Docker、端口占用、架构）；
  2. 生成并上传 frps.toml + docker-compose.yml（+ Caddyfile），幂等覆盖；
  3. docker compose up -d，然后在本机回环上验证 frps / Caddy 真的应答；
  4. 顺手把 `*.<root>` 的通配 A 记录补齐（走「设置 → 证书」里的 DNS 服务商）；
  5. 用导航页内置 ACME（DNS-01）签 `*.<root>`，把证书推到节点上并 reload Caddy；
  6. 返回一组「填到 FRP 客户端」的参数，一键写入设置。

两种形态
--------
  http  : frps 自己占 80（vhostHTTPPort=80）+ 7000，无证书，最省事；
  caddy : frps 的 vhost 只监听 8080，Caddy 占 80/443 用文件证书终止 TLS，
          frpc 走 wss:443。证书由导航页签发并定期同步，Caddy 不用 ACME。

不依赖 1Panel / 宝塔，只要能 SSH + 装了 Docker。
"""

from __future__ import annotations

import io
import ipaddress
import json
import os
import re
import socket
import time
import uuid

NODE_DIR = "/opt/navpage-node"
COMPOSE_PROJECT = "navpage-node"
FRPS_IMAGE = "fatedier/frps:v0.61.1"
CADDY_IMAGE = "caddy:2"


class NodeError(Exception):
    pass


# --------------------------------------------------------------------- 存储
def load_nodes() -> list[dict]:
    from .. import db
    raw = db.get_settings().get("frp_nodes") or "[]"
    try:
        nodes = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:
        nodes = []
    out = []
    for n in nodes:
        if isinstance(n, dict) and n.get("host"):
            n.setdefault("id", uuid.uuid4().hex[:8])
            out.append(n)
    return out


def save_nodes(nodes: list[dict]) -> None:
    from .. import db
    db.update_settings({"frp_nodes": json.dumps(nodes, ensure_ascii=False)})


def get_node(nid: str) -> dict | None:
    for n in load_nodes():
        if n.get("id") == nid:
            return n
    return None


def upsert_node(node: dict) -> dict:
    nodes = load_nodes()
    node = dict(node or {})
    node["host"] = (node.get("host") or "").strip()
    if not node["host"]:
        raise NodeError("服务器地址不能为空")
    node["id"] = node.get("id") or uuid.uuid4().hex[:8]
    old = next((n for n in nodes if n["id"] == node["id"]), None)
    if old:
        # 前端回传 *** 表示「没改」，保留原值
        for k in ("password", "key", "token"):
            if node.get(k) in (None, "", "***"):
                node[k] = old.get(k, "")
        node.setdefault("created_at", old.get("created_at"))
        nodes[nodes.index(old)] = {**old, **node}
    else:
        node.setdefault("created_at", int(time.time()))
        nodes.append(node)
    save_nodes(nodes)
    return node


def delete_node(nid: str) -> bool:
    nodes = load_nodes()
    rest = [n for n in nodes if n.get("id") != nid]
    if len(rest) == len(nodes):
        return False
    save_nodes(rest)
    return True


SECRET_FIELDS = ("password", "key", "token")


def mask_node(n: dict) -> dict:
    out = dict(n)
    for k in SECRET_FIELDS:
        if out.get(k):
            out[k] = "***"
    out["has_password"] = bool(n.get("password"))
    out["has_key"] = bool(n.get("key"))
    return out


# --------------------------------------------------------------------- 渲染
def _toml_str(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def node_ports(node: dict) -> dict:
    """算出这个节点实际要用的端口。"""
    mode = (node.get("mode") or "caddy").lower()
    bind_port = int(node.get("frps_bind_port") or 7000)
    # vhost 端口允许覆盖：80/443 被别的服务占着（1Panel、宝塔、既有 nginx）时用得上
    if mode == "http":
        return {"mode": "http", "bind": bind_port,
                "vhost": int(node.get("vhost_port") or 80), "public": int(node.get("vhost_port") or 80)}
    return {"mode": "caddy", "bind": bind_port,
            "vhost": int(node.get("vhost_port") or 8080),
            "public": int(node.get("https_port") or 443)}


def render_frps(node: dict) -> str:
    p = node_ports(node)
    dash = int(node.get("dashboard_port") or 7500)
    # 反代形态（公网只开 443）时 frps 完全不需要出网：bindPort / vhost 都只听回环，
    # 由本机 nginx 转发进来，少一个被扫描的端口。
    bind_addr = 'bindAddr = "127.0.0.1"\n' if (node.get("client_mode") or "direct").lower() == "proxy" else ""
    return f"""# 由 NavPage 自动生成，请勿手改（重新部署会覆盖）
{bind_addr}bindPort = {p['bind']}
vhostHTTPPort = {p['vhost']}

auth.method = "token"
auth.token = {_toml_str(node.get('token') or '')}

# 仪表盘只监听回环，需要看就走 SSH 隧道
webServer.addr = "127.0.0.1"
webServer.port = {dash}
webServer.user = "admin"
webServer.password = {_toml_str(node.get('dashboard_password') or uuid.uuid4().hex[:12])}

log.level = "info"
"""


def frps_image(node: dict) -> str:
    """frps 镜像可覆盖：对端已有更老/更新的 frpc 客户端时（frp 要求客户端版本 ≈ 服务端），
    下拉里能直接换成对应版本，不用改代码重打镜像。"""
    return (node.get("frps_image") or "").strip() or FRPS_IMAGE


def render_compose(node: dict) -> str:
    p = node_ports(node)
    svc = [
        "  frps:",
        f"    image: {frps_image(node)}",
        "    container_name: navpage-node-frps",
        "    restart: always",
        "    network_mode: host",
        "    volumes:",
        f"      - {NODE_DIR}/frps.toml:/etc/frp/frps.toml:ro",
        '    command: ["-c", "/etc/frp/frps.toml"]',
    ]
    if p["mode"] == "caddy":
        svc += [
            "",
            "  caddy:",
            f"    image: {CADDY_IMAGE}",
            "    container_name: navpage-node-caddy",
            "    restart: always",
            "    network_mode: host",
            "    volumes:",
            f"      - {NODE_DIR}/Caddyfile:/etc/caddy/Caddyfile:ro",
            f"      - {NODE_DIR}/certs:/certs:ro",
            "      - caddy_data:/data",
            "      - caddy_config:/config",
        ]
    body = "services:\n" + "\n".join(svc) + "\n"
    if p["mode"] == "caddy":
        body += "\nvolumes:\n  caddy_data:\n  caddy_config:\n"
    return body


def render_caddyfile(node: dict) -> str:
    p = node_ports(node)
    root = (node.get("root_domain") or "").strip().strip(".").lower()
    return f"""{{
	admin 127.0.0.1:2019
	auto_https off
}}

# frpc 的 wss 握手走这里 → frps 的 bindPort
{root}:{p['public']} {{
	tls /certs/cert.pem /certs/key.pem
	reverse_proxy 127.0.0.1:{p['bind']}
}}

# frpc 的 wss 握手（frp.<root>，客户端默认连这个；缺了会被通配站点抢走）
frp.{root}:{p['public']} {{
	tls /certs/cert.pem /certs/key.pem
	reverse_proxy 127.0.0.1:{p['bind']}
}}

# 各容器的 HTTP 隧道 → frps 的 vhost HTTP 端口（Host 头必须原样带）
*.{root}:{p['public']} {{
	tls /certs/cert.pem /certs/key.pem
	reverse_proxy 127.0.0.1:{p['vhost']} {{
		header_up Host {{host}}
		flush_interval -1
	}}
}}
"""


def self_signed_pem(root: str) -> tuple[str, str]:
    """占位证书：Caddy 起来时真证书可能还没签好，没有文件它会直接起不来。"""
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"*.{root}")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(f"*.{root}"), x509.DNSName(root)]), False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


# --------------------------------------------------------------------- SSH
class Ssh:
    """薄薄一层 paramiko 包装：run() 返回 (rc, out)，失败抛 NodeError。"""

    def __init__(self, node: dict, timeout: float = 12.0):
        try:
            import paramiko  # noqa: F401
        except Exception as e:  # pragma: no cover
            raise NodeError("镜像里缺少 paramiko，无法 SSH（请升级镜像）") from e
        self.node = node
        self.timeout = timeout
        self.cli = None

    def __enter__(self):
        import paramiko

        host = (self.node.get("host") or "").strip()
        port = int(self.node.get("ssh_port") or 22)
        user = (self.node.get("ssh_user") or "root").strip()
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(hostname=host, port=port, username=user,
                  timeout=self.timeout, banner_timeout=self.timeout,
                  auth_timeout=self.timeout, allow_agent=False, look_for_keys=False)
        key = (self.node.get("key") or "").strip()
        pwd = self.node.get("password") or ""
        try:
            if key:
                pkey = None
                for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
                    try:
                        pkey = cls.from_private_key(io.StringIO(key))
                        break
                    except Exception:
                        continue
                if pkey is None:
                    raise NodeError("私钥解析失败：只支持 OpenSSH 格式的 ed25519 / ecdsa / rsa 私钥")
                kw["pkey"] = pkey
            else:
                kw["password"] = pwd
            cli.connect(**kw)
        except NodeError:
            raise
        except Exception as e:
            raise NodeError(f"SSH 连接失败（{user}@{host}:{port}）：{e}") from e
        self.cli = cli
        return self

    def __exit__(self, *exc):
        try:
            if self.cli:
                self.cli.close()
        except Exception:
            pass

    def run(self, cmd: str, timeout: float = 120.0) -> tuple[int, str]:
        if not self.cli:
            raise NodeError("SSH 未连接")
        _in, out, err = self.cli.exec_command(cmd, timeout=timeout, get_pty=False)
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        return rc, (o + ("\n" + e if e.strip() else "")).strip()

    def put(self, remote: str, content: str, mode: int = 0o644) -> None:
        if not self.cli:
            raise NodeError("SSH 未连接")
        sftp = self.cli.open_sftp()
        try:
            tmp = remote + ".tmp"
            with sftp.file(tmp, "w") as f:
                f.write(content)
            sftp.chmod(tmp, mode)
            try:
                sftp.remove(remote)
            except Exception:
                pass
            sftp.rename(tmp, remote)
        finally:
            sftp.close()


def _step(log: list, name: str, ok: bool, msg: str = "") -> None:
    log.append({"step": name, "ok": bool(ok), "msg": msg})


# --------------------------------------------------------------------- 体检
def preflight(node: dict) -> dict:
    """连上去看清楚现状：Docker 有没有、端口占没占、架构对不对。"""
    p = node_ports(node)
    out = {"ok": True, "checks": [], "host_ip": "", "os": "", "arch": "", "docker": "", "compose": ""}

    def chk(name, ok, msg):
        out["checks"].append({"name": name, "ok": bool(ok), "msg": msg})
        if not ok:
            out["ok"] = False

    with Ssh(node) as sh:
        rc, o = sh.run("uname -srm; cat /etc/os-release 2>/dev/null | grep -E '^PRETTY_NAME' | head -1")
        out["arch"] = (o.split("\n")[0] if o else "").strip()
        m = re.search(r"PRETTY_NAME=\"?([^\"\n]+)", o or "")
        out["os"] = m.group(1).strip() if m else (o or "").strip()
        chk("SSH 登录", rc == 0, f"{out['os'] or out['arch']}")

        rc, o = sh.run("id -u")
        chk("权限", (o or "").strip() in ("0",) or True,
            "root" if (o or "").strip() == "0" else f"非 root（uid {o.strip()}），需要 docker 组权限或 sudo")

        rc, o = sh.run("docker -v 2>&1 | head -1")
        out["docker"] = (o or "").strip()
        chk("Docker", rc == 0 and "version" in out["docker"].lower(), out["docker"] or "未安装 Docker")

        rc, o = sh.run("docker compose version 2>&1 | head -1")
        out["compose"] = (o or "").strip()
        chk("docker compose", rc == 0, out["compose"] or "缺少 compose 插件（v2）")

        rc, o = sh.run("curl -fsS -m 8 https://api.ipify.org 2>/dev/null || curl -fsS -m 8 https://ifconfig.me 2>/dev/null")
        out["host_ip"] = (o or "").strip().split("\n")[-1].strip()
        if not out["host_ip"]:
            # 机房里取不到外网（或 ipify 被墙）时，回落到填的地址，DNS 才能自动写
            out["host_ip"] = (node.get("host") or "").strip()
            if out["host_ip"]:
                chk("公网 IP", True, out["host_ip"] + "（机器内取不到外网，用填写的地址）")
            else:
                chk("公网 IP", False, "取不到公网 IP（不影响部署，但 DNS 记录要手动加）")
        else:
            chk("公网 IP", True, out["host_ip"])

        # 已有 navpage-node 容器说明之前部署过，重部署是覆盖，不算错
        rc, o = sh.run("docker ps -a --filter name=navpage-node- --format '{{.Names}} {{.Status}}'")
        out["existing"] = (o or "").strip()

        rc, o = sh.run("ss -lntH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -u | tr '\\n' ' '")
        used = set((o or "").split())
        need = [p["bind"], p["vhost"]]
        if p["mode"] == "caddy":
            need += [p["public"]]
        busy = [str(x) for x in need if str(x) in used]
        ours = "navpage-node" in out["existing"]
        if busy and ours:
            chk("端口空闲", True, "已被本工具上次部署的节点容器占用：" + ", ".join(busy) + "（重复部署会重建，正常）")
        else:
            chk("端口空闲", not busy,
                "占用中：" + ", ".join(busy) if busy else
                "需要 " + ", ".join(str(x) for x in sorted(set(need))) + " 都空闲（已占用：" + (", ".join(sorted(used)) or "无") + "）")
        rc, o = sh.run(f"test -d {NODE_DIR} && echo yes || echo no")
        out["has_dir"] = (o or "").strip() == "yes"
    out["ports"] = p
    return out


# --------------------------------------------------------------------- 部署
def deploy(node: dict, log_fn=None) -> dict:
    """一键部署。返回 {"ok", "steps", "suggest", "host_ip"}。"""
    log: list[dict] = []
    say = log_fn or (lambda *_: None)
    p = node_ports(node)
    root = (node.get("root_domain") or "").strip().strip(".").lower()
    if not root:
        raise NodeError("必须填「FRP 域名后缀」，例如 frp.example.com（卡片地址 = 容器名.这个后缀）")

    with Ssh(node) as sh:
        # 仪表盘端口：默认 7500；同机已经有 frps（比如 1Panel 装的那个）就会撞，自动往后挑
        rc, o = sh.run("ss -lntH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -u | tr '\\n' ' '")
        used = set((o or "").split())
        dash = int(node.get("dashboard_port") or 0)
        if not dash or str(dash) in used:
            dash = next((x for x in range(7500, 7600) if str(x) not in used), 7500)
            node["dashboard_port"] = dash
            if log:
                _step(log, "仪表盘端口", True, f"{dash}（7500 被占用，已自动改用 {dash}）")

        # 端口被别人的服务占着时早失败，别等容器起来再靠日志猜
        rc, o = sh.run("docker ps -a --filter name=navpage-node- --format '{{.Names}}'")
        ours = "navpage-node" in (o or "")
        need = [p["bind"], p["vhost"]] + ([p["public"]] if p["mode"] == "caddy" else [])
        busy = [str(x) for x in need if str(x) in used]
        if busy and not ours:
            raise NodeError("端口被占用：" + ", ".join(busy) +
                            "。换一个端口再部署（vhost/HTTPS 端口或 frps 端口），或先停掉占用它的服务。")

        rc, o = sh.run("mkdir -p %s/certs && echo ok" % NODE_DIR)
        _step(log, "创建目录", rc == 0, NODE_DIR)
        say("目录就绪")

        rc, o = sh.run("docker -v >/dev/null 2>&1 && echo ok")
        if rc != 0:
            raise NodeError("目标服务器没有 Docker，请先安装 Docker（https://get.docker.com）")

        files = {f"{NODE_DIR}/frps.toml": (render_frps(node), 0o600),
                 f"{NODE_DIR}/docker-compose.yml": (render_compose(node), 0o644)}
        if p["mode"] == "caddy":
            files[f"{NODE_DIR}/Caddyfile"] = (render_caddyfile(node), 0o644)
            c, k = self_signed_pem(root)
            files[f"{NODE_DIR}/certs/cert.pem"] = (c, 0o644)
            files[f"{NODE_DIR}/certs/key.pem"] = (k, 0o600)
        for path, (content, mode) in files.items():
            sh.put(path, content, mode)
        _step(log, "上传配置", True, "、".join(os.path.basename(x) for x in files))
        say("配置已上传")

        # --force-recreate：只改了挂载进去的配置（端口、token、Caddyfile）时，
        # compose 认为服务没变不会重建容器，新配置就白写了。
        cmd = (f"cd {NODE_DIR} && docker compose -p {COMPOSE_PROJECT} up -d "
               f"--force-recreate --remove-orphans 2>&1 | tail -20")
        rc, o = sh.run(cmd, timeout=600)
        _step(log, "拉起容器", rc == 0, (o or "").replace("\n", " ")[:400])
        if rc != 0:
            raise NodeError("docker compose 启动失败：" + (o or "")[:400])
        say("容器已启动，等 5 秒做回环验证")

        time.sleep(5)
        rc, o = sh.run("docker ps --filter name=navpage-node- --format '{{.Names}}:{{.Status}}'")
        _step(log, "容器状态", "Up" in (o or ""), (o or "").replace("\n", " | "))

        # frps 活着 → vhost 端口对任意 Host 都回 404（没有匹配的隧道）
        code = ""
        for _i in range(15):
            rc, o = sh.run(f"curl -s -o /dev/null -w '%{{http_code}}' -m 6 http://127.0.0.1:{p['vhost']}/__navpage_probe")
            code = (o or "").strip()
            if rc == 0 and code:
                break
            time.sleep(2)
        _step(log, "frps vhost 应答", rc == 0 and bool(code), f"HTTP {code}")
        if not (rc == 0 and code):
            rc2, logs = sh.run("docker logs --tail 15 navpage-node-frps 2>&1")
            raise NodeError("frps 没有响应：" + (logs or "无日志")[-500:])
        say("frps 已应答")

        if p["mode"] == "caddy":
            # 必须用真域名访问：直接连 IP 时 SNI 是 IP，Caddy 找不到匹配站点会直接 TLS alert
            code = ""
            for _i in range(10):
                rc, o = sh.run(f"curl -sk -o /dev/null -w '%{{http_code}}' -m 8 "
                               f"--resolve probe.{root}:{p['public']}:127.0.0.1 "
                               f"https://probe.{root}:{p['public']}/__navpage_probe")
                code = (o or "").strip()
                if rc == 0 and code and code != "000":
                    break
                time.sleep(2)
            _step(log, "Caddy TLS 应答", rc == 0 and code not in ("", "000"),
                  f"HTTP {code}（占位自签证书阶段也算通过）")
            rc, o = sh.run("docker logs navpage-node-caddy 2>&1 | tail -5")
            if rc == 0 and "error" in (o or "").lower():
                _step(log, "Caddy 日志", False, (o or "").replace("\n", " ")[:300])

        # 防火墙：只处理本机防火墙，云厂商安全组得用户自己去点
        fw_note = ""
        try:
            rc, o = sh.run("command -v ufw >/dev/null && ufw status 2>/dev/null | head -1")
            if "active" in (o or "").lower():
                ports = [p["bind"], p["vhost"]] if p["mode"] == "http" else [443, 80]
                for port in sorted(set(str(x) for x in ports)):
                    sh.run(f"ufw allow {port}/tcp >/dev/null 2>&1")
                if p["mode"] == "caddy":
                    sh.run("ufw allow %d/tcp >/dev/null 2>&1" % p["bind"])
                fw_note = "已放行 ufw：" + ", ".join(sorted(set(str(x) for x in ports + [p["bind"]])))
                _step(log, "本机防火墙", True, fw_note)
            rc, o = sh.run("command -v firewall-cmd >/dev/null && firewall-cmd --state 2>/dev/null")
            if "running" in (o or "").lower():
                ports = [p["bind"], p["vhost"]] if p["mode"] == "http" else [p["bind"], 80, 443]
                for port in sorted(set(str(x) for x in ports)):
                    sh.run(f"firewall-cmd --permanent --add-port={port}/tcp >/dev/null 2>&1")
                sh.run("firewall-cmd --reload >/dev/null 2>&1")
                _step(log, "本机防火墙", True, "已放行 firewalld 端口")
        except Exception as e:  # 防火墙不是关键路径
            _step(log, "本机防火墙", False, f"检测失败（不影响，手动放行端口即可）：{e}")

        # 云厂商安全组改不了，只能提醒（Oracle/AWS/阿里云都拦在 VPC 层，本机 ufw 开了也没用）
        need_pub = sorted({p["bind"], p["public"]} if p["mode"] == "caddy" else {p["bind"], p["vhost"]})  # caddy: 7000 给 frpc，443 给站点
        _step(log, "云安全组", True,
              "安全组要放行 TCP " + "/".join(str(x) for x in need_pub) + "（云控制台那条入站规则，本工具改不了）")

        rc, o = sh.run("curl -fsS -m 8 https://api.ipify.org 2>/dev/null || curl -fsS -m 8 https://ifconfig.me 2>/dev/null")
        host_ip = (o or "").strip().split("\n")[-1].strip()

    return {"ok": True, "steps": log, "host_ip": host_ip, "ports": p,
            "node_patch": {"dashboard_port": int(node.get("dashboard_port") or 7500)},
            "suggest": suggest_client(node, host_ip), "root_domain": root}


def client_endpoint(node: dict) -> dict:
    """frpc 该往哪连、用什么协议。

    direct：直连 frps 的 bindPort（裸 VPS + 自己放行防火墙，最省事）。
    proxy ：frps 藏在已有的 nginx / OpenResty / 1Panel 后面，公网只开 443，
            客户端走 wss:443 由反代转给 bindPort（本机 7000 不出网）。
            VPS 安全组不放行 7000、或者 443 是唯一出网端口时用这个。
    """
    p = node_ports(node)
    root = (node.get("root_domain") or "").strip().strip(".").lower()
    if (node.get("client_mode") or "direct").lower() == "proxy":
        return {"host": (node.get("client_host") or (f"frp.{root}" if root else "")).strip(),
                "port": int(node.get("client_port") or 443),
                "protocol": (node.get("client_protocol") or "wss").strip().lower(),
                "scheme": "https"}
    return {"host": (node.get("host") or "").strip(), "port": p["bind"],
            "protocol": "tcp", "scheme": "http" if p["mode"] == "http" else "https"}


def suggest_client(node: dict, host_ip: str = "") -> dict:
    """部署完该往「设置 → FRP」里填什么。"""
    p = node_ports(node)
    root = (node.get("root_domain") or "").strip().strip(".").lower()
    base = {"frp_token": node.get("token") or "", "frp_domain": root, "frp_subdomain_host": "",
            "frp_name_prefix": node.get("name_prefix") or "np-", "frp_enabled": True}
    ep = client_endpoint(node)
    # vhost 端口 = 浏览器实际访问的端口，只影响「卡片地址要不要带 :端口」。
    #   direct + http   → 浏览器直连 frps 的 vhost 端口
    #   direct + caddy  → Caddy 占 443，卡片就是 https://x.root
    #   proxy           → 公网那层由既有反代终结 TLS，同样落在 443
    if ep["scheme"] == "https":
        vh_http = 80
        # caddy 形态下浏览器访问的 HTTPS 端口就是 Caddy 的公网端口（443 或自定义 8443 等），
        # 跟 frpc 用 tcp 还是 wss 连无关——之前只认 tcp，改端口后卡片地址就漏了 :端口。
        vh_https = p["public"] if p["mode"] == "caddy" else 443
    else:
        vh_http = vh_https = p["vhost"]
    return {**base, "frp_server": ep["host"], "frp_port": ep["port"],
            "frp_protocol": ep["protocol"], "frp_scheme": ep["scheme"],
            "frp_vhost_http_port": vh_http, "frp_vhost_https_port": vh_https}


def sync_cert(node: dict, svc, force: bool = False) -> dict:
    """签（或复用）`*.<root>` 通配证书，推到节点并 reload Caddy。"""
    log: list[dict] = []
    root = (node.get("root_domain") or "").strip().strip(".").lower()
    p = node_ports(node)
    if p["mode"] != "caddy":
        return {"ok": True, "steps": [{"step": "证书", "ok": True, "msg": "http 模式不需要证书"}], "skipped": True}
    if not root:
        raise NodeError("没填域名后缀，无法签证书")

    domains = [f"*.{root}", root]
    res = svc.issue_cert_sync(domains, name=safe_cert_name(root), force=force)
    _step(log, "签发通配证书", bool(res.get("ok")), res.get("error") or
          ("复用现有证书（剩 %s 天）" % res.get("days_left") if res.get("skipped") else "新签发，剩 %s 天" % res.get("days_left")))
    if not res.get("ok"):
        return {"ok": False, "steps": log}

    cp, kp = svc.certs.cert_paths(res["name"])
    try:
        cert_pem = open(cp, encoding="utf-8").read()
        key_pem = open(kp, encoding="utf-8").read()
    except Exception as e:
        raise NodeError(f"读本地证书失败：{e}") from e

    pushed = push_cert(node, cert_pem, key_pem)
    log.extend(pushed["steps"])
    return {"ok": pushed["ok"], "steps": log, "domains": domains, "not_after": res.get("not_after")}


def push_cert(node: dict, cert_pem: str, key_pem: str) -> dict:
    """把证书文件推到节点并让 Caddy 重新加载（不负责签发）。"""
    p = node_ports(node)
    root = (node.get("root_domain") or "").strip().strip(".").lower()
    log: list[dict] = []
    with Ssh(node) as sh:
        sh.put(f"{NODE_DIR}/certs/cert.pem", cert_pem, 0o644)
        sh.put(f"{NODE_DIR}/certs/key.pem", key_pem, 0o600)
        _step(log, "上传证书", True, f"{root} / *.{root}")
        # 宿主机文件变了，容器里的 bind mount 会跟着变，reload 一下让 Caddy 重新读
        rc, o = sh.run("docker exec navpage-node-caddy caddy reload --force "
                       "--config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -3")
        _step(log, "Caddy reload", rc == 0, (o or "").replace("\n", " ")[:200])
        rc, o = sh.run(f"echo | openssl s_client -connect 127.0.0.1:{p['public']} -servername {root} 2>/dev/null "
                       "| openssl x509 -noout -issuer -dates")
        _step(log, "实测线上证书", "CN" in (o or "") or "notAfter" in (o or ""), (o or "").replace("\n", " ")[:200])
    return {"ok": all(s["ok"] for s in log), "steps": log}


def safe_cert_name(root: str) -> str:
    return "frpnode-" + re.sub(r"[^a-z0-9.-]+", "-", root.lower()).strip("-")


# --------------------------------------------------------------------- DNS
def ensure_wildcard_dns(node: dict, ip: str) -> dict:
    """把 `*.<root>`（和裸域）A 记录补到节点 IP。"""
    from . import dnsprov
    from .. import db

    root = (node.get("root_domain") or "").strip().strip(".").lower()
    if not root or not ip:
        return {"ok": True, "skip": True, "msg": "缺域名或 IP，跳过 DNS（手动加一条 *.<后缀> A 记录即可）"}
    st = db.get_settings()
    prov_name, creds = st.get("dns_provider") or "", st.get("dns_creds") or {}
    if not prov_name:
        return {"ok": True, "skip": True,
                "msg": f"没配 DNS 服务商（设置 → 证书），跳过自动加记录：请手动把 *.{root} 和 {root} 的 A 记录指向 {ip}"}
    prov = dnsprov.get_provider(prov_name, creds)
    out = {}
    for fqdn in (f"*.{root}", root):
        try:
            out[fqdn] = prov.upsert_a(fqdn, ip, ttl=600)
        except Exception as e:
            out[fqdn] = f"失败：{e}"
    ok = all(not str(v).startswith("失败") for v in out.values())
    return {"ok": ok, "records": out}


def check_dns(node: dict) -> dict:
    """解析 `*.<root>` 看是否已经指到节点。"""
    root = (node.get("root_domain") or "").strip().strip(".").lower()
    if not root:
        return {"ok": False, "msg": "没填域名后缀"}
    try:
        ips = {ai[4][0] for ai in socket.getaddrinfo("probe." + root, None)}
    except Exception as e:
        return {"ok": False, "msg": f"解析失败：{e}", "ips": []}
    return {"ok": True, "ips": sorted(ips)}


def is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except Exception:
        return False
