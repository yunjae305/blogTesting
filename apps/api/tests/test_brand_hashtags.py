"""모든 글에 고정으로 붙는 브랜드 해시태그(2026-08-20 사용자 요청).

> "해시태그에 aiona 또는 아이오나 또는 관련 해시태그도 1~2개는 고정으로 들어가면 좋겠어"

여기서 지키는 것:

1. **언제나 붙는다** — 모델이 아니라 코드가 얹으므로 회차마다 달라지지 않는다.
2. **원고가 만든 해시태그를 밀어내지 않는다** — 소재로 검색해 들어올 사람의 말이 먼저다.
3. **두 개까지** — '1~2개'가 요청이었다.
4. **같은 말을 두 번 달지 않는다** — 대소문자만 다른 것도 같은 것으로 본다.
"""

import pytest

from app.modules.brand import validate_brand_body
from app.modules.brand.defaults import DEFAULT_BRAND_ID, HASHTAGS, default_brand_body
from app.modules.brand.service import with_brand_materials
from app.modules.draft.closing import BRAND_HASHTAG_COUNT, with_brand_hashtags
from app.shared import BrandProfile, FinalPost


def post(*tags: str) -> FinalPost:
    return FinalPost(
        title="마이쮸, 요즘 왜 다시 보일까",
        body="본문입니다.",
        hashtags=list(tags),
        html_content="<article><p>본문입니다.</p></article>",
    )


class TestTheyAreAlwaysThere:
    def test_two_get_added(self):
        result = with_brand_hashtags(post("마이쮸", "간식"), ["AIONA", "아이오나", "AI업무"])

        assert result.hashtags == ["마이쮸", "간식", "AIONA", "아이오나"]
        assert BRAND_HASHTAG_COUNT == 2

    def test_the_articles_own_tags_are_kept(self):
        """소재로 검색해 들어올 사람이 쓰는 말이다 — 브랜드 태그가 대신할 수 없다."""
        result = with_brand_hashtags(post("마이쮸", "간식", "편의점"), ["AIONA"])

        assert result.hashtags[:3] == ["마이쮸", "간식", "편의점"]

    def test_one_brand_tag_is_fine(self):
        """'1~2개'였다. 하나만 등록해 두었으면 하나만 붙는다."""
        assert with_brand_hashtags(post("간식"), ["AIONA"]).hashtags == ["간식", "AIONA"]

    def test_the_hash_mark_is_not_stored(self):
        """발행할 때 '#'이 붙는다. 여기 남아 있으면 '##AIONA'가 된다."""
        result = with_brand_hashtags(post("간식"), ["#AIONA"])

        assert result.hashtags == ["간식", "AIONA"]


class TestTheSameTagIsNotAddedTwice:
    def test_an_exact_repeat_is_skipped(self):
        result = with_brand_hashtags(post("간식", "AIONA"), ["AIONA", "아이오나"])

        assert result.hashtags == ["간식", "AIONA", "아이오나"]

    def test_case_only_differences_count_as_the_same(self):
        """발행하면 #AIONA와 #aiona가 나란히 선다 — 읽는 사람에게는 같은 말이다."""
        result = with_brand_hashtags(post("간식", "aiona"), ["AIONA", "아이오나"])

        assert result.hashtags == ["간식", "aiona", "아이오나"]

    def test_duplicates_within_the_brand_list_are_skipped(self):
        result = with_brand_hashtags(post("간식"), ["AIONA", "aiona", "아이오나"])

        assert result.hashtags == ["간식", "AIONA", "아이오나"]


class TestWhenNothingHappens:
    def test_a_post_without_a_brand_is_untouched(self):
        original = post("간식")

        assert with_brand_hashtags(original, []) is original
        assert with_brand_hashtags(original, None) is original

    def test_blank_entries_are_ignored(self):
        original = post("간식")

        assert with_brand_hashtags(original, ["  ", ""]) is original

    def test_nothing_is_added_when_everything_is_already_there(self):
        original = post("간식", "AIONA", "아이오나")

        assert with_brand_hashtags(original, ["AIONA", "아이오나"]) is original


class TestTheDefaultBrandShipsWithThem:
    def test_aiona_has_both_spellings(self):
        """'AIONA'만 달면 '아이오나'로 찾는 사람에게 이 글이 걸리지 않는다."""
        cleaned = validate_brand_body(default_brand_body())

        assert cleaned["hashtags"][:2] == ["AIONA", "아이오나"]
        assert HASHTAGS[:2] == ["AIONA", "아이오나"]


@pytest.mark.asyncio
class TestTheyRideAlongOnTheSavedPost:
    """글을 저장할 때 베껴 둔다 — 나중에 브랜드 자료를 고쳐도 이미 나간 글은 그대로다."""

    async def test_saving_a_brand_post_copies_them(self, monkeypatch):
        profile = BrandProfile(
            brand_id=DEFAULT_BRAND_ID,
            user_id="u",
            created_at="x",
            updated_at="x",
            **validate_brand_body(default_brand_body()),
        )

        class Stub:
            async def get_brand(self, user_id, brand_id):
                return profile

        payload, _ = await with_brand_materials(
            Stub(), "u", {"topic": "마이쮸", "brandId": DEFAULT_BRAND_ID}
        )

        assert payload["brandHashtags"][:2] == ["AIONA", "아이오나"]

    async def test_a_post_without_a_brand_gets_none(self):
        class Stub:
            async def get_brand(self, user_id, brand_id):  # pragma: no cover - 불릴 일 없음
                raise AssertionError("브랜드가 없는 글에서 조회하면 안 된다")

        payload, _ = await with_brand_materials(Stub(), "u", {"topic": "마이쮸"})

        assert payload["brandHashtags"] == []
