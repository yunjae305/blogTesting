"""편집 문체 계획 11항목(5-5)과 섹션 설계 6항목(5-6).

전까지 편집 계획은 카테고리·아키타입·시각 예산만 정했고, '어떻게 쓰는가'는 모든 글이 같은
공통 규칙 하나뿐이었다. 콘텐츠 설계도 마찬가지로 소제목 목록이라, 원고 단계에서 섹션마다
같은 분량·같은 구성으로 수렴할 여지가 그대로 있었다.

여기서 검증하는 경계는 둘이다.

1. **실행 가능성** — '자연스럽게 쓴다' 같은 형용사는 계획에 남지 않는다. 남으면 원고가
   달라지지 않으면서 프롬프트만 길어진다.
2. **형식과 내용의 분리** — 도입·결말을 *어떤 방식으로* 열고 닫는지는 코드 회전이 정하고
   (5-4), 그 자리에 *무엇을* 담는지만 모델이 정한다. 형식까지 모델이 고르면 아키타입이 같은
   글은 매번 같은 도입·결말이 된다.
"""

from app.llm import parsing, prompts, schemas
from app.shared import (
    ContentPlan,
    ContentPlanSection,
    EditorialStylePlan,
    ReferenceEvidenceProfile,
    WritingDirection,
)
from tests.test_prompt_audit import draft_input

DIRECTION_FIELDS = (
    "voiceDistance",
    "readerRelationship",
    "sentenceDensity",
    "openingMode",
    "rhythmProfile",
    "transitionStyle",
    "detailFocus",
    "firstPersonPolicy",
    "certaintyPolicy",
    "closingMode",
    "avoidPatterns",
)

SECTION_FIELDS = (
    "interpretation",
    "omitBackground",
    "connection",
    "lengthShare",
    "personaDetail",
    "forbiddenClaims",
)


def direction(**overrides) -> WritingDirection:
    base = dict(
        voice_distance="설명은 3인칭으로 하고, 판단이 필요한 대목에서만 견해를 드러낸다.",
        reader_relationship="매 문단 질문하지 않고 선택이 필요한 구간에서만 말을 건다.",
        sentence_density="핵심 판단은 한두 문장으로 먼저 쓰고 조건은 뒤 문단에서 설명한다.",
        opening_mode="물통을 비워도 냄새가 남는 상황에서 바로 시작한다.",
        rhythm_profile="판단은 짧게 끊고 근거 설명은 한 문장 안에서 이어간다.",
        transition_style="부품이 바뀌는 지점에서 무엇이 달라지는지로 넘어간다.",
        detail_focus="세척 주기와 순서는 구체적으로, 제습 원리는 짧게 넘어간다.",
        first_person_policy="자료에 개인 경험이 없으므로 1인칭 체험 표현을 쓰지 않는다.",
        certainty_policy="제조사가 밝힌 주기는 단정하고 체감 냄새는 조건과 함께 쓴다.",
        closing_mode="다음 세척에서 무엇부터 할지 하나만 남긴다.",
        avoid_patterns=["모든 섹션을 세척 절차로 끝내기"],
    )
    base.update(overrides)
    return WritingDirection(**base)


def style_plan(**overrides) -> EditorialStylePlan:
    base = dict(article_rhythm="ANSWER_FIRST", writing_direction=direction())
    base.update(overrides)
    return EditorialStylePlan(**base)


def section(**overrides) -> ContentPlanSection:
    base = dict(
        section_id="section-1",
        heading="물통은 어떻게 관리하나",
        question="물통을 어떻게 씻나",
        purpose="해결 방법",
        key_points=["미온수 헹굼"],
        interpretation="구연산과 세제 중 무엇을 쓸지는 냄새의 원인에 따라 갈린다",
        omit_background="제습 원리는 도입에서 이미 말했다",
        connection="냄새의 출발점이 물통이라 여기서 시작한다",
        length_share="30~40%",
        persona_detail="물때가 남는 자리를 짚어 준다",
        forbidden_claims=["구연산이 세균을 몇 % 없앤다"],
    )
    base.update(overrides)
    return ContentPlanSection(**base)


def content_plan(sections=None) -> ContentPlan:
    return ContentPlan(
        target_reader="1인 가구",
        reader_problem="냄새가 남는다",
        reader_question="무엇부터 씻나",
        article_promise="관리 순서를 정리한다",
        content_angle="순서와 주기를 함께 준다",
        sections=sections if sections is not None else [section()],
    )


class TestTheSchemaAsksForAllElevenItems:
    def test_every_item_is_required(self):
        schema = schemas.WRITING_DIRECTION_SCHEMA
        assert set(schema["required"]) == set(DIRECTION_FIELDS)
        assert set(schema["properties"]) == set(DIRECTION_FIELDS)

    def test_the_editorial_style_call_actually_asks_for_it(self):
        plan = schemas.EDITORIAL_STYLE_PLAN_SCHEMA["properties"]["editorialStylePlan"]
        assert "writingDirection" in plan["required"]
        assert plan["properties"]["writingDirection"] is schemas.WRITING_DIRECTION_SCHEMA

    def test_every_item_explains_what_an_executable_answer_looks_like(self):
        # 설명이 비어 있으면 모델은 필드 이름만 보고 형용사를 채운다.
        for field, spec in schemas.WRITING_DIRECTION_SCHEMA["properties"].items():
            assert spec["description"].strip(), field

    def test_the_form_of_the_opening_and_closing_is_not_left_to_the_model(self):
        # 5-4의 회전이 형식을 정한다. 여기서 형식까지 고르면 두 축이 서로 싸운다.
        props = schemas.WRITING_DIRECTION_SCHEMA["properties"]
        assert "articleRhythm" in props["openingMode"]["description"]
        assert "형식" in props["closingMode"]["description"]


class TestUnusableAnswersAreDropped:
    def test_it_reads_the_eleven_items(self):
        parsed = parsing.writing_direction_from_json(
            {
                "voiceDistance": "설명은 3인칭으로 하고 판단에서만 견해를 드러낸다.",
                "avoidPatterns": ["모든 섹션을 절차로 끝내기"],
            }
        )
        assert parsed is not None
        assert parsed.voice_distance.startswith("설명은 3인칭으로")
        assert parsed.avoid_patterns == ["모든 섹션을 절차로 끝내기"]

    def test_an_adjective_only_answer_becomes_empty(self):
        parsed = parsing.writing_direction_from_json(
            {"rhythmProfile": "자연스럽게 쓴다", "voiceDistance": "읽기 좋게"}
        )
        assert parsed is None

    def test_the_same_word_inside_a_real_instruction_survives(self):
        # '자연스럽게'가 들어 있다는 이유로 지시까지 버리면 쓸 수 있는 계획을 잃는다.
        parsed = parsing.writing_direction_from_json(
            {
                "transitionStyle": "앞 문단의 결론을 다음 문단의 조건으로 이어받아"
                " 자연스럽게 넘어가고, 목차형 연결어는 쓰지 않는다."
            }
        )
        assert parsed is not None
        assert "목차형 연결어" in parsed.transition_style

    def test_nothing_usable_means_no_plan_at_all(self):
        assert parsing.writing_direction_from_json({}) is None
        assert parsing.writing_direction_from_json(None) is None
        assert parsing.writing_direction_from_json("문자열") is None

    def test_the_avoid_list_has_a_ceiling(self):
        parsed = parsing.writing_direction_from_json(
            {"avoidPatterns": [f"패턴{index}" for index in range(20)]}
        )
        assert parsed is not None
        assert len(parsed.avoid_patterns) == 6

    def test_the_editorial_plan_carries_it(self):
        parsed = parsing.editorial_style_plan_from_json(
            {
                "editorialStylePlan": {
                    "contentCategory": "LIFE_HOME",
                    "writingDirection": {
                        "closingMode": "다음 세척에서 무엇부터 할지 하나만 남긴다."
                    },
                }
            }
        )
        assert parsed is not None
        assert parsed.writing_direction is not None
        assert parsed.writing_direction.closing_mode.startswith("다음 세척")

    def test_an_old_response_without_the_field_still_parses(self):
        parsed = parsing.editorial_style_plan_from_json(
            {"editorialStylePlan": {"contentCategory": "LIFE_HOME"}}
        )
        assert parsed is not None
        assert parsed.writing_direction is None


class TestTheEditorialStyleCallAsksForExecutableSentences:
    def test_it_shows_a_bad_example_and_a_good_example(self):
        text = prompts.editorial_style_prompt(draft_input())
        assert "좋지 않은 예: '자연스럽고 다양하게 작성한다.'" in text
        assert "핵심 판단은 한두 문장 안에 먼저 제시하고" in text

    def test_it_requires_the_eleven_items_to_differ_from_each_other(self):
        text = prompts.editorial_style_prompt(draft_input())
        assert "11개가 서로 다른 것을 정해야 한다" in text

    def test_the_first_person_policy_is_left_to_the_model(self):
        """2026-08-03 사용자 결정: 경험 자료 유무로 1인칭 정책을 고정하지 않는다.

        예전에는 자료가 없으면 '1인칭 체험 표현을 쓰지 않는다'로, 있으면 '자료 범위로
        한정해 적는다'로 프롬프트가 답을 정해 줬다. 이제 어느 쪽도 강제하지 않는다.
        """
        for evidence in (
            None,
            ReferenceEvidenceProfile(has_references=True, has_user_experience_evidence=True),
        ):
            text = prompts.editorial_style_prompt(draft_input(reference_evidence=evidence))
            assert "1인칭을 어디까지 쓸지 한 줄로 적는다" in text
            assert "1인칭 체험 표현을 쓰지" not in text
            assert "자료 범위로 한정해 적는다" not in text


class TestTheDraftPromptExecutesThePlan:
    def test_the_directions_reach_the_draft(self):
        text = prompts.draft_prompt(draft_input(editorial_style=style_plan()))
        assert "이 글의 편집 지시" in text
        assert "부품이 바뀌는 지점에서" in text
        assert "이 글에서 특히 피할 것: 모든 섹션을 세척 절차로 끝내기" in text

    def test_the_opening_keeps_the_rotation_and_adds_this_article(self):
        text = prompts.draft_prompt(draft_input(editorial_style=style_plan()))
        assert "- 도입 방식(ANSWER_FIRST):" in text
        assert "이 글의 첫 문단: 물통을 비워도 냄새가 남는 상황에서 바로 시작한다." in text

    def test_the_closing_keeps_the_rotation_and_adds_what_it_leaves(self):
        text = prompts.draft_prompt(draft_input(editorial_style=style_plan()))
        # 형식(회전)은 그대로 한 줄이고, 내용이 같은 줄에 붙는다.
        assert text.count("- 결말 방식(") == 1
        assert "이 글의 결말이 남길 것: 다음 세척에서 무엇부터 할지 하나만 남긴다." in text

    def test_an_empty_item_does_not_become_an_empty_line(self):
        plan = style_plan(writing_direction=direction(transition_style="", detail_focus=""))
        text = prompts.draft_prompt(draft_input(editorial_style=plan))
        assert "문단·섹션 전환:" not in text
        assert "화자와 독자의 거리:" in text

    def test_an_old_plan_without_directions_still_builds_a_prompt(self):
        text = prompts.draft_prompt(draft_input(editorial_style=EditorialStylePlan()))
        assert "이 글의 편집 지시" not in text
        assert "- 결말 방식(" in text

    def test_the_common_rules_are_still_there(self):
        # 이 글의 지시가 하한선을 대체하지 않는다. 둘 다 있어야 한다.
        text = prompts.draft_prompt(draft_input(editorial_style=style_plan()))
        assert "자연스러운 원고의 조건" in text
        assert "다음 패턴이 나타나면 실패다" in text


class TestTheSectionDesignGoesBeyondHeadings:
    def test_the_schema_requires_the_six_items(self):
        items = schemas.CONTENT_PLAN_SCHEMA["properties"]["contentPlan"]["properties"]["sections"][
            "items"
        ]
        for field in SECTION_FIELDS:
            assert field in items["required"], field
            assert field in items["properties"], field

    def test_it_reads_them(self):
        plan = parsing.content_plan_from_json(
            {
                "contentPlan": {
                    "sections": [
                        {
                            "heading": f"소제목 {index}",
                            "question": f"질문 {index}",
                            "interpretation": "판단이 필요한 지점",
                            "omitBackground": "이미 말한 배경",
                            "connection": "앞 섹션과 이어지는 이유",
                            "lengthShare": "25~35%",
                            "personaDetail": "화자가 짚을 것",
                            "forbiddenClaims": ["자료 밖 수치"],
                        }
                        for index in range(3)
                    ]
                }
            }
        )
        assert plan is not None
        first = plan.sections[0]
        assert first.interpretation == "판단이 필요한 지점"
        assert first.length_share == "25~35%"
        assert first.forbidden_claims == ["자료 밖 수치"]

    def test_a_share_without_a_number_is_thrown_away(self):
        # 이 값은 분량 수정 지시가 참조한다. '적당히'로는 얼마나 늘릴지 계산할 수 없다.
        assert parsing._length_share_value("적당히") == ""
        assert parsing._length_share_value("길게 씁니다") == ""
        assert parsing._length_share_value("25~35%") == "25~35%"
        assert parsing._length_share_value("30%") == "30%"

    def test_an_old_response_without_the_items_still_yields_a_plan(self):
        plan = parsing.content_plan_from_json(
            {
                "contentPlan": {
                    "sections": [
                        {"heading": f"소제목 {index}", "question": f"질문 {index}"}
                        for index in range(3)
                    ]
                }
            }
        )
        assert plan is not None
        assert plan.sections[0].length_share == ""
        assert plan.sections[0].forbidden_claims == []

    def test_the_forbidden_claim_list_has_a_ceiling(self):
        plan = parsing.content_plan_from_json(
            {
                "contentPlan": {
                    "sections": [
                        {
                            "heading": f"소제목 {index}",
                            "question": f"질문 {index}",
                            "forbiddenClaims": [f"주장{n}" for n in range(10)],
                        }
                        for index in range(3)
                    ]
                }
            }
        )
        assert plan is not None
        assert len(plan.sections[0].forbidden_claims) == 4


class TestTheContentPlanCallAsksForThem:
    def test_it_lists_the_six_items(self):
        text = prompts.content_plan_prompt(draft_input())
        for label in ("interpretation", "omitBackground", "connection", "lengthShare",
                      "personaDetail", "forbiddenClaims"):
            assert label in text, label

    def test_it_forbids_giving_every_section_the_same_share(self):
        text = prompts.content_plan_prompt(draft_input())
        assert "모든 섹션에 같은 값을 주지 않는다" in text

    def test_it_rejects_answers_that_fit_any_section(self):
        text = prompts.content_plan_prompt(draft_input())
        assert "어느 섹션에나 붙는 문장은 쓰지 않는다" in text


class TestTheDesignBlockCarriesThemToTheDraft:
    def test_the_filled_items_become_lines(self):
        text = prompts.draft_prompt(draft_input(content_plan=content_plan()))
        assert "· 분량 비중: 30~40%" in text
        assert "· 앞 섹션과의 연결: 냄새의 출발점이 물통이라 여기서 시작한다" in text
        assert "· 이 섹션에서 하면 안 되는 주장: 구연산이 세균을 몇 % 없앤다" in text

    def test_an_empty_item_makes_no_line(self):
        plan = content_plan([section(interpretation="", persona_detail="", forbidden_claims=[])])
        text = prompts.draft_prompt(draft_input(content_plan=plan))
        assert "· 작성자 판단이 필요한 곳:" not in text
        assert "· 화자가 드러낼 디테일:" not in text
        assert "· 분량 비중: 30~40%" in text

    def test_the_share_is_stated_as_a_direction_not_a_target(self):
        text = prompts.draft_prompt(draft_input(content_plan=content_plan()))
        assert "분량 비중은 배분 방향이지 목표가 아니다" in text

    def test_an_old_plan_without_the_items_still_renders(self):
        plan = content_plan(
            [
                ContentPlanSection(
                    section_id="section-1",
                    heading="물통은 어떻게 관리하나",
                    question="물통을 어떻게 씻나",
                    purpose="해결 방법",
                )
            ]
        )
        text = prompts.draft_prompt(draft_input(content_plan=plan))
        assert "물통은 어떻게 관리하나" in text
        assert "· 분량 비중:" not in text
