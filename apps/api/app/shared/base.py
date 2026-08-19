"""통신 또는 Mongo에 오가는 모든 것의 기반 모델.

원래 TypeScript 버전은 camelCase 필드명(`postId`, `hashtagCount`)으로 저장하고
내보냈다. 기존 문서와 기존 프런트엔드가 모두 그 표기에 의존하므로, 파이썬 쪽
snake_case는 내부 사정일 뿐이다. 모든 모델은 camelCase 별칭으로 직렬화한다.
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        # Mongo 문서에는 우리가 모델링하지 않는 _id가 있다. 거부하지 말고 무시한다.
        extra="ignore",
    )

    def to_wire(self) -> dict:
        """설정되지 않은 옵션은 뺀 camelCase 딕셔너리 — TS의 JSON 형태와 맞춘다."""
        return self.model_dump(by_alias=True, exclude_none=True)
