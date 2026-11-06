"""页面路由。"""
from __future__ import annotations

from flask import Blueprint, Response, render_template

bp = Blueprint("pages", __name__)


@bp.get("/")
def chat_page():
    return render_template("chat.html", active="chat")


@bp.get("/admin/customers")
def admin_customers_page():
    return render_template("admin_customers.html", active="customers")


@bp.get("/admin/kb")
def admin_kb_page():
    return render_template("admin_kb.html", active="kb")


@bp.get("/favicon.ico")
def favicon():
    return Response(status=204)
