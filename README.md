# NavPage — 自建 NAS 导航页

[![build image](https://github.com/yuwancumian2009/NavPage/actions/workflows/image.yml/badge.svg)](https://github.com/yuwancumian2009/NavPage/actions/workflows/image.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/yuwancumian2009/navpage)](https://hub.docker.com/r/yuwancumian2009/navpage)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

参考 SunPanel 的视觉风格，围绕「自动发现本机 Docker 服务 + 内外网地址自动切换」重做。

## 功能

- **自动发现 Docker 容器**：经只读 socket-proxy 读 Docker API，列出所有带端口映射的容器，
  自动生成条目（名称 / 端口 / 内网地址 / 图标），并按 compose 项目自动分好类。
- **内外网地址自动切换**：同一个条目同时保存内网、外网两个地址；页面顶部有
  「自动 / 内网 / 外网」开关。自动模式下按客户端 IP 判断——内网 IP 走内网地址，
  公网 IP 走外网地址，每张卡片右上角标出当前用的是哪个。
- **零配置主机名推导**（重点）：内网地址默认写成 `{scheme}://{host}:端口` 模板。
  容器里拿不到 NAS 的局域网 IP，所以 `{host}` 在**每次请求时**按浏览器实际访问的
  Host 头展开——用 `http://192.168.1.10:8095` 打开就生成 `http://192.168.1.10:8078`，
  用域名打开就生成域名地址。不用手动填 IP，换网络环境也不用改配置。
- **收藏网页**：手动添加任意网址（可只填外网地址），支持分组、排序、描述。
- **自定义壁纸**：本地上传（多张，可切换/删除）或粘贴图片 URL，支持模糊、暗化、
  随机切换；不设壁纸则用内置深色渐变。
- **横竖屏自适应壁纸**：填随机图 API 地址（如 `/api/pc`、`/api/random`）后，
  电脑取横图、手机取竖图，旋转屏幕自动换；其它接口可写 `{orientation}` 占位符。
- **管理控制台**：项目 / 分类 / 扫描导入 / 外观 / 网络与安全 / 镜像 六个页签，
  密码登录（PBKDF2 哈希），可改密码。

## 快速开始

```bash
cd <部署目录>

# 方式一：用发布好的镜像（多架构，amd64 / arm64 都支持）
docker compose pull && docker compose up -d

# 方式二：本地构建
docker compose up -d --build

# 打开 http://<局域网IP>:8095
# 先把 docker-compose.yml 里的 NAVPAGE_DEFAULT_PASSWORD 改成你自己的密码再启动；
# 若删掉那一行，则随机生成并打印在日志里：docker compose logs navpage | grep navpage
```

镜像：`yuwancumian2009/navpage:latest`（GitHub Actions 自动构建推送，见 `.github/workflows/image.yml`）。

**初始密码**：`docker-compose.yml` 里有 `NAVPAGE_DEFAULT_PASSWORD`，**部署前改成你自己的密码**，
首次启动就用它当管理员密码，同时打开 `require_login`，所以开箱就是登录页。
删掉那一行则随机生成一个并打印在容器日志里（`docker compose logs navpage | grep navpage`）。
该变量**只在数据库里还没有密码时生效**，已有部署改它不会覆盖现有密码；登录后可在
「网络与安全」页签改密码。
登录后 → 右下角齿轮 → 「扫描导入」→ 扫描容器 → 勾选 → 导入。想换密码：「网络与安全」页签里改。

> 仓库里刻意**不写死**任何默认密码：镜像会发布到 Docker Hub，
> 镜像里带固定口令等于把所有部署者的初始密码一起发出去。
> 另外面板默认对局域网开放，反代/穿透到公网前务必先改掉初始密码。

数据（SQLite 库、壁纸、图标缓存）全部落在 `./data`，删容器不丢配置。

## 目录结构

```
navpage/
├── app/
│   ├── main.py       FastAPI 路由：bootstrap / admin / 扫描 / 壁纸 / 鉴权
│   ├── db.py         SQLite：items / categories / settings
│   ├── dockerapi.py  只读 Docker API 客户端（容器、端口、compose 项目）
│   ├── netutil.py    局域网判定、客户端 IP、{host} 模板展开
│   └── icons.py      favicon 抓取与缓存
├── static/           index.html + app.js + app.css + vendor/（Vue3/ElementPlus/axios 本地化）
├── docker-compose.yml  navpage + docker-socket-proxy（只读 socket）
└── Dockerfile
```

## 内外网地址怎么填

| 写法 | 含义 |
|---|---|
| `{scheme}://{host}:8078` | 自动填当前访问的主机名（推荐，扫描导入的默认值） |
| `http://192.168.1.10:8078` | 写死内网地址 |
| `https://nas.example.com/emby` | 写死外网地址（反代域名） |

`{scheme}` 在内网模式展开为 `http`，外网模式展开为「网络与安全」里配置的协议（默认 https）。

判定内网的网段在「网络与安全」里改（默认 `192.168.0.0/16,10.0.0.0/8,172.16.0.0/12,127.0.0.1/32`）。
如果 NavPage 挂在 Nginx / 反代后面，打开「信任代理头」，否则客户端 IP 会是反代的地址。

## 安全说明

- 容器**不直接挂载** `/var/run/docker.sock`，而是通过 `docker-socket-proxy` 只读访问，
  且只放开了 `CONTAINERS / VERSION / PING / INFO / IMAGES / NETWORKS` 这几个 GET 接口，
  `POST / EXEC / BUILD` 全部关闭。
- 管理密码用 PBKDF2-HMAC-SHA256（12 万轮 + 随机盐）存哈希，库里不存明文。
- 扫描只读容器元数据，不会启动/停止/修改任何容器。

## 常用操作

```bash
sudo docker compose logs -f navpage          # 看日志
sudo docker compose restart navpage          # 重启
cp -r data data.bak                          # 备份配置
```

改完代码重新部署：

```bash
sudo docker compose up -d --build
```

## 版本与镜像标签

当前版本：**v1.1.2**（页面「☰ → 关于」里也能看到）。

版本递进规则：**每次发版第三位 +1（0.0.1 步进），第三位满 10 进位到第二位**
（`1.1.9` → `1.2.0`，不会出现 `1.1.10`）；第一位只在重大改版时动。

Docker Hub 上的镜像按版本打标签，`latest` 会跟着主分支走：

| 标签 | 含义 |
| --- | --- |
| `latest` | 主分支最新构建 |
| `1.1.2` / `v1.1.2` | 对应版本的固定镜像（推荐生产用） |
| `1.1` | 该次版本的最新补丁 |
| `sha-<commit>` | 某次提交的构建 |

生产建议钉死版本，避免 `latest` 悄悄升级：

```yaml
image: yuwancumian2009/navpage:1.1.2
```

升级时把版本号改掉再 `docker compose pull && docker compose up -d`。
改了哪些东西看 [CHANGELOG.md](CHANGELOG.md)。

发版流程：改 `app/version.py` 里的 `VERSION` → 在 `CHANGELOG.md` 顶部加一节 →
`git tag vX.Y.Z && git push origin vX.Y.Z`，CI 会自动构建并推对应版本标签。

## 端口

默认对外 `8095`，容器内 `8080`。改 `docker-compose.yml` 里的 `ports` 即可。

## 已知取舍

- 按 compose 项目自动分组时，单容器项目会各占一个分组。分组名和条目名相同的会自动
  省略分组标题，页面不会显得啰嗦；想合并成自己的分类，在「分类」页签里改。
- 单条目分组的卡片宽度由分组宽度决定（桌面约 400px，手机半屏），避免右侧留白。
