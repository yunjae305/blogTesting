"""콘텐츠 설계 캐시 키가 모델·effort·thinking을 담는지 (PDF 8단계).

왜 중요한가: 예전 키에는 모델이 없었다. 그래서 M4_DRAFT_MODEL을 바꿔도 키가 같아 **옛 모델이
만든 설계를 그대로 재사용**했다 — 모델을 바꾼 이유가 설계 품질이었다면 그 변경이 아무 효과가
없다. effort·thinking도 같은 이유로 넣었다(Anthropic 문서: 두 값은 프롬프트에 렌더링된다).
"""

import pytest

from app.llm.live_adapters import STAGE_BUDGETS, StageBudget
from app.modules.draft.service import DraftService
from app.shared import (
    BlogTaskInput,
    DraftFormat,
    DraftGenerationInput,
    SelectedIntentForDraft,
)


class _Role:
    def __init__(self, model: str):
        self.model = model


class _Generator:
    """설계 생성 능력이 있는 최소 어댑터. 키 계산에는 _role.model만 쓰인다."""

    def __init__(self, model: str = "claude-opus-5"):
        self._role = _Role(model)

    async def generate_content_plan(self, draft_input):  # pragma: no cover - 호출 안 함
        return None


def draft_input() -> DraftGenerationInput:
    return DraftGenerationInput(
        post_id="post_1",
        user_id="user_1",
        input=BlogTaskInput(topic="제습기 관리", purpose=["문제 해결"], keywords=["제습기"]),
        selected_intent=SelectedIntentForDraft(
            intent_id="i1", title="제습기 관리", target_reader="1인 가구", rationale="근거"
        ),
        prompt_version="m4-draft@v2.0",
        format=DraftFormat.MARKDOWN,
    )


def service(model: str = "claude-opus-5") -> DraftService:
    # 키 계산은 순수 함수라 저장소·설정 서비스가 필요 없다.
    return DraftService.__new__(DraftService)._with_generator(_Generator(model))


def _with_generator(self, generator):
    self._draft_generator = generator
    return self


DraftService._with_generator = _with_generator  # 테스트 전용 조립


def key(model: str = "claude-opus-5") -> str:
    return service(model)._plan_cache_key(draft_input())


class TestModelIsPartOfTheKey:
    def test_two_models_do_not_share_a_cached_plan(self):
        assert key("claude-opus-5") != key("claude-opus-4-6")

    def test_the_same_model_keeps_the_cache_hit(self):
        assert key("claude-opus-5") == key("claude-opus-5")


class TestEffortAndThinkingArePartOfTheKey:
    def test_changing_the_effort_invalidates_the_plan_cache(self):
        original = STAGE_BUDGETS["m4-content-plan"]
        before = key()
        # 원본이 무엇이든 '다른 effort'로 바꾼다 — 특정 값을 박아 두면 기본 effort를
        # 조정할 때(2026-08-10 high→low) 테스트가 같은 값으로 바꾸며 헛돈다.
        swapped = "high" if original.effort != "high" else "low"
        STAGE_BUDGETS["m4-content-plan"] = StageBudget(
            effort=swapped, max_tokens=original.max_tokens, thinking=original.thinking
        )
        try:
            assert key() != before
        finally:
            STAGE_BUDGETS["m4-content-plan"] = original

    def test_changing_the_thinking_mode_invalidates_the_plan_cache(self):
        original = STAGE_BUDGETS["m4-content-plan"]
        before = key()
        # effort는 원본 그대로 — 이 테스트는 thinking 하나만 바꿔야 한다.
        STAGE_BUDGETS["m4-content-plan"] = StageBudget(
            effort=original.effort, max_tokens=original.max_tokens, thinking="disabled"
        )
        try:
            assert key() != before
        finally:
            STAGE_BUDGETS["m4-content-plan"] = original

    def test_max_tokens_alone_does_not_invalidate_it(self):
        # 출력 상한은 설계 내용을 바꾸지 않는다(잘리지만 않으면). 키에 넣으면 여유를 조정할
        # 때마다 캐시가 통째로 날아간다.
        original = STAGE_BUDGETS["m4-content-plan"]
        before = key()
        STAGE_BUDGETS["m4-content-plan"] = StageBudget(
            effort=original.effort, max_tokens=original.max_tokens + 1000
        )
        try:
            assert key() == before
        finally:
            STAGE_BUDGETS["m4-content-plan"] = original


class TestOldAdaptersStillWork:
    def test_an_adapter_without_a_role_does_not_crash(self):
        class Bare:
            async def generate_content_plan(self, draft_input):  # pragma: no cover
                return None

        bare = DraftService.__new__(DraftService)._with_generator(Bare())
        assert isinstance(bare._plan_cache_key(draft_input()), str)

    def test_the_prompt_version_still_invalidates(self):
        other = draft_input().model_copy(update={"prompt_version": "m4-draft@v3.0"})
        assert service()._plan_cache_key(other) != key()


@pytest.mark.parametrize("field", ["topic", "purpose"])
def test_input_changes_still_invalidate(field: str):
    base = draft_input()
    if field == "topic":
        changed = base.model_copy(
            update={"input": base.input.model_copy(update={"topic": "로봇청소기"})}
        )
    else:
        changed = base.model_copy(
            update={"input": base.input.model_copy(update={"purpose": ["비교·추천"]})}
        )
    assert service()._plan_cache_key(changed) != key()
