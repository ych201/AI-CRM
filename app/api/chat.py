"""客户端接口：/api/session、/api/chat（SSE）。"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading

from flask import Blueprint, Response, jsonify, request, stream_with_context

from app.config import get_config
from app.core import pipeline, ratelimit
from app.core.errors import InvalidArgument, RateLimited, Unprocessable

logger = logging.getLogger("app.api.chat")

bp = Blueprint("chat_api", __name__, url_prefix="/api")

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_SENTINEL = object()


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise InvalidArgument("请求体必须是 JSON 对象")
    return data


def _phone(data: dict) -> str:
    phone = str(data.get("phone") or "").strip()
    if not phone:
        raise InvalidArgument("phone 不能为空", field="phone")
    if not PHONE_RE.match(phone):
        raise InvalidArgument("phone 格式不正确", field="phone")
    return phone


def format_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def event_stream(session: pipeline.ChatStream):
    """后台线程产出事件，主线程负责保活与格式化为 SSE。"""
    box: queue.Queue = queue.Queue()

    def produce() -> None:
        try:
            for event in session.run():
                box.put(event)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sse producer crashed")
            box.put(
                ("error", {"code": "INTERNAL", "message": "服务异常，请稍后重试", "retryable": True})
            )
        finally:
            box.put(_SENTINEL)

    worker = threading.Thread(target=produce, name="sse-producer", daemon=True)
    worker.start()

    try:
        while True:
            try:
                item = box.get(timeout=15)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            if item is _SENTINEL:
                break
            event, data = item
            yield format_sse(event, data)
    finally:
        session.cancel()


@bp.post("/session")
def create_session():
    data = _json_body()
    phone = _phone(data)
    payload = pipeline.start_session(phone)
    return jsonify(payload)


@bp.post("/chat")
def chat():
    data = _json_body()
    phone = _phone(data)

    message = data.get("message")
    if not isinstance(message, str):
        raise InvalidArgument("message 不能为空", field="message")
    message = message.strip()
    if not message:
        raise Unprocessable("message 不能为空", field="message")
    if len(message) > get_config().max_message_len:
        raise Unprocessable(
            f"message 不能超过 {get_config().max_message_len} 字符", field="message"
        )

    conversation_id = data.get("conversation_id")
    if conversation_id is not None:
        try:
            conversation_id = int(conversation_id)
        except (TypeError, ValueError):
            raise InvalidArgument("conversation_id 必须是整数", field="conversation_id")

    ok, wait = ratelimit.check_chat(phone)
    if not ok:
        raise RateLimited(f"请求过于频繁，请 {wait} 秒后再试")

    session = pipeline.start_chat(phone, conversation_id, message)
    resp = Response(
        stream_with_context(event_stream(session)),
        mimetype="text/event-stream",
    )
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp


@bp.get("/customer-card")
def customer_card():
    """聊天页刷新客户信息卡用（手机号即身份，与管理端接口隔离）。"""
    phone = (request.args.get("phone") or "").strip()
    if not PHONE_RE.match(phone):
        raise InvalidArgument("phone 格式不正确", field="phone")
    with_messages = (request.args.get("with_messages") or "") in ("1", "true", "yes")
    card = pipeline.customer_card(phone, with_messages=with_messages)
    if card is None:
        raise InvalidArgument("客户不存在", field="phone")
    return jsonify(card)
