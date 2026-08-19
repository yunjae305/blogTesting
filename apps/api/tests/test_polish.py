"""M4 5단계 문장 다듬기: 어떤 교정을 원고에 넣고 어떤 것을 버리는가.

모델 호출은 여기 없다. 다듬기 모델이 무엇을 돌려주든 **그것을 원고에 어떻게 반영하는가**만
본다 — 이 단계는 사실을 건드리면 안 되는 자리라, 무엇을 막는지가 무엇을 고치는지만큼
중요하다.

시나리오는 2026-08-05 작업 지시서의 여섯 가지를 그대로 따른다:
AI 답변형 문구 / 같은 어미 반복 / 체험 자료 없는 후기 / 체험 자료가 있는 글 /
가격·수치가 든 글 / 이미지 배치 표식이 든 글.
"""

from app.llm.parsing import polish_edits_from_json
from app.llm.prompts import polish_prompt
from app.modules.draft.polish import (
    REJECT_FAKE_EXPERIENCE,
    REJECT_HEADING,
    REJECT_KEYWORD_DROPPED,
    REJECT_NOT_FOUND,
    REJECT_NUMBER_CHANGED,
    REJECT_STRUCTURE,
    REJECT_TOO_LONG,
    apply_polish,
)
from app.shared import (
    BlogTaskInput,
    DraftFormat,
    DraftGenerationInput,
    DraftGenerationSettings,
    FinalPost,
    PolishEdit,
    ReferenceMaterial,
    ReferenceMaterialType,
    SelectedIntentForDraft,
    SeoKeywordPlan,
)


def post(body: str, **overrides) -> FinalPost:
    """같은 글을 세 벌로 들고 있는 완성 원고. 셋 중 하나만 고쳐지면 안 된다."""
    defaults = dict(
        title="공기청정기 고르는 법",
        body=body,
        hashtags=["공기청정기"],
        html_content=f"<article><h1>공기청정기 고르는 법</h1><p>{body}</p></article>",
        markdown_content=f"# 공기청정기 고르는 법\n\n{body}",
    )
    return FinalPost(**{**defaults, **overrides})


def edit(before: str, after: str, kind: str = "assistant_tone") -> PolishEdit:
    return PolishEdit(kind=kind, reason="어색합니다", before=before, after=after)


def draft_input(materials: list[ReferenceMaterial] | None = None) -> DraftGenerationInput:
    return DraftGenerationInput(
        post_id="post_1",
        user_id="user_1",
        input=BlogTaskInput(
            topic="공기청정기",
            keywords=[],
            purpose=["후기·리뷰 작성"],
            target_reader="1인 가구",
            reference_materials=materials or [],
        ),
        selected_intent=SelectedIntentForDraft(
            intent_id="i1",
            title="1인 가구용 공기청정기 고르는 기준",
            target_reader="1인 가구",
            rationale="원룸 기준이 필요합니다",
        ),
        prompt_version="m4-draft@v1.0",
        format=DraftFormat.MARKDOWN,
        settings=DraftGenerationSettings(hashtag_count=5),
    )


# ------------------------------------------------- 1. AI 답변형 문구가 든 글


def test_an_assistant_style_sentence_is_replaced_in_all_three_copies():
    """'확인되는 범위는 이렇습니다'는 블로그 문장이 아니라 AI가 사람에게 하는 답변이다."""
    body = "확인되는 범위는 이렇습니다. 원룸에서는 소음이 먼저 걸립니다."
    polished, judged = apply_polish(
        post(body),
        [edit("확인되는 범위는 이렇습니다.", "찾아보니 판단 기준은 결국 두 가지였습니다.")],
    )

    assert judged[0].applied is True
    assert "확인되는 범위" not in polished.body
    assert "판단 기준은 결국 두 가지였습니다." in polished.body
    # 화면과 발행본이 다른 말을 하면 안 된다.
    assert "확인되는 범위" not in polished.html_content
    assert "확인되는 범위" not in polished.markdown_content


def test_an_assistant_style_sentence_can_be_dropped_outright():
    """군더더기 한 줄은 고치는 것보다 빼는 것이 자연스러울 때가 있다. 숫자가 없는
    문장이므로 삭제해도 잃는 사실이 없다."""
    body = "정리하면 다음과 같습니다. 원룸에서는 소음이 먼저 걸립니다."
    polished, judged = apply_polish(post(body), [edit("정리하면 다음과 같습니다. ", "")])

    assert judged[0].applied is True
    assert polished.body == "원룸에서는 소음이 먼저 걸립니다."


# ------------------------------------------------------- 2. 같은 어미 반복


def test_repeated_sentence_endings_are_varied_without_touching_the_rest():
    """세 문장이 같은 어미로 끝나면 기계가 쓴 글로 읽힌다. 고치는 것은 그 문장뿐이다.

    다양화는 **같은 문체 안에서** 한다(2026-08-07): '~입니다' 반복을 명사형 종결로
    푸는 것은 되지만, '~다'·'~요'로 문체를 갈아타는 것은 문체 변경으로 버려진다.
    """
    body = (
        "소음은 27데시벨입니다. 필터는 3단계입니다. 전기료는 월 2천원입니다."
        " 원룸이라면 이 정도가 무난합니다."
    )
    polished, judged = apply_polish(
        post(body),
        [
            edit("필터는 3단계입니다.", "필터는 3단계 구성.", kind="repetition"),
            edit("전기료는 월 2천원입니다.", "전기료도 월 2천원 수준.", kind="repetition"),
        ],
    )

    assert [e.applied for e in judged] == [True, True]
    assert "필터는 3단계 구성." in polished.body
    assert "전기료도 월 2천원 수준." in polished.body
    # 손대지 않은 문장은 그대로다 — 다듬기는 원고를 다시 쓰는 일이 아니다.
    assert "소음은 27데시벨입니다." in polished.body
    assert "원룸이라면 이 정도가 무난합니다." in polished.body


# ------------------------------------- 3. 사용자가 체험 정보를 주지 않은 후기 글


def test_a_fabricated_experience_is_rejected_when_the_user_gave_none():
    """다듬기에서 가장 자연스러운 문장은 겪어 본 사람의 문장이다. 겪은 적이 없으면
    그것은 조작이므로, 프롬프트로 부탁하는 데 그치지 않고 코드가 막는다."""
    body = "소음은 27데시벨로 표기돼 있습니다."
    polished, judged = apply_polish(
        post(body),
        [edit("소음은 27데시벨로 표기돼 있습니다.", "직접 사용해 보니 27데시벨이 맞았습니다.")],
        allow_experience=False,
    )

    assert judged[0].applied is False
    assert judged[0].rejected_rule == REJECT_FAKE_EXPERIENCE
    assert polished.body == body


def test_every_shape_of_invented_firsthand_claim_is_rejected():
    """문구 목록만으로는 부족하다 — 모델은 목록에 없는 변형을 만들어 낸다."""
    body = "매장 진열 정보는 공식 안내에 나와 있습니다."
    for after in (
        "제가 방문했을 때 매장에 진열돼 있었습니다.",
        "현장에서 확인해 보니 진열돼 있었습니다.",
        "실제로 구매해서 써봤는데 진열 상태가 좋았습니다.",
        "영화를 보고 나서 느낀 점은 진열이 인상적이라는 것입니다.",
    ):
        _, judged = apply_polish(post(body), [edit(body, after)], allow_experience=False)
        assert judged[0].rejected_rule == REJECT_FAKE_EXPERIENCE, after


def test_telling_the_reader_to_go_check_is_not_a_fabricated_experience():
    """'직접 확인해 보세요'는 독자에게 권하는 말이지 화자가 겪었다는 말이 아니다.
    이걸 막으면 멀쩡한 문장이 다듬어지지 않고 남는다."""
    body = "정확한 재고는 판매처 확인이 필요하다는 안내가 있습니다."
    polished, judged = apply_polish(
        post(body),
        [edit(body, "재고는 판매처에서 직접 확인해 보세요.", kind="report_tone")],
        allow_experience=False,
    )

    assert judged[0].applied is True
    assert "직접 확인해 보세요" in polished.body


def test_an_existing_experience_sentence_can_still_be_rewritten_away():
    """원고가 이미 들고 있던 체험 문장은 **고쳐야 할 대상**이다. 지어낸 것이 아니라
    지우는 쪽이므로 막지 않는다."""
    body = "직접 사용해 보니 소음이 적었습니다."
    polished, judged = apply_polish(
        post(body),
        [edit(body, "공개된 사양 기준으로는 소음이 낮은 편입니다.", kind="fake_experience")],
        allow_experience=False,
    )

    assert judged[0].applied is True
    assert "직접 사용해 보니" not in polished.body


# --------------------------------------- 4. 실제 사용자 후기 메모가 제공된 글


def test_experience_wording_survives_when_the_user_actually_gave_a_memo():
    """사용자가 겪은 것을 적어 줬으면 그 말투를 굳이 지우지 않는다 — 없는 경험을
    만들지 말라는 규칙이지, 있는 경험을 지우라는 규칙이 아니다."""
    body = "원룸에서 밤새 틀어 두니 소음이 거슬리지 않았습니다."
    polished, judged = apply_polish(
        post(body),
        [edit(body, "원룸에서 밤새 틀어 두니 소음은 거의 신경 쓰이지 않았습니다.", kind="awkward")],
        allow_experience=True,
    )

    assert judged[0].applied is True
    assert "밤새 틀어 두니" in polished.body


def test_the_prompt_tells_the_model_whether_experience_material_exists():
    """이 한 줄이 갈리면 같은 원고에서 전혀 다른 문장이 나온다. 프롬프트에 실제로
    실리는지 확인한다."""
    memo = ReferenceMaterial(
        type=ReferenceMaterialType.TEXT, value="제가 한 달 써보니 소음이 적었습니다"
    )
    with_memo = polish_prompt(
        draft_input([memo]), post("본문"), has_experience_material=True
    )
    without_memo = polish_prompt(draft_input(), post("본문"), has_experience_material=False)

    assert "사용자 경험 자료: 있음" in with_memo
    assert "제가 한 달 써보니" in with_memo
    assert "사용자 경험 자료: **없음**" in without_memo
    assert "정보형·관찰형" in without_memo


# ----------------------------------------------- 5. 가격과 수치가 포함된 글


def test_a_price_can_never_change_while_polishing():
    """표현을 다듬다가 값이 달라지면 그것은 다듬기가 아니라 사실 편집이다."""
    body = "가격은 3만원입니다."
    polished, judged = apply_polish(post(body), [edit(body, "가격은 2만원 정도예요.")])

    assert judged[0].rejected_rule == REJECT_NUMBER_CHANGED
    assert polished.body == body


def test_a_new_number_cannot_be_smuggled_in():
    body = "필터 교체 주기는 사용 환경에 따라 다릅니다."
    _, judged = apply_polish(post(body), [edit(body, "필터는 6개월마다 갈면 됩니다.")])

    assert judged[0].rejected_rule == REJECT_NUMBER_CHANGED


def test_a_sentence_that_carries_a_number_is_never_deleted():
    """숫자가 든 문장을 통째로 빼면 사실이 조용히 사라진다."""
    body = "소음은 27데시벨입니다. 원룸에서는 이 정도면 조용한 편입니다."
    _, judged = apply_polish(post(body), [edit("소음은 27데시벨입니다. ", "")])

    assert judged[0].rejected_rule == REJECT_NUMBER_CHANGED


def test_the_same_number_written_with_a_comma_is_the_same_number():
    """'1,200'과 '1200'은 표기 차이일 뿐이다. 이걸 다르게 보면 멀쩡한 교정이 막힌다."""
    body = "한 달 전기료는 1,200원입니다."
    polished, judged = apply_polish(
        post(body), [edit(body, "한 달 전기료는 1200원 남짓입니다.")]
    )

    assert judged[0].applied is True
    assert "1200원" in polished.body


# --------------------------------------- 6. 이미지 배치 표식이 포함된 글


def test_a_line_holding_an_image_marker_is_left_alone():
    """이미지 자리 표식이 든 줄을 문장처럼 갈아 끼우면 그림이 사라진다."""
    body = "[[IMAGE: a quiet living room | alt=거실에 놓인 공기청정기]]"
    _, judged = apply_polish(post(body), [edit(body, "거실에 두면 이렇게 보입니다.")])

    assert judged[0].rejected_rule == REJECT_STRUCTURE


def test_images_stay_where_they_were_after_a_neighbouring_sentence_is_polished():
    """사진 바로 옆 문장을 고쳐도 사진의 자리와 순서는 그대로여야 한다."""
    body = "확인되는 범위는 이렇습니다. 원룸에서는 소음이 먼저 걸립니다."
    html = (
        "<article><h1>공기청정기 고르는 법</h1>"
        "<p>확인되는 범위는 이렇습니다.</p>"
        '<figure class="blog-media"><img src="data:image/png;base64,AAA" alt="사진" /></figure>'
        "<p>원룸에서는 소음이 먼저 걸립니다.</p></article>"
    )
    markdown = (
        "# 공기청정기 고르는 법\n\n확인되는 범위는 이렇습니다.\n\n"
        "![사진](data:image/png;base64,AAA)\n\n원룸에서는 소음이 먼저 걸립니다."
    )
    polished, judged = apply_polish(
        post(body, html_content=html, markdown_content=markdown),
        [edit("확인되는 범위는 이렇습니다.", "찾아보니 기준은 두 가지였습니다.")],
    )

    assert judged[0].applied is True
    assert 'src="data:image/png;base64,AAA"' in polished.html_content
    assert "![사진](data:image/png;base64,AAA)" in polished.markdown_content
    # 사진은 고친 문장과 다음 문단 **사이**에 그대로 있다.
    assert polished.html_content.index("찾아보니") < polished.html_content.index("base64,AAA")
    assert polished.html_content.index("base64,AAA") < polished.html_content.index("소음이 먼저")


def test_a_table_row_or_html_fragment_is_never_taken_as_a_sentence():
    for before in ("| 모델 | 소음 |", "<p>본문입니다.</p>", "![사진](data:image/png;base64,AAA)"):
        _, judged = apply_polish(post(before), [edit(before, "다듬은 문장입니다.")])
        assert judged[0].rejected_rule == REJECT_STRUCTURE, before


# ------------------------------------------------------------- 그 밖의 규칙


def test_headings_are_off_limits():
    """소제목은 SEO와 목차의 뼈대다. 표현이 어색해도 이 단계에서 바꾸지 않는다."""
    body = "## 소음은 얼마나 되나"
    _, judged = apply_polish(post(body), [edit(body, "## 소음, 실제로는 어느 정도일까")])

    assert judged[0].rejected_rule == REJECT_HEADING


def test_an_seo_keyword_cannot_be_polished_away():
    body = "공기청정기 소음은 생각보다 중요합니다."
    _, judged = apply_polish(
        post(body),
        [edit(body, "이 제품 소음은 생각보다 중요합니다.")],
        keywords=("공기청정기",),
    )

    assert judged[0].rejected_rule == REJECT_KEYWORD_DROPPED


def test_spacing_differences_do_not_count_as_dropping_a_keyword():
    """'공기 청정기'와 '공기청정기'는 검색자에게 같은 말이다."""
    body = "공기청정기 소음은 생각보다 중요합니다."
    _, judged = apply_polish(
        post(body),
        [edit(body, "공기 청정기 소음은 생각보다 중요합니다.")],
        keywords=("공기청정기",),
    )

    assert judged[0].applied is True


def test_an_edit_that_grows_into_a_new_paragraph_is_rejected():
    """다듬기는 문장을 바꾸는 일이지 덧붙이는 일이 아니다."""
    body = "소음이 신경 쓰입니다."
    _, judged = apply_polish(
        post(body),
        [
            edit(
                body,
                "소음이 신경 쓰입니다. 특히 원룸이라면 밤에 더 크게 들리고, 잠자리에서는"
                " 그 차이가 훨씬 분명하게 느껴집니다.",
            )
        ],
    )

    assert judged[0].rejected_rule == REJECT_TOO_LONG


def test_a_sentence_the_model_did_not_copy_exactly_is_reported_not_swallowed():
    """모델이 문장을 옮겨 적으며 바꿨다. 조용히 버리면 왜 안 고쳐졌는지 남지 않는다."""
    polished, judged = apply_polish(
        post("원룸에서는 소음이 먼저 걸립니다."),
        [edit("원고에 없는 문장입니다.", "다듬은 문장입니다.")],
    )

    assert judged[0].rejected_rule == REJECT_NOT_FOUND
    assert polished.body == "원룸에서는 소음이 먼저 걸립니다."


def test_a_correction_is_refused_rather_than_applied_to_only_some_copies():
    """html에서 문장을 못 찾으면 아무것도 고치지 않는다(검수와 같은 계약)."""
    original = post(
        "원룸에서는 소음이 먼저 걸립니다.", html_content="<article><p>전혀 다른 문장.</p></article>"
    )
    polished, judged = apply_polish(
        original, [edit("원룸에서는 소음이 먼저 걸립니다.", "원룸이라면 소음부터 걸립니다.")]
    )

    assert judged[0].rejected_rule == REJECT_NOT_FOUND
    assert polished.body == original.body


# --------------------------------------------------------------- 응답 파싱


def test_parsing_drops_edits_that_cannot_be_acted_on():
    parsed = polish_edits_from_json(
        {
            "edits": [
                {
                    "kind": "assistant_tone",
                    "reason": "AI 말투",
                    "before": "확인되는 범위는 이렇습니다.",
                    "after": "찾아보니 이렇습니다.",
                },
                # 고칠 자리가 없다 → 버린다
                {"kind": "hedge", "reason": "군더더기", "before": "  ", "after": "무엇이든"},
                # 바뀐 것이 없다 → 버린다
                {"kind": "awkward", "reason": "없음", "before": "같은 문장", "after": "같은 문장"},
                # 모르는 종류 → 버린다
                {"kind": "vibes", "reason": "느낌", "before": "문장", "after": "다른 문장"},
            ]
        }
    )

    assert [e.kind for e in parsed] == ["assistant_tone"]
    # 판정은 아직 붙지 않았다 — 그것은 원고와 대조하는 apply_polish의 몫이다.
    assert parsed[0].applied is False and parsed[0].rejected_rule is None


def test_a_broken_response_is_read_as_nothing_to_polish():
    """다듬기는 원고 생성의 관문이 아니다. 응답이 이상하면 '고칠 것 없음'이 맞다."""
    assert polish_edits_from_json(None) == []
    assert polish_edits_from_json({"edits": "nope"}) == []
    assert polish_edits_from_json({}) == []


# ----------------------------------------------------------------- 프롬프트


def test_the_prompt_carries_what_the_stage_needs_to_judge_a_sentence():
    """지시서가 요구한 입력(제목·소재·목적·페르소나·독자·자료·키워드·금지 표현)이
    실제로 프롬프트에 실리는지."""
    prompt = polish_prompt(
        draft_input().model_copy(
            update={
                "seo_keyword_plan": SeoKeywordPlan(
                    primary="공기청정기 추천", secondary=["원룸 공기청정기"]
                )
            }
        ),
        post("원룸에서는 소음이 먼저 걸립니다."),
        has_experience_material=False,
    )

    assert "공기청정기 고르는 법" in prompt  # 제목
    assert "소재: 공기청정기" in prompt
    assert "후기·리뷰 작성" in prompt  # 목적
    assert "1인 가구" in prompt  # 대상 독자
    assert "페르소나" in prompt
    assert "공기청정기 추천" in prompt and "원룸 공기청정기" in prompt  # 유지할 키워드
    assert "원룸에서는 소음이 먼저 걸립니다." in prompt  # 원본 원고
    # 금지 표현과 절대 규칙
    assert "확인되는 범위" in prompt
    assert "정확하지 않을 수 있습니다" in prompt
    assert "본 글에서는" in prompt
    assert "before에 있던" in prompt
    assert "[[IMAGE:" in prompt


def test_the_prompt_refuses_to_turn_uncertainty_into_certainty():
    """책임 회피 문구를 떼는 것과 없는 확신을 만드는 것은 다르다. 이 구분이 프롬프트에서
    빠지면 '정확하지 않을 수 있습니다'가 그냥 단정문이 된다."""
    prompt = polish_prompt(draft_input(), post("본문"), has_experience_material=False)

    assert "단정문으로 바꾸지 않는다" in prompt
    assert "근거가 어디까지인지" in prompt


# ------------------------------------------------- 7. 종결 문체가 갈라지는 교정

# 2026-08-07 사용자 신고: 완성 글이 처음에는 '~다'로 끝나는데 뒤로 갈수록 '~요'로
# 가벼워졌다. 다듬기가 '~습니다' 문장을 '~요'로 바꿔 놓은 것이 원인이었다 — 손댄
# 문장만 말투가 달라진다. 그래서 원고의 지배 문체에서 멀어지는 교정은 버린다.


def test_a_formal_sentence_cannot_be_polished_into_casual_tone():
    """'~습니다' 원고에 '~요' 문장을 심는 교정은 다듬기가 아니라 말투 변경이다."""
    from app.modules.draft.polish import REJECT_TONE_SHIFT

    body = (
        "원룸에서는 소음이 먼저 걸립니다. "
        "필터 교체 주기도 함께 확인해야 합니다. "
        "전기요금은 표시 소비전력으로 따져 봅니다."
    )
    polished, judged = apply_polish(
        post(body),
        [edit("필터 교체 주기도 함께 확인해야 합니다.", "필터 교체 주기도 함께 확인해야 해요.")],
    )

    assert judged[0].applied is False
    assert judged[0].rejected_rule == REJECT_TONE_SHIFT
    assert "확인해야 합니다." in polished.body
    assert "확인해야 해요" not in polished.body


def test_a_stray_casual_sentence_can_be_fixed_toward_the_dominant_tone():
    """반대 방향은 허용한다: 원고 대부분이 '~습니다'인데 혼자 '~요'인 문장을 원고 쪽
    문체로 맞추는 것이야말로 이 단계가 할 일이다."""
    body = (
        "원룸에서는 소음이 먼저 걸립니다. "
        "필터 교체 주기도 함께 봐야 해요. "
        "전기요금은 표시 소비전력으로 따져 봅니다."
    )
    polished, judged = apply_polish(
        post(body),
        [edit("필터 교체 주기도 함께 봐야 해요.", "필터 교체 주기도 함께 확인해야 합니다.")],
    )

    assert judged[0].applied is True
    assert "봐야 해요" not in polished.body
    assert "확인해야 합니다." in polished.body


def test_the_prompt_forbids_switching_the_sentence_ending_style():
    """코드 검사와 별개로, 프롬프트가 먼저 문체 유지를 지시해야 거절이 예외적 사건이 된다."""
    prompt = polish_prompt(draft_input(), post("본문"), has_experience_material=False)

    assert "종결 문체를 바꾸지 않는다" in prompt
