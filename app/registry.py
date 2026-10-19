"""镜像更新检查：比对本地镜像摘要与 registry 上的最新摘要。

只做只读查询（打外网 registry 的 manifest 接口），不依赖 docker socket。
支持 Docker Hub / GHCR / lscr.io / quay.io / 自建 registry（含匿名 token 换取的
Bearer 挑战流程）。

坑：
  * lscr.io 是 ghcr.io 的重定向层，会 307 到 ghcr，得跟随重定向并重新处理挑战
  * Docker Hub 的官方镜像仓库名要补 `library/` 前缀
  * 多架构镜像的摘要要和本地 RepoDigests 里的比（都是 manifest list 摘要）
"""
import re
import httpx

TIMEOUT = 8.0   # 镜像站实测 1~3 秒返回；8 秒足够，同时给最坏情况封顶

# 依次尝试这些 Accept，拿到的 Docker-Content-Digest 才是可比对的
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

# 私有/自建 registry 常见别名
REGISTRY_ALIASES = {
    "docker.io": "registry-1.docker.io",
    "index.docker.io": "registry-1.docker.io",
    "registry.hub.docker.com": "registry-1.docker.io",
}


def parse_ref(image):
    """把镜像引用拆成 (registry, repo, tag)。"""
    ref = (image or "").strip()
    if not ref:
        return "", "", ""
    tag = "latest"
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        ref, tag = ref.rsplit(":", 1)
    parts = [p for p in ref.split("/") if p]
    if not parts:
        return "", "", ""
    head = parts[0]
    if "." in head or ":" in head or head == "localhost":
        registry = REGISTRY_ALIASES.get(head, head)
        repo = "/".join(parts[1:])
    else:
        registry = "registry-1.docker.io"
        repo = "/".join(parts)
        if "/" not in repo:
            repo = "library/" + repo
    return registry, repo, tag


def _parse_challenge(hdr):
    """解析 WWW-Authenticate: Bearer realm="...",service="...",scope="..." """
    out = {}
    for k, v in re.findall(r'(\w+)="([^"]*)"', hdr or ""):
        out[k.lower()] = v
    return out


def _get_token(c, challenge, repo):
    ch = _parse_challenge(challenge)
    realm = ch.get("realm")
    if not realm:
        return ""
    params = {}
    if ch.get("service"):
        params["service"] = ch["service"]
    params["scope"] = ch.get("scope") or f"repository:{repo}:pull"
    r = c.get(realm, params=params)
    if r.status_code != 200:
        return ""
    j = {}
    try:
        j = r.json() or {}
    except Exception:
        return ""
    return j.get("token") or j.get("access_token") or ""


def normalize_mirror(mirror):
    """把用户填的镜像源规范成主机名：docker.1panel.live / https://docker.1panel.live/ 都接受。"""
    m = (mirror or "").strip()
    if not m:
        return ""
    m = re.sub(r"^https?://", "", m)
    return m.split("/")[0].strip().rstrip("/")


def _digest_from(host, repo, tag, proxy=None):
    """向指定 registry 主机要 manifest 摘要。host 不带协议。"""
    url = f"https://{host}/v2/{repo}/manifests/{tag}"
    headers = {"Accept": ACCEPT}
    kw = {"timeout": TIMEOUT, "follow_redirects": True,
          "headers": {"User-Agent": "navpage/1.0"}}
    if proxy:
        kw["proxy"] = proxy
    try:
        with httpx.Client(**kw) as c:
            r = c.get(url, headers=headers)
            if r.status_code == 401:
                tok = _get_token(c, r.headers.get("www-authenticate", ""), repo)
                if not tok:
                    return "", "registry 拒绝了匿名访问（私有仓库）"
                headers["Authorization"] = "Bearer " + tok
                r = c.get(url, headers=headers)
            # lscr.io → ghcr 的重定向可能带来新的挑战
            if r.status_code == 401:
                tok = _get_token(c, r.headers.get("www-authenticate", ""), repo)
                if tok:
                    headers["Authorization"] = "Bearer " + tok
                    r = c.get(url, headers=headers)
            if r.status_code == 404:
                return "", f"registry 上没有 {repo}:{tag}"
            if r.status_code == 429:
                return "", "HTTP 429（registry 限流：本机 IP 的匿名配额用完了）"
            if r.status_code != 200:
                return "", f"HTTP {r.status_code}"
            d = r.headers.get("docker-content-digest") or ""
            if not d:
                return "", "registry 没返回摘要"
            return d.strip(), ""
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"


def _upstream_digest(registry, repo, tag, proxy=None):
    """问上游 registry。**先直连，失败再用代理兜底**。

    实测（2026-10-15，面板容器内）：ghcr.io 直连 1 秒内返回挑战，走代理反而
    被 `SSL: UNEXPECTED_EOF_WHILE_READING` 打断；quay.io 两条路都通；registry-1.docker.io
    直连是立刻失败的（没有直连路由），所以代理兜底仍然必要。
    """
    d, err = "", ""
    try:
        d, err = _digest_from(registry, repo, tag, proxy=None)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    if d or not proxy:
        return d, err
    try:
        d2, err2 = _digest_from(registry, repo, tag, proxy=proxy)
    except Exception as e:
        d2, err2 = "", f"{type(e).__name__}: {e}"
    if d2:
        return d2, ""
    return "", f"直连失败（{err}）；代理失败（{err2}）"


def remote_digest(image, proxy=None, mirror=None):
    """返回 (digest, error)。digest 形如 sha256:xxxx。

    proxy 形如 http://user:pass@host:port。国内直连 Docker Hub 基本不通，
    建议在「镜像」设置里填上软路由上的代理（NAS 的 docker daemon 也是这么走的）。

    查询顺序：**镜像源**（仅 Docker Hub 镜像；直连、不走代理、不占匿名配额）
    → 上游 registry（走代理）。加镜像源的原因：Docker Hub 按 IP 限流匿名请求，
    面板一次查几十个镜像极易整片 429，用户看到的就是「一直显示不出来更新」。
    """
    registry, repo, tag = parse_ref(image)
    if not registry:
        return "", "镜像名无法解析"
    mhost = normalize_mirror(mirror)
    m_err = ""
    if mhost and registry == "registry-1.docker.io" and mhost != registry:
        try:
            d, m_err = _digest_from(mhost, repo, tag, proxy=None)
        except Exception as e:
            m_err = f"{type(e).__name__}: {e}"
        if d:
            return d, ""
    d, err = _upstream_digest(registry, repo, tag, proxy=proxy)
    if d:
        return d, ""
    if m_err:
        return "", f"镜像源 {mhost} 失败：{m_err}；上游失败：{err}"
    return "", err


def normalize(digest):
    d = (digest or "").strip()
    if "@" in d:
        d = d.split("@", 1)[1]
    return d


def local_digest_for(repo_digests, image):
    """从 /images/{name}/json 的 RepoDigests 里挑出和该镜像对应的摘要。"""
    registry, repo, _ = parse_ref(image)
    want = repo
    if registry == "registry-1.docker.io":
        want = repo
    for rd in repo_digests or []:
        if "@" not in rd:
            continue
        name, dg = rd.split("@", 1)
        if name == want or name.endswith("/" + want) or want.endswith(name):
            return normalize(dg)
    if repo_digests:
        return normalize(repo_digests[0])
    return ""


def _manifest_body(host, repo, ref, proxy=None):
    """取 manifest（ref 可以是 tag，也可以是 digest）-> (body, content_type, error)。"""
    url = f"https://{host}/v2/{repo}/manifests/{ref}"
    headers = {"Accept": ACCEPT}
    kw = {"timeout": TIMEOUT, "follow_redirects": True,
          "headers": {"User-Agent": "navpage/1.0"}}
    if proxy:
        kw["proxy"] = proxy
    try:
        with httpx.Client(**kw) as c:
            r = c.get(url, headers=headers)
            if r.status_code == 401:
                tok = _get_token(c, r.headers.get("www-authenticate", ""), repo)
                if not tok:
                    return None, "", "registry 拒绝了匿名访问（私有仓库）"
                headers["Authorization"] = "Bearer " + tok
                r = c.get(url, headers=headers)
            if r.status_code == 404:
                return None, "", f"registry 上没有 {repo}:{ref}"
            if r.status_code == 429:
                return None, "", "HTTP 429（registry 限流：本机 IP 的匿名配额用完了）"
            if r.status_code != 200:
                return None, "", f"HTTP {r.status_code}"
            try:
                return (r.json() or {}), r.headers.get("content-type", ""), ""
            except Exception:
                return None, "", "manifest 不是 JSON"
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}"


def _pick_platform_digest(body, want=("linux", "amd64")):
    """从 manifest list 里挑出目标平台的 manifest 摘要。

    跳过 buildx 的 attestation 条目（platform 是 unknown/unknown），
    否则会拿到一个没有 config 的空壳 manifest。
    """
    ms = [m for m in (body.get("manifests") or []) if isinstance(m, dict)]
    if not ms:
        return ""
    for m in ms:
        p = m.get("platform") or {}
        if (p.get("os"), p.get("architecture")) == want:
            return m.get("digest") or ""
    for m in ms:
        p = m.get("platform") or {}
        if p.get("os") == "linux" and p.get("architecture") not in ("unknown", "", None):
            return m.get("digest") or ""
    return ""


def remote_config_digest(image, proxy=None, mirror=None):
    """取 registry 上该 tag 的**镜像 config 摘要**（= 本地 `docker images` 的 IMAGE ID）。

    为什么需要它：RepoDigests 为空不等于「本地构建」。飞牛面板 / 离线包导入的镜像
    没有 RepoDigests，但确实是 registry 上的镜像（用户反馈 openlist 被误判成
    「本地镜像」就是这个原因）。manifest 的 config.digest 和本地镜像 ID 是同一个
    东西，可以直接比，判断「已最新 / 可更新」不需要先 pull。
    """
    registry, repo, tag = parse_ref(image)
    if not registry:
        return "", "镜像名无法解析"
    mhost = normalize_mirror(mirror)
    tries = []
    if mhost and registry == "registry-1.docker.io" and mhost != registry:
        tries.append((mhost, None))
    tries.append((registry, None))          # 直连
    if proxy:
        tries.append((registry, proxy))     # 代理兜底
    last = ""
    for host, px in tries:
        body, _ctype, err = _manifest_body(host, repo, tag, proxy=px)
        if err:
            last = err
            continue
        if body.get("manifests"):
            d = _pick_platform_digest(body)
            if not d:
                last = "manifest list 里没有 linux/amd64"
                continue
            body, _ctype, err = _manifest_body(host, repo, d, proxy=px)
            if err:
                last = err
                continue
        cfg = (body.get("config") or {}).get("digest") or ""
        if cfg:
            return cfg, ""
        last = "manifest 里没有 config.digest"
    return "", last or "取不到 config digest"


def is_available(proxy=None):
    """探测能否访问 registry（用于设置页给个即时反馈）。"""
    kw = {"timeout": 8.0, "follow_redirects": True}
    if proxy:
        kw["proxy"] = proxy
    out = {}
    try:
        with httpx.Client(**kw) as c:
            for name, url in (("docker.io", "https://registry-1.docker.io/v2/"),
                              ("ghcr.io", "https://ghcr.io/v2/")):
                try:
                    out[name] = c.get(url).status_code
                except Exception as e:
                    out[name] = f"{type(e).__name__}"
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return out
