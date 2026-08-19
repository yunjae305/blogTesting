"""생성 후 SEO·콘텐츠 품질 검증(§6~§11).

두 부류를 분명히 나눠 확인한다: SEO Primary가 제목에 없을 때만 FAIL이고, 첫 문단 누락과
나머지는 WARN/PASS/SKIPPED다. 어떤 WARN도 원고를 반려시키지 않는다.
"""

from app.modules.draft.content_validation import (
    aggregate_validation_results,
    count_h2,
    first_substantive_paragraph,
    run_content_validations,
    validate_h2_count,
    validate_primary_keyword_in_first_paragraph,
    validate_primary_keyword_in_title,
    validate_secondary_keyword_usage,
    validate_source_usage,
    validate_title_promise,
)
from app.shared import (
    FinalPost,
    ReferenceMaterial,
    ReferenceMaterialType,
    SearchSource,
    SeoKeywordPlan,
    SourceDataPoint,
)

LONG_BODY = "\n\n".join(
    f"{n}번 문단입니다. 실제 설명과 사례와 판단 기준을 담아 독자에게 정보를 전달합니다."
    for n in range(1, 8)
)


def build_post(title="아이폰17 출시일과 가격 총정리", body=None, markdown=None) -> FinalPost:
    body = LONG_BODY if body is None else body
    markdown = markdown if markdown is not None else f"# {title}\n\n{body}"
    return FinalPost(
        title=title,
        body=body,
        hashtags=["아이폰17"],
        html_content=f"<article><h1>{title}</h1><p>{body}</p></article>",
        markdown_content=markdown,
    )


def plan(primary="아이폰17", secondary=None, avoid=None) -> SeoKeywordPlan:
    return SeoKeywordPlan(
        primary=primary,
        secondary=secondary if secondary is not None else [],
        avoid=avoid if avoid is not None else [],
    )


class TestPrimaryInTitle:
    def test_present_passes(self):
        result = validate_primary_keyword_in_title(build_post(), plan("아이폰17"))
        assert result.status == "PASS"

    def test_missing_warns_without_rewriting_the_article(self):
        result = validate_primary_keyword_in_title(
            build_post(title="갤럭시 신제품 소식"), plan("아이폰17")
        )
        assert result.status == "FAIL"
        assert result.check == "seo_primary_in_title"
        assert result.rejected is True

    def test_no_plan_is_skipped(self):
        result = validate_primary_keyword_in_title(build_post(), None)
        assert result.status == "SKIPPED"

    def test_a_locked_title_warns_instead_of_rejecting_forever(self):
        """제목이 확정된 글은 원고를 다시 써도 제목이 그대로다. FAIL로 두면 재생성이 매번
        같은 곳에서 걸려 사용자가 결과물 없이 막힌다 — 신호로만 남긴다."""
        result = validate_primary_keyword_in_title(
            build_post(title="갤럭시 신제품 소식"), plan("아이폰17"), title_locked=True
        )

        assert result.status == "WARN"
        assert result.rejected is False

    def test_a_locked_title_that_matches_still_passes(self):
        result = validate_primary_keyword_in_title(
            build_post(), plan("아이폰17"), title_locked=True
        )
        assert result.status == "PASS"

    def test_spacing_difference_still_matches(self):
        result = validate_primary_keyword_in_title(
            build_post(title="아이폰 17 출시일 총정리"), plan("아이폰17")
        )
        assert result.status == "PASS"


class TestPrimaryInFirstParagraph:
    def test_present_passes(self):
        markdown = "# 제목\n\n아이폰17 출시일을 먼저 정리합니다.\n\n## 본문\n\n내용"
        result = validate_primary_keyword_in_first_paragraph(
            build_post(markdown=markdown), plan("아이폰17")
        )
        assert result.status == "PASS"

    def test_missing_fails(self):
        markdown = "# 아이폰17 정리\n\n이번 글에서는 여러 가지를 다룹니다.\n\n## 본문\n\n아이폰17 이야기"
        result = validate_primary_keyword_in_first_paragraph(
            build_post(markdown=markdown), plan("아이폰17")
        )
        assert result.status == "WARN"
        assert result.check == "seo_primary_in_first_paragraph"
        assert result.rejected is False

    def test_headings_and_markers_are_skipped_to_find_the_first_prose(self):
        markdown = "# 제목\n\n[[IMAGE: cover]]\n\n## 소제목\n\n아이폰17 첫 설명 문단."
        para = first_substantive_paragraph(markdown)
        assert para == "아이폰17 첫 설명 문단."


class TestSecondaryUsage:
    def test_all_used_passes(self):
        body = "아이폰17 가격과 아이폰17 출시일을 정리합니다."
        result = validate_secondary_keyword_usage(
            build_post(body=body), plan(secondary=["아이폰17 가격", "아이폰17 출시일"])
        )
        assert result.status == "PASS"

    def test_some_unused_only_warns(self):
        body = "아이폰17 가격만 다룹니다."
        result = validate_secondary_keyword_usage(
            build_post(body=body), plan(secondary=["아이폰17 가격", "아이폰17 색상"])
        )
        assert result.status == "WARN"
        assert result.rejected is False
        assert "아이폰17 색상" in result.details["unusedKeywords"]

    def test_no_secondary_is_skipped(self):
        result = validate_secondary_keyword_usage(build_post(), plan(secondary=[]))
        assert result.status == "SKIPPED"


class TestH2Count:
    def _markdown_with_h2(self, count):
        body = "\n\n".join(f"## 소제목 {i}\n\n문단 내용입니다." for i in range(1, count + 1))
        return f"# 제목\n\n도입 문단입니다.\n\n{body}"

    def test_two_headings_warn(self):
        result = validate_h2_count(build_post(markdown=self._markdown_with_h2(2)))
        assert result.status == "WARN"
        assert result.details["actualCount"] == 2

    def test_three_headings_pass(self):
        result = validate_h2_count(build_post(markdown=self._markdown_with_h2(3)))
        assert result.status == "PASS"

    def test_six_headings_pass(self):
        result = validate_h2_count(build_post(markdown=self._markdown_with_h2(6)))
        assert result.status == "PASS"

    def test_seven_headings_warn(self):
        result = validate_h2_count(build_post(markdown=self._markdown_with_h2(7)))
        assert result.status == "WARN"

    def test_h1_h3_and_code_fences_are_not_counted(self):
        markdown = (
            "# 제목\n\n## 진짜 소제목 1\n\n### 하위 제목\n\n"
            "```\n## 코드 안의 샵\n```\n\n## 진짜 소제목 2\n\n> ## 인용 안의 샵"
        )
        assert count_h2(markdown) == 2


class TestSourceUsage:
    def test_all_sources_used_passes(self):
        body = "아이폰17 공식 사양을 정리하면 배터리와 카메라가 개선되었습니다."
        sources = [SearchSource(title="아이폰17 공식 사양", url="https://a.com", snippet="사양 요약")]
        result = validate_source_usage(build_post(body=body), sources, [])
        assert result.status == "PASS"

    def test_some_unused_warns(self):
        body = "아이폰17 공식 사양만 다룹니다."
        sources = [
            SearchSource(title="아이폰17 공식 사양", url="https://a.com", snippet="사양"),
            SearchSource(title="갤럭시 비교표", url="https://b.com", snippet="갤럭시 비교"),
        ]
        result = validate_source_usage(build_post(body=body), sources, [])
        assert result.status == "WARN"
        assert result.rejected is False
        assert any(s["title"] == "갤럭시 비교표" for s in result.details["unusedSources"])

    def test_data_point_value_counts_as_used(self):
        body = "조사에 따르면 이용률은 85까지 올랐습니다."
        sources = [
            SearchSource(
                title="전혀 다른 제목",
                url="https://a.com",
                snippet="요약",
                data_points=[SourceDataPoint(label="이용률", value=85, unit="%")],
            )
        ]
        result = validate_source_usage(build_post(body=body), sources, [])
        assert result.status == "PASS"

    def test_no_sources_is_skipped(self):
        result = validate_source_usage(build_post(), [], [])
        assert result.status == "SKIPPED"

    def test_only_empty_sources_is_skipped_with_reason(self):
        sources = [SearchSource(title="", url="https://a.com", snippet="")]
        result = validate_source_usage(build_post(), sources, [])
        assert result.status == "SKIPPED"
        assert result.details["excludedSources"]

    def test_image_material_is_excluded_not_failed(self):
        materials = [
            ReferenceMaterial(type=ReferenceMaterialType.IMAGE, value="data:image/png;base64,x")
        ]
        result = validate_source_usage(build_post(), [], materials)
        assert result.status == "SKIPPED"


class TestTitlePromise:
    def test_missing_promise_warns(self):
        # 제목은 '가격과 기능'을 약속했지만 본문에는 기능만 있다.
        body = "이 제품의 기능과 특징을 자세히 설명합니다. 지원하는 항목이 많습니다."
        result = validate_title_promise(build_post(title="아이폰17 가격과 기능 정리", body=body))
        assert result.status == "WARN"
        promises = {p["promise"]: p["status"] for p in result.details["promises"]}
        assert promises.get("가격") == "MISSING"
        assert promises.get("기능") == "COVERED"

    def test_number_promise_shortfall_warns(self):
        markdown = "# 초보자에게 추천하는 운동 5가지\n\n도입.\n\n- 걷기\n\n- 스트레칭\n\n- 요가"
        result = validate_title_promise(build_post(title="초보자에게 추천하는 운동 5가지", markdown=markdown))
        assert result.status == "WARN"

    def test_no_promise_is_skipped(self):
        result = validate_title_promise(build_post(title="아이폰17 이야기"))
        assert result.status == "SKIPPED"

    def test_fully_covered_promise_passes(self):
        body = "가격은 100만원이며 주요 기능과 특징도 지원합니다."
        result = validate_title_promise(build_post(title="아이폰17 가격과 기능 정리", body=body))
        assert result.status == "PASS"


class TestAggregateAndRun:
    def test_status_is_pass_with_warnings_when_any_warn(self):
        checks = [
            validate_primary_keyword_in_title(build_post(), plan("아이폰17")),
            validate_secondary_keyword_usage(
                build_post(body="다른 내용"), plan(secondary=["미사용 키워드"])
            ),
        ]
        result = aggregate_validation_results(checks)
        assert result.status == "PASS_WITH_WARNINGS"
        assert result.has_fail is False

    def test_status_is_fail_when_primary_missing(self):
        result = run_content_validations(
            build_post(title="갤럭시 소식", markdown="# 갤럭시 소식\n\n갤럭시 이야기입니다."),
            plan("아이폰17"),
            [],
            [],
        )
        assert result.status == "FAIL"
        assert result.has_fail is True
        assert result.fail_messages()

    def test_a_locked_title_produces_no_fail(self):
        """같은 상황이라도 제목이 확정된 글이면 반려하지 않는다 — 재생성이 못 고친다."""
        result = run_content_validations(
            build_post(title="갤럭시 소식", markdown="# 갤럭시 소식\n\n갤럭시 이야기입니다."),
            plan("아이폰17"),
            [],
            [],
            title_locked=True,
        )
        assert result.has_fail is False
        assert result.fail_messages() == []

    def test_no_seo_plan_produces_no_fail(self):
        """SEO 계획이 없는 기존 데이터·스텁 경로: SEO 검사는 SKIPPED, 품질 검사는 WARN/PASS로만
        나와 절대 FAIL이 없다(반려로 이어지지 않는다)."""
        result = run_content_validations(build_post(), None, [], [])
        assert result.has_fail is False

    def test_run_produces_every_check(self):
        result = run_content_validations(build_post(), plan("아이폰17"), [], [])
        names = {c.check for c in result.checks}
        assert names == {
            "seo_primary_in_title",
            "seo_primary_in_first_paragraph",
            "seo_secondary_usage",
            "h2_count",
            "source_usage",
            "title_promise",
            # 2026-07-28 추가: 시각자료가 목적·근거에 맞는지, 참고자료가 실제로 반영됐는지,
            # 겪지 않은 경험을 쓰지 않았는지, 구성이 템플릿으로 굳지 않았는지.
            "visual_purpose_fit",
            "visual_evidence_sufficiency",
            "visual_redundancy",
            "reference_anchor_usage",
            "template_repetition",
            # 2026-08-03 추가: 원본 검색어를 명사처럼 썼는지, 영상 콘텐츠의 핵심 포맷이
            # 도입부에 있는지, 보조 장면이 글의 얼굴을 전부 차지했는지.
            "raw_keyword_grammar",
            "program_format_grounding",
            "secondary_activity_emphasis",
            # 2026-08-03 추가(카테고리): 카테고리가 요구하는 정보가 들어 있는지, 제목이
            # 겪지 않은 체험을 약속했는지, 정확성이 중요한 주제에서 단정했는지.
            "category_fit",
            "high_stakes_certainty",
            # 2026-08-06 추가: 이미 만들어 둔 내 글들과의 닮음. 한 편씩 보는 다른 검사는
            # 못 잡는다(자동 생성은 쌓였을 때 닮는 것이 위험이다).
            "published_duplication",
        }

    def test_new_checks_are_skipped_when_their_inputs_are_absent(self):
        """새 인자는 전부 선택이다 — 옛 호출부(시각자료·근거 없이 부르던 곳)가 그대로 돈다."""
        result = run_content_validations(build_post(), plan("아이폰17"), [], [])
        by_name = {c.check: c for c in result.checks}
        assert by_name["visual_purpose_fit"].status == "SKIPPED"
        assert by_name["visual_evidence_sufficiency"].status == "SKIPPED"
        assert by_name["visual_redundancy"].status == "SKIPPED"
        assert by_name["reference_anchor_usage"].status == "SKIPPED"
        assert result.has_fail is False
