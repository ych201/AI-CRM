"""画像 → 回答策略（SDD 7.2 回答策略叠加）。"""
from __future__ import annotations

PROFILE_LABELS = {
    "current_role": "身份",
    "years_exp": "经验",
    "goal": "目标",
    "budget": "预算",
    "time_window": "时间窗",
    "city": "城市",
    "concerns": "顾虑",
}


def profile_summary(profile: dict | None, name: str | None = None) -> str:
    if not profile and not name:
        return "暂无"
    parts = []
    if name:
        parts.append(f"称呼={name}")
    for key, label in PROFILE_LABELS.items():
        value = (profile or {}).get(key)
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            value = "、".join(str(v) for v in value)
        parts.append(f"{label}={value}")
    return "；".join(parts) if parts else "暂无"


def strategy_for(customer: dict) -> str:
    stage = customer.get("stage") or "new"
    level = customer.get("intent_level") or "cold"
    profile = customer.get("profile") or {}

    if stage in ("won", "lost"):
        return "语气克制，只答不问，不推销，不主动引导下一步。"
    if stage == "qualified" or level == "hot":
        return (
            "直接给结论与下一步动作（试听或报名入口），不重复基础介绍，一句话收尾。"
            "不要再询问背景或预算。"
        )
    if stage == "contacted" and level in ("cold", "warm"):
        return (
            "结合已知背景作答；给出 2 个具体选项让对方选；结尾问预算或时间窗。"
        )
    if stage == "new" and not profile:
        return (
            "先用一句话确认对方处境，再给信息；末尾必带一个开放式提问；不报价、不催单。"
        )
    return "结合已知背景作答；先用一句话确认处境，再给信息，末尾带一个开放式提问。"


def build_system_prompt(
    customer: dict, strategy_instruction: str, context: str | None = None
) -> str:
    from app.core.prompts import ANSWER_SYSTEM, CONTEXT_BLOCK

    text = ANSWER_SYSTEM.format(
        stage=customer.get("stage", "new"),
        intent_level=customer.get("intent_level", "cold"),
        profile_summary=profile_summary(customer.get("profile"), customer.get("name")),
        strategy_instruction=strategy_instruction,
    )
    if context:
        text += "\n\n" + context
    return text
