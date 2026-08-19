"""제목을 원고보다 먼저 확정하는 경로(STEP 2).

여기서 지키려는 것은 넷이다.

1. 제목은 본문보다 **먼저** 정해지고, 트렌드를 건너뛴 글도 예외가 아니다.
2. 확정 제목은 DB에 저장된다 — 저장하지 않으면 선행 생성과 실제 생성이 서로 다른 제목을
   만들어 콘텐츠 설계 캐시가 매번 어긋난다(그게 이 단계에서 가장 깨지기 쉬운 지점이다).
3. 원고는 제목을 바꿀 수 없다. 모델이 다른 제목을 반환해도 코드가 확정 제목으로 되돌리고,
   마크다운 H1도 같은 제목이 된다.
4. 제목 규격 위반은 **제목 생성만** 다시 하게 한다. 본문은 아직 쓰기 전이라 버릴 것이 없다.
"""

import pytest

from app.llm.parsing import final_post_from_json, title_plan_from_json
from app.llm.prompts import draft_prompt, title_plan_prompt
from app.modules.blog_task.repository import InMemoryBlogTaskRepository
from app.modules.draft.quality import check_draft, check_title_plan
from app.modules.draft.service import DraftService
from app.shared import BlogTaskStatus, DraftFormat, FinalPost, TitlePlan

from test_draft_content_design import build_draft_input
from test_draft_service import build_task

pytestmark = pytest.mark.anyio


def plan(
    title: str = "대학생이 과제 시간을 줄이는 AIONA 활용법",
    keyword: str = "AIONA 활용법",
    strategy: str = "HOW_TO",
    alternatives: list[str] | None = None,
) -> TitlePlan:
    return TitlePlan(
        primary_title=title,
        alternative_titles=alternatives if alternatives is not None else ["다른 각도의 제목"],
        h1=title,
        primary_keyword=keyword,
        title_strategy=strategy,
    )


class TitlePlanningGenerator:
    """제목 계획과 콘텐츠 설계를 모두 지원하는 스텁. 호출 횟수를 센다."""

    def __init__(self, plans=None):
        self.title_calls = 0
        self.draft_calls = 0
        self._plans = list(plans) if plans else None

    async def generate_title_plan(self, draft_input):
        self.title_calls += 1
        if self._plans is None:
            return plan()
        # 목록을 넘기면 호출마다 다음 것을 준다 — 재시도 동작을 볼 때 쓴다.
        return self._plans[min(self.title_calls - 1, len(self._plans) - 1)]

    async def generate_draft(self, draft_input):
        self.draft_calls += 1
        raise AssertionError("이 테스트는 원고를 생성하지 않는다")


def _service(generator) -> DraftService:
    return DraftService(
        repository=InMemoryBlogTaskRepository(),
        draft_generator=generator,
    )


# ------------------------------------------------------------------ 파싱·강제


def test_h1_is_forced_to_the_primary_title():
    """규격상 h1 == primaryTitle이다. 모델이 어겨도 되물어 볼 값이 아니라 아는 값이다."""
    parsed = title_plan_from_json(
        {
            "titlePlan": {
                "primaryTitle": "대학생을 위한 AIONA 활용법",
                "alternativeTitles": ["다른 제목"],
                "h1": "완전히 다른 H1",
                "primaryKeyword": "AIONA 활용법",
                "titleStrategy": "HOW_TO",
            }
        }
    )

    assert parsed.h1 == "대학생을 위한 AIONA 활용법"


def test_trend_title_wins_over_whatever_the_model_returned():
    """사용자가 M2에서 고른 제목은 이미 확정된 값이다. 모델이 다듬어도 코드가 되돌린다."""
    parsed = title_plan_from_json(
        {
            "titlePlan": {
                "primaryTitle": "모델이 다듬은 제목",
                "alternativeTitles": [],
                "h1": "모델이 다듬은 제목",
                "primaryKeyword": "AIONA",
                "titleStrategy": "SEARCH_INTENT",
            }
        },
        fixed_title="사용자가 고른 제목",
    )

    assert parsed.primary_title == "사용자가 고른 제목"
    assert parsed.h1 == "사용자가 고른 제목"


def test_a_keyword_outside_a_chosen_title_is_repaired_not_kept():
    """제목이 잠긴 글에서 제목에 없는 키워드를 그대로 두면 check_title_plan이 계획을
    반려하고, 두 번 반려되면 사용자가 고른 제목 자체가 사라진다. 고칠 수 있는 쪽(키워드)을
    고쳐서 고칠 수 없는 쪽(제목)을 지킨다."""
    title = "월드투어 다시 시작하는 BTS, 일정과 배경 살펴보기"
    parsed = title_plan_from_json(
        {
            "titlePlan": {
                "primaryTitle": title,
                "alternativeTitles": [],
                "h1": title,
                # 제목의 단어를 순서만 바꾼 구문이라 제목 어디에도 없다.
                "primaryKeyword": "BTS 월드투어",
                "titleStrategy": "TREND_CONNECTION",
            }
        },
        fixed_title=title,
    )

    assert parsed.primary_keyword == "월드투어"
    assert check_title_plan(parsed, fixed_title=title) == []


def test_a_free_title_still_reports_a_keyword_violation():
    """제목을 사용자가 정하지 않은 글은 모델이 제목을 다시 써서 키워드를 넣을 수 있다.
    그쪽은 고쳐 주지 않고 규격 위반을 그대로 알린다 — 제목 생성만 다시 하면 된다."""
    parsed = title_plan_from_json(
        {
            "titlePlan": {
                "primaryTitle": "대학생 과제 도우미",
                "alternativeTitles": [],
                "h1": "대학생 과제 도우미",
                "primaryKeyword": "AIONA 활용법",
                "titleStrategy": "HOW_TO",
            }
        }
    )

    assert parsed.primary_keyword == "AIONA 활용법"
    assert check_title_plan(parsed) != []


def test_unknown_strategy_falls_back_instead_of_failing():
    parsed = title_plan_from_json(
        {
            "titlePlan": {
                "primaryTitle": "제목",
                "alternativeTitles": [],
                "h1": "제목",
                "primaryKeyword": "제목",
                "titleStrategy": "존재하지_않는_전략",
            }
        }
    )

    assert parsed.title_strategy == "SEARCH_INTENT"


def test_broken_response_yields_no_plan():
    """제목 계획을 못 만들면 None — 원고는 예전처럼 제목을 직접 짓는다."""
    assert title_plan_from_json({"titlePlan": {}}) is None
    assert title_plan_from_json(None) is None


def test_draft_title_and_h1_come_from_the_plan_not_the_model():
    post = final_post_from_json(
        {
            "title": "모델이 멋대로 지은 제목",
            "markdownContent": "# 모델이 멋대로 지은 제목\n\n본문 문단입니다.",
            "hashtags": ["#AI"],
        },
        "폴백 제목",
        1,
        ["AI"],
        forced_title="확정된 제목",
    )

    assert post.title == "확정된 제목"
    assert post.markdown_content.startswith("# 확정된 제목")
    assert "<h1>확정된 제목</h1>" in post.html_content
    # 모델이 쓴 제목 줄은 본문 글자수에 끼지 않는다.
    assert "모델이 멋대로 지은 제목" not in post.body


# ------------------------------------------------------------------ 제목 규격


def test_keyword_must_actually_appear_in_the_title():
    problems = check_title_plan(plan(title="대학생 과제 도우미", keyword="AIONA 활용법"))
    assert any("AIONA 활용법" in problem for problem in problems)


def test_spacing_differences_do_not_fail_the_keyword_check():
    """'AI 블로그 자동화'와 'AI블로그 자동화'는 검색자에게 같은 말이다."""
    assert check_title_plan(plan(title="AI블로그 자동화로 시간 줄이기", keyword="AI 블로그 자동화")) == []


def test_overlong_and_clickbait_titles_are_rejected():
    assert check_title_plan(plan(title="가" * 61, keyword="가")) != []
    assert check_title_plan(plan(title="충격적인 AIONA 활용법", keyword="AIONA 활용법")) != []


def test_a_changed_trend_title_is_a_spec_violation():
    problems = check_title_plan(
        plan(title="모델이 다듬은 AIONA 활용법", keyword="AIONA 활용법"),
        fixed_title="사용자가 고른 AIONA 활용법",
    )
    assert any("변형" in problem for problem in problems)


def test_a_well_formed_plan_passes():
    assert check_title_plan(plan()) == []


# ------------------------------------------------------------------ 재시도


async def test_a_bad_title_regenerates_only_the_title():
    """규격 위반이면 제목 생성만 다시 한다 — 원고는 손대지 않는다."""
    generator = TitlePlanningGenerator(
        plans=[
            plan(title="키워드가 빠진 제목", keyword="AIONA 활용법"),
            plan(),
        ]
    )
    service = _service(generator)
    result = await service._generate_checked_title_plan(build_draft_input())

    assert generator.title_calls == 2
    assert generator.draft_calls == 0
    assert result.primary_title == "대학생이 과제 시간을 줄이는 AIONA 활용법"


async def test_giving_up_on_the_title_does_not_fail_the_article():
    """두 번 다 규격을 못 지키면 계획 없이 간다. 제목 하나로 글 생성을 실패시키지 않는다."""
    generator = TitlePlanningGenerator(plans=[plan(title="키워드 없음", keyword="AIONA 활용법")])
    service = _service(generator)

    assert await service._generate_checked_title_plan(build_draft_input()) is None
    assert generator.title_calls == 2


# ------------------------------------------------------------- 저장·캐시·선행


async def test_the_plan_is_saved_and_reused_instead_of_regenerated():
    """저장하지 않으면 선행 생성과 실제 생성이 다른 제목을 만들어 설계 캐시가 어긋난다."""
    generator = TitlePlanningGenerator()
    service = _service(generator)
    task = build_task(status=BlogTaskStatus.GENERATING)
    await service._repository.create(task)

    first = await service._with_title_plan(
        await service._build_draft_input(task, style=None, format_=DraftFormat.HTML), task
    )
    assert generator.title_calls == 1
    assert first.title_plan is not None

    stored = await service._repository.find_by_post_id(task.post_id)
    assert stored.title_plan == first.title_plan

    # 두 번째 호출은 저장된 계획을 읽는다 — 오래된 스냅샷(title_plan=None)을 넘겨도.
    second = await service._with_title_plan(
        await service._build_draft_input(task, style=None, format_=DraftFormat.HTML), task
    )
    assert generator.title_calls == 1
    assert second.title_plan == first.title_plan


async def test_a_generator_without_title_planning_keeps_the_old_behaviour():
    class OldGenerator:
        async def generate_draft(self, draft_input):
            raise AssertionError("호출되지 않는다")

    service = _service(OldGenerator())
    task = build_task(status=BlogTaskStatus.GENERATING)
    await service._repository.create(task)
    draft_input = await service._build_draft_input(task, style=None, format_=DraftFormat.HTML)

    assert (await service._with_title_plan(draft_input, task)).title_plan is None


def test_the_plan_cache_key_tracks_the_confirmed_title():
    """제목·핵심 검색 구문·전략이 바뀌면 옛 설계를 재사용하면 안 된다."""
    service = _service(TitlePlanningGenerator())
    base = build_draft_input(title_plan=plan())

    assert service._plan_cache_key(base) != service._plan_cache_key(
        build_draft_input(title_plan=plan(title="완전히 다른 제목", keyword="다른 제목"))
    )
    assert service._plan_cache_key(base) != service._plan_cache_key(
        build_draft_input(title_plan=plan(keyword="AIONA 사용법"))
    )
    assert service._plan_cache_key(base) != service._plan_cache_key(
        build_draft_input(title_plan=plan(strategy="COMPARISON"))
    )
    # 같은 계획이면 같은 키 — 선행 생성이 실제 생성에 그대로 재사용된다.
    assert service._plan_cache_key(base) == service._plan_cache_key(
        build_draft_input(title_plan=plan())
    )


# ------------------------------------------------------------------ 프롬프트


def test_the_draft_prompt_stops_asking_for_a_title():
    """제목이 확정된 글에서는 원고가 제목을 짓지 않는다."""
    prompt = draft_prompt(build_draft_input(title_plan=plan()))

    assert "확정 제목(이미 정해졌다. 새로 짓지 않는다):" in prompt
    assert "대학생이 과제 시간을 줄이는 AIONA 활용법" in prompt
    # 예전의 '제목을 만들어라' 분기는 빠진다.
    assert "제목만 봐도 대상 독자와 글의 핵심 이익" not in prompt


def test_without_a_plan_the_draft_prompt_is_unchanged():
    prompt = draft_prompt(build_draft_input())

    assert "확정 제목(이미 정해졌다" not in prompt
    assert "제목만 봐도 대상 독자와 글의 핵심 이익" in prompt


def test_the_title_prompt_pins_a_chosen_trend_title():
    prompt = title_plan_prompt(build_draft_input(trend_title="사용자가 고른 트렌드 제목"))

    assert "한 글자도 바꾸지 않고 그대로 쓴다: 사용자가 고른 트렌드 제목" in prompt


def test_the_title_prompt_bans_trend_framing_when_trend_was_skipped():
    prompt = title_plan_prompt(build_draft_input())

    assert "TREND_CONNECTION을" in prompt
    assert "지금 뜨는" in prompt


# --------------------------------------------------------------- 원고 안전망


def test_the_draft_check_flags_a_title_that_drifted():
    """파싱이 이미 강제하지만, 그 강제를 우회하는 경로가 생기면 조용히 통과하지 않는다."""
    post = FinalPost(
        title="다른 제목",
        body="본문" * 700,
        hashtags=["#AI"] * 5,
        html_content="<article><h1>다른 제목</h1><p>본문</p></article>",
        markdown_content="# 또 다른 제목\n\n본문",
    )
    report = check_draft(post, 5, min_body_chars=100, final_title="확정 제목")

    assert any("제목이 확정 제목과 다릅니다" in warning for warning in report.warnings)
    assert any("H1이 확정 제목과 다릅니다" in warning for warning in report.warnings)
    # 확정 제목은 이미 아는 값이다 — 이것 때문에 멀쩡한 본문을 다시 쓰지는 않는다.
    assert report.ok
