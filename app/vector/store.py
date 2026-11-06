"""Chroma 本地向量库：三个 collection、余弦距离、幂等写入。"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from app.config import get_config

logger = logging.getLogger("app.vector")

COLLECTIONS = ("market", "course", "general")
COLLECTION_NAMES = {
    "market": "kb_market",
    "course": "kb_course",
    "general": "kb_general",
}

_lock = threading.RLock()
_client: Any = None
_client_path: Path | None = None
_collections: dict[str, Any] = {}


def _require_collection_key(key: str) -> str:
    if key not in COLLECTION_NAMES:
        raise ValueError(f"未知 collection: {key}")
    return key


def get_client() -> Any:
    global _client, _client_path
    cfg = get_config()
    with _lock:
        if _client is not None and _client_path == cfg.chroma_path:
            return _client
        import chromadb
        from chromadb.config import Settings

        cfg.chroma_path.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(
            path=str(cfg.chroma_path),
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        _client_path = cfg.chroma_path
        _collections.clear()
        return _client


def get_collection(key: str) -> Any:
    key = _require_collection_key(key)
    with _lock:
        coll = _collections.get(key)
        if coll is not None:
            return coll
        client = get_client()
        coll = client.get_or_create_collection(
            name=COLLECTION_NAMES[key],
            metadata={"hnsw:space": "cosine"},
        )
        _collections[key] = coll
        return coll


def reset() -> None:
    """测试用：释放缓存句柄。"""
    global _client, _client_path
    with _lock:
        _collections.clear()
        _client = None
        _client_path = None


def heartbeat() -> bool:
    try:
        get_client().heartbeat()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("chroma heartbeat failed", extra={"error": str(exc)})
        return False


# ---------------------------------------------------------------- 写入
def add_chunks(
    collection_key: str,
    doc_id: str,
    title: str,
    chunks: list[str],
    embeddings: list[list[float]],
) -> int:
    if not chunks:
        return 0
    coll = get_collection(collection_key)
    ids = [f"{doc_id}::{i}" for i in range(len(chunks))]
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    metadatas = [
        {
            "doc_id": doc_id,
            "collection": collection_key,
            "title": title,
            "chunk_index": i,
            "updated_at": now,
        }
        for i in range(len(chunks))
    ]
    with _lock:
        coll.add(ids=ids, embeddings=embeddings, documents=chunks, metadatas=metadatas)
    return len(ids)


def delete_doc(collection_key: str, doc_id: str) -> None:
    coll = get_collection(collection_key)
    with _lock:
        try:
            coll.delete(where={"doc_id": doc_id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("chroma delete failed", extra={"error": str(exc)})
            raise


def delete_ids(collection_key: str, ids: list[str]) -> None:
    if not ids:
        return
    coll = get_collection(collection_key)
    with _lock:
        try:
            coll.delete(ids=ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("chroma delete ids failed", extra={"error": str(exc)})
            raise


def clear_collection(collection_key: str) -> None:
    """清空 collection（重建索引用）。"""
    cfg = get_config()
    get_client()
    with _lock:
        _collections.pop(collection_key, None)
        try:
            get_client().delete_collection(name=COLLECTION_NAMES[collection_key])
        except Exception:  # noqa: BLE001  不存在的 collection 忽略
            pass
        get_collection(collection_key)
    logger.info("chroma collection cleared", extra={"status": collection_key})


def count(collection_key: str) -> int:
    try:
        return int(get_collection(collection_key).count())
    except Exception:  # noqa: BLE001
        return 0


def total_count() -> int:
    return sum(count(k) for k in COLLECTIONS)


# ---------------------------------------------------------------- 检索
def query(collection_key: str, embedding: list[float], k: int = 4) -> list[dict]:
    coll = get_collection(collection_key)
    try:
        res = coll.query(
            query_embeddings=[embedding],
            n_results=max(1, k),
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("chroma query failed", extra={"error": str(exc)})
        raise

    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    hits = []
    for doc, meta, dist in zip(docs, metas, dists):
        score = 1.0 - float(dist)  # cosine distance → 相似度
        hits.append(
            {
                "content": doc or "",
                "doc_id": (meta or {}).get("doc_id", ""),
                "title": (meta or {}).get("title", ""),
                "collection": (meta or {}).get("collection", collection_key),
                "chunk_index": (meta or {}).get("chunk_index", 0),
                "score": round(score, 4),
            }
        )
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits


def search_all(embedding: list[float], k: int = 4) -> list[dict]:
    hits: list[dict] = []
    for key in COLLECTIONS:
        try:
            hits.extend(query(key, embedding, k))
        except Exception:  # noqa: BLE001
            continue
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:k]
