"""환경 변수로부터 각 모듈을 어떤 LLM provider가 맡을지 정한다.

순수 함수: os.environ을 직접 읽지 않고 평범한 env 매핑만 읽으며, 원본 키를 로그에
남기거나 반환하지 않는다. 역할별 provider·모델 배정은 아래 ROLE_SPECS가 전부다.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from app.llm.youtube_api import (
    DEFAULT_YOUTUBE_API_REFERRER,
    YOUTUBE_API_REFERRER_ENV,
)


class LlmProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"


class LlmRole(StrEnum):
    M2_TOPIC = "m2-topic"
    M3_COLLECT = "m3-collect"
    M3_SUMMARY = "m3-summary"
    M4_DRAFT = "m4-draft"
    M4_REVIEW = "m4-review"
    M5_IMAGE = "m5-image"


# provider마다 필요한 자격 증명.
API_KEY_ENV: dict[LlmProvider, str] = {
    LlmProvider.ANTHROPIC: "ANTHROPIC_API_KEY",
    LlmProvider.OPENAI: "OPENAI_API_KEY",
    LlmProvider.GEMINI: "GOOGLE_API_KEY",
}

# M2는 여러 소스에서 수집한다. Google Trends(SerpApi)가 기준선이고 — 지금 뜨는 것을
# 실시간으로 주는 유일한 소스다 — 나머지는 자격 증명이 있을 때 이를 보강한다.
TREND_API_KEY_ENV = "SERPAPI_API_KEY"
NAVER_CLIENT_ID_ENV = "NAVER_CLIENT_ID"
NAVER_CLIENT_SECRET_ENV = "NAVER_CLIENT_SECRET"
YOUTUBE_API_KEY_ENV = "YOUTUBE_API_KEY"
INSTAGRAM_TOKEN_ENV = "FACEBOOK_USER_ACCESS_TOKEN"
INSTAGRAM_USER_ID_ENV = "INSTAGRAM_BUSINESS_ACCOUNT_ID"
INSTAGRAM_API_VERSION_ENV = "INSTAGRAM_GRAPH_API_VERSION"

# 선택: 수집한 트렌드 풀을 캐시할 곳. 설정 안 하면 프로세스 메모리.
REDIS_URL_ENV = "REDIS_URL"

DEFAULT_INSTAGRAM_API_VERSION = "v25.0"


@dataclass(frozen=True)
class RoleSpec:
    role: LlmRole
    label: str
    provider_env: str
    model_env: str
    default_provider: LlmProvider
    default_model: str


ROLE_SPECS: list[RoleSpec] = [
    RoleSpec(
        role=LlmRole.M2_TOPIC,
        label="M2 제목 추천",
        provider_env="M2_TOPIC_PROVIDER",
        model_env="M2_TOPIC_MODEL",
        # 제목은 사용자가 원고를 확정하기 전에 읽는 유일한 것이고, 프롬프트는 일부러
        # 자극적인 한글 카피를 요구한다 — 원고를 쓰는 바로 그 모델이 이를 감당할 수 있다.
        default_provider=LlmProvider.ANTHROPIC,
        default_model="claude-opus-5",
    ),
    RoleSpec(
        role=LlmRole.M3_COLLECT,
        label="M3 자료 수집",
        provider_env="M3_COLLECT_PROVIDER",
        model_env="M3_COLLECT_MODEL",
        default_provider=LlmProvider.GEMINI,
        # 2026-08-05 사용자 결정으로 3.6-flash를 기본값에 둔다.
        #
        # 속도만 보면 이 선택이 아니었다. 실측(2026-08-03, 같은 수집 프롬프트로 모델당
        # 5회)에서 3.6-flash는 중앙값 34.0초 · 최대 69.8초로 45초 상한을 2/5에서 넘겼고,
        # 2.5-flash는 12.6초 · 최대 23.8초로 한 번도 넘기지 않았다. 그 표는 지우지 않고
        # live_adapters.RESEARCH_FALLBACK_MODELS 위에 그대로 남겨 뒀다.
        #
        # 상한을 넘겨도 수집이 실패하지는 않는다 — 그 목록의 빠른 형제 모델
        # (2.5-flash → 3.5-flash-lite)로 넘어간다. 대신 넘어가는 그 회차는 45초를
        # 통째로 더 쓴다. 사용자가 팝업을 띄워 놓고 기다리는 자리라는 점은 그대로다.
        default_model="gemini-3.6-flash",
    ),
    RoleSpec(
        role=LlmRole.M3_SUMMARY,
        label="M3 요약·의도 후보",
        provider_env="M3_SUMMARY_PROVIDER",
        model_env="M3_SUMMARY_MODEL",
        # 2026-08-07 사용자 결정으로 OpenAI에서 Gemini로 옮겼다. 수집과 정리를 한
        # provider로 모은 것이고, 수집 호출은 그대로라 자료의 질은 달라지지 않는다.
        # 역할은 갈라 둔다 — 정리는 방향을 가르는 판단이라 필요하면 모델만 올릴 수 있다.
        default_provider=LlmProvider.GEMINI,
        default_model="gemini-3.6-flash",
    ),
    RoleSpec(
        role=LlmRole.M4_REVIEW,
        label="M4 품질 검수(2차)",
        provider_env="M4_REVIEW_PROVIDER",
        model_env="M4_REVIEW_MODEL",
        # 원고를 쓴 모델과 **다른 모델**이어야 뜻이 있다. 자기가 쓴 글을 자기가 보면
        # 같은 자리를 같은 이유로 지나친다. 그림을 실제로 볼 수 있는 쪽이기도 하다
        # (2026-08-07 사용자 결정 — Claude와 GPT 둘이 각자 보고 지적을 합친다).
        default_provider=LlmProvider.OPENAI,
        default_model="gpt-5.6-sol",
    ),
    RoleSpec(
        role=LlmRole.M4_DRAFT,
        label="M4 원고 생성",
        provider_env="M4_DRAFT_PROVIDER",
        model_env="M4_DRAFT_MODEL",
        default_provider=LlmProvider.ANTHROPIC,
        default_model="claude-opus-5",
    ),
    RoleSpec(
        role=LlmRole.M5_IMAGE,
        label="M5 이미지 생성",
        provider_env="M5_IMAGE_PROVIDER",
        model_env="M5_IMAGE_MODEL",
        default_provider=LlmProvider.OPENAI,
        default_model="gpt-image-2",
    ),
]


@dataclass(frozen=True)
class RoleConfig:
    role: LlmRole
    label: str
    provider: LlmProvider
    model: str
    # 이 provider의 키를 담은 환경 변수 이름.
    api_key_env: str
    # 키 자체. 절대 로그에 남기지 않는다.
    api_key: str | None
    has_credentials: bool


@dataclass(frozen=True)
class TrendConfig:
    # SerpApi / Google Trends. 기준 소스이고, 나머지는 자격 증명이 있을 때 보강한다.
    api_key_env: str
    api_key: str | None
    has_credentials: bool
    naver_client_id: str | None = None
    naver_client_secret: str | None = None
    youtube_api_key: str | None = None
    # 유튜브 키에 HTTP 리퍼러 제한이 걸려 있을 때 호출에 실어 보낼 리퍼러(youtube_api 참고).
    youtube_api_referrer: str = DEFAULT_YOUTUBE_API_REFERRER
    instagram_access_token: str | None = None
    instagram_user_id: str | None = None
    instagram_api_version: str = DEFAULT_INSTAGRAM_API_VERSION
    redis_url: str | None = None

    @property
    def has_naver(self) -> bool:
        """DataLab과 Search 둘 다 id와 secret이 필요하다."""
        return bool(self.naver_client_id and self.naver_client_secret)

    @property
    def has_youtube(self) -> bool:
        return bool(self.youtube_api_key)

    @property
    def has_instagram(self) -> bool:
        """Graph 해시태그 검색은 토큰과 그 토큰이 행세할 비즈니스 계정이 함께 필요하다."""
        return bool(self.instagram_access_token and self.instagram_user_id)


@dataclass(frozen=True)
class LlmConfig:
    roles: list[RoleConfig]
    trend: TrendConfig


class LlmConfigError(Exception):
    pass


# .env.example을 복사만 하고 값을 채우지 않은 .env를 걸러낸다.
PLACEHOLDER = re.compile(r"^(<|your[-_ ]|changeme|xxx+$|paste|sk-xxx|todo)", re.IGNORECASE)


def _read_value(env: Mapping[str, str | None], name: str) -> str | None:
    raw = env.get(name)
    value = raw.strip() if raw else ""
    return value or None


def read_api_key(env: Mapping[str, str | None], name: str) -> str | None:
    """진짜처럼 보일 때만 키를 반환한다. 그래서 placeholder는 '설정 안 됨'으로 읽힌다."""
    value = _read_value(env, name)
    return value if value and not PLACEHOLDER.match(value) else None


def resolve_llm_config(env: Mapping[str, str | None]) -> LlmConfig:
    roles = [_resolve_role(env, spec) for spec in ROLE_SPECS]
    serp_key = read_api_key(env, TREND_API_KEY_ENV)
    return LlmConfig(
        roles=roles,
        trend=TrendConfig(
            api_key_env=TREND_API_KEY_ENV,
            api_key=serp_key,
            has_credentials=serp_key is not None,
            naver_client_id=read_api_key(env, NAVER_CLIENT_ID_ENV),
            naver_client_secret=read_api_key(env, NAVER_CLIENT_SECRET_ENV),
            youtube_api_key=read_api_key(env, YOUTUBE_API_KEY_ENV),
            youtube_api_referrer=(
                _read_value(env, YOUTUBE_API_REFERRER_ENV) or DEFAULT_YOUTUBE_API_REFERRER
            ),
            instagram_access_token=read_api_key(env, INSTAGRAM_TOKEN_ENV),
            instagram_user_id=read_api_key(env, INSTAGRAM_USER_ID_ENV),
            instagram_api_version=(
                _read_value(env, INSTAGRAM_API_VERSION_ENV) or DEFAULT_INSTAGRAM_API_VERSION
            ),
            redis_url=_read_value(env, REDIS_URL_ENV),
        ),
    )


def _resolve_role(env: Mapping[str, str | None], spec: RoleSpec) -> RoleConfig:
    raw_provider = (_read_value(env, spec.provider_env) or spec.default_provider).lower()
    try:
        provider = LlmProvider(raw_provider)
    except ValueError:
        providers = ", ".join(p.value for p in LlmProvider)
        raise LlmConfigError(
            f'{spec.provider_env} must be one of {providers}, received "{raw_provider}"'
        ) from None

    api_key_env = API_KEY_ENV[provider]
    api_key = read_api_key(env, api_key_env)

    return RoleConfig(
        role=spec.role,
        label=spec.label,
        provider=provider,
        model=_read_value(env, spec.model_env) or spec.default_model,
        api_key_env=api_key_env,
        api_key=api_key,
        has_credentials=api_key is not None,
    )
