"""validation.test.ts.

Also covers the Mongo $unset path — clearing a custom persona has to actually
remove it from the document, which the TypeScript version got wrong.
"""

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

from app.errors import InvalidUserSettingsError
from app.modules.persona import InMemoryPersonaRepository, PersonaService
from app.modules.user_settings.repository import (
    InMemoryUserSettingsRepository,
    MongoUserSettingsRepository,
)
from app.modules.user_settings.service import UserSettingsService
from app.modules.user_settings.validation import (
    UpsertUserSettingsInput,
    parse_upsert_user_settings_body,
    validate_user_settings_input,
)

MONGO_TEST_URI = "mongodb://localhost:27017/blog_it_test"


def settings_service(repository) -> UserSettingsService:
    return UserSettingsService(
        repository,
        PersonaService(InMemoryPersonaRepository()),
    )


def base_input(**overrides) -> UpsertUserSettingsInput:
    defaults = dict(
        user_id="user_1",
        hashtag_count=5,
        default_persona="p_1",
        auto_posting_enabled=False,
    )
    return UpsertUserSettingsInput(**{**defaults, **overrides})


def test_accepts_custom_persona_metadata():
    errors = validate_user_settings_input(
        base_input(
            default_persona="custom",
            custom_persona_name="내 페르소나",
            custom_persona_description="설명",
            custom_persona="프롬프트",
        )
    )

    assert errors == []


def test_rejects_hashtag_counts_outside_1_to_10():
    for count in (0, 11):
        errors = validate_user_settings_input(base_input(hashtag_count=count))
        assert [e.code for e in errors] == ["INVALID_RANGE"]


def test_rejects_overlong_custom_persona_metadata():
    errors = validate_user_settings_input(
        base_input(
            custom_persona_name="n" * 81,
            custom_persona_description="d" * 201,
            custom_persona="p" * 1201,
        )
    )

    assert {e.field for e in errors} == {
        "customPersonaName",
        "customPersonaDescription",
        "customPersona",
    }
    assert all(e.code == "TOO_LONG" for e in errors)


def test_custom_selection_requires_a_nonblank_prompt():
    for custom_persona in (None, "", "   "):
        errors = validate_user_settings_input(
            base_input(default_persona="custom", custom_persona=custom_persona)
        )
        assert any(
            error.field == "customPersona" and error.code == "REQUIRED"
            for error in errors
        )


def test_custom_selection_requires_a_nonblank_name():
    for custom_name in (None, "", "   "):
        errors = validate_user_settings_input(
            base_input(
                default_persona="custom",
                custom_persona_name=custom_name,
                custom_persona="내 말투로 쓴다",
            )
        )
        assert any(
            error.field == "customPersonaName" and error.code == "REQUIRED"
            for error in errors
        )


def test_accumulates_every_error_rather_than_stopping_at_the_first():
    errors = validate_user_settings_input(
        UpsertUserSettingsInput(
            user_id="",
            hashtag_count="not a number",
            default_persona="",
            auto_posting_enabled="nope",
        )
    )

    assert {e.field for e in errors} == {
        "userId",
        "hashtagCount",
        "defaultPersona",
        "autoPostingEnabled",
    }


def test_parse_body_takes_user_id_from_the_route_not_the_body():
    parsed = parse_upsert_user_settings_body(
        {
            "userId": "attacker",
            "hashtagCount": 5,
            "defaultPersona": "p_1",
            "autoPostingEnabled": True,
        },
        "user_real",
    )

    assert parsed.user_id == "user_real"


def test_parse_body_raises_on_invalid_input():
    with pytest.raises(InvalidUserSettingsError):
        parse_upsert_user_settings_body({}, "user_1")


async def test_save_preserves_created_at_across_updates():
    service = settings_service(InMemoryUserSettingsRepository())

    first = await service.save(base_input())
    second = await service.save(base_input(hashtag_count=9))

    assert second.created_at == first.created_at
    assert second.hashtag_count == 9


async def test_save_rejects_an_unknown_preset_id():
    service = settings_service(InMemoryUserSettingsRepository())

    with pytest.raises(InvalidUserSettingsError) as caught:
        await service.save(base_input(default_persona="p_999"))

    assert [(error.field, error.code) for error in caught.value.errors] == [
        ("defaultPersona", "INVALID_RANGE")
    ]


async def test_save_normalizes_a_legacy_custom_prompt():
    service = settings_service(InMemoryUserSettingsRepository())

    saved = await service.save(base_input(default_persona="예전 형식의 커스텀 말투"))

    assert saved.default_persona == "custom"
    assert saved.custom_persona_name == "이전 커스텀 페르소나"
    assert saved.custom_persona == "예전 형식의 커스텀 말투"


async def test_legacy_active_prompt_does_not_inherit_dormant_draft_metadata():
    service = settings_service(InMemoryUserSettingsRepository())

    saved = await service.save(
        base_input(
            default_persona="현재 선택된 구형 말투",
            custom_persona_name="다른 초안 이름",
            custom_persona_description="다른 초안 설명",
            custom_persona="보관 중이던 다른 말투",
        )
    )

    assert saved.default_persona == "custom"
    assert saved.custom_persona == "현재 선택된 구형 말투"
    assert saved.custom_persona_name == "이전 커스텀 페르소나"
    assert saved.custom_persona_description is None


async def test_mongo_upsert_clears_a_custom_persona_that_was_removed():
    """The bug the TypeScript version had: ignoreUndefined dropped the field from
    $set, so the previously-saved value survived."""
    client = AsyncIOMotorClient(MONGO_TEST_URI, serverSelectionTimeoutMS=1500)
    try:
        await client.admin.command("ping")
    except Exception:
        client.close()
        pytest.skip("MongoDB is not reachable")

    try:
        db = client.get_default_database()
        # 설정은 users 문서 안에 산다. 저장소는 upsert하지 않으므로(없는 사용자에게
        # 반쪽짜리 계정 문서를 만들지 않는다) 대상 사용자를 먼저 만들어 둔다.
        await db["users"].delete_many({"userId": "user_1"})
        await db["users"].insert_one({"userId": "user_1"})
        service = settings_service(MongoUserSettingsRepository(db))

        await service.save(base_input(custom_persona_name="지영", custom_persona="프롬프트"))
        stored = await service.get_by_user_id("user_1")
        assert stored.custom_persona_name == "지영"

        # User switches back to a preset persona and leaves the custom fields blank.
        await service.save(base_input())

        reloaded = await service.get_by_user_id("user_1")
        assert reloaded.custom_persona_name is None
        assert reloaded.custom_persona is None
    finally:
        client.close()
