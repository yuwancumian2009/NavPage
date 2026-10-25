"""DDNS：把家里的公网 IP 同步到 DNS 记录上。

和证书共用一套 DNS 凭据（见 dnsprov），所以只需要在设置里填一次。

设计要点：
  * 取 IP 用多个源，任何一个成功就用它；全失败时保留上次记录、只在状态里标红，
    绝不把空值写进 DNS（那是灾难）；
  * 只在 IP 变化时才调 API，避免触发供应商的频率限制；
  * 状态落盘到 gw/ddns.json，容器重启后能立刻知道「上次同步到哪个 IP」。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import time

import httpx

from . import dnsprov


def _valid_ip(s: str) -> str:
    s = (s or "").strip().strip('"')
    try:
        return str(ipaddress.ip_address(s))
    except Exception:
        return ""


def fetch_public_ip(sources: str = "", timeout: float = 6.0) -> tuple[str, str]:
    """返回 (ip, 来源)。全部失败时 ip 为空串。"""
    urls = [u.strip() for u in (sources or "").split(",") if u.strip()] or [
        "https://api.ipify.org", "https://4.ipw.cn", "https://myip.ipip.net/json",
        "https://www.cloudflare.com/cdn-cgi/trace",
    ]
    with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=True) as c:
        for u in urls:
            try:
                r = c.get(u)
                if r.status_code >= 400:
                    continue
                text = r.text.strip()
                ip = ""
                if text.startswith("{"):
                    data = r.json()
                    for k in ("ip", "clientIp", "YourFuckingIPAddress", "query"):
                        if isinstance(data.get(k), str) and _valid_ip(data[k]):
                            ip = data[k]
                            break
                    if not ip:
                        ip = _valid_ip((data.get("data") or {}).get("ip") if isinstance(data.get("data"), dict) else "")
                elif "ip=" in text:      # cloudflare trace
                    for line in text.splitlines():
                        if line.startswith("ip="):
                            ip = _valid_ip(line.split("=", 1)[1])
                else:
                    ip = _valid_ip(text.split()[0] if text else "")
                if ip:
                    return ip, u
            except Exception:
                continue
    return "", ""


class DdnsManager:
    def __init__(self, data_dir: str, cfg_fn, log=None):
        self.cfg_fn = cfg_fn
        self.log = log or (lambda *a: None)
        self.path = os.path.join(data_dir, "gw", "ddns.json")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.state = self._load()
        self.task: asyncio.Task | None = None
        self.last_try = 0

    def _load(self) -> dict:
        try:
            return json.load(open(self.path, encoding="utf-8"))
        except Exception:
            return {}

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def status(self) -> dict:
        cfg = self.cfg_fn() or {}
        st = dict(self.state)
        st["enabled"] = bool(cfg.get("ddns_enabled"))
        st["record"] = cfg.get("ddns_record") or ""
        st["provider"] = cfg.get("dns_provider") or ""
        st["interval"] = int(cfg.get("ddns_interval") or 300)
        return st

    # ---------------------------------------------------------------- 主循环
    def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except BaseException:
                pass
            self.task = None

    async def _loop(self):
        await asyncio.sleep(5)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"DDNS 循环异常：{e}")
            iv = int((self.cfg_fn() or {}).get("ddns_interval") or 300)
            await asyncio.sleep(max(60, min(iv, 86400)))

    async def tick(self, force: bool = False) -> dict:
        cfg = self.cfg_fn() or {}
        if not cfg.get("ddns_enabled"):
            self.state.update({"status": "off", "error": ""})
            self._save()
            return self.state
        record = (cfg.get("ddns_record") or "").strip().lower()
        if not record:
            self.state.update({"status": "error", "error": "没填要更新的域名"})
            self._save()
            return self.state
        ip, src = await asyncio.to_thread(fetch_public_ip, cfg.get("ddns_ip_sources") or "")
        self.last_try = int(time.time())
        if not ip:
            self.state.update({"status": "error", "error": "取不到本机公网 IP（所有探测源都失败）",
                               "last_try": self.last_try})
            self._save()
            return self.state
        prev = str(self.state.get("ip") or "")
        if ip == prev and not force:
            self.state.update({"status": "ok", "error": "", "ip": ip, "source": src,
                               "last_try": self.last_try, "last_check": self.last_try})
            self._save()
            return self.state
        try:
            prov = dnsprov.get_provider(cfg.get("dns_provider"), cfg.get("dns_creds") or {})
            result = await asyncio.to_thread(prov.upsert_a, record, ip, 600)
            self.state.update({"status": "ok", "error": "", "ip": ip, "source": src,
                               "record": record, "result": result,
                               "last_try": self.last_try, "last_change": int(time.time()),
                               "changed_from": prev})
            self.log(f"DDNS：{record} → {ip}（{result}）")
        except Exception as e:
            self.state.update({"status": "error", "error": str(e)[:300], "ip": ip,
                               "source": src, "last_try": self.last_try})
            self.log(f"DDNS 更新失败：{e}")
        self._save()
        return self.state
