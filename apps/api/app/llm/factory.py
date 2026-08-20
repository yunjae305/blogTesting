"""콘텐츠 역할마다 live provider를 만들고, 만들 수 없으면 시작을 거부한다.

예전에는 역할마다 mock 구현이 있었고 LLM_MODE가 둘 중 하나를 골랐다. 기본값 `auto`는
키가 있으면 live, 나머지는 조용히 mock으로 돌려서, 만료된 키가 오류를 내지 않고 그럴듯한
가짜 원고를 발행 화면까지 흘려보냈다. 그 절충은 없앴다. 글과 검색을 만드는 역할은 모두
live로 돌고, 그럴 수 없는 역할은 이유를 밝히며 시작 실패가 된다. 선택 기능인 트렌드는
자격 증명이 있는 소스만 돌린다 — 구글 트렌드는 키가 없어도 공식 RSS로 돌기 때문에
소스가 하나도 없는 상태는 이제 없다.
"""

from dataclasses import dataclass
from typing import Callable, TypeVar

from .contracts import (
    KeywordRelevanceRanker,
    DraftGenerator,
    PhotoSearch,
    PostImageGenerator,
    TopicEvaluator,
    TopicGenerator,
    TrendProvider,
    SiteReader,
    WebSearchAnalyzer,
)
from .live_adapters import (
    AnthropicDraftGenerator,
    AnthropicKeywordRanker,
    AnthropicTopicEvaluator,
    AnthropicTopicGenerator,
    GeminiResearchAnalyzer,
    GeminiSiteReader,
    OpenAiFinalReviewer,
    OpenAiPostImageGenerator,
)
from .naver_blog import NaverBlogResearch
from .naver_news import NaverNewsResearch
from .photo_search import NaverPhotoSearch, YouTubeThumbnailSearch
from .provider_config import (
    INSTAGRAM_TOKEN_ENV,
    INSTAGRAM_USER_ID_ENV,
    NAVER_CLIENT_ID_ENV,
    NAVER_CLIENT_SECRET_ENV,
    YOUTUBE_API_KEY_ENV,
    LlmConfig,
    LlmConfigError,
    LlmProvider,
    LlmRole,
    RoleConfig,
)
from .trends import (
    AggregateTrendProvider,
    GoogleTrendsCollector,
    InstagramTrendCollector,
    NaverTrendCollector,
    TrendCollector,
    YouTubeTrendCollector,
    create_pool_cache,
)

LIVE_DRAFT_GENERATORS: dict[LlmProvider, Callable[[RoleConfig], DraftGenerator]] = {
    LlmProvider.ANTHROPIC: AnthropicDraftGenerator,
}
LIVE_TOPIC_GENERATORS: dict[LlmProvider, Callable[[RoleConfig], TopicGenerator]] = {
    LlmProvider.ANTHROPIC: AnthropicTopicGenerator,
}
LIVE_POST_IMAGE_GENERATORS: dict[LlmProvider, Callable[[RoleConfig], PostImageGenerator]] = {
    LlmProvider.OPENAI: OpenAiPostImageGenerator,
}
# 2차 품질 검수. 그림을 실제로 볼 수 있는 provider여야 한다.
LIVE_FINAL_REVIEWERS: dict[LlmProvider, Callable[[RoleConfig], object]] = {
    LlmProvider.OPENAI: OpenAiFinalReviewer,
}

# 합성 analyzer가 동작하려면 M3의 각 절반이 반드시 써야 하는 provider.
#
# 2026-08-07부터 정리도 Gemini다(사용자 결정). 수집은 Google Search grounding을 쓸 수
# 있어야 하고, 정리는 responseSchema로 JSON을 강제할 수 있어야 하는데 Gemini가 둘 다
# 한다 — 실제 호출로 확인했다(schemas.to_gemini_schema 주석).
M3_COLLECT_PROVIDER = LlmProvider.GEMINI
M3_SUMMARY_PROVIDER = LlmProvider.GEMINI

TREND_LABEL = "M2 트렌드 키워드"

# `def _resolve[T](...)`(PEP 695)를 쓰지 않는다 — 그 문법은 3.12부터라 pyproject의
# requires-python(>=3.11)을 어긴다. 이 저장소의 다른 제네릭도 같은 방식이다
# (`posting/naver/browser.py`).
T = TypeVar("T")


@dataclass
class RoleStatus:
    role: str
    label: str
    provider: str
    model: str
    # 출력할 만한 부가 정보. 로그에 남겨도 안전하다 — 키는 담기지 않는다.
    note: str = ""


@dataclass
class LlmProviders:
    trend_provider: TrendProvider
    topic_generator: TopicGenerator
    # 제목 루브릭 채점기(있으면). 제목 어댑터와 같은 M2 역할에서 돌며, 없으면 규칙 기반으로만
    # 채점한다 — 그래서 optional이고 앱 시작을 막지 않는다.
    topic_evaluator: TopicEvaluator | None
    web_search_analyzer: WebSearchAnalyzer
    draft_generator: DraftGenerator
    post_image_generator: PostImageGenerator
    # 실제 사진 검색기(있으면). 네이버 검색 자격 증명이 없으면 None이고, 그때는
    # 예전처럼 이미지 생성만 한다 — 없다고 앱이 못 뜨지는 않는다.
    photo_search: PhotoSearch | None
    # 유튜브 썸네일 소스(있으면). 카드 계획이 YOUTUBE_THUMBNAIL을 골랐을 때 쓴다.
    youtube_photo_search: PhotoSearch | None
    # 2차 품질 검수기(있으면). 원고를 쓴 모델과 **다른 모델**이 같은 원고를 한 번 더 보고,
    # 그림은 실제로 본다. 없으면 예전처럼 1차 검수 결과만 쓴다 — 이것 때문에 앱이 못 뜨지
    # 않는다(마무리 단계이지 관문이 아니다).
    final_reviewer: object | None
    # 브랜드 사이트를 읽어 자료를 채워 주는 쪽(있으면). 글을 쓰는 길에 있는 것이 아니라
    # 자료 입력을 덜어 주는 편의라, 없어도 앱은 그대로 뜬다.
    site_reader: SiteReader | None
    status: list[RoleStatus]


def _role_or_throw(config: LlmConfig, role: LlmRole) -> RoleConfig:
    for candidate in config.roles:
        if candidate.role == role:
            return candidate
    raise LlmConfigError(f"missing configuration for role {role.value}")


def _blocker(role: RoleConfig, adapters: dict[LlmProvider, object]) -> str | None:
    """이 역할이 돌 수 없는 이유, 돌 수 있으면 None."""
    if not role.has_credentials:
        return f"{role.api_key_env} is not set"
    if role.provider not in adapters:
        return f"no live {role.provider.value} adapter is implemented"
    return None


def _status(role: RoleConfig) -> RoleStatus:
    return RoleStatus(
        role=role.role.value,
        label=role.label,
        provider=role.provider.value,
        model=role.model,
    )


def _resolve(
    config: LlmConfig,
    role: LlmRole,
    live_adapters: dict[LlmProvider, Callable[[RoleConfig], T]],
    blockers: list[str],
) -> tuple[T | None, RoleStatus]:
    """어댑터를 만들거나, 만들 수 없는 이유를 기록한다.

    첫 번째에서 바로 예외를 던지지 않고 blocker를 모으므로, 시작 오류가 재시작마다 하나씩이
    아니라 잘못된 것을 한 번에 모두 알려준다.
    """
    resolved = _role_or_throw(config, role)
    blocker = _blocker(resolved, live_adapters)
    if blocker:
        blockers.append(f"  - {resolved.label}: {blocker}")
        return None, _status(resolved)

    return live_adapters[resolved.provider](resolved), _status(resolved)


def _resolve_web_search(
    config: LlmConfig, blockers: list[str]
) -> tuple[WebSearchAnalyzer | None, list[RoleStatus]]:
    """M3는 두 절반이 다 필요하고, 각 절반이 provider에 고정돼야 한다 — collector는 Google
    Search로 grounding할 수 있어야 하고, summariser는 JSON 스키마에 묶여야 한다.

    둘 다 Gemini이지만 **역할은 여전히 갈라 둔다.** 모델을 따로 지정할 수 있어야 하기
    때문이다(M3_COLLECT_MODEL / M3_SUMMARY_MODEL) — 수집은 빠른 모델로, 정리는 방향을
    가르는 판단이라 필요하면 더 나은 모델로 올릴 수 있다."""
    collect = _role_or_throw(config, LlmRole.M3_COLLECT)
    summary = _role_or_throw(config, LlmRole.M3_SUMMARY)

    def half_blocker(role: RoleConfig, expected: LlmProvider) -> str | None:
        if not role.has_credentials:
            return f"{role.api_key_env} is not set"
        if role.provider != expected:
            return f"must be {expected.value}, not {role.provider.value}"
        return None

    ok = True
    for role, expected in ((collect, M3_COLLECT_PROVIDER), (summary, M3_SUMMARY_PROVIDER)):
        blocker = half_blocker(role, expected)
        if blocker:
            blockers.append(f"  - {role.label}: {blocker}")
            ok = False

    statuses = [_status(collect), _status(summary)]
    if not ok:
        return None, statuses
    # 네이버 자격 증명이 있으면 검증 자료에 네이버 블로그 실사용 글을 보강한다
    # (2026-08-10 사용자 결정). 없으면 예전 그대로 — 구글 검색만.
    blog_research = (
        NaverBlogResearch(
            config.trend.naver_client_id, config.trend.naver_client_secret
        )
        if config.trend.has_naver
        else None
    )
    # 관련된 최신 기사도 함께 모은다(2026-08-11 사용자 지시). 같은 자격 증명이라 새로
    # 발급할 것이 없고, 없으면 예전 그대로 — 구글 수집 + 블로그 보강만.
    news_research = (
        NaverNewsResearch(
            config.trend.naver_client_id, config.trend.naver_client_secret
        )
        if config.trend.has_naver
        else None
    )
    return (
        GeminiResearchAnalyzer(
            collect, summary, blog_research=blog_research, news_research=news_research
        ),
        statuses,
    )


def _resolve_site_reader(config: LlmConfig) -> SiteReader | None:
    """브랜드 사이트를 읽는 쪽(2026-08-20). **M3와 같은 자격 증명을 쓴다.**

    새 역할·새 환경변수를 만들지 않는 이유: 키를 하나 더 넣지 않았다는 이유로 이 기능만
    조용히 죽는다. M3가 도는 서버라면 이것도 돈다.

    돌 수 없으면 None이다 — blocker에 넣지 않는다. 이것은 글을 쓰는 길에 있는 것이
    아니라 자료를 채워 주는 편의라, 없다고 서버가 못 뜨면 안 된다. 화면이 "지금은 못
    쓴다"고 알린다.
    """
    collect = _role_or_throw(config, LlmRole.M3_COLLECT)
    summary = _role_or_throw(config, LlmRole.M3_SUMMARY)
    if not (collect.has_credentials and summary.has_credentials):
        return None
    if collect.provider != M3_COLLECT_PROVIDER or summary.provider != M3_SUMMARY_PROVIDER:
        return None
    return GeminiSiteReader(collect, summary)


def _resolve_trends(
    config: LlmConfig,
    blockers: list[str],
    ranker: KeywordRelevanceRanker | None,
) -> tuple[TrendProvider | None, RoleStatus]:
    """M2는 자격 증명이 있는 모든 소스를 돌린다.

    이건 여전히, 그리고 의도적으로 degrade한다: 소스들이 독립적이라 Instagram 토큰이
    없어도 Google·Naver·YouTube는 계속 수집한다. 이제 허용되지 않는 것은 그중 하나도 없는
    경우다.
    """
    trend = config.trend
    status = RoleStatus(role="m2-trend", label=TREND_LABEL, provider="multi", model="none")

    collectors: list[TrendCollector] = []
    live_sources: list[str] = []
    skipped: list[str] = []

    # 구글 트렌드는 키를 쓰지 않는다 — 트렌드 페이지를 브라우저로 직접 읽는다
    # (google_trends.py 모듈 docstring: SerpApi 크레딧 소진·RSS의 정보 부족이 이유).
    # 그래서 SERPAPI_API_KEY는 더 이상 수집에 관여하지 않는다. 필요한 것은 Chrome이고,
    # 없으면 이 소스만 빈 손으로 돌아온다(나머지 소스가 패널을 채운다).
    collectors.append(GoogleTrendsCollector())
    live_sources.append("google_trends(web)")

    if trend.has_naver:
        collectors.append(NaverTrendCollector(trend.naver_client_id, trend.naver_client_secret))
        live_sources.append("naver")
    else:
        skipped.append(f"{NAVER_CLIENT_ID_ENV}/{NAVER_CLIENT_SECRET_ENV}")

    if trend.has_youtube:
        collectors.append(
            YouTubeTrendCollector(trend.youtube_api_key, trend.youtube_api_referrer)
        )
        live_sources.append("youtube")
    else:
        skipped.append(YOUTUBE_API_KEY_ENV)

    if trend.has_instagram:
        collectors.append(
            InstagramTrendCollector(
                trend.instagram_access_token,
                trend.instagram_user_id,
                trend.instagram_api_version,
            )
        )
        live_sources.append("instagram")
    else:
        skipped.append(f"{INSTAGRAM_TOKEN_ENV}/{INSTAGRAM_USER_ID_ENV}")

    # 소스가 하나도 없는 경우는 이제 없다 — 구글 트렌드가 키 없이도 공식 RSS로 돌기
    # 때문이다. 그래서 '자격 증명이 하나도 없으면 트렌드를 끈다'는 분기(UnavailableTrendProvider)는
    # 도달할 수 없는 코드가 되어 지웠다. 트렌드가 비활성으로 남는 경로는 하나뿐이다:
    # 수집기가 전부 실패하는 것(런타임 문제이고, 그건 소스별로 이미 견딘다).
    cache = create_pool_cache(trend.redis_url)

    status.model = "+".join(live_sources)
    notes = [f"캐시: {cache.name}"]
    if skipped:
        notes.append(f"skipping {', '.join(skipped)}")
    status.note = ", ".join(notes)
    # 결정적 랭킹: 프로덕션은 상위 후보 창을 무작위로 회전하지 않고 점수순 그대로 내보낸다.
    # 화면이 후보를 4개가 아니라 목록으로 보여주므로, 새로고침이 "다음 배치"를 부르는 것은
    # exclude_keywords가 이미 처리한다 — 회전의 무작위성은 "왜 이게 추천됐나"를 흐릴 뿐이다.
    return AggregateTrendProvider(
        collectors,
        cache=cache,
        ranker=ranker,
        rotate=lambda size: 0,
    ), status


def create_llm_providers(config: LlmConfig) -> LlmProviders:
    """콘텐츠 생성 역할은 live로 강제하고, 선택적인 트렌드는 명시적으로 비활성화한다."""
    blockers: list[str] = []

    web_search, m3_statuses = _resolve_web_search(config, blockers)
    topic, topic_status = _resolve(config, LlmRole.M2_TOPIC, LIVE_TOPIC_GENERATORS, blockers)

    # 관련도는 제목을 쓰는 것과 같은 역할이 판단한다: 같은 종류의 편집 판단이고, 두 번째
    # 모델은 유지할 것이 하나 더 늘어나는 일이다.
    m2_role = _role_or_throw(config, LlmRole.M2_TOPIC)
    ranker = (
        AnthropicKeywordRanker(m2_role)
        if m2_role.has_credentials and m2_role.provider == LlmProvider.ANTHROPIC
        else None
    )
    # 제목 채점기도 같은 M2 역할에서 돈다(관련도 랭커와 동일한 이유). 없으면 규칙 기반 채점만 쓴다.
    topic_evaluator = (
        AnthropicTopicEvaluator(m2_role)
        if m2_role.has_credentials and m2_role.provider == LlmProvider.ANTHROPIC
        else None
    )
    draft, draft_status = _resolve(config, LlmRole.M4_DRAFT, LIVE_DRAFT_GENERATORS, blockers)
    image, image_status = _resolve(config, LlmRole.M5_IMAGE, LIVE_POST_IMAGE_GENERATORS, blockers)
    # 2차 검수는 **없어도 앱이 떠야 한다.** blocker 목록에 넣지 않고 조용히 None으로 둔다 —
    # 이 단계가 빠지면 검수가 한 번만 돌 뿐 원고는 그대로 나온다.
    review_role = _role_or_throw(config, LlmRole.M4_REVIEW)
    reviewer_factory = LIVE_FINAL_REVIEWERS.get(review_role.provider)
    final_reviewer = (
        reviewer_factory(review_role)
        if reviewer_factory is not None and review_role.has_credentials
        else None
    )
    review_status = _status(review_role)
    trends, trend_status = _resolve_trends(config, blockers, ranker)
    # 소재의 실제 사진 검색. 트렌드 수집과 같은 네이버 검색 자격 증명을 쓴다. 없으면 None —
    # 이미지 생성은 그대로 돌고 인물 사진만 못 구한다. 그것 때문에 앱을 못 뜨게 하지 않는다.
    photo_search = (
        NaverPhotoSearch(config.trend.naver_client_id, config.trend.naver_client_secret)
        if config.trend.has_naver
        else None
    )
    # 유튜브 썸네일 소스. 트렌드 수집과 같은 YOUTUBE_API_KEY를 쓴다. 없으면 None —
    # 카드가 YOUTUBE_THUMBNAIL을 골라도 네이버→생성 사다리로 폴백한다.
    youtube_photo_search = (
        YouTubeThumbnailSearch(config.trend.youtube_api_key, config.trend.youtube_api_referrer)
        if config.trend.has_youtube
        else None
    )

    if blockers:
        raise LlmConfigError(
            "these roles cannot run live:\n"
            + "\n".join(blockers)
            + "\n\nSet the missing keys in .env. There is no mock to fall back to:"
            " a fake article is worse than a refusal to start."
        )

    return LlmProviders(
        trend_provider=trends,
        topic_generator=topic,
        topic_evaluator=topic_evaluator,
        web_search_analyzer=web_search,
        draft_generator=draft,
        post_image_generator=image,
        photo_search=photo_search,
        youtube_photo_search=youtube_photo_search,
        final_reviewer=final_reviewer,
        site_reader=_resolve_site_reader(config),
        status=[
            trend_status,
            topic_status,
            *m3_statuses,
            draft_status,
            review_status,
            image_status,
        ],
    )


def describe_llm_status(status: list[RoleStatus]) -> list[str]:
    """역할마다 출력용 한 줄. 로그에 남겨도 안전하다 — 키는 담기지 않는다."""
    return [
        f"  - {entry.label}: [{entry.provider}/{entry.model}]"
        + (f" ({entry.note})" if entry.note else "")
        for entry in status
    ]
