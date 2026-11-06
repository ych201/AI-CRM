"""kb_documents 表读写（向量本体在 Chroma）。"""
from __future__ import annotations

from typing import Any

from app.db.connection import read_conn, write_conn

COLLECTIONS = ("market", "course", "general")


def _row_to_doc(row: Any) -> dict:
    return {
        "doc_id": row["id"],
        "collection": row["collection"],
        "title": row["title"],
        "content": row["content"],
        "source": row["source"],
        "chunk_count": row["chunk_count"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get(doc_id: str) -> dict | None:
    with read_conn() as conn:
        row = conn.execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,)).fetchone()
    return _row_to_doc(row) if row else None


def exists(doc_id: str) -> bool:
    return get(doc_id) is not None


def upsert(
    doc_id: str,
    collection: str,
    title: str,
    content: str,
    source: str = "manual",
    chunk_count: int = 0,
    enabled: bool = True,
) -> dict:
    with write_conn() as conn:
        conn.execute(
            """
            INSERT INTO kb_documents (id, collection, title, content, source, chunk_count, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              collection  = excluded.collection,
              title       = excluded.title,
              content     = excluded.content,
              source      = excluded.source,
              chunk_count = excluded.chunk_count,
              enabled     = excluded.enabled,
              updated_at  = datetime('now','localtime')
            """,
            (doc_id, collection, title, content, source, chunk_count, 1 if enabled else 0),
        )
        row = conn.execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,)).fetchone()
    return _row_to_doc(row)


def update_chunk_count(doc_id: str, chunk_count: int, enabled: bool = True) -> None:
    with write_conn() as conn:
        conn.execute(
            """
            UPDATE kb_documents
               SET chunk_count = ?, enabled = ?, updated_at = datetime('now','localtime')
             WHERE id = ?
            """,
            (chunk_count, 1 if enabled else 0, doc_id),
        )


def set_enabled(doc_id: str, enabled: bool) -> None:
    with write_conn() as conn:
        conn.execute(
            "UPDATE kb_documents SET enabled = ? WHERE id = ?", (1 if enabled else 0, doc_id)
        )


def delete(doc_id: str) -> bool:
    with write_conn() as conn:
        cur = conn.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
    return cur.rowcount > 0


def list_documents(
    collection: str | None = None,
    include_disabled: bool = False,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    where = []
    params: list[Any] = []
    if collection:
        where.append("collection = ?")
        params.append(collection)
    if not include_disabled:
        where.append("enabled = 1")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * page_size
    with read_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM kb_documents {where_sql}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM kb_documents {where_sql} ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
    items = []
    for row in rows:
        doc = _row_to_doc(row)
        doc.pop("content", None)  # 列表不返回全文
        items.append(doc)
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def all_enabled(collection: str | None = None) -> list[dict]:
    with read_conn() as conn:
        if collection:
            rows = conn.execute(
                "SELECT * FROM kb_documents WHERE enabled = 1 AND collection = ? ORDER BY updated_at DESC",
                (collection,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM kb_documents WHERE enabled = 1 ORDER BY updated_at DESC"
            ).fetchall()
    return [_row_to_doc(r) for r in rows]


def count() -> int:
    with read_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM kb_documents").fetchone()["n"]
