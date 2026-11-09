"""页面路由：用户端（入口 / 聊天）与管理端（登录 / 客户 / 知识库）分离。"""
from __future__ import annotations

from flask import Blueprint, Response, redirect, render_template, request, url_for

from app.api.auth import (
    admin_page_required,
    check_credentials,
    current_admin,
    guard_login_attempt,
    is_authenticated,
    login_admin,
    logout_admin,
    record_login_failure,
)

bp = Blueprint("pages", __name__)


def _safe_next(value: str | None) -> str:
    """只允许跳回后台内部路径，避免开放重定向。"""
    target = (value or "").strip()
    if target.startswith("/admin/") and "//" not in target and "\\" not in target:
        return target
    return ""


# ------------------------------------------------------------------ 用户端
@bp.get("/")
def index():
    """用户入口页：点一下即可进入咨询。"""
    return render_template("index.html")


@bp.get("/chat")
def chat_page():
    return render_template("chat.html")


# ------------------------------------------------------------------ 管理端
@bp.get("/admin")
def admin_root():
    if is_authenticated():
        return redirect(url_for("pages.admin_customers_page"))
    return redirect(url_for("pages.admin_login_page"))


@bp.route("/admin/login", methods=["GET", "POST"])
def admin_login_page():
    next_url = _safe_next(request.values.get("next"))
    if request.method == "GET" and is_authenticated():
        return redirect(next_url or url_for("pages.admin_customers_page"))

    error = ""
    username = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        allowed, wait = guard_login_attempt()
        if not allowed:
            error = f"尝试次数过多，请 {wait} 秒后再试"
        elif not check_credentials(username, password):
            record_login_failure()
            error = "账号或密码不正确"
        else:
            login_admin(username)
            return redirect(next_url or url_for("pages.admin_customers_page"))

    return render_template("admin_login.html", error=error, username=username, next_url=next_url)


@bp.post("/admin/logout")
def admin_logout():
    logout_admin()
    return redirect(url_for("pages.admin_login_page"))


@bp.get("/admin/customers")
@admin_page_required
def admin_customers_page():
    return render_template("admin_customers.html", active="customers", admin=current_admin())


@bp.get("/admin/kb")
@admin_page_required
def admin_kb_page():
    return render_template("admin_kb.html", active="kb", admin=current_admin())


@bp.get("/favicon.ico")
def favicon():
    return Response(status=204)
