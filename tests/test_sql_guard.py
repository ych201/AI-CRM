"""SQL Agent 安全约束。"""
from __future__ import annotations

import pytest

from app.core.agents import run_sql, validate_sql
from app.core.errors import SQLRejected
from app.db import customers as customers_db


def test_accepts_single_select():
    assert validate_sql("SELECT COUNT(*) FROM customers").startswith("SELECT")


def test_accepts_with_clause():
    sql = "WITH t AS (SELECT id FROM customers) SELECT COUNT(*) FROM t"
    assert validate_sql(sql) == sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers; DROP TABLE customers",
        "DELETE FROM customers",
        "UPDATE customers SET stage='won'",
        "PRAGMA table_info(customers)",
        "SELECT * FROM sqlite_master",
        "ATTACH DATABASE 'x.db' AS x",
        "SELECT * FROM customers WHERE 1=1 -- 注释",
    ],
)
def test_rejects_dangerous_sql(sql):
    with pytest.raises(SQLRejected):
        validate_sql(sql)


def test_readonly_connection_blocks_writes(app):
    with pytest.raises(SQLRejected):
        run_sql("SELECT 1; ")


def test_masks_phone_in_results(app):
    customers_db.create("13800138000")
    result = run_sql("SELECT phone, stage FROM customers")
    assert result["row_count"] == 1
    assert result["rows"][0][0] == "138****8000"
    assert "columns" in result and result["truncated"] is False


def test_limits_rows_to_100(app):
    from app.db.connection import write_conn

    with write_conn() as conn:
        conn.executemany(
            "INSERT INTO customers (phone) VALUES (?)",
            [(f"1390000{i:04d}",) for i in range(120)],
        )
    result = run_sql("SELECT id FROM customers")
    assert result["row_count"] == 100
    assert result["truncated"] is True


def test_unknown_table_rejected(app):
    with pytest.raises(SQLRejected):
        run_sql("SELECT name FROM sqlite_temp_master")
