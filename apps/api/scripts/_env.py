"""독립 실행 스크립트들이 공용으로 쓰는 .env 로더.

스크립트는 단독 파일로 실행되기 때문에(`python apps/api/scripts/init_mongo.py`)
`app`을 import할 수 없어 app.config.load_env_file을 재사용하지 못한다. 이게 없으면
.env의 MONGODB_URI가 무시되고 스크립트가 조용히 localhost로 넘어간다 — 겉보기엔
"됐다"처럼 보이지만 실제로는 엉뚱한 DB를 가리킨다.
"""

import os
from pathlib import Path

DEFAULT_MONGODB_URI = "mongodb://localhost:27017/blog_it"


def find_env_file() -> Path | None:
    """저장소 루트의 .env를 찾아 상위 디렉터리로 거슬러 올라간다.

    상대 경로로만 읽으면 프로세스가 마침 저장소 루트에서 시작했을 때만 파일을
    찾을 수 있고, 그 외의 위치에서는 키가 미설정으로 읽힌다. 스크립트는 어디서
    실행하든 동일하게 동작해야 한다.
    """
    for directory in Path(__file__).resolve().parents:
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_env_file(path: str | Path | None = None) -> None:
    """서버와 마찬가지로, 실제 환경 변수가 파일 값보다 우선한다."""
    env_path = Path(path) if path else find_env_file()
    if env_path is None or not env_path.is_file():
        return

    with env_path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")


def mongodb_uri() -> str:
    load_env_file()
    return os.environ.get("MONGODB_URI") or DEFAULT_MONGODB_URI
