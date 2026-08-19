"""이미 발행한 내 글과 새 원고가 얼마나 겹치는가.

자동 생성의 위험은 **한 편의 품질**이 아니라 **쌓였을 때의 닮음**이다. 한 편씩 보면
어느 것도 이상하지 않아, 기존 검사(한 원고 안의 반복·같은 배치의 제목 중복)는
아무것도 잡지 못한다.
"""

from app.modules.draft.content_validation import validate_published_duplication
from app.modules.draft.duplication import (
    NEAR_DUPLICATE,
    SIMILAR,
    PostDigest,
    compare,
    extract_headings,
    first_paragraph,
    similarity,
)
from app.shared import FinalPost

BODY = """## 강서구 에어컨 청소가 필요한 이유

여름이 오기 전에 에어컨 상태를 점검해야 합니다. 송풍구에서 냄새가 난다면 내부 오염을
의심할 수 있습니다.

## 에어컨 종류별 예상 비용

### 벽걸이형

벽걸이형은 분해 범위가 좁습니다.
"""


def _post(title: str, markdown: str) -> FinalPost:
    return FinalPost(
        title=title,
        body=markdown,
        hashtags=[],
        images=[],
        html_content="",
        markdown_content=markdown,
    )


class TestReadingTheDraft:
    def test_it_reads_the_headings_and_the_opening(self):
        assert extract_headings(BODY) == [
            "강서구 에어컨 청소가 필요한 이유",
            "에어컨 종류별 예상 비용",
            "벽걸이형",
        ]
        # 소제목이 아니라 **첫 실질 문단**이다.
        assert first_paragraph(BODY).startswith("여름이 오기 전에")

    def test_headings_images_and_quotes_are_not_the_opening(self):
        markdown = "## 소제목\n\n![사진](data:image/png;base64,AAAA)\n\n> 인용\n\n진짜 첫 문단입니다."

        assert first_paragraph(markdown) == "진짜 첫 문단입니다."

    def test_one_letter_tokens_do_not_make_everything_look_alike(self):
        # '이', '그'까지 세면 아무 글이나 닮아 보인다.
        assert similarity("이 그 저", "이 그 저 다른 이야기") < 1.0
        assert similarity("", "무엇이든") == 0.0


class TestComparingWithWhatIsAlreadyThere:
    def test_the_same_article_twice_is_caught(self):
        digest = PostDigest(
            post_id="post_old",
            title="강서구 에어컨 청소 비용",
            headings=extract_headings(BODY),
            opening=first_paragraph(BODY),
        )
        candidate = PostDigest(
            post_id="post_new",
            title="강서구 에어컨 청소 비용",
            headings=extract_headings(BODY),
            opening=first_paragraph(BODY),
        )

        verdict = compare(candidate, [digest])

        assert verdict.near_duplicate
        assert verdict.score >= NEAR_DUPLICATE
        assert verdict.closest is not None and verdict.closest.post_id == "post_old"

    def test_a_different_article_is_not_flagged(self):
        digest = PostDigest(
            post_id="post_old",
            title="강서구 에어컨 청소 비용",
            headings=["에어컨 종류별 예상 비용"],
            opening="여름이 오기 전에 에어컨 상태를 점검해야 합니다.",
        )
        candidate = PostDigest(
            post_id="post_new",
            title="부산 이사 업체 고르는 법",
            headings=["포장이사와 반포장이사"],
            opening="이사 견적은 짐의 양과 층수에 따라 크게 달라집니다.",
        )

        verdict = compare(candidate, [digest])

        assert not verdict.similar
        assert verdict.score < SIMILAR

    def test_the_same_post_is_not_compared_with_itself(self):
        digest = PostDigest(post_id="post_1", title="같은 글", headings=["가"], opening="나")

        assert compare(PostDigest(post_id="post_1", title="같은 글", headings=["가"], opening="나"), [digest]).closest is None

    def test_axes_are_reported_separately(self):
        """제목만 같은 글과 도입부가 통째로 같은 글은 손봐야 할 곳이 다르다."""
        digest = PostDigest(
            post_id="post_old",
            title="강서구 에어컨 청소 비용",
            headings=["전혀 다른 소제목 구성입니다"],
            opening="완전히 다른 도입부 문장이 여기에 옵니다.",
        )
        candidate = PostDigest(
            post_id="post_new",
            title="강서구 에어컨 청소 비용",
            headings=["또 다른 이야기 구성"],
            opening="이 글은 아주 다른 이야기로 시작합니다.",
        )

        verdict = compare(candidate, [digest])

        assert verdict.title_score == 1.0
        assert verdict.opening_score < 0.5


class TestTheCheck:
    def test_no_previous_posts_means_skipped(self):
        result = validate_published_duplication(_post("제목", BODY), [])

        assert result.status == "SKIPPED"
        assert not result.rejected

    def test_a_near_duplicate_only_warns(self):
        """임계값은 검색엔진 기준이 아니라 우리 내부 관리 기준이다. 그걸 근거로 완성된
        원고를 반려하지 않는다."""
        previous = [
            PostDigest(
                post_id="post_old",
                title="강서구 에어컨 청소 비용",
                headings=extract_headings(BODY),
                opening=first_paragraph(BODY),
            )
        ]

        result = validate_published_duplication(_post("강서구 에어컨 청소 비용", BODY), previous)

        assert result.status == "WARN"
        assert not result.rejected
        assert result.details["closestPostId"] == "post_old"
        assert result.details["score"] >= NEAR_DUPLICATE

    def test_a_fresh_article_passes_and_reports_the_axes(self):
        previous = [
            PostDigest(
                post_id="post_old",
                title="부산 이사 업체 고르는 법",
                headings=["포장이사와 반포장이사"],
                opening="이사 견적은 짐의 양과 층수에 따라 크게 달라집니다.",
            )
        ]

        result = validate_published_duplication(_post("강서구 에어컨 청소 비용", BODY), previous)

        assert result.status == "PASS"
        for key in ("score", "titleScore", "headingsScore", "openingScore", "comparedWith"):
            assert key in result.details
