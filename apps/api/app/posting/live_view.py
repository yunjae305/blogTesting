"""발행 크롬 화면을 웹으로 중계하고 입력을 전달한다 (라이브 뷰).

브라우저는 서버 PC에서 돈다 — 사용자는 외부 PC의 웹 브라우저에서 그 화면을 봐야
로그인·2단계 인증·캡차를 처리할 수 있다(이 기능이 없어서 외부 PC에서 로그인이
불가능했다). Chrome DevTools Protocol(CDP)로 해결한다:

- ``Page.startScreencast`` — 크롬이 렌더러에서 JPEG 프레임을 밀어 준다. 창이 가려져
  있거나 원격 데스크톱이 잠겨 있어도 나온다(화면 캡처가 아니라 렌더러 캡처다).
- ``Input.dispatchMouseEvent`` / ``dispatchKeyEvent`` / ``insertText`` — 사용자의
  클릭·키보드를 크롬에 넣는다.

**WebDriver 세션은 건드리지 않는다.** Selenium 명령은 발행 스레드가 독점하고 있고
스레드 안전하지 않다. 그래서 중계는 **별도의 CDP 웹소켓**(브라우저 엔드포인트)으로
직접 붙는다 — 발행 자동화와 화면 중계가 서로를 모른 채 같은 크롬을 쓴다.

프레임은 **구독자가 있을 때만** 받는다(Page.startScreencast는 첫 구독에서 켜고 마지막
구독이 떠나면 끈다) — 아무도 안 보는 화면을 인코딩해 보내는 낭비를 막는다.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 화면 중계 채널 — 프로필이 다른 두 브라우저가 동시에 뜰 수 있어 채널로 가른다.
LIVE_CHANNELS = ("naver", "threads")

# 스크린캐스트 품질. 발행 화면은 글자 판독이 목적이라 해상도를 유지하고 품질을 낮춘다.
SCREENCAST_FORMAT = {"format": "jpeg", "quality": 55, "maxWidth": 1440, "maxHeight": 1440}

# 프레임이 없어도 이 주기마다 keepalive를 내보낸다 — nginx의 proxy_read_timeout(기본
# 60초)이 유휴 연결을 끊지 않게 한다.
STREAM_HEARTBEAT_SECONDS = 15.0

# 세션 하나를 동시에 보는 스트림 상한. SSE는 동기 제너레이터라 시청자마다 서버
# 스레드풀 토큰(anyio 기본 40개)을 거의 상시 점유한다 — 무제한이면 라이브 뷰가
# 자기 부하로 다른 요청까지 굶긴다. 같은 화면을 탭 두 개까지는 허용한다.
MAX_STREAM_SUBSCRIBERS = 2

# CDP 웹소켓 연결·명령 대기 한도.
_WS_CONNECT_TIMEOUT_SECONDS = 5.0
_WS_RECV_TIMEOUT_SECONDS = 30.0

# 특수 키 → (windowsVirtualKeyCode, code). 글자는 Input.insertText로 넣으므로
# 여기에는 글자가 아닌 키만 있다. 한글 입력도 insertText가 완성형으로 받는다.
_SPECIAL_KEYS: dict[str, tuple[int, str]] = {
    "Enter": (13, "Enter"),
    "Backspace": (8, "Backspace"),
    "Tab": (9, "Tab"),
    "Escape": (27, "Escape"),
    "Delete": (46, "Delete"),
    "ArrowLeft": (37, "ArrowLeft"),
    "ArrowRight": (39, "ArrowRight"),
    "ArrowUp": (38, "ArrowUp"),
    "ArrowDown": (40, "ArrowDown"),
    "Home": (36, "Home"),
    "End": (35, "End"),
    "PageUp": (33, "PageUp"),
    "PageDown": (34, "PageDown"),
}


class LiveViewError(RuntimeError):
    """입력 전달 실패 등 라이브 뷰 조작 오류. 메시지는 화면에 그대로 보여도 된다."""


@dataclass
class LiveFrame:
    seq: int
    image_base64: str
    width: int
    height: int


def _browser_ws_url(driver) -> str | None:
    """드라이버가 띄운 크롬의 **브라우저 레벨** CDP 웹소켓 주소.

    페이지 레벨이 아니라 브라우저 레벨에 붙는 이유: 자동화가 새 탭을 열면(글쓰기 버튼이
    새 창으로 열리는 경우) 페이지 웹소켓은 옛 탭에 남는다. 브라우저 레벨에서는 탭이
    생기고 사라지는 것을 이벤트로 받아 따라갈 수 있다.
    """
    try:
        address = (driver.capabilities.get("goog:chromeOptions") or {}).get(
            "debuggerAddress"
        )
    except Exception:
        return None
    if not address:
        return None
    try:
        from urllib.request import urlopen

        with urlopen(f"http://{address}/json/version", timeout=_WS_CONNECT_TIMEOUT_SECONDS) as answer:
            info = json.loads(answer.read().decode("utf-8"))
        return info.get("webSocketDebuggerUrl") or None
    except Exception as error:
        logger.warning("CDP 브라우저 엔드포인트 조회 실패: %s", error)
        return None


def _connect_ws(url: str):
    """websocket-client(동기)로 CDP 웹소켓에 붙는다.

    asyncio ``websockets``가 아니라 동기 클라이언트를 쓰는 이유: 이 코드는 발행과 같은
    **스레드 세계**에서 돌고, FastAPI 이벤트 루프에 태우면 발행 대기가 루프를 막는
    사고와 얽힌다. 세션마다 리더 스레드 하나가 전부인 단순한 모델을 지킨다.
    """
    import websocket  # websocket-client

    return websocket.create_connection(
        url,
        timeout=_WS_RECV_TIMEOUT_SECONDS,
        # 보내는 쪽이 여러 스레드다(리더 스레드의 ack + 요청 스레드의 입력 전달).
        enable_multithread=True,
        # Chrome 111+는 Origin 헤더가 붙은 CDP 웹소켓을 거부한다(403). websocket-client는
        # 기본으로 Origin을 붙이므로 빼야 연결된다 — 크롬에 --remote-allow-origins를
        # 여는 방법도 있지만, 디버그 포트를 웹페이지에 여는 셈이라 이쪽이 안전하다.
        # (실기기 검증 2026-08-18: 이 옵션 없이는 WebSocketBadStatusException.)
        suppress_origin=True,
    )


class LiveSession:
    """크롬 한 대의 화면 중계 세션. 리더 스레드 하나가 CDP 이벤트를 처리한다.

    ``connect``를 바꿔 끼울 수 있게 한 것은 테스트 때문이다 — 실제 크롬 없이 가짜
    웹소켓으로 프레임·타깃 이벤트를 흘려 규칙을 검증한다.
    """

    def __init__(
        self,
        user_id: str,
        channel: str,
        label: str,
        ws_url: str,
        connect: Callable[[str], Any] = _connect_ws,
        kind: str = "publish",
    ):
        self.user_id = user_id
        self.channel = channel
        self.label = label
        # login | publish | preview — 화면이 어느 자리에 보여줄지 가른다(로그인 카드는
        # login만, 발행·예약 탭은 publish만). 섞으면 로그인 중계가 발행 탭에 뜬다.
        self.kind = kind
        # 흐름(발행·로그인)이 끝나면 True. 브라우저는 확인용으로 열려 있어도 중계
        # 목록에서는 내린다 — 발행이 끝난 화면이 탭에 계속 떠 있으면 안 된다.
        self.idle = False
        self.started_at = time.time()
        self._ws_url = ws_url
        self._connect = connect
        self._ws = None
        self._send_lock = threading.Lock()
        self._ids = itertools.count(1)
        # 명령 결과를 기다리는 곳: id → ("attach", targetId) 같은 후속 처리 표식.
        self._pending: dict[int, tuple[str, str]] = {}

        self._condition = threading.Condition()
        self._frame: LiveFrame | None = None
        self._seq = 0
        self._subscribers = 0
        self._alive = False
        self._closed = False

        # 페이지 타깃 상태: targetId → sessionId(붙은 것만). 중계 대상은 하나다.
        # 이 상태(_page_sessions·_current_target·_streaming·attach 중 표시)는 리더
        # 스레드와 HTTP 스레드(구독 증감)가 함께 고친다 — _state_lock으로 지킨다.
        # 잠금 순서는 항상 _state_lock → _send_lock 한 방향이다(역전 없음 = 교착 없음).
        self._state_lock = threading.RLock()
        self._page_sessions: dict[str, str] = {}
        self._attaching: set[str] = set()
        self._current_target: str | None = None
        self._streaming = False

        self._thread = threading.Thread(
            target=self._run, name=f"live-view-{channel}", daemon=True
        )
        self._thread.start()

    # --- 상태 ---------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._alive and not self._closed

    def to_wire(self) -> dict:
        return {
            "channel": self.channel,
            "label": self.label,
            "kind": self.kind,
            "active": self.alive,
            "startedAt": self.started_at,
        }

    # --- 구독 (SSE가 부른다) --------------------------------------------------

    @property
    def subscriber_count(self) -> int:
        return self._subscribers

    def add_subscriber(self) -> None:
        with self._condition:
            self._subscribers += 1
        self._sync_streaming()

    def remove_subscriber(self) -> None:
        with self._condition:
            self._subscribers -= 1
        self._sync_streaming()

    def wait_frame(self, after_seq: int, timeout: float) -> LiveFrame | None:
        """``after_seq`` 다음 프레임이 올 때까지 기다린다. 시간 안에 없으면 None."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._frame is not None and self._frame.seq > after_seq:
                    return self._frame
                if self._closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    # --- 입력 (HTTP 라우트가 부른다) -------------------------------------------

    def dispatch_input(self, events: list[dict]) -> int:
        """사용자의 클릭·키보드를 크롬에 넣는다. 처리한 이벤트 수를 돌려준다.

        좌표는 0~1 정규화 값으로 받는다 — 화면의 <img> 크기와 실제 뷰포트 크기가
        달라서, 픽셀로 받으면 클라이언트가 뷰포트 크기를 알아야 한다. 서버가 마지막
        프레임의 크기로 환산한다.
        """
        if not self.alive:
            raise LiveViewError("화면 중계가 연결돼 있지 않습니다.")
        session_id = self._page_sessions.get(self._current_target or "")
        if not session_id:
            raise LiveViewError("중계할 탭이 아직 없습니다.")
        with self._condition:
            frame = self._frame
        if frame is None:
            raise LiveViewError("첫 화면이 오기 전에는 입력할 수 없습니다.")

        handled = 0
        for event in events:
            if self._dispatch_one(session_id, frame, event):
                handled += 1
        return handled

    def _dispatch_one(self, session_id: str, frame: LiveFrame, event: dict) -> bool:
        kind = event.get("type")
        modifiers = int(event.get("modifiers") or 0)
        if kind == "click":
            x = float(event.get("x") or 0) * frame.width
            y = float(event.get("y") or 0) * frame.height
            button = str(event.get("button") or "left")
            count = int(event.get("clickCount") or 1)
            for press in ("mousePressed", "mouseReleased"):
                self._send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": press,
                        "x": x,
                        "y": y,
                        "button": button,
                        "clickCount": count,
                        "modifiers": modifiers,
                    },
                    session_id=session_id,
                )
            return True
        if kind == "move":
            self._send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": float(event.get("x") or 0) * frame.width,
                    "y": float(event.get("y") or 0) * frame.height,
                    "modifiers": modifiers,
                },
                session_id=session_id,
            )
            return True
        if kind == "wheel":
            self._send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": float(event.get("x") or 0.5) * frame.width,
                    "y": float(event.get("y") or 0.5) * frame.height,
                    "deltaX": float(event.get("deltaX") or 0),
                    "deltaY": float(event.get("deltaY") or 0),
                    "modifiers": modifiers,
                },
                session_id=session_id,
            )
            return True
        if kind == "text":
            text = str(event.get("text") or "")
            if not text:
                return False
            self._send("Input.insertText", {"text": text}, session_id=session_id)
            return True
        if kind == "key":
            name = str(event.get("key") or "")
            special = _SPECIAL_KEYS.get(name)
            if special is None:
                # 글자 키는 text 이벤트로 오는 것이 규약이다 — 모르는 키는 버린다.
                return False
            code, dom_code = special
            down: dict = {
                "type": "rawKeyDown",
                "windowsVirtualKeyCode": code,
                "nativeVirtualKeyCode": code,
                "key": name,
                "code": dom_code,
                "modifiers": modifiers,
            }
            if name == "Enter":
                # keypress(\r)까지 줘야 폼 제출·줄바꿈이 실제로 일어난다.
                down["type"] = "keyDown"
                down["text"] = "\r"
            self._send("Input.dispatchKeyEvent", down, session_id=session_id)
            self._send(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "windowsVirtualKeyCode": code,
                    "nativeVirtualKeyCode": code,
                    "key": name,
                    "code": dom_code,
                    "modifiers": modifiers,
                },
                session_id=session_id,
            )
            return True
        return False

    # --- 종료 ---------------------------------------------------------------

    def close(self) -> None:
        """세션을 끝낸다. 브라우저는 건드리지 않는다 — 그건 발행 코드의 몫이다."""
        self._closed = True
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        with self._condition:
            self._condition.notify_all()

    # --- 내부: CDP 통신 -------------------------------------------------------

    def _send(self, method: str, params: dict | None = None, session_id: str | None = None) -> int:
        ws = self._ws
        if ws is None:
            raise LiveViewError("화면 중계가 연결돼 있지 않습니다.")
        message: dict = {"id": next(self._ids), "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        try:
            with self._send_lock:
                ws.send(json.dumps(message))
        except Exception as error:
            raise LiveViewError(f"크롬에 명령을 보내지 못했습니다: {type(error).__name__}") from error
        return message["id"]

    def _sync_streaming(self) -> None:
        """**지금** 구독자 수에 맞춰 스크린캐스트를 켜거나 끈다.

        결정(수 세기)과 전송(start/stop)을 한 잠금 구간에서 한다. 예전에는 '첫/마지막
        구독'이라는 호출 시점 판단을 잠금 밖에서 전송했는데, 화면 새로고침처럼 옛
        스트림의 구독 해지와 새 스트림의 구독이 다른 스레드에서 겹치면 stop이 start
        뒤에 도착해 **구독자가 있는데 스크린캐스트가 꺼진 채** 고정될 수 있었다.
        매 호출이 현재 수를 다시 읽으므로 어떤 순서로 겹쳐도 마지막 호출이 맞춘다.
        """
        with self._state_lock:
            wanted = self._subscribers > 0
            session_id = self._page_sessions.get(self._current_target or "")
            if not session_id or wanted == self._streaming:
                return
            try:
                if wanted:
                    self._send(
                        "Page.startScreencast", dict(SCREENCAST_FORMAT), session_id=session_id
                    )
                else:
                    self._send("Page.stopScreencast", session_id=session_id)
                self._streaming = wanted
            except LiveViewError as error:
                logger.warning(
                    "스크린캐스트 %s 실패: %s", "시작" if wanted else "중지", error
                )

    def _run(self) -> None:
        try:
            self._ws = self._connect(self._ws_url)
        except Exception as error:
            logger.warning(
                "라이브 뷰 연결 실패(발행은 계속됩니다): %s", type(error).__name__
            )
            self._closed = True
            with self._condition:
                self._condition.notify_all()
            return
        self._alive = True
        try:
            # 새 탭이 생기고 사라지는 것을 이벤트로 받는다(글쓰기가 새 창으로 열리는 경우).
            self._send("Target.setDiscoverTargets", {"discover": True})
            attach_id = self._send("Target.getTargets")
            self._pending[attach_id] = ("targets", "")
            while not self._closed:
                try:
                    raw = self._ws.recv()
                except Exception as error:
                    # 구독자가 없으면 몇 분씩 조용할 수 있다 — recv 타임아웃은 끊김이
                    # 아니라 '조용함'이므로 계속 기다린다. (이름으로 가르는 이유:
                    # websocket 모듈은 지연 임포트라 타입을 직접 참조할 수 없다.)
                    if type(error).__name__ == "WebSocketTimeoutException":
                        continue
                    raise
                if raw is None:
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if not raw:
                    continue
                self._handle_message(json.loads(raw))
        except Exception as error:
            if not self._closed:
                logger.info("라이브 뷰 웹소켓 종료: %s", type(error).__name__)
        finally:
            self._closed = True
            self._alive = False
            with self._condition:
                self._condition.notify_all()
            try:
                if self._ws is not None:
                    self._ws.close()
            except Exception:
                pass

    def _handle_message(self, message: dict) -> None:
        if "id" in message:
            # 오류 응답을 조용히 버리면 화면이 왜 멈췄는지 서버 로그에 아무 단서가
            # 없다 — 명령이 무엇이었든 오류는 남긴다.
            error = message.get("error")
            if error:
                logger.warning(
                    "CDP 명령 오류(id=%s): %s", message.get("id"), error.get("message")
                )
            tag = self._pending.pop(message["id"], None)
            if tag is None:
                return
            kind, target_id = tag
            if error:
                with self._state_lock:
                    self._attaching.discard(target_id)
                return
            result = message.get("result") or {}
            if kind == "targets":
                pages = [
                    info
                    for info in result.get("targetInfos", [])
                    if info.get("type") == "page"
                ]
                for info in pages:
                    self._attach(info["targetId"])
            elif kind == "attach":
                session_id = result.get("sessionId")
                with self._state_lock:
                    self._attaching.discard(target_id)
                    if session_id:
                        self._page_sessions[target_id] = session_id
                if session_id:
                    self._switch_to(target_id)
            return

        method = message.get("method")
        params = message.get("params") or {}
        if method == "Page.screencastFrame":
            self._on_frame(message.get("sessionId"), params)
        elif method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            if info.get("type") == "page":
                self._attach(info["targetId"])
        elif method in ("Target.targetDestroyed", "Target.detachedFromTarget"):
            # detachedFromTarget: 크롬이 세션을 강제로 뗀 경우다 — 죽은 sessionId를
            # 들고 있으면 이후 모든 명령이 조용히 실패한다.
            target_id = params.get("targetId")
            with self._state_lock:
                self._page_sessions.pop(target_id, None)
                was_current = target_id == self._current_target
                if was_current:
                    self._current_target = None
                remaining = list(reversed(list(self._page_sessions))) if was_current else []
            if was_current:
                # 남아 있는 다른 탭으로 넘어간다(마지막에 붙은 것부터).
                for candidate in remaining:
                    self._switch_to(candidate)
                    break

    def _attach(self, target_id: str) -> None:
        # 같은 타깃에 attach를 두 번 보내면 안 된다. setDiscoverTargets는 켜는 순간
        # 기존 타깃에도 targetCreated를 재생하는데, 그 직후의 getTargets 결과와 겹쳐
        # 같은 탭에 flat 세션이 2개 붙는다 — 첫 세션은 추적에서 밀려나 스크린캐스트를
        # 끌 방법이 없는 고아가 된다. 붙었거나 붙는 중이면 건너뛴다.
        with self._state_lock:
            if target_id in self._page_sessions or target_id in self._attaching:
                return
            self._attaching.add(target_id)
        try:
            attach_id = self._send(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )
        except LiveViewError:
            with self._state_lock:
                self._attaching.discard(target_id)
            return
        self._pending[attach_id] = ("attach", target_id)

    def _switch_to(self, target_id: str) -> None:
        """중계 대상 탭을 바꾼다. 구독자가 보고 있으면 새 탭에서 스크린캐스트를 이어간다."""
        with self._state_lock:
            previous = self._page_sessions.get(self._current_target or "")
            self._current_target = target_id
            session_id = self._page_sessions.get(target_id)
            if not session_id:
                return
            try:
                self._send("Page.enable", session_id=session_id)
                if previous and self._streaming:
                    try:
                        self._send("Page.stopScreencast", session_id=previous)
                    except LiveViewError:
                        pass
                # 새 탭 기준에서는 아직 꺼져 있다 — 켤지는 아래 _sync가 구독자 수로 정한다.
                self._streaming = False
            except LiveViewError as error:
                logger.warning("중계 탭 전환 실패: %s", error)
                return
        self._sync_streaming()

    def _on_frame(self, session_id: str | None, params: dict) -> None:
        # 프레임마다 ack를 보내야 다음 프레임이 온다. 어느 세션 것이든 ack는 한다 —
        # 안 하면 그쪽 스트림이 멈춘 채 크롬에 쌓인다.
        ack = params.get("sessionId")
        if ack is not None and session_id:
            try:
                self._send(
                    "Page.screencastFrameAck", {"sessionId": ack}, session_id=session_id
                )
            except LiveViewError:
                pass
        # 지금 중계 중인 탭의 프레임만 싣는다. 탭 전환 직후 소켓에 남아 있던 이전
        # 탭 프레임이 새 탭 화면 사이에 끼어들면 화면이 두 페이지 사이에서 깜빡인다.
        with self._state_lock:
            current_session = self._page_sessions.get(self._current_target or "")
        if session_id and current_session and session_id != current_session:
            return
        data = params.get("data")
        if not data:
            return
        metadata = params.get("metadata") or {}
        with self._condition:
            self._seq += 1
            self._frame = LiveFrame(
                seq=self._seq,
                image_base64=data,
                width=int(metadata.get("deviceWidth") or 0) or 1280,
                height=int(metadata.get("deviceHeight") or 0) or 720,
            )
            self._condition.notify_all()


class LiveViewHub:
    """(사용자, 채널) → 세션. 발행·로그인 코드가 브라우저를 열 때 등록한다."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str], LiveSession] = {}
        self._by_driver: dict[int, LiveSession] = {}

    def register(
        self, user_id: str, channel: str, driver, label: str, kind: str = "publish"
    ) -> LiveSession | None:
        """이 크롬을 중계 대상으로 올린다. 실패해도 발행을 막지 않는다(None)."""
        if not user_id or channel not in LIVE_CHANNELS:
            return None
        ws_url = _browser_ws_url(driver)
        if not ws_url:
            logger.info("CDP 주소를 얻지 못해 라이브 뷰 없이 진행합니다.")
            return None
        try:
            session = LiveSession(user_id, channel, label, ws_url, kind=kind)
        except Exception as error:
            logger.warning("라이브 뷰 세션 생성 실패(발행은 계속됩니다): %s", error)
            return None
        with self._lock:
            old = self._sessions.get((user_id, channel))
            self._sessions[(user_id, channel)] = session
            self._by_driver[id(driver)] = session
        if old is not None:
            old.close()
        logger.info("라이브 뷰 등록: %s (%s)", channel, label)
        return session

    def mark_idle(self, driver) -> None:
        """발행·로그인 **흐름이 끝났다** — 창은 열려 있어도 중계 목록에서 내린다.

        세션 수명은 브라우저 수명(닫힐 때 unregister)인데, 확인용으로 열어 둔 창이
        최대 15분을 살아서 발행 탭들에 '끝난 발행/로그인 화면'이 계속 떠 있었다
        (2026-08-18 사용자 지적). 목록에서만 내리고 세션은 남긴다 — 이미 보고 있던
        스트림은 화면이 닫힐 때까지 이어지고, 리퍼의 시청 중 보류도 그대로 동작한다.
        """
        with self._lock:
            session = self._by_driver.get(id(driver))
        if session is not None:
            session.idle = True

    def unregister_driver(self, driver) -> None:
        """브라우저를 닫는 쪽(close_browser)이 부른다."""
        with self._lock:
            session = self._by_driver.pop(id(driver), None)
            if session is not None and self._sessions.get(
                (session.user_id, session.channel)
            ) is session:
                del self._sessions[(session.user_id, session.channel)]
        if session is not None:
            session.close()

    def get(self, user_id: str, channel: str) -> LiveSession | None:
        with self._lock:
            session = self._sessions.get((user_id, channel))
        if session is not None and not session.alive and session._closed:
            # 죽은 세션은 목록에서 걷어낸다(브라우저가 크래시로 사라진 경우).
            with self._lock:
                if self._sessions.get((user_id, channel)) is session:
                    del self._sessions[(user_id, channel)]
            return None
        return session

    def list_for_user(self, user_id: str) -> list[dict]:
        found = []
        for channel in LIVE_CHANNELS:
            session = self.get(user_id, channel)
            if session is not None and not session.idle:
                found.append(session.to_wire())
        return found

    def has_watchers(self, driver) -> bool:
        """이 브라우저를 지금 누가 보고 있는가 — 정리(작업 D)가 닫기 전에 물어본다."""
        with self._lock:
            session = self._by_driver.get(id(driver))
        return session is not None and session.alive and session._subscribers > 0


def frame_stream(session: LiveSession):
    """SSE 본문 제너레이터. StreamingResponse가 스레드풀에서 돌린다(블로킹 OK).

    구독을 세션에 등록하는 것이 스크린캐스트를 켜는 스위치다 — 마지막 구독자가 떠나면
    꺼진다. 클라이언트가 끊으면 Starlette가 제너레이터를 닫아 finally가 돈다.
    """
    session.add_subscriber()
    try:
        yield f"event: status\ndata: {json.dumps(session.to_wire())}\n\n"
        last = 0
        while True:
            frame = session.wait_frame(last, timeout=STREAM_HEARTBEAT_SECONDS)
            if frame is None:
                if not session.alive:
                    yield "event: closed\ndata: {}\n\n"
                    return
                # 프레임이 없어도 연결을 살려 둔다(nginx 유휴 타임아웃 방지).
                yield ": keepalive\n\n"
                continue
            last = frame.seq
            payload = json.dumps(
                {
                    "seq": frame.seq,
                    "image": frame.image_base64,
                    "width": frame.width,
                    "height": frame.height,
                }
            )
            yield f"event: frame\ndata: {payload}\n\n"
    finally:
        session.remove_subscriber()


#: 프로세스 전역 허브 — 발행 코드와 HTTP 라우트가 같은 것을 본다.
hub = LiveViewHub()
