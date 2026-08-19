"""네이버 로그인: 아이디/비밀번호 입력, 사람 확인(캡차·2단계) 대기, 새 기기 등록 처리.

세션이 있으면 바로 통과하고, 로그인 정보가 없으면 열린 Chrome에서 사용자가 직접
로그인하도록 기다린다. 캡차나 2단계 인증은 보이는 브라우저에서 사람이 처리한다.
"""

import logging
import time

from ..config import forget_blog_address
from ..credentials import forget_session_account, remember_session_account, session_account
from .browser import NaverConfig, _dismiss_stray_alert, _has_live_session, _NeedsHuman
from .constants import (
    DEVICE_CONFIRM_PATH,
    ELEMENT_TIMEOUT_SECONDS,
    HUMAN_CHECK_TIMEOUT_SECONDS,
    LOGIN_BUTTON_SELECTORS,
    LOGIN_EVENT_DELAY_MILLISECONDS,
    LOGIN_FIELD_PAUSE_SECONDS,
    LOGIN_HOST,
    LOGIN_KEYSTROKE_DELAY_SECONDS,
    LOGIN_REJECTED_MARKERS,
    LOGIN_URL,
    LOGOUT_URL,
    TRUST_BROWSER_ATTEMPTS,
    WRITE_URL,
)

logger = logging.getLogger(__name__)


def _type_input_value(driver, element, value: str) -> None:
    """한 번의 브라우저 작업 안에서 글자별 input 이벤트를 **사람 속도로** 발생시킨다.

    매 글자마다 WebDriver/Clipboard API를 왕복하지 않으면서, 글자별 ``input`` 이벤트와
    누적된 값이 순서대로 전달된다. 글자 사이 간격은 고정값이 아니라 **매번 다른 지연**
    이다(기본 지연 + 무작위 흔들림) — 기계처럼 정확히 같은 간격으로 치면 봇으로
    판정된다(2026-08-18 사용자 지적: "너무 빠르면 봇인 줄 알아"). 로그인 아이디·
    비밀번호와 2단계 인증 코드가 전부 이 함수를 지나므로 한 곳만 고치면 된다.
    """
    field_name = element.get_attribute("id") or "unknown"
    actual_length = 0

    for attempt in range(2):
        driver.set_script_timeout(10)
        result = driver.execute_async_script(
            """
            const el = arguments[0], value = arguments[1], delay = arguments[2];
            const done = arguments[arguments.length - 1];
            const setter = Object.getOwnPropertyDescriptor(
              HTMLInputElement.prototype, 'value'
            ).set;

            (async () => {
              try {
                el.focus();
                setter.call(el, '');
                el.dispatchEvent(new InputEvent('input', {
                  bubbles: true, data: null, inputType: 'deleteContentBackward'
                }));

                let current = '';
                for (const character of value) {
                  current += character;
                  setter.call(el, current);
                  el.dispatchEvent(new KeyboardEvent('keydown', {
                    bubbles: true, key: character
                  }));
                  el.dispatchEvent(new InputEvent('input', {
                    bubbles: true, data: character, inputType: 'insertText'
                  }));
                  el.dispatchEvent(new KeyboardEvent('keyup', {
                    bubbles: true, key: character
                  }));
                  // 사람은 같은 간격으로 치지 않는다 — 글자마다 다른 지연을 준다.
                  await new Promise(resolve =>
                    setTimeout(resolve, delay + Math.random() * delay * 1.5));
                }
                el.dispatchEvent(new Event('change', {bubbles: true}));
                done({ok: true, length: el.value.length});
              } catch (error) {
                done({ok: false, error: String(error)});
              }
            })();
            """,
            element,
            value,
            LOGIN_EVENT_DELAY_MILLISECONDS,
        )

        # 마지막 글자의 input/keyup 이벤트와 네이버 로그인 폼 상태가 반영될 시간을 준다.
        time.sleep(LOGIN_FIELD_PAUSE_SECONDS)

        actual = element.get_attribute("value") or ""
        actual_length = len(actual)
        if result and result.get("ok") and actual == value:
            return
        logger.warning(
            "네이버 로그인 입력 재시도 | field=%s expected_length=%d actual_length=%d attempt=%d",
            field_name,
            len(value),
            actual_length,
            attempt + 1,
        )
        time.sleep(LOGIN_FIELD_PAUSE_SECONDS)

    # DOM 이벤트 입력이 차단된 화면에서만 실제 키 입력을 짧은 간격으로 한 번 시도한다.
    element.click()
    element.clear()
    for character in value:
        element.send_keys(character)
        time.sleep(LOGIN_KEYSTROKE_DELAY_SECONDS)
    time.sleep(LOGIN_FIELD_PAUSE_SECONDS)
    actual = element.get_attribute("value") or ""
    actual_length = len(actual)
    if actual == value:
        return

    raise RuntimeError(
        f"네이버 로그인 {field_name} 입력란에 값을 입력하지 못했습니다. "
        f"(입력 길이 {len(value)}, 반영 길이 {actual_length})"
    )


class NaverLogin:
    def __init__(self, driver, config: NaverConfig, human_wait_seconds: float | None = None):
        self.driver = driver
        self.config = config
        # 사람이 캡차·2단계 인증을 끝내기까지 기다리는 시간. 설정 화면에서 누른 로그인은
        # 사람이 앞에 있으니 더 기다리고, 발행 중에는 기본값(180초)을 지킨다.
        self.human_wait_seconds = human_wait_seconds or HUMAN_CHECK_TIMEOUT_SECONDS

    def ensure_logged_in(self) -> None:
        """로그인을 보장하고, **성공한 계정을 프로필에 적어 둔다.**

        적어 두지 않으면 다음 실행에서 이 프로필의 쿠키가 누구 것인지 알 수 없다.
        네이버 계정을 여러 개 번갈아 쓰는 사람은 설정만 바꾸고 예전 계정으로 발행하게 된다.
        """
        self._ensure_logged_in()
        remember_session_account(self.config.profile_dir, self.config.username)

    def _session_belongs_to_settings(self) -> bool:
        """지금 살아 있는 세션이 설정에 저장된 네이버 계정 것인지.

        판단할 수 없을 때의 선택이 중요하다.

        - 설정에 아이디가 없으면(사람이 직접 로그인하는 방식) 비교할 대상이 없다 → 그대로 쓴다.
        - 아이디는 있는데 **자동 로그인할 수 없으면**(비밀번호 없음) 세션을 끊지 않는다.
          다시 만들 수 없는 것을 버리면 발행 자체가 불가능해진다.
        - 누구 것인지 모르면(예전 프로필에는 기록이 없다) **한 번 새로 로그인한다.** 그
          한 번의 비용으로 이후에는 항상 설정과 맞는 계정으로 발행된다.
        """
        wanted = (self.config.username or "").strip()
        if not wanted or not self.config.can_log_in:
            return True
        current = session_account(self.config.profile_dir)
        if current is None:
            logger.info("이 Chrome 프로필의 네이버 계정을 알 수 없어 한 번 새로 로그인합니다.")
            return False
        if current.strip().lower() == wanted.lower():
            return True
        # 계정명이 이메일·전화번호일 수도 있다. 암호화 저장한 값을 진단 로그에
        # 평문 사본으로 다시 남기지 않는다.
        logger.info("설정의 네이버 계정과 브라우저 세션 계정이 달라 새로 로그인합니다.")
        return False

    def _sign_out(self) -> None:
        """이전 계정의 세션만 끊는다. **쿠키를 통째로 지우지 않는다.**

        '이 브라우저는 2단계 인증 없이 로그인 합니다'로 얻은 신뢰도 쿠키에 들어 있다.
        ``delete_all_cookies``로 쓸어버리면 계정을 바꿀 때마다 2단계 인증을 다시 하게 된다.
        네이버 로그아웃 주소는 로그인 쿠키만 지우므로 그 신뢰는 남는다 — 한 번 인증해 둔
        계정으로 돌아올 때 다시 묻지 않는다.

        쿠키를 지우는 것은 **로그아웃이 듣지 않았을 때뿐이다.** 그때는 2단계 인증을 다시
        하는 편이 이전 계정으로 남의 블로그에 발행하는 것보다 낫다.
        """
        forget_session_account(self.config.profile_dir)
        # 이전 계정의 블로그 주소는 새 계정과 아무 상관이 없다. 남겨 두면 다음 발행이
        # 남의 블로그 주소로 먼저 들어가려 한다.
        forget_blog_address(self.config.profile_dir)
        try:
            self._go_and_clear_alert(LOGOUT_URL)
        except Exception as error:
            logger.warning("이전 네이버 계정 로그아웃에 실패했습니다: %s", error)
        try:
            if not self._past_alerts(lambda: _has_live_session(self.driver)):
                return
            logger.warning(
                "네이버 로그아웃이 듣지 않아 쿠키를 지웁니다 — 2단계 인증을 다시 요구할 수 있습니다."
            )
            self.driver.delete_all_cookies()
        except Exception as error:
            logger.warning("이전 세션을 정리하지 못했습니다: %s", error)

    def _ensure_logged_in(self) -> None:
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        # 블로그 홈(BLOG_URL)이 아니라 **글쓰기로 바로 들어간다.** 홈을 거치면 "게시물이
        # 삭제되었거나 다른 페이지로 변경되었습니다" 같은 알림창이 떠서 그 뒤 Selenium
        # 명령이 전부 막힌다. 어차피 가려는 곳도 글쓰기다.
        # **계정이 바뀌었는지는 브라우저를 열기 전에 파일만 보고 안다.**
        # 이 판단을 뒤로 미루고 글쓰기 주소부터 열면, 이전 계정으로 로그인된 채 남의
        # 블로그에 들어가는 꼴이 된다. 네이버는 원래 계정 블로그로 되돌리면서 "삭제되었거나
        # 존재하지 않는 게시물입니다" 안내창을 띄우고, 그 뒤 쿠키를 읽는 것조차 막혀
        # 로그인이 502로 끝났다(2026-08-05 실사용).
        if not self._session_belongs_to_settings():
            self._sign_out()
        else:
            self._go_and_clear_alert(WRITE_URL.format(blog_id=self.config.blog_id))
            if self._past_alerts(lambda: _has_live_session(self.driver)):
                logger.info("저장된 네이버 세션으로 로그인 없이 진행합니다.")
                return
        if not self.config.can_log_in:
            self.driver.get(LOGIN_URL)
            self._enable_keep_logged_in()
            logger.info("열린 Chrome에서 네이버 로그인을 완료해 주세요.")
            self._wait_for_completed_login()
            return

        return_url = WRITE_URL.format(blog_id=self.config.blog_id)
        self.driver.get(f"{LOGIN_URL}?url={return_url}")
        wait = WebDriverWait(self.driver, ELEMENT_TIMEOUT_SECONDS)
        id_input = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input#id")))
        pw_input = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input#pw")))
        try:
            _type_input_value(self.driver, id_input, self.config.username or "")
            _type_input_value(self.driver, pw_input, self.config.password or "")
        except RuntimeError as error:
            logger.warning("%s 열린 Chrome에서 직접 로그인해 주세요.", error)
            self._wait_for_completed_login()
            return

        # 아이디 입력 후 비밀번호를 치는 동안 보안 스크립트가 앞 입력란을 다시 비우는
        # 경우까지 막는다. 제출 직전 재입력도 JavaScript 값 주입 없이 실제 키로만 한다.
        for element, value in (
            (id_input, self.config.username or ""),
            (pw_input, self.config.password or ""),
        ):
            if (element.get_attribute("value") or "") != value:
                try:
                    _type_input_value(self.driver, element, value)
                except RuntimeError as error:
                    logger.warning("%s 열린 Chrome에서 직접 로그인해 주세요.", error)
                    self._wait_for_completed_login()
                    return

        time.sleep(LOGIN_FIELD_PAUSE_SECONDS)
        self._enable_keep_logged_in()

        def visible_login_button(_driver):
            for selector in LOGIN_BUTTON_SELECTORS:
                for element in _driver.find_elements(By.CSS_SELECTOR, selector):
                    if not element.is_displayed() or not element.is_enabled():
                        continue
                    text = (element.text or "").strip()
                    # btn_done은 패스키 완료 버튼에도 쓰이므로 텍스트가 로그인인 것만 누른다.
                    if selector != "button.btn_done" or text == "로그인":
                        return element
            return False

        try:
            login_button = wait.until(visible_login_button)
            login_url_before_click = self.driver.current_url
            ActionChains(self.driver).move_to_element(login_button).pause(0.2).click().perform()

            # 일부 로그인 폼은 자동화된 pointer click을 무시한다. 화면과 URL이 그대로인
            # 경우에만 비밀번호 입력란의 Enter를 한 번 폴백으로 보낸다.
            time.sleep(2)
            if (
                self.driver.current_url == login_url_before_click
                and LOGIN_HOST in self.driver.current_url
                and visible_login_button(self.driver)
            ):
                password_fields = self.driver.find_elements(By.CSS_SELECTOR, "input#pw")
                if password_fields and password_fields[0].is_displayed():
                    password_fields[0].send_keys(Keys.ENTER)
        except Exception as error:
            # 버튼이 안 보인다고 곧바로 실패로 끝내지 않는다. 비밀번호를 넣는 사이 폼이
            # 스스로 제출돼 **2단계 인증 화면으로 넘어가 있으면 그 화면에는 로그인 버튼이
            # 없다** — 로그인은 잘 되고 있는데 "로그인 버튼을 찾지 못했습니다"로 발행이
            # 끊겼다(2026-08-05 실사용). 화면이 이미 넘어갔으면 인증 완료를 기다린다.
            if self._login_form_is_gone():
                logger.info("로그인 폼이 이미 넘어갔습니다 — 인증 완료를 기다립니다.")
                self._wait_for_completed_login()
                return

            buttons = [
                {
                    "id": element.get_attribute("id"),
                    "class": element.get_attribute("class"),
                    "text": (element.text or "").strip(),
                    "displayed": element.is_displayed(),
                }
                for element in self.driver.find_elements(By.CSS_SELECTOR, "button")
            ]
            logger.warning("네이버 로그인 버튼 후보: %s", buttons[:30])
            raise RuntimeError("네이버 로그인 버튼을 찾지 못했습니다. 로그인 화면이 바뀌었을 수 있습니다.") from error

        self._wait_for_completed_login()

    def _enable_keep_logged_in(self) -> None:
        """로그인 폼의 '로그인 상태 유지'를 켠다.

        이 토글 없이 로그인하면 NID_AUT/NID_SES가 세션 쿠키로 발급돼 브라우저가 닫히는
        순간 사라진다 — 프로필에 쿠키 DB 파일은 남는데 알맹이가 없어, 매 발행이
        재로그인과 새 기기 확인을 반복하던 원인이다. 켜 두면 다음 발행부터
        ensure_logged_in의 세션 빠른 경로로 로그인 없이 진행된다.

        비치명적: 못 켜도 로그인은 계속한다(그 회차의 세션이 저장되지 않을 뿐이다).
        """
        from selenium.webdriver.common.action_chains import ActionChains

        try:
            found = self.driver.execute_script(
                """
                const checkbox = document.querySelector(
                  "input#keep, input[name='nvlong'], input#nvlong, input[name='keep']");
                if (checkbox) {
                  if (checkbox.checked) return {state: 'on'};
                  // 스타일된 토글은 input이 숨겨져 있어 label을 눌러야 한다.
                  const label = checkbox.id
                    ? document.querySelector("label[for='" + checkbox.id + "']") : null;
                  return {state: 'off', element: label || checkbox};
                }
                // 마크업 변형 폴백: '로그인 상태 유지' 문구를 가진 클릭 대상을 찾는다.
                for (const el of document.querySelectorAll(
                    "label, button, [role='switch'], [role='checkbox'], span")) {
                  if ((el.innerText || '').trim() === '로그인 상태 유지') {
                    return {state: 'off', element: el.closest('label') || el};
                  }
                }
                return {state: 'missing'};
                """
            )
        except Exception as error:
            logger.warning("'로그인 상태 유지' 확인 실패(이번 세션은 저장되지 않습니다): %s", error)
            return

        state = (found or {}).get("state")
        if state == "on":
            return
        if state != "off" or (found or {}).get("element") is None:
            logger.warning("'로그인 상태 유지' 토글을 찾지 못했습니다 — 로그인 화면이 바뀌었을 수 있습니다.")
            return

        element = found["element"]
        try:
            ActionChains(self.driver).move_to_element(element).pause(0.2).click().perform()
        except Exception:
            try:
                self.driver.execute_script("arguments[0].click();", element)
            except Exception as error:
                logger.warning("'로그인 상태 유지'를 켜지 못했습니다(이번 세션은 저장되지 않습니다): %s", error)
                return
        time.sleep(0.3)
        try:
            enabled = bool(
                self.driver.execute_script(
                    """
                    const checkbox = document.querySelector(
                      "input#keep, input[name='nvlong'], input#nvlong, input[name='keep']");
                    return checkbox ? checkbox.checked : null;
                    """
                )
            )
        except Exception:
            enabled = False
        if enabled:
            logger.info("'로그인 상태 유지'를 켰습니다 — 다음 발행부터 재로그인 없이 진행됩니다.")
        else:
            # 체크박스를 못 읽는 마크업(폴백 경로)에서는 클릭까지만 하고 넘어간다.
            logger.info("'로그인 상태 유지'를 눌렀습니다(상태 확인 불가 마크업).")

    def _wait_for_completed_login(self) -> None:
        from selenium.webdriver.common.by import By

        started_at = time.monotonic()
        deadline = started_at + self.human_wait_seconds
        reminder_at = started_at + 15
        warned = False
        device_choice_attempted = False
        # 2단계 인증 화면은 늦게 그려질 수 있어 몇 번 다시 시도한다. 성공하면 더 누르지
        # 않는다 — 다시 누르면 방금 켠 체크가 도로 꺼진다.
        trust_attempts_left = TRUST_BROWSER_ATTEMPTS
        while time.monotonic() < deadline:
            time.sleep(1)
            # 매 바퀴 알림창부터 치운다. 하나라도 떠 있으면 아래 모든 호출이 막힌다.
            alert_text = _dismiss_stray_alert(self.driver)
            if alert_text:
                logger.info("네이버 안내창을 닫고 로그인을 계속합니다: %s", alert_text)
            current_url = self._current_url_safely()

            # 로그인 성공 뒤 표시되는 새 기기 등록 선택은 인증 우회가 아니다. 자동화
            # 프로필을 신뢰 기기로 등록하지 않는 보수적인 선택만 자동으로 누른다.
            if DEVICE_CONFIRM_PATH in current_url and not device_choice_attempted:
                device_choice_attempted = True
                if self._decline_device_registration():
                    logger.info("네이버 새 기기 등록을 건너뛰고 로그인을 계속합니다.")
                    time.sleep(1)
                    continue
                logger.info("새 기기 등록 화면의 '등록안함'을 직접 눌러 주세요.")

            if LOGIN_HOST not in current_url:
                # 2단계 인증을 끝내면 로그인 버튼 없이 곧바로 이 단계다. 홈을 들르지 않고
                # 글쓰기로 바로 간다(홈은 알림창이 떠서 드라이버를 멈춘다).
                self._go_and_clear_alert(WRITE_URL.format(blog_id=self.config.blog_id))
                if self._past_alerts(lambda: _has_live_session(self.driver)):
                    return

            try:
                body = self.driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                body = ""
            if any(marker in body for marker in LOGIN_REJECTED_MARKERS):
                raise RuntimeError("네이버 아이디 또는 비밀번호가 맞지 않습니다.")
            has_challenge = any(
                marker in body for marker in ("캡차", "보안 확인", "2단계 인증", "추가 인증")
            )
            # 2단계 인증 화면이면 '이 브라우저는 2단계 인증 없이 로그인 합니다'를 켜 둔다.
            # 사용자가 매번 휴대폰 알림을 승인하지 않아도 되게 하려는 것이다.
            if "2단계 인증" in body and trust_attempts_left > 0:
                trust_attempts_left -= 1
                if self._trust_this_browser():
                    trust_attempts_left = 0
            if not warned and (has_challenge or time.monotonic() >= reminder_at):
                logger.info("네이버 추가 인증이 있다면 열린 Chrome에서 완료해 주세요.")
                warned = True

        raise _NeedsHuman(
            "네이버가 추가 인증(캡차·2단계 인증)을 요구했습니다. "
            "설정 화면에서 네이버 '로그인' 버튼을 눌러 인증을 끝낸 뒤 다시 발행해 주세요 — "
            "발행 도중에는 기다릴 수 있는 시간이 짧습니다."
        )

    def _go_and_clear_alert(self, url: str) -> None:
        """주소로 이동하고, 떠 있는 알림창이 있으면 닫는다.

        네이버는 블로그 주소로 들어갈 때 "게시물이 삭제되었거나 다른 페이지로
        변경되었습니다" 같은 안내창을 띄우곤 한다. 그대로 두면 그다음 Selenium 명령이
        전부 막혀 발행이 통째로 멈춘다.
        """
        self.driver.get(url)
        text = _dismiss_stray_alert(self.driver)
        if text:
            logger.info("네이버 안내창을 닫고 계속합니다: %s", text)

    def _past_alerts(self, work):
        """알림창에 막힌 호출을 창을 닫고 다시 해 본다.

        안내창은 페이지가 리다이렉트된 **뒤에** 뜬다. 그래서 이동 직후 한 번 닫는 것으로는
        부족하고, 그다음 호출이 대신 예외를 맞는다 — 실제로 세션 쿠키를 읽다가 터져
        로그인이 502로 끝났다. 알림창이 아니면 예외를 그대로 올린다(삼키지 않는다).
        """
        for _ in range(3):
            try:
                return work()
            except Exception:
                if _dismiss_stray_alert(self.driver) is None:
                    raise
        return work()

    def _current_url_safely(self) -> str:
        """알림창이 떠 있으면 닫고 주소를 읽는다.

        안내창은 ``driver.get()`` 직후가 아니라 **로그인 리다이렉트 뒤에 늦게 뜬다.**
        그래서 이동할 때 한 번 닫는 것만으로는 부족하다. 떠 있는 동안에는 주소를 읽는
        것조차 ``UnexpectedAlertPresentException``으로 막혀, 로그인 대기 루프가 통째로
        죽고 크롬이 새 탭인 채로 멈춘다(2026-08-05 실사용).
        """
        for _ in range(2):
            try:
                return self.driver.current_url
            except Exception:
                if _dismiss_stray_alert(self.driver) is None:
                    return ""
        return ""

    def _login_form_is_gone(self) -> bool:
        """아이디/비밀번호 폼을 이미 지나왔는지 — 로그인이 진행 중이라는 뜻이다.

        로그인 호스트를 벗어났거나, 입력칸이 사라졌거나, 추가 인증 문구가 보이면
        폼 단계는 끝난 것으로 본다. 판단이 애매하면 **False**를 돌려 기존대로 실패하게
        둔다 — 진짜로 화면이 바뀐 경우까지 조용히 기다리면 3분을 버리고 끝난다.
        """
        from selenium.webdriver.common.by import By

        try:
            if LOGIN_HOST not in self.driver.current_url:
                return True
            visible_fields = [
                element
                for selector in ("input#id", "input#pw")
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector)
                if element.is_displayed()
            ]
            if not visible_fields:
                return True
            body = self.driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            return False
        return any(
            marker in body for marker in ("2단계 인증", "인증 알림", "캡차", "보안 확인", "추가 인증")
        )

    def _trust_this_browser(self) -> bool:
        """2단계 인증 화면의 '이 브라우저는 "2단계 인증" 없이 로그인 합니다'를 켠다.

        켜 두면 이 Chrome 프로필에서는 다음 로그인부터 2단계 인증을 다시 묻지 않는다.
        켜지 않으면 세션이 만료될 때마다 사람이 휴대폰 알림을 승인해야 하고, 그동안
        발행은 ``_NeedsHuman``으로 멈춘다.

        **계정 보안 설정을 바꾸는 동작이다.** 새 기기 등록(``_decline_device_registration``)
        을 일부러 거절하는 것과는 방향이 반대라, 사용자가 명시적으로 요청해서 넣었다.
        대상은 발행 자동화용 프로필 하나뿐이고 사용자의 평소 브라우저는 건드리지 않는다.

        비치명적: 못 켜도 로그인은 계속한다(다음에 또 물어볼 뿐이다).
        """
        from selenium.webdriver.common.action_chains import ActionChains

        try:
            found = self.driver.execute_script(
                """
                // 문구는 따옴표 모양이 화면마다 달라서(" vs “) 따옴표를 빼고 맞춘다.
                const wanted = (t) =>
                  t.includes('2단계') && t.includes('없이') && t.includes('로그인');

                for (const box of document.querySelectorAll("input[type='checkbox']")) {
                  const label = box.id
                    ? document.querySelector("label[for='" + CSS.escape(box.id) + "']") : null;
                  const near = box.closest('label') || box.parentElement;
                  const text = ((label && label.innerText) || (near && near.innerText) || '').trim();
                  if (!wanted(text)) continue;
                  if (box.checked) return {state: 'on'};
                  // 스타일된 체크박스는 input이 숨겨져 있어 label을 눌러야 한다.
                  return {state: 'off', element: label || box.closest('label') || box};
                }

                // 마크업 변형 폴백: 문구를 가진 클릭 대상 중 가장 좁은 것을 고른다.
                // 문서 순서로 먼저 잡으면 문구를 품은 바깥 컨테이너가 걸려 엉뚱한 곳을 누른다.
                let best = null;
                for (const el of document.querySelectorAll(
                    "label, button, [role='checkbox'], [role='switch'], span, div, a")) {
                  const text = (el.innerText || '').trim();
                  if (!text || text.length > 80 || !wanted(text)) continue;
                  if (!best || text.length < (best.innerText || '').trim().length) best = el;
                }
                if (best) return {state: 'off', element: best.closest('label') || best};
                return {state: 'missing'};
                """
            )
        except Exception as error:
            logger.warning("'2단계 인증 없이 로그인' 확인 실패: %s", error)
            return False

        state = (found or {}).get("state")
        if state == "on":
            return True
        if state != "off" or (found or {}).get("element") is None:
            # 아직 화면이 그려지는 중일 수 있다. 호출한 쪽이 다시 시도한다.
            return False

        element = found["element"]
        try:
            ActionChains(self.driver).move_to_element(element).pause(0.2).click().perform()
        except Exception:
            try:
                self.driver.execute_script("arguments[0].click();", element)
            except Exception as error:
                logger.warning("'2단계 인증 없이 로그인'을 켜지 못했습니다: %s", error)
                return False
        logger.info(
            "'이 브라우저는 2단계 인증 없이 로그인 합니다'를 켰습니다 — "
            "다음 로그인부터 이 프로필은 2단계 인증을 묻지 않습니다."
        )
        return True

    def _decline_device_registration(self) -> bool:
        """새 기기 등록 화면에서 계정 보안을 바꾸지 않는 '등록안함'만 선택한다."""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By

        xpath = (
            "//button[normalize-space(.)='등록안함'] | "
            "//a[normalize-space(.)='등록안함'] | "
            "//*[@role='button' and normalize-space(.)='등록안함'] | "
            "//input[@value='등록안함'] | "
            "//*[normalize-space(.)='등록안함']"
        )
        for element in self.driver.find_elements(By.XPATH, xpath):
            try:
                if not element.is_displayed() or not element.is_enabled():
                    continue
                ActionChains(self.driver).move_to_element(element).pause(0.2).click().perform()
                return True
            except Exception:
                continue
        return False
