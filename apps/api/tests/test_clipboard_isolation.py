"""클립보드 격리(작업 A): 잠금 + 붙여넣기 직전 대조.

OS 클립보드는 기기 전체에 하나다. 같은 서버에서 발행 여러 건이 동시에 돌면 쓰기→Ctrl+V
사이에 서로를 덮어써 **다른 사람의 글이 붙는다**. 여기서 검증하는 것은 두 겹의 방어다:

1. ``use_os_clipboard()`` — 프로세스 안의 겹침을 잠금으로 직렬화한다.
2. ``clipboard_still_holds()`` — 잠금 밖(사용자 복사·화면 캡처·원격 데스크톱)이 끼어든
   것을 붙여넣기 직전 바이트 대조로 잡고, 어긋나면 붙여넣지 않는다(fail-closed).

실제 Windows 클립보드를 만지는 테스트는 없다 — 시스템 클립보드는 테스트가 건드리면
사용자 내용이 지워지고, 사용자가 복사하는 순간 테스트가 흔들린다(우리가 고치는 바로 그
경합이다). 저수준 읽기/지문만 대역으로 바꿔 규칙을 검증한다.

기본 모드가 synthetic으로 바뀌었으므로(2026-08-19) 이 파일은 **클립보드 모드를
명시하고** 돈다 — 여기서 지키는 규칙은 clipboard·auto(전환 후) 경로의 규칙이다.
"""

import hashlib
import threading
import time

import pytest

from app.posting.naver import clipboard as clipboard_module
from app.posting.naver.clipboard import (
    ClipboardOverwrittenError,
    clipboard_still_holds,
    use_os_clipboard,
)
from app.posting.naver.editor import SmartEditorOne


@pytest.fixture(autouse=True)
def clipboard_mode(monkeypatch):
    monkeypatch.setenv("NAVER_PASTE_MODE", "clipboard")


def _fingerprint(data: bytes) -> tuple[str, int]:
    return (hashlib.sha256(data).hexdigest(), len(data))


class TestClipboardStillHolds:
    def test_nothing_recorded_means_nothing_to_verify(self, monkeypatch):
        """OS 클립보드에 쓴 적이 없으면(브라우저 폴백 경로) 대조할 대상이 없다 — 통과."""
        monkeypatch.setattr(clipboard_module, "_last_os_write", None)
        assert clipboard_still_holds() is True

    def test_matching_bytes_pass(self, monkeypatch):
        written = "제목 A".encode("utf-16-le") + b"\x00\x00"
        monkeypatch.setattr(clipboard_module, "_last_os_write", {13: _fingerprint(written)})
        monkeypatch.setattr(clipboard_module, "_windows_clipboard_read", lambda _f: written)
        assert clipboard_still_holds() is True

    def test_different_bytes_fail(self, monkeypatch):
        """다른 발행·사용자 복사가 덮어쓴 상태 — 이게 잡혀야 남의 글이 붙지 않는다."""
        written = "제목 A".encode("utf-16-le") + b"\x00\x00"
        replaced = "다른 사람 글".encode("utf-16-le") + b"\x00\x00"
        monkeypatch.setattr(clipboard_module, "_last_os_write", {13: _fingerprint(written)})
        monkeypatch.setattr(clipboard_module, "_windows_clipboard_read", lambda _f: replaced)
        assert clipboard_still_holds() is False

    def test_a_missing_format_fails(self, monkeypatch):
        """캡처(이미지만 남음) 등으로 우리 포맷이 사라졌다 — 실패다."""
        written = b"payload"
        monkeypatch.setattr(clipboard_module, "_last_os_write", {13: _fingerprint(written)})
        monkeypatch.setattr(clipboard_module, "_windows_clipboard_read", lambda _f: None)
        assert clipboard_still_holds() is False

    def test_a_shorter_readback_fails(self, monkeypatch):
        written = b"0123456789"
        monkeypatch.setattr(clipboard_module, "_last_os_write", {13: _fingerprint(written)})
        monkeypatch.setattr(clipboard_module, "_windows_clipboard_read", lambda _f: b"0123")
        assert clipboard_still_holds() is False

    def test_alloc_slack_after_our_bytes_is_tolerated(self, monkeypatch):
        """GlobalSize는 요청보다 큰 할당을 돌려줄 수 있다 — 앞부분만 대조한다."""
        written = b"0123456789"
        monkeypatch.setattr(clipboard_module, "_last_os_write", {13: _fingerprint(written)})
        monkeypatch.setattr(
            clipboard_module, "_windows_clipboard_read", lambda _f: written + b"\x00\x00\x00"
        )
        assert clipboard_still_holds() is True

    def test_every_written_format_is_checked(self, monkeypatch):
        """CF_HTML만 사라지고 평문이 남아도 실패다 — 서식 없는 붙여넣기도 잘못된 발행이다."""
        text, html = b"plain", b"<b>rich</b>"
        store = {13: text}  # HTML 포맷(49xxx)은 사라졌다
        monkeypatch.setattr(
            clipboard_module,
            "_last_os_write",
            {13: _fingerprint(text), 49999: _fingerprint(html)},
        )
        monkeypatch.setattr(
            clipboard_module, "_windows_clipboard_read", lambda fmt: store.get(fmt)
        )
        assert clipboard_still_holds() is False


class TestUseOsClipboardLock:
    def test_two_threads_never_overlap(self):
        """쓰기→붙여넣기 구간이 스레드 사이에 겹치면 남의 내용이 붙는다 — 상호 배제 확인."""
        inside = 0
        overlapped = False
        lock_check = threading.Lock()

        def worker():
            nonlocal inside, overlapped
            for _ in range(20):
                with use_os_clipboard():
                    with lock_check:
                        inside += 1
                        if inside > 1:
                            overlapped = True
                    time.sleep(0.001)
                    with lock_check:
                        inside -= 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert overlapped is False

    def test_the_lock_is_reentrant_for_writes_inside_a_session(self, monkeypatch):
        """잠금 구간 안에서 부르는 쓰기 함수도 같은 잠금을 잡는다 — 재진입이어야 한다."""
        monkeypatch.setattr(clipboard_module.platform, "system", lambda: "NotWindows")
        with use_os_clipboard():
            # 비-Windows 분기라 실제 클립보드는 건드리지 않지만 잠금 획득 경로는 지나간다.
            assert clipboard_module._windows_clipboard_write({13: b"x"}) is False


class _FakeChain:
    """ActionChains 대역 — 어떤 조합이든 받고 perform만 기록한다."""

    def __init__(self, ops: list):
        self._ops = ops

    def __getattr__(self, name):
        def record(*_args, **_kwargs):
            self._ops.append(name)
            return self

        return record


class TestPasteVerified:
    def _editor(self, monkeypatch, holds: list[bool]):
        """clipboard_still_holds가 holds 순서대로 답하는 에디터. 붙여넣기 횟수를 센다."""
        editor = SmartEditorOne(None)
        pastes: list[str] = []
        monkeypatch.setattr(editor, "_paste_from_clipboard", lambda: pastes.append("v"))
        answers = list(holds)
        monkeypatch.setattr(
            "app.posting.naver.editor.clipboard_still_holds",
            lambda: answers.pop(0) if answers else True,
        )
        return editor, pastes

    def test_a_clean_clipboard_is_pasted(self, monkeypatch):
        editor, pastes = self._editor(monkeypatch, holds=[True])
        editor._paste_verified("제목")
        assert pastes == ["v"]

    def test_an_overwritten_clipboard_is_never_pasted(self, monkeypatch):
        """대조가 어긋나면 Ctrl+V를 보내지 않는다 — 남의 내용을 붙이는 것보다 실패가 낫다."""
        editor, pastes = self._editor(monkeypatch, holds=[False])
        with pytest.raises(ClipboardOverwrittenError):
            editor._paste_verified("제목")
        assert pastes == []


class TestPasteTitleUnderInterference:
    def _editor(self, monkeypatch, holds: list[bool], title: str = "제목 하나"):
        editor = SmartEditorOne(None)
        state = {"pasted": False, "clipboard_writes": 0}

        def put_text(_text):
            state["clipboard_writes"] += 1
            return True

        monkeypatch.setattr("app.posting.naver.editor._os_clipboard_text", put_text)
        monkeypatch.setattr(editor, "_focus_editor_target", lambda *_a: None)
        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains",
            lambda _d: _FakeChain([]),
        )

        def paste():
            state["pasted"] = True

        monkeypatch.setattr(editor, "_paste_from_clipboard", paste)
        monkeypatch.setattr(editor, "_title_text", lambda: title if state["pasted"] else "")
        answers = list(holds)
        monkeypatch.setattr(
            "app.posting.naver.editor.clipboard_still_holds",
            lambda: answers.pop(0) if answers else True,
        )
        return editor, state

    def test_interference_is_retried_with_a_fresh_clipboard(self, monkeypatch):
        """1차에서 끼어듦이 잡히면 클립보드에 **다시 넣고** 재시도한다 — 그대로 다시
        붙이면 끼어든 내용이 붙는다."""
        editor, state = self._editor(monkeypatch, holds=[False, True])
        editor._paste_title("제목 하나")
        assert state["clipboard_writes"] == 2
        assert state["pasted"] is True

    def test_persistent_interference_stops_the_publish(self, monkeypatch):
        """매번 끼어들면 발행을 중단한다 — 붙여넣기는 한 번도 하지 않는다."""
        editor, state = self._editor(monkeypatch, holds=[False, False, False, False, False])
        with pytest.raises(RuntimeError):
            editor._paste_title("제목 하나")
        assert state["pasted"] is False


class TestReplaceAnchorUnderInterference:
    def _editor(self, monkeypatch, holds: list[bool]):
        from app.posting.naver.plan import NaverImageAnchor

        editor = SmartEditorOne(None)
        state = {"pasted": 0, "image_writes": 0}
        monkeypatch.setattr(editor, "_image_component_count", lambda: 0)
        monkeypatch.setattr(editor, "_select_anchor_token", lambda _t: None)

        def put_image(_bytes):
            state["image_writes"] += 1
            return True

        monkeypatch.setattr("app.posting.naver.editor._os_clipboard_image", put_image)

        def paste():
            state["pasted"] += 1

        monkeypatch.setattr(editor, "_paste_from_clipboard", paste)
        monkeypatch.setattr(
            editor,
            "_anchor_status",
            lambda _t: {
                "tokenPresent": state["pasted"] == 0,
                "imageCount": 1 if state["pasted"] else 0,
                "inlineImageCount": 0,
            },
        )
        answers = list(holds)
        monkeypatch.setattr(
            "app.posting.naver.editor.clipboard_still_holds",
            lambda: answers.pop(0) if answers else True,
        )
        anchor = NaverImageAnchor(
            index=0,
            token="[[IMG-1]]",
            image_bytes=b"png-bytes",
            alt_text="",
            caption=None,
            expected_previous_text="",
            expected_next_text="",
        )
        return editor, state, anchor

    def test_interference_rewrites_the_image_and_retries(self, monkeypatch):
        """이미지도 시도마다 클립보드에 다시 넣는다 — 한 번 넣고 여러 번 붙이면
        시도 사이의 틈에 다른 내용이 들어온다."""
        editor, state, anchor = self._editor(monkeypatch, holds=[False, True])
        editor._replace_anchor_with_image(anchor)
        assert state["image_writes"] == 2
        assert state["pasted"] == 1

    def test_persistent_interference_aborts_without_pasting(self, monkeypatch):
        editor, state, anchor = self._editor(monkeypatch, holds=[False] * 10)
        with pytest.raises(RuntimeError) as caught:
            editor._replace_anchor_with_image(anchor)
        assert "클립보드" in str(caught.value)
        assert state["pasted"] == 0
