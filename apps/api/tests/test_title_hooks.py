"""제목 후킹 프레임워크의 프롬프트·스키마·채점 동작을 지킨다.

후킹은 기존 검색 친화 제목 위에 '실제 내용이 뒷받침될 때만' 얹는 각도라는 것이 핵심이다.
그래서 (1) 목적에 맞는 후킹만 노출되고, (2) 참고자료가 없으면 근거가 필요한 후킹(권위·
스토리·반전)을 금지하며, (3) 근거 없는 과장 문구는 채점에서 감점되는지를 확인한다. 제목은
본문보다 먼저(M2) 만들어지므로, 선택된 제목의 약속을 원고(M4) 프롬프트가 요구하는지도 본다.
"""

from app.llm.prompts import (
    PURPOSE_HOOK_MAP,
    TITLE_HOOK_LIBRARY,
    draft_prompt,
    topic_prompt,
)
from app.llm.schemas import (
    TITLE_HOOK_STRENGTHS,
    TITLE_HOOK_TYPES,
    TOPIC_SCHEMA,
)
from app.modules.trend.topic_scoring import build_context, evaluate_rules
from app.shared import (
    BlogTaskInput,
    DraftFormat,
    DraftGenerationInput,
    DraftGenerationSettings,
    ReferenceMaterial,
    ReferenceMaterialType,
    SelectedIntentForDraft,
    TitleHookStrength,
    TitleHookType,
    TrendKeyword,
    TrendSource,
)
from app.llm.contracts import TopicGenerationInput


def _keyword(text: str = "월드컵") -> TrendKeyword:
    return TrendKeyword(
        trend_keyword_id="k1",
        keyword=text,
        source=TrendSource.GOOGLE_TRENDS,
        rank=1,
        score=100.0,
        collected_at="2026-07-24T00:00:00Z",
    )


def _topic_input(
    *,
    purpose: str = "비교·추천",
    materials: list[ReferenceMaterial] | None = None,
) -> TopicGenerationInput:
    return TopicGenerationInput(
        post_id="post_1",
        input=BlogTaskInput(
            topic="AIONA",
            subject="멀티 LLM 플랫폼",
            purpose=[purpose],
            keywords=["도입 검토"],
            target_reader="AI 도입을 고민하는 실무자",
            reference_materials=materials or [],
        ),
        trend_keyword=_keyword(),
        settings=DraftGenerationSettings(hashtag_count=5, default_persona="실무 코치"),
        exclude_titles=[],
    )


class TestSchemaAndEnumParity:
    def test_schema_enums_match_shared_enums(self):
        # 스키마가 모델을 제약하는 집합과 파이썬 enum이 어긋나면, 어댑터가 유효한 값을
        # 조용히 버린다. 두 정의는 한 몸이어야 한다.
        assert set(TITLE_HOOK_TYPES) == {h.value for h in TitleHookType}
        assert set(TITLE_HOOK_STRENGTHS) == {s.value for s in TitleHookStrength}

    def test_schema_requires_hook_fields(self):
        item = TOPIC_SCHEMA["properties"]["topicCandidates"]["items"]
        assert set(item["required"]) == {"title", "titleType", "hookType", "hookStrength"}
        assert item["properties"]["hookType"]["enum"] == list(TITLE_HOOK_TYPES)
        assert item["properties"]["hookStrength"]["enum"] == list(TITLE_HOOK_STRENGTHS)

    def test_every_library_hook_is_a_valid_schema_type(self):
        for hook in TITLE_HOOK_LIBRARY:
            assert hook.code in TITLE_HOOK_TYPES

    def test_purpose_map_covers_the_app_purposes_with_valid_hooks(self):
        # 목적 라벨은 PURPOSE_GUIDES(=UI 목적 값)와 맞아야 조회가 빗나가지 않는다.
        from app.llm.prompts import PURPOSE_GUIDES

        assert set(PURPOSE_HOOK_MAP) == set(PURPOSE_GUIDES)
        for hooks in PURPOSE_HOOK_MAP.values():
            for code in hooks:
                assert code in TITLE_HOOK_TYPES


class TestTopicPromptHooks:
    def test_only_purpose_appropriate_hooks_are_offered(self):
        # 비교·추천 목적: COMPARISON은 후킹 설명 줄에 나오고, 트렌드 전용 FOMO는 나오지 않는다.
        # (스키마 enum에는 전 유형이 박히므로, 후킹 설명 줄의 '코드(라벨)' 형식으로 검사한다.)
        prompt = topic_prompt(_topic_input(purpose="비교·추천"))
        assert "COMPARISON(비교)" in prompt
        assert "NONE(기본" in prompt  # NONE은 항상 제공
        assert "FOMO(시의성)" not in prompt

    def test_introduction_title_stays_beginner_friendly_instead_of_promotional(self):
        prompt = topic_prompt(_topic_input(purpose="입문·소개"))

        assert "처음 접하는 독자" in prompt
        assert "IDENTITY(정체성)" in prompt
        assert "COMPARISON(비교)" not in prompt
        assert "FOMO(시의성)" not in prompt
        assert "STORY(스토리)" not in prompt

    def test_no_reference_material_forbids_evidence_hooks(self):
        prompt = topic_prompt(_topic_input(purpose="후기·리뷰 작성", materials=[]))
        # 후기 목적은 STORY를 추천하지만, 근거(경험)가 없으면 쓰지 말라고 명시해야 한다.
        assert "참고자료 없음" in prompt
        assert "AUTHORITY" in prompt and "STORY" in prompt and "REVERSAL" in prompt
        assert "쓰지 않는다" in prompt

    def test_reference_material_lists_the_kinds_present(self):
        prompt = topic_prompt(
            _topic_input(
                purpose="후기·리뷰 작성",
                materials=[ReferenceMaterial(type=ReferenceMaterialType.URL, value="https://x")],
            )
        )
        assert "참고자료 있음(URL)" in prompt

    def test_prompt_grades_hook_strength_across_five_candidates(self):
        prompt = topic_prompt(_topic_input())
        assert "hookStrength" in prompt
        # 1~2번 기본, HIGH는 근거가 있을 때만 — 강도 배분 지시가 들어 있어야 한다.
        assert "NONE" in prompt and "MEDIUM" in prompt and "HIGH" in prompt

    def test_prompt_bans_the_spec_forbidden_phrases(self):
        prompt = topic_prompt(_topic_input())
        assert "상위 1%" in prompt  # 금지 예시로 명시
        assert "인생이 바뀝니다" in prompt


class TestScoringForbiddenPhrases:
    def _ctx(self):
        return build_context(
            topic="AIONA",
            subject="멀티 LLM 플랫폼",
            purpose=["정보 전달"],
            audience="실무자",
            trend_keyword="월드컵",
        )

    def test_new_spec_phrases_are_penalized_as_clickbait(self):
        clean = evaluate_rules("AIONA로 월드컵 콘텐츠 만드는 법", self._ctx())
        bait = evaluate_rules("상위 1%만 아는 AIONA, 인생이 바뀝니다", self._ctx())
        assert bait.is_clickbait and not clean.is_clickbait
        assert bait.quality < clean.quality


class TestDraftPromiseRule:
    def _draft_input(self, trend_title: str | None) -> DraftGenerationInput:
        return DraftGenerationInput(
            post_id="post_1",
            user_id="user_1",
            prompt_version="m4-draft@v1.0",
            format=DraftFormat.MARKDOWN,
            input=BlogTaskInput(topic="AIONA", keywords=["정보 전달"]),
            selected_intent=SelectedIntentForDraft(
                intent_id="i1",
                title="제목",
                target_reader="실무자",
                rationale="근거",
                sources=[],
            ),
            trend_title=trend_title,
        )

    def test_selected_title_promise_must_be_honored_in_the_body(self):
        prompt = draft_prompt(self._draft_input("AIONA와 경쟁 서비스, 무엇이 다를까"))
        assert "제목이 약속한 내용은 본문에서 반드시 확인" in prompt

    def test_no_trend_title_keeps_the_generic_title_rules(self):
        prompt = draft_prompt(self._draft_input(None))
        # 트렌드 제목이 없으면 약속 전달 규칙 대신 일반 제목 규칙이 붙는다.
        assert "제목이 약속한 내용은 본문에서 반드시 확인" not in prompt
