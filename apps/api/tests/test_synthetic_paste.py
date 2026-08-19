"""합성 붙여넣기(NAVER_PASTE_MODE) — 클립보드를 안 만지는 발행 경로.

2026-08-19 전환: **기본 모드가 synthetic이다.** 핵심 계약:

- synthetic(기본)에서는 **OS 클립보드 함수가 한 번도 불리지 않고 잠금도 없다** —
  발행끼리 완전히 독립이라 여러 사용자가 동시에 발행해도 내용이 섞일 자원 자체가 없다.
- 에디터가 이벤트를 거부하면(fail-closed): 제목은 신뢰 키 입력으로, 이미지는 업로드
  자동화로 폴백하고, 본문 거부는 명확한 오류로 멈춘다.
- ``auto``는 거부 시 **그 발행 인스턴스만** 클립보드 경로로 갈아탄다.
- ``clipboard``는 기존 경로(잠금+대조)다 — test_clipboard_isolation.py가 검증한다.
"""

from contextlib import contextmanager

import pytest

from app.posting.naver.editor import (
    SmartEditorOne,
    SyntheticImagePasteError,
    SyntheticPasteError,
    _image_mime,
    paste_mode,
)
from app.posting.naver.plan import NaverImageAnchor


@pytest.fixture
def synthetic(monkeypatch):
    monkeypatch.setenv("NAVER_PASTE_MODE", "synthetic")


def _anchor(image_bytes: bytes = b"\x89PNG rest") -> NaverImageAnchor:
    return NaverImageAnchor(
        index=0,
        token="[[IMG-1]]",
        image_bytes=image_bytes,
        alt_text="",
        caption=None,
        expected_previous_text="",
        expected_next_text="",
    )


class _ScriptDriver:
    """execute_script로 들어온 합성 붙여넣기 호출을 기록하는 드라이버 대역."""

    def __init__(self, consumed: bool = True):
        self.calls: list[tuple] = []
        self.consumed = consumed

    def execute_script(self, script, *args):
        self.calls.append(args)
        return {"dispatched": True, "consumed": self.consumed, "targetTag": "DIV"}


class _KeysChain:
    """ActionChains 대역 — 호출 이름과 send_keys 인자를 기록한다."""

    def __init__(self, log: list):
        self._log = log

    def __getattr__(self, name):
        def record(*args, **_kwargs):
            self._log.append((name, args))
            return self

        return record


class TestModeSwitch:
    def test_default_mode_is_auto(self, monkeypatch):
        """플래그가 없으면 auto다 — 합성으로 **시작**하되(클립보드를 만지지 않는다)
        에디터가 거부하면 클립보드로 갈아타 발행이 죽지 않는다. 스마트에디터가 합성
        paste를 받지 않는 것이 2026-08-19 실발행에서 확인됐다."""
        monkeypatch.delenv("NAVER_PASTE_MODE", raising=False)
        assert paste_mode() == "auto"
        editor = SmartEditorOne(None)
        assert editor._synthetic_active() is True

        def forbidden(*_args):
            raise AssertionError("첫 시도에서 OS 클립보드를 만졌습니다")

        monkeypatch.setattr("app.posting.naver.editor._os_clipboard_text", forbidden)
        editor._put_text_on_clipboard("제목", "제목")
        assert editor._pending_paste == {"text": "제목"}

    def test_the_default_falls_back_instead_of_dying(self, monkeypatch):
        """기본값의 핵심 계약: 에디터가 거부해도 발행이 멈추지 않는다(클립보드로 전환)."""
        monkeypatch.delenv("NAVER_PASTE_MODE", raising=False)
        editor = SmartEditorOne(_ScriptDriver(consumed=False))
        editor._pending_paste = {"text": "제목"}
        with pytest.raises(SyntheticPasteError):
            editor._paste_verified("제목")
        assert editor._synthetic_active() is False

    def test_clipboard_mode_still_works_as_a_fallback(self, monkeypatch):
        """NAVER_PASTE_MODE=clipboard면 기존 경로다 — 클립보드에 쓴다."""
        monkeypatch.setenv("NAVER_PASTE_MODE", "clipboard")
        editor = SmartEditorOne(None)
        wrote: list[str] = []
        monkeypatch.setattr(
            "app.posting.naver.editor._os_clipboard_text",
            lambda text: wrote.append(text) or True,
        )
        editor._put_text_on_clipboard("제목", "제목")
        assert wrote == ["제목"]

    def test_an_unknown_mode_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("NAVER_PASTE_MODE", "banana")
        assert paste_mode() == "auto"

    def test_strict_synthetic_is_still_available(self, monkeypatch):
        """엄격 모드는 남아 있다 — 거부돼도 클립보드로 갈아타지 않는다."""
        monkeypatch.setenv("NAVER_PASTE_MODE", "synthetic")
        assert paste_mode() == "synthetic"

    def test_synthetic_mode_never_touches_the_clipboard(self, synthetic, monkeypatch):
        editor = SmartEditorOne(_ScriptDriver())

        def forbidden(*_args):
            raise AssertionError("합성 모드에서 OS 클립보드를 만졌습니다")

        monkeypatch.setattr("app.posting.naver.editor._os_clipboard_text", forbidden)
        monkeypatch.setattr("app.posting.naver.editor._os_clipboard_html", forbidden)
        editor._put_text_on_clipboard("제목", "제목")
        editor._paste_verified("제목")
        editor._put_html_on_clipboard("<p>본문</p>", "본문")
        editor._paste_verified("본문 스캐폴드")
        editor._clear_clipboard()

    def test_synthetic_mode_holds_no_global_lock(self, synthetic, monkeypatch):
        """합성 모드의 붙여넣기 구간은 전역 클립보드 잠금을 잡지 않는다 — 동시 발행이
        직렬화되지 않는 것이 이 전환의 목적이다."""

        def forbidden():
            raise AssertionError("합성 모드에서 클립보드 잠금을 잡았습니다")

        monkeypatch.setattr("app.posting.naver.editor.use_os_clipboard", forbidden)
        editor = SmartEditorOne(None)
        with editor._paste_guard():
            pass


class TestAutoFallback:
    def test_a_rejected_paste_switches_this_publish_to_clipboard(self, monkeypatch):
        monkeypatch.setenv("NAVER_PASTE_MODE", "auto")
        editor = SmartEditorOne(_ScriptDriver(consumed=False))
        editor._pending_paste = {"text": "제목"}
        with pytest.raises(SyntheticPasteError):
            editor._paste_verified("제목")
        assert editor._synthetic_active() is False
        # 전환 뒤의 담기는 클립보드로 간다 — 재시도가 클립보드 경로로 이어진다.
        wrote: list[str] = []
        monkeypatch.setattr(
            "app.posting.naver.editor._os_clipboard_text",
            lambda text: wrote.append(text) or True,
        )
        editor._put_text_on_clipboard("제목", "제목")
        assert wrote == ["제목"]

    def test_after_the_fallback_the_guard_takes_the_lock(self, monkeypatch):
        """클립보드로 갈아탄 발행은 작업 A의 규칙(전역 잠금)으로 돌아간다."""
        monkeypatch.setenv("NAVER_PASTE_MODE", "auto")
        editor = SmartEditorOne(_ScriptDriver(consumed=False))
        editor._pending_paste = {"text": "제목"}
        with pytest.raises(SyntheticPasteError):
            editor._paste_verified("제목")

        acquired: list[bool] = []

        @contextmanager
        def lock():
            acquired.append(True)
            yield

        monkeypatch.setattr("app.posting.naver.editor.use_os_clipboard", lock)
        with editor._paste_guard():
            pass
        assert acquired == [True]

    def test_pure_synthetic_does_not_switch(self, synthetic):
        editor = SmartEditorOne(_ScriptDriver(consumed=False))
        editor._pending_paste = {"text": "제목"}
        with pytest.raises(SyntheticPasteError):
            editor._paste_verified("제목")
        assert editor._synthetic_active() is True

    def test_the_switch_is_per_instance(self, monkeypatch):
        """auto 폴백은 그 발행 인스턴스만 갈아탄다 — 동시에 도는 다른 발행은 계속
        synthetic이다(전역 상태를 늘리지 않는다)."""
        monkeypatch.setenv("NAVER_PASTE_MODE", "auto")
        rejected = SmartEditorOne(_ScriptDriver(consumed=False))
        other = SmartEditorOne(_ScriptDriver())
        rejected._pending_paste = {"text": "제목"}
        with pytest.raises(SyntheticPasteError):
            rejected._paste_verified("제목")
        assert rejected._synthetic_active() is False
        assert other._synthetic_active() is True


class TestSyntheticDispatch:
    def test_text_and_html_are_sent_as_a_paste_event(self, synthetic):
        driver = _ScriptDriver()
        editor = SmartEditorOne(driver)
        editor._put_html_on_clipboard("<b>굵게</b>", "굵게")
        editor._paste_verified("본문 스캐폴드")
        (html, text, image, mime), = driver.calls
        assert html == "<b>굵게</b>"
        assert text == "굵게"
        assert image is None and mime is None

    def test_an_image_goes_as_a_file(self, synthetic):
        driver = _ScriptDriver()
        editor = SmartEditorOne(driver)
        editor._pending_paste = {"image_bytes": b"\x89PNG rest"}
        editor._paste_verified("1번째 이미지")
        (html, text, image, mime), = driver.calls
        assert html is None and text is None
        assert image  # base64
        assert mime == "image/png"

    def test_a_rejected_event_raises_a_synthetic_paste_error(self, synthetic):
        """에디터가 이벤트를 소비하지 않으면(신뢰 검사 등) 그 자리에서 멈춘다 —
        시간 초과를 기다리며 조용히 빈 글을 만들지 않는다."""
        editor = SmartEditorOne(_ScriptDriver(consumed=False))
        editor._pending_paste = {"text": "제목"}
        with pytest.raises(SyntheticPasteError) as caught:
            editor._paste_verified("제목")
        assert "받지 않았습니다" in str(caught.value)

    def test_pasting_without_preparing_is_a_bug(self, synthetic):
        editor = SmartEditorOne(_ScriptDriver())
        with pytest.raises(RuntimeError):
            editor._paste_verified("제목")

    def test_the_payload_is_consumed_once(self, synthetic):
        """같은 내용이 두 번 붙지 않도록, 쏜 뒤에는 비운다."""
        driver = _ScriptDriver()
        editor = SmartEditorOne(driver)
        editor._pending_paste = {"text": "한 번만"}
        editor._paste_verified("제목")
        with pytest.raises(RuntimeError):
            editor._paste_verified("제목")
        assert len(driver.calls) == 1


class TestTitleTypingFallback:
    def test_a_rejected_title_paste_is_typed_instead(self, synthetic, monkeypatch):
        """제목은 평문이라, 합성 이벤트가 거부되면 신뢰 키 입력으로 그 자리에서
        대체한다 — 반영 검증(제목 대조)은 그대로 지나야 한다."""
        driver = _ScriptDriver(consumed=False)
        editor = SmartEditorOne(driver)
        state = {"typed": False}
        chain_log: list[tuple] = []

        class _TitleChain(_KeysChain):
            def send_keys(self, *keys):
                self._log.append(("send_keys", keys))
                if keys == ("제목 하나",):
                    state["typed"] = True
                return self

        monkeypatch.setattr(editor, "_focus_editor_target", lambda *_a: None)
        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains",
            lambda _d: _TitleChain(chain_log),
        )
        monkeypatch.setattr(
            editor, "_title_text", lambda: "제목 하나" if state["typed"] else ""
        )
        editor._paste_title("제목 하나")
        assert state["typed"] is True
        assert ("send_keys", ("제목 하나",)) in chain_log

    def test_in_auto_mode_the_title_retries_on_the_clipboard_path(self, monkeypatch):
        """auto에서는 제목 거부가 발행을 클립보드로 갈아태우고, 재시도가 그 경로로
        제목을 다시 붙인다 — 키 입력 폴백이 아니라 모드 전환이 auto의 계약이다."""
        monkeypatch.setenv("NAVER_PASTE_MODE", "auto")
        driver = _ScriptDriver(consumed=False)
        editor = SmartEditorOne(driver)
        state = {"pasted": False}
        wrote: list[str] = []
        monkeypatch.setattr(editor, "_focus_editor_target", lambda *_a: None)
        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains",
            lambda _d: _KeysChain([]),
        )
        monkeypatch.setattr(
            "app.posting.naver.editor._os_clipboard_text",
            lambda text: wrote.append(text) or True,
        )
        monkeypatch.setattr("app.posting.naver.editor.clipboard_still_holds", lambda: True)

        def paste():
            state["pasted"] = True

        monkeypatch.setattr(editor, "_paste_from_clipboard", paste)
        monkeypatch.setattr(
            editor, "_title_text", lambda: "제목 하나" if state["pasted"] else ""
        )
        editor._paste_title("제목 하나")
        assert state["pasted"] is True
        assert wrote == ["제목 하나"]


class TestUploadFallback:
    def test_a_rejected_image_paste_falls_back_to_upload(self, synthetic, monkeypatch):
        editor = SmartEditorOne(_ScriptDriver(consumed=False))
        monkeypatch.setattr(editor, "_image_component_count", lambda: 0)
        monkeypatch.setattr(editor, "_select_anchor_token", lambda _t: None)
        monkeypatch.setattr(editor, "_wait_for_images_settled", lambda: None)
        called: dict = {}

        def fake_fallback(anchor, count_before, replaced):
            called["index"] = anchor.index
            called["count_before"] = count_before
            return True

        monkeypatch.setattr(editor, "_upload_image_fallback", fake_fallback)
        editor._replace_anchor_with_image(_anchor())
        assert called == {"index": 0, "count_before": 0}

    def test_when_the_upload_also_fails_the_publish_stops(self, synthetic, monkeypatch):
        editor = SmartEditorOne(_ScriptDriver(consumed=False))
        monkeypatch.setattr(editor, "_image_component_count", lambda: 0)
        monkeypatch.setattr(editor, "_select_anchor_token", lambda _t: None)
        monkeypatch.setattr(editor, "_wait_for_images_settled", lambda: None)
        monkeypatch.setattr(editor, "_upload_image_fallback", lambda *_a: False)
        with pytest.raises(SyntheticImagePasteError) as caught:
            editor._replace_anchor_with_image(_anchor())
        assert "업로드" in str(caught.value)

    def test_without_a_file_input_nothing_is_touched(self, synthetic, monkeypatch):
        """파일 입력이 없으면 문서를 건드리지 않고 물러난다 — 토큰이 남아 있어야
        중단 뒤에도 화면에서 무엇이 실패했는지 보인다."""
        editor = SmartEditorOne(_ScriptDriver())
        monkeypatch.setattr(editor, "_find_image_file_input", lambda: None)

        def forbidden(_driver):
            raise AssertionError("파일 입력이 없는데 문서를 건드렸습니다")

        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains", forbidden
        )
        assert editor._upload_image_fallback(_anchor(), 0, lambda _d: True) is False

    def test_the_bytes_are_attached_as_a_temp_file(self, synthetic, monkeypatch):
        """업로드 폴백은 이미지 바이트를 임시 파일로 내려 input[type=file]에 붙인다.
        MIME에 맞는 확장자를 쓴다 — 확장자로 형식을 거르는 업로더도 있다."""
        editor = SmartEditorOne(_ScriptDriver())
        attached: list[str] = []

        class _Input:
            def send_keys(self, path):
                attached.append(path)

        monkeypatch.setattr(editor, "_find_image_file_input", lambda: _Input())
        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains",
            lambda _d: _KeysChain([]),
        )
        assert editor._upload_image_fallback(_anchor(), 0, lambda _d: True) is True
        assert len(attached) == 1
        assert attached[0].endswith(".png")


class TestImageMime:
    def test_known_headers(self):
        assert _image_mime(b"\x89PNG....") == "image/png"
        assert _image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
        assert _image_mime(b"GIF89a") == "image/gif"
        assert _image_mime(b"RIFF1234WEBPVP8 ") == "image/webp"

    def test_unknown_bytes_default_to_png(self):
        assert _image_mime(b"????") == "image/png"
