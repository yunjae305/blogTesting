"""B-0 실험: 네이버 SmartEditor가 **합성 DataTransfer 붙여넣기**를 받는지 확인한다.

    python apps/api/scripts/probe_synthetic_paste.py

naver_paste_test.py와 완전히 같은 실제 발행 경로(제목 → 스캐폴드 CF_HTML → 이미지
바이트 → 캡션 → 발행 전 검증)를 태우되, ``NAVER_PASTE_MODE=synthetic``으로 돌린다 —
OS 클립보드 대신 페이지 안에서 ClipboardEvent(paste)를 직접 만들어 쏜다.

이 실험이 판가름하는 것 (2026-08-19부터 **기본 모드가 synthetic**이다):

- **통과** → 기본 모드 그대로 두면 된다. 발행에서 OS 클립보드가 빠져 발행끼리
  완전히 독립이고(잠금 불필요), 서버에서 사람이 복사를 해도 발행과 겹치지 않는다.
- **이미지 단계에서 실패** → SmartEditor가 신뢰하지 않는(isTrusted=false) 이미지
  paste를 거부하는 것이다. 업로드 폴백까지 실패하면 운영 .env에
  ``NAVER_PASTE_MODE=auto``(거부 시 그 발행만 클립보드로 전환, 권장) 또는
  ``clipboard``(항상 기존 경로)를 넣는다. 이미지만 좁혀 보려면
  ``probe_synthetic_image_paste.py``를 쓴다.

먼저 세션을 저장해 둔다: ``python apps/api/scripts/naver_login.py``.
발행 버튼은 누르지 않는다.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 실험의 전부가 이 한 줄이다 — 같은 경로, 다른 붙여넣기 방식.
os.environ["NAVER_PASTE_MODE"] = "synthetic"

from scripts.naver_paste_test import main  # noqa: E402


if __name__ == "__main__":
    print("=== B-0: 합성 붙여넣기 실험 (NAVER_PASTE_MODE=synthetic) ===\n")
    code = asyncio.run(main())
    if code == 0:
        print(
            "\n[B-0 판정] 합성 붙여넣기가 검증까지 통과했습니다."
            "\n기본 모드가 synthetic이므로 그대로 두면 발행이 OS 클립보드 없이 돕니다."
        )
    else:
        print(
            "\n[B-0 판정] 합성 붙여넣기가 실패했습니다. 어느 단계(제목/스캐폴드/이미지)에서"
            "\n멈췄는지 위 [NAVER_PUBLISH] 로그로 확인하세요. 운영 .env에"
            "\nNAVER_PASTE_MODE=auto(거부 시 그 발행만 클립보드 전환, 권장) 또는"
            "\nclipboard(항상 기존 경로)를 넣으면 기존 방식으로 돕니다.",
            file=sys.stderr,
        )
    raise SystemExit(code)
