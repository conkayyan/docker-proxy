"""docker-proxy — Flask UI for pushing images to a target registry via docker CLI.

工作流：docker pull（可选）→ docker login → docker tag → docker push → docker rmi（可选）→ docker logout。
目标 Registry 凭据加密保存在本地 SQLite；登录态用 Flask-Login session。
"""

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from queue import Queue

import requests
from cryptography.fernet import Fernet
from requests.auth import HTTPBasicAuth
from flask import (
    Flask,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from flask_wtf import FlaskForm
from wtforms import BooleanField, IntegerField, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, EqualTo, Length, NumberRange, Optional
from werkzeug.security import check_password_hash, generate_password_hash

from i18n import (
    LOCALE_COOKIE,
    LOCALE_COOKIE_MAX_AGE,
    SUPPORTED_LOCALES,
    _,
    _l,
    get_locale,
    resolve_locale_from_request,
)


# ---------------------------------------------------------------------------
# 配置 / 路径
# ---------------------------------------------------------------------------

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)
DB_PATH = os.path.join(INSTANCE_DIR, "app.db")
KEY_FILE = os.path.join(INSTANCE_DIR, "secret.key")


def _load_or_create_fernet() -> Fernet:
    """读取/生成用于加密 Registry 密码的 Fernet key。"""
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            key = f.read()
    else:
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f:
            f.write(key)
        os.chmod(KEY_FILE, 0o600)
    return Fernet(key)


# 推送到目标 Registry 时强制追加的 project 路径前缀。
# 不再是全局：每个 Registry 在创建/编辑时单独设置 project 字段。
# 保留此变量是为了给"新建 Registry"表单提供默认值；不再参与命令构建。
DEFAULT_PROJECT = os.environ.get("HARBOR_PROJECT", "docker-proxy").strip("/")


FERNET = _load_or_create_fernet()


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", os.urandom(32).hex())
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["WTF_CSRF_TIME_LIMIT"] = 60 * 60 * 8
# 私有部署工具：让浏览器不要缓存静态文件，方便改 CSS/JS 后直接刷新就能看到效果。
# 生产里反代/CDN 一般会再加自己的 cache header，这条只影响 Flask 自带的 send_file。
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Registry(db.Model):
    __tablename__ = "registries"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    url = db.Column(db.String(200), nullable=False)  # e.g. harbor.company.local
    username = db.Column(db.String(80), nullable=False)
    password_enc = db.Column(db.String(1024), nullable=False)
    # 校验 TLS 证书：默认 True。仅在自签证书 / 内网环境才置 False。
    verify_tls = db.Column(db.Boolean, default=True, nullable=False)
    # 推送到该 Registry 时，自动追加到目标镜像路径前的 project 前缀。
    # 例如 project="docker-proxy"，目标镜像 nginx:1.27 → docker-proxy/nginx:1.27。
    # 留空则不追加。建表时由 DEFAULT_PROJECT 提供初始值。
    project = db.Column(db.String(120), default=DEFAULT_PROJECT, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password: str) -> None:
        self.password_enc = FERNET.encrypt(password.encode()).decode()

    def get_password(self) -> str:
        return FERNET.decrypt(self.password_enc.encode()).decode()


class CopyTask(db.Model):
    __tablename__ = "copy_tasks"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    registry_id = db.Column(db.Integer, db.ForeignKey("registries.id"), nullable=False)
    source_image = db.Column(db.String(300), nullable=False)
    # 历史遗留字段：skopeo 时代用于区分 "docker" / "docker-daemon"。
    # 当前实现统一走 docker CLI（自动 pull），不再读这个值；保留仅为兼容老 DB 行。
    source_type = db.Column(db.String(20), default="docker", nullable=False)
    dest_image = db.Column(db.String(300), nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)
    log = db.Column(db.Text, default="", nullable=False)
    error = db.Column(db.Text, default="", nullable=False)
    return_code = db.Column(db.Integer)
    multi_arch = db.Column(db.Boolean, default=True, nullable=False)
    # 历史遗留字段：早期版本里传给 skopeo copy 的 --retry-times（默认 3）。
    # 当前实现走 docker CLI，docker daemon 自带重试；这里保留列仅为兼容老 DB 行，
    # 写入/读取时一律忽略。
    retry_times = db.Column(db.Integer, default=3, nullable=False)
    # 推完后是否 `docker rmi` 删掉本地副本（source + 临时 tag）。
    # 默认 True —— 大多数场景是「拉 → 推 → 清」的临时操作。
    # 取消勾选则保留本地镜像，适合「我自己 build 的镜像只想顺便推一份到远端」。
    cleanup = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    # 看门狗用：worker 每次落库都刷新 heartbeat_at；watchdog 据此判断 worker/docker
    # 子进程是否已挂。subprocess_pid 记录当前 docker 子进程的 PID，挂起时 watchdog 可以 SIGTERM。
    heartbeat_at = db.Column(db.DateTime)
    subprocess_pid = db.Column(db.Integer)

    user = db.relationship(
        "User",
        backref=db.backref("tasks", lazy=True, cascade="all, delete-orphan"),
    )
    registry = db.relationship("Registry", backref=db.backref("tasks", lazy=True))


# ---------------------------------------------------------------------------
# 表单
# ---------------------------------------------------------------------------


class LoginForm(FlaskForm):
    username = StringField(_l("Username"), validators=[DataRequired(), Length(1, 80)])
    password = PasswordField(_l("Password"), validators=[DataRequired()])
    submit = SubmitField(_l("Login"))


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField(_l("Current password"), validators=[DataRequired()])
    new_password = PasswordField(
        _l("New password (min 6 chars)"), validators=[DataRequired(), Length(min=6, max=128)]
    )
    confirm = PasswordField(
        _l("Confirm new password"),
        validators=[DataRequired(), EqualTo("new_password", message=_l("Two entries do not match"))],
    )
    submit = SubmitField(_l("Change password"))


class AdminCreateUserForm(FlaskForm):
    username = StringField(_l("Username"), validators=[DataRequired(), Length(3, 80)])
    password = PasswordField(
        _l("Password"), validators=[DataRequired(), Length(min=6, max=128)]
    )
    confirm = PasswordField(
        _l("Confirm password"),
        validators=[DataRequired(), EqualTo("password", message=_l("Two entries do not match"))],
    )
    is_admin = BooleanField(_l("Grant admin privileges"))
    submit = SubmitField(_l("Create account"))


class AdminResetPasswordForm(FlaskForm):
    new_password = PasswordField(
        _l("New password (min 6 chars)"), validators=[DataRequired(), Length(min=6, max=128)]
    )
    confirm = PasswordField(
        _l("Confirm password"),
        validators=[DataRequired(), EqualTo("new_password", message=_l("Two entries do not match"))],
    )
    submit = SubmitField(_l("Reset password"))


class RegistryForm(FlaskForm):
    name = StringField(_l("Name"), validators=[DataRequired(), Length(1, 80)])
    url = StringField(
        _l("Registry address"),
        validators=[DataRequired(), Length(3, 200)],
        description=_l("e.g. harbor.company.local"),
    )
    username = StringField(_l("Username"), validators=[DataRequired(), Length(1, 80)])
    # 编辑模式下留空 = 不修改密码（路由层判断）。新建模式下必填（路由层校验）。
    password = PasswordField(_l("Password"), validators=[Optional(), Length(max=1024)])
    # 校验 TLS 证书：默认勾选 = 安全默认；自签证书 / 内网环境可取消勾选。
    verify_tls = BooleanField(
        _l("Verify TLS certificate (recommended; uncheck for self-signed / intranet)"),
        default=True,
    )
    project = StringField(
        _l("Project path prefix (project)"),
        validators=[Length(0, 120)],
        default=DEFAULT_PROJECT,
        description=_l("Auto-prepended to the destination image on push. e.g. docker-proxy → pushed as docker-proxy/nginx:1.27; leave blank to skip."),
    )
    submit = SubmitField(_l("Save"))


class CopyForm(FlaskForm):
    registry_id = StringField(_l("Target Registry"), validators=[DataRequired()])
    source_image = StringField(
        _l("Source image"),
        validators=[DataRequired()],
        description=_l("e.g. docker.io/library/nginx:1.27. Full image:tag — the worker runs `docker pull` first; already-local images are detected and skipped."),
    )
    dest_image = StringField(
        _l("Destination image"),
        validators=[DataRequired()],
        description=_l("e.g. nginx:1.27 (only image:tag; project is auto-appended by the selected Registry)"),
    )
    cleanup = BooleanField(
        _l("Cleanup local copy after push"),
        default=True,
        description=_l("Run `docker rmi` to delete the source image and the temporary target tag locally after a successful push. Uncheck to keep your local images (e.g. for self-built images you also use elsewhere)."),
    )
    multi_arch = SelectField(
        _l("Multi-arch"),
        choices=[("1", _l("Yes")), ("0", _l("No"))],
        default="1",
        description=_l("Push the full multi-arch manifest list (amd64 / arm64 / armv7). Default Yes."),
    )
    # 历史遗留：早期 skopeo 时代的 --retry-times 字段。docker CLI 自身有重试，
    # 表单里不再展示，这里只保留字段以免 WTForms 校验老任务行时炸。
    retry_times = IntegerField(
        _l("Retry times"),
        default=3,
        validators=[NumberRange(min=0, max=10)],
        description=_l("Legacy field; docker push handles retries itself."),
    )
    submit = SubmitField(_l("Start Copy"))


# ---------------------------------------------------------------------------
# 任务队列 / 后台 worker
# ---------------------------------------------------------------------------


task_queue: "Queue[int]" = Queue()
_worker_started = False
_worker_thread: "threading.Thread | None" = None
_worker_lock = threading.Lock()

# 看门狗：worker 必须至少每 HEARTBEAT_TIMEOUT 秒刷新一次 heartbeat_at，
# 否则 watchdog 会把对应的 running 任务标为 failed 并 SIGTERM 子进程。
# 默认 120s 心跳超时（docker push 大镜像前几分钟常无日志）、10s 巡检一次；
# 可通过环境变量调。
WATCHDOG_INTERVAL_SEC = int(os.environ.get("TASK_WATCHDOG_INTERVAL", "10"))
HEARTBEAT_TIMEOUT_SEC = int(os.environ.get("TASK_HEARTBEAT_TIMEOUT", "120"))

# 心跳 ticker 间隔：_run_task 在跑期间，每 N 秒刷一次 heartbeat_at，跟
# 子进程有没有 stdout 输出无关。docker push 大镜像时切完 layer 就沉默
# 几分钟传数据，靠这个保命。可通过环境变量调。
HEARTBEAT_TICK_SEC = int(os.environ.get("TASK_HEARTBEAT_TICK", "5"))


def _ensure_worker() -> None:
    """惰性启动后台 worker + 看门狗线程（每次进程一个）。

    如果旧的 worker 线程已死（_worker_thread.is_alive() 为 False），会重置
    _worker_started 并重新拉起 —— 这条路径由 watchdog 触发，用于把"worker
    线程自己崩了但进程还活着"的情况也覆盖掉。
    """
    global _worker_started, _worker_thread
    with _worker_lock:
        if _worker_started:
            if _worker_thread is not None and _worker_thread.is_alive():
                return
            # 之前标记 started，但线程已死 → 重置，重新拉
            _worker_started = False
        _worker_thread = threading.Thread(
            target=_worker_loop, args=(app,), daemon=True
        )
        _worker_thread.start()
        threading.Thread(target=_watchdog_loop, daemon=True).start()
        _worker_started = True


def _enqueue_pending_tasks() -> None:
    """把 DB 里所有 status='pending' 的任务塞回 in-memory 队列。

    task_queue 是进程内的，进程重启 / worker 线程崩了都会丢；而 DB 里的
    pending 才是真权威。启动时 + watchdog 检测到 worker 死亡时各调一次，
    避免 pending 任务被永久遗忘。
    """
    with app.app_context():
        pending_ids = [
            t.id
            for t in CopyTask.query.filter_by(status="pending")
            .order_by(CopyTask.id)
            .all()
        ]
    for tid in pending_ids:
        task_queue.put(tid)


def _migrate_columns() -> None:
    """给已有 SQLite 表加新列。db.create_all() 不会动已存在的表。

    每条 ALTER 都包在 try/except 里：列已存在时 SQLite 抛 OperationalError，
    直接吞掉，保证幂等。
    """
    stmts = [
        "ALTER TABLE copy_tasks ADD COLUMN heartbeat_at DATETIME",
        "ALTER TABLE copy_tasks ADD COLUMN subprocess_pid INTEGER",
        "ALTER TABLE copy_tasks ADD COLUMN source_type VARCHAR(20) DEFAULT 'docker' NOT NULL",
        "ALTER TABLE copy_tasks ADD COLUMN retry_times INTEGER DEFAULT 3 NOT NULL",
        "ALTER TABLE copy_tasks ADD COLUMN cleanup BOOLEAN DEFAULT 1 NOT NULL",
    ]
    with app.app_context():
        for sql in stmts:
            try:
                db.session.execute(text(sql))
                db.session.commit()
            except Exception:
                db.session.rollback()


def _recover_unfinished_tasks() -> None:
    """服务启动时清理上一次会话遗留的 running / pending 任务。

    重启时 in-memory 的 task_queue 已经丢失；如果还把这些任务的 id 塞回队列
    让新 worker 接着跑，dashboard 上就会混着"上次会话的"和"这次会话的"任务，
    行为不符合用户预期（重启即清场，重试由用户手动发起）。所以把 status 为
    running 和 pending 的行全部置为 failed，错误信息区分两种来源。
    """
    with app.app_context():
        now = datetime.utcnow()
        # running：上一次会话跑到一半没跑完
        running = CopyTask.query.filter(CopyTask.status == "running").all()
        for task in running:
            task.status = "failed"
            task.error = "Detected unfinished task from previous session at startup; auto-marked as failed"
            task.finished_at = now
        # pending：上一次会话还没轮到跑的，统一判失败，由用户主动重试
        pending = CopyTask.query.filter(CopyTask.status == "pending").all()
        for task in pending:
            task.status = "failed"
            task.error = "Detected unstarted task from previous session at startup; auto-marked as failed"
            task.finished_at = now
        if running or pending:
            db.session.commit()


def _target_ref(task: CopyTask) -> str:
    """返回 docker push 的目标引用 = registry.url/project/dest_image。

    与 _project_dest 一起把 dest_image 拼上 project 前缀（避免重复）。
    """
    return f"{task.registry.url}/{_project_dest(task.dest_image, task.registry.project)}"


def _login_cmd(task: CopyTask) -> list[str]:
    """docker login 的 argv。密码走 -p 参数（与老 skopeo --dest-creds 等价，
    简单且 _mask_command_str 可以直接遮；如要更安全可改 --password-stdin）。
    """
    cmd: list[str] = ["docker", "login"]
    if not task.registry.verify_tls:
        # docker login 没有 --tls-verify 之类的开关；用环境变量影响 daemon 行为比较隐式，
        # 这里只把凭据传过去，推送时由 docker push 配合 DOCKER_CONTENT_TRUST 等处理。
        # 保留 verify_tls 字段为兼容旧 Registry 配置；新流程里此 flag 不再影响 login。
        pass
    cmd.extend(["-u", task.registry.username])
    cmd.extend(["-p", task.registry.get_password()])
    cmd.append(task.registry.url)
    return cmd


def _build_pipeline(task: CopyTask) -> list[tuple[str, list[str]]]:
    """返回该任务实际要执行的命令列表（label, argv）。

    通用流程（所有任务都跑）：
      docker pull → docker login → docker tag → docker push → docker logout

    cleanup=True 时尾部多一步 `docker rmi <source> <target>` 清理本地副本。
    cleanup=False 时保留本地镜像（用户自己 build 的、还要在本地用的）。

    中间任何一步失败：
    - pull 失败 → 后面 login/tag/push/rmi/logout 全部跳过（本地没图可推）
    - push 失败 → rmi 仍执行（清掉 tag 出来的本地副本），logout 仍执行
    - rmi / logout 失败 → 只记日志，不影响任务成败
    """
    target = _target_ref(task)
    pipeline: list[tuple[str, list[str]]] = [
        (
            _("docker pull (fetch source image locally)"),
            ["docker", "pull", task.source_image],
        ),
        (
            _("docker login (authenticate to target registry)"),
            _login_cmd(task),
        ),
        (
            _("docker tag (retag local image for target registry)"),
            ["docker", "tag", task.source_image, target],
        ),
        (
            _("docker push (upload image to target registry)"),
            ["docker", "push", target],
        ),
    ]
    if task.cleanup:
        pipeline.append(
            (
                _("docker rmi (cleanup local copies)"),
                ["docker", "rmi", task.source_image, target],
            )
        )
    pipeline.append(
        (
            _("docker logout (clear stored credentials)"),
            ["docker", "logout", task.registry.url],
        )
    )
    return pipeline


def _display_command(task: CopyTask) -> str:
    """为 UI 拼接展示用的命令字符串（密码已掩码）。

    始终基于 task 的结构化字段重新生成 pipeline，不读 DB 也不缓存。
    任务还没启动时返回 "(not yet generated)"。
    """
    if task.status == "pending":
        return _("(not yet generated)")
    if task.registry is None:
        return _("(registry was deleted, cannot rebuild command)")
    lines: list[str] = []
    for label, args in _build_pipeline(task):
        lines.append(f"# {label}")
        lines.append(_mask_command_str(args))
    return "\n".join(lines)


# 这些 token 后面接的下一个参数是凭证，保存到 DB / 展示时必须掩盖
# - `--dest-creds` / `--src-creds` / `--creds`  → skopeo（已废弃但留作兜底）
# - `-p` / `--password`                         → docker login
_CRED_FLAGS = {"--dest-creds", "--src-creds", "--creds", "-p", "--password"}
_MASK = "********"


def _mask_command_str(cmd_list: list[str]) -> str:
    """把命令行 list 转成展示用字符串；遇到 --creds 之类的 flag，下一参数里的
    user:password 形式仅把 password 部分替换为 ********，用户名保留。

    子进程实际调用仍用原始 list（含真实密码），这只影响存到 DB 的 command 字段。
    """
    out: list[str] = []
    skip_next = False
    for token in cmd_list:
        if skip_next:
            if ":" in token:
                user, _, _ = token.partition(":")
                out.append(f"{user}:{_MASK}")
            else:
                out.append(_MASK)
            skip_next = False
            continue
        if token in _CRED_FLAGS:
            out.append(token)
            skip_next = True
        else:
            out.append(token)
    return " ".join(out)


_LOG_CREDS_RE = re.compile(r"(https?://[^/\s:@]+):[^@\s]+@")


def _mask_log_str(s: str) -> str:
    """掩盖日志里 URL 中嵌入的 user:pass 形式凭证，例如 https://u:p@host → https://u:********@host"""
    if not s:
        return s
    return _LOG_CREDS_RE.sub(rf"\1:{_MASK}@", s)


def _project_dest(dest_image: str, project: str = "") -> str:
    """返回带 project 前缀的目标路径。

    - 自动去除用户输入首部的 `/`
    - 若设置了 project 且 dest_image 尚未以它开头，自动追加
    - 避免重复：用户输入 docker-proxy/nginx 不会再被前缀一次
    """
    dest = dest_image.lstrip("/")
    if not project:
        return dest
    if dest == project or dest.startswith(f"{project}/"):
        return dest
    return f"{project}/{dest}"


# ---------------------------------------------------------------------------
# Registry 浏览 / 删除辅助
# ---------------------------------------------------------------------------


def _docker_or_raise() -> None:
    """所有跟 docker CLI 打交道的路径都需要它。"""
    if shutil.which("docker") is None:
        raise RuntimeError(
            _("docker command not found. Install Docker or set DOCKER_HOST for a remote daemon.")
        )


def list_registry_catalog(registry: Registry) -> list[str]:
    """读取 v2 Registry 的全量 repo 列表（HTTP `/v2/_catalog`，含分页）。

    错误信息要尽量 actionable：401/403 通常是临时密码过期或权限不足，
    不是 app 本身的问题。
    """
    scheme = "https" if registry.verify_tls else "http"
    base = f"{scheme}://{registry.url}/v2/_catalog"
    auth = HTTPBasicAuth(registry.username, registry.get_password())
    repos: list[str] = []
    url: str | None = base
    pages = 0
    while url and pages < 50:  # 上限保护
        pages += 1
        resp = requests.get(
            url,
            auth=auth,
            verify=registry.verify_tls,
            timeout=30,
        )
        if resp.status_code == 404:
            raise RuntimeError(
                _("%(url)s does not have the catalog API enabled (404). Harbor: project settings → allow catalog ('Enable catalog'); some registries simply do not implement this API.", url=registry.url)
            )
        if resp.status_code in (401, 403):
            # 401 = 凭证不被认可；403 = 凭证有效但无权限。
            # 阿里云 ACR 个人版：临时密码 1 小时过期，过期后即 401。
            raise RuntimeError(
                _("Auth failed: HTTP %(code)s @ %(url)s. Most common causes: ① the temporary password has expired (console → access credentials → regenerate, then save on the Registry edit page); ② the current account has no read permission on this Registry namespace.", code=resp.status_code, url=registry.url)
            )
        resp.raise_for_status()
        data = resp.json()
        repos.extend(data.get("repositories", []))
        # Docker Registry v2 风格分页：Link 头里带 ?n=...&last=...
        link = resp.headers.get("Link", "")
        url = _next_link(link)
    return repos


def _next_link(link_header: str) -> str | None:
    """从 Link 头解析 next 链接。"""
    if not link_header:
        return None
    for part in link_header.split(","):
        part = part.strip()
        if part.endswith('rel="next"'):
            url = part.split(";")[0].strip().strip("<>")
            return url
    return None


def list_repo_tags(registry: Registry, repo: str) -> list[str]:
    """调用 Registry v2 HTTP API 列出单个 repo 的所有 tag。

    走 `GET /v2/<repo>/tags/list`，基本认证。和 list_registry_catalog 用同一套
    401/403 错误语义，便于在 UI 上看到一致的提示。
    """
    scheme = "https" if registry.verify_tls else "http"
    base = f"{scheme}://{registry.url}/v2/{repo}/tags/list"
    auth = HTTPBasicAuth(registry.username, registry.get_password())
    resp = requests.get(base, auth=auth, verify=registry.verify_tls, timeout=30)
    if resp.status_code in (401, 403):
        raise RuntimeError(
            _("Auth failed: HTTP %(code)s @ %(url)s. Most common causes: ① the temporary password has expired (console → access credentials → regenerate, then save on the Registry edit page); ② the current account has no read permission on this Registry namespace.", code=resp.status_code, url=registry.url)
        )
    if resp.status_code == 404:
        return []  # repo 不存在或 catalog 还没把它列上来 —— 当作"没 tag"
    resp.raise_for_status()
    try:
        return list(resp.json().get("tags") or [])
    except (ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(_("Cannot parse registry response: %(err)s", err=e))


def delete_image(registry: Registry, repo: str, tag: str) -> tuple[bool, str]:
    """通过 Registry v2 HTTP API 删除单个 repo:tag。返回 (success, output)。

    流程：先 HEAD/GET manifest 拿 digest（Docker-Content-Digest），再 DELETE manifest。
    Docker CLI 的 `docker rmi <remote>` 只删本地视角，删不掉远端；这里直接走
    registry 自身的 REST 端点。
    """
    scheme = "https" if registry.verify_tls else "http"
    auth = HTTPBasicAuth(registry.username, registry.get_password())
    base = f"{scheme}://{registry.url}/v2/{repo}"
    # 多 manifest 媒体类型都列上 —— 不同 registry 默认 media type 不一样
    manifest_headers = {
        "Accept": ", ".join(
            [
                "application/vnd.docker.distribution.manifest.v2+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.oci.image.index.v1+json",
            ]
        ),
    }
    try:
        head = requests.get(
            f"{base}/manifests/{tag}",
            auth=auth,
            verify=registry.verify_tls,
            timeout=30,
            headers=manifest_headers,
        )
    except requests.RequestException as e:
        return False, f"GET manifest failed: {e}"
    if head.status_code in (401, 403):
        return False, f"HTTP {head.status_code} (auth failed)"
    if head.status_code == 404:
        return False, "manifest not found (already gone?)"
    if not head.ok:
        return False, f"GET manifest HTTP {head.status_code}: {head.text.strip()[:200]}"
    digest = head.headers.get("Docker-Content-Digest")
    if not digest:
        return False, "registry did not return Docker-Content-Digest header"
    try:
        delete = requests.delete(
            f"{base}/manifests/{digest}",
            auth=auth,
            verify=registry.verify_tls,
            timeout=60,
        )
    except requests.RequestException as e:
        return False, f"DELETE manifest failed: {e}"
    if delete.status_code in (401, 403):
        return False, f"HTTP {delete.status_code} (auth failed)"
    if delete.status_code == 404:
        return False, "manifest not found (already gone?)"
    if not delete.ok:
        return False, f"DELETE manifest HTTP {delete.status_code}: {delete.text.strip()[:200]}"
    return True, f"deleted {repo}:{tag} (digest {digest[:12]}…)"


def delete_all_tags(registry: Registry, repo: str) -> tuple[int, int, list[str]]:
    """删除某个 repo 的所有 tag。返回 (成功数, 失败数, 错误信息列表)。"""
    tags = list_repo_tags(registry, repo)
    ok = 0
    errors: list[str] = []
    for tag in tags:
        success, output = delete_image(registry, repo, tag)
        if success:
            ok += 1
        else:
            errors.append(f"{repo}:{tag} → {_mask_log_str(output)}")
    return ok, len(tags) - ok, errors


def _flush_task_log(task: CopyTask, log_lines: list[str]) -> None:
    """把内存里的 log_lines 节流落库（含掩码）。"""
    task.log = _mask_log_str("\n".join(log_lines))
    db.session.commit()


def _run_step(task: CopyTask, args: list[str], log_lines: list[str]) -> int:
    """跑一个流水线步骤（拉、推、清），流式把 stdout/stderr 写进 log_lines。

    返回子进程 returncode（OSError 起进程失败时返回 -1）。同步刷新 task.subprocess_pid
    和 heartbeat_at，让 watchdog 能在卡住时 SIGTERM 当前正在跑的子进程。
    """
    # bufsize=0 + os.read：text=True/bufsize=1 是 line-buffered，只在 \n 到达
    # 时 yield —— 而 docker 的进度条 [=>--] 是用 \r 原地刷新的，中间
    # 没 \n，那段窗口里心跳会停。改读原始字节，任意 chunk 都算进度。
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
    except OSError as e:
        log_lines.append(f"Failed to start {args[0] if args else 'process'}: {e}")
        return -1

    task.subprocess_pid = proc.pid
    task.heartbeat_at = datetime.utcnow()
    db.session.commit()

    assert proc.stdout is not None
    stdout_fd = proc.stdout.fileno()
    buf = b""
    last_flush = 0.0
    while True:
        try:
            chunk = os.read(stdout_fd, 4096)
        except OSError:
            # pipe 已被子进程关闭 = EOF
            break
        if not chunk:
            break
        # 任何字节都算子进程还在干活，立刻更新心跳（仅 in-memory，
        # 落库交给下面 1.5s 节流 + ticker 共同保证，避免狂 commit）
        task.heartbeat_at = datetime.utcnow()
        buf += chunk
        while b"\n" in buf:
            nl_idx = buf.index(b"\n")
            line_buf = buf[:nl_idx]
            buf = buf[nl_idx + 1:]
            if line_buf.endswith(b"\r"):
                line_buf = line_buf[:-1]
            cr_idx = line_buf.rfind(b"\r")
            if cr_idx != -1:
                line_buf = line_buf[cr_idx + 1:]
            if line_buf:
                log_lines.append(line_buf.decode("utf-8", errors="replace"))
        # 节流落库：每 5 行 或 每 1.5 秒
        now = time.monotonic()
        if len(log_lines) % 5 == 0 or (now - last_flush) > 1.5:
            _flush_task_log(task, log_lines)
            last_flush = now
    # EOF：把最后一段没 \n 收尾的也写进 log
    if buf:
        cr_idx = buf.rfind(b"\r")
        if cr_idx != -1:
            buf = buf[cr_idx + 1:]
        if buf:
            log_lines.append(buf.decode("utf-8", errors="replace"))
    proc.wait()
    return proc.returncode


def _run_task(task_id: int) -> None:
    with app.app_context():
        task: CopyTask | None = db.session.get(CopyTask, task_id)
        if task is None:
            return

        task.status = "running"
        task.started_at = datetime.utcnow()
        task.heartbeat_at = datetime.utcnow()
        db.session.commit()

        # 预检：必备外部命令
        if shutil.which("docker") is None:
            task.status = "failed"
            task.error = (
                "docker command not found. Install Docker (or set DOCKER_HOST to a "
                "remote daemon) and retry."
            )
            task.finished_at = datetime.utcnow()
            db.session.commit()
            return

        pipeline = _build_pipeline(task)
        # pipeline 里 docker push 那一格的索引 —— 它的成败决定任务最终状态。
        main_step_index = next(
            i for i, (label, _) in enumerate(pipeline) if label.startswith("docker push")
        )

        # ticker：独立于 stdout 的心跳线程。docker pull/push 切完 layer 就沉默
        # 好几分钟传数据，主线程 os.read 卡住没法自己刷心跳 + 没法 commit，
        # 靠 ticker 每 5s commit 一次 heartbeat 到 DB 来保命。
        stop_ticker = threading.Event()
        ticker = threading.Thread(
            target=_heartbeat_ticker, args=(task_id, stop_ticker), daemon=True
        )
        ticker.start()

        log_lines: list[str] = []
        main_rc: int = 0  # 主步骤（docker push）的 returncode
        early_aborted = False
        try:
            for i, (label, args) in enumerate(pipeline):
                log_lines.append(f"=== [{i + 1}/{len(pipeline)}] {label} ===")
                log_lines.append(f"$ {_mask_command_str(args)}")
                _flush_task_log(task, log_lines)

                # 清掉上一个 step 的 PID，watchdog 看到 None 就不会误杀上一个已退出进程
                task.subprocess_pid = None
                db.session.commit()

                rc = _run_step(task, args, log_lines)
                log_lines.append(f"--- exit code: {rc} ---")

                if i == main_step_index:
                    main_rc = rc
                elif i < main_step_index and rc != 0:
                    # 前置步骤（pull）失败 → 后续步骤直接跳过。
                    # rmi 即便跑也是 "No such image" 没意义。
                    log_lines.append("(previous step failed; skipping remaining steps)")
                    early_aborted = True
                    break

            # 看门狗可能已在我们跑期间把状态改了（如心跳超时介入）。
            # 重新从 DB 读一次，避免覆盖外部写入。
            current = db.session.get(CopyTask, task_id)
            if current is None or current.status != "running":
                return

            task.subprocess_pid = None
            task.return_code = main_rc
            task.finished_at = datetime.utcnow()
            if early_aborted:
                # 错误信息已在 _run_step 输出里写明（log_lines 已落库）；
                # 这里只保留简短 summary。
                task.status = "failed"
                if not task.error:
                    task.error = "docker pull failed; cannot proceed to docker push"
            elif main_rc == 0:
                task.status = "success"
            else:
                task.status = "failed"
                task.error = f"docker push exited with code {main_rc}"
        except Exception as e:  # pragma: no cover
            task.status = "failed"
            task.error = f"Execution error: {e}"
            task.finished_at = datetime.utcnow()
        finally:
            stop_ticker.set()
            _flush_task_log(task, log_lines)
            db.session.commit()


def _heartbeat_ticker(task_id: int, stop: threading.Event) -> None:
    """_run_task 期间独立刷 heartbeat 的守护线程。

    主线程用 os.read 阻塞读 stdout 期间没法自己刷心跳；docker push 大镜像
    时切完 layer 就沉默好几分钟传数据，那段窗口里如果只看 stdout 长度就会
    被 watchdog 误判挂起。ticker 每 HEARTBEAT_TICK_SEC 秒把 task.heartbeat_at
    拨到当前时间并 commit 到 DB（自己开 app_context 重新 fetch 任务，避免和
    主线程共享 SQLAlchemy session）。

    stop 事件置位后 ticker 在 0.5s 内退出。任何异常吞掉，不让 ticker 死。
    """
    last_tick = time.monotonic()
    while not stop.wait(0.5):
        now = time.monotonic()
        if now - last_tick >= HEARTBEAT_TICK_SEC:
            try:
                with app.app_context():
                    t = db.session.get(CopyTask, task_id)
                    if t is not None and t.status == "running":
                        t.heartbeat_at = datetime.utcnow()
                        db.session.commit()
            except Exception:
                # SQLite 锁、session 冲突等都吞掉 —— ticker 不能影响主线程
                try:
                    db.session.rollback()
                except Exception:
                    pass
            last_tick = now


def _worker_loop(app_obj: Flask) -> None:
    while True:
        try:
            task_id = task_queue.get()
        except Exception:  # pragma: no cover
            continue
        try:
            _run_task(task_id)
        finally:
            task_queue.task_done()


def _check_hung_tasks() -> None:
    """看门狗核心：找出心跳超时的 running 任务，标记 failed 并 SIGTERM 子进程。

    心跳由 _run_task 在每次 stdout 刷库时刷新（≈1.5s 一次）。
    若 worker 卡住（DB 死锁、docker 子进程僵死但 stdout EOF、worker 线程崩了），
    heartbeat_at 就会停在过去；超时后这条路径负责善后。
    """
    with app.app_context():
        threshold = datetime.utcnow() - timedelta(seconds=HEARTBEAT_TIMEOUT_SEC)
        stale = CopyTask.query.filter(
            CopyTask.status == "running",
            CopyTask.heartbeat_at.isnot(None),
            CopyTask.heartbeat_at < threshold,
        ).all()
        if not stale:
            return
        now = datetime.utcnow()
        for task in stale:
            task.status = "failed"
            task.error = (
                f"Task had no heartbeat for over {HEARTBEAT_TIMEOUT_SEC}s; "
                "worker/docker may have hung, auto-marked as failed"
            )
            task.finished_at = now
            if task.subprocess_pid:
                try:
                    os.kill(task.subprocess_pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    # 子进程已退出 / 不属于本进程，跳过即可
                    pass
        db.session.commit()


def _watchdog_loop() -> None:
    """看门狗线程：周期性扫描并处理心跳超时的任务。

    任何异常都不能让这条线程死掉 —— 否则一次失败后整个机制就废了。
    同时还负责：worker 线程死了就自动拉起 + 把 DB 里的 pending 重新塞回队列。
    """
    while True:
        try:
            _check_hung_tasks()
            # worker 线程崩了但进程没死的情况：拉起，并补回 queue 里可能丢失的 pending
            if _worker_thread is None or not _worker_thread.is_alive():
                _ensure_worker()
                if task_queue.empty():
                    _enqueue_pending_tasks()
        except Exception:  # pragma: no cover
            pass
        time.sleep(WATCHDOG_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# 路由：认证
# ---------------------------------------------------------------------------


@login_manager.user_loader
def load_user(user_id: str):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


def api_login_required(fn):
    """API 鉴权装饰器：未登录返回 401 JSON（不要 302 跳 HTML 登录页）。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    """仅 admin 用户可访问；未登录走 Flask-Login 重定向，非 admin 给出 flash + 302。"""

    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            flash(_("Admin permission required"), "error")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)

    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip()).first()
        if user is None or not user.check_password(form.password.data):
            flash(_("Invalid username or password"), "error")
        else:
            login_user(user)
            return redirect(url_for("dashboard"))
    return render_template("login.html", form=form)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# 路由：账号管理（admin）+ 个人信息
# ---------------------------------------------------------------------------


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    """当前用户改自己的密码。"""
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash(_("Current password is incorrect"), "error")
        else:
            current_user.set_password(form.new_password.data)
            db.session.commit()
            flash(_("Password updated"), "success")
            return redirect(url_for("account"))
    return render_template("account.html", form=form)


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    """管理员：列账号 + 新建账号。"""
    form = AdminCreateUserForm()
    if form.validate_on_submit():
        username = form.username.data.strip()
        if User.query.filter_by(username=username).first():
            flash(_("User already exists"), "error")
        else:
            u = User(username=username, is_admin=form.is_admin.data)
            u.set_password(form.password.data)
            db.session.add(u)
            db.session.commit()
            flash(_("User %(name)s created", name=username), "success")
            return redirect(url_for("admin_users"))
    users = User.query.order_by(User.created_at.asc()).all()
    admin_count = User.query.filter_by(is_admin=True).count()
    return render_template(
        "admin_users.html",
        form=form,
        users=users,
        admin_count=admin_count,
    )


@app.route("/admin/users/<int:uid>/reset", methods=["GET", "POST"])
@admin_required
def admin_user_reset(uid: int):
    target = db.session.get(User, uid)
    if target is None:
        flash(_("User does not exist"), "error")
        return redirect(url_for("admin_users"))
    form = AdminResetPasswordForm()
    if form.validate_on_submit():
        target.set_password(form.new_password.data)
        db.session.commit()
        flash(_("Password for %(name)s has been reset", name=target.username), "success")
        return redirect(url_for("admin_users"))
    return render_template("admin_user_reset.html", form=form, target=target)


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_user_delete(uid: int):
    target = db.session.get(User, uid)
    if target is None:
        flash(_("User does not exist"), "error")
    elif target.id == current_user.id:
        flash(_("Cannot delete the currently logged-in account"), "error")
    elif target.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
        flash(_("Cannot delete the last admin"), "error")
    else:
        # 先数一下该用户的任务数（cascade 在 commit 时会自动删，但提示用户更友好）
        task_count = CopyTask.query.filter_by(user_id=target.id).count()
        username = target.username
        db.session.delete(target)  # cascade="all, delete-orphan" 会带删 CopyTask
        db.session.commit()
        suffix = _(" (including %(count)s task records)", count=task_count) if task_count else ""
        flash(_("User %(name)s deleted%(suffix)s", name=username, suffix=suffix), "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/toggle-admin", methods=["POST"])
@admin_required
def admin_user_toggle_admin(uid: int):
    target = db.session.get(User, uid)
    if target is None:
        flash(_("User does not exist"), "error")
    elif target.id == current_user.id:
        flash(_("Cannot change your own admin status"), "error")
    elif target.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
        flash(_("Cannot revoke the last admin"), "error")
    else:
        target.is_admin = not target.is_admin
        db.session.commit()
        state = _("Admin") if target.is_admin else _("Regular user")
        flash(_("%(name)s is now %(role)s", name=target.username, role=state), "success")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------------------
# 路由：Registry 管理
# ---------------------------------------------------------------------------


@app.route("/registries", methods=["GET"])
@login_required
def registries_list():
    items = Registry.query.order_by(Registry.created_at.desc()).all()
    return render_template("registries.html", items=items)


@app.route("/registries/new", methods=["GET", "POST"])
@login_required
def registries_create():
    form = RegistryForm()
    if request.method == "GET":
        form.project.data = DEFAULT_PROJECT
    if form.validate_on_submit():
        if not (form.password.data or "").strip():
            flash(_("Password cannot be empty when creating a Registry"), "error")
            return render_template("registry_form.html", form=form, mode="new")
        reg = Registry(
            name=form.name.data.strip(),
            url=form.url.data.strip().replace("https://", "").replace("http://", "").rstrip("/"),
            username=form.username.data.strip(),
            verify_tls=form.verify_tls.data,
            project=(form.project.data or "").strip().strip("/"),
        )
        reg.set_password(form.password.data)
        db.session.add(reg)
        db.session.commit()
        flash(_("Registry saved"), "success")
        return redirect(url_for("registries_list"))
    return render_template("registry_form.html", form=form, mode="new")


@app.route("/registries/<int:rid>/edit", methods=["GET", "POST"])
@login_required
def registries_edit(rid: int):
    reg: Registry | None = db.session.get(Registry, rid)
    if reg is None:
        flash(_("Registry does not exist"), "error")
        return redirect(url_for("registries_list"))
    form = RegistryForm()
    if request.method == "GET":
        form.name.data = reg.name
        form.url.data = reg.url
        form.username.data = reg.username
        form.verify_tls.data = reg.verify_tls
        form.project.data = reg.project
        form.password.data = ""  # never echoed back
    if form.validate_on_submit():
        reg.name = form.name.data.strip()
        reg.url = (
            form.url.data.strip()
            .replace("https://", "")
            .replace("http://", "")
            .rstrip("/")
        )
        reg.username = form.username.data.strip()
        reg.verify_tls = form.verify_tls.data
        reg.project = (form.project.data or "").strip().strip("/")
        if form.password.data:
            reg.set_password(form.password.data)
        db.session.commit()
        flash(_("Updated"), "success")
        return redirect(url_for("registries_list"))
    return render_template("registry_form.html", form=form, mode="edit", registry=reg)


@app.route("/registries/<int:rid>/delete", methods=["POST"])
@login_required
def registries_delete(rid: int):
    reg = db.session.get(Registry, rid)
    if reg is None:
        flash(_("Registry does not exist"), "error")
    else:
        if reg.tasks:
            flash(_("Registry still has task records and cannot be deleted"), "error")
        else:
            db.session.delete(reg)
            db.session.commit()
            flash(_("Deleted"), "success")
    return redirect(url_for("registries_list"))


# ---------------------------------------------------------------------------
# 路由：Registry catalog 浏览 / 镜像删除
# ---------------------------------------------------------------------------


@app.route("/registries/<int:rid>/catalog")
@login_required
def registry_catalog(rid: int):
    reg = db.session.get(Registry, rid)
    if reg is None:
        flash(_("Registry does not exist"), "error")
        return redirect(url_for("registries_list"))

    repos_with_tags: list[tuple[str, list[str] | None, str | None]] = []
    error: str | None = None
    try:
        repos = list_registry_catalog(reg)
        # 只读顶层 + tag 总数；tag 列表按需展开
        for repo in repos:
            tags = list_repo_tags(reg, repo)
            repos_with_tags.append((repo, tags, None))
    except Exception as e:
        error = str(e)

    return render_template(
        "catalog.html",
        registry=reg,
        repos=repos_with_tags,
        error=error,
        total_repos=len(repos_with_tags),
    )


@app.route("/registries/<int:rid>/images/delete", methods=["POST"])
@login_required
def registry_image_delete(rid: int):
    reg = db.session.get(Registry, rid)
    if reg is None:
        flash(_("Registry does not exist"), "error")
        return redirect(url_for("registries_list"))

    repo = (request.form.get("repo") or "").strip().lstrip("/")
    tag = (request.form.get("tag") or "").strip()
    delete_repo = request.form.get("scope") == "repo"

    if not repo:
        flash(_("Missing repo name"), "error")
        return redirect(url_for("registry_catalog", rid=rid))

    try:
        if delete_repo:
            ok, fail, errors = delete_all_tags(reg, repo)
            if fail == 0 and ok > 0:
                flash(_("Deleted %(ok)s tags under repository %(repo)s", ok=ok, repo=repo), "success")
            elif ok == 0 and fail == 0:
                flash(_("Repository %(repo)s has no tags", repo=repo), "info")
            else:
                errs = _(" Errors: %(errs)s", errs="; ".join(errors[:3])) if errors else ""
                flash(
                    _("Repository %(repo)s: %(ok)s ok, %(fail)s failed.%(errs)s", repo=repo, ok=ok, fail=fail, errs=errs),
                    "error",
                )
        else:
            if not tag:
                flash(_("Missing tag"), "error")
                return redirect(url_for("registry_catalog", rid=rid))
            success, output = delete_image(reg, repo, tag)
            if success:
                flash(_("Deleted %(repo)s:%(tag)s", repo=repo, tag=tag), "success")
            else:
                flash(_("Failed to delete %(repo)s:%(tag)s: %(output)s", repo=repo, tag=tag, output=output), "error")
    except Exception as e:
        flash(str(e), "error")

    return redirect(url_for("registry_catalog", rid=rid))


# ---------------------------------------------------------------------------
# 路由：镜像拷贝
# ---------------------------------------------------------------------------


@app.route("/")
@login_required
def dashboard():
    form = CopyForm()
    registries = Registry.query.order_by(Registry.name).all()
    recent = (
        CopyTask.query.filter_by(user_id=current_user.id)
        .order_by(CopyTask.created_at.desc())
        .limit(10)
        .all()
    )
    docker_ok = shutil.which("docker") is not None
    return render_template(
        "dashboard.html",
        form=form,
        registries=registries,
        tasks=recent,
        docker_ok=docker_ok,
    )


@app.route("/copy", methods=["POST"])
@login_required
def copy_create():
    form = CopyForm()
    registries = {r.id: r for r in Registry.query.all()}
    try:
        registry_id = int(request.form.get("registry_id", "0"))
    except ValueError:
        registry_id = 0
    reg = registries.get(registry_id)
    if reg is None:
        flash(_("Please select a valid target Registry"), "error")
        return redirect(url_for("dashboard"))

    source = (request.form.get("source_image") or "").strip()
    dest = (request.form.get("dest_image") or "").strip()
    multi_arch = (request.form.get("multi_arch") or "1") == "1"
    # HTML checkbox 不勾时浏览器根本不会提交字段，所以缺失即代表 False。
    # cleanup=False → 不在尾部 rmi，保留用户本地镜像。
    cleanup = request.form.get("cleanup") in ("1", "true", "on")
    if not source or not dest:
        flash(_("Source and destination images cannot be empty"), "error")
        return redirect(url_for("dashboard"))
    # --retry-times：表单给的就是整数串；脏值兜底为默认 3，再夹到 [0,10]。
    # 直接读 request.form 而不走 WTForms 校验，因为这个表单字段不是 submit 必备的
    # （dashboard 上始终渲染一个 number input，用户填了才传），先宽松再夹紧。
    try:
        retry_times = int(request.form.get("retry_times", "3"))
    except (TypeError, ValueError):
        retry_times = 3
    retry_times = max(0, min(10, retry_times))

    task = CopyTask(
        user_id=current_user.id,
        registry_id=reg.id,
        source_image=source,
        dest_image=dest.lstrip("/"),
        multi_arch=multi_arch,
        cleanup=cleanup,
        retry_times=retry_times,
        status="pending",
    )
    db.session.add(task)
    db.session.commit()

    _ensure_worker()
    task_queue.put(task.id)
    flash(_("Task #%(id)s queued", id=task.id), "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>")
@login_required
def task_detail(task_id: int):
    task = db.session.get(CopyTask, task_id)
    if task is None or task.user_id != current_user.id:
        flash(_("Task does not exist"), "error")
        return redirect(url_for("dashboard"))
    response = make_response(render_template(
        "task.html",
        task=task,
        display_command=_display_command(task),
    ))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.route("/tasks/<int:task_id>/retry", methods=["POST"])
@login_required
def task_retry(task_id: int):
    task = db.session.get(CopyTask, task_id)
    if task is None or task.user_id != current_user.id:
        flash(_("Task does not exist"), "error")
        return redirect(url_for("dashboard"))
    if task.status in ("pending", "running"):
        flash(_("Task is queued or running, no retry needed"), "error")
        return redirect(url_for("task_detail", task_id=task.id))
    if task.status not in ("success", "failed"):
        flash(_("Current status does not allow retry"), "error")
        return redirect(url_for("task_detail", task_id=task.id))

    # 重置后重新入队；保留 created_at 以便追溯首次提交时间
    # 命令不在 DB 中——worker 会从结构化字段重新 _build_command()
    task.status = "pending"
    task.log = ""
    task.error = ""
    task.return_code = None
    task.started_at = None
    task.finished_at = None
    db.session.commit()

    _ensure_worker()
    task_queue.put(task.id)
    flash(_("Task #%(id)s re-queued", id=task.id), "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
@login_required
def task_delete(task_id: int):
    task = db.session.get(CopyTask, task_id)
    if task is None or task.user_id != current_user.id:
        flash(_("Task does not exist"), "error")
        return redirect(url_for("dashboard"))
    if task.status in ("pending", "running"):
        flash(_("Task is queued or running, cannot be deleted"), "error")
        return redirect(url_for("task_detail", task_id=task.id))

    db.session.delete(task)
    db.session.commit()
    flash(_("Task #%(id)s deleted", id=task_id), "success")

    # 任务已删，回详情页会触发「任务不存在」——统一回 dashboard
    return redirect(url_for("dashboard"))


@app.route("/api/tasks/<int:task_id>")
@api_login_required
def task_status(task_id: int):
    task = db.session.get(CopyTask, task_id)
    if task is None or task.user_id != current_user.id:
        resp = jsonify({"error": "not found"})
        resp.headers["Cache-Control"] = "no-store"
        return resp, 404
    resp = jsonify(
        {
            "id": task.id,
            "status": task.status,
            "log": task.log,
            "error": task.error,
            "return_code": task.return_code,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
            "command": _display_command(task),
        }
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/tasks/recent")
@api_login_required
def tasks_recent():
    """供仪表盘轮询：返回当前用户最近的任务列表（按状态变化驱动 DOM 更新）。"""
    try:
        limit = max(1, min(int(request.args.get("limit", 10)), 50))
    except ValueError:
        limit = 10
    tasks = (
        CopyTask.query.filter_by(user_id=current_user.id)
        .order_by(CopyTask.created_at.desc())
        .limit(limit)
        .all()
    )
    resp = make_response(
        jsonify(
            {
                "tasks": [
                    {
                        "id": t.id,
                        "status": t.status,
                        "source_image": t.source_image,
                        "dest_image": t.dest_image,
                        "registry_url": t.registry.url,
                        "registry_project": t.registry.project,
                        "project_dest": _project_dest(t.dest_image, t.registry.project),
                        "cleanup": t.cleanup,
                        "created_at": t.created_at.isoformat() if t.created_at else None,
                        "started_at": t.started_at.isoformat() if t.started_at else None,
                        "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                    }
                    for t in tasks
                ]
            }
        )
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------


@app.context_processor
def inject_globals():
    return {
        "current_year": datetime.utcnow().year,
        "default_project": DEFAULT_PROJECT,
        "project_dest": _project_dest,
        "_": _,
        "get_locale": get_locale,
        "supported_locales": SUPPORTED_LOCALES,
    }


@app.before_request
def _set_locale() -> None:
    """Populate g.locale from ?lang=, cookie, or default."""
    g.locale = resolve_locale_from_request()


@app.after_request
def _persist_locale(response):
    """If ?lang= overrode the locale, persist via cookie so subsequent requests stick."""
    qp = request.args.get("lang", "").strip().lower() if request else ""
    if qp in SUPPORTED_LOCALES:
        existing = request.cookies.get(LOCALE_COOKIE, "")
        if existing != qp:
            response.set_cookie(
                LOCALE_COOKIE,
                qp,
                max_age=LOCALE_COOKIE_MAX_AGE,
                samesite="Lax",
                httponly=False,
            )
    return response


@app.route("/lang/<code>")
def set_lang(code: str):
    """Explicit language switch endpoint. Sets cookie and redirects back."""
    code = (code or "").strip().lower()
    nxt = request.args.get("next") or request.referrer or url_for("dashboard")
    # 防 open-redirect：只允许相对路径或同源 path
    if not nxt.startswith("/"):
        nxt = url_for("dashboard")
    resp = redirect(nxt)
    if code in SUPPORTED_LOCALES:
        resp.set_cookie(
            LOCALE_COOKIE,
            code,
            max_age=LOCALE_COOKIE_MAX_AGE,
            samesite="Lax",
            httponly=False,
        )
    return resp


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message=_("Page not found")), 404


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------


def init_db() -> None:
    # 给已存在的 SQLite 表补上新列（create_all 不会动已有表）
    _migrate_columns()
    with app.app_context():
        db.create_all()
        # 私有部署：首次启动时建一个默认账号 admin / admin123
        # 若 admin 已存在则跳过（idempotent），但确保其 is_admin=True
        admin = User.query.filter_by(username="admin").first()
        if admin is None:
            admin = User(username="admin", is_admin=True)
            admin.set_password("admin123")
            db.session.add(admin)
        elif not admin.is_admin:
            admin.is_admin = True
        db.session.commit()
    # 清理上一次会话遗留的 running / pending 任务 —— 重启即清场，
    # 想重试由用户自己在 dashboard 上点。
    _recover_unfinished_tasks()


init_db()


if __name__ == "__main__":
    _ensure_worker()
    # 监听参数全部走环境变量，方便本地 / 容器 / 反代前调试时切换。
    #  - LISTEN_HOST：默认 0.0.0.0（监听所有网卡；容器/反代场景想要「只本机」可设 127.0.0.1）
    #  - LISTEN_PORT：默认 5000；与 docker-compose.yml 的 ports 段、healthcheck 一致
    #  - FLASK_DEBUG：0/1；本机调试可设 1，但生产别开（debugger 可执行任意代码）
    host = os.environ.get("LISTEN_HOST", "127.0.0.1")
    port = int(os.environ.get("LISTEN_PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug, threaded=True)
