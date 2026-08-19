"""이미 저장된 이미지를 이진 + 900px JPEG으로 다시 담는다.

    python apps/api/scripts/migrate_shrink_post_images.py            # 무엇이 바뀔지만 본다
    python apps/api/scripts/migrate_shrink_post_images.py --apply    # 실제로 바꾼다

왜
--
글 하나를 여는 데 오가는 것의 **85%가 이미지**였다(2026-08-06 실측: 이미지 970KB,
참고자료 144KB, 글자 104KB). 회선이 0.09MB/s라 그대로 대기 시간이다.

    지금            1200px JPEG, base64  204KB/장  →  970KB/글  11초
    바꾼 뒤          900px JPEG, 이진      78KB/장  →  390KB/글   4.3초

두 가지를 한다. base64를 이진으로(원본보다 33% 크다 — **화질 손실 0**), 그리고 1200px를
900px로(블로그 본문 폭이 보통 800~900px이다).

왜 오래 걸리나
--------------
줄이는 것은 **이미지를 받아서** 해야 한다 — 서버 안에서 할 수 없는 유일한 작업이다.
72MB를 받고 25MB를 되쓰는 데 회선 0.09MB/s로 20분 남짓 걸린다. 백그라운드로 돌리면 된다.

안전한가
--------
- 한 장씩 처리한다. 중간에 끊겨도 다시 돌리면 **남은 것부터** 이어서 한다(이미 이진으로
  바뀐 행은 걸리지 않는다).
- 한 장을 받아 줄이고 **그 자리를 바로 덮어쓴다.** 원본이 사라진 뒤에 새것을 넣는 순간이
  없다.
- 줄이지 못하는 이미지(못 여는 형식 등)는 이진으로만 바꾸고 화질은 그대로 둔다.
"""

import asyncio
import sys
import time
from pathlib import Path

from bson import Binary
from motor.motor_asyncio import AsyncIOMotorDatabase

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from _env import mongodb_uri  # noqa: E402

from app.db.mongo import connect_mongo  # noqa: E402
from app.modules.blog_task.repository import POST_IMAGES  # noqa: E402
from app.shared.image_bytes import shrink  # noqa: E402

#: 아직 base64 글자로 들어 있는 행. 이미 바꾼 행에는 `dataUrl`이 없다.
NOT_SHRUNK_YET = {"dataUrl": {"$exists": True}}


async def run(db: AsyncIOMotorDatabase, apply: bool) -> int:
    images = db[POST_IMAGES]

    stats = await db.command("collstats", POST_IMAGES)
    print(f"지금 {POST_IMAGES}: {stats['size'] / 1e6:.0f}MB / {stats['count']}장 "
          f"(한 장 평균 {stats.get('avgObjSize', 0) / 1e3:.0f}KB)\n")

    left = await images.count_documents(NOT_SHRUNK_YET)
    print(f"줄일 이미지: {left}장")
    if left == 0:
        print("줄일 것이 없습니다.")
        return 0
    if not apply:
        print("\n실제로 바꾸려면 --apply 를 붙여 다시 실행하세요.")
        print("회선이 0.09MB/s라 20분 남짓 걸립니다 — 백그라운드로 돌리세요.")
        return 0

    # 무엇을 처리할지 먼저 정해 둔다(바이트는 안 가져온다). 처리하면서 커서를 돌리면
    # 갱신된 행이 커서에 다시 걸릴 수 있다.
    todo = [
        (row["postId"], row["index"])
        async for row in images.find(NOT_SHRUNK_YET, {"_id": 0, "postId": 1, "index": 1})
    ]

    started = time.perf_counter()
    done = failed = 0
    before_bytes = after_bytes = 0
    for number, (post_id, index) in enumerate(todo, 1):
        try:
            row = await images.find_one({"postId": post_id, "index": index}, {"_id": 0})
            data_url = (row or {}).get("dataUrl")
            if not data_url:
                continue  # 다른 실행이 이미 처리했다

            raw, mime = shrink(data_url)
            if not raw:
                # 열지 못하는 값. base64 글자를 그대로 두는 편이 안전하다.
                failed += 1
                continue

            before_bytes += len(data_url)
            after_bytes += len(raw)
            await images.update_one(
                {"postId": post_id, "index": index},
                {"$set": {"bytes": Binary(raw), "mimeType": mime}, "$unset": {"dataUrl": ""}},
            )
            done += 1
            if number % 10 == 0 or number == len(todo):
                elapsed = time.perf_counter() - started
                rate = number / elapsed
                print(f"  {number}/{len(todo)}장  {elapsed / 60:.1f}분 지남, "
                      f"남은 예상 {(len(todo) - number) / rate / 60:.0f}분", flush=True)
        except Exception as error:  # noqa: BLE001 - 한 장이 실패해도 나머지는 줄인다
            failed += 1
            print(f"  !! {post_id} {index}번: {type(error).__name__}: {str(error)[:100]}", flush=True)

    print(f"\n  {done}장을 줄였습니다" + (f" (실패 {failed}장)" if failed else "") + ".")
    if before_bytes:
        print(f"  오가던 양: {before_bytes / 1e6:.0f}MB → {after_bytes / 1e6:.0f}MB "
              f"(장당 {before_bytes / done / 1e3:.0f}KB → {after_bytes / done / 1e3:.0f}KB)")

    after = await db.command("collstats", POST_IMAGES)
    print(f"  {POST_IMAGES}: {stats['size'] / 1e6:.0f}MB → {after['size'] / 1e6:.0f}MB")
    remaining = await images.count_documents(NOT_SHRUNK_YET)
    if remaining:
        print(f"  아직 {remaining}장 남았습니다 — 다시 실행하면 이어서 합니다.")
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
