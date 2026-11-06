"""统一错误类型与错误响应体（SDD 6.5）。"""
from __future__ import annotations


class ApiError(Exception):
    status = 400
    code = "INVALID_ARGUMENT"

    def __init__(
        self,
        message: str,
        code: str | None = None,
        status: int | None = None,
        field: str | None = None,
        retryable: bool = False,
        extra: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.field = field
        self.retryable = retryable
        self.extra = extra or {}

    def to_dict(self) -> dict:
        payload: dict = {"code": self.code, "message": self.message}
        if self.field:
            payload["field"] = self.field
        payload.update(self.extra)
        return {"error": payload}


class InvalidArgument(ApiError):
    status = 400
    code = "INVALID_ARGUMENT"


class Unauthorized(ApiError):
    status = 401
    code = "UNAUTHORIZED"


class NotFound(ApiError):
    status = 404
    code = "NOT_FOUND"


class Conflict(ApiError):
    status = 409
    code = "CONFLICT"


class Unprocessable(ApiError):
    status = 422
    code = "UNPROCESSABLE"


class RateLimited(ApiError):
    status = 429
    code = "RATE_LIMITED"


class UpstreamUnavailable(ApiError):
    status = 503
    code = "UPSTREAM_UNAVAILABLE"

    def __init__(self, message: str = "上游服务不可用", **kwargs) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(message, status=503, code="UPSTREAM_UNAVAILABLE", **kwargs)


class SQLRejected(Exception):
    """query_crm_db 的安全拒绝，作为工具结果回填给模型。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
