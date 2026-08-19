"""validation.ts.

다른 검증기와 달리 이것은 모든 문제를 모은 뒤에 던진다. 그래서 클라이언트는 첫 실패
하나가 아니라 필드별 에러 목록을 받는다.
"""

from dataclasses import dataclass
from typing import Any

from app.errors import InvalidUserSettingsError
from app.shared import CUSTOM_PERSONA_ID, UserSettingsLimits, UserSettingsValidationError

LIMITS = UserSettingsLimits


@dataclass
class UpsertUserSettingsInput:
    user_id: str
    hashtag_count: Any
    default_persona: Any
    auto_posting_enabled: Any
    article_length: Any = LIMITS.DEFAULT_ARTICLE_LENGTH
    blend_mode: Any = LIMITS.DEFAULT_BLEND_MODE
    custom_persona_name: Any = None
    custom_persona_description: Any = None
    custom_persona: Any = None


def _error(field: str, code: str, message: str) -> UserSettingsValidationError:
    return UserSettingsValidationError(field=field, code=code, message=message)


def _check_optional_string(
    value: Any, field: str, max_length: int, limit_label: str
) -> list[UserSettingsValidationError]:
    if value is None:
        return []
    if not isinstance(value, str):
        return [_error(field, "INVALID_TYPE", f"{field} must be a string when provided.")]
    if len(value) > max_length:
        return [_error(field, "TOO_LONG", f"{field} must be {limit_label} or fewer.")]
    return []


def validate_user_settings_input(
    settings: UpsertUserSettingsInput,
) -> list[UserSettingsValidationError]:
    errors: list[UserSettingsValidationError] = []

    if not isinstance(settings.user_id, str) or not settings.user_id.strip():
        errors.append(_error("userId", "REQUIRED", "userId is required."))

    count = settings.hashtag_count
    if not isinstance(count, int) or isinstance(count, bool):
        errors.append(_error("hashtagCount", "INVALID_TYPE", "hashtagCount must be an integer."))
    elif not LIMITS.MIN_HASHTAG_COUNT <= count <= LIMITS.MAX_HASHTAG_COUNT:
        errors.append(
            _error(
                "hashtagCount",
                "INVALID_RANGE",
                f"hashtagCount must be between {LIMITS.MIN_HASHTAG_COUNT} and {LIMITS.MAX_HASHTAG_COUNT}.",
            )
        )

    if settings.article_length not in LIMITS.ARTICLE_LENGTHS:
        allowed = ", ".join(LIMITS.ARTICLE_LENGTHS)
        errors.append(
            _error("articleLength", "INVALID_RANGE", f"articleLength must be one of {allowed}.")
        )

    if settings.blend_mode not in LIMITS.BLEND_MODES:
        allowed = ", ".join(LIMITS.BLEND_MODES)
        errors.append(
            _error("blendMode", "INVALID_RANGE", f"blendMode must be one of {allowed}.")
        )

    persona = settings.default_persona
    if not isinstance(persona, str) or not persona.strip():
        errors.append(_error("defaultPersona", "REQUIRED", "defaultPersona is required."))
    elif len(persona) > LIMITS.MAX_PERSONA_LENGTH:
        errors.append(
            _error(
                "defaultPersona", "TOO_LONG", "defaultPersona must be 1200 characters or fewer."
            )
        )

    errors += _check_optional_string(
        settings.custom_persona, "customPersona", LIMITS.MAX_CUSTOM_PERSONA_LENGTH,
        "1200 characters",
    )
    errors += _check_optional_string(
        settings.custom_persona_name,
        "customPersonaName",
        LIMITS.MAX_CUSTOM_PERSONA_NAME_LENGTH,
        "80 characters",
    )
    errors += _check_optional_string(
        settings.custom_persona_description,
        "customPersonaDescription",
        LIMITS.MAX_CUSTOM_PERSONA_DESCRIPTION_LENGTH,
        "200 characters",
    )

    if isinstance(persona, str) and persona.strip() == CUSTOM_PERSONA_ID:
        custom_name = settings.custom_persona_name
        if custom_name is None or (
            isinstance(custom_name, str) and not custom_name.strip()
        ):
            errors.append(
                _error(
                    "customPersonaName",
                    "REQUIRED",
                    "customPersonaName is required when defaultPersona is custom.",
                )
            )
        custom_prompt = settings.custom_persona
        if custom_prompt is None or (
            isinstance(custom_prompt, str) and not custom_prompt.strip()
        ):
            errors.append(
                _error(
                    "customPersona",
                    "REQUIRED",
                    "customPersona is required when defaultPersona is custom.",
                )
            )

    if not isinstance(settings.auto_posting_enabled, bool):
        errors.append(
            _error("autoPostingEnabled", "INVALID_TYPE", "autoPostingEnabled must be a boolean.")
        )

    return errors


def assert_valid_user_settings_input(settings: UpsertUserSettingsInput) -> None:
    errors = validate_user_settings_input(settings)
    if errors:
        raise InvalidUserSettingsError(errors)


def parse_upsert_user_settings_body(body: Any, user_id: str) -> UpsertUserSettingsInput:
    """userId는 인증된 라우트에서 오고, 본문에서는 절대 오지 않는다."""
    source = body if isinstance(body, dict) else {}

    settings = UpsertUserSettingsInput(
        user_id=user_id,
        hashtag_count=source.get("hashtagCount"),
        article_length=source.get("articleLength", LIMITS.DEFAULT_ARTICLE_LENGTH),
        blend_mode=source.get("blendMode", LIMITS.DEFAULT_BLEND_MODE),
        # imageMode는 더 이상 받지 않는다 — AI 이미지는 항상 생성한다. 구형 클라이언트가
        # 보내와도 extra 키로 무시된다.
        default_persona=source.get("defaultPersona"),
        custom_persona_name=source.get("customPersonaName"),
        custom_persona_description=source.get("customPersonaDescription"),
        custom_persona=source.get("customPersona"),
        auto_posting_enabled=source.get("autoPostingEnabled"),
    )
    assert_valid_user_settings_input(settings)
    return settings
