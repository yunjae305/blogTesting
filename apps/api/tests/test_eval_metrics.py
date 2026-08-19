"""기준선 harness 지표 함수의 단위 테스트.

검증되지 않은 자로 재면 그 숫자로는 아무것도 주장할 수 없다. 여기 있는 테스트는 "지표가
의도한 것을 세는가"만 확인한다 — 원고 품질을 판정하지 않는다.
"""

from evals import metrics
from evals.cases import all_cases, extra_cases, grid_cases
from app.shared import FinalPost, TopicCandidate, TitleHookStrength, TitleHookType


def candidate(
    title: str,
    *,
    index: int = 1,
    hook: TitleHookType | None = None,
    strength: TitleHookStrength | None = None,
    title_type: str = "정보형",
) -> TopicCandidate:
    return TopicCandidate(
        topic_candidate_id=f"t{index}",
        title=title,
        description=title_type,
        trend_keyword_ids=["k1"],
        recommended=False,
        hook_type=hook,
        hook_strength=strength,
    )


class TestTitleMetrics:
    def test_length_compliance_counts_only_the_25_to_45_band(self):
        result = metrics.title_metrics(
            [
                candidate("짧은 제목", index=1),  # 5자
                candidate("제습기 물통 냄새를 없애는 관리 순서 정리해 봤어요", index=2),
            ]
        )
        assert result.lengths[0] == 5
        assert result.length_compliance == 0.5

    def test_same_opening_words_are_counted_as_duplicate_start_patterns(self):
        result = metrics.title_metrics(
            [
                candidate("제습기 관리 이렇게 하면 냄새가 사라집니다", index=1),
                candidate("제습기 관리 순서를 처음부터 정리했습니다", index=2),
                candidate("장마철 습도를 낮추는 가장 빠른 방법", index=3),
            ]
        )
        assert result.duplicate_start_patterns == 1

    def test_same_hook_and_title_type_counts_as_one_repeated_angle(self):
        result = metrics.title_metrics(
            [
                candidate("가", index=1, hook=TitleHookType.CURIOSITY, title_type="정보형"),
                candidate("나", index=2, hook=TitleHookType.CURIOSITY, title_type="정보형"),
                candidate("다", index=3, hook=TitleHookType.COMPARISON, title_type="비교형"),
            ]
        )
        assert result.same_angle_pairs == 1
        assert result.hook_type_distribution["CURIOSITY"] == 2

    def test_a_number_without_reference_material_is_flagged_but_a_count_is_not(self):
        result = metrics.title_metrics(
            [
                candidate("제습기 전기요금 30% 줄이는 방법", index=1),
                candidate("제습기 관리 3가지만 기억하세요", index=2),
            ],
            has_reference_material=False,
        )
        assert result.titles_with_unsourced_numbers == 1

    def test_numbers_are_not_flagged_when_reference_material_exists(self):
        result = metrics.title_metrics(
            [candidate("제습기 전기요금 30% 줄이는 방법", index=1)],
            has_reference_material=True,
        )
        assert result.titles_with_unsourced_numbers == 0

    def test_clickbait_markers_are_counted(self):
        result = metrics.title_metrics(
            [candidate("아무도 안 알려준 제습기 관리 비밀", index=1)]
        )
        assert result.clickbait_titles == 1

    def test_similarity_to_an_excluded_title_is_reported(self):
        result = metrics.title_metrics(
            [candidate("제습기 물통 냄새 없애는 관리 순서", index=1)],
            exclude_titles=["제습기 물통 냄새 없애는 관리 순서"],
        )
        assert result.max_similarity_to_excluded == 1.0

    def test_a_repeated_hook_and_type_combo_from_the_previous_batch_is_counted(self):
        result = metrics.title_metrics(
            [candidate("가", index=1, hook=TitleHookType.CURIOSITY, title_type="정보형")],
            previous_combos={("CURIOSITY", "정보형")},
        )
        assert result.repeated_hook_title_combos == 1


BODY = """# 제습기 관리 순서

장마가 시작되면 제습기를 하루 종일 돌리게 됩니다. 물통을 비우는 것만으로는 냄새가 잡히지
않습니다.

## 물통을 어떻게 관리하나

먼저 물통을 분리해 미온수로 헹굅니다. 또한 바닥에 남은 물기를 마른 천으로 닫아 냅니다.
또한 주 1회는 구연산을 풀어 담가 둡니다.

## 필터는 얼마나 자주 씻나

필터는 2주에 한 번 물로 씻어 그늘에서 말립니다. 습도 60% 아래로 유지하면 곰팡이가 덜
생깁니다.

## 그래서 무엇부터 하나

물통을 어떻게 관리하나, 필터는 얼마나 자주 씻나. 결론적으로 오늘은 물통부터 헹구면 됩니다.
"""


def _post(markdown: str = BODY, *, title: str = "제습기 관리 순서") -> FinalPost:
    plain = "\n".join(
        line for line in markdown.splitlines() if not line.strip().startswith("#")
    )
    return FinalPost(
        title=title,
        body=plain,
        hashtags=["제습기", "장마"],
        html_content=f"<h1>{title}</h1>",
        markdown_content=markdown,
    )


class TestDraftMetrics:
    def test_structure_counts_match_the_markdown(self):
        result = metrics.draft_metrics(_post(), target_min=2500, target_max=3500)
        assert result.h2_count == 3
        assert result.paragraph_count >= 4
        assert result.within_target is False
        assert result.delta_to_target < 0  # 목표 하한보다 짧다

    def test_the_same_connective_repeated_is_counted_per_word(self):
        result = metrics.draft_metrics(_post(), target_min=100, target_max=100000)
        assert result.connective_counts["또한"] == 2
        assert result.max_connective_repeats == 2

    def test_an_intro_cliche_is_positional_not_global(self):
        with_intro = _post("오늘은 제습기 관리에 대해 알아보겠습니다.\n\n## 소제목\n\n본문입니다.")
        with_outro = _post("장마가 시작됐습니다.\n\n## 소제목\n\n오늘은 여기까지입니다.")
        assert "오늘은" in metrics.draft_metrics(
            with_intro, target_min=1, target_max=10
        ).intro_cliche_hits
        assert (
            metrics.draft_metrics(with_outro, target_min=1, target_max=10).intro_cliche_hits
            == []
        )

    def test_uniform_h2_grammar_needs_at_least_three_headings_all_alike(self):
        alike = _post("도입\n\n## 관리하는 방법\n\n가\n\n## 씻는 방법\n\n나\n\n## 고르는 방법\n\n다")
        mixed = _post("도입\n\n## 관리하는 방법\n\n가\n\n## 필터는 왜 막히나\n\n나\n\n## 오늘 할 일\n\n다")
        assert metrics.draft_metrics(alike, target_min=1, target_max=10).uniform_h2_grammar
        assert not metrics.draft_metrics(mixed, target_min=1, target_max=10).uniform_h2_grammar

    def test_a_number_present_in_the_reference_text_is_not_an_unsupported_claim(self):
        post = _post("도입\n\n## 소제목\n\n급속충전은 18분이 걸립니다.")
        supported = metrics.draft_metrics(
            post, target_min=1, target_max=10, reference_text="여름 급속충전 18분"
        )
        unsupported = metrics.draft_metrics(post, target_min=1, target_max=10)
        assert supported.unsupported_numeric_claims == []
        assert unsupported.unsupported_numeric_claims == ["18분"]

    def test_a_structural_count_is_not_treated_as_a_numeric_claim(self):
        post = _post("도입\n\n## 소제목\n\n확인할 것은 3가지입니다.")
        assert metrics.draft_metrics(post, target_min=1, target_max=10).unsupported_numeric_claims == []

    def test_title_words_repeated_in_the_first_paragraph_raise_the_overlap(self):
        echo = _post("제습기 관리 순서를 정리했습니다.\n\n## 소제목\n\n본문", title="제습기 관리 순서")
        fresh = _post("장마가 시작되면 빨래가 마르지 않습니다.\n\n## 소제목\n\n본문", title="제습기 관리 순서")
        assert (
            metrics.draft_metrics(echo, target_min=1, target_max=10).title_first_paragraph_overlap
            > metrics.draft_metrics(fresh, target_min=1, target_max=10).title_first_paragraph_overlap
        )

    def test_a_conclusion_that_lists_the_headings_again_scores_high_overlap(self):
        result = metrics.draft_metrics(_post(), target_min=1, target_max=10)
        assert result.conclusion_body_overlap > 0.2

    def test_markdown_emphasis_is_counted_even_though_the_body_has_no_html(self):
        post = _post("도입\n\n## 소제목\n\n**핵심**은 여기고 ==강조==도 있습니다.")
        assert metrics.draft_metrics(post, target_min=1, target_max=10).emphasis_count == 2

    def test_seo_avoid_violations_are_reported(self):
        class Plan:
            primary = "제습기 관리"
            secondary = ["물통 냄새", "필터 세척"]
            avoid = ["최저가"]

        post = _post("제습기 관리는 최저가 제품이어도 같습니다.\n\n## 소제목\n\n물통 냄새를 잡습니다.")
        result = metrics.draft_metrics(post, target_min=1, target_max=10, seo_plan=Plan())
        assert result.seo_avoid_violations == ["최저가"]
        assert result.seo_secondary_used == 1
        assert result.seo_primary_in_first_paragraph is True


class TestVisualRotation:
    def test_the_palette_is_stable_per_post_and_neighbouring_shots_differ(self):
        report = metrics.rotation_determinism()
        assert report["palette_stable_per_post"] is True
        assert report["neighbour_shots_differ"] is True
        assert report["rotation_wraps_to_start"] is True
        assert report["distinct_palettes_over_24_posts"] > 1


class TestCases:
    def test_the_grid_is_27_combinations(self):
        assert len(grid_cases()) == 27

    def test_the_three_conflict_pairs_the_spec_named_are_present(self):
        conflicts = {
            (case.persona_label, case.purpose) for case in grid_cases() if case.is_conflict
        }
        assert ("일상 기록 블로거", "문제 해결") in conflicts
        assert ("브랜드 스토리텔러", "비교·추천") in conflicts
        assert ("실무 코치", "후기·리뷰 작성") in conflicts

    def test_extra_cases_cover_custom_persona_and_a_locked_trend_title(self):
        tags = {tag for case in extra_cases() for tag in case.tags}
        assert "custom-persona" in tags
        assert "locked-title" in tags
        assert any(case.trend_title for case in extra_cases())

    def test_every_case_builds_a_valid_blog_input_and_trend_keyword(self):
        for case in all_cases():
            assert case.blog_input.topic
            assert case.blog_input.purpose == [case.purpose]
            assert case.trend_keyword_model().keyword
            assert case.settings.default_persona
