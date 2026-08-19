"""환경 변수. .env는 실행 위치와 무관하게 저장소 루트에서 찾는다."""

import os
from pathlib import Path

from app.llm import LlmConfig, resolve_llm_config

DEFAULT_PORT = 3000
DEFAULT_MONGODB_URI = "mongodb://localhost:27017/blog_it"


def find_env_file() -> Path | None:
    """이 파일에서 위로 올라가며 저장소 루트의 .env를 찾는다.

    예전에는 단순 상대 경로로 읽어서, 프로세스가 마침 저장소 루트에서 시작할 때만
    찾을 수 있었다. 그 외 위치에서는 키가 조용히 미설정으로 읽혔다 — 지금은 그러면
    API가 시작을 거부하고 작업 디렉터리가 아니라 빠진 키를 원인으로 알린다.
    """
    for directory in Path(__file__).resolve().parents:
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_env_file(path: str | None = None) -> bool:
    """최소한의 .env 리더. 실제 환경 변수가 파일 값보다 우선하며,
    Node의 process.loadEnvFile 동작과 같다."""
    env_path = Path(path) if path else find_env_file()
    if env_path is None or not env_path.is_file():
        return False

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

    return True


def llm_config_from_env() -> LlmConfig:
    return resolve_llm_config(dict(os.environ))


def mongodb_uri() -> str:
    return os.environ.get("MONGODB_URI") or DEFAULT_MONGODB_URI


def redis_url() -> str | None:
    """비어 있으면 Redis 없이 도는 배포다(로컬 개발). 접속 정보는 코드에 두지 않는다."""
    return (os.environ.get("REDIS_URL") or "").strip() or None


def _positive_number(name: str, default: float) -> float:
    """설정값이 비었거나 말이 안 되면 기본값. 잘못된 값 때문에 서버가 못 뜨는 것보다,
    기본값으로 돌면서 로그를 남기는 편이 낫다 — TTL 오타로 전체가 멈추면 안 된다."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        import logging

        logging.getLogger(__name__).warning(
            "%s=%r 는 양수가 아닙니다. 기본값 %s 를 씁니다.", name, raw, default
        )
        return default
    return value


def trend_exposure_ttl_seconds() -> float:
    """노출 이력 보관 기간. 기본 24시간 — 하루가 지나면 같은 키워드를 다시 보여도 된다."""
    return _positive_number("TREND_EXPOSURE_TTL_SECONDS", 24 * 60 * 60.0)


def trend_exposure_max_entries() -> int:
    """사용자·글·모드당 이력 개수 상한. 키 하나가 무한정 자라지 않게 한다."""
    return int(_positive_number("TREND_EXPOSURE_MAX_ENTRIES", 120))


def final_review_max_rounds() -> int:
    """최종 검수를 몇 번까지 도는가. 한 회차 = 검수 1회 + 지적된 자리 교정.

    2026-08-05 사용자 결정으로 기본 3회다. 값을 코드에 박지 않는 이유는 이 숫자가
    비용과 직결되기 때문이다 — 운영에서 검수가 너무 자주 돈다고 판단되면 배포 없이
    1로 내릴 수 있어야 한다. 0으로 두면 검수를 건너뛴다(끄는 스위치).
    """
    raw = (os.environ.get("FINAL_REVIEW_MAX_ROUNDS") or "").strip()
    if not raw:
        return 3
    try:
        value = int(float(raw))
    except ValueError:
        return 3
    # 위쪽 상한은 안전장치다. 오타로 30이 들어가면 글 한 편에 모델을 서른 번 부른다.
    return max(0, min(value, 5))


def final_review_length_tolerance() -> float:
    """목표 상한을 몇 배까지 봐주는가. 품질검사가 '길다'고 말하기 시작하는 지점이다.

    2026-08-05 사용자 지시: "목표가 2,300자면 2,600~3,000자까지는 즉시 실패로 두지 마라."
    2300 × 1.3 ≈ 2,990이라 기본값은 1.3이다. **비율로 두는 것이 핵심이다** — 길이 목표
    (짧게/중간)가 바뀌어도 허용 폭이 따라 움직이고, 값 하나만 고치면 전 구간에 적용된다.

    1.0 미만은 목표 상한보다 좁다는 뜻이라 받지 않는다.
    """
    return max(1.0, _positive_number("FINAL_REVIEW_LENGTH_TOLERANCE", 1.3))


def app_environment() -> str:
    return (os.environ.get("APP_ENV") or "development").strip().lower()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def allow_in_memory_storage() -> bool:
    return _env_flag("ALLOW_IN_MEMORY_STORAGE", app_environment() != "production")


def seed_demo_account() -> bool:
    return _env_flag("SEED_DEMO_ACCOUNT", False)


def cors_allowed_origins() -> list[str]:
    """브라우저 CORS 허용 목록. 운영은 같은 origin이 기본이라 빈 목록이 안전하다."""
    configured = (os.environ.get("CORS_ALLOWED_ORIGINS") or "").strip()
    if configured:
        origins = [value.strip().rstrip("/") for value in configured.split(",") if value.strip()]
        if app_environment() == "production" and "*" in origins:
            raise RuntimeError("운영 환경에서는 CORS_ALLOWED_ORIGINS에 '*'를 사용할 수 없습니다.")
        return origins
    if app_environment() == "production":
        return []
    return ["http://localhost:5173", "http://127.0.0.1:5173"]


def port() -> int:
    return int(os.environ.get("PORT") or DEFAULT_PORT)


def web_dir() -> Path:
    """빌드된 프론트엔드로, 운영 환경에서 API가 서빙한다. apps/api/app/ -> apps/web/dist.

    개발 환경에는 없다: `npm run dev`는 Vite에서 프론트엔드를 서빙하고 API를 이쪽으로
    프록시하므로, 이 디렉터리는 `npm run build` 이후에만 존재한다.
    """
    configured = os.environ.get("WEB_DIR")
    if configured:
        return Path(configured).resolve()
    return (Path(__file__).parent.parent.parent / "web" / "dist").resolve()
