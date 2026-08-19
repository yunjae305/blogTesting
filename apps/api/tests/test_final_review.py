"""M4 4단계 최종 검수: 지적을 완성 원고에 반영하는 규칙.

모델 호출은 여기 없다. 검수 모델이 무엇을 돌려주든 그것을 **원고에 어떻게 반영하는가**만
본다 — 그 부분이 사용자가 실제로 받는 글을 정한다.
"""


from app.llm.parsing import final_review_issues_from_json
from app.modules.draft.final_review import (
    apply_review,
    apply_text_issue,
    remove_images,
)
from app.shared import FinalPost, FinalReviewIssue, GeneratedPostImage

NOW = "1970-01-01T00:00:00.000Z"


def image(data_url: str, alt: str = "사진") -> GeneratedPostImage:
    return GeneratedPostImage(
        data_url=data_url,
        alt_text=alt,
        prompt="a photo",
        provider="stub",
        model="stub",
        generated_at=NOW,
        mime_type="image/png",
        source="generated",
    )


def post(**overrides) -> FinalPost:
    body = "첫 문장입니다. 가격은 3만원입니다. 마지막 문장입니다."
    defaults = dict(
        title="제목",
        body=body,
        hashtags=["a"],
        html_content=f"<article><h1>제목</h1><p>{body}</p></article>",
        markdown_content=f"# 제목\n\n{body}",
    )
    return FinalPost(**{**defaults, **overrides})


def issue(**overrides) -> FinalReviewIssue:
    defaults = dict(
        kind="fact",
        severity="critical",
        reason="자료의 가격과 다르다",
        quote="가격은 3만원입니다.",
        replacement="가격은 2만원입니다.",
        image_index=None,
    )
    return FinalReviewIssue(**{**defaults, **overrides})


# --------------------------------------------------------------- 본문 교정


def test_a_correction_lands_in_all_three_copies_of_the_article():
    """FinalPost는 같은 글을 body·html·markdown 세 벌로 들고 있다. 한 벌만 고치면
    화면에서는 고쳐진 글을 보면서 안 고쳐진 글을 발행하게 된다."""
    fixed = apply_text_issue(post(), issue())

    assert fixed is not None
    assert "2만원" in fixed.body and "3만원" not in fixed.body
    assert "2만원" in fixed.html_content and "3만원" not in fixed.html_content
    assert "2만원" in fixed.markdown_content and "3만원" not in fixed.markdown_content


def test_a_correction_is_refused_rather_than_applied_to_only_some_copies():
    """html에서 문장을 못 찾으면 아무것도 고치지 않는다. 반쪽만 고친 원고가 가장 나쁘다."""
    mismatched = post(html_content="<article><h1>제목</h1><p>전혀 다른 문장.</p></article>")

    assert apply_text_issue(mismatched, issue()) is None


def test_a_sentence_broken_up_by_inline_markup_is_still_found():
    """모델은 본문(평문)을 보고 인용하는데, 발행본에는 그 문장 한가운데에 <strong>이 있다.
    글자만 맞으면 태그는 건너뛰고 찾아야 한다 — 안 그러면 강조된 문장은 영영 못 고친다."""
    marked = post(
        html_content="<article><h1>제목</h1><p>첫 문장입니다. 가격은 <strong>3만원</strong>입니다. 마지막 문장입니다.</p></article>"
    )

    fixed = apply_text_issue(marked, issue())

    assert fixed is not None
    assert "가격은 2만원입니다." in fixed.html_content
    assert "3만원" not in fixed.html_content


def test_an_empty_replacement_deletes_the_sentence():
    fixed = apply_text_issue(post(), issue(replacement=""))

    assert fixed is not None
    assert "3만원" not in fixed.body
    # 지운 자국이 남으면 안 된다 — 공백 두 칸이나 빈 줄이 그대로 발행된다.
    assert "  " not in fixed.body


def test_only_the_first_occurrence_is_replaced():
    """같은 문장이 두 번 나오면 지적된 것이 어느 쪽인지 알 수 없다. 둘 다 바꾸면
    지적하지 않은 곳까지 건드리게 된다."""
    twice = "가격은 3만원입니다. 이어지는 설명입니다. 가격은 3만원입니다."
    fixed = apply_text_issue(
        post(
            body=twice,
            html_content=f"<article><p>{twice}</p></article>",
            markdown_content=twice,
        ),
        issue(),
    )

    assert fixed is not None
    assert fixed.body.count("2만원") == 1
    assert fixed.body.count("3만원") == 1


# --------------------------------------------------------------- 이미지 제외


def test_a_mismatched_image_is_dropped_from_every_surface():
    """images 목록에서만 빼면 발행본(html)에는 사진이 그대로 남는다."""
    keep, drop = image("data:image/png;base64,KEEP"), image("data:image/png;base64,DROP")
    html = (
        "<article>"
        f'<figure class="blog-media"><img src="{keep.data_url}" alt="사진" /></figure>'
        f'<figure class="blog-media"><img src="{drop.data_url}" alt="사진" /></figure>'
        '<p class="visual-caption"><em>출처: 어딘가</em></p>'
        "</article>"
    )
    original = post(
        images=[keep, drop],
        featured_image=keep,
        html_content=html,
        markdown_content=f"![사진]({keep.data_url})\n\n![사진]({drop.data_url})\n*출처: 어딘가*",
    )

    updated, removed = remove_images(original, {1})

    assert removed == 1
    assert [i.data_url for i in updated.images or []] == [keep.data_url]
    assert drop.data_url not in updated.html_content
    assert drop.data_url not in updated.markdown_content
    # 사진 없는 캡션만 남으면 안 된다.
    assert "출처: 어딘가" not in updated.html_content
    assert keep.data_url in updated.html_content


def test_dropping_the_cover_promotes_the_next_image():
    """대표 이미지는 네이버가 집어 가는 자리다. 빼고 비워 두지 않는다."""
    cover, second = image("data:image/png;base64,COVER"), image("data:image/png;base64,SECOND")
    original = post(
        images=[cover, second],
        featured_image=cover,
        html_content=(
            f'<figure><img src="{cover.data_url}" /></figure>'
            f'<figure><img src="{second.data_url}" /></figure>'
        ),
    )

    updated, removed = remove_images(original, {0})

    assert removed == 1
    assert updated.featured_image is not None
    assert updated.featured_image.data_url == second.data_url


def test_an_image_index_outside_the_list_removes_nothing():
    original = post(images=[image("data:image/png;base64,ONE")])

    updated, removed = remove_images(original, {7})

    assert removed == 0
    assert len(updated.images or []) == 1


# --------------------------------------------------------------- 전체 적용


def test_minor_issues_never_touch_the_article():
    """어감·취향 차이로 완성된 원고를 고치면 잃는 것이 더 많다."""
    updated, applied, removed, unapplied, targets = apply_review(
        post(), [issue(severity="minor")]
    )

    assert applied == 0 and removed == 0
    assert updated.body == post().body
    # minor는 반영 실패가 아니므로 '반영하지 못한 지적'에도 들어가지 않는다.
    assert unapplied == []


def test_an_issue_whose_quote_is_not_in_the_article_is_reported_not_swallowed():
    """모델이 문장을 지어냈거나 옮겨 적으며 바꿨다. 조용히 버리면 왜 안 고쳐졌는지
    아무 데도 남지 않는다."""
    updated, applied, removed, unapplied, targets = apply_review(
        post(), [issue(quote="원고에 없는 문장입니다.")]
    )

    assert applied == 0
    assert updated.body == post().body
    assert len(unapplied) == 1


def test_text_and_image_issues_are_applied_together():
    drop = image("data:image/png;base64,DROP")
    original = post(
        images=[drop],
        featured_image=drop,
        html_content=(
            "<article><p>첫 문장입니다. 가격은 3만원입니다. 마지막 문장입니다.</p>"
            f'<figure><img src="{drop.data_url}" /></figure></article>'
        ),
    )

    updated, applied, removed, unapplied, targets = apply_review(
        original,
        [issue(), issue(kind="image", quote="", replacement="", image_index=0)],
    )

    assert applied == 1
    assert removed == 1
    assert unapplied == []
    assert "2만원" in updated.body
    assert not updated.images


# --------------------------------------------------------------- 응답 파싱


def test_parsing_drops_issues_that_cannot_be_acted_on():
    """고칠 자리를 못 찾는 지적은 남겨도 아무 일도 못 한다."""
    parsed = final_review_issues_from_json(
        {
            "issues": [
                # 정상
                {
                    "kind": "fact",
                    "severity": "critical",
                    "reason": "자료와 다름",
                    "quote": "가격은 3만원입니다.",
                    "replacement": "가격은 2만원입니다.",
                    "imageIndex": None,
                },
                # quote가 없는 본문 지적 → 버린다
                {
                    "kind": "unsupported",
                    "severity": "critical",
                    "reason": "근거 없음",
                    "quote": "  ",
                    "replacement": "",
                    "imageIndex": None,
                },
                # 번호가 없는 이미지 지적 → 버린다
                {
                    "kind": "image",
                    "severity": "critical",
                    "reason": "무관",
                    "quote": "",
                    "replacement": "",
                    "imageIndex": None,
                },
                # 모르는 종류 → 버린다
                {
                    "kind": "vibes",
                    "severity": "critical",
                    "reason": "느낌",
                    "quote": "첫 문장입니다.",
                    "replacement": "",
                    "imageIndex": None,
                },
            ]
        }
    )

    assert [i.kind for i in parsed] == ["fact"]


def test_a_broken_response_is_read_as_no_issues():
    """검수는 원고 생성의 관문이 아니다. 응답이 이상하면 '지적 없음'이 맞다 —
    여기서 예외를 던지면 이미 완성된 원고가 통째로 실패한다."""
    assert final_review_issues_from_json(None) == []
    assert final_review_issues_from_json({"issues": "nope"}) == []
    assert final_review_issues_from_json({}) == []


def test_an_unknown_severity_falls_back_to_minor():
    """모르는 값을 critical로 읽으면 검수가 원고를 마음대로 고치게 된다."""
    parsed = final_review_issues_from_json(
        {
            "issues": [
                {
                    "kind": "fact",
                    "severity": "URGENT!!",
                    "reason": "r",
                    "quote": "가격은 3만원입니다.",
                    "replacement": "",
                    "imageIndex": None,
                }
            ]
        }
    )

    assert parsed[0].severity == "minor"


# ------------------------------------------------- 두 모델의 검수를 합치는 규칙


def report(issues, *, status="revise", score=70):
    from app.shared import FinalReviewReport

    return FinalReviewReport(overall_status=status, overall_score=score, issues=issues)


def test_the_second_reviewer_adds_what_the_first_one_missed():
    """두 모델을 쓰는 이유가 이것이다 — 한쪽만 잡은 문제도 고쳐야 한다."""
    from app.modules.draft.final_review import merge_review_reports

    first = report([issue(quote="가격은 3만원입니다.")])
    second = report([issue(quote="마지막 문장입니다.", reason="근거 없음", kind="unsupported")])

    merged = merge_review_reports(first, second)

    assert [i.quote for i in merged.issues] == ["가격은 3만원입니다.", "마지막 문장입니다."]


def test_the_same_sentence_is_never_corrected_twice():
    """두 모델이 같은 자리를 각자 잡아 왔다. 한 자리를 두 번 바꿀 수는 없다 —
    먼저 적용된 교정이 두 번째의 quote를 없애 버려, 남겨 두면 '적용되지 않은 지적'으로만
    기록된다."""
    from app.modules.draft.final_review import merge_review_reports

    first = report([issue(quote="가격은 3만원입니다.", replacement="가격은 2만원입니다.")])
    second = report([issue(quote="가격은 3만원입니다.", replacement="가격은 2만 원입니다.")])

    merged = merge_review_reports(first, second)

    assert len(merged.issues) == 1
    # 먼저 잡은 쪽의 교정문을 쓴다.
    assert merged.issues[0].replacement == "가격은 2만원입니다."


def test_the_same_image_is_never_dropped_twice():
    """이미지 지적은 quote가 비어 있다 — 자리를 가르는 것은 그림 번호다."""
    from app.modules.draft.final_review import merge_review_reports

    first = report([issue(kind="image", quote="", replacement="", image_index=1)])
    second = report(
        [
            issue(kind="image", quote="", replacement="", image_index=1),
            issue(kind="image", quote="", replacement="", image_index=2),
        ]
    )

    merged = merge_review_reports(first, second)

    assert [i.image_index for i in merged.issues] == [1, 2]


def test_the_worse_verdict_wins_when_the_two_reviewers_disagree():
    """한쪽이 고칠 것이 있다고 했는데 통과로 적으면, 그 지적을 왜 무시했는지 설명할 수 없다."""
    from app.modules.draft.final_review import merge_review_reports

    passing = report([], status="pass", score=95)
    revising = report([issue()], status="revise", score=60)

    merged = merge_review_reports(passing, revising)

    assert merged.overall_status == "revise"
    assert merged.overall_score == 60
