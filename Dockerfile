# docker-proxy 镜像
# 用法：docker compose up -d，然后浏览器访问 http://<host>:5000
# 默认账号 admin / admin123（首次启动 init_db 自动建）

FROM python:3.11-slim

# skopeo 是系统命令，pip 装不到；用 apt
RUN apt-get update \
    && apt-get install -y --no-install-recommends skopeo \
    && rm -rf /var/lib/apt/lists/*

# 验证一下 skopeo 装上了，build 时就 fail
RUN skopeo --version

WORKDIR /app

# 先装依赖（缓存复用）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码（依赖没变就不会重装）
COPY app.py .
COPY templates/ ./templates/
COPY static/ ./static/

# 数据目录用 volume 挂载（SQLite + Fernet key）
RUN mkdir -p /app/instance \
    && useradd -m -u 1000 -s /bin/bash app \
    && chown -R app:app /app

USER app

# Flask app 默认监听 0.0.0.0:5000（app.py 里已 hardcode；如要换端口改 app.py）
EXPOSE 5000

# 健康检查：GET /login 应返回 200
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/login', timeout=3)" || exit 1

# 启动（init_db 在 import 时就跑）
CMD ["python", "app.py"]
