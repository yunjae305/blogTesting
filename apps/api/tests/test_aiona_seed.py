"""기본 브랜드 자료(AIONA)가 실제로 쓸 만한가(2026-08-19).

이 자료는 문서가 아니라 **입력**이다. 기준표의 한 줄이 "어느 소재에 어떤 기능을 붙일
것인가"를 정하고, 그 판정이 화면에서 글을 막기도 한다(C 등급). 그래서 자료 자체를
테스트한다 — 검증을 통과하는가, 그리고 실제 트렌드 소재에 대해 **의도한 등급이 나오는가.**

기능 이름은 AIONA 화면에 실제로 있는 이름이다(사용자가 준 `AIONA_기능정리.md`). 원고가
이 글자를 그대로 쓰므로 한 글자만 달라져도 없는 기능이 글에 실린다.

아래 소재 목록은 운영 큐가 다룰 갈래에서 뽑았다. 표를 손보다가 이 중 하나가 C로
떨어지면, 그것은 운영 중인 콘텐츠 큐 하나가 통째로 막힌다는 뜻이다.
"""

import pytest

from app.modules.brand import (
    BRAND_FIT_DIRECT,
    BRAND_FIT_FORCED,
    DEFAULT_BRAND_NAME,
    brand_use_case_lines,
    default_brand_body,
    evaluate_brand_fit,
    validate_brand_body,
)
from app.shared import BrandLimits, BrandProfile


@pytest.fixture(scope="module")
def profile() -> BrandProfile:
    cleaned = validate_brand_body(default_brand_body())
    return BrandProfile(
        brand_id="brand_aiona",
        user_id="user_1",
        created_at="2026-08-19T00:00:00.000Z",
        updated_at="2026-08-19T00:00:00.000Z",
        **cleaned,
    )


class TestTheDefaultIsSavable:
    def test_it_passes_the_same_validation_the_screen_does(self, profile):
        """기본 자료가 화면과 다른 길로 저장되지 않는다 — 검증은 한 곳뿐이다."""
        assert profile.name == DEFAULT_BRAND_NAME
        assert profile.description and profile.features

    def test_the_prose_fields_fit_in_every_prompt(self, profile):
        """이 글자는 **모든 글의 프롬프트에 그대로 실린다** — 길수록 매 편이 비싸진다."""
        assert len(profile.description) <= BrandLimits.MAX_SECTION_LENGTH
        assert len(profile.features) <= BrandLimits.MAX_SECTION_LENGTH

    def test_the_table_fits_under_the_limit(self, profile):
        assert 0 < len(profile.use_cases) <= BrandLimits.MAX_USE_CASES

    def test_every_row_has_both_halves(self, profile):
        """한 칸만 채운 줄은 프롬프트에 반쪽짜리 지시를 싣는다."""
        for case in profile.use_cases:
            assert case.situation.strip(), case
            assert case.feature.strip(), case

    def test_feature_names_are_not_repeated(self, profile):
        """같은 기능이 두 줄이면 프롬프트가 같은 이름을 두 번 권한다."""
        names = [case.feature for case in profile.use_cases]
        assert len(names) == len(set(names))

    def test_the_official_links_are_there(self, profile):
        """자료 수집이 이 주소를 **실제로 열어 읽는다.** 살아 있는 주소만 둔다."""
        assert "https://aiona.kr" in [link.url for link in profile.links]

    def test_the_closing_carries_the_facts_and_the_link(self, profile):
        """글 맨 끝에 붙는 글자다 — 검수를 거치지 않고 그대로 발행된다."""
        assert profile.closing is not None
        assert "웰컴 크레딧 100" in profile.closing.note
        assert profile.closing.url == "https://aiona.kr"
        assert profile.closing.label == "aiona.kr"


class TestTheFeatureNamesAreTheRealOnes:
    """화면에 없는 이름을 적으면 그 이름이 그대로 글에 나간다.

    아래는 AIONA 앱스튜디오의 **기본 제공 앱**과 도구 이름이다. 표를 손보다 이름이
    바뀌면 여기서 걸린다 — 오타 하나가 없는 기능을 만들어 낸다.
    """

    #: 사용자가 준 기능 정리서에서 확인한 이름들.
    REAL_NAMES = {
        "자료 조사",
        "문서 요약",
        "번역",
        "이메일·메시지 작성",
        "회의 준비",
        "주간 식단 짜기",
        "리뷰 답글·후기 쓰기",
        "경조사 문구",
        "알림장 정리",
        "자기소개서 초안",
        "계약서·약관 핵심 짚기",
        "영수증·경비 정리",
        "사기 문자 확인",
        "이미지 생성",
        "동영상 생성",
        "슬라이드 생성",
        "AI 요약",
        "고민상담소",
        "AI 플래너",
        "회의록",
        "심층 리서치",
        "브리핑",
        "프로젝트",
        "앱스튜디오",
        "작업실 루틴",
        "학습 코치",
        "커리어 코치",
        "리서치 코파일럿",
    }

    def test_every_row_names_a_real_feature(self, profile):
        for case in profile.use_cases:
            assert case.feature in self.REAL_NAMES, f"모르는 기능 이름: {case.feature}"


class TestTheTableCoversTheContentQueue:
    """운영 큐가 여러 갈래로 분산된다 — 그 갈래마다 쓸 기능이 있어야 한다."""

    #: (소재, 소재 분야, 나와야 하는 기능)
    QUEUE = [
        ("빼빼로 신제품 성분", "음식·맛집", "자료 조사"),
        ("다이어트 간식 칼로리", "건강·생활", "자료 조사"),
        ("아이폰17 가격", "IT·컴퓨터·AI", "자료 조사"),
        ("스파이더맨 4편 해외 반응", "영화·드라마·방송", "번역"),
        ("신입 면접 이메일", "정책·시사", "이메일·메시지 작성"),
        ("자취 밀프렙 식단", "음식·맛집", "주간 식단 짜기"),
        ("추석 인사말", "건강·생활", "경조사 문구"),
        ("사장님 리뷰 답글", "제품·쇼핑·리뷰", "리뷰 답글·후기 쓰기"),
        ("학교 준비물 목록", "건강·생활", "알림장 정리"),
        ("전세 계약서 특약", "정책·시사", "계약서·약관 핵심 짚기"),
        ("보이스피싱 수법", "정책·시사", "사기 문자 확인"),
        ("연말정산 영수증", "정책·시사", "영수증·경비 정리"),
        ("블로그 썸네일 만들기", "IT·컴퓨터·AI", "이미지 생성"),
        ("공모전 포트폴리오", "정책·시사", "커리어 코치"),
        ("중간고사 복습", "건강·생활", "학습 코치"),
    ]

    @pytest.mark.parametrize("topic,category,feature", QUEUE)
    def test_the_queue_topic_reaches_the_intended_feature(
        self, profile, topic, category, feature
    ):
        fit = evaluate_brand_fit(profile, topic, context=[category])

        assert fit.grade == BRAND_FIT_DIRECT, f"{topic} → {fit.grade} ({fit.reason})"
        assert feature in fit.features, f"{topic} → {fit.features}"

    @pytest.mark.parametrize("topic,category,feature", QUEUE)
    def test_the_prompt_gets_that_feature_name(self, profile, topic, category, feature):
        """원고는 여기 실린 이름을 그대로 쓴다 — 실려 나가지 않으면 모델이 지어낸다."""
        fit = evaluate_brand_fit(profile, topic, context=[category])

        assert any(feature in line for line in brand_use_case_lines(profile, fit))


class TestTheTableDoesNotCatchEverything:
    """모든 트렌드에 브랜드를 얹을 수 있으면 등급은 아무 일도 하지 않는 것이다.

    C를 버리는 것이 이 판정의 목적이다(2026-08-19 사용자 지시). 표를 넓히다 보면 검색어가
    헐거워져 모든 소재가 A가 되기 쉬운데, 그러면 브랜드를 쓸 이유가 없는 글에도 브랜드
    문장이 붙어 블로그 전체가 홍보 채널로 읽힌다.
    """

    @pytest.mark.parametrize(
        "topic,category",
        [
            ("프로야구 순위", "스포츠"),
            ("강아지 산책 코스", "여행·장소"),
            ("동네 벚꽃 명소", "여행·장소"),
            ("아이돌 컴백 무대", "인물·연예인"),
        ],
    )
    def test_a_topic_with_no_tool_moment_is_refused(self, profile, topic, category):
        fit = evaluate_brand_fit(profile, topic, context=[category])

        assert fit.grade == BRAND_FIT_FORCED, f"{topic} → {fit.grade} ({fit.features})"
        assert not fit.usable
