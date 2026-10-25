"""2.0.0 内置网关：反代 / 证书 / DDNS / FRP。

设计原则（为什么不用 Lucky 或 Gately）：
  * Lucky 的规则是加密私有格式，第三方读不出来；
  * Gately 要另起一个容器、另配一套端口，卡片和规则永远是两份数据；
  * 用户要的是「一个容器、一份配置」：卡片就是反代规则，改卡片就改路由。

子模块：
  dnsprov   DNS 服务商（证书 DNS-01 + DDNS 共用一套凭据）
  acme      ACME v2 客户端（手写 JWS，不依赖 certbot）
  certs     证书库 + 热加载（SNI 选证书）
  proxy     反向代理（Host 路由、WebSocket、流式转发）
  ddns      公网 IP 同步
  frpc      FRP 客户端进程管理
  service   编排：把上面这些和卡片数据绑在一起
"""
from . import acme, certs, ddns, dnsprov, frpc, proxy, service  # noqa: F401

__all__ = ["acme", "certs", "ddns", "dnsprov", "frpc", "proxy", "service",
           "configure", "instance"]

configure = service.configure
instance = service.instance
