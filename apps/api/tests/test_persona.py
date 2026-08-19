"""공용 페르소나 카탈로그, 저장소, 선택값 해석."""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.persona import (
    CUSTOM_PERSONA_OPTION,
    DEFAULT_PERSONAS,
    InMemoryPersonaRepository,
    MongoPersonaRepository,
    PERSONA_CATALOG,
    PersonaSelectionError,
    PersonaService,
)


def _service() -> PersonaService:
    return PersonaService(InMemoryPersonaRepository())


async def test_repository_is_seeded_with_all_presets():
    repo = InMemoryPersonaRepository()
    personas = await repo.list_all()
    assert len(personas) == len(DEFAULT_PERSONAS) == 9
    one = await repo.get("p_3")
    assert one is not None and "제품 비교 리뷰어" in one.prompt


def test_catalog_includes_custom_as_a_real_selectable_item():
    assert len(PERSONA_CATALOG) == 10
    assert PERSONA_CATALOG[-1] == CUSTOM_PERSONA_OPTION
    assert CUSTOM_PERSONA_OPTION.persona_id == "custom"
    assert CUSTOM_PERSONA_OPTION.kind == "custom"
    assert CUSTOM_PERSONA_OPTION.prompt is None


async def test_service_lists_only_presets_in_catalog_order():
    personas = await _service().list_presets()
    assert [persona.persona_id for persona in personas] == [f"p_{index}" for index in range(1, 10)]


async def test_service_catalog_appends_custom_after_prompt_bearing_presets():
    catalog = await _service().list_catalog()
    assert [item.persona_id for item in catalog] == [
        *(f"p_{index}" for index in range(1, 10)),
        "custom",
    ]
    assert all(item.kind == "preset" and item.prompt for item in catalog[:-1])
    assert catalog[-1].kind == "custom"
    assert catalog[-1].prompt is None


async def test_resolve_preset_id_to_prompt():
    # 문구 개정에 흔들리지 않게 페르소나 이름으로 시작하는지만 고정한다(배선 검증이 목적).
    prompt = await _service().resolve_prompt("p_6")
    assert prompt.startswith("트렌드 에디터")


async def test_resolve_custom_uses_custom_persona_text():
    prompt = await _service().resolve_prompt("custom", "내 전용 말투로 쓴다")
    assert prompt == "내 전용 말투로 쓴다"


async def test_resolve_unknown_value_preserves_legacy_prompt():
    legacy = "옛날에 저장된 페르소나 프롬프트 전문"
    assert await _service().resolve_prompt(legacy) == legacy


async def test_normalize_legacy_preset_prompt_to_id():
    selection = await _service().normalize_selection(DEFAULT_PERSONAS[5].prompt)
    assert selection.default_persona == "p_6"
    assert selection.custom_persona is None


async def test_normalize_legacy_custom_prompt_to_custom_fields():
    selection = await _service().normalize_selection("옛 커스텀 말투로 쓴다")
    assert selection.default_persona == "custom"
    assert selection.custom_persona == "옛 커스텀 말투로 쓴다"


async def test_normalize_legacy_active_prompt_wins_over_dormant_custom_draft():
    selection = await _service().normalize_selection(
        "현재 실제로 사용하는 구형 말투",
        "예전에 보관한 다른 커스텀 초안",
    )
    assert selection.default_persona == "custom"
    assert selection.custom_persona == "현재 실제로 사용하는 구형 말투"


async def test_normalize_rejects_unknown_preset_like_id():
    for unknown_id in ("p_999", "p_999!", "p_999 extra", "p-bad.value"):
        with pytest.raises(PersonaSelectionError) as caught:
            await _service().normalize_selection(unknown_id)
        assert caught.value.field == "defaultPersona"
        assert caught.value.code == "INVALID_RANGE"


class _Cursor:
    def __init__(self, documents: list[dict]):
        self._documents = documents

    async def to_list(self, length=None) -> list[dict]:
        return self._documents


class _PersonaCollection:
    def __init__(self):
        self.documents = {
            "user_owned": {
                "_id": "user_owned",
                "name": "별도 문서",
                "description": "카탈로그 관리 대상 아님",
                "prompt": "삭제되면 안 된다",
            }
        }

    async def update_one(self, filter_: dict, update: dict, upsert: bool = False) -> None:
        persona_id = filter_["_id"]
        self.documents[persona_id] = {
            "_id": persona_id,
            **update["$set"],
        }

    def find(self, filter_: dict) -> _Cursor:
        ids = set(filter_["_id"]["$in"])
        return _Cursor([doc for key, doc in self.documents.items() if key in ids])

    async def find_one(self, filter_: dict) -> dict | None:
        return self.documents.get(filter_["_id"])


class _Database:
    def __init__(self):
        self.collection = _PersonaCollection()

    def __getitem__(self, name: str) -> _PersonaCollection:
        assert name == "persona"
        return self.collection


async def test_mongo_seed_preserves_unmanaged_documents_and_lists_only_catalog():
    database = _Database()
    repository = MongoPersonaRepository(database)  # type: ignore[arg-type]

    await repository.seed_defaults()

    assert "user_owned" in database.collection.documents
    assert "custom" not in database.collection.documents
    assert [persona.persona_id for persona in await repository.list_all()] == [
        f"p_{index}" for index in range(1, 10)
    ]
    # 보존은 하되 공용 프리셋처럼 해석하거나 API에 노출하지 않는다.
    assert await PersonaService(repository).resolve_prompt("user_owned") == "user_owned"


async def test_get_personas_returns_camel_case_catalog_with_custom_descriptor():
    app = create_app()
    app.state.services = SimpleNamespace(persona_service=_service())
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/personas")

    assert response.status_code == 200
    payload = response.json()
    assert [persona["personaId"] for persona in payload] == [
        *(f"p_{index}" for index in range(1, 10)),
        "custom",
    ]
    assert all(persona["kind"] == "preset" for persona in payload[:-1])
    assert payload[-1] == {
        "personaId": "custom",
        "name": "커스텀 페르소나",
        "description": "이름과 설명, 작성 지침을 직접 설정합니다.",
        "kind": "custom",
    }
