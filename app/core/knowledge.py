"""知识条目入库 / 删除 / 重建索引。

写入顺序遵循 SDD 7.4：先写临时前缀 chunk → 成功后删旧 → 改回正式 doc_id → 最后写元数据。
embedding 失败时旧数据保持可用。
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import uuid
from pathlib import Path

from app.config import get_config
from app.core.errors import Conflict, Unprocessable, UpstreamUnavailable
from app.db import kb as kb_db
from app.llm.embedding import get_embedder
from app.vector import store

logger = logging.getLogger("app.knowledge")

CHUNK_MIN_LEN = 15
CONTENT_MAX = 20000
DOC_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")

_rebuild_lock = threading.Lock()


# ---------------------------------------------------------------- 切片
def _splitter():
    cfg = get_config()
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        return RecursiveCharacterTextSplitter(
            chunk_size=cfg.chunk_size,
            chunk_overlap=cfg.chunk_overlap,
            separators=["\n\n", "\n", "。", "！", "？", " ", ""],
        )
    except Exception:  # noqa: BLE001  极端情况下退化
        return None


def split_text(content: str) -> list[str]:
    text = (content or "").strip()
    if not text:
        return []
    splitter = _splitter()
    chunks = splitter.split_text(text) if splitter else [text[i : i + 500] for i in range(0, len(text), 500)]
    return [c.strip() for c in chunks if len(c.strip()) >= CHUNK_MIN_LEN]


def split_for_documents(content: str, limit: int = CONTENT_MAX) -> list[str]:
    """把超长文本按段落切成若干 ≤ limit 的片段（文件导入用）。"""
    text = (content or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    blocks: list[str] = []
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        while len(para) > limit:
            cut = para.rfind("。", 0, limit)
            cut = cut + 1 if cut >= limit // 2 else limit
            blocks.append(para[:cut].strip())
            para = para[cut:].lstrip()
        if para:
            blocks.append(para)

    parts: list[str] = []
    buf = ""
    for block in blocks:
        candidate = f"{buf}\n\n{block}" if buf else block
        if len(candidate) <= limit:
            buf = candidate
        else:
            if buf:
                parts.append(buf)
            buf = block
    if buf:
        parts.append(buf)
    return parts


def new_doc_id(collection: str) -> str:
    return f"{collection}_{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"


def doc_id_from_file(collection: str, filename: str, content: str) -> str:
    """用「文件名 slug + 内容指纹」生成稳定 doc_id：同一文件重复上传只更新不重复。"""
    stem = Path(filename or "").stem
    slug = re.sub(r"[^A-Za-z0-9_.\-]+", "-", stem).strip("-._")[:40]
    digest = hashlib.sha1((content or "").encode("utf-8")).hexdigest()[:6]
    parts = [collection] + ([slug] if slug else []) + [digest]
    return "_".join(parts)[:80]


def _validate(collection: str, content: str, doc_id: str | None) -> None:
    if collection not in kb_db.COLLECTIONS:
        raise Unprocessable(f"collection 必须是 {'/'.join(kb_db.COLLECTIONS)}", field="collection")
    if not content or not content.strip():
        raise Unprocessable("content 不能为空", field="content")
    if len(content) > CONTENT_MAX:
        raise Unprocessable(f"content 不能超过 {CONTENT_MAX} 字符", field="content")
    if doc_id and not DOC_ID_RE.match(doc_id):
        raise Unprocessable("doc_id 只允许字母、数字、下划线、短横线、点，且不超过 80 字符", field="doc_id")


# ---------------------------------------------------------------- 单条写入
def upsert_document(
    collection: str,
    content: str,
    title: str | None = None,
    doc_id: str | None = None,
    source: str = "manual",
) -> dict:
    content = (content or "").strip()
    doc_id = (doc_id or "").strip()
    _validate(collection, content, doc_id or None)
    if not doc_id:
        doc_id = new_doc_id(collection)
    title = (title or "").strip() or content[:20]

    existing = kb_db.get(doc_id)
    action = "updated" if existing else "created"

    chunks = split_text(content)
    if not chunks:
        raise Unprocessable("content 切片后为空，请补充内容", field="content")

    embeddings = get_embedder().embed_documents(chunks)  # 失败直接抛 503，此时尚未动旧数据
    tmp_id = f"{doc_id}::tmp::{uuid.uuid4().hex[:8]}"

    try:
        store.add_chunks(collection, tmp_id, title, chunks, embeddings)
    except Exception as exc:  # noqa: BLE001
        logger.error("staging chunks failed", extra={"error": str(exc)})
        _safe_disable(doc_id, existing)
        raise UpstreamUnavailable("向量库写入失败") from exc

    try:
        store.delete_doc(collection, doc_id)
        if existing and existing.get("collection") and existing["collection"] != collection:
            store.delete_doc(existing["collection"], doc_id)
        n = store.add_chunks(collection, doc_id, title, chunks, embeddings)
        store.delete_doc(collection, tmp_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("commit chunks failed", extra={"error": str(exc)})
        try:
            store.delete_doc(collection, tmp_id)
        except Exception:  # noqa: BLE001
            pass
        _safe_disable(doc_id, existing)
        raise UpstreamUnavailable("向量库写入失败") from exc

    try:
        doc = kb_db.upsert(
            doc_id=doc_id,
            collection=collection,
            title=title,
            content=content,
            source=source,
            chunk_count=n,
            enabled=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("kb metadata write failed", extra={"error": str(exc)})
        _safe_disable(doc_id, existing)
        raise

    indexed_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    logger.info(
        "kb document indexed",
        extra={"status": action, "tokens": {"chunks": n}},
    )
    return {
        "doc_id": doc_id,
        "collection": collection,
        "chunks": n,
        "action": action,
        "indexed_at": indexed_at,
        "title": doc["title"],
    }


def _safe_disable(doc_id: str, existing: dict | None) -> None:
    if not existing:
        return
    try:
        kb_db.set_enabled(doc_id, False)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 删除
def delete_document(doc_id: str) -> dict:
    doc = kb_db.get(doc_id)
    if not doc:
        return {"deleted": False, "doc_id": doc_id}
    try:
        store.delete_doc(doc["collection"], doc_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("vector delete failed", extra={"error": str(exc)})
        raise UpstreamUnavailable("向量库删除失败") from exc
    kb_db.delete(doc_id)
    return {"deleted": True, "doc_id": doc_id}


# ---------------------------------------------------------------- 重建
def _rebuild_one(collection: str) -> tuple[int, int, int]:
    docs = kb_db.all_enabled(collection)
    store.clear_collection(collection)
    doc_count = 0
    chunk_count = 0
    failed = 0
    for doc in docs:
        chunks = split_text(doc["content"])
        if not chunks:
            kb_db.set_enabled(doc["doc_id"], False)
            failed += 1
            continue
        try:
            embeddings = get_embedder().embed_documents(chunks)
            n = store.add_chunks(collection, doc["doc_id"], doc["title"], chunks, embeddings)
            kb_db.update_chunk_count(doc["doc_id"], n)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "rebuild doc failed",
                extra={"error": str(exc), "status": doc["doc_id"]},
            )
            kb_db.set_enabled(doc["doc_id"], False)
            failed += 1
            continue
        doc_count += 1
        chunk_count += n
    return doc_count, chunk_count, failed


def rebuild(collection: str = "all") -> dict:
    if collection != "all" and collection not in kb_db.COLLECTIONS:
        raise Unprocessable("collection 必须是 market/course/general/all", field="collection")
    if not _rebuild_lock.acquire(blocking=False):
        raise Conflict("索引重建正在进行中")
    started = time.monotonic()
    try:
        targets = list(kb_db.COLLECTIONS) if collection == "all" else [collection]
        documents = 0
        chunks = 0
        failed = 0
        for key in targets:
            d, c, f = _rebuild_one(key)
            documents += d
            chunks += c
            failed += f
    finally:
        _rebuild_lock.release()
    elapsed = int((time.monotonic() - started) * 1000)
    return {
        "collection": collection,
        "documents": documents,
        "chunks": chunks,
        "failed": failed,
        "elapsed_ms": elapsed,
    }
