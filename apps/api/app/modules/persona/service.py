"""페르소나 카탈로그 조회, 선택 정규화, 생성 프롬프트 해석."""

import re
from dataclasses import dataclass

from app.shared import CUSTOM_PERSONA_ID, Persona, PersonaCatalogItem

from .catalog import CUSTOM_PERSONA_OPTION, DEFAULT_PERSONA_IDS
from .repository import PersonaRepository


@dataclass(frozen=True)
class NormalizedPersonaSelection:
    """기존 wire 형식에 다시 저장할 정규화된 페르소나 선택값."""

    default_persona: str
    custom_persona: str | None


class PersonaSelectionError(ValueError):
    """새 설정으로 저장할 수 없는 페르소나 선택."""

    def __init__(self, field: str, code: str, message: str):
        super().__init__(message)
        self.field = field
        self.code = code
        self.message = message


_PRESET_ID_LIKE = re.compile(r"^p(?:[_-].*|\d.*)$", re.IGNORECASE)


class PersonaService:
    def __init__(self, repository: PersonaRepository):
        self._repository = repository

    async def list_presets(self) -> list[Persona]:
        """공용 카탈로그에 등록된 프리셋만 p_1~p_9 순서로 반환한다."""
        stored = await self._repository.list_all()
        by_id = {persona.persona_id: persona for persona in stored}
        return [by_id[persona_id] for persona_id in DEFAULT_PERSONA_IDS if persona_id in by_id]

    async def list_catalog(self) -> list[PersonaCatalogItem]:
        """공용 프리셋과 사용자 입력용 custom 선택 항목을 반환한다."""
        presets = [
            PersonaCatalogItem(
                persona_id=persona.persona_id,
                name=persona.name,
                description=persona.description,
                kind="preset",
                prompt=persona.prompt,
            )
            for persona in await self.list_presets()
        ]
        return [*presets, CUSTOM_PERSONA_OPTION]

    async def resolve_prompt(
        self,
        default_persona: str,
        custom_persona: str | None = None,
    ) -> str:
        """저장된 선택값을 생성 모델에 전달할 프롬프트 전문으로 해석한다.

        미등록 값은 옛 userSettings 문서가 프롬프트 전문을 defaultPersona에 저장하던
        형식일 수 있으므로 읽기 경로에서는 그대로 반환한다.
        """
        selection = (default_persona or "").strip()
        if selection == CUSTOM_PERSONA_ID:
            return custom_persona or ""

        persona = (
            await self._repository.get(selection)
            if selection in DEFAULT_PERSONA_IDS
            else None
        )
        return persona.prompt if persona is not None else selection

    async def normalize_selection(
        self,
        default_persona: str,
        custom_persona: str | None = None,
    ) -> NormalizedPersonaSelection:
        """새 저장 요청을 ID 기반 형식으로 정규화한다.

        현재 ID와 custom은 그대로 사용한다. 구형 클라이언트가 프리셋 전문을 보내면 해당
        ID로 바꾸고, 임의의 구형 전문은 customPersona로 옮긴다. p_999처럼 ID 모양인데
        등록되지 않은 값은 프롬프트로 오인하지 않고 거부한다.
        """
        selection = default_persona.strip()
        custom_prompt = custom_persona.strip() if isinstance(custom_persona, str) else None

        if selection == CUSTOM_PERSONA_ID:
            if not custom_prompt:
                raise PersonaSelectionError(
                    "customPersona",
                    "REQUIRED",
                    "customPersona is required when defaultPersona is custom.",
                )
            return NormalizedPersonaSelection(CUSTOM_PERSONA_ID, custom_prompt)

        persona = (
            await self._repository.get(selection)
            if selection in DEFAULT_PERSONA_IDS
            else None
        )
        if persona is not None:
            return NormalizedPersonaSelection(persona.persona_id, custom_prompt)

        for preset in await self.list_presets():
            if preset.prompt == selection:
                return NormalizedPersonaSelection(preset.persona_id, custom_prompt)

        if _PRESET_ID_LIKE.fullmatch(selection):
            raise PersonaSelectionError(
                "defaultPersona",
                "INVALID_RANGE",
                "defaultPersona must reference a registered persona id or custom.",
            )

        # 구형 커스텀 전문을 현재 저장 형식으로 옮긴다. defaultPersona가 현재 실제 선택이고
        # 별도 customPersona는 보관 중인 초안일 수 있으므로 selection을 우선한다.
        return NormalizedPersonaSelection(
            CUSTOM_PERSONA_ID,
            selection,
        )
