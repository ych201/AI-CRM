"""customers 表读写。"""
from __future__ import annotations

import json
from typing import Any

from app.db.connection import read_conn, write_conn

STAGES = ("new", "contacted", "qualified", "won", "lost")
INTENT_LEVELS = ("cold", "warm", "hot")

# profile 白名单（SDD 5.3）；name 单独落到 customers.name
PROFILE_FIELDS = (
    "current_role",
    "years_exp",
    "goal",
    "budget",
    "time_window",
    "city",
    "concerns",
)

_FIELD_MAX_LEN = 120


def mask_phone(phone: str | None) -> str:
    if not phone:
        return ""
    if len(phone) >= 7:
        return f"{phone[:3]}****{phone[-4:]}"
    return phone


def _loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _row_to_customer(row: Any, with_mask: bool = True) -> dict:
    return {
        "id": row["id"],
        "phone": mask_phone(row["phone"]) if with_mask else row["phone"],
        "name": row["name"],
        "stage": row["stage"],
        "intent_level": row["intent_level"],
        "tags": _loads(row["tags"], []),
        "profile": _loads(row["profile"], {}),
        "note": row["note"],
        "created_at": row["created_at"],
        "last_active_at": row["last_active_at"],
    }


def get_by_phone(phone: str) -> dict | None:
    with read_conn() as conn:
        row = conn.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()
    return _row_to_customer(row) if row else None


def get_by_id(customer_id: int, with_mask: bool = True) -> dict | None:
    with read_conn() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return _row_to_customer(row, with_mask=with_mask) if row else None


def create(phone: str, name: str | None = None) -> dict:
    with write_conn() as conn:
        cur = conn.execute(
            "INSERT INTO customers (phone, name) VALUES (?, ?)",
            (phone, name),
        )
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_customer(row)


def get_or_create(phone: str) -> tuple[dict, bool]:
    existing = get_by_phone(phone)
    if existing:
        return existing, False
    return create(phone), True


def touch(customer_id: int) -> None:
    with write_conn() as conn:
        conn.execute(
            "UPDATE customers SET last_active_at = datetime('now','localtime') WHERE id = ?",
            (customer_id,),
        )


def _sanitize_profile_value(key: str, value: Any) -> Any:
    if key == "concerns":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return None
        items = [str(v).strip()[:20] for v in value if str(v).strip()]
        return items[:6] or None
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:_FIELD_MAX_LEN]


def merge_profile(
    customer_id: int,
    fields: dict | None = None,
    intent_level: str | None = None,
    note: str | None = None,
) -> dict | None:
    """按白名单合并画像；返回更新后的客户对象。"""
    with write_conn() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not row:
            return None
        profile = _loads(row["profile"], {})
        name = row["name"]
        changed = []

        for key, value in (fields or {}).items():
            if key == "name":
                text = str(value or "").strip()
                if text and text != name:
                    name = text[:40]
                    changed.append("name")
                continue
            if key not in PROFILE_FIELDS:
                continue
            clean = _sanitize_profile_value(key, value)
            if clean is None:
                continue
            if profile.get(key) != clean:
                profile[key] = clean
                changed.append(key)

        stage = row["stage"]
        level = intent_level if intent_level in INTENT_LEVELS else row["intent_level"]
        if level == "hot" and profile.get("goal") and stage in ("new", "contacted"):
            stage = "qualified"

        conn.execute(
            """
            UPDATE customers
               SET name = ?, stage = ?, intent_level = ?, profile = ?, note = COALESCE(?, note),
                   last_active_at = datetime('now','localtime')
             WHERE id = ?
            """,
            (
                name,
                stage,
                level,
                json.dumps(profile, ensure_ascii=False),
                note,
                customer_id,
            ),
        )
        updated = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    result = _row_to_customer(updated)
    result["_changed"] = changed
    return result


def update_fields(
    customer_id: int,
    stage: str | None = None,
    intent_level: str | None = None,
    tags: list[str] | None = None,
    note: str | None = None,
) -> dict | None:
    sets = []
    params: list[Any] = []
    if stage is not None:
        sets.append("stage = ?")
        params.append(stage)
    if intent_level is not None:
        sets.append("intent_level = ?")
        params.append(intent_level)
    if tags is not None:
        sets.append("tags = ?")
        params.append(json.dumps(tags, ensure_ascii=False))
    if note is not None:
        sets.append("note = ?")
        params.append(note)
    if not sets:
        return get_by_id(customer_id)

    sets.append("last_active_at = datetime('now','localtime')")
    params.append(customer_id)
    with write_conn() as conn:
        conn.execute(f"UPDATE customers SET {', '.join(sets)} WHERE id = ?", params)
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return _row_to_customer(row) if row else None


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_customers(
    q: str | None = None,
    stage: str | None = None,
    intent_level: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    where = []
    params: list[Any] = []
    if q:
        where.append("(c.phone LIKE ? ESCAPE '\\' OR IFNULL(c.name,'') LIKE ? ESCAPE '\\')")
        like = _like(q)
        params.extend([like, like])
    if stage:
        where.append("c.stage = ?")
        params.append(stage)
    if intent_level:
        where.append("c.intent_level = ?")
        params.append(intent_level)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * page_size

    with read_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM customers c {where_sql}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT c.*,
                   (SELECT m.content FROM messages m
                      JOIN conversations cv ON cv.id = m.conversation_id
                     WHERE cv.customer_id = c.id AND m.role = 'user'
                     ORDER BY m.id DESC LIMIT 1) AS last_message,
                   (SELECT COUNT(*) FROM messages m
                      JOIN conversations cv ON cv.id = m.conversation_id
                     WHERE cv.customer_id = c.id) AS message_count
              FROM customers c
              {where_sql}
             ORDER BY c.last_active_at DESC, c.id DESC
             LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        ).fetchall()

    items = []
    for row in rows:
        item = _row_to_customer(row)
        last = row["last_message"] or ""
        item["last_message"] = last[:30]
        item["message_count"] = row["message_count"]
        items.append(item)
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def stats() -> dict:
    with read_conn() as conn:
        row = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM customers) AS total,
              (SELECT COUNT(*) FROM customers WHERE date(last_active_at) = date('now','localtime')) AS active_today,
              (SELECT COUNT(*) FROM customers WHERE intent_level = 'hot') AS hot,
              (SELECT COUNT(*) FROM customers
                WHERE date(created_at) >= date('now','localtime','-6 days')) AS new_this_week
            """
        ).fetchone()
    return {
        "total": row["total"],
        "active_today": row["active_today"],
        "hot": row["hot"],
        "new_this_week": row["new_this_week"],
    }


def auto_stage_by_rounds(customer_id: int, min_user_messages: int = 3) -> None:
    """new → contacted：产生 ≥ 3 轮对话。"""
    with write_conn() as conn:
        row = conn.execute("SELECT stage FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not row or row["stage"] != "new":
            return
        n = conn.execute(
            """
            SELECT COUNT(*) AS n FROM messages m
              JOIN conversations cv ON cv.id = m.conversation_id
             WHERE cv.customer_id = ? AND m.role = 'user'
            """,
            (customer_id,),
        ).fetchone()["n"]
        if n >= min_user_messages:
            conn.execute("UPDATE customers SET stage = 'contacted' WHERE id = ?", (customer_id,))


def delete(customer_id: int) -> bool:
    with write_conn() as conn:
        cur = conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    return cur.rowcount > 0
