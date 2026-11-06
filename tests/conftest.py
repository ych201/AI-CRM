from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def app(tmp_path):
    from app import create_app

    application = create_app(
        {
            "sqlite_path": tmp_path / "crm.db",
            "chroma_path": tmp_path / "chroma",
            "admin_api_key": "test-key",
            "deepseek_api_key": "",
            "dashscope_api_key": "",
            "tavily_api_key": "",
            "greeting_use_llm": False,
        }
    )
    application.config.update(TESTING=True)
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def headers():
    return {"X-API-Key": "test-key"}
