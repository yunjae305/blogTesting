"""발행 결과 모델."""

from enum import StrEnum

from .base import CamelModel


class PostingMethod(StrEnum):
    COPY = "copy"
    DRAFT = "draft"
    AUTO = "auto"


class PostingChannel(StrEnum):
    """발행 목적지. 같은 원고를 어느 플랫폼에 올렸는가를 가른다.

    중복 발행 가드는 채널별이다 — 스레드에 올린 글을 네이버에 또 올리는 것은 중복이
    아니라 두 번째 채널 발행이다.
    """

    NAVER = "naver"
    THREADS = "threads"


class PostingResultStatus(StrEnum):
    SUCCESS = "success"
    FAIL = "fail"
    NEEDS_HUMAN = "needs_human"


class PostingLog(CamelModel):
    log_id: str
    post_id: str
    user_id: str
    method: PostingMethod
    # 채널 개념이 없던 옛 로그는 전부 네이버 발행이었다 — 기본값이 곧 과거 사실이다.
    channel: PostingChannel = PostingChannel.NAVER
    result: PostingResultStatus
    post_url: str | None = None
    error_message: str | None = None
    created_at: str
