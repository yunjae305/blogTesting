"""이미 저장된 글에서 중복된 원고 한 벌을 지운다.

    python apps/api/scripts/migrate_dedupe_final_post.py            # 무엇이 바뀔지만 본다
    python apps/api/scripts/migrate_dedupe_final_post.py --apply    # 실제로 고친다

무엇을 지우나
-------------
``draftGenerationResult.finalPost``는 문서 맨 위의 ``finalPost``와 **항상 같은 값**이다
(둘 다 같은 객체를 직렬화한 것이다). 그 안에 base64 이미지가 들어 있어 두 벌을 쓰면
문서가 정확히 두 배가 된다.

실측(2026-08-06): `blogTask` 371MB / 119건, 한 건 평균 3.12MB. 글 하나를 뜯어보니
1.11MB 중 0.54MB씩 똑같은 것이 두 벌이었다. 글 목록 하나를 그리는 데 30KB를 얻으려고
206MB를 읽고 있었다(450ms).

읽는 쪽은 이미 대비돼 있다 — `repository._with_restored_final_post()`가 맨 위
``finalPost``로 되돌린다. 그래서 이 스크립트를 돌리지 않아도 앱은 동작하고, 돌리면
기존 문서가 가벼워진다.

왜 서버에서 다 하나
-------------------
문서를 파이썬으로 끌어와 비교하면 371MB가 네트워크를 타고 넘어와 **20초 제한에 걸린다**
(실제로 걸렸다). 그래서 비교(`$expr`)도 삭제(`$unset`)도 Mongo 안에서 끝낸다 — 오가는
것은 개수뿐이다.

안전한가
--------
**같은 값일 때만 지운다.** 두 벌이 다른 문서는 건드리지 않고 postId를 알려 준다. 다를
리는 없지만, 다르다면 어느 쪽이 맞는지는 이 스크립트가 정할 일이 아니다.
"""

import asyncio
import sys
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorDatabase

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from _env import mongodb_uri  # noqa: E402

from app.db.mongo import connect_mongo  # noqa: E402

#: 중복이 남아 있는 문서.
HAS_DUPLICATE = {"draftGenerationResult.finalPost": {"$exists": True}}
#: 그중 두 벌이 **서로 다른** 문서. 이런 것은 건드리지 않는다.
DIFFERS = {
    **HAS_DUPLICATE,
    "$expr": {"$ne": ["$draftGenerationResult.finalPost", "$finalPost"]},
}


async def collection_size(db: AsyncIOMotorDatabase) -> tuple[float, int, float]:
    stats = await db.command("collstats", "blogTask")
    return stats["size"] / 1e6, stats["count"], stats.get("avgObjSize", 0) / 1e6


async def run(db: AsyncIOMotorDatabase, apply: bool) -> int:
    collection = db["blogTask"]

    size_mb, count, avg_mb = await collection_size(db)
    print(f"지금 blogTask: {size_mb:.0f}MB / {count}건 (한 건 평균 {avg_mb:.2f}MB)\n")

    duplicated = await collection.count_documents(HAS_DUPLICATE)
    print(f"중복이 남아 있는 글: {duplicated}건")
    if duplicated == 0:
        print("정리할 것이 없습니다.")
        return 0

    # 값이 다른 문서는 따로 센다. 비교는 서버가 한다(문서를 가져오지 않는다).
    differing = [
        doc["postId"]
        async for doc in collection.find(DIFFERS, {"postId": 1, "_id": 0})
    ]
    safe = duplicated - len(differing)
    print(f"  같은 값이라 지워도 되는 글: {safe}건")
    if differing:
        print(f"  !! 두 벌이 서로 달라 건드리지 않는 글: {len(differing)}건 !!")
        for post_id in differing[:10]:
            print(f"     {post_id}")
        print("     사람이 확인해야 합니다 — 어느 쪽이 맞는지는 여기서 정할 수 없습니다.")

    if not apply:
        print("\n실제로 고치려면 --apply 를 붙여 다시 실행하세요.")
        return 0

    if safe == 0:
        print("\n지울 수 있는 글이 없습니다.")
        return 0

    # 같은 값인 것만. $unset도 서버에서 끝난다.
    result = await collection.update_many(
        {**HAS_DUPLICATE, "$expr": {"$eq": ["$draftGenerationResult.finalPost", "$finalPost"]}},
        {"$unset": {"draftGenerationResult.finalPost": ""}},
    )
    print(f"\n  {result.modified_count}건에서 중복 원고를 지웠습니다.")

    after_mb, after_count, after_avg = await collection_size(db)
    print(f"\n  {size_mb:.0f}MB → {after_mb:.0f}MB   (한 건 평균 {avg_mb:.2f}MB → {after_avg:.2f}MB)")
    # 디스크 크기는 compact 전까지 바로 줄지 않는다. 줄어든 것은 문서 자체다.
    print("  (디스크 사용량은 Atlas가 정리한 뒤 반영됩니다. 읽는 비용은 즉시 줄어듭니다.)")
    return 0


async def main() -> int:
    apply = "--apply" in sys.argv
    client, db = await connect_mongo(mongodb_uri())
    print(f"database: {db.name}   ({'실제 수정' if apply else '미리보기'})\n")
    try:
        return await run(db, apply)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
