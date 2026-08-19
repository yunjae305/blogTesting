"""emailEnc를 활성 key id의 v3 형식으로 재암호화한다 — 이메일 암호화 키 회전의 마무리.

회전 절차(docs/Windows-Server-보안-배포.md '키 회전 원칙'):
  1) vault/.env에 ``EMAIL_ENC_KEY_<n>``(정확히 32바이트 canonical base64url)을 추가하고
     서버를 재기동한다. 이때부터 새 가입은 새 키(v3:<n>:)로 저장되고, 옛 kid·v2·AAD 없는
     암호문도 계속 읽힌다.
  2) 이 스크립트를 dry-run으로 실행해 대상 수를 확인하고 ``--apply``로 재암호화한다.
  3) 잔여 0을 확인한 뒤에만 옛 EMAIL_ENC_KEY(_n)을 설정에서 제거한다.

활성 키가 kid 1 그대로여도 v2·AAD 없는 암호문을 v3 형식으로 승격하는 용도로 쓸 수 있다
(옛 migrate_email_ciphertext_v2.py의 역할을 흡수했다).

EMAIL_INDEX_KEY는 회전하지 않는다 — 블라인드 인덱스는 결정적이어야 해서 바꾸려면 전체
emailHash 재계산 마이그레이션이 따로 필요하다. 대신 쓰기 전에 모든 문서의 emailHash가
현재 인덱스 키와 일치하는지 확인하고, 하나라도 다르면 아무것도 바꾸지 않고 중단한다.

값·이메일·사용자 id는 출력하지 않는다. 기본은 dry-run, 적용은 ``--apply``.
행별 CAS(update 조건에 기존 암호문 포함)로만 쓰므로 유지보수 창 없이 실행해도 안전하고,
중간에 실패하면 재실행하면 된다.
"""

import asyncio
import sys
from pathlib import Path

# Windows 콘솔은 기본 cp949라 한글·특수문자를 못 찍고 크래시한다. 출력만의 문제이므로
# UTF-8로 고정하고, 못 찍는 글자는 대체한다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from app.config import load_env_file, mongodb_uri  # noqa: E402
from app.modules.auth.email_crypto import email_cipher_from_env  # noqa: E402


async def rotate(*, apply: bool) -> None:
    load_env_file()
    cipher = email_cipher_from_env()  # 키가 없거나 형식이 틀리면 여기서 멈춘다.
    client = AsyncIOMotorClient(mongodb_uri(), serverSelectionTimeoutMS=5_000)
    try:
        await client.admin.command("ping")
        collection = client.get_default_database()["users"]
        documents = [
            document
            async for document in collection.find(
                {"emailEnc": {"$exists": True}},
                {"userId": 1, "emailEnc": 1, "emailHash": 1},
            )
        ]

        # 쓰기 전에 전 문서를 현재 키 설정으로 검증한다: 복호화가 되고(암호화 키가 맞고)
        # 블라인드 인덱스가 일치해야(인덱스 키가 맞아야) 재암호화를 시작할 자격이 있다.
        stale: list[tuple[object, str, str, str]] = []
        for document in documents:
            user_id = str(document.get("userId") or "")
            token = str(document.get("emailEnc") or "")
            if not user_id or not token:
                raise RuntimeError("userId/emailEnc가 비어 있는 사용자 문서가 있습니다.")
            email = cipher.decrypt(token, context=user_id)
            if cipher.blind_index(email) != document.get("emailHash"):
                raise RuntimeError(
                    "emailHash가 현재 EMAIL_INDEX_KEY와 일치하지 않는 문서가 있어 "
                    "중단합니다 — 인덱스 키가 바뀌지 않았는지 확인하십시오."
                )
            if cipher.needs_rewrap(token):
                stale.append((document["_id"], user_id, token, email))

        print(
            f"emailEnc 전체 {len(documents)}건 · 활성 kid {cipher.active_kid} · "
            f"재암호화 대상 {len(stale)}건 · mode={'apply' if apply else 'dry-run'}"
        )
        if not apply:
            return

        rotated = 0
        for document_id, user_id, old, email in stale:
            wrapped = cipher.encrypt(email, context=user_id)
            if cipher.decrypt(wrapped, context=user_id) != email:
                raise RuntimeError("재암호화 자체 검증에 실패했습니다.")
            result = await collection.update_one(
                {"_id": document_id, "emailEnc": old},
                {"$set": {"emailEnc": wrapped}},
            )
            rotated += int(result.modified_count)

        remaining = await collection.count_documents(
            {
                "emailEnc": {
                    "$exists": True,
                    "$not": {"$regex": f"^v3:{cipher.active_kid}:"},
                }
            }
        )
        plaintext = await collection.count_documents({"email": {"$exists": True}})
        print(
            f"재암호화 {rotated}건 · 잔여(비활성 형식) {remaining}건 · "
            f"평문 email {plaintext}건"
        )
        if remaining:
            raise RuntimeError(
                "활성 키 형식이 아닌 emailEnc가 남아 있습니다 — 재실행으로 수렴시키고, "
                "잔여 0이 되기 전에는 옛 키를 제거하면 안 됩니다."
            )
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(rotate(apply="--apply" in sys.argv))
