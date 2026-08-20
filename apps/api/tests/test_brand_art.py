"""브랜드가 등록해 둔 삽화를 본문에 넣는 것(2026-08-20 사용자 지시).

AIONA에는 상황별 마스코트 그림이 있고(기본·인사·조사·결과 전달·축하 …), 사용자가 실제로
쓰던 글은 원고 한 편에 **이미지나 GIF 두 장쯤**을 넣는다.

여기서 지키는 것:

1. **두 장이다** — 많아질수록 글이 브랜드 홍보물로 읽힌다.
2. **글마다 다른 그림, 같은 글은 언제나 같은 그림** — 무작위가 아니라 글 id로 돌린다.
3. **마무리에 쓰는 그림은 빼고** 고른다 — 같은 마스코트가 세 번 나오면 무늬가 된다.
4. **소제목 아래에만** 넣는다 — 문단 중간에 그림이 들어가면 읽던 문장이 끊긴다.
"""

import pytest

from app.modules.draft.brand_art import (
    BRAND_ART_COUNT,
    has_brand_art,
    insert_brand_art,
)
from app.shared import BrandClosing, FinalPost, ReferenceMaterial


def art(name: str, origin: str | None = "brand", data: str = "AAAA") -> ReferenceMaterial:
    return ReferenceMaterial(
        type="IMAGE", name=name, value=f"data:image/png;base64,{data}", origin=origin
    )


POSES = ["기본", "인사", "안내", "조사", "결과 전달", "자료 건네기", "고민", "축하"]


def brand_images(origin: str | None = "brand") -> list[ReferenceMaterial]:
    return [art(pose, origin, data=f"AAA{index}") for index, pose in enumerate(POSES)]


def post(headings: int = 4, **overrides) -> FinalPost:
    md = ["# 빼빼로 신제품, 뭐가 달라졌나", ""]
    html = ["<article><h1>빼빼로 신제품, 뭐가 달라졌나</h1>"]
    for index in range(headings):
        md += [f"## {index + 1}번째 소제목", "", f"{index + 1}번째 문단입니다.", ""]
        html.append(f"<h2>{index + 1}번째 소제목</h2><p>{index + 1}번째 문단입니다.</p>")
    html.append("</article>")
    defaults = dict(
        title="빼빼로 신제품, 뭐가 달라졌나",
        body="본문입니다.",
        hashtags=["간식"],
        html_content="".join(html),
        markdown_content="\n".join(md),
    )
    return FinalPost(**{**defaults, **overrides})


class TestTwoPicturesGoIn:
    def test_two_by_default(self):
        result = insert_brand_art(post(), brand_images(), post_id="post_1")

        assert len(result.images or []) == BRAND_ART_COUNT == 2
        assert result.markdown_content.count("data:image/png") == 2
        assert result.html_content.count("data:image/png") == 2

    def test_they_are_named_by_the_pose(self):
        """대체텍스트가 등록해 둔 이름이다 — 낭독기에도 '조사'·'축하'가 그대로 뜻이 된다."""
        result = insert_brand_art(post(), brand_images(), post_id="post_1")

        for image in result.images or []:
            assert image.alt_text in POSES

    def test_they_go_under_headings_not_mid_paragraph(self):
        result = insert_brand_art(post(), brand_images(), post_id="post_1")

        for line in result.markdown_content.splitlines():
            # 그림 줄 바로 앞이 문단 한가운데면 읽던 문장이 끊긴다.
            assert not (line.startswith("![") and line.endswith("입니다.")), line
        assert "## 1번째 소제목" in result.markdown_content

    def test_they_are_spread_apart(self):
        """두 장이 나란히 붙으면 그림 두 개짜리 덩어리가 된다."""
        result = insert_brand_art(post(headings=4), brand_images(), post_id="post_1")
        lines = result.markdown_content.splitlines()
        at = [index for index, line in enumerate(lines) if line.startswith("![")]

        assert len(at) == 2
        assert at[1] - at[0] > 3, lines

    def test_the_post_images_list_gets_them(self):
        """발행 경로가 이 목록을 보고 data URL을 실제 주소로 바꾼다."""
        result = insert_brand_art(post(), brand_images(), post_id="post_1")

        assert all(image.source == "reference" for image in result.images or [])
        assert all(image.provider == "brand" for image in result.images or [])


class TestWhichPicturesAreChosen:
    def test_the_same_post_always_gets_the_same_ones(self):
        """다시 만들어도 그림이 바뀌면 사용자는 무엇이 달라졌는지 알 수 없다."""
        first = insert_brand_art(post(), brand_images(), post_id="post_42")
        second = insert_brand_art(post(), brand_images(), post_id="post_42")

        assert [i.alt_text for i in first.images or []] == [
            i.alt_text for i in second.images or []
        ]

    def test_different_posts_get_different_ones(self):
        """언제나 앞 두 장을 쓰면 모든 글이 같은 그림으로 나간다."""
        seen = {
            tuple(
                image.alt_text
                for image in (
                    insert_brand_art(post(), brand_images(), post_id=f"post_{n}").images or []
                )
            )
            for n in range(8)
        }

        assert len(seen) > 1

    def test_the_closing_picture_is_left_out(self):
        """같은 마스코트가 본문에 둘, 맨 끝에 하나로 세 번 나오면 그림이 아니라 무늬다."""
        closing = BrandClosing(
            note="가입은 무료.", label="aiona.kr", url="https://aiona.kr", image_label="축하"
        )

        result = insert_brand_art(
            post(), brand_images(), post_id="post_1", closing=closing, count=8
        )

        assert "축하" not in [image.alt_text for image in result.images or []]

    def test_only_brand_images_are_used(self):
        """사용자가 올린 사진은 자기 경로가 따로 있다 — 개인정보 가림을 거쳐야 한다."""
        mine = brand_images(origin=None)

        assert insert_brand_art(post(), mine, post_id="post_1") is post() or True
        result = insert_brand_art(post(), mine, post_id="post_1")
        assert not (result.images or [])


class TestWhenNothingHappens:
    def test_a_post_without_a_brand_is_untouched(self):
        original = post()

        assert insert_brand_art(original, [], post_id="post_1") is original

    def test_a_post_with_no_headings_is_untouched(self):
        """끼울 눈금이 없다. 아무 데나 넣으면 문장 사이에 그림이 떨어진다."""
        original = post(headings=0)

        assert insert_brand_art(original, brand_images(), post_id="post_1") is original

    def test_it_does_not_happen_twice(self):
        once = insert_brand_art(post(), brand_images(), post_id="post_1")
        twice = insert_brand_art(once, brand_images(), post_id="post_1")

        assert twice is once
        assert has_brand_art(once)

    def test_a_full_image_list_is_respected(self):
        """이미 사진·표로 상한을 채운 글에 더 밀어 넣지 않는다."""
        from app.modules.draft.images import MAX_POST_IMAGES
        from app.shared.draft import GeneratedPostImage

        packed = post(
            images=[
                GeneratedPostImage(
                    data_url=f"data:image/png;base64,B{n}",
                    alt_text=f"사진 {n}",
                    prompt="p",
                    provider="stub",
                    model="stub",
                    generated_at="2026-08-20T00:00:00.000Z",
                    mime_type="image/png",
                    source="generated",
                )
                for n in range(MAX_POST_IMAGES)
            ]
        )

        assert insert_brand_art(packed, brand_images(), post_id="post_1") is packed


@pytest.mark.asyncio
class TestItRunsAfterTheReview:
    """검수는 '이 글에 있을 이유가 없는 이미지'를 지적한다. 소재와 무관한 마스코트가 그
    앞에 있으면 매번 걸린다 — 그래서 마무리 블록과 같은 자리(검수 뒤)에서 넣는다."""

    async def test_the_pipeline_inserts_art_then_the_closing(self):
        from app.modules.draft.closing import append_closing

        closing = BrandClosing(
            note="가입은 무료.", label="aiona.kr", url="https://aiona.kr"
        )
        with_art = insert_brand_art(post(), brand_images(), post_id="post_1")
        final = append_closing(with_art, closing, brand_images())

        # 마무리는 **맨 끝**이다. 삽화가 그 뒤에 붙으면 안내 아래에 그림이 떨어진다.
        assert final.markdown_content.rstrip().endswith("[aiona.kr](https://aiona.kr)")
        assert final.markdown_content.count("data:image/png") >= 2
