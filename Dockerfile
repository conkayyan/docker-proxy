# docker-proxy 镜像
# 用法：docker compose up -d，然后浏览器访问 http://<host>:5000
# 默认账号 admin / admin123（首次启动 init_db 自动建）

FROM python:3.11-slim

# 时区：默认 Asia/Shanghai，需要别的时区在 build 时传 --build-arg TZ=Asia/Tokyo
# tzdata 同时给系统（/usr/share/zoneinfo）和 Python（zoneinfo）用。
ARG TZ=Asia/Shanghai

# skopeo 是系统命令，pip 装不到；用 apt
# ca-certificates：拉镜像时源 registry 的 TLS 证书验证靠它（python:3.11-slim 默认带，
# 这里再装一次显式声明，免得起新 base 镜像后静默丢失）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends skopeo tzdata ca-certificates curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 验证一下 skopeo 装上了，build 时就 fail
RUN skopeo --version

# ------------------------------------------------------------------
# docker CLI（只需要 client，不用 dockerd）。
# 我们用 docker pull → skopeo docker-daemon → docker rmi 流水线
# 把远端镜像推到目标 registry（详见 app.py._build_pipeline），
# 所以容器里必须能跑 docker 命令 —— 但 daemon 用的是宿主机的，
# 通过挂载 /var/run/docker.sock（或 DOCKER_HOST 环境变量）连过去。
#
# 用 download.docker.com 的静态二进制：单文件、无 systemd 依赖、
# 多架构走 BuildKit 自动注入的 TARGETARCH，比 apt 装 docker-ce-cli 更轻。
ARG DOCKER_CLI_VERSION=27.3.1
ARG TARGETARCH
RUN set -eux; \
    case "$TARGETARCH" in \
        amd64)  DOCKER_ARCH=x86_64 ;; \
        arm64)  DOCKER_ARCH=aarch64 ;; \
        *) echo "Unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_CLI_VERSION}.tgz" \
      -o /tmp/docker.tgz; \
    tar -xzf /tmp/docker.tgz -C /tmp; \
    install -m 0755 /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker /tmp/docker.tgz; \
    docker --version

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
#
# docker 组（GID 999）：让 app 用户能访问宿主挂进来的 /var/run/docker.sock，
# 这样 source_type=docker 才能 docker pull / docker rmi。如果宿主 docker 组
# 的 GID 不是 999，启动时把宿主 socket GID 告诉容器（或 DOCKER_HOST=tcp://…）。
RUN useradd -m -u 1000 -s /bin/bash app \
    && groupadd --system --gid 999 docker \
    && usermod -aG docker app

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
