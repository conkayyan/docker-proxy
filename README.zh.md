# docker-proxy

> [English](./README.md) | 中文

一个基于 Flask 的小型工具：用 Web 登录后，把远端镜像通过 `skopeo copy` 推到目标 Harbor/Registry。
目标仓库的用户名/密码保存在本地 SQLite 中（Fernet 加密）。

## 功能

- 本地账号登录（SQLite，密码 `werkzeug` 哈希，**无注册入口，私有部署**）
- 在 Web 上管理多个目标 Registry（地址 + 用户名 + 密码，加密落库）
- 表单提交源镜像 + 目标镜像 + 目标 Registry，后台 worker 按 pipeline 执行拷贝
- 实时查看任务状态与日志（前端轮询）

## 依赖

- Python 3.10+
- `skopeo`（系统命令，需 `which skopeo` 可用；macOS 可 `brew install skopeo`）
- 如果要用**源类型 `docker`**：还需要 `docker` CLI 能联通一个 docker daemon（见 [命令映射](#命令映射)）；只用 `docker-daemon` 则无需 docker

## 启动

### 方式 A：本地直接跑

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 可选：自定义 session 密钥；不设则每次启动随机（重启后旧 session 失效）
export SECRET_KEY='change-me'

python app.py
# 浏览器打开 http://127.0.0.1:5000
```

### 方式 B：Docker（推荐私有部署）

每次 push 到 `main` 或打 `v*` tag，CI 都会把镜像推到 GitHub Container Registry。

```bash
# 直接拉预构建镜像启动（不需要本地 build）
docker compose up -d
# 浏览器打开 http://localhost:5000
# 数据持久化在 ./data/ 目录（SQLite + Fernet key）
```

镜像基于 `python:3.11-slim`：apt 装 skopeo；从 `download.docker.com` 拉静态 docker CLI；以非 root 用户（uid 1000）跑；自带 healthcheck（30s 探一次 `/login`）。多架构：`linux/amd64` + `linux/arm64`。

> **`docker-compose.yml` 默认把 `/var/run/docker.sock` 挂进了容器**，源类型 `docker` 才能跑 `docker pull` / `docker rmi`。这等于把宿主 docker 控制权交给容器内的 app 用户（可起特权容器、挂载 / 等）—— 私有部署可接受；公网/多租户请参考 [源类型：docker](#源类型dockerdocker-cli--docker-daemon-流水线)。

可用 tag：

| Tag | 触发 |
| --- | --- |
| `latest` | 每次 push 到 `main` |
| `main` | 每次 push 到 `main` |
| `vX.Y.Z`、`vX.Y`、`vX` | 每次打 `v*` tag（例：`git tag v1.2.3 && git push --tags`） |
| `<sha>` | 每次 build |

要锁版本，改 `docker-compose.yml` 里的 `image: ghcr.io/conkayyan/docker-proxy:latest` 为例如 `:v1.2.3`。

> **镜像默认私有**。要在免登录下拉取，去 https://github.com/users/conkayyan/packages/container/docker-proxy/settings 改成 Public（或用一个有 `read:packages` 权限的 PAT 登录）。

#### 本地构建

如果想自己 build（比如 fork 后改东西）：

```bash
# 在 docker-compose.yml 里：注释掉 image 行，打开 build: . 行
docker compose up -d --build
# 等价独立命令：docker build -t docker-proxy . && docker run -d -p 5000:5000 \
#   -v "$(pwd)/data:/app/instance" --name docker-proxy docker-proxy:latest
```

环境变量（docker-compose.yml 里改）：

| 变量 | 用途 | 默认 |
| --- | --- | --- |
| `SECRET_KEY` | Flask session 密钥 | 随机（重启失效） |
| `HARBOR_PROJECT` | 新建 Registry 时 project 字段的默认值 | `docker-proxy` |
| `DOCKER_HOST` | 覆盖 `docker` CLI 用的 daemon 地址（如 `tcp://docker.example.com:2375`）。不设就走 `/var/run/docker.sock`。 | 未设 |
| `SKOPEO_HTTP2` | 透传给 skopeo，启用 HTTP/2 多路复用；对支持 HTTP/2 的 registry 吞吐显著提升。 | 未设（skopeo 自定） |
| `SKOPEO_MAX_CONCURRENT_DOWNLOADS` | 透传给 skopeo，单次 copy 并行下载的 blob 数。skopeo 默认 4；内网高速环境可拉到 10 左右加速大镜像。 | 未设（skopeo 默认 4） |

## 首次启动会自动

- 在 `instance/`（本地）或 `/app/instance`（容器）下创建 SQLite 数据库 `app.db`
- 生成 Fernet 密钥 `secret.key`（用于加密 Registry 密码）
- **创建默认账号 `admin` / `admin123`**（如果不存在的话；idempotent）

> 默认密码是固定的，部署到非私网环境前请：
> 1. 改 `SECRET_KEY` 为随机字符串（建议 64+ 字符 hex）
> 2. 在「我的账号」里把 `admin` 的密码改了，或在 UI 里建新管理员后把 `admin` 删了

## 命令映射

表单提供两种源类型，对应不同流水线。

### 源类型 `docker`：docker CLI → docker-daemon 流水线

> 要求容器内的 `docker` CLI 能连通一个 docker daemon：挂 `/var/run/docker.sock` 或设 `DOCKER_HOST`。`docker-compose.yml` 默认已经挂好。

每次任务按顺序跑三条命令：

```
docker pull <source_image>
skopeo copy [--multi-arch=all] [--dest-tls-verify=false] --retry-times <N> \
  docker-daemon:<source_image> \
  docker://<registry.url>/<registry.project>/<dest_image> \
  --dest-creds <username>:<password>
docker rmi <source_image>   # 清理；这一步失败不影响任务成败
```

`--retry-times <N>` 在表单上可配（默认 `3`，范围 `0–10`；`0` 即不重试）。控制 skopeo 在网络抖动 / registry 5xx 等瞬时错误时的重试次数；默认值与 skopeo 自身一致 —— 想更早硬失败就调小，registry 不稳就调大。

中间的 `docker pull` 让这个模式很有用：可以直接复用宿主 `~/.docker/config.json` 里配好的镜像仓库镜像 / 鉴权；最后 `docker rmi` 顺手清掉本地副本，不挤占宿主磁盘。`docker pull` 失败的话后面 skopeo / rmi 都跳过（本地没图可推，也没东西可清）。

### 源类型 `docker-daemon`

只跑一条命令。镜像必须已经存在于宿主 docker daemon 里 —— worker 不会帮你 `docker pull`，也**不会**在推完之后 `docker rmi`（那是你的镜像，不是我们临时拉的）：

```
skopeo copy [--multi-arch=all] [--dest-tls-verify=false] --retry-times <N> \
  docker-daemon:<source_image> \
  docker://<registry.url>/<registry.project>/<dest_image> \
  --dest-creds <username>:<password>
```

`--retry-times <N>` 在表单上可配（默认 `3`，范围 `0–10`；`0` 即不重试）。控制 skopeo 在网络抖动 / registry 5xx 等瞬时错误时的重试次数；默认值与 skopeo 自身一致 —— 想更早硬失败就调小，registry 不稳就调大。

`registry.project` 在「Registry 管理 → 新增/编辑」中为每个 Registry 单独设置，留空则不追加前缀。
新建 Registry 时表单的默认值取自环境变量 `HARBOR_PROJECT`（缺省 `docker-proxy`）。

| 表单输入 | 实际流水线（`docker`） |
| --- | --- |
| 源镜像: `docker.io/library/nginx:1.27`<br>目标镜像: `nginx:1.27`<br>Registry: `harbor.company.local`，project: `docker-proxy` | `docker pull docker.io/library/nginx:1.27` → `skopeo copy --retry-times 3 docker-daemon:docker.io/library/nginx:1.27 docker://harbor.company.local/docker-proxy/nginx:1.27 --dest-creds admin:********` → `docker rmi docker.io/library/nginx:1.27` |

任务详情页会把整条流水线（含每条命令）按行展开，凭证已掩码。

> **目标镜像的命名规范**：
> - 目标只填 `<image>:<tag>`，**不要带** `library/` 这类 Docker Hub 的 namespace 前缀。
> - 完整路径由 app 拼成 `<registry.url>/<registry.project>/<image>:<tag>`，与 ACR / Harbor 等主流 registry 的「命名空间/仓库:tag」结构一致。
> - 如果目标镜像已以 `<project>/` 开头（例如用户复制时带上了），app 不会重复追加。

> **修改默认 project**：在 Registry 详情中直接改 `项目路径前缀` 字段；改完只影响该 Registry。
> **设置所有新 Registry 的默认值**：
> ```bash
> export HARBOR_PROJECT=my-team     # 新建 Registry 时 project 默认填 my-team
> ```
> 如果目标镜像已以 `<project>/` 开头（例如用户复制时带上了），不会重复追加。

### 源类型 `docker` 但连不上 docker daemon

如果容器访问不到 docker daemon（比如为了安全把 `/var/run/docker.sock` 挂载注释掉了），任务会立即失败，报 `docker command not found` / `Cannot connect to the Docker daemon`。处理办法：要么改用源类型 `docker-daemon`（先用 `docker pull` / `docker load` 把镜像准备好），要么把 socket 挂回来 / 设 `DOCKER_HOST` 指向远端 daemon。

## 目录

```
app.py                # Flask 主程序：模型、路由、worker、流水线
templates/            # Jinja2 模板
static/style.css      # 基础样式
instance/             # 运行时生成：SQLite + Fernet key（加入 .gitignore）
```
