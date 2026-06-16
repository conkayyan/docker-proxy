"""docker-proxy — Flask UI for running `skopeo copy` against a target registry.

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
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, EqualTo, Length, Optional
from werkzeug.security import check_password_hash, generate_password_hash


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
    dest_image = db.Column(db.String(300), nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)
    log = db.Column(db.Text, default="", nullable=False)
    error = db.Column(db.Text, default="", nullable=False)
    return_code = db.Column(db.Integer)
    multi_arch = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    # 看门狗用：worker 每次落库都刷新 heartbeat_at；watchdog 据此判断 worker/skopeo
    # 是否已挂。subprocess_pid 记录当前 skopeo 子进程的 PID，挂起时 watchdog 可以 SIGTERM。
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
    username = StringField("用户名", validators=[DataRequired(), Length(1, 80)])
    password = PasswordField("密码", validators=[DataRequired()])
    submit = SubmitField("登录")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("当前密码", validators=[DataRequired()])
    new_password = PasswordField(
        "新密码", validators=[DataRequired(), Length(min=6, max=128)]
    )
    confirm = PasswordField(
        "确认新密码",
        validators=[DataRequired(), EqualTo("new_password", message="两次输入不一致")],
    )
    submit = SubmitField("修改密码")


class AdminCreateUserForm(FlaskForm):
    username = StringField("用户名", validators=[DataRequired(), Length(3, 80)])
    password = PasswordField(
        "密码", validators=[DataRequired(), Length(min=6, max=128)]
    )
    confirm = PasswordField(
        "确认密码",
        validators=[DataRequired(), EqualTo("password", message="两次输入不一致")],
    )
    is_admin = BooleanField("授予管理员权限")
    submit = SubmitField("创建账号")


class AdminResetPasswordForm(FlaskForm):
    new_password = PasswordField(
        "新密码", validators=[DataRequired(), Length(min=6, max=128)]
    )
    confirm = PasswordField(
        "确认密码",
        validators=[DataRequired(), EqualTo("new_password", message="两次输入不一致")],
    )
    submit = SubmitField("重置密码")


class RegistryForm(FlaskForm):
    name = StringField("名称", validators=[DataRequired(), Length(1, 80)])
    url = StringField(
        "Registry 地址",
        validators=[DataRequired(), Length(3, 200)],
        description="例如 harbor.company.local",
    )
    username = StringField("用户名", validators=[DataRequired(), Length(1, 80)])
    # 编辑模式下留空 = 不修改密码（路由层判断）。新建模式下必填（路由层校验）。
    password = PasswordField("密码", validators=[Optional(), Length(max=1024)])
    # 校验 TLS 证书：默认勾选 = 安全默认；自签证书 / 内网环境可取消勾选。
    verify_tls = BooleanField(
        "校验 TLS 证书（推荐勾选；自签证书 / 内网环境请取消）",
        default=True,
    )
    project = StringField(
        "项目路径前缀",
        validators=[Length(0, 120)],
        default=DEFAULT_PROJECT,
        description="推送时自动加在目标镜像前。例如 docker-proxy → 实际推送为 docker-proxy/nginx:1.27；留空则不追加。",
    )
    submit = SubmitField("保存")


class CopyForm(FlaskForm):
    registry_id = StringField("目标 Registry", validators=[DataRequired()])
    source_image = StringField(
        "源镜像",
        validators=[DataRequired()],
        description="例如 docker.io/library/nginx:1.27（Docker Hub 官方镜像的完整路径）",
    )
    dest_image = StringField(
        "目标镜像",
        validators=[DataRequired()],
        description="例如 nginx:1.27（仅 image:tag，project 由所选 Registry 自动追加）",
    )
    multi_arch = BooleanField(
        "多架构 (--multi-arch all)",
        description="推送整张 manifest list（amd64 / arm64 / armv7 等）。在 Mac ARM 上推 nginx 等多架构镜像时建议勾选。",
    )
    submit = SubmitField("开始拷贝")


# ---------------------------------------------------------------------------
# 任务队列 / 后台 worker
# ---------------------------------------------------------------------------


task_queue: "Queue[int]" = Queue()
_worker_started = False
_worker_lock = threading.Lock()

# 看门狗：worker 必须至少每 HEARTBEAT_TIMEOUT 秒刷新一次 heartbeat_at，
# 否则 watchdog 会把对应的 running 任务标为 failed 并 SIGTERM 子进程。
# 默认 30s 心跳超时、10s 巡检一次；可通过环境变量调。
WATCHDOG_INTERVAL_SEC = int(os.environ.get("TASK_WATCHDOG_INTERVAL", "10"))
HEARTBEAT_TIMEOUT_SEC = int(os.environ.get("TASK_HEARTBEAT_TIMEOUT", "30"))


def _ensure_worker() -> None:
    """惰性启动后台 worker + 看门狗线程（每次进程一个）。"""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker_loop, args=(app,), daemon=True).start()
        threading.Thread(target=_watchdog_loop, daemon=True).start()
        _worker_started = True


def _migrate_columns() -> None:
    """给已有 SQLite 表加新列。db.create_all() 不会动已存在的表。

    每条 ALTER 都包在 try/except 里：列已存在时 SQLite 抛 OperationalError，
    直接吞掉，保证幂等。
    """
    stmts = [
        "ALTER TABLE copy_tasks ADD COLUMN heartbeat_at DATETIME",
        "ALTER TABLE copy_tasks ADD COLUMN subprocess_pid INTEGER",
    ]
    with app.app_context():
        for sql in stmts:
            try:
                db.session.execute(text(sql))
                db.session.commit()
            except Exception:
                db.session.rollback()


def _recover_orphan_running_tasks() -> None:
    """服务启动时清理上一次会话遗留的 running 任务。

    进程被 kill -9 / OOM / docker compose restart 时，DB 里的状态不会跟着清。
    在内存里的 task_queue 早已丢失这些 id，新 worker 不会再去拉它们，
    所以必须主动把它们标为 failed，否则 dashboard 上会一直显示 running。
    """
    with app.app_context():
        orphans = CopyTask.query.filter(CopyTask.status == "running").all()
        if not orphans:
            return
        now = datetime.utcnow()
        for task in orphans:
            task.status = "failed"
            task.error = "服务重启时检测到上一次会话未完成的任务，已自动标记为失败"
            task.finished_at = now
        db.session.commit()


def _build_command(task: CopyTask) -> list[str]:
    cmd: list[str] = ["skopeo", "copy"]
    if task.multi_arch:
        cmd.append("--multi-arch=all")
    if not task.registry.verify_tls:
        cmd.append("--dest-tls-verify=false")
    cmd.append(f"docker://{task.source_image}")
    cmd.append(f"docker://{task.registry.url}/{_project_dest(task.dest_image, task.registry.project)}")
    cmd.extend(["--dest-creds", f"{task.registry.username}:{task.registry.get_password()}"])
    return cmd


def _display_command(task: CopyTask) -> str:
    """为 UI 拼接展示用的命令字符串（密码已掩码）。

    始终基于 task 的结构化字段（source_image / dest_image / multi_arch / registry）
    重新生成，不读 DB 也不缓存。任务还没启动时返回 "(尚未生成)"。
    """
    if task.status == "pending":
        return "(尚未生成)"
    if task.registry is None:
        return "(registry 已被删除，无法重建命令)"
    return _mask_command_str(_build_command(task))


# 这些 token 后面接的下一个参数是凭证，保存到 DB / 展示时必须掩盖
_CRED_FLAGS = {"--dest-creds", "--src-creds", "--creds"}
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


def _skopeo_or_raise() -> None:
    if shutil.which("skopeo") is None:
        raise RuntimeError("skopeo 命令未找到。请先安装：brew install skopeo")


def _tls_flag(verify_tls: bool) -> list[str]:
    """不校验 TLS 时追加 --tls-verify=false。校验时返回空（skopeo 默认即 verify）。"""
    return [] if verify_tls else ["--tls-verify=false"]


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
                f"{registry.url} 未启用 catalog API（404）。"
                "Harbor：项目设置 → 允许清单（'Enable catalog'）；"
                "部分 Registry 干脆没实现此接口。"
            )
        if resp.status_code in (401, 403):
            # 401 = 凭证不被认可；403 = 凭证有效但无权限。
            # 阿里云 ACR 个人版：临时密码 1 小时过期，过期后即 401。
            raise RuntimeError(
                f"鉴权失败：HTTP {resp.status_code} @ {registry.url}。"
                "最常见原因：① 临时密码已过期（控制台 → 访问凭证 → "
                "重新生成临时密码，再在 Registry 编辑页保存）；"
                "② 当前账号对该 Registry 命名空间缺少读权限。"
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
    """调用 `skopeo list-tags` 列出单个 repo 的所有 tag。"""
    _skopeo_or_raise()
    cmd = [
        "skopeo",
        *_tls_flag(registry.verify_tls),
        "--creds",
        f"{registry.username}:{registry.get_password()}",
        "list-tags",
        f"docker://{registry.url}/{repo}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(
            f"list-tags 失败：{proc.stderr.strip() or proc.stdout.strip()}"
        )
    try:
        return json.loads(proc.stdout).get("Tags", []) or []
    except json.JSONDecodeError as e:
        raise RuntimeError(f"无法解析 skopeo 输出：{e}")


def delete_image(registry: Registry, repo: str, tag: str) -> tuple[bool, str]:
    """通过 `skopeo delete` 删除单个 repo:tag。返回 (success, output)。"""
    _skopeo_or_raise()
    target = f"docker://{registry.url}/{repo}:{tag}"
    cmd = [
        "skopeo",
        *_tls_flag(registry.verify_tls),
        "--creds",
        f"{registry.username}:{registry.get_password()}",
        "delete",
        target,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output


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


def _run_task(task_id: int) -> None:
    with app.app_context():
        task: CopyTask | None = db.session.get(CopyTask, task_id)
        if task is None:
            return

        task.status = "running"
        task.started_at = datetime.utcnow()
        task.heartbeat_at = datetime.utcnow()
        db.session.commit()

        cmd = _build_command(task)
        # 子进程实际调用用原始 cmd（包含真实密码，仅在内存中传给 skopeo）。
        # 不再存 task.command 到 DB；UI 上要展示时由 _display_command() 重新拼接 + 掩码。

        if shutil.which("skopeo") is None:
            task.status = "failed"
            task.error = "skopeo 命令未找到。请先安装：brew install skopeo"
            task.finished_at = datetime.utcnow()
            db.session.commit()
            return

        log_lines: list[str] = []
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            # 记录子进程 PID，给 watchdog 用于 SIGTERM；同步刷新一次心跳
            task.subprocess_pid = proc.pid
            task.heartbeat_at = datetime.utcnow()
            db.session.commit()
        except OSError as e:
            task.status = "failed"
            task.error = f"启动 skopeo 失败：{e}"
            task.finished_at = datetime.utcnow()
            db.session.commit()
            return

        try:
            assert proc.stdout is not None
            last_flush = 0.0
            for line in proc.stdout:
                log_lines.append(line.rstrip())
                # 每 5 行 或 每 1.5 秒（取较快者）落库一次，让前端轮询能及时看到
                now = time.monotonic()
                if len(log_lines) % 5 == 0 or (now - last_flush) > 1.5:
                    task.log = _mask_log_str("\n".join(log_lines))
                    task.heartbeat_at = datetime.utcnow()
                    db.session.commit()
                    last_flush = now
            proc.wait()
            task.log = _mask_log_str("\n".join(log_lines))
            task.finished_at = datetime.utcnow()

            # 看门狗可能已在我们 wait 期间把状态改成 failed / success（如有其他
            # 进程接手、或者 watchdog 因心跳超时介入）。重新从 DB 读一次，避免
            # 覆盖外部写入。
            current = db.session.get(CopyTask, task_id)
            if current is None or current.status != "running":
                return

            task.return_code = proc.returncode
            if proc.returncode == 0:
                task.status = "success"
            else:
                task.status = "failed"
                task.error = f"skopeo 退出码 {proc.returncode}"
        except Exception as e:  # pragma: no cover
            task.status = "failed"
            task.error = f"执行异常：{e}"
            task.finished_at = datetime.utcnow()
        finally:
            db.session.commit()


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
    若 worker 卡住（DB 死锁、skopeo 子进程僵死但 stdout EOF、worker 线程崩了），
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
                f"任务超过 {HEARTBEAT_TIMEOUT_SEC}s 未上报心跳，"
                "worker/skopeo 可能已挂起，已自动标记为失败"
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
    """
    while True:
        try:
            _check_hung_tasks()
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
            flash("需要管理员权限", "error")
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
            flash("用户名或密码错误", "error")
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
            flash("当前密码错误", "error")
        else:
            current_user.set_password(form.new_password.data)
            db.session.commit()
            flash("密码已更新", "success")
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
            flash("用户已存在", "error")
        else:
            u = User(username=username, is_admin=form.is_admin.data)
            u.set_password(form.password.data)
            db.session.add(u)
            db.session.commit()
            flash(f"已创建用户 {username}", "success")
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
        flash("用户不存在", "error")
        return redirect(url_for("admin_users"))
    form = AdminResetPasswordForm()
    if form.validate_on_submit():
        target.set_password(form.new_password.data)
        db.session.commit()
        flash(f"已重置 {target.username} 的密码", "success")
        return redirect(url_for("admin_users"))
    return render_template("admin_user_reset.html", form=form, target=target)


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_user_delete(uid: int):
    target = db.session.get(User, uid)
    if target is None:
        flash("用户不存在", "error")
    elif target.id == current_user.id:
        flash("不能删除当前登录的账号", "error")
    elif target.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
        flash("不能删除最后一个管理员", "error")
    else:
        # 先数一下该用户的任务数（cascade 在 commit 时会自动删，但提示用户更友好）
        task_count = CopyTask.query.filter_by(user_id=target.id).count()
        username = target.username
        db.session.delete(target)  # cascade="all, delete-orphan" 会带删 CopyTask
        db.session.commit()
        suffix = f"（含 {task_count} 个任务记录）" if task_count else ""
        flash(f"已删除用户 {username}{suffix}", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/toggle-admin", methods=["POST"])
@admin_required
def admin_user_toggle_admin(uid: int):
    target = db.session.get(User, uid)
    if target is None:
        flash("用户不存在", "error")
    elif target.id == current_user.id:
        flash("不能修改自己的管理员身份", "error")
    elif target.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
        flash("不能取消最后一个管理员", "error")
    else:
        target.is_admin = not target.is_admin
        db.session.commit()
        state = "管理员" if target.is_admin else "普通用户"
        flash(f"{target.username} 已设为 {state}", "success")
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
            flash("新建 Registry 时密码不能为空", "error")
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
        flash("Registry 已保存", "success")
        return redirect(url_for("registries_list"))
    return render_template("registry_form.html", form=form, mode="new")


@app.route("/registries/<int:rid>/edit", methods=["GET", "POST"])
@login_required
def registries_edit(rid: int):
    reg: Registry | None = db.session.get(Registry, rid)
    if reg is None:
        flash("Registry 不存在", "error")
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
        flash("已更新", "success")
        return redirect(url_for("registries_list"))
    return render_template("registry_form.html", form=form, mode="edit", registry=reg)


@app.route("/registries/<int:rid>/delete", methods=["POST"])
@login_required
def registries_delete(rid: int):
    reg = db.session.get(Registry, rid)
    if reg is None:
        flash("Registry 不存在", "error")
    else:
        if reg.tasks:
            flash("该 Registry 仍有任务记录，无法删除", "error")
        else:
            db.session.delete(reg)
            db.session.commit()
            flash("已删除", "success")
    return redirect(url_for("registries_list"))


# ---------------------------------------------------------------------------
# 路由：Registry catalog 浏览 / 镜像删除
# ---------------------------------------------------------------------------


@app.route("/registries/<int:rid>/catalog")
@login_required
def registry_catalog(rid: int):
    reg = db.session.get(Registry, rid)
    if reg is None:
        flash("Registry 不存在", "error")
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
        flash("Registry 不存在", "error")
        return redirect(url_for("registries_list"))

    repo = (request.form.get("repo") or "").strip().lstrip("/")
    tag = (request.form.get("tag") or "").strip()
    delete_repo = request.form.get("scope") == "repo"

    if not repo:
        flash("缺少 repo 名称", "error")
        return redirect(url_for("registry_catalog", rid=rid))

    try:
        if delete_repo:
            ok, fail, errors = delete_all_tags(reg, repo)
            if fail == 0 and ok > 0:
                flash(f"已删除仓库 {repo} 下的 {ok} 个 tag", "success")
            elif ok == 0 and fail == 0:
                flash(f"仓库 {repo} 没有任何 tag", "info")
            else:
                flash(
                    f"仓库 {repo}：成功 {ok}，失败 {fail}。"
                    + (" 错误：" + "; ".join(errors[:3]) if errors else ""),
                    "error",
                )
        else:
            if not tag:
                flash("缺少 tag", "error")
                return redirect(url_for("registry_catalog", rid=rid))
            success, output = delete_image(reg, repo, tag)
            if success:
                flash(f"已删除 {repo}:{tag}", "success")
            else:
                flash(f"删除 {repo}:{tag} 失败：{output}", "error")
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
    skopeo_ok = shutil.which("skopeo") is not None
    return render_template(
        "dashboard.html",
        form=form,
        registries=registries,
        tasks=recent,
        skopeo_ok=skopeo_ok,
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
        flash("请选择一个有效的目标 Registry", "error")
        return redirect(url_for("dashboard"))

    source = (request.form.get("source_image") or "").strip()
    dest = (request.form.get("dest_image") or "").strip()
    multi_arch = request.form.get("multi_arch") in ("1", "on", "true", "yes")
    if not source or not dest:
        flash("源镜像和目标镜像不能为空", "error")
        return redirect(url_for("dashboard"))

    task = CopyTask(
        user_id=current_user.id,
        registry_id=reg.id,
        source_image=source,
        dest_image=dest.lstrip("/"),
        multi_arch=multi_arch,
        status="pending",
    )
    db.session.add(task)
    db.session.commit()

    _ensure_worker()
    task_queue.put(task.id)
    flash(f"任务 #{task.id} 已加入队列", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>")
@login_required
def task_detail(task_id: int):
    task = db.session.get(CopyTask, task_id)
    if task is None or task.user_id != current_user.id:
        flash("任务不存在", "error")
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
        flash("任务不存在", "error")
        return redirect(url_for("dashboard"))
    if task.status in ("pending", "running"):
        flash("任务正在排队或运行中，无需重试", "error")
        return redirect(url_for("task_detail", task_id=task.id))
    if task.status not in ("success", "failed"):
        flash("当前状态不允许重试", "error")
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
    flash(f"任务 #{task.id} 已重新加入队列", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
@login_required
def task_delete(task_id: int):
    task = db.session.get(CopyTask, task_id)
    if task is None or task.user_id != current_user.id:
        flash("任务不存在", "error")
        return redirect(url_for("dashboard"))
    if task.status in ("pending", "running"):
        flash("任务正在排队或运行中，不能删除", "error")
        return redirect(url_for("task_detail", task_id=task.id))

    db.session.delete(task)
    db.session.commit()
    flash(f"任务 #{task_id} 已删除", "success")

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
    }


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="页面不存在"), 404


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
    # 清理上一次会话遗留的 running 任务（worker 内存里的队列已丢，DB 上的状态得主动改）
    _recover_orphan_running_tasks()


init_db()


if __name__ == "__main__":
    _ensure_worker()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
