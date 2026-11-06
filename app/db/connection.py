"""SQLite 连接与建表。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import get_config

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def _db_path() -> Path:
    return get_config().sqlite_path


def _configure(conn: sqlite3.Connection, readonly: bool) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")


def connect(readonly: bool = False) -> sqlite3.Connection:
    """普通读写连接。调用方负责 commit / close（或用 conn() 上下文）。"""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    else:
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
    _configure(conn, readonly)
    return conn


@contextmanager
def read_conn() -> Iterator[sqlite3.Connection]:
    conn = connect(readonly=True)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_conn() -> Iterator[sqlite3.Connection]:
    conn = connect(readonly=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(verbose: bool = False) -> dict[str, int]:
    """执行 schema.sql（幂等），返回各表行数。"""
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with write_conn() as conn:
        conn.executescript(sql)
        tables = ("customers", "conversations", "messages", "kb_documents", "intents")
        counts = {}
        for table in tables:
            counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    if verbose:
        print("[init_db] sqlite:", _db_path())
        for table, n in counts.items():
            print(f"  - {table:14s} {n:>6d} rows")
    return counts
