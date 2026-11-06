"""RAG 检索与引用组装。"""
from __future__ import annotations

import logging

from app.config import get_config
from app.core.prompts import CONTEXT_BLOCK, CONTEXT_ITEM
from app.llm.embedding import get_embedder
from app.vector import store

logger = logging.getLogger("app.retriever")

SNIPPET_LEN = 60


# 本地哈希向量与真实 embedding 的相似度分布不同（短查询对长文档天然偏低），
# 离线演示模式下改用「相对阈值」保留 top 命中；配置 DASHSCOPE_API_KEY 后回到 SPEC 的 0.35。
OFFLINE_SCORE_FLOOR = 0.02
OFFLINE_SCORE_RATIO = 0.6


def retrieve(
    collection: str,
    query: str,
    top_k: int | None = None,
    threshold: float | None = None,
) -> list[dict]:
    cfg = get_config()
    k = top_k or cfg.rag_top_k
    cutoff = cfg.rag_score_threshold if threshold is None else threshold
    embedder = get_embedder()
    embedding = embedder.embed_query(query)
    hits = store.query(collection, embedding, k=k)
    if not hits:
        return []
    if not embedder.available and threshold is None:
        cutoff = max(OFFLINE_SCORE_FLOOR, min(cutoff, hits[0]["score"] * OFFLINE_SCORE_RATIO))
    return [h for h in hits if h["score"] >= cutoff][:k]


def build_context(hits: list[dict]) -> str:
    if not hits:
        return ""
    items = [
        CONTEXT_ITEM.format(
            index=i + 1,
            title=h.get("title") or "未命名",
            doc_id=h.get("doc_id") or "",
            content=(h.get("content") or "").strip(),
        )
        for i, h in enumerate(hits)
    ]
    return CONTEXT_BLOCK.format(items="\n\n".join(items))


def citations_from_hits(hits: list[dict]) -> list[dict]:
    items = []
    for h in hits:
        items.append(
            {
                "doc_id": h.get("doc_id", ""),
                "title": h.get("title", ""),
                "score": h.get("score"),
                "snippet": (h.get("content") or "")[:SNIPPET_LEN],
            }
        )
    return items


def citations_from_web(results: list[dict]) -> list[dict]:
    items = []
    for r in results:
        items.append(
            {
                "doc_id": r.get("url", ""),
                "title": r.get("title", ""),
                "score": None,
                "snippet": (r.get("content") or "")[:SNIPPET_LEN],
            }
        )
    return items
