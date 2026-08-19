"""스레드 로그인이 2단계 인증을 만났을 때의 흐름.

예전에는 자동화가 띄운 Chrome 창을 사람이 직접 만져야 했고, 사람이 없으면 180초 뒤
NEEDS_HUMAN으로 끝났다. 이제는 앱 화면에 코드를 물어보고 대신 입력한다.

여기서 막는 것: 비밀번호 단계를 2단계 인증으로 오인해 있지도 않은 코드를 요구하는 것,
코드를 못 받았는데 통과한 척하는 것, 오답인데 다시 물어보지 않는 것.
"""

import threading

import pytest

from app.posting import threads_browser as tb
from app.posting.verification import MAX_ATTEMPTS, VerificationBroker


class FakeElement:
    def __init__(
        self,
        *,
        displayed: bool = True,
        enabled: bool = True,
        attrs: dict | None = None,
        children: dict | None = None,
        text: str = "",
    ):
        self._displayed = displayed
        self._enabled = enabled
        self._attrs = attrs or {}
        self._children = children or {}
        self.text = text
        self.typed: list[str] = []
        self.submitted = False

    def get_attribute(self, name: str):
        return self._attrs.get(name)

    # 모달 요소도 스코프가 되므로 자식 탐색을 지원한다.
    def find_elements(self, _by, selector: str):
        return self._children.get(selector, [])

    def is_displayed(self) -> bool:
        return self._displayed

    def is_enabled(self) -> bool:
        return self._enabled

    def send_keys(self, _keys) -> None:
        self.submitted = True


class FakeDriver:
    """CSS 선택자별로 미리 정한 요소를 돌려주는 최소 드라이버."""

    def __init__(self, elements: dict[str, list[FakeElement]], body_text: str = ""):
        self._elements = elements
        self._body = FakeElement(attrs={}, text=body_text)

    def find_elements(self, _by, selector: str):
        return self._elements.get(selector, [])

    # _says_verification이 body 글자를 읽는 경로.
    def find_element(self, _by, _selector):
        return self._body


@pytest.fixture
def publisher() -> tb.ThreadsBrowserPublisher:
    return tb.ThreadsBrowserPublisher()


class TestFindingTheVerificationField:
    def test_a_plain_login_form_is_not_a_verification_screen(self, publisher):
        """아이디·비밀번호만 있는 화면에서 코드를 물어보면 안 된다.

        사용자는 받은 적도 없는 코드를 요구받는다. 코드 칸임을 알리는 단서가 하나도 없을
        때 비밀번호 칸이 그 판단 근거다.
        """
        driver = FakeDriver(
            {
                "input[type='password']": [FakeElement(attrs={"type": "password"})],
                "input": [
                    FakeElement(attrs={"type": "text"}),
                    FakeElement(attrs={"type": "password"}),
                ],
            }
        )
        assert publisher._verification_code_field(driver) is None

    def test_an_explicit_code_field_wins_over_a_background_login_form(self, publisher):
        """실사용(2026-08-04): 2단계 인증 팝업이 떴는데 우리 팝업이 안 떴다.

        비밀번호 검사를 먼저 하면, 모달을 `[role='dialog']`로 못 찾은 화면에서 **눈앞에
        코드 칸이 있는데도** 뒤 배경의 비밀번호 칸 때문에 None이 나온다. 코드 칸임이
        분명한 단서가 있으면 그것이 이긴다.
        """
        code_field = FakeElement(attrs={"type": "text", "placeholder": "보안 코드"})
        driver = FakeDriver(
            {
                "input[type='password']": [FakeElement(attrs={"type": "password"})],
                "input[placeholder*='보안 코드']": [code_field],
            }
        )
        assert publisher._verification_code_field(driver) is code_field

    def test_a_one_time_code_input_is_the_verification_field(self, publisher):
        field = FakeElement()
        driver = FakeDriver({"input[autocomplete='one-time-code']": [field]})
        assert publisher._verification_code_field(driver) is field

    def test_a_hidden_input_is_not_the_field(self, publisher):
        driver = FakeDriver(
            {"input[autocomplete='one-time-code']": [FakeElement(displayed=False)]}
        )
        assert publisher._verification_code_field(driver) is None

    def test_no_matching_input_means_no_verification(self, publisher):
        assert publisher._verification_code_field(FakeDriver({})) is None


class TestSolvingTheVerification:
    """``_solve_verification``은 broker에서 코드를 받아 입력한다.

    브라우저 대신 FakeDriver를 쓰고, 코드 입력은 monkeypatch로 가로챈다 — 여기서 보려는
    것은 '코드를 못 받으면 어떻게 되는가'와 '오답이면 다시 묻는가'다.
    """

    def _patch(self, monkeypatch, publisher, broker, *, field, typed_ok=True):
        monkeypatch.setattr(tb, "VERIFICATION_SETTLE_SECONDS", 0.05)
        monkeypatch.setattr(tb, "VERIFICATION_WAIT_SECONDS", 2.0)
        import app.posting.verification as verification

        monkeypatch.setattr(verification, "broker", broker)
        monkeypatch.setattr(
            publisher, "_verification_code_field", lambda _driver: field, raising=False
        )
        monkeypatch.setattr(
            publisher, "_type_verification_code", lambda *_args: typed_ok, raising=False
        )
        monkeypatch.setattr(publisher, "_has_session", staticmethod(lambda _driver: False))

    def test_no_code_within_the_wait_gives_up(self, publisher, monkeypatch):
        broker = VerificationBroker()
        self._patch(monkeypatch, publisher, broker, field=FakeElement())
        monkeypatch.setattr(tb, "VERIFICATION_WAIT_SECONDS", 0.05)

        # 아무도 코드를 넣지 않는다 → False. 호출부가 NEEDS_HUMAN으로 끝낸다.
        assert publisher._solve_verification(None, user_id="u1", post_id="p1") is False

    def test_a_cancelled_request_gives_up_immediately(self, publisher, monkeypatch):
        broker = VerificationBroker()
        self._patch(monkeypatch, publisher, broker, field=FakeElement())

        done: dict = {}

        def run():
            done["result"] = publisher._solve_verification(None, user_id="u1", post_id="p1")

        thread = threading.Thread(target=run)
        thread.start()
        for _ in range(50):
            if broker.pending("u1") is not None:
                break
            threading.Event().wait(0.02)
        broker.cancel("u1")
        thread.join(5)
        assert done["result"] is False

    def test_the_field_disappearing_counts_as_passed(self, publisher, monkeypatch):
        """기다리는 사이 화면이 넘어갔으면 이미 통과한 것이다."""
        broker = VerificationBroker()
        self._patch(monkeypatch, publisher, broker, field=None)
        assert publisher._solve_verification(None, user_id="u1", post_id="p1") is True

    def test_a_wrong_code_is_asked_again_up_to_the_limit(self, publisher, monkeypatch):
        """코드가 통하지 않으면 화면에 다시 물어본다 — 한 번 틀렸다고 끝내지 않는다."""
        broker = VerificationBroker()
        self._patch(monkeypatch, publisher, broker, field=FakeElement())

        asked: list[int] = []
        original = broker.request

        def spy(**kwargs):
            asked.append(kwargs["attempt"])
            request = original(**kwargs)
            # 매번 코드를 넣어 준다. 세션은 계속 안 생기므로 오답 취급된다.
            broker.submit(kwargs["user_id"], "000000")
            return request

        monkeypatch.setattr(broker, "request", spy)

        assert publisher._solve_verification(None, user_id="u1", post_id="p1") is False
        assert asked == list(range(1, MAX_ATTEMPTS + 1))


class TestResendAndBackupCodes:
    """실사용(2026-08-18): 문자가 늦거나 아예 안 와서 2단계 인증에서 막혔다.

    화면이 ``__RESEND__``를 보내면 자동화가 '코드 재전송' 버튼을 대신 누르고(시도
    횟수를 소모하지 않는다), 8자리 코드가 오면 '백업 코드 사용' 화면으로 넘어가 넣는다.
    """

    def _patch(self, monkeypatch, publisher, broker, *, clicked, typed):
        monkeypatch.setattr(tb, "VERIFICATION_SETTLE_SECONDS", 0.05)
        monkeypatch.setattr(tb, "VERIFICATION_WAIT_SECONDS", 2.0)
        monkeypatch.setattr(tb, "HUMAN_PAUSE_SECONDS", 0.0)
        monkeypatch.setattr(tb, "BACKUP_SWITCH_SETTLE_SECONDS", 0.0)
        import app.posting.verification as verification

        monkeypatch.setattr(verification, "broker", broker)
        # 코드가 입력되면 인증 화면이 사라진다 — 통과 판정 경로.
        field_holder = {"field": FakeElement()}
        monkeypatch.setattr(
            publisher,
            "_verification_code_field",
            lambda _driver: field_holder["field"],
            raising=False,
        )

        def type_code(_driver, _field, code):
            typed.append(code)
            field_holder["field"] = None
            return True

        monkeypatch.setattr(publisher, "_type_verification_code", type_code, raising=False)
        monkeypatch.setattr(publisher, "_has_session", staticmethod(lambda _driver: False))
        monkeypatch.setattr(
            publisher,
            "_click_dialog_button",
            lambda _driver, *labels: clicked.append(labels) or True,
            raising=False,
        )
        monkeypatch.setattr(
            publisher,
            "_click_dialog_back",
            lambda _driver: clicked.append(("뒤로",)) or True,
            raising=False,
        )

    def _feed_codes(self, monkeypatch, broker, codes: list[str]) -> list[int]:
        """요청이 올 때마다 순서대로 코드를 넣어 주고, 물어본 attempt를 기록한다."""
        asked: list[int] = []
        original = broker.request
        remaining = list(codes)

        def spy(**kwargs):
            asked.append(kwargs["attempt"])
            request = original(**kwargs)
            broker.submit(kwargs["user_id"], remaining.pop(0))
            return request

        monkeypatch.setattr(broker, "request", spy)
        return asked

    def test_resend_presses_the_button_without_consuming_an_attempt(
        self, publisher, monkeypatch
    ):
        broker = VerificationBroker()
        clicked: list = []
        typed: list = []
        self._patch(monkeypatch, publisher, broker, clicked=clicked, typed=typed)
        asked = self._feed_codes(monkeypatch, broker, ["__RESEND__", "123456"])

        assert publisher._solve_verification(None, user_id="u1", post_id="p1") is True
        # 재전송 버튼이 눌렸고, 두 번째 요청도 여전히 1회차다 — 코드를 틀린 게 아니다.
        assert any("코드 재전송" in labels for labels in clicked)
        assert asked == [1, 1]
        assert typed == ["123456"]

    def test_an_8_digit_code_goes_through_the_backup_screen(self, publisher, monkeypatch):
        broker = VerificationBroker()
        clicked: list = []
        typed: list = []
        self._patch(monkeypatch, publisher, broker, clicked=clicked, typed=typed)
        self._feed_codes(monkeypatch, broker, ["12345678"])

        assert publisher._solve_verification(None, user_id="u1", post_id="p1") is True
        assert any("백업 코드 사용" in labels for labels in clicked)
        assert typed == ["12345678"]

    def test_the_backup_button_request_switches_without_consuming_an_attempt(
        self, publisher, monkeypatch
    ):
        """화면의 '백업 코드 사용' 버튼 — 사용자가 누르면 자동화가 크롬에서 대신 누른다."""
        broker = VerificationBroker()
        clicked: list = []
        typed: list = []
        self._patch(monkeypatch, publisher, broker, clicked=clicked, typed=typed)
        asked = self._feed_codes(monkeypatch, broker, ["__BACKUP__", "12345678"])

        assert publisher._solve_verification(None, user_id="u1", post_id="p1") is True
        assert any("백업 코드 사용" in labels for labels in clicked)
        assert asked == [1, 1]
        assert typed == ["12345678"]

    def test_the_back_arrow_request_returns_without_consuming_an_attempt(
        self, publisher, monkeypatch
    ):
        """뒤로(←) — 백업 코드를 쓰려다 다시 인증번호로 돌아가는 길(2026-08-18 요청)."""
        broker = VerificationBroker()
        clicked: list = []
        typed: list = []
        self._patch(monkeypatch, publisher, broker, clicked=clicked, typed=typed)
        asked = self._feed_codes(monkeypatch, broker, ["__BACK__", "123456"])

        assert publisher._solve_verification(None, user_id="u1", post_id="p1") is True
        assert ("뒤로",) in clicked
        assert asked == [1, 1]
        assert typed == ["123456"]

    def test_a_6_digit_code_never_touches_the_backup_button(self, publisher, monkeypatch):
        """문자 코드는 지금 화면에 그대로 넣는다 — 버튼을 잘못 누르면 화면이 바뀐다."""
        broker = VerificationBroker()
        clicked: list = []
        typed: list = []
        self._patch(monkeypatch, publisher, broker, clicked=clicked, typed=typed)
        self._feed_codes(monkeypatch, broker, ["123456"])

        assert publisher._solve_verification(None, user_id="u1", post_id="p1") is True
        assert clicked == []
        assert typed == ["123456"]


class TestWaitingForTheLoginForm:
    """실사용(2026-08-04): 아이디·비밀번호가 한 번도 입력되지 않았다.

    threads.com은 React 앱이라 `driver.get()`이 돌아온 시점에는 입력칸이 아직 DOM에 없다.
    예전 코드는 그 직후에 찾아서 늘 빈손이었고, 그대로 "직접 로그인해 주세요"로 넘어갔다.
    """

    def test_a_form_that_renders_late_is_still_found(self, publisher, monkeypatch):
        monkeypatch.setattr(tb, "LOGIN_FORM_TIMEOUT_SECONDS", 3.0)
        field = FakeElement()
        calls = {"n": 0}

        class LateDriver:
            def find_elements(self, _by, selector):
                # 처음 두 번은 비어 있다 — 아직 렌더 전이다.
                calls["n"] += 1
                if selector != "input[type='password']" or calls["n"] < 3:
                    return []
                return [field]

        assert publisher._wait_for_login_form(LateDriver()) is field
        assert calls["n"] >= 3  # 곧바로 포기하지 않았다

    def test_a_form_that_never_renders_times_out(self, publisher, monkeypatch):
        monkeypatch.setattr(tb, "LOGIN_FORM_TIMEOUT_SECONDS", 0.2)
        assert publisher._wait_for_login_form(FakeDriver({})) is None

    def test_a_hidden_password_field_does_not_count(self, publisher, monkeypatch):
        monkeypatch.setattr(tb, "LOGIN_FORM_TIMEOUT_SECONDS", 0.2)
        driver = FakeDriver({"input[type='password']": [FakeElement(displayed=False)]})
        assert publisher._wait_for_login_form(driver) is None


class TestFindingTheFieldByShape:
    """실사용(2026-08-04): 2단계 인증 화면이 떴는데 선택자 6개가 모두 빗나가 팝업이 안 떴다.

    이름표(name·id·autocomplete)는 Meta가 바꿀 때마다 흔들린다. 화면의 **모양**은 안정적이다 —
    비밀번호 칸은 지나갔고 남은 글자 입력칸은 코드 하나뿐이다.
    """

    def test_a_lone_unnamed_input_is_taken_as_the_code_field(self, publisher):
        # 아는 속성이 하나도 없는 칸이라도, 하나뿐이면 그것이 코드 칸이다.
        field = FakeElement(attrs={"placeholder": "보안 코드"})
        driver = FakeDriver({"input": [field]})
        assert publisher._verification_code_field(driver) is field

    def test_two_inputs_are_left_alone(self, publisher):
        """무엇이 코드 칸인지 알 수 없으면 손대지 않는다.

        엉뚱한 칸에 코드를 넣느니 사람이 처리하는 편이 낫다.
        """
        driver = FakeDriver({"input": [FakeElement(), FakeElement()]})
        assert publisher._verification_code_field(driver) is None

    def test_a_checkbox_is_not_a_code_field(self, publisher):
        driver = FakeDriver(
            {"input": [FakeElement(attrs={"type": "checkbox"}), FakeElement(attrs={"type": "text"})]}
        )
        # 체크박스는 후보가 아니므로 남은 text 하나가 코드 칸이 된다.
        assert publisher._verification_code_field(driver) is not None

    def test_the_login_form_is_never_mistaken_for_a_code_field(self, publisher):
        """비밀번호 칸이 보이면 아직 로그인 단계다 — 구조 탐색까지 가지 않는다."""
        driver = FakeDriver(
            {
                "input[type='password']": [FakeElement()],
                "input": [FakeElement()],
            }
        )
        assert publisher._verification_code_field(driver) is None

    def test_a_named_field_still_wins(self, publisher):
        """이름표로 찾히면 구조 탐색을 쓰지 않는다 — 확실한 쪽이 먼저다."""
        named = FakeElement()
        other = FakeElement()
        driver = FakeDriver(
            {"input[autocomplete='one-time-code']": [named], "input": [named, other]}
        )
        assert publisher._verification_code_field(driver) is named


class TestMissingCredentials:
    """실사용(2026-08-04): 자격증명이 없는데 자동화가 브라우저만 띄우고 3분을 기다렸다.

    로그는 INFO 한 줄뿐이라 "왜 자동 입력이 안 되지"로 헤맸다. 자동화가 할 수 있는 일이
    없다는 사실이 로그와 실패 문구에 분명히 드러나야 한다.
    """

    def _driver_stuck_on_login(self):
        class Stuck:
            current_url = "https://www.threads.com/login"

            def get(self, _url):
                return None

            def get_cookies(self):
                return []

            def find_elements(self, _by, selector):
                # 로그인 폼이 계속 떠 있다 — 세션이 안 생긴 상태.
                if selector == "input[type='password']":
                    return [FakeElement(attrs={"type": "password"})]
                return []

        return Stuck()

    def test_it_says_what_to_do_instead_of_waiting_silently(self, publisher, monkeypatch, caplog):
        import logging

        monkeypatch.setattr(tb, "LOGIN_TIMEOUT_SECONDS", 1)

        with caplog.at_level(logging.WARNING, logger="app.posting.threads_browser"):
            with pytest.raises(tb._NeedsHuman) as raised:
                publisher._ensure_logged_in(
                    self._driver_stuck_on_login(), None, user_id="u1", post_id="p1"
                )

        # 로그와 실패 문구 둘 다 '설정에서 저장하라'고 말해야 한다.
        assert "저장된 스레드 로그인 정보가 없습니다" in caplog.text
        assert "설정" in str(raised.value)

    def test_with_credentials_the_message_is_about_the_login_itself(
        self, publisher, monkeypatch
    ):
        monkeypatch.setattr(tb, "LOGIN_TIMEOUT_SECONDS", 1)
        monkeypatch.setattr(publisher, "_fill_login_form", lambda *_a: None, raising=False)

        class Credentials:
            username = "u"
            password = "p"

        with pytest.raises(tb._NeedsHuman) as raised:
            publisher._ensure_logged_in(
                self._driver_stuck_on_login(), Credentials(), user_id="u1", post_id="p1"
            )
        # 자격증명이 있는데 실패한 것이므로 '설정에 저장하라'는 엉뚱한 안내를 하면 안 된다.
        # ('설정' 이라는 낱말 자체는 이제 로그인 버튼 안내에도 쓰이므로 그것으로 가르지 않는다.)
        message = str(raised.value)
        assert "저장" not in message
        assert "로그인" in message


class TestTheDialogHidesTheLoginFormBehindIt:
    """실측(2026-08-04): 2단계 인증은 **모달**로 뜨고 로그인 폼은 그 뒤에 그대로 남는다.

    Selenium은 가려진 요소도 is_displayed()를 True로 본다. 화면 전체를 뒤지면 뒤에 있는
    비밀번호 칸을 보고 "아직 로그인 단계"라 판단해 코드를 영영 물어보지 않는다.
    """

    def _screen_with_dialog(self, code_field):
        dialog = FakeElement(children={"input[type='password']": [], "input": [code_field]})
        return FakeDriver(
            {
                # 뒤 배경의 로그인 폼 — 여전히 DOM에 있고 '보이는' 상태다.
                "input[type='password']": [FakeElement(attrs={"type": "password"})],
                "[role='dialog']": [dialog],
            }
        )

    def test_the_code_field_inside_the_dialog_is_found(self, publisher):
        code_field = FakeElement(attrs={"type": "text", "placeholder": "보안 코드"})
        driver = self._screen_with_dialog(code_field)
        # 뒤에 비밀번호 칸이 있어도 모달 안의 코드 칸을 찾아야 한다.
        assert publisher._verification_code_field(driver) is code_field

    def test_a_password_field_inside_the_dialog_still_blocks(self, publisher):
        """모달 자체가 로그인 폼이면(예: 재로그인 요구) 코드를 묻지 않는다."""
        dialog = FakeElement(
            children={"input[type='password']": [FakeElement(attrs={"type": "password"})]}
        )
        driver = FakeDriver({"[role='dialog']": [dialog]})
        assert publisher._verification_code_field(driver) is None

    def test_a_hidden_dialog_is_not_used_as_the_scope(self, publisher):
        """닫힌 모달이 남아 있을 수 있다 — 보이는 것만 범위로 삼는다."""
        hidden = FakeElement(displayed=False, children={"input": [FakeElement()]})
        code_field = FakeElement(attrs={"type": "text"})
        driver = FakeDriver({"[role='dialog']": [hidden], "input": [code_field]})
        assert publisher._verification_code_field(driver) is code_field


class TestTheScreenSaysWhatItIs:
    """사용자 제안(2026-08-04): "브라우저에 '2단계 인증'이라는 글씨가 보이면 팝업을 띄우면 되지 않나?"

    맞는 접근이다. DOM 속성은 Meta가 빌드할 때마다 바뀌지만 사용자에게 보이는 글자는
    안정적이다. 여기에 더해, 로그인 칸은 autocomplete=username/current-password를 달고
    있어(실측) 코드 칸과 갈라낼 수 있다.
    """

    def test_the_marker_text_makes_a_bare_input_the_code_field(self, publisher):
        code_field = FakeElement(attrs={"type": "text"})
        driver = FakeDriver(
            {"input": [code_field]}, body_text="2단계 인증 · 끝자리가 0706인 전화번호로…"
        )
        assert publisher._verification_code_field(driver) is code_field

    def test_the_login_fields_behind_the_dialog_are_filtered_out(self, publisher):
        """실사용 화면 그대로: 모달 뒤에 로그인 폼이 남아 있다.

        아이디·비밀번호 칸은 autocomplete로 걸러지므로 남는 후보는 코드 칸 하나다.
        """
        code_field = FakeElement(attrs={"type": "text"})
        driver = FakeDriver(
            {
                "input": [
                    FakeElement(attrs={"type": "text", "autocomplete": "username"}),
                    FakeElement(attrs={"type": "password", "autocomplete": "current-password"}),
                    code_field,
                ]
            },
            body_text="2단계 인증",
        )
        assert publisher._verification_code_field(driver) is code_field

    def test_without_the_marker_a_login_form_is_left_alone(self, publisher):
        driver = FakeDriver(
            {
                "input[type='password']": [FakeElement(attrs={"type": "password"})],
                "input": [
                    FakeElement(attrs={"type": "text", "autocomplete": "username"}),
                    FakeElement(attrs={"type": "password", "autocomplete": "current-password"}),
                ],
            },
            body_text="Instagram 계정으로 로그인",
        )
        assert publisher._verification_code_field(driver) is None

    def test_two_code_candidates_are_left_alone_even_with_the_marker(self, publisher):
        """무엇이 코드인지 못 가리면 손대지 않는다 — 엉뚱한 칸에 넣느니 사람이 낫다."""
        driver = FakeDriver(
            {"input": [FakeElement(attrs={"type": "text"}), FakeElement(attrs={"type": "text"})]},
            body_text="2단계 인증",
        )
        assert publisher._verification_code_field(driver) is None


class TestFindingThePostButton:
    """실사용(2026-08-04): '게시'가 아니라 '취소'가 눌려 "스레드를 삭제하시겠어요?"가 떴다.

    화면에 '게시' 버튼이 둘이다 — 배경 피드의 것과 작성 모달 안의 것. 문서 순서로는 배경
    것이 먼저라 그것을 집었고, 그 좌표를 누르니 모달 뒷배경이 눌려 작성 취소로 이어졌다.
    """

    def test_the_button_is_looked_for_inside_the_dialog(self, publisher, monkeypatch):
        monkeypatch.setattr(tb, "COMPOSER_TIMEOUT_SECONDS", 1)
        background = FakeElement()          # 배경 피드의 '게시'
        in_dialog = FakeElement()           # 작성 모달의 '게시'
        dialog = FakeElement(children={"__xpath__": [in_dialog]})

        class Driver:
            def find_elements(self, by, selector):
                if selector == "[role='dialog']":
                    return [dialog]
                return [background]

        # 모달 안에서 찾은 것을 써야 한다.
        monkeypatch.setattr(
            FakeElement, "find_elements", lambda self, _by, _sel: self._children.get("__xpath__", [])
        )
        assert publisher._wait_for_post_button(Driver()) is in_dialog

    def test_the_xpath_is_relative_so_scoping_actually_works(self):
        """`//`로 시작하면 요소에 대고 호출해도 문서 전체를 뒤진다.

        그러면 범위를 모달로 좁혀도 아무 효과가 없다 — 이 한 글자가 버그의 핵심이었다.
        """
        import inspect

        source = inspect.getsource(tb.ThreadsBrowserPublisher._wait_for_post_button)
        assert '".//*[self::button' in source
        assert '"//*[self::button' not in source

    def test_without_a_dialog_it_still_finds_the_button(self, publisher, monkeypatch):
        """모달이 없는 화면(예: 인텐트 URL이 전체 페이지로 열림)에서도 동작해야 한다."""
        monkeypatch.setattr(tb, "COMPOSER_TIMEOUT_SECONDS", 1)
        button = FakeElement()
        driver = FakeDriver({"[role='dialog']": [], "__any__": [button]})
        monkeypatch.setattr(
            FakeDriver, "find_elements",
            lambda self, _by, sel: [] if sel == "[role='dialog']" else [button],
        )
        assert publisher._wait_for_post_button(driver) is button


class TestDialogScope:
    def test_the_topmost_dialog_wins(self, publisher):
        """모달이 겹치면 가장 위(마지막) 것이 사용자가 보는 화면이다."""
        first = FakeElement()
        second = FakeElement()
        driver = FakeDriver({"[role='dialog']": [first, second]})
        assert publisher._dialog_scope(driver) is second

    def test_no_visible_dialog_falls_back_to_the_whole_page(self, publisher):
        driver = FakeDriver({"[role='dialog']": [FakeElement(displayed=False)]})
        assert publisher._dialog_scope(driver) is driver
