"""예약 포스팅 오케스트레이션(ScheduledPostingService) 테스트.

이 서비스는 **글을 쓰지 않는다** — 기존 서비스들을 순서대로 부를 뿐이다. 그래서 여기서
확인하는 것도 '어느 것을 언제 어떤 인자로 불렀는가'와 '그 결과를 어떻게 읽었는가' 둘이다.

실제 LLM·Mongo·셀레니움은 한 번도 부르지 않는다. 저장소는 InMemory 구현이고, 기존
서비스 세 개(blog_task/trend/draft)는 호출을 기록만 하는 가짜다. 네이버 저장 정보 확인
(``_naver_saved``)은 로컬 파일을 읽으므로 테스트마다 monkeypatch로 갈아 끼운다.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.errors import BlogTaskError
from app.modules.scheduled_posting import service as service_module
from app.modules.scheduled_posting.models import (
    ACTIVE_BATCH_STATUSES,
    ScheduledBatch,
    ScheduledBatchStatus,
    ScheduledJob,
    ScheduledJobStage,
    ScheduledJobStatus,
    SchedulePlatform,
    ScheduleTopicMode,
)
from app.modules.scheduled_posting.repository import InMemoryScheduledPostingRepository
from app.modules.scheduled_posting.service import (
    ScheduledPostingService,
    pick_intent,
    pick_title,
    pick_trend_keyword,
)
from app.modules.scheduled_posting.validation import SCHEDULED_DEFAULT_PURPOSE
from app.shared import (
    BlogTask,
    BlogTaskInput,
    BlogTaskStatus,
    FinalPost,
    IntentCandidate,
    IntentValidationResult,
    PostingChannel,
    PostingLog,
    PostingMethod,
    PostingResultStatus,
    SearchSource,
    SelectedIntent,
    TopicCandidate,
    TrendKeyword,
    TrendRecommendationResult,
    TrendSelection,
    TrendSource,
    TrendTopicResult,
)
from app.shared.format import now_iso

#: 파이프라인이 부르는 기존 서비스 메서드들. 순서 검증은 이 목록으로 거른 기록만 본다
#: (get_blog_task 같은 조회는 중간중간 섞여 들어오므로 순서의 뜻을 흐린다).
PIPELINE_METHODS = [
    "create_blog_task",
    "recommend_topics",
    "generate_topics",
    "select_topic",
    "analyze_intent_candidates",
    "select_intent",
    "generate_draft",
    "publish_blog_task",
]


# --------------------------------------------------------------------- 만들기 도구


def make_task(
    post_id: str = "post_1",
    user_id: str = "user_1",
    status: BlogTaskStatus = BlogTaskStatus.REFERENCE_PROCESSING,
    **overrides,
) -> BlogTask:
    """가짜 세계가 들고 다닐 BlogTask 하나."""
    now = now_iso()
    fields = {
        "post_id": post_id,
        "user_id": user_id,
        "status": status,
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "status_history": [],
        "input": BlogTaskInput(topic="소재", keywords=[]),
        "posting_logs": [],
    }
    fields.update(overrides)
    return BlogTask(**fields)


def make_trend_keyword(keyword: str = "키워드", **overrides) -> TrendKeyword:
    fields = {
        "trend_keyword_id": f"trend_{keyword}",
        "keyword": keyword,
        "source": TrendSource.NAVER_DATALAB,
        "rank": 1,
        "score": 80.0,
        "collected_at": now_iso(),
    }
    fields.update(overrides)
    return TrendKeyword(**fields)


def make_topic_candidate(
    candidate_id: str = "cand_1", title: str = "자동 선택된 제목", **overrides
) -> TopicCandidate:
    fields = {
        "topic_candidate_id": candidate_id,
        "title": title,
        "description": "설명",
        "trend_keyword_ids": ["trend_키워드"],
        "recommended": False,
    }
    fields.update(overrides)
    return TopicCandidate(**fields)


def make_intent_candidate(intent_id: str, relevance: list[int]) -> IntentCandidate:
    """relevanceScore 목록으로 자료를 붙인 의도 후보."""
    return IntentCandidate(
        intent_id=intent_id,
        title=f"의도 {intent_id}",
        target_reader="독자",
        rationale="근거",
        keywords=["k"],
        sources=[
            SearchSource(
                title=f"자료 {index}",
                url=f"https://example.com/{intent_id}/{index}",
                snippet="요약",
                relevance_score=score,
            )
            for index, score in enumerate(relevance)
        ],
    )


def make_posting_log(
    result: PostingResultStatus = PostingResultStatus.SUCCESS,
    method: PostingMethod = PostingMethod.AUTO,
    channel: PostingChannel = PostingChannel.NAVER,
    post_url: str | None = "https://blog.naver.com/u/1",
    error_message: str | None = None,
) -> PostingLog:
    return PostingLog(
        log_id="log_1",
        post_id="post_1",
        user_id="user_1",
        method=method,
        channel=channel,
        result=result,
        post_url=post_url,
        error_message=error_message,
        created_at=now_iso(),
    )


# ----------------------------------------------------------------- 가짜 기존 서비스


class FakeWorld:
    """가짜 서비스 셋이 함께 들여다보는 글 저장소이자 호출 기록장.

    **복사하지 않고 같은 객체를 돌려준다.** 원고가 만든 FinalPost가 발행기까지 손대지
    않은 채 가는지(is 비교)를 확인하려면 그래야 한다.
    """

    def __init__(self) -> None:
        self.tasks: dict[str, BlogTask] = {}
        #: (메서드명, 인자들)
        self.calls: list[tuple[str, tuple]] = []
        #: 원고 단계가 만든 FinalPost와 발행기가 실제로 받은 FinalPost.
        self.made_final_post: FinalPost | None = None
        self.published_final_post: FinalPost | None = None

        # 단계별 손잡이 — 테스트가 상황을 바꿔 끼우는 자리다.
        self.trend_keywords: list[TrendKeyword] = [make_trend_keyword("한강 러닝")]
        self.topic_candidates: list[TopicCandidate] = [make_topic_candidate()]
        #: ok | failed_status | ready_without_final_post
        self.draft_outcome = "ok"
        self.publish_result = PostingResultStatus.SUCCESS
        #: 스레드 발행만 다른 결과를 내야 할 때 끼우는 손잡이. None이면 publish_result다.
        self.threads_publish_result: PostingResultStatus | None = None
        self.publish_error_message: str | None = None
        #: generate_draft 직전에 부르는 훅(예: 도중에 정지 버튼을 누른 상황).
        self.on_generate_draft = None

    # ------------------------------------------------------------------ 기록

    def record(self, name: str, *args) -> None:
        self.calls.append((name, args))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    @property
    def pipeline_names(self) -> list[str]:
        return [name for name in self.names if name in PIPELINE_METHODS]

    def count(self, name: str) -> int:
        return sum(1 for recorded, _ in self.calls if recorded == name)

    def args_of(self, name: str) -> tuple:
        """그 메서드가 **처음** 받은 인자들."""
        for recorded, args in self.calls:
            if recorded == name:
                return args
        raise AssertionError(f"{name} 호출 기록이 없습니다: {self.names}")

    # ------------------------------------------------------------------ 글 보관

    def put(self, task: BlogTask) -> BlogTask:
        self.tasks[task.post_id] = task
        return task

    def patch(self, post_id: str, **updates) -> BlogTask:
        """저장된 글을 갱신한다. model_copy는 얕은 복사라 FinalPost 객체는 그대로 간다."""
        return self.put(self.tasks[post_id].model_copy(update=updates))


class FakeBlogTaskService:
    def __init__(self, world: FakeWorld):
        self._world = world

    async def create_blog_task(self, body, max_reference_materials=None):
        # 상한은 **브랜드를 건 작업에서만** 온다(2026-08-19). 진짜 서비스와 같은 모양이라
        # 여기서도 받아 둔다 — 받지 않으면 브랜드를 건 예약이 TypeError로 실패한다.
        self._world.record("create_blog_task", body)
        post_id = f"post_{len(self._world.tasks) + 1}"
        return self._world.put(
            make_task(
                post_id=post_id,
                user_id=body.get("userId", "user_1"),
                status=BlogTaskStatus.REFERENCE_PROCESSING,
                input=BlogTaskInput(
                    topic=body.get("topic", ""),
                    purpose=body.get("purpose"),
                    keywords=[],
                ),
            )
        )

    async def get_user_blog_task(self, user_id, post_id):
        """예약으로 넘길 글을 주인 확인과 함께 읽는다."""
        self._world.record("get_user_blog_task", (user_id, post_id))
        task = self._world.tasks.get(post_id)
        return task if task is not None and task.user_id == user_id else None

    async def clone_for_direction(self, post_id, intent_id, *, final_topic=None, intent=None):
        """방향만 다른 글 하나를 더 만든다(2026-08-12). 원본을 베끼고 방향을 갈아 끼운다.

        ``intent``는 화면이 보낸 **고른 방향 전체**다(2026-08-12). 진짜 구현은 그것을
        복제본의 후보로 심는다 — 여기서는 넘어왔다는 사실만 기록한다.
        """
        self._world.record("clone_for_direction", (post_id, intent_id, intent))
        origin = self._world.tasks[post_id]
        clone_id = f"{post_id}_clone_{intent_id}"
        return self._world.put(
            origin.model_copy(update={"post_id": clone_id, "final_post": None})
        )

    async def get_blog_task(self, post_id):
        self._world.record("get_blog_task", post_id)
        return self._world.tasks.get(post_id)

    async def refresh_selected_intent_sources(self, post_id):
        """원고를 만들 차례에 자료만 새로 모은다 — **시각을 정한 작업만** 지난다
        (2026-08-13). 여기서는 불렸다는 사실만 기록한다."""
        self._world.record("refresh_selected_intent_sources", post_id)
        return self._world.tasks.get(post_id)

    async def get_post_summaries(self, post_ids):
        """예약 목록이 **글의 실제 상태**까지 한 번에 물어본다(2026-08-06).

        제목만 받던 것을 대신한다 — 작업이 실패로 끝나도 그 글은 완성되거나 발행돼
        있을 수 있어서, 화면이 사실대로 말하려면 글 쪽 상태가 필요하다.
        """
        from app.shared import PostSummary

        # '작업 현황' 줄은 DB가 아니라 프로세스 메모리에서 붙인다 — 진짜 구현과 같은
        # 자리다(BlogTaskService.get_post_summaries). 이것을 빠뜨리면 예약 목록이 촘촘한
        # 진행 줄을 싣는지 여기서 확인할 수 없다(2026-08-12).
        from app.modules.blog_task.jobs import activity_log_for

        self._world.record("get_post_summaries", list(post_ids))
        found = {}
        for post_id in post_ids:
            task = self._world.tasks.get(post_id)
            if task is None:
                continue
            title = (task.final_post.title if task.final_post else "") or ""
            published = None
            for log in task.posting_logs or []:
                if log.post_url and log.result == PostingResultStatus.SUCCESS:
                    published = log.post_url
            found[post_id] = PostSummary(
                post_id=post_id,
                status=task.status,
                title=title.strip() or None,
                published_url=published,
                progress=task.progress,
                activity_log=activity_log_for(post_id),
            )
        return found

    async def delete_user_blog_task(self, user_id, post_id):
        self._world.record("delete_user_blog_task", user_id, post_id)
        self._world.tasks.pop(post_id, None)

    async def analyze_intent_candidates(self, post_id):
        self._world.record("analyze_intent_candidates", post_id)
        return self._world.patch(
            post_id,
            status=BlogTaskStatus.SEARCH_ANALYZING,
            intent_validation_result=IntentValidationResult(
                prompt_version="m3-intent@v1.0",
                provider="mock",
                model="mock-analyzer",
                analyzed_at=now_iso(),
                intent_candidates=[
                    make_intent_candidate("intent_1", [10]),
                    make_intent_candidate("intent_2", [40, 40]),
                ],
            ),
        )

    async def select_intent(self, post_id, body):
        self._world.record("select_intent", post_id, body)
        return self._world.patch(
            post_id,
            status=BlogTaskStatus.INTENT_SELECTED,
            selected_intent=SelectedIntent(
                intent_id=body["intentId"],
                title="선택된 의도",
                target_reader="독자",
                rationale="근거",
            ),
        )

    async def publish_blog_task(self, post_id, body):
        self._world.record("publish_blog_task", post_id, body)
        task = self._world.tasks[post_id]
        # 발행기가 실제로 받은 원고. 원고 단계가 만든 그 객체여야 한다.
        self._world.published_final_post = task.final_post
        channel = PostingChannel(body.get("channel", "naver"))
        result = self._world.publish_result
        if channel == PostingChannel.THREADS and self._world.threads_publish_result is not None:
            result = self._world.threads_publish_result
        log = make_posting_log(
            result=result,
            channel=channel,
            error_message=self._world.publish_error_message,
            post_url=(
                (
                    "https://blog.naver.com/u/1"
                    if channel == PostingChannel.NAVER
                    else "https://www.threads.com/@u/post/1"
                )
                if result == PostingResultStatus.SUCCESS
                else None
            ),
        )
        return self._world.patch(
            post_id,
            status=(
                BlogTaskStatus.POSTED
                if result == PostingResultStatus.SUCCESS
                else BlogTaskStatus.POSTING_NEEDS_HUMAN
            ),
            posting_logs=[*task.posting_logs, log],
        )


class FakeTrendService:
    def __init__(self, world: FakeWorld):
        self._world = world

    async def recommend_topics(self, post_id, body):
        self._world.record("recommend_topics", post_id, body)
        return TrendRecommendationResult(
            post_id=post_id,
            trend_keywords=list(self._world.trend_keywords),
            topic_candidates=[],
            generated_at=now_iso(),
        )

    async def generate_topics(self, post_id, body):
        self._world.record("generate_topics", post_id, body)
        return TrendTopicResult(
            post_id=post_id,
            trend_keyword_id=body["trendKeywordId"],
            topic_candidates=list(self._world.topic_candidates),
            generated_at=now_iso(),
        )

    async def select_topic(self, post_id, body):
        self._world.record("select_topic", post_id, body)
        return self._world.patch(
            post_id,
            status=BlogTaskStatus.SEARCH_ANALYZING,
            trend_selection=TrendSelection(
                topic_candidate_id=body.get("topicCandidateId"),
                final_topic=body.get("finalTopic") or "소재",
                selected_trend_keyword_ids=body.get("selectedTrendKeywordIds", []),
                skipped=bool(body.get("skipped")),
                selected_at=now_iso(),
            ),
        )


class FakeDraftService:
    def __init__(self, world: FakeWorld):
        self._world = world

    async def generate_draft(self, post_id, body):
        self._world.record("generate_draft", post_id, body)
        if self._world.on_generate_draft is not None:
            await self._world.on_generate_draft()

        if self._world.draft_outcome == "failed_status":
            # 실제 generate_draft는 실패해도 예외를 던지지 않는다 — status만 바뀐다.
            return self._world.patch(post_id, status=BlogTaskStatus.FAILED)
        if self._world.draft_outcome == "ready_without_final_post":
            return self._world.patch(post_id, status=BlogTaskStatus.READY_TO_PUBLISH)

        final_post = FinalPost(
            title="최종 제목",
            body="최종 본문",
            hashtags=["blogit"],
            html_content="<h1>최종 제목</h1>",
        )
        self._world.made_final_post = final_post
        return self._world.patch(
            post_id,
            status=BlogTaskStatus.READY_TO_PUBLISH,
            final_post=final_post,
        )


def build_service(world: FakeWorld | None = None, repository=None):
    world = world or FakeWorld()
    repository = repository or InMemoryScheduledPostingRepository()
    service = ScheduledPostingService(
        repository=repository,
        blog_task_service=FakeBlogTaskService(world),
        trend_service=FakeTrendService(world),
        draft_service=FakeDraftService(world),
    )
    return service, repository, world


async def seed_batch(
    repository,
    topics: list[str],
    *,
    user_id: str = "user_1",
    interval_seconds: int = 600,
    batch_status: ScheduledBatchStatus = ScheduledBatchStatus.READY,
    job_status: ScheduledJobStatus = ScheduledJobStatus.WAITING,
    post_ids: list[str | None] | None = None,
    job_statuses: list[ScheduledJobStatus] | None = None,
    publish_naver: bool = True,
    publish_threads: bool = False,
) -> tuple[ScheduledBatch, list[ScheduledJob]]:
    """start_batch를 거치지 않고 배치·작업을 바로 심는다(네이버 확인을 타지 않는다)."""
    now = now_iso()
    jobs = [
        ScheduledJob(
            job_id=f"job_{index}",
            batch_id="batch_1",
            user_id=user_id,
            platform=SchedulePlatform.NAVER,
            sequence=index,
            topic=topic,
            post_id=(post_ids[index] if post_ids else None),
            publish_naver=publish_naver,
            publish_threads=publish_threads,
            status=(job_statuses[index] if job_statuses else job_status),
            scheduled_at=now if index == 0 else None,
            created_at=now,
            updated_at=now,
        )
        for index, topic in enumerate(topics)
    ]
    batch = ScheduledBatch(
        batch_id="batch_1",
        user_id=user_id,
        platform=SchedulePlatform.NAVER,
        publish_naver=publish_naver,
        publish_threads=publish_threads,
        status=batch_status,
        target_count=len(jobs),
        interval_seconds=interval_seconds,
        total_count=len(jobs),
        created_at=now,
        updated_at=now,
    )
    await repository.create_batch(batch, jobs)
    return batch, jobs


def naver_saved(monkeypatch, saved: bool) -> None:
    """네이버 저장 정보 확인만 갈아 끼운다 — 로컬 파일·자격 증명을 읽지 않는다."""
    monkeypatch.setattr(service_module, "_naver_saved", lambda user_id: saved)


def threads_saved(monkeypatch, saved: bool) -> None:
    """스레드 저장 정보 확인만 갈아 끼운다 — naver_saved와 같은 이유다."""
    monkeypatch.setattr(service_module, "_threads_saved", lambda user_id: saved)


VALID_START_BODY = {"topics": ["첫 소재", "둘째 소재"], "intervalSeconds": 600}


# ------------------------------------------------------------------------- 시작


async def test_네이버_저장정보가_없으면_예약_시작을_거부한다(monkeypatch):
    """자격 증명 없이 시작하면 첫 작업이 발행에서 죽는다 — 시작 전에 막는다.

    코드가 NAVER_NOT_CONNECTED여야 화면이 '설정으로 가기'를 안내한다(errors.py에서
    이 코드만 409로 내려간다). 일반 검증 실패로 뭉뚱그리면 안내가 사라진다.
    """
    naver_saved(monkeypatch, False)
    service, repository, _ = build_service()

    with pytest.raises(BlogTaskError) as caught:
        await service.start_batch("user_1", VALID_START_BODY)

    assert caught.value.code == "NAVER_NOT_CONNECTED"
    # 거부했으면 배치도 남지 않아야 한다.
    assert await repository.find_active_batch("user_1") is None


async def test_다음_발행을_기다리는_배치는_새_예약으로_갈아탄다(monkeypatch):
    """정지를 따로 누르지 않아도 새 입력값으로 시작할 수 있어야 한다.

    배치가 RUNNING이어도 대개는 다음 발행까지 기다리는 중이다. 그때까지 거절하면
    간격을 바꾸려고 정지를 눌러야 했고, 그 한 단계가 "1분으로 바꿨는데 5분마다
    올라간다"의 원인이었다.
    """
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()

    first = await service.start_batch("user_1", VALID_START_BODY)
    second = await service.start_batch(
        "user_1", {**VALID_START_BODY, "intervalSeconds": 60}
    )

    assert second.batch.batch_id != first.batch.batch_id
    assert second.batch.interval_seconds == 60
    # 옛 배치는 닫히고, 활성 배치는 여전히 하나뿐이다.
    closed = await repository.find_batch(first.batch.batch_id)
    assert closed.status == ScheduledBatchStatus.STOPPED
    active = await repository.find_active_batch("user_1")
    assert active.batch_id == second.batch.batch_id


async def test_글을_쓰는_중에는_새_예약을_시작할_수_없다(monkeypatch):
    """도는 중인 LLM·Selenium을 버리면 발행됐는지 알 수 없는 글이 생긴다.

    네이버 크롬이 두 개 뜨는 것도 이 가드가 막는다(프로필 잠금으로 둘 다 죽는다).
    """
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()

    started = await service.start_batch("user_1", VALID_START_BODY)
    jobs = await repository.list_jobs(started.batch.batch_id)
    await repository.save_job(
        jobs[0].model_copy(update={"status": ScheduledJobStatus.PUBLISHING})
    )

    with pytest.raises(BlogTaskError) as caught:
        await service.start_batch("user_1", VALID_START_BODY)

    assert caught.value.code == "VALIDATION_FAILED"
    assert "글을 쓰거나 발행하는 중" in caught.value.message
    # 옛 배치는 그대로 살아 있다 — 아무것도 버리지 않았다.
    assert (await repository.find_batch(started.batch.batch_id)).status != (
        ScheduledBatchStatus.STOPPED
    )


async def test_같은_clientRequestId는_배치를_새로_만들지_않는다(monkeypatch):
    """같은 클릭이 두 번 도착해도 배치는 하나다(멱등)."""
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    body = {**VALID_START_BODY, "clientRequestId": "click-1"}

    first = await service.start_batch("user_1", body)
    second = await service.start_batch("user_1", body)

    assert second.batch.batch_id == first.batch.batch_id
    assert [job.job_id for job in second.jobs] == [job.job_id for job in first.jobs]
    # 저장소에 정말 하나만 있어야 한다 — 응답만 같고 문서가 둘이면 워커가 둘 다 돌린다.
    saved = await repository.list_batches_by_status(list(ACTIVE_BATCH_STATUSES))
    assert len(saved) == 1


async def test_작업은_입력_순서를_지키고_첫_작업만_예정시각을_갖는다(monkeypatch):
    """나머지 작업의 시각은 앞 글이 실제로 발행된 뒤에 정해진다 — 지금 계산하면 어긋난다."""
    naver_saved(monkeypatch, True)
    service, _, _ = build_service()

    view = await service.start_batch(
        "user_1", {"topics": ["가 소재", "나 소재", "다 소재"], "intervalSeconds": 15}
    )

    assert [job.topic for job in view.jobs] == ["가 소재", "나 소재", "다 소재"]
    assert [job.sequence for job in view.jobs] == [0, 1, 2]
    assert view.jobs[0].scheduled_at is not None
    assert [job.scheduled_at for job in view.jobs[1:]] == [None, None]
    assert view.batch.status == ScheduledBatchStatus.READY
    assert view.batch.total_count == 3


# ----------------------------------------------------------------- 파이프라인 순서


async def test_execute_job은_기존_서비스들을_정해진_순서로_부른다():
    """이 서비스가 하는 일의 전부다 — 새 프롬프트도, 새 발행 경로도 없다."""
    service, repository, world = build_service()
    await seed_batch(repository, ["한강 러닝 코스"])

    job = await service.execute_job("job_0")

    assert world.pipeline_names == PIPELINE_METHODS
    assert job.status == ScheduledJobStatus.COMPLETED
    assert job.stage == ScheduledJobStage.DONE
    assert job.post_url == "https://blog.naver.com/u/1"


async def test_create_blog_task에_사용자와_소재와_기본_목적을_넘긴다():
    """예약에는 목적을 고르는 화면이 없다 — create_blog_task가 요구하는 자리를 상수로 채운다."""
    service, repository, world = build_service()
    await seed_batch(repository, ["한강 러닝 코스"])

    await service.execute_job("job_0")

    (body,) = world.args_of("create_blog_task")
    assert body["userId"] == "user_1"
    assert body["topic"] == "한강 러닝 코스"
    assert body["purpose"] == SCHEDULED_DEFAULT_PURPOSE
    # 상수를 그대로 넘기면 호출된 쪽이 고칠 때 다음 작업까지 오염된다.
    assert body["purpose"] is not SCHEDULED_DEFAULT_PURPOSE


async def test_관련_키워드가_없으면_제목_생성을_건너뛴다():
    """지어낸 제목을 쓰지 않는다 — 사용자가 입력한 소재가 그대로 제목이 된다."""
    service, repository, world = build_service()
    world.trend_keywords = []
    await seed_batch(repository, ["아주 좁은 소재"])

    job = await service.execute_job("job_0")

    assert "generate_topics" not in world.names
    _, select_body = world.args_of("select_topic")
    assert select_body["skipped"] is True
    assert select_body["selectedTrendKeywordIds"] == []
    # 건너뛰었을 뿐 실패가 아니다 — 나머지 단계는 그대로 간다.
    assert job.status == ScheduledJobStatus.COMPLETED


async def test_원고_생성_요청은_html_형식_하나만_넘긴다():
    """예약이 원고 설정을 새로 만들지 않는다 — 나머지는 전부 기존 사용자 설정이 정한다."""
    service, repository, world = build_service()
    await seed_batch(repository, ["한강 러닝 코스"])

    await service.execute_job("job_0")

    post_id, draft_body = world.args_of("generate_draft")
    assert draft_body == {"format": "html"}
    assert post_id == "post_1"


async def test_발행_요청은_네이버_자동발행_하나만_넘긴다():
    """publishThreads를 켜지 않은 예약은 예전 그대로 네이버만 발행한다.

    (에이전트 발행의 동반 정보였던 origin·deviceId는 2026-08-18 에이전트 코드
    제거와 함께 없어졌다 — 발행 요청은 method·channel 둘뿐이다.)
    """
    service, repository, world = build_service()
    await seed_batch(repository, ["한강 러닝 코스"])

    await service.execute_job("job_0")

    post_id, publish_body = world.args_of("publish_blog_task")
    assert publish_body == {"method": "auto", "channel": "naver"}
    assert post_id == "post_1"


async def test_원고가_만든_FinalPost가_손대지_않은_채_발행기로_간다():
    """예약이 원고를 자르거나 다시 조립하면 예약으로 쓴 글만 품질이 달라진다."""
    service, repository, world = build_service()
    await seed_batch(repository, ["한강 러닝 코스"])

    await service.execute_job("job_0")

    assert world.made_final_post is not None
    assert world.published_final_post is world.made_final_post


# ------------------------------------------------------------------ 중복 방지·재시도


async def test_원고가_이미_있으면_다시_쓰지_않고_발행만_다시_한다():
    """발행이 실패해 다시 들어온 경우다. 크롬이 죽은 것은 원고 품질 문제가 아니다."""
    service, repository, world = build_service()
    world.put(
        make_task(
            post_id="post_기존",
            status=BlogTaskStatus.READY_TO_PUBLISH,
            trend_selection=TrendSelection(
                final_topic="기존 제목",
                selected_trend_keyword_ids=[],
                skipped=False,
                selected_at=now_iso(),
            ),
            selected_intent=SelectedIntent(
                intent_id="intent_1", title="의도", target_reader="독자", rationale="근거"
            ),
            final_post=FinalPost(
                title="이미 쓴 제목",
                body="이미 쓴 본문",
                hashtags=[],
                html_content="<p>이미 쓴 본문</p>",
            ),
            posting_logs=[
                make_posting_log(
                    result=PostingResultStatus.FAIL,
                    post_url=None,
                    error_message="크롬이 죽었습니다",
                )
            ],
        )
    )
    await seed_batch(repository, ["한강 러닝 코스"], post_ids=["post_기존"])

    job = await service.execute_job("job_0")

    assert "create_blog_task" not in world.names
    assert "generate_draft" not in world.names
    assert world.pipeline_names == ["publish_blog_task"]
    assert job.status == ScheduledJobStatus.COMPLETED


async def test_네이버_발행_성공_기록이_있으면_다시_올리지_않는다():
    """같은 글이 두 번 게시되면 되돌릴 수 없다 — 성공 기록이 있으면 그 결과로 맞춘다."""
    service, repository, world = build_service()
    world.put(
        make_task(
            post_id="post_기존",
            status=BlogTaskStatus.POSTED,
            selected_intent=SelectedIntent(
                intent_id="intent_1", title="의도", target_reader="독자", rationale="근거"
            ),
            final_post=FinalPost(
                title="이미 올린 글", body="본문", hashtags=[], html_content="<p>본문</p>"
            ),
            posting_logs=[
                make_posting_log(post_url="https://blog.naver.com/u/이미올림"),
            ],
        )
    )
    await seed_batch(repository, ["한강 러닝 코스"], post_ids=["post_기존"])

    job = await service.execute_job("job_0")

    assert "publish_blog_task" not in world.names
    assert job.status == ScheduledJobStatus.COMPLETED
    assert job.stage == ScheduledJobStage.DONE
    assert job.post_url == "https://blog.naver.com/u/이미올림"


# ------------------------------------------------------------- 스레드 함께 발행


async def test_스레드_함께_발행이_켜지면_네이버_다음에_스레드를_발행한다():
    """순서가 규칙이다: Naver가 먼저, Threads가 뒤. 작업의 post_url은 네이버 주소다."""
    service, repository, world = build_service()
    await seed_batch(repository, ["한강 러닝 코스"], publish_threads=True)

    job = await service.execute_job("job_0")

    channels = [args[1]["channel"] for name, args in world.calls if name == "publish_blog_task"]
    assert channels == ["naver", "threads"]
    assert job.status == ScheduledJobStatus.COMPLETED
    assert job.post_url == "https://blog.naver.com/u/1"


async def test_스레드_발행_실패는_네이버_발행_사실을_함께_알린다():
    """네이버에는 이미 올라갔다 — 그 사실을 숨기면 사용자가 전부 실패한 줄 안다."""
    service, repository, world = build_service()
    world.threads_publish_result = PostingResultStatus.FAIL
    world.publish_error_message = "스레드 로그인 실패"
    await seed_batch(repository, ["한강 러닝 코스"], publish_threads=True)

    job = await service.execute_job("job_0")

    assert job.status == ScheduledJobStatus.FAILED
    assert job.error_code == "THREADS_PUBLISH_FAILED"
    assert "네이버에는 발행됨" in job.error_message


async def test_네이버_성공_기록이_있는_재시도는_스레드만_다시_발행한다():
    """스레드가 실패해 다시 들어온 경우다. 네이버에 같은 글을 또 올리면 안 된다."""
    service, repository, world = build_service()
    world.put(
        make_task(
            post_id="post_기존",
            status=BlogTaskStatus.POSTED,
            selected_intent=SelectedIntent(
                intent_id="intent_1", title="의도", target_reader="독자", rationale="근거"
            ),
            final_post=FinalPost(
                title="이미 올린 글", body="본문", hashtags=[], html_content="<p>본문</p>"
            ),
            posting_logs=[
                make_posting_log(post_url="https://blog.naver.com/u/이미올림"),
            ],
        )
    )
    await seed_batch(
        repository, ["한강 러닝 코스"], post_ids=["post_기존"], publish_threads=True
    )

    job = await service.execute_job("job_0")

    channels = [args[1]["channel"] for name, args in world.calls if name == "publish_blog_task"]
    assert channels == ["threads"]
    assert job.status == ScheduledJobStatus.COMPLETED
    assert job.post_url == "https://blog.naver.com/u/이미올림"


async def test_스레드까지_올라간_글은_발행을_통째로_건너뛴다():
    """두 채널 모두 성공 기록이 있으면 어느 쪽에도 다시 올리지 않는다."""
    service, repository, world = build_service()
    world.put(
        make_task(
            post_id="post_기존",
            status=BlogTaskStatus.POSTED,
            selected_intent=SelectedIntent(
                intent_id="intent_1", title="의도", target_reader="독자", rationale="근거"
            ),
            final_post=FinalPost(
                title="이미 올린 글", body="본문", hashtags=[], html_content="<p>본문</p>"
            ),
            posting_logs=[
                make_posting_log(post_url="https://blog.naver.com/u/이미올림"),
                make_posting_log(
                    channel=PostingChannel.THREADS,
                    post_url="https://www.threads.com/@u/post/이미올림",
                ),
            ],
        )
    )
    await seed_batch(
        repository, ["한강 러닝 코스"], post_ids=["post_기존"], publish_threads=True
    )

    job = await service.execute_job("job_0")

    assert "publish_blog_task" not in world.names
    assert job.status == ScheduledJobStatus.COMPLETED


async def test_스레드_저장정보가_없으면_스레드_함께_발행_시작을_거부한다(monkeypatch):
    """글을 다 만들어 놓고 스레드 발행에서 죽는 것보다 시작 전에 막는 편이 낫다."""
    naver_saved(monkeypatch, True)
    threads_saved(monkeypatch, False)
    service, repository, _ = build_service()

    with pytest.raises(BlogTaskError) as caught:
        await service.start_batch("user_1", {**VALID_START_BODY, "publishThreads": True})

    assert caught.value.code == "THREADS_NOT_CONNECTED"
    assert await repository.find_active_batch("user_1") is None


async def test_시작_요청의_publishThreads가_배치와_작업에_실린다(monkeypatch):
    naver_saved(monkeypatch, True)
    threads_saved(monkeypatch, True)
    service, _repository, _ = build_service()

    started = await service.start_batch(
        "user_1", {**VALID_START_BODY, "publishThreads": True}
    )

    assert started.batch.publish_threads is True
    assert all(job.publish_threads for job in started.jobs)


# ----------------------------------------------------------- 쓰레드 단독 예약


async def test_쓰레드에만_올리는_작업은_네이버를_아예_부르지_않는다():
    """2026-08-06 — 사용자가 고른 곳에만 올린다. 네이버 발행을 끼워 넣지 않는다."""
    service, repository, world = build_service()
    await seed_batch(
        repository, ["한강 러닝 코스"], publish_naver=False, publish_threads=True
    )

    job = await service.execute_job("job_0")

    channels = [args[1]["channel"] for name, args in world.calls if name == "publish_blog_task"]
    assert channels == ["threads"]
    assert job.status == ScheduledJobStatus.COMPLETED
    # 네이버 주소가 없으므로 대표 주소는 스레드 주소다.
    assert job.post_url == "https://www.threads.com/@u/post/1"


async def test_쓰레드_단독_예약의_실패는_네이버_발행을_들먹이지_않는다():
    """올라간 곳이 없다 — '네이버에는 발행됨'은 이 경우 거짓말이다."""
    service, repository, world = build_service()
    world.threads_publish_result = PostingResultStatus.FAIL
    world.publish_error_message = "스레드 로그인 실패"
    await seed_batch(
        repository, ["한강 러닝 코스"], publish_naver=False, publish_threads=True
    )

    job = await service.execute_job("job_0")

    assert job.status == ScheduledJobStatus.FAILED
    assert job.error_code == "THREADS_PUBLISH_FAILED"
    assert "네이버" not in job.error_message


async def test_쓰레드에_이미_올라간_단독_예약은_다시_올리지_않는다():
    """재시도로 다시 들어와도 같은 글이 스레드에 두 벌 생기면 안 된다."""
    service, repository, world = build_service()
    world.put(
        make_task(
            post_id="post_기존",
            status=BlogTaskStatus.POSTED,
            selected_intent=SelectedIntent(
                intent_id="intent_1", title="의도", target_reader="독자", rationale="근거"
            ),
            final_post=FinalPost(
                title="이미 올린 글", body="본문", hashtags=[], html_content="<p>본문</p>"
            ),
            posting_logs=[
                make_posting_log(
                    channel=PostingChannel.THREADS,
                    post_url="https://www.threads.com/@u/post/이미올림",
                ),
            ],
        )
    )
    await seed_batch(
        repository,
        ["한강 러닝 코스"],
        post_ids=["post_기존"],
        publish_naver=False,
        publish_threads=True,
    )

    job = await service.execute_job("job_0")

    assert "publish_blog_task" not in world.names
    assert job.status == ScheduledJobStatus.COMPLETED
    assert job.post_url == "https://www.threads.com/@u/post/이미올림"


async def test_쓰레드_단독_예약은_네이버_계정이_없어도_시작된다(monkeypatch):
    """쓰지 않는 플랫폼의 계정을 요구하면 고른 대로 발행할 수 없다(2026-08-06)."""
    naver_saved(monkeypatch, False)
    threads_saved(monkeypatch, True)
    service, _repository, _ = build_service()

    started = await service.start_batch(
        "user_1",
        {**VALID_START_BODY, "publishNaver": False, "publishThreads": True},
    )

    assert started.batch.publish_naver is False
    assert all(job.publish_naver is False for job in started.jobs)
    assert all(job.publish_threads for job in started.jobs)


async def test_시작부터_발행까지_쓰레드_단독_예약은_네이버를_거치지_않는다(monkeypatch):
    """화면이 보내는 몸통 그대로 start_batch를 거쳐 실제로 작업을 돌려 본다.

    앞의 테스트들은 배치를 바로 심어 발행 단계만 봤다. 여기서는 **검증 → 배치 생성 →
    실행**을 한 줄로 이어 본다 — 사용자가 겪은 "네이버 발행 뒤 쓰레드"가 이 경로에서
    다시 나오지 않는지 확인하는 자리다(2026-08-06 신고).
    """
    naver_saved(monkeypatch, True)
    threads_saved(monkeypatch, True)
    service, repository, world = build_service()

    started = await service.start_batch(
        "user_1",
        {
            "topics": ["AI 툴 소개"],
            "intervalSeconds": 600,
            "platform": "naver",
            "publishNaver": False,
            "publishThreads": True,
        },
    )
    job_id = started.jobs[0].job_id
    assert started.jobs[0].publish_naver is False

    job = await service.execute_job(job_id)

    channels = [args[1]["channel"] for name, args in world.calls if name == "publish_blog_task"]
    assert channels == ["threads"]
    assert job.status == ScheduledJobStatus.COMPLETED
    # 네이버 발행 단계를 지나지 않았다.
    stages = [entry.message for entry in (await repository.find_batch(job.batch_id)).logs]
    assert not any("네이버 발행" in message for message in stages)


async def test_네이버를_쓰는_예약은_여전히_네이버_계정을_요구한다(monkeypatch):
    """느슨해진 것은 '안 쓰는 플랫폼'뿐이다 — 쓰는 곳의 확인은 그대로다."""
    naver_saved(monkeypatch, False)
    threads_saved(monkeypatch, True)
    service, repository, _ = build_service()

    with pytest.raises(BlogTaskError) as caught:
        await service.start_batch(
            "user_1", {**VALID_START_BODY, "publishThreads": True}
        )

    assert caught.value.code == "NAVER_NOT_CONNECTED"
    assert await repository.find_active_batch("user_1") is None


# --------------------------------------------------------------------- 실패 읽기


async def _seed_ready_for_draft(repository, world, post_id: str = "post_원고전") -> None:
    """M2·M3을 이미 마친 글 하나를 심는다 — 원고 단계만 보고 싶을 때 쓴다."""
    world.put(
        make_task(
            post_id=post_id,
            status=BlogTaskStatus.INTENT_SELECTED,
            trend_selection=TrendSelection(
                final_topic="제목",
                selected_trend_keyword_ids=[],
                skipped=False,
                selected_at=now_iso(),
            ),
            selected_intent=SelectedIntent(
                intent_id="intent_1", title="의도", target_reader="독자", rationale="근거"
            ),
        )
    )
    await seed_batch(repository, ["한강 러닝 코스"], post_ids=[post_id])


async def test_generate_draft가_조용히_실패하면_작업도_실패로_남는다():
    """generate_draft는 실패해도 예외를 던지지 않는다 — 성공으로 읽으면 빈 글이 올라간다."""
    service, repository, world = build_service()
    world.draft_outcome = "failed_status"
    await _seed_ready_for_draft(repository, world)

    job = await service.execute_job("job_0")

    assert job.status == ScheduledJobStatus.FAILED
    assert job.error_code == "DRAFT_FAILED"
    assert "publish_blog_task" not in world.names


async def test_READY_TO_PUBLISH여도_본문이_없으면_실패로_본다():
    """상태만 보고 넘기면 본문 없는 글이 네이버로 간다."""
    service, repository, world = build_service()
    world.draft_outcome = "ready_without_final_post"
    await _seed_ready_for_draft(repository, world)

    job = await service.execute_job("job_0")

    assert job.status == ScheduledJobStatus.FAILED
    assert job.error_code == "DRAFT_FAILED"
    assert "publish_blog_task" not in world.names


async def test_발행이_추가인증을_요구하면_작업도_배치도_NEEDS_HUMAN이_된다():
    """캡차·2단계 인증은 실패가 아니다 — 사람이 인증을 마치고 재개하면 그대로 이어진다."""
    service, repository, world = build_service()
    world.publish_result = PostingResultStatus.NEEDS_HUMAN
    world.publish_error_message = "네이버가 추가 인증을 요구합니다"
    await seed_batch(repository, ["한강 러닝 코스", "다음 소재"])

    job = await service.execute_job("job_0")

    assert job.status == ScheduledJobStatus.NEEDS_HUMAN
    assert job.error_code == "NAVER_NEEDS_HUMAN"
    assert job.error_message == "네이버가 추가 인증을 요구합니다"

    batch = await repository.find_batch("batch_1")
    assert batch.status == ScheduledBatchStatus.NEEDS_HUMAN
    assert batch.current_job_id is None
    # 실패로 세지 않는다 — 재개하면 이 작업이 다시 돈다.
    assert batch.failed_count == 0


async def test_발행이_실패하면_작업만_실패하고_배치는_계속_간다():
    """소재 하나가 실패했다고 나머지 예약까지 닫으면 안 된다."""
    service, repository, world = build_service()
    world.publish_result = PostingResultStatus.FAIL
    world.publish_error_message = "네이버 에디터를 찾지 못했습니다"
    await seed_batch(repository, ["한강 러닝 코스", "다음 소재"])

    job = await service.execute_job("job_0")

    assert job.status == ScheduledJobStatus.FAILED
    assert job.error_code == "PUBLISH_FAILED"
    assert job.error_message == "네이버 에디터를 찾지 못했습니다"

    batch = await repository.find_batch("batch_1")
    assert batch.failed_count == 1
    assert batch.current_job_id is None
    assert batch.status in ACTIVE_BATCH_STATUSES

    # 다음 작업은 손대지 않은 채 대기로 남아 있어야 한다.
    jobs = await repository.list_jobs("batch_1")
    assert jobs[1].status == ScheduledJobStatus.WAITING


# --------------------------------------------------------------------- 자동 선택


def test_pick_intent는_관련도_합계_자료수_원래순서로_고른다():
    """새 LLM 호출 없이 검증이 이미 매긴 점수만 읽는다."""
    낮음 = make_intent_candidate("낮음", [10, 10])
    높음 = make_intent_candidate("높음", [50])
    assert pick_intent([낮음, 높음]) is 높음

    # 합계가 같으면 자료가 많은 쪽 — 근거가 두꺼운 의도다.
    하나 = make_intent_candidate("하나", [50])
    둘 = make_intent_candidate("둘", [25, 25])
    assert pick_intent([하나, 둘]) is 둘

    # 둘 다 같으면 원래 반환 순서가 빠른 쪽. 매번 다른 의도를 고르면 안 된다.
    앞 = make_intent_candidate("앞", [10])
    뒤 = make_intent_candidate("뒤", [10])
    assert pick_intent([앞, 뒤]) is 앞

    assert pick_intent([]) is None


def test_pick_title은_추천_다음_점수_다음_첫번째다():
    """제목을 새로 만들지 않고 기존 후보 중에서만 고른다."""
    추천 = make_topic_candidate("cand_추천", "추천 제목", recommended=True, score=10.0)
    고점 = make_topic_candidate("cand_고점", "높은 점수", recommended=False, score=99.0)
    assert pick_title([고점, 추천]) is 추천

    낮음 = make_topic_candidate("cand_낮음", "낮은 점수", score=30.0)
    assert pick_title([낮음, 고점]) is 고점

    # 채점 전 후보만 있으면 첫 번째.
    첫째 = make_topic_candidate("cand_1", "첫째")
    둘째 = make_topic_candidate("cand_2", "둘째")
    assert pick_title([첫째, 둘째]) is 첫째

    assert pick_title([]) is None


def test_pick_trend_keyword는_isEligible이_없으면_자격_있는_것으로_본다():
    """없는 필드를 False로 읽으면 키워드가 있는데도 매번 트렌드를 건너뛴다."""
    # 필드 자체가 없는 옛 응답.
    앞 = SimpleNamespace(keyword="앞")
    뒤 = SimpleNamespace(keyword="뒤")
    assert pick_trend_keyword([앞, 뒤]) is 앞

    # 판정 전(None)도 자격 있는 것으로 본다.
    미판정 = make_trend_keyword("미판정", is_eligible=None)
    assert pick_trend_keyword([미판정]) is 미판정

    # 명시적으로 False인 것만 건너뛴다.
    탈락 = make_trend_keyword("탈락", is_eligible=False)
    통과 = make_trend_keyword("통과", is_eligible=True)
    assert pick_trend_keyword([탈락, 통과]) is 통과
    assert pick_trend_keyword([탈락]) is None
    assert pick_trend_keyword([]) is None


# ----------------------------------------------------------------- 정지·재개·재시도


async def test_새_예약_시작은_미완료_작업을_DB에서_지운다():
    """'새 예약 시작'(discard, 2026-08-04): 정지가 아니라 폐기다. 대기·실패 작업이
    취소 표시로 남는 게 아니라 사라지고, 완료가 없으면 배치째 사라진다."""
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["첫 소재", "둘째 소재", "셋째 소재"],
        job_statuses=[
            ScheduledJobStatus.WAITING,
            ScheduledJobStatus.FAILED,
            ScheduledJobStatus.NEEDS_HUMAN,
        ],
        batch_status=ScheduledBatchStatus.RUNNING,
    )

    await service.discard("user_1", "batch_1")

    assert await repository.find_batch("batch_1") is None
    assert await repository.list_jobs("batch_1") == []


async def test_새_예약_시작도_완료된_작업의_기록은_남긴다():
    """이미 네이버에 올라간 글은 지운다고 사라지지 않는다 — 무엇이 발행됐는지는 남는다."""
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["발행된 소재", "대기 소재"],
        job_statuses=[ScheduledJobStatus.COMPLETED, ScheduledJobStatus.WAITING],
        batch_status=ScheduledBatchStatus.RUNNING,
    )

    await service.discard("user_1", "batch_1")

    batch = await repository.find_batch("batch_1")
    assert batch is not None
    assert batch.status == ScheduledBatchStatus.STOPPED
    remaining = await repository.list_jobs("batch_1")
    assert [job.topic for job in remaining] == ["발행된 소재"]
    assert batch.completed_count == 1 and batch.total_count == 1
    # 닫힌 배치라 활성 조회에는 잡히지 않는다 — 화면이 null을 받아 입력을 초기화한다.
    assert await repository.find_active_batch("user_1") is None


async def test_글을_쓰는_중에도_새_예약_시작이_멈추고_지운다():
    """처음에는 실행 중이면 거절했는데 사용자가 재확인했다(2026-08-04): "작업을 하고
    있더라도 중지가 되고 새 예약으로 되어야해".

    도는 단계를 끊지는 않는다 — 원고 생성이 끝난 다음 안전 지점에서 작업 기록이 사라진
    것을 보고 스스로 멈춘다. 발행까지는 가지 않는다.
    """
    service, repository, world = build_service()
    await seed_batch(repository, ["쿠팡"], batch_status=ScheduledBatchStatus.RUNNING)

    async def 새_예약_시작을_누른다():
        # 원고를 만드는 도중(=실행 중)에 버튼이 눌린 상황.
        await service.discard("user_1", "batch_1")

    world.on_generate_draft = 새_예약_시작을_누른다

    await service.execute_job("job_0")

    # 발행하지 않았고, 배치·작업이 DB에서 사라졌다.
    assert "publish_blog_task" not in world.names
    assert await repository.find_batch("batch_1") is None
    assert await repository.list_jobs("batch_1") == []
    # 만들다 만 글(blogTask)도 함께 사라졌다 — 남으면 '내 글 목록'에 '원고 준비 중'
    # 카드가 남는다(2026-08-04 실사용, '뚜벅이 여행자…' 카드).
    assert world.tasks == {}


async def test_새_예약_시작은_대기_작업의_만들다_만_글도_지운다():
    """대기 중이던 작업이 이미 글(blogTask)을 만들어 둔 경우 — 그 글도 목록에서 사라져야 한다."""
    service, repository, world = build_service()
    world.put(make_task(post_id="post_준비중"))
    await seed_batch(
        repository,
        ["대기 소재"],
        post_ids=["post_준비중"],
        batch_status=ScheduledBatchStatus.RUNNING,
    )

    await service.discard("user_1", "batch_1")

    assert "post_준비중" not in world.tasks
    assert ("delete_user_blog_task", ("user_1", "post_준비중")) in world.calls


async def test_네이버에_올라간_글은_새_예약_시작도_지우지_않는다():
    """발행 성공 기록이 있는 글을 지우면 무엇이 발행됐는지의 기록이 사라진다.

    작업 상태가 미완료로 남아 있어도(발행 직후 죽어 카운터가 안 맞는 경우) 글에 성공
    로그가 있으면 보존한다.
    """
    service, repository, world = build_service()
    world.put(
        make_task(
            post_id="post_발행됨",
            status=BlogTaskStatus.POSTED,
            posting_logs=[make_posting_log()],
        )
    )
    await seed_batch(
        repository,
        ["발행된 소재"],
        post_ids=["post_발행됨"],
        job_statuses=[ScheduledJobStatus.FAILED],  # 기록상으로는 미완료다
        batch_status=ScheduledBatchStatus.RUNNING,
    )

    await service.discard("user_1", "batch_1")

    assert "post_발행됨" in world.tasks  # 글은 남는다
    assert await repository.find_batch("batch_1") is None  # 예약 기록은 사라진다


async def test_큐에서_뺀_작업의_만들다_만_글도_함께_지운다():
    """휴지통 버튼(작업 삭제)도 같은 정책이다 — 잔재 카드를 남기지 않는다."""
    service, repository, world = build_service()
    world.put(make_task(post_id="post_대기"))
    await seed_batch(
        repository,
        ["첫 소재", "둘째 소재"],
        post_ids=[None, "post_대기"],
        batch_status=ScheduledBatchStatus.RUNNING,
    )

    await service.delete_job("user_1", "job_1")

    assert "post_대기" not in world.tasks


async def test_남의_배치는_새_예약_시작으로_지울_수_없다():
    service, repository, _ = build_service()
    await seed_batch(repository, ["첫 소재"])

    with pytest.raises(BlogTaskError) as caught:
        await service.discard("다른_사용자", "batch_1")

    assert caught.value.code == "NOT_FOUND"


async def test_정지하면_대기_중인_작업이_취소된다():
    """대기 중인 것은 즉시 취소하고, 돌고 있는 것은 안전한 지점에서 멈춘다."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["첫 소재", "둘째 소재", "셋째 소재"])

    view = await service.request_stop("user_1", "batch_1")

    assert [job.status for job in view.jobs] == [ScheduledJobStatus.CANCELED] * 3
    assert view.batch.stop_requested is True
    assert view.batch.canceled_count == 3
    # 돌고 있는 작업이 없으므로 배치는 그 자리에서 닫힌다.
    assert view.batch.status == ScheduledBatchStatus.STOPPED
    assert view.batch.completed_at is not None


async def test_재개는_같은_배치와_작업과_글을_다시_쓴다():
    """새 배치를 만들면 이미 만든 원고를 버리고 처음부터 다시 쓰게 된다."""
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["끝난 소재", "인증이 필요한 소재"],
        batch_status=ScheduledBatchStatus.NEEDS_HUMAN,
        post_ids=["post_끝남", "post_인증대기"],
        job_statuses=[ScheduledJobStatus.COMPLETED, ScheduledJobStatus.NEEDS_HUMAN],
    )
    # 인증 대기 작업에는 실패 사유가 적혀 있다.
    멈춘작업 = await repository.find_job("job_1")
    await repository.save_job(
        멈춘작업.model_copy(
            update={"error_code": "NAVER_NEEDS_HUMAN", "error_message": "추가 인증 필요"}
        )
    )

    view = await service.resume("user_1", "batch_1")

    assert view.batch.batch_id == "batch_1"
    assert view.batch.status == ScheduledBatchStatus.RUNNING
    assert view.batch.pause_requested is False
    # 재개는 곧바로 이어 간다 — 멈춰 있던 시간이 이미 간격을 채웠다.
    assert view.batch.next_run_at is None

    assert [job.job_id for job in view.jobs] == ["job_0", "job_1"]
    assert view.jobs[1].status == ScheduledJobStatus.WAITING
    assert view.jobs[1].post_id == "post_인증대기"
    assert view.jobs[1].error_code is None
    assert view.jobs[1].error_message is None
    # 이미 끝난 작업은 건드리지 않는다.
    assert view.jobs[0].status == ScheduledJobStatus.COMPLETED


async def test_재시도하면_배치의_실패_개수가_줄어든다():
    """진행률이 100%인데 발행은 하나도 안 된 화면의 원인이 여기였다(2026-08-06).

    ``retry_job``은 실패한 작업을 대기로 되돌리면서 ``failed_count``를 그대로 뒀다.
    그 작업이 나중에 성공하면 **같은 작업이 실패 1 · 완료 1로 두 번 세어진다.**
    화면의 진행률은 (완료+실패+취소)/전체라 그 배치가 100%가 된다.

    사용자의 DB에 그 상태가 그대로 있었다: 작업 2건짜리 배치에 ``done=1 fail=2``.
    """
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["실패한 소재", "남은 소재"],
        job_statuses=[ScheduledJobStatus.FAILED, ScheduledJobStatus.WAITING],
    )
    await _batch_counts(repository, failed_count=1)

    view = await service.retry_job("user_1", "job_0")

    # 되살아난 작업은 더 이상 실패가 아니다.
    assert view.jobs[0].status == ScheduledJobStatus.WAITING
    assert view.batch.failed_count == 0
    assert view.batch.completed_count == 0


async def test_실패한_작업이_성공하면_실패로도_완료로도_두_번_세지_않는다():
    """위 테스트의 결말. 재시도 → 성공까지 가서 집계가 실제 작업과 맞는지 본다."""
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["다시 쓸 소재", "남은 소재"],
        job_statuses=[ScheduledJobStatus.FAILED, ScheduledJobStatus.WAITING],
    )
    await _batch_counts(repository, failed_count=1)

    await service.retry_job("user_1", "job_0")
    await service.execute_job("job_0")

    batch = await repository.find_batch("batch_1")
    jobs = await repository.list_jobs("batch_1")
    assert jobs[0].status == ScheduledJobStatus.COMPLETED
    assert batch.completed_count == 1
    assert batch.failed_count == 0
    # 끝난 작업 수가 전체를 넘지 않는다 — 넘으면 진행률이 100%로 굳는다.
    assert (
        batch.completed_count + batch.failed_count + batch.canceled_count
        <= batch.total_count
    )


async def test_한_작업이_두_번_실패해도_실패_개수는_하나다():
    """``_fail``이 부를 때마다 1씩 더하던 자리다. 작업은 하나인데 2가 됐다."""
    service, repository, world = build_service()
    world.draft_outcome = "failed_status"
    await seed_batch(repository, ["안 써지는 소재"])

    await service.execute_job("job_0")
    await service.retry_job("user_1", "job_0")
    await service.execute_job("job_0")

    batch = await repository.find_batch("batch_1")
    assert (await repository.find_job("job_0")).status == ScheduledJobStatus.FAILED
    assert batch.failed_count == 1


async def test_완료된_작업은_다시_시도할_수_없다():
    """같은 원고가 두 번 올라간다."""
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["끝난 소재"],
        job_statuses=[ScheduledJobStatus.COMPLETED],
    )

    with pytest.raises(BlogTaskError) as caught:
        await service.retry_job("user_1", "job_0")

    assert caught.value.code == "VALIDATION_FAILED"
    assert "이미 완료된 작업" in caught.value.message
    # 상태도 그대로여야 한다.
    job = await repository.find_job("job_0")
    assert job.status == ScheduledJobStatus.COMPLETED
    assert job.retry_count == 0


async def test_발행에_성공하면_다음_실행_시각을_간격만큼_뒤로_잡는다():
    """간격은 직전 **발행 성공** 시각부터 잰다 — 원고 생성 시간은 간격을 갉아먹지 않는다."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["한강 러닝 코스", "다음 소재"], interval_seconds=45)

    기준 = datetime.now(timezone.utc)
    await service.execute_job("job_0")

    batch = await repository.find_batch("batch_1")
    assert batch.completed_count == 1
    assert batch.current_job_id is None
    다음 = datetime.fromisoformat(batch.next_run_at.replace("Z", "+00:00"))
    간격 = 다음 - 기준
    assert timedelta(seconds=44) < 간격 < timedelta(seconds=46)


async def test_발행_완료_로그_뒤에_다음_작업_예고가_붙는다():
    """'발행 완료' 뒤의 침묵은 끝난 것인지 기다리는 것인지 구분되지 않는다(2026-08-04
    사용자 요청). 다음 대기 작업의 소재와 시작 예정 시각을 로그로 예고한다."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["한강 러닝 코스", "다음 소재"], interval_seconds=45)

    await service.execute_job("job_0")

    batch = await repository.find_batch("batch_1")
    messages = [log.message for log in batch.logs]
    # 작업에 딸린 로그는 **어느 소재의 글인지**를 앞에 달고 나온다(2026-08-06).
    발행_완료 = messages.index("'한강 러닝 코스'의 네이버 발행이 완료되었습니다.")
    예고 = [m for m in messages if "'다음 소재' 소재에 대한 원고 작업이 시작됩니다" in m]
    assert 예고, messages
    # '발행 완료' 바로 뒤에 온다 — 그래야 완료를 본 눈이 다음 줄에서 예정을 읽는다.
    assert messages.index(예고[0]) == 발행_완료 + 1
    # 예정 시각은 next_run_at을 이 PC의 지역 시간으로 적은 것이다.
    due = datetime.fromisoformat(batch.next_run_at.replace("Z", "+00:00")).astimezone()
    assert 예고[0].startswith(f"{due:%H시 %M분 %S초}에")


async def test_작업에_딸린_로그는_모두_어느_소재의_글인지_밝힌다():
    """한 배치의 글들이 이어 돌기 때문에, 소재 없이는 어느 글의 이야기인지 알 수 없다.

    2026-08-06 사용자 신고 — 시작 줄에만 소재가 있고 그 뒤 단계들은 소재 없이 찍혀,
    두 글의 단계가 섞인 목록에서 짝을 맞출 수 없었다.
    """
    service, repository, world = build_service()
    world.trend_keywords = [make_trend_keyword("라면 추천")]
    world.topic_candidates = [make_topic_candidate(title="신라면 맛있게 끓이는 법")]
    await seed_batch(repository, ["신라면"])

    await service.execute_job("job_0")

    batch = await repository.find_batch("batch_1")
    단계들 = [
        log.message
        for log in batch.logs
        # 작업에 딸린 줄만 본다(배치 전체에 대한 줄은 소재가 없는 것이 맞다).
        if log.job_id is not None
    ]
    assert 단계들, [log.message for log in batch.logs]
    for message in 단계들:
        assert message.startswith("'신라면'의"), message


async def test_키워드_선택_로그는_어떤_키워드를_골랐는지_적는다():
    """그 선택이 제목·자료·원고의 방향을 정한다 — 결과가 이상할 때 어디서 갈렸는지
    볼 수 있는 유일한 자리다(2026-08-06 사용자 요청)."""
    service, repository, world = build_service()
    world.trend_keywords = [make_trend_keyword("라면 추천")]
    await seed_batch(repository, ["신라면"])

    await service.execute_job("job_0")

    batch = await repository.find_batch("batch_1")
    키워드줄 = [log.message for log in batch.logs if "소재 관련 키워드를 선택" in log.message]
    assert 키워드줄 == ["'신라면'의 소재 관련 키워드를 선택했습니다: '라면 추천'"]


async def test_마지막_작업의_발행_완료_뒤에는_예고가_없다():
    """대기 작업이 없으면 예고도 없다 — 시작되지 않을 작업을 예고하지 않는다."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["한강 러닝 코스"])

    await service.execute_job("job_0")

    batch = await repository.find_batch("batch_1")
    assert not any("원고 작업이 시작됩니다" in log.message for log in batch.logs)


async def test_정지나_일시정지를_요청한_배치에는_다음_작업_예고가_없다():
    """멈추라고 한 배치의 대기 작업은 시작되지 않는다 — 거짓 예고를 남기지 않는다.

    발행이 끝나는 순간과 정지 요청이 겹치는 좁은 창이라 execute_job으로는 재현이
    안정적이지 않다 — 예고 함수를 그 상태에서 직접 부른다.
    """
    for 요청 in ("stop_requested", "pause_requested"):
        service, repository, _ = build_service()
        batch, _ = await seed_batch(repository, ["한강 러닝 코스", "다음 소재"])
        await repository.save_batch(batch.model_copy(update={요청: True}))

        await service._announce_next_job("batch_1")

        refreshed = await repository.find_batch("batch_1")
        assert not any(
            "원고 작업이 시작됩니다" in log.message for log in refreshed.logs
        ), 요청


async def test_도중에_정지를_누르면_발행하지_않고_멈춘다():
    """원고까지 만든 뒤 정지를 눌렀다. 실패가 아니라 멈춤이므로 다시 대기로 남는다."""
    service, repository, world = build_service()
    await seed_batch(repository, ["한강 러닝 코스"])

    async def 정지를_누른다():
        batch = await repository.find_batch("batch_1")
        await repository.save_batch(
            batch.model_copy(
                update={
                    "stop_requested": True,
                    "status": ScheduledBatchStatus.STOP_REQUESTED,
                }
            )
        )

    world.on_generate_draft = 정지를_누른다

    job = await service.execute_job("job_0")

    assert "publish_blog_task" not in world.names
    # 멈춤은 실패가 아니다 — 재개하면 원고가 있으므로 발행부터 이어진다.
    assert job.status == ScheduledJobStatus.WAITING
    assert job.error_code is None
    batch = await repository.find_batch("batch_1")
    assert batch.failed_count == 0
    assert batch.completed_count == 0


# ------------------------------------------- 회귀: 정지한 배치가 갇히던 문제


async def test_정지로_멈춘_작업은_배치의_현재작업_표시도_비운다():
    """`_park`가 current_job_id를 비우지 않아 배치가 영영 갇히던 버그의 회귀 테스트.

    워커의 `_reconcile_controls`는 current_job_id가 비어 있을 때만 정지를 STOPPED로
    옮긴다. 그래서 여기서 비우지 않으면 배치가 STOP_REQUESTED에 머물고,
    `find_active_batch`가 그것을 계속 활성으로 보아 **사용자는 서버를 다시 켤 때까지
    새 예약을 시작하지 못한다.**
    """
    service, repository, world = build_service()
    await seed_batch(repository, ["한강 러닝 코스"])

    async def 정지를_누른다():
        batch = await repository.find_batch("batch_1")
        await repository.save_batch(
            batch.model_copy(
                update={
                    "stop_requested": True,
                    "status": ScheduledBatchStatus.STOP_REQUESTED,
                }
            )
        )

    world.on_generate_draft = 정지를_누른다

    await service.execute_job("job_0")

    batch = await repository.find_batch("batch_1")
    assert batch.current_job_id is None

    # 워커가 다음에 살펴볼 때 배치가 실제로 닫히는지까지 본다.
    from app.modules.scheduled_posting import ScheduledPostingWorker

    worker = ScheduledPostingWorker(service, repository)
    await worker._tick()
    closed = await repository.find_batch("batch_1")
    assert closed.status == ScheduledBatchStatus.STOPPED


# --------------------------------------------------------------------- 작업 삭제
#
# 사용자가 원하는 것은 하나다: 소재 1·2·3을 넣어 두고 2만 빼면 **1과 3이 그대로 이어서**
# 쓰이는 것. 그래서 여기서 보는 것도 '뺀 뒤에도 남은 것들이 원래 순서대로 돌아가는가'와
# '무엇을 빼면 안 되는가' 둘이다.


async def _batch_counts(repository, **counts):
    """배치의 집계 값을 일부러 어긋나게 심는다 — 다시 세는지 보려면 그래야 한다."""
    batch = await repository.find_batch("batch_1")
    await repository.save_batch(batch.model_copy(update=counts))


async def test_가운데_작업을_빼면_남은_작업의_순서가_그대로다():
    """1·2·3에서 2를 빼면 1·3이다. sequence를 다시 매기지 않는다 — 워커는 sequence 순으로
    다음 WAITING을 집으므로 빈 번호가 있어도 순서는 유지된다."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["첫 소재", "둘째 소재", "셋째 소재"])

    view = await service.delete_job("user_1", "job_1")

    assert [job.job_id for job in view.jobs] == ["job_0", "job_2"]
    assert [job.topic for job in view.jobs] == ["첫 소재", "셋째 소재"]
    # 번호를 다시 매기지 않는다 — 0·2 그대로다.
    assert [job.sequence for job in view.jobs] == [0, 2]
    # 저장소에서도 정말 사라졌어야 한다(응답만 걸러 낸 것이면 워커가 그대로 돌린다).
    assert await repository.find_job("job_1") is None
    assert [job.job_id for job in await repository.list_jobs("batch_1")] == ["job_0", "job_2"]


async def test_가운데를_뺀_뒤_워커가_집는_다음_작업은_셋째다():
    """이 기능의 목적 그 자체다 — 1 다음이 (2가 아니라) 3이어야 한다.

    목록만 확인하지 않고 워커에게 실제로 다음 작업을 집게 한다. 워커의 선택 규칙이
    바뀌면 목록 검사만으로는 못 잡는다.
    """
    service, repository, world = build_service()
    await seed_batch(
        repository,
        ["첫 소재", "둘째 소재", "셋째 소재"],
        job_statuses=[
            ScheduledJobStatus.COMPLETED,
            ScheduledJobStatus.WAITING,
            ScheduledJobStatus.WAITING,
        ],
    )

    await service.delete_job("user_1", "job_1")

    from app.modules.scheduled_posting import ScheduledPostingWorker

    worker = ScheduledPostingWorker(service, repository)
    돌렸다 = await worker.run_next_job("batch_1")

    assert 돌렸다 is True
    # 워커가 집어 든 것은 셋째 소재다 — 지운 둘째 소재로는 글을 만들지 않는다.
    (body,) = world.args_of("create_blog_task")
    assert body["topic"] == "셋째 소재"
    assert world.count("create_blog_task") == 1
    assert (await repository.find_job("job_2")).status == ScheduledJobStatus.COMPLETED


async def test_삭제하면_total_count가_남은_작업_수로_줄어든다():
    """화면의 '3건 중 n건'이 이 값을 읽는다. 안 줄이면 영영 못 채우는 목표가 남는다."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["첫 소재", "둘째 소재", "셋째 소재"])

    view = await service.delete_job("user_1", "job_1")

    assert view.batch.total_count == 2
    assert len(view.jobs) == 2
    assert (await repository.find_batch("batch_1")).total_count == 2


async def test_삭제하면_완료_실패_취소_개수를_남은_작업에서_다시_센다():
    """하나씩 빼고 더하면 언젠가 어긋난다 — 남은 작업에서 다시 세는지 본다.

    일부러 틀린 집계를 심어 두고, 삭제 뒤 값이 **실제 남은 작업과 맞는지**로 판정한다.
    감산 방식이면 심어 둔 엉뚱한 값이 그대로 남는다.
    """
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["끝난 소재", "실패한 소재", "취소된 소재", "남은 소재"],
        job_statuses=[
            ScheduledJobStatus.COMPLETED,
            ScheduledJobStatus.FAILED,
            ScheduledJobStatus.CANCELED,
            ScheduledJobStatus.WAITING,
        ],
    )
    await _batch_counts(repository, completed_count=5, failed_count=9, canceled_count=7)

    view = await service.delete_job("user_1", "job_1")  # 실패한 소재를 뺀다

    assert view.batch.completed_count == 1
    assert view.batch.failed_count == 0  # 유일한 실패 작업을 뺐다
    assert view.batch.canceled_count == 1
    assert view.batch.total_count == 3


async def test_실행_중인_작업은_삭제할_수_없고_그대로_남는다():
    """도는 중인 LLM·Selenium을 버리면 네이버에 올라갔는지 알 수 없는 글이 남는다.

    거부만으로는 부족하다 — 거부해 놓고 문서를 지우면 워커가 끝낸 결과를 적을 곳이 없다.
    """
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["첫 소재", "지금 쓰는 중인 소재", "셋째 소재"],
        job_statuses=[
            ScheduledJobStatus.COMPLETED,
            ScheduledJobStatus.RUNNING,
            ScheduledJobStatus.WAITING,
        ],
    )

    with pytest.raises(BlogTaskError) as caught:
        await service.delete_job("user_1", "job_1")

    assert caught.value.code == "VALIDATION_FAILED"
    assert "글을 쓰거나 발행하는 중" in caught.value.message
    # 작업이 그대로 남아 있어야 한다.
    남은 = await repository.find_job("job_1")
    assert 남은 is not None
    assert 남은.status == ScheduledJobStatus.RUNNING
    assert len(await repository.list_jobs("batch_1")) == 3
    # 집계도 손대지 않았다.
    assert (await repository.find_batch("batch_1")).total_count == 3


async def test_발행_중인_작업도_삭제할_수_없다():
    """PUBLISHING이 가장 위험하다 — 크롬이 네이버에 글을 올리는 중이다."""
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["발행 중인 소재", "다음 소재"],
        job_statuses=[ScheduledJobStatus.PUBLISHING, ScheduledJobStatus.WAITING],
    )

    with pytest.raises(BlogTaskError) as caught:
        await service.delete_job("user_1", "job_0")

    assert caught.value.code == "VALIDATION_FAILED"
    남은 = await repository.find_job("job_0")
    assert 남은 is not None
    assert 남은.status == ScheduledJobStatus.PUBLISHING


async def test_이미_발행된_작업도_내역에서_지울_수_있다():
    """발행 내역을 **직접 정리**할 수 있어야 한다(2026-08-06 사용자 요청).

    예전에는 완료된 작업의 삭제를 거절했다. 그러면 몇 주치 기록이 쌓인 뒤 화면을
    정리할 방법이 아예 없다 — 실제로 22건이 한 화면에 깔려 "뭘 확인하라는건지
    모르겠다"는 신고가 됐다.

    지워지는 것은 **예약 기록 한 줄**뿐이다. 아래 테스트가 원고까지 남는 것을 본다.
    """
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["이미 올린 소재", "다음 소재"],
        job_statuses=[ScheduledJobStatus.COMPLETED, ScheduledJobStatus.WAITING],
    )

    view = await service.delete_job("user_1", "job_0")

    assert await repository.find_job("job_0") is None
    assert [job.job_id for job in view.jobs] == ["job_1"]
    assert view.batch.completed_count == 0
    assert view.batch.total_count == 1


async def test_발행된_작업을_내역에서_지워도_원고는_남는다():
    """게시물은 네이버에 그대로 있다. 그 원고까지 지우면 '내 글 목록'에서도 사라져,
    무엇이 올라갔는지 확인할 길이 없어진다 — 기록 한 줄만 지운다."""
    service, repository, world = build_service()
    world.tasks["post_올림"] = make_task(
        post_id="post_올림",
        status=BlogTaskStatus.POSTED,
        posting_logs=[make_posting_log()],
    )
    await seed_batch(
        repository,
        ["이미 올린 소재", "다음 소재"],
        post_ids=["post_올림", None],
        job_statuses=[ScheduledJobStatus.COMPLETED, ScheduledJobStatus.WAITING],
    )

    await service.delete_job("user_1", "job_0")

    assert await repository.find_job("job_0") is None
    assert "post_올림" in world.tasks


async def test_실패_인증대기_취소된_작업은_삭제된다():
    """더 진행하지 않는(또는 사람 손을 기다리는) 작업은 목록에서 빼는 것이 사용자가
    원하는 일이다. 발행된 글이 없으므로 지워도 잃을 것이 없다."""
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["실패한 소재", "인증이 필요한 소재", "취소된 소재", "남은 소재"],
        job_statuses=[
            ScheduledJobStatus.FAILED,
            ScheduledJobStatus.NEEDS_HUMAN,
            ScheduledJobStatus.CANCELED,
            ScheduledJobStatus.WAITING,
        ],
    )

    await service.delete_job("user_1", "job_0")
    await service.delete_job("user_1", "job_1")
    view = await service.delete_job("user_1", "job_2")

    assert [job.job_id for job in view.jobs] == ["job_3"]
    assert view.jobs[0].status == ScheduledJobStatus.WAITING
    assert view.batch.total_count == 1
    assert view.batch.failed_count == 0
    assert view.batch.canceled_count == 0


async def test_다른_사용자의_작업은_찾을_수_없다():
    """jobId를 알아도 남의 예약을 지울 수 없다. 권한 없음이 아니라 없는 것으로 답한다 —
    존재 여부 자체를 흘리지 않는다."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["첫 소재", "둘째 소재"], user_id="user_1")

    with pytest.raises(BlogTaskError) as caught:
        await service.delete_job("user_2", "job_1")

    assert caught.value.code == "NOT_FOUND"
    # 주인의 작업은 멀쩡하다.
    assert await repository.find_job("job_1") is not None
    assert len(await repository.list_jobs("batch_1")) == 2


async def test_마지막_하나를_빼면_배치가_닫혀_활성_배치가_사라진다():
    """빈 배치를 열어 두면 활성 배치로 남아 **다음 예약을 시작하지 못한다**
    (start_batch가 남은 배치를 보고 갈아타기·거부를 판단한다)."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["하나뿐인 소재"])

    view = await service.delete_job("user_1", "job_0")

    assert view.jobs == []
    assert view.batch.total_count == 0
    assert view.batch.status == ScheduledBatchStatus.STOPPED
    assert view.batch.status not in ACTIVE_BATCH_STATUSES
    assert view.batch.completed_at is not None
    assert view.batch.current_job_id is None
    # 활성 배치가 없어야 새 예약을 시작할 수 있다.
    assert await repository.find_active_batch("user_1") is None


async def test_삭제해도_target_count가_남은_작업_수를_넘지_않는다():
    """목표가 남은 작업보다 크면 배치가 영영 끝나지 않는다."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["첫 소재", "둘째 소재", "셋째 소재"])
    assert (await repository.find_batch("batch_1")).target_count == 3

    view = await service.delete_job("user_1", "job_1")

    assert view.batch.target_count == 2
    assert view.batch.target_count <= view.batch.total_count


async def test_target_count가_이미_작으면_삭제가_키우지_않는다():
    """min이지 대입이 아니다 — 사용자가 3건 중 1건만 쓰기로 했으면 그대로 1건이다."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["첫 소재", "둘째 소재", "셋째 소재"])
    await _batch_counts(repository, target_count=1)

    view = await service.delete_job("user_1", "job_1")

    assert view.batch.target_count == 1
    assert view.batch.total_count == 2


async def test_없는_작업을_지우면_NOT_FOUND다():
    """화면이 오래된 목록을 들고 있다가 이미 사라진 작업을 지우려는 경우다."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["첫 소재"])

    with pytest.raises(BlogTaskError) as caught:
        await service.delete_job("user_1", "job_없음")

    assert caught.value.code == "NOT_FOUND"
    # 있던 작업은 그대로다.
    assert len(await repository.list_jobs("batch_1")) == 1


# --------------------------------------------- 소재 하나로 여러 편 (변종)


async def test_소재_하나_모드는_같은_소재로_글의_개수만큼_작업을_만든다(monkeypatch):
    """소재는 하나인데 글은 세 편 — 이 모드가 있는 이유 그 자체다."""
    naver_saved(monkeypatch, True)
    service, _repository, _ = build_service()

    view = await service.start_batch(
        "user_1",
        {
            "topics": ["한 가지 소재"],
            "targetCount": 3,
            "intervalSeconds": 600,
            "topicMode": "single",
        },
    )

    assert view.batch.topic_mode is ScheduleTopicMode.SINGLE
    assert view.batch.total_count == 3
    assert [job.topic for job in view.jobs] == ["한 가지 소재"] * 3
    # 변종 번호가 0·1·2로 매겨져야 각도를 벌릴 수 있다.
    assert [job.variant_index for job in view.jobs] == [0, 1, 2]
    assert [job.sequence for job in view.jobs] == [0, 1, 2]


async def test_소재별_한_편_모드의_변종_번호는_전부_0이다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, _, _ = build_service()

    view = await service.start_batch(
        "user_1",
        {"topics": ["첫 소재", "둘째 소재"], "targetCount": 2, "intervalSeconds": 600},
    )

    assert view.batch.topic_mode is ScheduleTopicMode.MULTI
    assert [job.variant_index for job in view.jobs] == [0, 0]


def test_변종마다_다른_트렌드_키워드를_고른다():
    """같은 키워드로 N번 쓰면 N편이 서로 닮는다. 변종마다 다음 키워드를 집는다."""
    키워드 = [
        make_trend_keyword("첫 키워드"),
        make_trend_keyword("둘째 키워드"),
        make_trend_keyword("셋째 키워드"),
    ]

    assert pick_trend_keyword(키워드, 0).keyword == "첫 키워드"
    assert pick_trend_keyword(키워드, 1).keyword == "둘째 키워드"
    assert pick_trend_keyword(키워드, 2).keyword == "셋째 키워드"
    # 키워드가 변종 수보다 적으면 돌려 쓴다(그때는 제목 배제가 차이를 만든다).
    assert pick_trend_keyword(키워드, 3).keyword == "첫 키워드"


def test_자격_미달_키워드는_변종_번호를_셀_때도_빠진다():
    """자격 없는 것을 세면 변종 1이 '없는 자리'를 가리켜 각도가 겹친다."""
    키워드 = [
        make_trend_keyword("자격 없음", is_eligible=False),
        make_trend_keyword("첫 키워드"),
        make_trend_keyword("둘째 키워드"),
    ]

    assert pick_trend_keyword(키워드, 0).keyword == "첫 키워드"
    assert pick_trend_keyword(키워드, 1).keyword == "둘째 키워드"
    assert pick_trend_keyword(키워드, 2).keyword == "첫 키워드"


async def test_같은_소재의_앞선_글_제목을_제목_생성에서_배제한다():
    """제목이 겹치면 '여러 편'이 아니라 같은 글 여러 개다.

    새 프롬프트를 만들지 않고 제목 생성기가 이미 받는 excludeTitles에 넘긴다.
    """
    service, repository, world = build_service()
    await seed_batch(repository, ["같은 소재", "같은 소재"], post_ids=["post_done", None])

    # 앞선 작업은 이미 제목을 골라 뒀다.
    world.put(
        make_task(
            post_id="post_done",
            status=BlogTaskStatus.POSTED,
            trend_selection=TrendSelection(
                final_topic="앞선 글이 쓴 제목",
                selected_trend_keyword_ids=[],
                skipped=False,
                selected_at=now_iso(),
            ),
        )
    )
    # 둘째 작업의 변종 번호를 1로 올려 둔다.
    두번째 = (await repository.list_jobs("batch_1"))[1]
    await repository.save_job(두번째.model_copy(update={"variant_index": 1}))

    await service.execute_job("job_1")

    body = world.args_of("generate_topics")[1]
    assert "앞선 글이 쓴 제목" in body["excludeTitles"]
    # 제목 생성기가 이번 회차의 방향을 고르는 값에도 변종 번호가 실린다.
    assert body["regenerationCount"] == 1


# --- 글을 지우면 그 글을 가리키던 예약 기록도 사라진다 (2026-08-06 신고) ---


async def test_글을_지우면_그_글의_예약_기록도_사라진다():
    """내 글 목록을 전부 비웠는데 발행 내역에 예전 줄이 그대로 남아 있었다.

    그 줄이 화면에 적는 제목·상태·발행 주소는 전부 그 글에서 읽어 오는 값이라,
    글이 없으면 아무것도 설명하지 못한다.
    """
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["지울 소재", "남을 소재"],
        post_ids=["post_gone", "post_kept"],
        job_statuses=[ScheduledJobStatus.COMPLETED, ScheduledJobStatus.COMPLETED],
    )

    touched = await service.forget_post("user_1", "post_gone")

    assert touched == 1
    remaining = await repository.list_user_jobs("user_1")
    assert [job.post_id for job in remaining] == ["post_kept"]


async def test_예약_기록을_지우면_배치_집계를_다시_센다():
    service, repository, _ = build_service()
    await seed_batch(
        repository,
        ["지울 소재", "남을 소재"],
        post_ids=["post_gone", "post_kept"],
        job_statuses=[ScheduledJobStatus.COMPLETED, ScheduledJobStatus.FAILED],
    )
    await _batch_counts(repository, completed_count=9, failed_count=9)

    await service.forget_post("user_1", "post_gone")

    batch = await repository.find_batch("batch_1")
    assert batch.completed_count == 0  # 유일한 완료 작업이 그 글이었다
    assert batch.failed_count == 1
    assert batch.total_count == 1


async def test_남의_글은_지우지_않는다():
    """postId가 같아도 다른 사용자의 기록은 건드리지 않는다(소유자를 쿼리에 넣는다)."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["내 소재"], post_ids=["post_x"], user_id="user_1")

    touched = await service.forget_post("user_2", "post_x")

    assert touched == 0
    assert len(await repository.list_user_jobs("user_1")) == 1


async def test_그_글의_예약_기록이_없으면_아무_일도_하지_않는다():
    """새 글 작성으로 만든 글을 지우는 흔한 경우다 — 예약과 무관하다."""
    service, repository, _ = build_service()
    await seed_batch(repository, ["예약 소재"], post_ids=["post_scheduled"])

    assert await service.forget_post("user_1", "post_never_scheduled") == 0
    assert len(await repository.list_user_jobs("user_1")) == 1


class TestSeveralDraftsFromOneTopic:
    """한 소재로 여러 편(2026-08-12). 검증에서 고른 방향 수만큼 글이 줄지어 걸린다.

    편마다 시각을 받지 않는다 — 뒤 편은 앞 편이 끝나야 시작한다(worker의 문지기).
    """

    @staticmethod
    def _prepared(world, *, run_at: str = "2026-08-13T09:00:00Z"):
        return world.put(
            make_task(
                post_id="post_1",
                status=BlogTaskStatus.READY_TO_PUBLISH,
                input=BlogTaskInput(
                    topic="소재",
                    keywords=[],
                    scheduled_run_at=run_at,
                    draft_count=3,
                ),
                trend_selection=TrendSelection(
                    topic_candidate_id=None,
                    final_topic="소재",
                    selected_trend_keyword_ids=[],
                    skipped=False,
                    selected_at=now_iso(),
                ),
            )
        )

    async def test_one_direction_makes_one_job(self, monkeypatch):
        """방향을 하나만 골랐으면 예전 그대로다 — 줄을 세우지 않는다."""
        service, repository, world = build_service()
        naver_saved(monkeypatch, True)
        self._prepared(world)

        view = await service.schedule_prepared_post("user_1", "post_1")

        jobs = await repository.list_jobs(view.batch.batch_id)
        assert len(jobs) == 1
        assert jobs[0].after_job_id is None

    async def test_three_directions_make_three_jobs_in_a_row(self, monkeypatch):
        service, repository, world = build_service()
        naver_saved(monkeypatch, True)
        self._prepared(world)

        view = await service.schedule_prepared_post(
            "user_1", "post_1", [{"intentId": "intent_b"}, {"intentId": "intent_c"}]
        )

        jobs = sorted(await repository.list_jobs(view.batch.batch_id), key=lambda j: j.sequence)
        assert len(jobs) == 3
        # 첫 편만 시각을 보고, 나머지는 바로 앞 편을 가리킨다.
        assert jobs[0].after_job_id is None
        assert jobs[1].after_job_id == jobs[0].job_id
        assert jobs[2].after_job_id == jobs[1].job_id

    async def test_each_draft_is_its_own_post(self, monkeypatch):
        """편마다 자기 방향을 가진 글이 따로 있어야 한다 — 한 글에 원고가 셋일 수 없다."""
        service, repository, world = build_service()
        naver_saved(monkeypatch, True)
        self._prepared(world)

        view = await service.schedule_prepared_post(
            "user_1", "post_1", [{"intentId": "intent_b"}, {"intentId": "intent_c"}]
        )

        jobs = await repository.list_jobs(view.batch.batch_id)
        post_ids = {job.post_id for job in jobs}
        assert len(post_ids) == 3
        assert "post_1" in post_ids  # 원본이 첫 편이다

    async def test_the_log_says_how_many_are_coming(self, monkeypatch):
        """작업 큐만 보고도 몇 편이 어떤 순서로 도는지 알 수 있어야 한다."""
        service, repository, world = build_service()
        naver_saved(monkeypatch, True)
        self._prepared(world)

        view = await service.schedule_prepared_post("user_1", "post_1", [{"intentId": "intent_b"}])

        batch = await repository.find_batch(view.batch.batch_id)
        assert "2편" in batch.logs[0].message
        assert "이어서" in batch.logs[0].message

    async def test_the_chosen_direction_travels_to_the_clone(self, monkeypatch):
        """편마다 고른 방향이 복제본까지 그대로 가야 한다(2026-08-12 사용자 신고).

        자리번호(intentId)만 보내던 동안, 앞 편의 번호가 마지막 편의 후보로 조용히
        해석돼 고르지도 않은 방향으로 원고가 만들어질 수 있었다.
        """
        service, _repository, world = build_service()
        naver_saved(monkeypatch, True)
        self._prepared(world)
        chosen = IntentCandidate(
            intent_id="intent_b",
            title="2편째가 고른 방향",
            target_reader="독자",
            rationale="근거",
            keywords=[],
            sources=[],
        )

        await service.schedule_prepared_post(
            "user_1", "post_1", [{"intentId": "intent_b", "intent": chosen}]
        )

        handed = [args[0] for name, args in world.calls if name == "clone_for_direction"]
        assert handed[0][2] is chosen


class TestEachRegistrationIsOneSeries:
    """'1편째·2편째'는 **한 번에 건 묶음** 안에서 센다(2026-08-13 사용자 지적).

        "지금 한번에 예약한 편끼리 1편2편3편으로 편수가 늘어나는게 아니라 지금까지 했던
         작업들 전부 합쳐셔 편수로 해뒀네."

    이 경로는 돌고 있는 배치에 계속 붙으므로 배치도 소재도 묶음이 아니다 — 등록 한 번이
    한 묶음이고, 그것을 seriesId가 들고 있다.
    """

    @staticmethod
    def _prepared(world, post_id: str = "post_1"):
        return world.put(
            make_task(
                post_id=post_id,
                status=BlogTaskStatus.READY_TO_PUBLISH,
                input=BlogTaskInput(topic="소재", keywords=[], draft_count=2),
                trend_selection=TrendSelection(
                    topic_candidate_id=None,
                    final_topic="소재",
                    selected_trend_keyword_ids=[],
                    skipped=False,
                    selected_at=now_iso(),
                ),
            )
        )

    async def test_한_번에_건_편들은_같은_묶음이다(self, monkeypatch):
        service, repository, world = build_service()
        naver_saved(monkeypatch, True)
        self._prepared(world)

        view = await service.schedule_prepared_post(
            "user_1", "post_1", [{"intentId": "intent_b"}]
        )

        jobs = await repository.list_jobs(view.batch.batch_id)
        assert len(jobs) == 2
        assert jobs[0].series_id is not None
        assert len({job.series_id for job in jobs}) == 1

    async def test_다음_등록은_다른_묶음이다(self, monkeypatch):
        """같은 배치에 붙어도 묶음은 갈린다 — 이것이 '6편째'를 만들던 자리다."""
        service, repository, world = build_service()
        naver_saved(monkeypatch, True)
        self._prepared(world)
        first = await service.schedule_prepared_post("user_1", "post_1")
        self._prepared(world, "post_2")

        second = await service.schedule_prepared_post("user_1", "post_2")

        # 같은 배치에 붙었는지부터 확인한다 — 그래야 이 테스트가 뜻이 있다.
        assert first.batch.batch_id == second.batch.batch_id
        jobs = await repository.list_jobs(second.batch.batch_id)
        assert len(jobs) == 2
        assert len({job.series_id for job in jobs}) == 2

    async def test_돌고_있는_배치에_붙인_예약이_실제로_저장된다(self, monkeypatch):
        """2026-08-13에 찾은 버그. 덧붙이는 경로가 `save_job`을 불렀는데 그쪽은 일부러
        upsert하지 않아(삭제한 작업이 되살아나는 것을 막는 장치) **아무것도 저장되지
        않았다.** 활성 배치가 있으면 새 글 작성에서 건 예약이 조용히 사라졌다."""
        service, repository, world = build_service()
        naver_saved(monkeypatch, True)
        self._prepared(world)
        first = await service.schedule_prepared_post("user_1", "post_1")
        self._prepared(world, "post_2")

        second = await service.schedule_prepared_post("user_1", "post_2")

        assert first.batch.batch_id == second.batch.batch_id
        jobs = await repository.list_jobs(second.batch.batch_id)
        assert [job.post_id for job in jobs] == ["post_1", "post_2"]
        # 순번도 이어져야 한다 — 화면의 표와 워커의 실행 순서가 이 값을 따른다.
        assert [job.sequence for job in jobs] == [0, 1]
        assert (await repository.find_batch(second.batch.batch_id)).total_count == 2

    async def test_자동_포스팅은_소재마다_한_묶음이다(self, monkeypatch):
        """한 소재로 여러 편(SINGLE)이 한 묶음이다. 소재별 한 편은 어차피 표시가 없다."""
        naver_saved(monkeypatch, True)
        service, repository, _ = build_service()

        view = await service.start_batch(
            "user_1",
            {"topics": ["소재"], "topicMode": "single", "targetCount": 3, "intervalSeconds": 900},
        )

        jobs = await repository.list_jobs(view.batch.batch_id)
        assert len(jobs) == 3
        assert len({job.series_id for job in jobs}) == 1


class TestImmediateJobsAreMarked:
    """'지금 바로'로 건 작업에 표시를 남긴다(2026-08-13).

    그런 작업도 publish_at은 채워진다 — 걸린 시각이 들어간다. 그래서 값만 보면 예약과
    구별되지 않고, 발행 순서(즉시가 먼저)를 정할 수 없다.
    """

    @staticmethod
    def _prepared(world, *, run_at: str | None):
        return world.put(
            make_task(
                post_id="post_1",
                status=BlogTaskStatus.READY_TO_PUBLISH,
                input=BlogTaskInput(topic="소재", keywords=[], scheduled_run_at=run_at),
                trend_selection=TrendSelection(
                    topic_candidate_id=None,
                    final_topic="소재",
                    selected_trend_keyword_ids=[],
                    skipped=False,
                    selected_at=now_iso(),
                ),
            )
        )

    async def test_시각을_비우면_즉시_표시가_켜진다(self, monkeypatch):
        service, repository, world = build_service()
        naver_saved(monkeypatch, True)
        self._prepared(world, run_at=None)

        view = await service.schedule_prepared_post("user_1", "post_1")

        job = (await repository.list_jobs(view.batch.batch_id))[0]
        assert job.starts_immediately is True
        # 표시만 다르다 — 시각 자체는 예전처럼 채워 둔다(화면이 그 값을 보여 준다).
        assert job.publish_at is not None

    async def test_시각을_고르면_즉시_표시가_꺼진다(self, monkeypatch):
        service, repository, world = build_service()
        naver_saved(monkeypatch, True)
        self._prepared(world, run_at="2026-08-13T09:00:00Z")

        view = await service.schedule_prepared_post("user_1", "post_1")

        job = (await repository.list_jobs(view.batch.batch_id))[0]
        assert job.starts_immediately is False

    async def test_나중에_시각을_주면_즉시_표시가_사라진다(self, monkeypatch):
        """예약작업 관리에서 시각을 정해 주면 그때부터는 약속이 있는 작업이다."""
        service, repository, world = build_service()
        naver_saved(monkeypatch, True)
        self._prepared(world, run_at=None)
        view = await service.schedule_prepared_post("user_1", "post_1")
        job = (await repository.list_jobs(view.batch.batch_id))[0]

        await service.reschedule_job(
            "user_1", job.job_id, {"publishAt": "2026-08-20T09:00:00Z"}
        )

        assert (await repository.find_job(job.job_id)).starts_immediately is False
