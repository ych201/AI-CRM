"""管理端 API Key 鉴权（hmac.compare_digest 常量时间比较）。"""
from __future__ import annotations

import hmac
from functools import wraps

from flask import request

from app.config import get_config
from app.core.errors import Unauthorized


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        provided = request.headers.get("X-API-Key", "")
        expected = get_config().admin_api_key
        if not provided or not expected or not hmac.compare_digest(provided, expected):
            raise Unauthorized("API Key 缺失或不匹配")
        return fn(*args, **kwargs)

    return wrapper
