"""기본 브랜드 자료(AIONA)를 **지금 정의로 다시 덮어쓴다**(2026-08-19).

    python apps/api/scripts/seed_aiona_brand.py --email 내계정@example.com
    python apps/api/scripts/seed_aiona_brand.py --email ... --apply

평소에는 쓸 일이 없다. AIONA 자료는 **처음부터 DB에 있다** — 브랜드 목록을 처음 열 때
서버가 만들어 둔다(`BrandService.ensure_default_brands`). 이 스크립트는 그 자동 생성이
하지 않는 한 가지, **이미 만들어진 자료를 최신 정의로 되돌리는 일**을 한다.

자동 생성이 덮어쓰지 않는 이유가 곧 이 스크립트가 필요한 이유다: 사용자가 이미지·문구를
고쳐 두었는데 조회할 때마다 코드가 원래 값으로 되돌리면 그 편집이 사라진다. 그래서
덮어쓰기는 **사람이 눈으로 확인하고** 한다 — ``--apply`` 없이 돌리면 무엇이 들어갈지
보여 주기만 한다.

**올려 둔 이미지는 건드리지 않는다.** 마스코트·서비스 화면은 화면에서 올리는 것이고,
정의를 다시 씌운다고 그것까지 지워지면 안 된다.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).parent))
from _env import mongodb_uri  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.modules.auth.email_crypto import email_cipher_from_env  # noqa: E402
from app.modules.auth.repository import normalize_email  # noqa: E402
from app.modules.brand import (  # noqa: E402
    DEFAULT_BRAND_ID,
    DEFAULT_BRAND_NAME,
    DEFAULTS_REVISION,
    BrandService,
    MongoBrandRepository,
    default_brand_body,
)

URI = mongodb_uri()


async def resolve_user_id(database, email: str) -> str:
    """이메일로 사용자를 찾는다.

    이메일은 평문으로 저장되지 않는다 — 조회는 HMAC 블라인드 인덱스(``emailHash``)로
    한다. 그래서 이 스크립트도 서버와 **같은 키**(.env의 EMAIL_ENC/INDEX 키)를 읽어야
    한다. 키가 다르면 계정을 못 찾고, 그때는 "없는 계정"과 구분되지 않는다.
    """
    normalized = normalize_email(email)
    cipher = email_cipher_from_env()
    document = await database["users"].find_one(
        {"emailHash": cipher.blind_index(normalized)}
    )
    if document is None:
        # 마이그레이션 전 옛 평문 문서 호환(auth/repository.find_by_email과 같은 순서).
        document = await database["users"].find_one({"email": normalized})
    if document is None:
        raise SystemExit(f"그 이메일의 계정을 찾지 못했습니다: {email}")
    return document["userId"]


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="기본 브랜드 자료(AIONA)를 지금 정의로 다시 씌운다."
    )
    parser.add_argument("--email", required=True, help="이 자료를 소유한 Blog-it 계정")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="실제로 덮어쓴다. 없으면 무엇이 들어갈지 보여 주기만 한다.",
    )
    args = parser.parse_args()

    body = default_brand_body()
    client = AsyncIOMotorClient(URI)
    try:
        database = client.get_default_database()
        user_id = await resolve_user_id(database, args.email)
        service = BrandService(MongoBrandRepository(database))

        print(f"계정: {args.email} ({user_id})")
        print(f"브랜드: {DEFAULT_BRAND_NAME} ({DEFAULT_BRAND_ID})")
        print(f"기준표: {len(body['useCases'])}줄")
        for case in body["useCases"]:
            print(f"  - {case['situation']} → {case['feature']}")
        print(f"마무리: {body['closing']['note']} / {body['closing']['url']}")

        existing = await service.list_brands(user_id)
        current = next((b for b in existing if b.brand_id == DEFAULT_BRAND_ID), None)
        if not args.apply:
            print("\n--apply 를 붙이면 위 내용으로 덮어씁니다.")
            if current is not None:
                print(
                    f"  주의: 화면에서 고쳐 둔 소개·기능·기준표·마무리는 사라집니다"
                    f"(이미지 {len(current.images)}장·문서 {len(current.documents)}개는 그대로)."
                )
            return

        # 이미지·문서는 넘기지 않으면 사라진다 — 자료 검증이 "보내지 않은 것은 없는
        # 것"으로 읽는다. 올려 둔 것을 그대로 실어 보낸다.
        #
        # 다만 **이미지는 합친다**(2026-08-20). 예전에는 올려 둔 것으로 통째로 덮었는데,
        # 정의에 마스코트가 생긴 뒤로는 그것이 곧 "마스코트를 빼고 덮어쓰기"가 됐다 —
        # 마스코트를 넣으려고 --apply를 돌린 사람에게 마스코트가 오지 않았다.
        keep = {}
        if current is not None:
            builtin_images = {image["label"] for image in body.get("images", [])}
            # 문서도 이미지와 같이 합친다(2026-08-20). 정의가 자료 한 벌을 문서로 싣게
            # 되면서, 올려 둔 것으로 통째로 덮으면 그 문서가 오지 않는다.
            builtin_docs = {doc["name"] for doc in body.get("documents", [])}
            keep = {
                "images": [
                    *body.get("images", []),
                    *(
                        image.model_dump(by_alias=True)
                        for image in current.images
                        if image.label not in builtin_images
                    ),
                ],
                "documents": [
                    *body.get("documents", []),
                    *(
                        doc.model_dump(by_alias=True)
                        for doc in current.documents
                        if doc.name not in builtin_docs
                    ),
                ],
            }
        saved = await service.update_brand(user_id, DEFAULT_BRAND_ID, {**body, **keep})
        # 판번호도 최신으로 못 박는다. 그러지 않으면 다음 조회에서 빈 칸 채우기가 또 돈다.
        saved = await service._repository.upsert(
            saved.model_copy(update={"defaults_revision": DEFAULTS_REVISION})
        )
        print(f"\n덮어썼습니다: {saved.brand_id}")
        print(f"이미지 {len(saved.images)}장 · 문서 {len(saved.documents)}개는 그대로 남았습니다.")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
