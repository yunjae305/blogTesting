"""트렌드가 주인공이고 브랜드는 도구인 글(2026-08-19).

이 저장소가 만들려는 글이 무엇인지가 여기 적혀 있다. 검색해서 들어온 사람이 원한 답을
먼저 주고, 그 과정에서 브랜드를 **발견하게** 하는 글이다. 브랜드를 주인공으로 두면 그
사람은 광고를 읽게 되고, 광고는 검색으로 들어온 독자를 붙잡지 못한다.

2026-08-11에는 화면이 소재 칸과 브랜드 칸을 서로 잠갔고, 프롬프트도 "브랜드를 골랐다 =
브랜드가 주인공"이라고 단정했다. 그래서 이 글은 **만들 수 없었다.** 여기서 지키는 것은
그 둘을 되돌리지 않는 것이다:

1. 소재와 브랜드를 함께 골랐을 때 역할이 UTILITY로 갈린다(`brand_mode_for`).
2. 그 글의 프롬프트가 브랜드를 도구로 말한다(`brand_utility_rules`).
3. 억지 조합은 등급으로 걸러진다(`evaluate_brand_fit`).
"""

import pytest

from app.llm.contracts import TopicGenerationInput
from app.llm.prompts import (
    BRAND_UTILITY_SHARES,
    blog_input_summary,
    brand_utility_rules,
    brand_utility_title_rules,
    content_plan_prompt,
    draft_prompt,
    topic_prompt,
)
from app.modules.brand import (
    BRAND_FIT_DIRECT,
    BRAND_FIT_FORCED,
    BRAND_FIT_SITUATIONAL,
    brand_mode_for,
    brand_use_case_lines,
    evaluate_brand_fit,
)
from app.shared import (
    BRAND_MODE_FOCUS,
    BRAND_MODE_UTILITY,
    BlogTaskInput,
    BrandProfile,
    BrandUseCase,
    TrendKeyword,
    TrendSource,
)
# 원고 입력 한 벌을 만드는 helper. 프롬프트 감사 테스트가 이미 갖고 있어 그것을 쓴다 —
# 여기서 다시 만들면 필수 필드가 늘 때마다 두 곳을 고쳐야 한다.
from tests.test_prompt_audit import draft_input


def aiona(**overrides) -> BrandProfile:
    """기준표를 갖춘 브랜드. 실제 등록할 모양과 같다(docs/aiona-brand.md)."""
    defaults = dict(
        brand_id="brand_1",
        user_id="user_1",
        name="AIONA",
        description="여러 AI 모델을 한자리에서 쓰는 서비스입니다.",
        features="자료 조사, 문서 요약, 번역, 이메일·메시지 작성",
        created_at="2026-08-19T00:00:00.000Z",
        updated_at="2026-08-19T00:00:00.000Z",
        use_cases=[
            BrandUseCase(
                situation="어떤 정보를 알아보고 싶을 때",
                feature="자료 조사",
                keywords=["다이어트", "칼로리", "성분", "신제품"],
            ),
            BrandUseCase(
                situation="해외 자료를 확인할 때",
                feature="번역",
                keywords=["해외", "외신", "직구"],
            ),
            BrandUseCase(
                situation="회사에 보낼 글을 써야 할 때",
                feature="이메일·메시지 작성",
                keywords=["취업", "면접", "퇴사"],
            ),
        ],
    )
    return BrandProfile(**{**defaults, **overrides})


def utility_input(**overrides) -> BlogTaskInput:
    defaults = dict(
        topic="빼빼로 신제품",
        keywords=["정보 전달"],
        purpose=["정보 전달"],
        subject_category="음식·맛집",
        brand_id="brand_1",
        brand_name="AIONA",
        brand_mode=BRAND_MODE_UTILITY,
    )
    return BlogTaskInput(**{**defaults, **overrides})


class TestWhichGlassTheBrandIsSeenThrough:
    """소재를 적었는가가 역할을 가른다."""

    def test_a_brand_with_no_topic_is_the_subject(self):
        """소재를 비우고 브랜드만 골랐다 = 그 브랜드로 쓰겠다는 뜻이다."""
        assert brand_mode_for(aiona(), "") == BRAND_MODE_FOCUS
        assert brand_mode_for(aiona(), None) == BRAND_MODE_FOCUS

    def test_a_topic_plus_a_brand_makes_the_brand_a_tool(self):
        assert brand_mode_for(aiona(), "빼빼로 신제품") == BRAND_MODE_UTILITY

    def test_a_topic_that_is_the_brand_name_is_still_the_subject(self):
        """화면이 이름을 채워 보내는 경로가 남아 있어도 글의 성격은 같아야 한다."""
        assert brand_mode_for(aiona(), "AIONA") == BRAND_MODE_FOCUS
        assert brand_mode_for(aiona(), "  aiona  ") == BRAND_MODE_FOCUS

    def test_the_user_can_override_when_both_are_filled(self):
        """'AIONA 앱스튜디오 사용법'은 소재가 있어도 브랜드가 주인공인 글이다.

        어느 쪽으로 쓸지는 편집 판단이라 사용자가 고를 수 있어야 한다. 이름(brand_name)과
        달리 이 값은 사실을 주장하지 않으므로, 사용자를 믿어도 없는 것이 글에 실리지 않는다.
        """
        assert (
            brand_mode_for(aiona(), "AIONA 앱스튜디오 사용법", BRAND_MODE_FOCUS)
            == BRAND_MODE_FOCUS
        )

    def test_utility_without_a_topic_falls_back_to_focus(self):
        """소재가 비면 그 자리를 브랜드 이름이 채운다 — 주인공이자 도구일 수는 없다."""
        assert brand_mode_for(aiona(), "", BRAND_MODE_UTILITY) == BRAND_MODE_FOCUS

    def test_an_unknown_requested_mode_is_ignored(self):
        assert brand_mode_for(aiona(), "빼빼로", "SOMETHING") == BRAND_MODE_UTILITY

    def test_no_brand_means_no_role(self):
        assert brand_mode_for(None, "빼빼로") is None


class TestTheBrandIsNotTheSubjectAnyMore:
    """UTILITY 글의 입력 요약이 브랜드를 **도구**라고 말하는가.

    이 한 줄이 뒤집히면 나머지 지침이 아무리 자세해도 소용이 없다 — 참고자료에 브랜드
    소개가 실려 있으므로, 모델은 그것을 글의 대상으로 읽는다.
    """

    def test_the_summary_calls_the_brand_a_tool(self):
        summary = blog_input_summary(utility_input(), include_materials=False)

        assert "주인공이 아니라 활용한 도구다" in summary
        assert "'빼빼로 신제품'과 그것을 둘러싼 트렌드" in summary
        # 옛 문장이 남아 있으면 두 지시가 서로를 부순다.
        assert "이 글의 주인공 브랜드" not in summary

    def test_a_brand_only_post_still_says_the_brand_is_the_subject(self):
        summary = blog_input_summary(
            utility_input(topic="AIONA", brand_mode=BRAND_MODE_FOCUS),
            include_materials=False,
        )

        assert "이 글의 주인공 브랜드: AIONA" in summary

    def test_an_old_post_without_a_mode_is_read_as_brand_first(self):
        """2026-08-19 이전 글에는 brand_mode가 없다. 그때는 브랜드가 언제나 주인공이었다."""
        summary = blog_input_summary(
            utility_input(topic="AIONA", brand_mode=None), include_materials=False
        )

        assert "이 글의 주인공 브랜드: AIONA" in summary

    def test_a_post_without_a_brand_is_untouched(self):
        summary = blog_input_summary(
            BlogTaskInput(topic="빼빼로", keywords=["정보 전달"]), include_materials=False
        )

        assert "브랜드" not in summary


class TestSeventyTwentyTen:
    """트렌드 70 : 브랜드 활용 20 : 정리 10(2026-08-19 사용자 지시)."""

    def test_the_rules_only_exist_for_utility_posts(self):
        assert brand_utility_rules(utility_input(brand_mode=BRAND_MODE_FOCUS)) == []
        assert brand_utility_rules(utility_input(brand_mode=None)) == []
        assert brand_utility_rules(utility_input(brand_name=None)) == []
        assert brand_utility_rules(utility_input()) != []

    def test_the_body_belongs_to_the_topic(self):
        text = "\n".join(brand_utility_rules(utility_input()))

        assert "소재·트렌드 본론 60~70%" in text
        assert "브랜드를 쓴 장면 15~20%" in text
        # 도입부에 브랜드가 나오면 검색해서 들어온 독자는 광고를 읽은 것이 된다.
        assert "도입부와 첫 본문 섹션에는 AIONA가 나오지 않는다" in text

    def test_the_used_scene_has_an_order(self):
        """"AIONA를 써 봤습니다"만 있고 무엇을 했는지 없는 문단을 막는다."""
        text = "\n".join(brand_utility_rules(utility_input()))

        assert "왜 필요했는지" in text
        assert "어떤 기능을 썼는지" in text
        assert "무엇을 얻었는지" in text

    def test_only_real_feature_names_are_allowed(self):
        text = "\n".join(
            brand_utility_rules(
                utility_input(brand_use_cases=["- 어떤 정보를 알아보고 싶을 때 → 자료 조사"])
            )
        )

        assert "- 어떤 정보를 알아보고 싶을 때 → 자료 조사" in text
        assert "이 이름 그대로" in text

    def test_without_a_table_it_refuses_to_invent_names(self):
        text = "\n".join(brand_utility_rules(utility_input(brand_use_cases=[])))

        assert "참고자료의 브랜드 자료에 적힌 것만" in text
        assert "기능명을 만들어 붙이지 말고" in text

    def test_a_b_grade_asks_for_the_situation_first(self):
        text = "\n".join(brand_utility_rules(utility_input(brand_fit_grade=BRAND_FIT_SITUATIONAL)))

        assert "곧바로** 필요한 소재가 아니다" in text

    def test_experience_is_never_invented(self):
        """실제로 하지 않은 사용 후기를 만들어 내면 글 전체가 거짓이 된다."""
        text = "\n".join(brand_utility_rules(utility_input()))

        assert "하지 않은 경험을 지어내지 않는다" in text
        assert "가능성" in text


class TestTheVoiceIsDiscoveryNotRecommendation:
    """비중을 지켜도 문장이 "써 보세요"면 결국 광고다(2026-08-19 사용자 지시).

    이 글이 서 있는 자리는 하나다: **"이걸 알아보다가 마침 이런 기능이 있길래 한번 써
    봤고, 이런 점이 도움이 됐다."** 발견담이지 권유가 아니다.

    비중(70:20:10)만으로는 이것을 얻을 수 없다. 브랜드 자리에 20%를 주고 그 안을
    "지금 바로 사용해 보세요"로 채워도 비중은 지켜지기 때문이다.
    """

    def test_the_stance_is_spelled_out(self):
        text = "\n".join(brand_utility_rules(utility_input()))

        assert "권하는 글이 아니라" in text
        assert "한번 써 봤고 이런 점이 도움이 됐다" in text

    def test_the_narrative_order_is_given(self):
        """"알아보고 싶었다 → 번거로웠다 → 마침 있길래 써 봤다 → 이런 결과 → 편했다"."""
        text = "\n".join(brand_utility_rules(utility_input()))

        assert "이걸 알아보고 싶었다" in text
        assert "마침 이런 기능이 있길래 써 봤다" in text

    def test_recommendation_phrases_are_named_and_banned(self):
        """열린 지시("광고처럼 쓰지 마라")로는 갈리지 않는다 — 표현을 예로 든다."""
        text = "\n".join(brand_utility_rules(utility_input()))

        for phrase in ("지금 바로 사용해 보세요", "강력 추천합니다", "꼭 한번 써 보세요"):
            assert phrase in text
        # 가입·요금제 안내도 이 글에 있을 이유가 없다.
        assert "가입·설치·방문을 권하는 문장" in text

    def test_limits_are_written_next_to_the_good_parts(self):
        """좋은 점만 나열한 문단은 광고로 읽힌다."""
        text = "\n".join(brand_utility_rules(utility_input()))

        assert "한계도 같이" in text
        # 다만 없는 단점을 지어내라는 뜻은 아니다 — 그것도 날조다.
        assert "없는 단점을 지어내라는 뜻은 아니다" in text

    def test_the_closing_is_what_happened_not_a_pitch(self):
        text = "\n".join(brand_utility_rules(utility_input()))

        assert "권하는 말이 아니라 **겪은 것**으로 닫는다" in text

    def test_the_review_catches_the_pitch_voice(self):
        """검수도 같은 것을 본다 — 지시만 있고 검사가 없으면 새어 나간 문장이 그대로 나간다."""
        from app.llm.prompts import _brand_utility_review_block

        block = _brand_utility_review_block(utility_input())

        assert "알아보다가 마침 이런 기능이 있길래 써 봤고" in block
        assert "tone으로 지적" in block
        assert "요금제·혜택·이벤트 안내도 tone이다" in block


class TestTheTitleStaysSearchable:
    """제목에 브랜드가 들어가면 그 브랜드를 아는 사람만 누른다 — 신규 유입이 목적이다."""

    def test_the_brand_is_kept_out_of_the_title(self):
        text = "\n".join(brand_utility_title_rules(utility_input()))

        assert "제목에 'AIONA'를 넣지 않는다" in text
        assert "기능 이름·서비스 이름도 제목에 넣지 않는다" in text

    def test_a_brand_first_post_titles_normally(self):
        assert brand_utility_title_rules(utility_input(brand_mode=BRAND_MODE_FOCUS)) == []


class TestTheFitGrade:
    """A·B·C — 얹을 수 있다는 것과 얹어야 한다는 것은 다르다."""

    def test_the_topic_itself_hitting_the_table_is_an_a(self):
        fit = evaluate_brand_fit(aiona(), "다이어트 간식 추천", context=["음식·맛집"])

        assert fit.grade == BRAND_FIT_DIRECT
        assert fit.features == ("자료 조사",)
        assert fit.usable

    def test_hitting_only_through_the_wider_context_is_a_b(self):
        """소재는 안 닿는데 분야가 닿는다 — 상황을 만들면 쓸 수 있다."""
        fit = evaluate_brand_fit(aiona(), "이번 주말 나들이", context=["해외 여행 준비"])

        assert fit.grade == BRAND_FIT_SITUATIONAL
        assert fit.features == ("번역",)

    def test_nothing_touching_is_a_c(self):
        fit = evaluate_brand_fit(aiona(), "강아지 산책 코스", context=["여행·장소"])

        assert fit.grade == BRAND_FIT_FORCED
        assert not fit.usable
        assert "브랜드 자료에 없습니다" in fit.reason

    def test_a_brand_with_no_table_never_reaches_an_a(self):
        """줄글에서 우연히 겹친 낱말은 "이 상황에서 이 기능을 쓴다"는 근거가 못 된다."""
        plain = aiona(use_cases=[], features="번역과 요약을 해 주는 서비스")

        fit = evaluate_brand_fit(plain, "번역 앱 비교", context=[])

        assert fit.grade == BRAND_FIT_SITUATIONAL

    def test_a_brand_with_no_table_and_no_overlap_is_still_a_c(self):
        plain = aiona(use_cases=[], description=None, features=None)

        assert evaluate_brand_fit(plain, "강아지 산책", context=[]).grade == BRAND_FIT_FORCED

    def test_no_brand_is_not_graded(self):
        """브랜드를 안 쓰는 글은 이 규칙과 무관하다 — 호출부가 등급만 보고 막을 수 있어야 한다."""
        assert evaluate_brand_fit(None, "무엇이든").grade == BRAND_FIT_DIRECT

    def test_a_multi_word_keyword_needs_every_word(self):
        """'일본 여행 준비물'에 '학교 준비물'이 닿으면 여행 글에 알림장 정리가 붙는다.

        한때 소재의 낱말 하나가 검색어 안에 들어 있기만 해도 닿은 것으로 봤다(실측에서
        바로 이 조합이 잘못 걸렸다). 낱말 하나로 판정하면 어디든 닿는다.
        """
        profile = aiona(
            use_cases=[
                BrandUseCase(
                    situation="아이 학교 알림을 정리할 때",
                    feature="알림장 정리",
                    keywords=["학교 준비물"],
                )
            ]
        )

        assert evaluate_brand_fit(profile, "일본 여행 준비물", context=[]).grade == BRAND_FIT_FORCED
        assert (
            evaluate_brand_fit(profile, "학교 준비물 챙기기", context=[]).grade == BRAND_FIT_DIRECT
        )

    def test_a_keyword_hits_through_particles_and_spacing(self):
        """대상 글은 붙여 놓고 본다 — 조사·띄어쓰기가 달라도 같은 말이다."""
        fit = evaluate_brand_fit(aiona(), "다이어트할 때 먹는 간식", context=[])

        assert fit.grade == BRAND_FIT_DIRECT

    def test_only_the_directly_matching_rows_reach_the_prompt(self):
        """분야 이름이 겹쳤다는 이유로 상관없는 기능이 후보에 오르면 안 된다.

        소재 '추석 선물 세트'의 분야는 '제품·쇼핑·리뷰'다. 그 이름 안의 '리뷰'가 리뷰
        기능에 닿는데, 이 글에서 쓸 기능은 경조사 문구다.
        """
        profile = aiona(
            use_cases=[
                BrandUseCase(situation="명절 문구가 필요할 때", feature="경조사 문구", keywords=["추석"]),
                BrandUseCase(situation="답글을 써야 할 때", feature="리뷰 답글", keywords=["리뷰"]),
            ]
        )
        fit = evaluate_brand_fit(profile, "추석 선물 세트", context=["제품·쇼핑·리뷰"])

        assert fit.grade == BRAND_FIT_DIRECT
        assert brand_use_case_lines(profile, fit) == ["- 명절 문구가 필요할 때 → 경조사 문구"]

    def test_common_words_alone_do_not_count_as_a_hit(self):
        """'정보'·'사용' 같은 낱말은 어디에나 있다. 그것만으로 닿았다고 하면 전부 A가 된다."""
        vague = aiona(
            use_cases=[
                BrandUseCase(situation="정보가 필요할 때", feature="자료 조사", keywords=["정보"])
            ]
        )

        assert evaluate_brand_fit(vague, "강아지 산책 코스", context=[]).grade == BRAND_FIT_FORCED

    def test_only_the_matching_rows_go_into_the_prompt(self):
        """표 전체를 주면 모델이 아무 줄이나 고른다 — 실제로 같은 기능만 반복됐다."""
        profile = aiona()
        fit = evaluate_brand_fit(profile, "다이어트 간식", context=[])

        assert brand_use_case_lines(profile, fit) == ["- 어떤 정보를 알아보고 싶을 때 → 자료 조사"]

    def test_a_table_less_brand_contributes_no_feature_name(self):
        """기능 이름을 모르는 것이 사실이다. 브랜드 이름을 기능인 척 넘기지 않는다."""
        plain = aiona(use_cases=[], features="번역과 요약")
        fit = evaluate_brand_fit(plain, "번역 앱 비교", context=[])

        assert brand_use_case_lines(plain, fit) == []


class TestThePromptsActuallyCarryTheRules:
    """지침을 만들어 두고 프롬프트에 붙이지 않으면 아무 일도 일어나지 않는다."""

    def test_the_plan_prompt_swaps_the_length_shares(self):
        prompt = content_plan_prompt(draft_input(input=utility_input()))

        for share in BRAND_UTILITY_SHARES:
            assert share in prompt
        assert "브랜드 활용 지침" in prompt

    def test_a_normal_post_keeps_the_purpose_shares(self):
        from app.llm.prompts import section_length_shares

        prompt = content_plan_prompt(
            draft_input(input=BlogTaskInput(topic="빼빼로", purpose=["정보 전달"], keywords=["빼빼로"]))
        )

        assert BRAND_UTILITY_SHARES[1] not in prompt
        assert section_length_shares("정보 전달")[1] in prompt

    def test_the_body_prompt_carries_the_rules_too(self):
        """설계는 섹션을 나누는 일이고, 브랜드 문장이 실제로 새어 나오는 곳은 본문이다."""
        prompt = draft_prompt(draft_input(input=utility_input()))

        assert "브랜드 활용 지침" in prompt
        assert "도입부와 첫 본문 섹션에는 AIONA가 나오지 않는다" in prompt

    def test_the_title_prompt_keeps_the_brand_out(self):
        prompt = topic_prompt(
            TopicGenerationInput(
                post_id="post_1",
                input=utility_input(),
                trend_keyword=TrendKeyword(
                    trend_keyword_id="kw_1",
                    keyword="빼빼로데이",
                    source=TrendSource.NAVER_DATALAB,
                    rank=1,
                    score=1.0,
                    collected_at="2026-08-19T00:00:00Z",
                ),
            )
        )

        assert "제목에 'AIONA'를 넣지 않는다" in prompt

    def test_the_final_review_watches_for_the_centre_moving(self):
        """문장 하나하나는 자료와 맞는데 글 전체가 브랜드 소개문인 경우를 잡는 자리다."""
        from app.llm.prompts import final_review_prompt
        from tests.test_final_review import post

        prompt = final_review_prompt(
            draft_input(input=utility_input()),
            post(title="빼빼로 신제품, 뭐가 달라졌나"),
        )

        assert "주인공이 아니라 **활용한 도구**다" in prompt
        assert "offtopic으로" in prompt


@pytest.mark.asyncio
class TestSavingAPostDecidesTheRole:
    """라우트까지 통과하는지 본다. 판정을 붙여 놓고 저장에 싣지 않으면 원고가 옛 규칙으로 돈다."""

    async def _client_and_brand(self):
        from types import SimpleNamespace

        from httpx import ASGITransport, AsyncClient

        from app.main import create_app
        from app.modules.auth.repository import InMemoryUserRepository
        from app.modules.auth.service import AuthService
        from app.modules.blog_task.repository import InMemoryBlogTaskRepository
        from app.modules.blog_task.service import BlogTaskService
        from app.modules.brand import BrandService, InMemoryBrandRepository

        auth_service = AuthService(InMemoryUserRepository())
        signed_up = await auth_service.sign_up(
            {"email": "utility@example.com", "password": "password123", "nickname": "작성자"}
        )
        repository = InMemoryBrandRepository()
        profile = aiona(user_id=signed_up.user.user_id)
        await repository.upsert(profile)

        app = create_app()
        app.state.services = SimpleNamespace(
            auth_service=auth_service,
            brand_service=BrandService(repository),
            blog_task_service=BlogTaskService(InMemoryBlogTaskRepository(), None, None),
            trend_service=SimpleNamespace(start_keyword_prefetch=lambda _task: None),
        )
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        headers = {"authorization": f"Bearer {signed_up.access_token}"}
        return client, profile, headers

    async def test_a_topic_plus_a_brand_is_saved_as_a_utility_post(self):
        client, profile, headers = await self._client_and_brand()
        async with client:
            response = await client.post(
                "/posts",
                json={
                    "topic": "다이어트 간식 추천",
                    "purpose": ["정보 전달"],
                    "subjectCategory": "음식·맛집",
                    "brandId": profile.brand_id,
                },
                headers=headers,
            )

        assert response.status_code == 201, response.text
        saved = response.json()["data"]["input"]
        assert saved["brandMode"] == "UTILITY"
        # 소재는 사용자가 정한 것 그대로다 — 브랜드 이름으로 덮이지 않는다.
        assert saved["topic"] == "다이어트 간식 추천"
        # 결합 가능성과, 그 소재에 닿은 기준표 줄이 함께 저장된다.
        assert saved["brandFitGrade"] == "A"
        assert saved["brandUseCases"] == ["- 어떤 정보를 알아보고 싶을 때 → 자료 조사"]

    async def test_a_brand_only_post_is_still_a_brand_post(self):
        client, profile, headers = await self._client_and_brand()
        async with client:
            response = await client.post(
                "/posts",
                json={
                    "purpose": ["제품·서비스 홍보"],
                    "subjectCategory": "브랜드·기업",
                    "brandId": profile.brand_id,
                },
                headers=headers,
            )

        assert response.status_code == 201, response.text
        saved = response.json()["data"]["input"]
        assert saved["brandMode"] == "FOCUS"
        assert saved["topic"] == "AIONA"
        # 브랜드가 주인공인 글에는 결합 가능성을 묻지 않는다 — 소재가 곧 브랜드다.
        assert "brandFitGrade" not in saved

    async def test_the_fit_endpoint_answers_before_saving(self):
        """저장 뒤에 알면 되돌릴 수 있는 시점은 원고를 다 만든 뒤다."""
        client, profile, headers = await self._client_and_brand()
        async with client:
            response = await client.post(
                f"/brands/{profile.brand_id}/fit",
                json={"topic": "강아지 산책 코스", "subjectCategory": "여행·장소"},
                headers=headers,
            )

        assert response.status_code == 200, response.text
        assert response.json()["grade"] == "C"

    async def test_a_post_with_no_brand_carries_no_role(self):
        client, _profile, headers = await self._client_and_brand()
        async with client:
            response = await client.post(
                "/posts",
                json={
                    "topic": "빼빼로 신제품",
                    "purpose": ["정보 전달"],
                    "subjectCategory": "음식·맛집",
                },
                headers=headers,
            )

        saved = response.json()["data"]["input"]
        assert "brandMode" not in saved
        assert "brandFitGrade" not in saved
