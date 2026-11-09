"""管理端登录：账号密码会话鉴权、页面跳转、登出。"""
from __future__ import annotations

import pytest


@pytest.fixture()
def admin_credentials(app):
    from app.config import get_config

    cfg = get_config()
    return {"username": cfg.admin_username, "password": cfg.admin_password}


def test_user_pages_are_public(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "开始咨询" in home.get_data(as_text=True)

    chat = client.get("/chat")
    assert chat.status_code == 200
    assert "找回你的咨询记录" in chat.get_data(as_text=True)
    assert "X-API-Key" not in chat.get_data(as_text=True)


def test_admin_page_redirects_to_login(client):
    resp = client.get("/admin/customers")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]

    login = client.get("/admin/login")
    body = login.get_data(as_text=True)
    assert login.status_code == 200
    assert 'name="username"' in body and 'type="password"' in body
    assert "X-API-Key" not in body


def test_admin_api_requires_login(client):
    resp = client.get("/api/customers")
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "UNAUTHORIZED"


def test_login_with_wrong_password(client, admin_credentials):
    resp = client.post(
        "/admin/login",
        data={"username": admin_credentials["username"], "password": "wrong-password"},
    )
    assert resp.status_code == 200
    assert "账号或密码不正确" in resp.get_data(as_text=True)

    still_blocked = client.get("/api/customers")
    assert still_blocked.status_code == 401


def test_login_then_access_admin(client, admin_credentials):
    resp = client.post(
        "/admin/login",
        data={
            "username": admin_credentials["username"],
            "password": admin_credentials["password"],
            "next": "/admin/kb",
        },
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/admin/kb")

    assert client.get("/admin/kb").status_code == 200
    assert client.get("/admin/customers").status_code == 200
    listing = client.get("/api/customers")
    assert listing.status_code == 200
    assert "items" in listing.get_json()


def test_logout_clears_session(client, admin_credentials):
    client.post(
        "/admin/login",
        data={
            "username": admin_credentials["username"],
            "password": admin_credentials["password"],
        },
    )
    assert client.get("/api/customers").status_code == 200

    out = client.post("/admin/logout")
    assert out.status_code == 302
    assert client.get("/api/customers").status_code == 401
