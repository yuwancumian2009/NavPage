# NavPage — 自建 NAS 导航页

[![build image](https://github.com/yuwancumian2009/NavPage/actions/workflows/image.yml/badge.svg)](https://github.com/yuwancumian2009/NavPage/actions/workflows/image.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/yuwancumian2009/navpage)](https://hub.docker.com/r/yuwancumian2009/navpage)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

部署在自己 NAS 上的导航页：自动把 Docker 容器变成卡片，同一张卡同时记内网和外网地址，
按你从哪里访问自动选对的那个。多架构镜像（amd64 / arm64），一条 `docker compose up -d` 就能跑。

- 不想一条条手填地址？扫一下 Docker 容器就有了。
- 在家用 IP、在外面用域名，不想两头改？按客户端 IP 自动切。
- 只有内网、没有公网 IP？给一台便宜 VPS 填个 SSH，**frps、通配 DNS、通配证书、frpc 参数全自动**，卡片直接拿到 `https://容器名.frp.你的域名`。

---

## 功能

内置网关一个容器全包，不用再外挂 Lucky / Gately / Nginx Proxy Manager：

| 能力 | 说明 | 设置位置 |
|---|---|---|
| 反向代理 | 按域名转发到内网容器，支持 WebSocket / 流式响应 | 设置 → 网关 → 反向代理 |
| HTTPS 证书 | ACME DNS-01，自动申请与续期，可签通配符证书 | 设置 → 网关 → 证书 |
| DNS 凭据 | 阿里云 / DNSPod / Cloudflare / 自定义 webhook，证书和 DDNS 共用 | 设置 → 网关 → DNS 凭据 |
| WAF | IP 白黑名单、扫描器 UA、敏感路径、请求方法、CC 限速，带拦截记录 | 设置 → 网关 → WAF |
| DDNS | 取 IP 三选一（在线探测 / 指定网卡 / 手动），变化时更新 A 记录 | 设置 → DDNS |
| FRP 隧道 | 镜像内置 frpc，按卡片自动生成隧道；**可一键部署远端 frps 节点** | 设置 → 网关 → FRP / 远程节点 |
| 卡片地址 | 自动 / 内网 / 反代 / FRP 四档，可逐条固定；域名按卡片名自动生成 | 首页右上角 / 编辑条目 |
| 备份与恢复 | 一个 zip 带走卡片、分类、全部设置、图标库、壁纸、证书，新容器一键还原 | 设置 → 备份与恢复 |

其他：自动发现 Docker 容器并按 compose 项目归类、拖拽排序、大卡/小卡、12 种状态小组件
（下载速度、Emby 正在播放、CPU 温度…）、壁纸与图标库、Compose 管理、镜像更新、搜索框回车当搜索引擎。

---

## 一、先把导航页跑起来

**前置**：Docker 20.10+ 与 Docker Compose v2（`docker compose` 命令），一个空闲端口（默认 8095）。

新建目录放 `docker-compose.yml`（下面这份是只用镜像的最简版，**把密码改掉**）：

```yaml
services:
  navpage:
    image: yuwancumian2009/navpage:latest
    container_name: navpage
    restart: unless-stopped
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
    ports:
      - "8095:8080"      # 面板
      - "8081:8081"      # 内置反代 HTTP（可改，改完设置里也要同步）
      - "18443:18443"    # 内置反代 HTTPS
    environment:
      TZ: Asia/Shanghai
      NAVPAGE_DATA: /data
      NAVPAGE_DEFAULT_PASSWORD: 改成你自己的密码
      DOCKER_HOST: tcp://navpage-socket-proxy:2375
      NAVPAGE_COMPOSE_ROOT: /vol1/1000/docker   # 不用 Compose 管理可以不写
    volumes:
      - ./data:/data
      - /vol1/1000/docker:/vol1/1000/docker     # 必须与宿主机同路径，见下方说明
    depends_on: [socket-proxy]

  socket-proxy:
    image: ccr.ccs.tencentyun.com/bestnav/docker-socket-proxy:latest
    container_name: navpage-socket-proxy
    restart: unless-stopped
    environment:
      CONTAINERS: 1
      VERSION: 1
      PING: 1
      INFO: 1
      IMAGES: 1
      NETWORKS: 1
      VOLUMES: 1
      SESSION: 1       # compose CLI 启动要开 BuildKit session，不放开会被代理 403
      ALLOW_START: 1
      ALLOW_STOP: 1
      POST: 0          # 想用「面板内更新镜像 / Compose 管理」改成 1（等于把容器创建权交给面板）
      EXEC: 0
      BUILD: 0
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

```bash
sudo docker compose pull
sudo docker compose up -d
```

打开 `http://<NAS的局域网IP>:8095`，用初始密码登录。

**部署后三步**：右下角齿轮 →「扫描导入」→ 扫描容器 → 勾选导入；「外观」改标题、传壁纸；
「网络与安全」改密码。

**必改的两处**

| 位置 | 默认 | 说明 |
|---|---|---|
| `NAVPAGE_DEFAULT_PASSWORD` | `changeme` | 必须改。只在库里还没有密码时生效，已有部署改它不会覆盖旧密码；删掉这行则随机生成并打印在容器日志 |
| `ports` | `8095:8080` | 端口冲突就改左边（宿主侧） |

**容器内路径必须与宿主机一致**：`/vol1/1000/docker:/vol1/1000/docker` 里的路径要和宿主机上一模一样，
否则 compose 文件里的相对路径 `./data` 会被解析到容器里，Docker 会建出错误的挂载点。

**不想用 compose**（不要容器扫描和镜像更新时）：单容器 `docker run -d --name navpage -p 8095:8080 -e NAVPAGE_DEFAULT_PASSWORD='你的密码' -v "$PWD/data:/data" --restart unless-stopped yuwancumian2009/navpage:latest`。

---

## 二、没有公网 IP：一键让导航页上外网（VPS + FRP）

这是本项目最省事的玩法：**你只要有一台能 SSH 的公网 VPS 和一个域名，剩下全自动**。
面板会把 frps 装到 VPS、补 `*.frp.你的域名` 通配解析、签通配证书、把参数写回 frpc，
之后每张卡片自动得到 `https://容器名.frp.你的域名`。

### 你要准备的三样

| 东西 | 要求 |
|---|---|
| 一台 VPS | 有公网 IP，能 SSH（root 密码或私钥都行），1 核 512M 就够，Ubuntu 22.04 / Debian 12 最省事 |
| 一个域名 | **DNS 托管在阿里云 / DNSPod / Cloudflare 之一**（要靠 API 自动加记录），例如 `example.com` |
| NAS 能出网 | 导航页容器要能访问外网（HTTPS 出站） |

> 用哪台机器当 frps 都行：甲骨文免费机、搬瓦工、腾讯云轻量都可以。
> 工具会自动处理 VPS 上的 ufw / firewalld，**但云厂商安全组只能你自己点**（见步骤 3）。

### 步骤 1：填 DNS 凭据

设置 → 网关 → **DNS 凭据（证书 + DDNS 共用）**

1. 「服务商」选你域名所在的平台（阿里云 DNS / DNSPod / Cloudflare / webhook）；
2. 填凭据：
   - **阿里云**：AccessKey ID + Secret（建议单独建一个只有 DNS 权限的子账号）
   - **DNSPod**：ID + Token
   - **Cloudflare**：API Token（Zone:DNS:Edit 权限即可）
3. 点「测试凭据」，提示成功且列出你的域名就对了。

### 步骤 2：填 ACME 邮箱

设置 → 网关 → **证书（自动申请 / 续期）** → 「ACME 邮箱」填一个真实邮箱（Let's Encrypt 的账号标识 + 到期提醒）。
「自动签发 / 续期」保持开启即可。

### 步骤 3：准备 VPS

1. 装 Docker（一行）：

   ```bash
   curl -fsSL https://get.docker.com | sh
   ```

   `docker -v` 和 `docker compose version` 都有输出就算好了。

2. **云控制台安全组放行入站 TCP**：
   - `7000` —— frpc 控制连接（默认值，可改，改动后步骤 4 里要填同一个）
   - `443` —— 站点 HTTPS
   - `80` —— 可选，只做 HTTP→HTTPS 跳转

   > 这一步最容易漏。只开 22 的话，部署会卡在「frps 应答」之前的连通性检查上。

### 步骤 4：在面板里一键部署

设置 → 网关 → **远程节点**，逐项填：

| 字段 | 填什么 |
|---|---|
| 名称 | 随便，例如「甲骨文」，只是给你自己看 |
| 服务器 / SSH 端口 | VPS 的公网 IP 或域名 + SSH 端口（默认 22） |
| SSH 用户 | `root`（或能免密执行 `docker compose` 的用户） |
| 认证方式 | 密码 或 私钥（OpenSSH 格式，ed25519 / ecdsa / rsa 都支持） |
| 接入形态 | **Caddy 自动 HTTPS（占 80/443，推荐）**；选「纯 HTTP」就没有证书、站点走 80 |
| FRP 域名后缀 | 例如 `frp.example.com`。**卡片地址 = 容器名.这个后缀**，必须是你 DNS 里那个域名的子域 |
| frps 端口 / Token | 端口保持 `7000`；Token 留空会自动生成（两端同步写） |
| vhost 端口 | 留空即可（Caddy 形态默认 8080，纯 HTTP 形态默认 80） |

先点「测试连接」（验证 SSH、Docker、端口占用），通过后点「**部署 / 更新**」。

日志里会依次出现这些步骤，全绿就成功了：

```
体检 · SSH 登录            ✓
体检 · Docker / compose    ✓
上传 frps.toml + compose   ✓
写入 Caddyfile             ✓
拉起容器                   ✓
frps vhost 应答            ✓ HTTP 404          ← 404 是正常的：这时还没有隧道
DNS 通配记录               ✓ *.frp.example.com → VPS IP
签发通配证书               ✓ *.frp.example.com，剩 90 天
推送证书到节点 / reload     ✓
```

部署过程中工具会提醒你「云安全组要放行 TCP 7000/443」——那就是步骤 3 第 2 条，
没放行的话日志会在「frps vhost 应答」或 DNS/证书步骤之间报错。

### 步骤 5：点「用这个节点」

节点列表里点「**用这个节点**」，面板会把服务器地址、端口、Token、域名后缀一起写进 FRP 设置并立刻重载。
切到「FRP」页签，看到 `frpc 运行中 · N 条隧道` 就成了（N = 有外网域名的卡片数量）。

### 步骤 6：验证

1. **隧道本身**：FRP 页签状态灯是绿的、日志末尾有 `start proxy success`。
2. **卡片地址**：回首页，卡片外网地址已经变成 `https://容器名.frp.example.com`；
   右上角地址模式选「自动」或「外网」。
3. **真访问**：在手机上（4G/5G，别连家里 WiFi）打开那张卡片。
   打不开先看下面「排错」第一条。

> 首张证书要 1～3 分钟（Let's Encrypt 校验 DNS 记录）。刚点完「用这个节点」就打不开，等两分钟再看。

### 排错：`frpc` 起不来的几种原文

面板会把 frpc 日志里的原文翻成人话直接显示在 FRP 页签（「诊断：…」那一行）。
对照表见下面「常见问题 → FRP 连接失败」。

---

## 三、只在内网用 / 你自己已经有反代

- **纯内网**：什么都不用配，卡片的内网地址开箱可用。
- **已有 Nginx / 1Panel / 群晖反代**：设置 → 网关 → 反向代理，填根域名 + 卡片域名，
  证书用「证书」里的 DNS-01 自动签（不占 80 端口），或者直接用你现有反代自己的证书。
- **只想给某一两张卡开外网**：编辑卡片 → 填外网域名 → 保存（会自动生成反代路由）。

---

## 四、手工搭 FRP（不用「远程节点」功能）

不想让面板碰 VPS、或者 VPS 上已经有 frps：看 **[docs/frp-manual.md](docs/frp-manual.md)**，
里面有 frps.toml、nginx / Caddy 通配入口、wss over 443 的完整配置。

你只需要在「设置 → 网关 → FRP」里填三项：

| 场景 | 传输协议 | 端口 |
|---|---|---|
| frps 直接暴露 7000 | `tcp` | `7000`（frps 的 `bindPort`） |
| frps 挂在反代后面、对外只有 443 | `wss` | `443` |

**别把 tcp 的端口填成 80/443** —— 那是网站端口，填了会看到 `EOF`（面板现在会当场提示）。

---

## 五、常见问题

### FRP 连接失败（frpc 日志原文 → 原因）

| 日志 | 原因 | 怎么办 |
|---|---|---|
| `connect to server error: EOF` | TCP 连上了，但对面立刻断开：端口上跑的不是 frps | ① tcp 的端口要用 frps 的 `bindPort`（默认 **7000**），不是 80/443/8080；② 别填 frps 仪表盘端口 `7500`；③ 确认 VPS 上那套 frps 是本项目部署的（v0.61.1） |
| `connect to server error: unexpected EOF` | 选了 `websocket`/`wss`，但服务端这个端口不认 ws | 直连 frps 请选 `tcp`；只有前面有 nginx/Caddy 反代时才用 `wss` + 443 |
| `dial tcp …: connect: connection refused` | 端口没有服务在听 | frps 没起来、端口写错，或云安全组/防火墙没放行 |
| `dial tcp …: i/o timeout` | 包被丢了 | 云安全组没放行，或机房拦了非标准端口（换 443 反代方案） |
| `token in login doesn't match` | 两端 token 不一致 | 把 VPS 上 `frps.toml` 的 `auth.token` 和面板里填的改成一样，或重新点「部署 / 更新」 |
| `proxy [xxx] already exists` | 同一个 frps 上隧道重名 | 「代理名前缀」填个独一无二的，例如 `np2-` |
| 隧道 online 但访问 404 | 域名没解析到 frps，或通配入口没生效 | 查 `*.frp.你的域名` 有没有 A 记录指向 VPS |
| 隧道 online 但 502 / 空白 | 隧道通了，目标服务没起 | 先在局域网用 `IP:端口` 打开那张卡确认服务活着 |

> 容器里如果设置了 `HTTP_PROXY` / `HTTPS_PROXY`，frpc 会把控制连接也塞进代理，表现就是 `EOF`。
> 本项目启动 frpc 时已经自动清掉代理变量；你要是自己跑 frpc，记得 `env -u HTTP_PROXY -u HTTPS_PROXY`。

### 证书

| 报错 | 原因 | 怎么办 |
|---|---|---|
| `QuotaExceeded.TTL The specified TTL is invalid`（阿里云） | 记录 TTL 低于 600 | 2.2.2 起已统一收敛到 600–86400，升级即可 |
| `InvalidAccessKeyId` / `SignatureDoesNotMatch` | 凭据错或子账号没 DNS 权限 | 重新生成 AccessKey，给 `AliyunDNSFullAccess` |
| `No valid authorization`（Let's Encrypt） | DNS 记录没生效就校验了 | 等 1～2 分钟重试；或查 DNS 服务商凭据是否只有读权限 |
| 签发一直「进行中」 | 域名不在这个 DNS 账号里 | 「DNS 凭据」里点「测试凭据」，确认列出的域名包含你要签的 |

### 其他

- **登录页打不开/很慢**：`docker logs navpage` 看是否在启动；面板默认对局域网开放，公网暴露前一定先改密码。
- **扫描不到容器**：`socket-proxy` 没起，或 `DOCKER_HOST` 没指向它；`POST: 1` 才能用面板内更新镜像。
- **反代后客户端 IP 变成反代地址**：打开「网络与安全 → 信任代理头」，并让反代带上 `X-Forwarded-For`。
- **卡片地址里出现 `{host}`**：说明反代没把 `Host` 头传过来，模板变量展不开。

---

## 六、设置项速查

| 位置 | 关键项 |
|---|---|
| 环境变量 | `NAVPAGE_DATA`（数据目录，默认 `/data`）、`NAVPAGE_DEFAULT_PASSWORD`、`DOCKER_HOST`、`NAVPAGE_COMPOSE_ROOT` |
| 网络与安全 | 改密码、信任代理头、允许的 Host |
| 网关 → 反向代理 | 开关、根域名、HTTP/HTTPS 监听端口（默认 8081 / 18443）、通配符、上游 TLS 校验 |
| 网关 → 证书 | 自动签发/续期、ACME 邮箱、CA 环境（LE 正式 / 测试）、提前续期天数 |
| 网关 → DNS 凭据 | 服务商 + 凭据，证书和 DDNS 共用 |
| 网关 → WAF | 开关、模式（拦截/观察）、IP 黑白名单、UA、路径、方法、CC 限速 |
| 网关 → FRP | 服务器地址/端口、传输协议、Token、域名后缀、代理名前缀 |
| 网关 → 远程节点 | VPS SSH、接入形态、frps 端口、Token、vhost 端口 → 「部署 / 更新」「用这个节点」 |
| DDNS | 域名、IP 获取方式（在线探测 / **指定网卡** / 手动）、更新间隔（A 记录指向家里当前公网 IP） |

> 机器走了代理/加速器时，在线探测拿到的是**代理出口 IP**，写到域名上打不开 —— 选「指定网卡」直接读本机网口地址（Lucky 的接口方式）；也可以在「手动」里写死。网卡下拉会标出默认路由那块，点「试一下」能先看取到什么再保存。
| 外观 | 站点标题、壁纸（本地/URL）、大卡小卡、每行分组数 |
| 图标 | 上传 / 在线库 / 图标源管理，卡片图标存本地文件名 |
| 备份与恢复 | 一键导出/导入全部配置（含图标库、壁纸、证书，不含容器和数据卷） |

---

## 七、升级 / 备份 / 迁移

```bash
# 升级
sudo docker compose pull && sudo docker compose up -d

# 升级前留后路（面板里也有「设置 → 备份与恢复」，更省事）
tar czf navpage-data-$(date +%F).tgz data/
```

- **迁移**：新机器起容器 → 设置 → 备份与恢复 → 上传 zip。管理员密码默认保持当前新机器的，
  勾选「覆盖密码」才会用备份里的那份。
- **备份包内容**：卡片、分类、采集源、全部设置（含图标地址、外网地址、网关/FRP/DDNS/WAF 配置）、
  本地图标库、壁纸、logo、证书与 frpc 配置。恢复是**整体替换**，恢复前会自动留一份
  `navpage-prerestore-*.zip` 供回滚。

---

## 八、安全说明

- 面板登录**没有防爆破**（无限速、无失败锁定），暴露到公网前必须改掉默认密码，最好放在反代 + HTTPS 后面。
- `socket-proxy` 的 `POST: 1` 配合 `CONTAINERS: 1` 等于把「创建特权容器」的能力交给了面板（≈宿主机 root）。
  不需要面板内更新/Compose 管理时保持 `POST: 0`。
- 凭据（SSH 密码、私钥、DNS Token、frp Token）只存在本地 SQLite，接口返回一律打码。

---

## 九、目录结构

```
app/            FastAPI 后端（main.py 路由、db.py 存储、gw/ 网关各模块）
static/         前端（Vue3 + Element Plus，无构建步骤）
data/           运行数据：navpage.db、icons/、wallpapers/、gw/（证书、frps/frpc 配置）
docs/           补充文档（frp-manual.md 手工 FRP）
Dockerfile      多阶段构建，内置 frpc 二进制
docker-compose.yml
```

---

## 十、版本与镜像标签

- 每次打 tag（`v2.2.3`）CI 构建多架构镜像并推 `latest`、`2.2.3`、`2.2`、`v2.2.3`、`sha-<commit>`。
- 默认用 `latest`；想钉版本就把 compose 里改成 `yuwancumian2009/navpage:2.2.3`。
- 变更记录见 [CHANGELOG.md](CHANGELOG.md)。

---

## 许可

[MIT](LICENSE)
