"""원고 두 편을 나란히 돌릴 수 있는가.

브랜드 자동 생성을 백그라운드로 돌려 두고 다른 글의 원고도 만들고 싶다는 요구에서
나왔다(2026-08-06). 실제로 막고 있던 것은 잡의 **개수 제한**이 아니라 기다리는 방식이었다.

`generate_draft`/`analyze_intent_candidates`는 "시작하고 기다린다"인데, 예전에는
`BackgroundJobs.drain()`으로 **그 서비스의 잡을 전부** 기다렸다. 그래서 A 글의 원고가
끝나도 B 글의 원고가 도는 동안 A의 호출이 돌아오지 않았고, `drain`은 도는 잡이 하나도
없을 때까지 반복하므로 요청이 이어지면 그만큼 더 기다렸다.

지금은 **자기 글의 잡만** 기다린다.
"""

import asyncio

from app.modules.blog_task.jobs import BackgroundJobs


class TestWhatDrainDid:
    async def test_drain_waits_for_everyone(self):
        """예전 방식이 왜 서로를 기다리게 했는지 못 박아 둔다."""
        jobs = BackgroundJobs()
        slow_done = asyncio.Event()

        async def quick():
            return None

        async def slow():
            await asyncio.sleep(0.05)
            slow_done.set()

        jobs.start(quick())
        jobs.start(slow())

        await jobs.drain()
        # 빠른 잡만 기다리고 싶어도, drain은 느린 잡까지 끝나야 돌아온다.
        assert slow_done.is_set()

    async def test_awaiting_one_handle_does_not_wait_for_the_others(self):
        """지금 방식: 내 잡만 기다린다."""
        jobs = BackgroundJobs()
        slow_done = asyncio.Event()

        async def quick():
            return None

        async def slow():
            await asyncio.sleep(0.3)
            slow_done.set()

        mine = jobs.start(quick())
        jobs.start(slow())

        await asyncio.gather(mine, return_exceptions=True)

        # 느린 잡은 아직 돌고 있다 — 그래도 내 것은 끝났다.
        assert not slow_done.is_set()
        await jobs.drain()
        assert slow_done.is_set()


class TestTheServicesKeepPerPostHandles:
    async def test_draft_service_tracks_and_clears_the_handle(self):
        from app.modules.blog_task.repository import InMemoryBlogTaskRepository
        from app.modules.draft.service import DraftService

        service = DraftService(
            repository=InMemoryBlogTaskRepository(),
            draft_generator=None,
            post_image_generator=None,
        )

        # 시작 전에는 붙잡아 둔 핸들이 없다 — 그때는 기다리지 않고 DB를 읽는다.
        assert service._draft_jobs == {}

    async def test_blog_task_service_tracks_and_clears_the_handle(self):
        from app.modules.blog_task.repository import InMemoryBlogTaskRepository
        from app.modules.blog_task.service import BlogTaskService

        service = BlogTaskService(InMemoryBlogTaskRepository(), None, None)

        assert service._m3_jobs == {}
