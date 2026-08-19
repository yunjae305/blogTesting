"""예약 포스팅 저장소(InMemoryScheduledPostingRepository) 테스트.

여기서 확인하는 것은 두 가지다.

1. **소유권이 쿼리의 일부인가.** 다른 사용자의 batchId·jobId를 알아도 읽히면 안 된다.
   문서를 먼저 읽고 나중에 비교하는 방식이면 이 테스트가 통과하지 못한다.
2. **저장이 실제로 값을 떼어 놓는가.** 저장소가 호출자의 객체를 그대로 물고 있으면,
   호출자가 그 객체를 계속 고칠 때 저장된 값까지 함께 바뀌어 '저장이 없었던 것처럼'
   보이는 버그가 생긴다.

Mongo 구현은 여기서 돌리지 않는다 — 실제 서버가 필요하다.
"""

from datetime import datetime, timezone

from app.modules.scheduled_posting.models import (
    ScheduledBatch,
    ScheduledBatchStatus,
    ScheduledJob,
    ScheduledJobStatus,
    ScheduledLogEntry,
)
from app.modules.scheduled_posting.repository import (
    MAX_BATCH_LOGS,
    InMemoryScheduledPostingRepository,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_batch(**overrides) -> ScheduledBatch:
    now = _now()
    defaults = dict(
        batch_id="batch_1",
        user_id="user_1",
        target_count=2,
        interval_seconds=600,
        total_count=2,
        created_at=now,
        updated_at=now,
    )
    return ScheduledBatch(**{**defaults, **overrides})


def build_job(**overrides) -> ScheduledJob:
    now = _now()
    defaults = dict(
        job_id="job_1",
        batch_id="batch_1",
        user_id="user_1",
        sequence=0,
        topic="소재 1",
        created_at=now,
        updated_at=now,
    )
    return ScheduledJob(**{**defaults, **overrides})


# ---------------------------------------------------------------- 생성과 조회


async def test_create_batch는_배치와_작업을_함께_저장하고_list_jobs는_sequence_순으로_돌려준다():
    """화면의 표와 워커의 실행 순서가 같아야 하므로 정렬 기준은 항상 sequence다."""
    repository = InMemoryScheduledPostingRepository()
    jobs = [
        build_job(job_id="job_c", sequence=2, topic="셋째"),
        build_job(job_id="job_a", sequence=0, topic="첫째"),
        build_job(job_id="job_b", sequence=1, topic="둘째"),
    ]

    await repository.create_batch(build_batch(target_count=3, total_count=3), jobs)
    # 다른 배치의 작업이 섞여 들어오면 안 된다.
    await repository.create_batch(
        build_batch(batch_id="batch_2"),
        [build_job(job_id="job_other", batch_id="batch_2", sequence=0, topic="남의 소재")],
    )

    stored = await repository.find_batch("batch_1")
    assert stored is not None
    assert stored.total_count == 3
    assert stored.status == ScheduledBatchStatus.READY

    listed = await repository.list_jobs("batch_1")
    assert [job.sequence for job in listed] == [0, 1, 2]
    assert [job.topic for job in listed] == ["첫째", "둘째", "셋째"]
    assert [job.job_id for job in listed] == ["job_a", "job_b", "job_c"]

    # 작업은 jobId로도 바로 찾힌다.
    assert (await repository.find_job("job_b")).topic == "둘째"
    assert await repository.find_job("없는_작업") is None
    assert await repository.list_jobs("없는_배치") == []


async def test_list_user_jobs는_최근_예약부터_그_안에서는_입력한_순서로_돌려준다():
    """예약 목록 화면(작업 큐·발행 내역)이 읽는 순서다.

    예전 정렬은 ``[publishAt asc, createdAt desc]``이었다. 그런데 **한 배치의 작업은
    createdAt이 전부 같고**(start_batch가 시각을 한 번 읽어 모두에 넣는다) 간격 방식은
    publishAt도 전부 없다 — 정렬 키가 통째로 동점이라 순서가 정해지지 않았고, 실제로
    입력의 역순으로 나왔다(2026-08-06: GS25 · 세븐일레븐 순으로 넣었는데 큐 맨 위에
    세븐일레븐이 섰다. 저장돼 있던 7개 배치 중 6개가 뒤집혀 있었다).

    그래서 **같은 시각의 작업은 sequence가 가른다.** 그 값이 곧 입력 순서다.
    """
    repository = InMemoryScheduledPostingRepository()
    옛날 = "2026-08-05T11:00:00.000Z"
    최근 = "2026-08-06T09:39:19.848Z"

    # 일부러 뒤집어 넣는다 — 넣은 순서가 아니라 sequence가 결과를 정해야 한다.
    await repository.create_batch(
        build_batch(batch_id="batch_새것", created_at=최근),
        [
            build_job(
                job_id="job_세븐", batch_id="batch_새것", sequence=1,
                topic="세븐일레븐", created_at=최근, updated_at=최근,
            ),
            build_job(
                job_id="job_GS", batch_id="batch_새것", sequence=0,
                topic="GS25", created_at=최근, updated_at=최근,
            ),
        ],
    )
    await repository.create_batch(
        build_batch(batch_id="batch_옛것", created_at=옛날),
        [
            build_job(
                job_id="job_커피", batch_id="batch_옛것", sequence=0,
                topic="커피", created_at=옛날, updated_at=옛날,
            ),
        ],
    )

    listed = await repository.list_user_jobs("user_1")

    # 최근 예약이 먼저, 그 안에서는 입력 순서 그대로.
    assert [job.topic for job in listed] == ["GS25", "세븐일레븐", "커피"]

    # 남의 작업은 섞이지 않는다.
    assert await repository.list_user_jobs("user_2") == []


async def test_list_user_jobs는_절대_시각_예약을_시각_순으로_둔다():
    """입력 순서(sequence)는 **시각이 없을 때만** 쓰는 마지막 기준이다.

    글마다 발행 시각을 정한 예약은 '언제 무엇이 올라가는가'로 읽혀야 하므로 시각이
    먼저다. 순서를 sequence 하나로 바꿔 버리면 예약 시각을 고친 뒤(reschedule_job)
    목록이 실제 발행 순서와 어긋난다.
    """
    repository = InMemoryScheduledPostingRepository()
    now = _now()
    await repository.create_batch(
        build_batch(created_at=now),
        [
            build_job(job_id="job_늦은", sequence=0, topic="나중 글",
                      publish_at="2026-08-07T05:00:00.000Z", created_at=now, updated_at=now),
            build_job(job_id="job_이른", sequence=1, topic="먼저 글",
                      publish_at="2026-08-07T01:00:00.000Z", created_at=now, updated_at=now),
        ],
    )

    listed = await repository.list_user_jobs("user_1")

    assert [job.topic for job in listed] == ["먼저 글", "나중 글"]


async def test_list_user_jobs는_limit만큼만_돌려준다():
    """목록은 화면이 2초마다 다시 부르는 것이라 상한이 있어야 한다."""
    repository = InMemoryScheduledPostingRepository()
    now = _now()
    await repository.create_batch(
        build_batch(),
        [
            build_job(job_id=f"job_{index}", sequence=index, topic=f"소재 {index}",
                      created_at=now, updated_at=now)
            for index in range(5)
        ],
    )

    listed = await repository.list_user_jobs("user_1", limit=2)

    # 앞에서부터 자른다 — 잘려 나가는 것은 순서상 뒤(오래된·나중 순번)의 것이다.
    assert [job.topic for job in listed] == ["소재 0", "소재 1"]


async def test_find_user_batch는_소유자가_다르면_None이다():
    """다른 사용자의 batchId를 알아도 읽히지 않는다."""
    repository = InMemoryScheduledPostingRepository()
    await repository.create_batch(build_batch(user_id="user_1"), [])

    mine = await repository.find_user_batch("user_1", "batch_1")
    assert mine is not None
    assert mine.batch_id == "batch_1"

    assert await repository.find_user_batch("user_2", "batch_1") is None
    assert await repository.find_user_batch("user_1", "없는_배치") is None

    # 소유자를 받지 않는 find_batch는 내부(워커·복구)용이라 그대로 찾는다.
    assert await repository.find_batch("batch_1") is not None


async def test_find_user_job은_소유자가_다르면_None이다():
    """작업도 마찬가지다 — jobId만으로는 남의 것을 열 수 없다."""
    repository = InMemoryScheduledPostingRepository()
    await repository.create_batch(
        build_batch(user_id="user_1"), [build_job(user_id="user_1")]
    )

    mine = await repository.find_user_job("user_1", "job_1")
    assert mine is not None
    assert mine.job_id == "job_1"

    assert await repository.find_user_job("user_2", "job_1") is None
    assert await repository.find_user_job("user_1", "없는_작업") is None

    assert await repository.find_job("job_1") is not None


async def test_find_batch_by_client_request는_같은_사용자와_같은_키일_때만_찾는다():
    """같은 클릭이 두 번 도착했을 때만 접는다. 열쇠는 사용자별로 따로 논다."""
    repository = InMemoryScheduledPostingRepository()
    await repository.create_batch(
        build_batch(batch_id="batch_1", user_id="user_1", client_request_id="click_1"), []
    )
    await repository.create_batch(
        build_batch(batch_id="batch_2", user_id="user_2", client_request_id="click_1"), []
    )

    found = await repository.find_batch_by_client_request("user_1", "click_1")
    assert found is not None
    assert found.batch_id == "batch_1"

    # 같은 열쇠라도 사용자가 다르면 자기 것만 본다.
    other = await repository.find_batch_by_client_request("user_2", "click_1")
    assert other is not None
    assert other.batch_id == "batch_2"

    assert await repository.find_batch_by_client_request("user_3", "click_1") is None
    assert await repository.find_batch_by_client_request("user_1", "click_2") is None


# ------------------------------------------------------------------ 활성 배치


async def test_find_active_batch는_활성_상태의_배치만_찾는다():
    """멈춰 있어도(PAUSED·NEEDS_HUMAN) 그 배치는 아직 사용자의 것이다.

    반대로 끝난 배치(COMPLETED·STOPPED·FAILED)는 자리를 비켜야 새 예약을 시작할 수 있다.
    """
    활성 = [
        ScheduledBatchStatus.READY,
        ScheduledBatchStatus.RUNNING,
        ScheduledBatchStatus.PAUSE_REQUESTED,
        ScheduledBatchStatus.PAUSED,
        ScheduledBatchStatus.NEEDS_HUMAN,
        ScheduledBatchStatus.STOP_REQUESTED,
    ]
    for status in 활성:
        repository = InMemoryScheduledPostingRepository()
        await repository.create_batch(build_batch(status=status), [])

        found = await repository.find_active_batch("user_1")
        assert found is not None, f"{status}는 아직 진행 중인 배치다"
        assert found.status == status
        # 남의 배치는 잡히지 않는다.
        assert await repository.find_active_batch("user_2") is None

    끝난_상태 = [
        ScheduledBatchStatus.COMPLETED,
        ScheduledBatchStatus.STOPPED,
        ScheduledBatchStatus.FAILED,
    ]
    for status in 끝난_상태:
        repository = InMemoryScheduledPostingRepository()
        await repository.create_batch(build_batch(status=status), [])

        assert await repository.find_active_batch("user_1") is None, f"{status}는 끝난 배치다"


async def test_find_active_batch는_여러_개면_createdAt이_최신인_것을_돌려준다():
    repository = InMemoryScheduledPostingRepository()
    await repository.create_batch(
        build_batch(
            batch_id="batch_old",
            status=ScheduledBatchStatus.RUNNING,
            created_at="2026-08-01T00:00:00.000Z",
        ),
        [],
    )
    await repository.create_batch(
        build_batch(
            batch_id="batch_new",
            status=ScheduledBatchStatus.PAUSED,
            created_at="2026-08-03T00:00:00.000Z",
        ),
        [],
    )
    await repository.create_batch(
        build_batch(
            batch_id="batch_mid",
            status=ScheduledBatchStatus.READY,
            created_at="2026-08-02T00:00:00.000Z",
        ),
        [],
    )
    # 끝난 배치는 아무리 최신이어도 후보가 아니다.
    await repository.create_batch(
        build_batch(
            batch_id="batch_done",
            status=ScheduledBatchStatus.COMPLETED,
            created_at="2026-08-04T00:00:00.000Z",
        ),
        [],
    )

    found = await repository.find_active_batch("user_1")
    assert found is not None
    assert found.batch_id == "batch_new"

    # 그 배치가 닫히면 그다음으로 최신인 것이 잡힌다.
    최신 = await repository.find_batch("batch_new")
    await repository.save_batch(
        최신.model_copy(update={"status": ScheduledBatchStatus.STOPPED})
    )
    assert (await repository.find_active_batch("user_1")).batch_id == "batch_mid"


# ---------------------------------------------------------------------- 저장


async def test_save_batch와_save_job은_덮어쓰기다():
    """같은 id로 다시 저장하면 문서가 하나 더 생기지 않고 그 자리를 바꾼다."""
    repository = InMemoryScheduledPostingRepository()
    await repository.create_batch(build_batch(), [build_job()])

    배치 = await repository.find_batch("batch_1")
    await repository.save_batch(
        배치.model_copy(
            update={
                "status": ScheduledBatchStatus.RUNNING,
                "completed_count": 1,
                "current_job_id": "job_1",
            }
        )
    )

    stored = await repository.find_batch("batch_1")
    assert stored.status == ScheduledBatchStatus.RUNNING
    assert stored.completed_count == 1
    assert stored.current_job_id == "job_1"
    # 총 개수·소유자 같은 나머지 값은 그대로 남는다.
    assert stored.total_count == 2
    assert stored.user_id == "user_1"
    assert len(await repository.list_batches_by_status([ScheduledBatchStatus.RUNNING])) == 1
    assert await repository.list_batches_by_status([ScheduledBatchStatus.READY]) == []

    작업 = await repository.find_job("job_1")
    await repository.save_job(
        작업.model_copy(
            update={
                "status": ScheduledJobStatus.COMPLETED,
                "post_url": "https://blog.naver.com/u/1",
            }
        )
    )

    stored_job = await repository.find_job("job_1")
    assert stored_job.status == ScheduledJobStatus.COMPLETED
    assert stored_job.post_url == "https://blog.naver.com/u/1"
    assert stored_job.topic == "소재 1"
    assert len(await repository.list_jobs("batch_1")) == 1

    # 배치는 없던 id로 저장하면 새로 들어간다(Mongo 쪽 upsert=True와 같은 동작).
    await repository.save_batch(build_batch(batch_id="batch_9"))
    assert await repository.find_batch("batch_9") is not None


async def test_save_job은_없는_작업을_되살리지_않는다():
    """지운 작업을 상태 저장이 다시 만들어 넣으면 안 된다.

    워커가 작업을 집어 든 직후 사용자가 그것을 지우면, 곧이어 실행되는
    ``save_job(RUNNING)``이 upsert였을 때 지워진 문서를 되살렸다. 그러면 사용자가
    "이 소재는 쓰기 싫다"고 뺀 바로 그 소재로 글이 만들어져 **네이버에 발행된다** —
    삭제 기능의 목적을 정면으로 뒤집는다. 작업 문서는 create_batch에서만 생긴다.
    """
    repository = InMemoryScheduledPostingRepository()
    await repository.create_batch(build_batch(), [build_job()])

    작업 = await repository.find_job("job_1")
    await repository.delete_job("job_1")

    # 워커가 들고 있던 사본으로 상태를 저장한다 — 되살아나면 안 된다.
    await repository.save_job(작업.model_copy(update={"status": ScheduledJobStatus.RUNNING}))

    assert await repository.find_job("job_1") is None
    assert await repository.list_jobs("batch_1") == []


# ------------------------------------------------------------------------ 로그


async def test_append_batch_log는_로그를_덧붙이고_상한을_넘으면_앞에서부터_버린다():
    """배치 문서가 무한히 자라지 않게 마지막 MAX_BATCH_LOGS개만 남긴다."""
    repository = InMemoryScheduledPostingRepository()
    await repository.create_batch(
        build_batch(logs=[ScheduledLogEntry(at="2026-08-04T00:00:00.000Z", message="시작")]),
        [],
    )

    await repository.append_batch_log(
        "batch_1",
        ScheduledLogEntry(
            at="2026-08-04T00:00:01.000Z",
            message="둘째 줄",
            tone="success",
            job_id="job_1",
        ),
    )

    stored = await repository.find_batch("batch_1")
    assert [entry.message for entry in stored.logs] == ["시작", "둘째 줄"]
    assert stored.logs[-1].tone == "success"
    assert stored.logs[-1].job_id == "job_1"
    # 로그가 붙으면 배치도 그때 만져진 것으로 본다.
    assert stored.updated_at == "2026-08-04T00:00:01.000Z"

    for index in range(MAX_BATCH_LOGS + 50):
        await repository.append_batch_log(
            "batch_1",
            ScheduledLogEntry(at="2026-08-04T00:01:00.000Z", message=f"로그 {index}"),
        )

    stored = await repository.find_batch("batch_1")
    assert len(stored.logs) == MAX_BATCH_LOGS
    # 넣은 것은 모두 252줄(기존 2줄 + 250줄), 남는 것은 뒤에서 200줄이다.
    assert stored.logs[-1].message == f"로그 {MAX_BATCH_LOGS + 49}"
    assert stored.logs[0].message == "로그 50"
    # 앞에서부터 버렸으므로 맨 처음 줄은 사라져 있다.
    assert "시작" not in [entry.message for entry in stored.logs]


async def test_append_batch_log는_없는_배치를_조용히_넘긴다():
    """워커가 이미 지워진 배치에 로그를 남기려 해도 터지지 않는다."""
    repository = InMemoryScheduledPostingRepository()

    await repository.append_batch_log(
        "없는_배치", ScheduledLogEntry(at=_now(), message="아무도 못 볼 줄")
    )

    assert await repository.find_batch("없는_배치") is None


# ------------------------------------------------------------------ 복사 방어


async def test_저장소는_모델을_복사해_넣는다():
    """넣은 객체를 나중에 고쳐도 저장된 값은 바뀌지 않는다(model_copy 방어).

    호출자가 들고 있는 객체와 저장된 것이 같은 객체면, 저장하지 않은 변경이 조회에
    나타나고 저장한 변경은 되레 묻힌다.
    """
    repository = InMemoryScheduledPostingRepository()
    배치 = build_batch(logs=[ScheduledLogEntry(at=_now(), message="시작")])
    작업 = build_job()
    await repository.create_batch(배치, [작업])

    # (1) create_batch에 넘긴 객체를 호출자가 계속 고친다.
    배치.status = ScheduledBatchStatus.FAILED
    배치.completed_count = 99
    배치.logs.append(ScheduledLogEntry(at=_now(), message="끼어든 줄"))
    작업.status = ScheduledJobStatus.FAILED
    작업.topic = "바뀐 소재"

    stored_batch = await repository.find_batch("batch_1")
    assert stored_batch.status == ScheduledBatchStatus.READY
    assert stored_batch.completed_count == 0
    assert [entry.message for entry in stored_batch.logs] == ["시작"]
    stored_job = await repository.find_job("job_1")
    assert stored_job.status == ScheduledJobStatus.WAITING
    assert stored_job.topic == "소재 1"

    # (2) 꺼내 온 것을 고쳐도 저장소는 그대로다.
    stored_batch.status = ScheduledBatchStatus.STOPPED
    stored_batch.logs.clear()
    stored_job.topic = "또 바뀐 소재"
    listed = await repository.list_jobs("batch_1")
    listed[0].topic = "목록에서 바꾼 소재"

    다시 = await repository.find_batch("batch_1")
    assert 다시.status == ScheduledBatchStatus.READY
    assert [entry.message for entry in 다시.logs] == ["시작"]
    assert (await repository.find_job("job_1")).topic == "소재 1"
    assert (await repository.list_jobs("batch_1"))[0].topic == "소재 1"

    # (3) save_batch·save_job도 같은 방어를 한다.
    새_배치 = build_batch(batch_id="batch_2", status=ScheduledBatchStatus.RUNNING)
    await repository.save_batch(새_배치)
    새_배치.status = ScheduledBatchStatus.FAILED
    assert (await repository.find_batch("batch_2")).status == ScheduledBatchStatus.RUNNING

    # 작업은 create_batch로만 생기므로(save_job은 되살리지 않는다) 그 경로로 넣어 본다.
    새_작업 = build_job(job_id="job_2", batch_id="batch_2")
    await repository.create_batch(build_batch(batch_id="batch_3"), [새_작업])
    await repository.save_job(새_작업.model_copy(update={"sequence": 5}))
    새_작업.topic = "다른 소재"
    새_작업.status = ScheduledJobStatus.CANCELED
    assert (await repository.find_job("job_2")).topic == "소재 1"
    assert (await repository.find_job("job_2")).status == ScheduledJobStatus.WAITING

    # (4) 소유자 조회로 꺼낸 것도 사본이다.
    내_배치 = await repository.find_user_batch("user_1", "batch_1")
    내_배치.target_count = 123
    내_작업 = await repository.find_user_job("user_1", "job_1")
    내_작업.sequence = 123
    assert (await repository.find_batch("batch_1")).target_count == 2
    assert (await repository.find_job("job_1")).sequence == 0
