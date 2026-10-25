"""内置反代的 WAF（Web 应用防火墙）。

对标 Gately 的 WAF 模块：给「内置反代」每个站点做请求前置检查，命中即拦（或只记日志）。
为什么自己写而不塞 ModSecurity：宿主是纯净 aiohttp 进程，加 .so 要重打包镜像；
这里要的能力只有「IP 黑白名单 / 扫描器 UA / 敏感路径 / 请求方法 / CC 限速」+ 一条能看的日志，
几十行 Python 足够，且规则跟着卡片实时生效（不用 reload 外部引擎）。

配置都存在 settings 表里（waf_ 前缀），WAF 每次请求读一次 dict，改完下一个请求就生效。
"""
from __future__ import annotations

import ipaddress
import re
import time
from collections import deque

# 出厂基线：常见扫描器/漏洞探测工具的 UA 特征。只认明确的工具特征，不误伤正常浏览器。
DEFAULT_UA = [
    "sqlmap", "nikto", "nmap", "masscan", "zgrab", "zmap", "dirbuster", "gobuster",
    "wpscan", "acunetix", "nessus", "openvas", "fscan", "xray", "nuclei", "feroxbuster",
    "ffuf", "wfuzz", "jaeles", "commix", "havij", "netsparker", "appscan",
]

# 出厂基线：不该被公网扫到的路径。命中就是探测行为，正常业务不会访问。
DEFAULT_PATH = [
    "/.env", "/.git/", "/.svn/", "/.hg/", "/wp-admin", "/wp-login.php", "/xmlrpc.php",
    "/phpmyadmin", "/pma/", "/myadmin", "/cgi-bin/", "/etc/passwd", "/proc/self",
    "/.aws/", "/.ssh/", "/.docker/", "/actuator/", "/druid/", "/solr/", "/.DS_Store",
    "/config.php.bak", "/backup.zip", "/db.sql", "/web.config",
]

BAD_METHODS = {"TRACE", "TRACK", "DEBUG"}


def _split(raw) -> list[str]:
    """把「逗号/分号/换行/空格」分隔的规则串拆成 list，去空、去重、保序。"""
    if isinstance(raw, (list, tuple)):
        parts = []
        for x in raw:
            parts.extend(_split(x))
        return parts
    out, seen = [], set()
    for tok in re.split(r"[,;\s]+", str(raw or "")):
        t = tok.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _ip_of(request, trust_proxy: bool):
    """拿客户端 IP：不信任代理时用 socket 对端地址，信任时取 XFF 第一跳。"""
    if trust_proxy:
        for h in ("X-Real-IP", "X-Forwarded-For"):
            v = request.headers.get(h, "")
            if v:
                first = v.split(",")[0].strip()
                if first:
                    return first
    return request.remote or ""


def _ip_hit(ip: str, rules: list[str]) -> str:
    """ip 是否落在任一规则里（支持单 IP 与 CIDR）；命中返回该规则。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    for r in rules:
        try:
            if "/" in r:
                if addr in ipaddress.ip_network(r, strict=False):
                    return r
            elif addr == ipaddress.ip_address(r):
                return r
        except ValueError:
            continue      # 写错的行直接跳过，不让一条坏规则拖跨整个 WAF
    return ""


class WAF:
    def __init__(self, cfg_provider, log=None, ring: int = 200):
        self.cfg_provider = cfg_provider
        self.log = log or (lambda *a: None)
        self.ring = ring
        self.hits: list[dict] = []          # 最近拦截/观察记录，新的在前
        self.counters = {"checked": 0, "blocked": 0, "monitored": 0}
        self._cc: dict[str, deque] = {}      # ip -> 时间戳队列（CC 限速）
        self._cc_last_prune = 0.0

    # ------------------------------------------------------------ 配置
    def cfg(self) -> dict:
        try:
            return self.cfg_provider() or {}
        except Exception:
            return {}

    def rules(self) -> dict:
        c = self.cfg()
        return {
            "enabled": bool(c.get("waf_enabled")),
            "mode": (c.get("waf_mode") or "block").lower(),
            "allow": _split(c.get("waf_ip_allow")),
            "deny": _split(c.get("waf_ip_deny")),
            "ua": [u.lower() for u in _split(c.get("waf_ua_block"))] or DEFAULT_UA,
            "path": [p.lower() for p in _split(c.get("waf_path_block"))] or DEFAULT_PATH,
            "cc_limit": int(c.get("waf_cc_limit") or 0),
            "cc_window": max(5, int(c.get("waf_cc_window") or 60)),
            "trust_proxy": bool(c.get("trust_proxy")),
            "block_methods": bool(c.get("waf_block_methods", True)),
        }

    # ------------------------------------------------------------ 检查
    def check(self, request, host: str, record: bool = True) -> dict | None:
        """返回 None = 放行；返回 dict = 命中（action: block / monitor）。

        record=False 用于设置页的「试跑」：只回判定结果，不计入统计、不写日志。
        """
        r = self.rules()
        if not r["enabled"]:
            return None
        if record:
            self.counters["checked"] += 1

        ip = _ip_of(request, r["trust_proxy"])
        path = request.path or "/"
        ua = request.headers.get("User-Agent", "")
        method = request.method.upper()

        rule = ""
        # 1) 白名单优先：自己的运维网段绝不能被自己拦掉
        if r["allow"] and _ip_hit(ip, r["allow"]):
            return None
        # 2) IP 黑名单
        hit = _ip_hit(ip, r["deny"]) if r["deny"] else ""
        if hit:
            rule = f"IP 黑名单（{hit}）"
        # 3) 扫描器 UA
        if not rule and ua:
            low = ua.lower()
            for kw in r["ua"]:
                if kw in low:
                    rule = f"扫描器 UA（{kw}）"
                    break
        if not rule and not ua:
            rule = "空 User-Agent"
        # 4) 敏感路径
        if not rule:
            low_path = path.lower()
            for kw in r["path"]:
                if kw in low_path:
                    rule = f"敏感路径（{kw}）"
                    break
        # 5) 请求方法
        if not rule and r["block_methods"] and method in BAD_METHODS:
            rule = f"危险方法（{method}）"
        # 6) CC 限速
        if not rule and r["cc_limit"] > 0 and ip:
            if self._cc_over(ip, r["cc_limit"], r["cc_window"]):
                rule = f"CC 限速（>{r['cc_limit']}/{r['cc_window']}s）"

        if not rule:
            return None

        action = "monitor" if r["mode"] == "monitor" else "block"
        rec = {"t": int(time.time()), "ip": ip, "host": host, "path": path[:160],
               "method": method, "rule": rule, "action": action, "ua": ua[:120]}
        if not record:
            return rec
        self.counters["monitored" if action == "monitor" else "blocked"] += 1
        self.hits.insert(0, rec)
        del self.hits[self.ring:]
        if action == "block":
            self.log(f"WAF 拦截 {ip} → {host}{path[:80]}（{rule}）")
        return rec

    def _cc_over(self, ip: str, limit: int, window: int) -> bool:
        now = time.time()
        q = self._cc.get(ip)
        if q is None:
            q = self._cc[ip] = deque()
        while q and now - q[0] > window:
            q.popleft()
        q.append(now)
        # 顺带清理长期不活跃的 IP，避免 dict 无限长
        if now - self._cc_last_prune > 60:
            self._cc_last_prune = now
            for k in [k for k, v in self._cc.items() if not v or now - v[-1] > window * 5]:
                self._cc.pop(k, None)
        return len(q) > limit

    # ------------------------------------------------------------ 展示
    def status(self) -> dict:
        r = self.rules()
        return {
            "enabled": r["enabled"], "mode": r["mode"],
            "counters": dict(self.counters),
            "hits": self.hits[:60],
            "rule_counts": {"ip_deny": len(r["deny"]), "ip_allow": len(r["allow"]),
                            "ua": len(r["ua"]), "path": len(r["path"]),
                            "cc": r["cc_limit"]},
            "ua_baseline": not bool(_split(self.cfg().get("waf_ua_block"))),
            "path_baseline": not bool(_split(self.cfg().get("waf_path_block"))),
        }

    def clear(self) -> int:
        n = len(self.hits)
        self.hits.clear()
        self.counters = {"checked": self.counters["checked"], "blocked": 0, "monitored": 0}
        self._cc.clear()
        return n
