"""图标抓取与缓存。

支持两种来源：
  1) fetch_favicon(base_url)  —— 从站点抓 favicon（先 /favicon.ico，再解析 HTML <link rel=icon>）
  2) fetch_icon_url(url)      —— 抓任意图片 URL（收藏网站用）

带 SSRF 防护：仅允许 http/https，禁止云元数据地址，限制响应体大小。
"""
import os
import re
import io
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


def _download(client, url):
    ok, why = _safe(url)
    if not ok:
        return None, why
    try:
        with client.stream("GET", url, follow_redirects=True) as r:
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            ct = r.headers.get("content-type", "")
            buf = io.BytesIO()
            for chunk in r.iter_bytes():
                buf.write(chunk)
                if buf.tell() > MAX_BYTES:
                    return None, "文件过大"
            data = buf.getvalue()
    except Exception as e:
        return None, f"{type(e).__name__}"
    if not data:
        return None, "空响应"
    ext = _detect_ext(data, ct)
    if not ext:
        return None, "非图片内容"
    return save_bytes(data, ext), ext


def fetch_icon_url(url, timeout=6.0):
    with httpx.Client(timeout=timeout, headers={"User-Agent": UA}) as client:
        return _download(client, url)


LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
ATTR_RE = re.compile(r"""(\w[\w-]*)\s*=\s*["']([^"']*)["']""")


def _find_icon_links(html, base):
    found = []
    for tag in LINK_RE.findall(html[:200000]):
        attrs = {k.lower(): v for k, v in ATTR_RE.findall(tag)}
        rel = (attrs.get("rel") or "").lower()
        if "icon" not in rel:
            continue
        href = attrs.get("href") or ""
        if not href:
            continue
        found.append((attrs.get("sizes") or "", urljoin(base, href)))
    # 有 sizes 的（真图标）优先，其次 apple-touch-icon
    found.sort(key=lambda x: (0 if x[0] else 1))
    return [u for _, u in found]


def fetch_favicon(base_url, timeout=5.0):
    """返回 (filename, ext) 或 (None, reason)"""
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        return None, "无地址"
    with httpx.Client(timeout=timeout, headers={"User-Agent": UA},
                      verify=False) as client:
        f, why = _download(client, base_url + "/favicon.ico")
        if f:
            return f, "favicon.ico"
        html = ""
        try:
            r = client.get(base_url, follow_redirects=True)
            if r.status_code == 200 and "html" in (r.headers.get("content-type") or ""):
                html = r.text
        except Exception:
            pass
        for link in _find_icon_links(html, base_url + "/")[:4]:
            f, why2 = _download(client, link)
            if f:
                return f, link
    return None, why or "未找到图标"


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
