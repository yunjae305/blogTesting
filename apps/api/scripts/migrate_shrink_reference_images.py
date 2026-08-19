"""이미 저장된 참고 이미지를 900px JPEG으로 줄인다.

    python apps/api/scripts/migrate_shrink_reference_images.py            # 무엇이 바뀔지만 본다
    python apps/api/scripts/migrate_shrink_reference_images.py --apply    # 실제로 줄인다

왜
--
원고 이미지를 옆 컬렉션으로 빼고 줄인 뒤에도 **글 2건이 계속 안 열렸다**(2026-08-06).
원인은 원고 이미지가 아니라 **참고자료**였다.

    post_20260806_072122_561_588d90534554   문서 4.40MB
    post_20260806_045221_355_a6b79017e34a   문서 2.20MB

둘 다 참고자료가 2.11MB다. 뜯어보니 TEXT 3개는 합쳐 11KB, URL 1개는 0KB, **IMAGE 9장이
2.1MB**였다. 회선이 0.09MB/s라 그 글을 여는 데만 23초 — 20초 소켓 제한을 넘는다.

새로 올리는 이미지는 저장할 때 줄인다(`blog_task/validation.py`의 `_shrunk_image`).
이 스크립트는 **그 전에 저장된 것**을 같은 크기로 맞춘다.

왜 서버 안에서 못 하나
----------------------
줄이려면 이미지를 **받아야** 한다. Mongo 안에서 할 수 있는 일이 아니다. 그래서 글마다
참고자료를 받아 줄이고 되쓴다 — 느리다. 다행히 대상은 몇 건뿐이다.

안전한가
--------
- **한 글씩, 다 줄인 뒤에 한 번에 되쓴다.** 중간에 끊기면 그 글은 손대지 않은 상태로
  남는다(반쯤 줄어든 목록이 저장되지 않는다).
- **줄지 않는 것은 그대로 둔다.** 못 여는 형식, 이미 작은 것, 줄였더니 더 커진 것.
- IMAGE만 만진다. PDF는 서버가 텍스트를 뽑아 쓰므로 이미지로 다시 구우면 그 글자가
  사라진다. TEXT·URL은 애초에 작다.
"""

import asyncio
import sys
import time
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorDatabase

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from _env import mongodb_uri  # noqa: E402

from app.db.mongo import connect_mongo  # noqa: E402
from app.shared.image_bytes import shrink, to_data_url  # noqa: E402

#: 참고 이미지를 들고 있는 글.
HAS_REFERENCE_IMAGES = {"input.referenceMaterials": {"$elemMatch": {"type": "IMAGE"}}}

#: 이 크기를 넘는 글만 손댄다. 이미 가벼운 글을 받아 되쓰는 것은 회선만 쓰는 일이다.
WORTH_SHRINKING_BYTES = 300_000


async def _heavy_posts(db: AsyncIOMotorDatabase) -> list[tuple[str, int]]:
    """참고자료가 무거운 글의 (postId, 참고자료 크기). **크기 계산은 서버가 한다.**"""
    rows = await db["blogTask"].aggregate(
        [
            {"$match": HAS_REFERENCE_IMAGES},
            {
                "$project": {
                    "_id": 0,
                    "postId": 1,
                    "크기": {"$bsonSize": {"w": "$input.referenceMaterials"}},
                }
            },
            {"$match": {"크기": {"$gt": WORTH_SHRINKING_BYTES}}},
            {"$sort": {"크기": -1}},
        ]
    ).to_list(None)
    return [(row["postId"], row["크기"]) for row in rows]


async def run(db: AsyncIOMotorDatabase, apply: bool) -> int:
    tasks = db["blogTask"]

    heavy = await _heavy_posts(db)
    total = sum(size for _, size in heavy)
    print(f"참고자료가 무거운 글: {len(heavy)}건 (합계 {total / 1e6:.2f}MB)")
    for post_id, size in heavy[:10]:
        print(f"   {post_id}  {size / 1e6:.2f}MB")
    if not heavy:
        print("줄일 것이 없습니다.")
        return 0
    if not apply:
        print(f"\n실제로 줄이려면 --apply 를 붙여 다시 실행하세요.")
        print(f"이미지를 받아서 줄여야 해 회선 0.09MB/s 기준 약 {total / 1e6 / 0.09 / 60:.0f}분 걸립니다.")
        return 0

    started = time.perf_counter()
    changed = failed = 0
    before_bytes = after_bytes = 0
    for number, (post_id, _) in enumerate(heavy, 1):
        try:
            document = await tasks.find_one(
                {"postId": post_id}, {"_id": 0, "input.referenceMaterials": 1}
            )
            materials = ((document or {}).get("input") or {}).get("referenceMaterials") or []

            shrunk = []
            touched = False
            for material in materials:
                value = material.get("value") or ""
                if material.get("type") != "IMAGE" or not value:
                    shrunk.append(material)
                    continue
                raw, mime = shrink(value)
                smaller = to_data_url(raw, mime) if raw else value
                if len(smaller) < len(value):
                    before_bytes += len(value)
                    after_bytes += len(smaller)
                    touched = True
                    shrunk.append({**material, "value": smaller})
                else:
                    shrunk.append(material)

            if not touched:
                continue
            # 다 줄인 뒤에 한 번에 되쓴다. 중간에 끊기면 이 글은 손대지 않은 채로 남는다.
            await tasks.update_one(
                {"postId": post_id}, {"$set": {"input.referenceMaterials": shrunk}}
            )
            changed += 1
            print(f"  {number}/{len(heavy)}건  {post_id}  {time.perf_counter() - started:.0f}초 지남",
                  flush=True)
        except Exception as error:  # noqa: BLE001 - 한 건이 실패해도 나머지는 줄인다
            failed += 1
            print(f"  !! {post_id}: {type(error).__name__}: {str(error)[:110]}", flush=True)

    print(f"\n  글 {changed}건의 참고 이미지를 줄였습니다" + (f" (실패 {failed}건)" if failed else "") + ".")
    if before_bytes:
        print(f"  참고 이미지: {before_bytes / 1e6:.2f}MB → {after_bytes / 1e6:.2f}MB")

    after = await db.command("collstats", "blogTask")
    print(f"  blogTask: {after['size'] / 1e6:.0f}MB / 한 건 평균 {after.get('avgObjSize', 0) / 1e3:.0f}KB")
    return 1 if failed else 0


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
