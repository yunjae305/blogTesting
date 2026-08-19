"""이미지 합성 붙여넣기 실험: SmartEditor가 **File 합성 paste**를 받는지만 좁혀 본다.

    python apps/api/scripts/probe_synthetic_image_paste.py

probe_synthetic_paste.py(전체 경로)와 달리 **문단 1개 + 이미지 1장**짜리 최소 원고를
태운다 — 제목·본문이 통과하고 이미지에서 갈리는지가 한눈에 보인다. 발행하지 않는다.

읽는 법(콘솔의 [NAVER_PUBLISH] 로그):

- ``image_paste=ok``                    → 합성 이미지 붙여넣기를 에디터가 받는다.
- ``image_paste=failed fallback=upload`` 뒤 ``image_upload=ok``
                                        → 합성은 거부하지만 업로드 폴백이 통한다.
- ``image_upload=unavailable`` / ``image_upload=failed``
                                        → 둘 다 안 된다. 운영 .env에
                                          ``NAVER_PASTE_MODE=auto``(권장) 또는
                                          ``clipboard``를 넣는다.

먼저 세션을 저장해 둔다: ``python apps/api/scripts/naver_login.py``.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["NAVER_PASTE_MODE"] = "synthetic"

from scripts.naver_paste_test import _data_url, _sample_image, _solid_png  # noqa: E402
from scripts._env import load_env_file  # noqa: E402

load_env_file()

from app.posting.config import naver_config_from_env  # noqa: E402
from app.posting.naver import build_naver_publish_plan, fill_editor_for_preview  # noqa: E402
from app.shared import FinalPost  # noqa: E402

TITLE = "이미지 합성 붙여넣기 실험 — 이 글은 발행되지 않습니다"


def _minimal_post() -> FinalPost:
    image_url = _data_url(_solid_png(640, 360, (46, 125, 214)))
    html = (
        "<article>"
        "<p>이미지 합성 붙여넣기 실험용 문단입니다. 아래에 파란 이미지 한 장이 보여야 합니다.</p>"
        f'<figure><img src="{image_url}" alt="실험 이미지" /></figure>'
        "</article>"
    )
    image = _sample_image(image_url, "실험 이미지")
    return FinalPost(
        title=TITLE,
        body="이미지 합성 붙여넣기 실험",
        hashtags=[],
        images=[image],
        featured_image=image,
        html_content=html,
        markdown_content=None,
    )


async def main() -> int:
    config = naver_config_from_env()
    if config is None or not config.has_session:
        print(
            "저장된 네이버 세션이 없습니다. 먼저 로그인해 주세요:\n"
            "    python apps/api/scripts/naver_login.py",
            file=sys.stderr,
        )
        return 1

    plan = build_naver_publish_plan(_minimal_post(), "probe-image-paste")
    print("=== 이미지 합성 붙여넣기 실험 (문단 1 + 이미지 1, 발행 안 함) ===\n")
    try:
        await fill_editor_for_preview(config, plan)
    except Exception as error:
        print(f"\n[판정] 실패: {error}", file=sys.stderr)
        print(
            "위 [NAVER_PUBLISH] 로그에서 image_paste / image_upload 줄을 확인하세요.",
            file=sys.stderr,
        )
        return 1
    print("\n[판정] 통과 — 이미지가 합성 붙여넣기(또는 업로드 폴백)로 들어갔습니다.")
    print("어느 쪽이었는지는 위 [NAVER_PUBLISH] image_* 로그가 말해 줍니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
