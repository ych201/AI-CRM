"""意图识别：一次 DeepSeek 强制 tool_choice 调用 + 关键词降级。"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.prompts import INTENT_SYSTEM, ROUTE_INTENT_TOOL
from app.db.connection import read_conn
from app.llm.client import get_llm

logger = logging.getLogger("app.intent")

DEFAULT_INTENTS: dict[int, dict] = {
    0: {"name": "闲聊/其他", "route": "direct"},
    1: {"name": "职业咨询", "route": "rag_market"},
    2: {"name": "课程咨询", "route": "rag_course"},
    3: {"name": "实时资讯", "route": "web_search"},
    4: {"name": "数据查询", "route": "sql"},
}

# 关键词降级（顺序即优先级）
_SQL_WORDS = ("我上次", "我的记录", "几个客户", "多少客户", "新增了几", "统计", "这周有几个")
_REALTIME_WORDS = ("最新", "今年", "行情", "政策", "新闻", "薪资", "工资", "趋势", "就业率")
_COURSE_WORDS = (
    "价格",
    "多少钱",
    "学费",
    "费用",
    "收费",
    "报价",
    "报名",
    "课程",
    "课时",
    "老师",
    "师资",
    "优惠",
    "活动价",
    "团报",
    "分期",
)
_CAREER_WORDS = (
    "转行",
    "想转",
    "转岗",
    "岗位",
    "前景",
    "要求",
    "简历",
    "面试",
    "能不能做",
    "能不能入",
    "入行",
    "入门",
    "学习路径",
    "零基础",
)

_cache: dict[int, dict] | None = None


@dataclass
class IntentResult:
    code: int
    name: str
    route: str
    confidence: float
    reason: str
    source: str = "llm"
    low_confidence: bool = False


def _load_intents() -> dict[int, dict]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with read_conn() as conn:
            rows = conn.execute("SELECT code, name, route FROM intents WHERE enabled = 1").fetchall()
        data = {int(r["code"]): {"name": r["name"], "route": r["route"]} for r in rows}
        _cache = data or dict(DEFAULT_INTENTS)
    except Exception:  # noqa: BLE001  表未初始化时兜底
        _cache = dict(DEFAULT_INTENTS)
    return _cache


def reset_cache() -> None:
    global _cache
    _cache = None


def rule_classify(query: str) -> tuple[int, str]:
    text = query or ""
    if any(w in text for w in _SQL_WORDS):
        return 4, "命中数据查询关键词"
    has_course = any(w in text for w in _COURSE_WORDS)
    has_realtime = any(w in text for w in _REALTIME_WORDS)
    if has_course and not any(w in text for w in ("行情", "政策", "新闻", "薪资", "工资", "趋势")):
        return 2, "命中课程关键词"
    if has_realtime:
        return 3, "命中实时资讯关键词"
    if has_course:
        return 2, "命中课程关键词"
    if any(w in text for w in _CAREER_WORDS):
        return 1, "命中职业咨询关键词"
    return 0, "未命中关键词"


def _result(code: int, confidence: float, reason: str, source: str) -> IntentResult:
    meta = _load_intents().get(code) or DEFAULT_INTENTS[0]
    return IntentResult(
        code=code,
        name=meta["name"],
        route=meta["route"],
        confidence=confidence,
        reason=reason,
        source=source,
    )


def classify(
    query: str,
    history_summary: str = "",
    profile_summary: str = "暂无",
) -> IntentResult:
    prompt = f"客户信息：{profile_summary}\n最近对话：{history_summary or '无'}\n用户这句话：{query}"
    try:
        resp = get_llm().chat(
            [
                {"role": "system", "content": INTENT_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            tools=[ROUTE_INTENT_TOOL],
            tool_choice={"type": "function", "function": {"name": "route_intent"}},
            temperature=0.0,
            max_tokens=80,
        )
        for call in resp.tool_calls:
            if call.name != "route_intent":
                continue
            args = call.arguments or {}
            code = args.get("intent_code")
            confidence = args.get("confidence")
            if not isinstance(code, int) or code not in (0, 1, 2, 3, 4):
                break
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.0
            reason = str(args.get("reason") or "")[:60]
            result = _result(code, confidence, reason, "llm")
            if confidence < 0.55:
                low = _result(0, confidence, f"置信度低({confidence:.2f})，转直答", "llm")
                low.low_confidence = True
                return low
            return result
        raise ValueError("意图工具未返回合法参数")
    except Exception as exc:  # noqa: BLE001
        code, reason = rule_classify(query)
        logger.warning(
            "intent fallback to rules",
            extra={"error": str(exc), "intent_code": code},
        )
        return _result(code, 0.6, reason, "rule")
