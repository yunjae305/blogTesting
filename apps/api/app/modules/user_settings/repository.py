"""사용자 설정 저장소.repository.ts.
"""

from typing import Protocol

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db.mongo import strip_id
from app.shared import UserSettings

# 사용자가 비워 두어 지울 수 있는 필드. None으로 도착하면 저장된 값을 남겨 두지 말고
# 지워야 한다.
CLEARABLE_FIELDS = ("customPersonaName", "customPersonaDescription", "customPersona")


class UserSettingsRepository(Protocol):
    async def find_by_user_id(self, user_id: str) -> UserSettings | None: ...
    async def upsert(self, settings: UserSettings) -> UserSettings: ...


class InMemoryUserSettingsRepository:
    def __init__(self) -> None:
        self._by_user_id: dict[str, UserSettings] = {}

    async def find_by_user_id(self, user_id: str) -> UserSettings | None:
        return self._by_user_id.get(user_id)

    async def upsert(self, settings: UserSettings) -> UserSettings:
        existing = self._by_user_id.get(settings.user_id)
        stored = settings.model_copy(
            update={"created_at": existing.created_at if existing else settings.created_at}
        )
        self._by_user_id[settings.user_id] = stored
        return stored


class MongoUserSettingsRepository:
    """설정은 `users` 문서 안의 ``settings`` 서브도큐먼트에 산다.

    예전에는 별도 `userSettings` 컬렉션이었다. 관계가 userId 유니크 인덱스가 걸린 완전한
    1:1이라 컬렉션을 나눌 이유가 없었고 — 설정을 읽을 때마다 쿼리가 한 번 더 나갔다.
    사용자가 사라지면 설정도 같이 사라져야 하는데, 나뉘어 있으면 그 정합성을 코드가
    직접 지켜야 한다는 점도 있었다.

    ``userId``는 서브도큐먼트에 넣지 않는다 — 담고 있는 문서가 이미 그 답이고, 두 군데에
    두면 어긋날 수 있는 자리가 하나 생긴다. 읽을 때 채워 모델을 완성한다.
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        self._collection = db["users"]

    async def find_by_user_id(self, user_id: str) -> UserSettings | None:
        document = await self._collection.find_one(
            {"userId": user_id}, {"settings": 1, "_id": 0}
        )
        settings = (document or {}).get("settings")
        if not settings:
            return None
        # userId는 저장하지 않으므로 여기서 채운다.
        return UserSettings.model_validate({**strip_id(settings), "userId": user_id})

    async def upsert(self, settings: UserSettings) -> UserSettings:
        existing = await self.find_by_user_id(settings.user_id)
        stored = settings.model_copy(
            update={"created_at": existing.created_at if existing else settings.created_at}
        )

        # to_wire()는 None 필드를 버리므로, 지운 페르소나는 $set에서 그냥 빠지고 옛 값이
        # 살아남는다. 실제로 지우는 것은 $unset이다.
        document = {
            field: value for field, value in stored.to_wire().items() if field != "userId"
        }
        update: dict = {"$set": {f"settings.{k}": v for k, v in document.items()}}
        unset = {
            f"settings.{field}": "" for field in CLEARABLE_FIELDS if field not in document
        }
        if unset:
            update["$unset"] = unset

        # upsert하지 않는다. 예전 컬렉션에서는 설정 문서를 새로 만들면 그만이었지만, 지금
        # 대상은 users 문서다 — 없는 사용자에게 upsert하면 passwordHash·emailHash가 없는
        # 반쪽짜리 계정 문서가 생기고(스키마 검증에도 걸린다), 그건 설정 저장이 할 일이
        # 아니다. 인증을 통과한 요청만 여기 오므로 사용자 문서는 이미 존재한다.
        result = await self._collection.update_one({"userId": stored.user_id}, update)
        if result.matched_count == 0:
            raise LookupError(f"user {stored.user_id} not found")
        return stored
