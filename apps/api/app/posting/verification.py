"""발행 자동화가 사람에게 인증코드를 물어보는 창구.

## 왜 필요한가

스레드(인스타그램 계정) 로그인은 아이디·비밀번호까지 자동으로 넣을 수 있지만, Meta가
2단계 인증을 걸어 두면 거기서 멈춘다. 예전에는 **열린 Chrome 창에서 사람이 직접** 코드를
넣기를 180초 기다렸고, 시간이 지나면 `NEEDS_HUMAN`으로 끝났다. 그래서 사용자는 자동화가
띄운 브라우저를 직접 만져야 했고, 예약 발행처럼 사람이 없는 상황에서는 아예 불가능했다.

이제는 앱 화면이 코드를 받아 넘긴다: 자동화가 2단계 인증 화면을 만나면 여기에 요청을
등록하고 **기다린다**. 화면은 그 요청을 폴링해 입력창을 띄우고, 사용자가 넣은 코드를
여기에 넣어 준다. 자동화는 깨어나 코드를 대신 입력하고 하던 일을 계속한다.

## 왜 메모리인가

기다리는 쪽은 Selenium이 도는 **워커 스레드**이고, 코드를 넣는 쪽은 asyncio 이벤트
루프에서 도는 **HTTP 핸들러**다. 둘은 같은 프로세스 안에 있으므로 `threading.Event`
하나로 충분하다. DB에 둘 이유도 없다 — 요청은 브라우저가 열려 있는 동안만 유효하고,
프로세스가 죽으면 그 브라우저도 함께 죽어서 어차피 무효다.

여러 프로세스로 늘리면 이 창구는 Redis 같은 공유 저장소로 옮겨야 한다.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 코드를 기다리는 최대 시간. 사용자가 문자를 확인하고 앱에 옮겨 적는 시간이다 —
# 짧으면 멀쩡한 로그인이 실패로 끝나고, 길면 브라우저와 워커가 그만큼 붙들려 있다.
DEFAULT_WAIT_SECONDS = 180.0

# 코드를 잘못 넣었을 때 다시 물어보는 횟수(첫 시도 포함).
MAX_ATTEMPTS = 3


@dataclass
class VerificationRequest:
    """'이 사용자에게 코드를 받아야 한다'는 사실 하나.

    ``prompt``는 화면에 그대로 보여 줄 안내다. 어느 채널이 왜 막혔는지 사용자가 알아야
    무엇을 찾아 넣을지 안다.
    """

    user_id: str
    post_id: str
    channel: str
    prompt: str
    attempt: int
    created_at: float
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _code: str | None = field(default=None, repr=False)
    _cancelled: bool = field(default=False, repr=False)

    def as_dict(self) -> dict:
        """화면에 내보내는 형태. 코드 자체는 절대 돌려주지 않는다."""
        return {
            "postId": self.post_id,
            "channel": self.channel,
            "prompt": self.prompt,
            "attempt": self.attempt,
            "maxAttempts": MAX_ATTEMPTS,
            "waitingSeconds": round(time.monotonic() - self.created_at, 1),
        }


class VerificationBroker:
    """사용자 한 명당 대기 요청 하나. 동시에 두 발행이 돌면 나중 것이 앞의 것을 취소한다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, VerificationRequest] = {}

    # --- 자동화 쪽 (Selenium 워커 스레드) ---------------------------------

    def request(
        self, *, user_id: str, post_id: str, channel: str, prompt: str, attempt: int = 1
    ) -> VerificationRequest:
        request = VerificationRequest(
            user_id=user_id,
            post_id=post_id,
            channel=channel,
            prompt=prompt,
            attempt=attempt,
            created_at=time.monotonic(),
        )
        with self._lock:
            previous = self._pending.get(user_id)
            if previous is not None:
                # 앞선 대기를 그대로 두면 화면이 옛 요청에 코드를 넣는다.
                previous._cancelled = True
                previous._event.set()
            self._pending[user_id] = request
        logger.info(
            "인증코드 요청 | user=%s post=%s channel=%s (%d/%d회차)",
            user_id,
            post_id,
            channel,
            attempt,
            MAX_ATTEMPTS,
        )
        return request

    def wait(
        self, request: VerificationRequest, timeout: float = DEFAULT_WAIT_SECONDS
    ) -> str | None:
        """코드가 들어오면 그 값을, 시간이 지나거나 취소되면 None을 돌려준다.

        **워커 스레드에서만 부른다** — 이벤트 루프에서 부르면 서버 전체가 멈춘다.
        """
        got = request._event.wait(timeout)
        with self._lock:
            if self._pending.get(request.user_id) is request:
                self._pending.pop(request.user_id, None)
        if not got:
            logger.info("인증코드 대기 시간 초과 | user=%s", request.user_id)
            return None
        if request._cancelled:
            logger.info("인증코드 대기 취소됨 | user=%s", request.user_id)
            return None
        return request._code

    # --- 화면 쪽 (HTTP 핸들러) --------------------------------------------

    def pending(self, user_id: str) -> VerificationRequest | None:
        with self._lock:
            return self._pending.get(user_id)

    def submit(self, user_id: str, code: str) -> bool:
        """코드를 기다리던 자동화를 깨운다. 기다리는 것이 없으면 False."""
        with self._lock:
            request = self._pending.get(user_id)
            if request is None:
                return False
            request._code = code
            request._event.set()
        logger.info("인증코드 입력됨 | user=%s post=%s", user_id, request.post_id)
        return True

    def cancel(self, user_id: str) -> bool:
        """사용자가 창을 닫았다. 자동화는 None을 받고 실패로 끝난다."""
        with self._lock:
            request = self._pending.get(user_id)
            if request is None:
                return False
            request._cancelled = True
            request._event.set()
        logger.info("인증코드 대기 취소 | user=%s", user_id)
        return True


# 프로세스 하나에 하나. 발행 자동화와 HTTP 라우트가 같은 것을 본다.
broker = VerificationBroker()
