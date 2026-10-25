"""DNS 供应商：签证书要写 TXT，DDNS 要改 A 记录，两件事共用一套凭据。

支持 Cloudflare / 阿里云 DNS / DNSPod / 通用 webhook。
凭据统一是 dict，存在设置 `dns_creds` 里：

  cloudflare: {"token": "API Token（Zone.DNS 编辑权限）"}
  alidns:     {"access_key_id": "...", "access_key_secret": "..."}
  dnspod:     {"id": "12345", "token": "..."}      # DNSPod 的 login_token = "id,token"
  webhook:    {"url": "https://...", "token": "可选，放 Authorization: Bearer"}

webhook 用来对接自建 DNS / 其他厂商：每次变更 POST 一段 JSON：{"action":"set|del",
"domain":"_acme-challenge.example.com","type":"TXT","value":"...","ttl":120}。
也能直接对接 Pebble 的 challtestsrv 做端到端测试（见 scripts/gw_pebble_test.sh）。

全部同步接口（requests/httpx.Client）：调用点在后台线程/task 里，串行执行不会互相踩。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
import urllib.parse

import httpx


class DnsError(Exception):
    """DNS API 报错。message 直接给用户看，所以尽量带上供应商原文。"""


def _client(timeout=20.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False)


# ----------------------------------------------------------------- 工具

def split_fqdn(fqdn: str) -> tuple[str, str]:
    """把 `_acme-challenge.a.b.com` 拆成 (记录名, 可用于推断域名的部分)。

    这里只做字符串规整：真正的域名（zone）由各供应商自己按后缀匹配，
    因为只有它知道账号里有哪些域名。
    """
    fqdn = (fqdn or "").strip().strip(".").lower()
    return fqdn, fqdn.lstrip("_acme-challenge.")


def guess_zones(fqdn: str) -> list[str]:
    """把 `_acme-challenge.home.example.com` 依次猜成
    ['home.example.com', 'example.com', 'com']，交给供应商从长到短试。"""
    parts = [p for p in (fqdn or "").strip(".").lower().split(".") if p]
    out = []
    for i in range(len(parts) - 1):
        out.append(".".join(parts[i:]))
    return out


def _record_name(fqdn: str, zone: str) -> str:
    fqdn = fqdn.strip(".").lower()
    zone = zone.strip(".").lower()
    if fqdn == zone:
        return "@"
    if fqdn.endswith("." + zone):
        return fqdn[: -(len(zone) + 1)]
    return fqdn


# ----------------------------------------------------------------- 基类

class Provider:
    name = "base"

    def __init__(self, creds: dict | None = None):
        self.creds = creds or {}

    # 子类实现：找到 fqdn 所属 zone（返回 None 表示不在这个账号里）
    def find_zone(self, fqdn: str) -> str | None:
        raise NotImplementedError

    def set_txt(self, fqdn: str, value: str, ttl: int = 120) -> None:
        raise NotImplementedError

    def del_txt(self, fqdn: str, value: str = "") -> None:
        raise NotImplementedError

    def upsert_a(self, fqdn: str, ip: str, ttl: int = 600) -> str:
        raise NotImplementedError

    def get_a(self, fqdn: str) -> str:
        raise NotImplementedError

    # 统一入口：DDNS 和证书都先要走这一步
    def zone(self, fqdn: str) -> str:
        z = self.find_zone(fqdn)
        if not z:
            raise DnsError(f"在 {self.name} 账号里找不到 {fqdn} 所属的域名，请检查凭据和域名是否正确")
        return z

    def test(self) -> str:
        """连通性测试：返回一句人话，失败抛 DnsError。"""
        raise NotImplementedError


# ----------------------------------------------------------------- Cloudflare

class Cloudflare(Provider):
    name = "Cloudflare"
    API = "https://api.cloudflare.com/client/v4"

    def _req(self, method: str, path: str, **kw):
        token = (self.creds.get("token") or "").strip()
        if not token:
            raise DnsError("Cloudflare 需要 API Token")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        with _client() as c:
            r = c.request(method, self.API + path, headers=headers, **kw)
        try:
            data = r.json()
        except Exception:
            raise DnsError(f"Cloudflare 返回非 JSON（HTTP {r.status_code}）")
        if r.status_code >= 400 or not data.get("success", False):
            errs = data.get("errors") or []
            msg = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errs) or f"HTTP {r.status_code}"
            raise DnsError(f"Cloudflare 报错：{msg}")
        return data.get("result")

    def _zones(self) -> list[dict]:
        return self._req("GET", "/zones?per_page=100") or []

    def find_zone(self, fqdn: str) -> str | None:
        zones = {z["name"].lower() for z in self._zones()}
        for cand in guess_zones(fqdn):
            if cand in zones:
                return cand
        return None

    def _zone_id(self, zone: str) -> str:
        res = self._req("GET", "/zones?name=" + urllib.parse.quote(zone))
        if not res:
            raise DnsError(f"Cloudflare 找不到区域 {zone}")
        return res[0]["id"]

    def _records(self, zone: str, rtype: str, name: str) -> list[dict]:
        zid = self._zone_id(zone)
        q = f"/zones/{zid}/dns_records?type={rtype}&name={urllib.parse.quote(name)}&per_page=100"
        return self._req("GET", q) or []

    def set_txt(self, fqdn: str, value: str, ttl: int = 120) -> None:
        zone = self.zone(fqdn)
        zid = self._zone_id(zone)
        body = {"type": "TXT", "name": fqdn, "content": value, "ttl": max(60, ttl)}
        # 同一个名字可能已有别的 TXT（例如别的证书），只改内容一致的或新建
        for rec in self._records(zone, "TXT", fqdn):
            if rec.get("content") == value:
                return
            if rec.get("content", "").startswith("_"):
                continue
        self._req("POST", f"/zones/{zid}/dns_records", json=body)

    def del_txt(self, fqdn: str, value: str = "") -> None:
        zone = self.find_zone(fqdn)
        if not zone:
            return
        zid = self._zone_id(zone)
        for rec in self._records(zone, "TXT", fqdn):
            if value and rec.get("content") != value:
                continue
            self._req("DELETE", f"/zones/{zid}/dns_records/{rec['id']}")

    def upsert_a(self, fqdn: str, ip: str, ttl: int = 600) -> str:
        zone = self.zone(fqdn)
        zid = self._zone_id(zone)
        body = {"type": "A", "name": fqdn, "content": ip, "ttl": max(60, ttl),
                "proxied": False}
        recs = self._records(zone, "A", fqdn)
        if recs:
            if recs[0].get("content") == ip:
                return "unchanged"
            self._req("PUT", f"/zones/{zid}/dns_records/{recs[0]['id']}", json=body)
            return "updated"
        self._req("POST", f"/zones/{zid}/dns_records", json=body)
        return "created"

    def get_a(self, fqdn: str) -> str:
        zone = self.find_zone(fqdn)
        if not zone:
            return ""
        recs = self._records(zone, "A", fqdn)
        return recs[0].get("content", "") if recs else ""

    def test(self) -> str:
        zones = self._zones()
        names = ", ".join(z["name"] for z in zones[:5])
        return f"连接正常，账号下 {len(zones)} 个域名" + (f"：{names}" if names else "")


# ----------------------------------------------------------------- 阿里云 DNS

class AliyunDns(Provider):
    name = "阿里云 DNS"
    API = "https://alidns.aliyuncs.com/"

    def _sign(self, params: dict) -> str:
        """阿里云 RPC 签名 v1：参数排序 → 规范化 → HMAC-SHA1。"""
        sec = (self.creds.get("access_key_secret") or "").strip()
        if not (self.creds.get("access_key_id") and sec):
            raise DnsError("阿里云 DNS 需要 AccessKey ID / Secret")
        p = dict(params)
        p.update({
            "Format": "JSON", "Version": "2015-01-09", "AccessKeyId": self.creds["access_key_id"].strip(),
            "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
            "SignatureNonce": uuid.uuid4().hex, "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        enc = lambda s: urllib.parse.quote(str(s), safe="~")
        canonical = "&".join(f"{enc(k)}={enc(p[k])}" for k in sorted(p))
        string_to_sign = "GET&%2F&" + enc(canonical)
        sig = base64.b64encode(
            hmac.new((sec + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
        ).decode()
        p["Signature"] = sig
        return urllib.parse.urlencode(p)

    def _req(self, action: str, **params) -> dict:
        q = {"Action": action, **{k: v for k, v in params.items() if v not in (None, "")}}
        url = self.API + "?" + self._sign(q)
        with _client() as c:
            r = c.get(url)
        try:
            data = r.json()
        except Exception:
            raise DnsError(f"阿里云返回非 JSON（HTTP {r.status_code}）")
        if r.status_code >= 400:
            raise DnsError(f"阿里云报错：{data.get('Code')} {data.get('Message')}")
        return data

    def _domains(self) -> list[str]:
        out, page = [], 1
        while True:
            d = self._req("DescribeDomains", PageNumber=page, PageSize=100)
            out += [x["DomainName"].lower() for x in (d.get("Domains", {}).get("Domain") or [])]
            if page * 100 >= int(d.get("TotalCount") or 0):
                break
            page += 1
            if page > 20:
                break
        return out

    def find_zone(self, fqdn: str) -> str | None:
        doms = set(self._domains())
        for cand in guess_zones(fqdn):
            if cand in doms:
                return cand
        return None

    def _records(self, zone: str, rtype: str, rr: str) -> list[dict]:
        d = self._req("DescribeDomainRecords", DomainName=zone, RRKeyWord=rr,
                      Type=rtype, PageSize=500)
        rows = d.get("DomainRecords", {}).get("Record") or []
        return [r for r in rows if str(r.get("RR", "")).lower() == rr.lower()]

    def set_txt(self, fqdn: str, value: str, ttl: int = 120) -> None:
        zone = self.zone(fqdn)
        rr = _record_name(fqdn, zone)
        for rec in self._records(zone, "TXT", rr):
            if rec.get("Value", "").strip('"') == value:
                return
        self._req("AddDomainRecord", DomainName=zone, RR=rr, Type="TXT",
                  Value=value, TTL=max(60, ttl))

    def del_txt(self, fqdn: str, value: str = "") -> None:
        zone = self.find_zone(fqdn)
        if not zone:
            return
        rr = _record_name(fqdn, zone)
        for rec in self._records(zone, "TXT", rr):
            if value and rec.get("Value", "").strip('"') != value:
                continue
            self._req("DeleteDomainRecord", RecordId=rec["RecordId"])

    def upsert_a(self, fqdn: str, ip: str, ttl: int = 600) -> str:
        zone = self.zone(fqdn)
        rr = _record_name(fqdn, zone)
        recs = self._records(zone, "A", rr)
        if recs:
            if recs[0].get("Value") == ip:
                return "unchanged"
            self._req("UpdateDomainRecord", RecordId=recs[0]["RecordId"], RR=rr,
                      Type="A", Value=ip, TTL=max(60, ttl))
            return "updated"
        self._req("AddDomainRecord", DomainName=zone, RR=rr, Type="A",
                  Value=ip, TTL=max(60, ttl))
        return "created"

    def get_a(self, fqdn: str) -> str:
        zone = self.find_zone(fqdn)
        if not zone:
            return ""
        recs = self._records(zone, "A", _record_name(fqdn, zone))
        return recs[0].get("Value", "") if recs else ""

    def test(self) -> str:
        doms = self._domains()
        return f"连接正常，账号下 {len(doms)} 个域名" + (f"：{', '.join(doms[:5])}" if doms else "")


# ----------------------------------------------------------------- DNSPod

class Dnspod(Provider):
    name = "DNSPod"
    API = "https://dnsapi.cn/"

    def _login_token(self) -> str:
        i = str(self.creds.get("id") or "").strip()
        t = str(self.creds.get("token") or "").strip()
        if i and t:
            return f"{i},{t}"
        if t and "," in t:
            return t
        raise DnsError("DNSPod 需要 ID + Token（登录令牌）")

    def _req(self, path: str, **data) -> dict:
        form = {"login_token": self._login_token(), "format": "json", "lang": "cn", **data}
        with _client() as c:
            r = c.post(self.API + path, data=form,
                       headers={"User-Agent": "NavPage/2.0 (gateway)"})
        try:
            j = r.json()
        except Exception:
            raise DnsError(f"DNSPod 返回非 JSON（HTTP {r.status_code}）")
        st = (j.get("status") or {}).get("code")
        if str(st) != "1":
            raise DnsError(f"DNSPod 报错：{(j.get('status') or {}).get('message')}")
        return j

    def _domains(self) -> list[str]:
        j = self._req("Domain.List", type="all", page_size=100)
        return [d["name"].lower() for d in (j.get("domains") or [])]

    def find_zone(self, fqdn: str) -> str | None:
        doms = set(self._domains())
        for cand in guess_zones(fqdn):
            if cand in doms:
                return cand
        return None

    def _records(self, zone: str, rtype: str, sub: str) -> list[dict]:
        j = self._req("Record.List", domain=zone, sub_domain=sub, record_type=rtype)
        return [r for r in (j.get("records") or [])
                if str(r.get("name", "")).lower() == sub.lower()]

    def set_txt(self, fqdn: str, value: str, ttl: int = 120) -> None:
        zone = self.zone(fqdn)
        sub = _record_name(fqdn, zone)
        for rec in self._records(zone, "TXT", sub):
            if rec.get("value", "").strip('"') == value:
                return
        self._req("Record.Create", domain=zone, sub_domain=sub, record_type="TXT",
                  record_line="默认", value=value, ttl=max(60, ttl))

    def del_txt(self, fqdn: str, value: str = "") -> None:
        zone = self.find_zone(fqdn)
        if not zone:
            return
        sub = _record_name(fqdn, zone)
        for rec in self._records(zone, "TXT", sub):
            if value and rec.get("value", "").strip('"') != value:
                continue
            self._req("Record.Remove", domain=zone, record_id=rec["id"])

    def upsert_a(self, fqdn: str, ip: str, ttl: int = 600) -> str:
        zone = self.zone(fqdn)
        sub = _record_name(fqdn, zone)
        recs = self._records(zone, "A", sub)
        if recs:
            if recs[0].get("value") == ip:
                return "unchanged"
            self._req("Record.Modify", domain=zone, record_id=recs[0]["id"],
                      sub_domain=sub, record_type="A", record_line="默认",
                      value=ip, ttl=max(60, ttl))
            return "updated"
        self._req("Record.Create", domain=zone, sub_domain=sub, record_type="A",
                  record_line="默认", value=ip, ttl=max(60, ttl))
        return "created"

    def get_a(self, fqdn: str) -> str:
        zone = self.find_zone(fqdn)
        if not zone:
            return ""
        recs = self._records(zone, "A", _record_name(fqdn, zone))
        return recs[0].get("value", "") if recs else ""

    def test(self) -> str:
        doms = self._domains()
        return f"连接正常，账号下 {len(doms)} 个域名" + (f"：{', '.join(doms[:5])}" if doms else "")


# ----------------------------------------------------------------- Webhook

class Webhook(Provider):
    """通用 webhook：把变更 POST 出去，由外部脚本/自建 DNS/Pebble 处理。

    请求体：{"action":"set|del|test","domain":...,"type":"TXT|A","value":...,"ttl":...}
    可选 token 会放进 Authorization: Bearer。
    """
    name = "Webhook"

    def _post(self, payload: dict) -> dict:
        url = (self.creds.get("url") or "").strip()
        if not url:
            raise DnsError("Webhook 需要目标 URL")
        headers = {"Content-Type": "application/json"}
        if self.creds.get("token"):
            headers["Authorization"] = "Bearer " + str(self.creds["token"])
        with _client() as c:
            r = c.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            raise DnsError(f"Webhook 返回 HTTP {r.status_code}：{r.text[:120]}")
        try:
            return r.json()
        except Exception:
            return {}

    def find_zone(self, fqdn: str) -> str | None:
        # webhook 由对端决定，不预判；返回最长猜测，避免报错
        g = guess_zones(fqdn)
        return g[0] if g else None

    def set_txt(self, fqdn: str, value: str, ttl: int = 120) -> None:
        self._post({"action": "set", "domain": fqdn, "type": "TXT", "value": value, "ttl": ttl})

    def del_txt(self, fqdn: str, value: str = "") -> None:
        self._post({"action": "del", "domain": fqdn, "type": "TXT", "value": value})

    def upsert_a(self, fqdn: str, ip: str, ttl: int = 600) -> str:
        r = self._post({"action": "set", "domain": fqdn, "type": "A", "value": ip, "ttl": ttl})
        return r.get("result") or "updated"

    def get_a(self, fqdn: str) -> str:
        r = self._post({"action": "get", "domain": fqdn, "type": "A"})
        return r.get("value") or ""

    def test(self) -> str:
        self._post({"action": "test"})
        return "Webhook 请求已发出"


PROVIDERS = {
    "cloudflare": Cloudflare,
    "alidns": AliyunDns,
    "dnspod": Dnspod,
    "webhook": Webhook,
}


def get_provider(name: str, creds: dict | None) -> Provider:
    cls = PROVIDERS.get((name or "").strip().lower())
    if not cls:
        raise DnsError("未配置 DNS 服务商（证书走 DNS-01、DDNS 都要用它）")
    return cls(creds or {})
