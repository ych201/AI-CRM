"""工具定义、工具执行与 Agent 循环（function calling）。"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.config import get_config
from app.core.errors import SQLRejected
from app.core.retriever import citations_from_web, retrieve
from app.db import customers as customers_db
from app.db.connection import connect
from app.llm.client import get_llm

logger = logging.getLogger("app.agents")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "在本地知识库中检索资料。当需要课程信息、价格、师资、就业数据或行业背景时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "collection": {"type": "string", "enum": ["market", "course", "general"]},
                    "query": {"type": "string", "description": "检索语句，用用户原话或改写后的关键词"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 8, "default": 4},
                },
                "required": ["collection", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索实时信息。仅在问题涉及最新行情、政策、新闻且知识库无法回答时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 120},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_crm_db",
            "description": "用只读 SQL 查询 CRM 数据库。可查 customers、conversations、messages、kb_documents、intents。只允许单条 SELECT。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "单条 SELECT 语句，不要带分号"},
                    "purpose": {"type": "string", "description": "一句话说明查询目的"},
                },
                "required": ["sql", "purpose"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_customer_profile",
            "description": "当用户在对话中透露个人信息（身份、目标、预算、时间、顾虑）时，更新客户画像。只在信息明确时调用，不要推测。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "object",
                        "description": "要写入的字段，键必须在白名单内",
                        "properties": {
                            "name": {"type": "string"},
                            "current_role": {"type": "string"},
                            "years_exp": {"type": "string"},
                            "goal": {"type": "string"},
                            "budget": {"type": "string"},
                            "time_window": {"type": "string"},
                            "city": {"type": "string"},
                            "concerns": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "intent_level": {"type": "string", "enum": ["cold", "warm", "hot"]},
                    "reason": {"type": "string"},
                },
                "required": ["fields"],
            },
        },
    },
]

TOOLS_BY_NAME = {t["function"]["name"]: t for t in TOOLS}

ROUTE_TOOLS = {
    "web_search": ["web_search"],
    "sql": ["query_crm_db"],
    "direct": [],
}


def tools_for_route(route: str) -> list[dict]:
    names = ROUTE_TOOLS.get(route, [])
    return [TOOLS_BY_NAME[n] for n in names]


# ---------------------------------------------------------------- SQL 守卫
ALLOWED_TABLES = {"customers", "conversations", "messages", "kb_documents", "intents"}
MAX_ROWS = 100
SQL_TIMEOUT_MS = 5000

_FORBIDDEN = re.compile(
    r"\b(PRAGMA|ATTACH|DETACH|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|VACUUM|TRIGGER|REINDEX)\b",
    re.IGNORECASE,
)
_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def validate_sql(sql: str) -> str:
    text = (sql or "").strip()
    if not text:
        raise SQLRejected("SQL 为空")
    if ";" in text:
        raise SQLRejected("仅允许单条语句，不要带分号")
    if not re.match(r"^\s*(SELECT|WITH)\b", text, re.IGNORECASE):
        raise SQLRejected("仅允许 SELECT/WITH 查询")
    if "--" in text or "/*" in text:
        raise SQLRejected("不允许 SQL 注释")
    if _FORBIDDEN.search(text):
        raise SQLRejected("SQL 含禁止的关键字（写操作或 PRAGMA）")
    tables = {t.lower() for t in _TABLE_REF.findall(text)}
    if not tables:
        raise SQLRejected("未识别到查询表，请使用业务表")
    cte_names = {
        name.lower()
        for name in re.findall(
            r"(?:WITH|,)\s+([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", text, re.IGNORECASE
        )
    }
    illegal = tables - ALLOWED_TABLES - cte_names
    if illegal:
        raise SQLRejected(f"仅允许查询 {', '.join(sorted(ALLOWED_TABLES))} 等业务表")
    return text


def _make_authorizer(deadline: float):
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }

    def authorizer(action: int, arg1: Any, arg2: Any, dbname: Any, trigger: Any) -> int:
        if time.monotonic() > deadline:
            return sqlite3.SQLITE_DENY
        if action in allowed:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table = str(arg1 or "").lower()
            if table in ALLOWED_TABLES:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    return authorizer


def _mask_value(column: str, value: Any) -> Any:
    if isinstance(value, str) and "phone" in column.lower():
        return customers_db.mask_phone(value)
    return value


def run_sql(sql: str) -> dict:
    """只读执行单条 SELECT，行数上限 100，phone 脱敏。"""
    statement = validate_sql(sql)
    deadline = time.monotonic() + SQL_TIMEOUT_MS / 1000.0
    conn = connect(readonly=True)
    try:
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        conn.set_authorizer(_make_authorizer(deadline))
        cur = conn.execute(statement)
        columns = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        data = [
            [_mask_value(col, row[idx]) for idx, col in enumerate(columns)]
            for row in rows
        ]
        return {
            "columns": columns,
            "rows": data,
            "row_count": len(data),
            "truncated": truncated,
        }
    except sqlite3.Error as exc:
        raise SQLRejected(f"SQL 执行失败：{exc}") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------- 工具实现
def _tool_search_knowledge_base(args: dict) -> dict:
    collection = args.get("collection")
    query = str(args.get("query") or "").strip()
    if collection not in ("market", "course", "general"):
        return {"error": "INVALID_COLLECTION", "hits": []}
    if not query:
        return {"error": "EMPTY_QUERY", "hits": []}
    top_k = int(args.get("top_k") or 4)
    top_k = max(1, min(top_k, 8))
    hits = retrieve(collection, query, top_k=top_k)
    return {
        "hits": [
            {
                "title": h["title"],
                "snippet": h["content"][:200],
                "score": h["score"],
                "doc_id": h["doc_id"],
            }
            for h in hits
        ]
    }


def _tool_web_search(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "EMPTY_QUERY", "results": []}
    max_results = max(1, min(int(args.get("max_results") or 3), 5))
    cfg = get_config()
    if not cfg.tavily_api_key:
        return {
            "error": "TAVILY_NOT_CONFIGURED",
            "message": "未配置 TAVILY_API_KEY，无法联网检索",
            "results": [],
        }
    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=cfg.tavily_api_key)
        resp = client.search(query=query, search_depth="basic", max_results=max_results)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tavily search failed", extra={"error": str(exc)})
        return {"error": "TAVILY_FAILED", "message": str(exc)[:200], "results": []}
    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": (r.get("content") or "")[:400],
        }
        for r in (resp.get("results") or [])
    ]
    return {"results": results}


def _tool_query_crm_db(args: dict) -> dict:
    sql = str(args.get("sql") or "")
    try:
        return run_sql(sql)
    except SQLRejected as exc:
        return {"error": "SQL_REJECTED", "reason": exc.reason}


def _tool_update_customer_profile(args: dict, customer_id: int) -> dict:
    fields = args.get("fields") or {}
    if not isinstance(fields, dict):
        return {"ok": False, "error": "INVALID_FIELDS"}
    level = args.get("intent_level")
    updated = customers_db.merge_profile(customer_id, fields, level)
    if updated is None:
        return {"ok": False, "error": "CUSTOMER_NOT_FOUND"}
    return {"ok": True, "updated": updated.get("_changed", [])}


# ---------------------------------------------------------------- 执行器
@dataclass
class ToolContext:
    customer_id: int
    conversation_id: int
    citations: list[dict] = field(default_factory=list)
    last_sql_result: dict | None = None


def _with_timeout(fn, timeout: float, *args, **kwargs):
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            raise TimeoutError(f"工具执行超时（{timeout}s）") from exc


def _with_retry(fn, timeout: float, args: dict) -> dict:
    """只读工具失败重试 1 次（SDD 7.3 工具执行契约）。"""
    try:
        return _with_timeout(fn, timeout, args)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool retry", extra={"error": str(exc)})
        time.sleep(1.0)
        return _with_timeout(fn, timeout, args)


def execute_tool(name: str, args: dict, ctx: ToolContext) -> dict:
    """白名单分派；异常统一转成 {"error": ...} 回填给模型。"""
    timeout = get_config().tool_timeouts.get(name, 5.0)
    if name == "search_knowledge_base":
        result = _with_retry(_tool_search_knowledge_base, timeout, args)
        hits = result.get("hits") or []
        for h in hits:
            ctx.citations.append(
                {
                    "doc_id": h.get("doc_id", ""),
                    "title": h.get("title", ""),
                    "score": h.get("score"),
                    "snippet": (h.get("snippet") or "")[:60],
                }
            )
        return result
    if name == "web_search":
        result = _with_retry(_tool_web_search, timeout, args)
        results = result.get("results") or []
        if results:
            ctx.citations.extend(citations_from_web(results))
        return result
    if name == "query_crm_db":
        result = _with_timeout(_tool_query_crm_db, timeout, args)
        ctx.last_sql_result = result
        return result
    if name == "update_customer_profile":
        return _with_timeout(_tool_update_customer_profile, timeout, args, ctx.customer_id)
    return {"error": f"UNKNOWN_TOOL: {name}"}


# ---------------------------------------------------------------- Agent 循环
def _assistant_tool_message(content: str, calls) -> dict:
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.name,
                    "arguments": json.dumps(c.arguments or {}, ensure_ascii=False),
                },
            }
            for c in calls
        ],
    }


def _iter_text(text: str, size: int = 10) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i : i + size]


def dedupe_citations(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for item in items:
        key = item.get("doc_id") or item.get("title")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def run_agent(
    messages: list[dict],
    customer_id: int,
    conversation_id: int,
    route: str,
    max_steps: int | None = None,
) -> Iterator[tuple[str, Any]]:
    """工具调用循环：产出 ("delta", text) / ("tool", info)。

    最后一轮回答直接使用非流式结果切片下发，避免多一次生成（首字仍即刻可见）。
    """
    cfg = get_config()
    steps = max_steps or cfg.max_tool_steps
    tools = tools_for_route(route)
    llm = get_llm()
    ctx = ToolContext(customer_id=customer_id, conversation_id=conversation_id)
    usage = {"in": 0, "out": 0}

    if not tools:
        for delta in llm.stream(messages, temperature=0.6, usage_out=usage):
            yield "delta", delta
        yield "usage", usage
        yield "citations", dedupe_citations(ctx.citations)
        return

    for step in range(steps):
        resp = llm.chat(messages, tools=tools, tool_choice="auto", temperature=0.6)
        usage["in"] += resp.tokens_in
        usage["out"] += resp.tokens_out

        if not resp.tool_calls:
            text = resp.content
            if not text:
                for delta in llm.stream(messages, temperature=0.6, usage_out=usage):
                    yield "delta", delta
            else:
                for piece in _iter_text(text):
                    yield "delta", piece
            yield "usage", usage
            yield "citations", dedupe_citations(ctx.citations)
            return

        messages.append(_assistant_tool_message(resp.content, resp.tool_calls))
        for call in resp.tool_calls:
            started = time.monotonic()
            try:
                result = execute_tool(call.name, call.arguments or {}, ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning("tool failed", extra={"error": str(exc)})
                result = {"error": "TOOL_FAILED", "message": str(exc)[:200]}
            elapsed = int((time.monotonic() - started) * 1000)
            yield "tool", {
                "name": call.name,
                "args": call.arguments or {},
                "ms": elapsed,
            }
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False)[:6000],
                }
            )

    messages.append(
        {"role": "system", "content": "工具调用已达上限，请基于已有信息直接给出回答，不要再调用工具。"}
    )
    for delta in llm.stream(messages, temperature=0.6, usage_out=usage):
        yield "delta", delta
    yield "usage", usage
    yield "citations", dedupe_citations(ctx.citations)
