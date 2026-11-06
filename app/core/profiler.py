"""客户画像抽取与合并（后台线程）。"""
from __future__ import annotations

import json
import logging
import re
import threading

from app.core.prompts import PROFILE_SYSTEM, PROFILE_TOOL
from app.db import conversations as conv_db
from app.db import customers as customers_db
from app.llm.client import get_llm

logger = logging.getLogger("app.profiler")

_ROLE_RE = re.compile(r"(?:我现在|我目前|我是|我做|我干)(?:做|是)?([\u4e00-\u9fa5A-Za-z]{2,10}?)(?:的|，|,|。|；|;|$|想|打算|准备)")
_GOAL_RE = re.compile(r"(?:想|打算|准备)(?:转|做|学|往)([\u4e00-\u9fa5A-Za-z0-9 ]{1,16})")
_BUDGET_RE = re.compile(r"预算([^，,。；;]{1,16})")
_MONEY_RE = re.compile(r"([0-9一二三四五六七八九十]+(?:\.[0-9]+)?万(?:以内|以下|左右|内)?)")
_TIME_RE = re.compile(r"([0-9一二三四五六七八九十]+个?月(?:内|以内)|半年内|明年|今年内)")
_YEARS_RE = re.compile(r"([0-9]+)\s*年(?:工作)?经验")
_CONCERN_RE = re.compile(r"(?:担心|怕|顾虑)([^，,。；;]{1,20})")


def extract_with_rules(text: str) -> tuple[dict, str | None]:
    fields: dict = {}
    role = _ROLE_RE.search(text)
    if role:
        fields["current_role"] = role.group(1)

    goal = _GOAL_RE.search(text)
    if goal:
        fields["goal"] = goal.group(1).strip()
    budget = _BUDGET_RE.search(text)
    if budget:
        fields["budget"] = budget.group(1).strip()
    else:
        money = _MONEY_RE.search(text)
        if money:
            fields["budget"] = money.group(1)

    window = _TIME_RE.search(text)
    if window:
        fields["time_window"] = window.group(1)
    years = _YEARS_RE.search(text)
    if years:
        fields["years_exp"] = years.group(1)
    concern = _CONCERN_RE.search(text)
    if concern:
        fields["concerns"] = [concern.group(1).strip()]

    level = None
    if fields.get("goal") and (fields.get("budget") or fields.get("time_window")):
        level = "hot"
    elif fields.get("goal"):
        level = "warm"
    return fields, level


def _llm_extract(messages: list[dict]) -> tuple[dict, str | None]:
    resp = get_llm().chat(
        [{"role": "system", "content": PROFILE_SYSTEM}, *messages],
        tools=[PROFILE_TOOL],
        tool_choice={"type": "function", "function": {"name": "update_customer_profile"}},
        temperature=0.0,
        max_tokens=300,
    )
    for call in resp.tool_calls:
        if call.name != "update_customer_profile":
            continue
        args = call.arguments or {}
        fields = args.get("fields") or {}
        level = args.get("intent_level")
        if isinstance(fields, dict):
            return fields, level if level in ("cold", "warm", "hot") else None
    return {}, None


def run_extraction(customer_id: int, conversation_id: int) -> dict | None:
    recent = conv_db.list_messages(conversation_id, limit=4)
    if not recent:
        return None
    payload = [{"role": m["role"], "content": m["content"]} for m in recent if m["role"] in ("user", "assistant")]
    user_text = " ".join(m["content"] for m in recent if m["role"] == "user")

    fields: dict = {}
    level: str | None = None
    try:
        fields, level = _llm_extract(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("profile llm extract failed", extra={"error": str(exc)})

    rule_fields, rule_level = extract_with_rules(user_text)
    for key, value in rule_fields.items():
        fields.setdefault(key, value)
    if not level:
        level = rule_level

    if not fields and not level:
        return None

    updated = customers_db.merge_profile(customer_id, fields, level)
    if updated:
        customers_db.auto_stage_by_rounds(customer_id)
        logger.info(
            "profile updated",
            extra={"intent_code": None, "status": json.dumps(updated.get("_changed", []), ensure_ascii=False)},
        )
    return updated


def _safe_extract(customer_id: int, conversation_id: int) -> None:
    try:
        run_extraction(customer_id, conversation_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("profile extraction crashed", extra={"error": str(exc)})


def schedule(customer_id: int, conversation_id: int) -> None:
    threading.Thread(
        target=_safe_extract,
        args=(customer_id, conversation_id),
        name=f"profiler-{customer_id}",
        daemon=True,
    ).start()
