import logging

from app.modules.blog_task.jobs import ProgressReporter
from app.shared import TaskPhase, TaskProgress


class FakeClock:
    def __init__(self, now: float = 100.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ProgressSink:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.updates: list[TaskProgress | None] = []

    async def update_progress(
        self, post_id: str, progress: TaskProgress | None
    ) -> None:
        if self.fail:
            raise RuntimeError("storage unavailable")
        self.updates.append(progress)


def _summaries(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("PIPELINE summary")
    ]


async def test_completion_logs_every_stage_and_total_once(caplog):
    caplog.set_level(logging.INFO, logger="app.modules.blog_task.jobs")
    clock = FakeClock()
    sink = ProgressSink()
    reporter = ProgressReporter(
        sink, "post_1", TaskPhase.DRAFT, clock=clock
    )

    await reporter.step(0)
    clock.advance(1.2)
    await reporter.step(1)
    clock.advance(3.4)
    await reporter.step(2)
    clock.advance(5.4)
    await reporter.step(3)
    clock.advance(0.8)
    await reporter.clear(ok=True)
    await reporter.clear(ok=True)

    assert _summaries(caplog) == [
        "PIPELINE summary | phase=DRAFT post=post_1 ok=true total=10.8s "
        "stages=[원고 구조 설계=1.2s, 본문 원고 작성=3.4s, "
        "카드 이미지 생성=5.4s, 사실 검수·문장 다듬기=0.8s]"
    ]
    assert sink.updates[-1] is None


async def test_failure_summary_only_lists_stages_that_ran(caplog):
    caplog.set_level(logging.INFO, logger="app.modules.blog_task.jobs")
    clock = FakeClock()
    reporter = ProgressReporter(
        ProgressSink(), "post_1", TaskPhase.DRAFT, clock=clock
    )

    await reporter.step(0)
    clock.advance(2.5)
    await reporter.clear(ok=False)

    summary = _summaries(caplog)
    assert summary == [
        "PIPELINE summary | phase=DRAFT post=post_1 ok=false total=2.5s "
        "stages=[원고 구조 설계=2.5s]"
    ]
    assert "본문 원고 작성" not in summary[0]


async def test_summary_survives_progress_storage_failure(caplog):
    caplog.set_level(logging.INFO, logger="app.modules.blog_task.jobs")
    clock = FakeClock()
    reporter = ProgressReporter(
        ProgressSink(fail=True), "post_1", TaskPhase.SEARCH, clock=clock
    )

    await reporter.step(0)
    clock.advance(1.0)
    await reporter.clear(ok=False)

    assert _summaries(caplog) == [
        "PIPELINE summary | phase=SEARCH post=post_1 ok=false total=1.0s "
        "stages=[자료 검색=1.0s]"
    ]
    assert "진행 상황 기록 실패" in caplog.text
    assert "진행 상황 정리 실패" in caplog.text


async def test_activity_log_records_steps_and_details_for_the_screen():
    """'작업 현황' 로그(2026-08-10 사용자 요청) — 단계 시작과 내레이션이 줄로 쌓이고,
    status 응답이 그 목록을 실어 화면이 터미널 로그처럼 보여 준다."""
    from app.modules.blog_task.jobs import activity_log_for, reset_activity_log

    reset_activity_log("post_log")
    reporter = ProgressReporter(
        ProgressSink(), "post_log", TaskPhase.DRAFT, clock=FakeClock()
    )
    await reporter.step(0)
    await reporter.detail("서론·본론·결론 뼈대를 짜는 중이에요…")
    await reporter.detail("서론·본론·결론 뼈대를 짜는 중이에요…")  # 같은 문구 재전송
    await reporter.clear(ok=True)

    messages = [entry.message for entry in activity_log_for("post_log")]
    assert messages[0].endswith("시작했어요")
    assert any("1/" in message and "시작했어요" in message for message in messages)
    # 같은 내레이션이 연달아 와도 한 줄만 남는다.
    assert messages.count("서론·본론·결론 뼈대를 짜는 중이에요…") == 1
    assert messages[-1].startswith("원고 생성")
    assert "마쳤어요" in messages[-1]


async def test_a_new_run_starts_a_fresh_activity_log():
    """재생성 화면에 이전 실행의 줄이 섞이면 지금 무슨 일이 도는지 알 수 없다."""
    from app.modules.blog_task.jobs import activity_log_for

    first = ProgressReporter(
        ProgressSink(), "post_fresh", TaskPhase.DRAFT, clock=FakeClock()
    )
    await first.step(0)
    await first.detail("첫 실행의 줄")
    second = ProgressReporter(
        ProgressSink(), "post_fresh", TaskPhase.DRAFT, clock=FakeClock()
    )
    await second.step(0)

    messages = [entry.message for entry in activity_log_for("post_fresh")]
    assert "첫 실행의 줄" not in messages
