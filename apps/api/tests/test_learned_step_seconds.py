"""진행률 가중치를 어림값 대신 **실측**으로 잡는다(2026-08-11 사용자 요청).

"지금 어림값으로 하는데 그것보다 좋은 방법 없을까?" — reporter는 매 실행 단계별 실제
경과시간을 이미 재고 있었고(요약 로그로 한 번 찍고 버렸다), 그것을 쌓아 다음 실행의
가중치로 쓴다. 사람이 숫자를 다시 고를 필요가 없어진다.
"""

import pytest

from app.modules.blog_task import jobs
from app.modules.blog_task.jobs import (
    LEARNING_RATE,
    learned_step_seconds,
    record_step_seconds,
)
from app.shared import PHASE_STEPS, TaskPhase

STEPS = PHASE_STEPS[TaskPhase.DRAFT]


@pytest.fixture(autouse=True)
def _clear():
    jobs._learned_step_seconds.clear()
    yield
    jobs._learned_step_seconds.clear()


class TestLearning:
    def test_nothing_observed_yet_means_the_client_keeps_its_defaults(self):
        assert learned_step_seconds(TaskPhase.DRAFT, STEPS) is None

    def test_the_first_run_is_taken_as_is(self):
        record_step_seconds(TaskPhase.DRAFT, STEPS, [30.0, 60.0, 200.0, 40.0])

        assert learned_step_seconds(TaskPhase.DRAFT, STEPS) == [30.0, 60.0, 200.0, 40.0]

    def test_later_runs_move_the_average_toward_the_new_sample(self):
        record_step_seconds(TaskPhase.DRAFT, STEPS, [30.0, 60.0, 200.0, 40.0])
        record_step_seconds(TaskPhase.DRAFT, STEPS, [40.0, 60.0, 200.0, 40.0])

        learned = learned_step_seconds(TaskPhase.DRAFT, STEPS)
        # 30 → 30*(1-0.3) + 40*0.3 = 33
        assert learned[0] == pytest.approx(30 * (1 - LEARNING_RATE) + 40 * LEARNING_RATE)
        assert learned[1] == pytest.approx(60.0)

    def test_one_stuck_run_does_not_drag_the_average(self):
        """크롬이 멈춰 20분 걸린 한 번이 평균을 통째로 끌고 가면 안 된다."""
        record_step_seconds(TaskPhase.DRAFT, STEPS, [30.0, 60.0, 200.0, 40.0])
        record_step_seconds(TaskPhase.DRAFT, STEPS, [30.0, 60.0, 5_000.0, 40.0])

        learned = learned_step_seconds(TaskPhase.DRAFT, STEPS)
        assert learned[2] == pytest.approx(200.0)  # 이상치는 무시하고 이전 평균 유지

    @pytest.mark.parametrize(
        "observed",
        [
            [30.0, 60.0, 200.0],  # 단계 수가 안 맞는다
            [30.0, 0.0, 200.0, 40.0],  # 재지 못한 단계가 있다
            [-1.0, 60.0, 200.0, 40.0],
        ],
    )
    def test_a_half_measured_run_is_ignored(self, observed):
        """반쯤 채워진 값으로 막대를 그리면 어림값보다 나쁠 수 있다."""
        record_step_seconds(TaskPhase.DRAFT, STEPS, observed)

        assert learned_step_seconds(TaskPhase.DRAFT, STEPS) is None

    def test_phases_do_not_mix(self):
        record_step_seconds(TaskPhase.DRAFT, STEPS, [30.0, 60.0, 200.0, 40.0])

        search_steps = PHASE_STEPS[TaskPhase.SEARCH]
        assert learned_step_seconds(TaskPhase.SEARCH, search_steps) is None
