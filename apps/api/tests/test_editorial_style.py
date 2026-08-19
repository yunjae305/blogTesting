"""스펙 §14의 열 가지 시나리오를 코드 수준에서 고정한다.

한 줄로 요약하면 이 파일이 지키는 것은 하나다: **시각자료는 근거가 있을 때만 만들고,
디자인은 글마다 달라지되 같은 글 안에서는 흔들리지 않는다.**

1. 일상 공유 → 도표 0개
2. 뷰티 리뷰 + 제품 이미지 → 원본 재사용·부드러운 계열·그래프 0개
3. 뷰티 리뷰 + 경험 근거 없음 → 후기 아키타입 강등, 체험 문구 반려
4. 나이키 참고 이미지 → 대상 고정·브랜드 표식 보존·가짜 경험 금지
5. 테크 비교 + 벤치마크 → 표 1·막대 1, 테크 테마, 출처 수치와 일치
6. 트렌드 + 수치 없음 → 그래프 0개, 장식 인포그래픽 0개
7. 운동 기록 → 숫자 자료가 있을 때만 그래프
8. 썸네일 피사체 보호 → 문구가 피사체를 덮지 않음, 문구 없음 허용, 구형 폴백
9. 미리보기 CSS → 콘텐츠 이미지에 Post-it 노란 그림자 없음
10. 스타일 다양성 → 글마다 다름, 다시 불러오면 같음, 다시 생성하면 다름
"""

from pathlib import Path

import pytest

from app.llm.imaging import (
    CANVAS_HEIGHT,
    SAFE_AREA_LEFT,
    SAFE_AREA_RIGHT,
    render_thumbnail,
    resolve_thumbnail_layout,
    zone_box,
    zones_overlap,
)
from app.llm.prompts import (
    card_plan_prompt,
    content_plan_prompt,
    draft_prompt,
    editorial_style_prompt,
    image_prompt,
    reference_evidence_prompt,
)
from app.modules.draft.editorial_style import (
    colour_direction_for,
    normalize_style_plan,
    thumbnail_layout_plan_for,
)
from app.modules.draft.quality import check_draft
from app.modules.draft.reference_evidence import build_profile, enrich
from app.modules.draft.visual_policy import gate_visuals, has_numeric_user_material, purpose_policy
from app.modules.draft.visuals import _style_for, render_planned_visual
from app.shared import (
    BlogTaskInput,
    CardBrief,
    CardScene,
    DraftFormat,
    DraftGenerationInput,
    DraftGenerationSettings,
    EditorialStylePlan,
    FinalPost,
    PlannedVisual,
    ReferenceEvidenceProfile,
    ReferenceImageEvidence,
    ReferenceMaterial,
    ReferenceMaterialType,
    SearchSource,
    SelectedIntentForDraft,
    SourceDataPoint,
    ThumbnailLayoutPlan,
    VisualBudget,
    VisualDataPoint,
    VisualGroup,
    VisualTableRow,
)


def build_input(**overrides) -> DraftGenerationInput:
    defaults = dict(
        post_id="post_1",
        user_id="user_1",
        prompt_version="m4-draft@v2.0",
        format=DraftFormat.MARKDOWN,
        input=BlogTaskInput(
            topic="소재",
            purpose=["정보 전달"],
            keywords=["키워드"],
            target_reader="독자",
            reference_materials=[],
        ),
        selected_intent=SelectedIntentForDraft(
            intent_id="i1",
            title="의도",
            target_reader="독자",
            rationale="근거",
            sources=[],
        ),
        settings=DraftGenerationSettings(hashtag_count=5, article_length="medium"),
    )
    return DraftGenerationInput(**{**defaults, **overrides})


def style_for(topic: str, purposes: list[str], *, evidence=None, revision=0, post_id="post_1"):
    return normalize_style_plan(
        None,
        post_id=post_id,
        revision=revision,
        topic=topic,
        subject=None,
        purposes=purposes,
        article_length="medium",
        evidence=evidence,
    )


def visual(visual_type: str, **overrides) -> PlannedVisual:
    base = dict(visual_id="visual-1", type=visual_type, title="제목")
    if visual_type == "TABLE":
        base |= {
            "columns": ["기준A", "기준B"],
            "rows": [
                VisualTableRow(name="대상1", cells=["값1", "값2"]),
                VisualTableRow(name="대상2", cells=["값3", "값4"]),
            ],
        }
    elif visual_type == "INFOGRAPHIC":
        base |= {
            "center_topic": "중심",
            "groups": [
                VisualGroup(name="갈래1", items=["항목1", "항목2"]),
                VisualGroup(name="갈래2", items=["항목3", "항목4"]),
            ],
        }
    elif visual_type in ("BAR_CHART", "LINE_CHART", "PIE_CHART"):
        base |= {
            "data": [
                VisualDataPoint(label="2025년", value=42.0),
                VisualDataPoint(label="2026년", value=67.0),
                VisualDataPoint(label="2027년", value=51.0),
            ],
            "source": "출처 기관",
        }
    return PlannedVisual(**{**base, **overrides})


def gate(visuals, purposes, *, plan=None, body="", user_numeric=False):
    plan = plan or style_for("소재", purposes)
    return gate_visuals(
        visuals,
        policy=purpose_policy(purposes),
        budget=plan.visual_budget,
        body=body,
        has_user_numeric_data=user_numeric,
    )


LONG_BODY = "\n\n".join(
    f"{n}번 문단입니다. 항목{n} 준비과정{n} 사례{n} 판단기준{n} 연결설명{n}을 다룹니다. "
    f"독자{n}는 단계{n} 확인사항{n} 선택지{n} 이해흐름{n} 실행메모{n}를 얻습니다."
    for n in range(1, 31)
)


def post(body: str = LONG_BODY, title: str = "제목") -> FinalPost:
    return FinalPost(
        title=title,
        body=body,
        hashtags=["a"] * 5,
        html_content=f"<article><p>{body}</p></article>",
        markdown_content=f"# {title}\n\n{body}",
    )


class TestIntroductionPurpose:
    PURPOSES = ["입문·소개"]

    def test_it_uses_an_explainer_with_one_evidence_based_overview_visual(self):
        plan = style_for("새 제품 첫 소개", self.PURPOSES)
        policy = purpose_policy(self.PURPOSES)

        assert plan.editorial_archetype == "EXPERT_EXPLAINER"
        assert policy.rendered_max == 1
        assert policy.allowed_visual_types == frozenset({"PROCESS_DIAGRAM", "TABLE"})
        assert "가짜 화면" in policy.note
        assert "검증된 수치가 없는 그래프" in policy.note


# --- 1. 일상 공유 -----------------------------------------------------------


class TestDailyLifeMakesNoCharts:
    PURPOSES = ["일상·경험 공유"]

    def test_the_budget_is_zero_rendered_visuals(self):
        plan = style_for("주말 산책 기록", self.PURPOSES)
        assert plan.content_category == "DAILY_LIFE"
        assert plan.visual_budget.rendered_visuals_max == 0
        assert plan.allowed_visual_types == []
        assert plan.visual_density == "NONE"

    @pytest.mark.parametrize(
        "visual_type", ["TABLE", "BAR_CHART", "INFOGRAPHIC", "PIE_CHART"]
    )
    def test_every_rendered_visual_is_dropped(self, visual_type):
        result = gate([visual(visual_type)], self.PURPOSES)
        assert result.kept == []
        assert result.rejections

    def test_the_prompt_says_the_default_is_none(self):
        draft_input = build_input(
            input=BlogTaskInput(
                topic="주말 산책 기록", purpose=self.PURPOSES, keywords=["산책"]
            )
        )
        draft_input = draft_input.model_copy(
            update={"editorial_style": style_for("주말 산책 기록", self.PURPOSES)}
        )
        prompt = content_plan_prompt(draft_input)
        assert "표·그래프·과정도·인포그래픽을 만들지 않는다" in prompt
        assert "없음(전부 NONE)" in prompt

    def test_a_no_copy_thumbnail_is_available(self):
        assert "NO_COPY_EDITORIAL_PHOTO" in _daily_layout_candidates()

    def test_numeric_records_unlock_exactly_one_chart(self):
        """실제 기록을 준 경우에만 열린다 — 그게 스펙이 말한 유일한 예외다."""
        records = [
            ReferenceMaterial(
                type=ReferenceMaterialType.TEXT,
                value="1월 3일 5.2km\n1월 5일 7.1km\n1월 9일 10.4km\n1월 12일 8.0km",
            )
        ]
        assert has_numeric_user_material(records) is True
        result = gate(
            [visual("LINE_CHART")], self.PURPOSES, user_numeric=True
        )
        assert [v.visual_id for v in result.kept] == ["visual-1"]

    def test_a_single_number_in_a_note_does_not_unlock_a_chart(self):
        casual = [
            ReferenceMaterial(type=ReferenceMaterialType.TEXT, value="3분 정도 걸렸어요")
        ]
        assert has_numeric_user_material(casual) is False


def _daily_layout_candidates() -> tuple[str, ...]:
    from app.modules.draft.editorial_style import CATEGORY_PROFILES

    return CATEGORY_PROFILES["DAILY_LIFE"].thumbnail_layouts


# --- 2·3. 뷰티 리뷰 ---------------------------------------------------------


BEAUTY_MATERIALS = [
    ReferenceMaterial(type=ReferenceMaterialType.IMAGE, name="lip-1.png", value="data:image/png;base64,AAA"),
    ReferenceMaterial(type=ReferenceMaterialType.IMAGE, name="lip-2.png", value="data:image/png;base64,BBB"),
    ReferenceMaterial(
        type=ReferenceMaterialType.TEXT,
        value="제가 직접 3주 동안 발라 보니 낮에는 유지가 잘 됐어요.",
    ),
]


class TestBeautyReviewWithProductImages:
    PURPOSES = ["후기·리뷰 작성"]

    def test_experience_material_keeps_the_review_archetype(self):
        evidence = build_profile(BEAUTY_MATERIALS, [])
        assert evidence.has_user_experience_evidence is True
        plan = style_for("립 틴트 발색", self.PURPOSES, evidence=evidence)
        assert plan.content_category == "BEAUTY"
        assert plan.editorial_archetype == "FIELD_REVIEW"

    def test_the_theme_is_a_soft_beauty_family_not_the_blue_default(self):
        evidence = build_profile(BEAUTY_MATERIALS, [])
        plan = style_for("립 틴트 발색", self.PURPOSES, evidence=evidence)
        assert plan.chart_theme in ("BEAUTY_EDITORIAL", "LIFESTYLE_JOURNAL")
        assert plan.photo_language in ("SOFT_BEAUTY_DESK", "PRODUCT_STUDIO_NATURAL")
        # 파란 포인트의 기본 팔레트로 되돌아가지 않는다.
        assert plan.chart_theme != "EDITORIAL_NEUTRAL"

    def test_charts_are_refused_and_one_table_is_allowed(self):
        evidence = build_profile(BEAUTY_MATERIALS, [])
        plan = style_for("립 틴트 발색", self.PURPOSES, evidence=evidence)
        assert gate([visual("PIE_CHART")], self.PURPOSES, plan=plan).kept == []
        kept = gate([visual("TABLE"), visual("TABLE", visual_id="visual-2")], self.PURPOSES, plan=plan).kept
        assert len(kept) == 1

    def test_each_reference_image_gets_its_own_role(self):
        evidence = build_profile(BEAUTY_MATERIALS, [])
        ids = [role.reference_id for role in evidence.reference_image_roles]
        assert ids == ["reference-image-1", "reference-image-2"]
        assert evidence.reference_image_roles[0].role == "PRODUCT_ANCHOR"
        assert evidence.reference_image_roles[1].role != "PRODUCT_ANCHOR"

    def test_the_photo_plan_prompt_asks_for_a_specific_reference_not_the_first(self):
        draft_input = build_input(
            input=BlogTaskInput(
                topic="립 틴트",
                purpose=self.PURPOSES,
                keywords=["립"],
                reference_materials=BEAUTY_MATERIALS,
            )
        )
        evidence = build_profile(BEAUTY_MATERIALS, [])
        draft_input = draft_input.model_copy(
            update={
                "reference_evidence": evidence,
                "editorial_style": style_for("립 틴트", self.PURPOSES, evidence=evidence),
            }
        )
        prompt = card_plan_prompt(draft_input, post(), 0, 2)
        assert "첫 장을 기본값으로 쓰지 않는다" in prompt
        assert "generatedOrReused를 REUSED로 둔다" in prompt
        assert "reference-image-1" in prompt


class TestBeautyReviewWithoutExperience:
    """2026-08-03 사용자 결정 뒤의 동작.

    예전에는 경험 자료가 없으면 후기 아키타입을 설명형으로 강등하고, 구매·사용 표현을
    금지 목록으로 주입하고, 그 표현이 본문에 있으면 원고를 반려했다. 이제 셋 다 하지
    않는다 — AI 자동 생성 글이 직접 겪은 것처럼 읽히는 것이 목적이기 때문이다.
    """

    PURPOSES = ["후기·리뷰 작성"]
    URL_ONLY = [
        ReferenceMaterial(type=ReferenceMaterialType.URL, value="https://brand.example/lip")
    ]

    def test_the_review_archetype_survives_without_experience(self):
        evidence = build_profile(self.URL_ONLY, [])
        assert evidence.has_user_experience_evidence is False
        plan = style_for("립 틴트 발색", self.PURPOSES, evidence=evidence)
        assert plan.editorial_archetype == "FIELD_REVIEW"

    def test_purchase_wording_is_no_longer_forbidden(self):
        evidence = build_profile(self.URL_ONLY, [])
        assert not any("내돈내산" in claim for claim in evidence.forbidden_claims)
        # 수치 날조 금지는 남는다 — 지어낸 측정값은 문체가 아니라 사실 문제다.
        assert any("측정한 성능 수치" in claim for claim in evidence.forbidden_claims)

    @pytest.mark.parametrize("phrase", ["내돈내산", "재구매", "직접 결제", "배송받은"])
    def test_a_purchase_claim_is_no_longer_rejected(self, phrase):
        report = check_draft(
            post(f"{phrase} 제품입니다. {LONG_BODY}"), 5, has_experience_material=False
        )
        assert report.ok

    def test_the_same_phrase_passes_when_the_user_actually_wrote_about_it(self):
        report = check_draft(
            post(f"내돈내산 제품입니다. {LONG_BODY}"), 5, has_experience_material=True
        )
        assert report.ok


# --- 4. 나이키 참고 이미지 --------------------------------------------------


NIKE_EVIDENCE = ReferenceEvidenceProfile(
    has_references=True,
    has_user_experience_evidence=False,
    primary_entity="Air Max 90",
    brand="Nike",
    product_category="운동화",
    confirmed_attributes=["화이트와 그레이 중심 색상", "측면 스우시", "두꺼운 에어 미드솔"],
    confirmed_use_scenes=["제품 단독 사진"],
    reference_image_roles=[
        ReferenceImageEvidence(
            reference_id="reference-image-1",
            role="PRODUCT_ANCHOR",
            subject="화이트 계열 운동화",
            allowed_uses=["원본 재사용", "썸네일 배경 확장"],
            forbidden_inferences=["실제 구매 가격", "착화감"],
        )
    ],
    forbidden_claims=["직접 구매했다는 표현", "일주일간 착용했다는 표현"],
)


class TestBrandedProductReference:
    def test_the_image_prompt_pins_the_exact_product(self):
        from app.llm.contracts import PostImageGenerationInput

        image_input = PostImageGenerationInput(
            post_id="post_1",
            user_id="user_1",
            input=BlogTaskInput(topic="나이키 운동화", keywords=["나이키"]),
            selected_intent=SelectedIntentForDraft(
                intent_id="i1", title="t", target_reader="r", rationale="r"
            ),
            final_post=post(),
            prompt_version="m5-image@v3.0",
            image_index=1,
            total_images=2,
            content_prompt="a shoe on a bench",
            subject_identity="Nike Air Max 90",
            fidelity_requirements=NIKE_EVIDENCE.confirmed_attributes,
            preserve_brand_marks=True,
        )
        prompt = image_prompt(image_input)
        assert "Nike Air Max 90" in prompt
        assert "Do not substitute a similar product or a generic stand-in" in prompt
        assert "측면 스우시" in prompt
        # 로고를 새로 그리지는 않는다 — 원본에 있는 것을 그대로 둔다.
        assert "Do NOT redraw, restyle, relabel" in prompt

    def test_without_a_reference_the_logo_ban_stays_absolute(self):
        from app.llm.contracts import PostImageGenerationInput

        image_input = PostImageGenerationInput(
            post_id="post_1",
            user_id="user_1",
            input=BlogTaskInput(topic="운동화", keywords=["운동화"]),
            selected_intent=SelectedIntentForDraft(
                intent_id="i1", title="t", target_reader="r", rationale="r"
            ),
            final_post=post(),
            prompt_version="m5-image@v3.0",
            image_index=1,
            total_images=2,
            content_prompt="a shoe on a bench",
        )
        assert "No readable text, letters, numbers, logos" in image_prompt(image_input)

    def test_the_anchor_reaches_the_draft_prompt(self):
        draft_input = build_input().model_copy(update={"reference_evidence": NIKE_EVIDENCE})
        prompt = draft_prompt(draft_input)
        assert "Nike Air Max 90" in prompt
        assert "측면 스우시" in prompt
        assert "일주일간 착용했다는 표현" in prompt

    def test_wear_impressions_are_no_longer_rejected(self):
        """2026-08-03 사용자 결정: 착용 기간 서술도 더는 반려 사유가 아니다."""
        report = check_draft(
            post(f"일주일 사용 결과 발이 편했습니다. {LONG_BODY}"),
            5,
            has_experience_material=False,
        )
        assert report.ok


# --- 5. 테크 비교 + 벤치마크 ------------------------------------------------


BENCH_SOURCES = [
    SearchSource(
        title="노트북 벤치마크",
        url="https://example.com/bench",
        snippet="멀티코어 점수",
        data_points=[
            SourceDataPoint(label="A 노트북", value=1420.0, unit="점"),
            SourceDataPoint(label="B 노트북", value=1680.0, unit="점"),
        ],
    )
]


class TestTechComparisonWithBenchmarks:
    PURPOSES = ["비교·추천"]

    def test_the_theme_is_a_tech_family(self):
        plan = style_for("노트북 성능 비교", self.PURPOSES)
        assert plan.content_category == "TECH_IT"
        assert plan.chart_theme in ("TECH_BENCHMARK_LIGHT", "TECH_BENCHMARK_DARK")

    def test_at_most_one_table_and_one_bar_chart_survive(self):
        plan = style_for("노트북 성능 비교", self.PURPOSES)
        result = gate(
            [
                visual("TABLE"),
                visual("TABLE", visual_id="visual-2"),
                visual("BAR_CHART", visual_id="visual-3"),
                visual("BAR_CHART", visual_id="visual-4"),
            ],
            self.PURPOSES,
            plan=plan,
        )
        types = [v.type for v in result.kept]
        assert types.count("TABLE") <= 1
        assert types.count("BAR_CHART") <= 1
        assert len(result.kept) <= plan.visual_budget.rendered_visuals_max

    def test_a_chart_must_match_the_source_numbers_exactly(self):
        """서비스의 출처 대조 규칙 — 값 하나만 달라도 그 그래프는 만들지 않는다."""
        from app.modules.draft.service import _verified_visual

        draft_input = build_input(
            selected_intent=SelectedIntentForDraft(
                intent_id="i1",
                title="비교",
                target_reader="독자",
                rationale="근거",
                sources=BENCH_SOURCES,
            )
        )
        matching = PlannedVisual(
            visual_id="visual-1",
            type="BAR_CHART",
            title="멀티코어 점수",
            source="source-1",
            data=[
                VisualDataPoint(label="A 노트북", value=1420.0),
                VisualDataPoint(label="B 노트북", value=1680.0),
            ],
        )
        assert _verified_visual(matching, draft_input) is not None
        drifted = matching.model_copy(
            update={
                "data": [
                    VisualDataPoint(label="A 노트북", value=1420.0),
                    VisualDataPoint(label="B 노트북", value=1699.0),
                ]
            }
        )
        assert _verified_visual(drifted, draft_input) is None

    def test_a_table_source_label_is_resolved_to_the_real_source(self):
        """표·과정도는 수치 대조 대상이 아니지만 출처 라벨은 똑같이 풀어야 한다.

        실측(2026-08-03): 렌더된 표 하단에 '출처: source-2'가 글자 그대로 찍혔다 —
        source-N은 모델이 수집 목록을 가리키라고 우리가 준 형식이지 독자에게 보여 줄
        문자열이 아니다.
        """
        from app.modules.draft.service import _verified_visual

        draft_input = build_input(
            selected_intent=SelectedIntentForDraft(
                intent_id="i1",
                title="비교",
                target_reader="독자",
                rationale="근거",
                sources=BENCH_SOURCES,
            )
        )
        table = PlannedVisual(
            visual_id="visual-1",
            type="TABLE",
            title="작품별 결",
            source="source-1",
            columns=["작품", "결"],
            rows=[VisualTableRow(name="A", cells=["학원 액션"])],
        )
        resolved = _verified_visual(table, draft_input)
        assert resolved is not None
        assert resolved.source == BENCH_SOURCES[0].title
        assert not resolved.source.startswith("source-")

    def test_a_table_pointing_at_a_missing_source_drops_the_label(self):
        """가리키는 출처가 없으면 라벨을 지운다 — 뜻 모를 'source-9'를 싣지 않는다."""
        from app.modules.draft.service import _verified_visual

        draft_input = build_input(
            selected_intent=SelectedIntentForDraft(
                intent_id="i1",
                title="비교",
                target_reader="독자",
                rationale="근거",
                sources=BENCH_SOURCES,
            )
        )
        table = PlannedVisual(
            visual_id="visual-1",
            type="TABLE",
            title="작품별 결",
            source="source-9",
            columns=["작품", "결"],
            rows=[VisualTableRow(name="A", cells=["학원 액션"])],
        )
        resolved = _verified_visual(table, draft_input)
        assert resolved is not None and resolved.source is None

    def test_the_highlighted_bar_follows_the_conclusion_not_the_maximum(self):
        from app.modules.draft.visuals import _highlight_indexes

        chart = visual("BAR_CHART", conclusion="2025년 값이 기준선이다")
        assert _highlight_indexes(chart, chart.data) == {0}
        explicit = chart.model_copy(
            update={"conclusion": None, "highlight_labels": ["2027년"]}
        )
        assert _highlight_indexes(explicit, explicit.data) == {2}


# --- 6. 트렌드 + 수치 없음 --------------------------------------------------


class TestTrendWithoutNumbers:
    PURPOSES = ["트렌드·이슈 소개"]

    def test_charts_without_data_are_dropped(self):
        plan = style_for("요즘 화제의 흐름", self.PURPOSES)
        empty_chart = PlannedVisual(
            visual_id="visual-1", type="LINE_CHART", title="추세", data=None
        )
        assert gate([empty_chart], self.PURPOSES, plan=plan).kept == []

    def test_decorative_infographics_are_not_allowed_at_all(self):
        plan = style_for("요즘 화제의 흐름", self.PURPOSES)
        assert "INFOGRAPHIC" in plan.forbidden_visual_types
        assert gate([visual("INFOGRAPHIC")], self.PURPOSES, plan=plan).kept == []

    def test_zero_visuals_is_a_valid_result(self):
        result = gate([], self.PURPOSES)
        assert result.kept == []
        assert result.rejections == []

    def test_the_prompt_forbids_boxing_the_body_text(self):
        draft_input = build_input(
            input=BlogTaskInput(topic="트렌드", purpose=self.PURPOSES, keywords=["트렌드"])
        )
        draft_input = draft_input.model_copy(
            update={"editorial_style": style_for("트렌드", self.PURPOSES)}
        )
        prompt = content_plan_prompt(draft_input)
        assert "본문 문장을 다시 박스에 넣은 이미지는 만들지 않는다" in prompt


# --- 7. 운동 기록 -----------------------------------------------------------


class TestWorkoutRecords:
    def test_without_numbers_there_is_no_chart(self):
        purposes = ["일상·경험 공유"]
        plan = style_for("러닝 일상", purposes)
        assert plan.content_category in ("FITNESS_SPORTS", "DAILY_LIFE")
        assert gate([visual("LINE_CHART")], purposes, plan=plan).kept == []

    def test_with_dated_records_a_single_line_chart_is_allowed(self):
        purposes = ["일상·경험 공유"]
        plan = style_for("러닝 일상", purposes)
        result = gate(
            [visual("LINE_CHART"), visual("LINE_CHART", visual_id="visual-2")],
            purposes,
            plan=plan,
            user_numeric=True,
        )
        assert len(result.kept) == 1

    def test_a_fitness_article_uses_the_performance_theme(self):
        plan = style_for("마라톤 기록 분석", ["정보 전달"])
        assert plan.content_category == "FITNESS_SPORTS"
        assert plan.chart_theme in ("FITNESS_PERFORMANCE", "EDITORIAL_NEUTRAL")

    def test_the_rendered_chart_keeps_the_exact_values(self):
        chart = PlannedVisual(
            visual_id="visual-1",
            type="LINE_CHART",
            title="월별 러닝 거리",
            unit="km",
            source="개인 기록",
            style="FITNESS_PERFORMANCE",
            data=[
                VisualDataPoint(label="6월", value=42.0),
                VisualDataPoint(label="7월", value=55.0),
                VisualDataPoint(label="8월", value=98.0),
            ],
        )
        rendered = render_planned_visual(chart)
        assert rendered is not None
        assert _style_for(chart) is _style_for(chart.model_copy())


# --- 8. 썸네일 피사체 보호 --------------------------------------------------


def _photo(colour=(190, 190, 190)) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1536, 1024), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def _copy_box(layout: ThumbnailLayoutPlan, lines: list[str]):
    import io

    from PIL import Image, ImageChops

    blank = Image.open(
        io.BytesIO(render_thumbnail(_photo(), [], layout.model_copy(update={"show_copy": False})))
    ).convert("RGB")
    lettered = Image.open(io.BytesIO(render_thumbnail(_photo(), lines, layout))).convert("RGB")
    diff = ImageChops.difference(blank, lettered).convert("L")
    return diff.point(lambda p: p if p > 12 else 0).getbbox()


class TestThumbnailProtectsTheSubject:
    LINES = ["직접 비교한", "핵심 차이"]

    def test_copy_left_keeps_the_right_half_for_the_subject(self):
        layout = ThumbnailLayoutPlan(
            layout="COPY_LEFT_SUBJECT_RIGHT",
            subject_zone="RIGHT_CENTER",
            copy_zone="LEFT_CENTER",
            copy_alignment="LEFT",
            copy_lines=self.LINES,
            show_copy=True,
        )
        box = _copy_box(layout, self.LINES)
        assert box is not None, "문구가 그려지지 않았다"
        subject = zone_box("RIGHT_CENTER")
        # 문구는 피사체 영역 왼쪽에서 끝난다.
        assert box[2] <= subject[0] + 40

    def test_copy_and_subject_zones_never_overlap(self):
        for copy_zone, subject_zone in (
            ("LEFT_CENTER", "RIGHT_CENTER"),
            ("RIGHT_CENTER", "LEFT_CENTER"),
            ("TOP_CENTER", "BOTTOM_CENTER"),
            ("BOTTOM_CENTER", "TOP_CENTER"),
            ("TOP_LEFT", "RIGHT_CENTER"),
            ("BOTTOM_LEFT", "RIGHT_CENTER"),
        ):
            assert not zones_overlap(copy_zone, subject_zone), (copy_zone, subject_zone)

    def test_an_overlapping_plan_is_moved_to_the_other_side(self):
        clashing = ThumbnailLayoutPlan(
            layout="COPY_LEFT_SUBJECT_RIGHT",
            subject_zone="LEFT_CENTER",
            copy_zone="LEFT_CENTER",
            copy_alignment="LEFT",
            show_copy=True,
        )
        assert resolve_thumbnail_layout(clashing).copy_zone == "RIGHT_CENTER"

    def test_the_copy_survives_the_mobile_square_crop(self):
        for copy_zone in ("LEFT_CENTER", "RIGHT_CENTER", "TOP_CENTER", "BOTTOM_CENTER"):
            layout = ThumbnailLayoutPlan(
                copy_zone=copy_zone, subject_zone="CENTER", copy_lines=self.LINES, show_copy=True
            )
            box = _copy_box(layout, self.LINES)
            assert box is not None
            assert box[0] >= SAFE_AREA_LEFT - 2 and box[2] <= SAFE_AREA_RIGHT + 2

    def test_show_copy_false_draws_nothing_and_does_not_fall_back_to_the_title(self):
        layout = ThumbnailLayoutPlan(
            layout="NO_COPY_EDITORIAL_PHOTO", copy_mode="NONE", show_copy=False
        )
        assert _copy_box(layout, ["무시되어야 하는 문구"]) is None

    def test_legacy_posts_without_a_layout_keep_the_centred_copy(self):
        import io

        from PIL import Image, ImageChops

        blank = Image.open(io.BytesIO(render_thumbnail(_photo(), []))).convert("RGB")
        lettered = Image.open(io.BytesIO(render_thumbnail(_photo(), ["핵심 문구"]))).convert("RGB")
        diff = ImageChops.difference(blank, lettered).convert("L")
        box = diff.point(lambda p: p if p > 12 else 0).getbbox()
        assert box is not None
        centre = CANVAS_HEIGHT // 2
        assert box[1] < centre < box[3]

    def test_a_plan_without_copy_lines_never_claims_to_show_copy(self):
        plan = style_for("소재", ["정보 전달"])
        assert thumbnail_layout_plan_for(plan, []).show_copy is False

    def test_the_written_copy_is_kept_whole(self):
        """배치 때문에 원고가 정한 문구를 잘라 내지 않는다 — 화면과 썸네일이 갈리면 안 된다."""
        plan = style_for("노트북 성능 비교", ["비교·추천"])
        layout = thumbnail_layout_plan_for(plan, self.LINES)
        assert layout.copy_lines == self.LINES

    def test_new_thumbnails_use_the_reference_style_center_title_box(self):
        plan = style_for("노트북 성능 비교", ["비교·추천"])
        layout = thumbnail_layout_plan_for(plan, self.LINES)

        assert layout.layout == "CENTER_COPY_ON_NEGATIVE_SPACE"
        assert layout.copy_zone == "CENTER"
        assert layout.copy_alignment == "CENTER"
        assert layout.scrim_style == "LOCAL_ROUNDED"
        assert layout.show_copy is True


# --- 9. 미리보기 CSS --------------------------------------------------------


CSS = (Path(__file__).resolve().parents[2] / "web" / "src" / "app.css").read_text(
    encoding="utf-8"
)


class TestPreviewCss:
    def test_content_images_carry_no_post_it_offset_shadow(self):
        """Blog-it의 노란 오프셋 그림자는 서비스 UI의 것이지 원고 콘텐츠의 것이 아니다."""
        rules = _css_rules_for(".preview-image img")
        assert rules, ".preview-image img 규칙을 찾지 못했다"
        combined = " ".join(rules)
        assert "var(--brand)" not in combined
        assert "box-shadow: none" in combined

    @pytest.mark.parametrize("kind", ["photo", "cover", "visual", "reference", "screenshot"])
    def test_every_media_kind_has_its_own_rule(self, kind):
        assert f".blog-media--{kind}" in CSS

    def test_charts_get_neither_rounded_corners_nor_a_shadow(self):
        rules = _css_rules_for(".preview-image.blog-media--visual img")
        combined = " ".join(rules)
        assert "border-radius: 0" in combined
        assert "box-shadow: none" in combined

    def test_the_service_ui_keeps_its_post_it_identity(self):
        # 원고 이미지에서만 걷어냈다는 것을 확인한다 — 서비스 카드·버튼은 그대로다.
        # (예전 기준 문자열 'var(--brand)'는 죽은 규칙(.toggle)에만 남아 있어, 2026-08-02
        # 고아 CSS 정리 때 실제 화면 버튼이 쓰는 규칙으로 기준을 옮겼다.)
        assert "box-shadow: 5px 5px 0 var(--brand-deep)" in CSS


def _css_rules_for(selector: str) -> list[str]:
    import re

    return [
        match.group(1)
        for match in re.finditer(
            rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", CSS
        )
    ]


# --- 10. 스타일 다양성 ------------------------------------------------------


class TestStyleVariation:
    def test_two_posts_in_the_same_category_share_the_identity_but_can_differ(self):
        plans = [
            style_for("노트북 성능 비교", ["비교·추천"], post_id=f"post_{n}")
            for n in range(12)
        ]
        assert {plan.content_category for plan in plans} == {"TECH_IT"}
        # 카테고리 정체성은 같되, 그 안의 선택은 글마다 갈린다.
        assert len({(plan.chart_theme, plan.thumbnail_layout, plan.accent_family) for plan in plans}) > 1
        for plan in plans:
            assert plan.chart_theme in ("TECH_BENCHMARK_LIGHT", "TECH_BENCHMARK_DARK")

    def test_reloading_the_same_post_gives_the_same_design(self):
        first = style_for("노트북 성능 비교", ["비교·추천"])
        second = style_for("노트북 성능 비교", ["비교·추천"])
        assert first == second

    def test_regenerating_can_choose_a_different_variant(self):
        revisions = [
            style_for("노트북 성능 비교", ["비교·추천"], revision=n) for n in range(6)
        ]
        assert {plan.content_category for plan in revisions} == {"TECH_IT"}
        assert len({plan.variation_seed for plan in revisions}) == 6
        assert len({(plan.chart_theme, plan.thumbnail_layout) for plan in revisions}) > 1

    def test_a_stored_plan_is_honoured_instead_of_being_recomputed(self):
        """저장된 계획을 다시 정규화해도 같은 값이 나온다 — 새로고침으로 디자인이 바뀌지 않는다."""
        stored = style_for("립 틴트 발색", ["후기·리뷰 작성"])
        again = normalize_style_plan(
            stored,
            post_id="post_1",
            revision=0,
            topic="립 틴트 발색",
            subject=None,
            purposes=["후기·리뷰 작성"],
            article_length="medium",
        )
        assert again.content_category == stored.content_category
        assert again.chart_theme == stored.chart_theme
        assert again.thumbnail_layout == stored.thumbnail_layout

    def test_different_categories_get_different_colour_directions(self):
        beauty = colour_direction_for(style_for("립 틴트", ["후기·리뷰 작성"]))
        tech = colour_direction_for(style_for("노트북 성능 비교", ["비교·추천"]))
        assert beauty != tech
        assert "dressing table" in beauty or "cosmetic" in beauty

    def test_the_length_cap_bounds_the_budget(self):
        short = normalize_style_plan(
            None,
            post_id="post_1",
            revision=0,
            topic="노트북 성능 비교",
            subject=None,
            purposes=["비교·추천"],
            article_length="short",
        )
        long = normalize_style_plan(
            None,
            post_id="post_1",
            revision=0,
            topic="노트북 성능 비교",
            subject=None,
            purposes=["비교·추천"],
            article_length="long",
        )
        # 2026-08-03 사용자 결정: 사진은 길이 범위(짧게 2~3장·중간 3~5장)를 따르고,
        # 표·그래프는 길이와 무관하게 AI 판단(목적별 근거 원칙)만 따른다. 옛 "long"은
        # 중간 취급. 예산의 상한은 범위의 최댓값에서 썸네일 몫을 뺀 값이다.
        assert short.visual_budget.body_photos_max <= 2  # 사진 최대 3장 − 썸네일
        assert short.visual_budget.rendered_visuals_max == 2  # 목적(비교·추천) 원칙만 남는다
        assert long.visual_budget.rendered_visuals_max == 2
        assert long.visual_budget.body_photos_max <= 4  # 사진 최대 5장 − 썸네일


# --- 계획 프롬프트가 근거를 실제로 싣는가 ------------------------------------


class TestPlanningPromptsCarryTheEvidence:
    def test_the_evidence_prompt_separates_seen_from_assumed(self):
        draft_input = build_input(
            input=BlogTaskInput(
                topic="립 틴트",
                purpose=["후기·리뷰 작성"],
                keywords=["립"],
                reference_materials=BEAUTY_MATERIALS,
            )
        )
        prompt = reference_evidence_prompt(draft_input)
        assert "reference-image-1" in prompt and "reference-image-2" in prompt
        assert "보이는 것" in prompt and "추정되는 것" in prompt
        assert "없는 증거를 만들지 않는다" in prompt

    def test_the_style_prompt_refuses_to_judge_from_the_persona_alone(self):
        draft_input = build_input().model_copy(
            update={"reference_evidence": build_profile([], [])}
        )
        prompt = editorial_style_prompt(draft_input)
        assert "페르소나만으로 정하지 않는다" in prompt
        assert "화장품은 BEAUTY, 러닝화는 FITNESS_SPORTS" in prompt
        assert "상한이지 최소가 아니다" in prompt

    def test_the_draft_prompt_switches_structure_with_the_archetype(self):
        journal = build_input().model_copy(
            update={
                "editorial_style": EditorialStylePlan(
                    editorial_archetype="DAILY_JOURNAL", article_rhythm="SCENE_FIRST"
                )
            }
        )
        lab = build_input().model_copy(
            update={
                "editorial_style": EditorialStylePlan(
                    editorial_archetype="COMPARISON_LAB", article_rhythm="ANSWER_FIRST"
                )
            }
        )
        journal_prompt, lab_prompt = draft_prompt(journal), draft_prompt(lab)
        assert "그날의 한 장면이나 특정 시간에서 시작한다" in journal_prompt
        assert "결론을 첫 문단에서 먼저 제시한다" in lab_prompt
        # 예전의 고정 골격은 아키타입이 있는 글에서 사라진다.
        assert "독자의 현재 상황 → 구체적인 불편" not in journal_prompt
        assert "독자의 현재 상황 → 구체적인 불편" not in lab_prompt

    def test_a_post_without_a_style_plan_keeps_the_old_skeleton(self):
        """구형 어댑터·계획 실패 경로의 프롬프트는 예전과 같아야 한다."""
        prompt = draft_prompt(build_input())
        assert "독자의 현재 상황 → 구체적인 불편" in prompt

    def test_the_persona_expression_limit_follows_the_purpose_rules(self):
        """2026-07-30: 조회 키가 표시 이름 → persona_id로 바뀌었다.

        예전에는 표시 이름을 후보 문자열 안에서 부분 일치로 찾았고(`"트렌드 에디터" in
        default_persona`), 그래서 커스텀 이름 "실무 코치처럼 쓰는 사람"이 실무 코치 프리셋으로
        오인됐다. 이 테스트도 그 방식에 맞춰 이름을 프롬프트 자리에 넣고 있었으므로, 실제
        운용 경로(서비스가 저장된 id를 default_persona_id로 넘긴다)와 같게 고쳤다.
        """
        draft_input = build_input(
            settings=DraftGenerationSettings(
                hashtag_count=5,
                article_length="medium",
                default_persona="트렌드 에디터다. 담담하게 관찰한다.",
                default_persona_id="p_6",
            )
        )
        prompt = draft_prompt(draft_input)
        assert "페르소나 표현 강도" in prompt
        assert "장식용 인포그래픽을 만들지 않는다" in prompt
        # 목적이 글의 종류를 정한다는 규칙이 페르소나 규칙보다 앞에 나온다.
        assert prompt.index("목적과 페르소나") < prompt.index("페르소나 표현 강도")

    def test_the_expression_limit_is_skipped_when_no_preset_id_is_stored(self):
        """이름만으로는 프리셋을 판정하지 않는다 — 커스텀 화자에 프리셋 규칙을 얹지 않기 위해."""
        draft_input = build_input(
            settings=DraftGenerationSettings(
                hashtag_count=5, article_length="medium", default_persona="트렌드 에디터"
            )
        )
        assert "페르소나 표현 강도" not in draft_prompt(draft_input)


# --- 사진 계획의 역할 분리 --------------------------------------------------


def _card(card_id: str, role: str, reference_id: str | None = None) -> CardBrief:
    return CardBrief(
        card_id=card_id,
        card_type="SECTION_CARD",
        article_claim="원고에 있는 문장",
        visual_purpose="목적",
        photo_role=role,
        reference_id=reference_id,
        uses_reference=reference_id is not None,
        scene=CardScene(main_subject=f"subject for {card_id}"),
        necessity_score=90,
    )


class TestPhotoRolesAndReferenceMapping:
    def test_each_card_can_point_at_a_different_reference_image(self):
        from app.modules.draft.service import _reference_url_for

        urls = {
            "reference-image-1": "data:image/png;base64,AAA",
            "reference-image-2": "data:image/png;base64,BBB",
        }
        detail = _card("photo-1", "PRODUCT_DETAIL", "reference-image-2")
        assert _reference_url_for(detail, urls) == "data:image/png;base64,BBB"

    def test_a_card_without_a_reference_gets_none(self):
        from app.modules.draft.service import _reference_url_for

        urls = {"reference-image-1": "data:image/png;base64,AAA"}
        assert _reference_url_for(_card("photo-1", "IN_USE_SCENE"), urls) is None

    def test_an_unknown_reference_id_falls_back_to_the_first_image(self):
        from app.modules.draft.service import _reference_url_for

        urls = {"reference-image-1": "data:image/png;base64,AAA"}
        stale = _card("photo-1", "PRODUCT_HERO", "reference-image-9")
        assert _reference_url_for(stale, urls) == "data:image/png;base64,AAA"

    def test_the_plan_prompt_forbids_repeating_a_role(self):
        draft_input = build_input(
            input=BlogTaskInput(
                topic="립 틴트",
                purpose=["후기·리뷰 작성"],
                keywords=["립"],
                reference_materials=BEAUTY_MATERIALS,
            )
        ).model_copy(update={"reference_evidence": build_profile(BEAUTY_MATERIALS, [])})
        prompt = card_plan_prompt(draft_input, post(), 0, 2)
        assert "같은 photoRole을 두 번 쓰지 않는다" in prompt
        assert "조명만 다른 정면처럼 같은 정보를 반복하는 사진을 만들지 않는다" in prompt

    def test_the_plan_prompt_keeps_the_length_quota_over_the_style_budget(self):
        """실사례(2026-07-31): 스타일 계획이 bodyPhotosMax=1을 요청해 중간 글이 썸네일
        포함 총 2장으로 발행됐다. 길이 규격(중간=3장 고정, 2026-08-07)은 사용자 결정이라
        스타일 예산이 줄이지 못한다 — 카드 계획은 반드시 본문 2장 + 예비 1장을 요구해야
        한다."""
        style = normalize_style_plan(
            None,
            post_id="post_1",
            revision=0,
            topic="소재",
            subject=None,
            purposes=["정보 전달"],
            article_length="medium",
        ).model_copy(
            update={
                "visual_budget": VisualBudget(
                    thumbnail=1,
                    reference_images_max=1,
                    body_photos_max=1,
                    rendered_visuals_max=0,
                )
            }
        )
        draft_input = build_input().model_copy(update={"editorial_style": style})

        prompt = card_plan_prompt(draft_input, post(), 0, 0)

        # 중간 = 사진 3장 고정(썸네일 포함) → 본문 사진 정확히 2장.
        assert "SECTION_CARD는 **정확히 2장**을 계획한다" in prompt
        assert "예비 SECTION_CARD 1장을 더해 총 3장까지" in prompt
        assert "실리는 사진은 최대 3장" in prompt

    def test_the_short_length_plan_prompt_still_asks_for_body_photos(self):
        """짧게 = 사진 2장 고정(썸네일 포함, 2026-08-07 사용자 결정) — 예전처럼
        썸네일 한 장으로 끝내지 않는다."""
        draft_input = build_input(
            settings=DraftGenerationSettings(hashtag_count=5, article_length="short")
        )
        prompt = card_plan_prompt(draft_input, post(), 0, 0)
        assert "SECTION_CARD는 **정확히 1장**을 계획한다" in prompt
        assert "실리는 사진은 최대 2장" in prompt

    def test_references_use_up_the_photo_quota_but_rendered_visuals_do_not(self):
        """첨부 이미지는 사진 자리를 차지하지만, 표·그래프는 사진 규격과 별개다
        (2026-08-03 사용자 결정)."""
        prompt = card_plan_prompt(build_input(), post(), 1, 1)
        # 중간(3장 고정)에서 첨부 1이 자리를 차지하면 본문 사진은 정확히 1장이 된다.
        assert "SECTION_CARD는 **정확히 1장**을 계획한다" in prompt
        assert "표·그래프 1개는 이 규격과 별개로 실린다" in prompt

    def test_references_alone_can_fill_the_photo_quota(self):
        """첨부가 본문 사진 자리를 다 차지하면 SECTION_CARD는 계획하지 않는다."""
        prompt = card_plan_prompt(build_input(), post(), 0, 2)
        assert "SECTION_CARD는 계획하지 않는다" in prompt

    def test_evidence_only_roles_are_closed_without_material(self):
        draft_input = build_input().model_copy(
            update={"reference_evidence": build_profile([], [])}
        )
        prompt = card_plan_prompt(draft_input, post(), 0, 0)
        assert "지금은 그런 자료가 없으므로" in prompt

    def test_the_plan_prompt_classifies_the_core_visual_subject(self):
        """고유 캐릭터·실제 인물이 소재면 그 대상 자체를 피사체로 삼으라고 지시한다."""
        draft_input = build_input(
            input=BlogTaskInput(topic="스파이더맨", purpose=["정보 전달"], keywords=["스파이더맨"])
        )
        prompt = card_plan_prompt(draft_input, post(), 0, 0)

        assert "핵심 시각 대상 판정" in prompt
        assert "FICTIONAL_CHARACTER" in prompt
        assert "REAL_NAMED_PERSON" in prompt
        assert "GENERIC_PERSON_ROLE" in prompt
        assert "mustShowSubject=true" in prompt
        # 이름이 한 번 스쳤다는 이유로 모든 사진을 인물 중심으로 만들지 않는다.
        assert "이름이 본문에 한 번 스쳐 지나갔다는 이유만으로" in prompt
        # 배우·작품 버전을 임의로 고정하지 않는다.
        assert "특정 배우의 얼굴로 고정하지 않고" in prompt
        # 직업·역할은 고유 인물이 아니다.
        assert "직업·역할은 고유 인물이 아니다" in prompt

    def test_the_model_cannot_invent_a_receipt_role(self):
        """모델이 영수증 역할을 붙여도 근거가 없으면 코드가 되돌린다."""
        base = build_profile(BEAUTY_MATERIALS, [])
        proposed = ReferenceEvidenceProfile(
            has_references=True,
            reference_image_roles=[
                ReferenceImageEvidence(
                    reference_id="reference-image-1", role="RECEIPT_EVIDENCE", subject=""
                )
            ],
        )
        merged = enrich(base, proposed)
        assert merged.reference_image_roles[0].role == "PRODUCT_ANCHOR"

    def test_the_code_owns_whether_experience_evidence_exists(self):
        """모델이 '경험 있음'이라고 해도 코드 판정이 이긴다 — 낙관이 통과하는 문이 없어야 한다."""
        base = build_profile(
            [ReferenceMaterial(type=ReferenceMaterialType.URL, value="https://a.example")], []
        )
        optimistic = ReferenceEvidenceProfile(
            has_references=True, has_user_experience_evidence=True, primary_entity="제품"
        )
        merged = enrich(base, optimistic)
        assert merged.has_user_experience_evidence is False
        assert merged.primary_entity == "제품"
