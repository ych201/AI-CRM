"""对话主链路：会话 → SSE → 引用 → 落库 → 画像（离线模式）。"""
from __future__ import annotations

import json
import time

PHONE = "13800138000"
DOC = {
    "doc_id": "course_pricing",
    "collection": "course",
    "title": "2026 年课程价格表",
    "content": (
        "2026 年课程价格表：AI 产品经理实战班课程原价 12800 元，本期优惠价 9800 元，"
        "课程周期 8 周，支持 3 期免息分期，报名后 7 天内可申请退款。"
    ),
}


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        event = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if event:
            events.append((event, json.loads(data or "{}")))
    return events


def test_session_and_chat(client, headers):
    client.post("/api/kb/documents", json=DOC, headers=headers)

    session = client.post("/api/session", json={"phone": PHONE})
    assert session.status_code == 200
    body = session.get_json()
    assert body["greeting"]
    assert body["is_new"] is True
    conv_id = body["conversation_id"]

    resp = client.post(
        "/api/chat",
        json={"phone": PHONE, "conversation_id": conv_id, "message": "课程多少钱"},
    )
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    events = parse_sse(resp.get_data(as_text=True))
    kinds = [e for e, _ in events]

    assert "meta" in kinds and "delta" in kinds and "done" in kinds
    meta = next(d for e, d in events if e == "meta")
    assert meta["intent_code"] == 2
    assert meta["route"] == "rag_course"
    assert meta["message_id"] is None

    citations = [d for e, d in events if e == "citation"]
    assert citations and citations[0]["items"][0]["doc_id"] == "course_pricing"

    done = next(d for e, d in events if e == "done")
    assert done["message_id"]
    assert done["persisted"] is True
    assert done["tokens"]["out"] >= 0

    detail = client.get(f"/api/customers/{body['customer']['id']}", headers=headers).get_json()
    assistant = [m for m in detail["recent_messages"] if m["role"] == "assistant"]
    assert assistant and assistant[-1]["route"] == "rag_course"
    assert assistant[-1]["citations"][0]["doc_id"] == "course_pricing"


def test_profile_pipeline(client):
    session = client.post("/api/session", json={"phone": "13900139000"}).get_json()
    resp = client.post(
        "/api/chat",
        json={
            "phone": "13900139000",
            "conversation_id": session["conversation_id"],
            "message": "我现在做行政，想转 AI 产品，预算一万以内",
        },
    )
    resp.get_data()  # 必须消费完 SSE，否则生成器会被关闭

    card = None
    for _ in range(30):
        card = client.get("/api/customer-card?phone=13900139000").get_json()
        if (card.get("profile") or {}).get("goal"):
            break
        time.sleep(0.1)

    assert card["stage"] == "qualified"
    assert card["intent_level"] == "hot"
    assert card["profile"]["current_role"] == "行政"
    assert card["profile"]["budget"] == "一万以内"


def test_chat_validations(client):
    assert client.post("/api/session", json={"phone": "123"}).status_code == 400
    assert client.post("/api/chat", json={"phone": PHONE, "message": ""}).status_code == 422
    assert (
        client.post("/api/chat", json={"phone": PHONE, "message": "x" * 2001}).status_code == 422
    )
