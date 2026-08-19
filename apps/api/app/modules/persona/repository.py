"""공용 페르소나 프리셋 저장소."""

from typing import Protocol

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.shared import Persona

from .catalog import DEFAULT_PERSONA_IDS, DEFAULT_PERSONAS


class PersonaRepository(Protocol):
    async def list_all(self) -> list[Persona]: ...
    async def get(self, persona_id: str) -> Persona | None: ...
    async def seed_defaults(self) -> None: ...


class InMemoryPersonaRepository:
    """프리셋으로 미리 채워져 있다. Mongo 없이(로컬·테스트) 그대로 해석에 쓴다."""

    def __init__(self, personas: list[Persona] | tuple[Persona, ...] | None = None):
        source = personas if personas is not None else DEFAULT_PERSONAS
        self._by_id: dict[str, Persona] = {p.persona_id: p for p in source}

    async def list_all(self) -> list[Persona]:
        return list(self._by_id.values())

    async def get(self, persona_id: str) -> Persona | None:
        return self._by_id.get(persona_id)

    async def seed_defaults(self) -> None:
        for persona in DEFAULT_PERSONAS:
            self._by_id.setdefault(persona.persona_id, persona)


class MongoPersonaRepository:
    """`persona` 컬렉션. 문서 _id = persona_id."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self._collection = db["persona"]

    def _to_persona(self, document: dict | None) -> Persona | None:
        if not document:
            return None
        return Persona(
            persona_id=document["_id"],
            name=document["name"],
            description=document.get("description", ""),
            prompt=document["prompt"],
        )

    async def list_all(self) -> list[Persona]:
        # 이 컬렉션에 관리 대상이 아닌 문서가 있어도 공용 카탈로그에는 노출하지 않는다.
        cursor = self._collection.find({"_id": {"$in": list(DEFAULT_PERSONA_IDS)}})
        documents = await cursor.to_list(length=None)
        by_id = {
            persona.persona_id: persona
            for persona in (self._to_persona(document) for document in documents)
            if persona is not None
        }
        return [by_id[persona_id] for persona_id in DEFAULT_PERSONA_IDS if persona_id in by_id]

    async def get(self, persona_id: str) -> Persona | None:
        return self._to_persona(await self._collection.find_one({"_id": persona_id}))

    async def seed_defaults(self) -> None:
        """프리셋을 upsert한다. 카탈로그 밖 문서는 임의로 삭제하지 않는다."""
        for persona in DEFAULT_PERSONAS:
            await self._collection.update_one(
                {"_id": persona.persona_id},
                {
                    "$set": {
                        "name": persona.name,
                        "description": persona.description,
                        "prompt": persona.prompt,
                    }
                },
                upsert=True,
            )
