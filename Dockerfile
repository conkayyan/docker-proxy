# docker-proxy 镜像
# 用法：docker compose up -d，然后浏览器访问 http://<host>:5000
# 默认账号 admin / admin123（首次启动 init_db 自动建）

FROM python:3.11-slim

# 时区：默认 Asia/Shanghai，需要别的时区在 build 时传 --build-arg TZ=Asia/Tokyo
# tzdata 同时给系统（/usr/share/zoneinfo）和 Python（zoneinfo）用。
ARG TZ=Asia/Shanghai

# skopeo 是系统命令，pip 装不到；用 apt
RUN apt-get update \
    && apt-get install -y --no-install-recommends skopeo tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 验证一下 skopeo 装上了，build 时就 fail
RUN skopeo --version

ENV TZ=$TZ

WORKDIR /app

# 先装依赖（缓存复用）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码（依赖没变就不会重装）
COPY app.py .
COPY templates/ ./templates/
COPY static/ ./static/

# 数据目录用 volume 挂载（SQLite + Fernet key）
# 创建 app 用户，但不切换；entrypoint 负责在启动时修正 bind-mount 的属主
# （Docker 会把不存在的 host 目录创建为 root，会让 app 用户写不动 secret.key）
RUN useradd -m -u 1000 -s /bin/bash app

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Flask app 默认监听 0.0.0.0:5000（app.py 里已 hardcode；如要换端口改 app.py）
EXPOSE 5000

# 健康检查：GET /login 应返回 200
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/login', timeout=3)" || exit 1

# entrypoint 会修正 /app/instance 属主，再以 app 用户身份 exec CMD
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# 启动（init_db 在 import 时就跑）
CMD ["python", "app.py"]
