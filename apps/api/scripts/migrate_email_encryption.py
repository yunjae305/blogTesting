"""users 컬렉션의 평문 email을 암호화 형식으로 옮기는 1회 마이그레이션.

각 문서에 대해:
  - emailHash = HMAC(정규화 email)  → 조회·유니크
  - emailEnc  = AES-GCM(정규화 email) → 표시·발송용 복호화
  - 평문 email 필드 제거

그리고 유니크 인덱스를 email → emailHash로 교체한다.

기본은 **dry-run**(아무것도 바꾸지 않고 계획만 출력). 실제 적용은 API와 가입 쓰기를
중단한 유지보수 창에서 ``--apply --maintenance-confirm``으로만 허용한다.

인덱스 교체 순서가 중요하다: email 필드를 지우기 전에 uniq_email을 먼저 없애야 한다.
non-sparse 유니크 인덱스라, email을 여러 문서에서 지우면 전부 null이 되어 충돌한다.
그래서 (1) 해시·암호문 추가 → (2) emailHash 유니크 인덱스 생성 → (3) uniq_email 드롭
→ (4) email 필드 제거 순으로, 항상 어느 한쪽 유니크 제약이 살아 있게 한다.

실행(저장소 루트에서):
  python apps/api/scripts/migrate_email_encryption.py            # dry-run
  python apps/api/scripts/migrate_email_encryption.py --apply --maintenance-confirm
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Windows 콘솔은 기본 cp949라 이 스크립트의 한글·특수문자(— 등)를 못 찍고 크래시한다.
# 출력만의 문제이므로 UTF-8로 고정하고, 못 찍는 글자는 대체한다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# apps/api 를 import 경로에 올린다 (이 파일은 apps/api/scripts/).
API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from pymongo.errors import OperationFailure  # noqa: E402

from app.config import load_env_file, mongodb_uri  # noqa: E402
from app.modules.auth.email_crypto import email_cipher_from_env  # noqa: E402
from app.modules.auth.repository import normalize_email  # noqa: E402

OLD_EMAIL_INDEX = "uniq_email"
NEW_HASH_INDEX = "uniq_emailHash"


class MigrationSafetyError(RuntimeError):
    """스냅샷과 현재 문서가 다르면 평문 삭제 전에 마이그레이션을 중단한다."""


@dataclass(frozen=True)
class EmailMigrationPlan:
    document_id: Any
    original_email: str
    user_id: str
    normalized_email: str
    email_hash: str
    email_enc: str

    @property
    def encrypted_fields(self) -> dict[str, str]:
        return {"emailHash": self.email_hash, "emailEnc": self.email_enc}

    @property
    def exact_staged_filter(self) -> dict[str, Any]:
        return {
            "_id": self.document_id,
            "email": self.original_email,
            "userId": self.user_id,
            "emailHash": self.email_hash,
            "emailEnc": self.email_enc,
        }


def _build_plans(cipher, documents: list[dict[str, Any]]) -> list[EmailMigrationPlan]:
    """전체 스냅샷이 유효한지 먼저 확인한 뒤 쓰기 계획을 만든다."""
    plans: list[EmailMigrationPlan] = []
    document_ids: set[Any] = set()
    hashes: set[str] = set()
    for document in documents:
        document_id = document.get("_id")
        original_email = document.get("email")
        user_id = document.get("userId")
        if document_id is None or document_id in document_ids:
            raise MigrationSafetyError("이메일 이관 스냅샷의 문서 식별자가 유효하지 않습니다.")
        if not isinstance(original_email, str) or not original_email.strip():
            raise MigrationSafetyError("평문 email 필드가 문자열이 아닌 대상이 있습니다.")
        if not isinstance(user_id, str) or not user_id.strip():
            raise MigrationSafetyError("userId가 없는 대상은 안전하게 이메일을 이관할 수 없습니다.")

        normalized = normalize_email(original_email)
        email_hash = cipher.blind_index(normalized)
        if email_hash in hashes:
            raise MigrationSafetyError("정규화 후 중복되는 이메일 대상이 있습니다.")
        document_ids.add(document_id)
        hashes.add(email_hash)
        plans.append(
            EmailMigrationPlan(
                document_id=document_id,
                original_email=original_email,
                user_id=user_id,
                normalized_email=normalized,
                email_hash=email_hash,
                email_enc=cipher.encrypt(normalized, context=user_id),
            )
        )
    return plans


async def _stage_encrypted_fields(collection, plans: list[EmailMigrationPlan]) -> None:
    """스냅샷의 정확한 원문이 그대로인 행에만 암호문을 기록한다."""
    for plan in plans:
        result = await collection.update_one(
            {
                "_id": plan.document_id,
                "email": plan.original_email,
                "userId": plan.user_id,
            },
            {"$set": plan.encrypted_fields},
        )
        if result.matched_count != 1 or result.modified_count != 1:
            raise MigrationSafetyError(
                "이메일 이관 대상이 스냅샷 이후 변경되어 암호문 기록을 중단했습니다."
            )


async def _verify_staged_documents(collection, cipher, plans: list[EmailMigrationPlan]) -> None:
    """모든 대상의 원문과 계획한 *정확한* 암호문을 평문 삭제 전에 재검증한다."""
    expected_ids = [plan.document_id for plan in plans]
    if expected_ids:
        unexpected = await collection.count_documents(
            {"email": {"$exists": True}, "_id": {"$nin": expected_ids}}
        )
        if unexpected:
            raise MigrationSafetyError(
                "스냅샷 이후 새 평문 이메일 문서가 생겨 삭제 단계를 중단했습니다."
            )

    for plan in plans:
        document = await collection.find_one(
            {"_id": plan.document_id},
            {"email": 1, "userId": 1, "emailHash": 1, "emailEnc": 1},
        )
        if not document or any(
            (
                document.get("email") != plan.original_email,
                document.get("userId") != plan.user_id,
                document.get("emailHash") != plan.email_hash,
                document.get("emailEnc") != plan.email_enc,
            )
        ):
            raise MigrationSafetyError(
                "이메일 이관 대상의 원문 또는 암호문이 계획과 달라 삭제 단계를 중단했습니다."
            )
        try:
            decrypted = cipher.decrypt(plan.email_enc, context=plan.user_id)
        except ValueError as error:
            raise MigrationSafetyError(
                "이메일 암호문 인증 검증에 실패해 삭제 단계를 중단했습니다."
            ) from error
        if (
            decrypted != plan.normalized_email
            or cipher.blind_index(decrypted) != plan.email_hash
        ):
            raise MigrationSafetyError(
                "이메일 암호문 복호 결과가 계획과 달라 삭제 단계를 중단했습니다."
            )


async def _unset_verified_plaintext(collection, plans: list[EmailMigrationPlan]) -> int:
    """최종 CAS가 일치하는 검증 완료 행에서만 평문을 하나씩 제거한다."""
    modified = 0
    for plan in plans:
        result = await collection.update_one(
            plan.exact_staged_filter,
            {"$unset": {"email": ""}},
        )
        if result.matched_count != 1 or result.modified_count != 1:
            raise MigrationSafetyError(
                "최종 검증 뒤 대상이 변경되어 해당 행의 평문을 지우지 않고 중단했습니다."
            )
        modified += 1
    return modified


async def _relax_validator(db) -> None:
    """users 컬렉션의 $jsonSchema에서 email 필수를 풀고 emailHash·emailEnc를 넣는다.
    기존 스키마를 읽어 최소한만 바꾼다 — 다른 제약은 그대로 둔다. 검증기가 없으면 무시."""
    options = None
    cursor = await db.list_collections(filter={"name": "users"})
    async for coll in cursor:
        options = coll.get("options", {})
    schema = ((options or {}).get("validator") or {}).get("$jsonSchema")
    if not schema:
        print("검증기 없음 — 스키마 갱신 건너뜀")
        return

    required = [field for field in schema.get("required", []) if field != "email"]
    for field in ("emailHash", "emailEnc"):
        if field not in required:
            required.append(field)
    properties = dict(schema.get("properties", {}))
    properties.setdefault("emailHash", {"bsonType": "string"})
    properties.setdefault("emailEnc", {"bsonType": "string"})

    new_schema = {**schema, "required": required, "properties": properties}
    await db.command({"collMod": "users", "validator": {"$jsonSchema": new_schema}})
    print("검증기 갱신: required에서 email 제거, emailHash·emailEnc 추가")


async def migrate(apply: bool, *, maintenance_confirmed: bool = False) -> None:
    if apply and not maintenance_confirmed:
        raise MigrationSafetyError(
            "적용 전 API와 가입 쓰기를 중단하고 --maintenance-confirm을 함께 지정해야 합니다."
        )
    load_env_file()
    cipher = email_cipher_from_env()  # 키가 없으면 여기서 멈춘다.

    client = AsyncIOMotorClient(mongodb_uri(), serverSelectionTimeoutMS=5000)
    try:
        await client.admin.command("ping")
        db = client.get_default_database()
        col = db["users"]

        mode = "APPLY(실제 적용)" if apply else "DRY-RUN(변경 없음)"
        print(f"=== 이메일 암호화 마이그레이션 · {mode} ===")
        if apply:
            print("유지보수 확인됨: API와 가입 쓰기가 중단된 상태에서만 계속하십시오.")

        plaintext = [doc async for doc in col.find({"email": {"$exists": True}})]
        plans = _build_plans(cipher, plaintext)
        already = await col.count_documents(
            {"emailEnc": {"$exists": True}, "email": {"$exists": False}}
        )
        print(f"대상(평문 email 있음): {len(plans)}개 · 이미 암호화됨: {already}개\n")

        if plans:
            print(
                f"emailHash/emailEnc 계산 대상: {len(plans)}개 "
                "(계정 식별자는 출력하지 않음)"
            )

        if not apply:
            print("\n인덱스 계획: uniq_emailHash(unique) 생성 → uniq_email 드롭")
            print("검증기 계획: $jsonSchema required에서 email 제거, emailHash·emailEnc 추가")
            print(
                "\n(dry-run이라 아무것도 바꾸지 않았습니다. 실제 적용은 "
                "--apply --maintenance-confirm)"
            )
            return

        if not plans:
            print("평문 이메일 대상이 없어 변경하지 않았습니다.")
            return

        # (1) 원문 email/userId CAS로 암호문을 기록하고 전 행을 정확히 재검증한다.
        await _stage_encrypted_fields(col, plans)
        await _verify_staged_documents(col, cipher, plans)

        # (2) emailHash 유니크 인덱스 생성 (email이 유니크였으므로 해시도 유니크).
        await col.create_index("emailHash", unique=True, name=NEW_HASH_INDEX)
        print(f"\n인덱스 생성: {NEW_HASH_INDEX}(unique)")

        # (3) 옛 email 유니크 인덱스 드롭. IndexNotFound만 재실행으로 간주한다.
        try:
            await col.drop_index(OLD_EMAIL_INDEX)
            print(f"인덱스 드롭: {OLD_EMAIL_INDEX}")
        except OperationFailure as error:
            if error.code != 27:
                raise
            print(f"인덱스 {OLD_EMAIL_INDEX} 없음(이미 드롭됨) — 건너뜀")

        await _relax_validator(db)

        # 스키마/인덱스 작업 사이의 변경까지 다시 확인한 뒤 exact CAS로만 지운다.
        await _verify_staged_documents(col, cipher, plans)
        removed = await _unset_verified_plaintext(col, plans)
        print(f"평문 email 필드 제거: {removed}개")

        remaining = await col.count_documents({"email": {"$exists": True}})
        if remaining:
            raise MigrationSafetyError(
                "평문 이메일이 남아 있어 마이그레이션을 완료로 표시하지 않습니다."
            )
        print("\n완료. 검증:")
        print(f"  평문 남은 수: {remaining}")
        print(
            "  암호화된 수 : "
            f"{await col.count_documents({'emailEnc': {'$exists': True}})}"
        )
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="실제 DB 변경 적용")
    parser.add_argument(
        "--maintenance-confirm",
        action="store_true",
        help="API와 가입 쓰기를 중단한 유지보수 창임을 확인",
    )
    arguments = parser.parse_args()
    try:
        asyncio.run(
            migrate(
                apply=arguments.apply,
                maintenance_confirmed=arguments.maintenance_confirm,
            )
        )
    except MigrationSafetyError as error:
        print(f"안전 검증 실패: {error}", file=sys.stderr)
        raise SystemExit(2) from None
