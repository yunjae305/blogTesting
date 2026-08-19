"""브랜드 자료 저장소.

사용자별 컬렉션 하나(`brandProfiles`)에 문서로 둔다. 사용자 설정처럼 `users` 안에 접어
넣지 않은 이유는 **1:N**이기 때문이다 — 한 사람이 여러 브랜드를 둘 수 있고, 나중에는
다른 사용자와 나눠 쓰게 할 계획이라 문서가 따로 서 있어야 한다.
"""

from typing import Protocol

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db.mongo import strip_id
from app.shared import BrandListItem, BrandProfile

# 이름은 최신 컬렉션 규약(scheduled_jobs·trend_keywords)을 따른다.
COLLECTION = "brand_profiles"

#: 목록에 필요한 필드만. 이미지·문서의 base64는 **DB에서부터 가져오지 않는다** —
#: 브랜드 하나가 2MB인데(실측: 이미지 9장) 목록은 이름과 한 줄 소개만 그린다.
_LIST_PROJECTION = {
    "brandId": 1,
    "userId": 1,
    "name": 1,
    "description": 1,
    "createdAt": 1,
    "updatedAt": 1,
    "linkCount": {"$size": {"$ifNull": ["$links", []]}},
    "documentCount": {"$size": {"$ifNull": ["$documents", []]}},
    "imageCount": {"$size": {"$ifNull": ["$images", []]}},
}


class BrandRepository(Protocol):
    async def list_by_user_id(self, user_id: str) -> list[BrandProfile]: ...
    async def list_items_by_user_id(self, user_id: str) -> list[BrandListItem]: ...
    async def find(self, user_id: str, brand_id: str) -> BrandProfile | None: ...
    async def find_light(self, user_id: str, brand_id: str) -> BrandProfile | None: ...
    async def upsert(self, profile: BrandProfile) -> BrandProfile: ...
    async def delete(self, user_id: str, brand_id: str) -> bool: ...


class InMemoryBrandRepository:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], BrandProfile] = {}

    async def list_by_user_id(self, user_id: str) -> list[BrandProfile]:
        found = [p for (uid, _), p in self._by_key.items() if uid == user_id]
        # 최근에 고친 것이 위로. 화면이 목록을 그대로 그린다.
        return sorted(found, key=lambda p: p.updated_at, reverse=True)

    async def list_items_by_user_id(self, user_id: str) -> list[BrandListItem]:
        return [BrandListItem.of(p) for p in await self.list_by_user_id(user_id)]

    async def find(self, user_id: str, brand_id: str) -> BrandProfile | None:
        return self._by_key.get((user_id, brand_id))

    async def find_light(self, user_id: str, brand_id: str) -> BrandProfile | None:
        profile = self._by_key.get((user_id, brand_id))
        if profile is None:
            return None
        return profile.model_copy(update={"images": [], "documents": []})

    async def upsert(self, profile: BrandProfile) -> BrandProfile:
        existing = self._by_key.get((profile.user_id, profile.brand_id))
        stored = profile.model_copy(
            update={"created_at": existing.created_at if existing else profile.created_at}
        )
        self._by_key[(profile.user_id, profile.brand_id)] = stored
        return stored

    async def delete(self, user_id: str, brand_id: str) -> bool:
        return self._by_key.pop((user_id, brand_id), None) is not None


class MongoBrandRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self._collection = db[COLLECTION]

    async def list_by_user_id(self, user_id: str) -> list[BrandProfile]:
        cursor = self._collection.find({"userId": user_id}).sort("updatedAt", -1)
        return [BrandProfile.model_validate(strip_id(d)) async for d in cursor]

    async def list_items_by_user_id(self, user_id: str) -> list[BrandListItem]:
        """목록용 가벼운 조회. base64는 **Mongo에서부터 읽지 않는다.**"""
        cursor = self._collection.find({"userId": user_id}, _LIST_PROJECTION).sort(
            "updatedAt", -1
        )
        return [BrandListItem.model_validate(strip_id(d)) async for d in cursor]

    async def find(self, user_id: str, brand_id: str) -> BrandProfile | None:
        document = await self._collection.find_one({"userId": user_id, "brandId": brand_id})
        return BrandProfile.model_validate(strip_id(document)) if document else None

    async def find_light(self, user_id: str, brand_id: str) -> BrandProfile | None:
        """텍스트 필드만 — 이미지·문서의 base64는 **Mongo에서부터 읽지 않는다.**

        브랜드 하나가 2MB인데(이미지 9장) 그 문서 하나를 Atlas에서 읽는 데 **23초**가
        걸렸다(2026-08-07 실측, 3회 22~25초 — 대역폭이 제한된 클러스터다). 자료 편집
        화면은 이름·소개·특징·고객·링크부터 열면 되므로, 무거운 두 필드를 뺀 이 조회로
        먼저 열고 첨부는 뒤따라 받는다.
        """
        document = await self._collection.find_one(
            {"userId": user_id, "brandId": brand_id}, {"images": 0, "documents": 0}
        )
        if not document:
            return None
        document["images"] = []
        document["documents"] = []
        return BrandProfile.model_validate(strip_id(document))

    async def upsert(self, profile: BrandProfile) -> BrandProfile:
        existing = await self.find(profile.user_id, profile.brand_id)
        # 만든 시각은 처음 것을 지킨다 — 고칠 때마다 새로 찍히면 '언제 만든 자료인지'를
        # 잃는다.
        stored = profile.model_copy(
            update={"created_at": existing.created_at if existing else profile.created_at}
        )
        # _id를 brandId로 고정한다. 검증기가 문자열 _id를 요구하고, 같은 자료가 두 번
        # 생기지 않는다.
        await self._collection.update_one(
            {"_id": stored.brand_id},
            {"$set": {"_id": stored.brand_id, **stored.to_wire()}},
            upsert=True,
        )
        return stored

    async def delete(self, user_id: str, brand_id: str) -> bool:
        result = await self._collection.delete_one({"_id": brand_id, "userId": user_id})
        return result.deleted_count > 0
