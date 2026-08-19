"""서버가 꺼질 때 돌던 원고 생성은 **이어받지 않는다**(2026-08-12 사용자 지시).

    "서버를 끄면 작업은 중지되게 만들어"

신고된 증상: 서버를 켜니 언제 만든지도 모르는 옛 글이 원고 생성으로 돌았다. GENERATING
으로 적혀 있지만 그것을 돌리던 프로세스는 이미 없다 — 화면은 '원고 만드는 중'이라는
거짓을 말하고, 그 글을 열면 0%에서 버튼도 없이 멈춘다.
"""


from datetime import datetime, timezone

from app.shared import BlogTask, BlogTaskInput, BlogTaskStatus

from test_blog_task_service import InMemoryBlogTaskRepository, build_service


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


async def _stuck(repository, post_id: str, status: BlogTaskStatus):
    await repository.create(
        BlogTask(
            post_id=post_id,
            user_id="user_1",
            status=status,
            version=1,
            created_at=_now(),
            updated_at=_now(),
            input=BlogTaskInput(topic="소재", keywords=["k"]),
        )
    )


class TestStartupStopsWhatNobodyIsRunning:
    async def test_a_generating_post_is_marked_failed(self):
        repository = InMemoryBlogTaskRepository()
        service = build_service(repository=repository)
        await _stuck(repository, "post_stuck", BlogTaskStatus.GENERATING)

        stopped = await service.stop_orphaned_generations()

        assert stopped == 1
        task = await repository.find_by_post_id("post_stuck")
        # FAILED여야 화면이 '다시 생성하기'를 준다 — 사람이 눌러야 다시 돈다.
        assert task.status == BlogTaskStatus.FAILED

    async def test_posts_in_other_states_are_untouched(self):
        """돌고 있지 않던 글까지 건드리면 멀쩡한 글이 실패로 표시된다."""
        repository = InMemoryBlogTaskRepository()
        service = build_service(repository=repository)
        await _stuck(repository, "post_ready", BlogTaskStatus.INTENT_SELECTED)
        await _stuck(repository, "post_done", BlogTaskStatus.READY_TO_PUBLISH)

        stopped = await service.stop_orphaned_generations()

        assert stopped == 0
        assert (await repository.find_by_post_id("post_ready")).status == (
            BlogTaskStatus.INTENT_SELECTED
        )
        assert (await repository.find_by_post_id("post_done")).status == (
            BlogTaskStatus.READY_TO_PUBLISH
        )

    async def test_nothing_stuck_is_not_an_error(self):
        repository = InMemoryBlogTaskRepository()
        service = build_service(repository=repository)

        assert await service.stop_orphaned_generations() == 0
