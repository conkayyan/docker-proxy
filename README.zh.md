# docker-proxy

> [English](./README.md) | 中文

一个基于 Flask 的小型工具：用 Web 登录后，通过 `docker` CLI（`docker login` → `docker tag` → `docker push`）把远端镜像推到目标 Harbor/Registry。
目标仓库的用户名/密码保存在本地 SQLite 中（Fernet 加密）。

## 功能

- 本地账号登录（SQLite，密码 `werkzeug` 哈希，**无注册入口，私有部署**）
- 在 Web 上管理多个目标 Registry（地址 + 用户名 + 密码，加密落库）
- 表单提交源镜像 + 目标镜像 + 目标 Registry，后台 worker 按 pipeline 执行拷贝
- 实时查看任务状态与日志（前端轮询）

## 依赖

- Python 3.10+
- `docker` CLI（容器镜像已自带静态 docker 二进制；本地直接跑请自行安装 Docker Desktop / docker-ce）。所有源类型都依赖它：`docker pull` / `docker tag` / `docker push` 都要走它。

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

镜像基于 `python:3.11-slim`：从 `download.docker.com` 拉静态 docker CLI；以非 root 用户（uid 1000）跑；自带 healthcheck（30s 探一次 `/login`）。多架构：`linux/amd64` + `linux/arm64`。

> **`docker-compose.yml` 默认把 `/var/run/docker.sock` 挂进了容器**，worker 才能跑 `docker pull` / `docker tag` / `docker push`。这等于把宿主 docker 控制权交给容器内的 app 用户（可起特权容器、挂载 / 等）—— 私有部署可接受；公网/多租户请把挂载去掉并把 `DOCKER_HOST` 指向远端 daemon。

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
| `LISTEN_HOST` | 监听地址（传给 `app.run()`） | `0.0.0.0` |
| `LISTEN_PORT` | 监听端口（传给 `app.run()`；和 `ports` 段、healthcheck URL 保持一致） | `5000` |
| `FLASK_DEBUG` | 设为 `1` 开启 Flask debug 模式（自动重载、交互式 traceback）。**生产别开** —— debugger 可执行任意代码。 | `0` |

## 首次启动会自动

- 在 `instance/`（本地）或 `/app/instance`（容器）下创建 SQLite 数据库 `app.db`
- 生成 Fernet 密钥 `secret.key`（用于加密 Registry 密码）
- **创建默认账号 `admin` / `admin123`**（如果不存在的话；idempotent）

> 默认密码是固定的，部署到非私网环境前请：
> 1. 改 `SECRET_KEY` 为随机字符串（建议 64+ 字符 hex）
> 2. 在「我的账号」里把 `admin` 的密码改了，或在 UI 里建新管理员后把 `admin` 删了

## 命令映射

每次任务由 worker 通过 `docker` CLI 按顺序跑下列命令：

```
docker pull <source_image>
docker login -u <username> -p ******** <registry.url>
docker tag <source_image> <registry.url>/<registry.project>/<dest_image>
docker push <registry.url>/<registry.project>/<dest_image>
docker rmi <source_image> <registry.url>/<registry.project>/<dest_image>   # 只有勾选「推完后清理本地副本」时才跑
docker logout <registry.url>
```

> 要求容器内的 `docker` CLI 能连通一个 docker daemon：挂 `/var/run/docker.sock` 或设 `DOCKER_HOST`。`docker-compose.yml` 默认已经挂好。

**多架构镜像**会被作为完整的 manifest list 一起推送 —— `docker push` 默认会推本地镜像里包含的所有架构，无需额外 flag。

**「推完后清理本地副本」复选框**（默认勾选）控制 `docker rmi` 这一步。不勾则保留本地镜像 —— 适合自己 build、推完还想在宿主机用的场景。`docker pull` 失败的话后面 login/tag/push/rmi/logout 都跳过（本地没图可推）；`docker rmi` / `docker logout` 失败只记日志，不影响任务成败。

**Registry 级别的 project 前缀**在「Registry 管理 → 新增/编辑」中为每个 Registry 单独设置，留空则不追加。新建 Registry 时表单的默认值取自环境变量 `HARBOR_PROJECT`（缺省 `docker-proxy`）。

| 表单输入 | 实际流水线 |
| --- | --- |
| 源镜像: `docker.io/library/nginx:1.27`<br>目标镜像: `nginx:1.27`<br>Registry: `harbor.company.local`，project: `docker-proxy`<br>清理本地副本: 勾选 | `docker pull docker.io/library/nginx:1.27` → `docker login -u admin -p ******** harbor.company.local` → `docker tag docker.io/library/nginx:1.27 harbor.company.local/docker-proxy/nginx:1.27` → `docker push harbor.company.local/docker-proxy/nginx:1.27` → `docker rmi docker.io/library/nginx:1.27 harbor.company.local/docker-proxy/nginx:1.27` → `docker logout harbor.company.local` |
| 同上，清理本地副本: 不勾 | 同上，但去掉 `docker rmi` 那一步。 |

任务详情页会把整条流水线（含每条命令）按行展开，login 命令里的密码已掩码。

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

### 连不上 docker daemon

如果容器访问不到 docker daemon（比如为了安全把 `/var/run/docker.sock` 挂载注释掉了），任务会立即失败，报 `docker command not found` / `Cannot connect to the Docker daemon`。处理办法：把 socket 挂回来，或设 `DOCKER_HOST` 指向可达的 daemon。

## 目录

```
app.py                # Flask 主程序：模型、路由、worker、流水线
templates/            # Jinja2 模板
static/style.css      # 基础样式
instance/             # 运行时生成：SQLite + Fernet key（加入 .gitignore）
```
