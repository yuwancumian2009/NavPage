FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    NAVPAGE_DATA=/data

WORKDIR /app

COPY requirements.txt /app/requirements.txt
# 国内镜像优先，逐级回退
RUN pip install --no-cache-dir -r /app/requirements.txt \
        -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com \
 || pip install --no-cache-dir -r /app/requirements.txt \
        -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn \
 || pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY static /app/static

# Compose 管理要用 docker CLI + compose 插件。只拷两个静态二进制，
# 不装 docker.io 那一坨（省 100MB+）。插件目录必须是 /usr/local/libexec/docker/cli-plugins，
# CLI 只在这个路径下自动发现插件。
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

# 2.0.0：内置 frpc（FRP 隧道）。官方 release 按 TARGETARCH 取对应架构的包，
# 只留 frpc 一个二进制（约 15MB），frps 由用户自己的公网服务器跑。
ARG TARGETARCH
ARG FRP_VERSION=0.61.1
RUN set -eux; \
    arch="${TARGETARCH:-amd64}"; \
    url="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${arch}.tar.gz"; \
    (python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/tmp/frp.tgz')" "$url" \
     || python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/tmp/frp.tgz')" "https://ghfast.top/$url" \
     || python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/tmp/frp.tgz')" "https://gh-proxy.com/$url"); \
    tar -xzf /tmp/frp.tgz -C /tmp; \
    mv /tmp/frp_${FRP_VERSION}_linux_${arch}/frpc /usr/local/bin/frpc; \
    chmod +x /usr/local/bin/frpc; rm -rf /tmp/frp.tgz /tmp/frp_${FRP_VERSION}_linux_${arch}; \
    frpc -v

RUN mkdir -p /data/wallpapers /data/icons /data/gw

# 8080 = 面板；8081/18443 = 2.0.0 内置反代的 HTTP/HTTPS 监听（可在设置里改）
EXPOSE 8080 8081 18443

HEALTHCHECK --interval=30s --timeout=6s --start-period=12s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=5).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
