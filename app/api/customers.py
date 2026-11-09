"""管理端客户接口：/api/customers*（需要 X-API-Key）。"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from app.db import conversations as conv_db
from app.db import customers as customers_db
from app.api.auth import require_admin
from app.core.errors import InvalidArgument, NotFound, Unprocessable

bp = Blueprint("customers_api", __name__, url_prefix="/api/customers")
logger = logging.getLogger("app.api.customers")


def _int_arg(name: str, default: int, low: int, high: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise InvalidArgument(f"{name} 必须是整数", field=name)
    if value < low or value > high:
        raise Unprocessable(f"{name} 超出范围（{low}~{high}）", field=name)
    return value


@bp.get("")
@require_admin
def list_customers():
    q = (request.args.get("q") or "").strip()
    stage = (request.args.get("stage") or "").strip() or None
    intent_level = (request.args.get("intent_level") or "").strip() or None
    if stage and stage not in customers_db.STAGES:
        raise Unprocessable(f"stage 必须是 {'/'.join(customers_db.STAGES)}", field="stage")
    if intent_level and intent_level not in customers_db.INTENT_LEVELS:
        raise Unprocessable(
            f"intent_level 必须是 {'/'.join(customers_db.INTENT_LEVELS)}", field="intent_level"
        )
    page = _int_arg("page", 1, 1, 100000)
    page_size = _int_arg("page_size", 20, 1, 100)

    result = customers_db.list_customers(q or None, stage, intent_level, page, page_size)
    result["stats"] = customers_db.stats()
    return jsonify(result)


@bp.get("/<int:customer_id>")
@require_admin
def get_customer(customer_id: int):
    customer = customers_db.get_by_id(customer_id)
    if not customer:
        raise NotFound("客户不存在")
    payload = dict(customer)
    payload["conversations"] = conv_db.list_for_customer(customer_id)
    payload["recent_messages"] = conv_db.recent_for_customer(customer_id, limit=50)
    return jsonify(payload)


@bp.get("/<int:customer_id>/contact")
@require_admin
def get_contact(customer_id: int):
    """「查看完整手机号」（审计用途，SDD 9.3 P1）：仅此接口返回明文，日志不落号码。"""
    customer = customers_db.get_by_id(customer_id, with_mask=False)
    if not customer:
        raise NotFound("客户不存在")
    logger.info("admin viewed phone", extra={"status": f"customer:{customer_id}"})
    return jsonify({"id": customer["id"], "phone": customer["phone"]})


@bp.patch("/<int:customer_id>")
@require_admin
def patch_customer(customer_id: int):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise InvalidArgument("请求体必须是 JSON 对象")

    allowed = {"stage", "intent_level", "tags", "note"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        raise InvalidArgument("至少提供一个字段：stage/intent_level/tags/note")

    if "stage" in fields and fields["stage"] not in customers_db.STAGES:
        raise Unprocessable(f"stage 必须是 {'/'.join(customers_db.STAGES)}", field="stage")
    if "intent_level" in fields and fields["intent_level"] not in customers_db.INTENT_LEVELS:
        raise Unprocessable(
            f"intent_level 必须是 {'/'.join(customers_db.INTENT_LEVELS)}",
            field="intent_level",
        )
    tags = None
    if "tags" in fields:
        raw_tags = fields["tags"]
        if not isinstance(raw_tags, list) or any(not isinstance(t, str) for t in raw_tags):
            raise Unprocessable("tags 必须是字符串数组", field="tags")
        tags = [t.strip() for t in raw_tags if t.strip()]
        if any(len(t) > 12 for t in tags):
            raise Unprocessable("单个标签不超过 12 字", field="tags")
        if len(tags) > 12:
            raise Unprocessable("标签最多 12 个", field="tags")
    note = None
    if "note" in fields:
        if fields["note"] is not None and not isinstance(fields["note"], str):
            raise Unprocessable("note 必须是字符串", field="note")
        note = (fields["note"] or "")[:500]

    updated = customers_db.update_fields(
        customer_id,
        stage=fields.get("stage"),
        intent_level=fields.get("intent_level"),
        tags=tags,
        note=note,
    )
    if not updated:
        raise NotFound("客户不存在")
    return jsonify(updated)


@bp.delete("/<int:customer_id>")
@require_admin
def delete_customer(customer_id: int):
    if not customers_db.get_by_id(customer_id):
        raise NotFound("客户不存在")
    customers_db.delete(customer_id)
    return jsonify({"deleted": True, "id": customer_id})
