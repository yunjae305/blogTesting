"""콘텐츠 설계(plan) + 시각자료(표·차트·과정도·인포그래픽) 파이프라인.

스펙의 네 시나리오를 코드 수준에서 고정한다:
- A(홍보형): 설계가 시각자료를 계획하고, 실측 수치가 있는 그래프가 렌더링돼 배치된다.
- B(데이터 없음): 출처·수치 없는 그래프는 렌더링되지 않고 마커만 걷힌다.
- C(경험 없는 리뷰): 가상 체험 문구는 반려되고, 설계 프롬프트가 정보형 전환을 지시한다.
- D(여행·맛집): 통계·구조도를 강제하지 않는다는 지시가 설계 프롬프트에 있다.
"""

import pytest

from app.llm.parsing import content_plan_from_json, planned_visuals_from_json
from app.llm.prompts import (
    content_plan_prompt,
    draft_prompt,
    planned_photo_count,
)
from app.llm.schemas import DRAFT_SCHEMA
from app.modules.draft.quality import check_draft
from app.modules.draft.service import DraftService
from app.modules.draft.visuals import (
    _fit_table_text,
    _table_cell_alignment,
    _table_column_groups,
    render_planned_visual,
    replace_visual_markers,
    visual_html,
)
from app.modules.blog_task.repository import InMemoryBlogTaskRepository
from app.shared import (
    BlogTaskInput,
    CardBrief,
    CardScene,
    ContentPlan,
    ContentPlanSection,
    DraftFormat,
    DraftGenerationInput,
    DraftGenerationSettings,
    DraftGenerationResult,
    FinalPost,
    PlannedVisual,
    ReferenceMaterial,
    ReferenceMaterialType,
    SearchSource,
    SelectedIntentForDraft,
    SourceDataPoint,
    VisualCardPlan,
    VisualDataPoint,
    VisualGroup,
    VisualTableRow,
)

NOW = "1970-01-01T00:00:00.000Z"


def test_draft_visual_schema_requires_its_content_plan_section():
    visual_schema = DRAFT_SCHEMA["properties"]["visuals"]["items"]

    assert "sectionId" in visual_schema["required"]
    section_schema = visual_schema["properties"]["sectionId"]
    assert section_schema["minLength"] == 1
    assert section_schema["pattern"] == r"^section-[1-9][0-9]*$"


def _card(card_id: str, card_type: str, section_id: str | None, score: float) -> CardBrief:
    return CardBrief(
        card_id=card_id,
        card_type=card_type,
        section_id=section_id,
        article_claim="AIONA는 여러 AI 모델을 한 화면에서 쓸 수 있다",
        visual_purpose="통합 사용 장면을 보여준다",
        eyebrow="한눈에 보는 핵심",
        headline_lines=["여러 AI를", "한 화면에서"],
        emphasis_words=["한 화면"],
        summary_lines=["구독 전 꼭 확인해 보세요"],
        icon_type="check",
        scene=CardScene(
            main_subject=f"a student desk for {card_id}",
            action="comparing AI tools on a laptop",
            setting=f"a Korean university library seat {card_id}",
        ),
        necessity_score=score,
    )


def build_draft_input(**overrides) -> DraftGenerationInput:
    defaults = dict(
        post_id="post_1",
        user_id="user_1",
        prompt_version="m4-draft@v1.1",
        format=DraftFormat.MARKDOWN,
        input=BlogTaskInput(
            topic="AIONA",
            subject="IT·디지털",
            purpose=["제품·서비스 홍보"],
            keywords=["AI"],
            target_reader="대학생",
            reference_materials=[],
        ),
        selected_intent=SelectedIntentForDraft(
            intent_id="i1",
            title="AIONA 대학생 활용",
            target_reader="대학생",
            rationale="활용 관점",
            sources=[],
        ),
        settings=DraftGenerationSettings(hashtag_count=5, article_length="medium"),
    )
    return DraftGenerationInput(**{**defaults, **overrides})


def build_plan(*section_visuals: str) -> ContentPlan:
    return ContentPlan(
        target_reader="대학생",
        reader_problem="여러 AI를 따로 구독하고 오가는 번거로움",
        reader_question="여러 AI를 한곳에서 쓸 수 없을까?",
        article_promise="통합 플랫폼 활용법을 안다",
        content_angle="AI 구독 피로와 통합",
        article_type="PROMOTION",
        sections=[
            ContentPlanSection(
                section_id=f"section-{i + 1}",
                heading=f"질문형 소제목 {i + 1}",
                question=f"질문 {i + 1}",
                purpose="근거 제시",
                visual_type=visual,
            )
            for i, visual in enumerate(section_visuals)
        ],
    )


BAR_VISUAL = PlannedVisual(
    visual_id="visual-1",
    type="BAR_CHART",
    title="생성형 AI 구독 이용 비교",
    caption="KB국민카드 생성형 AI 구독 고객 분석, 2026.02",
    alt_text="생성형 AI 구독 이용 비교 막대그래프",
    data=[
        VisualDataPoint(label="2025년", value=42.0),
        VisualDataPoint(label="2026년", value=67.0),
    ],
    unit="%",
    source="source-1",
    published_at="2026-02",
)


def build_chart_draft_input(**overrides) -> DraftGenerationInput:
    selected_intent = SelectedIntentForDraft(
        intent_id="i1",
        title="AIONA 대학생 활용",
        target_reader="대학생",
        rationale="활용 관점",
        sources=[
            SearchSource(
                title="KB국민카드 데이터본부",
                url="https://example.com/report",
                snippet="생성형 AI 구독 이용 비교",
                data_points=[
                    SourceDataPoint(label="2025년", value=42.0, unit="%"),
                    SourceDataPoint(label="2026년", value=67.0, unit="%"),
                ],
            )
        ],
    )
    return build_draft_input(selected_intent=selected_intent, **overrides)


class TestRenderers:
    def test_bar_chart_renders_to_png_data_url(self):
        image = render_planned_visual(BAR_VISUAL)
        assert image is not None
        assert image.data_url.startswith("data:image/png;base64,")
        assert len(image.data_url) > 2000
        assert image.source == "rendered"
        assert "KB국민카드" in image.caption

    def test_chart_without_a_source_is_refused(self):
        """테스트 B: 출처 없는 그래프는 그리지 않는다 — 임의 그래프 생성 금지."""
        visual = BAR_VISUAL.model_copy(update={"source": None})
        assert render_planned_visual(visual) is None

    def test_chart_with_too_few_points_is_refused(self):
        visual = BAR_VISUAL.model_copy(
            update={"data": [VisualDataPoint(label="하나", value=1.0)]}
        )
        assert render_planned_visual(visual) is None

    def test_line_chart_needs_a_real_time_series(self):
        visual = BAR_VISUAL.model_copy(update={"type": "LINE_CHART"})
        # 점 2개는 시계열이 아니다.
        assert render_planned_visual(visual) is None
        three = visual.model_copy(
            update={
                "data": [
                    VisualDataPoint(label="1분기", value=10.0),
                    VisualDataPoint(label="2분기", value=20.0),
                    VisualDataPoint(label="3분기", value=35.0),
                ]
            }
        )
        assert render_planned_visual(three) is not None

    def test_process_diagram_and_infographic_render_without_numeric_data(self):
        diagram = PlannedVisual(
            visual_id="visual-2",
            type="PROCESS_DIAGRAM",
            title="소재 입력부터 발행까지",
            steps=["소재 입력", "제목 선택", "원고 생성", "발행"],
        )
        infographic = PlannedVisual(
            visual_id="visual-3",
            type="INFOGRAPHIC",
            title="AIONA의 대학 생활 활용 분야",
            center_topic="AIONA",
            groups=[
                VisualGroup(name="학습", items=["강의 요약", "시험 준비"]),
                VisualGroup(name="취업", items=["자기소개서", "면접 준비"]),
            ],
        )
        assert render_planned_visual(diagram) is not None
        rendered = render_planned_visual(infographic)
        assert rendered is not None
        # 출처 없는 자체 구성 자료에는 하단 문구를 붙이지 않는다 — 서비스명 워터마크가 사라졌다.
        assert rendered.caption == ""
        assert "Blog-it" not in rendered.caption

    def test_a_step_saved_as_plain_text_still_loads(self):
        """단계가 문자열이던 시절의 저장 문서도 열려야 한다 — 형식이 바뀌었다고 예전 글이
        안 열리면 안 된다."""
        diagram = PlannedVisual(
            visual_id="visual-2",
            type="PROCESS_DIAGRAM",
            title="예전 형식",
            steps=["소재 입력", "제목 선택", "발행"],
        )
        assert [step.label for step in diagram.steps] == ["소재 입력", "제목 선택", "발행"]
        assert diagram.steps[0].detail is None
        assert render_planned_visual(diagram) is not None

    def test_a_calculation_step_keeps_its_formula(self):
        """계산 과정 도표에서 잘리면 안 되는 건 식이다. 파싱이 label·detail을 모두 살린다."""
        visuals = planned_visuals_from_json(
            [
                {
                    "visualId": "visual-1",
                    "type": "PROCESS_DIAGRAM",
                    "title": "월 전기 사용량 계산",
                    "steps": [
                        {"label": "소비전력 확인", "detail": "1,500W"},
                        {"label": "kW로 변환", "detail": "1,500 ÷ 1,000 = 1.5kW"},
                        {"label": "최종 사용량", "detail": "월 약 234kWh"},
                    ],
                }
            ]
        )
        assert [step.detail for step in visuals[0].steps] == [
            "1,500W",
            "1,500 ÷ 1,000 = 1.5kW",
            "월 약 234kWh",
        ]
        assert render_planned_visual(visuals[0]) is not None

    def test_a_comparison_table_renders_and_needs_both_axes(self):
        table = PlannedVisual(
            visual_id="visual-4",
            type="TABLE",
            title="요금제 비교",
            columns=["월 요금", "장점"],
            rows=[
                VisualTableRow(name="A 요금제", cells=["1만 원", "저렴함"]),
                VisualTableRow(name="B 요금제", cells=["2만 원", "속도 빠름"]),
            ],
        )
        assert render_planned_visual(table) is not None
        # 기준이 하나뿐이면 비교가 아니다.
        assert render_planned_visual(table.model_copy(update={"columns": ["월 요금"]})) is None
        # 대상이 하나뿐이어도 비교가 아니다.
        assert render_planned_visual(table.model_copy(update={"rows": table.rows[:1]})) is None

    def test_visuals_are_drawn_at_double_size_and_scaled_down(self):
        """글자는 2배로 그린 뒤 줄인다.

        표의 칸 중앙 같은 좌표는 나눗셈에서 나와 소수가 되는데, 그 자리에 바로 그리면 획이
        픽셀 두 칸에 반씩 걸쳐 '910원'처럼 얇은 글자가 깨져 보인다. 결과물 크기는 규격
        그대로여야 하므로(발행 본문 폭), 크게 그렸다는 사실이 크기로 새어 나오면 안 된다.
        """
        import base64
        import io

        from PIL import Image

        from app.modules.draft.visuals import _SCALE

        assert _SCALE >= 2

        table = PlannedVisual(
            visual_id="visual-9",
            type="TABLE",
            title="구간별 요금 비교",
            columns=["기본요금", "단가"],
            rows=[
                VisualTableRow(name="1단계", cells=["910원", "120.0원/kWh"]),
                VisualTableRow(name="2단계", cells=["1,600원", "214.6원/kWh"]),
            ],
        )
        rendered = render_planned_visual(table)
        assert rendered is not None

        image = Image.open(
            io.BytesIO(base64.b64decode(rendered.data_url.split(",", 1)[1]))
        )
        assert image.width == 960

    def test_marker_replacement_fills_known_and_strips_unknown(self):
        content = "앞 문단\n\n[[VISUAL: visual-1]]\n\n뒤 문단 [[VISUAL: visual-9]] 끝"
        replaced = replace_visual_markers(content, {"visual-1": "<figure>차트</figure>"})
        assert "<figure>차트</figure>" in replaced
        assert "[[VISUAL" not in replaced

    def test_visual_html_includes_caption_paragraph(self):
        image = render_planned_visual(BAR_VISUAL)
        html = visual_html(image)
        assert "<figure" in html
        # 캡션은 figcaption이 아니라 전 경로에서 살아남는 별도 문단이다.
        assert '<p class="visual-caption"><em>' in html
        assert "figcaption" not in html

    def test_a_rendered_visual_is_marked_as_a_visual_not_a_photo(self):
        """도표와 사진은 화면에서 다르게 보여야 한다 — 도표는 테두리가 이미 그림 안에 있다.

        예전에는 미리보기 CSS가 모든 이미지에 노란 오프셋 그림자를 붙여, 생성된 블로그
        콘텐츠가 Blog-it 서비스 UI 부품처럼 보였다.
        """
        from app.modules.draft.images import media_kind_of

        image = render_planned_visual(BAR_VISUAL)
        assert image.media_kind == "visual"
        assert media_kind_of(image) == "visual"
        html = visual_html(image)
        assert 'class="blog-media blog-media--visual"' in html
        assert 'data-media-kind="visual"' in html

    def test_self_composed_visuals_carry_no_service_watermark(self):
        """자체 구성 자료(출처 없음)에는 하단 캡션에 서비스명·제작 도구 문구가 붙지 않는다.

        표·과정도·인포그래픽 모두 캡션이 비어야 하고, 자체 구성 자료를 별도 문단 없이
        본문에 그대로 얹히게 한다(스펙 2·9)."""
        table = PlannedVisual(
            visual_id="v",
            type="TABLE",
            title="요금제 비교",
            columns=["월 요금", "장점"],
            rows=[
                VisualTableRow(name="A", cells=["1만 원", "저렴"]),
                VisualTableRow(name="B", cells=["2만 원", "빠름"]),
            ],
        )
        diagram = PlannedVisual(
            visual_id="v",
            type="PROCESS_DIAGRAM",
            title="절차",
            steps=["기획", "작성", "발행"],
        )
        for visual in (table, diagram):
            rendered = render_planned_visual(visual)
            assert rendered is not None
            assert rendered.caption == ""
            assert "Blog-it" not in visual_html(rendered)

    def test_external_source_still_shows_source_caption(self):
        """외부 출처가 있는 그래프는 출처·기준시점을 캡션에 그대로 밝힌다."""
        chart = BAR_VISUAL.model_copy(update={"caption": None})
        rendered = render_planned_visual(chart)
        assert rendered is not None
        assert "source-1" in rendered.caption  # 출처 id는 그대로
        assert "Blog-it" not in rendered.caption

    def test_pie_chart_with_more_than_five_slices_is_refused(self):
        """항목이 5개를 넘는 파이차트는 만들지 않는다(스펙 7)."""
        six = BAR_VISUAL.model_copy(
            update={
                "type": "PIE_CHART",
                "data": [
                    VisualDataPoint(label=f"항목{n}", value=float(n + 1)) for n in range(6)
                ],
            }
        )
        assert render_planned_visual(six) is None
        five = six.model_copy(update={"data": six.data[:5]})
        assert render_planned_visual(five) is not None

    def test_long_table_cells_split_columns_instead_of_being_lost(self):
        """두 줄을 넘기는 4열 표는 2+2 블록으로 나뉘고 핵심 문구를 보존한다."""
        short = PlannedVisual(
            visual_id="v",
            type="TABLE",
            title="비교",
            columns=["설명", "비고", "비용", "기간"],
            rows=[
                VisualTableRow(name="A", cells=["짧음", "짧음", "무료", "1일"]),
                VisualTableRow(name="B", cells=["짧음", "짧음", "유료", "2일"]),
            ],
        )
        long = short.model_copy(
            update={
                "rows": [
                    VisualTableRow(
                        name="A",
                        cells=["가나다라마바 사아자차카타 파하거너더러", "짧음", "무료", "1일"],
                    ),
                    VisualTableRow(name="B", cells=["짧음", "짧음", "유료", "2일"]),
                ]
            }
        )

        def rendered_height(visual: PlannedVisual) -> int:
            import base64
            import io

            from PIL import Image

            image = render_planned_visual(visual)
            return Image.open(
                io.BytesIO(base64.b64decode(image.data_url.split(",", 1)[1]))
            ).height

        assert _table_column_groups(short.columns, short.rows) == [[0, 1, 2, 3]]
        assert _table_column_groups(long.columns, long.rows) == [[0, 1], [2, 3]]
        assert rendered_height(long) > rendered_height(short)

        text = "가나다라마바 사아자차카타 파하거너더러"
        lines, _ = _fit_table_text(text, 300, (17, 16, 15, 14))
        assert len(lines) <= 2
        assert all("…" not in line for line in lines)
        assert "".join(lines).replace(" ", "") == text.replace(" ", "")

    def test_table_rejects_overlong_or_misaligned_cells(self):
        base = PlannedVisual(
            visual_id="v",
            type="TABLE",
            title="비교",
            columns=["요금", "지원"],
            rows=[
                VisualTableRow(name="A", cells=["1만 원", "가능"]),
                VisualTableRow(name="B", cells=["2만 원", "불가"]),
            ],
        )
        assert render_planned_visual(base) is not None
        assert render_planned_visual(
            base.model_copy(update={"columns": ["요금", " "]})
        ) is None
        assert render_planned_visual(
            base.model_copy(
                update={
                    "rows": [
                        VisualTableRow(name=" ", cells=["1만 원", "가능"]),
                        base.rows[1],
                    ]
                }
            )
        ) is None
        assert render_planned_visual(
            base.model_copy(
                update={
                    "rows": [
                        VisualTableRow(name="가" * 21, cells=["1만 원", "가능"]),
                        base.rows[1],
                    ]
                }
            )
        ) is None
        assert render_planned_visual(
            base.model_copy(
                update={
                    "rows": [
                        VisualTableRow(name="A", cells=["1만 원"]),
                        base.rows[1],
                    ]
                }
            )
        ) is None
        assert render_planned_visual(
            base.model_copy(
                update={
                    "rows": [
                        VisualTableRow(name="A", cells=["이 셀은 스무 자를 명백하게 넘기는 긴 설명입니다", "가능"]),
                        base.rows[1],
                    ]
                }
            )
        ) is None

    def test_table_alignment_follows_value_semantics(self):
        assert _table_cell_alignment("12,500원") == "right"
        assert _table_cell_alignment("1만 원") == "right"
        assert _table_cell_alignment("120.0원/kWh") == "right"
        assert _table_cell_alignment("1~2일") == "right"
        assert _table_cell_alignment("약 3시간") == "right"
        assert _table_cell_alignment("포함") == "center"
        assert _table_cell_alignment("미포함") == "center"
        assert _table_cell_alignment("지원") == "center"
        assert _table_cell_alignment("초보자에게 적합") == "left"

    def test_table_schema_rejects_blank_headers_names_and_cells(self):
        table_properties = DRAFT_SCHEMA["properties"]["visuals"]["items"]["properties"]
        column_items = table_properties["columns"]["items"]
        assert column_items["minLength"] == 1
        assert column_items["pattern"] == r"\S"
        row_properties = table_properties["rows"]["items"]["properties"]
        assert row_properties["name"]["minLength"] == 1
        assert row_properties["name"]["maxLength"] == 20
        assert row_properties["name"]["pattern"] == r"\S"
        assert row_properties["cells"]["items"]["minLength"] == 1
        assert row_properties["cells"]["items"]["pattern"] == r"\S"

    def test_style_preset_is_honored_without_breaking_default(self):
        """style 프리셋이 있으면 그 팔레트로, 없으면 기본값으로 렌더링된다(둘 다 성공)."""
        base = PlannedVisual(
            visual_id="v",
            type="TABLE",
            title="비교",
            columns=["월 요금", "장점"],
            rows=[
                VisualTableRow(name="A", cells=["1만 원", "저렴"]),
                VisualTableRow(name="B", cells=["2만 원", "빠름"]),
            ],
        )
        assert render_planned_visual(base) is not None
        assert render_planned_visual(base.model_copy(update={"style": "LIFESTYLE_SOFT"})) is not None
        # 알 수 없는 값이 와도 기본값으로 안전하게 그려진다.
        assert render_planned_visual(base.model_copy(update={"style": "NOPE"})) is not None


class TestContentPlanParsing:
    def test_parses_a_plan_and_renumbers_sections(self):
        plan = content_plan_from_json(
            {
                "contentPlan": {
                    "targetReader": "대학생",
                    "readerProblem": "구독 부담",
                    "readerQuestion": "한곳에서?",
                    "articlePromise": "활용법",
                    "contentAngle": "통합",
                    "articleType": "PROMOTION",
                    "tone": "정보형",
                    "sections": [
                        {
                            "sectionId": "weird-7",
                            "heading": "문제는 너무 많은 AI",
                            "question": "왜 피곤한가",
                            "purpose": "문제 제기",
                            "keyPoints": ["구독 분산"],
                            "evidenceIds": ["source-1"],
                            "visualType": "bar_chart",
                            "visualReason": "이용 증가 수치",
                        },
                        {
                            "sectionId": "x",
                            "heading": "무엇을 묶어주는가",
                            "question": "해결 방식",
                            "purpose": "해결책 설명",
                            "keyPoints": [],
                            "evidenceIds": [],
                            "visualType": "INFOGRAPHIC",
                            "visualReason": "구조",
                        },
                        {
                            "sectionId": "y",
                            "heading": "누구에게 유용한가",
                            "question": "대상",
                            "purpose": "활용 사례",
                            "keyPoints": [],
                            "evidenceIds": [],
                            "visualType": "NONE",
                            "visualReason": "",
                        },
                    ],
                }
            }
        )
        assert plan is not None
        assert [s.section_id for s in plan.sections] == ["section-1", "section-2", "section-3"]
        assert plan.sections[0].visual_type == "BAR_CHART"
        assert plan.article_type == "PROMOTION"

    def test_fewer_than_three_sections_is_no_plan(self):
        assert (
            content_plan_from_json(
                {
                    "contentPlan": {
                        "targetReader": "a",
                        "readerProblem": "b",
                        "readerQuestion": "c",
                        "articlePromise": "d",
                        "contentAngle": "e",
                        "articleType": "INFORMATION",
                        "sections": [
                            {"heading": "하나", "question": "q", "purpose": "결론"}
                        ],
                    }
                }
            )
            is None
        )

    def test_parses_visuals_with_data(self):
        visuals = planned_visuals_from_json(
            [
                {
                    "visualId": "visual-1",
                    "type": "BAR_CHART",
                    "title": "비교",
                    "caption": "출처, 2026",
                    "altText": "비교 그래프",
                    "data": [
                        {"label": "A", "value": 1},
                        {"label": "B", "value": "숫자아님"},
                    ],
                    "source": "기관",
                }
            ]
        )
        assert len(visuals) == 1
        assert [p.label for p in visuals[0].data] == ["A"]

    def _table(self, **overrides):
        return {
            "visualId": "visual-1",
            "type": "TABLE",
            "title": "비교",
            "columns": ["a", "b"],
            "rows": [{"name": "n", "cells": ["1", "2"]}],
            **overrides,
        }

    def test_parses_theme_and_normalizes_case(self):
        visuals = planned_visuals_from_json([self._table(style="beauty_editorial")])
        assert visuals[0].style == "BEAUTY_EDITORIAL"

    def test_a_legacy_preset_name_maps_onto_its_new_theme(self):
        """옛 저장 문서의 프리셋 이름(TECH_MINIMAL 등)도 계속 읽혀야 한다 — 형식이 바뀌었다고
        예전 글의 도표가 기본 팔레트로 되돌아가면 안 된다."""
        for legacy, expected in (
            ("tech_minimal", "TECH_BENCHMARK_LIGHT"),
            ("LIFESTYLE_SOFT", "LIFESTYLE_JOURNAL"),
            ("PROFESSIONAL_DATA", "FINANCE_REPORT"),
            ("EDITORIAL_NEUTRAL", "EDITORIAL_NEUTRAL"),
        ):
            visuals = planned_visuals_from_json([self._table(style=legacy)])
            assert visuals[0].style == expected

    def test_an_unknown_theme_falls_back_to_none_not_a_bogus_value(self):
        assert planned_visuals_from_json([self._table(style="NOPE")])[0].style is None

    def test_a_layout_variant_from_another_type_is_dropped(self):
        """표에 과정도 변형이 붙으면 렌더러가 알아보지 못하고 조용히 기본값으로 그린다.
        어긋난 조합은 파싱에서 버려 '계획대로 안 나온' 상태를 만들지 않는다."""
        kept = planned_visuals_from_json([self._table(layoutVariant="WINNER_HIGHLIGHT")])
        assert kept[0].layout_variant == "WINNER_HIGHLIGHT"
        dropped = planned_visuals_from_json([self._table(layoutVariant="VERTICAL_TIMELINE")])
        assert dropped[0].layout_variant is None

    def test_parses_the_necessity_rubric_fields(self):
        visuals = planned_visuals_from_json(
            [
                self._table(
                    visualReason="세 요금제의 월 비용 차이를 나란히 비교해야 선택이 가능하다",
                    necessityScore=91,
                    highlightLabels=["n"],
                )
            ]
        )
        assert visuals[0].necessity_score == 91
        assert visuals[0].highlight_labels == ["n"]
        assert "월 비용" in visuals[0].visual_reason

    @pytest.mark.parametrize(
        "columns, rows",
        [
            (
                ["a", "b", "c", "d", "e"],
                [
                    {"name": "A", "cells": ["1", "2", "3", "4", "5"]},
                    {"name": "B", "cells": ["1", "2", "3", "4", "5"]},
                ],
            ),
            (
                ["a", "b"],
                [
                    {"name": name, "cells": ["1", "2"]}
                    for name in ("A", "B", "C", "D", "E", "F")
                ],
            ),
            (
                ["a", "b", "c", "d"],
                [
                    {"name": "A", "cells": ["1", "2", "3", "4", "5"]},
                    {"name": "B", "cells": ["1", "2", "3", "4"]},
                ],
            ),
        ],
    )
    def test_oversized_table_is_dropped_instead_of_silently_truncated(
        self, columns, rows
    ):
        visuals = planned_visuals_from_json(
            [
                {
                    "visualId": "visual-1",
                    "type": "TABLE",
                    "title": "초과 표",
                    "columns": columns,
                    "rows": rows,
                }
            ]
        )

        assert visuals == []

    def test_table_parser_preserves_the_supported_maximum_shape(self):
        columns = ["a", "b", "c", "d"]
        rows = [
            {"name": name, "cells": ["1", "2", "3", "4"]}
            for name in ("A", "B", "C", "D", "E")
        ]

        visuals = planned_visuals_from_json(
            [
                {
                    "visualId": "visual-1",
                    "type": "TABLE",
                    "title": "최대 규격 표",
                    "columns": columns,
                    "rows": rows,
                }
            ]
        )

        assert len(visuals) == 1
        assert visuals[0].columns == columns
        assert [row.cells for row in visuals[0].rows] == [
            row["cells"] for row in rows
        ]


class TestPlannedPhotoCount:
    """본문 사진 수는 원고 완성 후의 카드 계획(VisualCardPlan)이 정한다. 글 길이·콘텐츠
    설계의 PHOTO 섹션 수로 정하던 규칙은 폐기됐다."""

    def test_without_a_card_plan_does_not_invent_body_photos(self):
        assert planned_photo_count(None) == 0

    def test_the_card_plan_drives_the_count(self):
        plan = VisualCardPlan(
            cards=[
                _card("card-1", "THUMBNAIL", None, score=90),
                _card("card-2", "SECTION_CARD", "section-1", score=88),
                _card("card-3", "SECTION_CARD", "section-2", score=80),
                _card("card-4", "SECTION_CARD", "section-3", score=76),
            ]
        )
        assert planned_photo_count(plan) == 3


class TestPromptRules:
    def test_plan_prompt_keeps_photos_separate_and_caps_rendered_visuals(self):
        prompt = content_plan_prompt(build_draft_input())
        assert "사용자가 고른 검색 의도" in prompt
        assert "AIONA 대학생 활용" in prompt
        assert "PHOTO는 이 단계에서 고르지 않는다" in prompt
        # 상한만 말하면 모델은 늘 상한을 채운다. 이제 기본값이 NONE이라고 먼저 말한다.
        assert "**기본값은 NONE이다**" in prompt
        assert "시각자료가 하나도 없는" in prompt
        assert "상한이지 목표가 아니다" in prompt
        assert "additionalProperties" not in prompt  # tool schema를 텍스트로 중복하지 않음

    def test_plan_prompt_carries_the_purpose_visual_policy(self):
        """목적이 허용하는 유형만 제시한다 — 홍보 글에 그래프 선택지를 보여 주지 않는다."""
        prompt = content_plan_prompt(build_draft_input())
        assert "이 글의 목적별 정책" in prompt
        assert "검증된 지표가 없는 성장률" in prompt
        assert "85점 미만은 NONE으로 둔다" in prompt
        assert "구체적이지 않은 visualReason" in prompt

    def test_plan_prompt_forbids_charts_without_data_points(self):
        """테스트 B: 출처에 실측 수치가 없으면 설계 단계에서 차트를 계획하지 못하게 한다."""
        prompt = content_plan_prompt(build_draft_input())
        assert "실측수치(dataPoints)가 하나도 없으므로" in prompt
        assert "수치를 만들어 그래프를 그리는 것은 금지" in prompt

    def test_plan_prompt_allows_charts_only_from_data_points_when_present(self):
        with_data = build_draft_input(
            selected_intent=SelectedIntentForDraft(
                intent_id="i1",
                title="T",
                target_reader="대학생",
                rationale="R",
                sources=[
                    SearchSource(
                        title="KB 보고서",
                        url="https://example.com",
                        snippet="이용 증가",
                        source_type="REPORT",
                        data_points=[
                            SourceDataPoint(label="2026년 이용률", value=67.0, unit="%")
                        ],
                    )
                ],
            )
        )
        prompt = content_plan_prompt(with_data)
        assert "실측수치(dataPoints)가 있는 내용에만 계획한다" in prompt
        assert "실측수치: 2026년 이용률 = 67.0%" in prompt

    def test_plan_prompt_no_longer_forces_information_over_review(self):
        """2026-08-03 사용자 결정: 경험 자료가 없어도 REVIEW로 설계할 수 있다."""
        prompt = content_plan_prompt(build_draft_input())
        assert "REVIEW(체험 후기)로 정하지 않는다" not in prompt
        assert "가상의 체험을 계획하지 않는다" not in prompt
        assert "- articleType은 글 목적을 따른다." in prompt

    def test_plan_prompt_does_not_force_stats_on_travel_articles(self):
        """테스트 D: 여행·맛집·일상 글에 통계·구조도를 강제하지 않는다."""
        prompt = content_plan_prompt(build_draft_input())
        assert "여행·맛집·일상 소재는" in prompt
        assert "억지로 계획하지 않는다" in prompt

    def test_draft_prompt_embeds_the_approved_plan(self):
        draft_input = build_draft_input(content_plan=build_plan("PHOTO", "BAR_CHART", "NONE"))
        prompt = draft_prompt(draft_input)
        assert "콘텐츠 설계(이 설계를 따라 쓴다)" in prompt
        assert "[section-2]" in prompt
        assert "[[VISUAL: visual-1]]" in prompt  # 렌더링 시각자료 마커 규칙
        assert "2~4열" in prompt  # 표 규칙
        # 사람이 쓴 글처럼 읽히게 하는 규칙이 원고 프롬프트에 실린다.
        assert "모든 문단의 길이를 비슷하게 만들지 않는다" in prompt
        assert "같은 종결 어미를 세 문장 이상" in prompt
        assert "메타 문장을 쓰지 않는다" in prompt

    def test_draft_prompt_forbids_image_tags_when_no_photos_planned(self):
        draft_input = build_draft_input(content_plan=build_plan("TABLE", "NONE", "NONE"))
        prompt = draft_prompt(draft_input)
        assert "태그를 넣지 않는다" in prompt
        # 사진은 없지만 비교표는 코드로 그린다 — TABLE도 렌더링 시각자료다.
        assert "TABLE은 columns" in prompt

    def test_planned_table_is_not_duplicated_as_a_markdown_table(self):
        prompt = draft_prompt(
            build_draft_input(content_plan=build_plan("TABLE", "NONE", "NONE"))
        )
        assert "구조화 TABLE 한 벌로만" in prompt
        assert "2~4열의 짧은 마크다운 표" not in prompt

    def test_draft_prompt_does_not_duplicate_schema_materials_or_output_format(self):
        draft_input = build_draft_input(
            input=BlogTaskInput(
                topic="AIONA",
                purpose=["정보 전달"],
                keywords=["AI"],
                reference_materials=[
                    ReferenceMaterial(
                        type=ReferenceMaterialType.TEXT,
                        value="프롬프트 중복 확인용 고유 메모",
                    )
                ],
            )
        )
        prompt = draft_prompt(draft_input)
        assert prompt.count("프롬프트 중복 확인용 고유 메모") == 1
        assert "additionalProperties" not in prompt
        assert "출력 형식:" not in prompt

    def test_draft_prompt_empties_the_visuals_array_when_nothing_is_rendered(self):
        draft_input = build_draft_input(content_plan=build_plan("PHOTO", "NONE", "NONE"))
        prompt = draft_prompt(draft_input)
        assert "visuals 배열은 비워 둔다" in prompt

    def test_draft_prompt_never_asks_for_image_tags(self):
        """본문 사진은 원고 완성 후의 카드 계획이 정한다 — 원고 프롬프트는 설계 유무와
        무관하게 [[IMAGE:]] 태그를 금지한다."""
        assert "`[[IMAGE: ...]]` 태그를 넣지 않는다" in draft_prompt(build_draft_input())
        with_plan = build_draft_input(content_plan=build_plan("PHOTO", "NONE", "NONE"))
        assert "`[[IMAGE: ...]]` 태그를 넣지 않는다" in draft_prompt(with_plan)


class TestQualityChecks:
    def _post(self, body: str, html: str | None = None) -> FinalPost:
        return FinalPost(
            title="제목",
            body=body,
            hashtags=["a"] * 5,
            html_content=html or f"<article><p>{body}</p></article>",
            markdown_content=f"# 제목\n\n{body}",
        )

    LONG_BODY = "\n\n".join(
        f"{n}번 문단입니다. 항목{n} 준비과정{n} 사례{n} 판단기준{n} 연결설명{n}을 다룹니다. "
        f"독자{n}는 단계{n} 확인사항{n} 선택지{n} 이해흐름{n} 실행메모{n}를 얻습니다."
        for n in range(1, 31)
    )

    def test_first_person_experience_is_no_longer_rejected(self):
        """2026-08-03 사용자 결정: 경험 자료가 없어도 1인칭 체험 서술을 반려하지 않는다.

        AI로 자동 생성하는 목적 자체가 직접 겪은 것처럼 읽히는 글이라는 판단이다.
        """
        body = f"제가 직접 사용해보니 좋았습니다. {self.LONG_BODY}"
        report = check_draft(self._post(body), 5, has_experience_material=False)
        assert report.ok
        assert not any("경험" in problem for problem in report.problems)

    def test_experience_claims_pass_when_the_user_provided_material(self):
        body = f"제가 직접 사용해보니 좋았습니다. {self.LONG_BODY}"
        report = check_draft(self._post(body), 5, has_experience_material=True)
        assert report.ok

    def test_hype_without_evidence_is_warned(self):
        body = f"이것은 완벽한 도구이고 무조건 써야 합니다. {self.LONG_BODY}"
        report = check_draft(self._post(body), 5)
        assert any("과장 표현" in warning for warning in report.warnings)

    def test_emphasis_overuse_is_warned(self):
        # 상한(MAX_EMPHASIS_RATE)은 2026-08-03에 15%→20%로 올랐다. 프롬프트가 소제목
        # 구간마다 강조 한 곳을 요구하게 되면서, 짧은 글은 하한만 지켜도 15%를 넘었다.
        # 상한을 숫자로 박지 않고 상수에서 끌어와, 다음에 값이 바뀌어도 뜻이 유지된다.
        from app.modules.draft.quality import MAX_EMPHASIS_RATE, SENTENCE_SPLIT

        body_sentences = len(SENTENCE_SPLIT.split(self.LONG_BODY))
        over_limit = int(body_sentences * MAX_EMPHASIS_RATE) + 5
        sentences = " ".join(
            f"<strong>강조 문장 {n}번입니다 아주 중요합니다</strong>." for n in range(over_limit)
        )
        html = f"<article><p>{sentences}</p><p>{self.LONG_BODY}</p></article>"
        report = check_draft(self._post(self.LONG_BODY, html=html), 5)
        assert any("강조 과다" in warning for warning in report.warnings)

    def test_emphasis_scarcity_is_warned(self):
        """2026-08-03 추가: 지금까지는 '과다'만 봤다. 실측에서 5섹션 글에 굵게가 1곳뿐이라
        눈이 쉴 곳이 없었는데 검사는 아무 말도 하지 않았다."""
        html = (
            "<article>"
            + "".join(f"<h2>소제목 {n}</h2><p>{self.LONG_BODY}</p>" for n in range(4))
            + "</article>"
        )
        report = check_draft(self._post(self.LONG_BODY, html=html), 5)
        assert any("강조 부족" in warning for warning in report.warnings)


class StubPlanningGenerator:
    """설계와 원고를 둘 다 아는 스텁 — 서비스 파이프라인 통합 검증용."""

    def __init__(self, plan, result):
        self.plan = plan
        self.result = result
        self.captured: list = []

    async def generate_content_plan(self, draft_input):
        return self.plan

    async def generate_draft(self, draft_input):
        self.captured.append(draft_input)
        return self.result


def _service(generator) -> DraftService:
    return DraftService(
        repository=InMemoryBlogTaskRepository(),
        draft_generator=generator,
    )


LONG_BODY = "\n\n".join(
    f"{n}번 문단입니다. 항목{n} 준비과정{n} 사례{n} 판단기준{n} 연결설명{n}을 다룹니다. "
    f"독자{n}는 단계{n} 확인사항{n} 선택지{n} 이해흐름{n} 실행메모{n}를 얻습니다."
    for n in range(1, 40)
)


def _result_with_marker(visuals) -> DraftGenerationResult:
    return DraftGenerationResult(
        prompt_version="m4-draft@v1.1",
        provider="stub",
        model="stub",
        generated_at=NOW,
        final_post=FinalPost(
            title="T",
            body=f"{LONG_BODY}\n\n[[VISUAL: visual-1]]\n\n마무리 해석 문장입니다.",
            hashtags=["a"] * 5,
            html_content=(
                f"<article><h1>T</h1><p>{LONG_BODY}</p>"
                "<p>[[VISUAL: visual-1]]</p><p>마무리 해석 문장입니다.</p></article>"
            ),
            markdown_content=f"# T\n\n{LONG_BODY}\n\n[[VISUAL: visual-1]]\n\n마무리 해석 문장입니다.",
        ),
        visuals=visuals,
    )


class TestRenderedVisualPipeline:
    def test_valid_chart_is_rendered_into_the_marker_slot(self):
        """테스트 A: 실측 수치·출처가 있는 그래프가 본문 마커 자리에 캡션과 함께 들어간다."""
        service = _service(StubPlanningGenerator(None, None))
        result = _result_with_marker([BAR_VISUAL])

        updated = service._with_rendered_visuals(result, build_chart_draft_input())
        post = updated.final_post

        assert "data:image/png;base64," in post.html_content
        assert "[[VISUAL" not in post.html_content
        assert "[[VISUAL" not in post.body
        assert "KB국민카드" in post.html_content  # 캡션(출처·기준시점)
        assert post.images and post.images[0].source == "rendered"

    def test_invalid_chart_strips_the_marker_instead_of_faking_a_graph(self):
        """테스트 B: 검증 탈락(출처 없음) 그래프는 그려지지 않고 마커만 걷힌다."""
        service = _service(StubPlanningGenerator(None, None))
        bogus = BAR_VISUAL.model_copy(update={"source": None})
        result = _result_with_marker([bogus])

        updated = service._with_rendered_visuals(result, build_draft_input())
        post = updated.final_post

        assert "data:image/png" not in post.html_content
        assert "[[VISUAL" not in post.html_content
        assert not post.images

    def test_rendered_visuals_are_not_capped_by_the_photo_quota(self):
        """표·그래프는 사진 장수 규격과 별개다(2026-08-03 사용자 결정).

        예전에는 폴백 경로만 사진 규격으로 도표를 잘라, 같은 글이 카드 경로냐
        폴백이냐에 따라 다른 밀도로 나갔다. 남는 상한은 폭주 방지선뿐이다.
        """
        service = _service(StubPlanningGenerator(None, None))

        def existing(count: int) -> list:
            from app.shared import GeneratedPostImage

            return [
                GeneratedPostImage(
                    data_url=f"data:image/jpeg;base64,x{n}",
                    alt_text="x",
                    prompt="p",
                    provider="openai",
                    model="m",
                    generated_at=NOW,
                    mime_type="image/jpeg",
                )
                for n in range(count)
            ]

        short_input = build_draft_input(
            settings=DraftGenerationSettings(hashtag_count=5, article_length="short")
        )

        # 짧은 글이고 사진 규격(2~3장)이 이미 찼어도 근거 있는 도표는 그린다.
        short_full = _result_with_marker([BAR_VISUAL])
        short_full = short_full.model_copy(
            update={
                "final_post": short_full.final_post.model_copy(
                    update={"images": existing(3), "featured_image": existing(3)[0]}
                )
            }
        )
        updated = service._with_rendered_visuals(short_full, short_input)
        assert "data:image/png" in updated.final_post.html_content
        assert "[[VISUAL" not in updated.final_post.html_content

    def test_the_runaway_guard_still_caps_rendered_visuals(self):
        """상한이 사라진 것은 아니다 — 폭주 방지선(MAX_TOTAL_IMAGES)은 남는다."""
        from app.modules.draft.card_selection import MAX_TOTAL_IMAGES
        from app.shared import GeneratedPostImage

        service = _service(StubPlanningGenerator(None, None))
        packed = [
            GeneratedPostImage(
                data_url=f"data:image/jpeg;base64,x{n}",
                alt_text="x",
                prompt="p",
                provider="openai",
                model="m",
                generated_at=NOW,
                mime_type="image/jpeg",
            )
            for n in range(MAX_TOTAL_IMAGES)
        ]
        result = _result_with_marker([BAR_VISUAL])
        result = result.model_copy(
            update={
                "final_post": result.final_post.model_copy(
                    update={"images": packed, "featured_image": packed[0]}
                )
            }
        )
        updated = service._with_rendered_visuals(
            result,
            build_draft_input(
                settings=DraftGenerationSettings(hashtag_count=5, article_length="medium")
            ),
        )
        assert "data:image/png" not in updated.final_post.html_content
        assert len(updated.final_post.images) == MAX_TOTAL_IMAGES


class TestPlanDrivenPhotoCount:
    async def test_fallback_only_honors_explicit_legacy_image_tags(self):
        """사진 계획이 없으면 썸네일만 만들되, 구형 [[IMAGE:]] 태그는 계속 존중한다."""
        from test_draft_service import StubImageGenerator, build_task

        plan = build_plan("PHOTO", "NONE", "NONE")
        tagged = DraftGenerationResult(
            prompt_version="m4-draft@v1.1",
            provider="stub",
            model="stub",
            generated_at=NOW,
            final_post=FinalPost(
                title="T",
                body=f"{LONG_BODY} [[IMAGE: campus scene]]",
                hashtags=["a"] * 5,
                html_content=f"<article><p>{LONG_BODY}</p><p>[[IMAGE: campus scene]]</p></article>",
                markdown_content=f"# T\n\n{LONG_BODY}\n\n[[IMAGE: campus scene]]",
            ),
        )
        generator = StubPlanningGenerator(plan, tagged)
        images = StubImageGenerator()
        repository = InMemoryBlogTaskRepository()
        service = DraftService(
            repository=repository,
                draft_generator=generator,
            post_image_generator=images,
        )
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        # 썸네일 1 + 구형 태그가 명시한 본문 사진 1. 장식 사진은 패딩하지 않는다.
        assert len(images.calls) == 2
        assert generator.captured[0].content_plan is not None
        # 썸네일은 글마다 정확히 한 장이다. 나머지는 전부 본문 사진이다.
        assert [call.is_thumbnail for call in images.calls].count(True) == 1
        assert images.calls[0].is_thumbnail
        assert images.calls[1].content_prompt == "campus scene"
        assert {call.total_images for call in images.calls} == {2}

    async def test_the_quality_check_counts_against_the_plan_not_a_fixed_two(self):
        """설계가 사진 0장을 계획한 글은 프롬프트가 태그를 금지한다. 그런 글에 '2개여야
        하는데 0개'라는 경고가 남으면, 지시와 검수가 서로 다른 기준을 보고 있는 것이다."""
        post = FinalPost(
            title="T",
            body=LONG_BODY,
            hashtags=["a"] * 5,
            html_content=f"<article><p>{LONG_BODY}</p></article>",
            markdown_content=f"# T\n\n{LONG_BODY}",
        )

        planned_none = check_draft(post, 5, photo_count=0)
        assert not [w for w in planned_none.warnings if "이미지 태그" in w]

        # 4장을 계획했는데 원고에 태그가 없으면 그건 알려야 한다.
        planned_four = check_draft(post, 5, photo_count=4)
        assert [w for w in planned_four.warnings if "이미지 태그" in w]
