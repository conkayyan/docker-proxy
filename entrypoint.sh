#!/bin/sh
# Container entrypoint.
#
# Fixes ownership of the bind-mounted /app/instance directory before the
# app starts. When the host side of the volume doesn't exist, Docker
# auto-creates it as root, and the unprivileged "app" user (UID 1000)
# can't write the Fernet key or the SQLite DB there — the container
# crash-loops on PermissionError. Re-chowning on every start is cheap
# and idempotent, and it makes `docker compose up` work on a clean host.
set -eu

mkdir -p /app/instance
chown -R app:app /app/instance

# Drop to the unprivileged user and exec the CMD.
exec runuser -u app -- "$@"
