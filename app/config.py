"""全局配置与日志。

约定：所有路径都是绝对路径（相对路径按项目根目录解析），
模块内统一通过 ``get_config()`` 读取，测试可调用 ``config.reload()`` 切换数据目录。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: str, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_path(value: str, default: str) -> Path:
    raw = (value or default).strip() or default
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = BASE_DIR / p
    return p.resolve()


class Config:
    """进程内单例配置。"""

    def __init__(self) -> None:
        load_dotenv(BASE_DIR / ".env", override=False)
        self.reload()

    def reload(self) -> None:
        self.base_dir = BASE_DIR

        # LLM
        self.deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
        self.deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

        # Embedding
        self.dashscope_api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        self.dashscope_base_url = os.getenv(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).strip()
        self.embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-v4").strip()
        self.embedding_dim = _as_int(os.getenv("EMBEDDING_DIM"), 1024)
        self.embedding_batch = max(1, min(_as_int(os.getenv("EMBEDDING_BATCH"), 10), 10))

        # 联网
        self.tavily_api_key = os.getenv("TAVILY_API_KEY", "").strip()

        # 存储
        self.sqlite_path = _as_path(os.getenv("SQLITE_PATH", ""), "./data/crm.db")
        self.chroma_path = _as_path(os.getenv("CHROMA_PATH", ""), "./data/chroma")

        # 安全：管理端走「账号 + 密码 + Session」，API Key 仅保留给脚本调用
        self.admin_username = os.getenv("ADMIN_USERNAME", "admin").strip()
        self.admin_password = os.getenv("ADMIN_PASSWORD", "change-me-please").strip()
        self.admin_api_key = os.getenv("ADMIN_API_KEY", "change-me-please").strip()
        self.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me").strip()
        self.session_hours = _as_int(os.getenv("SESSION_HOURS"), 8)

        # 运行
        self.flask_env = os.getenv("FLASK_ENV", "development").strip()
        self.debug = self.flask_env == "development"
        self.log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        self.host = os.getenv("HOST", "127.0.0.1").strip()
        self.port = _as_int(os.getenv("PORT"), 5000)

        # 业务参数
        self.rag_top_k = _as_int(os.getenv("RAG_TOP_K"), 4)
        self.rag_score_threshold = _as_float(os.getenv("RAG_SCORE_THRESHOLD"), 0.35)
        self.history_window = _as_int(os.getenv("HISTORY_WINDOW"), 6)
        self.max_tool_steps = _as_int(os.getenv("MAX_TOOL_STEPS"), 3)
        self.max_message_len = 2000
        self.llm_timeout = _as_float(os.getenv("LLM_TIMEOUT"), 60.0)
        self.greeting_use_llm = _as_bool(os.getenv("GREETING_USE_LLM"), True)
        self.conversation_reuse_hours = _as_int(os.getenv("CONVERSATION_REUSE_HOURS"), 24)

        # 切片
        self.chunk_size = 500
        self.chunk_overlap = 80

        # 限流（次 / 窗口秒）
        self.chat_limit_per_phone = _as_int(os.getenv("RATE_LIMIT_PHONE"), 10)
        self.chat_limit_global = _as_int(os.getenv("RATE_LIMIT_GLOBAL"), 60)
        self.kb_limit_global = _as_int(os.getenv("RATE_LIMIT_KB"), 20)
        self.rate_window = _as_int(os.getenv("RATE_WINDOW_SECONDS"), 60)

        # 工具超时（秒）
        self.tool_timeouts = {
            "search_knowledge_base": 3.0,
            "web_search": 8.0,
            "query_crm_db": 5.0,
            "update_customer_profile": 2.0,
        }

        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.chroma_path.mkdir(parents=True, exist_ok=True)

    @property
    def llm_configured(self) -> bool:
        return bool(self.deepseek_api_key)

    @property
    def embedding_configured(self) -> bool:
        return bool(self.dashscope_api_key)


config = Config()


def get_config() -> Config:
    return config


class JsonFormatter(logging.Formatter):
    """结构化日志：一行一个 JSON 对象。"""

    _EXTRA_KEYS = ("route", "intent_code", "latency_ms", "tokens", "phone_hash", "status", "error")

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in self._EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str | None = None) -> None:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, (level or config.log_level or "INFO").upper(), logging.INFO))
    logging.getLogger("chromadb").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
