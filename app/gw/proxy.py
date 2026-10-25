"""内嵌反向代理：一个端口收所有域名，按 Host 转发到内网容器。

为什么不套 nginx/caddy：
  * 规则要跟卡片实时联动（改一个卡片就该改一条路由），外部进程要么重载配置要么走 API，
    都不如进程内一张 dict 直接；改完下一次请求就生效，没有 reload 延迟；
  * 容器里再塞一个 nginx 会多一份配置语法要维护、多一个进程要守护。

能力：
  * HTTP / HTTPS 双监听，HTTPS 按 SNI 选证书（不同域名不同证书），证书热加载；
  * WebSocket 透传（Emby/Jellyfin/qBittorrent/MoviePilot 都靠它）；
  * 大响应流式转发，不落内存；
  * 自动带 X-Forwarded-For / -Proto / -Host，后端生成链接不会退回内网地址；
  * ACME http-01 挑战文件直接从内存字典出，不用落盘。

没匹配到域名时返回一张人话报错页（含当前已配置的域名列表），方便排查 DNS 指错。
"""
from __future__ import annotations

import asyncio
import ssl
import time
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def _clean_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        out[k] = v
    return out


class ProxyEngine:
    def __init__(self, routes_provider, cert_store=None, log=None, waf=None):
        """routes_provider() -> dict[host] = "http://ip:port"（每次请求都会取，天然热更新）

        waf: app.gw.waf.WAF 实例；每个请求进来先过一遍规则，命中 block 直接 403。
        """
        self.routes_provider = routes_provider
        self.certs = cert_store
        self.waf = waf
        self.log = log or (lambda *a: None)
        self.http_runner: web.AppRunner | None = None
        self.https_runner: web.AppRunner | None = None
        self.error = ""
        self.session: ClientSession | None = None
        self.http_port = 0
        self.https_port = 0
        self.challenges: dict[str, str] = {}
        self.started_at = 0.0
        self.stats = {"requests": 0, "errors": 0, "bytes": 0, "waf_blocked": 0}
        # 最近若干条转发记录，设置页里能直接看到「刚才那个请求去哪了」
        self.recent: list[dict] = []

    # ---------------------------------------------------------------- 生命周期
    def _make_app(self) -> web.Application:
        app = web.Application(client_max_size=1024 ** 3)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    async def start(self, http_port: int, https_port: int) -> dict:
        await self.stop()
        self.session = ClientSession(
            timeout=ClientTimeout(total=600, sock_read=300),
            connector=TCPConnector(limit=200, ssl=False, force_close=False),
            auto_decompress=False,     # 原样转发，避免二次压缩/长度对不上
            trust_env=False,
        )
        app = self._make_app()
        self.http_runner = web.AppRunner(app, access_log=None)
        await self.http_runner.setup()
        try:
            site = web.TCPSite(self.http_runner, "0.0.0.0", int(http_port))
            await site.start()
            self.http_port = int(http_port)
        except OSError as e:
            self.log(f"HTTP 监听 {http_port} 失败：{e}")
            self.http_port = 0

        if https_port:
            ssl_ctx = self.certs.server_context() if self.certs else None
            if ssl_ctx:
                self.https_runner = web.AppRunner(app, access_log=None)
                await self.https_runner.setup()
                try:
                    site = web.TCPSite(self.https_runner, "0.0.0.0", int(https_port), ssl_context=ssl_ctx)
                    await site.start()
                    self.https_port = int(https_port)
                except OSError as e:
                    self.log(f"HTTPS 监听 {https_port} 失败：{e}")
                    self.https_port = 0
        self.started_at = time.time()
        self.log(f"反代已启动：HTTP {self.http_port or '关闭'} / HTTPS {self.https_port or '关闭'}")
        return {"http": self.http_port, "https": self.https_port}

    async def stop(self) -> None:
        for r in (self.http_runner, self.https_runner):
            if r:
                try:
                    await r.cleanup()
                except Exception:
                    pass
        self.http_runner = self.https_runner = None
        if self.session:
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = None
        self.http_port = self.https_port = 0

    # ---------------------------------------------------------------- 请求处理
    async def _handle(self, request: web.Request) -> web.StreamResponse:
        host = (request.host or "").split(":")[0].lower()
        path = request.path

        if path.startswith("/.well-known/acme-challenge/"):
            token = path.rsplit("/", 1)[-1]
            if token in self.challenges:
                return web.Response(text=self.challenges[token], content_type="text/plain")
            return web.Response(status=404, text="challenge not found")

        # WAF：证书挑战之外的所有请求都要先过规则（拦掉的请求不产生后端流量）
        waf_hit = None
        if self.waf is not None:
            try:
                waf_hit = self.waf.check(request, host)
            except Exception as e:      # WAF 自身出错不能把代理打死
                self.log(f"WAF 检查异常（已放行）：{e}")
                waf_hit = None
            if waf_hit and waf_hit.get("action") == "block":
                self.stats["waf_blocked"] += 1
                self._note(host, path, "-", 403, 0, "WAF：" + waf_hit.get("rule", ""))
                return self._waf_block(host, path, waf_hit)

        routes = self.routes_provider() or {}
        target = routes.get(host) or routes.get("*")
        if not target:
            return self._no_route(host, routes)

        self.stats["requests"] += 1
        started = time.time()
        try:
            resp = await self._forward(request, target, host)
            ms = int((time.time() - started) * 1000)
            note = ("WAF 观察：" + waf_hit["rule"]) if waf_hit else ""
            self._note(host, path, target, resp.status, ms, note)
            return resp
        except asyncio.TimeoutError:
            self.stats["errors"] += 1
            self._note(host, path, target, 504, int((time.time() - started) * 1000), "超时")
            return self._err(504, host, target, "后端响应超时")
        except Exception as e:      # 连不上/SSL 错/域名解析失败
            self.stats["errors"] += 1
            self._note(host, path, target, 502, int((time.time() - started) * 1000), str(e)[:120])
            return self._err(502, host, target, str(e))

    async def _forward(self, request: web.Request, target: str, host: str):
        assert self.session is not None
        base = urlsplit(target if "://" in target else "http://" + target)
        url = urlunsplit((base.scheme, base.netloc, request.path, request.query_string, ""))

        headers = _clean_headers(request.headers)
        # Host 保留用户访问的域名：很多面板按 Host 拼绝对链接/判断站点，改成内网地址会串
        headers.pop("Host", None)
        headers["Host"] = request.host
        prev_xff = request.headers.get("X-Forwarded-For", "")
        headers["X-Forwarded-For"] = (prev_xff + ", " if prev_xff else "") + (request.remote or "")
        headers["X-Forwarded-Proto"] = request.scheme
        headers["X-Forwarded-Host"] = request.host
        headers["X-Real-IP"] = request.remote or ""
        if request.scheme == "https":
            headers["X-Forwarded-Port"] = str(self.https_port or 443)

        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._forward_ws(request, url, headers, base)

        body = None
        if request.can_read_body:
            body = await request.read()

        async with self.session.request(
            request.method, url, headers=headers, data=body,
            allow_redirects=False, auto_decompress=False,
        ) as upstream:
            out = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for k, v in upstream.headers.items():
                lk = k.lower()
                if lk in HOP_BY_HOP or lk in ("content-encoding", "content-length"):
                    continue
                if lk == "location":
                    v = self._rewrite_location(v, base, request, host)
                out.headers[k] = v
            # content-length / encoding 按上游原样回，才能让浏览器正确判断流结束
            if upstream.headers.get("Content-Encoding"):
                out.headers["Content-Encoding"] = upstream.headers["Content-Encoding"]
            if upstream.headers.get("Content-Length"):
                out.headers["Content-Length"] = upstream.headers["Content-Length"]
            out.headers["X-Proxied-By"] = "NavPage"
            await out.prepare(request)
            async for chunk in upstream.content.iter_chunked(65536):
                await out.write(chunk)
                self.stats["bytes"] += len(chunk)
            await out.write_eof()
            return out

    async def _forward_ws(self, request: web.Request, url: str, headers: dict, base):
        assert self.session is not None
        ws_server = web.WebSocketResponse(protocols=request.headers.get("Sec-WebSocket-Protocol", "").split(",") if request.headers.get("Sec-WebSocket-Protocol") else ())
        await ws_server.prepare(request)
        try:
            async with self.session.ws_connect(url, headers=headers, protocols=ws_server.ws_protocol and [ws_server.ws_protocol] or (),
                                               autoping=True, heartbeat=30) as ws_client:
                await self._relay(ws_server, ws_client)
        except Exception as e:
            self.log(f"WebSocket 转发失败 {request.host}{request.path}：{e}")
        return ws_server

    async def _relay(self, ws_server: web.WebSocketResponse, ws_client):
        async def a_to_b():
            async for msg in ws_server:
                if msg.type.name == "TEXT":
                    await ws_client.send_str(msg.data)
                elif msg.type.name == "BINARY":
                    await ws_client.send_bytes(msg.data)
                elif msg.type.name in ("CLOSE", "CLOSING", "CLOSED"):
                    await ws_client.close()
                    break

        async def b_to_a():
            async for msg in ws_client:
                if msg.type.name == "TEXT":
                    await ws_server.send_str(msg.data)
                elif msg.type.name == "BINARY":
                    await ws_server.send_bytes(msg.data)
                elif msg.type.name in ("CLOSE", "CLOSING", "CLOSED"):
                    await ws_server.close()
                    break

        await asyncio.gather(a_to_b(), b_to_a(), return_exceptions=True)

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _rewrite_location(loc: str, base, request: web.Request, host: str) -> str:
        """后端常把 Location 写成自己的内网地址，改回对外的域名，否则跳转会把用户带到 192.168.x.x。"""
        try:
            if loc.startswith("/") or loc.startswith("#"):
                return loc
            u = urlsplit(loc)
            internal = {base.netloc.lower(), (base.hostname or "").lower()}
            if u.netloc.lower() in internal:
                return urlunsplit((request.scheme, request.host, u.path, u.query, u.fragment))
        except Exception:
            pass
        return loc

    def _note(self, host, path, target, status, ms, err=""):
        self.recent.insert(0, {"t": int(time.time()), "host": host, "path": path[:120],
                               "target": target, "status": status, "ms": ms, "err": err})
        del self.recent[60:]

    def _err(self, status, host, target, detail) -> web.Response:
        html = f"""<!doctype html><meta charset=utf-8>
<title>{status} · 反代</title>
<style>body{{font:15px/1.7 -apple-system,'Segoe UI',sans-serif;background:#0f1115;color:#e6e8ee;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.c{{max-width:560px;padding:32px;background:#171a21;border:1px solid #2a2f3a;border-radius:14px}}
h1{{font-size:20px;margin:0 0 12px}}code{{background:#0f1115;padding:2px 6px;border-radius:6px;color:#7dd3fc}}</style>
<div class=c><h1>{status} · 反代无法访问后端</h1>
<p>域名 <code>{host}</code> 已匹配到 <code>{target}</code>，但转发失败。</p>
<p style="color:#fca5a5">{detail}</p>
<p style="color:#9aa4b2">常见原因：容器没启动、端口写错、后端只监听 127.0.0.1（容器里要监听 0.0.0.0）、
或者目标实际是 HTTPS 而这里按 HTTP 连。</p></div>"""
        return web.Response(status=status, text=html, content_type="text/html")

    def _waf_block(self, host, path, hit: dict) -> web.Response:
        html = f"""<!doctype html><meta charset=utf-8><title>403 · 被 WAF 拦截</title>
<style>body{{font:15px/1.7 -apple-system,'Segoe UI',sans-serif;background:#0f1115;color:#e6e8ee;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.c{{max-width:560px;padding:32px;background:#171a21;border:1px solid #2a2f3a;border-radius:14px}}
h1{{font-size:20px;margin:0 0 12px}}code{{background:#0f1115;padding:2px 6px;border-radius:6px;color:#fca5a5}}</style>
<div class=c><h1>403 · 请求被 WAF 拦截</h1>
<p>域名 <code>{host}</code>，路径 <code>{path[:120]}</code>。</p>
<p>命中规则：<code>{hit.get('rule','')}</code>，来源 IP <code>{hit.get('ip','')}</code>。</p>
<p style="color:#9aa4b2">这是 NavPage 内置反代的防火墙判定，请求没有转发到后端。
如果是你自己被拦了，到「设置 → 网关 → WAF 防火墙」把这个 IP 加进白名单。</p></div>"""
        return web.Response(status=403, text=html, content_type="text/html")

    def _no_route(self, host, routes: dict) -> web.Response:
        names = "".join(f"<li><code>{h}</code></li>" for h in sorted(routes)[:40])
        html = f"""<!doctype html><meta charset=utf-8><title>没有匹配的反代规则</title>
<style>body{{font:15px/1.7 -apple-system,'Segoe UI',sans-serif;background:#0f1115;color:#e6e8ee;padding:48px}}
.c{{max-width:640px;margin:0 auto}}code{{background:#171a21;padding:2px 6px;border-radius:6px;color:#7dd3fc}}
h1{{font-size:20px}}</style>
<div class=c><h1>没有找到 <code>{host}</code> 的反代规则</h1>
<p>请求已经到 NavPage 的网关了，但这张卡片还没绑定域名。到「设置 → 网关」给对应的卡片生成一个反代地址即可。</p>
<p>当前已配置的域名：</p><ul>{names or '<li>（空）</li>'}</ul></div>"""
        return web.Response(status=404, text=html, content_type="text/html")

    def status(self) -> dict:
        return {"running": bool(self.http_runner or self.https_runner),
                "http_port": self.http_port, "https_port": self.https_port,
                "started_at": int(self.started_at), "stats": dict(self.stats),
                "error": self.error, "recent": self.recent[:20]}
