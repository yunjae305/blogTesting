"""사용자 설정(페르소나, 해시태그 수, 자동 발행 여부) 저장과 조회."""

from dataclasses import replace

from app.errors import InvalidUserSettingsError
from app.shared.format import now_iso as _now

from app.shared import CUSTOM_PERSONA_ID, UserSettings, UserSettingsValidationError

from app.modules.persona.service import PersonaSelectionError, PersonaService

from .repository import UserSettingsRepository
from .validation import UpsertUserSettingsInput, assert_valid_user_settings_input


class UserSettingsService:
    def __init__(
        self,
        repository: UserSettingsRepository,
        persona_service: PersonaService,
    ):
        self._repository = repository
        self._personas = persona_service

    async def get_by_user_id(self, user_id: str) -> UserSettings | None:
        return await self._repository.find_by_user_id(user_id)

    async def save(self, settings_input: UpsertUserSettingsInput) -> UserSettings:
        assert_valid_user_settings_input(settings_input)
        try:
            selection = await self._personas.normalize_selection(
                settings_input.default_persona,
                settings_input.custom_persona,
            )
        except PersonaSelectionError as error:
            raise InvalidUserSettingsError(
                [
                    UserSettingsValidationError(
                        field=error.field,
                        code=error.code,
                        message=error.message,
                    )
                ]
            ) from error

        custom_name = settings_input.custom_persona_name
        custom_description = settings_input.custom_persona_description
        if selection.default_persona == CUSTOM_PERSONA_ID:
            # 구형 클라이언트의 전문 저장값을 custom으로 옮길 때 이름 필드가 없을 수 있다.
            # 새 custom 요청은 위 검증에서 이름을 필수로 받지만, 레거시 호환 변환에는
            # 설명 가능한 기본 이름을 채워 저장 형식을 완성한다.
            custom_name = (
                custom_name.strip()
                if isinstance(custom_name, str) and custom_name.strip()
                else "이전 커스텀 페르소나"
            )
            original_selection = settings_input.default_persona.strip()
            dormant_prompt = (
                settings_input.custom_persona.strip()
                if isinstance(settings_input.custom_persona, str)
                else ""
            )
            if (
                original_selection != CUSTOM_PERSONA_ID
                and dormant_prompt
                and dormant_prompt != original_selection
            ):
                # 현재 선택 전문 A와 보관 중이던 커스텀 초안 B를 한 슬롯으로 합칠 수 없다.
                # 실제 선택 A를 보존하고 B의 메타데이터를 A에 잘못 붙이지 않는다.
                custom_name = "이전 커스텀 페르소나"
                custom_description = None

        normalized_input = replace(
            settings_input,
            default_persona=selection.default_persona,
            custom_persona_name=custom_name,
            custom_persona_description=custom_description,
            custom_persona=selection.custom_persona,
        )
        now = _now()

        return await self._repository.upsert(
            UserSettings(
                user_id=normalized_input.user_id,
                hashtag_count=normalized_input.hashtag_count,
                article_length=normalized_input.article_length,
                blend_mode=normalized_input.blend_mode,
                default_persona=normalized_input.default_persona,
                custom_persona_name=normalized_input.custom_persona_name,
                custom_persona_description=normalized_input.custom_persona_description,
                custom_persona=normalized_input.custom_persona,
                auto_posting_enabled=normalized_input.auto_posting_enabled,
                # 저장소는 갱신 시 원래 createdAt을 보존한다.
                created_at=now,
                updated_at=now,
            )
        )
