"""SEO 키워드 계획 파싱·정규화(validateSeoKeywordPlan)와 원고 프롬프트 전달.

정규화가 곧 §3 검증이다: 빈 문자열 제거, primary/secondary 중복 제거, avoid에서 primary·
secondary와 겹치는 것 제거, 그리고 primary를 확정 제목이 노리는 핵심 검색 구문에 고정.
"""

from app.llm.parsing import align_seo_plan_with_title, seo_keyword_plan_from_json
from app.llm.prompts import draft_prompt, seo_keyword_plan_prompt
from app.shared import (
    BlogTaskInput,
    DraftFormat,
    DraftGenerationInput,
    SelectedIntentForDraft,
    SeoKeywordPlan,
    TitlePlan,
)


def _payload(primary="아이폰17", secondary=None, avoid=None):
    return {
        "seoKeywordPlan": {
            "primary": primary,
            "secondary": ["아이폰17 출시일", "아이폰17 가격", "아이폰17 주요 기능"]
            if secondary is None
            else secondary,
            "avoid": ["아이폰16 중고", "갤럭시 할인"] if avoid is None else avoid,
        }
    }


def _title_plan(title="아이폰17 출시일과 가격 총정리", keyword="아이폰17"):
    return TitlePlan(
        primary_title=title,
        h1=title,
        primary_keyword=keyword,
        title_strategy="SEARCH_INTENT",
    )


class TestNormalization:
    def test_normal_plan_parses_to_the_three_fields(self):
        plan = seo_keyword_plan_from_json(_payload())

        assert plan is not None
        assert plan.primary == "아이폰17"
        assert isinstance(plan.secondary, list) and plan.secondary
        assert isinstance(plan.avoid, list) and plan.avoid

    def test_empty_primary_without_title_plan_is_dropped(self):
        assert seo_keyword_plan_from_json(_payload(primary="   ")) is None

    def test_broken_shape_returns_none(self):
        assert seo_keyword_plan_from_json({"nope": 1}) is None
        assert seo_keyword_plan_from_json("not a dict") is None

    def test_empty_strings_are_removed_from_arrays(self):
        plan = seo_keyword_plan_from_json(
            _payload(secondary=["아이폰17 가격", "", "  "], avoid=["", "갤럭시"])
        )

        assert "" not in plan.secondary and "" not in plan.avoid
        assert plan.secondary == ["아이폰17 가격"]
        assert plan.avoid == ["갤럭시"]

    def test_secondary_duplicates_and_primary_overlap_are_removed(self):
        plan = seo_keyword_plan_from_json(
            _payload(
                primary="아이폰17",
                # 조사·띄어쓰기만 다른 것은 중복으로 본다. primary와 같은 것도 뺀다.
                secondary=["아이폰 17", "아이폰17", "아이폰17 가격", "아이폰17가격"],
            )
        )

        assert plan.secondary == ["아이폰17 가격"]

    def test_avoid_drops_terms_that_equal_primary_or_secondary(self):
        plan = seo_keyword_plan_from_json(
            _payload(
                primary="아이폰17",
                secondary=["아이폰17 가격"],
                avoid=["아이폰17", "아이폰17 가격", "갤럭시 할인"],
            )
        )

        assert plan.avoid == ["갤럭시 할인"]


class TestPrimaryAnchoring:
    def test_primary_is_pinned_to_the_title_keyword_when_model_drifts(self):
        """모델이 제목에 없는 primary를 내면, 제목이 노리는 핵심 검색 구문으로 고정한다 —
        그래야 생성 후 seo_primary_in_title 검사가 항상 통과한다."""
        plan = seo_keyword_plan_from_json(
            _payload(primary="전혀 다른 키워드"),
            title_plan=_title_plan(keyword="아이폰17"),
        )

        assert plan.primary == "아이폰17"

    def test_model_primary_is_kept_when_it_is_actually_in_the_title(self):
        plan = seo_keyword_plan_from_json(
            _payload(primary="아이폰17 출시일"),
            title_plan=_title_plan(title="아이폰17 출시일과 가격 총정리", keyword="아이폰17"),
        )

        # 제목 안에 실제로 들어 있는 표현이면 모델의 선택을 존중한다.
        assert plan.primary == "아이폰17 출시일"

    def test_a_reordered_keyword_is_pinned_to_a_phrase_that_is_in_the_title(self):
        """제목의 단어를 순서만 바꾼 키워드('월드투어…BTS' 제목에 'BTS 월드투어')는 제목
        어디에도 없다. 제목은 사용자가 고른 값이라 바꿀 수 없으므로 키워드를 제목 안에서
        고른다 — 이걸 안 하면 생성 후 검증이 매번 같은 이유로 원고를 반려한다."""
        title = "월드투어 다시 시작하는 BTS, 일정과 배경 살펴보기"
        plan = seo_keyword_plan_from_json(
            _payload(primary="BTS 월드투어", secondary=["BTS 콘서트"], avoid=[]),
            title_plan=_title_plan(title=title, keyword="BTS 월드투어"),
        )

        assert plan.primary == "월드투어"
        assert plan.primary in title
        # 밀려난 키워드는 버리지 않는다 — 제목에는 못 들어가도 본문에서는 쓰인다.
        assert plan.secondary[0] == "BTS 월드투어"

    def test_a_chosen_title_pins_the_primary_even_without_a_title_plan(self):
        """제목 계획을 만들지 못한 글에도 M2에서 고른 제목은 있다. 그 제목도 확정값이다."""
        plan = seo_keyword_plan_from_json(
            _payload(primary="BTS 월드투어"),
            fixed_title="월드투어 다시 시작하는 BTS, 일정과 배경 살펴보기",
        )

        assert plan.primary == "월드투어"

    def test_without_any_title_the_model_primary_is_left_alone(self):
        assert seo_keyword_plan_from_json(_payload(primary="BTS 월드투어")).primary == "BTS 월드투어"


class TestAligningAStoredPlan:
    """SEO 계획은 DB에 저장해 재사용한다. 제목이 확정되기 전에 만들어진 계획이 그대로 돌아올
    수 있으므로, 파싱 시점의 고정만으로는 부족하고 쓰는 시점에 한 번 더 맞춰야 한다."""

    def test_a_stale_primary_is_realigned_to_the_confirmed_title(self):
        stored = SeoKeywordPlan(primary="BTS 월드투어", secondary=["월드투어 티켓"])
        aligned = align_seo_plan_with_title(
            stored, "월드투어 다시 시작하는 BTS, 일정과 배경 살펴보기"
        )

        assert aligned.primary == "월드투어"
        assert aligned.secondary == ["BTS 월드투어", "월드투어 티켓"]

    def test_an_already_aligned_plan_is_returned_untouched(self):
        stored = SeoKeywordPlan(primary="아이폰17", secondary=["아이폰17 가격"])

        assert align_seo_plan_with_title(stored, "아이폰17 출시일과 가격 총정리") is stored

    def test_nothing_to_align_against_is_not_an_error(self):
        stored = SeoKeywordPlan(primary="아이폰17")

        assert align_seo_plan_with_title(stored, None) is stored
        assert align_seo_plan_with_title(stored, "   ") is stored
        assert align_seo_plan_with_title(None, "아이폰17 총정리") is None


def _draft_input(seo_plan=None):
    return DraftGenerationInput(
        post_id="post_1",
        user_id="user_1",
        prompt_version="m4-draft@v1.1",
        format=DraftFormat.MARKDOWN,
        input=BlogTaskInput(topic="아이폰17", keywords=["아이폰17"]),
        selected_intent=SelectedIntentForDraft(
            intent_id="i1", title="아이폰17 정리", target_reader="관심 독자", rationale="why"
        ),
        seo_keyword_plan=seo_plan,
    )


class TestDraftPromptWiring:
    def test_seo_block_is_absent_without_a_plan(self):
        prompt = draft_prompt(_draft_input(seo_plan=None))
        assert "[SEO 키워드 계획]" not in prompt

    def test_seo_block_is_present_with_a_plan(self):
        plan = SeoKeywordPlan(
            primary="아이폰17", secondary=["아이폰17 가격"], avoid=["갤럭시 할인"]
        )
        prompt = draft_prompt(_draft_input(seo_plan=plan))

        assert "[SEO 키워드 계획]" in prompt
        assert "아이폰17" in prompt
        assert "아이폰17 가격" in prompt
        assert "갤럭시 할인" in prompt
        # Avoid·SEO 계획 자체를 독자에게 노출하지 말라는 규칙이 함께 실린다.
        assert "독자에게" in prompt

    def test_seo_plan_prompt_lists_the_priority_order(self):
        prompt = seo_keyword_plan_prompt(_draft_input())
        assert "참고자료와 URL의 실제 문맥" in prompt
        assert "primary 규칙" in prompt


class TestRawKeywordSeparation:
    """원본 검색어를 SEO Primary로 그대로 복사하지 않는다(2026-08-03).

    Primary는 제목과 첫 문단의 **문장 안에** 들어가는 표현이다. 검색어 조합을 그대로
    쓰면 그 검증이 비문을 요구하게 된다.
    """

    def _combination_input(self):
        return DraftGenerationInput(
            post_id="post_1",
            user_id="user_1",
            prompt_version="m4-draft@v2.1",
            format=DraftFormat.MARKDOWN,
            input=BlogTaskInput(topic="전과자", keywords=["창섭 전과자"]),
            selected_intent=SelectedIntentForDraft(
                intent_id="i1",
                title="전과자는 어떤 프로그램인가",
                target_reader="관심 독자",
                rationale="why",
            ),
            raw_keywords=["창섭 전과자"],
        )

    def test_the_prompt_shows_the_raw_keyword_separately(self):
        prompt = seo_keyword_plan_prompt(self._combination_input())
        assert "사용자가 고른 원본 검색어: 창섭 전과자" in prompt
        assert "의도 단계가 뽑은 검색 키워드" in prompt

    def test_the_prompt_forbids_copying_the_raw_keyword_into_primary(self):
        prompt = seo_keyword_plan_prompt(self._combination_input())
        assert "**원본 검색어를 그대로 복사하지 않는다.**" in prompt
        assert "관계를 풀어 쓴 표현으로 고친다" in prompt

    def test_secondary_must_be_usable_in_a_sentence(self):
        prompt = seo_keyword_plan_prompt(self._combination_input())
        assert "문장에 넣으면" in prompt

    def test_a_post_without_a_selected_keyword_says_so(self):
        prompt = seo_keyword_plan_prompt(_draft_input())
        assert "사용자가 고른 원본 검색어: 없음" in prompt
