#!/bin/sh
# Container entrypoint.
#
# Fixes ownership of the bind-mounted /app/instance directory before the
# app starts. When the host side of the volume doesn't exist, Docker
# auto-creates it as root, and the unprivileged "app" user (UID 1000)
# can't write the Fernet key or the SQLite DB there — the container
# crash-loops on PermissionError. Re-chowning on every start is cheap
# and idempotent, and it makes `docker compose up` work on a clean host.
#
# Also adapts the app user to whichever GID owns /var/run/docker.sock on
# the host (commonly 999 on Debian/Ubuntu; varies on Mac/Windows Docker
# Desktop). Without this, source_type=docker would silently fail with
# "permission denied" on `docker pull`.
set -eu

mkdir -p /app/instance
chown -R app:app /app/instance

# 运行时设置系统时区：docker-compose 传入的 TZ 会覆盖 Dockerfile 烘焙的默认值。
# 既要让 /etc/localtime 指向正确的 zoneinfo（子进程 date / docker CLI 等会读），
# 也让 TZ 环境变量透传给 python —— entrypoint 不调 tzset，glibc 在 exec 时
# 会自己读取 TZ，所以只要保证这里 export 即可。
if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/$TZ" ]; then
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
    echo "$TZ" > /etc/timezone
    export TZ
elif [ -n "${TZ:-}" ]; then
    echo "warning: TZ='$TZ' not found in /usr/share/zoneinfo, keeping system default" >&2
fi

# 适配宿主机 docker socket 的 GID：起一个同名 group 把 app 加进去。
# 没有挂 socket（DOCKER_HOST=tcp://...）就跳过。
if [ -S /var/run/docker.sock ]; then
    SOCK_GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo "")
    if [ -n "$SOCK_GID" ] && ! getent group "$SOCK_GID" >/dev/null 2>&1; then
        groupadd -g "$SOCK_GID" docker-host 2>/dev/null || true
    fi
    if [ -n "$SOCK_GID" ]; then
        # 已在 docker 组（GID 999，Dockerfile 里建的）就跳过，避免 usermod 噪音
        ALREADY=$(id -G app | tr ' ' '\n' | grep -Fx "$SOCK_GID" || true)
        if [ -z "$ALREADY" ]; then
            usermod -aG "$SOCK_GID" app 2>/dev/null || true
        fi
    fi
fi

# Drop to the unprivileged user and exec the CMD.
exec runuser -u app -- "$@"
