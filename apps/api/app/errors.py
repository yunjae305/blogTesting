"""도메인 에러와 HTTP 매핑.

상태 코드는 프론트엔드가 분기하는 API 계약의 일부이므로, 어긋나서는 안 된다.
"""

from typing import Literal

from app.shared import UserSettingsValidationError

BlogTaskErrorCode = Literal[
    "VALIDATION_FAILED", "INVALID_STATUS_TRANSITION", "NOT_FOUND", "DUPLICATE_POST_ID"
]

AuthErrorCode = Literal[
    "VALIDATION_FAILED",
    "EMAIL_ALREADY_EXISTS",
    "INVALID_CREDENTIALS",
    "RATE_LIMITED",
    "UNAUTHORIZED",
    "FORBIDDEN",
]


class BlogTaskError(Exception):
    def __init__(self, code: BlogTaskErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AuthError(Exception):
    def __init__(self, code: AuthErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidUserSettingsError(Exception):
    def __init__(self, errors: list[UserSettingsValidationError]):
        super().__init__("Invalid user settings")
        self.errors = errors


_BLOG_TASK_STATUS = {
    "VALIDATION_FAILED": 400,
    "NOT_FOUND": 404,
    "INVALID_STATUS_TRANSITION": 409,
    "DUPLICATE_POST_ID": 409,
    # 예약 포스팅: 네이버 계정을 저장하지 않은 채 예약을 시작한 경우. 화면이 이 코드로
    # '설정으로 가기'를 안내하므로 500(원인 불명)이 되면 안 된다. 다른 예약 실패 코드는
    # 작업 문서에만 남고 HTTP로 나가지 않는다.
    "NAVER_NOT_CONNECTED": 409,
}

_AUTH_STATUS = {
    "VALIDATION_FAILED": 400,
    "UNAUTHORIZED": 401,
    "INVALID_CREDENTIALS": 401,
    "RATE_LIMITED": 429,
    "FORBIDDEN": 403,
    "EMAIL_ALREADY_EXISTS": 409,
}


def status_for_blog_task_error(code: str) -> int:
    return _BLOG_TASK_STATUS.get(code, 500)


def status_for_auth_error(code: str) -> int:
    return _AUTH_STATUS.get(code, 500)
