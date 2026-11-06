"""DeepSeek（OpenAI 兼容）客户端封装。

- 统一 tools 参数格式（OpenAI function calling 原生格式）
- 5xx / 超时重试 1 次，指数退避 1s
- 未配置 DEEPSEEK_API_KEY 时自动降级为 ``EchoClient``（离线演示模式），
  保证整条链路在没有密钥的环境下依然可跑通、可测试。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.config import get_config
from app.core.errors import UpstreamUnavailable

logger = logging.getLogger("app.llm")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""


def _retryable(exc: Exception) -> bool:
    try:
        import openai
    except ImportError:  # pragma: no cover
        return False
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code >= 500
    return False


def _estimate_tokens(text: str) -> int:
    # 粗略估算：中文按 1 字 ≈ 1 token，英文按 4 字符 ≈ 1 token
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return max(1, cjk + (len(text) - cjk) // 4)


class DeepSeekClient:
    """线上客户端。"""

    def __init__(self) -> None:
        cfg = get_config()
        from openai import OpenAI

        self._cfg = cfg
        self._client = OpenAI(
            api_key=cfg.deepseek_api_key,
            base_url=cfg.deepseek_base_url,
            timeout=cfg.llm_timeout,
            max_retries=0,
        )

    available = True

    # ---------------------------------------------------------------- chat
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        temperature: float = 0.6,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        kwargs: dict = {
            "model": self._cfg.deepseek_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        if timeout:
            kwargs["timeout"] = timeout

        resp = self._call(kwargs, stream=False)
        choice = resp.choices[0].message
        calls: list[ToolCall] = []
        for tc in (choice.tool_calls or []):
            raw_args = tc.function.arguments or "{}"
            try:
                args = json.loads(raw_args)
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        usage = getattr(resp, "usage", None)
        return LLMResult(
            content=(choice.content or "").strip(),
            tool_calls=calls,
            tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
            tokens_out=getattr(usage, "completion_tokens", 0) or 0,
            model=self._cfg.deepseek_model,
        )

    # -------------------------------------------------------------- stream
    def stream(
        self,
        messages: list[dict],
        temperature: float = 0.6,
        max_tokens: int | None = None,
        usage_out: dict | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        kwargs: dict = {
            "model": self._cfg.deepseek_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if timeout:
            kwargs["timeout"] = timeout

        attempt = 0
        while True:
            produced = False
            try:
                stream = self._client.chat.completions.create(**kwargs)
                for chunk in stream:
                    if usage_out is not None and getattr(chunk, "usage", None):
                        usage_out["in"] = getattr(chunk.usage, "prompt_tokens", 0) or 0
                        usage_out["out"] = getattr(chunk.usage, "completion_tokens", 0) or 0
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    text = getattr(delta, "content", None)
                    if text:
                        produced = True
                        yield text
                return
            except Exception as exc:  # noqa: BLE001
                if produced or attempt >= 1 or not _retryable(exc):
                    logger.error("llm stream failed", extra={"error": str(exc)})
                    raise UpstreamUnavailable("模型响应失败") from exc
                attempt += 1
                logger.warning("llm stream retry", extra={"error": str(exc)})
                time.sleep(1.0)

    # ---------------------------------------------------------------- util
    def _call(self, kwargs: dict, stream: bool = False) -> Any:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                return self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 0 and _retryable(exc):
                    logger.warning("llm call retry", extra={"error": str(exc)})
                    time.sleep(1.0)
                    continue
                break
        logger.error("llm call failed", extra={"error": str(last_exc)})
        raise UpstreamUnavailable("模型服务不可用") from last_exc


class EchoClient:
    """离线演示客户端：无密钥时保证链路可跑通（不做真实生成）。"""

    available = False

    def __init__(self) -> None:
        self._cfg = get_config()

    # ---------------------------------------------------------------- chat
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        temperature: float = 0.6,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        tool_names = {t.get("function", {}).get("name") for t in (tools or [])}
        user_text = self._last_user(messages)

        # 演示用：让 SQL Agent 走真实工具（本地 SQLite 可用，不需要外部服务）
        if "query_crm_db" in tool_names and not self._has_tool_result(messages):
            sql = self._demo_sql(user_text)
            if sql:
                return LLMResult(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="call_demo_sql",
                            name="query_crm_db",
                            arguments={"sql": sql, "purpose": "统计新增客户数"},
                        )
                    ],
                )

        if "route_intent" in tool_names:
            # 不返回工具调用 → 上层走关键词降级
            return LLMResult(content="")

        return LLMResult(content=self._compose(messages), model="offline-echo")

    # -------------------------------------------------------------- stream
    def stream(
        self,
        messages: list[dict],
        temperature: float = 0.6,
        max_tokens: int | None = None,
        usage_out: dict | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        text = self._compose(messages)
        if usage_out is not None:
            usage_out["in"] = _estimate_tokens(json.dumps(messages, ensure_ascii=False))
            usage_out["out"] = _estimate_tokens(text)
        step = 4
        for i in range(0, len(text), step):
            yield text[i : i + step]
            time.sleep(0.008)

    # ---------------------------------------------------------------- util
    @staticmethod
    def _last_user(messages: list[dict]) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return str(msg.get("content") or "")
        return ""

    @staticmethod
    def _has_tool_result(messages: list[dict]) -> bool:
        return any(m.get("role") == "tool" for m in messages)

    @staticmethod
    def _demo_sql(text: str) -> str | None:
        if any(k in text for k in ("新增", "几个客户", "多少客户", "统计")):
            return "SELECT COUNT(*) AS today_new FROM customers WHERE date(created_at) = date('now','localtime')"
        return None

    def _compose(self, messages: list[dict]) -> str:
        user_text = self._last_user(messages)
        system = ""
        for msg in messages:
            if msg.get("role") == "system":
                system = str(msg.get("content") or "")
                break

        tool_result = None
        for msg in reversed(messages):
            if msg.get("role") == "tool":
                tool_result = msg.get("content")
                break
        if tool_result:
            try:
                data = json.loads(tool_result)
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("rows"):
                cols = data.get("columns") or []
                rows = data.get("rows") or []
                if cols and rows:
                    first = ", ".join(f"{c} = {v}" for c, v in zip(cols, rows[0]))
                    return f"查了一下，结果是：{first}。"

        context = ""
        start = system.find("<context>")
        end = system.find("</context>")
        if start != -1 and end != -1 and end > start:
            context = system[start + len("<context>") : end].strip()

        if context:
            body = context
            if "]" in body:
                body = body.split("]", 1)[1]
            body = body.replace("\n", " ").strip()
            body = body[:220]
            return f"根据知识库里的资料：{body}。\n\n还想了解哪一部分？"

        if any(k in user_text for k in ("最新", "行情", "新闻", "薪资")):
            return (
                "这个问题需要联网查最新数据，当前是离线演示模式（未配置 TAVILY_API_KEY 与模型密钥），"
                "暂时无法给出实时结论。配置密钥后我会检索最新行情并附上来源链接。"
            )

        return (
            "我是 SeaChat 的 AI 顾问小海。当前运行在离线演示模式（未配置 DEEPSEEK_API_KEY），"
            f"所以只能给出简短回应。你问的是「{user_text[:40]}」。"
            "配置模型密钥后，我会结合知识库、联网搜索和客户数据给出完整回答。"
        )


_lock = threading.Lock()
_llm: Any = None


def get_llm() -> Any:
    global _llm
    with _lock:
        if _llm is None:
            cfg = get_config()
            if cfg.llm_configured:
                _llm = DeepSeekClient()
            else:
                logger.warning("DEEPSEEK_API_KEY 未配置，LLM 进入离线演示模式")
                _llm = EchoClient()
        return _llm


def reset_llm() -> None:
    global _llm
    with _lock:
        _llm = None


def llm_available() -> bool:
    return get_config().llm_configured
