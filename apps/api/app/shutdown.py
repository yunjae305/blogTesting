"""정리가 끝난 뒤에도 안 죽는 프로세스를 끝낸다.

무슨 문제였나
-------------
터미널에서 백엔드를 끄면 **37초 동안 멈춘 것처럼 보였다**(2026-08-06 실측, 같은 조건에서
네 번 재서 37.4 / 37.6 / 37.4 / 38.0초).

원인은 이렇다. motor는 pymongo를 **전역 ThreadPoolExecutor**에서 돌리는데
(`motor/frameworks/asyncio.py`의 `_EXECUTOR`), 그 워커 스레드는 데몬이 아니다. 파이썬은
인터프리터를 끝낼 때 데몬이 아닌 스레드를 전부 join한다. 그래서 읽기 하나가 소켓에
걸려 있으면 그것이 끝날 때까지 프로세스가 안 죽는다.

    소켓 타임아웃 20초 × 재시도 2회 ≈ 37초

이 회선(0.09MB/s)에서는 큰 읽기가 흔히 20초를 넘긴다.

왜 다른 방법으로는 안 되나 (전부 실측했다)
-------------------------------------------
- `client.close()` — **소용없다**(37.6초). pymongo는 쓰이지 않는 소켓만 닫는다. 이미
  읽는 중인 소켓은 건드리지 않는다.
- `_EXECUTOR.shutdown(wait=False, cancel_futures=True)` — **소용없다**(37.5초). 아직
  시작 안 한 작업만 취소한다. 진행 중인 읽기는 그대로다.
- 소켓 타임아웃을 줄이기 — **하면 안 된다.** 지금도 글 하나를 여는 데 최대 10.9초가
  걸린다. 20초는 그것을 담는 값이다.

그래서 무엇을 하나
------------------
정리가 다 끝난 뒤에 짧게 기다렸다가, 그래도 안 죽으면 프로세스를 끝낸다.

여기까지 왔다는 것은 **DB·연결 풀·백그라운드 잡을 모두 정리했다는 뜻**이다. 남은 것은
아무도 결과를 쓰지 않을 읽기 하나뿐이라 기다릴 이유가 없다. 정상적으로 끝나는 경우에는
이 감시 스레드가 데몬이라 프로세스와 함께 조용히 사라진다 — 아무 일도 하지 않는다.
"""

import os
import sys
import threading
import time
from collections.abc import Callable

#: 정리가 끝난 뒤 이만큼 기다려 본다. 정상 종료는 이 안에 끝난다.
#:
#: 이 시간이 지나도 살아 있다면 남은 것은 소켓에 걸린 읽기뿐이고, 그것은 최소 20초를
#: 더 기다려야 끝난다 — 기다릴 값이 아니다.
GRACE_SECONDS = 3.0


def _flush_and_exit() -> None:
    # os._exit는 버퍼를 비우지 않는다. 종료 로그가 잘려 나가면 왜 껐는지 알 수 없다.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def force_exit_after(
    grace_seconds: float = GRACE_SECONDS,
    exit_now: Callable[[], None] = _flush_and_exit,
) -> threading.Thread:
    """``grace_seconds`` 뒤에도 프로세스가 살아 있으면 끝낸다.

    데몬 스레드로 돈다 — 정상적으로 끝나면 이 스레드도 함께 사라지므로 아무 일도
    일어나지 않는다.
    """

    def wait_then_exit() -> None:
        time.sleep(grace_seconds)
        print(
            f"정리가 끝났는데 {grace_seconds:.0f}초가 지나도 프로세스가 살아 있어 종료합니다"
            " (Mongo 읽기가 소켓에 걸려 있습니다).",
            flush=True,
        )
        exit_now()

    watchdog = threading.Thread(target=wait_then_exit, name="shutdown-watchdog", daemon=True)
    watchdog.start()
    return watchdog
