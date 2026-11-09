"""管理端登录鉴权：账号 + 密码 → 服务端 Session（不再用 API Key 登录）。

- 页面级：未登录跳转 ``/admin/login``，登录后回到原地址。
- 接口级：未登录返回 401 JSON。
- 兼容：仍接受 ``X-API-Key`` 头（供脚本 / 冒烟测试调用），页面不再暴露 Key 输入。
"""
from __future__ import annotations

import hmac
from functools import wraps
from urllib.parse import urlparse

from flask import redirect, request, session, url_for

from app.config import get_config
from app.core import ratelimit
from app.core.errors import Unauthorized

SESSION_KEY = "admin_user"
LOGIN_LIMIT = 10
LOGIN_WINDOW = 300


def _eq(left: str, right: str) -> bool:
    return hmac.compare_digest((left or "").encode("utf-8"), (right or "").encode("utf-8"))


def check_credentials(username: str, password: str) -> bool:
    """常量时间比较账号与密码，两项都匹配才算通过。"""
    cfg = get_config()
    user_ok = _eq((username or "").strip(), cfg.admin_username)
    pass_ok = _eq(password or "", cfg.admin_password)
    return user_ok and pass_ok


def _login_key() -> str:
    client = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    return f"admin_login:{client or 'unknown'}"


def guard_login_attempt() -> tuple[bool, int]:
    """登录前检查：该来源是否已连续失败过多（进程内，多 worker 失效）。"""
    return ratelimit.peek(_login_key(), LOGIN_LIMIT, LOGIN_WINDOW)


def record_login_failure() -> None:
    ratelimit.hit(_login_key(), LOGIN_LIMIT, LOGIN_WINDOW)


def login_admin(username: str) -> None:
    session.clear()
    session[SESSION_KEY] = username
    session.permanent = True


def logout_admin() -> None:
    session.pop(SESSION_KEY, None)


def current_admin() -> str | None:
    return session.get(SESSION_KEY)


def _api_key_ok() -> bool:
    provided = request.headers.get("X-API-Key", "")
    expected = get_config().admin_api_key
    return bool(provided) and bool(expected) and _eq(provided, expected)


def is_authenticated() -> bool:
    return bool(current_admin()) or _api_key_ok()


def _safe_next(target: str | None) -> str:
    """只允许跳回后台内部路径，避免开放重定向。"""
    value = (target or "").strip()
    if not value.startswith("/admin/"):
        return ""
    if "//" in value or "\\" in value or urlparse(value).scheme:
        return ""
    return value


def login_url() -> str:
    safe = _safe_next(request.full_path.rstrip("?"))
    if safe and not safe.startswith("/admin/login"):
        return url_for("pages.admin_login_page", next=safe)
    return url_for("pages.admin_login_page")


def require_admin(fn):
    """管理端接口鉴权：Session 优先，其次兼容 X-API-Key。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            raise Unauthorized("请先登录管理后台")
        return fn(*args, **kwargs)

    return wrapper


def admin_page_required(fn):
    """管理端页面鉴权：未登录重定向到登录页。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            return redirect(login_url())
        return fn(*args, **kwargs)

    return wrapper


# 兼容旧引用
require_api_key = require_admin
