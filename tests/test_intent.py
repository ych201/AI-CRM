"""意图识别：关键词降级 + 低置信度处理。"""
from __future__ import annotations

import pytest

from app.core import intent

CASES = [
    ("AI 产品经理课程多少钱", 2),
    ("你们学费多少，可以分期吗", 2),
    ("2026 年 AI 岗位薪资什么水平", 3),
    ("最近 AI 行业有什么新政策", 3),
    ("今天新增了几个客户", 4),
    ("我上次聊到哪了", 4),
    ("这行能不能入", 1),
    ("35 岁转行做 AI 产品前景怎么样", 1),
    ("我现在做行政，想转 AI 产品，预算一万以内", 1),
    ("你好", 0),
    ("谢谢", 0),
]


@pytest.mark.parametrize("query,expected", CASES)
def test_rule_classify(query, expected):
    code, reason = intent.rule_classify(query)
    assert code == expected, f"{query} → {code} ({reason})"


def test_classify_falls_back_to_rules(app):
    result = intent.classify("AI 产品经理课程多少钱")
    assert result.code == 2
    assert result.route == "rag_course"
    assert result.source == "rule"
    assert result.low_confidence is False


def test_low_confidence_becomes_direct(app, monkeypatch):
    from app.llm.client import LLMResult, ToolCall

    class FakeLLM:
        available = True

        def chat(self, *args, **kwargs):
            return LLMResult(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="route_intent",
                        arguments={"intent_code": 2, "confidence": 0.3, "reason": "不确定"},
                    )
                ]
            )

    monkeypatch.setattr(intent, "get_llm", lambda: FakeLLM())
    result = intent.classify("那个东西怎么弄")
    assert result.code == 0
    assert result.route == "direct"
    assert result.low_confidence is True


def test_routes_match_intent_table(app):
    expected = {
        0: "direct",
        1: "rag_market",
        2: "rag_course",
        3: "web_search",
        4: "sql",
    }
    for code, route in expected.items():
        result = intent._result(code, 0.9, "test", "llm")
        assert result.route == route
