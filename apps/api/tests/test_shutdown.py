"""정리가 끝난 뒤에도 안 죽는 프로세스를 끝내는 것.

실측에서 출발했다(2026-08-06). 터미널에서 백엔드를 끄면 **37초 동안 멈춘 것처럼**
보였다(네 번 재서 37.4 / 37.6 / 37.4 / 38.0초). motor의 워커 스레드가 데몬이 아니라
인터프리터가 종료할 때 join하는데, 소켓에 걸린 읽기 하나가 소켓 타임아웃 20초 ×
재시도 2회를 다 채우기 때문이다.

`client.close()`도(37.6초) `_EXECUTOR.shutdown(cancel_futures=True)`도(37.5초) 소용이
없었다 — 둘 다 **이미 시작된 읽기**는 건드리지 못한다.
"""

import threading
import time

from app.shutdown import GRACE_SECONDS, force_exit_after


class TestWaitingOnlyAsLongAsItIsWorth:
    def test_it_ends_the_process_when_the_wait_is_pointless(self):
        """여기까지 왔다는 것은 DB·연결 풀·잡을 다 정리했다는 뜻이다.

        남은 것은 아무도 결과를 쓰지 않을 읽기 하나뿐이라 기다릴 이유가 없다.
        """
        ended = threading.Event()

        force_exit_after(0.05, exit_now=ended.set)

        assert ended.wait(timeout=2), "유예가 지났는데도 끝내지 않았다"

    def test_it_does_nothing_while_the_grace_lasts(self):
        """정상 종료는 유예 안에 끝난다. 그때는 아무 일도 일어나면 안 된다."""
        ended = threading.Event()

        force_exit_after(5.0, exit_now=ended.set)
        time.sleep(0.1)

        assert not ended.is_set()

    def test_the_watchdog_does_not_keep_the_process_alive(self):
        """감시 스레드 자신이 데몬이 아니면, 이 코드가 바로 그 '안 죽는 이유'가 된다."""
        watchdog = force_exit_after(60.0, exit_now=lambda: None)

        assert watchdog.daemon

    def test_the_grace_is_shorter_than_the_wait_it_replaces(self):
        """유예가 소켓 타임아웃(20초)보다 길면 아무것도 줄이지 못한다."""
        assert GRACE_SECONDS < 20
