"""이메일 저장 시 암호화: 블라인드 인덱스(조회·유니크) + AES-GCM 암호문(표시)."""

import base64
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pymongo.errors import DuplicateKeyError

from app.errors import AuthError
from app.modules.auth.email_crypto import (
    EmailCipher,
    EmailCryptoConfigError,
    _derive_key,
    email_cipher_from_env,
)
from app.modules.auth.repository import MongoUserRepository
from app.shared import User

KEYS = {"EMAIL_ENC_KEY": "enc-secret-one", "EMAIL_INDEX_KEY": "index-secret-two"}

# 회전용 새 키(EMAIL_ENC_KEY_2)는 32바이트 canonical base64url만 허용된다.
KEY2 = base64.urlsafe_b64encode(b"\x02" * 32).decode("ascii").rstrip("=")
ROTATED_KEYS = {**KEYS, "EMAIL_ENC_KEY_2": KEY2}


def make_cipher(env: dict[str, str] | None = None) -> EmailCipher:
    return email_cipher_from_env(env or KEYS)


def legacy_aead() -> AESGCM:
    """v3 이전 코드가 쓰던 키(sha256(EMAIL_ENC_KEY)). 이미 저장돼 있는 옛 암호문을
    그대로 흉내 내기 위한 것으로, 이 유도 방식은 바꿀 수 없다."""
    return AESGCM(_derive_key(KEYS["EMAIL_ENC_KEY"]))


def make_v2_token(email: str, context: str = "") -> str:
    """옛 코드의 encrypt()가 저장한 v2 형식(userId AAD)을 재현한다."""
    nonce = os.urandom(12)
    aad = f"blog-it|email|v2|{context}".encode("utf-8")
    sealed = legacy_aead().encrypt(nonce, email.encode("utf-8"), aad)
    return "v2:" + base64.b64encode(nonce + sealed).decode("ascii")


def make_prefixless_token(email: str) -> str:
    """v2 이전의 AAD 없는 최초 형식을 재현한다."""
    nonce = os.urandom(12)
    sealed = legacy_aead().encrypt(nonce, email.encode("utf-8"), None)
    return base64.b64encode(nonce + sealed).decode("ascii")


class TestEmailCipher:
    def test_ciphertext_round_trips(self):
        cipher = make_cipher()
        assert cipher.decrypt(cipher.encrypt("writer@blog-it.test")) == "writer@blog-it.test"

    def test_encryption_is_non_deterministic_but_still_decrypts(self):
        """랜덤 논스라 같은 이메일도 매번 다른 암호문이 된다 — 암호문만 보고 두 계정이
        같은 이메일인지 알 수 없다. 그래도 둘 다 원문으로 복호화된다."""
        cipher = make_cipher()
        first = cipher.encrypt("writer@blog-it.test")
        second = cipher.encrypt("writer@blog-it.test")
        assert first != second
        assert cipher.decrypt(first) == cipher.decrypt(second) == "writer@blog-it.test"

    def test_blind_index_is_deterministic_and_separates_emails(self):
        cipher = make_cipher()
        assert cipher.blind_index("a@b.com") == cipher.blind_index("a@b.com")
        assert cipher.blind_index("a@b.com") != cipher.blind_index("c@d.com")

    def test_a_different_key_cannot_decrypt(self):
        made = make_cipher()
        other = make_cipher({"EMAIL_ENC_KEY": "different", "EMAIL_INDEX_KEY": "index-secret-two"})
        token = made.encrypt("writer@blog-it.test")
        with pytest.raises(ValueError):
            other.decrypt(token)

    def test_ciphertext_is_bound_to_the_user_context(self):
        cipher = make_cipher()
        token = cipher.encrypt("writer@blog-it.test", context="user_1")

        assert cipher.decrypt(token, context="user_1") == "writer@blog-it.test"
        with pytest.raises(ValueError):
            cipher.decrypt(token, context="user_2")

    def test_missing_keys_refuse_to_build(self):
        for env in ({}, {"EMAIL_ENC_KEY": "x"}, {"EMAIL_INDEX_KEY": "y"}):
            with pytest.raises(EmailCryptoConfigError):
                email_cipher_from_env(env)


class FakeUsersCollection:
    """MongoUserRepository가 기대하는 최소한의 컬렉션. emailHash·userId에 유니크
    인덱스가 있다고 가정하고 동작을 흉내 낸다."""

    def __init__(self) -> None:
        self._docs: list[dict] = []

    async def find_one(self, query: dict) -> dict | None:
        for doc in self._docs:
            if all(doc.get(key) == value for key, value in query.items()):
                return dict(doc)
        return None

    async def insert_one(self, document: dict) -> None:
        for doc in self._docs:
            if doc.get("emailHash") == document.get("emailHash") or doc.get("userId") == document.get(
                "userId"
            ):
                raise DuplicateKeyError("duplicate emailHash/userId")
        self._docs.append(dict(document))

    async def update_one(self, query: dict, update: dict):
        for doc in self._docs:
            if all(doc.get(key) == value for key, value in query.items()):
                doc.update(update.get("$set", {}))
                break


class FakeDb:
    def __init__(self, collection: FakeUsersCollection):
        self._collection = collection

    def __getitem__(self, name: str) -> FakeUsersCollection:
        assert name == "users"
        return self._collection


def make_repo() -> tuple[MongoUserRepository, FakeUsersCollection]:
    collection = FakeUsersCollection()
    return MongoUserRepository(FakeDb(collection), make_cipher()), collection


def a_user(email: str = "Writer@Blog-It.test") -> User:
    return User(
        user_id="user_1",
        email=email,
        password_hash="scrypt$aa$bb",
        created_at="2026-07-20T00:00:00.000Z",
        updated_at="2026-07-20T00:00:00.000Z",
    )


class TestMongoUserRepositoryEncryption:
    async def test_create_stores_no_plaintext_email(self):
        repo, collection = make_repo()
        await repo.create(a_user())

        stored = collection._docs[0]
        assert "email" not in stored
        assert stored["emailHash"]
        assert stored["emailEnc"]
        assert stored["emailEnc"].startswith("v3:1:")
        # 저장된 어떤 문자열에도 평문 이메일이 남지 않는다.
        assert "writer@blog-it.test" not in repr(stored)

    async def test_find_by_email_normalizes_and_decrypts(self):
        repo, _ = make_repo()
        await repo.create(a_user())

        found = await repo.find_by_email("writer@blog-it.test")
        assert found is not None
        # 대소문자가 달라도 정규화 후 같은 블라인드 인덱스라 찾힌다.
        assert (await repo.find_by_email("WRITER@blog-it.TEST")) is not None
        assert found.email == "writer@blog-it.test"

    async def test_find_by_user_id_decrypts_email(self):
        repo, _ = make_repo()
        await repo.create(a_user())

        found = await repo.find_by_user_id("user_1")
        assert found is not None and found.email == "writer@blog-it.test"

    async def test_duplicate_email_raises_email_already_exists(self):
        repo, _ = make_repo()
        await repo.create(a_user())

        with pytest.raises(AuthError) as excinfo:
            await repo.create(a_user().model_copy(update={"user_id": "user_2"}))
        assert excinfo.value.code == "EMAIL_ALREADY_EXISTS"

    async def test_legacy_plaintext_document_still_reads(self):
        """마이그레이션 전 옛 평문 문서도 로그인·표시가 계속 돼야 한다."""
        repo, collection = make_repo()
        collection._docs.append(
            {
                "userId": "legacy_1",
                "email": "old@blog-it.test",
                "passwordHash": "scrypt$aa$bb",
                "createdAt": "2026-07-01T00:00:00.000Z",
                "updatedAt": "2026-07-01T00:00:00.000Z",
            }
        )

        by_email = await repo.find_by_email("old@blog-it.test")
        by_id = await repo.find_by_user_id("legacy_1")
        assert by_email is not None and by_email.email == "old@blog-it.test"
        assert by_id is not None and by_id.email == "old@blog-it.test"

    async def test_legacy_aadless_ciphertext_is_upgraded_after_read(self):
        repo, collection = make_repo()
        cipher = make_cipher()
        email = "legacy-encrypted@blog-it.test"
        legacy = make_prefixless_token(email)
        collection._docs.append(
            {
                "_id": "mongo_legacy",
                "userId": "legacy_encrypted",
                "emailHash": cipher.blind_index(email),
                "emailEnc": legacy,
                "passwordHash": "scrypt$aa$bb",
                "createdAt": "2026-07-01T00:00:00.000Z",
                "updatedAt": "2026-07-01T00:00:00.000Z",
            }
        )

        found = await repo.find_by_user_id("legacy_encrypted")

        assert found is not None and found.email == email
        upgraded = collection._docs[0]["emailEnc"]
        assert upgraded.startswith("v3:1:")
        assert cipher.decrypt(upgraded, context="legacy_encrypted") == email

    async def test_v2_ciphertext_reads_but_is_not_rewritten_on_read(self):
        """버전 있는 암호문(v2)은 읽기 경로에서 조용히 재저장하지 않는다 — 재암호화는
        rotate_email_key.py의 명시적 절차만 한다(읽을 때마다의 재저장은 롤백을 어렵게 한다)."""
        repo, collection = make_repo()
        email = "v2-encrypted@blog-it.test"
        stored_token = make_v2_token(email, context="v2_user")
        collection._docs.append(
            {
                "_id": "mongo_v2",
                "userId": "v2_user",
                "emailHash": make_cipher().blind_index(email),
                "emailEnc": stored_token,
                "passwordHash": "scrypt$aa$bb",
                "createdAt": "2026-08-01T00:00:00.000Z",
                "updatedAt": "2026-08-01T00:00:00.000Z",
            }
        )

        found = await repo.find_by_user_id("v2_user")

        assert found is not None and found.email == email
        assert collection._docs[0]["emailEnc"] == stored_token


class TestKeyRotation:
    """Windows Server 이전 대비: EMAIL_ENC_KEY를 kid로 회전해도 저장된 모든 형식
    (AAD 없는 최초 형식·v2·이전 kid의 v3)이 계속 읽혀야 한다."""

    def test_stored_v2_and_prefixless_tokens_still_decrypt(self):
        """지금 Atlas에 저장돼 있는 v2(userId AAD) 암호문 회귀 방어 — 이 테스트가
        깨지면 배포 순간 기존 계정 로그인·표시가 전부 깨진다."""
        cipher = make_cipher()
        v2 = make_v2_token("writer@blog-it.test", context="user_1")
        assert cipher.decrypt(v2, context="user_1") == "writer@blog-it.test"
        assert cipher.decrypt(make_prefixless_token("old@blog-it.test")) == "old@blog-it.test"

    def test_new_key_encrypts_while_old_formats_remain_readable(self):
        before = make_cipher()  # 회전 전: kid 1만
        old_v3 = before.encrypt("writer@blog-it.test", context="user_1")
        assert old_v3.startswith("v3:1:")

        rotated = make_cipher(ROTATED_KEYS)  # EMAIL_ENC_KEY_2 추가 후
        fresh = rotated.encrypt("writer@blog-it.test", context="user_1")
        assert fresh.startswith("v3:2:")
        assert rotated.decrypt(fresh, context="user_1") == "writer@blog-it.test"
        # 회전 뒤에도 이전 kid·v2 암호문이 그대로 읽힌다.
        assert rotated.decrypt(old_v3, context="user_1") == "writer@blog-it.test"
        v2 = make_v2_token("writer@blog-it.test", context="user_1")
        assert rotated.decrypt(v2, context="user_1") == "writer@blog-it.test"

    def test_blind_index_is_stable_across_rotation(self):
        """인덱스 키는 회전 대상이 아니다 — 회전 뒤에도 조회·유니크 값이 같아야
        기존 emailHash로 로그인을 찾을 수 있다."""
        assert (
            make_cipher().blind_index("a@b.com")
            == make_cipher(ROTATED_KEYS).blind_index("a@b.com")
        )

    def test_needs_rewrap_identifies_stale_formats(self):
        rotated = make_cipher(ROTATED_KEYS)
        assert rotated.needs_rewrap(make_v2_token("a@b.com"))
        assert rotated.needs_rewrap(make_prefixless_token("a@b.com"))
        assert rotated.needs_rewrap(make_cipher().encrypt("a@b.com"))  # v3:1:
        assert not rotated.needs_rewrap(rotated.encrypt("a@b.com"))

    def test_unknown_kid_is_reported_not_garbled(self):
        """옛 키를 너무 일찍 지운 배포 실수를 어느 키가 없는지 알 수 있는 문구로 드러낸다."""
        token = make_cipher(ROTATED_KEYS).encrypt("writer@blog-it.test")
        with pytest.raises(ValueError, match="key id 2"):
            make_cipher().decrypt(token)

    def test_ring_without_kid1_cannot_read_legacy_formats(self):
        """회전을 끝내고 kid 1을 제거한 최종 상태 — 새 암호화는 되고, v2·무접두는
        (rotate_email_key.py 잔여 0 확인 없이 지웠다면) 명시적 오류로 드러난다."""
        cipher = make_cipher(
            {"EMAIL_ENC_KEY_2": KEY2, "EMAIL_INDEX_KEY": "index-secret-two"}
        )
        assert cipher.encrypt("a@b.com").startswith("v3:2:")
        with pytest.raises(ValueError):
            cipher.decrypt(make_v2_token("a@b.com"))

    def test_rotation_key_must_be_canonical_base64url(self):
        """새 키(kid≥2)는 약한 문구를 받지 않는다 — 비 canonical 표기도 오타로 본다."""
        for bad in ("passphrase-key", "x" * 43):
            with pytest.raises(EmailCryptoConfigError):
                make_cipher({**KEYS, "EMAIL_ENC_KEY_2": bad})

    def test_misspelled_or_conflicting_key_names_refuse_to_build(self):
        with pytest.raises(EmailCryptoConfigError):
            make_cipher({**KEYS, "EMAIL_ENC_KEY_01": KEY2})  # 앞자리 0 — 오타로 본다
        with pytest.raises(EmailCryptoConfigError):
            make_cipher({**KEYS, "EMAIL_ENC_KEY_1": "other-value"})  # kid 1 값 충돌
