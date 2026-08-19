"""사용자 저장소.repository.ts."""

from typing import Protocol

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.db.mongo import strip_id
from app.errors import AuthError
from app.shared import User

from .email_crypto import VERSIONED_CIPHERTEXT_PREFIXES, EmailCipher


def normalize_email(email: str) -> str:
    return email.strip().lower()


class UserRepository(Protocol):
    async def find_by_email(self, email: str) -> User | None: ...
    async def find_by_user_id(self, user_id: str) -> User | None: ...
    async def create(self, user: User) -> User: ...
    async def set_nickname(self, user_id: str, nickname: str) -> None: ...
    async def update_password_hash(
        self, user_id: str, password_hash: str, updated_at: str
    ) -> None: ...


class InMemoryUserRepository:
    def __init__(self) -> None:
        self._by_user_id: dict[str, User] = {}

    async def find_by_email(self, email: str) -> User | None:
        target = normalize_email(email)
        for user in self._by_user_id.values():
            if user.email == target:
                return user
        return None

    async def find_by_user_id(self, user_id: str) -> User | None:
        return self._by_user_id.get(user_id)

    async def create(self, user: User) -> User:
        stored = user.model_copy(update={"email": normalize_email(user.email)})
        if await self.find_by_email(stored.email):
            raise AuthError("EMAIL_ALREADY_EXISTS", f"user {stored.email} already exists")
        self._by_user_id[stored.user_id] = stored
        return stored

    async def set_nickname(self, user_id: str, nickname: str) -> None:
        user = self._by_user_id.get(user_id)
        if user is not None:
            self._by_user_id[user_id] = user.model_copy(update={"nickname": nickname})

    async def update_password_hash(
        self, user_id: str, password_hash: str, updated_at: str
    ) -> None:
        user = self._by_user_id.get(user_id)
        if user is not None:
            self._by_user_id[user_id] = user.model_copy(
                update={"password_hash": password_hash, "updated_at": updated_at}
            )


class MongoUserRepository:
    """이메일은 평문으로 저장하지 않는다. 문서에는 조회·유니크용 ``emailHash``(블라인드
    인덱스)와 표시·발송용 ``emailEnc``(AES-GCM 암호문)만 넣고, 읽을 때 복호화한다.

    마이그레이션 전 옛 평문 문서(``email`` 필드만 있는 것)도 계속 로그인·표시되도록,
    조회는 해시 → 평문 순으로 시도하고 읽기는 둘 다 받아들인다. 새로 쓰는 문서는 항상
    암호화 형식이다.
    """

    def __init__(self, db: AsyncIOMotorDatabase, cipher: EmailCipher):
        self._collection = db["users"]
        self._cipher = cipher

    def _to_user(self, document: dict | None) -> User | None:
        stripped = strip_id(document)
        if stripped is None:
            return None
        # 새 문서는 emailEnc를 복호화, 옛 문서는 평문 email 그대로.
        encrypted = stripped.get("emailEnc")
        email = (
            self._cipher.decrypt(encrypted, context=stripped["userId"])
            if encrypted
            else stripped.get("email", "")
        )
        return User(
            user_id=stripped["userId"],
            email=email,
            # 옛 문서에는 없다 — 기본값 "".
            nickname=stripped.get("nickname", ""),
            password_hash=stripped["passwordHash"],
            created_at=stripped["createdAt"],
            updated_at=stripped["updatedAt"],
        )

    async def find_by_email(self, email: str) -> User | None:
        normalized = normalize_email(email)
        document = await self._collection.find_one(
            {"emailHash": self._cipher.blind_index(normalized)}
        )
        if document is None:
            # 마이그레이션 전 옛 평문 문서 호환. 변환 후에는 이 조회가 아무것도 못 찾는다.
            document = await self._collection.find_one({"email": normalized})
        user = self._to_user(document)
        await self._upgrade_legacy_ciphertext(document, user)
        return user

    async def find_by_user_id(self, user_id: str) -> User | None:
        document = await self._collection.find_one({"userId": user_id})
        user = self._to_user(document)
        await self._upgrade_legacy_ciphertext(document, user)
        return user

    async def _upgrade_legacy_ciphertext(self, document: dict | None, user: User | None) -> None:
        """AAD가 없던 최초 형식만 읽은 직후 현재 형식으로 승격한다. v2·v3처럼 버전이
        있는 암호문은 읽기 경로에서 다시 쓰지 않는다 — 키 회전 재암호화는 명시적으로
        scripts/rotate_email_key.py가 한다(읽을 때마다의 조용한 재저장은 코드 롤백을
        어렵게 만들 뿐이다)."""
        if document is None or user is None:
            return
        encrypted = document.get("emailEnc")
        if not encrypted or encrypted.startswith(VERSIONED_CIPHERTEXT_PREFIXES):
            return
        upgraded = self._cipher.encrypt(user.email, context=user.user_id)
        await self._collection.update_one(
            {"userId": user.user_id, "emailEnc": encrypted},
            {"$set": {"emailEnc": upgraded}},
        )

    async def create(self, user: User) -> User:
        normalized = normalize_email(user.email)
        # 평문 email 필드는 넣지 않는다 — 해시(조회·유니크)와 암호문(복호화)만 저장.
        document = {
            "userId": user.user_id,
            "nickname": user.nickname,
            "emailHash": self._cipher.blind_index(normalized),
            "emailEnc": self._cipher.encrypt(normalized, context=user.user_id),
            "passwordHash": user.password_hash,
            "createdAt": user.created_at,
            "updatedAt": user.updated_at,
        }
        try:
            await self._collection.insert_one(document)
        except DuplicateKeyError:
            # 같은 이메일로 동시에 가입하는 두 요청 사이를 막는 것은 유니크 인덱스뿐이다.
            # 유니크 인덱스는 이제 emailHash에 걸어야 한다(마이그레이션 단계에서 email →
            # emailHash로 교체). Node 원본은 여기서 평범한 Error를 던졌고, HTTP 계층은
            # 그걸 409가 아니라 400으로 보고했다.
            raise AuthError("EMAIL_ALREADY_EXISTS", f"user {normalized} already exists") from None
        return user.model_copy(update={"email": normalized})

    async def set_nickname(self, user_id: str, nickname: str) -> None:
        await self._collection.update_one({"userId": user_id}, {"$set": {"nickname": nickname}})

    async def update_password_hash(
        self, user_id: str, password_hash: str, updated_at: str
    ) -> None:
        await self._collection.update_one(
            {"userId": user_id},
            {"$set": {"passwordHash": password_hash, "updatedAt": updated_at}},
        )
