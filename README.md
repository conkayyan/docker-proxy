# docker-proxy

> English | [中文](./README.zh.md)

A small Flask-based tool: log in via the web UI, then push remote images to a target Harbor/Registry through `skopeo copy`.
Target registry credentials are stored locally in SQLite (Fernet-encrypted).

## Features

- Local account login (SQLite, passwords hashed with `werkzeug`, **no signup endpoint, intended for private deployment**)
- Manage multiple target Registries from the web UI (address + username + password, encrypted at rest)
- Submit source image + destination image + target Registry via a form; a background worker invokes `skopeo copy`
- View task status and logs in real time (frontend polling)

## Requirements

- Python 3.10+
- `skopeo` (system command, `which skopeo` must succeed; on macOS use `brew install skopeo`)

## Quick Start

### Option A: Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Optional: set a custom session key; if unset, a random one is generated per start (old sessions invalidated on restart)
export SECRET_KEY='change-me'

python app.py
# Open http://127.0.0.1:5000 in your browser
```

### Option B: Docker (recommended for private deployment)

The image is published to GitHub Container Registry on every push to `main` and on `v*` tags.

```bash
# Pull the prebuilt image and start (no local build needed)
docker compose up -d
# Open http://localhost:5000 in your browser
# Data is persisted in ./data/ (SQLite + Fernet key)
```

The image is based on `python:3.11-slim`, installs skopeo via apt, runs as a non-root user (uid 1000), and includes a healthcheck (probes `/login` every 30s). Multi-arch: `linux/amd64` and `linux/arm64`.

Available tags:

| Tag | When pushed |
| --- | --- |
| `latest` | every push to `main` |
| `main` | every push to `main` |
| `vX.Y.Z`, `vX.Y`, `vX` | every `v*` tag (e.g. `git tag v1.2.3 && git push --tags`) |
| `<sha>` | every build |

Pin a version in `docker-compose.yml` by changing `image: ghcr.io/conkayyan/docker-proxy:latest` to e.g. `:v1.2.3`.

> **The image is private by default.** Make it public at https://github.com/users/conkayyan/packages/container/docker-proxy/settings (or log in with a PAT that has `read:packages`) to pull without auth.

#### Build from source

If you'd rather build locally (e.g. you forked the repo or need a custom image):

```bash
# In docker-compose.yml: comment out the `image:` line and uncomment the `build: .` line
docker compose up -d --build
# Equivalent standalone: docker build -t docker-proxy . && docker run -d -p 5000:5000 \
#   -v "$(pwd)/data:/app/instance" --name docker-proxy docker-proxy:latest
```

Environment variables (edit in `docker-compose.yml`):

| Variable | Purpose | Default |
| --- | --- | --- |
| `SECRET_KEY` | Flask session key | Random (invalidated on restart) |
| `HARBOR_PROJECT` | Default value for the `project` field when creating a new Registry | `docker-proxy` |

## On first startup

- Creates the SQLite database `app.db` under `instance/` (local) or `/app/instance` (container)
- Generates a Fernet key `secret.key` (used to encrypt Registry passwords)
- **Creates the default account `admin` / `admin123`** (if it does not exist; idempotent)

> The default password is fixed. Before deploying to anything other than a private network:
> 1. Change `SECRET_KEY` to a random string (64+ hex chars recommended)
> 2. Change the `admin` password from "My Account", or create a new admin in the UI and delete `admin`

## Command mapping

Web form fields → the actual `skopeo` command executed:

```
skopeo copy [--dest-tls-verify=false] \
  docker://<source_image> \
  docker://<registry.url>/<registry.project>/<dest_image> \
  --dest-creds <username>:<password>
```

`registry.project` is configured per-Registry under "Registry Management → New/Edit"; leave empty to omit the prefix.
The default value when creating a new Registry comes from the `HARBOR_PROJECT` environment variable (defaults to `docker-proxy`).

| Form input | Actual command |
| --- | --- |
| Source: `docker.io/library/nginx:1.27`<br>Destination: `nginx:1.27`<br>Registry: `harbor.company.local`, project: `docker-proxy` | `skopeo copy docker://docker.io/library/nginx:1.27 docker://harbor.company.local/docker-proxy/nginx:1.27 --dest-creds admin:YourStrongPassword123` |

> **Destination image naming convention**:
> - Use only `<image>:<tag>` for the destination; **do not** include Docker Hub namespace prefixes such as `library/`.
> - The full path is composed by the app as `<registry.url>/<registry.project>/<image>:<tag>`, matching the "namespace/repo:tag" structure used by ACR, Harbor, and other mainstream registries.
> - If the destination image already starts with `<project>/` (e.g. copied by the user), the app will not duplicate the prefix.

> **Change the default project**: edit the `Project path prefix` field in the Registry detail page; the change only affects that Registry.
> **Set the default for all new Registries**:
> ```bash
> export HARBOR_PROJECT=my-team     # New Registries default to project = my-team
> ```
> If the destination image already starts with `<project>/`, the prefix is not duplicated.

## Layout

```
app.py                # Flask main program: models, routes, worker
templates/            # Jinja2 templates
static/style.css      # Base styles
instance/             # Generated at runtime: SQLite + Fernet key (add to .gitignore)
```
