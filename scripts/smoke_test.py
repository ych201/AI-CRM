"""端到端冒烟：健康检查 → 建立会话 → 流式对话 → 画像 → SQL Agent → 查库。

用法：python scripts/smoke_test.py
无需外部密钥也可以跑（离线演示模式）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.config import get_config  # noqa: E402
from app.db import conversations as conv_db  # noqa: E402
from app.db import customers as customers_db  # noqa: E402

PHONE = "13800138000"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        failures.append(name)


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        event = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data += line[len("data:") :].strip()
        if event:
            try:
                events.append((event, json.loads(data or "{}")))
            except ValueError:
                events.append((event, {"raw": data}))
    return events


def main() -> int:
    app = create_app()
    client = app.test_client()
    cfg = get_config()

    health = client.get("/api/health").get_json()
    print("health:", json.dumps(health, ensure_ascii=False))

    session = client.post("/api/session", json={"phone": PHONE})
    check("POST /api/session", session.status_code == 200, str(session.status_code))
    session_data = session.get_json() or {}
    print("greeting:", session_data.get("greeting"))
    conversation_id = session_data.get("conversation_id")

    resp = client.post(
        "/api/chat",
        json={
            "phone": PHONE,
            "conversation_id": conversation_id,
            "message": "AI 产品经理课程多少钱",
        },
    )
    body = resp.get_data(as_text=True)
    events = parse_sse(body)
    kinds = [e[0] for e in events]
    check("SSE 含 meta", "meta" in kinds, str(kinds))
    check("SSE 含 delta", "delta" in kinds)
    check("SSE 含 done", "done" in kinds)
    meta = next((d for k, d in events if k == "meta"), {})
    print("meta:", json.dumps(meta, ensure_ascii=False))
    answer = "".join(d.get("t", "") for k, d in events if k == "delta")
    print("answer:", answer[:160])

    resp2 = client.post(
        "/api/chat",
        json={
            "phone": PHONE,
            "conversation_id": conversation_id,
            "message": "我今天新增了几个客户，帮我查一下",
        },
    )
    events2 = parse_sse(resp2.get_data(as_text=True))
    tools = [d for k, d in events2 if k == "tool"]
    print("tools:", json.dumps(tools, ensure_ascii=False))
    check("SQL Agent 触发 query_crm_db", any(t.get("name") == "query_crm_db" for t in tools))

    time.sleep(1.5)  # 等后台画像线程
    customer = customers_db.get_by_phone(PHONE)
    check("客户记录存在", customer is not None)
    messages = conv_db.recent_for_customer(customer["id"], limit=10) if customer else []
    check("消息已落库", len(messages) >= 4, f"{len(messages)} 条")

    with_key = client.get("/api/customers", headers={"X-API-Key": cfg.admin_api_key})
    check("GET /api/customers", with_key.status_code == 200, str(with_key.status_code))
    no_key = client.get("/api/customers")
    check("无 Key 被拒绝", no_key.status_code == 401, str(no_key.status_code))

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
