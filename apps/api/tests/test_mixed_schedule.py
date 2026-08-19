"""한 배치에 **시각을 적은 줄과 안 적은 줄이 섞인다**(2026-08-12).

「예약 포스팅」 탭에서 소재 줄마다 작업 시각을 고를 수 있게 되면서 생긴 규칙이다.

    "사용자가 날짜 지정도 따로 안하면 자동 발행이 되고 앞의 글이 발행이 완료가 되면
     다음 소재의 원고가 발행될수 있게 하는거야."

예전에는 "발행 시각은 모든 글에 있거나 모든 글에 없어야 합니다"로 막았다 — 한 배치가
어느 방식인지 정할 수 없다는 이유였다. 이제 답이 있다:

- 시각을 **적은** 줄 → 그 시각에 올린다(절대 시각).
- 시각을 **안 적은** 줄 → 앞 줄이 끝나면 올린다(``after_job_id``, 2026-08-12 #2의 문지기).

여기서 보는 것은 셋이다: 검증이 섞인 요청을 받는가, 서비스가 줄을 제대로 엮는가,
워커가 그 둘을 함께 굴리는가. **섞이지 않은 배치가 예전 그대로인지**도 함께 고정한다.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.errors import BlogTaskError
from app.modules.scheduled_posting.models import (
    ScheduleMode,
    ScheduledJob,
    ScheduledJobStatus,
)
from app.modules.scheduled_posting.validation import validate_start_batch_request
from app.modules.scheduled_posting.worker import ScheduledPostingWorker

from test_scheduled_posting_service import (  # noqa: E402
    build_service,
    naver_saved,
)


def at(**delta) -> str:
    """지금부터 얼마 뒤의 절대 시각(UTC ISO). 클라이언트가 보내는 형식과 같다."""
    moment = datetime.now(timezone.utc) + timedelta(**delta)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def body(*schedules) -> dict:
    return {
        "topics": [item["topic"] for item in schedules],
        "schedules": list(schedules),
        "intervalSeconds": 15,
        "platform": "naver",
    }


def row(topic: str, publish_at: str | None = None) -> dict:
    item: dict = {"topic": topic, "publishNaver": True, "publishThreads": False}
    if publish_at is not None:
        item["publishAt"] = publish_at
    return item


def job(
    sequence: int,
    *,
    status: ScheduledJobStatus = ScheduledJobStatus.READY_TO_PUBLISH,
    publish_at: str | None = None,
    after: str | None = None,
) -> ScheduledJob:
    return ScheduledJob(
        job_id=f"job_{sequence}",
        batch_id="batch_1",
        user_id="user_1",
        sequence=sequence,
        topic=f"소재 {sequence}",
        status=status,
        publish_at=publish_at,
        after_job_id=after,
        created_at="2026-08-12T00:00:00Z",
        updated_at="2026-08-12T00:00:00Z",
    )


# ------------------------------------------------------------------------ 검증


def test_섞어_보내도_통과한다():
    request = validate_start_batch_request(body(row("가", at(hours=1)), row("나")))

    assert request.schedule_mode is ScheduleMode.ABSOLUTE
    assert [item.publish_at is not None for item in request.schedules] == [True, False]


def test_한_줄만_시각이_있어도_절대_시각_방식이다():
    """워커가 시각을 보는 쪽으로 돌아야 그 약속을 지킨다."""
    request = validate_start_batch_request(body(row("가"), row("나"), row("다", at(days=1))))

    assert request.schedule_mode is ScheduleMode.ABSOLUTE


def test_아무도_시각이_없으면_간격_방식이다():
    """예전 그대로다 — 「예약 포스팅」 탭의 기본 사용법이 이쪽이다."""
    request = validate_start_batch_request(body(row("가"), row("나")))

    assert request.schedule_mode is ScheduleMode.INTERVAL
    assert all(item.publish_at is None for item in request.schedules)


def test_간격_규칙은_시각을_적은_줄끼리만_본다():
    """비운 줄은 약속한 시각이 없어 넘길 시각도 없다 — 사이에 끼어도 상관없다."""
    request = validate_start_batch_request(
        body(row("가", at(hours=1)), row("나"), row("다", at(hours=3)))
    )

    assert len(request.schedules) == 3


def test_적은_줄끼리_너무_붙어_있으면_거부한다():
    """섞였다고 검사를 건너뛰지 않는다. 발행은 한 번에 하나씩 돈다."""
    with pytest.raises(BlogTaskError) as caught:
        validate_start_batch_request(
            body(row("가", at(hours=1)), row("나"), row("다", at(hours=1, minutes=3)))
        )

    assert caught.value.code == "VALIDATION_FAILED"
    assert "떨어져" in caught.value.message


# ----------------------------------------------------------------- 줄 세우기


async def test_시각을_비운_줄은_앞_줄을_가리킨다(monkeypatch):
    naver_saved(monkeypatch, True)
    service, _, _ = build_service()

    view = await service.start_batch(
        "user_1", body(row("가", at(hours=1)), row("나"), row("다"))
    )

    first, second, third = view.jobs
    assert first.after_job_id is None
    assert second.after_job_id == first.job_id
    assert third.after_job_id == second.job_id


async def test_섞인_배치는_입력한_줄_순서를_지킨다(monkeypatch):
    """시각으로 다시 세우면 '앞 글'이 사용자가 적은 줄과 달라진다.

    2번 줄('나')은 시각이 없어 1번 줄을 기다린다. 시각 순으로 정렬해 버리면 3번 줄이
    앞으로 올라와, 사용자가 보기에 아무 이유 없이 순서가 바뀐다.
    """
    naver_saved(monkeypatch, True)
    service, _, _ = build_service()

    view = await service.start_batch(
        "user_1", body(row("가", at(days=2)), row("나"), row("다", at(hours=1)))
    )

    assert [item.topic for item in view.jobs] == ["가", "나", "다"]


async def test_전부_시각을_적으면_예전처럼_시각_순으로_선다(monkeypatch):
    """섞이지 않은 배치는 손대지 않았다 — 회귀 고정."""
    naver_saved(monkeypatch, True)
    service, _, _ = build_service()

    view = await service.start_batch(
        "user_1", body(row("늦은 것", at(hours=5)), row("이른 것", at(hours=1)))
    )

    assert [item.topic for item in view.jobs] == ["이른 것", "늦은 것"]
    # 시각이 곧 순서다. 앞 글에 매달지 않는다.
    assert all(item.after_job_id is None for item in view.jobs)


async def test_전부_비우면_앞_줄을_가리키지_않는다(monkeypatch):
    """간격 방식은 워커가 sequence로 이미 줄을 세운다(_interval_due_to_publish)."""
    naver_saved(monkeypatch, True)
    service, _, _ = build_service()

    view = await service.start_batch("user_1", body(row("가"), row("나")))

    assert all(item.after_job_id is None for item in view.jobs)


# --------------------------------------------------------------------- 워커

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def test_시각을_비운_작업은_앞_작업이_끝나야_올라간다():
    first = job(0, status=ScheduledJobStatus.RUNNING)
    second = job(1, after="job_0")

    assert ScheduledPostingWorker._due_to_publish([first, second], NOW) is None


def test_앞_작업이_끝나면_비운_작업이_올라간다():
    first = job(0, status=ScheduledJobStatus.COMPLETED)
    second = job(1, after="job_0")

    due = ScheduledPostingWorker._due_to_publish([first, second], NOW)

    assert due is not None and due.job_id == "job_1"


def test_앞_작업이_실패해도_다음은_올라간다():
    """실패가 뒤를 영영 막으면 사용자는 손으로 치우기 전까지 아무 글도 못 받는다."""
    first = job(0, status=ScheduledJobStatus.FAILED)
    second = job(1, after="job_0")

    due = ScheduledPostingWorker._due_to_publish([first, second], NOW)

    assert due is not None and due.job_id == "job_1"


def test_시각을_정하지_않은_쪽이_먼저다():
    """2026-08-13 사용자 지시로 뒤집혔다.

    예전에는 '약속한 시각이 있는 쪽이 먼저'였다 — 지켜야 할 약속이 있으니 그쪽을
    앞세운다는 논리였다. 사용자가 원하는 것은 반대다: 시각을 정하지 않고 지금 올리려고
    건 글이 먼저 나가고, 예약한 글은 그 뒤다.
    """
    free = job(0)
    timed = job(1, publish_at="2026-08-12T11:30:00Z")

    due = ScheduledPostingWorker._due_to_publish([free, timed], NOW)

    assert due is not None and due.job_id == "job_0"


def test_예약_글의_원고가_먼저_끝나도_즉시_작업을_기다린다():
    """순서만 매겨서는 안 되는 이유다.

    예약 글이 먼저 READY_TO_PUBLISH가 되면 그 순간 후보는 그것 하나뿐이라, 정렬을
    어떻게 하든 그것이 나간다. 아직 올라가지 않은 즉시 작업이 남아 있으면 시각을 정한
    작업은 후보에서 빠져야 한다.
    """
    still_writing = job(0, status=ScheduledJobStatus.RUNNING)
    timed = job(1, publish_at="2026-08-12T11:30:00Z")

    assert ScheduledPostingWorker._due_to_publish([still_writing, timed], NOW) is None


def test_즉시_작업이_끝나면_예약_글이_올라간다():
    """문이 열린다 — 즉시 작업이 종결되면 더 기다릴 것이 없다."""
    done = job(0, status=ScheduledJobStatus.COMPLETED)
    timed = job(1, publish_at="2026-08-12T11:30:00Z")

    due = ScheduledPostingWorker._due_to_publish([done, timed], NOW)

    assert due is not None and due.job_id == "job_1"


def test_즉시_작업이_실패해도_예약_글이_올라간다():
    """실패가 예약을 영영 막으면 사용자는 손으로 치우기 전까지 아무 글도 못 받는다."""
    failed = job(0, status=ScheduledJobStatus.FAILED)
    timed = job(1, publish_at="2026-08-12T11:30:00Z")

    due = ScheduledPostingWorker._due_to_publish([failed, timed], NOW)

    assert due is not None and due.job_id == "job_1"


def test_올릴_곳이_없는_즉시_작업은_예약_글을_막지_않는다():
    """자동 발행을 끈 작업은 원고까지만 만들고 큐에 선다 — 발행 줄에 서 있지 않다."""
    no_publish = job(0, status=ScheduledJobStatus.RUNNING)
    no_publish = no_publish.model_copy(
        update={"publish_naver": False, "publish_threads": False}
    )
    timed = job(1, publish_at="2026-08-12T11:30:00Z")

    due = ScheduledPostingWorker._due_to_publish([no_publish, timed], NOW)

    assert due is not None and due.job_id == "job_1"


def test_새_글_작성의_지금_바로도_시각을_정하지_않은_것으로_본다():
    """그쪽은 publish_at에 '걸린 시각'이 채워진다 — 값만 보면 예약과 구별되지 않는다.

    starts_immediately가 그 구분이다. 없으면 즉시 작업이 자기 등록 시각을 약속으로
    들고 예약 글과 같은 줄에 서게 된다.
    """
    immediate = job(0, publish_at="2026-08-12T11:59:00Z").model_copy(
        update={"starts_immediately": True}
    )
    timed = job(1, publish_at="2026-08-12T11:30:00Z")

    due = ScheduledPostingWorker._due_to_publish([immediate, timed], NOW)

    # 시각만 보면 job_1이 이르다. 그래도 '지금 바로'로 건 job_0이 먼저다.
    assert due is not None and due.job_id == "job_0"


def test_비운_줄이_시각_있는_줄을_기다릴_때는_서로_막지_않는다():
    """「자동 포스팅」 탭의 [시각 있음, 비움, 비움]이 여기다.

    비운 줄은 앞 줄이 끝나야 올라간다(after_job_id). 그 앞 줄이 시각을 정한 줄인데
    '즉시가 먼저'를 곧이곧대로 적용하면, 시각 있는 줄은 비운 줄을 기다리고 비운 줄은
    그 줄을 기다려 배치가 통째로 멈춘다.
    """
    timed = job(0, publish_at="2026-08-12T11:30:00Z")
    free_1 = job(1, status=ScheduledJobStatus.WAITING, after="job_0")
    free_2 = job(2, status=ScheduledJobStatus.WAITING, after="job_1")

    due = ScheduledPostingWorker._due_to_publish([timed, free_1, free_2], NOW)

    assert due is not None and due.job_id == "job_0"


def test_옛_작업은_예전처럼_시각_순으로_올라간다():
    """회귀 고정 — starts_immediately가 없던 문서는 기본 False로 읽힌다."""
    early = job(0, publish_at="2026-08-12T11:00:00Z")
    late = job(1, publish_at="2026-08-12T11:30:00Z")

    due = ScheduledPostingWorker._due_to_publish([late, early], NOW)

    assert due is not None and due.job_id == "job_0"


def test_아직_시각이_안_된_글은_비운_글을_막지_않는다():
    free = job(0)
    later = job(1, publish_at="2026-08-12T18:00:00Z")

    due = ScheduledPostingWorker._due_to_publish([free, later], NOW)

    assert due is not None and due.job_id == "job_0"


def test_시각을_비운_작업은_지금_원고를_만든다():
    """언제 올릴지는 앞 글이 정하지만, 원고는 미리 만들어 둬야 차례에 기다리지 않는다."""
    waiting = job(0, status=ScheduledJobStatus.WAITING)
    chained = job(1, status=ScheduledJobStatus.WAITING, after="job_0")

    due = ScheduledPostingWorker._due_to_prepare([waiting, chained], NOW)

    assert [item.job_id for item in due] == ["job_0", "job_1"]


def test_먼_시각의_글은_아직_원고를_만들지_않는다():
    """회귀 고정 — 시각을 적은 줄은 예전 그대로 준비 여유만큼 앞서 시작한다."""
    later = job(0, status=ScheduledJobStatus.WAITING, publish_at="2026-08-13T12:00:00Z")

    assert ScheduledPostingWorker._due_to_prepare([later], NOW) == []
