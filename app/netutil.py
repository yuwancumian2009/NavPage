"""客户端 IP 识别、内网判定、地址拼装。"""
import ipaddress

DEFAULT_PORTS = {80, 443, 3000, 5000, 5001, 8000, 8001, 8080, 8081, 8088, 8090,
                 8096, 8443, 8888, 9000, 9090, 32400, 25600}


def parse_cidrs(text):
    nets = []
    for part in (text or "").replace(";", ",").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except Exception:
            continue
    return nets


def is_lan(ip, cidrs_text):
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except Exception:
        return False
    for net in parse_cidrs(cidrs_text):
        if addr.version != net.version:
            continue
        if addr in net:
            return True
    return False


def client_ip(request, trust_proxy=False):
    """返回 (ip, source)。trust_proxy 时优先 X-Forwarded-For / X-Real-IP。"""
    if trust_proxy:
        xff = request.headers.get("x-forwarded-for") or ""
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first, "xff"
        xri = request.headers.get("x-real-ip")
        if xri:
            return xri.strip(), "x-real-ip"
    if request.client and request.client.host:
        return request.client.host, "peer"
    return "", "none"


def host_of(request, trust_proxy=False):
    """取本次请求的访问主机名（不含端口）。trust_proxy 时优先 X-Forwarded-Host。"""
    raw = ""
    if trust_proxy:
        raw = request.headers.get("x-forwarded-host") or ""
    raw = raw or request.headers.get("host") or ""
    return _hostname_of(raw)


def match_host_list(host, text):
    """host 是否命中逗号分隔的主机名列表。支持 *.example.com 通配。"""
    h = (host or "").strip().lower()
    if not h:
        return False
    for pat in (text or "").replace(";", ",").replace("\n", ",").split(","):
        pat = pat.strip().lower()
        if not pat:
            continue
        if pat.startswith("*."):
            if h.endswith(pat[1:]) and h != pat[2:]:
                return True
        elif h == pat:
            return True
    return False


def resolve_mode(request, settings):
    """返回 (mode, client_ip, source)。

    判定优先级（靠前的赢）：

      1. 自定义请求头命中 —— 给 frp 用（frps 配 requestHeaders.set），最明确
      2. 访问主机名命中「外网域名」列表 —— frp 用域名访问时靠这条
      3. 访问主机名本身是 IP —— 公网 IP 算外网、内网 IP 算内网
      4. 访问主机名是域名但没配进列表 —— 按 domain_means_wan 决定（默认算外网）
      5. 客户端 IP 判定（需要 auto_switch 开启）
      6. default_mode

    为什么要按「访问主机名」判：frp/反代之后 request.client.host 是 frpc 的
    内网地址（或 127.0.0.1），光看客户端 IP 会把外网访问误判成内网。
    """
    trust = bool(settings.get("trust_proxy"))
    ip, ip_src = client_ip(request, trust)
    host = host_of(request, trust)

    # 1) 自定义请求头（frp requestHeaders.set 注入）
    hname = (settings.get("frp_header_name") or "").strip()
    if hname:
        got = (request.headers.get(hname.lower()) or "").strip()
        want = (settings.get("frp_header_value") or "").strip()
        if got and (not want or got.lower() == want.lower()):
            return "wan", ip, "header:" + hname

    # 2) 外网域名列表
    if match_host_list(host, settings.get("wan_hosts")):
        return "wan", ip, "host:" + host

    if host:
        # 3) 主机名是 IP
        try:
            addr = ipaddress.ip_address(host)
        except Exception:
            addr = None
        if addr is not None:
            private = addr.is_private or addr.is_loopback or addr.is_link_local
            return ("lan" if private else "wan"), ip, "host:" + host
        # 4) 域名：内网一般直接用 IP 访问，用域名多半走了反代/公网
        if settings.get("domain_means_wan", True):
            return "wan", ip, "host:" + host
        # 4b) 关掉「域名=外网」时，只有配了「内网地址」才能拼出真正的内网链接。
        #     没配内网地址还判内网，只会得到套着外网域名的内网链接（自相矛盾），
        #     所以这种情况一律按外网算，保证「判定」和「点开的地址」永远一致。
        #     frp 后面拿不到真实客户端 IP，别指望第 5 步兜底（隧道里是 frpc 的私网地址）。
        if not (settings.get("host_lan_ip") or "").strip():
            return "wan", ip, "host:" + host

    # 5) / 6) 退回客户端 IP
    if not settings.get("auto_switch", True):
        mode = settings.get("default_mode") or "auto"
        if mode not in ("lan", "wan"):
            mode = "lan"
        return mode, ip, ip_src
    return ("lan" if is_lan(ip, settings.get("lan_cidrs")) else "wan"), ip, ip_src


def build_url(host, port, scheme="http"):
    host = (host or "").strip()
    if not host:
        return ""
    if "://" in host:
        return host.rstrip("/")
    port = int(port or 0)
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return f"{scheme}://{host}"
    if port:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


# ---------------------------------------------------------------- 主机名推导
#
# 容器里拿不到 NAS 的局域网 IP（默认路由源地址是 bridge 地址，如 192.168.32.3），
# 所以内网地址一律写成 `{scheme}://{host}:{port}` 模板，在**请求时**按浏览器
# 实际访问的 Host 头展开：用 192.168.1.10:8095 访问 → {host}=192.168.1.10。

def _hostname_of(host_header):
    h = (host_header or "").strip()
    if not h:
        return ""
    if h.startswith("["):                      # IPv6: [fe80::1]:8080
        return h[1:].split("]")[0]
    return h.split(":")[0]


def host_context(request, settings):
    """推导 {host} 的取值。

    优先级：设置里的显式覆盖 > 请求 Host 头（按 内网/公网 判断归属）> 空。
    """
    raw = ""
    if settings.get("trust_proxy"):
        raw = request.headers.get("x-forwarded-host") or ""
    raw = raw or request.headers.get("host") or ""
    host = _hostname_of(raw)

    ctx = {
        "host": host,
        "host_is_ip": False,
        "host_private": False,
        "lan_base": (settings.get("host_lan_ip") or "").strip(),
        "wan_base": (settings.get("host_wan") or "").strip(),
        "wan_scheme": settings.get("wan_scheme") or "https",
    }

    if not host:
        return ctx
    try:
        addr = ipaddress.ip_address(host)
    except Exception:
        # 域名：一般走公网入口，只当作外网主机名
        ctx["wan_base"] = ctx["wan_base"] or host
        return ctx

    ctx["host_is_ip"] = True
    private = addr.is_private or addr.is_loopback or addr.is_link_local
    ctx["host_private"] = private
    if private:
        ctx["lan_base"] = ctx["lan_base"] or host
    else:
        ctx["wan_base"] = ctx["wan_base"] or host
    return ctx


def expand_url(raw, mode, ctx):
    """展开 {host} / {scheme} 占位符。没有占位符的地址原样返回。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "{host}" not in raw and "{scheme}" not in raw:
        return raw

    base = ctx.get("lan_base" if mode == "lan" else "wan_base") or ""
    if not base and mode == "wan":
        # 外网模式缺外网主机名时退回内网基址：至少还是个能打开的链接
        base = ctx.get("lan_base") or ""
    if not base:
        # 内网模式缺内网基址时**绝不能**借用外网域名 —— 那会产出
        # 「判定内网、点开却是外网域名」的自相矛盾地址（2026-10-15 用户报的问题）。
        # 宁可返回空串，让调用方按「本模式不可用」处理（availability 才诚实）。
        return ""
    scheme = "http" if mode == "lan" else (ctx.get("wan_scheme") or "https")
    return raw.replace("{host}", base).replace("{scheme}", scheme).rstrip("/")


def normalize_url(raw):
    """把「只填了端口」的残缺值补成模板：':9091' -> '{scheme}://{host}:9091'。

    历史原因（前端拼接缺前缀、早期入口不规范）库里存过这种值：渲染和抓图标两处
    各自打过补丁兜住，但**存的值始终残缺** —— 管理台地址框显示为空（看着像「没填
    占位符」），批量刷新图标还会拿 ':9091' 去请求、直接失败。统一在这里补全。
    """
    raw = (raw or "").strip()
    if raw.startswith(":"):
        return "{scheme}://{host}" + raw
    return raw


def template_url(port, scheme="http"):
    """扫描时生成的地址模板。"""
    if not port:
        return ""
    return f"{{scheme}}://{{host}}:{int(port)}"


def pick_primary_port(ports):
    """ports: [{'private':int,'public':int,'type':str}]，挑最像 Web 的公开端口。"""
    tcp = [p for p in ports if (p.get("type") or "tcp") == "tcp" and p.get("public")]
    if not tcp:
        return None
    for p in tcp:
        if p["private"] in DEFAULT_PORTS:
            return p["public"]
    for p in tcp:
        if p["public"] in DEFAULT_PORTS:
            return p["public"]
    return tcp[0]["public"]
