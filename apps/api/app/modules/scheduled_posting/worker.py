"""예약 실행 워커.

Celery 같은 새 인프라를 들이지 않는다. FastAPI가 이미 돌리고 있는 asyncio 루프에
백그라운드 태스크 하나를 띄우고, 기존 JobLease로 프로세스 간 중복을 막는다.

## 무엇이 동시에 돌고 무엇이 하나씩 도는가

**발행은 사용자마다 하나씩이다.** 같은 사용자 프로필로 크롬이 두 번 뜨면 프로필 잠금
때문에 발행이 통째로 실패한다(posting/naver/browser.py의 '프로필이 사용 중입니다').
프로필은 **사용자별**로 갈라져 있으므로(naver_profile_dir) 다른 사용자의 발행은 서로
막을 이유가 없다 — 예전에는 서버 전체에 발행 자리가 하나뿐이라, 10명이 각자 자기
계정으로 예약을 걸면 한 사람의 발행 1~2분씩이 전부 줄을 섰다(2026-08-18에 사용자별로
갈랐다). 서버 전체 동시 크롬 수만 ``SCHEDULED_MAX_CONCURRENT_PUBLISH``(기본 10)로
막는다 — 크롬 한 대가 수백 MB라 무한정 띄우면 서버가 먼저 넘어진다.

**원고 준비는 절대 시각 예약에 한해 동시에 돈다**(2026-08-07 사용자 결정, 최대
``MAX_CONCURRENT_PREPARE``편). 예전에는 준비도 하나씩이었고, 그 준비가 워커의 tick을
통째로 붙잡고 있었다. 원고 한 편이 실측 6분 27초(중앙값)이므로:

- 세 번째 글의 원고는 20분 뒤에야 시작됐다. 예약 시각이 그보다 이르면 반드시 늦는다.
- 더 나빴던 것은 **준비가 도는 동안 발행 시각을 아무도 보지 않았다**는 것이다. 앞 글의
  원고를 만드는 6분 사이에 뒤 글의 발행 시각이 지나가도 그 tick이 끝나야 알았다.

이제 준비와 발행은 각자 태스크로 떨어져 나가고, 루프는 어느 쪽도 기다리지 않는다.
그래서 원고를 만드는 중에도 발행 시각은 제때 온다.

간격(INTERVAL) 방식은 **예전 그대로 하나씩**이다. 그쪽은 '앞 글이 발행된 뒤 N분'이
곧 순서라, 미리 만들어 둘 대상이 정해지지 않는다.

## 두 가지 예약 방식

- **간격(INTERVAL)**: 앞 글이 발행된 뒤 설정한 간격이 지나면 다음 글을 만들어 올린다.
  간격은 직전 **발행 성공 시각**부터 잰다.
- **절대 시각(ABSOLUTE)**: 글마다 정해 둔 시각(``ScheduledJob.publish_at``)에 올린다.
  원고는 그 시각보다 먼저 만들어 둬야 하므로 한 작업이 두 번에 나뉘어 돈다.

      publish_at                       publish_at
          ↓                                ↓
      [준비: 글·트렌드·원고]  →  READY_TO_PUBLISH  →  [발행]

  준비를 앞당기는 '여유'는 없다(2026-08-12 사용자 지시로 없앴다). 저장된 시각이 곧
  작업을 시작할 시각이고, 발행은 그 준비가 끝나는 대로다.

발행은 항상 **시각이 된 것부터** 본다. 준비보다 발행이 먼저다 — 약속한 시각은 지켜야
하고, 원고 준비는 몇 분 늦어도 그 다음 글의 시각까지는 여유가 있다.

## 시각을 정한 글과 정하지 않은 글이 섞이면

**정하지 않은 쪽이 먼저 올라간다**(2026-08-13 사용자 지시). 예약 글의 원고가 먼저
끝났더라도, 아직 올라가지 않은 즉시 작업이 남아 있으면 예약 글은 기다린다
(``_due_to_publish``). 원고 **생성**은 그대로 섞어서 함께 돈다 — 상한
``MAX_CONCURRENT_PREPARE``편은 두 종류를 합쳐서 센다.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from app.modules.blog_task.locks import JobLease, NoOpJobLease, hold
from app.shared.format import now_iso
from app.shared.ids import short

from .models import (
    ACTIVE_BATCH_STATUSES,
    FINISHED_JOB_STATUSES,
    ScheduledBatch,
    ScheduledBatchStatus,
    ScheduledJob,
    ScheduledJobStage,
    ScheduledJobStatus,
    ScheduleMode,
    has_appointment,
    publishes_anywhere,
)
from .repository import ScheduledPostingRepository
from .service import ScheduledPostingService


logger = logging.getLogger(__name__)

#: 깨어나 살펴보는 기본 주기. 다음 실행 시각이 더 가까우면 그때 깨어난다.
POLL_SECONDS = 5.0

#: 워커가 아무 일도 없을 때 잠드는 최대 시간. 너무 길면 '예약 시작' 직후 반응이 늦다.
#: 절대 시각 예약이 며칠 뒤여도 이 주기로는 깨어난다 — 그 사이에 사용자가 시각을
#: 앞당겼을 수 있고, 그것을 알아채는 데 며칠이 걸리면 안 된다.
MAX_SLEEP_SECONDS = 30.0

#: 절대 시각 예약에서 **원고를 한꺼번에 몇 편까지 만들 것인가**.
#:
#: 전부 동시에 돌리지 않는 이유는 비용과 한도다 — 한 편이 LLM 호출 수십 번과 이미지
#: 생성을 쓰고, 예약은 한 배치에 최대 20편까지 들어온다. 3편이면 실측 6분 27초짜리
#: 원고 10편이 65분에서 22분으로 줄고, 그 이상 늘려서 얻는 것보다 레이트리밋에
#: 부딪힐 위험이 커진다.
#:
#: **발행에는 적용되지 않는다.** 발행은 사용자마다 하나씩이다(크롬 프로필).
MAX_CONCURRENT_PREPARE = 3


def max_concurrent_publish() -> int:
    """서버 전체에서 동시에 띄울 발행 크롬의 상한 (사용자별 하나는 그대로).

    사용자 수만큼 크롬이 뜰 수 있게 됐으므로 총량을 막는다 — 크롬 한 대가 수백 MB라
    RAM이 바닥나면 발행이 아니라 서버가 죽는다. 값은 서버 사양에 맞춰 .env로 조절한다.
    """
    raw = (os.environ.get("SCHEDULED_MAX_CONCURRENT_PUBLISH") or "10").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 10

#: 종결된 작업. 앞 순번이 여기 들어가면 뒤를 더 막지 않는다.
TERMINAL_JOB_STATUSES = frozenset(
    {
        ScheduledJobStatus.COMPLETED,
        ScheduledJobStatus.FAILED,
        ScheduledJobStatus.CANCELED,
    }
)

#: 준비 단계의 앞뒤. 값 비교가 아니라 **이 자리**로 어디까지 갔는지 잰다.
STAGE_ORDER: tuple[ScheduledJobStage, ...] = (
    ScheduledJobStage.CREATE_POST,
    ScheduledJobStage.TREND_RECOMMENDATION,
    ScheduledJobStage.TITLE_GENERATION,
    ScheduledJobStage.SEARCH_ANALYSIS,
    ScheduledJobStage.INTENT_SELECTION,
    ScheduledJobStage.DRAFT_GENERATION,
    ScheduledJobStage.NAVER_PUBLISH,
    ScheduledJobStage.THREADS_PUBLISH,
    ScheduledJobStage.DONE,
)

#: 다음 순번을 출발시키는 자리(2026-08-10 사용자 요청).
#:
#: 앞 순번이 이 단계에 닿아야 다음 순번을 띄운다. TITLE_GENERATION은 '소재 관련 키워드
#: 선택'(TREND_RECOMMENDATION)을 마치고 넘어가는 자리다 — 앞 글이 **첫 과정을 끝내면**
#: 다음 글이 출발한다. 병렬은 그대로고(최대 MAX_CONCURRENT_PREPARE편) 출발선만 한
#: 단계씩 어긋난다: 첫 글이 제목을 만들 때 둘째가 키워드를 고르는 식이다.
#:
#: 그렇게 미는 이유는 순서를 읽기 위해서다. 동시에 출발하면 두 글의 단계 로그가 뒤엉켜
#: 어느 글이 어디까지 갔는지 화면에서 알 수 없었다.
#:
#: **간격 방식에만 쓴다.** 절대 시각 예약은 글마다 약속한 게시 시각이 있고, 뒤 글의
#: 준비를 앞 글에 매달면 그 약속을 놓칠 수 있다.
PREPARE_STAGGER_STAGE = ScheduledJobStage.TITLE_GENERATION


def stage_rank(stage: ScheduledJobStage) -> int:
    """준비 단계가 몇 번째인가. 모르는 값은 맨 앞으로 본다(아직 시작 전으로 취급)."""
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return 0


def lease_key(batch_id: str) -> str:
    """발행 임차 — 배치 하나에 발행 한 건. 크롬을 두 개 띄우지 않기 위한 것이다."""
    return f"blogit:scheduled:{batch_id}"


def prepare_lease_key(job_id: str) -> str:
    """원고 준비 임차 — **작업 하나**에 하나.

    준비는 서로 동시에 돌아도 되지만, **같은 작업**을 두 프로세스가 함께 만들면
    LLM 비용이 두 벌 나가고 결과 하나는 버려진다. 그래서 배치가 아니라 작업 단위다.
    """
    return f"blogit:scheduled:prepare:{job_id}"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_due(publish_at: str | None, now: datetime, *, lead: float = 0.0) -> bool:
    """예약 시각이 됐는가(``lead``초만큼 앞당겨 본다).

    시각을 읽을 수 없으면 **되지 않은 것으로 본다.** 깨진 값을 '지금'으로 읽으면 뜻을
    알 수 없는 시각의 글이 즉시 올라간다 — 예약의 정반대다.
    """
    due = parse_iso(publish_at)
    if due is None:
        return False
    return (due - now).total_seconds() <= lead


class ScheduledPostingWorker:
    def __init__(
        self,
        service: ScheduledPostingService,
        repository: ScheduledPostingRepository,
        job_lease: JobLease | None = None,
    ):
        self._service = service
        self._repository = repository
        self._lease = job_lease or NoOpJobLease()
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        #: 지금 원고를 만들고 있는 작업들(job_id → 태스크). 상한은
        #: MAX_CONCURRENT_PREPARE이고, 여기 있는 작업은 다시 집지 않는다 —
        #: 저장된 status가 RUNNING으로 바뀌기 전에 다음 tick이 올 수 있다.
        self._preparing: dict[str, asyncio.Task] = {}
        #: 지금 발행하고 있는 태스크 — **사용자마다 하나**(user_id → 태스크).
        #: 크롬 프로필이 사용자별이라 같은 사용자만 줄을 서면 된다. 서버 전체 총량은
        #: max_concurrent_publish()가 막는다.
        self._publishing: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------- 생명주기

    def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        """진행 중인 단계는 끊지 않는다 — 루프가 다음 단계로 넘어가지 않게만 한다.

        Selenium 발행 도중에 태스크를 취소하면 크롬이 뜬 채 남고, 발행이 됐는지 안 됐는지
        알 수 없는 상태가 된다. 그것이 중복 발행의 씨앗이다.
        """
        self._stopping = True
        self._wake.set()
        # 도는 준비·발행 태스크는 여기서 끊지 않는다. 아래 루프 정리와 같은 이유이고,
        # 서버가 내려간 뒤 남은 상태는 다음 시작의 복구(recovery.py)가 정리한다.
        # blog_task_service·draft_service의 shutdown이 그 안쪽 잡을 마저 비운다.
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # 아직 돌고 있으면 취소한다. 여기까지 왔다는 것은 서버가 내려간다는 뜻이고,
            # 남은 상태는 다음 시작의 복구가 정리한다.
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass

    def wake(self) -> None:
        """새 배치가 생겼거나 제어(재개·정지)가 바뀌었다 — 지금 살펴보라."""
        self._wake.set()

    async def wait_for_running(self) -> bool:
        """지금 도는 준비·발행 태스크가 끝날 때까지 기다린다. 하나라도 있었으면 True.

        루프는 이것을 부르지 않는다 — 기다리지 않는 것이 이 구조의 핵심이다. '한 걸음이
        실제로 끝난 뒤'의 상태를 봐야 하는 쪽(테스트)을 위한 자리다.
        """
        running = [*self._preparing.values(), *self._publishing.values()]
        if not running:
            return False
        await asyncio.gather(*running, return_exceptions=True)
        return True

    # ----------------------------------------------------------------- 루프

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                sleep_for = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 루프는 어떤 실패에도 죽지 않는다
                logger.warning(
                    "예약 워커 tick 실패 | %s: %s", type(error).__name__, error, exc_info=True
                )
                sleep_for = POLL_SECONDS

            if self._stopping:
                return
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> float:
        """살펴보고 할 일을 띄운다. 다음에 깨어날 때까지의 초를 반환.

        **절대 시각 예약은 여기서 기다리지 않는다.** 준비·발행을 각자 태스크로 띄우고
        곧바로 돌아온다 — 원고를 만드는 6분 동안 발행 시각을 못 보던 것이 그 반대였다.
        간격 방식은 예전처럼 이 자리에서 끝까지 기다린다(한 번에 하나).
        """
        batches = await self._repository.list_batches_by_status(list(ACTIVE_BATCH_STATUSES))
        if not batches:
            return MAX_SLEEP_SECONDS

        soonest = MAX_SLEEP_SECONDS
        for batch in batches:
            batch = await self._reconcile_controls(batch)
            if batch.status in {
                ScheduledBatchStatus.PAUSED,
                ScheduledBatchStatus.NEEDS_HUMAN,
            }:
                continue
            if batch.status not in ACTIVE_BATCH_STATUSES:
                continue
            if batch.stop_requested or batch.pause_requested:
                continue

            if batch.schedule_mode is ScheduleMode.ABSOLUTE:
                soonest = min(soonest, await self.advance_scheduled_batch(batch))
                continue

            # 간격 방식도 원고 준비는 병렬이다(2026-08-10 사용자 결정). 발행만
            # 입력 순서대로 하나씩, 발행 사이 간격을 지킨다.
            soonest = min(soonest, await self.advance_interval_batch(batch))
        return max(0.5, min(soonest, MAX_SLEEP_SECONDS))

    def _seconds_until_due(self, batch: ScheduledBatch) -> float:
        due = parse_iso(batch.next_run_at)
        if due is None:
            return 0.0
        return max(0.0, (due - datetime.now(timezone.utc)).total_seconds())

    # ------------------------------------------------------- 절대 시각 예약

    async def advance_scheduled_batch(self, batch: ScheduledBatch) -> float:
        """절대 시각 배치를 진행시킨다 — **기다리지 않고** 태스크를 띄운다.

        순서가 중요하다. **발행할 것이 있으면 그것이 먼저다** — 사용자가 정한 것은 게시
        시각이고, 원고 준비는 그 다음 글의 시각까지 여유가 있다.

        돌아오는 값은 다음에 깨어날 때까지의 초다. 띄운 태스크는 끝나면서 워커를
        깨우므로(``_wake``), 그 값이 길어도 결과를 늦게 보지 않는다.
        """
        jobs = await self._repository.list_jobs(batch.batch_id)
        now = datetime.now(timezone.utc)

        # 1) 발행. **이 사용자의 자리가 비어 있으면** 시각이 이른 것부터 하나 띄운다.
        #    다른 사용자의 발행은 서로 기다리지 않는다(프로필이 다르다).
        due = self._due_to_publish(jobs, now)
        if due is not None and self._can_publish(batch.user_id):
            logger.info("예약 발행 시각 도달 | %s - %s", short(due.job_id), due.topic[:30])
            self._publishing[batch.user_id] = asyncio.create_task(
                self._run_publish(batch.batch_id, due.job_id, batch.user_id)
            )
            due = None
        # 올릴 것이 남았는데 발행 자리가 차 있다. 다음 계산은 '지금'이라 0을 돌려주게 되는데,
        # 그대로 두면 발행이 끝날 때까지 0.5초마다 헛돌며 DB를 읽는다. 발행이 끝나면
        # _run_publish가 워커를 깨우므로 여기서는 느긋하게 기다려도 늦지 않는다.
        waiting_to_publish = due is not None

        # 2) 준비. 빈 자리만큼 동시에 띄운다. 발행을 기다리지 않는다 — 발행이 도는
        #    동안에도 다음 글의 원고는 만들어 둘 수 있다.
        for job in self._due_to_prepare(jobs, now):
            if len(self._preparing) >= MAX_CONCURRENT_PREPARE:
                break
            if job.job_id in self._preparing:
                continue
            logger.info(
                "예약 원고 준비 시작 | %s - %s (동시 %d/%d)",
                short(job.job_id),
                job.topic[:30],
                len(self._preparing) + 1,
                MAX_CONCURRENT_PREPARE,
            )
            # 간격 방식과 같은 이유로 시작 줄을 여기서 차례로 남긴다.
            await self._service.note_generation_start(job)
            self._preparing[job.job_id] = asyncio.create_task(
                self._run_prepare(batch.batch_id, job.job_id)
            )

        # 도는 태스크가 하나도 없을 때만 닫힌다(_close_if_done이 상태로 판단한다).
        await self._close_if_done(batch.batch_id, jobs)
        if waiting_to_publish:
            return POLL_SECONDS
        return self._seconds_until_next(jobs, now)

    async def advance_interval_batch(self, batch: ScheduledBatch) -> float:
        """간격 배치를 진행시킨다 — 준비는 병렬, 발행은 입력 순서대로 하나씩.

        2026-08-10 사용자 결정: "순차발행도 소재 여러 개 입력하면 작업은 병렬로(최대
        3개), 발행만 순차. 두 번째 소재의 원고가 먼저 나와도 첫 번째가 발행돼야 다음이
        발행된다." 예전에는 생성→발행 전 구간이 한 작업씩 순차였다(run_next_job) —
        3편이면 세 번째 발행까지 원고 시간이 전부 더해졌다.

        발행 간격(interval_seconds)은 그대로 **발행 사이**의 간격이다: `_publish`가
        성공 시각 + 간격으로 `next_run_at`을 잡고, 여기서는 그 시각 전에 발행하지
        않는다. 준비는 간격과 무관하다 — 간격은 네이버 연속 게시를 벌리려는 것이지
        LLM을 쉬게 하려는 것이 아니다.
        """
        jobs = await self._repository.list_jobs(batch.batch_id)

        # 1) 발행 — 입력 순서 게이트를 통과한 차례 하나. 간격도 지나야 한다.
        due = self._interval_due_to_publish(jobs, datetime.now(timezone.utc))
        interval_wait = self._seconds_until_due(batch)
        if due is not None and interval_wait <= 0 and self._can_publish(batch.user_id):
            logger.info("예약 발행 차례 | %s - %s", short(due.job_id), due.topic[:30])
            self._publishing[batch.user_id] = asyncio.create_task(
                self._run_publish(batch.batch_id, due.job_id, batch.user_id)
            )
            due = None

        # 2) 준비 — 발행·간격과 무관하게, 입력 순서대로 **한 단계씩 밀어서** 띄운다.
        staggered = False
        for job in self._interval_due_to_prepare(jobs):
            if len(self._preparing) >= MAX_CONCURRENT_PREPARE:
                break
            if job.job_id in self._preparing:
                continue
            if not self._stagger_open(job, jobs):
                # 앞 순번이 아직 첫 과정(키워드 선택)에 있다. 뒤 순번은 더 볼 것도 없다 —
                # 앞이 막혀 있으면 그 뒤도 막혀 있다.
                staggered = True
                break
            logger.info(
                "예약 원고 준비 시작 | %s - %s (동시 %d/%d)",
                short(job.job_id),
                job.topic[:30],
                len(self._preparing) + 1,
                MAX_CONCURRENT_PREPARE,
            )
            # 시작 줄은 **띄우기 전에, 여기서 차례로** 남긴다. 태스크 안에서 각자
            # 남기면 둘째 소재가 먼저 찍힌다(note_generation_start 참고).
            await self._service.note_generation_start(job)
            self._preparing[job.job_id] = asyncio.create_task(
                self._run_prepare(batch.batch_id, job.job_id)
            )

        await self._close_if_done(batch.batch_id, jobs)
        if due is not None:
            # 차례는 정해졌는데 간격이 안 지났다 — 간격이 끝나는 시각에 깨어난다.
            return max(0.5, interval_wait)
        if staggered or self._publishing or self._preparing:
            # 도는 태스크가 끝나면 워커를 깨우므로 느긋해도 늦지 않는다. 밀어 둔 순번도
            # 앞 글이 단계를 넘기는 대로 집어야 하므로 같은 주기로 다시 본다.
            return POLL_SECONDS
        return MAX_SLEEP_SECONDS

    @staticmethod
    def _stagger_open(job: ScheduledJob, jobs: list[ScheduledJob]) -> bool:
        """앞 순번이 첫 과정을 지났는가 — 지났을 때만 이 작업을 띄운다.

        2026-08-10 사용자 요청: "병렬로 작업하되 두 번째 원고 생성의 시작은 첫 번째가
        첫 과정(소재 관련 키워드 선택)을 마치고 두 번째 단계(제목 생성)로 넘어갈 때
        시작해서 한 차례씩 뒤로 밀리게." 동시에 출발하면 두 글의 단계 로그가 뒤엉켜
        어느 글이 어디까지 갔는지 화면에서 알 수 없었다.

        **종결된 앞 순번은 지나간다** — 실패·취소가 뒤를 영영 막으면 안 된다
        (발행 게이트가 종결을 지나가는 것과 같은 이유다).
        """
        ahead = [
            item
            for item in jobs
            if item.sequence < job.sequence and item.status not in TERMINAL_JOB_STATUSES
        ]
        if not ahead:
            return True
        previous = max(ahead, key=lambda item: item.sequence)
        return stage_rank(previous.stage) >= stage_rank(PREPARE_STAGGER_STAGE)

    @staticmethod
    def _interval_due_to_publish(
        jobs: list[ScheduledJob], now: datetime
    ) -> ScheduledJob | None:
        """입력 순서 게이트를 통과한 발행 후보 하나.

        sequence 오름차순으로 보며 종결(완료·실패·취소)은 지나가고, 처음 만나는
        미종결 작업이 READY_TO_PUBLISH면 그것이 차례다. 아니면(아직 원고를 만드는
        중이거나 대기) 이번에는 아무도 발행하지 않는다 — 두 번째 원고가 먼저 나와도
        첫 번째가 발행되기 전에는 나가지 않는다(2026-08-10 사용자 결정).
        """
        for job in sorted(jobs, key=lambda item: item.sequence):
            if job.status in TERMINAL_JOB_STATUSES:
                continue
            if job.status == ScheduledJobStatus.READY_TO_PUBLISH:
                return job
            return None
        return None

    @staticmethod
    def _interval_due_to_prepare(jobs: list[ScheduledJob]) -> list[ScheduledJob]:
        """원고를 만들 후보들 — WAITING을 입력 순서대로. 발행 게이트와 무관하다."""
        return sorted(
            (job for job in jobs if job.status == ScheduledJobStatus.WAITING),
            key=lambda item: item.sequence,
        )

    def _can_publish(self, user_id: str) -> bool:
        """이 사용자의 발행 자리가 비어 있고, 서버 전체 상한도 남아 있는가."""
        return (
            user_id not in self._publishing
            and len(self._publishing) < max_concurrent_publish()
        )

    async def _run_publish(self, batch_id: str, job_id: str, user_id: str) -> None:
        """발행 한 건. 끝나면 자리를 비우고 워커를 깨운다."""
        try:
            held = await hold(self._lease, lease_key(batch_id))
            if held is None:
                # 다른 프로세스가 이 배치를 발행하고 있다. 다음 tick에 다시 본다.
                return
            async with held:
                await self._service.execute_job(job_id, publish=True)
                await self._close_if_done(
                    batch_id, await self._repository.list_jobs(batch_id)
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 한 건의 실패가 워커를 죽이지 않는다
            logger.warning(
                "예약 발행 태스크 실패 | %s - %s: %s",
                short(job_id),
                type(error).__name__,
                error,
                exc_info=True,
            )
        finally:
            self._publishing.pop(user_id, None)
            self._wake.set()

    async def _run_prepare(self, batch_id: str, job_id: str) -> None:
        """원고 준비 한 건. 끝나면 자리를 비우고 워커를 깨운다."""
        try:
            held = await hold(self._lease, prepare_lease_key(job_id))
            if held is None:
                # 다른 프로세스가 이 작업의 원고를 만들고 있다.
                return
            async with held:
                await self._service.execute_job(job_id, publish=False)
                await self._close_if_done(
                    batch_id, await self._repository.list_jobs(batch_id)
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 한 건의 실패가 워커를 죽이지 않는다
            logger.warning(
                "예약 준비 태스크 실패 | %s - %s: %s",
                short(job_id),
                type(error).__name__,
                error,
                exc_info=True,
            )
        finally:
            self._preparing.pop(job_id, None)
            self._wake.set()

    @staticmethod
    def _due_to_publish(jobs: list[ScheduledJob], now: datetime) -> ScheduledJob | None:
        """지금 올려야 하는 작업. 시각이 이른 것부터.

        원고가 준비된 것(READY_TO_PUBLISH)만 고른다. 아직 원고가 없는데 시각이 지났으면
        준비부터 해야 하고, 그것은 아래 _due_to_prepare가 (시각이 이미 지났으므로 곧바로)
        집어 간다.
        """
        # 새 글 작성에서 넘어온 작업은 **원고가 끝나는 대로 올린다**(2026-08-11 사용자
        # 지적: "원고 생성했으면 발행이 바로 되어야지"). 그 예약에서 사용자가 고른 것은
        # 작업 시작 시각이고, 발행 시각은 준비 여유를 더해 만든 내부 값일 뿐이다 —
        # 원고가 먼저 끝났는데 그 값을 기다리면 아무 이유 없이 몇 분을 더 세운다.
        #
        # 예약 화면에서 만든 작업은 예전 그대로 자기 시각을 지킨다.
        # 앞 편이 끝나야 올라가는 작업(2026-08-12). 한 소재로 여러 편을 만들 때 2편·3편이
        # 1편을 가리킨다. **원고는 함께 만들되 발행은 순서대로** 한다는 사용자 결정이다 —
        # 같은 소재의 글이 뒤엉킨 순서로 올라가면 읽는 사람에게 앞뒤가 맞지 않는다.
        #
        # 앞 편이 실패했어도 다음은 올린다(FINISHED_JOB_STATUSES에 FAILED가 든다).
        # 가리키는 작업이 이 배치에 없으면 막지 않는다 — 영원히 대기하느니 한 번 돈다.
        finished = {job.job_id for job in jobs if job.status in FINISHED_JOB_STATUSES}
        known = {job.job_id for job in jobs}

        def turn_has_come(job: ScheduledJob) -> bool:
            if job.after_job_id is None:
                return True
            return job.after_job_id not in known or job.after_job_id in finished

        # **시각을 적지 않은 작업은 차례가 곧 시각이다**(2026-08-12). 「자동 포스팅」 탭에서
        # 줄의 시각을 비우면 그렇게 온다 — 앞 글이 끝나면(after_job_id) 그때가 올릴 때다.
        # 원고만 준비되면 더 기다릴 것이 없다.
        ready = [
            job
            for job in jobs
            if job.status == ScheduledJobStatus.READY_TO_PUBLISH
            # **올릴 곳이 없는 작업은 집지 않는다**(2026-08-12). 소재 단계에서 자동 발행을
            # 끄면 두 스위치가 모두 꺼진 채로 온다 — 원고까지만 만들고 작업 큐에 서 있어야
            # 하는 작업이다. 걸러내지 않으면 끈 예약도 발행으로 넘어간다.
            and publishes_anywhere(job)
            and turn_has_come(job)
            and (
                job.starts_from_prepared_post
                or job.publish_at is None
                or _is_due(job.publish_at, now)
            )
        ]
        if not ready:
            return None

        # **시각을 지정하지 않은 작업이 먼저 올라간다**(2026-08-13 사용자 지시).
        #
        #     "즉시발행인 작업보다 시간 예약을 걸어둔 작업이 원고생성이 먼저 완료된다
        #      해도, 시간지정을 하지 않은 즉시발행 작업 먼저 발행완료 하고 그 뒤에
        #      시간 예약을 걸어둔 작업이 발행되도록"
        #
        # 그러려면 **순서를 매기는 것만으로는 부족하다.** 예약 글의 원고가 먼저 끝나면
        # 그 순간 발행 후보는 그것 하나뿐이라, 정렬을 어떻게 하든 그것이 나간다. 그래서
        # 아직 올라가지 않은 즉시 작업이 하나라도 남아 있으면 **시각을 정한 작업은
        # 후보에서 뺀다** — 즉시 작업이 끝나기를 기다린다.
        #
        # 무한정 막히지는 않는다: 즉시 작업은 시각을 볼 것이 없어 곧바로 원고 준비에
        # 들어가고(_due_to_prepare), 실패하면 FINISHED_JOB_STATUSES에 들어가 문이 열린다.
        # 이 대가는 사용자가 알고 고른 것이다 — 예약 시각이 그만큼 밀릴 수 있다.
        #
        # **서로 기다리는 짝은 세지 않는다.** 「자동 포스팅」 탭에서 [시각 있음, 비움,
        # 비움]으로 걸면 비운 줄이 앞 줄(시각 있음)을 가리킨다(after_job_id). 그 줄까지
        # 세면 시각을 정한 줄은 비운 줄을 기다리고 비운 줄은 그 줄을 기다려 배치가
        # 통째로 멈춘다. 앞이 약속을 쥐고 있는 즉시 작업은 이 문지기에서 뺀다.
        by_id = {job.job_id: job for job in jobs}

        def waits_on_appointment(job: ScheduledJob) -> bool:
            seen: set[str] = set()
            cursor = job.after_job_id
            while cursor is not None and cursor not in seen:
                seen.add(cursor)
                ahead = by_id.get(cursor)
                # 가리키는 작업이 없거나 이미 끝났으면 막고 있는 것이 없다.
                if ahead is None or ahead.job_id in finished:
                    return False
                if has_appointment(ahead):
                    return True
                cursor = ahead.after_job_id
            return False

        pending_immediate = any(
            publishes_anywhere(job)
            and not has_appointment(job)
            and job.status not in FINISHED_JOB_STATUSES
            and not waits_on_appointment(job)
            for job in jobs
        )
        if pending_immediate:
            ready = [job for job in ready if not has_appointment(job)]
            if not ready:
                return None

        def publish_order(job: ScheduledJob) -> tuple[int, str, int]:
            """시각을 정하지 않은 쪽이 먼저, 그다음이 이른 시각 순.

            발행은 한 번에 하나뿐이라 순서가 곧 지연이다. 같은 자리면 입력 순서가
            먼저다(2026-08-10) — 두 글의 순서까지 타이밍 우연에 맡기지 않는다.
            """
            if has_appointment(job):
                return (1, job.publish_at or "", job.sequence)
            return (0, "", job.sequence)

        return min(ready, key=publish_order)

    @staticmethod
    def _due_to_prepare(jobs: list[ScheduledJob], now: datetime) -> list[ScheduledJob]:
        """지금 원고를 만들기 시작해야 하는 작업들 — **시각이 이른 것부터.**

        저장된 시각이 곧 작업을 시작할 시각이라, 그 시각이 지나면 시작한다. 앞당기지
        않는다(2026-08-12 사용자 지시로 준비 여유 20분을 없앴다).

        하나가 아니라 목록을 돌려준다(2026-08-07). 부르는 쪽이 빈 자리만큼 집어 가고,
        먼저 올라갈 글의 원고가 먼저 시작되도록 순서를 지킨다.
        """
        # 원고를 함께 만들지, 하나씩 줄 세울지. **갈림길이 둘 있다.**
        #
        # 1. **자동 발행을 켠 작업은 함께 만든다**(2026-08-12). 앞 편이 끝나기를 기다리지
        #    않는다 — 세 편이 순서대로면 마지막 편은 15~25분 뒤에나 나온다. 줄을 세우는
        #    것은 발행이고, 그것은 _due_to_publish가 한다.
        #
        # 2. **시각을 정하지 않은 작업도 함께 만든다**(2026-08-13 사용자 지시). 원래
        #    "자동발행을 안 켰으면 하나씩 순서대로"는 **시각을 정한 예약** 이야기였다
        #    (사용자: "내가 말한건 시간지정을 했을때의 경우였어"). 시각을 정하지 않고
        #    한 소재로 여러 편을 걸면 편마다 자료를 새로 모으며 최대
        #    MAX_CONCURRENT_PREPARE편이 동시에 돈다.
        #
        # 남는 것 하나 — **시각을 정했는데 올릴 곳은 없는 작업**만 줄을 선다. 올릴 것이
        # 없으니 발행이 순서를 잡아 주지 않는데, 어차피 사람이 하나씩 확인할 것이라
        # 서두를 이유가 없다(2026-08-12 사용자 지시).
        finished = {job.job_id for job in jobs if job.status in FINISHED_JOB_STATUSES}
        known = {job.job_id for job in jobs}

        def turn_has_come(job: ScheduledJob) -> bool:
            if job.after_job_id is None or publishes_anywhere(job) or not has_appointment(job):
                return True
            # 가리키는 작업이 이 배치에 없으면 막지 않는다 — 영원히 대기하느니 한 번 돈다.
            return job.after_job_id not in known or job.after_job_id in finished

        # **시각을 적지 않은 작업은 지금이 곧 준비할 때다**(2026-08-12). 언제 올릴지는
        # 앞 글이 정하지만(_due_to_publish), 원고는 미리 만들어 둬야 그 차례가 왔을 때
        # 기다리지 않는다 — '원고는 함께 만들고 발행만 줄 세운다'와 같은 태도다.
        waiting = [
            job
            for job in jobs
            if job.status == ScheduledJobStatus.WAITING
            and turn_has_come(job)
            and (
                # 시각을 적지 않은 작업은 볼 시각이 없다 — 지금이 곧 준비할 때다.
                job.publish_at is None
                # **저장된 시각이 곧 작업을 시작할 시각이다**(2026-08-12 사용자 지시로
                # 준비 여유 20분을 없앴다). 앞당기지 않는다 — 앞당기면 사용자가 고른
                # 시각보다 일찍 돈다.
                or _is_due(job.publish_at, now, lead=0.0)
            )
        ]
        return sorted(waiting, key=lambda job: (job.publish_at or "", job.sequence))

    @staticmethod
    def _seconds_until_next(jobs: list[ScheduledJob], now: datetime) -> float:
        """다음에 할 일이 있을 때까지의 초. 발행과 준비 중 이른 쪽이다.

        **생성 간격(interval_seconds)은 더 이상 보지 않는다**(2026-08-07). 절대 시각
        예약의 준비는 이제 동시에 돌므로, 앞 글의 준비가 끝나기를 기다릴 이유가 없다.
        그 설정은 간격 방식 배치에서 그대로 쓰인다.
        """
        waits: list[float] = []
        for job in jobs:
            due = parse_iso(job.publish_at)
            if due is None:
                continue
            if job.status in (
                ScheduledJobStatus.READY_TO_PUBLISH,
                # 준비도 그 시각에 시작한다 — 앞당길 여유가 없어졌으므로 기다림도 같다.
                ScheduledJobStatus.WAITING,
            ):
                waits.append((due - now).total_seconds())
        if not waits:
            return MAX_SLEEP_SECONDS
        return max(0.0, min(waits))

    async def _reconcile_controls(self, batch: ScheduledBatch) -> ScheduledBatch:
        """요청된 일시정지·정지를 지금 상태에 반영한다.

        실행 중인 작업이 없을 때만 최종 상태로 옮긴다 — 돌고 있는 작업은 자기가 끝나며
        스스로 자리를 비운다.

        **``current_job_id`` 하나만 보지 않는다**(2026-08-07). 그 칸은 한 자리뿐인데
        절대 시각 예약은 이제 원고를 여러 편 동시에 만든다. 나중에 시작한 작업이 그
        칸을 덮어쓰고 먼저 끝나면, 아직 도는 작업이 있는데도 칸이 비어 보인다 — 그
        상태로 정지를 확정하면 도는 작업이 배치 밖에 홀로 남는다. 실제로 무엇이 도는지는
        작업 상태가 안다.
        """
        if batch.current_job_id is not None:
            return batch
        if any(
            job.status in {ScheduledJobStatus.RUNNING, ScheduledJobStatus.PUBLISHING}
            for job in await self._repository.list_jobs(batch.batch_id)
        ):
            return batch
        now = now_iso()
        if batch.stop_requested and batch.status != ScheduledBatchStatus.STOPPED:
            await self._service._cancel_waiting_jobs(batch.batch_id)
            updated = batch.model_copy(
                update={
                    "status": ScheduledBatchStatus.STOPPED,
                    "completed_at": now,
                    "updated_at": now,
                }
            )
            await self._repository.save_batch(updated)
            return updated
        if batch.pause_requested and batch.status != ScheduledBatchStatus.PAUSED:
            updated = batch.model_copy(
                update={
                    "status": ScheduledBatchStatus.PAUSED,
                    "paused_at": now,
                    "updated_at": now,
                }
            )
            await self._repository.save_batch(updated)
            return updated
        return batch

    async def run_next_job(self, batch_id: str) -> bool:
        """이 배치의 다음 작업 하나를 생성부터 발행까지 한 번에 돌린다.

        **워커 루프는 더 이상 부르지 않는다**(2026-08-10 — 간격 방식도 준비 병렬·발행
        순차로 바뀌어 advance_interval_batch가 대신한다). 전 구간을 한 호출로 모는
        테스트·수동 경로를 위해 남겨 둔다. 입력 순서 게이트를 지나지 않으므로 운영
        루프에 다시 넣으면 안 된다.
        """
        held = await hold(self._lease, lease_key(batch_id))
        if held is None:
            # 다른 프로세스가 이 배치를 돌리고 있다.
            return False
        async with held:
            jobs = await self._repository.list_jobs(batch_id)
            nxt = next((job for job in jobs if job.status == ScheduledJobStatus.WAITING), None)
            if nxt is None:
                await self._close_if_done(batch_id, jobs)
                return False

            logger.info(
                "예약 작업 시작 | %s - %s", short(nxt.job_id), nxt.topic[:30]
            )
            await self._service.execute_job(nxt.job_id)

            refreshed = await self._repository.list_jobs(batch_id)
            await self._close_if_done(batch_id, refreshed)
            return True

    async def _close_if_done(self, batch_id: str, jobs: list) -> None:
        batch = await self._repository.find_batch(batch_id)
        if batch is None:
            return
        if batch.status in {ScheduledBatchStatus.NEEDS_HUMAN, ScheduledBatchStatus.PAUSED}:
            return
        if any(job.status == ScheduledJobStatus.WAITING for job in jobs):
            return
        if any(
            job.status
            in {
                ScheduledJobStatus.RUNNING,
                ScheduledJobStatus.PUBLISHING,
                ScheduledJobStatus.READY_TO_PUBLISH,
            }
            for job in jobs
        ):
            return

        if batch.stop_requested:
            await self._service._finish_batch(batch_id, ScheduledBatchStatus.STOPPED)
            return
        # 하나라도 성공했으면 완료다. 전부 실패했으면 배치도 실패로 닫는다.
        completed = any(job.status == ScheduledJobStatus.COMPLETED for job in jobs)
        await self._service._finish_batch(
            batch_id,
            ScheduledBatchStatus.COMPLETED if completed else ScheduledBatchStatus.FAILED,
        )
        await self._service._log(
            batch_id,
            "모든 예약 작업이 완료되었습니다." if completed else "예약 작업을 완료하지 못했습니다.",
            tone="success" if completed else "muted",
        )
