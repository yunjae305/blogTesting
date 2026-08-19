"""저장된 세션으로 네이버 에디터를 열어 실제 발행 경로의 원고 입력만 확인하는 스크립트.

    python apps/api/scripts/naver_paste_test.py

먼저 naver_login.py로 세션을 저장해 둔 뒤 실행한다. 실제 발행과 **같은** 경로를 태운다:
build_naver_publish_plan → navigate → fill_publish_plan(스캐폴드 1회 붙여넣기 + 앵커를
이미지 바이트로 교체) → validate_publish_plan. 발행 버튼만 누르지 않고, 결과를 눈으로
확인할 수 있게 브라우저를 잠시 열어 둔다.

예전 버전은 URL 이미지를 fill()로 한 번에 붙이는 별도 테스트 경로를 썼다 — 그래서
미리보기가 정상이어도 실제 발행(조각 붙여넣기)이 꼬이는 것을 잡지 못했다. 지금은
이미지도 제품과 똑같이 data URL → 바이트 → 클립보드 붙여넣기로 들어간다.

샘플 원고는 실전에서 꼬이던 구조를 일부러 담는다: 제목 h1(제거돼야 함), 글 맨 위 대표
이미지, 소제목 직후 이미지와 캡션, 부분 강조 문단, 목록, 글 끝의 연속 이미지 2장.
"""

import asyncio
import base64
import logging
import struct
import sys
import zlib
from pathlib import Path

# Windows 콘솔(cp949)에서 한글 로그가 UnicodeEncodeError로 죽지 않도록 UTF-8로 맞춘다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # Python 3.7+
    except Exception:
        pass

# 에디터 내부 단계 로그(제목 입력/스캐폴드 붙여넣기/앵커 교체/검증)를 콘솔에 보이게 한다.
# 어디서 멈추는지 이 로그로 진단한다.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._env import load_env_file  # noqa: E402

load_env_file()

from app.posting.config import naver_config_from_env  # noqa: E402
from app.posting.naver import build_naver_publish_plan, fill_editor_for_preview  # noqa: E402
from app.shared import FinalPost, GeneratedPostImage  # noqa: E402

SAMPLE_TITLE = "붙여넣기 테스트 — 이 글은 발행되지 않습니다"


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """단색 PNG 바이트 생성 (Pillow 없이). 눈에 보이는 샘플 이미지용."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    row = bytes(rgb) * width
    raw = bytearray()
    for _ in range(height):
        raw.append(0)  # 필터 타입 0
        raw.extend(row)
    idat = zlib.compress(bytes(raw), 9)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _sample_image(data_url: str, alt: str, caption: str | None = None) -> GeneratedPostImage:
    return GeneratedPostImage(
        data_url=data_url,
        alt_text=alt,
        prompt="paste-test",
        provider="test",
        model="solid-png",
        generated_at="1970-01-01T00:00:00.000Z",
        mime_type="image/png",
        source="generated",
        caption=caption,
    )


def _build_sample_post() -> FinalPost:
    """실전 원고와 같은 골격의 FinalPost. 색이 다른 4장으로 순서·개수를 눈으로 구분한다."""
    lead = _data_url(_solid_png(640, 360, (46, 125, 214)))     # 파랑 — 대표(맨 위)
    section = _data_url(_solid_png(640, 360, (214, 92, 46)))   # 주황 — 소제목 직후
    tail_a = _data_url(_solid_png(640, 360, (52, 168, 83)))    # 초록 — 글 끝 연속 1
    tail_b = _data_url(_solid_png(640, 360, (156, 39, 176)))   # 보라 — 글 끝 연속 2

    html = (
        f"<article><h1>{SAMPLE_TITLE}</h1>"
        f'<figure><img src="{lead}" alt="대표 이미지" /></figure>'
        "<p>도입 일반 문단입니다. 대표 이미지가 이 문단 위의 독립 블록으로 보여야 합니다.</p>"
        "<h2>소제목은 여기까지만 굵어야 합니다</h2>"
        f'<figure><img src="{section}" alt="본문 이미지" /></figure>'
        '<p class="visual-caption"><em>이미지 캡션입니다 — 이미지 바로 아래에 있어야 합니다.</em></p>'
        "<p>이 문단 전체는 일반 굵기이며 <strong>이 부분만 강조</strong>됩니다. "
        "소제목 굵기가 여기로 번지면 실패입니다.</p>"
        "<ul><li>첫 번째 항목</li><li>두 번째 항목 — <strong>여기도 부분 강조</strong></li></ul>"
        f'<figure><img src="{tail_a}" alt="연속 이미지 1" /></figure>'
        f'<figure><img src="{tail_b}" alt="연속 이미지 2" /></figure>'
        "</article>"
    )
    images = [
        _sample_image(lead, "대표 이미지"),
        _sample_image(section, "본문 이미지", "이미지 캡션입니다 — 이미지 바로 아래에 있어야 합니다."),
        _sample_image(tail_a, "연속 이미지 1"),
        _sample_image(tail_b, "연속 이미지 2"),
    ]
    return FinalPost(
        title=SAMPLE_TITLE,
        body="붙여넣기 경로 검증용 샘플 원고",
        hashtags=[],
        images=images,
        featured_image=images[0],
        html_content=html,
        markdown_content=None,
    )


async def main() -> int:
    config = naver_config_from_env()
    if config is None:
        print(
            "공개 블로그 주소 NAVER_BLOG_ID를 지정하거나 설정 화면에서 네이버 계정을 먼저 저장해 주세요.",
            file=sys.stderr,
        )
        return 1

    if not config.has_session:
        print(
            "저장된 네이버 세션이 없습니다. 먼저 로그인해 주세요:\n"
            "    python apps/api/scripts/naver_login.py",
            file=sys.stderr,
        )
        return 1

    post = _build_sample_post()
    plan = build_naver_publish_plan(post, "naver-paste-test")

    print(f"계획된 텍스트 블록: {len(plan.expected_text_blocks)}개")
    print(f"계획된 이미지(앵커): {len(plan.image_anchors)}개")
    print("실제 발행 경로(스캐폴드 1회 붙여넣기 + 앵커 교체 + 검증)를 태웁니다. 발행하지 않습니다.\n")
    try:
        await fill_editor_for_preview(config, plan)
    except Exception:
        print("\n붙여넣기/검증에 실패했습니다. 식별정보 없는 서버 로그를 확인하세요.", file=sys.stderr)
        return 1

    print("\n검증까지 통과했습니다. (아무것도 발행하지 않았습니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
