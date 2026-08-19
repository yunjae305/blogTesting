"""service.test.ts."""

from datetime import datetime, timezone

import asyncio

import pytest

from app.errors import BlogTaskError
from app.modules.blog_task.repository import InMemoryBlogTaskRepository
from app.modules.blog_task.service import BlogTaskService
from app.posting import PublishResult
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
    TrendSelection,
    WebSearchAnalysisInput,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SucceedingPostingWorker:
    async def publish(self, job):
        return PublishResult(
            result=PostingResultStatus.SUCCESS,
            post_url=(
                f"https://blog.example.com/{job.post_id}"
                if job.method == PostingMethod.AUTO
                else None
            ),
        )


class SucceedingWebSearchAnalyzer:
    async def search_and_analyze(self, analysis_input, on_collected=None):
        return IntentValidationResult(
            prompt_version=analysis_input.prompt_version,
            provider="mock",
            model="mock-web-search-analyzer",
            analyzed_at=_now(),
            intent_candidates=[
                IntentCandidate(
                    intent_id=f"intent_{n}",
                    title=f"Intent {n}",
                    target_reader=f"Reader {n}",
                    rationale=f"Rationale {n}",
                    keywords=analysis_input.input.keywords,
                    sources=[],
                )
                for n in (1, 2, 3)
            ],
        )


def build_service(
    repository=None, posting_worker=None, web_search_analyzer=None, threads_writer=None
):
    return BlogTaskService(
        repository=repository or InMemoryBlogTaskRepository(),
        posting_worker=posting_worker or SucceedingPostingWorker(),
        web_search_analyzer=web_search_analyzer or SucceedingWebSearchAnalyzer(),
        threads_writer=threads_writer,
    )


VALID_REQUEST = {
    "userId": "user_1",
    "topic": "topic",
    "keywords": ["k1"],
    "referenceMaterials": [{"type": "URL", "value": "https://example.com"}],
}


def build_ready_task() -> BlogTask:
    now = _now()
    return BlogTask(
        post_id="post_ready",
        user_id="user_1",
        status=BlogTaskStatus.READY_TO_PUBLISH,
        version=1,
        created_at=now,
        updated_at=now,
        status_history=[],
        input=BlogTaskInput(topic="topic", keywords=["k1"], reference_materials=[]),
        final_post=FinalPost(
            title="Final title",
            body="Final body",
            hashtags=["blogit"],
            html_content="<h1>Final title</h1><p>Final body</p>",
        ),
        posting_logs=[],
    )


async def test_create_stores_the_task_and_advances_to_reference_processing():
    service = build_service()
    task = await service.create_blog_task(VALID_REQUEST)

    assert task.status == BlogTaskStatus.REFERENCE_PROCESSING
    assert task.user_id == "user_1"
    assert len(task.status_history) == 1

    fetched = await service.get_blog_task(task.post_id)
    assert fetched.post_id == task.post_id


async def test_editing_the_input_rewinds_the_post_and_drops_what_was_derived_from_it():
    """수정하기 sends the user back to the 소재 step. The trends and the intent were
    worked out for the old topic, so they cannot be carried over to the new one."""
    service = build_service()
    task = await service.create_blog_task(VALID_REQUEST)
    analyzed = await service.analyze_intent_candidates(task.post_id)
    assert analyzed.intent_validation_result is not None

    edited = await service.update_blog_task_input(
        task.post_id, {"topic": "새 소재", "keywords": ["k2"]}
    )

    assert edited.input.topic == "새 소재"
    assert edited.intent_validation_result is None
    assert edited.selected_intent is None
    assert edited.trend_selection is None
    # Straight back through reference intake, exactly as a fresh post would go.
    assert edited.status == BlogTaskStatus.REFERENCE_PROCESSING
    assert edited.status_history[-1].to == BlogTaskStatus.REFERENCE_PROCESSING
    assert edited.status_history[-2].to == BlogTaskStatus.INPUT


async def test_editing_the_input_is_refused_once_a_draft_exists():
    repository = InMemoryBlogTaskRepository()
    await repository.create(build_ready_task())
    service = build_service(repository=repository)

    with pytest.raises(BlogTaskError) as error:
        await service.update_blog_task_input("post_ready", {"topic": "새 소재", "keywords": ["k2"]})

    assert error.value.code == "INVALID_STATUS_TRANSITION"


async def test_editing_the_input_rejects_a_body_that_would_not_pass_creation():
    service = build_service()
    task = await service.create_blog_task(VALID_REQUEST)

    with pytest.raises(BlogTaskError) as error:
        await service.update_blog_task_input(task.post_id, {"topic": "  ", "keywords": ["k2"]})

    assert error.value.code == "VALIDATION_FAILED"


async def test_m3_starts_in_the_background_and_reports_both_of_its_halves():
    """M3 is about a minute: a search call, then a summary call that reads what the
    search found. The request returns while that is still running, and the two halves
    are reported separately so the client is not staring at one frozen label."""
    seen: list[tuple[int, str]] = []

    class ReportingAnalyzer(SucceedingWebSearchAnalyzer):
        async def search_and_analyze(self, analysis_input, on_collected=None):
            task = await repository.find_by_post_id(analysis_input.post_id)
            seen.append((task.progress.step, task.progress.label))
            if on_collected:
                await on_collected()
            task = await repository.find_by_post_id(analysis_input.post_id)
            seen.append((task.progress.step, task.progress.label))
            return await super().search_and_analyze(analysis_input, on_collected=None)

    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository, web_search_analyzer=ReportingAnalyzer())
    created = await service.create_blog_task(VALID_REQUEST)

    started = await service.start_intent_analysis(created.post_id)
    assert started.status == BlogTaskStatus.SEARCH_ANALYZING
    assert started.intent_validation_result is None

    await service._jobs.drain()

    # 라벨은 단계 이름 그대로가 아니라 '지금 무엇을 하는 중' 내레이션이다(2026-08-07).
    # 이 테스트의 계약은 **두 반쪽이 서로 다른 단계 번호로 따로 보고되는가**다.
    assert [step for step, _ in seen] == [1, 2]
    assert all(label for _, label in seen)

    finished = await repository.find_by_post_id(created.post_id)
    assert finished.intent_validation_result is not None
    assert finished.progress is None


async def test_create_with_no_reference_materials_still_advances_past_input():
    service = build_service()
    body = {k: v for k, v in VALID_REQUEST.items() if k != "referenceMaterials"}

    task = await service.create_blog_task(body)

    assert task.status == BlogTaskStatus.REFERENCE_PROCESSING


async def test_each_create_request_makes_its_own_task():
    """글 시작은 멱등하지 않다 — 요청 하나가 글 하나다.

    예전에는 멱등성 키로 재요청을 접었지만, 클라이언트가 요청마다 새 키를 만들어
    실제로는 한 번도 접힌 적이 없었다. 중복 클릭은 버튼 비활성화가 막는다.
    """
    service = build_service()
    first = await service.create_blog_task(VALID_REQUEST)
    second = await service.create_blog_task({**VALID_REQUEST, "userId": "user_2"})

    assert second.post_id != first.post_id
    assert second.user_id == "user_2"


async def test_create_rejects_invalid_input_before_creating_a_task():
    service = build_service()

    with pytest.raises(BlogTaskError):
        await service.create_blog_task({"userId": "user_1"})


async def test_saving_the_subject_costs_no_model_call_even_with_a_reference_url():
    """M1 ran a model per reference material and threw the answer away — nobody read
    it. Saving the 소재 waited on that, and a URL the model choked on took the whole
    post down: REFERENCE_PROCESSING -> FAILED, and the 제목 step then answered 409.

    A URL is checked for being a URL at validation. What is *in* it is M3's job."""
    service = build_service()

    task = await service.create_blog_task(
        {
            "userId": "user_1",
            "topic": "AIONA",
            "keywords": ["k1"],
            "referenceMaterials": [{"type": "URL", "value": "https://aiona.kr/"}],
        },
    )

    assert task.status == BlogTaskStatus.REFERENCE_PROCESSING
    assert task.input.reference_materials[0].value == "https://aiona.kr/"


async def test_publish_copies_a_ready_final_post_and_records_a_posting_log():
    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository)
    await repository.create(build_ready_task())

    published = await service.publish_blog_task("post_ready", {"method": "copy"})

    assert published.status == BlogTaskStatus.POSTED
    assert len(published.posting_logs) == 1
    assert published.posting_logs[0].method == PostingMethod.COPY
    assert published.posting_logs[0].result == PostingResultStatus.SUCCESS


async def test_publish_auto_stores_the_post_url():
    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository)
    await repository.create(build_ready_task())

    published = await service.publish_blog_task("post_ready", {"method": "auto"})

    assert published.status == BlogTaskStatus.POSTED
    assert published.posting_logs[0].post_url == "https://blog.example.com/post_ready"


async def test_publish_draft_returns_to_ready_and_records_the_draft_action():
    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository)
    await repository.create(build_ready_task())

    saved = await service.publish_blog_task("post_ready", {"method": "draft"})

    assert saved.status == BlogTaskStatus.READY_TO_PUBLISH
    assert len(saved.posting_logs) == 1
    assert saved.posting_logs[0].method == PostingMethod.DRAFT
    assert saved.posting_logs[0].result == PostingResultStatus.SUCCESS
    assert saved.posting_logs[0].post_url is None


async def test_auto_publish_retries_a_legacy_copy_completed_post():
    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository)
    ready = build_ready_task()
    legacy_copy = ready.model_copy(
        update={
            "status": BlogTaskStatus.POSTED,
            "posting_logs": [
                PostingLog(
                    log_id="log_copy",
                    post_id=ready.post_id,
                    user_id=ready.user_id,
                    method=PostingMethod.COPY,
                    result=PostingResultStatus.SUCCESS,
                    created_at=_now(),
                )
            ],
        }
    )
    await repository.create(legacy_copy)

    published = await service.publish_blog_task(
        ready.post_id, {"method": "auto"})

    assert published.status == BlogTaskStatus.POSTED
    assert [log.method for log in published.posting_logs] == [
        PostingMethod.COPY,
        PostingMethod.AUTO,
    ]
    assert published.posting_logs[-1].post_url == "https://blog.example.com/post_ready"


async def test_auto_publish_does_not_duplicate_an_already_auto_published_post():
    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository)
    ready = build_ready_task()
    already_published = ready.model_copy(
        update={
            "status": BlogTaskStatus.POSTED,
            "posting_logs": [
                PostingLog(
                    log_id="log_auto",
                    post_id=ready.post_id,
                    user_id=ready.user_id,
                    method=PostingMethod.AUTO,
                    result=PostingResultStatus.SUCCESS,
                    post_url="https://blog.naver.com/saved/1",
                    created_at=_now(),
                )
            ],
        }
    )
    await repository.create(already_published)

    with pytest.raises(BlogTaskError):
        await service.publish_blog_task(
            ready.post_id, {"method": "auto"})


async def test_publish_threads_routes_the_channel_to_the_worker():
    class CapturingWorker:
        def __init__(self):
            self.jobs = []

        async def publish(self, job):
            self.jobs.append(job)
            return PublishResult(
                result=PostingResultStatus.SUCCESS,
                post_url="https://www.threads.net/@u/post/1",
            )

    repository = InMemoryBlogTaskRepository()
    worker = CapturingWorker()
    service = build_service(repository=repository, posting_worker=worker)
    await repository.create(build_ready_task())

    published = await service.publish_blog_task(
        "post_ready", {"method": "auto", "channel": "threads"}
    )

    assert worker.jobs[0].channel == PostingChannel.THREADS
    assert published.status == BlogTaskStatus.POSTED
    assert published.posting_logs[0].channel == PostingChannel.THREADS


class CapturingWorker:
    def __init__(self):
        self.jobs = []

    async def publish(self, job):
        self.jobs.append(job)
        return PublishResult(result=PostingResultStatus.SUCCESS)


async def test_threads_publish_writes_a_threads_native_post():
    """스레드 발행은 블로그 원고를 자르는 게 아니라 스레드 문법의 글을 새로 쓴다.

    2026-08-06 사용자 요청으로 되살린 규칙이다("500자로 자르는 게 아니라 쓰레드 단일
    글 생성 방식으로"). 새 글 발행과 예약 발행이 **같은 이 경로**를 지난다.
    """

    class FakeThreadsWriter:
        def __init__(self):
            self.tasks = []
            self.lengths = []

        async def generate_threads_post(self, task, article_length=None):
            self.tasks.append(task)
            self.lengths.append(article_length)
            return ["첫 줄이 훅이다.", "본문 문단.", "마지막은 정리."]

    repository = InMemoryBlogTaskRepository()
    worker = CapturingWorker()
    writer = FakeThreadsWriter()
    service = build_service(
        repository=repository, posting_worker=worker, threads_writer=writer
    )
    await repository.create(build_ready_task())

    published = await service.publish_blog_task(
        "post_ready", {"method": "auto", "channel": "threads"}
    )

    assert published.status == BlogTaskStatus.POSTED
    assert writer.tasks[0].post_id == "post_ready"
    # 순서가 곧 게시 순서다 — 목록 그대로 발행 잡에 실린다.
    assert worker.jobs[0].threads_texts == ["첫 줄이 훅이다.", "본문 문단.", "마지막은 정리."]


async def test_a_failed_threads_draft_blocks_publishing_and_keeps_the_post():
    """생성 실패는 조용한 폴백이 아니다 — 발행이 멈추고 글은 READY_TO_PUBLISH에 남는다."""

    class FailingWriter:
        async def generate_threads_post(self, task, article_length=None):
            raise RuntimeError("provider request failed with 500")

    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository, threads_writer=FailingWriter())
    await repository.create(build_ready_task())

    with pytest.raises(BlogTaskError) as caught:
        await service.publish_blog_task(
            "post_ready", {"method": "auto", "channel": "threads"}
        )

    assert caught.value.code == "THREADS_DRAFT_FAILED"
    remained = await repository.find_by_post_id("post_ready")
    assert remained.status == BlogTaskStatus.READY_TO_PUBLISH
    assert remained.posting_logs == []


async def test_naver_publish_never_calls_the_threads_writer():
    class ExplodingWriter:
        async def generate_threads_post(self, task, article_length=None):
            raise AssertionError("네이버 발행이 스레드 원고를 쓰면 안 됩니다.")

    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository, threads_writer=ExplodingWriter())
    await repository.create(build_ready_task())

    published = await service.publish_blog_task("post_ready", {"method": "auto"})

    assert published.status == BlogTaskStatus.POSTED
    assert published.posting_logs[0].channel == PostingChannel.NAVER


async def test_without_a_writer_the_article_itself_goes_to_the_worker():
    """생성기가 없는 구성(테스트·구형 조립)의 폴백. 발행기가 원고를 나눠 담는다."""
    repository = InMemoryBlogTaskRepository()
    worker = CapturingWorker()
    service = build_service(repository=repository, posting_worker=worker)
    await repository.create(build_ready_task())

    published = await service.publish_blog_task(
        "post_ready", {"method": "auto", "channel": "threads"}
    )

    assert published.status == BlogTaskStatus.POSTED
    job = worker.jobs[0]
    assert job.threads_texts is None
    assert job.final_post is not None


async def test_threads_supports_only_auto_publishing():
    """스레드에는 임시저장이 없다 — draft/copy와 threads의 조합은 요청부터 거절한다."""
    service = build_service()

    with pytest.raises(BlogTaskError):
        await service.publish_blog_task("post_x", {"method": "draft", "channel": "threads"})


async def test_a_threads_published_post_can_still_go_to_naver_but_not_threads_again():
    """중복 발행 가드는 채널별이다 — 스레드에 올린 글의 네이버 발행은 중복이 아니다."""
    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository)
    ready = build_ready_task()
    threads_published = ready.model_copy(
        update={
            "status": BlogTaskStatus.POSTED,
            "posting_logs": [
                PostingLog(
                    log_id="log_threads",
                    post_id=ready.post_id,
                    user_id=ready.user_id,
                    method=PostingMethod.AUTO,
                    channel=PostingChannel.THREADS,
                    result=PostingResultStatus.SUCCESS,
                    post_url="https://www.threads.net/@u/post/1",
                    created_at=_now(),
                )
            ],
        }
    )
    await repository.create(threads_published)

    with pytest.raises(BlogTaskError):
        await service.publish_blog_task(
            ready.post_id, {"method": "auto", "channel": "threads"}
        )

    published = await service.publish_blog_task(
        ready.post_id, {"method": "auto", "channel": "naver"}
    )

    assert published.status == BlogTaskStatus.POSTED
    assert published.posting_logs[-1].channel == PostingChannel.NAVER


async def test_publish_rejects_tasks_that_are_not_ready_to_publish():
    service = build_service()
    task = await service.create_blog_task(VALID_REQUEST)

    with pytest.raises(BlogTaskError):
        await service.publish_blog_task(task.post_id, {"method": "copy"})


async def test_publish_moves_to_needs_human_when_posting_requires_manual_auth():
    class NeedsHumanWorker:
        async def publish(self, job):
            return PublishResult(
                result=PostingResultStatus.NEEDS_HUMAN, error_message="2FA required"
            )

    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository, posting_worker=NeedsHumanWorker())
    await repository.create(build_ready_task())

    published = await service.publish_blog_task("post_ready", {"method": "auto"})

    assert published.status == BlogTaskStatus.POSTING_NEEDS_HUMAN
    assert published.posting_logs[0].error_message == "2FA required"


async def test_publish_moves_to_failed_when_the_posting_worker_raises():
    class FailingWorker:
        async def publish(self, job):
            raise RuntimeError("platform unavailable")

    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository, posting_worker=FailingWorker())
    await repository.create(build_ready_task())

    published = await service.publish_blog_task("post_ready", {"method": "auto"})

    assert published.status == BlogTaskStatus.FAILED
    assert published.posting_logs[0].result == PostingResultStatus.FAIL
    assert published.posting_logs[0].error_message == "platform unavailable"


async def test_analyze_stores_candidates_and_leaves_the_task_in_search_analyzing():
    service = build_service()
    task = await service.create_blog_task(VALID_REQUEST)

    analyzed = await service.analyze_intent_candidates(task.post_id)

    assert analyzed.status == BlogTaskStatus.SEARCH_ANALYZING
    assert analyzed.intent_validation_result.provider == "mock"
    assert analyzed.intent_validation_result.prompt_version == "m3-intent@v1.2"
    assert len(analyzed.intent_validation_result.intent_candidates) == 3


async def test_analyze_continues_from_m2_selection_without_repeating_the_transition():
    repository = InMemoryBlogTaskRepository()
    captured = {}

    class CapturingAnalyzer(SucceedingWebSearchAnalyzer):
        async def search_and_analyze(self, analysis_input, on_collected=None):
            captured["topic"] = analysis_input.input.topic
            captured["keywords"] = analysis_input.input.keywords
            captured["selected_keywords"] = analysis_input.selected_keywords
            return await super().search_and_analyze(analysis_input)

    service = build_service(repository=repository, web_search_analyzer=CapturingAnalyzer())
    task = await service.create_blog_task(VALID_REQUEST)

    await repository.save_trend_selection(
        task.post_id,
        TrendSelection(
            final_topic="M2 selected topic",
            selected_trend_keyword_ids=["trend_1"],
            skipped=False,
            selected_at=_now(),
        ),
        "system:m2-trend-selection",
    )

    analyzed = await service.analyze_intent_candidates(task.post_id)

    assert analyzed.status == BlogTaskStatus.SEARCH_ANALYZING
    assert captured["topic"] == "M2 selected topic"
    assert captured["keywords"] == ["k1"]
    # 키워드 문자열이 없는 옛 선택은 빈 목록으로 간다 — 예전과 같은 프롬프트가 나온다.
    assert captured["selected_keywords"] == []
    transitions = [
        e for e in analyzed.status_history if e.to == BlogTaskStatus.SEARCH_ANALYZING
    ]
    assert len(transitions) == 1


async def test_analyze_carries_the_selected_trend_keywords_into_the_search():
    """사용자가 M2에서 고른 검색 키워드가 M3 수집까지 닿아야 한다.

    소재 제목(final_topic)만 넘기면 수집이 일반 상위 결과에 머문다 — 키워드는 독자가
    실제로 검색한 표현이라 검색어의 중심이 되어야 한다(2026-08-04 사용자 요청).
    """
    repository = InMemoryBlogTaskRepository()
    captured = {}

    class CapturingAnalyzer(SucceedingWebSearchAnalyzer):
        async def search_and_analyze(self, analysis_input, on_collected=None):
            captured["selected_keywords"] = analysis_input.selected_keywords
            return await super().search_and_analyze(analysis_input)

    service = build_service(repository=repository, web_search_analyzer=CapturingAnalyzer())
    task = await service.create_blog_task(VALID_REQUEST)

    await repository.save_trend_selection(
        task.post_id,
        TrendSelection(
            final_topic="M2 selected topic",
            selected_trend_keyword_ids=["trend_1"],
            selected_keywords=["창섭 전과자", "델타포스 창섭"],
            skipped=False,
            selected_at=_now(),
        ),
        "system:m2-trend-selection",
    )

    await service.analyze_intent_candidates(task.post_id)

    assert captured["selected_keywords"] == ["창섭 전과자", "델타포스 창섭"]


async def test_m3_does_not_start_a_second_job_while_one_is_running():
    """이미 검증이 돌고 있으면 요청을 무시한다.

    M4·M5와 달리 M3은 상태로 막히지 않는다 — M2가 먼저 SEARCH_ANALYZING으로 옮겨 놓기
    때문에 상태 검사를 그대로 통과한다. 막지 않으면 '다시 검증'을 누를 때마다 검색·요약이
    한 벌씩 더 돌아 과금이 그대로 배가 된다.
    """
    calls: list[str] = []

    class CountingAnalyzer(SucceedingWebSearchAnalyzer):
        async def search_and_analyze(self, analysis_input, on_collected=None):
            calls.append(analysis_input.post_id)
            return await super().search_and_analyze(analysis_input, on_collected)

    service = build_service(web_search_analyzer=CountingAnalyzer())
    task = await service.create_blog_task(VALID_REQUEST)

    # 잡을 띄우기만 하고 기다리지 않는다 — 돌고 있는 동안 두 번째 요청이 들어와야 한다.
    await service.start_intent_analysis(task.post_id)
    await service.start_intent_analysis(task.post_id)
    await service._jobs.drain()

    assert calls == [task.post_id]  # 두 번 불렸다면 검색·요약이 두 벌 돈 것이다
    # 끝나면 다시 검증할 수 있어야 한다 — in-flight에 남으면 그 글은 영영 막힌다.
    assert task.post_id not in service._m3_inflight


def _selection(final_topic: str, keyword: str) -> TrendSelection:
    return TrendSelection(
        topic_candidate_id="topic_1",
        final_topic=final_topic,
        selected_trend_keyword_ids=["trend_1"],
        selected_keywords=[keyword],
        skipped=False,
        selected_at=_now(),
    )


async def test_a_stale_analysis_result_is_discarded_after_the_title_was_repicked():
    """검색이 도는 사이(1~2분) 사용자가 제목을 다시 고르면, 옛 제목의 결과는 버린다.

    재선택이 지워 둔 검증 자리를 옛 결과로 도로 채우면, 새 제목의 검증 팝업이 옛
    제목의 방향·자료를 새 것인 양 보여주게 된다.
    """
    repository = InMemoryBlogTaskRepository()

    class RepickingAnalyzer(SucceedingWebSearchAnalyzer):
        async def search_and_analyze(self, analysis_input, on_collected=None):
            # 검색이 도는 사이 사용자가 제목을 다시 골랐다.
            await repository.save_trend_selection(
                analysis_input.post_id,
                _selection("두 번째 제목", "새 키워드"),
                "system:m2-trend-selection",
            )
            return await super().search_and_analyze(analysis_input, on_collected)

    service = build_service(repository=repository, web_search_analyzer=RepickingAnalyzer())
    task = await service.create_blog_task(VALID_REQUEST)
    await repository.save_trend_selection(
        task.post_id, _selection("첫 제목", "옛 키워드"), "system:m2-trend-selection"
    )

    analyzed = await service.analyze_intent_candidates(task.post_id)

    # 옛 제목의 결과는 저장되지 않는다 — 새 제목의 검증은 다음 요청이 새 잡으로 돌린다.
    assert analyzed.intent_validation_result is None
    assert analyzed.trend_selection.final_topic == "두 번째 제목"


async def test_repicking_the_title_starts_a_fresh_analysis_instead_of_being_ignored():
    """중복 무시는 '같은 근거'일 때만이다. 옛 제목의 검증이 아직 도는 중이어도, 제목을
    다시 골랐다면 새 검증이 떠야 한다 — 옛 잡은 결과를 버리므로 여기서 안 뜨면 아무도
    새 제목을 검증하지 않는다."""
    import asyncio

    release = asyncio.Event()
    calls: list[str] = []
    repository = InMemoryBlogTaskRepository()

    class BlockingAnalyzer(SucceedingWebSearchAnalyzer):
        async def search_and_analyze(self, analysis_input, on_collected=None):
            calls.append(analysis_input.input.topic)
            await release.wait()
            return await super().search_and_analyze(analysis_input, on_collected)

    service = build_service(repository=repository, web_search_analyzer=BlockingAnalyzer())
    task = await service.create_blog_task(VALID_REQUEST)
    await repository.save_trend_selection(
        task.post_id, _selection("첫 제목", "옛 키워드"), "system:m2-trend-selection"
    )

    await service.start_intent_analysis(task.post_id)
    # 잡이 analyzer 안까지 들어갈 시간을 준다(블로킹 상태로 대기).
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0)
    assert calls == ["첫 제목"]

    # 옛 검증이 도는 동안 제목을 다시 고르고 검증을 다시 요청한다.
    await repository.save_trend_selection(
        task.post_id, _selection("두 번째 제목", "새 키워드"), "system:m2-trend-selection"
    )
    await service.start_intent_analysis(task.post_id)
    release.set()
    await service._jobs.drain()

    # 새 근거의 잡이 실제로 떴고(무시되지 않았고), 최종 결과는 새 제목의 것이다.
    assert calls == ["첫 제목", "두 번째 제목"]
    final = await repository.find_by_post_id(task.post_id)
    assert final.intent_validation_result is not None
    assert final.trend_selection.final_topic == "두 번째 제목"


async def test_m3_can_run_again_after_it_finishes():
    service = build_service()
    task = await service.create_blog_task(VALID_REQUEST)

    first = await service.analyze_intent_candidates(task.post_id)
    second = await service.analyze_intent_candidates(task.post_id)

    assert second.post_id == first.post_id
    assert second.intent_validation_result.intent_candidates[0].intent_id == "intent_1"


async def test_select_intent_stores_the_candidate_and_advances():
    service = build_service()
    task = await service.create_blog_task(VALID_REQUEST)
    await service.analyze_intent_candidates(task.post_id)

    selected = await service.select_intent(task.post_id, {"intentId": "intent_2"})

    assert selected.status == BlogTaskStatus.INTENT_SELECTED
    assert selected.selected_intent.intent_id == "intent_2"
    assert selected.selected_intent.target_reader == "Reader 2"


async def test_a_web_search_failure_leaves_the_post_retryable():
    """FAILED is terminal, so a model that timed out once used to kill a post whose
    input and topic were perfectly fine. It stays in SEARCH_ANALYZING — which is what
    다시 검증 re-runs from — and the client says the search failed rather than
    showing an empty one."""

    class FailingWebSearchAnalyzer:
        def __init__(self):
            self.calls = 0

        async def search_and_analyze(self, analysis_input, on_collected=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider down")
            return await SucceedingWebSearchAnalyzer().search_and_analyze(analysis_input)

    analyzer = FailingWebSearchAnalyzer()
    service = build_service(web_search_analyzer=analyzer)
    task = await service.create_blog_task(VALID_REQUEST)

    analyzed = await service.analyze_intent_candidates(task.post_id)

    assert analyzed.status == BlogTaskStatus.SEARCH_ANALYZING
    assert analyzed.progress is None

    # 실패했다는 사실이 남아야 한다. 아무것도 저장하지 않으면 검증 팝업은 실패와 '결과
    # 없음'을 구분하지 못해 "검색 결과가 없습니다"만 반복하고, 사용자는 이유도 모른 채
    # 다시 검증을 눌러도 같은 화면에 갇힌다.
    failed = analyzed.intent_validation_result
    assert failed is not None
    assert "provider down" in failed.intent_candidates[0].rationale
    # 자료는 지어내지 않는다 — 실패는 실패로 남긴다.
    assert failed.intent_candidates[0].sources == []

    # 다시 검증 — the same post, a second attempt.
    retried = await service.analyze_intent_candidates(task.post_id)

    assert retried.intent_validation_result is not None
    assert retried.status == BlogTaskStatus.SEARCH_ANALYZING
    # 성공한 재시도는 실패 기록을 실제 결과로 덮어쓴다 — 실패 사유가 남아 있으면 안 된다.
    assert "provider down" not in retried.intent_validation_result.intent_candidates[0].rationale
    assert len(retried.intent_validation_result.intent_candidates) == 3


async def test_a_provider_overload_is_explained_in_plain_language():
    """제공자 혼잡은 사용자가 읽을 수 있는 말로 안내한다 (2026-07-30).

    예전에는 `str(error)`를 그대로 카드에 실어, 검증 팝업에 제공자 원문 JSON이 떴다:

        provider request failed with 500: {"message": "gemini-3.5-flash is currently
        experiencing high demand, ...", "code": "api_error"}

    사용자가 다음에 무엇을 해야 하는지 판단하는 데 쓸모가 없는 문장이다. 기술적 원문은
    로그에 남는다.
    """
    from app.llm.parsing import ProviderOverloadedError

    class OverloadedAnalyzer:
        async def search_and_analyze(self, analysis_input, on_collected=None):
            raise ProviderOverloadedError(
                provider="gemini",
                model="gemini-3.5-flash",
                status=500,
                detail='{"message": "gemini-3.5-flash is currently experiencing high demand"}',
            )

    service = build_service(web_search_analyzer=OverloadedAnalyzer())
    task = await service.create_blog_task(VALID_REQUEST)

    analyzed = await service.analyze_intent_candidates(task.post_id)

    rationale = analyzed.intent_validation_result.intent_candidates[0].rationale
    assert "혼잡" in rationale
    assert "gemini" in rationale
    assert "다시 검증" in rationale
    # 원문 JSON·내부 문구가 화면으로 새지 않는다.
    assert "provider request failed" not in rationale
    assert "api_error" not in rationale
    assert '{"message"' not in rationale
    # 글은 죽지 않고, 자료를 지어내지도 않는다.
    assert analyzed.status == BlogTaskStatus.SEARCH_ANALYZING
    assert analyzed.intent_validation_result.intent_candidates[0].sources == []


async def test_a_summary_failure_keeps_the_sources_the_search_already_found():
    """검색은 성공했는데 요약 모델만 실패한 경우까지 전부 버리면, 자료가 실제로 있는데도
    검증 화면은 '검색 결과가 없습니다'가 된다. 찾은 자료로 진행할 수 있어야 한다."""
    from app.llm.live_adapters import GeminiResearchAnalyzer
    from app.llm.provider_config import RoleConfig
    from app.shared import SearchSource

    found = [SearchSource(title="한강 공원 안내", url="https://example.com/hangang", snippet="s")]
    role = RoleConfig(
        role="M3_SEARCH",
        label="검증",
        provider="gemini",
        model="m",
        api_key_env="K",
        api_key="k",
        has_credentials=True,
    )
    analyzer = GeminiResearchAnalyzer(role, role)

    async def collected(_input):
        return "요약문", found, True, []

    async def summarize_fails(*_args, **_kwargs):
        raise RuntimeError("OpenAI did not return intent candidates")

    analyzer._collect_research = collected
    analyzer._summarize_intent = summarize_fails

    result = await analyzer.search_and_analyze(
        WebSearchAnalysisInput(
            post_id="post_1",
            user_id="user_1",
            input=BlogTaskInput(topic="투썸 플레이스", keywords=["2026 한강"]),
            prompt_version="m3-intent@v1.0",
        )
    )

    assert result.intent_candidates[0].sources == found


def test_the_client_step_labels_match_the_ones_the_server_reports():
    """The client renders its own copy of the DRAFT steps until the first poll
    lands. If the two lists drift, the list visibly rewrites itself under the user
    the moment progress arrives."""
    import re
    from pathlib import Path

    from app.shared import PHASE_STEPS, TaskPhase

    source = Path(__file__).resolve().parents[3] / "apps/web/src/constants.ts"
    declaration = re.search(
        r"export const DRAFT_FLOW_STEPS = \[(.*?)\];", source.read_text(encoding="utf-8"), re.S
    )
    assert declaration, "DRAFT_FLOW_STEPS not found in the client constants"

    client_steps = re.findall(r'"([^"]+)"', declaration.group(1))
    assert client_steps == PHASE_STEPS[TaskPhase.DRAFT]



async def test_돌고_있는_검증을_중단할_수_있다():
    """검증 화면의 '제목 다시 고르기'가 부르는 길(2026-08-07 사용자 지적).

    그 검증의 결과는 어느 쪽이든 쓰이지 않는다 — 제목을 바꾸면 근거가 달라져 다시 돌려야
    하고, 같은 제목으로 돌아와도 사용자가 다시 시작한다. 그런데 잡은 계속 돌면서 Google
    검색과 LLM을 끝까지 쓴다.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    class 매달리는_분석기:
        """취소될 때까지 끝나지 않는 검증. 실제 LLM 호출 자리를 대신한다."""

        async def search_and_analyze(self, analysis_input, on_collected=None):
            started.set()
            await release.wait()
            raise AssertionError("취소됐어야 한다")

    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository, web_search_analyzer=매달리는_분석기())
    task = await service.create_blog_task(VALID_REQUEST)

    await service.start_intent_analysis(task.post_id)
    await asyncio.wait_for(started.wait(), timeout=2)

    stopped = await service.cancel_intent_analysis(task.post_id)

    assert stopped is True
    # 표식과 핸들이 지워져 곧바로 새 검증을 시작할 수 있다.
    assert service._m3_inflight.get(task.post_id) is None
    assert service._m3_jobs.get(task.post_id) is None


async def test_이미_끝난_검증을_중단하면_멈출_것이_없다고_한다():
    """오류가 아니다 — 사용자가 늦게 눌렀을 뿐이다."""
    service = build_service()
    task = await service.create_blog_task(VALID_REQUEST)

    assert await service.cancel_intent_analysis(task.post_id) is False


async def test_예약_목록이_받는_요약에_작업_현황_줄이_함께_실린다():
    """예약 화면이 원고 만드는 5~8분 동안 멈춘 것처럼 보이던 것을 푼다(2026-08-10 요청).

    예약의 로그는 단계 경계에서만 한 줄씩 쌓인다. 그 사이를 채우는 것이 새 글 작성
    화면이 보여 주는 바로 그 줄들이라, 같은 목록을 요약에 실어 보낸다. DB가 아니라
    프로세스 메모리에서 온다(get_user_blog_task_status와 같은 자리).
    """
    from app.modules.blog_task.jobs import record_activity, reset_activity_log

    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository)
    task = await service.create_blog_task(VALID_REQUEST)

    reset_activity_log(task.post_id)
    record_activity(task.post_id, "3/4 카드 이미지 생성 시작했어요")
    record_activity(task.post_id, "사진을 고르는 중이에요…")

    summaries = await service.get_post_summaries([task.post_id])

    assert [entry.message for entry in summaries[task.post_id].activity_log] == [
        "3/4 카드 이미지 생성 시작했어요",
        "사진을 고르는 중이에요…",
    ]


async def test_작업_현황_줄이_없으면_요약은_빈_목록을_준다():
    """서버가 다시 시작하면 메모리의 줄이 사라진다 — 그때도 목록은 그려져야 한다."""
    from app.modules.blog_task.jobs import reset_activity_log

    repository = InMemoryBlogTaskRepository()
    service = build_service(repository=repository)
    task = await service.create_blog_task(VALID_REQUEST)
    reset_activity_log(task.post_id)

    summaries = await service.get_post_summaries([task.post_id])

    assert summaries[task.post_id].activity_log == []


class TestCloningAGlassForAnotherDirection:
    """한 소재로 여러 편일 때 편마다 자기 방향을 가진 글이 하나씩 있어야 한다(2026-08-12).

    **진짜 저장소로 검사한다.** 가짜 서비스로만 보던 동안 상태 전이가 막히는 것을 놓쳐,
    화면에서 '서버 내부 오류'가 났다 — 복제본이 원본의 INTENT_SELECTED를 물려받아
    거기서 다시 방향을 고를 수 없었다.
    """

    @staticmethod
    async def _origin(repository):
        """검증까지 끝내고 방향 하나를 고른 글 — 화면이 '예약 등록'을 누르는 그 상태다."""
        service = build_service(repository=repository)
        task = await service.create_blog_task(VALID_REQUEST)
        await service.analyze_intent_candidates(task.post_id)
        await service.select_intent(task.post_id, {"intentId": "intent_1"})
        return service, task.post_id

    async def test_the_clone_can_still_choose_its_own_direction(self):
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)

        clone = await service.clone_for_direction(post_id, "intent_2")

        assert clone.selected_intent is not None
        assert clone.selected_intent.intent_id == "intent_2"

    async def test_the_clone_is_a_separate_post(self):
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)

        clone = await service.clone_for_direction(post_id, "intent_2")

        assert clone.post_id != post_id
        origin = await repository.find_by_post_id(post_id)
        assert origin.selected_intent.intent_id == "intent_1"  # 원본은 그대로다

    async def test_the_clone_carries_what_the_person_decided(self):
        """소재·제목·방향 후보는 베낀다 — 편마다 다시 고르게 할 이유가 없다."""
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)
        origin = await repository.find_by_post_id(post_id)

        clone = await service.clone_for_direction(post_id, "intent_2")

        assert clone.input.topic == origin.input.topic
        assert clone.trend_selection == origin.trend_selection
        assert clone.intent_validation_result == origin.intent_validation_result

    async def test_the_clone_does_not_ask_for_more_drafts(self):
        """draft_count를 그대로 물려받으면 이 글이 또 여러 편을 불러 끝없이 불어난다."""
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)

        clone = await service.clone_for_direction(post_id, "intent_2")

        assert clone.input.draft_count == 1

    async def test_a_direction_that_is_not_a_candidate_is_refused(self):
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)

        with pytest.raises(BlogTaskError):
            await service.clone_for_direction(post_id, "없는방향")


class TestEachDraftCarriesItsOwnTitle:
    """글 하나가 **제목 하나와 방향 하나**를 들고, 원고는 그 둘로 만들어진다(2026-08-12).

        "지금 글 하나가 제목하나 방향하나 가져가고 예약시간 되면 제목과 방향을 가지고
        원고 생성되게 해야지"

    편마다 제목·방향을 함께 고르는 흐름에서, 복제가 원본의 제목을 그대로 베끼면 세 편이
    같은 제목으로 올라간다.
    """

    @staticmethod
    async def _origin(repository):
        service = build_service(repository=repository)
        task = await service.create_blog_task(VALID_REQUEST)
        # 제목을 확정한 글이어야 한다 — 복제가 갈아 끼울 대상이 있어야 한다.
        await repository.save_trend_selection(
            task.post_id,
            TrendSelection(
                topic_candidate_id=None,
                final_topic="원본 제목",
                selected_trend_keyword_ids=[],
                skipped=False,
                selected_at=_now(),
            ),
            "test",
        )
        await service.analyze_intent_candidates(task.post_id)
        await service.select_intent(task.post_id, {"intentId": "intent_1"})
        return service, task.post_id

    async def test_a_given_title_replaces_the_one_copied(self):
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)

        clone = await service.clone_for_direction(
            post_id, "intent_2", final_topic="다른 제목"
        )

        assert clone.trend_selection.final_topic == "다른 제목"

    async def test_the_origin_keeps_its_own_title(self):
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)
        before = (await repository.find_by_post_id(post_id)).trend_selection

        await service.clone_for_direction(post_id, "intent_2", final_topic="다른 제목")

        after = (await repository.find_by_post_id(post_id)).trend_selection
        assert after == before

    async def test_without_a_title_the_origin_one_is_kept(self):
        """예약 경로("같은 제목을 여러 각도로")가 예전과 똑같이 동작해야 한다."""
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)
        origin = await repository.find_by_post_id(post_id)

        clone = await service.clone_for_direction(post_id, "intent_2")

        assert clone.trend_selection == origin.trend_selection

    @pytest.mark.parametrize("blank", ["", "   "])
    async def test_a_blank_title_does_not_erase_the_one_there(self, blank):
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)
        origin = await repository.find_by_post_id(post_id)

        clone = await service.clone_for_direction(post_id, "intent_2", final_topic=blank)

        assert clone.trend_selection == origin.trend_selection


class TestTheDirectionTravelsWithTheDraft:
    """편마다 고른 방향은 **화면이 들고 와야** 한다(2026-08-12 사용자 신고).

        "3번째 편에 대해서 글 방향 선택하고 다음방향 가니까 순간에러가 났어"
        (화면 로그: ``POST /posts/.../schedule 400 Bad Request``)

    ``intentId``는 자리번호다(``{postId}_intent_{n}``). 여러 편을 만들면 편마다 제목을
    다시 골라 검증(M3)이 다시 도는데, 새 제목을 저장하는 ``save_trend_selection``이 **옛
    검증 결과를 지운다.** 그래서 마지막에 예약을 걸 때 글에 남아 있는 후보는 마지막 편의
    것뿐이고, 1·2편째의 자리번호는 거기서 **다른 방향**을 가리킨다.

    400보다 조용한 쪽이 더 나빴다 — 번호가 겹치지 않으면 그대로 통과해, 고르지도 않은
    방향으로 원고가 만들어진다.
    """

    @staticmethod
    def _chosen(intent_id: str, title: str) -> IntentCandidate:
        """1편째 화면에서 고른 방향. 그 뒤 검증이 다시 돌아 글에서는 사라진 것이다."""
        return IntentCandidate(
            intent_id=intent_id,
            title=title,
            target_reader="20대 직장인",
            rationale="1편째에서 고른 근거",
            keywords=["가", "나"],
            sources=[],
        )

    @staticmethod
    async def _origin(repository):
        service = build_service(repository=repository)
        task = await service.create_blog_task(VALID_REQUEST)
        await service.analyze_intent_candidates(task.post_id)
        return service, task.post_id

    async def test_a_direction_the_post_no_longer_has_is_still_usable(self):
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)
        gone = self._chosen("사라진_자리번호", "1편째가 고른 방향")

        clone = await service.clone_for_direction(post_id, gone.intent_id, intent=gone)

        assert clone.selected_intent.intent_id == gone.intent_id
        assert clone.selected_intent.title == "1편째가 고른 방향"

    async def test_the_sent_direction_wins_over_the_one_in_that_slot(self):
        """같은 자리번호에 **다른 방향**이 앉아 있을 때 무엇으로 쓰이는가 — 이게 핵심이다."""
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)
        origin = await repository.find_by_post_id(post_id)
        slot = origin.intent_validation_result.intent_candidates[1]
        mine = self._chosen(slot.intent_id, "내가 고른 방향")
        assert slot.title != mine.title  # 같은 번호, 다른 방향

        clone = await service.clone_for_direction(post_id, slot.intent_id, intent=mine)

        assert clone.selected_intent.title == "내가 고른 방향"

    async def test_the_first_draft_gets_its_own_direction_too(self):
        """1편째는 복제가 아니라 **원본 글**에 적용된다 — 같은 문제를 함께 겪었다."""
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)
        origin = await repository.find_by_post_id(post_id)
        slot = origin.intent_validation_result.intent_candidates[0]
        mine = self._chosen(slot.intent_id, "1편째가 고른 방향")

        task = await service.apply_round_pick(post_id, slot.intent_id, intent=mine)

        assert task.selected_intent.title == "1편째가 고른 방향"

    async def test_the_other_candidates_are_kept(self):
        """고른 것을 되돌려 놓되 나머지 후보를 지우지 않는다 — 정보가 줄 이유가 없다."""
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)
        origin = await repository.find_by_post_id(post_id)
        before = len(origin.intent_validation_result.intent_candidates)
        mine = self._chosen("새_자리번호", "내가 고른 방향")

        clone = await service.clone_for_direction(post_id, mine.intent_id, intent=mine)

        assert len(clone.intent_validation_result.intent_candidates) == before + 1

    async def test_without_a_direction_the_old_rule_still_holds(self):
        """방향을 보내지 않는 옛 요청은 예전 그대로 — 없는 자리번호는 거절한다."""
        repository = InMemoryBlogTaskRepository()
        service, post_id = await self._origin(repository)

        with pytest.raises(BlogTaskError):
            await service.clone_for_direction(post_id, "없는방향")


class TestSourcesAreCollectedOnlyWhenTheUserPicksThem:
    """검증 단계에서 **자료를 지금 모을 것인가**.

    가르는 것은 **작업 시각을 정했는가** 하나다(2026-08-13 사용자 지적: "수집한 자료가
    보여져야지"). 시각을 정한 글만 나중에 모은다 — 며칠 뒤에 쓸 글의 자료를 오늘 모으면
    그 사이에 나온 이슈가 빠지기 때문이다(2026-08-11).

    편수 조건은 걷어냈다. 2026-08-12에는 "설정한 편수가 한편일때만 검증단계에서
    자료수집해서 사용자에게 보여주고 2편 이상으로 설정한 경우에는 자료수집은 원고생성
    단계 진입했을때"였는데, 그때는 여러 편이 작업 큐에서 순서대로 돌아 원고 시점이 한참
    뒤였다. 지금은 시각을 정하지 않은 여러 편이 곧바로 함께 돌고, 검증에서 모은 자료가
    그대로 원고에 쓰인다(scheduled_posting.service — has_appointment가 아니면 다시 모으지
    않는다).
    """

    @staticmethod
    def _input(**overrides) -> BlogTaskInput:
        from app.shared import BlogTaskInput as Model

        return Model(topic="롯데리아", purpose=["정보 전달"], keywords=[], **overrides)

    def test_one_draft_right_away_collects(self):
        from app.modules.blog_task.service import _collects_sources_now

        assert _collects_sources_now(self._input()) is True

    def test_two_or_more_drafts_also_collect(self):
        """2026-08-13에 뒤집혔다 — 편수는 더 이상 보지 않는다."""
        from app.modules.blog_task.service import _collects_sources_now

        assert _collects_sources_now(self._input(draft_count=2)) is True
        assert _collects_sources_now(self._input(draft_count=3)) is True

    def test_a_scheduled_post_does_not(self):
        """며칠 뒤에 쓸 글의 자료를 오늘 모으면 그 사이에 나온 이슈가 빠진다(2026-08-11)."""
        from app.modules.blog_task.service import _collects_sources_now

        assert _collects_sources_now(self._input(scheduled_run_at="2026-08-13T09:00:00Z")) is False

    def test_a_scheduled_post_does_not_even_with_several_drafts(self):
        """시각이 있으면 편수와 무관하게 나중에 모은다 — 두 조건이 서로 새지 않는다."""
        from app.modules.blog_task.service import _collects_sources_now

        assert (
            _collects_sources_now(
                self._input(draft_count=3, scheduled_run_at="2026-08-13T09:00:00Z")
            )
            is False
        )

    def test_a_blank_time_is_not_a_reservation(self):
        from app.modules.blog_task.service import _collects_sources_now

        assert _collects_sources_now(self._input(scheduled_run_at="   ")) is True

    @pytest.mark.parametrize(
        "body, collects",
        [({"draftCount": 2}, True), ({"scheduled": True}, False), ({}, True)],
    )
    async def test_the_analyzer_is_told_whether_to_collect(self, body, collects):
        """판단이 실제로 수집기까지 간다 — 여기서 끊기면 화면만 '나중에'라고 말한다."""
        from datetime import datetime, timedelta, timezone

        seen: list[bool] = []

        class Watching(SucceedingWebSearchAnalyzer):
            async def search_and_analyze(self, analysis_input, on_collected=None):
                seen.append(analysis_input.collect_sources)
                return await super().search_and_analyze(analysis_input, on_collected)

        # 검증이 '지금부터 60일 안'만 받으므로 시각은 여기서 만든다.
        if body.pop("scheduled", None):
            moment = datetime.now(timezone.utc) + timedelta(days=1)
            body["scheduledRunAt"] = moment.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )

        service = build_service(web_search_analyzer=Watching())
        task = await service.create_blog_task({**VALID_REQUEST, **body})

        await service.analyze_intent_candidates(task.post_id)

        assert seen == [collects]


class TestTheProgressSaysWhatItActuallyDid:
    """자료를 모으지 않는 실행은 **그렇게 적어야 한다**(2026-08-12 사용자 신고).

        "3번째 이미지 보면 최신자료 다시 모은다는데 애초에 안모으고 지정해둔 원고생성
         시간이 되면 수집하는거 아니야?"

    화면에는 '1/2 자료 검색 단계를 시작했어요', 'Gemini가 검색 키워드로 웹 자료를 찾아 읽는
    중이에요'가 찍혀 있었는데 그 실행은 **5초** 만에 끝났다 — 자료를 모으지 않았기 때문이다.
    """

    @staticmethod
    async def _run(body: dict) -> list[str]:
        from app.modules.blog_task.jobs import activity_log_for

        service = build_service()
        task = await service.create_blog_task({**VALID_REQUEST, **body})
        await service.analyze_intent_candidates(task.post_id)
        return [entry.message for entry in activity_log_for(task.post_id)]

    # 모으지 않는 실행은 **시각을 정해 둔 글**이다(2026-08-13). 편수는 더 이상 보지
    # 않으므로 여기서도 시각으로 그 실행을 만든다. 검증이 '지금부터 60일 안'만 받으므로
    # 고정 문자열이 아니라 지금에서 하루 뒤를 쓴다.
    @staticmethod
    def _later() -> str:
        from datetime import datetime, timedelta, timezone

        moment = datetime.now(timezone.utc) + timedelta(days=1)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    async def test_a_run_without_collection_does_not_say_it_searched(self):
        messages = await self._run({"scheduledRunAt": self._later()})

        joined = "\n".join(messages)
        assert "자료 검색" not in joined
        assert "웹 자료를 찾아 읽는" not in joined
        assert "글의 방향" in joined

    async def test_a_run_without_collection_has_one_step(self):
        """두 칸을 그리면 하지 않은 일을 한 칸이 남는다."""
        messages = await self._run({"scheduledRunAt": self._later()})

        assert any("1/1" in message for message in messages)
        assert not any("2/2" in message for message in messages)

    async def test_several_drafts_now_do_say_it_searched(self):
        """편수 조건이 사라졌으므로 여러 편도 모으는 실행이다(2026-08-13)."""
        messages = await self._run({"draftCount": 2})

        assert "자료 검색" in "\n".join(messages)

    async def test_a_collecting_run_is_unchanged(self):
        messages = await self._run({})

        joined = "\n".join(messages)
        assert "자료 검색" in joined
        assert any("1/2" in message for message in messages)


class TestThreeDraftsRunTogether:
    """3편을 걸면 **셋 다 함께** 원고를 만든다(2026-08-12 사용자 확인 요청).

        "편수는 최대 3편이 동시발행되는거지? 그렇게 설정하고 싶어."

    한 번에 걸 수 있는 편수가 동시 생성 상한보다 크면, 마지막 편은 앞 편이 끝나야 시작한다 —
    사용자가 고른 '3편'이 실제로는 줄서기가 된다. 두 상수를 여기서 묶어 둔다.
    """

    def test_every_draft_we_allow_can_start_at_once(self):
        from app.modules.blog_task.validation import MAX_DRAFT_COUNT
        from app.modules.scheduled_posting.worker import MAX_CONCURRENT_PREPARE

        assert MAX_DRAFT_COUNT <= MAX_CONCURRENT_PREPARE
