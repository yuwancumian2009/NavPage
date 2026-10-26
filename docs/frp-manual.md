# 手工搭 FRP（不用「远程节点」功能时看这篇）

> 主 README 里推荐的是「设置 → 网关 → 远程节点」一键部署：填个 VPS 的 SSH，frps、通配 DNS、
> 通配证书、frpc 参数全自动。**这篇只给不用那套、或者想自己掌控 frps/反代的人**：
> 自己装 frps、自己写 nginx/Caddy 通配入口、自己签证书，然后让导航页当 frpc 客户端。
>
> 看之前先记住两条铁律：
> 1. frps 必须开 `vhostHTTPPort`（或 `subDomainHost`），否则所有 `type = "http"` 隧道都会被拒；
> 2. `*.你的后缀` 必须有通配 A 记录指向 frps，否则隧道 online 但浏览器打不开。
>
> 报错对照表在最末。想省事就回主 README 用一键部署。

---

## 总览：先决条件

导航页只负责**客户端那一半**（生成隧道、写地址、卡片可切 FRP）。公网那台跑 `frps` 的服务器，
必须先把两件事准备好，否则隧道一定注册失败：

1. **frps 要开 vhost HTTP 端口**（`vhostHTTPPort` 或 `subDomainHost` 二选一）。
   只开 `bindPort = 7000` 的 frps 会拒掉所有 `type = "http"` 的隧道，日志里是
   `type [http] not supported when vhost http port is not set`。
2. **域名要能解析到 frps**：给 `*.你的后缀` 加一条通配 A 记录（这一步最容易漏，浏览器会直接报 DNS 错误）。

然后按你的网络形态选一种：

### 场景 A：frps 直接对公网开 7000（最省事）

```toml
# /etc/frp/frps.toml
bindPort = 7000
vhostHTTPPort = 80                              # HTTP 隧道入口（外面能直连 80 就用它）
subDomainHost = "frp.example.com"               # DNS: *.frp.example.com → 这台服务器
auth.method = "token"
auth.token = "换成你的token"
```

导航页「设置 → FRP」：服务器 `frps.example.com`、端口 `7000`、Token 同上、
**域名后缀** `frp.example.com`、协议 `tcp`。保存后每张卡片得到 `http://容器名.frp.example.com`。

### 场景 B：frps 藏在 443 反代后面（只有 443 出网，最常见于 1Panel/宝塔服务器）

这种形态要用 **wss** 连 443，并用 **customDomains**（不是 subDomainHost），因为域名是反代层接的：

```toml
# frps.toml
bindPort = 7000
vhostHTTPPort = 8080            # 只给本机 nginx 用，防火墙不要放行
auth.method = "token"
auth.token = "换成你的token"
```

```nginx
# 反代服务器上的通配入口：<任意子域>.frp.example.com → frps 的 vhost 端口
server {
    listen 443 ssl;
    http2 on;
    server_name ~^[a-z0-9][a-z0-9-]*\.frp\.example\.com$;
    ssl_certificate     /path/fullchain.pem;    # 需要 *.frp.example.com 通配证书
    ssl_certificate_key /path/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;             # 必须原样带 Host，frps 靠它选隧道
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $http_connection;
        proxy_http_version 1.1;
    }
}

# frpc 的 wss 连接本身也要一条：443 → frps 的 bindPort
server {
    listen 443 ssl;
    http2 on;
    server_name frp.example.com;
    # ... ssl 同上 ...
    location / {
        proxy_pass http://127.0.0.1:7000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

导航页「设置 → FRP」：服务器 `frp.example.com`、端口 **443**、协议 **wss**、
Token 同上、**自定义域名根后缀** `frp.example.com`、地址用 **https**、vhost HTTPS 端口 `443`，
可选「代理名前缀」（同一个 frps 上还有别的 frpc 客户端时必须填，否则 `proxy already exists`）。
每张卡片得到 `https://容器名.frp.example.com`。

### 场景 D：裸 VPS，不装 1Panel / 宝塔（frps + Caddy 两个容器）

**导航页这条链路不依赖任何面板**：它只管两件事 —— 用 token 连上 frps，再提交
`type="http"` + `customDomains` 的隧道。真正需要的只有三样：frps 开了 vhost HTTP 端口、
DNS 有通配记录、80/443 上有个「按 Host 转发」的入口。这个入口是 nginx、Caddy、Traefik 还是
1Panel 都无所谓。

**D1 · 最省事：让 frps 自己占 80，不要证书**

```toml
# frps.toml
bindPort = 7000
vhostHTTPPort = 80          # 直接对公网开 80，省掉反代和证书
auth.method = "token"
auth.token = "换成你的token"
webServer.addr = "127.0.0.1"
webServer.port = 7500
webServer.user = "admin"
webServer.password = "换一个"
```

```yaml
# docker-compose.yml
services:
  frps:
    image: fatedier/frps:v0.61.1
    network_mode: host
    restart: always
    volumes: [./frps.toml:/etc/frp/frps.toml]
    command: ["-c", "/etc/frp/frps.toml"]
```

防火墙放行 `7000`、`80`；DNS 加 `*.frp.example.com → VPS IP`。
导航页：端口 `7000`、协议 `tcp`、地址用 **http**、vhost HTTP 端口 `80`、域名后缀 `frp.example.com`。
得到 `http://容器名.frp.example.com`。缺点：浏览器标「不安全」，个别应用（PWA、剪贴板 API）会受限。

**D2 · 要 https：加一个 Caddy，通配证书自动签、自动续**

Caddy 一个容器顶掉 nginx + certbot，通配证书走 DNS-01（不需要开 80 验证）：

```yaml
services:
  frps:
    image: fatedier/frps:v0.61.1
    network_mode: host
    restart: always
    volumes: [./frps.toml:/etc/frp/frps.toml]
    command: ["-c", "/etc/frp/frps.toml"]

  caddy:
    image: caddy:2
    network_mode: host
    restart: always
    environment:
      CF_API_TOKEN: "你的 Cloudflare Token（Zone.DNS 编辑权限）"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data

volumes:
  caddy_data:
```

```
# Caddyfile —— 官方 caddy:2 镜像自带 cloudflare 插件
*.frp.example.com {
    tls {
        dns cloudflare {env.CF_API_TOKEN}
    }
    reverse_proxy 127.0.0.1:8080 {
        header_up Host {host}      # frps 靠 Host 选隧道，必须原样带
        flush_interval -1          # 关缓冲：Emby/Jellyfin 拉流、日志实时输出都要
    }
}

# frpc 的 wss 握手也要一条
frp.example.com {
    tls {
        dns cloudflare {env.CF_API_TOKEN}
    }
    reverse_proxy 127.0.0.1:7000
}
```

frps 这边把 `vhostHTTPPort = 8080`（只给本机 Caddy 用，不对外），`bindPort = 7000`。
防火墙只放 `80`、`443`、`7000`（用 wss 时 7000 也可以只对本机/内网开）。
导航页填法同场景 B：端口 `443`、协议 `wss`、地址用 **https**、vhost HTTPS 端口 `443`。

> 别用 frps 自带的 `vhostHTTPSPort = 443` 顶替 Caddy：它用自签证书，浏览器会一直报警，
> 而且没法塞自己的证书进去。

### 场景 C：没有域名

FRP 这一档只做 `type = "http"` 的隧道，**必须有域名**。没有域名的只能自己手写
TCP 型隧道（`remotePort`），或者在「设置 → 网关」用反代 + 端口访问。

### 场景 E：交给导航页自动部署（2.0.2 起，推荐）

设置 → **远程节点** → 填一台裸 VPS 的 SSH → 点「部署 / 更新」，剩下的它来做：

| 步骤 | 做了什么 |
|---|---|
| 体检 | SSH 登录、root/docker 权限、`docker compose` 版本、公网出口 IP、端口占用 |
| 落文件 | `/opt/navpage-node/`：`frps.toml` + `docker-compose.yml`（Caddy 形态另有 `Caddyfile`、`certs/`） |
| 起服务 | `docker compose -p navpage-node up -d`，容器 `restart: always` |
| 探活 | 从 VPS 本机 `curl` vhost 端口，返回 404 = frps 活着（没有匹配的隧道） |
| DNS | 用「设置 → 网关 → DNS 凭据」自动补 `<域名后缀>` 与 `*.<域名后缀>` 的 A 记录 |
| 证书 | Caddy 形态下用内置 ACME 签 `*.<域名后缀>`，推到节点 `certs/` 并 reload Caddy |

要填的只有：服务器 IP、SSH 端口/用户、密码或私钥、接入形态（Caddy / 纯 HTTP）、FRP 域名后缀。

**必须自己做的两件事**（工具改不了）：

1. 云厂商安全组放行端口 —— Caddy 形态 `22 + 443 + 7000`（443 站点、7000 frpc 控制连接）；纯 HTTP 形态 `22 + 80 + 7000`。填了非标准端口就换成那个。
2. 域名 NS 已指向「DNS 凭据」里那个服务商，否则自动补的记录不生效。

部署完点卡片上的「用这个节点」：serverAddr / 端口 / 协议 / token / 域名后缀一次性写进 FRP 客户端并重载。
重复部署是幂等的（换端口、换 token、换域名都会重建容器）；证书续期后可单独点「同步证书」推一次。

### 排错顺序（按这个查，比猜快）

| 现象 | 原因 |
|---|---|
| 隧道起不来，日志 `vhost http port is not set` | frps 没配 `vhostHTTPPort` / `subDomainHost`，见上文第 1 条 |
| 日志 `proxy already exists` | 代理名和同 frps 上别的 frpc 撞了，填「代理名前缀」 |
| 连接超时 / EOF | 端口填错（443 却用 tcp，或 7000 却用 wss）；容器里若带 `HTTP_PROXY` 环境变量也会把隧道连接塞进代理，务必清掉 |
| 隧道 online 但访问 404 | 域名没走通配入口（nginx 正则没写或没 reload），或 DNS 没解析到 frps |
| 访问 502 / 空白 | 隧道通了但目标服务本身没起，先在局域网用 IP:端口 验证 |
| 某个站点 400（如 Home Assistant） | 上游不认 `X-Forwarded-For`；在通配入口里 `proxy_set_header X-Forwarded-For "";` 清掉，或在应用侧配置可信代理 |
