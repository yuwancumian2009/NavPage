"""Gately 反代面板联动。

为什么单独做一层（2026-10-25 用户提的联动需求）：
  * Lucky 的「Web 服务」规则被它自己加密（v2l 通道 / .lkcf 密文），第三方读不出来，
    所以早期只能靠用户复制粘贴规则再解析；现在已改为走 Gately 的公开 REST API，粘贴导入功能已移除。
  * Gately 提供公开的 REST API + 按模块授权的 API 令牌，`/api/webservice/listeners`
    直接返回「监听端口 → 站点（域名）→ 后端 targets」的明文结构，因此可以做到：
      ① 读：把现成的反代规则映射回卡片的外网地址；
      ② 写：给容器卡片新建/更新一条反代规则，地址自动写回卡片。

约定：一个 Listener = 一个监听端口（通常 443）；Listener.sites[] = 按域名的站点，
每个站点有 domains（域名列表）和 targets（后端，如 http://127.0.0.1:8096）。

只依赖标准库，方便单测。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class GatelyError(Exception):
    """调用 Gately 失败（网络、鉴权、接口错误都归到这里，调用方直接展示 message）。"""


def split_target(target: str) -> tuple[str, int, str]:
    """把 `http://127.0.0.1:8096` 拆成 (host, port, scheme)。解析不出来时 port=0。"""
    t = (target or "").strip()
    if not t:
        return "", 0, ""
    if "://" not in t:
        t = "http://" + t
    u = urllib.parse.urlsplit(t)
    host = (u.hostname or "").strip()
    scheme = (u.scheme or "http").lower()
    port = u.port or (443 if scheme == "https" else 80)
    return host, int(port), scheme


def rows_from_listeners(listeners) -> list[dict]:
    """把 Gately 的 listeners 摊平成「一个域名一行」，方便和卡片匹配与预览。"""
    out: list[dict] = []
    for lst in listeners or []:
        lport = int(lst.get("port") or 0)
        ltls = bool(lst.get("tls"))
        for site in (lst.get("sites") or []):
            for domain in (site.get("domains") or []):
                d = (domain or "").strip().lower()
                if not d:
                    continue
                targets = site.get("targets") or []
                host, port, _ = split_target(targets[0]) if targets else ("", 0, "")
                out.append({
                    "domain": d,
                    "scheme": "https" if ltls else "http",
                    "backend_host": host,
                    "backend_port": port,
                    "backend_all": targets,
                    "label": site.get("name") or "",
                    "site_name": site.get("name") or "",
                    "site_type": site.get("type") or "",
                    "listener_port": lport,
                    "listener_key": lst.get("key") or "",
                    "site_key": site.get("key") or "",
                    "site_enable": bool(site.get("enable", True)),
                    "listener_enable": bool(lst.get("enable", True)),
                })
    return out


class Client:
    """最小可用的 Gately API 客户端（bearer 令牌）。"""

    def __init__(self, base: str, token: str, timeout: float = 8.0):
        base = (base or "").strip().rstrip("/")
        if base and not base.lower().startswith(("http://", "https://")):
            base = "http://" + base
        self.base = base
        self.token = (token or "").strip()
        self.timeout = timeout
        # 容器里访问内网地址别走 http_proxy（NAS 上装过代理很容易踩这个坑）
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, body=None):
        if not self.base:
            raise GatelyError("未配置 Gately 地址")
        if not self.token:
            raise GatelyError("未配置 API 令牌")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "ignore").strip()
            if e.code == 401:
                raise GatelyError("令牌无效或已过期（401）")
            if e.code == 403:
                raise GatelyError("令牌缺少权限（403）：%s" % detail)
            raise GatelyError("HTTP %s：%s" % (e.code, detail or e.reason))
        except urllib.error.URLError as e:
            raise GatelyError("连不上 Gately：%s" % (getattr(e, "reason", e),))
        except Exception as e:  # 超时等
            raise GatelyError("%s：%s" % (type(e).__name__, e))
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise GatelyError("返回的不是 JSON：%s" % raw[:120])

    # ---- 读
    def info(self) -> dict:
        return self._call("GET", "/api/system/info")

    def listeners(self) -> list:
        data = self._call("GET", "/api/webservice/listeners")
        return data if isinstance(data, list) else []

    # ---- 写
    def create_listener(self, payload: dict) -> dict:
        return self._call("POST", "/api/webservice/listeners", payload)

    def update_listener(self, key: str, payload: dict) -> dict:
        return self._call("PUT", "/api/webservice/listeners/%s" % key, payload)


def new_listener_payload(port: int, tls: bool, name: str = "") -> dict:
    return {
        "name": name or ("web-%d" % port),
        "listenIp": "0.0.0.0",
        "port": int(port),
        "tls": bool(tls),
        "http3": False,
        "enable": True,
        "sites": [],
    }


# Listener 里只有这些字段可写；GET 会额外带 running / stats / upstreams，
# 原样 PUT 回去会被拒（实测 400 unknown field "running"）。
_LISTENER_WRITABLE = ("name", "listenIp", "port", "tls", "http3", "minTls",
                      "network", "acceptProxyProtocol", "enable", "remark", "default", "sites")
_SITE_DROP = ("stats", "running", "upstreams")


def listener_payload(lst: dict, sites=None) -> dict:
    """从 GET 回来的 listener 生成可 PUT 的 body（剔掉只读字段，保留用户已有配置）。"""
    out = {k: lst[k] for k in _LISTENER_WRITABLE if k in lst}
    if sites is not None:
        out["sites"] = [{k: v for k, v in (s or {}).items() if k not in _SITE_DROP} for s in sites]
    else:
        out["sites"] = [{k: v for k, v in (s or {}).items() if k not in _SITE_DROP}
                        for s in (lst.get("sites") or [])]
    return out


def _next_site_key(sites) -> str:
    used = {str(s.get("key") or "") for s in (sites or [])}
    i = 1
    while ("s%d" % i) in used:
        i += 1
    return "s%d" % i


def ensure_site(client: Client, port: int, tls: bool, domain: str, target: str,
                name: str = "") -> dict:
    """保证「domain → target」这条反代存在，返回 {action, listener}。

    action ∈ created / updated / unchanged；重复调用是幂等的（同域名多后端会并进去）。
    """
    domain = (domain or "").strip().lower()
    if not domain:
        raise GatelyError("域名为空")
    host, tport, _ = split_target(target)
    if not host or not tport:
        raise GatelyError("后端地址无效：%s" % target)
    target = target if "://" in target else "http://" + target

    listeners = client.listeners()
    lst = None
    for item in listeners:
        if int(item.get("port") or 0) == int(port):
            lst = item
            break

    action = "unchanged"
    if lst is None:
        # 没有监听端口 → 建一个（先不带站点，拿到 key 再补站点）
        lst = client.create_listener(new_listener_payload(port, tls))
        action = "created"

    sites = list(lst.get("sites") or [])
    hit = None
    for s in sites:
        if domain in [(d or "").strip().lower() for d in (s.get("domains") or [])]:
            hit = s
            break

    if hit is None:
        sites.append({
            "key": _next_site_key(sites),
            "name": name or domain.split(".")[0],
            "enable": True,
            "domains": [domain],
            "type": "proxy",
            "targets": [target],
            "keepPath": True,
        })
        action = "created" if action == "unchanged" else action
    else:
        targets = list(hit.get("targets") or [])
        if target not in targets:
            hit = dict(hit)
            hit["targets"] = targets + [target]
            for i, s in enumerate(sites):
                if (s.get("key") or "") == (hit.get("key") or ""):
                    sites[i] = hit
            action = "updated"

    if action != "unchanged":
        payload = listener_payload(lst, sites)
        key = lst.get("key") or ""
        kept = key
        lst = client.update_listener(kept, payload) if kept else client.create_listener(payload)
    return {"action": action, "listener": lst, "domain": domain, "target": target}


def remove_site(client: Client, listener_key: str, site_key: str) -> dict:
    """删掉一条反代站点（按站点 key）。找不到就原样返回。"""
    for item in client.listeners():
        if (item.get("key") or "") != listener_key:
            continue
        sites = [s for s in (item.get("sites") or []) if (s.get("key") or "") != site_key]
        return client.update_listener(listener_key, listener_payload(item, sites))
    return {}
