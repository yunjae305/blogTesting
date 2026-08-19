"""예약 발행의 사용자별 병렬화 (2026-08-18).

예전에는 서버 전체에 발행 자리가 하나뿐이었다(`_publishing` 단일 슬롯) — 사용자
10명이 각자 자기 계정으로 예약을 걸면, 다른 계정의 발행 1~2분씩이 전부 줄을 섰다.
크롬 프로필은 사용자별로 갈라져 있으므로(naver_profile_dir) 서로 막을 이유가 없다.

여기서 고정하는 규칙:

- 같은 사용자는 여전히 하나씩(프로필 잠금과 같은 이유).
- 다른 사용자는 동시에 발행할 수 있다.
- 서버 전체 동시 발행 수는 SCHEDULED_MAX_CONCURRENT_PUBLISH가 막는다(크롬 RAM).
- 발행이 끝나면 그 사용자의 자리만 비운다.
"""

import asyncio

from app.modules.blog_task.locks import NoOpJobLease
from app.modules.scheduled_posting.worker import ScheduledPostingWorker


class _Repo:
    async def list_jobs(self, _batch_id):
        return []

    async def find_batch(self, _batch_id):
        return None


class _Service:
    def __init__(self):
        self.executed: list[tuple[str, bool]] = []

    async def execute_job(self, job_id: str, publish: bool = True):
        self.executed.append((job_id, publish))


def _worker() -> ScheduledPostingWorker:
    return ScheduledPostingWorker(_Service(), _Repo(), NoOpJobLease())


class _Slot:
    """자리 표시용 가짜 태스크 — 슬롯 점유 여부만 본다."""


class TestCanPublish:
    def test_an_empty_slot_allows_publishing(self):
        assert _worker()._can_publish("user_a") is True

    def test_the_same_user_still_goes_one_by_one(self):
        worker = _worker()
        worker._publishing["user_a"] = _Slot()
        assert worker._can_publish("user_a") is False

    def test_a_different_user_is_not_blocked(self):
        """이것이 병렬화의 전부다 — 예전 단일 슬롯에서는 False였다."""
        worker = _worker()
        worker._publishing["user_a"] = _Slot()
        assert worker._can_publish("user_b") is True

    def test_the_server_wide_cap_holds(self, monkeypatch):
        monkeypatch.setenv("SCHEDULED_MAX_CONCURRENT_PUBLISH", "2")
        worker = _worker()
        worker._publishing["user_a"] = _Slot()
        worker._publishing["user_b"] = _Slot()
        assert worker._can_publish("user_c") is False

    def test_a_broken_cap_value_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("SCHEDULED_MAX_CONCURRENT_PUBLISH", "많이")
        worker = _worker()
        assert worker._can_publish("user_a") is True


class TestRunPublish:
    async def test_publishing_frees_only_that_users_slot(self):
        worker = _worker()
        other = _Slot()
        worker._publishing["user_b"] = other
        worker._publishing["user_a"] = _Slot()

        await worker._run_publish("batch_1", "job_1", "user_a")

        assert "user_a" not in worker._publishing
        assert worker._publishing["user_b"] is other
        assert worker._service.executed == [("job_1", True)]

    async def test_two_users_publish_at_the_same_time(self):
        """실제로 두 발행이 **겹쳐서** 돈다 — 한쪽이 끝나야 다른 쪽이 시작하는 게 아니다."""
        worker = _worker()
        running = {"count": 0, "peak": 0}

        async def slow_execute(job_id, publish=True):
            running["count"] += 1
            running["peak"] = max(running["peak"], running["count"])
            await asyncio.sleep(0.05)
            running["count"] -= 1

        worker._service.execute_job = slow_execute

        await asyncio.gather(
            worker._run_publish("batch_a", "job_a", "user_a"),
            worker._run_publish("batch_b", "job_b", "user_b"),
        )
        assert running["peak"] == 2
