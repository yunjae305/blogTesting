"""응답 형태 정리와 에러 매핑.

프런트엔드가 통신 형식에 따라 분기하므로, 두 가지 별난 점은 의도된 것이다:

- /auth/* 와 /users/{id}/settings 는 객체를 그대로 반환하고, /posts 아래는 모두
  {success, data} 로 감싼다.
- 인식되지 않는 예외는 상세를 숨기고 500 INTERNAL_SERVER_ERROR가 된다.
"""

import json
import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.errors import (
    AuthError,
    BlogTaskError,
    InvalidUserSettingsError,
    status_for_auth_error,
    status_for_blog_task_error,
)
from app.shared import CamelModel

logger = logging.getLogger(__name__)


def wire(value: Any) -> Any:
    """모델, 모델 리스트, 또는 일반 값을 camelCase JSON으로 변환한다."""
    if isinstance(value, CamelModel):
        return value.to_wire()
    if isinstance(value, list):
        return [wire(item) for item in value]
    return value


def envelope(value: Any, status_code: int = 200) -> JSONResponse:
    """모든 /posts 라우트가 쓰는 {success, data} 래퍼."""
    return JSONResponse({"success": True, "data": wire(value)}, status_code=status_code)


def bare(value: Any, status_code: int = 200) -> JSONResponse:
    """래퍼 없음 — 인증과 설정 응답은 객체 그 자체다."""
    return JSONResponse(wire(value), status_code=status_code)


def error_response(status_code: int, error_code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(
        {"success": False, "errorCode": error_code, "message": message, **extra},
        status_code=status_code,
    )


async def blog_task_error_handler(_request: Request, error: BlogTaskError) -> JSONResponse:
    return error_response(
        status_for_blog_task_error(error.code), error.code, error.message
    )


async def auth_error_handler(_request: Request, error: AuthError) -> JSONResponse:
    response = error_response(status_for_auth_error(error.code), error.code, error.message)
    response.headers["Cache-Control"] = "no-store"
    if error.code == "RATE_LIMITED":
        response.headers["Retry-After"] = "900"
    return response


async def invalid_settings_handler(
    _request: Request, error: InvalidUserSettingsError
) -> JSONResponse:
    return error_response(
        400,
        "VALIDATION_FAILED",
        "Invalid user settings",
        errors=[e.to_wire() for e in error.errors],
    )


async def fallback_error_handler(_request: Request, error: Exception) -> JSONResponse:
    if isinstance(error, json.JSONDecodeError):
        return error_response(400, "BAD_REQUEST", "요청 JSON 형식이 올바르지 않습니다.")
    logger.exception("처리되지 않은 서버 오류", exc_info=error)
    return error_response(500, "INTERNAL_SERVER_ERROR", "서버 내부 오류가 발생했습니다.")
