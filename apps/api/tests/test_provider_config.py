"""test.ts.

Rewritten when the mocks were deleted: there is no longer a downgrade to assert
on, so what these tests pin is that a role which cannot run live stops the
process instead of quietly serving something fake.
"""

import json

import pytest

from app.llm import (
    LlmConfigError,
    create_llm_providers,
    read_api_key,
    resolve_llm_config,
)

# Deliberately unlike real credentials: no `sk-`/`AIza` prefix, so secret
# scanners do not flag this file.
ALL_KEYS = {
    "ANTHROPIC_API_KEY": "test-anthropic-credential",
    "OPENAI_API_KEY": "test-openai-credential",
    "GOOGLE_API_KEY": "test-google-credential",
    "SERPAPI_API_KEY": "test-serpapi-credential",
}


def role_for(config, role: str):
    return next(entry for entry in config.roles if entry.role.value == role)


def status_for(status, role: str):
    return next(entry for entry in status if entry.role == role)


class TestResolveLlmConfig:
    def test_assigns_the_adr004_provider_and_model_per_role(self):
        config = resolve_llm_config({})

        assert role_for(config, "m3-collect").provider.value == "gemini"
        assert role_for(config, "m3-summary").provider.value == "gemini"
        assert role_for(config, "m4-draft").provider.value == "anthropic"
        assert role_for(config, "m5-image").provider.value == "openai"
        # 2026-08-05 사용자 결정. 느려서 45초 상한을 넘기는 회차가 있지만(실측 2/5),
        # 그때는 형제 모델로 넘어간다 — provider_config의 주석 참고.
        assert role_for(config, "m3-collect").model == "gemini-3.6-flash"
        assert role_for(config, "m3-summary").model == "gemini-3.6-flash"
        assert role_for(config, "m4-draft").model == "claude-opus-5"

    def test_reports_no_credentials_when_keys_absent(self):
        config = resolve_llm_config({})

        assert role_for(config, "m4-draft").has_credentials is False
        assert role_for(config, "m4-draft").api_key_env == "ANTHROPIC_API_KEY"

    def test_picks_up_each_providers_key(self):
        config = resolve_llm_config(ALL_KEYS)

        assert role_for(config, "m3-collect").api_key == ALL_KEYS["GOOGLE_API_KEY"]
        assert role_for(config, "m3-summary").api_key == ALL_KEYS["GOOGLE_API_KEY"]
        assert role_for(config, "m4-draft").api_key == ALL_KEYS["ANTHROPIC_API_KEY"]
        assert role_for(config, "m5-image").api_key == ALL_KEYS["OPENAI_API_KEY"]
        assert config.trend.api_key == ALL_KEYS["SERPAPI_API_KEY"]
        assert all(role.has_credentials for role in config.roles)
        assert config.trend.has_credentials is True

    @pytest.mark.parametrize(
        "placeholder", ["", "  ", "<your-key>", "your-api-key", "changeme", "xxxxx", "TODO"]
    )
    def test_treats_placeholder_as_no_key(self, placeholder):
        assert read_api_key({"ANTHROPIC_API_KEY": placeholder}, "ANTHROPIC_API_KEY") is None

    def test_trims_a_real_key(self):
        assert (
            read_api_key(
                {"ANTHROPIC_API_KEY": "  test-anthropic-credential  "}, "ANTHROPIC_API_KEY"
            )
            == "test-anthropic-credential"
        )

    def test_provider_and_model_can_be_overridden_per_role(self):
        config = resolve_llm_config(
            {"M4_DRAFT_PROVIDER": "openai", "M4_DRAFT_MODEL": "some-model"}
        )

        assert role_for(config, "m4-draft").provider.value == "openai"
        assert role_for(config, "m4-draft").model == "some-model"
        assert role_for(config, "m4-draft").api_key_env == "OPENAI_API_KEY"

    def test_rejects_an_unknown_provider(self):
        with pytest.raises(LlmConfigError):
            resolve_llm_config({"M4_DRAFT_PROVIDER": "skynet"})

    def test_mock_is_no_longer_a_provider(self):
        with pytest.raises(LlmConfigError):
            resolve_llm_config({"M4_DRAFT_PROVIDER": "mock"})


class TestCreateLlmProviders:
    def test_builds_every_role_live_when_the_keys_are_there(self):
        providers = create_llm_providers(resolve_llm_config(ALL_KEYS))

        assert providers.web_search_analyzer is not None
        assert providers.draft_generator is not None
        assert providers.post_image_generator is not None
        assert providers.trend_provider is not None
        assert status_for(providers.status, "m4-draft").provider == "anthropic"

    def test_refuses_to_start_when_a_key_is_missing(self):
        """This is the whole point of deleting the mocks. An expired key used to
        produce a fake article that reached the publish screen looking real."""
        keys = {k: v for k, v in ALL_KEYS.items() if k != "ANTHROPIC_API_KEY"}

        with pytest.raises(LlmConfigError, match="ANTHROPIC_API_KEY is not set"):
            create_llm_providers(resolve_llm_config(keys))

    def test_names_every_missing_key_at_once(self):
        """One restart per missing key would be a miserable way to find out."""
        with pytest.raises(LlmConfigError) as error:
            create_llm_providers(resolve_llm_config({}))

        message = str(error.value)
        for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
            assert env in message
        assert "M2 트렌드 키워드" not in message

    def test_refuses_a_provider_with_no_live_adapter(self):
        with pytest.raises(LlmConfigError, match="no live openai adapter"):
            create_llm_providers(
                resolve_llm_config({**ALL_KEYS, "M4_DRAFT_PROVIDER": "openai"})
            )

    def test_trends_still_run_without_a_paid_trend_key(self):
        """SerpApi 키가 없어도 트렌드는 돈다 (2026-07-30, 2026-08-07 갱신).

        예전에는 유일한 트렌드 키(SERPAPI)가 없으면 소스를 하나도 등록하지 않아 트렌드가
        통째로 비활성이었다. 이제 구글은 키를 쓰지 않는다 — 트렌드 페이지를 브라우저로
        직접 읽으므로(SerpApi 크레딧은 실제로 소진됐고, RSS는 검색량 말고는 주지 않는다),
        키가 있든 없든 같은 경로로 돈다.
        """
        keys = {k: v for k, v in ALL_KEYS.items() if k != "SERPAPI_API_KEY"}

        providers = create_llm_providers(resolve_llm_config(keys))
        trend = status_for(providers.status, "m2-trend")

        assert providers.trend_provider is not None
        assert trend.provider == "multi"
        # 어느 경로로 도는지가 상태에 보여야 한다 — 조용한 성능 저하가 되지 않도록.
        assert "google_trends(web)" in trend.model
        # 키가 빠졌다고 대체 경로를 알릴 것이 없다 — 애초에 키를 안 쓴다.
        assert "SERPAPI" not in trend.note

    def test_trend_collection_still_degrades_when_only_some_sources_are_set(self):
        """The trend sources are independent, so a missing Instagram token leaves
        the others collecting. Having none of them is what is refused."""
        providers = create_llm_providers(resolve_llm_config(ALL_KEYS))
        trend = status_for(providers.status, "m2-trend")

        assert trend.model == "google_trends(web)"
        assert "FACEBOOK_USER_ACCESS_TOKEN" in trend.note

    def test_never_leaks_a_key_into_the_loggable_status(self):
        providers = create_llm_providers(resolve_llm_config(ALL_KEYS))
        serialized = json.dumps([entry.__dict__ for entry in providers.status])

        for key in ALL_KEYS.values():
            assert key not in serialized, f"status leaked {key}"
