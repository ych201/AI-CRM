"""健康检查：探测 SQLite / Chroma / LLM / Embedding。"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, jsonify

from app.config import get_config
from app.db.connection import read_conn
from app.llm.client import get_llm
from app.llm.embedding import get_embedder
from app.vector import store

logger = logging.getLogger("app.api.health")

bp = Blueprint("health_api", __name__, url_prefix="/api")

VERSION = "1.0.0"


def _probe(fn, timeout: float = 3.0) -> str:
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn)
    try:
        future.result(timeout=timeout)
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("health probe failed", extra={"error": str(exc)})
        return "down"
    finally:
        pool.shutdown(wait=False)


def _sqlite_probe() -> None:
    with read_conn() as conn:
        conn.execute("SELECT 1").fetchone()


def _llm_probe() -> None:
    get_llm().chat(
        [{"role": "user", "content": "ping"}],
        temperature=0.0,
        max_tokens=1,
        timeout=3.0,
    )


def _embedding_probe() -> None:
    vector = get_embedder().embed_query("ping")
    if not vector:
        raise RuntimeError("empty embedding")


@bp.get("/health")
def health():
    cfg = get_config()
    checks = {
        "sqlite": _probe(_sqlite_probe),
        "chroma": "ok" if store.heartbeat() else "down",
        "llm": _probe(_llm_probe) if cfg.llm_configured else "degraded",
        "embedding": _probe(_embedding_probe) if cfg.embedding_configured else "degraded",
    }
    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return jsonify(
        {
            "status": status,
            "checks": checks,
            "version": VERSION,
            "mode": {
                "llm": "online" if cfg.llm_configured else "offline-echo",
                "embedding": "online" if cfg.embedding_configured else "local-hash",
            },
        }
    )
