"""발행 자동화와 화면 사이의 2단계 인증 코드 창구.

여기서 막는 것: 코드를 넣었는데 자동화가 못 깨어나는 것, 반대로 아무도 안 기다리는데
코드가 받아들여지는 것, 그리고 발행이 겹칠 때 옛 요청에 코드가 들어가는 것.
"""

import threading
import time

import pytest

from app.posting.verification import MAX_ATTEMPTS, VerificationBroker


@pytest.fixture
def broker() -> VerificationBroker:
    # 전역 broker를 쓰면 테스트끼리 대기 상태가 섞인다.
    return VerificationBroker()


def _wait_in_thread(broker: VerificationBroker, request, timeout: float, into: dict):
    def run():
        into["code"] = broker.wait(request, timeout=timeout)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


class TestTheCodeReachesTheWaitingAutomation:
    def test_a_submitted_code_wakes_the_worker(self, broker):
        request = broker.request(
            user_id="u1", post_id="p1", channel="threads", prompt="코드를 넣어 주세요"
        )
        got: dict = {}
        thread = _wait_in_thread(broker, request, 5, got)

        # 화면은 무엇을 물어보는지 볼 수 있어야 한다.
        pending = broker.pending("u1")
        assert pending is not None
        assert pending.as_dict()["prompt"] == "코드를 넣어 주세요"
        assert pending.as_dict()["postId"] == "p1"

        assert broker.submit("u1", "123456") is True
        thread.join(5)
        assert got["code"] == "123456"
        # 코드를 받은 요청은 대기 목록에서 빠진다 — 화면이 창을 닫을 근거다.
        assert broker.pending("u1") is None

    def test_a_cancelled_request_returns_nothing(self, broker):
        request = broker.request(user_id="u1", post_id="p1", channel="threads", prompt="코드")
        got: dict = {}
        thread = _wait_in_thread(broker, request, 5, got)

        assert broker.cancel("u1") is True
        thread.join(5)
        # None이면 호출부가 NEEDS_HUMAN으로 끝낸다.
        assert got["code"] is None

    def test_a_timeout_returns_nothing(self, broker):
        request = broker.request(user_id="u1", post_id="p1", channel="threads", prompt="코드")
        assert broker.wait(request, timeout=0.05) is None
        assert broker.pending("u1") is None


class TestTheBrokerRefusesWhatItCannotDeliver:
    def test_submitting_without_a_pending_request_fails(self, broker):
        # 아무도 안 기다리는데 True를 돌려주면 화면이 '넘겼다'고 잘못 말한다.
        assert broker.submit("u1", "123456") is False
        assert broker.cancel("u1") is False

    def test_pending_is_per_user(self, broker):
        broker.request(user_id="u1", post_id="p1", channel="threads", prompt="코드")
        assert broker.pending("u1") is not None
        assert broker.pending("u2") is None

    def test_a_new_request_cancels_the_previous_one(self, broker):
        """발행이 겹치면 옛 요청은 버린다 — 안 그러면 새 코드가 옛 자동화로 간다."""
        first = broker.request(user_id="u1", post_id="p1", channel="threads", prompt="코드")
        got: dict = {}
        thread = _wait_in_thread(broker, first, 5, got)
        time.sleep(0.1)

        second = broker.request(user_id="u1", post_id="p2", channel="threads", prompt="코드")
        thread.join(5)
        assert got["code"] is None  # 앞 요청은 취소돼 깨어난다

        assert broker.pending("u1") is second
        assert broker.pending("u1").as_dict()["postId"] == "p2"


class TestWhatTheScreenSees:
    def test_the_payload_never_carries_the_code(self, broker):
        request = broker.request(user_id="u1", post_id="p1", channel="threads", prompt="코드")
        broker.submit("u1", "SECRET99")
        # 코드는 자동화로만 간다. 화면으로 돌아가는 값에 섞이면 안 된다.
        assert "SECRET99" not in str(request.as_dict())

    def test_the_payload_states_which_attempt_this_is(self, broker):
        request = broker.request(
            user_id="u1", post_id="p1", channel="threads", prompt="다시", attempt=2
        )
        payload = request.as_dict()
        assert payload["attempt"] == 2
        assert payload["maxAttempts"] == MAX_ATTEMPTS
