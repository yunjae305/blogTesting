"""AIONA 자료는 **처음부터 거기 있다**(2026-08-19 사용자 지시).

이 저장소는 AIONA 유입용 콘텐츠 엔진이다. 그러니 AIONA 브랜드 자료는 누가 어딘가에서 한
번 등록해 주어야 하는 것이 아니라, 서버를 새로 세우거나 계정을 새로 만들어도 브랜드
고르기 칸에 이미 있어야 한다.

여기서 지키는 것은 셋이다.

1. **없으면 생긴다** — 목록을 처음 열 때 만들어진다.
2. **두 벌이 되지 않는다** — 여러 번 열어도 하나다.
3. **고쳐 둔 것을 덮지 않는다** — 사용자가 이미지·문구를 손보면 그것이 남는다.
   이 셋 중 마지막이 가장 중요하다: 조회할 때마다 원래 값으로 되돌리면 사용자가 올린
   마스코트 이미지가 매번 사라진다.
"""

import pytest

from app.errors import BlogTaskError
from app.modules.brand import (
    DEFAULT_BRAND_ID,
    DEFAULT_BRAND_NAME,
    BrandService,
    InMemoryBrandRepository,
)


def service() -> BrandService:
    return BrandService(InMemoryBrandRepository())


@pytest.mark.asyncio
class TestTheDefaultIsAlreadyThere:
    async def test_a_brand_new_account_already_has_aiona(self):
        """아무것도 하지 않은 계정이 목록을 열면 AIONA가 있다."""
        items = await service().list_brand_items("user_1")

        assert [item.name for item in items] == [DEFAULT_BRAND_NAME]
        assert items[0].brand_id == DEFAULT_BRAND_ID

    async def test_the_full_profile_carries_the_table_and_the_closing(self):
        api = service()
        await api.list_brand_items("user_1")

        brand = await api.get_brand("user_1", DEFAULT_BRAND_ID)

        assert brand.use_cases, "기준표가 비면 모델이 기능을 짐작한다"
        assert brand.closing is not None
        assert brand.closing.url == "https://aiona.kr"

    async def test_opening_the_list_twice_does_not_make_two(self):
        """id가 사람마다 같은 값으로 고정돼 있어 같은 문서를 다시 쓸 뿐이다."""
        api = service()
        await api.list_brand_items("user_1")
        await api.list_brands("user_1")
        await api.list_brand_items("user_1")

        assert len(await api.list_brands("user_1")) == 1

    async def test_each_account_gets_its_own_copy(self):
        """브랜드는 사용자별 자료다 — 남의 것을 고칠 수 있으면 안 된다."""
        api = service()
        await api.list_brand_items("user_1")
        await api.list_brand_items("user_2")

        mine = await api.list_brands("user_1")
        assert [b.user_id for b in mine] == ["user_1"]

    async def test_it_appears_even_when_asked_for_directly(self):
        """목록을 거치지 않고 이 id로 곧바로 들어오는 길이 있다(앱스튜디오 진입·저장된 글)."""
        brand = await service().get_brand("user_1", DEFAULT_BRAND_ID)

        assert brand.name == DEFAULT_BRAND_NAME

    async def test_an_unknown_brand_is_still_not_found(self):
        with pytest.raises(BlogTaskError) as caught:
            await service().get_brand("user_1", "brand_없는것")

        assert caught.value.code == "NOT_FOUND"


@pytest.mark.asyncio
class TestUserEditsSurvive:
    """조회할 때마다 원래 값으로 되돌리면 사용자가 올린 마스코트가 매번 사라진다."""

    async def test_edits_are_not_overwritten_by_the_next_read(self):
        api = service()
        await api.list_brand_items("user_1")
        await api.update_brand(
            "user_1",
            DEFAULT_BRAND_ID,
            {
                "name": DEFAULT_BRAND_NAME,
                "description": "내가 고친 소개",
                "images": [{"label": "마스코트", "dataUrl": "data:image/png;base64,AAAA"}],
            },
        )

        await api.list_brand_items("user_1")
        brand = await api.get_brand("user_1", DEFAULT_BRAND_ID)

        assert brand.description == "내가 고친 소개"
        assert len(brand.images) == 1


@pytest.mark.asyncio
class TestTheDefaultCannotBeDeleted:
    async def test_deleting_it_says_why_instead_of_quietly_coming_back(self):
        """지워도 다음 조회에서 되살아난다. 조용히 돌아오면 이유를 알 수 없다."""
        api = service()
        await api.list_brand_items("user_1")

        with pytest.raises(BlogTaskError) as caught:
            await api.delete_brand("user_1", DEFAULT_BRAND_ID)

        assert caught.value.code == "VALIDATION_FAILED"
        assert "삭제할 수 없습니다" in caught.value.message
        assert len(await api.list_brands("user_1")) == 1

    async def test_other_brands_delete_as_before(self):
        api = service()
        made = await api.create_brand("user_1", {"name": "다른 회사"})

        await api.delete_brand("user_1", made.brand_id)

        assert [b.brand_id for b in await api.list_brands("user_1")] == [DEFAULT_BRAND_ID]
