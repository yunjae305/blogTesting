"""글 맨 마지막에 붙는 마무리 블록(2026-08-19 사용자 지시).

본문은 광고가 아니어야 한다(`brand_utility_rules`). 그런데 글의 끝에는 "여기서 보면
된다"는 자리가 하나 있어야 하고, 사용자가 실제로 쓰던 글들이 전부 그 모양이었다 —
본문은 담담하고, 끝에 마스코트 한 장과 사실 한 줄, 그리고 링크.

둘은 충돌하지 않는다. 오히려 **본문에서 권유하지 않기 때문에** 마지막 한 줄이 신뢰를
얻는다. 그래서 규칙을 자리별로 나눈다: 본문에는 권유 문장 금지, 마지막 블록은 고정.

여기서 지키는 것:

1. **모델이 아니라 코드가 붙인다** — 매번 똑같아야 하는 사실이고(크레딧 수·가입 조건),
   붙는 자리가 최종 검수 뒤다(검수가 광고 문구로 읽고 고쳐 버리면 안 된다).
2. **두 번 붙지 않는다.**
3. **본문·HTML·마크다운 셋이 같은 말을 한다** — 발행 경로가 셋을 각각 읽는다.
"""

import pytest

from app.modules.draft.closing import (
    CLOSING_MARKER,
    append_closing,
    has_closing,
)
from app.shared import BrandClosing, FinalPost, ReferenceMaterial


def closing(**overrides) -> BrandClosing:
    defaults = dict(
        note="가입은 무료, 웰컴 크레딧 100 지급, 카드 등록 없음.",
        label="aiona.kr",
        url="https://aiona.kr",
    )
    return BrandClosing(**{**defaults, **overrides})


def post(**overrides) -> FinalPost:
    body = "빼빼로 신제품은 이렇게 달라졌습니다."
    defaults = dict(
        title="빼빼로 신제품, 뭐가 달라졌나",
        body=body,
        hashtags=["간식"],
        html_content=f"<article><h1>제목</h1><p>{body}</p></article>",
        markdown_content=f"# 제목\n\n{body}",
    )
    return FinalPost(**{**defaults, **overrides})


class TestTheBlockIsAppended:
    def test_all_three_representations_get_it(self):
        """발행 경로가 셋을 각각 읽는다 — 하나만 붙으면 어디선가 사라진다."""
        result = append_closing(post(), closing())

        assert "aiona.kr" in result.body
        assert "aiona.kr" in result.html_content
        assert "aiona.kr" in result.markdown_content

    def test_the_facts_go_in_untouched(self):
        """이 글자는 검수를 거치지 않고 그대로 발행된다."""
        result = append_closing(post(), closing())

        assert "가입은 무료, 웰컴 크레딧 100 지급, 카드 등록 없음." in result.body

    def test_the_link_is_clickable_in_html(self):
        result = append_closing(post(), closing())

        assert '<a href="https://aiona.kr"' in result.html_content
        assert "👉" in result.html_content

    def test_the_body_keeps_the_address_readable(self):
        """``body``는 순수 글자다 — 링크를 걸 수 없으니 주소를 그대로 적는다."""
        result = append_closing(post(), closing())

        assert "https://aiona.kr" in result.body

    def test_it_goes_at_the_very_end(self):
        result = append_closing(post(), closing())

        assert result.html_content.index("빼빼로 신제품은") < result.html_content.index("aiona.kr")
        assert result.markdown_content.rstrip().endswith("[aiona.kr](https://aiona.kr)")

    def test_the_article_body_is_left_alone(self):
        """마무리를 붙이면서 본문을 건드리면 검수를 통과한 글이 달라진다."""
        result = append_closing(post(), closing())

        assert "빼빼로 신제품은 이렇게 달라졌습니다." in result.body
        assert "<article><h1>제목</h1>" in result.html_content


class TestItDoesNotHappenTwice:
    def test_appending_again_changes_nothing(self):
        once = append_closing(post(), closing())
        twice = append_closing(once, closing())

        assert twice is once
        assert once.html_content.count(CLOSING_MARKER) == 1

    def test_a_post_without_the_block_is_recognised(self):
        assert not has_closing(post())
        assert has_closing(append_closing(post(), closing()))


class TestNothingToAppend:
    def test_no_closing_leaves_the_post_untouched(self):
        """브랜드를 안 쓴 글·마무리를 안 적어 둔 브랜드. 부르는 쪽이 조건을 따지지 않아도 된다."""
        original = post()

        assert append_closing(original, None) is original


class TestTheMascot:
    def _image_material(self, name: str, origin: str | None = "brand") -> ReferenceMaterial:
        return ReferenceMaterial(
            type="IMAGE",
            name=name,
            value="data:image/png;base64,AAAA",
            origin=origin,
        )

    def test_the_named_brand_image_is_attached(self):
        materials = [self._image_material("AIONA 마스코트")]

        result = append_closing(
            post(), closing(image_label="AIONA 마스코트"), materials
        )

        assert "data:image/png;base64,AAAA" in result.html_content
        # 발행 경로가 이 목록을 보고 data URL을 실제 주소로 바꾼다 — 목록에 없으면
        # 네이버에서 그림이 빠진다.
        assert [image.alt_text for image in result.images or []] == ["AIONA 마스코트"]

    def test_a_users_own_image_is_never_used(self):
        """이름이 같다는 이유로 사용자가 올린 사진이 마무리에 실리면 안 된다."""
        materials = [self._image_material("AIONA 마스코트", origin=None)]

        result = append_closing(
            post(), closing(image_label="AIONA 마스코트"), materials
        )

        assert "data:image" not in result.html_content

    def test_a_missing_image_still_leaves_the_text(self):
        """이미지를 아직 안 올렸어도 마무리는 붙어야 한다."""
        result = append_closing(post(), closing(image_label="없는 이름"), [])

        assert "aiona.kr" in result.html_content
        assert "data:image" not in result.html_content

    def test_no_image_label_means_text_only(self):
        materials = [self._image_material("AIONA 마스코트")]

        result = append_closing(post(), closing(), materials)

        assert "data:image" not in result.html_content


@pytest.mark.asyncio
class TestItIsCarriedFromTheBrandToThePost:
    """저장 시점에 베껴 둔다 — 나중에 브랜드 자료를 고쳐도 이미 나간 글은 바뀌지 않는다."""

    async def test_saving_a_post_copies_the_closing(self):
        from app.modules.brand import (
            DEFAULT_BRAND_ID,
            BrandService,
            InMemoryBrandRepository,
            with_brand_materials,
        )

        api = BrandService(InMemoryBrandRepository())
        await api.list_brand_items("user_1")

        payload, _ = await with_brand_materials(
            api, "user_1", {"topic": "다이어트 간식", "brandId": DEFAULT_BRAND_ID}
        )

        assert payload["brandClosing"]["url"] == "https://aiona.kr"
        assert "웰컴 크레딧 100" in payload["brandClosing"]["note"]

    async def test_a_post_without_a_brand_carries_none(self):
        from app.modules.brand import with_brand_materials

        payload, _ = await with_brand_materials(None, "user_1", {"topic": "빼빼로"})

        assert payload["brandClosing"] is None
