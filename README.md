# docker-proxy

一个基于 Flask 的小型工具：用 Web 登录后，把远端镜像通过 `skopeo copy` 推到目标 Harbor/Registry。
目标仓库的用户名/密码保存在本地 SQLite 中（Fernet 加密）。

## 功能

- 本地账号登录/注册（SQLite，密码 `werkzeug` 哈希）
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

## 命令映射

Web 表单字段 → 最终执行的 `skopeo` 命令：

```
skopeo copy [--dest-tls-verify=false] \
  docker://<source_image> \
  docker://<registry.url>/<HARBOR_PROJECT>/<dest_image> \
  --dest-creds <username>:<password>
```

目标路径会自动追加 project 前缀（默认 `docker-proxy`）。例如下方需求中的命令：

| 表单输入 | 实际执行 |
| --- | --- |
| 源镜像: `docker.io/library/nginx:1.27`<br>目标镜像: `library/nginx:1.27`<br>Registry: `harbor.company.local` | `skopeo copy docker://docker.io/library/nginx:1.27 docker://harbor.company.local/docker-proxy/library/nginx:1.27 --dest-creds admin:YourStrongPassword123` |

> **自定义 project**：通过环境变量修改默认前缀；设为空字符串可关闭自动追加。
> ```bash
> export HARBOR_PROJECT=my-team     # 推送路径变为 my-team/library/nginx:1.27
> export HARBOR_PROJECT=            # 关闭自动前缀
> ```
> 如果目标镜像已以 `HARBOR_PROJECT/` 开头（例如用户复制时带上了），不会重复追加。

## 目录

```
app.py                # Flask 主程序：模型、路由、worker
templates/            # Jinja2 模板
static/style.css      # 基础样式
instance/             # 运行时生成：SQLite + Fernet key（加入 .gitignore）
```
