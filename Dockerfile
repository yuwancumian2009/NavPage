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

RUN mkdir -p /data/wallpapers /data/icons

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=6s --start-period=12s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=5).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
