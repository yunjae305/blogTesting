"""사용자 설정 모델."""

from typing import Literal

from .base import CamelModel

# 원고 목표 분량. 짧은 글은 정보를, 긴 글은 검색 체류시간을 노린다. 기본은 중간.
# 문자열 값은 그대로 프런트가 보내고 프롬프트가 목표 글자수로 옮긴다(prompts.py).
ARTICLE_LENGTHS: tuple[str, ...] = ("short", "medium", "long")
DEFAULT_ARTICLE_LENGTH = "medium"

# 제목에서 소재와 트렌드 키워드 중 무엇을 중심에 둘지. 예전의 고정 비율 3:7을 대체한다 —
# 모델이 본문에서 정확히 30%/70%를 맞추지 못하는데 사용자에게 숫자를 약속하면 지켜지지
# 않으므로, 숫자 대신 "무엇이 중심인가"라는 강조 방향으로 지시한다(prompts.py).
BLEND_MODES: tuple[str, ...] = ("subject", "balanced", "trend")
# 기본은 기존 동작(키워드=트렌드 중심)을 보존하는 trend.
DEFAULT_BLEND_MODE = "trend"

# AI 이미지는 항상 생성한다. 예전에는 imageMode(off/on/on_with_disclosure) 설정이 있었으나,
# 이미지 없는 글은 쓰지 않기로 해서 설정 자체를 없앴다. 기존 문서에 남은 imageMode 값은
# CamelModel의 extra="ignore"로 그냥 무시된다.


class UserSettingsLimits:
    MIN_HASHTAG_COUNT = 1
    MAX_HASHTAG_COUNT = 10
    ARTICLE_LENGTHS = ARTICLE_LENGTHS
    DEFAULT_ARTICLE_LENGTH = DEFAULT_ARTICLE_LENGTH
    BLEND_MODES = BLEND_MODES
    DEFAULT_BLEND_MODE = DEFAULT_BLEND_MODE
    MAX_PERSONA_LENGTH = 1200
    MAX_CUSTOM_PERSONA_NAME_LENGTH = 80
    MAX_CUSTOM_PERSONA_DESCRIPTION_LENGTH = 200
    MAX_CUSTOM_PERSONA_LENGTH = 1200


class UserSettings(CamelModel):
    user_id: str
    hashtag_count: int
    # 기존 문서에는 이 필드가 없다. 기본값이 있어 그대로 읽힌다.
    article_length: str = DEFAULT_ARTICLE_LENGTH
    blend_mode: str = DEFAULT_BLEND_MODE
    default_persona: str
    custom_persona_name: str | None = None
    custom_persona_description: str | None = None
    custom_persona: str | None = None
    auto_posting_enabled: bool
    created_at: str
    updated_at: str


UserSettingsErrorCode = Literal["REQUIRED", "INVALID_RANGE", "INVALID_TYPE", "TOO_LONG"]


class UserSettingsValidationError(CamelModel):
    field: str
    code: UserSettingsErrorCode
    message: str
