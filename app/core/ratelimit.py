"""进程内滑动窗口限流（单进程有效，多 worker 需换 Redis，见 SDD 9.4）。"""
from __future__ import annotations

import threading
import time
from collections import deque

_lock = threading.Lock()
_buckets: dict[str, deque[float]] = {}


def _prune(bucket: deque[float], window: int, now: float) -> None:
    while bucket and now - bucket[0] > window:
        bucket.popleft()


def hit(key: str, limit: int, window: int) -> tuple[bool, int]:
    """返回 (是否允许, 需等待秒数)。"""
    now = time.monotonic()
    with _lock:
        bucket = _buckets.setdefault(key, deque())
        _prune(bucket, window, now)
        if len(bucket) >= limit:
            wait = max(1, int(window - (now - bucket[0])) + 1)
            return False, wait
        bucket.append(now)
        return True, 0


def peek(key: str, limit: int, window: int) -> tuple[bool, int]:
    """只查询是否已达上限，不消耗配额（登录前预检查用）。"""
    now = time.monotonic()
    with _lock:
        bucket = _buckets.setdefault(key, deque())
        _prune(bucket, window, now)
        if len(bucket) >= limit:
            wait = max(1, int(window - (now - bucket[0])) + 1)
            return False, wait
        return True, 0


def check_chat(phone: str) -> tuple[bool, int]:
    from app.config import get_config

    cfg = get_config()
    ok, wait = hit("global", cfg.chat_limit_global, cfg.rate_window)
    if not ok:
        return False, wait
    return hit(f"phone:{phone}", cfg.chat_limit_per_phone, cfg.rate_window)


def check_kb() -> tuple[bool, int]:
    from app.config import get_config

    cfg = get_config()
    return hit("kb", cfg.kb_limit_global, cfg.rate_window)


def reset() -> None:
    with _lock:
        _buckets.clear()
