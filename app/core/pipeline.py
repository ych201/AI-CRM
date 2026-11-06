"""对话编排：装载会话 → 意图识别 → 分流 → 流式生成 → 落库 → 后台画像。"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from typing import Iterator

from app.config import get_config
from app.core import profiler
from app.core.agents import dedupe_citations, run_agent
from app.core.errors import ApiError, NotFound, UpstreamUnavailable
from app.core.intent import classify
from app.core.prompts import (
    EMPTY_ANSWER_FALLBACK,
    GREETING_LLM_SYSTEM,
    GREETING_NEW,
    GREETING_NEW_WITH_PROFILE,
    GREETING_RETURNING,
    RAG_MISS_INSTRUCTION,
    TRUNCATED_SUFFIX,
)
from app.core.retriever import build_context, citations_from_hits, retrieve
from app.core.strategies import build_system_prompt, profile_summary, strategy_for
from app.db import conversations as conv_db
from app.db import customers as customers_db
from app.llm.client import get_llm

logger = logging.getLogger("app.pipeline")

RAG_COLLECTION = {"rag_market": "market", "rag_course": "course"}


def _clean(text: str, limit: int = 2000) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def phone_hash(phone: str) -> str:
    """日志中的手机号指纹（SDD 9.5）。"""
    return hashlib.sha256((phone or "").encode("utf-8")).hexdigest()[:8]


def build_greeting(customer: dict) -> str:
    cfg = get_config()
    profile = customer.get("profile") or {}
    last = conv_db.last_assistant_message(customer["id"])
    if last and last.get("content"):
        full = _clean(last["content"], 400)
        snippet = full[:20] + ("…" if len(full) > 20 else "")
        base = GREETING_RETURNING.format(last=snippet)
    elif profile:
        base = GREETING_NEW_WITH_PROFILE.format(profile_hint=profile_summary(profile))
    else:
        base = GREETING_NEW

    if cfg.greeting_use_llm and last and cfg.llm_configured:
        try:
            resp = get_llm().chat(
                [
                    {"role": "system", "content": GREETING_LLM_SYSTEM},
                    {
                        "role": "user",
                        "content": f"客户最近一句话：{_clean(last['content'], 60)}\n客户信息：{profile_summary(profile)}",
                    },
                ],
                temperature=0.5,
                max_tokens=60,
                timeout=5.0,
            )
            if resp.content:
                base = resp.content.strip()[:80]
        except Exception as exc:  # noqa: BLE001
            logger.warning("greeting llm failed", extra={"error": str(exc)})
    return base


def start_session(phone: str) -> dict:
    cfg = get_config()
    customer, is_new = customers_db.get_or_create(phone)
    conversation, conv_is_new = conv_db.get_or_create(customer["id"], cfg.conversation_reuse_hours)
    greeting = build_greeting(customer)
    if conv_is_new:
        try:
            conv_db.add_message(
                conversation["id"], "assistant", greeting, intent_code=0, route="direct"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("greeting persist failed", extra={"error": str(exc)})
    return {
        "customer": customer,
        "conversation_id": conversation["id"],
        "is_new": is_new,
        "greeting": greeting,
    }


class ChatStream:
    """一次对话的 SSE 事件源。"""

    def __init__(
        self, customer: dict, conversation: dict, message: str, phone_hash_value: str = ""
    ) -> None:
        self.customer = customer
        self.conversation = conversation
        self.message = message
        self.phone_hash = phone_hash_value
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        self.cancelled.set()

    # ------------------------------------------------------------------
    def _persist(
        self,
        content: str,
        intent_code: int | None,
        route: str | None,
        citations: list[dict],
        latency_ms: int,
        usage: dict,
        suffix: str = "",
    ) -> int | None:
        text = (content or "").strip() + suffix
        if not text.strip():
            return None
        try:
            return conv_db.add_message(
                self.conversation["id"],
                "assistant",
                text,
                intent_code=intent_code,
                route=route,
                citations=citations,
                latency_ms=latency_ms,
                tokens_in=usage.get("in") or None,
                tokens_out=usage.get("out") or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("message persist failed", extra={"error": str(exc)})
            return None

    # ------------------------------------------------------------------
    def run(self) -> Iterator[tuple[str, dict]]:
        cfg = get_config()
        started = time.monotonic()
        customer = self.customer
        conv_id = self.conversation["id"]
        parts: list[str] = []
        citations: list[dict] = []
        usage: dict = {"in": 0, "out": 0}
        intent = None
        route = "direct"
        persisted_id: int | None = None
        persisted = False

        try:
            user_msg_id = conv_db.add_message(conv_id, "user", self.message)
            customers_db.touch(customer["id"])

            history = [
                m
                for m in conv_db.history(conv_id, limit=cfg.history_window + 1)
                if m["id"] != user_msg_id
            ][-cfg.history_window :]

            summary = " | ".join(
                f"{'客户' if m['role'] == 'user' else '小海'}: {_clean(m['content'], 30)}"
                for m in history[-4:]
            )
            intent = classify(
                self.message,
                summary,
                profile_summary(customer.get("profile"), customer.get("name")),
            )
            route = intent.route

            context = None
            extra_instruction = ""
            if route in RAG_COLLECTION:
                try:
                    hits = retrieve(RAG_COLLECTION[route], self.message)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("retrieve failed", extra={"error": str(exc)})
                    hits = []
                if hits:
                    context = build_context(hits)
                    citations = citations_from_hits(hits)
                else:
                    route = "rag_miss"
                    extra_instruction = RAG_MISS_INSTRUCTION

            system_prompt = build_system_prompt(customer, strategy_for(customer), context)
            if extra_instruction:
                system_prompt += "\n\n" + extra_instruction

            yield "meta", {
                "conversation_id": conv_id,
                "message_id": None,
                "intent_code": intent.code,
                "intent_name": intent.name,
                "route": route,
                "customer": {
                    "stage": customer.get("stage"),
                    "intent_level": customer.get("intent_level"),
                },
                "intent_low_confidence": intent.low_confidence,
            }

            llm_messages: list[dict] = [{"role": "system", "content": system_prompt}]
            first = conv_db.first_user_message(conv_id)
            if first and all(m["id"] != first["id"] for m in history):
                # 超出历史窗口的内容只保留首条需求的摘要，防止上下文膨胀（SDD 7.6）
                llm_messages.append(
                    {"role": "system", "content": f"客户最初的需求：{_clean(first['content'], 60)}"}
                )
            for m in history:
                if m["role"] in ("user", "assistant") and m.get("content"):
                    llm_messages.append({"role": m["role"], "content": _clean(m["content"])})
            llm_messages.append({"role": "user", "content": self.message})

            if route in ("web_search", "sql"):
                for kind, payload in run_agent(llm_messages, customer["id"], conv_id, route):
                    if self.cancelled.is_set():
                        break
                    if kind == "delta":
                        parts.append(payload)
                        yield "delta", {"t": payload}
                    elif kind == "tool":
                        yield "tool", payload
                    elif kind == "citations":
                        citations.extend(payload)
                    elif kind == "usage":
                        usage = payload or usage
            else:
                usage_out: dict = {}
                for delta in get_llm().stream(
                    llm_messages, temperature=0.6, usage_out=usage_out
                ):
                    if self.cancelled.is_set():
                        break
                    parts.append(delta)
                    yield "delta", {"t": delta}
                usage = {"in": usage_out.get("in", 0), "out": usage_out.get("out", 0)}

            citations = dedupe_citations(citations)
            answer = "".join(parts)
            latency = int((time.monotonic() - started) * 1000)
            truncated = self.cancelled.is_set()
            suffix = TRUNCATED_SUFFIX if truncated else ""

            persisted_id = self._persist(
                answer or (EMPTY_ANSWER_FALLBACK if not truncated else ""),
                intent.code,
                route,
                citations,
                latency,
                usage,
                suffix,
            )
            persisted = persisted_id is not None

            if truncated:
                logger.info(
                    "stream cancelled by client",
                    extra={
                        "route": route,
                        "intent_code": intent.code,
                        "latency_ms": latency,
                        "phone_hash": self.phone_hash,
                    },
                )
                return

            if citations:
                yield "citation", {"items": citations}

            fresh = customers_db.get_by_id(customer["id"]) or customer
            yield "done", {
                "message_id": persisted_id,
                "latency_ms": latency,
                "tokens": usage,
                "persisted": persisted,
                "customer": fresh,
            }
            logger.info(
                "chat done",
                extra={
                    "route": route,
                    "intent_code": intent.code,
                    "latency_ms": latency,
                    "tokens": usage,
                    "phone_hash": self.phone_hash,
                },
            )
            profiler.schedule(customer["id"], conv_id)

        except ApiError as exc:
            partial = "".join(parts)
            if partial and persisted_id is None:
                self._persist(
                    partial,
                    getattr(intent, "code", None),
                    route,
                    dedupe_citations(citations),
                    int((time.monotonic() - started) * 1000),
                    usage,
                    TRUNCATED_SUFFIX,
                )
            logger.error(
                "chat failed",
                extra={"route": route, "error": exc.message, "phone_hash": self.phone_hash},
            )
            yield "error", {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
            }
        except Exception as exc:  # noqa: BLE001
            partial = "".join(parts)
            if partial and persisted_id is None:
                self._persist(
                    partial,
                    getattr(intent, "code", None),
                    route,
                    dedupe_citations(citations),
                    int((time.monotonic() - started) * 1000),
                    usage,
                    TRUNCATED_SUFFIX,
                )
            logger.exception("chat crashed", extra={"route": route, "phone_hash": self.phone_hash})
            yield "error", {
                "code": "INTERNAL",
                "message": "服务异常，请稍后重试",
                "retryable": True,
            }


def start_chat(phone: str, conversation_id: int | None, message: str) -> ChatStream:
    customer, _ = customers_db.get_or_create(phone)
    if conversation_id:
        conversation = conv_db.get(conversation_id)
        if not conversation or conversation["customer_id"] != customer["id"]:
            raise NotFound("会话不存在")
    else:
        conversation = conv_db.latest(customer["id"]) or conv_db.create(customer["id"])
    return ChatStream(customer, conversation, message, phone_hash_value=phone_hash(phone))


def customer_card(phone: str, with_messages: bool = False) -> dict | None:
    customer = customers_db.get_by_phone(phone)
    if not customer:
        return None
    card = {
        "id": customer["id"],
        "phone": customer["phone"],
        "name": customer["name"],
        "stage": customer["stage"],
        "intent_level": customer["intent_level"],
        "tags": customer["tags"],
        "profile": customer["profile"],
        "last_active_at": customer["last_active_at"],
    }
    if with_messages:
        conversation = conv_db.latest(customer["id"])
        card["recent_messages"] = (
            conv_db.list_messages(conversation["id"], limit=20) if conversation else []
        )
    return card
