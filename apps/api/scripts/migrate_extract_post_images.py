"""원고 이미지를 글 문서에서 떼어 `post_images` 컬렉션으로 옮긴다.

    python apps/api/scripts/migrate_extract_post_images.py            # 무엇이 바뀔지만 본다
    python apps/api/scripts/migrate_extract_post_images.py --apply    # 실제로 옮긴다

왜 옮기나
---------
base64 이미지가 글 문서 안에 있어 한 건이 3~5MB까지 커졌고, **그 글을 여는 것이 20초
타임아웃으로 실패했다**(2026-08-06 실측).

    가벼운 필드만 읽기                    0.01 ~ 0.26초
    이미지까지 통째로 읽기(2.8~4.7MB)     27 ~ 47초   ← 20초 제한을 넘긴다

목록은 프로젝션으로 피할 수 있지만 상세·발행은 이미지가 필요해 피할 수 없다. 그래서
바이트를 옆 컬렉션으로 옮기고, 글 문서에는 번호와 설명만 남긴다. 발행할 때는 저장소가
다시 붙여 **지금과 똑같은 data URL**을 만든다 — 네이버·스레드에 넘기는 값은 그대로다.

왜 서버 안에서 다 하나
----------------------
처음에는 한 건씩 파이썬으로 읽어 옮겼다. **그 방식으로는 끝나지 않는다.** 실측한
회선이 0.09MB/s여서(같은 PC에서 일반 인터넷 내려받기도 0.05MB/s였다 — 회선 문제지
Atlas 문제가 아니다) 3MB짜리 글 하나를 읽는 데만 30~47초가 걸리고, 20초 소켓 제한에
걸려 대부분 실패했다. 52건이면 읽기만 30분, 되쓰기까지 하면 한 시간이 넘는다.

그래서 바이트를 **아예 네트워크에 태우지 않는다.** `$merge`로 이미지를 서버 안에서
복사하고, `updateMany`의 파이프라인으로 글 문서를 서버 안에서 고친다. 오가는 것은
개수와 postId뿐이다.

안전한가
--------
세 단계로 나눠, **되돌릴 수 없는 단계를 마지막에 둔다.**

1. `$merge` — 이미지를 복사한다. 원본은 그대로다.
2. 대조 — 글마다 '있어야 할 장수'와 '실제로 복사된 장수'를 **서버에서** 세어 비교한다.
3. 표시로 바꾸기 — **둘이 맞는 글만** 고친다.

중간에 끊겨도 1·2단계는 몇 번을 다시 해도 같은 결과이고, 3단계를 못 한 글은 다음에
다시 돌리면 이어서 한다. 장수가 맞지 않는 글은 건드리지 않고 postId를 알려 준다.
"""

import asyncio
import sys
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorDatabase

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from _env import mongodb_uri  # noqa: E402

from app.db.mongo import connect_mongo  # noqa: E402
from app.modules.blog_task.repository import IMAGE_ELSEWHERE, POST_IMAGES  # noqa: E402

#: 옮길 필요가 없는 dataUrl. 없거나, 비었거나, 이미 옮긴 것.
ALREADY_OUT = [None, "", IMAGE_ELSEWHERE]

#: 아직 이미지를 안에 들고 있는 글. 이미 옮긴 글에는 표시가 들어가 있어 걸리지 않는다.
NOT_MOVED_YET = {
    "$or": [
        {"finalPost.images": {"$elemMatch": {"dataUrl": {"$nin": ALREADY_OUT}}}},
        {"finalPost.featuredImage.dataUrl": {"$nin": ALREADY_OUT}},
    ]
}

#: 이 글에서 옮겨야 할 이미지가 몇 장인지. 본문 이미지 + 대표 이미지(있으면).
EXPECTED_COUNT = {
    "$add": [
        {
            "$size": {
                "$filter": {
                    "input": {"$ifNull": ["$finalPost.images", []]},
                    "as": "img",
                    "cond": {"$not": [{"$in": [{"$ifNull": ["$$img.dataUrl", ""]}, ALREADY_OUT]}]},
                }
            }
        },
        {
            "$cond": [
                {"$in": [{"$ifNull": ["$finalPost.featuredImage.dataUrl", ""]}, ALREADY_OUT]},
                0,
                1,
            ]
        },
    ]
}


def _merge_into_images() -> dict:
    return {
        "$merge": {
            "into": POST_IMAGES,
            "on": ["postId", "index"],
            # 같은 자리에 이미 있으면 덮어쓴다. 여러 번 돌려도 결과가 같다.
            "whenMatched": "replace",
            "whenNotMatched": "insert",
        }
    }


async def copy_body_images(db: AsyncIOMotorDatabase) -> None:
    """본문 이미지를 서버 안에서 복사한다. 바이트는 네트워크를 타지 않는다."""
    await db["blogTask"].aggregate(
        [
            {"$match": NOT_MOVED_YET},
            {"$project": {"postId": 1, "images": {"$ifNull": ["$finalPost.images", []]}}},
            {"$unwind": {"path": "$images", "includeArrayIndex": "position"}},
            {"$match": {"images.dataUrl": {"$nin": ALREADY_OUT}}},
            {
                "$project": {
                    "_id": 0,
                    "postId": 1,
                    # includeArrayIndex는 64비트를 준다. 검증기가 int(32비트)를 요구하므로 맞춘다.
                    "index": {"$toInt": "$position"},
                    "dataUrl": "$images.dataUrl",
                }
            },
            _merge_into_images(),
        ]
    ).to_list(None)


async def copy_featured_images(db: AsyncIOMotorDatabase) -> None:
    """대표 이미지는 -1번으로 둔다. 본문 이미지 번호와 겹치지 않는다."""
    await db["blogTask"].aggregate(
        [
            {"$match": {"finalPost.featuredImage.dataUrl": {"$nin": ALREADY_OUT}}},
            {
                "$project": {
                    "_id": 0,
                    "postId": 1,
                    "index": {"$literal": -1},
                    "dataUrl": "$finalPost.featuredImage.dataUrl",
                }
            },
            _merge_into_images(),
        ]
    ).to_list(None)


async def check_counts(db: AsyncIOMotorDatabase) -> tuple[list[str], list[tuple[str, int, int]]]:
    """글마다 '있어야 할 장수'와 '실제로 복사된 장수'를 **서버에서** 센다.

    오가는 것은 postId와 숫자 둘뿐이다. 여기서 맞는 글만 다음 단계로 보낸다.
    """
    rows = await db["blogTask"].aggregate(
        [
            {"$match": NOT_MOVED_YET},
            {"$project": {"_id": 0, "postId": 1, "expected": EXPECTED_COUNT}},
            {
                "$lookup": {
                    "from": POST_IMAGES,
                    "localField": "postId",
                    "foreignField": "postId",
                    # 바이트는 가져오지 않는다. 세기만 한다.
                    "pipeline": [{"$count": "n"}],
                    "as": "stored",
                }
            },
            {
                "$project": {
                    "postId": 1,
                    "expected": 1,
                    "actual": {"$ifNull": [{"$first": "$stored.n"}, 0]},
                }
            },
        ]
    ).to_list(None)

    ready = [r["postId"] for r in rows if r["actual"] >= r["expected"] and r["expected"] > 0]
    mismatched = [
        (r["postId"], r["expected"], r["actual"])
        for r in rows
        if r["actual"] < r["expected"] or r["expected"] == 0
    ]
    return ready, mismatched


#: 글 문서의 dataUrl을 표시로 바꾸는 파이프라인. 서버 안에서 돈다.
#: 비어 있는 것은 건드리지 않는다 — 표시를 넣으면 없는 이미지를 가리키게 되고, 읽을 때
#: "이미지를 찾지 못했습니다"로 멈춘다.
TO_MARKER = [
    {
        "$set": {
            "finalPost.images": {
                "$map": {
                    "input": {"$ifNull": ["$finalPost.images", []]},
                    "as": "img",
                    "in": {
                        "$cond": [
                            {"$in": [{"$ifNull": ["$$img.dataUrl", ""]}, ALREADY_OUT]},
                            "$$img",
                            {"$mergeObjects": ["$$img", {"dataUrl": IMAGE_ELSEWHERE}]},
                        ]
                    },
                }
            }
        }
    },
    {
        "$set": {
            "finalPost.featuredImage": {
                "$cond": [
                    {"$in": [{"$ifNull": ["$finalPost.featuredImage.dataUrl", ""]}, ALREADY_OUT]},
                    # 없거나 이미 옮겼으면 그대로 둔다($$REMOVE는 없던 것을 없는 채로 둔다).
                    {"$ifNull": ["$finalPost.featuredImage", "$$REMOVE"]},
                    {"$mergeObjects": ["$finalPost.featuredImage", {"dataUrl": IMAGE_ELSEWHERE}]},
                ]
            }
        }
    },
]


async def run(db: AsyncIOMotorDatabase, apply: bool) -> int:
    tasks = db["blogTask"]

    stats = await db.command("collstats", "blogTask")
    print(f"지금 blogTask: {stats['size'] / 1e6:.0f}MB / {stats['count']}건 "
          f"(한 건 평균 {stats.get('avgObjSize', 0) / 1e6:.2f}MB)\n")

    remaining = await tasks.count_documents(NOT_MOVED_YET)
    print(f"옮길 글: {remaining}건")
    if remaining == 0:
        print("옮길 것이 없습니다.")
        return 0
    if not apply:
        print("\n실제로 옮기려면 --apply 를 붙여 다시 실행하세요.")
        return 0

    print("\n1) 이미지를 복사합니다(서버 안에서 — 바이트는 네트워크를 타지 않습니다) …")
    await copy_body_images(db)
    await copy_featured_images(db)

    print("2) 장수를 대조합니다 …")
    ready, mismatched = await check_counts(db)
    print(f"   복사가 확인된 글: {len(ready)}건")

    if ready:
        print("3) 글 문서의 이미지 자리를 표시로 바꿉니다 …")
        result = await tasks.update_many({"postId": {"$in": ready}}, TO_MARKER)
        print(f"   {result.modified_count}건을 고쳤습니다.")
    else:
        print("3) 고칠 글이 없습니다.")

    if mismatched:
        print(f"\n  !! 장수가 맞지 않아 건드리지 않은 글 {len(mismatched)}건 !!")
        for post_id, expected, actual in mismatched[:10]:
            print(f"     {post_id}  있어야 할 {expected}장 / 복사된 {actual}장")
        print("     이 글들은 바꾸지 않았습니다 — 지금 그대로 열리고 발행됩니다.")

    after = await db.command("collstats", "blogTask")
    image_stats = await db.command("collstats", POST_IMAGES)
    left = await tasks.count_documents(NOT_MOVED_YET)
    print(f"\n  blogTask : {stats['size'] / 1e6:.0f}MB → {after['size'] / 1e6:.0f}MB "
          f"(한 건 평균 {after.get('avgObjSize', 0) / 1e6:.2f}MB)")
    print(f"  {POST_IMAGES}: {image_stats['size'] / 1e6:.0f}MB / {image_stats['count']}장")
    print(f"  남은 글: {left}건")
    return 1 if mismatched else 0


async def main() -> int:
    apply = "--apply" in sys.argv
    client, db = await connect_mongo(mongodb_uri())
    print(f"database: {db.name}   ({'실제 이동' if apply else '미리보기'})\n")
    try:
        return await run(db, apply)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
