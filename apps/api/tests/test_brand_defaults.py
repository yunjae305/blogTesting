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
from app.modules.brand.defaults import MASCOTS
from app.shared import BrandProfile


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


class TestTheScreenKnowsWhichOneIsDefault:
    """화면도 기본 브랜드의 id를 안다 — **삭제 버튼을 내주지 않기 위해서**다.

    서버가 삭제를 거부하므로, 눌러 봐야 오류만 나는 버튼을 보여 줄 이유가 없다. 두 값이
    어긋나면 지울 수 없는 브랜드에 삭제 버튼이 뜬다. 원고 편수 상한(MAX_DRAFT_COUNT)을
    양쪽에서 대조하는 것과 같은 자리다.
    """

    def test_the_client_and_the_server_agree_on_the_id(self):
        from pathlib import Path

        # tests/ -> apps/api -> apps -> apps/web/src/constants.ts
        source = Path(__file__).resolve().parents[2] / "web" / "src" / "constants.ts"
        text = source.read_text(encoding="utf-8")

        assert f'DEFAULT_BRAND_ID = "{DEFAULT_BRAND_ID}"' in text


@pytest.mark.asyncio
class TestTheDefaultCanBeDeletedForGood:
    """기본 브랜드도 지울 수 있다(2026-08-20 사용자 요청).

    한동안 막아 두었다. 지워도 다음 조회에서 되살아났기 때문인데, 그건 서버를 고칠
    일이지 버튼을 숨길 일이 아니었다 — 쓰지 않는 자료를 목록에 계속 두어야 할 이유가
    없다.

    그래서 **문서를 지우는 대신 지웠다는 표시를 남긴다.** 다시 만들어 주는 자리가 그
    표시를 보고 손을 뗀다.
    """

    async def test_it_disappears_and_stays_gone(self):
        api = service()
        await api.list_brand_items("user_1")

        await api.delete_brand("user_1", DEFAULT_BRAND_ID)

        assert await api.list_brands("user_1") == []
        # 다시 조회해도 되살아나지 않는다 — 이것이 이 방식의 이유다.
        assert await api.list_brand_items("user_1") == []
        assert await api.list_brands("user_1") == []

    async def test_reading_it_directly_no_longer_finds_it(self):
        """앱스튜디오 진입·저장된 글이 이 id로 곧바로 들어오는 길이 있다."""
        api = service()
        await api.list_brand_items("user_1")
        await api.delete_brand("user_1", DEFAULT_BRAND_ID)

        with pytest.raises(BlogTaskError) as caught:
            await api.get_brand("user_1", DEFAULT_BRAND_ID)

        assert caught.value.code == "NOT_FOUND"

    async def test_deleting_it_twice_says_it_is_gone(self):
        api = service()
        await api.list_brand_items("user_1")
        await api.delete_brand("user_1", DEFAULT_BRAND_ID)

        with pytest.raises(BlogTaskError) as caught:
            await api.delete_brand("user_1", DEFAULT_BRAND_ID)

        assert caught.value.code == "NOT_FOUND"

    async def test_one_account_deleting_it_does_not_touch_another(self):
        api = service()
        await api.list_brand_items("user_1")
        await api.list_brand_items("user_2")

        await api.delete_brand("user_1", DEFAULT_BRAND_ID)

        assert await api.list_brands("user_1") == []
        assert [b.brand_id for b in await api.list_brands("user_2")] == [DEFAULT_BRAND_ID]

    async def test_other_brands_are_removed_outright(self):
        """기본 브랜드만 표시를 남긴다. 나머지는 다시 만들어 주는 자리가 없어 그럴 이유가 없다."""
        api = service()
        made = await api.create_brand("user_1", {"name": "다른 회사"})

        await api.delete_brand("user_1", made.brand_id)

        assert made.brand_id not in [b.brand_id for b in await api.list_brands("user_1")]

    async def test_an_unknown_brand_is_still_not_found(self):
        with pytest.raises(BlogTaskError) as caught:
            await service().delete_brand("user_1", "brand_없는것")

        assert caught.value.code == "NOT_FOUND"


class TestOlderCopiesGetNewMaterialLater:
    """기본 브랜드에 자료가 늘면 **이미 쓰던 사람에게도** 가야 한다(2026-08-20).

    "없으면 만든다"만 있었을 때, 한 번 만들어진 뒤에 정의로 새 자료가 들어오면(마스코트
    그림, 고정 해시태그) 이미 쓰던 사람에게는 영영 오지 않았다 — 등록한 적도 없는 자료가
    자기 것만 비어 있는데, 사용자에게는 그것을 알 방법도 고칠 방법도 없었다.

    실제로 겪은 일이다: 마스코트를 넣었는데 원고에 나오지 않았다. 코드는 맞았고, 그 사람의
    AIONA 자료가 마스코트보다 먼저 만들어져 있었다.
    """

    @pytest.mark.asyncio
    async def test_an_old_copy_gets_the_mascots_and_hashtags(self):
        repository = InMemoryBrandRepository()
        service = BrandService(repository)
        # 마스코트도 해시태그도 없던 시절의 자료.
        await repository.upsert(
            BrandProfile(
                brand_id=DEFAULT_BRAND_ID,
                user_id="user_1",
                name=DEFAULT_BRAND_NAME,
                created_at="x",
                updated_at="x",
            )
        )

        await service.ensure_default_brands("user_1")
        profile = await service.get_brand("user_1", DEFAULT_BRAND_ID)

        assert len(profile.images) == len(MASCOTS)
        assert profile.hashtags[:2] == ["AIONA", "아이오나"]
        assert profile.use_cases

    @pytest.mark.asyncio
    async def test_hand_written_text_is_not_overwritten(self):
        """손으로 다듬어 둔 글자를 되돌리면 그 편집이 통째로 사라진다."""
        repository = InMemoryBrandRepository()
        service = BrandService(repository)
        await repository.upsert(
            BrandProfile(
                brand_id=DEFAULT_BRAND_ID,
                user_id="user_1",
                name=DEFAULT_BRAND_NAME,
                description="내가 고쳐 둔 소개",
                created_at="x",
                updated_at="x",
            )
        )

        await service.ensure_default_brands("user_1")
        profile = await service.get_brand("user_1", DEFAULT_BRAND_ID)

        assert profile.description == "내가 고쳐 둔 소개"
        # 비어 있던 칸은 채워졌다.
        assert profile.images

    @pytest.mark.asyncio
    async def test_it_only_happens_once(self):
        """그 뒤로는 사용자가 비운 것이 곧 뜻이다 — 일부러 지운 그림이 되살아나면 안 된다."""
        repository = InMemoryBrandRepository()
        service = BrandService(repository)
        await service.ensure_default_brands("user_1")

        profile = await service.get_brand("user_1", DEFAULT_BRAND_ID)
        await repository.upsert(profile.model_copy(update={"images": [], "hashtags": []}))
        await service.ensure_default_brands("user_1")

        again = await service.get_brand("user_1", DEFAULT_BRAND_ID)
        assert again.images == []
        assert again.hashtags == []

    @pytest.mark.asyncio
    async def test_a_deleted_default_brand_stays_deleted(self):
        """빈 칸 채우기가 무덤을 열어서는 안 된다."""
        repository = InMemoryBrandRepository()
        service = BrandService(repository)
        await service.ensure_default_brands("user_1")
        await service.delete_brand("user_1", DEFAULT_BRAND_ID)

        await service.ensure_default_brands("user_1")

        assert [b.brand_id for b in await service.list_brands("user_1")] == []
