"""Lightweight i18n for docker-proxy.

English is the source language (no lookup needed); Chinese is loaded from the
TRANSLATIONS dict below. Locale resolution order (Flask request context):

    1. ?lang=en|zh query param  (persists via cookie)
    2. lang cookie
    3. default = "en"

Usage in Python:
    from i18n import _   # gettext-style
    flash(_("Login successful"), "success")

Usage in Jinja templates:
    {{ _("Login") }}
    {% set msg = _("Image %(name)s created", name=x) %}

Keep all source strings in English. To add a new string, just use it; if no
zh translation is registered it falls back to the English source.
"""
from __future__ import annotations

from flask import g, has_request_context, request

SUPPORTED_LOCALES = ("en", "zh")
DEFAULT_LOCALE = "en"
LOCALE_COOKIE = "lang"
LOCALE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


# English source → Chinese translation. Missing keys fall back to the source.
TRANSLATIONS: dict[str, dict[str, str]] = {
    "zh": {
        # ── Brand / nav ──────────────────────────────────────────────────
        "Image Copy": "镜像拷贝",
        "Registry Management": "Registry 管理",
        "User Management": "账号管理",
        "My Account": "我的账号",
        "Log out (%(name)s)": "退出（%(name)s）",
        "Local tool": "本地工具",
        "Language": "语言",
        "English": "English",
        "中文": "中文",

        # ── Login ────────────────────────────────────────────────────────
        "Login": "登录",
        "Login · docker-proxy": "登录 · docker-proxy",
        "Username": "用户名",
        "Password": "密码",
        "Invalid username or password": "用户名或密码错误",

        # ── Dashboard ────────────────────────────────────────────────────
        "Image Copy · docker-proxy": "镜像拷贝 · docker-proxy",
        "⚠️ `docker` command not found; tasks will fail immediately on submit. Install Docker or set DOCKER_HOST for a remote daemon.": "⚠️ 未检测到 docker 命令，任务提交后会立即失败。请安装 Docker，或设置 DOCKER_HOST 指向远端 daemon。",
        "No target Registry yet — %(link_start)sadd one first%(link_end)s.": "还没有目标 Registry —— %(link_start)s先添加一个%(link_end)s。",
        "Target Registry": "目标 Registry",
        "skip TLS": "跳过 TLS",
        "Source image": "源镜像",
        "Destination image": "目标镜像",
        "e.g. docker.io/library/nginx:1.27. Full image:tag — the worker runs `docker pull` first; already-local images are detected and skipped.": "例如 docker.io/library/nginx:1.27。完整 image:tag —— worker 会先 `docker pull`；本地已有的镜像会被自动跳过。",
        "e.g. docker.io/library/nginx:1.27 (full path of a Docker Hub official image)": "例如 docker.io/library/nginx:1.27（Docker Hub 官方镜像的完整路径）",
        "e.g. nginx:1.27 (only image:tag; project is auto-appended by the selected Registry)": "例如 nginx:1.27（仅 image:tag，project 由所选 Registry 自动追加）",
        "Cleanup local copy after push": "推完后清理本地副本",
        "Runs `docker rmi` to delete the source image and the temporary target tag locally after a successful push. Uncheck to keep your local images (e.g. for self-built images you also use elsewhere).": "推成功后跑 `docker rmi` 删除源镜像和临时目标 tag。取消勾选可保留本地镜像（适合自己 build 还要在本地用的）。",
        "Source must be a full image:tag, e.g. `docker.io/library/nginx:1.27`. The worker runs `docker pull` first; already-local images are detected and skipped automatically.": "源必须是完整的 image:tag，如 `docker.io/library/nginx:1.27`。worker 会先 `docker pull`；本地已有的镜像自动跳过。",
        "Destination only needs image:tag; the selected Registry's URL and project prefix are appended on submit.": "目标镜像只需填写image:tag，提交后会拼上对应的Registry 的 url 和 project 前缀。",
        "Multi-arch": "多架构",
        "Retry times": "重试次数",
        "Push the full multi-arch manifest list (amd64 / arm64 / armv7). Default Yes.": "推送整张多架构 manifest list（amd64 / arm64 / armv7 等）。默认 是。",
        "push the full manifest list (amd64 / arm64 / armv7, etc). Recommended for multi-arch images like nginx from an ARM Mac. Default Yes.": "推送整张 manifest list（amd64 / arm64 / armv7 等）。多架构镜像（如 Mac ARM 上的 nginx）建议勾选。默认 是。",
        "Yes": "是",
        "No": "否",
        "Legacy field; docker push handles retries itself.": "历史遗留字段；docker push 自带重试，可忽略。",
        "Start Copy": "开始拷贝",
        "Recent tasks": "最近任务",
        "Status": "状态",
        "Source → Destination": "源 → 目标",
        "Created at": "创建时间",
        "View": "查看",
        "Delete": "删除",
        "Confirm delete task #%(id)s?": "确认删除任务 #%(id)s 的记录？",
        "No tasks yet.": "暂无任务。",

        # ── Registries list ──────────────────────────────────────────────
        "Registry Management · docker-proxy": "Registry 管理 · docker-proxy",
        "+ Add": "+ 新增",
        "Name": "名称",
        "Address": "地址",
        "Project path": "项目路径",
        "Default: verify TLS certificate": "默认：校验 TLS 证书",
        "🔒 verify": "🔒 校验",
        "TLS verification skipped (self-signed / intranet)": "已跳过 TLS 证书校验（自签/内网）",
        "⚠ skip": "⚠ 跳过",
        "Images": "镜像",
        "Edit": "编辑",
        "Confirm delete this Registry configuration?": "确定删除该 Registry 配置？",
        "No Registry yet.": "还没有 Registry。",

        # ── Registry form ────────────────────────────────────────────────
        "Edit Registry · docker-proxy": "编辑 Registry · docker-proxy",
        "Add Registry · docker-proxy": "新增 Registry · docker-proxy",
        "Edit Registry": "编辑 Registry",
        "Add Registry": "新增 Registry",
        "Registry address": "Registry 地址",
        "Password (leave blank to keep unchanged)": "密码（留空表示不修改）",
        "Default checked = verify TLS certificate (recommended for production). Uncheck for self-signed / intranet.": "默认勾选 = 校验 TLS 证书（生产环境推荐）。自签证书 / 内网环境请取消勾选。",
        "Project path prefix (project)": "项目路径前缀 (project)",
        "Save": "保存",
        "Cancel": "取消",

        # ── Catalog ──────────────────────────────────────────────────────
        "Images · %(name)s · docker-proxy": "镜像列表 · %(name)s · docker-proxy",
        "⚠ skip TLS": "⚠ 跳过 TLS",
        "Back": "返回",
        "⚠️ Failed to read catalog: %(error)s": "⚠️ 读取 catalog 失败：%(error)s",
        "🔄 Retry": "🔄 重新拉取",
        "✏️ Update credentials": "✏️ 更新账号密码",
        "No repositories in this Registry yet.": "该 Registry 中暂无任何仓库。",
        "%(total)s repositories. Click \"view tags\" to expand.": "共 %(total)s 个仓库。点击\"查看 tag\"展开。",
        "Repository (repo)": "仓库 (repo)",
        "tag count": "tag 数",
        "Confirm delete ALL tags under repository %(repo)s? This cannot be undone.": "确认删除整个仓库 %(repo)s 的所有 tag？该操作不可撤销。",
        "Delete entire repo": "删除整个仓库",
        "tag load failed": "tag 加载失败",
        "Confirm delete %(repo)s:%(tag)s?": "确认删除 %(repo)s:%(tag)s ？",
        "This repository has no tags.": "该仓库没有任何 tag。",

        # ── Task detail ──────────────────────────────────────────────────
        "Task #%(id)s · docker-proxy": "任务 #%(id)s · docker-proxy",
        "Task": "任务",
        "polling": "正在轮询",
        "Retry": "重试",
        "⏳ Task queued, waiting for worker…": "⏳ 任务已加入队列，等待 worker 拾起…",
        "Source": "源镜像",
        "Destination": "目标",
        "Registry": "Registry",
        "Created": "创建",
        "Started": "开始",
        "Finished": "结束",
        "Exit code": "退出码",
        "Command": "命令",
        "Log": "日志",
        "(no output yet)": "(暂无输出)",
        "Error": "错误",
        "Refreshed at %(time)s": "刷新于 %(time)s",

        # ── Account ──────────────────────────────────────────────────────
        "My Account · docker-proxy": "个人信息 · docker-proxy",
        "Account": "个人信息",
        "Role": "角色",
        "Admin": "管理员",
        "Regular user": "普通用户",
        "Registered at": "注册时间",
        "Change password": "修改密码",
        "Current password": "当前密码",
        "New password (min 6 chars)": "新密码（至少 6 位）",
        "Confirm new password": "确认新密码",

        # ── Admin users ──────────────────────────────────────────────────
        "User Management · docker-proxy": "账号管理 · docker-proxy",
        "Existing accounts": "现有账号",
        "Operations": "操作",
        "Reset password": "重置密码",
        "Revoke admin": "取消管理员",
        "Grant admin": "设为管理员",
        "Confirm change admin status of %(name)s?": "确认修改 %(name)s 的管理员身份？",
        "Confirm delete user %(name)s?": "确认删除用户 %(name)s ？",
        "Create account": "新建账号",
        "Username (3-80 chars)": "用户名（3-80 字符）",
        "Password (min 6 chars)": "密码（至少 6 位）",
        "Confirm password": "确认密码",
        "Grant admin privileges": "授予管理员权限",

        # ── Admin user reset ─────────────────────────────────────────────
        "Reset password · %(name)s · docker-proxy": "重置密码 · %(name)s · docker-proxy",
        "Set a new password for %(name_html)s.": "为 %(name_html)s 设置新密码。",

        # ── Error page ───────────────────────────────────────────────────
        "Back to home": "返回首页",

        # ── Flash / error / form messages from app.py ────────────────────
        "Two entries do not match": "两次输入不一致",
        "Admin permission required": "需要管理员权限",
        "Current password is incorrect": "当前密码错误",
        "Password updated": "密码已更新",
        "User already exists": "用户已存在",
        "User %(name)s created": "已创建用户 %(name)s",
        "User does not exist": "用户不存在",
        "Password for %(name)s has been reset": "已重置 %(name)s 的密码",
        "Cannot delete the currently logged-in account": "不能删除当前登录的账号",
        "Cannot delete the last admin": "不能删除最后一个管理员",
        "User %(name)s deleted%(suffix)s": "已删除用户 %(name)s%(suffix)s",
        " (including %(count)s task records)": "（含 %(count)s 个任务记录）",
        "Cannot change your own admin status": "不能修改自己的管理员身份",
        "Cannot revoke the last admin": "不能取消最后一个管理员",
        "%(name)s is now %(role)s": "%(name)s 已设为 %(role)s",
        "Password cannot be empty when creating a Registry": "新建 Registry 时密码不能为空",
        "Registry saved": "Registry 已保存",
        "Registry does not exist": "Registry 不存在",
        "Updated": "已更新",
        "Registry still has task records and cannot be deleted": "该 Registry 仍有任务记录，无法删除",
        "Deleted": "已删除",
        "Missing repo name": "缺少 repo 名称",
        "Deleted %(ok)s tags under repository %(repo)s": "已删除仓库 %(repo)s 下的 %(ok)s 个 tag",
        "Repository %(repo)s has no tags": "仓库 %(repo)s 没有任何 tag",
        "Repository %(repo)s: %(ok)s ok, %(fail)s failed.%(errs)s": "仓库 %(repo)s：成功 %(ok)s，失败 %(fail)s。%(errs)s",
        " Errors: %(errs)s": " 错误：%(errs)s",
        "Missing tag": "缺少 tag",
        "Deleted %(repo)s:%(tag)s": "已删除 %(repo)s:%(tag)s",
        "Failed to delete %(repo)s:%(tag)s: %(output)s": "删除 %(repo)s:%(tag)s 失败：%(output)s",
        "Please select a valid target Registry": "请选择一个有效的目标 Registry",
        "Source and destination images cannot be empty": "源镜像和目标镜像不能为空",
        "Task #%(id)s queued": "任务 #%(id)s 已加入队列",
        "Task does not exist": "任务不存在",
        "Task is queued or running, no retry needed": "任务正在排队或运行中，无需重试",
        "Current status does not allow retry": "当前状态不允许重试",
        "Task #%(id)s re-queued": "任务 #%(id)s 已重新加入队列",
        "Task is queued or running, cannot be deleted": "任务正在排队或运行中，不能删除",
        "Task #%(id)s deleted": "任务 #%(id)s 已删除",
        "Page not found": "页面不存在",
        "(not yet generated)": "(尚未生成)",
        "(registry was deleted, cannot rebuild command)": "(registry 已被删除，无法重建命令)",
        "docker command not found. Install Docker or set DOCKER_HOST for a remote daemon.": "未检测到 docker 命令。请安装 Docker，或设置 DOCKER_HOST 指向远端 daemon。",
        "Failed to start docker: %(err)s": "启动 docker 失败：%(err)s",
        "docker exited with code %(rc)s": "docker 退出码 %(rc)s",
        "Execution error: %(err)s": "执行异常：%(err)s",
        "Detected unfinished task from previous session at startup; auto-marked as failed": "服务重启时检测到上一次会话未完成的任务，已自动标记为失败",
        "Detected unstarted task from previous session at startup; auto-marked as failed": "服务重启时检测到上一次会话未启动的任务，已自动标记为失败",
        "Task had no heartbeat for over %(sec)ss; worker/docker may have hung, auto-marked as failed": "任务超过 %(sec)ss 未上报心跳，worker/docker 可能已挂起，已自动标记为失败",
        "%(url)s does not have the catalog API enabled (404). Harbor: project settings → allow catalog ('Enable catalog'); some registries simply do not implement this API.": "%(url)s 未启用 catalog API（404）。Harbor：项目设置 → 允许清单（'Enable catalog'）；部分 Registry 干脆没实现此接口。",
        "Auth failed: HTTP %(code)s @ %(url)s. Most common causes: ① the temporary password has expired (console → access credentials → regenerate, then save on the Registry edit page); ② the current account has no read permission on this Registry namespace.": "鉴权失败：HTTP %(code)s @ %(url)s。最常见原因：① 临时密码已过期（控制台 → 访问凭证 → 重新生成临时密码，再在 Registry 编辑页保存）；② 当前账号对该 Registry 命名空间缺少读权限。",
        "list-tags failed: %(msg)s": "list-tags 失败：%(msg)s",
        "Cannot parse registry response: %(err)s": "无法解析 Registry 返回：%(err)s",

        # ── Form field labels / descriptions ─────────────────────────────
        "e.g. harbor.company.local": "例如 harbor.company.local",
        "Verify TLS certificate (recommended; uncheck for self-signed / intranet)": "校验 TLS 证书（推荐勾选；自签证书 / 内网环境请取消）",
        "Auto-prepended to the destination image on push. e.g. docker-proxy → pushed as docker-proxy/nginx:1.27; leave blank to skip.": "推送时自动加在目标镜像前。例如 docker-proxy → 实际推送为 docker-proxy/nginx:1.27；留空则不追加。",
        "e.g. docker.io/library/nginx:1.27 (full path of a Docker Hub official image)": "例如 docker.io/library/nginx:1.27（Docker Hub 官方镜像的完整路径）",
        "e.g. nginx:1.27 (only image:tag; project is auto-appended by the selected Registry)": "例如 nginx:1.27（仅 image:tag，project 由所选 Registry 自动追加）",
    },
}


class _LazyString:
    """A string proxy that resolves on str()/repr() against the current locale.

    Needed for WTForms field labels: they are evaluated once at class-definition
    time but rendered per-request, so we can't just call _() eagerly.
    """

    __slots__ = ("_msgid", "_kwargs")

    def __init__(self, msgid: str, **kwargs: object) -> None:
        self._msgid = msgid
        self._kwargs = kwargs

    def __str__(self) -> str:
        return translate(self._msgid, **self._kwargs)

    def __repr__(self) -> str:
        return str(self)

    def __html__(self) -> str:
        return str(self)

    def __add__(self, other: object) -> str:
        return str(self) + str(other)

    def __radd__(self, other: object) -> str:
        return str(other) + str(self)


def get_locale() -> str:
    """Return the locale to use for this request.

    Resolution: g.locale (set by middleware) → "en".
    """
    if has_request_context():
        loc = getattr(g, "locale", None)
        if loc in SUPPORTED_LOCALES:
            return loc
    return DEFAULT_LOCALE


def resolve_locale_from_request() -> str:
    """Read ?lang= or cookie; called by before_request to populate g.locale."""
    if not has_request_context():
        return DEFAULT_LOCALE
    qp = request.args.get("lang", "").strip().lower()
    if qp in SUPPORTED_LOCALES:
        return qp
    cookie = request.cookies.get(LOCALE_COOKIE, "").strip().lower()
    if cookie in SUPPORTED_LOCALES:
        return cookie
    return DEFAULT_LOCALE


def translate(msgid: str, **kwargs: object) -> str:
    """Look up msgid in the current locale, falling back to the source.

    Format with %(name)s style; if kwargs empty, return as-is. Works for both
    static strings and parameterized ones — keep all msgids in English source.
    """
    locale = get_locale()
    table = TRANSLATIONS.get(locale, {})
    text = table.get(msgid, msgid)
    if kwargs:
        try:
            return text % kwargs
        except (KeyError, ValueError):
            return text
    return text


def gettext(msgid: str, **kwargs: object) -> str:
    return translate(msgid, **kwargs)


def lazy_gettext(msgid: str, **kwargs: object) -> _LazyString:
    return _LazyString(msgid, **kwargs)


# Short aliases — `_` for runtime, `_l` for class-time (WTForms labels).
_ = gettext
_l = lazy_gettext
