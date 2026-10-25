"""网关编排：把反代、证书、DDNS、FRP 四件事串起来，并和卡片数据保持一致。

一条核心原则：**卡片就是规则**。
  * 卡片的 `proxy_domain` 是反代域名，`url_lan` 是后端地址，改卡片 → 路由立刻变；
  * 卡片没有 proxy_domain 时，按 `<容器名>.<根域名>` 自动推；
  * FRP 的 proxy 列表也由卡片推，所以不会出现「卡片删了隧道还在」的幽灵规则。

证书签发走 DNS-01 + 通配符：一张 `*.example.com` 覆盖所有卡片，加新卡片不用重新签，
也不会因为签发频率撞 Let's Encrypt 的每周限额（同一域名 5 张/周）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import struct
import threading
import time
from urllib.parse import urlsplit

from . import acme as acme_mod
from . import ddns as ddns_mod
from . import dnsprov, frpc
from .certs import CertStore, safe_name
from .proxy import ProxyEngine


def default_gateway_ip() -> str:
    """容器默认网关 = 宿主机在 docker bridge 上的地址。

    从容器里访问「宿主机发布的端口」，走网关 IP 最稳（172.17.0.1:8096 这种），
    不需要知道 NAS 的 LAN IP，也不受 navpage 自己跑在哪个网络影响。
    """
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) < 3:
                    continue
                if parts[1] == "00000000":      # destination 0.0.0.0
                    gw = int(parts[2], 16)
                    return socket.inet_ntoa(struct.pack("<L", gw))
    except Exception:
        pass
    return ""


def slugify(text: str, fallback: str = "") -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:40] or fallback


def url_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def url_port(url: str) -> int:
    try:
        u = urlsplit(url)
        if u.port:
            return u.port
        return 443 if u.scheme == "https" else 80
    except Exception:
        return 0


class Gateway:
    def __init__(self, data_dir: str, cfg_fn, items_fn, log_fn=None):
        self.data_dir = data_dir
        self.cfg_fn = cfg_fn
        self.items_fn = items_fn
        self.logs: list[str] = []
        self._log_fn = log_fn
        self.certs = CertStore(data_dir, log=self.log)
        self.proxy = ProxyEngine(self.routes, self.certs, log=self.log)
        self.frp = frpc.FrpManager(data_dir, log=self.log)
        self.ddns = ddns_mod.DdnsManager(data_dir, cfg_fn, log=self.log)
        self.started = False
        self._issue_lock = threading.Lock()
        self._renew_task: asyncio.Task | None = None
        self._last_issue: dict[str, dict] = {}

    # ---------------------------------------------------------------- 日志
    def log(self, msg: str):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.logs.insert(0, line)
        del self.logs[120:]
        try:
            if self._log_fn:
                self._log_fn(msg)
        except Exception:
            pass

    # ---------------------------------------------------------------- 配置推导
    def cfg(self) -> dict:
        return self.cfg_fn() or {}

    def target_host(self) -> str:
        return (self.cfg().get("gw_target_host") or "").strip() or default_gateway_ip() or "127.0.0.1"

    def item_target(self, item: dict) -> tuple[str, int]:
        """从卡片的 url_lan 推出 (后端主机, 端口)。

        优先用卡片里写死的 IP/域名；模板 `{host}` 一律换成「容器看宿主机的地址」。
        """
        raw = (item.get("url_lan") or "").strip()
        if not raw:
            return "", 0
        raw = raw.replace("{scheme}://", "http://").replace("{host}", self.target_host())
        try:
            u = urlsplit(raw if "://" in raw else "http://" + raw)
        except Exception:
            return "", 0
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        if not host or host in ("localhost", "127.0.0.1"):
            host = self.target_host()
        return host, int(port)

    def upstream_scheme(self, item: dict, port: int) -> str:
        forced = (self.cfg().get("gw_upstream_scheme") or "").strip().lower()
        raw = (item.get("url_lan") or "").lower()
        if "https://" in raw:
            return "https"
        if forced in ("http", "https"):
            return forced
        return "https" if port in (443, 8443) else "http"

    def domain_for(self, item: dict) -> str:
        d = (item.get("proxy_domain") or "").strip().lower()
        if d:
            return d
        root = (self.cfg().get("gw_root_domain") or "").strip().strip(".").lower()
        if not root:
            return ""
        base = slugify(item.get("container_name") or "") or slugify(item.get("name") or "") or f"app-{item.get('id')}"
        return f"{base}.{root}"

    def frp_domain_for(self, item: dict) -> str:
        cfg = self.cfg()
        base = slugify(item.get("container_name") or "") or slugify(item.get("name") or "") or f"app-{item.get('id')}"
        sub_host = (cfg.get("frp_subdomain_host") or "").strip().strip(".").lower()
        if sub_host:
            return f"{base}.{sub_host}"
        root = (cfg.get("frp_domain") or "").strip().strip(".").lower()
        return f"{base}.{root}" if root else ""

    def frp_url_for(self, item: dict) -> str:
        host = (item.get("url_frp") or "").strip()
        if host:
            return host
        d = self.frp_domain_for(item)
        if not d:
            return ""
        cfg = self.cfg()
        scheme = (cfg.get("frp_scheme") or "http").lower()
        port = int(cfg.get("frp_vhost_https_port" if scheme == "https" else "frp_vhost_http_port") or (443 if scheme == "https" else 80))
        suffix = "" if (scheme == "https" and port == 443) or (scheme == "http" and port == 80) else f":{port}"
        return f"{scheme}://{d}{suffix}"

    # ---------------------------------------------------------------- 路由表
    def routes(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if not self.cfg().get("gw_enabled"):
            return out
        for it in self.items_fn() or []:
            host, port = self.item_target(it)
            domain = self.domain_for(it)
            if not (host and port and domain):
                continue
            scheme = self.upstream_scheme(it, port)
            out[domain] = f"{scheme}://{host}:{port}"
        return out

    def frp_proxies(self) -> list[dict]:
        out = []
        used: set[str] = set()
        cfg = self.cfg()
        sub_host = (cfg.get("frp_subdomain_host") or "").strip()
        for it in self.items_fn() or []:
            host, port = self.item_target(it)
            if not (host and port):
                continue
            d = self.frp_domain_for(it)
            if not d:
                continue
            base = d.split(".")[0]
            # proxy 名必须唯一：同一个容器可能被建了两张卡片（比如内外网分开），
            # 重名会让 frpc 直接报 "proxy name already exists" 并整份配置失效。
            prefix = (cfg.get("frp_name_prefix") or "").strip()
            name = slugify(it.get("container_name") or "") or f"app-{it.get('id')}"
            name = f"{prefix}{name}"
            if name in used:
                name = f"{name}-{it.get('id')}"
            used.add(name)
            p = {"name": name, "local_ip": host, "local_port": port}
            if sub_host:
                p["subdomain"] = base
            else:
                p["domain"] = d
            out.append(p)
        return out

    # ---------------------------------------------------------------- 生命周期
    async def start(self):
        if self.started:
            return
        self.started = True
        cfg = self.cfg()
        if cfg.get("gw_enabled"):
            await self.proxy.start(int(cfg.get("gw_http_port") or 8081), int(cfg.get("gw_https_port") or 0))
        self.frp.apply(cfg, self.frp_proxies())
        if cfg.get("ddns_enabled"):
            self.ddns.start()
        self._renew_task = asyncio.create_task(self._renew_loop())
        self.log("网关服务已就绪")

    async def stop(self):
        self.started = False
        await self.proxy.stop()
        await self.ddns.stop()
        self.frp.stop()
        if self._renew_task:
            self._renew_task.cancel()
            try:
                await self._renew_task
            except BaseException:      # CancelledError 是 BaseException，别用 Exception 吞
                pass
            self._renew_task = None

    async def reload_all(self):
        """设置改完调这个：监听端口、路由、frpc 全部按新配置重来。"""
        cfg = self.cfg()
        await self.proxy.stop()
        if cfg.get("gw_enabled"):
            await self.proxy.start(int(cfg.get("gw_http_port") or 8081), int(cfg.get("gw_https_port") or 0))
        if cfg.get("ddns_enabled"):
            self.ddns.start()
        else:
            await self.ddns.stop()
        self.frp.apply(cfg, self.frp_proxies())
        self.log("网关已按新配置重载")

    # ---------------------------------------------------------------- 证书
    async def issue_cert(self, domains: list[str], name: str = "", force: bool = False,
                         env: str = "") -> dict:
        """签发证书（阻塞操作丢线程里跑，别卡住 FastAPI 事件循环）。"""
        domains = [d.strip().lower() for d in domains if d and d.strip()]
        if not domains:
            raise ValueError("没有域名")
        cfg = self.cfg()
        name = name or safe_name(domains)
        if not force:
            e = self.certs.find_for(name)
            if e and e.get("exists") and not e.get("expired") and e.get("days_left", 0) > 20:
                return {"ok": True, "skipped": True, "name": name,
                        "days_left": e.get("days_left"), "message": "证书还有效，跳过签发"}

        def _run():
            with self._issue_lock:
                prov = dnsprov.get_provider(cfg.get("dns_provider"), cfg.get("dns_creds") or {})
                cli = acme_mod.AcmeClient(env=env or cfg.get("gw_acme_env") or "letsencrypt",
                                          email=cfg.get("gw_acme_email") or "",
                                          data_dir=self.data_dir,
                                          directory_url=cfg.get("gw_acme_dir") or "",
                                          insecure=bool(cfg.get("gw_acme_insecure")))
                res = cli.issue(domains, challenge="dns-01", provider=prov,
                                prop_wait=float(cfg.get("gw_dns_wait") or 8),
                                log=self.log)
                self.certs.save(domains, res["cert"], res["key"], res["not_after"],
                                env=env or cfg.get("gw_acme_env") or "letsencrypt", name=name)
                return res

        try:
            res = await asyncio.to_thread(_run)
            out = {"ok": True, "name": name, "domains": domains,
                   "not_after": res["not_after"],
                   "days_left": max(0, int((res["not_after"] - time.time()) // 86400))}
            self._last_issue[name] = {**out, "at": int(time.time())}
            return out
        except Exception as e:
            self._last_issue[name] = {"ok": False, "name": name, "domains": domains,
                                      "error": str(e)[:400], "at": int(time.time())}
            return {"ok": False, "name": name, "error": str(e)[:400]}

    def issue_cert_sync(self, domains: list[str], name: str = "", force: bool = False) -> dict:
        """同步版 issue_cert：给「远程节点部署」这类线程池里的调用方用。"""
        import asyncio
        return asyncio.run(self.issue_cert(domains, name=name, force=force))

    def cert_for_domain(self, domain: str) -> dict | None:
        for e in self.certs.entries():
            for d in e.get("domains") or []:
                if d == domain or (d.startswith("*.") and domain.endswith(d[1:])):
                    return e
        return None

    async def ensure_cert_for(self, domain: str) -> dict:
        """给某个域名准备证书：优先复用通配符，没有再单独签。"""
        cfg = self.cfg()
        if not cfg.get("gw_auto_cert"):
            return {"ok": True, "skipped": True, "message": "自动证书已关闭"}
        if not domain:
            return {"ok": False, "error": "域名为空"}
        hit = self.cert_for_domain(domain)
        if hit and hit.get("exists") and not hit.get("expired") and hit.get("days_left", 0) > 20:
            return {"ok": True, "skipped": True, "name": hit["name"], "days_left": hit.get("days_left")}
        root = (cfg.get("gw_root_domain") or "").strip().strip(".").lower()
        if cfg.get("gw_wildcard", True) and root and domain.endswith("." + root):
            return await self.issue_cert([f"*.{root}", root], name=safe_name([f"*.{root}"]))
        return await self.issue_cert([domain])

    async def _renew_loop(self):
        await asyncio.sleep(30)
        while True:
            try:
                cfg = self.cfg()
                days = int(cfg.get("gw_cert_days") or 30)
                for e in self.certs.entries():
                    if not e.get("exists"):
                        continue
                    if self.certs.due_for_renew(e["name"], days):
                        self.log(f"证书 {e['name']} 剩余 {e.get('days_left')} 天，开始续期")
                        await self.issue_cert(e.get("domains") or [e["name"]], name=e["name"], force=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"续期循环异常：{e}")
            await asyncio.sleep(6 * 3600)

    # ---------------------------------------------------------------- 卡片联动
    def item_addresses(self, item: dict) -> dict:
        domain = self.domain_for(item)
        frp_url = self.frp_url_for(item)
        cfg = self.cfg()
        wan = ""
        if domain:
            scheme = (cfg.get("gw_wan_scheme") or "https").lower()
            # 端口要跟协议配套：http 用 gw_http_port，https 用 gw_https_port。
            # （2026-10-25 实测踩到：选 http 却拼了 18443，链接直接打不开）
            if scheme == "https":
                port = int(cfg.get("gw_https_port") or 443)
            else:
                port = int(cfg.get("gw_http_port") or 80)
            suffix = "" if (scheme == "https" and port == 443) or (scheme == "http" and port == 80) else f":{port}"
            wan = f"{scheme}://{domain}{suffix}"
        return {"proxy_domain": domain, "url_wan": wan, "url_frp": frp_url}

    async def sync_item(self, item: dict, issue: bool = True) -> dict:
        """给一张卡片补齐反代域名 / 外网地址 / FRP 地址。

        **只补空，不覆盖用户已有的地址**（2026-10-25 关键修正）：
        线上很多卡片的外网地址是手填的（比如 *.o.hhxin.top 走甲骨文的反代），
        如果同步时无条件重写，一开「自动反代」就会把这 70 多条全冲掉。
        只有当已有外网地址的主机名正好等于我们要生成的那个域名（说明它本来就是自动生成的）
        时，才允许更新（比如根域名改了端口）。
        """
        addr = self.item_addresses(item)
        cur_wan = (item.get("url_wan") or "").strip()
        cur_frp = (item.get("url_frp") or "").strip()
        patch = {}
        if addr["proxy_domain"] and not (item.get("proxy_domain") or "").strip():
            patch["proxy_domain"] = addr["proxy_domain"]
        if addr["url_wan"] and (not cur_wan or url_host(cur_wan) == addr["proxy_domain"]):
            if addr["url_wan"] != cur_wan:
                patch["url_wan"] = addr["url_wan"]
        if addr["url_frp"] and (not cur_frp or url_host(cur_frp) == url_host(addr["url_frp"])):
            if addr["url_frp"] != cur_frp:
                patch["url_frp"] = addr["url_frp"]
        out = {"id": item.get("id"), "patch": patch, "cert": None}
        if issue and addr["proxy_domain"]:
            out["cert"] = await self.ensure_cert_for(addr["proxy_domain"])
        return out

    async def sync_all(self, issue: bool = True) -> dict:
        items = self.items_fn() or []
        changed, certs = [], []
        for it in items:
            r = await self.sync_item(it, issue=issue)
            if r["patch"]:
                changed.append(r["patch"])
            if r.get("cert"):
                certs.append(r["cert"])
        self.frp.apply(self.cfg(), self.frp_proxies())
        return {"total": len(items), "changed": changed, "certs": certs}

    # ---------------------------------------------------------------- 状态
    def status(self) -> dict:
        cfg = self.cfg()
        certs = self.certs.entries()
        certs.sort(key=lambda e: e.get("name") or "")
        frp_st = self.frp.status()
        return {
            "enabled": bool(cfg.get("gw_enabled")),
            "http_port": int(cfg.get("gw_http_port") or 0),
            "https_port": int(cfg.get("gw_https_port") or 0),
            "root_domain": cfg.get("gw_root_domain") or "",
            "target_host": self.target_host(),
            "wan_scheme": cfg.get("gw_wan_scheme") or "https",
            "auto_proxy": bool(cfg.get("gw_auto_proxy")),
            "auto_cert": bool(cfg.get("gw_auto_cert")),
            "wildcard": bool(cfg.get("gw_wildcard", True)),
            "acme_email": cfg.get("gw_acme_email") or "",
            "acme_env": cfg.get("gw_acme_env") or "letsencrypt",
            "dns_provider": cfg.get("dns_provider") or "",
            "dns_configured": bool((cfg.get("dns_creds") or {}) and cfg.get("dns_provider")),
            "proxy": self.proxy.status(),
            "certs": certs,
            "frp": {**frp_st, "enabled": bool(cfg.get("frp_enabled")),
                    "server": cfg.get("frp_server") or "",
                    "server_port": int(cfg.get("frp_port") or 7000),
                    "domain": cfg.get("frp_subdomain_host") or cfg.get("frp_domain") or "",
                    "proxy_count": len(self.frp_proxies())},
            "ddns": self.ddns.status(),
            "route_count": len(self.routes()),
            "logs": self.logs[:40],
            "last_issue": list(self._last_issue.values())[-5:],
        }


_gw: Gateway | None = None


def configure(data_dir: str, cfg_fn, items_fn, log_fn=None) -> Gateway:
    global _gw
    _gw = Gateway(data_dir, cfg_fn, items_fn, log_fn)
    return _gw


def instance() -> Gateway | None:
    return _gw
