"""知识库 API：鉴权、幂等更新、删除、切片一致性。"""
from __future__ import annotations

from app.vector import store

DOC = {
    "doc_id": "course_pricing",
    "collection": "course",
    "title": "2026 年课程价格表",
    "content": "AI 产品经理实战班：原价 12800 元，本期优惠 9800 元。" * 20,
    "source": "cms",
}

DOC_V2 = dict(DOC, content="AI 产品经理实战班：本期价格 8800 元，赠送模拟面试 2 次。" * 20)


def test_requires_api_key(client):
    resp = client.post("/api/kb/documents", json=DOC)
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "UNAUTHORIZED"


def test_create_update_delete(client, headers):
    created = client.post("/api/kb/documents", json=DOC, headers=headers)
    assert created.status_code == 200, created.get_json()
    body = created.get_json()
    assert body["action"] == "created"
    assert body["chunks"] >= 1
    assert store.count("course") == body["chunks"]

    same = client.post("/api/kb/documents", json=DOC, headers=headers)
    assert same.get_json()["action"] == "updated"
    assert store.count("course") == same.get_json()["chunks"], "同 doc_id 覆盖不应产生重复切片"

    updated = client.post("/api/kb/documents", json=DOC_V2, headers=headers)
    assert updated.get_json()["action"] == "updated"
    assert store.count("course") == updated.get_json()["chunks"]

    detail = client.get("/api/kb/documents/course_pricing", headers=headers)
    assert detail.status_code == 200
    assert "8800" in detail.get_json()["content"]

    listing = client.get("/api/kb/documents?collection=course", headers=headers)
    assert listing.get_json()["total"] == 1

    deleted = client.delete("/api/kb/documents/course_pricing", headers=headers)
    assert deleted.get_json() == {"deleted": True, "doc_id": "course_pricing"}
    assert store.count("course") == 0

    again = client.delete("/api/kb/documents/course_pricing", headers=headers)
    assert again.status_code == 404


def test_validation(client, headers):
    bad_collection = dict(DOC, collection="unknown")
    resp = client.post("/api/kb/documents", json=bad_collection, headers=headers)
    assert resp.status_code == 422

    empty = dict(DOC, content="   ")
    resp = client.post("/api/kb/documents", json=empty, headers=headers)
    assert resp.status_code == 422

    too_long = dict(DOC, content="字" * 20001)
    resp = client.post("/api/kb/documents", json=too_long, headers=headers)
    assert resp.status_code == 422

    bad_id = dict(DOC, doc_id="bad id!")
    resp = client.post("/api/kb/documents", json=bad_id, headers=headers)
    assert resp.status_code == 422


def test_auto_doc_id(client, headers):
    payload = dict(DOC)
    payload.pop("doc_id")
    resp = client.post("/api/kb/documents", json=payload, headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["doc_id"].startswith("course_")


def test_rebuild(client, headers):
    client.post("/api/kb/documents", json=DOC, headers=headers)
    resp = client.post("/api/kb/rebuild", json={"collection": "course"}, headers=headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["documents"] == 1
    assert body["chunks"] >= 1
    assert store.count("course") == body["chunks"]
