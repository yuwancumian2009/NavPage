"""图标抓取与缓存。

支持两种来源：
  1) fetch_favicon(base_url)  —— 从站点抓 favicon（先 /favicon.ico，再解析 HTML <link rel=icon>）
  2) fetch_icon_url(url)      —— 抓任意图片 URL（收藏网站用）

带 SSRF 防护：仅允许 http/https，禁止云元数据地址，限制响应体大小。
"""
import os
import re
import io
import time
import hashlib
import ipaddress
from urllib.parse import urljoin, urlparse

import httpx

ICON_DIR = os.environ.get("NAVPAGE_ICON_DIR", "/data/icons")
MAX_BYTES = 512 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal", "100.100.100.200"}

MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"\x00\x00\x01\x00", "ico"),
    (b"\x00\x00\x02\x00", "ico"),
    (b"BM", "bmp"),
]
CT_EXT = {
    "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico", "image/ico": "ico",
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
    "image/gif": "gif", "image/svg+xml": "svg", "image/webp": "webp",
    "image/bmp": "bmp", "image/avif": "avif",
}


def _safe(url):
    try:
        p = urlparse(url)
    except Exception:
        return False, "url 解析失败"
    if p.scheme not in ("http", "https"):
        return False, "仅支持 http/https"
    host = (p.hostname or "").lower()
    if not host:
        return False, "缺少主机名"
    if host in BLOCKED_HOSTS:
        return False, "禁止访问该地址"
    try:
        ip = ipaddress.ip_address(host)
        if str(ip) in BLOCKED_HOSTS or ip.is_link_local:
            return False, "禁止访问该地址"
    except ValueError:
        pass
    return True, ""


def _detect_ext(data, content_type):
    for magic, ext in MAGIC:
        if data.startswith(magic):
            return ext
    head = data[:400].lstrip().lower()
    if head.startswith(b"<svg") or b"<svg" in head[:200]:
        return "svg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    ct = (content_type or "").split(";")[0].strip().lower()
    return CT_EXT.get(ct)


def save_bytes(data, ext):
    os.makedirs(ICON_DIR, exist_ok=True)
    name = hashlib.sha1(data).hexdigest()[:20] + "." + ext
    path = os.path.join(ICON_DIR, name)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return name


HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,image/apng,"
              "image/svg+xml,image/x-icon,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 抓 favicon 时依次尝试的常见路径（很多站点把图标放在静态目录，根 favicon.ico 并没有）
COMMON_PATHS = ("/favicon.ico", "/favicon.png", "/favicon.svg",
                "/apple-touch-icon.png", "/static/favicon.ico",
                "/assets/favicon.ico", "/img/favicon.ico")

# 在线图标库兜底。2026-10-25 在 NAS 上实测：favicon.im 可用（含境外站点，
# 返回真实图标）；Google s2 / DuckDuckGo / api.iowen.cn 在国内网络全部连接超时。
ONLINE_ICON_URL = "https://favicon.im/{host}"

# favicon.im 对「取不到图标」的目标（不存在的域名、内网 IP）会返回一张固定的灰色
# 占位图（灰圆 + 斜体 f），md5 c213d299a2638391440eff20c8cf0b8f。必须识别并丢弃，
# 否则每张失败的卡片都会挂上一个毫无意义的 f 头像。
ONLINE_PLACEHOLDER_MD5 = "c213d299a2638391440eff20c8cf0b8f"
LOCAL_TLDS = (".local", ".lan", ".internal", ".home", ".localdomain", ".localhost")


def _get_image(client, url, timeout=None):
    """抓一个 URL 并返回 (bytes, ext, why)；不是图片/太大/非 200 都算失败。"""
    ok, why = _safe(url)
    if not ok:
        return None, None, why
    try:
        with client.stream("GET", url, follow_redirects=True,
                           timeout=timeout) as r:
            if r.status_code != 200:
                return None, None, "HTTP %d" % r.status_code
            ct = r.headers.get("content-type", "")
            buf = io.BytesIO()
            for chunk in r.iter_bytes():
                buf.write(chunk)
                if buf.tell() > MAX_BYTES:
                    return None, None, "文件过大"
            data = buf.getvalue()
    except Exception as e:
        return None, None, type(e).__name__
    if not data:
        return None, None, "空响应"
    ext = _detect_ext(data, ct)
    if not ext:
        return None, None, "非图片内容"
    return data, ext, ""


def _download(client, url, timeout=None):
    data, ext, why = _get_image(client, url, timeout)
    if not data:
        return None, why
    return save_bytes(data, ext), ext


def fetch_icon_url(url, timeout=6.0):
    with httpx.Client(timeout=timeout, headers=HEADERS) as client:
        return _download(client, url)


LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
ATTR_RE = re.compile(r"""(\w[\w-]*)\s*=\s*["']([^"']*)["']""")
SIZE_RE = re.compile(r"(\d+)\s*[xX]\s*(\d+)")


def _link_tags(html):
    for tag in LINK_RE.findall(html[:300000]):
        yield {k.lower(): v for k, v in ATTR_RE.findall(tag)}


def _find_icon_links(html, base):
    """按质量排序的候选图标：矢量/sizes 大的优先，apple-touch-icon 次之。"""
    scored = []
    for attrs in _link_tags(html):
        rel = (attrs.get("rel") or "").lower()
        href = (attrs.get("href") or "").strip()
        if not href or "icon" not in rel:
            continue
        url = urljoin(base, href)
        score = 0
        if "apple-touch-icon" in rel:
            score = 180                       # 一般是 180x180 png，缩到 48px 也清楚
        elif "mask-icon" in rel:
            score = 1                         # 单色 svg，多数场景不好看，排最后
        else:
            m = SIZE_RE.search(attrs.get("sizes") or "")
            if m:
                score = max(16, min(int(m.group(1)), 512))
            low = url.lower().split("?")[0]
            typ = (attrs.get("type") or "").lower()
            if low.endswith(".svg") or "svg" in typ:
                score += 64                   # 矢量图任意尺寸都清晰
            elif low.endswith(".png") or "png" in typ:
                score += 8
        scored.append((score, url))
    scored.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _, url in scored:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _manifest_icons(html, base, client, timeout=None):
    """<link rel=manifest> 里的 icons：新版 SPA 面板常常只在这里声明图标。"""
    out = []
    for attrs in _link_tags(html):
        if "manifest" not in (attrs.get("rel") or "").lower():
            continue
        href = (attrs.get("href") or "").strip()
        if not href:
            continue
        try:
            r = client.get(urljoin(base, href), timeout=timeout)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception:
            continue
        icons = data.get("icons") if isinstance(data, dict) else None
        if not isinstance(icons, list):
            continue
        scored = []
        for it in icons:
            if not isinstance(it, dict) or not it.get("src"):
                continue
            m = SIZE_RE.search(it.get("sizes") or "")
            score = int(m.group(1)) if m else 0
            if str(it.get("src")).lower().split("?")[0].endswith(".svg"):
                score += 64
            scored.append((score, urljoin(base, str(it["src"]))))
        scored.sort(key=lambda x: -x[0])
        out.extend(u for _, u in scored)
    return out


def _is_public_host(base_url):
    """内网 IP / 单段主机名 / .lan 之类不走在线图标库（服务端也取不到）。"""
    host = (urlparse(base_url).hostname or "").lower()
    if not host or "." not in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False                        # 任何字面 IP 都不试
    except ValueError:
        pass
    return not host.endswith(LOCAL_TLDS)


def fetch_online_icon(base_url, timeout=8.0):
    """公共图标库兜底：站点被 Cloudflare 拦 / 只在内网可达时，用域名换图标。"""
    host = (urlparse(base_url).hostname or "").lower()
    if not _is_public_host(base_url):
        return None, "非公网域名，跳过在线图标库"
    url = ONLINE_ICON_URL.format(host=host)
    try:
        with httpx.Client(timeout=timeout, headers=HEADERS, verify=False) as client:
            data, ext, why = _get_image(client, url)
    except Exception as e:
        return None, type(e).__name__
    if not data:
        return None, why or "在线图标库无响应"
    if hashlib.md5(data).hexdigest() == ONLINE_PLACEHOLDER_MD5:
        return None, "在线图标库只有占位图（说明它也没取到该站图标）"
    if ext == "svg" and len(data) < 600 and b">f</text>" in data:
        return None, "在线图标库只有占位图"
    return save_bytes(data, ext), "在线图标库 favicon.im"


def fetch_favicon(base_url, timeout=5.0, online=True):
    """返回 (filename, 来源说明) 或 (None, 失败原因汇总)。

    顺序：首页 HTML 里的图标声明（质量最好）→ 常见静态路径 → 换协议再试
    → 在线图标库兜底（仅公网域名）。每步失败原因都会汇总到返回值里，
    前端直接展示，用户能看出到底是 404、还是要登录（401/403）。
    """
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        return None, "无地址"
    if not base_url.startswith(("http://", "https://")):
        base_url = "http://" + base_url
    tried = []
    # 被墙/黑洞的站点每个请求都要等满 timeout，这里给「站内探测」一个总预算，
    # 用完就提前进入兜底，避免一条卡片卡住几十秒（批量重抓时尤其明显）。
    budget = max(6.0, timeout * 2)
    deadline = time.time() + budget

    def left():
        return max(0.8, deadline - time.time())

    def note(what, why):
        if why:
            tried.append("%s → %s" % (what, why))

    with httpx.Client(timeout=timeout, headers=HEADERS, verify=False) as client:
        # 1) 首页 HTML：<link rel=icon> / apple-touch-icon / manifest
        html = ""
        root_status = 0
        unreachable = False
        try:
            r = client.get(base_url, timeout=left())
            root_status = r.status_code
            ct = (r.headers.get("content-type") or "").lower()
            if r.status_code == 200 and ("html" in ct or not ct):
                html = r.text
            else:
                note("首页", "HTTP %d" % r.status_code)
        except Exception as e:
            note("首页", type(e).__name__)
            # 连不上（连接超时/拒绝/DNS 失败）：本机到该站根本不通，再试 7 个静态
            # 路径纯属浪费时间，直接交给在线图标库兜底。
            unreachable = type(e).__name__ in (
                "ConnectTimeout", "ConnectError", "ReadTimeout", "RemoteProtocolError")

        if html:
            for u in _find_icon_links(html, base_url + "/")[:5]:
                if deadline - time.time() < 0.8:
                    break
                f, why = _download(client, u, left())
                if f:
                    return f, "站点声明的图标"
                note(urlparse(u).path or u, why)
            for u in _manifest_icons(html, base_url + "/", client, left())[:2]:
                if deadline - time.time() < 0.8:
                    break
                f, why = _download(client, u, left())
                if f:
                    return f, "manifest 里的图标"
                note("manifest 图标", why)

        # 2) 常见静态路径（站点从本机根本不通就跳过）
        for path in (() if unreachable else COMMON_PATHS):
            if deadline - time.time() < 0.8:
                note("站内探测", "超时预算用完")
                break
            f, why = _download(client, base_url + path, left())
            if f:
                return f, path.lstrip("/")
            note(path, why)
            # 首页和 favicon 同时 401/403：整站被挡（Cloudflare 盾 / 登录墙），
            # 剩下几个静态路径也必然一样，直接省掉，尽快走在线图标库兜底。
            if path == "/favicon.ico" and root_status in (401, 403) and why in ("HTTP 401", "HTTP 403"):
                note("站内探测", "整站被挡（需要登录或反爬）")
                break

        # 3) 换协议再来一轮（http-only 的内网盒子上很常见）
        alt = ("http://" if base_url.startswith("https://") else "https://") + base_url.split("://", 1)[1]
        if not unreachable and _safe(alt)[0] and deadline - time.time() > 1.0:
            for path in ("/favicon.ico", "/apple-touch-icon.png"):
                if deadline - time.time() < 0.8:
                    break
                f, why = _download(client, alt + path, left())
                if f:
                    return f, alt.split("://")[0] + path
                note(alt.split("://")[0] + path, why)

    # 4) 在线图标库兜底
    if online:
        f, why = fetch_online_icon(base_url)
        if f:
            return f, why
        note("在线图标库", why)

    # 同一原因别重复列太多次
    uniq, seen = [], set()
    for t in tried:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return None, "；".join(uniq[:3]) or "未找到图标"


def list_icons():
    if not os.path.isdir(ICON_DIR):
        return []
    return sorted(os.listdir(ICON_DIR))


def icon_path(name):
    name = os.path.basename(name or "")
    p = os.path.join(ICON_DIR, name)
    return p if os.path.isfile(p) else None


def guess_mime(name):
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    return {
        "ico": "image/x-icon", "png": "image/png", "jpg": "image/jpeg",
        "jpeg": "image/jpeg", "gif": "image/gif", "svg": "image/svg+xml",
        "webp": "image/webp", "bmp": "image/bmp", "avif": "image/avif",
    }.get(ext, "application/octet-stream")


# ---------------------------------------------------------------- 缩略图
# 卡片上的图标只显示 32~48px，但用户上传的图常常是几 MB 的原图
# （实测有一张 1.77MB 的 PNG 占首屏 60% 流量）。这里按需生成 128px 的 webp，
# 生成一次落盘复用；失败/太小/矢量的图直接回原图，绝不因为缩图把图标弄丢。
THUMB_DIR = (os.environ.get("NAVPAGE_ICON_THUMB_DIR")
             or os.path.join(os.path.dirname(ICON_DIR.rstrip("/")), "iconthumbs"))
THUMB_PX = 128
THUMB_SKIP_BYTES = 48 * 1024      # 本来就小的图不值得转
THUMB_MIME = "image/webp"


def thumb_path(name, max_px=THUMB_PX):
    """返回可直接返回给浏览器的图标路径。原图很小或转换失败时返回原图。"""
    src = icon_path(name)
    if not src:
        return None
    low = src.lower()
    if low.endswith(".svg"):
        return src
    try:
        st = os.stat(src)
    except OSError:
        return src
    if st.st_size <= THUMB_SKIP_BYTES:
        return src
    key = hashlib.sha1(("%s|%d|%d|%d" % (os.path.basename(src), st.st_size,
                                         int(st.st_mtime), max_px)).encode()).hexdigest()[:16]
    dst = os.path.join(THUMB_DIR, key + ".webp")
    if os.path.exists(dst):
        return dst
    try:
        from PIL import Image
    except Exception:
        return src                      # 没装 Pillow 就退回原图，功能不受影响
    tmp = dst + ".tmp"
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
        im = Image.open(src)
        # 多帧的 ico/gif 取信息量最大的一帧
        try:
            im.seek(getattr(im, "n_frames", 1) - 1)
        except Exception:
            pass
        im = im.convert("RGBA")
        im.thumbnail((max_px, max_px), Image.LANCZOS)
        im.save(tmp, "WEBP", quality=84, method=4)
        if os.path.getsize(tmp) >= st.st_size:
            os.unlink(tmp)
            return src                  # 转完反而更大（小尺寸 ico），用原图
        os.replace(tmp, dst)
        return dst
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return src
