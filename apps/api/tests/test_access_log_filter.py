"""폴링 access 로그 필터 — 성공한 폴링만 조용히 하고 나머지는 전부 남긴다.

화면이 몇 초마다 /posts/{id}/status 와 /posting/verification 을 두드려서, 성공 로그가
터미널을 도배했다(2026-08-10 사용자: "로그가 되게 기네?"). 실패는 계속 보여야 한다 —
조용해진 로그가 장애까지 숨기면 그날로 이 필터를 의심하게 된다.
"""

import logging

from app.main import _drop_polling_access_logs


def access_record(args) -> logging.LogRecord:
    """uvicorn.access가 만드는 것과 같은 모양의 레코드."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=args,
        exc_info=None,
    )


class TestPollingAccessLogFilter:
    def test_successful_status_polling_is_dropped(self):
        record = access_record(
            ("127.0.0.1:50000", "GET", "/posts/post_1/status", "1.1", 200)
        )
        assert _drop_polling_access_logs(record) is False

    def test_successful_verification_polling_is_dropped(self):
        record = access_record(
            ("127.0.0.1:50000", "GET", "/posting/verification", "1.1", 200)
        )
        assert _drop_polling_access_logs(record) is False

    def test_a_query_string_does_not_unhide_the_polling(self):
        record = access_record(
            ("127.0.0.1:50000", "GET", "/posting/verification?fresh=1", "1.1", 200)
        )
        assert _drop_polling_access_logs(record) is False

    def test_failed_polling_stays_visible(self):
        """500이 도배되는 것은 시끄러운 게 아니라 알아야 하는 일이다."""
        record = access_record(
            ("127.0.0.1:50000", "GET", "/posts/post_1/status", "1.1", 500)
        )
        assert _drop_polling_access_logs(record) is True

    def test_other_requests_stay_visible(self):
        record = access_record(
            ("127.0.0.1:50000", "POST", "/posts/post_1/publish", "1.1", 200)
        )
        assert _drop_polling_access_logs(record) is True
        record = access_record(("127.0.0.1:50000", "GET", "/posts/post_1", "1.1", 200))
        assert _drop_polling_access_logs(record) is True

    def test_an_unexpected_record_shape_is_left_alone(self):
        """uvicorn 내부 형식이 바뀌면 거르지 않는다 — 로그를 잃는 쪽이 더 나쁘다."""
        record = access_record(("only", "two"))
        assert _drop_polling_access_logs(record) is True
