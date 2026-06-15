# docker-proxy

一个基于 Flask 的小型工具：用 Web 登录后，把远端镜像通过 `skopeo copy` 推到目标 Harbor/Registry。
目标仓库的用户名/密码保存在本地 SQLite 中（Fernet 加密）。

## 功能

- 本地账号登录（SQLite，密码 `werkzeug` 哈希，**无注册入口，私有部署**）
- 在 Web 上管理多个目标 Registry（地址 + 用户名 + 密码，加密落库）
- 表单提交源镜像 + 目标镜像 + 目标 Registry，后台 worker 调用 `skopeo copy`
- 实时查看任务状态与日志（前端轮询）

## 依赖

- Python 3.10+
- `skopeo`（系统命令，需 `which skopeo` 可用；macOS 可 `brew install skopeo`）

## 启动

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 可选：自定义 session 密钥；不设则每次启动随机（重启后旧 session 失效）
export SECRET_KEY='change-me'

python app.py
# 浏览器打开 http://127.0.0.1:5000
```

首次启动会自动：

- 在 `instance/` 下创建 SQLite 数据库 `app.db`
- 生成 Fernet 密钥 `instance/secret.key`（用于加密 Registry 密码，请勿提交到 git）
- **创建默认账号 `admin` / `admin123`**（如果不存在的话；idempotent）

> 默认密码是固定的，部署到非私网环境前请直接 `sqlite3 instance/app.db` 改掉，或在登录后从 UI 改密（待加）。

## 命令映射

Web 表单字段 → 最终执行的 `skopeo` 命令：

```
skopeo copy [--dest-tls-verify=false] \
  docker://<source_image> \
  docker://<registry.url>/<registry.project>/<dest_image> \
  --dest-creds <username>:<password>
```

`registry.project` 在「Registry 管理 → 新增/编辑」中为每个 Registry 单独设置，留空则不追加前缀。
新建 Registry 时表单的默认值取自环境变量 `HARBOR_PROJECT`（缺省 `docker-proxy`）。

| 表单输入 | 实际执行 |
| --- | --- |
| 源镜像: `docker.io/library/nginx:1.27`<br>目标镜像: `nginx:1.27`<br>Registry: `harbor.company.local`，project: `docker-proxy` | `skopeo copy docker://docker.io/library/nginx:1.27 docker://harbor.company.local/docker-proxy/nginx:1.27 --dest-creds admin:YourStrongPassword123` |

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

## 目录

```
app.py                # Flask 主程序：模型、路由、worker
templates/            # Jinja2 模板
static/style.css      # 基础样式
instance/             # 运行时生成：SQLite + Fernet key（加入 .gitignore）
```
