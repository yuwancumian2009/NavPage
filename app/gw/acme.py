"""ACME v2 客户端（Let's Encrypt / ZeroSSL / 自建 ACME / Pebble 都能用）。

为什么手写而不是装 certbot：
  * certbot 会带一堆依赖和它自己的 /etc/letsencrypt 目录结构，容器里多 40MB；
  * 我们需要的是「拿到证书 PEM 写进自己的证书库」，certbot 的钩子反而更绕；
  * 这里只用到 cryptography 的 JWS / CSR，逻辑两百多行，全部可控。

支持两种校验：
  * dns-01（默认）—— 走 DNS API 加 `_acme-challenge.<域名>` TXT，**不需要 80 端口**，
    能签通配符 `*.example.com`。NAS 上 80/443 被 fnOS 占着，只能走这条路。
  * http-01 —— 把 token 写进反代的 `/.well-known/acme-challenge/` 目录，
    要求 80 端口能从公网访问到。

账号密钥 / 证书都落在 NAVPAGE_DATA/gw/ 下，容器重建不会丢。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils as asym_utils
from cryptography.x509.oid import NameOID

from . import dnsprov

DIRECTORIES = {
    "letsencrypt": "https://acme-v02.api.letsencrypt.org/directory",
    "staging": "https://acme-staging-v02.api.letsencrypt.org/directory",
    "zerossl": "https://acme.zerossl.com/v2/DV90",
}


class AcmeError(Exception):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _json_b64(obj) -> str:
    return b64u(json.dumps(obj, separators=(",", ":"), sort_keys=False).encode())


class AcmeClient:
    def __init__(self, env: str = "letsencrypt", email: str = "", data_dir: str = "/data",
                 timeout: float = 30.0, directory_url: str = "", insecure: bool = False):
        self.directory_url = directory_url or DIRECTORIES.get(env, DIRECTORIES["letsencrypt"])
        self.insecure = insecure    # 自建 ACME / Pebble 用自签证书，测试时关掉校验
        self.email = (email or "").strip()
        self.dir_meta: dict = {}
        self.nonce = ""
        self.kid = ""
        self.timeout = timeout
        self.http = httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False,
                                 verify=not insecure)
        self.account_dir = os.path.join(data_dir, "gw", "acme")
        os.makedirs(self.account_dir, exist_ok=True)
        self.account_key_path = os.path.join(self.account_dir, "account.key")
        self.account_key = self._load_or_make_key()

    # ------------------------------------------------------------ 账号密钥
    def _load_or_make_key(self):
        if os.path.exists(self.account_key_path):
            with open(self.account_key_path, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None)
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        with open(self.account_key_path, "wb") as f:
            f.write(pem)
        os.chmod(self.account_key_path, 0o600)
        return key

    def _jwk(self) -> dict:
        nums = self.account_key.public_key().public_numbers()
        return {"crv": "P-256", "kty": "EC",
                "x": b64u(nums.x.to_bytes(32, "big")),
                "y": b64u(nums.y.to_bytes(32, "big"))}

    def thumbprint(self) -> str:
        raw = json.dumps(self._jwk(), separators=(",", ":"), sort_keys=True).encode()
        return b64u(hashlib.sha256(raw).digest())

    # ------------------------------------------------------------ 传输层
    def _get(self, url: str) -> httpx.Response:
        r = self.http.get(url)
        if r.headers.get("replay-nonce"):
            self.nonce = r.headers["replay-nonce"]
        return r

    def _nonce(self) -> str:
        if not self.nonce:
            self._get(self.dir_meta["newNonce"])
        if not self.nonce:
            raise AcmeError("ACME 服务器没给 nonce")
        n, self.nonce = self.nonce, ""
        return n

    def _sign(self, url: str, payload, use_jwk: bool = False) -> str:
        protected = {"alg": "ES256", "nonce": self._nonce(), "url": url}
        if use_jwk or not self.kid:
            protected["jwk"] = self._jwk()
        else:
            protected["kid"] = self.kid
        if payload == "":
            body = ""
        else:
            body = _json_b64(payload)
        signing_input = (_json_b64(protected) + "." + body).encode()
        der = self.account_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = asym_utils.decode_dss_signature(der)
        sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return json.dumps({"protected": _json_b64(protected), "payload": body, "signature": sig})

    def _post(self, url: str, payload, use_jwk: bool = False,
              accept: str = "application/json") -> httpx.Response:
        data = self._sign(url, payload, use_jwk)
        r = self.http.post(url, content=data.encode(),
                           headers={"Content-Type": "application/jose+json", "Accept": accept})
        if r.headers.get("replay-nonce"):
            self.nonce = r.headers["replay-nonce"]
        return r

    def _post_json(self, url: str, payload, use_jwk: bool = False) -> tuple[dict, httpx.Response]:
        r = self._post(url, payload, use_jwk)
        if r.status_code >= 400:
            raise AcmeError(_acme_err(r))
        try:
            return (r.json() if r.content else {}), r
        except Exception:
            raise AcmeError(f"ACME 返回非 JSON：{r.text[:200]}")

    @staticmethod
    def _problem(r: httpx.Response) -> str:
        try:
            p = r.json()
            return f"{p.get('type', '').split(':')[-1]} {p.get('detail', '')}".strip()
        except Exception:
            return f"HTTP {r.status_code}：{r.text[:160]}"

    # ------------------------------------------------------------ 注册
    def register(self) -> str:
        if not self.dir_meta:
            r = self._get(self.directory_url)
            if r.status_code >= 400:
                raise AcmeError(f"取 ACME 目录失败（HTTP {r.status_code}）")
            self.dir_meta = r.json()
        payload = {"termsOfServiceAgreed": True}
        if self.email:
            payload["contact"] = ["mailto:" + self.email]
        r = self._post(self.dir_meta["newAccount"], payload, use_jwk=True)
        if r.status_code >= 400:
            raise AcmeError("ACME 账号注册失败：" + self._problem(r))
        self.kid = r.headers.get("location", "")
        if not self.kid:
            raise AcmeError("ACME 注册成功但没返回账号地址")
        return self.kid

    # ------------------------------------------------------------ 签发
    def issue(self, domains: list[str], challenge: str = "dns-01",
              provider: dnsprov.Provider | None = None,
              http_dir: str = "", prop_wait: float = 6.0,
              log=None) -> dict:
        """签发一张多域名证书，返回 {"cert": pem, "key": pem, "not_after": ts}。

        调用方负责把 PEM 落盘并热加载；这里只保证「签下来」。
        """
        log = log or (lambda *a: None)
        domains = [d.strip().lower() for d in domains if d and d.strip()]
        if not domains:
            raise AcmeError("没有要签的域名")
        if any(d.startswith("*.") for d in domains) and challenge != "dns-01":
            raise AcmeError("通配符只能用 dns-01 校验")
        if challenge == "dns-01" and not provider:
            raise AcmeError("dns-01 校验需要先配好 DNS 服务商")
        if not self.kid:
            self.register()

        order, r = self._post_json(self.dir_meta["newOrder"],
                                   {"identifiers": [{"type": "dns", "value": d} for d in domains]})
        order_url = r.headers.get("location", "")
        authz_urls = order.get("authorizations") or []
        log(f"订单已创建：{', '.join(domains)}（{len(authz_urls)} 个授权）")

        cleanup: list[tuple[str, str]] = []
        try:
            for i, az_url in enumerate(authz_urls):
                az, _ = self._post_json(az_url, "")
                ident = (az.get("identifier") or {}).get("value", "")
                if az.get("status") == "valid":
                    continue
                ch = _pick_challenge(az.get("challenges") or [], challenge)
                if not ch:
                    raise AcmeError(f"{ident} 没有 {challenge} 校验方式")
                token = ch.get("token", "")
                key_auth = f"{token}.{self.thumbprint()}"
                if challenge == "dns-01":
                    txt_name = "_acme-challenge." + ident.lstrip("*.")
                    txt_val = b64u(hashlib.sha256(key_auth.encode()).digest())
                    log(f"[{i+1}] 写 TXT：{txt_name} → {txt_val[:16]}…")
                    provider.set_txt(txt_name, txt_val, ttl=dnsprov.TXT_TTL)
                    cleanup.append((txt_name, txt_val))
                else:
                    if not http_dir:
                        raise AcmeError("http-01 需要反代提供 challenge 目录")
                    os.makedirs(http_dir, exist_ok=True)
                    with open(os.path.join(http_dir, token), "w") as f:
                        f.write(key_auth)
                    cleanup.append((os.path.join(http_dir, token), ""))
                ok, _ = self._post_json(ch["url"], {})
                if ok.get("status") not in ("pending", "processing", "valid"):
                    raise AcmeError(f"{ident} 校验被拒绝：{ok.get('error', {}).get('detail', '')}")

            # 等 DNS 生效再让 ACME 服务器去验（LE 自己也会重试，但域名多时更容易排队超时）
            if challenge == "dns-01" and cleanup:
                time.sleep(max(0.0, prop_wait))
                for az_url in authz_urls:
                    self._wait_authz(az_url, log)

            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            csr_der = _make_csr(key, domains)
            fin, _ = self._post_json(order["finalize"], {"csr": b64u(csr_der)})
            deadline = time.time() + 120
            while fin.get("status") in ("processing", "ready", "pending") and time.time() < deadline:
                time.sleep(3)
                fin, _ = self._post_json(order_url, "")
            if fin.get("status") != "valid":
                raise AcmeError(f"证书签发失败：状态 {fin.get('status')} "
                                f"{fin.get('error', {}).get('detail', '')}")
            r = self._post(fin["certificate"], "", accept="application/pem-certificate-chain")
            if r.status_code >= 400:
                raise AcmeError("下载证书失败：" + self._problem(r))
            cert_pem = r.text
            key_pem = key.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
            not_after = _cert_not_after(cert_pem)
            log(f"签发成功：{domains[0]}（到期 {time.strftime('%Y-%m-%d', time.localtime(not_after))}）")
            return {"cert": cert_pem, "key": key_pem, "not_after": not_after}
        finally:
            for name, val in cleanup:
                try:
                    if challenge == "dns-01":
                        provider.del_txt(name, val)
                    elif os.path.exists(name):
                        os.unlink(name)
                except Exception:
                    pass   # 清理失败不影响签发结果，只是留一条 TXT

    def _wait_authz(self, url: str, log) -> None:
        deadline = time.time() + 180
        while time.time() < deadline:
            az, _ = self._post_json(url, "")
            st = az.get("status")
            if st == "valid":
                return
            if st == "invalid":
                err = ""
                for ch in az.get("challenges") or []:
                    if ch.get("error"):
                        err = ch["error"].get("detail", "")
                raise AcmeError("授权校验失败：" + (err or "未知原因"))
            time.sleep(3)
        raise AcmeError("等待授权超时（180s）")


def _pick_challenge(challenges: list[dict], kind: str) -> dict | None:
    want = "dns-01" if kind == "dns-01" else "http-01"
    for ch in challenges:
        if ch.get("type") == want:
            return ch
    return None


def _make_csr(key, domains: list[str]) -> bytes:
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domains[0][:64])]))
    builder = builder.add_extension(
        x509.SubjectAlternativeName([x509.DNSName(d) for d in domains]), critical=False)
    csr = builder.sign(key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.DER)


def _cert_not_after(pem: str) -> int:
    try:
        cert = x509.load_pem_x509_certificate(pem.encode())
        try:
            return int(cert.not_valid_after_utc.timestamp())
        except AttributeError:      # cryptography < 42
            return int(cert.not_valid_after.timestamp())
    except Exception:
        return 0


def _acme_err(r: httpx.Response) -> str:
    try:
        p = r.json()
        detail = p.get("detail") or ""
        typ = (p.get("type") or "").rsplit(":", 1)[-1]
        return f"ACME {r.status_code}：{typ} {detail}".strip()
    except Exception:
        return f"ACME HTTP {r.status_code}：{r.text[:160]}"
