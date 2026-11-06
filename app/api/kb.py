"""知识库接口：/api/kb/*（写操作需要 X-API-Key）。"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.api.auth import require_api_key
from app.core import knowledge, ratelimit
from app.core.errors import InvalidArgument, NotFound, RateLimited, Unprocessable
from app.db import kb as kb_db

bp = Blueprint("kb_api", __name__, url_prefix="/api/kb")


def _check_limit() -> None:
    ok, wait = ratelimit.check_kb()
    if not ok:
        raise RateLimited(f"请求过于频繁，请 {wait} 秒后再试")


@bp.get("/documents")
@require_api_key
def list_documents():
    collection = (request.args.get("collection") or "").strip() or None
    if collection and collection not in kb_db.COLLECTIONS:
        raise Unprocessable(
            f"collection 必须是 {'/'.join(kb_db.COLLECTIONS)}", field="collection"
        )
    show_disabled = (request.args.get("show_disabled") or "").lower() in ("1", "true", "yes")
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = max(1, min(int(request.args.get("page_size", 50)), 100))
    except ValueError:
        raise InvalidArgument("page/page_size 必须是整数")

    result = kb_db.list_documents(collection, show_disabled, page, page_size)
    return jsonify(result)


@bp.post("/documents")
@require_api_key
def upsert_document():
    _check_limit()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise InvalidArgument("请求体必须是 JSON 对象")

    doc_id = str(data.get("doc_id") or "").strip() or None
    collection = str(data.get("collection") or "").strip()
    title = str(data.get("title") or "").strip() or None
    content = data.get("content")
    source = str(data.get("source") or "manual").strip() or "manual"

    if not collection:
        raise InvalidArgument("collection 不能为空", field="collection")
    if not isinstance(content, str):
        raise InvalidArgument("content 必须是字符串", field="content")

    result = knowledge.upsert_document(
        collection=collection,
        content=content,
        title=title,
        doc_id=doc_id,
        source=source,
    )
    return jsonify(result)


@bp.get("/documents/<doc_id>")
@require_api_key
def get_document(doc_id: str):
    """编辑弹窗取全文（列表接口不返回 content）。"""
    doc = kb_db.get(doc_id)
    if not doc:
        raise NotFound("知识条目不存在")
    return jsonify(doc)


@bp.delete("/documents/<doc_id>")
@require_api_key
def delete_document(doc_id: str):
    _check_limit()
    result = knowledge.delete_document(doc_id)
    if not result.get("deleted"):
        raise NotFound("知识条目不存在")
    return jsonify(result)


@bp.post("/rebuild")
@require_api_key
def rebuild():
    _check_limit()
    data = request.get_json(silent=True) or {}
    collection = str(data.get("collection") or "all").strip() or "all"
    return jsonify(knowledge.rebuild(collection))
