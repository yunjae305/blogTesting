"""9단계 감사: 자기검증 메타 지시는 줄이고, 사실성·보안 규칙은 남긴다.

Opus 5 공식 문서: "이 모델은 지시하지 않아도 자기 작업을 검증한다. 이전 모델에서 가져온 검증
지시('마지막에 검증 단계를 넣어라')는 제거하라 — Opus 5에서는 과잉 검증을 일으킨다."

그래서 줄인 것은 **같은 판단을 두 번 하게 만드는 문장**뿐이다. 사실 범위·출처·보안을 제한하는
규칙은 자기검증이 아니므로 그대로 둔다. 이 테스트는 그 경계가 지켜지는지 본다.
"""

from app.llm import prompts
from app.llm.contracts import KeywordRelevanceInput, TopicGenerationInput
from app.shared import (
    BlogTaskInput,
    DraftFormat,
    DraftGenerationInput,
    DraftGenerationSettings,
    ReferenceEvidenceProfile,
    SelectedIntentForDraft,
    TrendKeyword,
    TrendSource,
)


def blog_input() -> BlogTaskInput:
    return BlogTaskInput(topic="제습기 관리", purpose=["문제 해결"], keywords=["제습기"])


def draft_input(**overrides) -> DraftGenerationInput:
    base = dict(
        post_id="post_1",
        user_id="user_1",
        input=blog_input(),
        selected_intent=SelectedIntentForDraft(
            intent_id="i1", title="제습기 관리", target_reader="1인 가구", rationale="근거"
        ),
        prompt_version="m4-draft@v2.0",
        format=DraftFormat.MARKDOWN,
        settings=DraftGenerationSettings(hashtag_count=5, default_persona="화자"),
    )
    base.update(overrides)
    return DraftGenerationInput(**base)


def topic_input() -> TopicGenerationInput:
    return TopicGenerationInput(
        post_id="post_1",
        input=blog_input(),
        trend_keyword=TrendKeyword(
            trend_keyword_id="kw_1",
            keyword="장마",
            source=TrendSource.NAVER_DATALAB,
            rank=1,
            score=1.0,
            collected_at="2026-07-30T00:00:00Z",
        ),
    )


class TestMetaInstructionsRemoved:
    def test_the_scorer_is_no_longer_told_to_think_privately(self):
        # thinking이 기본 ON이 된 뒤로는 틀린 문장이다 — API가 사고를 별도 블록으로 분리하는데
        # 그것을 숨기라고 지시하는 셈이 된다. 예전에는 thinking disabled라 무의미했다.
        assert "속으로" not in prompts.RELEVANCE_SYSTEM_PROMPT
        assert "JSON만 반환한다" in prompts.RELEVANCE_SYSTEM_PROMPT

    def test_the_title_step_does_not_ask_to_verify_a_body_that_does_not_exist_yet(self):
        # 제목은 원고보다 먼저 생성된다. "본문에서 확인될 수 있어야 한다"는 검증 대상이 없는
        # 검증 지시라, 모델이 자기 문장을 자기 근거로 삼게 된다.
        text = prompts.topic_prompt(topic_input())
        assert "본문에서 확인될 수 있어야 한다" not in text
        # 사실성 제약 자체는 남는다.
        assert "과장·허위·불필요한 공포를 뺀다" in text
        assert "본문에서 증명할 수 없는 비교·수치·경험·최신성은 제목으로 약속하지 않는다" in text

    def test_the_promise_rule_appears_once_not_three_times(self):
        # 예전에는 설계 블록·의도 앵커·제목 블록이 같은 요구를 최대 3벌 실었다.
        text = prompts.draft_prompt(draft_input())
        assert text.count("제목이 약속한 내용은") <= 1
        assert "제목과 도입부가 약속한 내용은" not in text

    def test_the_photo_plan_asks_the_same_question_only_once(self):
        # '무엇을 놓치는가'가 체크리스트와 채점 규칙에 두 번 들어 있었다.
        rules = prompts.card_plan_prompt(
            draft_input(),
            _post(),
            0,
            0,
        )
        assert rules.count("이 사진이 없으면 독자가 무엇을 놓치는가") == 1


class TestFactAndSecurityRulesKept:
    def test_the_draft_still_forbids_facts_outside_the_search_results(self):
        text = prompts.draft_prompt(draft_input())
        assert "검색 결과에 없는 사실·수치·통계·사례를 지어내지 않는다" in text
        assert "실측수치(dataPoints)가 제공된 값만 쓴다" in text

    def test_the_draft_no_longer_forbids_first_person_experience(self):
        """2026-08-03 사용자 결정으로 1인칭 체험 서술 금지를 걷어냈다.

        사실·수치 조작 금지는 위 테스트가 계속 지킨다 — 둘은 다른 문제다.
        """
        text = prompts.draft_prompt(draft_input())
        assert "1인칭 체험 서술을 지어내지 않는다" not in text
        assert "내돈내산" not in text

    def test_the_evidence_block_still_bounds_what_can_be_claimed(self):
        text = prompts.draft_prompt(
            draft_input(
                reference_evidence=ReferenceEvidenceProfile(
                    has_references=True, primary_entity="제습기"
                )
            )
        )
        assert "위 목록에 없는 제품 모델·기능·가격·사용 기간·성능을 새로 만들지 않는다" in text

    def test_the_keyword_scorer_keeps_its_injection_guard(self):
        text = prompts.keyword_relevance_prompt(
            KeywordRelevanceInput(input=blog_input(), keywords=["장마"], persona="화자")
        )
        assert prompts.INJECTION_GUARD in text

    def test_the_persona_boundary_is_still_in_place(self):
        assert prompts.PERSONA_DATA_GUARD in prompts.draft_prompt(draft_input())


class TestVagueHedgesReplaced:
    def test_the_avoid_list_has_no_escape_hatch(self):
        # avoid는 금지 목록인데 "가능한 한 피한다"가 예외를 허용했다. 배치 규칙은 원고
        # 프롬프트의 SEO 블록에 있으므로 계획이 있는 원고 프롬프트로 확인한다.
        from app.shared import SeoKeywordPlan

        text = prompts.draft_prompt(
            draft_input(
                seo_keyword_plan=SeoKeywordPlan(
                    primary="제습기 관리", secondary=["물통 냄새"], avoid=["최저가"]
                )
            )
        )
        assert "가능한 한 피한다" not in text
        assert "예외 없다" in text

    def test_the_length_rule_states_a_range_without_hedging(self):
        text = prompts.draft_prompt(draft_input())
        assert "억지로 맞추려 하지 말되" not in text
        assert "범위 안이면 되고" in text

    def test_jargon_has_a_count_and_a_length_instead_of_as_much_as_needed(self):
        joined = "\n".join(prompts.ARCHETYPE_STRUCTURES["EXPERT_EXPLAINER"])
        assert "필요한 만큼만" not in joined
        assert "섹션당 2개 이하" in joined


def _post():
    from app.shared import FinalPost

    return FinalPost(
        title="제습기 관리 순서",
        body="## 물통\n\n헹굽니다.",
        hashtags=["제습기"],
        html_content="<h2>물통</h2><p>헹굽니다.</p>",
        markdown_content="# 제습기 관리 순서\n\n## 물통\n\n헹굽니다.",
    )


class TestTheLengthTargetIsAlsoGivenInParagraphs:
    """실측(2026-08-03): 새 목표로 생성한 5편 중 3편이 상한을 넘었다(+136 ~ +365자).

    모델은 글자를 셀 수 없다. 셀 수 있는 단위(문단 수)로도 같은 목표를 준다.
    """

    def test_the_draft_prompt_states_both_units(self):
        text = prompts.draft_prompt(draft_input())
        assert "공백 포함 1800~2300자로 쓴다" in text
        assert "본문 문단 24~30개" in text
        # 상한을 넘길 때 무엇을 하라는지가 없으면 지시가 아니라 정보다.
        assert "30개를 넘어가면 새 내용을 더하지 말고" in text
