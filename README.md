# NavPage — 自建 NAS 导航页

[![build image](https://github.com/yuwancumian2009/NavPage/actions/workflows/image.yml/badge.svg)](https://github.com/yuwancumian2009/NavPage/actions/workflows/image.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/yuwancumian2009/navpage)](https://hub.docker.com/r/yuwancumian2009/navpage)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个部署在自己 NAS 上的导航页：自动把本机 Docker 服务变成卡片，同一个服务同时记内网和外网地址，
按你当前从哪里访问自动选对的那个。多架构镜像（amd64 / arm64），一条 `docker compose up -d` 就能跑。

- 不想一条条手填服务地址？扫描一下 Docker 容器就有了。
- 在家用内网 IP、在外面用域名，不想两头改配置？它按客户端 IP 自动切。
- 想在导航页上直接看到下载速度、Emby 正在播什么、NAS 的 CPU 温度？卡片支持 12 种服务状态小组件。

---

## 功能

### 2.0.0 起：内置网关（反代 / 证书 / DDNS / FRP）

不用再外挂 Lucky 或 Gately，一个容器全包：

| 能力 | 说明 | 设置位置 |
|---|---|---|
| 反向代理 | 按域名转发到内网容器，支持 WebSocket / 流式响应 | 设置 → 网关 |
| HTTPS 证书 | ACME DNS-01，自动申请与续期，可签通配符证书 | 设置 → 网关 → 证书 |
| DDNS | 多源探测公网 IP，变化时更新 A 记录 | 设置 → DDNS |
| FRP 隧道 | 镜像内置 frpc，按卡片自动生成隧道 | 设置 → FRP |
| 卡片地址 | 自动 / 内网 / 反代 / FRP 四档，可逐条固定 | 首页右上角 / 编辑条目 |

反代默认监听容器内 `8081`（HTTP）和 `18443`（HTTPS），compose 已映射；端口可在设置里改。
证书走 DNS-01 所以不需要占用 80 端口，Cloudflare / 阿里云 / DNSPod / 自定义 webhook 任选其一。

### 卡片与布局

- **自动发现 Docker 容器**：通过只读 socket-proxy 读 Docker API，列出所有带端口映射的容器，
  自动生成卡片（名称 / 端口 / 内网地址 / 图标），并按 compose 项目自动归类。
- **收藏网页**：手动添加任意网址，支持分组、排序、描述、隐藏。
- **布局**：按分类分组、拖拽排序、大卡 / 小卡两种尺寸、每行分组数可调（1 列 / 自适应流式）。
- **卡片右键**：打开、复制地址、编辑，容器卡片还能启动 / 停止 / 重启。
- **状态灯**：卡片图标右下角绿 / 黄 / 红，分别表示正常、延迟偏高、访问不了。
- **搜索**：顶部搜索框可搜卡片，回车直接当搜索引擎用（内置必应 / 百度 / Google，可切换）。

### 内网 / 外网地址自动切换

每个条目同时保存「内网地址」和「外网地址」，页面顶部有「自动 / 内网 / 外网」开关：

- **自动模式**：按客户端 IP 判断。来源在内网网段 → 用内网地址；来自公网 → 用外网地址。
- 也可以用域名判定：用非内网域名访问就算外网；或指定哪些域名算外网（支持 `*.example.com`）。
- 每张卡片右上角标出当前用的是哪个地址，出问题一眼能看出走错没。

### 从 Lucky 批量导入外网域名

已经用 Lucky 之类把一堆内网服务映射到域名后，不用一条条手填「外网地址」：

1. 管理台 →「扫描导入」→ 拉到底部「导入外网地址」
2. 在 Lucky「Web服务」里点开主规则，看到子规则列表（表头是 名称 / 前端 / 目标 / 访问控制 / 速率），在表格上按 `Ctrl+A` 再 `Ctrl+C`，整个粘进来就行 —— 不用整理格式：

   ```
   emby.example.com
   http://192.168.1.109:8096
   jf.example.com
   http://192.168.1.109:8097
   ```

   手写也可以，一行一条、域名和内网地址写在同一行：

   ```
   emby.example.com     192.168.1.109:8096
   jellyfin.example.com 192.168.1.109:8096
   ```

3. 点「解析预览」，每行显示解析出的域名、后端地址、自动匹配到的卡片
4. 匹配错了在「匹配卡片」下拉里改；不想写的留空（显示「不写入」）
5. 点「写入 N 张卡片」

要点：

- **只写外网地址**，内网地址不动；默认跳过已有外网地址的卡片，勾「覆盖已有外网地址」才覆盖
- 协议默认 `https`；点域名前面的 `https://` 可切成 `http`
- 匹配顺序：先按后端端口对应卡片，端口对不上再按域名首段和卡片名（`emby.example.com` → `emby`）
- 不依赖固定列顺序和换行位置：Lucky 的表格复制出来是「一格一行」（域名一行、`http://IP:端口` 一行），会自动跨行配对；制表符、空格、`->`、`|` 分隔也都能认，多余的列（名称、状态、速率）会被忽略
- 一个子规则绑了多个域名（`a.com,b.com` 共用一个后端）时，会拆成多行、共用同一个后端
- 端口写了域名自己的（如 `https://emby.example.com:8443`）也能识别，不会被当成后端端口
- Lucky 的规则数据是加密存放的，第三方读不出来，所以这里是「粘贴 → 预览 → 确认」，不会去连你的 Lucky
- 注意：在「添加/编辑子规则」弹窗里复制是拿不到值的（域名、地址都在输入框里），要在**子规则列表**上复制

### 零配置主机名推导

内网地址默认写成模板 `{scheme}://{host}:端口`。容器里拿不到 NAS 的局域网 IP，所以 `{host}`
在**每次请求时**按浏览器实际访问的 Host 头展开：用 `http://192.168.1.10:8095` 打开就生成
`http://192.168.1.10:8078`，用域名打开就生成域名地址。换网络、换 IP 都不用改配置。

### 服务状态小组件

直接在卡片上显示服务运行时数据，支持：qBittorrent、Transmission、MoviePilot、Emby、Jellyfin、
Plex、Audiobookshelf、Navidrome、本机 / NAS（CPU、内存、温度、运行天数）、哪吒探针（云服务器）、
mihomo / Clash（连接数、上下行速率、机场数）、自定义 JSON。每种要填什么见 [小组件怎么配](#5-小组件怎么配)。

带明细数据的小组件（mihomo、哪吒探针等）在电脑上**鼠标悬停**会弹出浮层：mihomo 会逐条列出
每个策略组当前选中的线路和延迟，浮层可滚动，下方空间不够时自动翻到卡片上方。

### 外观与壁纸

- 三套卡片主题（毛玻璃 / 纯白 / 深色），自定义站点标题、副标题、Logo、页脚。
- 壁纸支持本地上传多张随机切换，或填一个图片 URL / 随机图 API 地址，可调模糊与暗化。
- **横竖屏自适应**：壁纸源是随机图 API 时，电脑取横图、手机竖屏取竖图，转屏自动换。

### 管理台

右下角菜单进入，七个页签：**项目**（增删改卡片）、**分类**（分组与排序）、
**扫描导入**（扫描容器 / 全量同步 / 按服务类型归类）、**外观**（标题与壁纸）、
**网络与安全**（密码、内网判定、反代）、**镜像**（检查更新 / 更新 / 定时任务 / 清理）、
**Compose**（改 YAML 并重构）。

### 多主机：采集源（远端容器自动建卡）

「扫描导入 → 采集源」可以加远端主机：在对端跑一个 agent 容器，它只读挂自己的
docker.sock、每 5 分钟把容器清单 POST 给面板（**纯出站，对端不用开端口、不用公网 IP**），
面板按该源配置的「地址前缀 + 容器端口」生成卡片地址，新容器默认自动建卡（可关）。

```bash
# 面板里点「+ 添加远端主机」会直接给出带令牌的完整命令
docker run -d --name navpage-agent --restart unless-stopped --no-healthcheck \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -e PANEL_URL=http://<面板地址> -e AGENT_TOKEN=npk_xxx \
  -e INTERVAL=300 yuwancumian2009/navpage:latest python -m app.agent
```

没 docker 的主机也能裸跑：把 `app/agent.py` 拷过去 `PANEL_URL=... AGENT_TOKEN=... python3 agent.py`
（零依赖，只用标准库）。令牌只在创建时显示一次，可随时轮换 / 停用。

### Compose 管理

把存放 compose 的目录挂进容器（**容器内路径必须和宿主机一致**，否则 YAML 里的
`./data` 这类相对路径会被解析到容器里，daemon 会在宿主机建出错误的挂载点），
就能在面板里管理所有项目：

- 列表：项目名、服务数、容器与运行状态，扫描 `NAVPAGE_COMPOSE_ROOT` 下的
  `docker-compose.yml` / `compose.yaml`（最多 3 层目录）
- 编辑：直接改 YAML，保存前跑 `docker compose config` 校验，**校验不过不会落盘**
- 备份：每次保存在同目录留一份 `<文件名>.bak-时间戳`，只保留最近 10 份
- 重构：`docker compose up -d`（可选 `--pull always`）；改的是面板自己那个项目时会
  异步执行，面板重启后回来即可

需要 `POST: 1` 和 `SESSION: 1`（compose v5 会开 BuildKit session，不放开会被代理 403）。

登录密码用 PBKDF2-HMAC-SHA256（12 万轮 + 随机盐）存哈希，库里不存明文；可随时改密码。

---

## 部署

### 前置要求

- Docker 20.10+，Docker Compose v2（`docker compose` 命令）
- 一个空闲端口（默认对外 **8095**）
- 想用「扫描导入 / 镜像更新」需要能访问宿主机的 Docker socket（下面的 compose 已经处理好）

### 方式一：用发布好的镜像（推荐）

新建一个目录放 `docker-compose.yml`（内容如下，把密码改掉）：

```yaml
services:
  navpage:
    image: yuwancumian2009/navpage:1.1.7
    container_name: navpage
    restart: unless-stopped
    ports:
      - "8095:8080"
    environment:
      TZ: Asia/Shanghai
      NAVPAGE_DATA: /data
      NAVPAGE_DEFAULT_PASSWORD: 改成你自己的密码
      DOCKER_HOST: tcp://navpage-socket-proxy:2375
      # Compose 管理：要扫描的 compose 根目录（不用这个功能可以不写）
      NAVPAGE_COMPOSE_ROOT: /vol1/1000/docker
    volumes:
      - ./data:/data
      # Compose 管理：把 compose 目录**按宿主机同样的绝对路径**挂进来（只读改 YAML 也要 rw）
      - /vol1/1000/docker:/vol1/1000/docker
    depends_on:
      - socket-proxy

  # 只读 Docker API 代理：让面板能发现容器，但不把宿主机 root 交出去
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
      SESSION: 1       # compose v5 启动时会开 BuildKit session，不放开会被 403
      POST: 0          # 想用「面板内更新镜像 / Compose 重构」改成 1（见安全说明）
      EXEC: 0
      BUILD: 0
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

```bash
sudo docker compose pull
sudo docker compose up -d
```

打开 `http://<NAS的局域网IP>:8095`。仓库里那份 `docker-compose.yml` 额外带 `build: .`，
是给源码构建用的；只跑镜像用上面这份更省事。

### 方式二：从源码构建

```bash
git clone https://github.com/yuwancumian2009/NavPage.git
cd NavPage
vi docker-compose.yml          # 改初始密码
sudo docker compose up -d --build
```

### 必改的两处

| 位置 | 默认值 | 说明 |
| --- | --- | --- |
| `environment.NAVPAGE_DEFAULT_PASSWORD` | `changeme` | **必须改成你自己的密码**。只在数据库里还没有密码时生效（已有部署改它不会覆盖旧密码）；删掉这一行则随机生成并打印在日志里 |
| `ports` | `"8095:8080"` | 左边是宿主机端口，端口被占用就改左边 |

### 不想用 compose（docker run）

不需要容器发现和镜像更新时，单容器也能跑：

```bash
docker run -d --name navpage -p 8095:8080 \
  -e TZ=Asia/Shanghai \
  -e NAVPAGE_DEFAULT_PASSWORD='你的密码' \
  -v "$PWD/data:/data" \
  --restart unless-stopped \
  yuwancumian2009/navpage:latest
```

这样「扫描导入」和「镜像」页签不可用，卡片、壁纸、小组件、内网切换都正常。

### 部署后三步

1. 用初始密码登录（登录页就是首页）。
2. 右下角齿轮 → **扫描导入** → 「扫描容器」→ 勾选要的 → 「导入选中」。
3. **外观** 里改站点标题、传壁纸；**网络与安全** 里改密码。

### 放在反向代理 / 公网访问后面

- 反代时把 `proxy_set_header Host $host;` 带上（`{host}` 模板靠它展开），并开启
  「网络与安全 → 信任代理头」，否则客户端 IP 会是反代的地址、自动内外网判定会失灵。
- **暴露到公网前务必先改初始密码**。面板默认对局域网开放，登录接口没有防爆破机制。

---

## 参数说明

### 1. compose 环境变量

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `NAVPAGE_DEFAULT_PASSWORD` | 无 | 首次启动的管理员密码，只在库里还没有密码时使用；不设则随机生成并打印到日志 |
| `NAVPAGE_DATA` | `/data` | 数据目录（SQLite 库、壁纸、图标缓存），一般不用改 |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker API 地址。compose 里指向 socket-proxy |
| `NAVPAGE_DOCKER_TIMEOUT` | `10` | 调 Docker API 的超时秒数，容器多 / NAS 慢可以调大 |
| `NAVPAGE_COMPOSE_ROOT` | `/vol1/1000/docker` | Compose 管理的扫描根目录，**要和挂载进容器的宿主机路径一致**；目录不存在时 Compose 页为空 |
| `TZ` | `UTC` | 时区，影响定时任务与日志时间，建议 `Asia/Shanghai` |

### 2. 端口与数据

| 项 | 值 |
| --- | --- |
| 对外端口 | `8095`（改 compose 的 `ports` 左边） |
| 容器内端口 | `8080` |
| 健康检查 | `GET /api/health` |
| 数据目录 | 挂载 `./data:/data`，含 `navpage.db`、`wallpapers/`、`icons/`、`logo/` |

`./data` 就是全部配置，**删容器不丢**；备份它等于备份整个面板。

### 3. 服务地址怎么填

「项目」里每个条目的内网 / 外网地址支持三种写法：

| 写法 | 含义 |
| --- | --- |
| `{scheme}://{host}:8078` | 用浏览器当前访问的主机名（**推荐**，扫描导入的默认值） |
| `http://192.168.1.10:8078` | 写死内网地址 |
| `https://nas.example.com/emby` | 写死外网地址（反代域名） |

`{scheme}` 在内网模式展开为 `http`，外网模式展开为「网络与安全」里配的协议（默认 `https`）。
只填端口（`8078`）保存时会自动补成模板 `{scheme}://{host}:8078`，内网访问时即 `http://<当前主机>:8078`。

外网地址多的时候不用手填：管理台「扫描导入」页底部有「导入外网地址」，把反代里的域名规则粘贴进去，
预览确认后一键写到对应卡片（见上文「从 Lucky 批量导入外网域名」）。

### 4. 内网判定与模式切换

「网络与安全」页签：

| 项 | 默认 | 说明 |
| --- | --- | --- |
| 内网网段 | `10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, 100.64/10` | 客户端 IP 落在这些网段即视为内网，逗号分隔 |
| 自动切换 | 开 | 关掉后固定用你在顶部手动选的模式 |
| 默认模式 | 自动 | 首次打开页面用哪种（自动 / 内网 / 外网） |
| 外网域名 | 空 | 命中这里的主机名 / 域名视为外网，支持 `*.example.com` |
| 用域名访问算外网 | 开 | 没用 IP 而是用域名打开，且不在上面清单里时也算外网 |
| 信任代理头 | 关 | 挂在 Nginx / Traefik / Cloudflare 后面时**必须打开** |
| 外网协议 | `https` | 外网模式下 `{scheme}` 展开成什么 |
| frp 请求头校验 | 空 | 如果你用 frp 且想靠注入头判定外网，填头名和期望值 |

### 5. 小组件怎么配

「项目」→ 编辑某个卡片 → 「状态小组件」选类型，按提示填。地址一律填**能从 NavPage 容器访问到**的地址
（同机服务用 `http://宿主机IP:端口` 或容器网络里的名字，别填 `127.0.0.1` 除非它们同容器）。

| 类型 | 要填 | 去哪拿 / 注意 |
| --- | --- | --- |
| qBittorrent | 地址、用户名、密码 | 填 WebUI 地址，如 `http://192.168.1.10:8889`；显示速度、任务数、做种数 |
| Transmission | 地址、用户名、密码 | 填 RPC 根地址，如 `http://192.168.1.10:9091` |
| MoviePilot | 地址、API Token（或用户名+密码） | Token 在 MP 的 `app.env` 里（`API_TOKEN`，去掉引号）；显示订阅总数 / 追更 / 缺集 |
| Emby | 地址、API Key | Emby 控制台 → 高级 → API 密钥 → 新建 |
| Jellyfin | 地址、API Key | 控制台 → API 密钥 → 新建 |
| Plex | 地址、X-Plex-Token | Plex 账号页或 XML 地址里找 |
| Audiobookshelf | 地址、API Token | 用户设置里生成 |
| Navidrome | 地址、用户名、密码 | 填 Navidrome 登录账号，显示专辑 / 艺术家 / 歌曲 / 歌单 |
| 本机 / NAS | 不用填 | 读容器可见的宿主 `/proc`、`/sys`：CPU、内存、温度、运行天数。SATA 盘温度需宿主先 `sudo modprobe drivetemp` |
| mihomo / Clash | 外部控制地址、密钥（没设就留空） | 填 `external-controller` 的地址，如 `http://192.168.1.10:9090`（不是面板网页端口）；显示连接数 / ↓速率 / ↑速率 / 机场订阅数，悬停看每个策略组当前线路 + 延迟。`external-controller` 建议只监听内网，或设 secret 后填到「密钥」 |
| 哪吒探针 | 面板地址、API 令牌、（可选）服务器名 | 面板 → 设置 → API 令牌，权限至少 `nezha:inventory:read`，要实时状态再加 `nezha:server:read`；多台时填服务器名或 id |
| 自定义 JSON | 地址、API Key、字段路径、显示名、单位 | 给一个返回 JSON 的接口，`path` 用点号取值，如 `data.count` |

刷新间隔在「外观 → 状态小组件」里设（默认 30 秒，后端另有 20 秒缓存）；样式可选
`rich`（多指标 + 进度条）或 `plain`（单行文字）。局域网自签证书会跳过校验，不用额外配。

### 6. 壁纸

「外观 → 壁纸」三种来源：

1. **本地上传**：可传多张，开启「随机」后每次刷新换一张。
2. **图片 URL**：直链图片地址。
3. **随机图 API**：填 API 地址即可，例如

   | 填什么 | 效果 |
   | --- | --- |
   | `https://pic.example.com/api/random` | 自动按屏幕方向换成 `/api/pc`（电脑）/ `/api/mobile`（手机） |
   | `https://pic.example.com/api/pc` | 直接指定横图，竖屏时自动换成 `/api/mobile` |
   | `https://pic.example.com/api/anime/{orientation}` | `{orientation}` 会替换成 `pc` / `mobile`，任意接口通用 |
   | `https://pic.example.com/api?orientation=pc` | 查询参数形式的接口也能自动替换 |

   方向变化（手机旋转、桌面窗口拉成竖的）会重新取一张，新图加载完成后才替换，不会闪白。

模糊（0–30px）和暗化（0–85）只作用于背景，不影响卡片文字。

### 7. 图标

- 卡片图标可以填：在线图标库的名字（内置 Dashboard Icons）、`http` 外链、或自己上传。
- 扫描导入时会自动尝试抓服务的 favicon 缓存到本地。
- 「外观」里可增删在线图标库（填 GitHub 仓库、分支、备注名、子目录，仓库需公开）。

### 8. 镜像更新与清理

「镜像」页签默认**只显示上次检查结果**（缓存 1 小时），不打开页面就自动打 registry；
要看最新就点「检查更新」。

| 项 | 默认 | 说明 |
| --- | --- | --- |
| 允许从面板更新 | 关 | 打开后卡片才能真的拉镜像并重建容器，需要 socket-proxy 放开写权限（见安全说明） |
| 检查缓存 | 3600 秒 | 检查结果保留多久 |
| 国内镜像站 | 空 | 填如 `https://docker.1panel.live`，避免 Docker Hub 按 IP 限流报 429 |
| 代理 | 空 | 检查更新走的外网代理，如 `http://user:pass@192.168.1.10:7893` |
| 定时检查 / 定时更新 | 关 | 5 段 cron（分 时 日 月 周），如 `0 3 * * *`；定时更新只处理你在名单里列出的容器 |
| 定时清理 | 关 | `dangling` 只清悬空镜像（安全），`unused` 清所有没被容器引用的镜像；可填保留清单（支持 `*`） |

---

## 安全说明

- 容器**不直接挂载** `/var/run/docker.sock`，而是经 `docker-socket-proxy`，只放开
  `CONTAINERS / VERSION / PING / INFO / IMAGES / NETWORKS` 等接口，`EXEC / BUILD` 关闭。
- 仓库里**不写死任何默认密码**：镜像发布在 Docker Hub，镜像内带固定口令等于把所有部署者的初始密码一起发出去。
- 仓库提供的 `docker-compose.yml` 为了支持「面板内更新镜像 / Compose 重构」，开了 `POST=1` +
  `ALLOW_START/ALLOW_STOP=1`（compose 还要 `SESSION=1`）。代价是面板被攻破时攻击面更大
  （POST + CONTAINERS 等于能创建特权容器）。不需要这些功能就把这几项改回 `0` 并重建 socket-proxy
  容器——「检查更新」仍然可用，只是不能真的更新；Compose 页会退化成只读不了也重构不了。
- Compose 管理会把 compose 目录以**可写**方式挂进容器，等于面板能改宿主机上所有 compose 文件；
  只想要只读查看就别挂这个目录。
- 登录接口没有失败锁定与限速，**不要把面板裸奔到公网**；必须公网访问时请套反代 + HTTPS + 访问控制。

---

## 升级 / 备份 / 迁移

```bash
# 升级（compose 里钉了版本号时，先改版本号）
sudo docker compose pull && sudo docker compose up -d

# 备份（配置 + 壁纸 + 图标全在 ./data）
tar czf navpage-data-$(date +%F).tar.gz data/

# 迁移：把 data/ 和 docker-compose.yml 拷到新机器，up -d 即可
```

日志 / 排错：

```bash
sudo docker compose logs -f navpage
sudo docker compose restart navpage
curl -s localhost:8095/api/health
```

---

## 常见问题

**卡片点开是空白 / 连不上**
先看卡片右上角标的是「内网」还是「外网」，再确认该地址在你当前网络里能打开。
用域名 + 反代访问面板却没开「信任代理头」时，会被判成内网，外网地址就用不上。

**扫描不到容器**
「扫描导入」页顶部会显示 Docker 连接状态。红字说明 socket 没通：检查 socket-proxy 容器是否在跑、
`DOCKER_HOST` 是否指向它。没有端口映射的容器默认也会建卡片（地址留空待填）。

**小组件显示认证失败**
qBittorrent 请确认密码正确（WebUI 里临时关闭「对本地地址跳过认证」以外的干扰项）；
MoviePilot 的 `API_TOKEN` 不要带引号；Emby / Jellyfin 的 Key 要属于管理员账号。

**图标不显示**
外链图标可能被对方站点防盗链。优先用内置图标库或自己上传。

**「检查更新」转圈很久或一片红**
Docker Hub 按 IP 限流（HTTP 429）。「镜像」里填一个国内镜像站，或配代理。

**手机上看背景图是被裁的**
把壁纸换成随机图 API 地址（见 [壁纸](#6-壁纸)），会自动取竖图；本地上传的多张壁纸不区分方向。

---

## 已知取舍

- 按 compose 项目自动分组时，单容器项目会各占一个分组；组名和条目名相同的会自动省略分组标题，
  页面不会显得啰嗦。想合并成自己的分类，在「分类」页签里改。
- 单条目分组的卡片宽度由分组宽度决定（桌面约 400px，手机半屏），避免右侧留白。
- 本地上传的壁纸随机切换时不区分横竖图（数据库没存图片尺寸）；用随机图 API 才有方向自适应。
- 面板内更新镜像需要 socket-proxy 的写权限，默认关闭，开启即扩大攻击面（见安全说明）。

---

## 目录结构

```
navpage/
├── app/
│   ├── main.py       FastAPI 路由：bootstrap / admin / 扫描 / 壁纸 / 鉴权
│   ├── db.py         SQLite：items / categories / settings
│   ├── dockerapi.py  只读 Docker API 客户端（容器、端口、compose 项目）
│   ├── widgets.py    12 种服务状态小组件
│   ├── registry.py   镜像更新检查（Registry v2 / 镜像站）
│   ├── cron.py       定时检查 / 更新 / 清理
│   ├── netutil.py    局域网判定、客户端 IP、{host} 模板展开
│   ├── iconlib.py    在线图标库（GitHub 仓库索引）
│   └── icons.py      favicon 抓取与缓存
├── static/           index.html + app.js + app.css + vendor/（Vue3 / ElementPlus / axios 本地化）
├── docker-compose.yml  navpage + docker-socket-proxy
└── Dockerfile
```

前端是 Vue 3 + Element Plus 的 UMD 版本，**没有构建步骤**，改完 `static/` 里的文件重建容器即可。

---

## 版本与镜像标签

当前版本：**v1.1.2**（页面「☰ → 关于」里也能看到）。

版本递进规则：每次发版第三位 +1（0.0.1 步进），第三位满 10 进位到第二位
（`1.1.9` → `1.2.0`，不会出现 `1.1.10`）；第一位只在重大改版时动。

| 标签 | 含义 |
| --- | --- |
| `latest` | 主分支最新构建 |
| `1.1.2` / `v1.1.2` | 对应版本的固定镜像（推荐生产用） |
| `1.1` | 该次版本的最新补丁 |
| `sha-<commit>` | 某次提交的构建 |

生产建议钉死版本，避免 `latest` 悄悄升级：

```yaml
image: yuwancumian2009/navpage:1.1.7
```

改了哪些东西看 [CHANGELOG.md](CHANGELOG.md)。发版流程：改 `app/version.py` 的 `VERSION` →
`CHANGELOG.md` 顶部加一节 → `git tag vX.Y.Z && git push origin vX.Y.Z`，CI 自动构建并推版本标签。

## 许可

[MIT](LICENSE)
