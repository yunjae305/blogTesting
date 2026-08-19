"""제목 루브릭 채점(순수 함수)의 단위 테스트.

추천이 index==0 같은 임의 기준이 아니라 설명 가능한 점수로 정해지는지, 감점 규칙과 트렌드
미선택 재정규화가 맞는지 확인한다.
"""

from app.modules.trend.topic_scoring import (
    RUBRIC_WITH_TREND,
    RUBRIC_WITHOUT_TREND,
    TitleJudgmentScore,
    build_context,
    evaluate_rules,
    near_duplicate_indices,
    score_titles,
)
from app.shared import TopicCandidate


def cand(title: str, index: int = 1) -> TopicCandidate:
    return TopicCandidate(
        topic_candidate_id=f"t{index}",
        title=title,
        description="정보형",
        trend_keyword_ids=["k1"],
        recommended=False,
    )


def context(trend: str | None = "월드컵"):
    return build_context(
        topic="AIONA",
        subject="멀티 LLM 플랫폼",
        purpose=["정보 전달"],
        audience="AI 도입을 고민하는 실무자",
        trend_keyword=trend,
    )


class TestRubricWeights:
    def test_with_trend_weights_sum_to_100(self):
        assert round(sum(RUBRIC_WITH_TREND.values()), 4) == 100.0
        assert RUBRIC_WITH_TREND == {
            "relevance": 30.0,
            "trend": 25.0,
            "purpose": 20.0,
            "audience": 15.0,
            "quality": 10.0,
        }

    def test_without_trend_renormalizes_to_100_without_the_trend_term(self):
        assert "trend" not in RUBRIC_WITHOUT_TREND
        assert round(sum(RUBRIC_WITHOUT_TREND.values()), 2) == 100.0
        # 소재 관련성은 30/75*100 = 40으로 커진다.
        assert round(RUBRIC_WITHOUT_TREND["relevance"], 1) == 40.0


class TestRules:
    def test_clickbait_is_penalized(self):
        clean = evaluate_rules("월드컵 마케팅에 AIONA 활용하는 법", context())
        bait = evaluate_rules("충격! 무조건 봐야 하는 대박 AIONA", context())
        assert clean.quality > bait.quality
        assert bait.is_clickbait and not clean.is_clickbait

    def test_fabricated_ratio_is_clickbait(self):
        assert evaluate_rules("10명 중 7명이 모르는 AIONA", context()).is_clickbait
        assert evaluate_rules("AIONA 도입률 80% 시대", context()).is_clickbait

    def test_overly_long_titles_lose_quality(self):
        short = evaluate_rules("AIONA로 월드컵 콘텐츠 만들기", context())
        long = evaluate_rules("월드컵" * 20, context())
        assert long.quality < short.quality

    def test_subject_and_trend_presence_is_detected(self):
        rule = evaluate_rules("월드컵 콘텐츠도 AIONA로 만드는 법", context())
        assert rule.has_subject and rule.has_trend
        missing = evaluate_rules("그냥 평범한 잡담 제목입니다", context())
        assert not missing.has_subject and not missing.has_trend


class TestNearDuplicate:
    def test_word_order_variants_are_flagged(self):
        titles = [
            "월드컵 콘텐츠를 AIONA로 만드는 법",
            "AIONA로 월드컵 콘텐츠 만드는 법",  # 단어 순서만 바꾼 사실상 같은 제목
            "AIONA와 월드컵, 마케팅 담당자를 위한 질문",
        ]
        assert near_duplicate_indices(titles) == {1}

    def test_distinct_titles_are_not_flagged(self):
        titles = [
            "월드컵 마케팅에 AIONA 쓰는 법",
            "AIONA와 경쟁 서비스, 무엇이 다를까",
            "콘텐츠 자동화가 처음이라면 알아야 할 것",
        ]
        assert near_duplicate_indices(titles) == set()


class TestScoreTitles:
    def test_recommended_is_the_highest_score_not_index_zero(self):
        candidates = [
            cand("충격 대박 무조건 봐야 하는 그것", 1),  # 낚시 + 소재·트렌드 없음
            cand("월드컵 마케팅에 AIONA를 활용하는 방법", 2),  # 깔끔 + 소재·트렌드 포함
            cand("AIONA 소개", 3),  # 너무 짧음 + 트렌드 없음
        ]
        scored = score_titles(candidates, context())

        recommended = [c for c in scored if c.recommended]
        assert len(recommended) == 1
        assert recommended[0].title == "월드컵 마케팅에 AIONA를 활용하는 방법"
        # 첫 후보(낚시)는 추천이 아니고 점수도 더 낮다.
        assert scored[0].score < scored[1].score

    def test_every_candidate_gets_a_score_and_a_reason(self):
        scored = score_titles([cand("월드컵과 AIONA 활용법", 1), cand("AIONA 질문", 2)], context())
        assert all(c.score is not None for c in scored)
        assert all(c.reason for c in scored)

    def test_clickbait_and_duplicates_are_penalized_in_the_total(self):
        base = cand("월드컵 콘텐츠에 AIONA 활용하는 법", 1)
        dupe = cand("AIONA로 월드컵 콘텐츠 활용하는 법", 2)  # 순서만 바꾼 중복
        bait = cand("충격! AIONA 무조건 써야 하는 이유", 3)  # 낚시 + 트렌드 없음
        scored = score_titles([base, dupe, bait], context())

        by_title = {c.title: c.score for c in scored}
        assert by_title[dupe.title] < by_title[base.title]  # 중복 감점
        assert by_title[bait.title] < by_title[base.title]  # 낚시 감점
        assert scored[0].recommended  # 깔끔한 원본이 추천

    def test_without_trend_does_not_require_the_trend_keyword(self):
        # 트렌드 미선택: 트렌드 항이 빠지므로, 트렌드가 없는 제목도 소재만 잘 담으면 추천될 수 있다.
        candidates = [
            cand("멀티 LLM 플랫폼 AIONA, 도입 전 체크리스트", 1),
            cand("월드컵 얘기만 잔뜩 있는 제목", 2),
        ]
        scored = score_titles(candidates, context(trend=None))

        assert scored[0].recommended  # 소재를 담은 제목
        assert all(0.0 <= c.score <= 100.0 for c in scored)

    def test_llm_judgments_drive_the_subjective_scores(self):
        candidates = [cand("월드컵 AIONA 활용법", 1), cand("AIONA 월드컵 마케팅", 2)]
        # 두 번째 제목을 LLM이 훨씬 높게 본다 — 규칙만으로는 비슷하지만 추천이 뒤집혀야 한다.
        judgments = {
            "월드컵 AIONA 활용법": TitleJudgmentScore(30, 30, 30, 30, reason="약함"),
            "AIONA 월드컵 마케팅": TitleJudgmentScore(
                95, 95, 95, 95, reason="소재와 트렌드를 자연스럽게 연결"
            ),
        }
        scored = score_titles(candidates, context(), judgments)

        recommended = next(c for c in scored if c.recommended)
        assert recommended.title == "AIONA 월드컵 마케팅"
        # LLM이 준 근거가 그대로 표시된다.
        assert recommended.reason == "소재와 트렌드를 자연스럽게 연결"

    def test_empty_input_returns_empty(self):
        assert score_titles([], context()) == []
