"""conversations / messages 表读写。"""
from __future__ import annotations

import json
from typing import Any

from app.db.connection import read_conn, write_conn


def _loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def create(customer_id: int, title: str | None = None) -> dict:
    with write_conn() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (customer_id, title) VALUES (?, ?)",
            (customer_id, title),
        )
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def get(conversation_id: int) -> dict | None:
    with read_conn() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    return dict(row) if row else None


def latest(customer_id: int) -> dict | None:
    with read_conn() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE customer_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
            (customer_id,),
        ).fetchone()
    return dict(row) if row else None


def reusable(customer_id: int, hours: int = 24) -> dict | None:
    """最近一条 updated_at 在 N 小时内的会话。"""
    with read_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM conversations
             WHERE customer_id = ?
               AND datetime(updated_at) >= datetime('now','localtime', ?)
             ORDER BY updated_at DESC, id DESC LIMIT 1
            """,
            (customer_id, f"-{int(hours)} hours"),
        ).fetchone()
    return dict(row) if row else None


def get_or_create(customer_id: int, hours: int = 24) -> tuple[dict, bool]:
    found = reusable(customer_id, hours)
    if found:
        return found, False
    return create(customer_id), True


def touch(conversation_id: int) -> None:
    with write_conn() as conn:
        conn.execute(
            "UPDATE conversations SET updated_at = datetime('now','localtime') WHERE id = ?",
            (conversation_id,),
        )


def add_message(
    conversation_id: int,
    role: str,
    content: str,
    intent_code: int | None = None,
    route: str | None = None,
    citations: list | None = None,
    latency_ms: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> int:
    with write_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages
              (conversation_id, role, content, intent_code, route, citations,
               latency_ms, tokens_in, tokens_out)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                role,
                content,
                intent_code,
                route,
                json.dumps(citations or [], ensure_ascii=False),
                latency_ms,
                tokens_in,
                tokens_out,
            ),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = datetime('now','localtime') WHERE id = ?",
            (conversation_id,),
        )
        if role == "user":
            conn.execute(
                """
                UPDATE conversations
                   SET title = COALESCE(title, ?)
                 WHERE id = ? AND (title IS NULL OR title = '')
                """,
                (content[:20], conversation_id),
            )
    return int(cur.lastrowid)


def list_messages(conversation_id: int, limit: int = 50, ascending: bool = True) -> list[dict]:
    order = "ASC" if ascending else "DESC"
    with read_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM (
              SELECT * FROM messages WHERE conversation_id = ?
               ORDER BY id DESC LIMIT ?
            ) ORDER BY id {order}
            """,
            (conversation_id, limit),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def _row_to_message(row: Any) -> dict:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "role": row["role"],
        "content": row["content"],
        "intent_code": row["intent_code"],
        "route": row["route"],
        "citations": _loads(row["citations"], []),
        "latency_ms": row["latency_ms"],
        "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"],
        "created_at": row["created_at"],
    }


def recent_for_customer(customer_id: int, limit: int = 50) -> list[dict]:
    with read_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM (
              SELECT m.* FROM messages m
                JOIN conversations cv ON cv.id = m.conversation_id
               WHERE cv.customer_id = ?
               ORDER BY m.id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (customer_id, limit),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def history(conversation_id: int, limit: int = 6) -> list[dict]:
    """最近 N 条消息，按时间正序。"""
    return list_messages(conversation_id, limit=limit, ascending=True)


def list_for_customer(customer_id: int) -> list[dict]:
    with read_conn() as conn:
        rows = conn.execute(
            """
            SELECT cv.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = cv.id) AS message_count
              FROM conversations cv
             WHERE cv.customer_id = ?
             ORDER BY cv.updated_at DESC, cv.id DESC
            """,
            (customer_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "message_count": r["message_count"],
        }
        for r in rows
    ]


def count(customer_id: int | None = None) -> int:
    with read_conn() as conn:
        if customer_id is None:
            row = conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM conversations WHERE customer_id = ?", (customer_id,)
            ).fetchone()
    return row["n"]


def first_user_message(conversation_id: int) -> dict | None:
    with read_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM messages
             WHERE conversation_id = ? AND role = 'user'
             ORDER BY id ASC LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
    return _row_to_message(row) if row else None


def last_assistant_message(customer_id: int) -> dict | None:
    with read_conn() as conn:
        row = conn.execute(
            """
            SELECT m.* FROM messages m
              JOIN conversations cv ON cv.id = m.conversation_id
             WHERE cv.customer_id = ? AND m.role = 'assistant'
             ORDER BY m.id DESC LIMIT 1
            """,
            (customer_id,),
        ).fetchone()
    return _row_to_message(row) if row else None
