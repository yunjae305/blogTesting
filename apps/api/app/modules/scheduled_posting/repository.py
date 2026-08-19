"""예약 배치·작업 저장소.

기존 모듈들과 같은 방식이다: 인터페이스는 Protocol이고, Mongo 구현과 메모리 구현이
그것을 상속하지 않은 채 구조적으로 만족한다(app/modules/blog_task/repository.py와 같음).

소유권은 **쿼리의 일부**다. 다른 사용자의 batchId를 알아도 읽히지 않도록, 조회 메서드는
전부 user_id를 함께 받는다 — 문서를 먼저 읽고 나중에 비교하는 방식이 아니다.
"""

from typing import Protocol

from motor.motor_asyncio import AsyncIOMotorDatabase

from .models import (
    ScheduledBatch,
    ScheduledBatchStatus,
    ScheduledJob,
    ScheduledLogEntry,
)

BATCHES = "scheduled_batches"
JOBS = "scheduled_jobs"

#: 화면에 남기는 로그 줄 수 상한. 배치 문서가 무한히 자라지 않게 앞에서부터 버린다.
MAX_BATCH_LOGS = 200


class ScheduledPostingRepository(Protocol):
    async def create_batch(self, batch: ScheduledBatch, jobs: list[ScheduledJob]) -> None: ...
    async def find_batch(self, batch_id: str) -> ScheduledBatch | None: ...
    async def find_user_batch(self, user_id: str, batch_id: str) -> ScheduledBatch | None: ...
    async def find_batch_by_client_request(
        self, user_id: str, client_request_id: str
    ) -> ScheduledBatch | None: ...
    async def find_active_batch(self, user_id: str) -> ScheduledBatch | None: ...
    async def list_batches_by_status(
        self, statuses: list[ScheduledBatchStatus]
    ) -> list[ScheduledBatch]: ...
    async def statuses_of_batches(
        self, batch_ids: list[str]
    ) -> dict[str, ScheduledBatchStatus]: ...
    async def save_batch(self, batch: ScheduledBatch) -> None: ...
    async def append_batch_log(self, batch_id: str, entry: ScheduledLogEntry) -> None: ...
    async def list_jobs(self, batch_id: str) -> list[ScheduledJob]: ...
    async def list_user_jobs(self, user_id: str, limit: int = 50) -> list[ScheduledJob]: ...
    async def find_job(self, job_id: str) -> ScheduledJob | None: ...
    async def find_user_job(self, user_id: str, job_id: str) -> ScheduledJob | None: ...
    async def save_job(self, job: ScheduledJob) -> None: ...
    async def add_jobs(self, jobs: list[ScheduledJob]) -> None: ...
    async def delete_job(self, job_id: str) -> None: ...
    async def delete_jobs_for_post(self, user_id: str, post_id: str) -> list[str]: ...
    async def delete_batch(self, batch_id: str) -> None: ...


def _sorted_jobs(jobs: list[ScheduledJob]) -> list[ScheduledJob]:
    """항상 sequence 순. 화면의 표와 워커의 실행 순서가 같아야 한다."""
    return sorted(jobs, key=lambda job: job.sequence)


class MongoScheduledPostingRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self._batches = db[BATCHES]
        self._jobs = db[JOBS]

    async def create_batch(self, batch: ScheduledBatch, jobs: list[ScheduledJob]) -> None:
        # 배치를 먼저 넣는다. 작업 삽입이 실패해도 배치만 남으면 총 개수와 실제 작업 수가
        # 어긋나 워커가 바로 알아채고 실패로 닫을 수 있다 — 반대 순서면 주인 없는 작업이 남는다.
        await self._batches.insert_one({"_id": batch.batch_id, **batch.to_wire()})
        if jobs:
            await self._jobs.insert_many(
                [{"_id": job.job_id, **job.to_wire()} for job in jobs]
            )

    async def find_batch(self, batch_id: str) -> ScheduledBatch | None:
        doc = await self._batches.find_one({"_id": batch_id})
        return ScheduledBatch.model_validate(doc) if doc else None

    async def statuses_of_batches(
        self, batch_ids: list[str]
    ) -> dict[str, ScheduledBatchStatus]:
        """여러 배치의 **상태만** 한 번에. 목록 화면이 배치를 하나씩 읽지 않게 한다."""
        if not batch_ids:
            return {}
        cursor = self._batches.find({"_id": {"$in": batch_ids}}, {"status": 1})
        found: dict[str, ScheduledBatchStatus] = {}
        async for doc in cursor:
            status = doc.get("status")
            if status:
                found[doc["_id"]] = ScheduledBatchStatus(status)
        return found

    async def find_user_batch(self, user_id: str, batch_id: str) -> ScheduledBatch | None:
        doc = await self._batches.find_one({"_id": batch_id, "userId": user_id})
        return ScheduledBatch.model_validate(doc) if doc else None

    async def find_batch_by_client_request(
        self, user_id: str, client_request_id: str
    ) -> ScheduledBatch | None:
        doc = await self._batches.find_one(
            {"userId": user_id, "clientRequestId": client_request_id}
        )
        return ScheduledBatch.model_validate(doc) if doc else None

    async def find_active_batch(self, user_id: str) -> ScheduledBatch | None:
        from .models import OCCUPYING_BATCH_STATUSES

        doc = await self._batches.find_one(
            {
                "userId": user_id,
                "status": {"$in": [status.value for status in OCCUPYING_BATCH_STATUSES]},
            },
            sort=[("createdAt", -1)],
        )
        return ScheduledBatch.model_validate(doc) if doc else None

    async def list_batches_by_status(
        self, statuses: list[ScheduledBatchStatus]
    ) -> list[ScheduledBatch]:
        cursor = self._batches.find(
            {"status": {"$in": [status.value for status in statuses]}}
        ).sort("createdAt", 1)
        docs = await cursor.to_list(length=None)
        return [ScheduledBatch.model_validate(doc) for doc in docs]

    async def save_batch(self, batch: ScheduledBatch) -> None:
        await self._batches.replace_one(
            {"_id": batch.batch_id}, {"_id": batch.batch_id, **batch.to_wire()}, upsert=True
        )

    async def append_batch_log(self, batch_id: str, entry: ScheduledLogEntry) -> None:
        # $push + $slice로 마지막 N개만 남긴다. 문서 전체를 다시 쓰지 않아 경합에 강하다.
        await self._batches.update_one(
            {"_id": batch_id},
            {
                "$push": {
                    "logs": {"$each": [entry.to_wire()], "$slice": -MAX_BATCH_LOGS},
                },
                "$set": {"updatedAt": entry.at},
            },
        )

    async def list_jobs(self, batch_id: str) -> list[ScheduledJob]:
        cursor = self._jobs.find({"batchId": batch_id}).sort("sequence", 1)
        docs = await cursor.to_list(length=None)
        return [ScheduledJob.model_validate(doc) for doc in docs]

    async def list_user_jobs(self, user_id: str, limit: int = 50) -> list[ScheduledJob]:
        """이 사용자의 예약을 **최근 예약부터, 배치 안에서는 올라갈 순서대로.**

        정렬 키가 셋인 이유가 각각 있다.

        1. ``createdAt`` 내림차순 — 최근에 건 예약이 위에 온다. 한 배치의 작업은 이
           값이 **전부 같다**(start_batch가 시각을 한 번 읽어 모든 작업에 그대로
           넣는다), 그래서 이 키는 사실상 '배치끼리 묶고 최신 배치를 먼저'다.
        2. ``publishAt`` 오름차순 — 절대 시각 예약은 **정해 둔 시각 순**으로 읽혀야
           한다. 간격 방식은 이 값이 전부 없어 아래 키로 넘어간다.
        3. ``sequence`` 오름차순 — **입력한 순서**다(워커의 실행 순서이기도 하다).

        3번이 이번에 더해졌다. 예전에는 1·2번뿐이었는데, 간격 방식 배치는 두 키가
        통째로 동점(같은 createdAt · publishAt 없음)이라 순서가 정해지지 않았고 실제로
        **입력의 역순**으로 나왔다: 사용자가 GS25 · 세븐일레븐 순으로 넣었는데 작업 큐
        맨 위에 세븐일레븐이 섰다(2026-08-06 신고 — 저장된 7개 배치 중 6개가 뒤집혀
        있었다).
        """
        cursor = (
            self._jobs.find({"userId": user_id})
            .sort([("createdAt", -1), ("publishAt", 1), ("sequence", 1)])
            .limit(max(1, limit))
        )
        docs = await cursor.to_list(length=None)
        return [ScheduledJob.model_validate(doc) for doc in docs]

    async def find_job(self, job_id: str) -> ScheduledJob | None:
        doc = await self._jobs.find_one({"_id": job_id})
        return ScheduledJob.model_validate(doc) if doc else None

    async def find_user_job(self, user_id: str, job_id: str) -> ScheduledJob | None:
        doc = await self._jobs.find_one({"_id": job_id, "userId": user_id})
        return ScheduledJob.model_validate(doc) if doc else None

    async def save_job(self, job: ScheduledJob) -> None:
        # **upsert하지 않는다.** 작업 문서는 create_batch에서만 생긴다. upsert로 두면
        # 사용자가 방금 지운 작업을 워커의 상태 저장이 되살려 놓고, 그 소재로 글이
        # 만들어져 네이버에 올라간다 — 삭제 기능의 목적을 정면으로 뒤집는 경로다.
        await self._jobs.replace_one(
            {"_id": job.job_id}, {"_id": job.job_id, **job.to_wire()}
        )

    async def add_jobs(self, jobs: list[ScheduledJob]) -> None:
        """**돌고 있는 배치에 작업을 덧붙인다**(2026-08-13).

        ``save_job``으로는 안 된다 — 그쪽은 일부러 upsert하지 않아서(위 주석) 새 작업이
        조용히 사라진다. 실제로 새 글 작성에서 건 예약이 활성 배치에 붙을 때 그 길로
        갔고, 저장된 것이 하나도 없었다. 만드는 일과 고치는 일은 다른 메서드다.
        """
        if not jobs:
            return
        await self._jobs.insert_many(
            [{"_id": job.job_id, **job.to_wire()} for job in jobs]
        )

    async def delete_job(self, job_id: str) -> None:
        await self._jobs.delete_one({"_id": job_id})

    async def delete_jobs_for_post(self, user_id: str, post_id: str) -> list[str]:
        """그 글을 가리키는 예약 기록을 전부 지우고, 어느 배치의 것이었는지 돌려준다.

        글이 지워지면 그 글을 만든 예약 기록은 **아무것도 가리키지 않는다.** 제목도
        원고도 발행 주소도 그 글에서 읽어 오던 것이라, 남겨 두면 발행 내역이 없는 글을
        설명하는 줄로 채워진다 — 내 글 목록을 전부 비웠는데 발행 내역에는 예전 기록이
        그대로 남아 있는 것이 그것이다(2026-08-06 신고).

        배치 id를 돌려주는 것은 부르는 쪽이 집계를 다시 세야 하기 때문이다.
        """
        cursor = self._jobs.find({"userId": user_id, "postId": post_id}, {"batchId": 1})
        batch_ids = {doc["batchId"] async for doc in cursor if doc.get("batchId")}
        if batch_ids:
            await self._jobs.delete_many({"userId": user_id, "postId": post_id})
        return sorted(batch_ids)

    async def delete_batch(self, batch_id: str) -> None:
        """배치 문서만 지운다. 작업은 호출하는 쪽이 지울 것만 골라 지운다 —
        완료된 작업(이미 발행된 글의 기록)은 남기는 정책이기 때문이다."""
        await self._batches.delete_one({"_id": batch_id})


class InMemoryScheduledPostingRepository:
    """Mongo 없이 띄운 개발 환경용. 다른 모듈의 InMemory 저장소와 같은 역할이다.

    모델을 복사해 넣는다(``model_copy``). 넣은 객체를 호출자가 계속 들고 고치면 저장된
    것까지 함께 바뀌어, 저장이 없었던 것처럼 보이는 버그가 생긴다.
    """

    def __init__(self) -> None:
        self._batches: dict[str, ScheduledBatch] = {}
        self._jobs: dict[str, ScheduledJob] = {}

    async def create_batch(self, batch: ScheduledBatch, jobs: list[ScheduledJob]) -> None:
        self._batches[batch.batch_id] = batch.model_copy(deep=True)
        for job in jobs:
            self._jobs[job.job_id] = job.model_copy(deep=True)

    async def find_batch(self, batch_id: str) -> ScheduledBatch | None:
        found = self._batches.get(batch_id)
        return found.model_copy(deep=True) if found else None

    async def statuses_of_batches(
        self, batch_ids: list[str]
    ) -> dict[str, ScheduledBatchStatus]:
        return {
            batch_id: self._batches[batch_id].status
            for batch_id in batch_ids
            if batch_id in self._batches
        }

    async def find_user_batch(self, user_id: str, batch_id: str) -> ScheduledBatch | None:
        found = self._batches.get(batch_id)
        if found is None or found.user_id != user_id:
            return None
        return found.model_copy(deep=True)

    async def find_batch_by_client_request(
        self, user_id: str, client_request_id: str
    ) -> ScheduledBatch | None:
        for batch in self._batches.values():
            if batch.user_id == user_id and batch.client_request_id == client_request_id:
                return batch.model_copy(deep=True)
        return None

    async def find_active_batch(self, user_id: str) -> ScheduledBatch | None:
        from .models import OCCUPYING_BATCH_STATUSES

        matches = [
            batch
            for batch in self._batches.values()
            if batch.user_id == user_id and batch.status in OCCUPYING_BATCH_STATUSES
        ]
        if not matches:
            return None
        matches.sort(key=lambda batch: batch.created_at, reverse=True)
        return matches[0].model_copy(deep=True)

    async def list_batches_by_status(
        self, statuses: list[ScheduledBatchStatus]
    ) -> list[ScheduledBatch]:
        wanted = set(statuses)
        matches = [batch for batch in self._batches.values() if batch.status in wanted]
        matches.sort(key=lambda batch: batch.created_at)
        return [batch.model_copy(deep=True) for batch in matches]

    async def save_batch(self, batch: ScheduledBatch) -> None:
        self._batches[batch.batch_id] = batch.model_copy(deep=True)

    async def append_batch_log(self, batch_id: str, entry: ScheduledLogEntry) -> None:
        batch = self._batches.get(batch_id)
        if batch is None:
            return
        # entry를 복사해 넣는다. model_copy(deep=True)는 self만 깊은 복사이고 update로
        # 넘긴 값은 그대로 들어가므로, 호출자가 들고 있는 엔트리를 나중에 고치면 저장된
        # 로그까지 함께 바뀐다(Mongo 구현에는 없는 차이다).
        logs = [*batch.logs, entry.model_copy(deep=True)][-MAX_BATCH_LOGS:]
        self._batches[batch_id] = batch.model_copy(
            update={"logs": logs, "updated_at": entry.at}, deep=True
        )

    async def list_jobs(self, batch_id: str) -> list[ScheduledJob]:
        matches = [job for job in self._jobs.values() if job.batch_id == batch_id]
        return [job.model_copy(deep=True) for job in _sorted_jobs(matches)]

    async def list_user_jobs(self, user_id: str, limit: int = 50) -> list[ScheduledJob]:
        matches = [job for job in self._jobs.values() if job.user_id == user_id]
        # Mongo 구현과 **같은 정렬이어야 한다**(위 docstring 참고).
        #
        # 빈 문자열을 시각 없음의 자리로 쓴다 — 어떤 ISO 문자열보다 앞서므로, 시각이
        # 없는 작업을 오름차순의 앞에 두는 Mongo의 null 처리와 결과가 같아진다.
        # 예전 구현은 시각 없는 것을 뒤로 밀어 Mongo와 반대였다.
        #
        # 두 번 나눠 정렬한다. 파이썬 정렬은 안정적이라 나중에 건 키가 1순위가 되고
        # 앞서 건 키가 동점을 가른다 — 문자열을 뒤집는 꼼수 없이 내림차순을 얻는다.
        matches.sort(key=lambda job: (job.publish_at or "", job.sequence))
        matches.sort(key=lambda job: job.created_at, reverse=True)
        return [job.model_copy(deep=True) for job in matches[: max(1, limit)]]

    async def find_job(self, job_id: str) -> ScheduledJob | None:
        found = self._jobs.get(job_id)
        return found.model_copy(deep=True) if found else None

    async def find_user_job(self, user_id: str, job_id: str) -> ScheduledJob | None:
        found = self._jobs.get(job_id)
        if found is None or found.user_id != user_id:
            return None
        return found.model_copy(deep=True)

    async def save_job(self, job: ScheduledJob) -> None:
        # Mongo 구현과 같은 이유로 없는 작업은 되살리지 않는다(위 주석 참고).
        if job.job_id not in self._jobs:
            return
        self._jobs[job.job_id] = job.model_copy(deep=True)

    async def add_jobs(self, jobs: list[ScheduledJob]) -> None:
        """돌고 있는 배치에 작업을 덧붙인다 — Mongo 구현과 같은 이유다(위 주석 참고)."""
        for job in jobs:
            self._jobs[job.job_id] = job.model_copy(deep=True)

    async def delete_job(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    async def delete_jobs_for_post(self, user_id: str, post_id: str) -> list[str]:
        doomed = [
            job
            for job in self._jobs.values()
            if job.user_id == user_id and job.post_id == post_id
        ]
        for job in doomed:
            self._jobs.pop(job.job_id, None)
        return sorted({job.batch_id for job in doomed if job.batch_id})

    async def delete_batch(self, batch_id: str) -> None:
        self._batches.pop(batch_id, None)
