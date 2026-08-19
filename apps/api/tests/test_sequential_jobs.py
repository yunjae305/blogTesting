"""한 소재로 여러 편일 때 작업이 **줄지어** 돈다(2026-08-12 사용자 결정).

    "하나의 소재로 만들때는 최대 3개를 한번에 만들게 예약할 수 있는거고 이거는 첫 작업
    시각만 설정할 수 있어. 다음 작업은 현재 작업이 끝나고 나서 시작하는거야."
    "1편이 실패하면 해당작업의 상태는 실패라고 표시하고 다음 작업 진행하게 해야지"

**원고는 함께 만들고, 발행만 줄을 세운다**(2026-08-12 사용자 결정으로 바뀌었다).
순서대로 만들면 마지막 편이 15~25분 뒤에나 나오기 때문이다. 대신 같은 소재의 글이
뒤엉킨 순서로 올라가지 않도록, 발행은 앞 편이 끝나야 차례가 온다.
"""

from datetime import datetime, timezone

from app.modules.scheduled_posting.models import (
    ScheduledJob,
    ScheduledJobStatus,
    SchedulePlatform,
)
from app.modules.scheduled_posting.worker import ScheduledPostingWorker

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)
LATER = "2026-08-12T23:00:00Z"


# 준비 여유가 없어졌으므로(2026-08-12) "곧"은 이미 지난 시각이어야 한다.
SOON = "2026-08-12T08:59:00Z"


def _job(
    job_id: str,
    *,
    status=ScheduledJobStatus.WAITING,
    after: str | None = None,
    publish_at: str | None = LATER,
    publishes: bool = True,
    immediate: bool = False,
):
    return ScheduledJob(
        job_id=job_id,
        batch_id="batch_1",
        user_id="user_1",
        platform=SchedulePlatform.NAVER,
        sequence=0,
        topic="소재",
        post_id=f"post_{job_id}",
        status=status,
        starts_from_prepared_post=True,
        publish_naver=publishes,
        publish_threads=False,
        after_job_id=after,
        publish_at=publish_at,
        starts_immediately=immediate,
        created_at="2026-08-12T08:00:00Z",
        updated_at="2026-08-12T08:00:00Z",
    )


def _ready_ids(jobs):
    """지금 **원고를 만들** 작업들."""
    return [job.job_id for job in ScheduledPostingWorker._due_to_prepare(jobs, NOW)]


def _publish_id(jobs):
    """지금 **올릴** 작업 하나(없으면 None)."""
    job = ScheduledPostingWorker._due_to_publish(jobs, NOW)
    return job.job_id if job else None


class TestDraftsAreMadeTogether:
    """자동 발행을 **켠** 예약: 원고는 앞 편을 기다리지 않는다 — 셋이 함께 만들어진다."""

    def test_all_three_start_at_once(self):
        jobs = [
            _job("a", publish_at=SOON),
            _job("b", after="a", publish_at=SOON),
            _job("c", after="b", publish_at=SOON),
        ]

        assert _ready_ids(jobs) == ["a", "b", "c"]

    def test_the_one_before_running_does_not_hold_the_rest(self):
        jobs = [
            _job("a", status=ScheduledJobStatus.RUNNING, publish_at=SOON),
            _job("b", after="a", publish_at=SOON),
        ]

        assert _ready_ids(jobs) == ["b"]


class TestPublishingWaitsItsTurn:
    """발행은 순서대로다 — 같은 소재의 글이 뒤엉켜 올라가면 앞뒤가 맞지 않는다."""

    def test_the_second_does_not_go_up_before_the_first(self):
        jobs = [
            _job("a", status=ScheduledJobStatus.RUNNING),
            _job("b", after="a", status=ScheduledJobStatus.READY_TO_PUBLISH),
        ]

        assert _publish_id(jobs) is None

    def test_the_second_goes_up_once_the_first_is_done(self):
        jobs = [
            _job("a", status=ScheduledJobStatus.COMPLETED),
            _job("b", after="a", status=ScheduledJobStatus.READY_TO_PUBLISH),
        ]

        assert _publish_id(jobs) == "b"

    def test_a_failure_does_not_stop_the_rest(self):
        """사용자 결정: 실패는 그 작업에만 표시하고 다음은 진행한다."""
        jobs = [
            _job("a", status=ScheduledJobStatus.FAILED),
            _job("b", after="a", status=ScheduledJobStatus.READY_TO_PUBLISH),
        ]

        assert _publish_id(jobs) == "b"

    def test_only_the_next_one_goes_up(self):
        """원고가 셋 다 준비돼도 올라가는 것은 차례가 온 하나뿐이다."""
        jobs = [
            _job("a", status=ScheduledJobStatus.COMPLETED),
            _job("b", after="a", status=ScheduledJobStatus.READY_TO_PUBLISH),
            _job("c", after="b", status=ScheduledJobStatus.READY_TO_PUBLISH),
        ]

        assert _publish_id(jobs) == "b"


class TestOldJobsAreUnaffected:
    def test_a_job_without_a_predecessor_goes_by_its_own_time(self):
        """옛 작업 문서에는 after_job_id가 없다 — 예전처럼 자기 시각만 본다."""
        assert _ready_ids([_job("a")]) == []  # 시각이 아직 멀다
        assert _ready_ids([_job("a", publish_at=SOON)]) == ["a"]

    def test_a_dangling_reference_does_not_freeze_publishing(self):
        """가리키는 작업이 사라졌으면 막지 않는다 — 영원히 대기하는 것보다 낫다."""
        jobs = [_job("b", after="없는작업", status=ScheduledJobStatus.READY_TO_PUBLISH)]

        assert _publish_id(jobs) == "b"


class TestWithoutPublishingTheDraftsQueueUp:
    """자동 발행을 **끈**, 그리고 **시각을 정한** 예약(2026-08-12 사용자 추가 지시).

        "자동발행 체크박스를 선택하지 않았다면 발행이 아닌 원고생성만 되는 것이니
        이때는 원고가 동시 생성이 아니라 하나하나 순서대로 생성되어야해"

    올릴 것이 없으니 발행이 순서를 잡아 주지 않는다. 그대로 두면 셋이 한꺼번에 돌아
    기계만 붙드는데, 어차피 사람이 하나씩 확인할 것이라 서두를 이유가 없다.

    **범위는 시각을 정한 예약까지다**(2026-08-13 사용자 확인: "내가 말한건 시간지정을
    했을때의 경우였어"). 시각을 정하지 않은 쪽은 아래 TestImmediateDraftsAreMadeTogether.
    """

    def test_only_the_first_starts(self):
        jobs = [
            _job("a", publish_at=SOON, publishes=False),
            _job("b", after="a", publish_at=SOON, publishes=False),
            _job("c", after="b", publish_at=SOON, publishes=False),
        ]

        assert _ready_ids(jobs) == ["a"]

    def test_the_next_starts_when_the_one_before_is_done(self):
        jobs = [
            _job("a", status=ScheduledJobStatus.COMPLETED, publishes=False),
            _job("b", after="a", publish_at=SOON, publishes=False),
            _job("c", after="b", publish_at=SOON, publishes=False),
        ]

        assert _ready_ids(jobs) == ["b"]

    def test_a_failure_does_not_stop_the_rest(self):
        jobs = [
            _job("a", status=ScheduledJobStatus.FAILED, publishes=False),
            _job("b", after="a", publish_at=SOON, publishes=False),
        ]

        assert _ready_ids(jobs) == ["b"]

    def test_publishing_jobs_are_untouched_by_this_rule(self):
        """켠 예약은 그대로 함께 만들어진다 — 규칙이 서로 새지 않는다."""
        jobs = [
            _job("a", publish_at=SOON),
            _job("b", after="a", publish_at=SOON),
        ]

        assert _ready_ids(jobs) == ["a", "b"]


class TestImmediateDraftsAreMadeTogether:
    """시각을 **정하지 않은** 여러 편(2026-08-13 사용자 지시).

        "지금은 시간지정을 안한 경우에 여러편 원고 생성할때는 3단계인 검증단계에서도
        자료수집을 각 원고마다 진행을 하며 원고 생성을 최대 3편까지 동시에 생성하는거야.
        물론 자동발행을 체크해뒀으면 1편부터 순서대로 발행도 되게 할거고"

    바로 앞의 '끈 예약은 줄을 세운다'와 갈리는 자리다. 그 규칙은 시각을 정한 예약
    이야기였고, 여기는 시각을 정하지 않은 쪽이다.
    """

    def test_all_three_start_at_once_even_without_publishing(self):
        """자동 발행을 안 켰어도 함께 만든다 — 시각을 정하지 않았기 때문이다."""
        jobs = [
            _job("a", immediate=True, publish_at=SOON, publishes=False),
            _job("b", after="a", immediate=True, publish_at=SOON, publishes=False),
            _job("c", after="b", immediate=True, publish_at=SOON, publishes=False),
        ]

        assert _ready_ids(jobs) == ["a", "b", "c"]

    def test_an_empty_time_counts_the_same(self):
        """「자동 포스팅」 탭에서 줄의 시각을 비운 경우다 — publish_at 자체가 없다."""
        jobs = [
            _job("a", publish_at=None, publishes=False),
            _job("b", after="a", publish_at=None, publishes=False),
        ]

        assert _ready_ids(jobs) == ["a", "b"]

    def test_publishing_still_goes_one_by_one(self):
        """원고는 함께 만들어도 발행은 1편부터다 — 사용자가 함께 지시한 부분이다."""
        jobs = [
            _job("a", immediate=True, publish_at=SOON, status=ScheduledJobStatus.RUNNING),
            _job(
                "b",
                after="a",
                immediate=True,
                publish_at=SOON,
                status=ScheduledJobStatus.READY_TO_PUBLISH,
            ),
        ]

        # 1편이 아직 안 끝났으므로 2편은 준비돼 있어도 올라가지 않는다.
        assert _publish_id(jobs) is None


class TestAutoPublishIsHonouredEvenAfterARetry:
    """자동 발행 체크 여부는 **작업에 새겨져** 재시도해도 그대로다(2026-08-12).

        "사용자가 작업을 이어서 진행하게 해도 해당 글이 자동발행 체크박스에 체크가
        되어있던 작업이면 발행 진행하고 체크가 되어있지 않았던 작업이면 원고생성만 해"

    체크 여부는 소재 단계에서 정해져 작업의 발행 스위치(publish_naver·publish_threads)로
    남는다. 재시도는 그 값을 바꾸지 않으므로, 여기서 지킬 것은 **발행 차례를 고를 때
    그 값을 본다**는 것뿐이다.
    """

    def test_a_draft_only_job_is_never_picked_for_publishing(self):
        """자동 발행을 끈 작업은 원고가 준비돼도 올리지 않는다."""
        jobs = [_job("a", status=ScheduledJobStatus.READY_TO_PUBLISH, publishes=False)]

        assert _publish_id(jobs) is None

    def test_a_publishing_job_is_still_picked(self):
        jobs = [_job("a", status=ScheduledJobStatus.READY_TO_PUBLISH)]

        assert _publish_id(jobs) == "a"

    def test_a_draft_only_job_does_not_block_the_one_after_it(self):
        """끈 작업이 발행 줄을 막으면, 그 뒤의 켠 작업이 영영 올라가지 못한다."""
        jobs = [
            _job("a", status=ScheduledJobStatus.COMPLETED, publishes=False),
            _job("b", after="a", status=ScheduledJobStatus.READY_TO_PUBLISH),
        ]

        assert _publish_id(jobs) == "b"
