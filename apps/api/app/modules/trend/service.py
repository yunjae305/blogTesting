"""M2: 트렌드 키워드를 모으고, 사용자가 고른 키워드로 제목을 쓴다."""

import asyncio
import logging
from dataclasses import dataclass
from app.shared.format import now_iso as _now
from app.shared.ids import short
from app.shared.trend import TrendMode
from typing import Any

from app.errors import BlogTaskError
from app.llm import (
    TOPIC_CANDIDATE_COUNT,
    ExcludedAngle,
    TitleEvaluationInput,
    TopicEvaluator,
    TopicGenerationInput,
    TopicGenerator,
    TrendFetchInput,
    TrendProvider,
)
from app.llm.trends.material_store import material_key
from app.modules.blog_task.jobs import BackgroundJobs
from app.modules.blog_task.repository import BlogTaskRepository
from app.modules.persona.service import PersonaService
from app.modules.trend.topic_scoring import (
    TitleJudgmentScore,
    build_context,
    score_titles,
)
from app.modules.user_settings.service import UserSettingsService
from app.shared import (
    BlogTask,
    BlogTaskInput,
    BlogTaskStatus,
    DraftGenerationSettings,
    TopicCandidate,
    TrendKeyword,
    TrendRecommendationResult,
    TrendSelection,
    TrendTopicResult,
)

from .keyword_store import (
    MAX_LIMIT as MAX_STORED_KEYWORD_LIMIT,
    InMemoryStoredTrendKeywordRepository,
    StoredTrendKeywordRepository,
)
from .validation import (
    validate_select_trend_topic_request,
    validate_topic_generation_request,
    validate_trend_recommendation_request,
)

logger = logging.getLogger(__name__)

TREND_SELECTION_ACTOR = "system:m2-trend-selection"

# M2(트렌드 추천·제목 생성·제목 선택)를 받아 주는 상태.
#
# REFERENCE_PROCESSING만 받던 시절에는 제목을 한 번 고르는 순간 글이 SEARCH_ANALYZING으로
# 옮겨 가면서 제목 단계가 통째로 잠겼다 — 검증 팝업에서 '수정하기'를 눌러 돌아와도 후보를
# 다시 받을 수도, 다른 제목을 고를 수도 없었다. 방향(selected_intent)을 확정하기 전까지는
# 제목을 바꿀 수 있어야 하므로 SEARCH_ANALYZING도 받는다. 확정한 뒤로는 원고가 그 제목으로
# 쓰이기 시작하므로 되돌리지 않는다(상태로도 이미 걸리지만, 옛 문서를 위해 함께 확인한다).
TITLE_EDITABLE_STATUSES = frozenset(
    {BlogTaskStatus.REFERENCE_PROCESSING, BlogTaskStatus.SEARCH_ANALYZING}
)


def _editable_status_names() -> str:
    return ", ".join(sorted(status.value for status in TITLE_EDITABLE_STATUSES))


@dataclass(frozen=True)
class _FallbackAngle:
    template: str
    description: str


# 제목 모델이 부실할 때만 여기 온다. 키워드를 앞세우고 강하게 — 성능이 떨어진 패널도
# 여전히 그 패널처럼 보이도록. 옛 폴백은 다른 제품처럼 읽혔다("...: 트렌드 활용 전략").
FALLBACK_TOPIC_ANGLES = [
    _FallbackAngle(
        template="{keyword} 모르고 {topic} 하면 진짜 손해 봅니다",
        description="손실 회피형",
    ),
    _FallbackAngle(
        template="{keyword}, 아무도 안 알려준 진짜 이유",
        description="폭로형",
    ),
    _FallbackAngle(
        template="{keyword} 하나로 {topic} 결과가 갈립니다",
        description="반전형",
    ),
    _FallbackAngle(
        template="아직도 {keyword} 모르세요? {topic}까지 바뀝니다",
        description="도발형",
    ),
    _FallbackAngle(
        template="{keyword} 지금 확인 안 하면 늦습니다",
        description="위기감형",
    ),
]


def normalize_topic_candidates(
    candidates: list[TopicCandidate], base_topic: str, keyword: TrendKeyword
) -> list[TopicCandidate]:
    """패널 크기에 맞게 자르고, 모든 제목을 선택된 키워드에 고정한다.

    키워드 묶음은 장식이 아니다: 제목을 고르면 그 trendKeywordIds가 원고로 넘어가므로,
    엉뚱한 키워드가 달린 제목은 사용자가 고른 적 없는 키워드로 조용히 글을 쓰게 된다.

    추천(recommended)은 여기서 정하지 않는다 — 모두 False로 두고, 뒤이은 루브릭 채점
    (score_titles)이 최고점 하나에 표시한다. index==0을 추천하던 임의 기준을 없앤 것이다.
    """
    normalized = [
        candidate.model_copy(
            update={
                "recommended": False,
                "trend_keyword_ids": [keyword.trend_keyword_id],
            }
        )
        for candidate in [c for c in candidates if c.title.strip()][:TOPIC_CANDIDATE_COUNT]
    ]
    existing = {candidate.title for candidate in normalized}

    for angle in FALLBACK_TOPIC_ANGLES:
        if len(normalized) >= TOPIC_CANDIDATE_COUNT:
            break

        title = angle.template.format(keyword=keyword.keyword, topic=base_topic)
        if title in existing:
            continue

        normalized.append(
            TopicCandidate(
                topic_candidate_id=f"topic_fallback_{len(normalized) + 1}",
                title=title,
                description=angle.description,
                trend_keyword_ids=[keyword.trend_keyword_id],
                recommended=False,
            )
        )
        existing.add(title)

    return normalized


class TrendService:
    def __init__(
        self,
        repository: BlogTaskRepository,
        trend_provider: TrendProvider,
        topic_generator: TopicGenerator,
        topic_evaluator: TopicEvaluator | None = None,
        user_settings_service: UserSettingsService | None = None,
        persona_service: PersonaService | None = None,
        stored_keywords: StoredTrendKeywordRepository | None = None,
    ):
        self._repository = repository
        self._trend_provider = trend_provider
        self._topic_generator = topic_generator
        # 제목 루브릭의 의미 판단 항(관련성·목적·독자)을 채우는 배치 평가기. 없으면 규칙 기반만.
        self._topic_evaluator = topic_evaluator
        self._user_settings = user_settings_service
        self._personas = persona_service
        # 이미 수집돼 DB에 쌓인 키워드를 그냥 읽는 통로. 없으면 빈 목록을 준다.
        self._stored_keywords = stored_keywords or InMemoryStoredTrendKeywordRepository()
        # 선행 수집이 돌고 있는 (글, 모드). 화면의 요청이 도착하면 새로 돌리지 않고
        # **이것을 기다린다** — 그러지 않으면 같은 수집이 두 번 돌아 비용이 두 배가 된다.
        self._prefetch: dict[tuple[str, str], tuple[asyncio.Task, tuple]] = {}
        self._jobs = BackgroundJobs()
        # 소재 풀 조기 데우기(start_material_pool_warmup)의 진행 중 작업. 소재 키
        # (material_key) 하나당 하나만 돈다.
        self._warmups: dict[str, Any] = {}

    async def shutdown(self) -> None:
        """서버가 내려간다 — 돌고 있는 선행 수집을 정리한다.

        아무것도 잃지 않는다. 선행 수집은 가속 장치일 뿐이라, 다음에 화면이 요청하면
        그때 다시 모은다.
        """
        await self._jobs.cancel()

    async def list_stored_keywords(self, limit: int, shuffle: bool = False) -> list[TrendKeyword]:
        """DB에 쌓인 키워드를 그대로 돌려준다 — 글도, 소스 호출도, 모델도 없다.

        `recommend_topics`와 다르다. 그쪽은 글 하나에 맞춰 관련도를 채점하므로 글이
        먼저 있어야 하고 비용이 든다. "지금 뭐가 있나"만 알고 싶을 때 그 값을 치를
        이유가 없고, 실제로 그 앞단(빈 글 만들기)이 막히면 목록까지 함께 죽었다.
        """
        limit = max(1, min(int(limit), MAX_STORED_KEYWORD_LIMIT))
        return await self._stored_keywords.list_recent(limit, shuffle)

    async def _require_trend_ready_task(self, post_id: str) -> BlogTask:
        task = await self._repository.find_by_post_id(post_id)
        if task is None:
            raise BlogTaskError("NOT_FOUND", f"blogTask {post_id} not found")
        if task.status not in TITLE_EDITABLE_STATUSES or task.selected_intent is not None:
            raise BlogTaskError(
                "INVALID_STATUS_TRANSITION",
                f"M2 requires one of {_editable_status_names()}, received {task.status.value}",
            )
        return task

    async def _load_settings(self, user_id: str) -> DraftGenerationSettings | None:
        """제목을 쓸 페르소나. None이면 사용자가 아직 설정을 저장하지 않았다는 뜻이고,
        프롬프트는 페르소나를 지어내지 않고 그렇다고 말한다."""
        if self._user_settings is None:
            return None
        settings = await self._user_settings.get_by_user_id(user_id)
        if settings is None:
            return None
        persona_prompt = (
            await self._personas.resolve_prompt(
                settings.default_persona,
                settings.custom_persona,
            )
            if self._personas is not None
            else settings.default_persona
        )
        return DraftGenerationSettings(
            hashtag_count=settings.hashtag_count,
            blend_mode=settings.blend_mode,
            # 저장된 id는 페르소나 도메인에서 실제 생성 프롬프트로 해석한다.
            default_persona=persona_prompt,
            # 해석 전의 id도 함께 넘긴다 — 표현 강도 표를 이름 대신 id로 조회한다.
            default_persona_id=settings.default_persona,
            custom_persona_name=settings.custom_persona_name,
            custom_persona_description=settings.custom_persona_description,
            custom_persona=settings.custom_persona,
        )

    async def _write_titles(
        self,
        task: BlogTask,
        post_id: str,
        keyword: TrendKeyword,
        exclude_titles: list[str],
        exclude_angles: list[dict] | None = None,
        regeneration_count: int = 0,
    ) -> tuple[list[TopicCandidate], str]:
        settings = await self._load_settings(task.user_id)
        result = await self._topic_generator.generate_topics(
            TopicGenerationInput(
                post_id=post_id,
                input=task.input,
                trend_keyword=keyword,
                settings=settings,
                exclude_titles=exclude_titles,
                exclude_angles=[
                    ExcludedAngle(
                        title=angle["title"],
                        hook_type=angle.get("hookType"),
                        title_type=angle.get("titleType"),
                    )
                    for angle in (exclude_angles or [])
                ],
                regeneration_count=regeneration_count,
            )
        )
        # 생성과 평가를 분리한다: 위 호출은 제목만 만들고, 아래에서 규칙 + (있으면) LLM 배치
        # 평가로 루브릭 점수를 매겨 최고점 하나를 추천으로 표시한다.
        candidates = normalize_topic_candidates(result.topic_candidates, task.input.topic, keyword)
        scored = await self._score_titles(task, keyword, candidates, exclude_titles)
        return (scored, result.generated_at)

    async def _score_titles(
        self,
        task: BlogTask,
        keyword: TrendKeyword,
        candidates: list[TopicCandidate],
        exclude_titles: list[str] | None = None,
    ) -> list[TopicCandidate]:
        """루브릭 채점으로 추천·점수·근거를 붙인다(관련성30/트렌드25/목적20/독자15/완성도10).

        의미 판단 항은 LLM 배치 평가가 채우고(있으면), 없으면 규칙 기반 근사값을 쓴다. 완성도와
        소재·트렌드 포함 여부는 규칙으로 결정한다. 트렌드가 없으면 트렌드 항을 빼고 재정규화한다.
        """
        context = build_context(
            topic=task.input.topic,
            subject=task.input.subject,
            purpose=task.input.purpose,
            audience=task.input.target_reader,
            trend_keyword=keyword.keyword,
        )
        judgments = await self._evaluate_titles(
            task, keyword, [c.title for c in candidates], exclude_titles
        )
        return score_titles(candidates, context, judgments)

    async def _evaluate_titles(
        self,
        task: BlogTask,
        keyword: TrendKeyword,
        titles: list[str],
        exclude_titles: list[str] | None = None,
    ) -> dict[str, TitleJudgmentScore] | None:
        """LLM 배치 평가(있으면). 실패하면 규칙 기반으로 조용히 대체한다 — 평가는 부가 정보이지,
        제목 패널을 막을 이유가 아니다."""
        if self._topic_evaluator is None or not titles:
            return None
        try:
            raw = await self._topic_evaluator.evaluate_titles(
                TitleEvaluationInput(
                    input=task.input,
                    trend_keyword=keyword,
                    titles=titles,
                    exclude_titles=list(exclude_titles or []),
                )
            )
        except Exception as error:
            logger.warning("제목 평가 실패 - 규칙 기반 채점으로 대체합니다: %s", error)
            return None
        return {
            title: TitleJudgmentScore(
                relevance=judgment.relevance,
                trend_reflection=judgment.trend_reflection,
                purpose_match=judgment.purpose_match,
                audience_interest=judgment.audience_interest,
                reason=judgment.reason,
            )
            for title, judgment in raw.items()
        }

    async def recommend_topics(self, post_id: str, raw_body: Any) -> TrendRecommendationResult:
        """키워드를 모은다. 제목은 여기서 쓰지 않는다.

        예전엔 같은 호출에서 가장 인기 있는 키워드로 제목까지 썼다. 그래서 제목 패널을
        열기만 해도 제목 모델 호출을 한 번 썼다 — 사용자가 고르지도 않았고 영영 고르지
        않을 수도 있는 키워드에. 제목은 제목 추천을 누를 때, 실제로 선택된 키워드로 쓴다.
        """
        request = validate_trend_recommendation_request(raw_body)

        # 입력을 저장할 때 미리 모으기 시작한 것이 아직 돌고 있으면 **그것을 기다린다.**
        # 새로 돌리면 같은 수집이 두 번 나가고, 소재 관련순은 LLM 관련도 판정까지 두 벌
        # 쓴다. 같은 요청인지는 화면이 보내는 값과 선행이 쓴 값이 같은지로 본다 — '다른
        # 후보 보기'(cursor·shuffle)나 강제 수집은 새로 도는 것이 맞다.
        shared = self._matching_prefetch(post_id, request)
        if shared is not None:
            try:
                result = await shared
                logger.info(
                    "키워드 선행 수집에 붙었습니다 | %s %s", short(post_id), request.mode.value
                )
                return result
            except Exception as error:  # noqa: BLE001 - 선행이 실패했으면 그냥 새로 모은다
                logger.info(
                    "선행 수집이 실패해 새로 모읍니다 | %s %s - %s",
                    short(post_id),
                    request.mode.value,
                    error,
                )

        return await self._collect_recommendation(post_id, request)

    async def _collect_recommendation(self, post_id: str, request):
        """실제 수집. **선행분 공유 검사를 지나온 뒤**의 몸통이다.

        선행 수집은 이 메서드를 직접 부른다 — recommend_topics로 들어가면 자기가 방금
        등록한 태스크를 자기가 기다리게 된다.
        """
        task = await self._require_trend_ready_task(post_id)

        # 소재 관련순의 관련도 판정에 쓸 페르소나. 최신순은 판정 모델을 호출하지 않는다.
        # 설정이 없으면 None으로 두고, 판정에서 페르소나 축은 자동 통과한다 — 지어내지 않는다.
        settings = await self._load_settings(task.user_id)
        persona = settings.default_persona if settings else None

        trends = await self._trend_provider.fetch_trends(
            TrendFetchInput(
                post_id=post_id,
                user_id=task.user_id,
                input=task.input,
                mode=request.mode,
                country=request.country,
                category=request.category,
                max_keywords=request.max_keywords,
                exclude_keywords=request.exclude_keywords,
                force_collect=request.force_collect,
                shuffle=request.shuffle,
                cursor=request.cursor,
                persona=persona,
            )
        )

        # 읽기 전용: 추천은 아무것도 저장하지 않고 상태도 바꾸지 않는다.
        return TrendRecommendationResult(
            post_id=post_id,
            mode=trends.mode,
            trend_keywords=trends.trend_keywords,
            topic_candidates=[],
            generated_at=trends.collected_at,
            cache_status=trends.cache_status,
            refreshing=trends.refreshing,
            source=trends.source,
            # 소재 관련순 로테이션 상태. 화면이 '다른 후보 보기'로 다음 배치를 요청할 때
            # next_cursor를 그대로 돌려보낸다. 최신순에서는 전부 None이다.
            next_cursor=trends.next_cursor,
            pool_size=trends.pool_size,
            has_more=trends.has_more,
            cycled=trends.cycled,
        )

    # ------------------------------------------------------------- 선행 수집

    #: 입력을 저장하는 순간 미리 모아 둘 모드와, 그때 쓰는 개수.
    #:
    #: **화면이 보내는 것과 같은 값이어야 한다**(web의 DEFAULT_TREND_MODE·
    #: TREND_KEYWORD_COUNT). 다르면 선행분이 다른 요청이 되어 화면의 요청이 처음부터
    #: 다시 돈다 — 미리 모은 값이 통째로 헛돌 뿐 아니라 비용이 두 배가 된다.
    PREFETCH_MODES = (TrendMode.TRENDING, TrendMode.MATERIAL_RELATED)
    PREFETCH_MAX_KEYWORDS = 16

    @staticmethod
    def _request_signature(request) -> tuple:
        """두 요청이 **같은 수집인가**를 가르는 값들.

        '다른 후보 보기'(cursor·shuffle)와 강제 수집은 일부러 다시 도는 것이므로 여기에
        들어간다 — 그 요청이 선행분에 붙으면 사용자는 같은 목록을 다시 보게 된다.
        """
        return (
            request.mode.value,
            request.max_keywords,
            request.country,
            request.category,
            tuple(request.exclude_keywords or []),
            bool(request.force_collect),
            bool(request.shuffle),
            request.cursor,
        )

    def _matching_prefetch(self, post_id: str, request):
        """이 요청과 같은 선행 수집이 돌고 있으면 그 태스크. 없으면 None."""
        entry = self._prefetch.get((post_id, request.mode.value))
        if entry is None:
            return None
        job, signature = entry
        if signature != self._request_signature(request):
            return None
        return job

    def start_keyword_prefetch(self, task: BlogTask) -> None:
        """입력을 저장한 직후 키워드를 미리 모아 둔다(2026-08-07 사용자 요청).

        예전에는 제목 단계 화면이 뜬 **뒤에야** 수집이 시작됐다. 그런데 그때 필요한 것은
        소재·목적·참고자료뿐이고, 그것은 사용자가 '다음'을 누른 순간 이미 서버에 다 있다.
        기다릴 이유가 없다.

        **입력을 감지하지 않는다.** '다음'을 누른 것이 곧 "입력을 다 했다"는 선언이다.
        타이핑 도중에 추측으로 돌리면 소재를 고칠 때마다 수집이 다시 돌고, 소재 관련순은
        LLM 관련도 판정을 쓰므로 그만큼 그대로 비용이다.

        두 모드를 모두 모은다. 화면이 처음 여는 탭은 최신순(TRENDING)이고 — 이쪽은 판정
        모델을 부르지 않아 싸다 — 사용자가 눌러서 보는 것이 소재 관련순이다.

        실패는 삼킨다. 선행 수집은 가속 장치일 뿐이라, 실패해도 화면의 요청이 어차피
        다시 모은다.
        """
        if task.status not in TITLE_EDITABLE_STATUSES or task.selected_intent is not None:
            # 제목을 고칠 수 있는 상태가 아니면 이 수집은 쓰이지 않는다.
            return
        for mode in self.PREFETCH_MODES:
            key = (task.post_id, mode.value)
            entry = self._prefetch.get(key)
            if entry is not None and not entry[0].done():
                continue
            request = validate_trend_recommendation_request(self._prefetch_body(mode))
            signature = self._request_signature(request)
            job = self._jobs.start(
                self._collect_recommendation(task.post_id, request),
                on_error=lambda error, mode=mode, post_id=task.post_id: logger.info(
                    "키워드 선행 수집 실패(무시) | %s %s - %s", short(post_id), mode.value, error
                ),
            )
            self._prefetch[key] = (job, signature)

    def _prefetch_body(self, mode: TrendMode) -> dict:
        """선행 수집이 보내는 요청 몸통. 화면이 처음 보내는 것과 **같아야 한다.**"""
        return {
            "mode": mode.value,
            "maxKeywords": self.PREFETCH_MAX_KEYWORDS,
            "excludeKeywords": [],
            "forceCollect": False,
            "shuffle": False,
        }

    # -------------------------------------------------- 소재 풀 조기 데우기

    #: 화면 검증과 별개로 두는 상한. 인증만 통과하면 아무 문자열이나 보낼 수 있는 자리다.
    WARMUP_TOPIC_MAX_CHARS = 200
    WARMUP_PURPOSE_MAX_ITEMS = 5

    def start_material_pool_warmup(self, user_id: str, raw_body: Any) -> bool:
        """소재 입력이 끝난 낌새에 소재 관련 키워드 풀을 미리 데운다(2026-08-10 사용자 요청).

        start_keyword_prefetch(2026-08-07)가 '다음'(입력 저장)을 신호로 삼는 것보다 한 발
        빠르다: 화면이 "사용자가 소재 칸을 떠나 글 목적·대상 연령·참고 자료를 만지기
        시작했다"고 알려 오는 순간 소재는 사실상 확정이고, 남은 입력 시간이 곧 수집과
        관련도 판정이 돌 시간이 된다. 2026-08-07의 "입력을 감지하지 않는다"가 막으려던
        것은 타이핑 도중의 추측 실행이다 — 이 경로는 소재당 1회만 돈다(화면이 같은 소재를
        다시 보내지 않고, 서버도 진행 중이면 무시하며, 끝난 뒤의 재호출은 저장 풀이
        흡수해 수집 없이 끝난다).

        글(post)이 아직 없어도 된다 — 소재 풀은 material_key(소재) 단위로 저장되므로
        뒤에 만들어질 어느 글이든 같은 소재면 그대로 재사용한다. 글 목적이 아직 비어
        있으면 목적 축 없이 판정한다: 적격 게이트는 소재 축(subject_relevance)이라 판정은
        같고, 정렬이 조금 덜 맞춤일 뿐이다. 실패는 삼킨다 — 가속 장치일 뿐, 트렌드
        화면의 요청이 어차피 다시 모은다.
        """
        body = raw_body if isinstance(raw_body, dict) else {}
        topic = str(body.get("topic") or "").strip()
        if not topic or len(topic) > self.WARMUP_TOPIC_MAX_CHARS:
            return False
        key = material_key(topic)
        if not key:
            return False
        running = self._warmups.get(key)
        if running is not None and not running.done():
            return True

        purposes = [
            str(item).strip()
            for item in (body.get("purpose") or [])
            if isinstance(item, str) and str(item).strip()
        ][: self.WARMUP_PURPOSE_MAX_ITEMS]
        try:
            blog_input = BlogTaskInput(topic=topic, purpose=purposes or None, keywords=[])
        except Exception:  # noqa: BLE001 - 검증에 걸리는 몸통은 데우기를 포기할 이유일 뿐이다
            return False

        job = self._jobs.start(
            self._warm_material_pool(user_id, key, blog_input),
            on_error=lambda error: logger.info(
                "소재 풀 조기 데우기 실패(무시) | %s - %s", topic[:20], error
            ),
        )
        self._warmups[key] = job
        return True

    async def _warm_material_pool(
        self, user_id: str, key: str, blog_input: BlogTaskInput
    ) -> None:
        settings = await self._load_settings(user_id)
        persona = settings.default_persona if settings else None
        await self._trend_provider.fetch_trends(
            TrendFetchInput(
                # 합성 id — 노출 이력 키(user:post:mode)가 실제 글 화면과 겹치지 않는다.
                post_id=f"warmup_{key[:24]}",
                user_id=user_id,
                input=blog_input,
                mode=TrendMode.MATERIAL_RELATED,
                max_keywords=self.PREFETCH_MAX_KEYWORDS,
                persona=persona,
            )
        )
        logger.info("소재 풀 조기 데우기 완료 | %s", blog_input.topic[:20])

    async def generate_topics(self, post_id: str, raw_body: Any) -> TrendTopicResult:
        """키워드 하나에 대한 제목 목록을 다시 쓴다 — 키워드 클릭, 또는 제목 추천.

        recommend_topics와 달리 여기서는 모델 실패가 드러나게 둔다: 사용자가 제목만이
        일인 버튼을 눌렀으니, 조용히 템플릿을 돌려주는 건 무슨 일이 있었는지 속이는 것이다.
        """
        task = await self._require_trend_ready_task(post_id)
        request = validate_topic_generation_request(raw_body)

        keyword = TrendKeyword(
            trend_keyword_id=request.trend_keyword_id,
            keyword=request.keyword,
            source=request.source,
            rank=1,
            score=100,
            collected_at=_now(),
        )
        candidates, generated_at = await self._write_titles(
            task,
            post_id,
            keyword,
            request.exclude_titles,
            request.exclude_angles,
            request.regeneration_count
        )

        return TrendTopicResult(
            post_id=post_id,
            trend_keyword_id=keyword.trend_keyword_id,
            topic_candidates=candidates,
            generated_at=generated_at,
        )

    async def select_topic(self, post_id: str, raw_body: Any) -> BlogTask:
        """제목을 확정한다. 이미 고른 제목이 있으면 그것을 갈아치운다.

        갈아치울 때 옛 제목으로 만든 검증 결과는 저장소가 함께 버린다
        (save_trend_selection) — 새 제목의 검증은 M3이 다시 돌려야 한다.
        """
        task = await self._require_trend_ready_task(post_id)
        request = validate_select_trend_topic_request(raw_body, task.input.topic)

        return await self._repository.save_trend_selection(
            post_id,
            TrendSelection(
                topic_candidate_id=request.topic_candidate_id,
                final_topic=request.final_topic,
                selected_trend_keyword_ids=request.selected_trend_keyword_ids,
                selected_keywords=request.selected_keywords,
                skipped=request.skipped,
                selected_at=_now(),
                hook_type=request.hook_type,
            ),
            TREND_SELECTION_ACTOR,
        )
