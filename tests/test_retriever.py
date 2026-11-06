"""向量检索与引用组装（离线本地向量，不依赖外部服务）。"""
from __future__ import annotations

import pytest

from app.core.knowledge import upsert_document
from app.core.retriever import build_context, citations_from_hits, retrieve

PRICING = """2026 年课程价格表：AI 产品经理实战班原价 12800 元，本期优惠价 9800 元，课程周期 8 周。
系统班 16800 元，包含简历辅导与模拟面试。支持 3 期免息分期，开课后 7 天内可申请退款。"""

OUTLINE = """AI 产品经理实战班课程大纲：模块一产品思维与需求分析，模块二 AI 能力边界与提示工程，
模块三原型与交互，模块四数据与指标，模块五项目实战，模块六求职冲刺。不需要编程基础。"""


@pytest.fixture()
def seeded(app):
    upsert_document("course", PRICING, title="2026 年课程价格表", doc_id="course_pricing")
    upsert_document("course", OUTLINE, title="课程大纲", doc_id="course_outline")
    yield


def test_retrieve_returns_ranked_hits(seeded):
    hits = retrieve("course", "课程多少钱，可以分期吗", threshold=0.0)
    assert hits, "应能检索到切片"
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert hits[0]["doc_id"] == "course_pricing"
    assert all(h["collection"] == "course" for h in hits)


def test_threshold_filters_everything(seeded):
    assert retrieve("course", "无关内容", threshold=0.99) == []


def test_build_context_and_citations(seeded):
    hits = retrieve("course", "课程价格", threshold=0.0)
    context = build_context(hits)
    assert context.startswith("<context>")
    assert "资料 1" in context
    assert "仅作为事实依据" in context

    citations = citations_from_hits(hits)
    assert citations and len(citations[0]["snippet"]) <= 60
    assert citations[0]["title"]
    assert "score" in citations[0]


def test_empty_collection(app):
    assert retrieve("general", "任意问题", threshold=0.0) == []
    assert build_context([]) == ""
