"""예약 발행 시각 — 간격이 아니라 **정한 날짜·시각에 올리는** 예약(2026-08-05).

시나리오는 작업 지시서의 열두 가지를 그대로 따른다. 실제 LLM·Mongo·셀레니움은 한 번도
부르지 않는다 — 저장소는 InMemory이고 기존 서비스 셋은 호출을 기록만 하는 가짜다
(test_scheduled_posting_service.py의 하네스를 그대로 쓴다).

여기서 보는 것은 두 가지다.

1. **약속한 시각에 올라가는가.** 원고는 시각보다 먼저 만들고(준비), 시각이 되면 발행한다.
2. **약속하지 않은 때에 올라가지 않는가.** 취소·중복·과거 시각·재시작이 그 반대편이다.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.errors import BlogTaskError
from app.modules.blog_task.locks import NoOpJobLease
from app.modules.scheduled_posting import service as service_module
from app.modules.scheduled_posting.models import (
    ScheduleMode,
    ScheduledBatch,
    ScheduledBatchStatus,
    ScheduledJob,
    ScheduledJobStatus,
    SchedulePlatform,
)
from app.modules.scheduled_posting.recovery import recover_active_batches
from app.modules.scheduled_posting.service import MAX_PUBLISH_ATTEMPTS
from app.modules.scheduled_posting.validation import validate_start_batch_request
from app.modules.scheduled_posting.worker import ScheduledPostingWorker
from app.shared import (
    PHASE_STEPS,
    BlogTaskStatus,
    FinalPost,
    PostingResultStatus,
    TaskPhase,
    TaskProgress,
)
from app.shared.format import now_iso

from test_scheduled_posting_service import (  # noqa: E402
    FakeBlogTaskService,
    build_service,
    make_posting_log,
    make_task,
    naver_saved,
)

pytestmark = pytest.mark.asyncio


def at(**delta) -> str:
    """지금부터 얼마 뒤의 절대 시각(UTC ISO). 클라이언트가 보내는 형식과 같다."""
    moment = datetime.now(timezone.utc) + timedelta(**delta)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def body(*schedules, interval_seconds: int = 15, timezone_name: str = "Asia/Seoul") -> dict:
    """예약 시작 요청 몸통. schedules가 있으면 절대 시각 방식이다."""
    return {
        "topics": [item["topic"] for item in schedules],
        "schedules": list(schedules),
        "intervalSeconds": interval_seconds,
        "timezone": timezone_name,
        "platform": "naver",
    }


def make_worker(service, repository) -> ScheduledPostingWorker:
    return ScheduledPostingWorker(service, repository, NoOpJobLease())


async def step(worker: ScheduledPostingWorker, batch) -> tuple[bool, float]:
    """절대 시각 배치를 한 걸음 진행시키고, **띄운 태스크가 끝날 때까지 기다린다.**

    워커는 2026-08-07부터 준비·발행을 각자 태스크로 떨어뜨리고 곧바로 돌아온다(원고를
    만드는 6분 동안 발행 시각을 못 보던 것을 고친 것이다). 테스트가 보고 싶은 것은
    '한 걸음이 끝난 뒤'의 상태이므로 여기서 마저 기다려 준다.

    돌려주는 값은 예전 run_scheduled_batch와 같은 (무언가 돌렸는가, 다음까지의 초)다.
    """
    wait = await worker.advance_scheduled_batch(batch)
    return await worker.wait_for_running(), wait


async def seed_absolute(
    repository,
    plan: list[tuple[str, str]],
    *,
    user_id: str = "user_1",
    interval_seconds: int = 15,
    statuses: list[ScheduledJobStatus] | None = None,
    post_ids: list[str | None] | None = None,
    immediates: list[bool] | None = None,
    prepared: bool = False,
    publishes: bool = True,
) -> ScheduledBatch:
    """(소재, 발행 시각) 목록으로 절대 시각 배치를 바로 심는다.

    ``immediates``는 그 줄이 **새 글 작성의 '지금 바로'로 걸린 작업**인지다(2026-08-13).
    그런 작업도 publish_at은 채워져 있어, 값만으로는 예약과 구별되지 않는다.
    """
    now = now_iso()
    jobs = [
        ScheduledJob(
            job_id=f"job_{index}",
            batch_id="batch_1",
            user_id=user_id,
            platform=SchedulePlatform.NAVER,
            sequence=index,
            topic=topic,
            publish_at=publish_at,
            starts_immediately=(immediates[index] if immediates else False),
            starts_from_prepared_post=prepared,
            publish_naver=publishes,
            publish_threads=False,
            timezone="Asia/Seoul",
            post_id=(post_ids[index] if post_ids else None),
            status=(statuses[index] if statuses else ScheduledJobStatus.WAITING),
            created_at=now,
            updated_at=now,
        )
        for index, (topic, publish_at) in enumerate(plan)
    ]
    batch = ScheduledBatch(
        batch_id="batch_1",
        user_id=user_id,
        platform=SchedulePlatform.NAVER,
        schedule_mode=ScheduleMode.ABSOLUTE,
        timezone="Asia/Seoul",
        status=ScheduledBatchStatus.READY,
        target_count=len(jobs),
        interval_seconds=interval_seconds,
        total_count=len(jobs),
        created_at=now,
        updated_at=now,
    )
    await repository.create_batch(batch, jobs)
    return batch


# ------------------------------------------------------ 1. 10분 뒤 한 건 예약


async def test_10분_뒤_한_건을_예약하면_그_시각이_저장된다(monkeypatch):
    """간격이 아니라 **절대 시각**이 저장되는지. 예약의 뼈대다."""
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    when = at(minutes=10)

    view = await service.start_batch("user_1", body({"topic": "여름 휴가", "publishAt": when}))

    assert view.batch.schedule_mode is ScheduleMode.ABSOLUTE
    assert view.batch.timezone == "Asia/Seoul"
    assert len(view.jobs) == 1
    assert view.jobs[0].publish_at == when
    assert view.jobs[0].status == ScheduledJobStatus.WAITING


async def test_예약한_시각이_되기_전에는_발행하지_않는다(monkeypatch):
    """준비(원고 만들기)는 미리 해도, 발행은 약속한 시각까지 기다린다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    # 아직 그 시각이 아니다 — 지금은 아무것도 시작할 때가 아니다. 준비를 앞당기는
    # 여유는 없다(2026-08-12에 없앴다).
    batch = await seed_absolute(repository, [("여름 휴가", at(seconds=3600))])
    worker = make_worker(service, repository)

    ran, _wait = await step(worker, batch)

    assert ran is False
    assert world.count("publish_blog_task") == 0
    assert world.count("create_blog_task") == 0


async def test_시각이_되면_준비한_원고를_발행한다(monkeypatch):
    """준비 → (시각) → 발행. 한 작업이 두 걸음으로 나뉘어 돈다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    # 작업 시각이 됐다 — 그 시각이 곧 '원고를 만들기 시작할 때'다(준비 여유 없음).
    batch = await seed_absolute(repository, [("여름 휴가", at(seconds=-1))])
    worker = make_worker(service, repository)

    # 1) 준비: 시각이 됐으므로 원고를 만든다. 같은 걸음에 올리지는 않는다 —
    #    발행 대상은 걸음을 시작할 때의 상태(WAITING)로 정해진다.
    ran, _ = await step(worker, batch)
    assert ran is True
    prepared = await repository.find_job("job_0")
    assert prepared.status == ScheduledJobStatus.READY_TO_PUBLISH
    assert world.count("generate_draft") == 1
    # 원고는 만들었지만 아직 올리지 않았다.
    assert world.count("publish_blog_task") == 0

    # 2) 시각이 됐다. 이제 발행한다(원고는 다시 만들지 않는다).
    await repository.save_job(prepared.model_copy(update={"publish_at": at(seconds=-1)}))
    batch = await repository.find_batch("batch_1")
    ran, _ = await step(worker, batch)

    assert ran is True
    published = await repository.find_job("job_0")
    assert published.status == ScheduledJobStatus.COMPLETED
    assert published.published_at is not None
    assert world.count("publish_blog_task") == 1
    assert world.count("generate_draft") == 1  # 원고를 다시 만들지 않았다


async def test_시각이_이미_지났는데_원고가_없으면_준비부터_하고_곧바로_올린다(monkeypatch):
    """서버가 꺼져 있던 사이에 시각이 지난 경우다. 늦더라도 올리는 것이 맞다 —
    사용자가 원한 것은 그 글이 올라가는 것이고, 조용히 버리는 쪽이 더 나쁘다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    batch = await seed_absolute(repository, [("여름 휴가", at(minutes=-30))])
    worker = make_worker(service, repository)

    # 1) 원고가 없으므로 준비부터 한다(발행 시각은 이미 지났다).
    ran, _ = await step(worker, batch)
    assert ran is True
    assert world.count("generate_draft") == 1
    assert (await repository.find_job("job_0")).status == ScheduledJobStatus.READY_TO_PUBLISH

    # 2) 준비가 끝나자마자 발행한다 — 다음 tick을 기다릴 뿐 더 미루지 않는다.
    ran, _ = await step(worker, await repository.find_batch("batch_1"))
    assert ran is True
    assert (await repository.find_job("job_0")).status == ScheduledJobStatus.COMPLETED


async def test_글마다_스레드를_따로_고를_수_있다(monkeypatch):
    """날짜·시각 방식에서는 플랫폼도 줄마다다. 배치 하나의 값으로 뭉치지 않는다."""
    naver_saved(monkeypatch, True)
    monkeypatch.setattr(service_module, "_threads_saved", lambda user_id: True)
    service, _repository, _ = build_service()

    view = await service.start_batch(
        "user_1",
        body(
            {"topic": "네이버만", "publishAt": at(hours=1), "publishThreads": False},
            {"topic": "스레드에도", "publishAt": at(hours=2), "publishThreads": True},
        ),
    )

    assert [job.publish_threads for job in view.jobs] == [False, True]


async def test_한_줄이라도_스레드를_골랐으면_연결을_먼저_확인한다(monkeypatch):
    """여기서 안 막으면 그 작업만 몇 분 뒤 발행 단계에서 죽는다."""
    naver_saved(monkeypatch, True)
    monkeypatch.setattr(service_module, "_threads_saved", lambda user_id: False)
    service, _repository, _ = build_service()

    with pytest.raises(BlogTaskError) as error:
        await service.start_batch(
            "user_1",
            body(
                {"topic": "네이버만", "publishAt": at(hours=1), "publishThreads": False},
                {"topic": "스레드에도", "publishAt": at(hours=2), "publishThreads": True},
            ),
        )

    assert error.value.code == "THREADS_NOT_CONNECTED"


async def test_글마다_네이버도_따로_끌_수_있다(monkeypatch):
    """2026-08-06 — 한 소재는 네이버에만, 다른 소재는 쓰레드에만 올릴 수 있다."""
    naver_saved(monkeypatch, True)
    monkeypatch.setattr(service_module, "_threads_saved", lambda user_id: True)
    service, _repository, _ = build_service()

    view = await service.start_batch(
        "user_1",
        body(
            {
                "topic": "네이버만",
                "publishAt": at(hours=1),
                "publishNaver": True,
                "publishThreads": False,
            },
            {
                "topic": "쓰레드만",
                "publishAt": at(hours=2),
                "publishNaver": False,
                "publishThreads": True,
            },
        ),
    )

    assert [job.publish_naver for job in view.jobs] == [True, False]
    assert [job.publish_threads for job in view.jobs] == [False, True]


async def test_모든_줄이_쓰레드만이면_네이버_계정을_묻지_않는다(monkeypatch):
    """쓰레드로만 쓰는 사람에게 네이버 로그인을 요구하지 않는다(2026-08-06)."""
    naver_saved(monkeypatch, False)
    monkeypatch.setattr(service_module, "_threads_saved", lambda user_id: True)
    service, _repository, _ = build_service()

    view = await service.start_batch(
        "user_1",
        body(
            {
                "topic": "쓰레드만",
                "publishAt": at(hours=1),
                "publishNaver": False,
                "publishThreads": True,
            },
        ),
    )

    assert [job.publish_naver for job in view.jobs] == [False]


async def test_시각_없는_schedules는_간격_방식이면서_글별_플랫폼을_싣는다(monkeypatch):
    """2026-08-06 — 간격 방식도 소재 줄마다 플랫폼을 고른다.

    예전에는 이 방식만 글별 설정을 보낼 자리가 없어 배치 하나의 값으로 뭉개졌다.
    화면은 줄에 '쓰레드'라고 적어 두고 요약은 '네이버 2건'이라고 말했다.
    """
    naver_saved(monkeypatch, True)
    monkeypatch.setattr(service_module, "_threads_saved", lambda user_id: True)
    service, _repository, _ = build_service()

    view = await service.start_batch(
        "user_1",
        {
            "topics": ["자동차", "롯데월드"],
            "schedules": [
                {"topic": "자동차", "publishNaver": False, "publishThreads": True},
                {"topic": "롯데월드", "publishNaver": True, "publishThreads": False},
            ],
            "intervalSeconds": 15,
            "platform": "naver",
        },
    )

    # 시각이 없으므로 간격 방식이다 — 순서도 입력한 줄 순서 그대로다.
    assert view.batch.schedule_mode is ScheduleMode.INTERVAL
    assert [job.topic for job in view.jobs] == ["자동차", "롯데월드"]
    assert [job.publish_at for job in view.jobs] == [None, None]
    assert [job.publish_naver for job in view.jobs] == [False, True]
    assert [job.publish_threads for job in view.jobs] == [True, False]
    # 간격 방식은 첫 작업이 곧바로 시작한다.
    assert view.jobs[0].scheduled_at is not None


async def test_시각이_있는_줄과_없는_줄을_섞어_받는다(monkeypatch):
    """2026-08-12에 규칙이 바뀌었다 — 예전에는 '모든 글에 있거나 없거나'로 막았다.

    이제 답이 있다: **시각을 적은 줄은 그 시각에, 안 적은 줄은 앞 글이 끝나면.**
    두 규칙이 한 배치 안에서 공존한다(「예약 포스팅」 탭의 줄마다 시각).
    """
    naver_saved(monkeypatch, True)
    service, _repository, _ = build_service()

    view = await service.start_batch(
        "user_1",
        {
            "topics": ["가", "나"],
            "schedules": [
                {"topic": "가", "publishAt": at(hours=1)},
                {"topic": "나"},
            ],
            "intervalSeconds": 15,
            "platform": "naver",
        },
    )

    # 한 줄이라도 시각이 있으면 절대 시각 방식이다 — 그래야 그 약속을 워커가 본다.
    assert view.batch.schedule_mode is ScheduleMode.ABSOLUTE
    assert [job.topic for job in view.jobs] == ["가", "나"]
    assert view.jobs[0].publish_at is not None
    # 시각을 적지 않은 줄은 **앞 줄을 가리킨다.** 그것이 '앞 글이 끝나면'의 뜻이다.
    assert view.jobs[1].publish_at is None
    assert view.jobs[1].after_job_id == view.jobs[0].job_id


async def test_어느_한_줄도_플랫폼이_없으면_거부한다(monkeypatch):
    """아무 데도 안 올라가는 줄을 만들어 두고 나중에 실패를 보게 하지 않는다."""
    naver_saved(monkeypatch, True)
    monkeypatch.setattr(service_module, "_threads_saved", lambda user_id: True)
    service, _repository, _ = build_service()

    with pytest.raises(BlogTaskError) as error:
        await service.start_batch(
            "user_1",
            body(
                {"topic": "네이버만", "publishAt": at(hours=1)},
                {
                    "topic": "어디에도 안 감",
                    "publishAt": at(hours=2),
                    "publishNaver": False,
                    "publishThreads": False,
                },
            ),
        )

    assert error.value.code == "VALIDATION_FAILED"
    assert "2번째" in error.value.message


# --------------------------------------------- 2. 서로 다른 시간으로 3건 예약


async def test_글_세_건을_서로_다른_시각에_예약한다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, _repository, _ = build_service()

    view = await service.start_batch(
        "user_1",
        body(
            {"topic": "첫째", "publishAt": at(hours=1)},
            {"topic": "둘째", "publishAt": at(hours=2)},
            {"topic": "셋째", "publishAt": at(hours=3)},
        ),
    )

    assert [job.topic for job in view.jobs] == ["첫째", "둘째", "셋째"]
    times = [job.publish_at for job in view.jobs]
    assert times == sorted(times)
    # 시각이 서로 다르다 — 하나의 간격에서 파생된 값이 아니다.
    assert len(set(times)) == 3


async def test_입력_순서가_아니라_시각_순서로_줄을_세운다(monkeypatch):
    """워커는 sequence 순으로 집는다. 등록 순서가 곧 발행 순서가 되면 안 된다."""
    naver_saved(monkeypatch, True)
    service, _repository, _ = build_service()

    view = await service.start_batch(
        "user_1",
        body(
            {"topic": "나중에 올릴 글", "publishAt": at(hours=5)},
            {"topic": "먼저 올릴 글", "publishAt": at(hours=1)},
        ),
    )

    assert [job.topic for job in view.jobs] == ["먼저 올릴 글", "나중에 올릴 글"]
    assert [job.sequence for job in view.jobs] == [0, 1]


# -------------------------------------------------------- 3. 예약 시간 변경


async def test_예약_시각을_바꾼다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    await seed_absolute(repository, [("여름 휴가", at(hours=1))])
    new_time = at(hours=5)

    view = await service.reschedule_job("user_1", "job_0", {"publishAt": new_time})

    changed = next(job for job in view.jobs if job.job_id == "job_0")
    assert changed.publish_at == new_time
    assert changed.status == ScheduledJobStatus.WAITING


async def test_시각을_바꿔도_다른_예약과_12분은_떨어져_있어야_한다(monkeypatch):
    """시작 화면에서만 간격을 보면 이 경로로 1분 간격 예약을 만들 수 있다(2026-08-07)."""
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    await seed_absolute(repository, [("첫째", at(hours=1)), ("둘째", at(hours=2))])

    with pytest.raises(BlogTaskError) as caught:
        await service.reschedule_job("user_1", "job_1", {"publishAt": at(hours=1, minutes=3)})
    assert "12분" in caught.value.message

    # 12분을 지키면 통과한다(2026-08-11 사용자 결정, 그전 10분).
    view = await service.reschedule_job(
        "user_1", "job_1", {"publishAt": at(hours=1, minutes=30)}
    )
    assert next(job for job in view.jobs if job.job_id == "job_1").publish_at is not None


async def test_이미_끝난_예약의_시각은_간격_계산에서_뺀다(monkeypatch):
    """발행이 끝났거나 취소된 예약은 앞으로의 발행 순서에 아무 영향이 없다."""
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    await seed_absolute(
        repository,
        [("이미 올라감", at(hours=1)), ("아직", at(hours=3))],
        statuses=[ScheduledJobStatus.COMPLETED, ScheduledJobStatus.WAITING],
    )

    # 완료된 예약과 3분밖에 안 떨어져 있어도 막지 않는다.
    view = await service.reschedule_job("user_1", "job_1", {"publishAt": at(hours=1, minutes=3)})
    assert next(job for job in view.jobs if job.job_id == "job_1").publish_at is not None


async def test_원고까지_만든_예약도_시각만_바꿀_수_있다(monkeypatch):
    """발행 대기 중인 글의 시각을 미루는 것은 흔한 일이다. 원고는 그대로 둔다."""
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    await seed_absolute(
        repository,
        [("여름 휴가", at(minutes=5))],
        statuses=[ScheduledJobStatus.READY_TO_PUBLISH],
        post_ids=["post_1"],
    )

    view = await service.reschedule_job("user_1", "job_0", {"publishAt": at(days=1)})

    changed = view.jobs[0]
    assert changed.status == ScheduledJobStatus.READY_TO_PUBLISH
    assert changed.post_id == "post_1"


async def test_발행_중이거나_이미_올라간_예약은_바꿀_수_없다(monkeypatch):
    """올라가는 중인 글의 시각을 고치면 화면과 실제 게시물이 다른 말을 하게 된다."""
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    await seed_absolute(
        repository,
        [("발행 중", at(minutes=1)), ("완료됨", at(minutes=2))],
        statuses=[ScheduledJobStatus.PUBLISHING, ScheduledJobStatus.COMPLETED],
    )

    for job_id in ("job_0", "job_1"):
        with pytest.raises(BlogTaskError) as error:
            await service.reschedule_job("user_1", job_id, {"publishAt": at(days=1)})
        assert error.value.code == "VALIDATION_FAILED"


async def test_실패한_예약에_새_시각을_주면_다시_살아난다(monkeypatch):
    """실패 사유와 자동 재시도 횟수는 지운다 — 새로 건 예약이기 때문이다."""
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    await seed_absolute(
        repository, [("여름 휴가", at(minutes=1))], statuses=[ScheduledJobStatus.FAILED]
    )
    failed = await repository.find_job("job_0")
    await repository.save_job(
        failed.model_copy(
            update={
                "error_code": "PUBLISH_FAILED",
                "error_message": "네이버 발행에 실패했습니다.",
                "publish_attempts": 3,
                "generated_at": now_iso(),
            }
        )
    )

    view = await service.reschedule_job("user_1", "job_0", {"publishAt": at(hours=2)})

    revived = view.jobs[0]
    # 원고가 이미 있으므로 발행만 기다리면 된다.
    assert revived.status == ScheduledJobStatus.READY_TO_PUBLISH
    assert revived.error_message is None
    assert revived.publish_attempts == 0


# ------------------------------------------------------------ 4. 예약 취소


async def test_예약을_취소하면_상태로_남고_문서는_지워지지_않는다(monkeypatch):
    """취소는 기록을 남기는 동작이다 — 삭제(delete_job)와 다르다."""
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    await seed_absolute(repository, [("여름 휴가", at(hours=1)), ("가을 여행", at(hours=2))])

    view = await service.cancel_job("user_1", "job_0")

    canceled = next(job for job in view.jobs if job.job_id == "job_0")
    assert canceled.status == ScheduledJobStatus.CANCELED
    # 문서가 남아 있어야 목록에서 '취소됨'으로 계속 보인다.
    assert await repository.find_job("job_0") is not None
    assert view.batch.canceled_count == 1


async def test_취소된_예약은_시각이_돼도_발행하지_않는다(monkeypatch):
    """취소 기능의 뜻이 여기 있다. 워커가 집지 않고, 서비스도 한 번 더 막는다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    batch = await seed_absolute(
        repository,
        [("여름 휴가", at(seconds=-1))],
        statuses=[ScheduledJobStatus.READY_TO_PUBLISH],
        post_ids=["post_1"],
    )
    await service.cancel_job("user_1", "job_0")

    ran, _ = await worker_tick(service, repository, batch)
    # 서비스를 직접 불러도 실행되지 않아야 한다(워커만 믿지 않는다).
    assert await service.execute_job("job_0", publish=True) is None

    assert ran is False
    assert world.count("publish_blog_task") == 0


async def worker_tick(service, repository, batch):
    worker = make_worker(service, repository)
    fresh = await repository.find_batch(batch.batch_id)
    return await step(worker, fresh)


async def test_이미_발행된_예약은_취소할_수_없다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    await seed_absolute(
        repository, [("여름 휴가", at(hours=1))], statuses=[ScheduledJobStatus.COMPLETED]
    )

    with pytest.raises(BlogTaskError) as error:
        await service.cancel_job("user_1", "job_0")
    assert error.value.code == "VALIDATION_FAILED"


# -------------------------------------------------------- 5. 과거 시간 입력


async def test_지난_시각으로는_예약할_수_없다(monkeypatch):
    """워커는 '시각이 지났으면 지금 올린다'로 돈다. 과거를 받으면 저장하자마자 발행된다 —
    사용자가 원한 것은 예약이지 즉시 발행이 아니다."""
    naver_saved(monkeypatch, True)
    service, _repository, _ = build_service()

    with pytest.raises(BlogTaskError) as error:
        await service.start_batch(
            "user_1", body({"topic": "여름 휴가", "publishAt": at(hours=-1)})
        )

    assert error.value.code == "VALIDATION_FAILED"
    assert "지난 시각" in error.value.message


async def test_지난_시각으로_변경하는_것도_막는다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    await seed_absolute(repository, [("여름 휴가", at(hours=1))])

    with pytest.raises(BlogTaskError):
        await service.reschedule_job("user_1", "job_0", {"publishAt": at(hours=-2)})


async def test_시간대_오프셋이_없는_시각은_거부한다(monkeypatch):
    """'2026-08-06T15:00'만 받으면 서울의 3시인지 런던의 3시인지 알 수 없다."""
    naver_saved(monkeypatch, True)
    service, _repository, _ = build_service()

    with pytest.raises(BlogTaskError) as error:
        await service.start_batch(
            "user_1", body({"topic": "여름 휴가", "publishAt": "2126-08-06T15:00:00"})
        )
    assert "오프셋" in error.value.message


async def test_같은_순간이면_어느_시간대로_적었든_같은_값으로_저장된다():
    """서울의 15시와 UTC의 6시는 같은 순간이다. 저장은 언제나 UTC 한 가지 기준이다.

    시간대 변환이 서버에서 일어나지 않는다는 것을 이 한 줄이 보여 준다 — 클라이언트가
    붙여 보낸 오프셋을 그대로 UTC로 정규화할 뿐이다.
    """
    moment = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    seoul = validate_start_batch_request(
        body({"topic": "글", "publishAt": moment.astimezone(
            timezone(timedelta(hours=9))
        ).isoformat()})
    )
    utc = validate_start_batch_request(
        body({"topic": "글", "publishAt": moment.isoformat().replace("+00:00", "Z")})
    )

    assert seoul.schedules[0].publish_at == utc.schedules[0].publish_at
    assert seoul.schedules[0].publish_at.endswith("Z")


# ------------------------------------------------- 6. 글과 글 사이의 최소 간격


async def test_예약_시각이_12분보다_붙어_있으면_거부한다(monkeypatch):
    """'9시에 두 편'은 **지킬 수 없는 약속**이라 이제 받지 않는다(2026-08-07 사용자 결정).

    발행은 한 번에 하나씩 돈다(크롬 프로필이 하나다). 실측으로 발행 동작 자체가
    네이버 26~35초, 스레드 1분 32초~2분 32초 걸리므로, 촘촘한 예약은 뒤 글이 반드시
    자기 시각을 넘긴다. 예전에는 이것을 허용하고 "순서대로 나간다"고만 적어 두었다.
    """
    naver_saved(monkeypatch, True)
    service, _repository, _world = build_service()
    same = at(hours=1)

    with pytest.raises(BlogTaskError) as caught:
        await service.start_batch(
            "user_1",
            body({"topic": "첫째", "publishAt": same}, {"topic": "둘째", "publishAt": same}),
        )
    assert "12분" in caught.value.message

    # 11분도 모자란다 — 경계는 12분이다.
    with pytest.raises(BlogTaskError):
        await service.start_batch(
            "user_1",
            body(
                {"topic": "첫째", "publishAt": at(hours=1)},
                {"topic": "둘째", "publishAt": at(hours=1, minutes=11)},
            ),
        )


async def test_간격은_입력_순서가_아니라_시각_순으로_본다(monkeypatch):
    """3시·1시 순으로 입력해도 실제로 올라가는 순서는 1시·3시다. 간격은 그 순서에서 잰다."""
    naver_saved(monkeypatch, True)
    service, _repository, _world = build_service()

    # 거꾸로 넣었지만 두 시각은 2시간 떨어져 있다 — 통과해야 한다.
    view = await service.start_batch(
        "user_1",
        body(
            {"topic": "나중", "publishAt": at(hours=3)},
            {"topic": "먼저", "publishAt": at(hours=1)},
        ),
    )
    assert len(view.jobs) == 2

    # 거꾸로 넣은 데다 5분밖에 안 떨어져 있으면 거부한다.
    with pytest.raises(BlogTaskError):
        await service.start_batch(
            "user_1",
            body(
                {"topic": "나중", "publishAt": at(hours=5, minutes=5)},
                {"topic": "먼저", "publishAt": at(hours=5)},
            ),
        )


async def test_시각이_된_예약도_한_tick에_한_건씩만_발행한다(monkeypatch):
    """간격을 지킨 두 건이라도 시각이 함께 지났으면(서버가 멈춰 있었다) 한 번에 하나씩."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()

    view = await service.start_batch(
        "user_1",
        body(
            {"topic": "첫째", "publishAt": at(hours=1)},
            {"topic": "둘째", "publishAt": at(hours=1, minutes=15)},
        ),
    )

    assert len(view.jobs) == 2

    # 시각이 됐을 때 한 tick에 한 건씩만 발행한다(크롬을 두 개 띄우지 않는다).
    for job in view.jobs:
        stored = await repository.find_job(job.job_id)
        await repository.save_job(
            stored.model_copy(
                update={
                    "status": ScheduledJobStatus.READY_TO_PUBLISH,
                    "post_id": f"post_{job.sequence + 1}",
                    "publish_at": at(seconds=-1),
                }
            )
        )
    FakeBlogTaskService(world)  # 가짜 세계에 글을 만들어 두기 위한 참조
    for job in view.jobs:
        world.put(_stub_task(world, f"post_{job.sequence + 1}"))

    batch = await repository.find_batch(view.batch.batch_id)
    ran, _ = await worker_tick(service, repository, batch)
    assert ran is True
    assert world.count("publish_blog_task") == 1

    ran, _ = await worker_tick(service, repository, batch)
    assert ran is True
    assert world.count("publish_blog_task") == 2


def _stub_task(world, post_id: str):
    """발행만 시키기 위해 원고까지 끝난 글을 가짜 세계에 심는다."""
    from app.shared import BlogTaskStatus, FinalPost

    from test_scheduled_posting_service import make_task

    return make_task(
        post_id=post_id,
        status=BlogTaskStatus.READY_TO_PUBLISH,
        final_post=FinalPost(
            title="제목", body="본문", hashtags=["a"], html_content="<p>본문</p>"
        ),
    )


# ------------------------------------------------- 7. 서버 재시작 후 예약 유지


async def test_서버가_다시_떠도_예약_시각은_그대로다(monkeypatch):
    """예약은 DB에 있다. 프런트 타이머가 아니라 문서가 약속을 들고 있다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    when = at(hours=3)
    await seed_absolute(
        repository,
        [("여름 휴가", when)],
        statuses=[ScheduledJobStatus.READY_TO_PUBLISH],
        post_ids=["post_1"],
    )
    world.put(_stub_task(world, "post_1"))

    await recover_active_batches(repository, service._blog_tasks)

    recovered = await repository.find_job("job_0")
    # 예약 시각은 그대로다 — 재시도하면 그 시각의 예약으로 이어진다.
    assert recovered.publish_at == when
    # 원고까지 끝난 예약도 **정지한다**(2026-08-12 사용자 지시). 원고는 그대로 두되
    # 서버가 다시 켜졌다고 저절로 올라가지 않는다 — 사람이 재시도를 눌러야 한다.
    assert recovered.status == ScheduledJobStatus.FAILED
    assert recovered.error_code == "STOPPED_BY_RESTART"
    assert world.count("publish_blog_task") == 0


async def test_재시작_뒤에도_취소된_예약은_취소된_채로_남는다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    await seed_absolute(
        repository, [("여름 휴가", at(hours=1))], statuses=[ScheduledJobStatus.CANCELED]
    )

    await recover_active_batches(repository, service._blog_tasks)

    assert (await repository.find_job("job_0")).status == ScheduledJobStatus.CANCELED


# --------------------------------------------------- 8. 중복 실행 방지


async def test_이미_올라간_글은_시각이_다시_와도_다시_올리지_않는다(monkeypatch):
    """중복 게시는 되돌릴 수 없다. 발행 성공 기록이 있으면 발행 단계를 건너뛴다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    await seed_absolute(
        repository,
        [("여름 휴가", at(seconds=-1))],
        statuses=[ScheduledJobStatus.READY_TO_PUBLISH],
        post_ids=["post_1"],
    )
    world.put(_stub_task(world, "post_1"))

    # 첫 발행.
    await service.execute_job("job_0", publish=True)
    assert world.count("publish_blog_task") == 1

    # 같은 작업을 한 번 더 부른다(워커가 두 번 집었거나, 재시작 뒤 다시 왔다).
    await service.execute_job("job_0", publish=True)

    assert world.count("publish_blog_task") == 1
    assert (await repository.find_job("job_0")).status == ScheduledJobStatus.COMPLETED


async def test_완료된_작업은_실행_요청을_받아도_돌지_않는다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    await seed_absolute(
        repository, [("여름 휴가", at(seconds=-1))], statuses=[ScheduledJobStatus.COMPLETED]
    )

    assert await service.execute_job("job_0", publish=True) is None
    assert world.count("publish_blog_task") == 0


# ------------------------------------------- 9·10. 발행 실패와 자동 재시도


async def test_발행에_실패하면_사유와_시도_시각이_남는다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    world.publish_result = PostingResultStatus.FAIL
    world.publish_error_message = "네이버가 응답하지 않습니다."
    await seed_absolute(
        repository,
        [("여름 휴가", at(seconds=-1))],
        statuses=[ScheduledJobStatus.READY_TO_PUBLISH],
        post_ids=["post_1"],
    )
    world.put(_stub_task(world, "post_1"))

    await service.execute_job("job_0", publish=True)

    failed = await repository.find_job("job_0")
    assert failed.error_message == "네이버가 응답하지 않습니다."
    assert failed.last_attempt_at is not None
    assert failed.publish_attempts == 1


async def test_발행_실패는_뒤로_미뤄_자동으로_다시_시도한다(monkeypatch):
    """발행기가 '실패'라고 분명히 말한 경우만이다. 결과를 알 수 없는 실패는
    recovery가 사람 확인으로 돌린다(그것까지 자동으로 다시 올리면 중복 게시다)."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    world.publish_result = PostingResultStatus.FAIL
    when = at(seconds=-1)
    await seed_absolute(
        repository,
        [("여름 휴가", when)],
        statuses=[ScheduledJobStatus.READY_TO_PUBLISH],
        post_ids=["post_1"],
    )
    world.put(_stub_task(world, "post_1"))

    await service.execute_job("job_0", publish=True)

    retrying = await repository.find_job("job_0")
    # 다시 발행할 자리로 돌아가되, 시각은 뒤로 밀렸다(곧바로 다시 두드리지 않는다).
    assert retrying.status == ScheduledJobStatus.READY_TO_PUBLISH
    assert retrying.publish_at > when


async def test_정해진_횟수를_넘기면_실패로_닫고_사람에게_넘긴다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    world.publish_result = PostingResultStatus.FAIL
    await seed_absolute(
        repository,
        [("여름 휴가", at(seconds=-1))],
        statuses=[ScheduledJobStatus.READY_TO_PUBLISH],
        post_ids=["post_1"],
    )
    world.put(_stub_task(world, "post_1"))

    for _ in range(MAX_PUBLISH_ATTEMPTS):
        job = await repository.find_job("job_0")
        # 재시도 시각이 됐다고 치고 다시 부른다.
        await repository.save_job(job.model_copy(update={"publish_at": at(seconds=-1)}))
        await service.execute_job("job_0", publish=True)

    final = await repository.find_job("job_0")
    assert final.status == ScheduledJobStatus.FAILED
    assert final.publish_attempts == MAX_PUBLISH_ATTEMPTS


# ----------------------------------------------- 11. 즉시 발행 회귀(간격 방식)


async def test_간격_방식_예약은_예전과_똑같이_돈다(monkeypatch):
    """절대 시각을 넣지 않은 요청은 예전 그대로여야 한다 — 옛 클라이언트와 돌고 있는
    배치가 여기 걸려 있다."""
    naver_saved(monkeypatch, True)
    service, _repository, _ = build_service()

    view = await service.start_batch(
        "user_1", {"topics": ["첫 소재", "둘째 소재"], "intervalSeconds": 600}
    )

    assert view.batch.schedule_mode is ScheduleMode.INTERVAL
    assert view.batch.interval_seconds == 600
    # 절대 시각은 없다. 앞 글이 끝나는 대로 이어서 올라간다.
    assert all(job.publish_at is None for job in view.jobs)


async def test_간격_방식_배치는_한_작업을_끝까지_끌고_간다(monkeypatch):
    """예전 경로(run_next_job)가 그대로 산다 — 원고 생성부터 발행까지 한 번에."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    from test_scheduled_posting_service import seed_batch

    await seed_batch(repository, ["여름 휴가"], interval_seconds=15)
    worker = make_worker(service, repository)

    ran = await worker.run_next_job("batch_1")

    assert ran is True
    assert world.count("generate_draft") == 1
    assert world.count("publish_blog_task") == 1
    assert (await repository.find_job("job_0")).status == ScheduledJobStatus.COMPLETED


# ------------------------------- 12. 원고는 병렬로, 발행은 하나씩 정각에


async def test_절대_시각_예약의_원고는_동시에_만든다(monkeypatch):
    """2026-08-07 사용자 결정 — "차라리 병렬로 동시에 원고생성 진행하게 하고
    플랫폼에 게시만 지정한 시각대로 게시하게 하자".

    예전에는 생성 간격(interval_seconds)이 다음 준비를 막았다. 원고 한 편이 실측
    6분 27초(중앙값)이므로 셋째 글은 20분 뒤에야 시작됐고, 예약 시각이 그보다 이르면
    반드시 늦었다. 이제 절대 시각 예약에서는 그 간격을 보지 않는다.
    """
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    batch = await seed_absolute(
        repository,
        # 셋 다 작업 시각이 됐다 — 지금 만들기 시작할 때다.
        [("첫째", at(seconds=-30)), ("둘째", at(seconds=-20)), ("셋째", at(seconds=-10))],
        interval_seconds=3600,  # 생성 간격을 아주 크게 둬도 막지 못한다
    )
    worker = make_worker(service, repository)

    ran, _ = await step(worker, batch)

    assert ran is True
    # 세 편을 한 걸음에 함께 만들었다.
    assert world.count("generate_draft") == 3
    for job_id in ("job_0", "job_1", "job_2"):
        assert (await repository.find_job(job_id)).status == ScheduledJobStatus.READY_TO_PUBLISH


async def test_원고를_한꺼번에_만들어도_상한을_넘지_않는다(monkeypatch):
    """전부 동시에 돌리지 않는다 — 한 편이 LLM 호출 수십 번과 이미지 생성을 쓴다."""
    from app.modules.scheduled_posting.worker import MAX_CONCURRENT_PREPARE

    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    많음 = MAX_CONCURRENT_PREPARE + 2
    batch = await seed_absolute(
        repository,
        # 전부 시각이 됐고, 이른 것부터 집는다.
        [(f"소재 {i}", at(seconds=-(많음 - i) * 10)) for i in range(많음)],
    )
    worker = make_worker(service, repository)

    ran, _ = await step(worker, batch)

    assert ran is True
    # 한 걸음에 상한만큼만 시작했다. 나머지는 자리가 나면 다음 걸음에 집는다.
    assert world.count("generate_draft") == MAX_CONCURRENT_PREPARE
    assert (await repository.find_job(f"job_{많음 - 1}")).status == ScheduledJobStatus.WAITING


async def test_즉시와_예약이_섞여도_원고는_상한만큼_함께_만든다(monkeypatch):
    """2026-08-13 사용자 확인 사항.

    시각을 안 정한 작업은 걸리는 즉시, 정한 작업은 그 시각이 되면 원고 생성에 들어간다.
    둘이 겹치는 순간에도 **합쳐서** 상한(MAX_CONCURRENT_PREPARE)까지만 돈다 — 종류마다
    따로 세면 상한이 두 배가 된다.
    """
    from app.modules.scheduled_posting.worker import MAX_CONCURRENT_PREPARE

    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    batch = await seed_absolute(
        repository,
        # 즉시 둘 + 시각이 이미 지난 예약 둘 = 넷. 상한은 셋이다.
        [
            ("즉시 1", at(seconds=-40)),
            ("즉시 2", at(seconds=-30)),
            ("예약 1", at(seconds=-20)),
            ("예약 2", at(seconds=-10)),
        ],
        immediates=[True, True, False, False],
    )
    worker = make_worker(service, repository)

    ran, _ = await step(worker, batch)

    assert ran is True
    assert world.count("generate_draft") == MAX_CONCURRENT_PREPARE
    # 남은 하나는 자리가 나면 다음 걸음에 집는다.
    assert (await repository.find_job("job_3")).status == ScheduledJobStatus.WAITING


async def _prepared_task(world, post_id: str):
    """방향까지 골라 둔 글 — 새 글 작성에서 넘어온 작업이 가리키는 모양이다."""
    from app.shared import SelectedIntent

    return world.put(
        make_task(
            post_id=post_id,
            status=BlogTaskStatus.INTENT_SELECTED,
            selected_intent=SelectedIntent(
                intent_id="intent_1",
                title="고른 방향",
                target_reader="독자",
                rationale="근거",
                keywords=[],
                sources=[],
                selected_at=now_iso(),
            ),
        )
    )


async def test_올릴_곳이_없으면_원고까지_만들고_작업이_끝난다(monkeypatch):
    """2026-08-13 사용자 지시: "플랫폼 선택안했는데 왜 발행을 하려고 그래.
    원고생성 완료하면 작업 끝이지"

    예전에는 이런 작업도 '발행 대기'에 세웠다. 워커는 올릴 곳이 없는 작업을 발행
    후보에서 빼므로 그 자리에서 영영 움직이지 않았고, 배치도 닫히지 않았다.
    """
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    batch = await seed_absolute(
        repository, [("소재", at(seconds=-10))], publishes=False
    )
    worker = make_worker(service, repository)

    await step(worker, batch)

    job = await repository.find_job("job_0")
    assert job.status == ScheduledJobStatus.COMPLETED
    # 올라간 적이 없으므로 발행 시각을 찍지 않는다 — 찍으면 발행된 글로 읽힌다.
    assert job.published_at is None
    assert world.count("publish_blog_task") == 0
    # 배치도 닫힌다. '발행 대기'에 남아 있으면 _close_if_done이 진행 중으로 본다.
    assert (await repository.find_batch("batch_1")).status == ScheduledBatchStatus.COMPLETED


async def test_올릴_곳이_있는_작업은_예전처럼_발행_대기에_선다(monkeypatch):
    """회귀 고정 — 규칙이 서로 새지 않는다."""
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    await seed_absolute(repository, [("소재", at(hours=5))])

    # 시각이 아직 멀어 준비만 돈다. 준비가 끝나면 발행 대기 자리다.
    await service.execute_job("job_0", publish=False)

    assert (await repository.find_job("job_0")).status == ScheduledJobStatus.READY_TO_PUBLISH


async def test_시각을_정하지_않은_작업은_자료를_다시_모으지_않는다(monkeypatch):
    """2026-08-13. 검증 화면에서 사용자가 보고 고른 자료가 그대로 원고에 쓰여야 한다 —
    여기서 새로 모으면 그 선택이 통째로 버려진다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    await _prepared_task(world, "post_now")
    batch = await seed_absolute(
        repository,
        [("즉시 글", at(seconds=-10))],
        immediates=[True],
        prepared=True,
        post_ids=["post_now"],
    )
    worker = make_worker(service, repository)

    await step(worker, batch)

    assert world.count("refresh_selected_intent_sources") == 0
    assert world.count("generate_draft") == 1


async def test_시각을_정한_작업은_그_시각에_자료를_새로_모은다(monkeypatch):
    """회귀 고정(2026-08-11) — 며칠 뒤에 쓸 글의 자료는 그때 모아야 한다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    await _prepared_task(world, "post_later")
    batch = await seed_absolute(
        repository,
        [("예약 글", at(seconds=-10))],
        prepared=True,
        post_ids=["post_later"],
    )
    worker = make_worker(service, repository)

    await step(worker, batch)

    assert world.count("refresh_selected_intent_sources") == 1


async def test_예약_원고가_먼저_끝나도_즉시_작업이_먼저_발행된다(monkeypatch):
    """2026-08-13 사용자 지시. 예약 글은 원고를 다 만들어 두고도 기다린다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    batch = await seed_absolute(
        repository,
        [("즉시 글", at(seconds=-30)), ("예약 글", at(seconds=-10))],
        immediates=[True, False],
        statuses=[ScheduledJobStatus.RUNNING, ScheduledJobStatus.READY_TO_PUBLISH],
        post_ids=["post_now", "post_later"],
    )
    worker = make_worker(service, repository)

    await step(worker, batch)

    # 예약 글은 원고가 준비됐지만 아직 올라가지 않았다.
    assert world.count("publish_blog_task") == 0
    assert (await repository.find_job("job_1")).status == ScheduledJobStatus.READY_TO_PUBLISH


async def test_발행은_원고를_만드는_중에도_시각을_지킨다(monkeypatch):
    """예전에는 준비가 tick을 통째로 붙잡아, 원고를 만드는 6분 사이에 다른 글의 발행
    시각이 지나가도 그 tick이 끝나야 알았다. 이제 둘이 갈라져 있다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    batch = await seed_absolute(
        repository,
        [("올릴 글", at(seconds=-2)), ("다음 글", at(seconds=-1))],
        statuses=[ScheduledJobStatus.READY_TO_PUBLISH, ScheduledJobStatus.WAITING],
        post_ids=["post_1", None],
        interval_seconds=3600,
    )
    world.put(_stub_task(world, "post_1"))
    # 생성 간격이 남아 있어도 이제 준비를 막지 않는다.
    await repository.save_batch(batch.model_copy(update={"next_run_at": at(minutes=30)}))
    worker = make_worker(service, repository)

    ran, _ = await step(worker, await repository.find_batch("batch_1"))

    assert ran is True
    # 발행과 준비가 한 걸음에 나란히 돌았다.
    assert world.count("publish_blog_task") == 1
    assert world.count("generate_draft") == 1
    assert (await repository.find_job("job_0")).status == ScheduledJobStatus.COMPLETED
    assert (await repository.find_job("job_1")).status == ScheduledJobStatus.READY_TO_PUBLISH


async def test_발행은_한_번에_하나씩만_돈다(monkeypatch):
    """크롬 프로필이 하나라 발행은 겹칠 수 없다 — 준비가 병렬이 돼도 이 규칙은 그대로다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    batch = await seed_absolute(
        repository,
        [("먼저", at(seconds=-120)), ("나중", at(seconds=-60))],
        statuses=[ScheduledJobStatus.READY_TO_PUBLISH, ScheduledJobStatus.READY_TO_PUBLISH],
        post_ids=["post_1", "post_2"],
    )
    world.put(_stub_task(world, "post_1"))
    world.put(_stub_task(world, "post_2"))
    worker = make_worker(service, repository)

    # 둘 다 시각이 지났지만 한 걸음에 한 건만 올라간다.
    ran, _ = await step(worker, batch)
    assert ran is True
    assert world.count("publish_blog_task") == 1
    # 시각이 이른 쪽이 먼저다.
    assert (await repository.find_job("job_0")).status == ScheduledJobStatus.COMPLETED
    assert (await repository.find_job("job_1")).status == ScheduledJobStatus.READY_TO_PUBLISH

    ran, _ = await step(worker, await repository.find_batch("batch_1"))
    assert ran is True
    assert world.count("publish_blog_task") == 2


# ------------------------------------------------------------ 예약 목록


async def test_예약_목록은_배치를_넘나들며_시각_순으로_나온다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    await seed_absolute(
        repository,
        [("나중 글", at(hours=5)), ("먼저 글", at(hours=1))],
        post_ids=["post_1", None],
    )
    world.put(_stub_task(world, "post_1"))

    items = await service.list_scheduled_jobs("user_1")

    assert [item.job.topic for item in items] == ["먼저 글", "나중 글"]
    # 원고를 만든 글은 제목을, 아직인 글은 None을 준다(화면이 소재를 대신 쓴다).
    # 원고가 있는 쪽은 '나중 글'(post_1)이다.
    assert items[0].title is None
    assert items[1].title == "제목"
    assert items[0].batch_status == ScheduledBatchStatus.READY


async def test_예약_목록은_글을_하나씩_읽지_않는다(monkeypatch):
    """작업이 몇 건이든 **조회는 몇 번뿐**이다(2026-08-06).

    예전에는 작업마다 글 한 편(제목을 얻으려고 원고 전체)과 배치 하나를 따로 읽었다.
    이 목록은 화면이 2초마다 다시 부르는 것이라, 작업이 쌓일수록 서버가 자기 폴링에
    눌려 **작업 큐가 갱신되지 않았다** — 글이 만들어지는 중인데도 화면은 '대기'였다.
    """
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    await seed_absolute(
        repository,
        [(f"소재 {index}", at(hours=index + 1)) for index in range(6)],
        post_ids=[f"post_{index}" for index in range(6)],
    )
    for index in range(6):
        world.put(_stub_task(world, f"post_{index}"))
    world.calls.clear()

    items = await service.list_scheduled_jobs("user_1")

    assert len(items) == 6
    assert all(item.title == "제목" for item in items)
    # 제목·글 상태·진행 칸을 **한 번에** 물어본다 — 글을 하나씩 읽지 않는다.
    assert world.count("get_post_summaries") == 1
    assert world.count("get_blog_task") == 0


async def test_실패한_작업도_글이_실제로_발행됐으면_그_주소를_함께_준다(monkeypatch):
    """작업의 상태는 그 실행이 끝났을 때의 **마지막 기억**이지 글의 지금 상태가 아니다.

    2026-08-06 사용자 신고: "발행내역에서는 실패라고 뜨는데 내 글 목록에서는 글이
    완성되어 있고 몇 개는 발행까지 됐다." 실제로 갈라지는 경로가 있다 — 사용자가 '내 글
    목록'에서 직접 발행하거나, 같은 글을 쥐고 있던 다른 실행이 원고를 끝내는 경우다.

    목록이 둘 다 실어 보내야 화면이 사실대로 말할 수 있다.
    """
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    await seed_absolute(
        repository,
        [("직접 올린 소재", at(hours=1))],
        post_ids=["post_직접"],
        statuses=[ScheduledJobStatus.FAILED],
    )
    world.put(
        make_task(
            post_id="post_직접",
            status=BlogTaskStatus.POSTED,
            final_post=FinalPost(
                title="직접 올린 글", body="본문", hashtags=[], html_content="<p>본문</p>"
            ),
            posting_logs=[make_posting_log(post_url="https://blog.naver.com/u/직접")],
        )
    )

    (item,) = await service.list_scheduled_jobs("user_1")

    # 작업은 실패 그대로 남는다 — 있었던 일을 지어내지 않는다.
    assert item.job.status == ScheduledJobStatus.FAILED
    # 그런데 글은 올라가 있다. 화면이 그 사실을 말할 수 있어야 한다.
    assert item.post_status == BlogTaskStatus.POSTED
    assert item.published_url == "https://blog.naver.com/u/직접"
    assert item.title == "직접 올린 글"


async def test_실패한_발행_기록의_주소는_발행됐다고_치지_않는다(monkeypatch):
    """실패 기록에도 주소 칸은 있다. 결과를 함께 보지 않으면 안 올라간 글이 올라간 것이 된다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    await seed_absolute(
        repository,
        [("못 올린 소재", at(hours=1))],
        post_ids=["post_실패"],
        statuses=[ScheduledJobStatus.FAILED],
    )
    world.put(
        make_task(
            post_id="post_실패",
            status=BlogTaskStatus.FAILED,
            posting_logs=[
                make_posting_log(
                    result=PostingResultStatus.FAIL,
                    post_url="https://blog.naver.com/u/찌꺼기",
                )
            ],
        )
    )

    (item,) = await service.list_scheduled_jobs("user_1")

    assert item.published_url is None


async def test_원고를_만드는_중이면_지금_어느_칸인지_함께_준다(monkeypatch):
    """화면이 '원고 생성 중'에 5~8분씩 멈춰 보이던 것을 푼다(2026-08-06 사용자 요청).

    서버는 그 사이 네 칸을 지나며 blogTask에 기록해 두고 있었는데, 예약 목록이 그걸
    싣지 않아 화면까지 오지 못했다.
    """
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    await seed_absolute(
        repository,
        [("쓰는 중인 소재", at(hours=1))],
        post_ids=["post_쓰는중"],
        statuses=[ScheduledJobStatus.RUNNING],
    )
    world.put(
        make_task(
            post_id="post_쓰는중",
            status=BlogTaskStatus.GENERATING,
            progress=TaskProgress(
                phase=TaskPhase.DRAFT,
                step=4,
                total_steps=4,
                label="사실 검수·문장 다듬기",
                steps=list(PHASE_STEPS[TaskPhase.DRAFT]),
                started_at=now_iso(),
                updated_at=now_iso(),
            ),
        )
    )

    (item,) = await service.list_scheduled_jobs("user_1")

    assert item.progress is not None
    assert item.progress.label == "사실 검수·문장 다듬기"
    assert (item.progress.step, item.progress.total_steps) == (4, 4)


async def test_작업_현황_줄도_목록이_함께_준다(monkeypatch):
    """2026-08-12 사용자 신고 — "작업현황 로그도 상세하게 안뜨네".

    예약 자신의 로그는 단계 경계에서만 한 줄씩 쌓여, 원고를 만드는 5~8분 동안 새 줄이
    하나도 오지 않는다. 글 요약(PostSummary)이 촘촘한 줄을 이미 들고 있는데 **목록이
    그것을 옮겨 담지 않아** 화면까지 오지 못했다.
    """
    from app.modules.blog_task.jobs import record_activity, reset_activity_log

    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    await seed_absolute(
        repository,
        [("쓰는 중인 소재", at(hours=1))],
        post_ids=["post_쓰는중"],
        statuses=[ScheduledJobStatus.RUNNING],
    )
    world.put(make_task(post_id="post_쓰는중", status=BlogTaskStatus.GENERATING))
    reset_activity_log("post_쓰는중")
    record_activity("post_쓰는중", "2/4 본문 작성 단계를 시작했어요")

    (item,) = await service.list_scheduled_jobs("user_1")

    assert [entry.message for entry in item.activity_log] == ["2/4 본문 작성 단계를 시작했어요"]


async def test_남의_예약은_목록에_나오지_않는다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, repository, _ = build_service()
    await seed_absolute(repository, [("내 글", at(hours=1))], user_id="user_1")

    assert await service.list_scheduled_jobs("user_2") == []


async def test_동시에_도는_작업이_배치의_집계를_되돌리지_않는다(monkeypatch):
    """``save_batch``는 문서를 통째로 바꾼다 — 낡은 사본으로 쓰면 그 사이에 다른 작업이
    남긴 값을 되돌린다.

    준비가 병렬이 되면서(2026-08-07) 그 '사이'가 실제로 생긴다. 여기서는 execute_job이
    배치를 읽은 **뒤**(save_job 시점에) 다른 작업이 완료된 것처럼 꾸며, 배치에 쓰기
    직전에 다시 읽는지를 본다.
    """
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    await seed_absolute(
        repository,
        [("먼저 끝남", at(minutes=5)), ("나중 시작", at(minutes=12))],
    )

    original_save_job = repository.save_job
    slipped = False

    async def save_job_then_slip(job):
        """execute_job이 배치를 읽은 뒤, 쓰기 전에 끼어드는 다른 작업 노릇을 한다."""
        nonlocal slipped
        await original_save_job(job)
        if slipped:
            return
        slipped = True
        current = await repository.find_batch("batch_1")
        await repository.save_batch(current.model_copy(update={"completed_count": 1}))

    monkeypatch.setattr(repository, "save_job", save_job_then_slip)

    await service.execute_job("job_1", publish=False)

    # 끼어든 값이 살아 있어야 한다. 낡은 사본으로 썼다면 0으로 되돌아간다.
    assert (await repository.find_batch("batch_1")).completed_count == 1


# ---------------- 13. 간격 방식 — 원고는 병렬, 발행은 입력 순서(2026-08-10)
#
# 사용자 결정: "순차발행도 소재 여러 개 입력하면 작업은 병렬로(최대 3개), 발행만 순차.
# 두 번째 소재의 원고가 먼저 나와도 첫 번째가 발행돼야 다음이 발행된다."


async def interval_step(worker: ScheduledPostingWorker, repository) -> None:
    """간격 배치를 한 걸음 진행시키고, 띄운 태스크가 끝날 때까지 기다린다(step과 같다)."""
    batch = await repository.find_batch("batch_1")
    await worker.advance_interval_batch(batch)
    await worker.wait_for_running()


async def test_간격_방식_준비는_한_단계씩_밀려_출발하고_간격_게이트는_밀리지_않는다(monkeypatch):
    """출발선을 한 단계씩 어긋나게 한다(2026-08-10 사용자 요청).

    **예전 계약을 대체한다.** 병렬로 바꾼 직후에는 세 편이 한 걸음에 함께 출발했는데,
    그러면 두 글의 단계 로그가 뒤엉켜 어느 글이 어디까지 갔는지 화면에서 알 수 없었다.
    이제 앞 글이 첫 과정(키워드 선택)을 마쳐야 다음 글이 출발한다.

    준비를 순차로 되돌린 것이 **아니다**. 실서비스에서는 앞 글이 제목을 만드는 동안
    둘째가 키워드를 고른다 — 여기서는 한 걸음이 한 작업을 끝까지 돌리는 테스트 세계라
    걸음 수로 나타난다.
    """
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    from test_scheduled_posting_service import seed_batch

    await seed_batch(repository, ["첫 소재", "둘째 소재", "셋째 소재"], interval_seconds=600)
    # 발행 게이트(직전 발행 + 간격)를 미래로 잠가 두고 준비만 본다.
    gate = at(minutes=30)
    batch = await repository.find_batch("batch_1")
    await repository.save_batch(batch.model_copy(update={"next_run_at": gate}))
    worker = make_worker(service, repository)

    # 첫 걸음에는 첫 소재만 출발한다 — 뒤 순번은 앞이 첫 과정을 지나야 한다.
    await interval_step(worker, repository)
    assert world.count("generate_draft") == 1
    assert (await repository.find_job("job_0")).status == ScheduledJobStatus.READY_TO_PUBLISH
    assert (await repository.find_job("job_1")).status == ScheduledJobStatus.WAITING

    # 앞이 지나가면 다음이 출발한다. 발행을 기다리지는 않는다(간격 게이트는 잠겨 있다).
    await interval_step(worker, repository)
    await interval_step(worker, repository)
    assert world.count("generate_draft") == 3
    for job_id in ("job_0", "job_1", "job_2"):
        assert (
            await repository.find_job(job_id)
        ).status == ScheduledJobStatus.READY_TO_PUBLISH
    # 준비 완료가 발행 게이트를 밀지 않는다 — 밀면 첫 발행이 준비 횟수만큼 늦어진다.
    assert (await repository.find_batch("batch_1")).next_run_at == gate
    # 간격이 안 지났으니 발행은 없다.
    assert world.count("publish_blog_task") == 0


def _stagger_jobs(previous_stage, previous_status=ScheduledJobStatus.RUNNING):
    """앞 순번 하나와 뒤 순번 하나. 게이트 판정만 보기 위한 최소 재료다."""
    now = now_iso()
    common = dict(
        batch_id="batch_1",
        user_id="user_1",
        platform=SchedulePlatform.NAVER,
        created_at=now,
        updated_at=now,
    )
    ahead = ScheduledJob(
        job_id="job_0",
        sequence=0,
        topic="앞",
        status=previous_status,
        stage=previous_stage,
        **common,
    )
    behind = ScheduledJob(
        job_id="job_1",
        sequence=1,
        topic="뒤",
        status=ScheduledJobStatus.WAITING,
        **common,
    )
    return behind, [ahead, behind]


async def test_뒤_순번은_앞이_첫_과정을_지나야_출발한다():
    from app.modules.scheduled_posting.models import ScheduledJobStage
    from app.modules.scheduled_posting.worker import ScheduledPostingWorker as W

    # 앞이 아직 글을 만들거나 키워드를 고르는 중 — 아직이다.
    for stage in (ScheduledJobStage.CREATE_POST, ScheduledJobStage.TREND_RECOMMENDATION):
        behind, jobs = _stagger_jobs(stage)
        assert W._stagger_open(behind, jobs) is False, stage

    # 제목 생성으로 넘어갔다 — 이제 뒤가 출발한다. 그 뒤 단계들도 마찬가지다.
    for stage in (
        ScheduledJobStage.TITLE_GENERATION,
        ScheduledJobStage.SEARCH_ANALYSIS,
        ScheduledJobStage.DRAFT_GENERATION,
    ):
        behind, jobs = _stagger_jobs(stage)
        assert W._stagger_open(behind, jobs) is True, stage


async def test_종결된_앞_순번은_뒤의_출발을_막지_않는다():
    """실패·취소가 뒤를 영영 막으면 안 된다 — 발행 게이트와 같은 태도다."""
    from app.modules.scheduled_posting.models import ScheduledJobStage
    from app.modules.scheduled_posting.worker import ScheduledPostingWorker as W

    for status in (
        ScheduledJobStatus.FAILED,
        ScheduledJobStatus.CANCELED,
        ScheduledJobStatus.COMPLETED,
    ):
        behind, jobs = _stagger_jobs(ScheduledJobStage.CREATE_POST, status)
        assert W._stagger_open(behind, jobs) is True, status


async def test_간격_발행은_둘째_원고가_먼저_나와도_입력_순서를_지킨다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    from test_scheduled_posting_service import seed_batch

    # 둘째는 원고가 준비됐고 첫째는 아직 만드는 중이다.
    await seed_batch(
        repository,
        ["첫 소재", "둘째 소재"],
        job_statuses=[ScheduledJobStatus.RUNNING, ScheduledJobStatus.READY_TO_PUBLISH],
        post_ids=[None, "post_2"],
    )
    world.put(_stub_task(world, "post_2"))
    worker = make_worker(service, repository)

    await interval_step(worker, repository)

    # 첫째가 발행되기 전에는 둘째가 나가지 않는다.
    assert world.count("publish_blog_task") == 0
    assert (
        await repository.find_job("job_1")
    ).status == ScheduledJobStatus.READY_TO_PUBLISH


async def test_간격_발행은_하나씩_그리고_발행_사이_간격을_지킨다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    from test_scheduled_posting_service import seed_batch

    await seed_batch(
        repository,
        ["첫 소재", "둘째 소재"],
        interval_seconds=600,
        job_statuses=[
            ScheduledJobStatus.READY_TO_PUBLISH,
            ScheduledJobStatus.READY_TO_PUBLISH,
        ],
        post_ids=["post_1", "post_2"],
    )
    world.put(_stub_task(world, "post_1"))
    world.put(_stub_task(world, "post_2"))
    worker = make_worker(service, repository)

    # 첫 걸음: 첫째만 올라간다(발행은 한 번에 하나).
    await interval_step(worker, repository)
    assert world.count("publish_blog_task") == 1
    assert (await repository.find_job("job_0")).status == ScheduledJobStatus.COMPLETED
    assert (
        await repository.find_job("job_1")
    ).status == ScheduledJobStatus.READY_TO_PUBLISH

    # 발행 성공이 간격 게이트를 다시 잠갔다 — 간격이 지나기 전에는 둘째가 안 나간다.
    await interval_step(worker, repository)
    assert world.count("publish_blog_task") == 1

    # 간격이 지난 것으로 만들면 둘째 차례다.
    batch = await repository.find_batch("batch_1")
    await repository.save_batch(batch.model_copy(update={"next_run_at": at(seconds=-1)}))
    await interval_step(worker, repository)
    assert world.count("publish_blog_task") == 2
    assert (await repository.find_job("job_1")).status == ScheduledJobStatus.COMPLETED


async def test_준비를_병렬로_띄워도_시작_줄은_입력_순서대로_찍힌다(monkeypatch):
    """둘째 소재가 먼저 시작한 것처럼 보이던 것 — 2026-08-10 사용자 신고.

    청년주택·애슐리 퀸즈 순으로 넣었는데 화면 로그에 애슐리가 먼저 찍혔다. 띄우는
    순서(sequence)는 처음부터 맞았고 발행 순서도 게이트가 지킨다 — 어긋난 것은
    **보이는 순서 하나**다. 준비가 병렬이 되면서 두 태스크가 각자 DB를 오가고(글
    생성 → 작업 저장 → 로그), 먼저 끝난 쪽이 시작 줄을 먼저 썼다.

    여기서는 **첫 소재의 글 생성만 늦춰** 그 뒤집힘을 재현한다. 시작 줄을 태스크
    안에서 남기던 예전 코드는 이 상황에서 애슐리를 위에 찍는다.

    출발을 한 단계씩 미는 규칙(_stagger_open)이 들어온 뒤로는 걸음을 두 번 밟아야 둘
    다 출발한다. 그래도 이 테스트는 남긴다 — 미는 규칙이 없는 절대 시각 예약에서는
    여전히 여러 편이 한 걸음에 함께 출발하고, 그때 순서를 지키는 것이 이 줄이다.
    """
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    from test_scheduled_posting_service import seed_batch

    await seed_batch(repository, ["청년주택", "애슐리 퀸즈"], interval_seconds=600)
    # 발행 게이트를 미래로 잠가 둔다 — 이 테스트가 보는 것은 준비 시작 순서뿐이다.
    batch = await repository.find_batch("batch_1")
    await repository.save_batch(batch.model_copy(update={"next_run_at": at(minutes=30)}))

    blog_tasks = service._blog_tasks
    original_create = blog_tasks.create_blog_task

    async def create_first_slowly(body):
        if body.get("topic") == "청년주택":
            for _ in range(5):
                await asyncio.sleep(0)
        return await original_create(body)

    monkeypatch.setattr(blog_tasks, "create_blog_task", create_first_slowly)
    worker = make_worker(service, repository)

    await interval_step(worker, repository)
    await interval_step(worker, repository)

    started = [
        entry.message
        for entry in (await repository.find_batch("batch_1")).logs
        if "글 생성을 시작합니다" in entry.message
    ]
    assert started == [
        "'청년주택'의 글 생성을 시작합니다.",
        "'애슐리 퀸즈'의 글 생성을 시작합니다.",
    ]
    assert world.count("generate_draft") == 2


async def test_앞_글이_발행되는_동안_뒤_글은_준비를_계속하고_끝나면_발행_대기다(monkeypatch):
    """사용자 확인(2026-08-10): "첫 소재가 원고 준비를 마치고 발행으로 올라가면,
    두 번째는 이미지 생성 중이거나 준비가 끝났으면 발행 대기가 되는 것 맞지?"

    맞다. 준비와 발행은 서로를 기다리지 않는다 — 발행은 한 번에 하나이고 입력 순서
    게이트를 지나야 하지만, 그 사이에도 뒤 글의 원고는 계속 만들어진다. 뒤 글이 먼저
    준비를 끝내도 앞 글이 종결되기 전에는 나가지 않는다.
    """
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    from test_scheduled_posting_service import seed_batch

    # 첫째는 준비가 끝나 발행 차례, 둘째는 아직 원고를 만드는 중이다.
    await seed_batch(
        repository,
        ["첫 소재", "둘째 소재"],
        interval_seconds=600,
        job_statuses=[ScheduledJobStatus.READY_TO_PUBLISH, ScheduledJobStatus.RUNNING],
        post_ids=["post_1", "post_2"],
    )
    world.put(_stub_task(world, "post_1"))
    worker = make_worker(service, repository)

    await interval_step(worker, repository)

    # 첫째는 발행됐다. 둘째는 발행되지 않았다(아직 준비 중이다).
    assert world.count("publish_blog_task") == 1
    assert (await repository.find_job("job_0")).status == ScheduledJobStatus.COMPLETED
    assert (await repository.find_job("job_1")).status == ScheduledJobStatus.RUNNING

    # 둘째의 준비가 끝났다 — 이제 발행 대기다. 간격이 안 지났으면 그대로 기다린다.
    ready = (await repository.find_job("job_1")).model_copy(
        update={"status": ScheduledJobStatus.READY_TO_PUBLISH}
    )
    await repository.save_job(ready)
    world.put(_stub_task(world, "post_2"))
    await interval_step(worker, repository)
    assert world.count("publish_blog_task") == 1  # 간격 게이트가 아직 잠겨 있다
    assert (
        await repository.find_job("job_1")
    ).status == ScheduledJobStatus.READY_TO_PUBLISH

    # 간격이 지나면 둘째가 나간다.
    batch = await repository.find_batch("batch_1")
    await repository.save_batch(batch.model_copy(update={"next_run_at": at(seconds=-1)}))
    await interval_step(worker, repository)
    assert world.count("publish_blog_task") == 2
    assert (await repository.find_job("job_1")).status == ScheduledJobStatus.COMPLETED


async def test_간격_실패한_앞_순번은_뒤를_막지_않는다(monkeypatch):
    """실패는 종결이다 — 앞 글이 실패했다고 뒤 글까지 영영 대기하면 안 된다."""
    naver_saved(monkeypatch, True)
    service, repository, world = build_service()
    from test_scheduled_posting_service import seed_batch

    await seed_batch(
        repository,
        ["실패한 소재", "둘째 소재"],
        job_statuses=[ScheduledJobStatus.FAILED, ScheduledJobStatus.READY_TO_PUBLISH],
        post_ids=[None, "post_2"],
    )
    world.put(_stub_task(world, "post_2"))
    worker = make_worker(service, repository)

    await interval_step(worker, repository)

    assert world.count("publish_blog_task") == 1
    assert (await repository.find_job("job_1")).status == ScheduledJobStatus.COMPLETED
