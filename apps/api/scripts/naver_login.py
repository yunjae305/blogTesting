"""Selenium Chrome에 네이버 로그인 세션을 한 번 저장하는 스크립트.

    python apps/api/scripts/naver_login.py

Chrome이 열리면 직접 로그인한다. 같은 프로필에 v2 암호화 자격증명이 저장돼 있으면 로그인
폼을 자동으로 채우고, 없으면 사용자가 직접 입력한다. 세션은 .naver-profile에 남는다.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._env import load_env_file  # noqa: E402

load_env_file()

from app.posting.config import naver_config_from_env  # noqa: E402
from app.posting.naver import log_in_and_store_session  # noqa: E402


async def main() -> int:
    config = naver_config_from_env()
    if config is None:
        print(
            "공개 블로그 주소 NAVER_BLOG_ID를 지정하거나 설정 화면에서 네이버 계정을 먼저 저장해 주세요.",
            file=sys.stderr,
        )
        return 1

    print("Chrome에서 네이버 로그인을 완료해 주세요. 최대 3분간 기다립니다.\n")
    try:
        await log_in_and_store_session(config)
    except Exception:
        print("\n로그인에 실패했습니다. 서버 로그의 식별정보 없는 오류 유형을 확인하세요.", file=sys.stderr)
        return 1

    print("\n로그인 세션을 안전한 로컬 프로필에 저장했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
