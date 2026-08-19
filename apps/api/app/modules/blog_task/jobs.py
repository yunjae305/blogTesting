"""긴 단계를 백그라운드에서 돌리고 어디까지 왔는지 알린다.

M3는 실제 모델 시간으로 1분쯤 걸리고 M4도 비슷하다. 그동안 HTTP 요청을 붙잡고 있으면
클라이언트가 할 수 있는 건 스피너와 짐작뿐이다. 대신 작업은 분리해 돌리고, 클라이언트는
글을 폴링해 서버가 실제로 있는 단계를 본다.
"""

import asyncio
import logging
import time
from app.shared.format import now_iso as _now
from collections import deque
from typing import Awaitable, Callable, Protocol

from app.shared import PHASE_STEPS, ActivityEntry, TaskPhase, TaskProgress
from app.shared.ids import short

logger = logging.getLogger(__name__)


# 터미널에 찍을 이름. 화면에 뜨는 단계 이름(PHASE_STEPS)과 같은 말을 쓴다 — 사용자가
# 보는 것과 개발자가 보는 것이 다른 말이면 둘을 맞춰 보느라 시간을 쓴다.
PHASE_NAMES: dict[TaskPhase, str] = {
    TaskPhase.SEARCH: "검증(자료 검색)",
    TaskPhase.DRAFT: "원고 생성",
}


class ProgressSink(Protocol):
    async def update_progress(self, post_id: str, progress: TaskProgress | None) -> None: ...


# 생성 중 화면의 '작업 현황' 로그(2026-08-10 사용자 요청 — 기다리는 동안 지금 무엇을
# 하는지 보이게). 단계 라벨과 detail 내레이션 같은 **사용자 문구만** 쌓는다 — 서버
# 로그의 URL·식별자·예외 원문을 그대로 흘리지 않는다. 프로세스 메모리에만 두고(참고
# 표시일 뿐이라 재시작에 잃어도 정확성에 영향 없음), 같은 글의 새 생성이 시작되면
# 새로 쌓는다. /posts/{id}/status가 이 목록을 함께 돌려준다.
ACTIVITY_LOG_LIMIT = 40
_activity_logs: dict[str, deque[ActivityEntry]] = {}


def _with_object_particle(word: str) -> str:
    """마지막 글자 받침 유무로 을/를을 고른다 — 화면에 '원고 생성을(를)' 병기를 남기지
    않는다(2026-08-10 실화면 지적)."""
    last = word[-1]
    if "가" <= last <= "힣" and (ord(last) - ord("가")) % 28:
        return f"{word}을"
    return f"{word}를"


def _with_subject_particle(word: str) -> str:
    last = word[-1]
    if "가" <= last <= "힣" and (ord(last) - ord("가")) % 28:
        return f"{word}이"
    return f"{word}가"


def record_activity(post_id: str, message: str) -> None:
    log = _activity_logs.setdefault(post_id, deque(maxlen=ACTIVITY_LOG_LIMIT))
    if log and log[-1].message == message:
        return  # 같은 문구가 연달아 오면 한 줄만 남긴다(내레이션 재전송 대비).
    log.append(ActivityEntry(at=_now(), message=message))


def reset_activity_log(post_id: str) -> None:
    _activity_logs.pop(post_id, None)


def activity_log_for(post_id: str) -> list[ActivityEntry]:
    return list(_activity_logs.get(post_id, ()))


#: 단계별 실제 소요의 누적 평균(초). 키는 (단계 묶음, 단계 수)다(2026-08-11).
#:
#: 왜 필요한가. 진행률 막대의 단계 몫은 상수표의 **어림값**이었고, 코드 주석부터
#: "실측 기반의 어림값"이라 적고 있었다. 그런데 이 reporter는 매 실행 단계별 실제
#: 경과시간을 이미 재고 있다(_step_durations) — 요약 로그로 한 번 찍고 버렸다.
#:
#: 그것을 지수이동평균으로 쌓아 다음 실행의 가중치로 쓴다. 회선이 느리거나 이미지를
#: 많이 넣는 환경에 **쓸수록 맞춰진다** — 사람이 숫자를 다시 고를 필요가 없다.
#:
#: 프로세스 메모리다. 재시작하면 클라이언트의 기본 상수부터 다시 시작하고(그때도
#: 예전과 똑같이 동작한다), 몇 번 돌면 다시 수렴한다. DB에 두지 않는 이유는 이 값이
#: 표시용 어림이라 잃어도 정확성에 영향이 없기 때문이다.
_learned_step_seconds: dict[tuple[str, int], list[float]] = {}

#: 새 관측이 평균에 실리는 비중. 낮으면 굼뜨고 높으면 한 번의 이상치에 휘둘린다.
LEARNING_RATE = 0.3
#: 이만큼 벗어난 관측은 버린다(중앙값 대비 배수). 크롬이 멈춰 20분 걸린 한 번이
#: 평균을 통째로 끌고 가지 않게 한다.
OUTLIER_FACTOR = 5.0


def learned_step_seconds(phase: TaskPhase, steps: list[str]) -> list[float] | None:
    """이 단계 묶음의 학습된 소요(초). 아직 관측이 없으면 None(화면이 기본 상수를 쓴다)."""
    return _learned_step_seconds.get((phase.value, len(steps)))


def record_step_seconds(phase: TaskPhase, steps: list[str], observed: list[float]) -> None:
    """한 번의 실행에서 잰 단계별 소요를 평균에 반영한다.

    **끝까지 돈 실행만 부른다.** 중간에 실패한 실행의 단계 소요는 '그 단계가 얼마나
    걸리는가'를 말해 주지 않는다.
    """
    if len(observed) != len(steps) or any(value <= 0 for value in observed):
        return
    key = (phase.value, len(steps))
    current = _learned_step_seconds.get(key)
    if current is None:
        _learned_step_seconds[key] = list(observed)
        return
    middle = sorted(current)[len(current) // 2] or 1.0
    updated = []
    for previous, sample in zip(current, observed, strict=True):
        if sample > middle * OUTLIER_FACTOR:
            updated.append(previous)  # 이상치는 무시하고 이전 평균을 유지한다
        else:
            updated.append(previous * (1 - LEARNING_RATE) + sample * LEARNING_RATE)
    _learned_step_seconds[key] = updated


class ProgressReporter:
    """태스크를 한 단계의 스텝들을 따라 진행시킨다.

    진행 상황 기록 실패는 삼킨다: 클라이언트가 오래된 라벨을 보게 될 뿐인데, 그것 때문에
    잘 돌아가던 생성을 실패시킬 이유는 없다.
    """

    def __init__(
        self,
        sink: ProgressSink,
        post_id: str,
        phase: TaskPhase,
        *,
        clock: Callable[[], float] = time.monotonic,
        steps: tuple[str, ...] | list[str] | None = None,
        name: str | None = None,
    ):
        """``steps``·``name``으로 **그 실행의 칸 이름을 갈아 끼울 수 있다**(2026-08-12).

        같은 단계(SEARCH)라도 하는 일이 다를 수 있기 때문이다: 자료를 모으는 실행은
        '자료 검색 → 검증 후보 정리' 두 칸이지만, 자료를 모으지 않는 실행(여러 편·예약
        글)은 방향을 나누는 한 칸뿐이다. 그런데도 고정된 이름을 쓰면 화면이 **하지 않은
        일을 했다고 말한다** — 사용자가 그것을 신고했다("최신자료 다시 모은다는데 애초에
        안모으고 …하는거 아니야?").
        """
        self._sink = sink
        self._post_id = post_id
        self._phase = phase
        self._name = name or PHASE_NAMES[phase]
        self._steps = list(steps) if steps else PHASE_STEPS[phase]
        # 학습된 단계 소요는 **여기서 한 번만 읽는다.** 도는 중에 바뀌면 같은 시점의
        # 퍼센트가 달라져 막대가 뒤로 갈 수 있다.
        self._step_seconds = learned_step_seconds(phase, self._steps)
        self._started_at = _now()
        self._now = clock
        self._phase_started = self._now()
        self._step_started = self._phase_started
        # 지금 단계가 시작된 벽시계 시각. 클라이언트는 progress.updatedAt을 '이 단계가
        # 시작된 시각'으로 읽어 경과 시간을 그린다(StepDraft) — detail()이 이 값을 새로
        # 찍으면 내레이션이 올 때마다 화면의 경과 시간이 0으로 되돌아간다(2026-08-07
        # 사용자 신고: 4초에서 1초로 튄다).
        self._step_started_at = self._started_at
        self._active_step: int | None = None
        self._step_durations: list[float | None] = [None] * len(self._steps)
        self._finished = False
        # 새 실행의 로그는 새로 쌓는다 — 재생성 화면에 이전 실행의 줄이 섞이면 지금
        # 무슨 일이 도는지 알 수 없다.
        reset_activity_log(post_id)
        record_activity(post_id, f"{_with_object_particle(self._name)} 시작했어요")
        logger.info("%s 시작 | %s", self._name, short(post_id))

    @property
    def step_count(self) -> int:
        """이 실행의 칸 수. 부르는 쪽이 있는 칸에만 step()을 걸 수 있게 한다."""
        return len(self._steps)

    async def step(self, index: int) -> None:
        """`index`는 0부터 센다. 클라이언트에는 N개 중 1번 스텝으로 보인다."""
        if not 0 <= index < len(self._steps):
            raise IndexError(f"progress step index out of range: {index}")

        now = self._now()
        previous_step = self._active_step
        previous_elapsed: float | None = None
        if previous_step is not None:
            previous_elapsed = self._close_active_step(now)

        self._active_step = index
        # 첫 단계는 reporter를 만든 순간부터 센다. 백그라운드 잡이 실행되기까지의 대기도
        # 사용자가 실제로 기다린 시간이고, 그래야 단계 합과 전체 시간이 어긋나지 않는다.
        if previous_step is not None:
            self._step_started = now
            self._step_started_at = _now()

        previous = (
            ""
            if previous_step is None or previous_elapsed is None
            else (
                f" (이전 {self._steps[previous_step]} "
                f"{previous_elapsed:.1f}초)"
            )
        )
        logger.info(
            "%s %d/%d %s | %s%s",
            self._name,
            index + 1,
            len(self._steps),
            self._steps[index],
            short(self._post_id),
            previous,
        )
        record_activity(
            self._post_id,
            f"{index + 1}/{len(self._steps)} {self._steps[index]} 단계를 시작했어요",
        )
        try:
            await self._sink.update_progress(
                self._post_id,
                TaskProgress(
                    phase=self._phase,
                    step=index + 1,
                    total_steps=len(self._steps),
                    label=self._steps[index],
                    steps=list(self._steps),
                    step_seconds=self._step_seconds,
                    started_at=self._started_at,
                    updated_at=self._step_started_at,
                ),
            )
        except Exception as error:
            logger.warning("진행 상황 기록 실패 | %s - %s", short(self._post_id), error)

    async def detail(
        self, message: str, *, units_done: int | None = None, units_total: int | None = None
    ) -> None:
        """지금 단계 **안에서** 무엇을 하는 중인지만 바꾼다.

        단계 번호·단계 수·단계 목록은 그대로다. 한 단계가 여러 일을 하는 경우(최종 검수는
        검수 → 확인 → 수정 → 완료를 한 단계에서 한다) 그것을 단계로 쪼개면 진행률 막대가
        통째로 다시 그려지고, 저장된 옛 글의 진행 표시와도 어긋난다.

        시간 측정에는 끼어들지 않는다 — 이 호출은 단계를 넘기는 것이 아니다.
        ``updated_at``도 단계 시작 시각 그대로 둔다: 클라이언트가 그 값으로 '이 단계에
        머문 시간'을 그리는데, 내레이션마다 새 시각을 찍으면 경과 시간이 매번 0으로
        되돌아간다(2026-08-07 사용자 신고: 4초 → 1초로 튐).
        """
        if self._active_step is None:
            return
        record_activity(self._post_id, message)
        try:
            await self._sink.update_progress(
                self._post_id,
                TaskProgress(
                    phase=self._phase,
                    step=self._active_step + 1,
                    # 단계 안에서 몇 개를 끝냈는지(있을 때만). 화면은 이 값이
                    # 있으면 시간 추정 대신 사실 비율로 그 칸을 채운다.
                    units_done=units_done,
                    units_total=units_total,
                    total_steps=len(self._steps),
                    label=message,
                    steps=list(self._steps),
                    step_seconds=self._step_seconds,
                    started_at=self._started_at,
                    updated_at=self._step_started_at,
                ),
            )
        except Exception as error:
            logger.warning("진행 상세 기록 실패 | %s - %s", short(self._post_id), error)

    def _close_active_step(self, now: float) -> float | None:
        if self._active_step is None:
            return None
        elapsed = max(0.0, now - self._step_started)
        accumulated = self._step_durations[self._active_step] or 0.0
        self._step_durations[self._active_step] = accumulated + elapsed
        return elapsed

    async def clear(self, *, ok: bool = True) -> None:
        """진행 표시를 지우고, 실행된 단계들의 실제 경과시간을 한 번에 요약한다."""
        if not self._finished:
            now = self._now()
            self._close_active_step(now)
            self._active_step = None
            if ok:
                # 끝까지 돈 실행만 평균에 싣는다 — 중간에 죽은 실행의 단계 소요는
                # '그 단계가 얼마나 걸리는가'를 말해 주지 않는다.
                record_step_seconds(
                    self._phase,
                    self._steps,
                    [value or 0.0 for value in self._step_durations],
                )
            total = max(0.0, now - self._phase_started)
            stages = ", ".join(
                f"{label}={duration:.1f}s"
                for label, duration in zip(self._steps, self._step_durations)
                if duration is not None
            )
            logger.info(
                "PIPELINE summary | phase=%s post=%s ok=%s total=%.1fs stages=[%s]",
                self._phase.value,
                short(self._post_id),
                str(ok).lower(),
                total,
                stages,
            )
            record_activity(
                self._post_id,
                f"{_with_object_particle(self._name)} 마쳤어요 (총 {total:.0f}초)"
                if ok
                else f"{_with_subject_particle(self._name)} 중단됐어요",
            )
            self._finished = True

        try:
            await self._sink.update_progress(self._post_id, None)
        except Exception as error:
            logger.warning("진행 상황 정리 실패 | %s - %s", short(self._post_id), error)


class BackgroundJobs:
    """분리된 태스크를 모두 참조로 붙잡아 둔다.

    asyncio는 실행 중인 태스크를 약한 참조로만 잡으므로, 아무도 참조하지 않는 태스크는
    await 도중에 GC될 수 있다 — 그러면 생성은 그냥 멈추고, 글은 GENERATING에 남으며
    에러는 어디에도 없다.
    """

    def __init__(self) -> None:
        self._running: set[asyncio.Task] = set()

    def start(self, coro: Awaitable[None], *, on_error: Callable[[BaseException], None] | None = None):
        task = asyncio.create_task(coro)  # type: ignore[arg-type]
        self._running.add(task)

        def done(finished: asyncio.Task) -> None:
            self._running.discard(finished)
            error = finished.exception() if not finished.cancelled() else None
            if error and on_error:
                on_error(error)
            elif error:
                logger.exception("background job failed", exc_info=error)

        task.add_done_callback(done)
        return task

    async def drain(self) -> None:
        """진행 중인 것을 기다린다 — 종료 시점과 테스트에서 쓴다."""
        while self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)

    async def cancel(self) -> None:
        """종료 중인 프로세스의 작업을 취소한다. 영속 상태는 다음 시작 때 복구한다."""
        tasks = list(self._running)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
