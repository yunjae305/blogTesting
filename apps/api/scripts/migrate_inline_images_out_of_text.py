"""본문 글(``htmlContent``·``markdownContent``) 안의 인라인 base64를 자리표로 바꾼다.

    python apps/api/scripts/migrate_inline_images_out_of_text.py            # 무엇이 바뀔지만 본다
    python apps/api/scripts/migrate_inline_images_out_of_text.py --apply    # 실제로 고친다

왜 필요한가
-----------
`migrate_extract_post_images.py`로 ``images[]``의 바이트를 옮긴 뒤에도 **글 열기가 계속
실패했다**(2026-08-06 실측: 8건 중 5건 NetworkTimeout). 글 문서가 아직 1.4~3.7MB였다.

뜯어보니 같은 이미지가 **세 벌**로 들어 있었다.

    finalPost.images[]          옮겼음
    finalPost.htmlContent       1.1~1.4MB  (인라인 base64)
    finalPost.markdownContent   1.1~1.4MB  (같은 것)

51건이 그랬다. 이 두 벌이 남아 있는 한 문서는 줄지 않는다.

무엇으로 바꾸나
---------------
바이트가 있던 자리에 ``stored:post_images#3`` 같은 자리표를 남긴다. 읽을 때 저장소가
되돌려 **글자 하나까지 같은 값**을 만든다(`repository._text_with_images`). 발행 경로에
넘어가는 값은 달라지지 않는다.

왜 서버 안에서 하나
-------------------
실측한 회선이 0.09MB/s다. 글 하나가 3MB면 읽는 데만 30초가 넘어 20초 소켓 제한에
걸린다(그래서 처음 이관이 끝나지 않았다). `$lookup`으로 이미 옮겨 둔 바이트를 서버
안에서 찾아 `$replaceAll`로 바꾸고 `$merge`로 되쓴다 — 바이트는 네트워크를 타지 않는다.

안전한가
--------
- **여러 번 돌려도 같다.** 두 번째에는 바꿀 base64가 없어 아무것도 안 바뀐다.
- **바이트는 이미 `post_images`에 있다.** 이 스크립트는 지우지 않고 가리키게만 한다.
- **못 알아본 이미지는 건드리지 않는다.** `post_images`에 없는 인라인 base64는 그대로
  둔다(그 글은 지금처럼 그대로 열린다). 남은 건수를 끝에 알려 준다.
"""

import asyncio
import sys
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorDatabase

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from _env import mongodb_uri  # noqa: E402

from app.db.mongo import connect_mongo  # noqa: E402
from app.modules.blog_task.repository import (  # noqa: E402
    IMAGE_BEARING_TEXT,
    IMAGE_ELSEWHERE,
    POST_IMAGES,
)

#: 본문 글 안에 아직 바이트가 들어 있는 글.
HAS_INLINE_BYTES = {"$or": [{f"finalPost.{field}": {"$regex": "data:image"}} for field in IMAGE_BEARING_TEXT]}


def _replaced(field: str) -> dict:
    """이 글에 딸린 이미지들을 하나씩 자리표로 바꾼다. 서버 안에서 돈다.

    필드가 없으면 만들지 않는다(``$$REMOVE``는 없던 것을 없는 채로 둔다).
    """
    path = f"$finalPost.{field}"
    return {
        "$cond": [
            {"$eq": [{"$type": path}, "string"]},
            {
                "$reduce": {
                    "input": "$이미지",
                    "initialValue": path,
                    "in": {
                        "$replaceAll": {
                            "input": "$$value",
                            "find": "$$this.dataUrl",
                            "replacement": {
                                "$concat": [IMAGE_ELSEWHERE, "#", {"$toString": "$$this.index"}]
                            },
                        }
                    },
                }
            },
            "$$REMOVE",
        ]
    }


async def run(db: AsyncIOMotorDatabase, apply: bool) -> int:
    tasks = db["blogTask"]

    stats = await db.command("collstats", "blogTask")
    print(f"지금 blogTask: {stats['size'] / 1e6:.0f}MB / {stats['count']}건 "
          f"(한 건 평균 {stats.get('avgObjSize', 0) / 1e6:.2f}MB)\n")

    before = await tasks.count_documents(HAS_INLINE_BYTES)
    print(f"본문 글에 바이트가 남은 글: {before}건")
    if before == 0:
        print("고칠 것이 없습니다.")
        return 0
    if not apply:
        print("\n실제로 고치려면 --apply 를 붙여 다시 실행하세요.")
        return 0

    print("\n자리표로 바꿉니다(서버 안에서 — 바이트는 네트워크를 타지 않습니다) …")
    await tasks.aggregate(
        [
            {"$match": HAS_INLINE_BYTES},
            {
                "$lookup": {
                    "from": POST_IMAGES,
                    "localField": "postId",
                    "foreignField": "postId",
                    "as": "이미지",
                }
            },
            # 옮겨 둔 이미지가 없으면 바꿀 것도 없다. 건드리지 않는다.
            {"$match": {"이미지": {"$ne": []}}},
            {"$set": {f"finalPost.{field}": _replaced(field) for field in IMAGE_BEARING_TEXT}},
            # finalPost 전체를 담아 보낸다. 일부만 담으면 $merge가 나머지를 지운다.
            {"$project": {"_id": 0, "postId": 1, "finalPost": 1}},
            {
                "$merge": {
                    "into": "blogTask",
                    "on": "postId",
                    "whenMatched": "merge",
                    "whenNotMatched": "discard",
                }
            },
        ]
    ).to_list(None)

    left = await tasks.count_documents(HAS_INLINE_BYTES)
    print(f"  {before - left}건을 고쳤습니다.")
    if left:
        print(f"\n  !! 아직 바이트가 남은 글 {left}건 !!")
        remaining = [
            d["postId"]
            async for d in tasks.find(HAS_INLINE_BYTES, {"postId": 1, "_id": 0}).limit(10)
        ]
        for post_id in remaining:
            print(f"     {post_id}")
        print(f"     {POST_IMAGES}에 없는 인라인 이미지입니다 — 건드리지 않았습니다.")
        print("     이 글들은 지금 그대로 열리고 발행됩니다(다만 여전히 무겁습니다).")

    after = await db.command("collstats", "blogTask")
    print(f"\n  blogTask : {stats['size'] / 1e6:.0f}MB → {after['size'] / 1e6:.0f}MB "
          f"(한 건 평균 {stats.get('avgObjSize', 0) / 1e6:.2f}MB → "
          f"{after.get('avgObjSize', 0) / 1e6:.2f}MB)")
    return 1 if left else 0


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
