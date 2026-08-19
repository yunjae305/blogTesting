"""원고 작업 예약 시각(2026-08-11 사용자 지시).

"시간을 입력하면 예약 발행, 넣지 않는다면 바로 원고 생성하는 단일글 생성 기능처럼."

그래서 이 값의 계약은 **없어도 되는 것**이다 — 비어 있으면 예전과 한 글자도 다르지 않게
동작해야 한다. 있을 때만 예약 경로가 열린다.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.errors import BlogTaskError
from app.modules.blog_task.validation import (
    MAX_SCHEDULE_AHEAD_DAYS,
    validate_blog_task_input,
    validate_scheduled_run_at,
)

NOW = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)


def body(**overrides) -> dict:
    return {"topic": "에어프라이어", "purpose": ["정보 전달"], **overrides}


class TestOptional:
    def test_no_time_means_the_old_single_post_flow(self):
        """비워 두면 예전 그대로 — 방향을 고르는 즉시 원고를 만든다."""
        result = validate_blog_task_input(body())

        assert result.scheduled_run_at is None
        assert result.scheduled_timezone is None

    def test_a_timezone_without_a_time_is_not_kept(self):
        """시각 없이 시간대만 남으면 아무 뜻도 없는 값이 문서에 남는다."""
        result = validate_blog_task_input(body(scheduledTimezone="Asia/Seoul"))

        assert result.scheduled_run_at is None
        assert result.scheduled_timezone is None


class TestAccepted:
    def test_a_future_time_is_stored_as_utc(self):
        later = (NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z")

        assert validate_scheduled_run_at(later, now=NOW) == "2026-08-13T06:00:00Z"

    def test_an_offset_time_is_converted_not_shifted(self):
        """사용자는 자기 시간대로 고른다. 서버는 그것을 UTC로 옮겨 적을 뿐이다."""
        # 2026-08-13 15:00 +09:00 == 2026-08-13 06:00Z
        assert (
            validate_scheduled_run_at("2026-08-13T15:00:00+09:00", now=NOW)
            == "2026-08-13T06:00:00Z"
        )

    def test_the_input_keeps_the_timezone_for_display(self):
        # 이 경로(validate_blog_task_input)는 now를 주입받지 않아 **실제 시계**로 검증한다.
        # 고정 NOW로 미래를 만들면 그 시각이 지나는 순간부터 영영 실패한다 — 2026-08-12
        # 06:00Z에 실제로 그렇게 됐다. 그래서 지금 시각을 기준으로 하루 뒤를 만든다.
        later = (
            (datetime.now(timezone.utc) + timedelta(days=1))
            .isoformat()
            .replace("+00:00", "Z")
        )

        result = validate_blog_task_input(
            body(scheduledRunAt=later, scheduledTimezone="Asia/Seoul")
        )

        assert result.scheduled_run_at
        assert result.scheduled_timezone == "Asia/Seoul"


class TestRejected:
    def test_a_past_time_is_refused(self):
        """지난 시각을 받으면 걸자마자 도는 셈이라 '지금 만들기'와 구분이 사라진다."""
        past = (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z")

        # 이 메시지는 화면 알림창에 그대로 뜬다 — 한국어여야 한다(2026-08-12).
        with pytest.raises(BlogTaskError, match="이미 지났습니다"):
            validate_scheduled_run_at(past, now=NOW)

    def test_a_time_too_far_ahead_is_refused(self):
        """두 달 뒤 글은 지금 방향을 정해 둘 값어치가 없다 — 날짜를 잘못 고른 것으로 본다."""
        far = (NOW + timedelta(days=MAX_SCHEDULE_AHEAD_DAYS + 1)).isoformat().replace(
            "+00:00", "Z"
        )

        with pytest.raises(BlogTaskError, match="안이어야 합니다"):
            validate_scheduled_run_at(far, now=NOW)

    @pytest.mark.parametrize("value", ["내일 3시", "", "   ", "2026-13-45"])
    def test_something_that_is_not_a_time_is_refused(self, value):
        with pytest.raises(BlogTaskError):
            validate_scheduled_run_at(value, now=NOW)

    def test_a_naive_time_is_read_as_utc_not_local(self):
        """서버가 임의로 로컬 시간대를 붙이면 배포 환경에 따라 몇 시간씩 밀린다."""
        assert (
            validate_scheduled_run_at("2026-08-13T06:00:00", now=NOW)
            == "2026-08-13T06:00:00Z"
        )
