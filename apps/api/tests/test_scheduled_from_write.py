"""새 글 작성에서 방향까지 고른 글을 예약으로 넘긴다(2026-08-11 사용자 지시).

계약은 두 줄로 줄어든다.

1. **시각을 넣지 않으면 예전 그대로다.** 단일 글 작성은 한 글자도 달라지지 않는다.
2. **시각을 넣으면 그 시각에 자료를 새로 모아 원고를 만든다.** 방향(제목·독자·논지)은
   사람이 고른 그대로 두고, 낡는 것(자료)만 갈아끼운다.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.errors import BlogTaskError
from app.modules.blog_task.repository import InMemoryBlogTaskRepository
from app.modules.blog_task.service import BlogTaskService
from app.shared import (
    BlogTaskStatus,
    IntentCandidate,
    IntentValidationResult,
    SearchSource,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class StubAnalyzer:
    """검증 스텁. 수집 재실행은 호출 횟수와 돌려준 자료로만 확인한다."""

    def __init__(self, fresh: list[SearchSource] | None = None, fail: bool = False):
        self.fresh = fresh if fresh is not None else [
            SearchSource(title="오늘 기사", url="https://news.example/today", snippet="새 자료")
        ]
        self.fail = fail
        self.collect_calls = 0

    async def search_and_analyze(self, analysis_input, on_collected=None):
        return IntentValidationResult(
            prompt_version=analysis_input.prompt_version,
            provider="mock",
            model="mock",
            analyzed_at=_now(),
            intent_candidates=[
                IntentCandidate(
                    intent_id="intent_1",
                    title="고른 방향",
                    target_reader="자취생",
                    rationale="근거",
                    keywords=["에어프라이어"],
                    sources=[
                        SearchSource(
                            title="지난주 기사",
                            url="https://news.example/old",
                            snippet="옛 자료",
                        )
                    ],
                )
            ],
        )

    async def collect_sources(self, analysis_input):
        self.collect_calls += 1
        if self.fail:
            raise RuntimeError("수집 실패")
        return self.fresh


class NoCollectAnalyzer(StubAnalyzer):
    """``collect_sources``가 없는 구형 어댑터·테스트 스텁."""

    collect_sources = None


async def _post_with_selected_intent(service, repository) -> str:
    created = await service.create_blog_task(
        {"userId": "user_1", "topic": "에어프라이어", "keywords": ["정보 전달"]}
    )
    post_id = created.post_id
    # 소재를 저장하면 글은 REFERENCE_PROCESSING으로 간다(서비스가 옮긴다).
    await service.update_blog_task_input(
        post_id, {"topic": "에어프라이어", "purpose": ["정보 전달"]}
    )
    await repository.transition_status(post_id, BlogTaskStatus.SEARCH_ANALYZING, "test")
    result = await service._web_search_analyzer.search_and_analyze(
        type("Input", (), {"prompt_version": "m3@v1", "input": type("I", (), {"keywords": []})()})()
    )
    await repository.save_intent_validation_result(post_id, result)
    await service.select_intent(post_id, {"intentId": "intent_1"})
    return post_id


@pytest.fixture
def repository():
    return InMemoryBlogTaskRepository()


def build(repository, analyzer):
    return BlogTaskService(
        repository=repository,
        posting_worker=None,
        web_search_analyzer=analyzer,
        threads_writer=None,
    )


class TestSourcesAreRefreshed:
    @pytest.mark.asyncio
    async def test_the_direction_stays_and_only_the_sources_change(self, repository):
        analyzer = StubAnalyzer()
        service = build(repository, analyzer)
        post_id = await _post_with_selected_intent(service, repository)

        updated = await service.refresh_selected_intent_sources(post_id)

        assert analyzer.collect_calls == 1
        # 방향은 사람이 고른 그대로다.
        assert updated.selected_intent.title == "고른 방향"
        assert updated.selected_intent.target_reader == "자취생"
        assert updated.selected_intent.keywords == ["에어프라이어"]
        # 자료만 새것으로 갈렸다.
        assert [s.url for s in updated.selected_intent.sources] == [
            "https://news.example/today"
        ]

    @pytest.mark.asyncio
    async def test_the_post_stays_selected_not_rewound(self, repository):
        """상태를 되돌리면 화면이 '검증 중'으로 보인다 — 바뀐 것은 근거뿐이다."""
        service = build(repository, StubAnalyzer())
        post_id = await _post_with_selected_intent(service, repository)

        updated = await service.refresh_selected_intent_sources(post_id)

        assert updated.status == BlogTaskStatus.INTENT_SELECTED


class TestFailureKeepsThePromise:
    @pytest.mark.asyncio
    async def test_a_failed_collection_keeps_the_old_sources(self, repository):
        """자료를 새로 못 모았다고 약속한 시각을 놓치지 않는다 — 옛 자료로 간다."""
        service = build(repository, StubAnalyzer(fail=True))
        post_id = await _post_with_selected_intent(service, repository)

        updated = await service.refresh_selected_intent_sources(post_id)

        assert [s.url for s in updated.selected_intent.sources] == [
            "https://news.example/old"
        ]

    @pytest.mark.asyncio
    async def test_an_empty_collection_keeps_the_old_sources(self, repository):
        service = build(repository, StubAnalyzer(fresh=[]))
        post_id = await _post_with_selected_intent(service, repository)

        updated = await service.refresh_selected_intent_sources(post_id)

        assert [s.url for s in updated.selected_intent.sources] == [
            "https://news.example/old"
        ]

    @pytest.mark.asyncio
    async def test_an_analyzer_without_the_method_is_not_an_error(self, repository):
        """구형 어댑터·스텁에서도 예약이 죽지 않는다."""
        service = build(repository, NoCollectAnalyzer())
        post_id = await _post_with_selected_intent(service, repository)

        updated = await service.refresh_selected_intent_sources(post_id)

        assert updated.selected_intent is not None

    @pytest.mark.asyncio
    async def test_a_post_without_a_direction_is_refused(self, repository):
        service = build(repository, StubAnalyzer())
        created = await service.create_blog_task(
            {"userId": "user_1", "topic": "에어프라이어", "keywords": ["정보 전달"]}
        )

        with pytest.raises(BlogTaskError, match="selected intent"):
            await service.refresh_selected_intent_sources(created.post_id)


class TestScheduleStaysOptional:
    @pytest.mark.asyncio
    async def test_a_post_without_a_time_carries_no_schedule(self, repository):
        """시각을 넣지 않으면 예약 경로를 지나지 않는다 — 단일 글 작성 그대로."""
        service = build(repository, StubAnalyzer())
        created = await service.create_blog_task(
            {"userId": "user_1", "topic": "에어프라이어", "keywords": ["정보 전달"]}
        )

        assert created.input.scheduled_run_at is None

    @pytest.mark.asyncio
    async def test_a_time_survives_an_input_update(self, repository):
        service = build(repository, StubAnalyzer())
        created = await service.create_blog_task(
            {"userId": "user_1", "topic": "에어프라이어", "keywords": ["정보 전달"]}
        )
        later = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace(
            "+00:00", "Z"
        )

        updated = await service.update_blog_task_input(
            created.post_id,
            {
                "topic": "에어프라이어",
                "purpose": ["정보 전달"],
                "scheduledRunAt": later,
                "scheduledTimezone": "Asia/Seoul",
            },
        )

        assert updated.input.scheduled_run_at
        assert updated.input.scheduled_timezone == "Asia/Seoul"
