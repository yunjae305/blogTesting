"""2026-08-05 미팅 점검표의 프롬프트 쪽 항목들.

- 4·5번: 대상 연령대가 제목에만 남지 않고 본문 구조와 관심축까지 내려간다
- 7번: AI 답변형 문구를 쓰지 못하게 막고, 나오면 잡는다
- 9번: 사용자가 준 자료가 웹 검색보다 우선한다 (이름이 겹치는 소재의 일반 해법)

프롬프트 문자열을 직접 보는 테스트다. 모델이 그 지시를 실제로 지키는지는 여기서 알 수
없다 — 지시가 **프롬프트에 실려 나가는지**만 고정한다. 그 줄이 조용히 빠지는 것이
지금까지 반복된 실패였다.
"""

from app.llm.prompts import (
    ASSISTANT_TONE_PHRASES,
    READER_AGE_GUIDES,
    READER_AGE_INTERESTS,
    age_focus,
    reader_age_keys,
    audience_guide,
    content_plan_prompt,
    draft_prompt,
    final_review_prompt,
    age_guide_lines,
    research_guide,
    title_plan_prompt,
)
from app.modules.draft.quality import check_draft
from app.shared import (
    BlogTaskInput,
    DraftFormat,
    DraftGenerationInput,
    FinalPost,
    ReferenceMaterial,
    ReferenceMaterialType,
    SearchSource,
    SelectedIntentForDraft,
)


def _draft_input(
    *,
    age: str | None = "20s",
    materials: list[ReferenceMaterial] | None = None,
    sources: list[SearchSource] | None = None,
) -> DraftGenerationInput:
    return DraftGenerationInput(
        post_id="post_1",
        user_id="user_1",
        prompt_version="m4-draft@v1.0",
        format=DraftFormat.MARKDOWN,
        input=BlogTaskInput(
            topic="아이오나",
            purpose=["정보 전달"],
            keywords=["정보 전달"],
            reader_age_range=age,
            reference_materials=materials or [],
        ),
        selected_intent=SelectedIntentForDraft(
            intent_id="i1",
            title="블로그 자동화를 처음 쓰는 사람을 위한 안내",
            target_reader="블로그를 막 시작한 사람",
            rationale="처음 쓰는 사람이 가장 많이 막히는 지점을 푼다",
            sources=sources or [],
        ),
    )


# 문단마다 다른 말을 하는 본문. 같은 문장을 반복해 채우면 '표현 반복' 검사에 걸려,
# 정작 보려는 신호(AI 답변형 문구)가 다른 실패에 묻힌다.
def _varied_body(paragraphs: int = 24) -> str:
    return "\n\n".join(
        f"## 소제목 {n}\n\n{n}번 문단입니다. 항목{n} 준비과정{n} 사례{n} 판단기준{n}을 다룹니다. "
        f"독자{n}는 단계{n} 확인사항{n} 선택지{n} 실행메모{n}를 얻습니다."
        for n in range(1, paragraphs + 1)
    )


def _post(body: str) -> FinalPost:
    return FinalPost(
        title="제목",
        body=body,
        hashtags=["a", "b", "c", "d", "e"],
        html_content=f"<article><h1>제목</h1><p>{body}</p></article>",
    )


# ------------------------------------------------------- 4·5 연령대 반영


def test_every_age_band_the_ui_offers_has_an_interest_axis():
    """화면(READER_AGE_RANGES)에서 고를 수 있는데 프롬프트가 모르는 연령대가 있으면,
    그 연령대만 조용히 예전처럼 동작한다."""
    # web/src/constants.ts의 READER_AGE_RANGES와 같은 키('전체'는 빈 문자열이라 제외).
    # 50대·60대 이상은 2026-08-05에 '50plus' 하나로 합쳤다.
    assert set(READER_AGE_INTERESTS) == {"10s", "20s", "30s", "40s", "50plus"}


def test_the_ages_that_were_merged_still_read_from_saved_posts():
    """선택지에서 사라졌다고 저장된 글이 연령대를 잃으면 안 된다."""
    assert reader_age_keys("50s") == ["50plus"]
    assert reader_age_keys("60plus") == ["50plus"]
    assert age_focus("50s") == age_focus("50plus")


def test_multiple_age_bands_are_written_around_what_they_share():
    """연령대별로 문단을 나눠 각각 설명하면 한 글이 여러 글로 쪼개진다."""
    assert reader_age_keys("20s,30s") == ["20s", "30s"]

    focus = age_focus("20s,30s")
    assert focus is not None
    interests, rule = focus
    # 두 연령대의 관심축이 모두 들어간다. 문구를 여기 박지 않는다 — 지침을 다듬을
    # 때마다 테스트가 깨지면, 지키려던 것(관심축이 실린다)이 아니라 문장을 지키게 된다.
    assert READER_AGE_GUIDES["20s"].interests in interests
    assert READER_AGE_GUIDES["30s"].interests in interests
    assert "겹치는 것" in rule
    assert "연령대별로 문단을 나눠" in rule


def test_an_unknown_or_duplicated_age_value_does_not_break_the_guide():
    # 목록에 없는 값은 버린다 — 모르는 값으로 관심축을 지어내지 않는다.
    assert reader_age_keys("70s") == []
    # 같은 값을 두 번 저장해도 한 번만 센다.
    assert reader_age_keys("20s,20s") == ["20s"]


def test_the_age_band_becomes_an_interest_axis_not_just_a_label():
    guide = audience_guide(_draft_input(age="20s"))

    # 관심축·예시 상황·말투·서술 규칙이 모두 실린다.
    twenties = READER_AGE_GUIDES["20s"]
    assert twenties.interests in guide
    assert twenties.situations in guide
    assert twenties.voice in guide
    assert twenties.example in guide
    for rule in twenties.rules:
        assert rule in guide

    # **읽는 사람의 나이**라고 말한다. 이걸 안 하면 모델이 화자의 나이로 읽어
    # 제목에 'N대의 시각으로 본 후기'가 나온다(2026-08-07 신고).
    assert "읽는 사람의 연령대" in guide
    assert "글쓴이의 나이가 아니라 독자의 나이다" in guide
    # 연령을 글에 적는 것으로 대신하지 못하게 한다.
    assert "독자의 연령대를 글에 직접 적지 않는다" in guide


def test_an_unspecified_age_band_forbids_inventing_one():
    """'전체'를 골랐는데 모델이 임의로 한 세대를 골라 쓰면 그것도 요구를 어긴 것이다."""
    guide = audience_guide(_draft_input(age=""))

    assert "특정 세대만 아는 표현" in guide
    assert READER_AGE_GUIDES["20s"].interests not in guide


def test_age_focus_ignores_values_it_does_not_know():
    assert age_focus(None) is None
    assert age_focus("") is None
    assert age_focus("70s") is None
    assert age_focus("20s") is not None


def test_both_the_structure_and_the_body_prompt_get_the_age_guidance():
    """설계 단계가 연령을 모르면 그 구조를 채우는 본문도 연령을 반영할 수 없다."""
    draft_input = _draft_input(age="30s")

    thirties = READER_AGE_GUIDES["30s"].interests
    assert thirties in content_plan_prompt(draft_input)
    assert thirties in draft_prompt(draft_input)


# ------------------------------------------------------- 7 AI 답변형 문구


def test_the_body_prompt_forbids_assistant_speak():
    prompt = draft_prompt(_draft_input())

    assert "확인되는 범위" in prompt
    assert "AI 답변" in prompt


def test_quality_flags_assistant_speak_on_a_single_occurrence():
    """상투구와 달리 '과다 사용'이 아니라 한 번만 나와도 지적한다."""
    body = "첫 문단입니다.\n\n확인되는 범위는 다음과 같습니다.\n\n" + ("본문 문단입니다. " * 200)

    report = check_draft(_post(body), hashtag_count=5, min_body_chars=100)

    assert any("AI 답변형 문구" in warning for warning in report.warnings)


def test_quality_stays_quiet_when_the_article_reads_like_a_person_wrote_it():
    body = "첫 문단입니다.\n\n" + ("사람이 쓴 문장입니다. " * 200)

    report = check_draft(_post(body), hashtag_count=5, min_body_chars=100)

    assert not any("AI 답변형 문구" in warning for warning in report.warnings)


def test_assistant_speak_never_costs_the_article():
    """문체 때문에 멀쩡한 사실을 담은 원고를 버리지는 않는다 — 경고이지 반려가 아니다."""
    body = "확인되는 범위는 다음과 같습니다.\n\n" + _varied_body()

    report = check_draft(_post(body), hashtag_count=5, min_body_chars=100)

    assert report.ok


def test_the_phrase_list_covers_what_the_meeting_reported():
    assert "확인되는 범위" in ASSISTANT_TONE_PHRASES
    assert "다음과 같습니다" in ASSISTANT_TONE_PHRASES


# ------------------------------------------------------- 9 자료 우선순위


def test_the_source_priority_rule_reaches_every_stage_that_writes():
    """설계·본문·검수가 같은 규칙을 봐야 한다. 한 곳이라도 빠지면 그 단계에서
    이름만 같은 다른 대상의 자료로 글이 세워진다."""
    draft_input = _draft_input(
        materials=[ReferenceMaterial(type=ReferenceMaterialType.URL, value="https://aiona.kr/")],
        sources=[SearchSource(title="자료", url="https://example.com/x", snippet="요약")],
    )
    marker = "사용자 자료가 맞다"

    assert marker in research_guide(draft_input)
    assert marker in draft_prompt(draft_input)
    assert marker in content_plan_prompt(draft_input)
    assert marker in final_review_prompt(draft_input, _post("본문입니다."))


def test_the_rule_is_written_for_any_subject_not_one_brand():
    """이름이 겹치는 소재는 앞으로도 계속 들어온다. 특정 브랜드를 코드에 적으면
    그 하나만 고쳐지고 나머지는 그대로다."""
    rule = research_guide(_draft_input())

    assert "동명이인" in rule or "이름이 같을 뿐인" in rule
    # 특정 브랜드명을 규칙 문장에 박아 두지 않는다.
    assert "아이오나" not in rule.split("자료 우선순위")[1]


def test_the_review_is_told_what_the_user_actually_chose():
    """검수가 '사용자가 정한 것'을 모르면 반영 누락(missing)을 판단할 수 없다."""
    prompt = final_review_prompt(_draft_input(age="20s"), _post("본문입니다."))

    assert "읽는 사람의 연령대: 20대" in prompt
    assert "이 나이대 독자가 궁금해하는 것" in prompt
    assert "사용자가 고른 글의 방향" in prompt
    assert "missing" in prompt
    assert "flow" in prompt
    assert "tone" in prompt


# --------------------------------------------- 2-1 검수 항목 여섯 가지


def test_the_review_covers_every_criterion_the_meeting_listed():
    """미팅 2-1이 적은 검수 항목 여섯 개가 모두 검수 지시에 있어야 한다.

    한 줄이 조용히 빠지면 그 항목만 검수되지 않고, 결과만 보고는 알 수 없다.
    """
    prompt = final_review_prompt(_draft_input(), _post("본문입니다."))

    # 1. 문장이 자연스러운지 / 2. 단락 간 연결
    assert "부자연스러운 문장" in prompt
    assert "앞뒤 문맥이 끊기는 문장" in prompt
    # 3. 소재와 무관한 내용
    assert "이 글에 있을 이유가 없는 내용" in prompt
    # 4. 제목에서 제시한 관점이 본문에 반영됐는지
    assert "확정 제목이 제시한 관점" in prompt
    assert "제목만 그렇게 달아 놓고 본문은 일반 소개" in prompt
    # 5. 이미지가 원고 내용과 관련 있는지
    assert "본문·자료와 맞지 않는 이미지" in prompt
    # 6. 사실관계가 불확실한 표현
    assert "사실처럼 단정한 진술" in prompt


# --------------------------------------- 2-2 다섯 가지를 두 단계 모두에


def test_both_stages_receive_every_input_the_meeting_listed():
    """미팅 2-2: 연령대·독자 대상·글의 목적·선택한 방향·사용자 입력 조건을
    **구조 설계와 원고 작성 단계 모두**에 전달해야 한다.

    설계에만 있으면 구조는 그 각도로 서는데 본문이 다른 글로 흐르고, 본문에만 있으면
    이미 정해진 구조 안에서 억지로 끼워 맞춘다.
    """
    draft_input = _draft_input(age="20s")
    intent = draft_input.selected_intent
    required = {
        "연령대 관심축": READER_AGE_GUIDES["20s"].interests,
        "독자 대상": intent.target_reader,
        "선택한 방향": intent.title,
        "방향을 고른 근거": intent.rationale,
        "글의 목적": "정보 전달",
        "사용자 입력 소재": "아이오나",
    }

    for stage, prompt in (
        ("구조 설계", content_plan_prompt(draft_input)),
        ("원고 작성", draft_prompt(draft_input)),
    ):
        for label, needle in required.items():
            assert needle in prompt, f"{stage} 프롬프트에 {label}이 없다"


# ------------------------------------- 연령은 독자의 나이지 글쓴이의 나이가 아니다


def test_the_chosen_age_is_the_readers_age_not_the_writers():
    """사용자가 고른 연령은 **읽는 사람**의 나이다.

    그것을 말하지 않았더니 모델이 화자의 나이로 읽어, 20대를 고른 글의 제목에
    "20대의 시각으로 본 후기"가 나왔다 — 글쓴이는 20대가 아닌데도(2026-08-07 신고).
    """
    draft_input = _draft_input(age="20s")

    for stage, prompt in (
        ("독자 안내", audience_guide(draft_input)),
        ("구조 설계", content_plan_prompt(draft_input)),
        ("원고 작성", draft_prompt(draft_input)),
        ("제목", title_plan_prompt(draft_input)),
        ("검수", final_review_prompt(draft_input, _post("본문입니다."))),
    ):
        assert "읽는 사람의 연령대" in prompt, f"{stage}가 누구의 나이인지 말하지 않는다"
        assert "글쓴이의 나이" in prompt, f"{stage}가 글쓴이의 나이와 구분하지 않는다"


def test_the_title_may_not_say_the_readers_age():
    """제목에 나이를 적으면 글쓴이가 그 나이인 것처럼 읽힌다 — 신고된 증상 그대로다."""
    prompt = title_plan_prompt(_draft_input(age="20s"))

    assert "제목에 독자의 연령을 적지 않는다" in prompt
    assert "20대의 시각으로" in prompt  # 하면 안 되는 예로 프롬프트에 적혀 있다


def test_every_age_has_a_full_guide():
    """화면에서 고를 수 있는 연령대에 지침이 비어 있으면, 그 연령만 조용히 밋밋해진다."""
    assert set(READER_AGE_GUIDES) == {"10s", "20s", "30s", "40s", "50plus"}
    for key, guide in READER_AGE_GUIDES.items():
        assert guide.interests and guide.situations and guide.voice and guide.example, key
        assert len(guide.rules) >= 5, f"{key}의 서술 규칙이 너무 적다"
        assert guide.sentence and guide.order and guide.persuasion and guide.action, key


def test_no_two_ages_are_written_the_same_way():
    """연령대별로 **확실하게** 달라야 한다(2026-08-07 사용자 요청).

    규칙 목록만 주면 연령끼리 겹치는 줄이 생겨 결과가 비슷해진다. 가이드의 공통 지침
    3번이 말한 조정 축(단어·순서·설득 기준·행동)은 **연령마다 값이 달라야 한다** —
    같은 값을 두 연령에 쓰면 여기서 깨진다.
    """
    axes = ("sentence", "order", "persuasion", "action", "voice", "example", "interests")
    for axis in axes:
        values = [getattr(guide, axis) for guide in READER_AGE_GUIDES.values()]
        assert len(set(values)) == len(values), f"{axis}가 두 연령에서 같다"


def test_the_prompt_says_how_this_age_differs():
    """축이 프롬프트에 실제로 실려야 모델이 다르게 쓴다."""
    for key, guide in READER_AGE_GUIDES.items():
        guide_text = "\n".join(age_guide_lines(key))
        for axis, value in (
            ("단어와 문장", guide.sentence),
            ("펼치는 순서", guide.order),
            ("설득하는 기준", guide.persuasion),
            ("읽고 나서 할 일", guide.action),
        ):
            assert value in guide_text, f"{key}의 {axis}가 프롬프트에 없다"
