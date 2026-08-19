"""페르소나 wire 모델.

공용 프리셋의 단일 정의는 modules/persona/catalog.py에 있고 `persona` 컬렉션은 그 값을
반영한다. userSettings.defaultPersona에는 프리셋 ID 또는 ``custom``을 저장한다.
"""

from typing import Literal

from .base import CamelModel

# 커스텀 페르소나를 고른 상태를 나타내는 특수 id. 이때 실제 프롬프트는 사용자 설정의
# customPersona 필드에서 온다(프리셋 컬렉션이 아니라).
CUSTOM_PERSONA_ID = "custom"


class Persona(CamelModel):
    persona_id: str
    name: str
    description: str
    prompt: str


class PersonaCatalogItem(CamelModel):
    """설정 화면에서 선택할 수 있는 페르소나 항목.

    공용 프리셋만 ``prompt``를 가진다. ``custom`` 항목은 선택지의 메타데이터만
    제공하고 실제 이름·설명·프롬프트는 사용자별 userSettings에 저장한다.
    """

    persona_id: str
    name: str
    description: str
    kind: Literal["preset", "custom"]
    prompt: str | None = None
