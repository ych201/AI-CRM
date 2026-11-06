"""Embedding 封装（百炼 text-embedding-v4，OpenAI 兼容端点）。

- 批量 ≤ 10
- 单批失败重试 1 次（指数退避 1s），仍失败抛 UpstreamUnavailable
- 未配置 DASHSCOPE_API_KEY 时退化为本地确定性向量（离线演示 / 单测可用）
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time

from app.config import get_config
from app.core.errors import UpstreamUnavailable

logger = logging.getLogger("app.embedding")


def _local_embed(text: str, dim: int) -> list[float]:
    """字符 1-gram + 2-gram 哈希到固定维度，L2 归一化。"""
    vec = [0.0] * dim
    clean = re.sub(r"\s+", "", (text or "").lower())
    if not clean:
        return vec
    tokens: list[tuple[str, float]] = [(ch, 0.5) for ch in clean]
    tokens += [(clean[i : i + 2], 1.0) for i in range(len(clean) - 1)]
    for token, weight in tokens:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        vec[idx] += weight
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


class Embedder:
    def __init__(self) -> None:
        cfg = get_config()
        self._cfg = cfg
        self._client = None
        if cfg.embedding_configured:
            try:
                from openai import OpenAI

                self._client = OpenAI(
                    api_key=cfg.dashscope_api_key,
                    base_url=cfg.dashscope_base_url,
                    timeout=30.0,
                    max_retries=0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("embedding client init failed", extra={"error": str(exc)})
        if self._client is None:
            logger.warning("DASHSCOPE_API_KEY 未配置，Embedding 使用本地离线向量")

    @property
    def available(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    def _batch(self, texts: list[str]) -> list[list[float]]:
        if self._client is None:
            return [_local_embed(t, self._cfg.embedding_dim) for t in texts]

        last_exc: Exception | None = None
        base_kwargs = {
            "model": self._cfg.embedding_model,
            "input": texts,
            "dimensions": self._cfg.embedding_dim,
            "encoding_format": "float",
        }
        for attempt in range(2):
            try:
                resp = self._client.embeddings.create(**base_kwargs)
                return [item.embedding for item in resp.data]
            except TypeError as exc:  # 老版本 SDK
                last_exc = exc
                base_kwargs.pop("dimensions", None)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 0:
                    logger.warning("embedding retry", extra={"error": str(exc)})
                    time.sleep(1.0)
                    continue
                break
        raise UpstreamUnavailable("向量服务不可用") from last_exc

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        size = self._cfg.embedding_batch
        out: list[list[float]] = []
        for i in range(0, len(texts), size):
            out.extend(self._batch(texts[i : i + size]))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._batch([text])[0]


_lock = threading.Lock()
_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    with _lock:
        if _embedder is None:
            _embedder = Embedder()
        return _embedder


def reset_embedder() -> None:
    global _embedder
    with _lock:
        _embedder = None
