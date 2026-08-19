"""조립 지점 — 리포지토리·서비스·LLM provider를 여기서 한 번에 엮는다."""

import asyncio
import logging
import re
from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import (
    allow_in_memory_storage,
    llm_config_from_env,
    mongodb_uri,
    redis_url,
    seed_demo_account,
    trend_exposure_max_entries,
    trend_exposure_ttl_seconds,
)
from app.db.mongo import connect_mongo
from app.db.redis import close_redis, create_redis, ping
from app.errors import AuthError
from app.llm import LlmConfig, LlmProviders, RoleStatus, create_llm_providers
from app.llm.trends import (
    MongoMaterialKeywordStore,
    MongoPoolCache,
)
from app.llm.trends.exposure import (
    InMemoryTrendExposureStore,
    RedisTrendExposureStore,
)
from app.modules.auth.email_crypto import email_cipher_from_env
from app.modules.auth.repository import InMemoryUserRepository, MongoUserRepository
from app.modules.auth.service import AuthService
from app.modules.blog_task.locks import FileJobLease, JobLease, RedisJobLease
from app.modules.blog_task.recovery import FRESH_SECONDS, recover_orphaned_tasks
from app.modules.blog_task.repository import (
    InMemoryBlogTaskRepository,
    MongoBlogTaskRepository,
)
from app.modules.blog_task.service import BlogTaskService
from app.modules.draft.service import DraftService
from app.modules.persona import (
    InMemoryPersonaRepository,
    MongoPersonaRepository,
    PersonaRepository,
    PersonaService,
)
from app.modules.scheduled_posting import (
    InMemoryScheduledPostingRepository,
    MongoScheduledPostingRepository,
    ScheduledPostingService,
    ScheduledPostingWorker,
)
from app.modules.trend.keyword_store import MongoStoredTrendKeywordRepository
from app.modules.trend.service import TrendService
from app.modules.user_settings.repository import (
    InMemoryUserSettingsRepository,
    MongoUserSettingsRepository,
)
from app.modules.brand import (
    BrandService,
    InMemoryBrandRepository,
    MongoBrandRepository,
)
from app.modules.user_settings.service import UserSettingsService
from app.posting import DefaultPostingWorker

logger = logging.getLogger(__name__)

DEMO_ACCOUNT = {"email": "demo@blog-it.dev", "password": "demo1234", "nickname": "데모 계정"}

# 유예했던 작업을 다시 훑기까지 기다리는 시간. 유예 자체보다 조금 더 준다 — 유예가 막
# 풀린 순간에 훑으면 경계에서 또 갈린다.
DEFERRED_SWEEP_SECONDS = FRESH_SECONDS + 60


@dataclass
class ApiServices:
    blog_task_service: BlogTaskService
    trend_service: TrendService
    draft_service: DraftService
    auth_service: AuthService
    user_settings_service: UserSettingsService
    brand_service: BrandService
    persona_service: PersonaService
    scheduled_posting_service: ScheduledPostingService
    scheduled_posting_worker: ScheduledPostingWorker
    llm_status: list[RoleStatus]
    # 로그로 남겨도 안전하다. 자격 증명을 담지 않는다.
    storage_status: str
    # 로그·헬스체크용. 자격 증명을 담지 않는다("Redis" / "메모리(Redis 미설정)" 같은 말만).
    shared_state_status: str = "메모리"
    mongo_client: AsyncIOMotorClient | None = None
    redis_client: object | None = None
    job_lease: JobLease | None = None
    blog_task_repository: object | None = None
    scheduled_posting_repository: object | None = None
    # 유예한 작업을 나중에 다시 훑는 잡. 들고 있지 않으면 GC가 가져간다.
    deferred_sweep: object | None = None


async def _seed_demo_account(auth_service: AuthService) -> None:
    try:
        await auth_service.sign_up(DEMO_ACCOUNT)
    except AuthError as error:
        if error.code != "EMAIL_ALREADY_EXISTS":
            raise
    # 닉네임 도입 전에 만들어진 데모 계정은 닉네임이 비어 있다 — 시작 때 메꿔 준다.
    await auth_service.backfill_nickname(DEMO_ACCOUNT["email"], DEMO_ACCOUNT["nickname"])


def _assemble(
    blog_task_repository,
    user_repository,
    user_settings_repository,
    persona_repository: PersonaRepository,
    llm: LlmProviders,
    storage_status: str,
    mongo_client: AsyncIOMotorClient | None = None,
    redis_client: object | None = None,
    shared_state_status: str = "메모리",
    scheduled_posting_repository=None,
    brand_repository=None,
    stored_trend_keywords=None,
) -> ApiServices:
    # 같은 작업을 두 프로세스가 동시에 돌리지 않게 하는 임차. Redis가 없으면 **파일**로
    # 막는다 — '프로세스가 하나'라는 가정은 --reload 재시작 겹침·서버 이중 실행에서
    # 깨지고, 그때 no-op은 예약 작업 두 개를 동시에 돌렸다(2026-08-04 실사용).
    job_lease: JobLease = (
        RedisJobLease(redis_client) if redis_client is not None else FileJobLease()
    )
    auth_service = AuthService(repository=user_repository)
    persona_service = PersonaService(persona_repository)
    user_settings_service = UserSettingsService(
        repository=user_settings_repository,
        persona_service=persona_service,
    )
    # 브랜드 자료. 저장소를 주지 않으면 메모리로 둔다 — Mongo 없이 뜨는 개발 실행에서도
    # 화면이 동작해야 한다(다른 저장소와 같은 규칙).
    brand_service = BrandService(brand_repository or InMemoryBrandRepository())

    # 예약 포스팅이 이 셋을 그대로 받아 써야 해서 먼저 만든다 — 예약은 별도의 생성기를
    # 두지 않고 새 글 작성과 **같은 인스턴스**를 부른다. 인스턴스를 따로 만들면 같은 글을
    # 두 곳에서 쓰는 것을 막아 주는 중복 방지 장치(_running_drafts)가 갈라진다.
    blog_task_service = BlogTaskService(
        repository=blog_task_repository,
        # 발행은 서버의 크롬에서 돈다. 사용자 PC 발행(에이전트)은 2026-08-18에 코드째
        # 제거했다 — 서버 유지 방향(docs/서버-발행-개선-계획.md) 확정에 따라.
        posting_worker=DefaultPostingWorker(),
        web_search_analyzer=llm.web_search_analyzer,
        job_lease=job_lease,
        # 스레드 발행 때 스레드 문법의 게시물을 새로 쓰는 생성기. 블로그 원고 생성기와
        # 같은 인스턴스지만 프롬프트·스키마는 분리돼 있다(llm/threads_prompts.py).
        threads_writer=llm.draft_generator,
        # 스레드를 몇 개로 나눌지는 글 길이 설정이 정한다.
        user_settings_service=user_settings_service,
    )
    trend_service = TrendService(
        repository=blog_task_repository,
        trend_provider=llm.trend_provider,
        topic_generator=llm.topic_generator,
        topic_evaluator=llm.topic_evaluator,
        user_settings_service=user_settings_service,
        persona_service=persona_service,
        # 이미 쌓인 키워드를 글 없이 읽는 통로(GET /trends/keywords). 주지 않으면 빈
        # 목록이라, Mongo 없이 뜨는 개발 실행에서도 화면이 죽지 않는다.
        stored_keywords=stored_trend_keywords,
    )
    draft_service = DraftService(
        repository=blog_task_repository,
        draft_generator=llm.draft_generator,
        post_image_generator=llm.post_image_generator,
        user_settings_service=user_settings_service,
        persona_service=persona_service,
        job_lease=job_lease,
        photo_search=llm.photo_search,
        youtube_photo_search=llm.youtube_photo_search,
        # 2차 품질 검수(2026-08-07). 원고를 쓴 모델과 다른 모델이 같은 원고를 한 번 더
        # 보고, 그림은 실제로 본다. 자격 증명이 없으면 None이고 그때는 1차만 돈다.
        final_reviewer=llm.final_reviewer,
    )

    scheduled_repository = scheduled_posting_repository or InMemoryScheduledPostingRepository()
    scheduled_posting_service = ScheduledPostingService(
        repository=scheduled_repository,
        blog_task_service=blog_task_service,
        trend_service=trend_service,
        draft_service=draft_service,
        # 브랜드를 건 배치의 글에 그 자료를 얹기 위한 것(2026-08-19). 위에서 만든
        # 인스턴스를 그대로 넘긴다 — 여기서 다시 만들면 저장소가 두 벌이 된다.
        brand_service=brand_service,
    )
    scheduled_posting_worker = ScheduledPostingWorker(
        service=scheduled_posting_service,
        repository=scheduled_repository,
        job_lease=job_lease,
    )

    return ApiServices(
        blog_task_repository=blog_task_repository,
        # 위에서 만든 인스턴스를 그대로 넘긴다. 예약 포스팅이 같은 것을 받아야 하기
        # 때문이다 — 여기서 다시 만들면 두 벌이 되어 중복 방지 장치가 갈라진다.
        blog_task_service=blog_task_service,
        trend_service=trend_service,
        draft_service=draft_service,
        auth_service=auth_service,
        user_settings_service=user_settings_service,
        brand_service=brand_service,
        persona_service=persona_service,
        scheduled_posting_service=scheduled_posting_service,
        scheduled_posting_worker=scheduled_posting_worker,
        scheduled_posting_repository=scheduled_repository,
        llm_status=llm.status,
        storage_status=storage_status,
        shared_state_status=shared_state_status,
        mongo_client=mongo_client,
        redis_client=redis_client,
        job_lease=job_lease,
    )


async def create_runtime_services(llm_config: LlmConfig | None = None) -> ApiServices:
    """Mongo를 기본 저장소로 사용하고, 개발 환경에서만 명시적으로 메모리 폴백한다.

    LLM provider는 Mongo 연결을 시도하기 전에 만든다: LlmConfigError는 설정 실수이며
    그 자체로 드러나야 하고, 원본처럼 삼켜져 "MongoDB unavailable"로 보고되어서는
    안 된다.
    """
    config = llm_config or llm_config_from_env()
    llm = create_llm_providers(config)

    # 프로세스 바깥에 두어야 하는 상태(노출 이력·멱등성 키)의 저장처. 없으면 예전처럼
    # 메모리에 두고 계속 간다 — Redis를 띄우지 않은 로컬에서도 서버는 떠야 한다.
    redis_client, shared_state_status = await _connect_shared_state()

    uri = mongodb_uri()
    try:
        client, db = await connect_mongo(uri)
    except Exception as error:
        if not allow_in_memory_storage():
            raise RuntimeError(f"MongoDB storage unavailable: {error}") from error
        print(f"MongoDB storage unavailable, using in-memory storage: {error}", flush=True)
        services = _assemble(
            InMemoryBlogTaskRepository(),
            InMemoryUserRepository(),
            InMemoryUserSettingsRepository(),
            InMemoryPersonaRepository(),
            llm,
            "in-memory",
            redis_client=redis_client,
            shared_state_status=shared_state_status,
        )
        _use_exposure_store(llm, redis_client)
        # 데모 계정은 명시적으로 켰을 때만 심는다(SEED_DEMO_ACCOUNT) — 알려진 자격
        # 증명이 운영 DB에 자동 생성되면 안 된다.
        if seed_demo_account():
            await _seed_demo_account(services.auth_service)
        return services

    # 중단된 작업 복구는 여기서 일괄 되감지 않는다 — 임차(lease)를 확인하는
    # recover_interrupted_jobs(main.py lifespan)가 맡는다. 무조건 되감으면 다른 프로세스가
    # 지금 돌리고 있는 작업까지 실패로 만든다.

    # 트렌드 수집분을 Mongo에 영속 저장·누적한다. 캐시는 LLM 프로바이더 생성 시(Mongo 연결
    # 전) 디스크/Redis로 만들어지므로, 연결이 되면 여기서 Mongo 캐시로 바꿔 재수집·재채점
    # 비용을 줄이고 후보 풀이 시간이 갈수록 많아지게 한다.
    trend_provider = llm.trend_provider
    if trend_provider is not None and hasattr(trend_provider, "use_pool_cache"):
        trend_provider.use_pool_cache(MongoPoolCache(db))
    # 소재별 관련 키워드 풀도 Mongo에 영속한다. 메모리에 두면 서버를 껐다 켤 때마다 같은
    # 소재를 다시 수집·채점하게 되고, 소재 단위 재사용이라는 설계가 성립하지 않는다.
    if trend_provider is not None and hasattr(trend_provider, "use_material_store"):
        trend_provider.use_material_store(MongoMaterialKeywordStore(db))

    # 노출 이력을 프로세스 바깥으로 옮긴다. 메모리에 두면 재시작마다 초기화되고 API 서버를
    # 늘리는 순간 서버마다 다른 이력을 보게 된다.
    _use_exposure_store(llm, redis_client)

    # 옛 형식(blob·복합 id, _id가 "trend:"로 시작) 트렌드 키워드 문서를 정리한다.
    # 현재 형식은 키워드당 문서 하나라 이 정리에 걸리지 않으므로 매 시작 실행해도 안전하다(멱등).
    await db["trend_keywords"].delete_many({"_id": {"$regex": "^trend:"}})
    # poolKey는 source로 대체돼 더는 저장하지 않는다 — 남아 있는 문서에서 걷어낸다(멱등).
    await db["trend_keywords"].update_many({}, {"$unset": {"poolKey": ""}})
    # 근거(검색량·문서 수·조회수)가 없는 옛 수집분을 지운다.
    #
    # 최신순 카드는 출처별 지표 세 줄로 "왜 이 키워드인지"를 스스로 설명한다. 근거를 싣기
    # 전에 수집된 문서는 그 자리를 "상세 지표는 새 수집 후 표시됩니다"로 채우는데, 이것들이
    # 풀에 섞여 있어 '다른 키워드 보기'·'새 키워드 찾기'를 누를 때마다 화면 대부분이 그
    # 카드였다. 화면에 낼 수 없는 후보를 계속 보관할 이유가 없다 — 지운 자리는 다음 수집이
    # 근거와 함께 다시 채운다(수집분은 누적된다). 남은 문서는 전부 evidence를 지녀 다시
    # 걸리지 않는다(멱등).
    await db["trend_keywords"].delete_many({"evidence": {"$exists": False}})
    # id 방식 정리: 문서 id는 key1, key2, … 순번이고 발급용 숫자 seq를 함께 둔다(이유는
    # 변경내역 참조 — 문자열 _id는 사전순이라 "key9">"key10"이어서 seq 없이는 다음 번호를
    # 못 뽑는다). 다른 형식(kw_해시, seq 누락·불일치)이 하나라도 있으면 전체를 순번으로
    # 다시 매긴다. 문서 수가 소스당 200 상한이라 가볍고, 정리 후에는 전부 형식이 맞아
    # 다시 걸리지 않는다(멱등).
    def _well_formed(doc: dict) -> bool:
        match = re.fullmatch(r"key(\d+)", str(doc.get("_id", "")))
        seq = doc.get("seq")
        return bool(match) and isinstance(seq, int) and seq == int(match.group(1))

    keyword_docs = await db["trend_keywords"].find({}).to_list(length=None)
    if keyword_docs and not all(_well_formed(doc) for doc in keyword_docs):
        keyword_docs.sort(key=lambda d: (float(d.get("at", 0.0)), str(d.get("keyword", ""))))
        await db["trend_keywords"].delete_many({})
        for index, doc in enumerate(keyword_docs, start=1):
            # 문서를 새로 쓰되 필드를 골라 담지 않는다. 예전에는 keyword·source·at·score만
            # 옮겨 적어서, id 형식이 하나만 어긋나도 이 정리가 **모든 문서의 근거를 지웠다** —
            # 화면이 통째로 "상세 지표는 새 수집 후 표시됩니다"가 되는 경로다.
            await db["trend_keywords"].insert_one(
                {**doc, "_id": f"key{index}", "seq": index}
            )
    # 잠깐 쓰였다 폐기된 부속 컬렉션. 순번은 문서의 seq 최댓값으로 발급하고, 관련도
    # 캐시는 디스크에 두므로 DB에는 trend_keywords 하나만 남긴다.
    await db.drop_collection("counters")
    await db.drop_collection("trend_relevance")
    # 점수 방식 단일화 마이그레이션: 예전에 소스 원시 값(구글 검색량 50000, 유튜브 언급 7.08)
    # 그대로 저장된 문서를 소스 내 40~100 상대 인기로 한 번 정규화한다. 정규화 후에는
    # 값이 40~100 안에 있어 다시 걸리지 않는다(멱등). 새 수집분은 저장 전에 정규화된다.
    for source in await db["trend_keywords"].distinct("source"):
        docs = await db["trend_keywords"].find(
            {"source": source}, {"score": 1}
        ).to_list(length=None)
        scores = [float(d.get("score", 0.0)) for d in docs]
        if not scores:
            continue
        low, high = min(scores), max(scores)
        if 40.0 <= low and high <= 100.0:
            continue
        span = high - low
        for doc in docs:
            raw = float(doc.get("score", 0.0))
            rescored = 40.0 + 60.0 * (raw - low) / span if span > 0 else 100.0
            await db["trend_keywords"].update_one(
                {"_id": doc["_id"]}, {"$set": {"score": round(rescored, 1)}}
            )

    # 기본 페르소나(프리셋)를 persona 컬렉션에 upsert한다. 문구를 고쳐도 여기 한 번이면
    # 되고, 사용자 문서는 id만 들고 있어 일괄 수정이 필요 없다.
    persona_repository = MongoPersonaRepository(db)
    await persona_repository.seed_defaults()

    # Mongo에 저장할 때만 이메일 암호화가 필요하다. 키가 없으면 여기서 멈춘다 — Mongo
    # 연결에는 성공한 뒤이므로 "MongoDB unavailable"로 잘못 보고되지 않고, 빠진 이메일
    # 키를 원인으로 알린다.
    services = _assemble(
        MongoBlogTaskRepository(db),
        MongoUserRepository(db, email_cipher_from_env()),
        MongoUserSettingsRepository(db),
        persona_repository,
        llm,
        f"mongodb:{db.name}",
        mongo_client=client,
        redis_client=redis_client,
        shared_state_status=shared_state_status,
        scheduled_posting_repository=MongoScheduledPostingRepository(db),
        brand_repository=MongoBrandRepository(db),
        # 수집분이 쌓이는 그 컬렉션(trend_keywords)을 읽기만 한다. 쓰는 쪽은
        # MongoPoolCache 그대로다.
        stored_trend_keywords=MongoStoredTrendKeywordRepository(db),
    )
    if seed_demo_account():
        await _seed_demo_account(services.auth_service)
    return services


async def _connect_shared_state() -> tuple[object | None, str]:
    """노출 이력·멱등성 키를 담을 Redis. 없거나 죽어 있으면 (None, 사유)."""
    url = redis_url()
    if not url:
        return None, "메모리(REDIS_URL 미설정)"

    client = create_redis(url)
    if client is None:
        return None, "메모리(redis 패키지 없음)"
    if not await ping(client):
        # 연결 실패는 치명적이지 않다 — 메모리로 계속 간다. 다만 그 사실이 로그와
        # 헬스체크에 드러나야 한다. 접속 정보는 남기지 않는다.
        await close_redis(client)
        return None, "메모리(Redis 연결 실패)"
    return client, "Redis"


def _use_exposure_store(llm, redis_client) -> None:
    """트렌드 프로바이더의 노출 이력 저장소를 정한다. Redis가 없으면 메모리 그대로."""
    provider = llm.trend_provider
    if provider is None or not hasattr(provider, "use_exposure_store"):
        return
    ttl = trend_exposure_ttl_seconds()
    cap = trend_exposure_max_entries()
    provider.use_exposure_store(
        RedisTrendExposureStore(redis_client, ttl, cap)
        if redis_client is not None
        else InMemoryTrendExposureStore(ttl, cap)
    )


async def shutdown_services(services: ApiServices) -> None:
    """종료 시 외부 연결을 정리한다. 하나가 실패해도 나머지 정리를 막지 않는다."""
    # 잠들어 있는 재훑기 잡을 남겨 두면 종료 때 '취소되지 않은 태스크' 경고가 난다.
    sweep = services.deferred_sweep
    if sweep is not None and not sweep.done():
        sweep.cancel()
    if services.mongo_client is not None:
        services.mongo_client.close()
    await close_redis(services.redis_client)


async def recover_interrupted_jobs(services: ApiServices) -> int:
    """시작 시 한 번, 진행 중인 채로 멈춰 있던 글을 되살린다. 되살린 글의 수.

    살아 있는 임차가 없는 것만 손댄다 — 서버를 여러 대로 돌릴 때 한 대가 재시작한다고
    다른 대가 지금 돌리고 있는 작업을 실패로 만들면 안 된다.

    방금 시작한 작업은 유예한다(recovery.FRESH_SECONDS). 그런데 이 스위퍼는 시작할 때만
    도므로, 유예한 것이 정말 죽은 작업이었다면 아무도 되살려 주지 않는다 — 그래서 유예가
    지난 뒤 한 번 더 훑는 잡을 띄운다.
    """
    if services.blog_task_repository is None or services.job_lease is None:
        return 0
    sweep = await _sweep_once(services)
    if sweep.deferred:
        services.deferred_sweep = asyncio.create_task(_sweep_after_grace(services))
    return sweep.recovered


async def _sweep_once(services: ApiServices):
    from app.modules.blog_task.service import _failed_validation_result

    return await recover_orphaned_tasks(
        services.blog_task_repository, services.job_lease, _failed_validation_result
    )


async def _sweep_after_grace(services: ApiServices) -> None:
    """유예했던 작업을 다시 본다. 그때도 임차가 없고 유예도 지났으면 죽은 작업이다."""
    try:
        await asyncio.sleep(DEFERRED_SWEEP_SECONDS)
        sweep = await _sweep_once(services)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # 이 잡이 실패해도 서버는 계속 돈다. 다음 재시작이 같은 일을 한다.
        logger.warning("작업 복구(유예분) 실패: %s", error)
        return
    if sweep.recovered:
        logger.info("작업 복구: 유예했던 글 %d건을 되살렸습니다.", sweep.recovered)


async def recover_scheduled_posting(services: ApiServices) -> int:
    """예약 배치를 재시작 뒤에도 이어서 돌 수 있게 정리한다.

    저장된 단계 표시가 아니라 연결된 BlogTask의 실제 상태를 보고 판단한다. 결과가
    불확실한 네이버 발행은 자동으로 다시 하지 않는다 — 같은 글이 두 번 올라간다.
    """
    from app.modules.scheduled_posting import recover_active_batches

    if services.scheduled_posting_repository is None:
        return 0
    return await recover_active_batches(
        services.scheduled_posting_repository, services.blog_task_service
    )
