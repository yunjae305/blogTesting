"""라이브 뷰(화면 중계) — CDP 프로토콜 규칙을 가짜 웹소켓으로 검증한다.

실제 크롬을 띄우는 테스트는 없다(네이버 발행 테스트와 같은 이유 — 크롬·화면이 필요한
것은 스위트에 두지 않는다). 여기서 지키려는 규칙:

- 스크린캐스트는 **구독자가 있을 때만** 켠다(아무도 안 보는 화면을 인코딩하지 않는다).
- 프레임마다 ack를 보낸다(안 보내면 크롬이 다음 프레임을 안 준다).
- 입력 좌표는 0~1 정규화 값을 마지막 프레임 크기로 환산한다.
- 새 탭이 생기면 따라가고, 중계 탭이 닫히면 남은 탭으로 넘어간다.
"""

import json
import queue
import time

import pytest

from app.posting import live_view as live_view_module
from app.posting.live_view import (
    LiveSession,
    LiveViewError,
    LiveViewHub,
    frame_stream,
)

_CLOSED = object()


class FakeWs:
    """CDP 브라우저 엔드포인트 대역. 명령에 자동 응답하고, 테스트가 이벤트를 밀어 넣는다."""

    def __init__(self):
        self.sent: list[dict] = []
        self._incoming: "queue.Queue" = queue.Queue()
        self.page_targets = ["page-1"]
        self._session_ids = {"page-1": "session-1"}

    # --- websocket-client 인터페이스 ---

    def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        method = message.get("method")
        if method == "Target.setDiscoverTargets":
            # 실제 크롬은 discover를 켜는 순간 **기존 타깃 전부**에 targetCreated를
            # 재생한다 — 곧이어 오는 getTargets 결과와 겹쳐 같은 탭이 두 번 보인다.
            for target in self.page_targets:
                self.push(
                    {
                        "method": "Target.targetCreated",
                        "params": {"targetInfo": {"targetId": target, "type": "page"}},
                    }
                )
        elif method == "Target.getTargets":
            self.push(
                {
                    "id": message["id"],
                    "result": {
                        "targetInfos": [
                            {"targetId": target, "type": "page"}
                            for target in self.page_targets
                        ]
                    },
                }
            )
        elif method == "Target.attachToTarget":
            target = message["params"]["targetId"]
            self.push(
                {
                    "id": message["id"],
                    "result": {"sessionId": self._session_ids.setdefault(target, f"session-{target}")},
                }
            )

    def recv(self) -> str:
        item = self._incoming.get(timeout=5)
        if item is _CLOSED:
            raise ConnectionError("closed")
        return item

    def close(self) -> None:
        self._incoming.put(_CLOSED)

    # --- 테스트 도우미 ---

    def push(self, obj: dict) -> None:
        self._incoming.put(json.dumps(obj))

    def push_frame(self, session_id="session-1", ack=7, width=1000, height=500) -> None:
        self.push(
            {
                "method": "Page.screencastFrame",
                "sessionId": session_id,
                "params": {
                    "data": "aGVsbG8=",
                    "sessionId": ack,
                    "metadata": {"deviceWidth": width, "deviceHeight": height},
                },
            }
        )

    def sent_methods(self) -> list[str]:
        return [message.get("method") for message in self.sent]

    def wait_for(self, method: str, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in self.sent:
                if message.get("method") == method:
                    return message
            time.sleep(0.01)
        raise AssertionError(f"{method}이(가) 전송되지 않았습니다: {self.sent_methods()}")


def _session(ws: FakeWs) -> LiveSession:
    return LiveSession("user-1", "naver", "네이버 발행", "ws://fake", connect=lambda _u: ws)


def _wait_attached(ws: FakeWs, session: LiveSession, target="page-1") -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if session._page_sessions.get(target):
            return
        time.sleep(0.01)
    raise AssertionError("타깃이 붙지 않았습니다")


class TestScreencastGating:
    def test_no_screencast_until_someone_watches(self):
        """아무도 안 보는 화면은 인코딩하지 않는다 — 구독 전에는 startScreencast가 없다."""
        ws = FakeWs()
        session = _session(ws)
        _wait_attached(ws, session)
        assert "Page.startScreencast" not in ws.sent_methods()
        session.close()

    def test_first_subscriber_starts_and_last_stops(self):
        ws = FakeWs()
        session = _session(ws)
        _wait_attached(ws, session)
        session.add_subscriber()
        ws.wait_for("Page.startScreencast")
        session.add_subscriber()
        assert ws.sent_methods().count("Page.startScreencast") == 1
        session.remove_subscriber()
        session.remove_subscriber()
        ws.wait_for("Page.stopScreencast")
        session.close()

    def test_every_frame_is_acked(self):
        """ack를 빼먹으면 크롬이 다음 프레임을 주지 않는다."""
        ws = FakeWs()
        session = _session(ws)
        _wait_attached(ws, session)
        session.add_subscriber()
        ws.push_frame(ack=42)
        frame = session.wait_frame(0, timeout=3)
        assert frame is not None and frame.image_base64 == "aGVsbG8="
        ack = ws.wait_for("Page.screencastFrameAck")
        assert ack["params"] == {"sessionId": 42}
        session.close()


class TestInput:
    def _ready(self):
        ws = FakeWs()
        session = _session(ws)
        _wait_attached(ws, session)
        session.add_subscriber()
        ws.push_frame(width=1000, height=500)
        assert session.wait_frame(0, timeout=3) is not None
        return ws, session

    def test_click_is_scaled_to_the_frame_size(self):
        """좌표는 0~1 정규화로 받아 서버가 뷰포트 픽셀로 환산한다."""
        ws, session = self._ready()
        handled = session.dispatch_input([{"type": "click", "x": 0.5, "y": 0.2}])
        assert handled == 1
        press = ws.wait_for("Input.dispatchMouseEvent")
        assert press["params"]["x"] == 500.0
        assert press["params"]["y"] == 100.0
        mouse_events = [m for m in ws.sent if m.get("method") == "Input.dispatchMouseEvent"]
        assert [m["params"]["type"] for m in mouse_events] == [
            "mousePressed",
            "mouseReleased",
        ]
        session.close()

    def test_text_goes_through_insert_text(self):
        """한글은 키 이벤트로 조립할 수 없다 — 완성된 글자를 insertText로 넣는다."""
        ws, session = self._ready()
        session.dispatch_input([{"type": "text", "text": "인증코드123"}])
        message = ws.wait_for("Input.insertText")
        assert message["params"] == {"text": "인증코드123"}
        session.close()

    def test_an_unknown_key_is_dropped(self):
        ws, session = self._ready()
        assert session.dispatch_input([{"type": "key", "key": "F13"}]) == 0
        session.close()

    def test_input_before_the_first_frame_is_refused(self):
        """프레임 크기를 모르면 좌표를 환산할 수 없다 — 조용히 엉뚱한 곳을 누르지 않는다."""
        ws = FakeWs()
        session = _session(ws)
        _wait_attached(ws, session)
        with pytest.raises(LiveViewError):
            session.dispatch_input([{"type": "click", "x": 0.5, "y": 0.5}])
        session.close()


class TestAttachDedupe:
    def test_a_replayed_target_is_attached_only_once(self):
        """setDiscoverTargets의 targetCreated 재생 + getTargets 결과가 같은 탭을 두 번
        보여줘도 attach는 한 번이다 — 두 번 붙으면 첫 세션이 끌 수 없는 고아가 된다."""
        ws = FakeWs()
        session = _session(ws)
        _wait_attached(ws, session)
        time.sleep(0.1)  # 늦게 도착하는 중복 attach가 없는지 잠깐 더 지켜본다.
        attaches = [m for m in ws.sent if m.get("method") == "Target.attachToTarget"]
        assert len(attaches) == 1
        session.close()


class TestFrameSource:
    def test_a_stale_tabs_frame_is_acked_but_not_shown(self):
        """탭 전환 직후 소켓에 남은 이전 탭 프레임이 새 화면 사이에 끼면 화면이 두
        페이지 사이에서 깜빡인다 — ack만 하고 싣지 않는다."""
        ws = FakeWs()
        ws.page_targets = ["page-1", "page-2"]
        ws._session_ids = {"page-1": "session-1", "page-2": "session-2"}
        session = _session(ws)
        _wait_attached(ws, session, target="page-1")
        _wait_attached(ws, session, target="page-2")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and session._current_target != "page-2":
            time.sleep(0.01)
        session.add_subscriber()
        ws.push_frame(session_id="session-1", ack=99)  # 이전 탭의 늦은 프레임
        assert session.wait_frame(0, timeout=0.3) is None
        ack = ws.wait_for("Page.screencastFrameAck")
        assert ack["params"] == {"sessionId": 99}
        session.close()


class TestTargetFollowing:
    def test_a_new_tab_takes_over_the_stream(self):
        """글쓰기가 새 탭으로 열리면 중계도 그 탭으로 넘어가야 한다."""
        ws = FakeWs()
        session = _session(ws)
        _wait_attached(ws, session)
        session.add_subscriber()
        ws.wait_for("Page.startScreencast")
        ws.push(
            {
                "method": "Target.targetCreated",
                "params": {"targetInfo": {"targetId": "page-2", "type": "page"}},
            }
        )
        _wait_attached(ws, session, target="page-2")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and session._current_target != "page-2":
            time.sleep(0.01)
        assert session._current_target == "page-2"
        # 새 탭에서도 스크린캐스트가 이어진다.
        starts = [
            m for m in ws.sent if m.get("method") == "Page.startScreencast"
        ]
        assert any(m.get("sessionId") == "session-page-2" for m in starts)
        session.close()

    def test_a_closed_tab_falls_back_to_the_remaining_one(self):
        ws = FakeWs()
        ws.page_targets = ["page-1", "page-2"]
        ws._session_ids = {"page-1": "session-1", "page-2": "session-2"}
        session = _session(ws)
        _wait_attached(ws, session, target="page-1")
        _wait_attached(ws, session, target="page-2")
        ws.push({"method": "Target.targetDestroyed", "params": {"targetId": session._current_target}})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and session._current_target is None:
            time.sleep(0.01)
        assert session._current_target is not None
        session.close()


class TestFrameStream:
    def test_the_stream_subscribes_and_unsubscribes(self):
        ws = FakeWs()
        session = _session(ws)
        _wait_attached(ws, session)
        stream = frame_stream(session)
        first = next(stream)
        assert first.startswith("event: status")
        ws.wait_for("Page.startScreencast")
        ws.push_frame()
        second = next(stream)
        assert second.startswith("event: frame")
        payload = json.loads(second.split("data: ", 1)[1])
        assert payload["width"] == 1000
        stream.close()
        ws.wait_for("Page.stopScreencast")
        session.close()


class TestHub:
    class _StubSession:
        def __init__(self, user_id, channel, label, ws_url, **_kwargs):
            self.user_id = user_id
            self.channel = channel
            self.label = label
            self.kind = _kwargs.get("kind", "publish")
            self.idle = False
            self.started_at = 0.0
            self._closed = False
            self._subscribers = 0

        @property
        def alive(self):
            return not self._closed

        def to_wire(self):
            return {
                "channel": self.channel,
                "label": self.label,
                "kind": self.kind,
                "active": self.alive,
                "startedAt": 0,
            }

        def close(self):
            self._closed = True

    def _hub(self, monkeypatch) -> LiveViewHub:
        monkeypatch.setattr(live_view_module, "_browser_ws_url", lambda _d: "ws://fake")
        monkeypatch.setattr(live_view_module, "LiveSession", self._StubSession)
        return LiveViewHub()

    def test_a_new_browser_replaces_the_old_session(self, monkeypatch):
        """같은 (사용자, 채널)에 새 크롬이 뜨면 옛 세션은 닫는다 — 유령 중계를 남기지 않는다."""
        hub = LiveViewHub()
        monkeypatch.setattr(live_view_module, "_browser_ws_url", lambda _d: "ws://fake")
        monkeypatch.setattr("app.posting.live_view.LiveSession", self._StubSession)
        driver_a, driver_b = object(), object()
        first = hub.register("user-1", "naver", driver_a, "발행")
        second = hub.register("user-1", "naver", driver_b, "발행")
        assert first._closed is True
        assert hub.get("user-1", "naver") is second

    def test_unregister_removes_and_closes(self, monkeypatch):
        hub = self._hub(monkeypatch)
        driver = object()
        session = hub.register("user-1", "naver", driver, "발행")
        hub.unregister_driver(driver)
        assert session._closed is True
        assert hub.get("user-1", "naver") is None
        assert hub.list_for_user("user-1") == []

    def test_an_unknown_channel_is_refused(self, monkeypatch):
        hub = self._hub(monkeypatch)
        assert hub.register("user-1", "tiktok", object(), "?") is None

    def test_no_ws_url_means_no_session(self, monkeypatch):
        """CDP 주소를 못 얻어도 발행은 계속돼야 한다 — None을 돌려줄 뿐 예외가 없다."""
        hub = LiveViewHub()
        monkeypatch.setattr(live_view_module, "_browser_ws_url", lambda _d: None)
        assert hub.register("user-1", "naver", object(), "발행") is None

    def test_a_finished_flow_leaves_the_list_but_keeps_the_session(self, monkeypatch):
        """발행·로그인이 끝나면 목록에서 내린다 — 확인용으로 열어 둔 창(최대 15분)의
        '끝난 화면'이 발행 탭들에 계속 떠 있으면 안 된다(2026-08-18 사용자 지적).
        세션 자체는 남는다: 보고 있던 스트림과 리퍼의 시청 보류는 이어져야 한다."""
        hub = self._hub(monkeypatch)
        driver = object()
        session = hub.register("user-1", "naver", driver, "발행")
        assert hub.list_for_user("user-1") != []
        hub.mark_idle(driver)
        assert hub.list_for_user("user-1") == []
        assert hub.get("user-1", "naver") is session  # 스트림·입력은 계속 닿는다

    def test_sessions_carry_their_kind(self, monkeypatch):
        """로그인 중계가 발행 탭에 뜨지 않도록 화면이 kind로 거른다 — 값이 실려야 한다."""
        hub = self._hub(monkeypatch)
        hub.register("user-1", "naver", object(), "네이버 로그인", kind="login")
        (wire,) = hub.list_for_user("user-1")
        assert wire["kind"] == "login"
