"""Flask 应用工厂。"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from app.config import configure_logging, get_config
from app.core.docparse import MAX_FILE_BYTES
from app.core.errors import ApiError, NotFound
from app.db.connection import init_db
from app.vector import store

logger = logging.getLogger("app")


def _register_blueprints(app: Flask) -> None:
    from app.api import chat, customers, health, kb, pages

    app.register_blueprint(pages.bp)
    app.register_blueprint(chat.bp)
    app.register_blueprint(customers.bp)
    app.register_blueprint(kb.bp)
    app.register_blueprint(health.bp)


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError):
        return jsonify(exc.to_dict()), exc.status

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        if request.path.startswith("/api/"):
            if exc.code == 413:
                limit_mb = MAX_FILE_BYTES // (1024 * 1024)
                return (
                    jsonify(
                        {
                            "error": {
                                "code": "UNPROCESSABLE",
                                "message": f"上传内容过大，单个文件不超过 {limit_mb}MB",
                            }
                        }
                    ),
                    413,
                )
            code = {
                404: "NOT_FOUND",
                405: "INVALID_ARGUMENT",
                413: "UNPROCESSABLE",
            }.get(exc.code or 500, "INTERNAL")
            return (
                jsonify({"error": {"code": code, "message": exc.description or "请求错误"}}),
                exc.code or 500,
            )
        return exc

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        logger.exception("unhandled error")
        if request.path.startswith("/api/"):
            return (
                jsonify({"error": {"code": "INTERNAL", "message": "服务异常，请稍后重试"}}),
                500,
            )
        return "服务异常", 500


def create_app(test_config: dict | None = None) -> Flask:
    cfg = get_config()
    if test_config:
        for key, value in test_config.items():
            setattr(cfg, key, value)
        cfg.ensure_dirs()
        from app.core import intent, ratelimit
        from app.llm.client import reset_llm
        from app.llm.embedding import reset_embedder

        reset_llm()
        reset_embedder()
        store.reset()
        intent.reset_cache()
        ratelimit.reset()

    configure_logging()

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        MAX_CONTENT_LENGTH=MAX_FILE_BYTES + 1024 * 1024,
        JSON_SORT_KEYS=False,
    )
    app.json.ensure_ascii = False
    app.json.sort_keys = False

    init_db()
    _register_blueprints(app)
    _register_error_handlers(app)

    @app.after_request
    def _json_charset(response):
        if response.mimetype == "application/json":
            response.headers["Content-Type"] = "application/json; charset=utf-8"
        return response

    if not cfg.llm_configured:
        logger.warning("未配置 DEEPSEEK_API_KEY：LLM 走离线演示模式")
    if not cfg.embedding_configured:
        logger.warning("未配置 DASHSCOPE_API_KEY：Embedding 走本地离线向量")

    return app


__all__ = ["create_app", "NotFound"]
