"""外网地址导入：把粘贴进来的文本解析成「域名 → 内网服务」映射。

设计目标（2026-10-19）：
  * 不假设列顺序 —— Lucky v2 的表格、v3 的子规则卡片、手写的一行一条都能吃
  * 认得出：完整 URL、裸域名、IPv4:端口、容器名:端口、全角冒号/箭头
  * 解析失败不报错，交给前端预览让用户改；宁可多给一行也别丢规则

只依赖标准库，方便单测。
"""
from __future__ import annotations

import re

# 全角/中文标点统一成 ASCII，箭头类分隔符变空格
_TRANS = str.maketrans({
    "：": ":", "｜": "|", "／": "/", "＠": "@", "，": ",", "。": ".",
    "　": " ", "→": " ", "⇒": " ", "—": " ", "－": "-",
})

# 域名：允许二级以上，TLD 必须是字母（这样 192.168.1.109 不会被当成域名）
_DOMAIN = re.compile(
    r"(?<![a-z0-9@._-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,24}))(?![a-z0-9-])",
    re.I,
)
_URL = re.compile(r"(https?)://([^\s/?#\"'<>]+)(?::(\d{1,5}))?", re.I)
_IP_PORT = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})(?![\d])")
_HOST_PORT = re.compile(
    r"(?<![a-z0-9@._-])([a-z0-9][a-z0-9._-]{0,62}):(\d{1,5})(?![\d])", re.I
)

# 表头/状态词，用来判断一行是不是纯表头
_HEADER_WORDS = (
    "域名", "后端", "前端", "地址", "端口", "名称", "状态", "规则", "监听",
    "证书", "分组", "路径", "协议", "备注", "domain", "backend", "target",
    "host", "port", "name", "status",
)
_SKIP_EXACT = {
    "http", "https", "tcp", "udp", "ws", "wss", "tls", "ssl",
    "启用", "停用", "已启用", "未启用", "开启", "关闭", "正常", "异常",
}
# 明显不是域名的（配置文件名/常见技术后缀）
_BAD_TLDS = {"js", "css", "json", "png", "jpg", "jpeg", "gif", "svg", "ico",
             "html", "htm", "txt", "zip", "gz", "conf", "log", "yml", "yaml"}


def _clean(line: str) -> str:
    s = line.translate(_TRANS).replace("\t", "  ")
    return re.sub(r"\s{2,}", "  ", s).strip()


def _is_header(line: str) -> bool:
    """整行都是表头词（没有域名、没有地址）→ 跳过"""
    low = line.lower()
    words = [w for w in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", low) if w]
    if not words:
        return True
    return all(any(w == h or w in h or h in w for h in _HEADER_WORDS) for w in words)


def _domains(line: str) -> list[str]:
    out = []
    for m in _DOMAIN.finditer(line):
        d = m.group(1).lower()
        tld = m.group(2).lower()
        if tld in _BAD_TLDS or d in _SKIP_EXACT:
            continue
        if d not in out:
            out.append(d)
    return out


def _pick_backend(line: str, domains: list[str]) -> tuple[str, int]:
    """挑内网后端地址。IP:port 优先，其次容器名:port；域名:port 视为前端监听端口。"""
    for m in _IP_PORT.finditer(line):
        return m.group(1), int(m.group(2))
    for m in _HOST_PORT.finditer(line):
        host, port = m.group(1).lower(), int(m.group(2))
        if host in domains or _DOMAIN.fullmatch(host):
            continue          # emby.example.com:443 —— 这是前端地址
        if host in _SKIP_EXACT:
            continue
        return host, port
    return "", 0


def _front(line: str, domains: list[str]) -> tuple[str, int]:
    """返回 (scheme, 前端监听端口)。没有就 ('', 0)。"""
    scheme = ""
    port = 0
    for m in _URL.finditer(line):
        host = m.group(2).split(":")[0].lower()
        if host in domains or not domains:
            scheme = m.group(1).lower()
            if m.group(3):
                port = int(m.group(3))
            break
    if not port:
        for m in _HOST_PORT.finditer(line):
            host, p = m.group(1).lower(), int(m.group(2))
            if host in domains:
                port = p
                break
    return scheme, port


def _guess_scheme(scheme: str, front_port: int, line: str) -> str:
    if scheme in ("http", "https"):
        return scheme
    if front_port == 80:
        return "http"
    if front_port in (443, 8443, 9443):
        return "https"
    if re.search(r"https|tls|ssl|证书", line, re.I):
        return "https"
    return "https"      # 外网反代默认按 https，前端可逐行改


def parse(text: str) -> list[dict]:
    """解析文本 → [{domain, scheme, port, backend_host, backend_port, label, raw}]

    port/backend 可能为 0/空：说明这行只有域名（Lucky v3 卡片复制就是这样），
    交给前端按名字兜底匹配。
    """
    rows: list[dict] = []
    seen: set[str] = set()
    pending: list[dict] = []      # 只有域名、还没配上后端的行（可被后一行补上）

    for raw in (text or "").splitlines():
        line = _clean(raw)
        if not line or _is_header(line):
            continue
        domains = _domains(line)
        if not domains:
            # 只有后端地址的行：补给上一行缺后端的规则（多行粘贴）
            host, port = _pick_backend(line, [])
            if host and pending:
                missing = [r for r in pending if not r["backend_port"]]
                if missing:
                    missing[-1].update(backend_host=host, backend_port=port)
                pending = [r for r in pending if not r["backend_port"]]
            continue

        scheme, front_port = _front(line, domains)
        host, port = _pick_backend(line, domains)
        for d in domains:
            if d in seen:
                continue
            seen.add(d)
            row = {
                "domain": d,
                "scheme": _guess_scheme(scheme, front_port, line),
                "port": port,
                "backend_host": host,
                "backend_port": port,
                "label": d.split(".")[0],
                "raw": line[:160],
            }
            rows.append(row)
            if not port:
                pending.append(row)
    return rows


def norm_name(s: str) -> str:
    """名字归一化：只留字母数字，用于域名首段 ↔ 容器名/卡片名的模糊比对"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def build_url(scheme: str, domain: str) -> str:
    return f"{(scheme or 'https')}://{domain}"
