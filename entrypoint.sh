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
