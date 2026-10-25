"""证书库：签发结果落盘 + 热加载 + SNI 按域名选证书。

目录结构（都在 NAVPAGE_DATA/gw/certs 下，容器重建不丢）：
  <name>/cert.pem     证书链
  <name>/key.pem      私钥（0600）
  <name>/meta.json    {"domains": [...], "not_after": ts, "env": "letsencrypt", "issued_at": ts}

`name` 用主域名，通配符存成 `wildcard.example.com`（文件名里不能有 *）。

热加载靠「每次握手时看当前缓存版本号」：保存新证书后 bump 版本，下一次 TLS 握手就用新证书，
不需要重启进程、也不需要断开现有连接。这是反代能不能做到「签完立刻生效」的关键。
"""
from __future__ import annotations

import json
import os
import ssl
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def safe_name(domains: list[str]) -> str:
    d = domains[0] if domains else "cert"
    return d.replace("*.", "wildcard.").replace("*", "wildcard").replace("/", "_")


class CertStore:
    def __init__(self, data_dir: str = "/data", log=None):
        self.dir = os.path.join(data_dir, "gw", "certs")
        os.makedirs(self.dir, exist_ok=True)
        self.log = log or (lambda *a: None)
        self._ctx_cache: dict[str, ssl.SSLContext] = {}
        self._fallback: ssl.SSLContext | None = None

    # ------------------------------------------------------------ 读取
    def entries(self) -> list[dict]:
        out = []
        for name in sorted(os.listdir(self.dir)):
            meta_p = os.path.join(self.dir, name, "meta.json")
            if not os.path.exists(meta_p):
                continue
            try:
                meta = json.load(open(meta_p, encoding="utf-8"))
            except Exception:
                continue
            meta["name"] = name
            meta.setdefault("domains", [])
            meta["exists"] = os.path.exists(os.path.join(self.dir, name, "cert.pem")) \
                and os.path.exists(os.path.join(self.dir, name, "key.pem"))
            not_after = int(meta.get("not_after") or 0)
            meta["days_left"] = max(0, int((not_after - time.time()) // 86400)) if not_after else 0
            meta["expired"] = bool(not_after and not_after < time.time())
            out.append(meta)
        return out

    def find_for(self, name: str) -> dict | None:
        for e in self.entries():
            if e["name"] == name:
                return e
        return None

    def cert_paths(self, name: str) -> tuple[str, str]:
        d = os.path.join(self.dir, name)
        return os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")

    # ------------------------------------------------------------ 写入
    def save(self, domains: list[str], cert_pem: str, key_pem: str, not_after: int = 0,
             env: str = "", name: str = "") -> str:
        name = name or safe_name(domains)
        d = os.path.join(self.dir, name)
        os.makedirs(d, exist_ok=True)
        cp, kp = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
        tmp = cp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(cert_pem)
        os.replace(tmp, cp)
        with open(kp, "w", encoding="utf-8") as f:
            f.write(key_pem)
        os.chmod(kp, 0o600)
        meta = {"domains": domains, "not_after": not_after or _not_after(cert_pem),
                "issued_at": int(time.time()), "env": env, "name": name}
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        self._ctx_cache.pop(name, None)
        self.log(f"证书已保存：{', '.join(domains)} → {name}")
        return name

    def update_meta(self, name: str, **kw) -> None:
        p = os.path.join(self.dir, name, "meta.json")
        meta = {}
        if os.path.exists(p):
            try:
                meta = json.load(open(p, encoding="utf-8"))
            except Exception:
                meta = {}
        meta.update(kw)
        meta.setdefault("name", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def remove(self, name: str) -> bool:
        import shutil
        d = os.path.join(self.dir, name)
        if not os.path.isdir(d):
            return False
        shutil.rmtree(d, ignore_errors=True)
        self._ctx_cache.pop(name, None)
        return True

    # ------------------------------------------------------------ TLS
    def _fallback_ctx(self) -> ssl.SSLContext:
        if self._fallback:
            return self._fallback
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        for e in self.entries():
            pass
        cp = os.path.join(self.dir, "_default", "cert.pem")
        kp = os.path.join(self.dir, "_default", "key.pem")
        if not (os.path.exists(cp) and os.path.exists(kp)):
            cert_pem, key_pem = self._self_signed()
            os.makedirs(os.path.dirname(cp), exist_ok=True)
            open(cp, "w").write(cert_pem)
            open(kp, "w").write(key_pem)
            os.chmod(kp, 0o600)
        ctx.load_cert_chain(cp, kp)
        self._fallback = ctx
        return ctx

    def _self_signed(self) -> tuple[str, str]:
        """没证书时也能起 HTTPS（浏览器会警告），至少让反代端口能握手、能排查。"""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "navpage.local")])
        now = time.time()
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(_dt(now - 86400)).not_valid_after(_dt(now + 3650 * 86400))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("navpage.local")]), critical=False)
                .sign(key, hashes.SHA256()))
        return (cert.public_bytes(serialization.Encoding.PEM).decode(),
                key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()).decode())

    def context_for(self, host: str) -> ssl.SSLContext:
        """按 SNI 找证书：精确匹配 → 通配符匹配 → 兜底自签。"""
        host = (host or "").lower().strip(".")
        for e in self.entries():
            if not e.get("exists"):
                continue
            doms = [d.lower() for d in e.get("domains") or []]
            hit = False
            for d in doms:
                if d == host:
                    hit = True
                elif d.startswith("*.") and host.count(".") == d.count(".") and host.endswith(d[1:]):
                    hit = True
            if not hit:
                continue
            if e["name"] in self._ctx_cache:
                return self._ctx_cache[e["name"]]
            cp, kp = self.cert_paths(e["name"])
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                ctx.load_cert_chain(cp, kp)
            except Exception as ex:
                self.log(f"加载证书 {e['name']} 失败：{ex}")
                continue
            self._ctx_cache[e["name"]] = ctx
            return ctx
        return self._fallback_ctx()

    def server_context(self) -> ssl.SSLContext:
        """给 aiohttp 的 SSLContext：默认证书 + SNI 回调按域名切换。"""
        base = self._fallback_ctx()
        base.set_servername_callback(self._sni_cb)
        return base

    def _sni_cb(self, sslobj, server_name, _ctx):
        if not server_name:
            return
        try:
            ctx = self.context_for(server_name)
            if ctx is not None:
                sslobj.context = ctx
        except Exception as e:      # 回调里抛异常只在日志里出现，不会中断握手
            self.log(f"SNI 选证书失败 {server_name}：{e}")

    # ------------------------------------------------------------ 续期判断
    def due_for_renew(self, name: str, days: int = 30) -> bool:
        e = self.find_for(name)
        if not e:
            return False
        na = int(e.get("not_after") or 0)
        return bool(na) and na - time.time() < days * 86400


def _dt(ts: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _not_after(cert_pem: str) -> int:
    try:
        c = x509.load_pem_x509_certificate(cert_pem.encode())
        try:
            return int(c.not_valid_after_utc.timestamp())
        except AttributeError:
            return int(c.not_valid_after.timestamp())
    except Exception:
        return 0
