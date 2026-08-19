"""스마트에디터 ONE 조작: 글쓰기 페이지 진입, 제목·본문 붙여넣기, 태그 입력, 발행.

본문 입력은 NaverPublishPlan 하나로만 받는다: 앵커 토큰이 든 스캐폴드 HTML을 **한 번**
붙여넣고, 각 앵커 문단을 실제 이미지로 교체한 뒤, DOM을 계획과 대조 검증한다. 예전의
"조각 붙여넣기 → 이미지 → 조각 붙여넣기" 방식은 캐럿 위치와 서식 상태(굵기 이어짐)에
의존해 순서가 꼬였다 — 그 경로(article_segments/fill_with_uploads/_focus_body_end)는
제거됐다. 검증이 하나라도 실패하면 임시저장·발행을 부르면 안 된다(fail-closed).
"""

import base64
import logging
import os
import platform
import time
from contextlib import nullcontext

from app.shared import FinalPost
from app.posting.image_url import signed_post_image_url
from app.posting.url_safety import safe_url_for_log

from .browser import _dismiss_stray_alert
from .clipboard import (
    ClipboardOverwrittenError,
    _os_clipboard_html,
    _os_clipboard_image,
    _os_clipboard_text,
    _write_browser_clipboard,
    clipboard_still_holds,
    use_os_clipboard,
)
from .constants import (
    ANCHOR_PASTE_ATTEMPTS,
    ANCHOR_SELECT_ATTEMPTS,
    BLOG_URL,
    BODY_SELECTORS,
    CAPTION_FIELD_TIMEOUT_SECONDS,
    CAPTION_IMAGE_TIMEOUT_SECONDS,
    CAPTION_VERIFY_TIMEOUT_SECONDS,
    DRAFT_CANCEL_XPATHS,
    DRAFT_POPUP_TIMEOUT_SECONDS,
    EDITOR_FRAME,
    ELEMENT_TIMEOUT_SECONDS,
    IMAGE_LOAD_TIMEOUT_SECONDS,
    IMAGE_PASTE_TIMEOUT_SECONDS,
    LAYOUT_SETTLE_INTERVAL_SECONDS,
    LAYOUT_SETTLE_TIMEOUT_SECONDS,
    PAGE_LOAD_TIMEOUT_SECONDS,
    PUBLISH_CONFIRM_SELECTORS,
    PUBLISH_OPEN_SELECTORS,
    SCAFFOLD_PASTE_ATTEMPTS,
    SCAFFOLD_PASTE_TIMEOUT_SECONDS,
    TEMP_SAVE_SELECTORS,
    TITLE_PASTE_ATTEMPTS,
    TITLE_SELECTORS,
    WRITE_BUTTON_SELECTORS,
    WRITE_REDIRECT_URL,
    WRITE_URL,
)
from .plan import ANCHOR_TOKEN_PATTERN, NaverImageAnchor, NaverPublishPlan, normalize_text

logger = logging.getLogger(__name__)


def paste_mode() -> str:
    """발행 입력 모드: ``auto``(기본) · ``synthetic`` · ``clipboard``.

    OS 클립보드는 기기 전체에 하나라, 여러 발행이 잠금으로 줄을 서야 하고(작업 A)
    사용자의 복사와도 겹친다. 합성 붙여넣기는 페이지 안에서 ClipboardEvent를 직접
    만들어 쏘므로 **클립보드를 아예 만지지 않는다** — 발행끼리 완전히 독립이 되고,
    여러 사용자 동시 발행의 전제다. 그래서 **합성을 먼저 시도한다.**

    다만 2026-08-19 실발행에서 확인됐다: **스마트에디터 ONE은 합성 paste를 받지
    않는다.** 제목은 키 입력 폴백으로 들어갔지만 본문 스캐폴드에서 곧바로 실패했다.
    그래서 기본은 ``synthetic``이 아니라 ``auto``다 — 합성을 시도하되 거부되면 그
    발행만 클립보드 경로로 갈아타 **발행이 죽지 않는다**. 네이버가 정책을 바꿔
    합성을 받게 되면 그날부터 자동으로 무잠금 병렬이 된다.

    - ``auto``(기본): synthetic으로 시작, 거부되면 **그 발행 인스턴스만** 클립보드로.
    - ``synthetic``: 클립보드를 절대 만지지 않는다(엄격). 거부 시 제목은 키 입력,
      이미지는 업로드 자동화로 폴백하고, 본문이 거부되면 멈춘다(fail-closed).
      합성이 통하는 환경에서 클립보드 사용을 원천 차단하고 싶을 때만 쓴다.
    - ``clipboard``: 기존 경로(전역 잠금 + SHA-256 대조 + Ctrl+V)만 쓴다.
    """
    value = (os.environ.get("NAVER_PASTE_MODE") or "").strip().lower()
    if value in ("clipboard", "synthetic"):
        return value
    if value and value != "auto":
        logger.warning("NAVER_PASTE_MODE=%r 는 모르는 값입니다 — auto로 봅니다.", value)
    return "auto"


class SyntheticPasteError(RuntimeError):
    """합성 paste 이벤트를 보냈지만 에디터가 소비하지 않았다(또는 보내지 못했다).

    이 시점의 문서는 그대로다 — 합성(isTrusted=false) 이벤트는 브라우저 기본 삽입
    동작을 일으키지 않으므로, 잡은 쪽은 안전하게 다른 경로(재시도·폴백)로 갈 수 있다.
    """


class SyntheticImagePasteError(SyntheticPasteError):
    """이미지 합성 붙여넣기가 거부됐고, 업로드 자동화 폴백도 실패했다."""


class EditorTargetNotFoundError(RuntimeError):
    """에디터의 입력 대상(제목 칸·본문 칸·iframe)을 찾지 못했다 — 화면이 바뀌었을 수 있다."""


def _image_mime(image_bytes: bytes) -> str:
    """붙여넣을 이미지의 MIME 추정. 계획에는 바이트만 남아 있어 머리글로 판별한다."""
    if image_bytes.startswith(b"\x89PNG"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF8"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


# 합성 붙여넣기: 현재 포커스된 요소에 paste 이벤트를 직접 쏜다. 에디터가 이벤트를
# 소비했으면 defaultPrevented가 True다 — 처리 여부의 1차 신호이고, 실제 반영은
# 부르는 쪽의 DOM 검증(제목 대조·스캐폴드 상태·이미지 수)이 최종 판정한다.
_SYNTHETIC_PASTE_SCRIPT = """
const [html, text, imageBase64, imageMime] = arguments;
const dt = new DataTransfer();
if (text) dt.setData('text/plain', text);
if (html) dt.setData('text/html', html);
if (imageBase64) {
  const bytes = Uint8Array.from(atob(imageBase64), c => c.charCodeAt(0));
  dt.items.add(new File([bytes], 'image.' + (imageMime || 'image/png').split('/')[1],
                        {type: imageMime || 'image/png'}));
}
const target = document.activeElement || document.body;
const event = new ClipboardEvent('paste',
  {clipboardData: dt, bubbles: true, cancelable: true});
const dispatched = target.dispatchEvent(event);
return {
  dispatched: true,
  consumed: event.defaultPrevented || !dispatched,
  targetTag: target.tagName,
};
"""


def _selection_is_on_anchor(selection: dict, token: str) -> bool:
    """지금 캐럿이 그 앵커 문단 위에 있는가 — 붙여넣기 직전의 마지막 관문.

    **DOM 선택이 비어 있어도(collapsed) 통과시킨다.** SmartEditor는 내부 선택 모델을
    따로 들고 있어서, 화면에서는 줄이 선택돼 보이고 실제 Ctrl+V도 그 줄을 덮어쓰는데
    ``window.getSelection()``은 접힌 캐럿만 돌려주는 경우가 있다(실발행 2026-08-03:
    '선택된 글자 ""·같은 문단 True'로 첫 앵커부터 3회 거부돼 발행이 통째로 막혔다).

    그래서 판정 기준은 '선택된 글자가 토큰인가'가 아니라 **'캐럿이 이 문단 안인가'**다.
    막으려는 것은 클릭이 다른 문단에 떨어지는 경우이고, 그건 inside=False로 잡힌다.
    DOM 선택이 실제로 잡힌 경우에만 그 글자가 토큰인지 함께 본다.
    """
    if not selection.get("found") or not selection.get("inside"):
        return False
    if selection.get("collapsed"):
        return True
    return normalize_text(str(selection.get("text") or "")) == token


def article_html(post: FinalPost, post_id: str, api_origin: str, hashtags: bool = True) -> str:
    """네이버가 가져갈 수 있도록 data URL 이미지를 로컬 HTTP URL로 바꾼다."""
    html = post.html_content
    for index, image in enumerate(post.images or []):
        html = html.replace(
            image.data_url, signed_post_image_url(api_origin, post_id, index)
        )

    if hashtags and post.hashtags:
        tags = " ".join(f"#{tag}" for tag in post.hashtags)
        html = f"{html}\n<p>{tags}</p>"
    return html


class SmartEditorOne:
    """SmartEditor ONE 탐색, 발행 계획 입력·검증, 임시저장과 최종 발행."""

    def __init__(self, driver):
        self.driver = driver
        # 합성 붙여넣기 모드에서 다음 _paste_verified가 쏠 내용. 클립보드 대신 여기에
        # 담는다 — 담는 쪽과 쏘는 쪽이 기존 클립보드 경로와 같은 자리이도록.
        self._pending_paste: dict | None = None
        # 이 발행의 실효 모드. auto는 synthetic으로 시작해 에디터가 거부하면
        # _fall_back_to_clipboard가 이 값을 clipboard로 바꾼다 — 환경변수는 그대로 두고
        # **이 발행 인스턴스만** 갈아탄다(동시 발행 중인 다른 인스턴스와 독립).
        self._paste_mode: str = "clipboard" if paste_mode() == "clipboard" else "synthetic"

    def _synthetic_active(self) -> bool:
        """지금 이 발행이 합성 붙여넣기로 도는가 (auto 폴백 후에는 False)."""
        return self._paste_mode == "synthetic"

    def _fall_back_to_clipboard(self, what: str) -> None:
        """auto 모드: 에디터가 합성 이벤트를 거부해 이 발행을 클립보드 경로로 갈아탄다."""
        self._paste_mode = "clipboard"
        self._pending_paste = None
        logger.warning(
            "[NAVER_PUBLISH] mode=auto fallback=clipboard — %s 합성 붙여넣기가 거부되어 "
            "이 발행을 클립보드 경로(전역 잠금+대조)로 전환합니다.",
            what,
        )

    def _paste_guard(self):
        """붙여넣기 구간 보호. 클립보드 모드에서는 기기 전역 잠금, 합성 모드에서는 없음.

        합성 붙여넣기는 페이지 안 이벤트라 다른 발행과 겹칠 자원이 없다 — 잠금을 걸면
        모처럼 없앤 직렬화가 되살아난다.
        """
        return nullcontext() if self._synthetic_active() else use_os_clipboard()

    def _synthetic_paste(self, what: str) -> None:
        """담아 둔 내용(_pending_paste)을 합성 paste 이벤트로 쏜다."""
        payload, self._pending_paste = self._pending_paste, None
        if not payload:
            raise RuntimeError(f"{what}: 붙여넣을 내용이 준비되지 않았습니다.")
        image_bytes = payload.get("image_bytes")
        try:
            result = self.driver.execute_script(
                _SYNTHETIC_PASTE_SCRIPT,
                payload.get("html"),
                payload.get("text"),
                base64.b64encode(image_bytes).decode("ascii") if image_bytes else None,
                _image_mime(image_bytes) if image_bytes else None,
            )
        except Exception as error:
            raise SyntheticPasteError(f"{what} 합성 붙여넣기 실행에 실패했습니다.") from error
        if not result or not result.get("dispatched"):
            raise SyntheticPasteError(f"{what} 합성 붙여넣기 이벤트를 보내지 못했습니다.")
        if not result.get("consumed"):
            # 에디터가 이벤트를 무시했다(신뢰하지 않는 이벤트 거부 등). 여기서 바로
            # 알린다 — DOM 검증 시간 초과를 기다리는 것보다 원인이 분명하다.
            raise SyntheticPasteError(
                f"{what} 합성 붙여넣기를 에디터가 받지 않았습니다 "
                f"(대상: {result.get('targetTag')}). NAVER_PASTE_MODE=auto(자동 폴백) "
                "또는 clipboard로 발행해 주세요."
            )

    def navigate(self, blog_id: str) -> str | None:
        """글쓰기 화면(스마트에디터)에 진입하고, **실제로 열린 블로그 주소**를 돌려준다.

        진입 경로는 셋이다: 고정 글쓰기 URL(``WRITE_URL``) → 로그인한 계정의 글쓰기
        주소(``WRITE_REDIRECT_URL``) → 화면의 '글쓰기' 버튼. 셋 다 실패하면 현재 URL과
        함께 실패를 남긴다.

        돌려주는 주소가 넘겨받은 ``blog_id``와 다를 수 있다. **네이버 아이디와 블로그
        주소는 같지 않아도 된다** — 아이디가 `win-z`인데 주소가 `aiona_it`인 계정이
        실제로 있었다. 그래서 이것은 오류가 아니고, 부르는 쪽이 기억해 두면 다음부터
        첫 번째 경로에서 바로 들어간다. 주소를 읽지 못하면 ``None``이다.
        """
        self.driver.get(WRITE_URL.format(blog_id=blog_id))
        # 네이버가 띄우는 안내창("게시물이 삭제되었거나…")을 닫는다. 열려 있으면
        # 아래 switch_to부터 모든 Selenium 명령이 막힌다.
        alert_text = _dismiss_stray_alert(self.driver)
        if alert_text:
            logger.info("네이버 안내창을 닫고 계속합니다: %s", alert_text)
        if not self._try_enter_editor_frame():
            logger.warning(
                "고정 글쓰기 URL로 에디터에 진입하지 못했습니다(현재 URL: %s). "
                "로그인한 계정의 글쓰기 주소로 다시 시도합니다.",
                safe_url_for_log(self._current_url()),
            )
            # blog_id가 지금 로그인한 계정의 블로그가 아닐 때 여기서 걸린다. 이 주소는
            # 계정을 보고 알아서 옮겨 주므로 blog_id가 어긋나 있어도 들어간다.
            self.driver.get(WRITE_REDIRECT_URL)
            _dismiss_stray_alert(self.driver)
            if not self._try_enter_editor_frame():
                self._enter_via_write_button(blog_id)
                if not self._try_enter_editor_frame():
                    self._log_failure("글쓰기 화면 진입")
                    raise RuntimeError(
                        "네이버 글쓰기 화면에 진입하지 못했습니다. 고정 URL·계정 글쓰기 "
                        "주소·글쓰기 버튼이 모두 실패했습니다. 네이버 블로그 화면이 "
                        "바뀌었을 수 있습니다."
                    )
        self._dismiss_draft_popup()
        self._wait_for_any(TITLE_SELECTORS, "제목 칸")
        # 실제로 열린 블로그 주소를 돌려준다. **네이버 아이디와 블로그 주소는 다를 수 있다**
        # (아이디 win-z, 주소 aiona_it). 부르는 쪽이 이걸 기억해 두면 다음부터는 고정
        # 글쓰기 URL로 한 번에 들어간다.
        return self._blog_id_in_url()

    def _blog_id_in_url(self) -> str | None:
        """현재 주소에서 블로그 이름을 읽는다. 확신할 수 없으면 None."""
        from urllib.parse import parse_qs, urlparse

        try:
            parsed = urlparse(self._current_url())
        except Exception:
            return None
        if "blog.naver.com" not in (parsed.netloc or ""):
            return None
        # PostWriteForm.naver?blogId=... 형태에서는 질의 문자열에 들어 있다.
        query_id = (parse_qs(parsed.query or "").get("blogId") or [""])[0].strip()
        if query_id:
            return query_id
        segment = (parsed.path or "").strip("/").split("/")[0]
        # GoBlogWrite.naver처럼 블로그 이름이 아닌 경로는 판단 근거가 되지 못한다.
        if not segment or segment.endswith((".naver", ".nhn")):
            return None
        return segment

    def _try_enter_editor_frame(self) -> bool:
        """에디터 iframe으로 전환을 시도하고, 없으면 예외 대신 False를 돌려준다."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        self.driver.switch_to.default_content()
        try:
            frame = WebDriverWait(self.driver, ELEMENT_TIMEOUT_SECONDS).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, EDITOR_FRAME))
            )
        except Exception:
            return False
        self.driver.switch_to.frame(frame)
        return True

    def _enter_via_write_button(self, blog_id: str) -> None:
        """'글쓰기' 버튼/링크를 눌러 에디터로 진입한다.

        **먼저 지금 떠 있는 화면에서 찾는다.** 글쓰기 URL이 "게시물이 삭제되었거나 다른
        페이지로 변경되었습니다" 안내창으로 막히면, 그 창을 닫은 뒤 네이버는 블로그 홈
        (``section.blog.naver.com``)으로 데려다 놓는다. 그 화면 오른쪽에 이미 '글쓰기'
        버튼이 있다. 그걸 두고 ``blog.naver.com/{blog_id}``로 다시 가면 같은 안내창을 한 번
        더 만나고 제자리로 돌아온다(2026-08-05 실사용).

        지금 화면에 없을 때만 블로그 홈으로 이동해 한 번 더 찾는다.
        """
        button = self._find_write_button()
        if button is not None:
            logger.info(
                "현재 화면의 '글쓰기' 버튼으로 에디터에 들어갑니다: %s",
                safe_url_for_log(self._current_url()),
            )
        else:
            self.driver.get(BLOG_URL.format(blog_id=blog_id))
            _dismiss_stray_alert(self.driver)
            time.sleep(1)
            button = self._find_write_button()
        if button is None:
            self._log_failure("블로그 홈 글쓰기 버튼")
            raise RuntimeError("블로그 홈에서 글쓰기 버튼을 찾지 못했습니다.")
        self._click_button(button, "글쓰기 버튼")
        # 글쓰기가 새 탭/창으로 열리는 경우 그 창으로 전환한다.
        time.sleep(2)
        handles = self.driver.window_handles
        if len(handles) > 1:
            self.driver.switch_to.window(handles[-1])

    def _find_write_button(self):
        """블로그 홈에서 '글쓰기' 버튼/링크를 찾는다. 최상위 문서와 mainFrame을 모두 훑는다."""
        from selenium.webdriver.common.by import By

        text_xpath = "//*[self::a or self::button][contains(normalize-space(.), '글쓰기')]"

        def search() -> object | None:
            for selector in WRITE_BUTTON_SELECTORS:
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    if element.is_displayed() and element.is_enabled():
                        return element
            for element in self.driver.find_elements(By.XPATH, text_xpath):
                if element.is_displayed() and element.is_enabled():
                    return element
            return None

        self.driver.switch_to.default_content()
        found = search()
        if found is not None:
            return found
        # 블로그 홈은 본문을 #mainFrame 안에 렌더링하기도 한다 — 그 안도 확인한다.
        if self._try_enter_editor_frame():
            found = search()
            if found is not None:
                return found
        self.driver.switch_to.default_content()
        return None

    def _current_url(self) -> str:
        try:
            return self.driver.current_url
        except Exception:
            return "(알 수 없음)"

    def _log_failure(self, step: str) -> None:
        """실패한 단계와 현재 URL을 남기고, 화면 컨트롤 후보도 함께 진단으로 남긴다."""
        logger.warning(
            "[%s] 실패 · 현재 URL: %s", step, safe_url_for_log(self._current_url())
        )
        self._log_controls(step)

    def _switch_to_editor_frame(self) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        self.driver.switch_to.default_content()
        try:
            frame = WebDriverWait(self.driver, ELEMENT_TIMEOUT_SECONDS).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, EDITOR_FRAME))
            )
        except Exception as error:
            raise EditorTargetNotFoundError("네이버 SmartEditor iframe을 찾지 못했습니다.") from error
        self.driver.switch_to.frame(frame)

    def _visible_draft_cancel(self):
        from selenium.webdriver.common.by import By

        for xpath in DRAFT_CANCEL_XPATHS:
            for element in self.driver.find_elements(By.XPATH, xpath):
                if element.is_displayed() and element.is_enabled():
                    return element
        return None

    def _dismiss_draft_popup(self) -> bool:
        """임시 작성글 복원 팝업은 취소하고 항상 빈 새 글을 사용한다."""
        from selenium.webdriver.support.ui import WebDriverWait

        deadline = time.monotonic() + DRAFT_POPUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            # 팝업은 현재 SmartEditor iframe 안에 나타나는 것이 일반적이다.
            self._switch_to_editor_frame()
            cancel_button = self._visible_draft_cancel()

            # 네이버 화면 변형에 따라 최상위 문서에 렌더링되는 경우도 함께 처리한다.
            if cancel_button is None:
                self.driver.switch_to.default_content()
                cancel_button = self._visible_draft_cancel()

            if cancel_button is not None:
                cancel_button.click()

                def popup_closed(_driver, button=cancel_button):
                    try:
                        return not button.is_displayed()
                    except Exception:
                        return True

                try:
                    WebDriverWait(self.driver, ELEMENT_TIMEOUT_SECONDS).until(popup_closed)
                except Exception as error:
                    raise RuntimeError("작성 중인 글 팝업의 취소 처리가 완료되지 않았습니다.") from error
                time.sleep(0.5)
                self._switch_to_editor_frame()
                logger.info("작성 중인 글 복원 팝업을 취소하고 새 글 작성을 시작합니다.")
                return True

            time.sleep(0.2)

        self._switch_to_editor_frame()
        return False

    def _wait_for_any(self, selectors: tuple[str, ...], what: str):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        def visible(_driver):
            for selector in selectors:
                for element in _driver.find_elements(By.CSS_SELECTOR, selector):
                    if element.is_displayed() and element.is_enabled():
                        return element
            return False

        try:
            return WebDriverWait(self.driver, ELEMENT_TIMEOUT_SECONDS).until(visible)
        except Exception as error:
            self._log_failure(what)
            raise EditorTargetNotFoundError(
                f"{what}을 찾지 못했습니다. 네이버 에디터 화면이 바뀌었을 수 있습니다."
            ) from error

    def _focus_editor_target(self, selectors: tuple[str, ...], what: str):
        """표시용 wrapper 대신 실제 contenteditable 요소를 찾아 포커스한다."""
        from selenium.webdriver.common.action_chains import ActionChains

        element = self._wait_for_any(selectors, what)
        try:
            editable = self.driver.execute_script(
                """
                const el = arguments[0];
                if (el.matches && el.matches('[contenteditable="true"]')) return el;
                return (el.querySelector && el.querySelector('[contenteditable="true"]')) ||
                       (el.closest && el.closest('[contenteditable="true"]')) || el;
                """,
                element,
            )
        except Exception:
            editable = element

        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", editable
        )
        try:
            ActionChains(self.driver).move_to_element(editable).pause(0.2).click().perform()
        except Exception:
            self.driver.execute_script(
                "arguments[0].focus(); arguments[0].click();", editable
            )
        time.sleep(0.3)
        return editable

    def _paste_from_clipboard(self) -> None:
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        modifier = Keys.COMMAND if platform.system() == "Darwin" else Keys.CONTROL
        ActionChains(self.driver).key_down(modifier).send_keys("v").key_up(modifier).perform()

    def _paste_verified(self, what: str) -> None:
        """붙여넣기 직전, 클립보드가 우리가 넣은 그대로인지 대조하고 Ctrl+V를 보낸다.

        OS 클립보드는 기기 전체에 하나다 — 다른 발행 스레드는 use_os_clipboard 잠금이
        막지만, 사용자의 복사·화면 캡처(Win+Shift+S)·원격 데스크톱 동기화는 잠금 밖이다.
        대조가 어긋나면 붙여넣지 않는다(fail-closed): 엉뚱한 내용(다른 사람 글·캡처
        이미지)이 붙는 것보다 이 시도가 실패하는 것이 낫다. 재시도는 부르는 쪽 몫이다.

        합성 모드에서는 클립보드가 아예 개입하지 않는다 — 담아 둔 내용을 paste
        이벤트로 직접 쏘므로 대조할 것도, Ctrl+V도 없다. 에디터가 이벤트를 거부하면
        SyntheticPasteError가 난다(문서는 그대로다) — auto 모드는 여기서 이 발행을
        클립보드 경로로 갈아타고, 부르는 쪽 재시도가 그 경로로 이어 간다.
        """
        if self._synthetic_active():
            try:
                self._synthetic_paste(what)
            except SyntheticPasteError:
                if paste_mode() == "auto":
                    self._fall_back_to_clipboard(what)
                raise
            return
        if not clipboard_still_holds():
            logger.warning(
                "%s 붙여넣기 직전에 클립보드가 다른 내용으로 바뀌어 있어 붙여넣지 "
                "않습니다 — 발행 중 복사·화면 캡처·원격 데스크톱 클립보드 동기화가 "
                "원인일 수 있습니다.",
                what,
            )
            raise ClipboardOverwrittenError(
                f"{what} 붙여넣기 직전에 클립보드가 다른 내용으로 바뀌었습니다."
            )
        self._paste_from_clipboard()

    def _put_text_on_clipboard(self, text: str, what: str) -> None:
        """제목 등 평문을 클립보드에 올린다. Windows는 OS 클립보드(창 포커스 불필요),
        그 외 OS는 브라우저 Clipboard API로 폴백한다. 합성 모드는 클립보드를 만지지
        않고 다음 _paste_verified가 쏠 내용으로 담아 둔다."""
        if self._synthetic_active():
            self._pending_paste = {"text": text}
            return
        if _os_clipboard_text(text):
            return
        # 폴백: Clipboard API 권한은 blog.naver.com 최상위 문서에 부여되므로 top에서 쓴다.
        self.driver.switch_to.default_content()
        ok = _write_browser_clipboard(self.driver, text)
        self._switch_to_editor_frame()
        if not ok:
            raise RuntimeError(f"{what}을 클립보드에 넣지 못했습니다.")

    def _put_html_on_clipboard(self, html: str, plain_text: str | None = None) -> None:
        """본문 rich HTML을 클립보드에 올린다. Windows는 OS 클립보드(CF_HTML), 그 외는 브라우저.
        합성 모드는 클립보드를 만지지 않는다(_put_text_on_clipboard와 같은 규칙)."""
        if self._synthetic_active():
            self._pending_paste = {"html": html, "text": plain_text}
            return
        if _os_clipboard_html(html, plain_text):
            return
        self.driver.switch_to.default_content()
        try:
            self._write_rich_clipboard(html)
        finally:
            self._switch_to_editor_frame()

    def fill_publish_plan(self, plan: NaverPublishPlan) -> None:
        """발행 계획을 에디터에 입력한다: 제목 → 스캐폴드 1회 붙여넣기 → 앵커 교체.

        본문 텍스트는 스캐폴드로 **단 한 번**만 붙여넣는다. 이미지 사이사이에 텍스트를
        다시 붙이지 않는 것이, 캐럿이 엉뚱한 곳에 있어 이미지가 문장 중간에 끼거나
        소제목 굵기가 다음 문단으로 번지는 문제를 원천적으로 막는 핵심이다.
        실패하면 즉시 예외를 낸다 — 이미지가 빠지거나 순서가 꼬인 글을 발행하지 않는다.
        """
        logger.info(
            "[NAVER_PUBLISH] mode=%s — 에디터 입력 시작 (텍스트 블록 %d개 · 이미지 %d장)",
            self._paste_mode,
            len(plan.expected_text_blocks),
            len(plan.image_anchors),
        )
        self._paste_title(plan.title)
        self._paste_scaffold(plan)
        for anchor in plan.image_anchors:
            self._replace_anchor_with_image(anchor, plan)
        if plan.image_anchors:
            # 교체가 전부 끝난 뒤에 정렬한다 — 교체 도중에 정렬하면 아직 남은 앵커의
            # 클릭 좌표 계산에 끼어들 여지를 만든다.
            self._center_align_images()
        self._clear_clipboard()
        logger.info(
            "[NAVER_PUBLISH] fill=ok mode=%s — 원고 입력 완료 (텍스트 블록 %d개 · 이미지 %d장)",
            self._paste_mode,
            len(plan.expected_text_blocks),
            len(plan.image_anchors),
        )

    def _paste_title(self, title: str) -> None:
        # navigate() 뒤 에디터 iframe 안이다. 클립보드 쓰기는 OS 레벨이라(Windows) 프레임·창
        # 포커스와 무관하고, 붙여넣기만 에디터 요소에 포커스를 준 뒤 Ctrl+V로 한다. 브라우저
        # navigator.clipboard 쓰기는 문서 포커스를 요구해 자동화 창에서 실패했었다.
        #
        # **몇 번 다시 시도한다.** OS 클립보드는 사용자와 공유된다 — 예약 발행은 사용자가
        # PC를 쓰는 동안 뒤에서 돌기 때문에, 클립보드에 넣고 Ctrl+V를 누르는 사이에
        # 사용자가 복사(특히 Win+Shift+S 캡처는 이미지를 올린다)를 하면 우리 제목이
        # 사라진다. 그러면 빈 제목이 붙는다(실사용 2026-08-04: 에디터 '' — 사용자가 그
        # 시각에 화면을 캡처하고 있었다). 매번 클립보드에 다시 넣고, 잘못 들어간 글자가
        # 있어도 전체 선택 뒤 붙여넣어 덮어쓴다.
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait

        expected = normalize_text(title)
        modifier = Keys.COMMAND if platform.system() == "Darwin" else Keys.CONTROL
        for attempt in range(1, TITLE_PASTE_ATTEMPTS + 1):
            # 잠금은 '클립보드에 넣기'부터 '에디터 반영 확인'까지 쥔다. Ctrl+V를 보낸
            # 직후에 풀면, 에디터가 paste를 처리하기 전에 다른 발행이 클립보드를
            # 갈아치울 수 있다 — 반영이 확인될 때까지가 한 덩어리다.
            # (합성 붙여넣기 모드에서는 클립보드를 안 쓰므로 잠금도 없다.)
            with self._paste_guard():
                self._put_text_on_clipboard(title, "제목")
                self._focus_editor_target(TITLE_SELECTORS, "제목 칸")
                # 앞 시도가 남긴(또는 사용자 클립보드에서 들어온) 글자를 덮어쓴다.
                ActionChains(self.driver).key_down(modifier).send_keys("a").key_up(
                    modifier
                ).perform()
                try:
                    try:
                        self._paste_verified("제목")
                    except SyntheticPasteError:
                        if not self._synthetic_active():
                            # auto가 이 발행을 클립보드로 전환했다 — 바깥 except가
                            # 재시도를 돌리고, 다음 시도부터 클립보드 경로다.
                            raise
                        # 순수 synthetic: 제목은 평문이라 신뢰 키 입력(실제 타이핑)으로
                        # 그 자리에서 대체한다. 전체 선택이 잡혀 있어 덮어써지고,
                        # 실제 반영 여부는 아래 대조가 판정한다(fail-closed 유지).
                        logger.warning(
                            "[NAVER_PUBLISH] title_input=synthetic-rejected fallback=typing "
                            "— 제목 합성 붙여넣기가 거부되어 키 입력으로 대체합니다."
                        )
                        ActionChains(self.driver).send_keys(title).perform()
                    WebDriverWait(self.driver, ELEMENT_TIMEOUT_SECONDS).until(
                        lambda _driver: self._title_text() == expected
                    )
                    logger.info("[NAVER_PUBLISH] title_input=ok — 네이버 에디터 제목 입력 완료")
                    return
                except Exception as error:
                    if attempt >= TITLE_PASTE_ATTEMPTS:
                        raise RuntimeError(
                            f"제목을 붙여넣었지만 에디터 제목이 계획과 다릅니다 "
                            f"(에디터: {self._title_text()!r} · 계획: {expected!r}) — "
                            f"{attempt}번 시도했습니다. 발행 중에 복사·화면 캡처를 하면 "
                            "클립보드가 겹쳐 이렇게 될 수 있습니다."
                        ) from error
                    logger.warning(
                        "제목 붙여넣기가 빗나갔습니다(에디터: %r) — 클립보드에 다시 넣고 "
                        "재시도합니다 (%d/%d).",
                        self._title_text(),
                        attempt,
                        TITLE_PASTE_ATTEMPTS,
                    )

    def _paste_scaffold(self, plan: NaverPublishPlan) -> None:
        """본문 전체(앵커 토큰 포함)를 CF_HTML로 한 번에 붙여넣고 DOM 반영을 기다린다.

        제목과 같은 클립보드 겹침(발행 중 사용자의 복사·화면 캡처)에 대비해 다시 시도하되,
        **하나도 안 붙었을 때만** 한다 — 일부가 붙은 채 또 붙여넣으면 본문이 중복된다.
        그때는 지금처럼 실패가 맞다(중복 글을 발행하는 것보다 낫다).
        """
        from selenium.webdriver.support.ui import WebDriverWait

        tokens = [anchor.token for anchor in plan.image_anchors]
        expected = list(plan.expected_text_blocks)

        def scaffold_ready(_driver):
            status = self._scaffold_status(tokens, expected)
            return bool(status and status.get("ok"))

        for attempt in range(1, SCAFFOLD_PASTE_ATTEMPTS + 1):
            # 제목과 같은 이유로, 클립보드 쓰기부터 DOM 반영 확인까지 잠금을 쥔다.
            with self._paste_guard():
                self._put_html_on_clipboard(plan.scaffold_html, plan.scaffold_plain_text)
                self._focus_editor_target(BODY_SELECTORS, "본문 칸")
                try:
                    self._paste_verified("본문 스캐폴드")
                    WebDriverWait(self.driver, SCAFFOLD_PASTE_TIMEOUT_SECONDS).until(
                        scaffold_ready
                    )
                    break
                except SyntheticPasteError:
                    # 이벤트가 거부됐다 — 합성 이벤트는 기본 삽입을 일으키지 않으므로
                    # 문서는 그대로다. auto는 이미 클립보드로 전환됐으니 남은 시도로
                    # 이어 가고, 순수 synthetic은 여기서 명확히 멈춘다(본문은 제목처럼
                    # 키 입력으로 대체할 수 없다 — 서식·HTML 구조가 통째로 사라진다).
                    if not self._synthetic_active() and attempt < SCAFFOLD_PASTE_ATTEMPTS:
                        logger.warning(
                            "[NAVER_PUBLISH] body_paste=synthetic-rejected fallback=clipboard "
                            "— 클립보드 경로로 재시도합니다 (%d/%d).",
                            attempt,
                            SCAFFOLD_PASTE_ATTEMPTS,
                        )
                        continue
                    raise
                except Exception as error:
                    status = self._scaffold_status(tokens, expected) or {}
                    nothing_landed = status.get("missingTexts") == len(expected) and status.get(
                        "missingTokens"
                    ) == len(tokens)
                    if nothing_landed and attempt < SCAFFOLD_PASTE_ATTEMPTS:
                        logger.warning(
                            "본문 스캐폴드가 하나도 붙지 않았습니다 — 클립보드에 다시 넣고 "
                            "재시도합니다 (%d/%d).",
                            attempt,
                            SCAFFOLD_PASTE_ATTEMPTS,
                        )
                        continue
                    raise RuntimeError(
                        "본문 스캐폴드 붙여넣기를 확인하지 못했습니다 "
                        f"(누락 텍스트 블록 {status.get('missingTexts', '?')}개 · "
                        f"누락 앵커 {status.get('missingTokens', '?')}개)"
                    ) from error
        logger.info(
            "[NAVER_PUBLISH] body_paste=ok blocks=%d anchors=%d — 본문 스캐폴드 붙여넣기 완료",
            len(expected),
            len(tokens),
        )

    def _scaffold_status(self, tokens: list[str], expected: list[str]) -> dict | None:
        """붙여넣은 스캐폴드가 DOM에 다 나타났는지 — 누락 개수를 돌려준다."""
        try:
            return self.driver.execute_script(
                """
                const tokens = arguments[0], expected = arguments[1];
                const norm = s => (s || '')
                  .replace(/[\\u200b\\ufeff]/g, '')
                  .replace(/\\u00a0/g, ' ')
                  .replace(/\\s+/g, ' ')
                  .trim();
                const container = document.querySelector('.se-main-container, .se-content');
                if (!container) {
                  return {ok: false, missingTexts: expected.length, missingTokens: tokens.length};
                }
                const text = norm(container.innerText);
                const missingTokens = tokens.filter(t => text.indexOf(t) < 0).length;
                const missingTexts = expected.filter(t => text.indexOf(norm(t)) < 0).length;
                return {
                  ok: missingTokens === 0 && missingTexts === 0,
                  missingTexts: missingTexts,
                  missingTokens: missingTokens,
                };
                """,
                tokens,
                expected,
            )
        except Exception:
            return None

    def _replace_anchor_with_image(
        self, anchor: NaverImageAnchor, plan: NaverPublishPlan | None = None
    ) -> None:
        """앵커 토큰 문단 하나를 실제 이미지 컴포넌트로 바꾼다.

        토큰 문단을 정확히 하나 찾아 전체 선택한 뒤, 이미지 바이트를 클립보드에 올려
        Ctrl+V 한다. 선택 영역을 덮어쓰므로 토큰은 사라지고 그 자리에 네이버가 이미지
        컴포넌트를 만든다(바이트는 네이버 서버로 업로드된다).

        **앞 이미지가 다 로드된 뒤에 시작한다.** 붙여넣은 이미지는 네이버 서버에서 다시
        내려오며 높이가 늘어나고, 표(se-table)는 컴포넌트로 변환되며 더 크게 움직인다.
        그 사이에 다음 앵커를 클릭하면 좌표가 계산된 뒤 문서가 밀려 엉뚱한 곳이 눌린다
        (실발행: 표 뒤 3번째 앵커가 다른 자리에 삽입되어 발행 중단).

        붙여넣기가 어긋나면 되돌리고(Ctrl+Z) 다시 시도한다. 되돌리지 못했거나 재시도가
        모두 실패하면 예외 — 위치가 어긋난 글을 발행하지 않는다.
        """
        from selenium.webdriver.support.ui import WebDriverWait

        count_before = self._image_component_count()

        def replaced(_driver):
            status = self._anchor_status(anchor.token)
            return bool(
                status
                and not status.get("tokenPresent", True)
                and status.get("imageCount", 0) >= count_before + 1
                and status.get("inlineImageCount", 1) == 0
            )

        for attempt in range(1, ANCHOR_PASTE_ATTEMPTS + 1):
            # 앞 이미지·표가 자리를 잡은 뒤에 클릭해야 좌표가 유효하다.
            self._wait_for_images_settled()
            self._select_anchor_token(anchor.token)
            # 이미지 바이트는 시도할 때마다 잠금 안에서 클립보드에 다시 넣는다. 한 번만
            # 넣고 여러 번 붙이면, 시도 사이에 다른 발행·사용자 복사가 끼어들 수 있다.
            paste_error: Exception | None = None
            with self._paste_guard():
                if self._synthetic_active():
                    self._pending_paste = {"image_bytes": anchor.image_bytes}
                elif not _os_clipboard_image(anchor.image_bytes):
                    raise RuntimeError(
                        f"{anchor.index + 1}번째 이미지를 클립보드에 넣지 못했습니다 — "
                        "이미지가 빠진 글을 발행하지 않도록 중단합니다."
                    )
                try:
                    self._paste_verified(f"{anchor.index + 1}번째 이미지")
                    WebDriverWait(self.driver, IMAGE_PASTE_TIMEOUT_SECONDS).until(replaced)
                except Exception as error:
                    paste_error = error
            if paste_error is None:
                break
            if isinstance(paste_error, SyntheticPasteError):
                # 합성 이미지 붙여넣기가 거부됐다 — 합성 이벤트는 기본 삽입을 일으키지
                # 않으므로 문서는 그대로고, 앵커 줄은 아직 선택돼 있다.
                if not self._synthetic_active():
                    # auto — 이 발행은 이미 클립보드로 전환됐다. 남은 시도가 클립보드
                    # 경로(잠금+대조+Ctrl+V)로 같은 앵커를 다시 붙인다.
                    if attempt < ANCHOR_PASTE_ATTEMPTS:
                        continue
                    raise RuntimeError(
                        f"{anchor.index + 1}번째 이미지 합성 붙여넣기가 거부됐고 "
                        f"클립보드 재시도 기회가 남지 않았습니다({attempt}회)."
                    ) from paste_error
                # 순수 synthetic: 업로드 자동화 폴백 — 에디터의 파일 입력에 직접 첨부.
                logger.warning(
                    "[NAVER_PUBLISH] image_paste=failed fallback=upload index=%d — "
                    "합성 이미지 붙여넣기가 거부되어 업로드 자동화로 폴백합니다.",
                    anchor.index + 1,
                )
                if self._upload_image_fallback(anchor, count_before, replaced):
                    logger.info(
                        "[NAVER_PUBLISH] image_upload=ok index=%d — 업로드 폴백으로 이미지를 넣었습니다.",
                        anchor.index + 1,
                    )
                    break
                raise SyntheticImagePasteError(
                    f"{anchor.index + 1}번째 이미지를 합성 붙여넣기로도, 업로드 자동화로도 "
                    "넣지 못했습니다. NAVER_PASTE_MODE=auto(자동 폴백) 또는 clipboard로 "
                    "발행해 주세요."
                ) from paste_error
            if isinstance(paste_error, ClipboardOverwrittenError):
                # 붙여넣기 전에 막았다 — 문서는 그대로다. 클립보드에 다시 넣고 다시 온다.
                if attempt < ANCHOR_PASTE_ATTEMPTS:
                    continue
                raise RuntimeError(
                    f"{anchor.index + 1}번째 이미지 붙여넣기 직전마다 클립보드가 다른 "
                    f"내용으로 바뀌었습니다({attempt}회 시도) — 엉뚱한 내용을 붙이지 "
                    "않도록 발행을 중단합니다."
                ) from paste_error
            status = self._anchor_status(anchor.token) or {}
            misplaced = (
                status.get("imageCount", 0) >= count_before + 1
                and status.get("tokenPresent")
            )
            if misplaced and attempt < ANCHOR_PASTE_ATTEMPTS:
                # 이미지는 생겼는데 토큰이 남았다 = 붙여넣기가 선택 위치가 아니라 다른
                # 곳(에디터 내부 캐럿)에 들어갔다. 되돌릴 수 있으면 되돌리고 다시 건다.
                if self._undo_misplaced_paste(anchor.token, count_before, plan):
                    logger.warning(
                        "%d번째 이미지가 앵커 위치가 아닌 곳에 들어가 되돌렸습니다 "
                        "(시도 %d/%d) — 다시 시도합니다.",
                        anchor.index + 1,
                        attempt,
                        ANCHOR_PASTE_ATTEMPTS,
                    )
                    continue
            if misplaced:
                raise RuntimeError(
                    f"{anchor.index + 1}번째 이미지가 앵커 위치가 아닌 곳에 삽입됐습니다 "
                    f"(토큰 잔존 · 이미지 {status.get('imageCount')}장, "
                    f"재시도 {attempt}회). 에디터가 선택 위치를 무시했습니다 — "
                    "발행을 중단합니다."
                ) from paste_error
            raise RuntimeError(
                f"{anchor.index + 1}번째 이미지 교체를 확인하지 못했습니다 "
                f"(토큰 잔존: {status.get('tokenPresent')} · 이미지 "
                f"{status.get('imageCount')}장, 기대 {count_before + 1}장)"
            ) from paste_error

        logger.info(
            "[NAVER_PUBLISH] image_paste=ok index=%d — 이미지 교체 완료: %s",
            anchor.index + 1,
            anchor.token,
        )
        if anchor.caption:
            self._fill_image_caption(anchor, count_before)

    def _upload_image_fallback(self, anchor: NaverImageAnchor, count_before: int, replaced) -> bool:
        """합성 붙여넣기가 거부된 이미지를 에디터의 파일 입력(input[type=file])으로 넣는다.

        전제: ``_select_anchor_token``으로 앵커 토큰 줄이 선택돼 있다. 순서:

        1. 이미지 파일 입력을 **먼저** 찾는다 — 없으면 문서를 건드리지 않고 False.
        2. 선택된 토큰 줄을 지운다(Delete). 업로드된 이미지는 에디터 캐럿 위치에
           삽입되므로, 토큰이 있던 그 자리가 삽입 지점이 된다.
        3. 이미지 바이트를 임시 파일로 내려 입력에 첨부한다. **업로드 버튼은 클릭하지
           않는다** — OS 파일 선택창이 떠서 자동화가 거기서 멈춘다. 숨은 입력에 직접
           send_keys 하고, 숨겨져 있으면 잠시 보이게 했다가 되돌린다.
        4. 기존 판정(replaced: 토큰 소멸 + 이미지 +1 + 문단 안 이미지 0)을 그대로
           기다린다. 삽입 **위치**는 여기서 못 본다 — 에디터가 캐럿이 아닌 곳(문서 끝
           등)에 넣었다면 발행 직전 validate_publish_plan의 앞뒤 텍스트 대조가 잡는다.
        5. 시간 안에 안 들어오면 지운 토큰을 Ctrl+Z로 복원해 보고, 성공·실패를
           로그로 남긴 뒤 False — 부르는 쪽이 중단한다(fail-closed).
        """
        import tempfile

        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait

        file_input = self._find_image_file_input()
        if file_input is None:
            logger.warning(
                "[NAVER_PUBLISH] image_upload=unavailable index=%d — 이미지 파일 입력"
                "(input[type=file])을 찾지 못해 업로드 폴백을 쓸 수 없습니다.",
                anchor.index + 1,
            )
            return False

        # 선택된 토큰 줄을 지워 캐럿을 그 자리에 남긴다.
        ActionChains(self.driver).send_keys(Keys.DELETE).perform()
        time.sleep(0.2)

        suffix = "." + _image_mime(anchor.image_bytes).split("/")[1]
        handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            handle.write(anchor.image_bytes)
            handle.close()
            if self._attach_file(file_input, handle.name):
                try:
                    WebDriverWait(self.driver, IMAGE_PASTE_TIMEOUT_SECONDS).until(replaced)
                    return True
                except Exception:
                    pass
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass

        # 여기 왔다면 토큰은 지워졌는데 이미지는 확인되지 않았다 — 문서를 되돌려 둔다.
        restored = self._restore_deleted_anchor(anchor.token, count_before)
        logger.warning(
            "[NAVER_PUBLISH] image_upload=failed index=%d restored=%s — 업로드 폴백이 "
            "시간 안에 반영되지 않았습니다.",
            anchor.index + 1,
            restored,
        )
        return False

    def _find_image_file_input(self):
        """문서의 이미지 첨부용 input[type=file]을 찾는다. 없으면 None.

        accept가 이미지를 받는 입력을 우선한다. 클래스 이름에 기대지 않는다 —
        네이버가 이름을 바꿔도 type=file과 accept의 뜻은 남는다.
        """
        try:
            return self.driver.execute_script(
                """
                const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                if (!inputs.length) return null;
                const imagey = inputs.filter(el => {
                  const accept = (el.getAttribute('accept') || '').toLowerCase();
                  return !accept || accept.indexOf('image') >= 0 ||
                         /png|jpe?g|gif|webp/.test(accept);
                });
                return (imagey.length ? imagey : inputs)[0];
                """
            )
        except Exception:
            return None

    def _attach_file(self, file_input, path: str) -> bool:
        """파일 입력에 경로를 첨부한다. 숨은 입력은 잠시 보이게 했다가 원래대로 되돌린다."""
        try:
            original_style = self.driver.execute_script(
                """
                const el = arguments[0];
                const old = el.getAttribute('style');
                el.style.display = 'block';
                el.style.visibility = 'visible';
                el.style.width = '1px';
                el.style.height = '1px';
                el.style.opacity = '0.01';
                return old;
                """,
                file_input,
            )
            try:
                file_input.send_keys(path)
            finally:
                self.driver.execute_script(
                    """
                    const el = arguments[0], old = arguments[1];
                    if (old === null) el.removeAttribute('style');
                    else el.setAttribute('style', old);
                    """,
                    file_input,
                    original_style,
                )
            return True
        except Exception as error:
            logger.warning("이미지 파일 입력에 첨부하지 못했습니다: %s", error)
            return False

    def _restore_deleted_anchor(self, token: str, count_before: int) -> bool:
        """업로드 폴백이 지운 앵커 토큰 줄을 Ctrl+Z로 한 번만 되돌린다. 성공하면 True.

        _undo_misplaced_paste와 같은 신중함이다 — 에디터의 undo 단위는 우리가 정한 것이
        아니므로, 토큰이 돌아오고 이미지 개수가 그대로일 때만 성공으로 본다.
        """
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        modifier = Keys.COMMAND if platform.system() == "Darwin" else Keys.CONTROL
        try:
            ActionChains(self.driver).key_down(modifier).send_keys("z").key_up(
                modifier
            ).perform()
        except Exception:
            return False
        deadline = time.monotonic() + ELEMENT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            status = self._anchor_status(token) or {}
            if status.get("tokenPresent") and status.get("imageCount") == count_before:
                return True
            time.sleep(0.3)
        return False

    def _undo_misplaced_paste(
        self, token: str, count_before: int, plan: NaverPublishPlan | None = None
    ) -> bool:
        """엉뚱한 곳에 들어간 이미지를 Ctrl+Z로 **한 번만** 되돌린다. 성공하면 True.

        되돌리기는 무조건 안전하지 않다 — SmartEditor의 undo 단위를 우리가 측정한 적이
        없어서, 한 번의 Ctrl+Z가 붙여넣기보다 많은 것을 되돌릴 수 있다. 그래서
        **정확히 직전 상태로 돌아왔을 때만** 성공으로 본다:

        - 이미지 개수가 count_before로 되돌아왔고,
        - 아직 교체하지 않은 이 앵커의 토큰이 그대로 남아 있고,
        - 본문 텍스트 블록이 하나도 사라지지 않았다(plan을 받은 경우).

        하나라도 어긋나면 False를 돌려주고 호출부가 발행을 중단한다. 되돌리기를 여러 번
        누르지 않는 것도 같은 이유다 — 본문(스캐폴드)까지 사라지는 것이 최악이다.
        """
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        modifier = Keys.COMMAND if platform.system() == "Darwin" else Keys.CONTROL
        try:
            ActionChains(self.driver).key_down(modifier).send_keys("z").key_up(
                modifier
            ).perform()
        except Exception as error:
            logger.warning("되돌리기(Ctrl+Z)를 보내지 못했습니다: %s", error)
            return False

        deadline = time.monotonic() + ELEMENT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            status = self._anchor_status(token) or {}
            if status.get("imageCount") == count_before:
                if not status.get("tokenPresent"):
                    logger.warning(
                        "되돌리기로 이미지는 사라졌지만 앵커 토큰도 함께 없어졌습니다 "
                        "— 본문이 손상됐을 수 있어 재시도하지 않습니다."
                    )
                    return False
                # 되돌리기가 본문까지 지웠는지 확인한다. Ctrl+Z의 단위는 우리가 정한 것이
                # 아니라 에디터가 정한다 — 이미지 개수만 맞고 문단이 사라졌을 수 있다.
                if plan is not None:
                    scaffold = self._scaffold_status([], list(plan.expected_text_blocks))
                    missing = (scaffold or {}).get("missingTexts")
                    if missing:
                        logger.warning(
                            "되돌리기 후 본문 텍스트 블록 %s개가 사라졌습니다 — "
                            "재시도하지 않습니다.",
                            missing,
                        )
                        return False
                return True
            time.sleep(0.3)
        logger.warning(
            "되돌리기 후에도 이미지 개수가 %d장으로 돌아오지 않았습니다(현재 %d장).",
            count_before,
            self._image_component_count(),
        )
        return False

    def _wait_for_images_settled(self) -> None:
        """본문의 모든 이미지가 로드를 마칠 때까지 기다린다(레이아웃 확정).

        시간 안에 끝나지 않아도 발행을 막지는 않는다 — 깨진 이미지는 발행 직전 검증이
        따로 잡고, 여기서 멈추면 정상적인 글까지 못 나간다. 경고만 남긴다.
        """
        deadline = time.monotonic() + IMAGE_LOAD_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                pending = self.driver.execute_script(
                    """
                    const container = document.querySelector('.se-main-container, .se-content');
                    if (!container) return 0;
                    let pending = 0;
                    for (const img of container.querySelectorAll('img')) {
                      if (!(img.complete && img.naturalWidth > 0)) pending += 1;
                    }
                    return pending;
                    """
                )
            except Exception:
                return
            if not pending:
                return
            time.sleep(0.3)
        logger.warning("본문 이미지 로드가 %d초 안에 끝나지 않았습니다 — 계속 진행합니다.", IMAGE_LOAD_TIMEOUT_SECONDS)

    def _fill_image_caption(self, anchor: NaverImageAnchor, image_position: int) -> bool:
        """방금 넣은 이미지의 '사진 설명' 칸에 캡션(출처)을 입력한다. 성공하면 True.

        웹에서 가져온 사진의 출처는 이 칸에 있어야 한다. 다만 **여기서 실패해도 발행을
        멈추지 않는다** — 출처가 빠진 채 발행되는 것보다 나쁜 것은 다 만든 글이 발행되지
        못하는 것이고, 출처는 미리보기·저장 문서에 그대로 남아 있다. 실패는 경고로 크게
        남긴다(조용히 지나가지는 않는다).

        순서가 중요하다. 캡션 줄은 이미지를 선택하기 전에는 **크기가 0**이라 클릭할 수
        없다(실발행 오류: ``element not interactable ... has no size and location``).
        그래서 이미지를 먼저 클릭해 컴포넌트를 선택한 뒤, 화면에 실제로 보이는 캡션
        요소만 고른다.

        입력은 클립보드 붙여넣기다 — SmartEditor는 내부 캐럿 모델을 따로 들고 있어 JS로
        값만 넣으면 저장 때 사라지고, contenteditable에 send_keys는 요소에 따라 흘린다.
        붙여넣은 뒤 실제로 들어갔는지 읽어서 확인한다.

        ``image_position``은 이 이미지의 0-based 순번이다(교체 직전의 이미지 개수).
        앵커를 문서 순서대로 처리하므로 방금 넣은 것이 그 자리의 이미지다.
        """
        from selenium.webdriver.common.action_chains import ActionChains

        try:
            # 교체 직후에는 컴포넌트가 아직 DOM에 없거나 크기가 0일 수 있다. 캡션 칸과
            # 마찬가지로 **기다린다** — 예전에는 한 번만 찾고 포기해서, 앞쪽 이미지들이
            # 설명을 통째로 잃었다(2026-08-10 실발행: 1·2번째 실패, 3번째만 성공).
            image = self._wait_caption_image(image_position)
            if image is None:
                logger.warning(
                    "%d번째 이미지를 %d초 안에 찾지 못해 사진 설명을 넣지 못했습니다 — "
                    "발행은 계속합니다.",
                    anchor.index + 1,
                    CAPTION_IMAGE_TIMEOUT_SECONDS,
                )
                return False
            # 캡션 줄을 살리려면 컴포넌트가 선택돼 있어야 한다.
            ActionChains(self.driver).move_to_element(image).click().perform()

            # 캡션 칸은 선택 직후 바로 나타나지 않을 수 있다 — 보일 때까지 기다리고,
            # 그래도 없으면 이미지를 한 번 더 클릭해 본다(첫 클릭이 선택으로 이어지지
            # 않은 경우). 실발행에서 캡션이 조용히 빠진 채 발행된 원인이다.
            field = self._wait_caption_field(image_position)
            if field is None:
                ActionChains(self.driver).move_to_element(image).click().perform()
                field = self._wait_caption_field(image_position)
            if field is None:
                logger.warning(
                    "%d번째 이미지의 '사진 설명' 칸을 찾지 못했습니다(%s) — 출처 없이 발행을 "
                    "계속합니다. 네이버 에디터 화면이 바뀌었을 수 있습니다.",
                    anchor.index + 1,
                    self._caption_diagnostics(image_position),
                )
                return False

            ActionChains(self.driver).move_to_element(field).click().perform()
            time.sleep(0.2)  # 실제 클릭으로 에디터 내부 캐럿이 캡션 칸에 오도록 한다.
            pasted = False
            with self._paste_guard():
                if self._synthetic_active():
                    self._pending_paste = {"text": anchor.caption}
                elif not _os_clipboard_text(anchor.caption):
                    logger.warning("사진 설명을 클립보드에 넣지 못했습니다 — 발행은 계속합니다.")
                    return False
                try:
                    self._paste_verified("사진 설명")
                    pasted = self._wait_caption_contains(image_position, anchor.caption)
                except (ClipboardOverwrittenError, SyntheticPasteError):
                    pasted = False  # 붙여넣지 않았다 — 아래 키 입력 폴백이 캡션을 채운다.
            if not pasted:
                # 붙여넣기가 흘렀다 — 캐럿은 캡션 칸에 있으니 실제 키 입력으로 한 번 더.
                # **그냥 치면 안 된다**: 붙여넣기가 사실은 들어갔는데 확인만 늦은
                # 경우, 이미 있는 글자 뒤에 덧붙어 캡션이 두 번 찍힌다(실측
                # 2026-08-03: '…정보 기준…정보 기준'). 줄을 통째로 선택해 덮어쓴다.
                from selenium.webdriver.common.keys import Keys

                ActionChains(self.driver).send_keys(Keys.END).key_down(
                    Keys.SHIFT
                ).send_keys(Keys.HOME).key_up(Keys.SHIFT).send_keys(
                    anchor.caption
                ).perform()
        except Exception as error:
            logger.warning(
                "%d번째 이미지의 사진 설명 입력에 실패했습니다 — 출처 없이 발행을 계속합니다: %s",
                anchor.index + 1,
                error,
            )
            return False

        if not self._wait_caption_contains(image_position, anchor.caption):
            logger.warning(
                "%d번째 이미지의 사진 설명이 반영되지 않았습니다 — 출처 없이 발행을 계속합니다.",
                anchor.index + 1,
            )
            return False
        logger.info("네이버 에디터 사진 설명 입력 완료: %s", anchor.caption)
        return True

    def _wait_caption_image(self, image_position: int):
        """그 자리의 이미지 요소가 화면에 자리를 잡을 때까지 기다린다. 없으면 None."""
        deadline = time.monotonic() + CAPTION_IMAGE_TIMEOUT_SECONDS
        while True:
            image = self._caption_target(image_position, want="image")
            if image is not None:
                return image
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)

    def _wait_caption_field(self, image_position: int):
        """화면에 보이는 '사진 설명' 칸이 나타날 때까지 기다린다. 없으면 None."""
        deadline = time.monotonic() + CAPTION_FIELD_TIMEOUT_SECONDS
        while True:
            field = self._caption_target(image_position, want="caption")
            if field is not None:
                return field
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)

    def _wait_caption_contains(self, image_position: int, caption: str) -> bool:
        """붙여넣은 캡션이 DOM에 반영될 때까지 기다린다. 시간 안에 안 보이면 False."""
        deadline = time.monotonic() + CAPTION_VERIFY_TIMEOUT_SECONDS
        while True:
            if self._caption_contains(image_position, caption):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def _caption_target(self, image_position: int, want: str):
        """이미지 컴포넌트에서 클릭할 대상을 고른다. ``want``는 'image' 또는 'caption'.

        **화면에 실제로 보이는(크기가 있는) 요소만** 돌려준다 — 스마트에디터는 쓰지 않는
        캡션 모듈을 크기 0으로 숨겨 두고, 그것을 클릭하면 셀레니움이 거부한다.
        """
        return self.driver.execute_script(
            """
            const position = arguments[0], want = arguments[1];
            const container = document.querySelector('.se-main-container, .se-content');
            if (!container) return null;
            const components = container.querySelectorAll(
              '.se-component.se-image, .se-component.se-imageStrip');
            const component = components[position];
            if (!component) return null;
            const visible = el => {
              const rect = el.getBoundingClientRect();
              return rect.width > 1 && rect.height > 1;
            };
            component.scrollIntoView({block: 'center'});
            if (want === 'image') {
              for (const img of component.querySelectorAll('img')) {
                if (visible(img)) return img;
              }
              return null;
            }
            // 네이버가 클래스 이름을 여러 번 바꿨다. 알려진 이름을 먼저 훑고, 그래도 없으면
            // '사진 설명' 안내 문구가 붙은 요소를 찾는다 — 클래스가 또 바뀌어도 화면에
            // 보이는 문구는 남는다.
            const selectors = [
              '.se-caption .se-text-paragraph',
              '.se-module-text.se-caption',
              '.se-caption',
              'figcaption',
              '[class*="caption"] .se-text-paragraph',
              '[class*="caption"]',
            ];
            for (const selector of selectors) {
              for (const el of component.querySelectorAll(selector)) {
                if (visible(el)) return el;
              }
            }
            for (const el of component.querySelectorAll('*')) {
              if (el.querySelector('img') || !visible(el)) continue;
              const hint = (el.getAttribute('placeholder') || '') + ' ' + (el.innerText || '');
              if (hint.indexOf('사진 설명') >= 0) return el;
            }
            return null;
            """,
            image_position,
            want,
        )

    def _caption_diagnostics(self, image_position: int) -> str:
        """캡션 칸을 못 찾았을 때 남길 실제 구조. 한 번의 실패로 셀렉터를 고칠 수 있게."""
        try:
            classes = self.driver.execute_script(
                """
                const position = arguments[0];
                const container = document.querySelector('.se-main-container, .se-content');
                if (!container) return [];
                const components = container.querySelectorAll(
                  '.se-component.se-image, .se-component.se-imageStrip');
                const component = components[position];
                if (!component) return [];
                const names = [];
                for (const el of component.querySelectorAll('*')) {
                  const name = el.className;
                  if (typeof name === 'string' && name && names.indexOf(name) < 0) {
                    names.push(name);
                  }
                }
                return names.slice(0, 25);
                """,
                image_position,
            )
        except Exception:
            return "구조 확인 실패"
        return f"컴포넌트 안의 class: {classes}"

    def _caption_contains(self, image_position: int, caption: str) -> bool:
        """붙여넣은 캡션이 실제로 그 이미지의 설명 칸에 들어갔는가."""
        try:
            text = self.driver.execute_script(
                """
                const position = arguments[0];
                const container = document.querySelector('.se-main-container, .se-content');
                if (!container) return '';
                const components = container.querySelectorAll(
                  '.se-component.se-image, .se-component.se-imageStrip');
                const component = components[position];
                return component ? (component.innerText || '') : '';
                """,
                image_position,
            )
        except Exception:
            return False
        return normalize_text(caption) in normalize_text(text or "")

    def _center_align_images(self) -> None:
        """본문 이미지 컴포넌트를 전부 가운데 정렬로 맞춘다(2026-08-07 사용자 결정).

        클립보드로 붙여넣은 이미지는 에디터가 준 기본 정렬(실측: 왼쪽)로 들어간다.
        이미지를 클릭해 컴포넌트를 선택하면 뜨는 도구 막대에서 '가운데 정렬'을 누르고,
        정렬 클래스가 실제로 바뀌었는지 읽어서 확인한다.

        **실패해도 발행을 막지 않는다** — 정렬은 표현이지 내용이 아니고, 이미 넣은
        이미지·캡션은 온전하다. 캡션과 같은 기준으로 경고만 크게 남긴다.
        """
        self._wait_for_images_settled()
        total = self._image_component_count()
        centered = 0
        for position in range(total):
            try:
                if self._center_align_image(position):
                    centered += 1
                else:
                    logger.warning(
                        "%d번째 이미지를 가운데 정렬하지 못했습니다 — 발행은 계속합니다. %s",
                        position + 1,
                        self._align_diagnostics(),
                    )
            except Exception as error:
                logger.warning(
                    "%d번째 이미지 가운데 정렬 중 오류 — 발행은 계속합니다: %s",
                    position + 1,
                    error,
                )
        if centered:
            logger.info("네이버 에디터 이미지 가운데 정렬 완료 (%d/%d장)", centered, total)

    def _center_align_image(self, position: int) -> bool:
        """이미지 하나를 가운데 정렬한다. 이미 가운데면 아무것도 누르지 않는다."""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        if self._image_alignment(position) == "center":
            return True
        image = self._caption_target(position, want="image")
        if image is None:
            return False
        # 컴포넌트를 선택해야 도구 막대가 뜬다(캡션 입력과 같은 순서).
        ActionChains(self.driver).move_to_element(image).click().perform()
        time.sleep(0.3)

        if not self._click_center_align_button():
            # 도구 막대를 못 찾았다(클래스가 바뀌었을 수 있다). 에디터 정렬 단축키로
            # 한 번 더 — 컴포넌트가 선택된 상태의 Ctrl+Shift+E는 그 컴포넌트에 걸린다.
            modifier = Keys.COMMAND if platform.system() == "Darwin" else Keys.CONTROL
            ActionChains(self.driver).key_down(modifier).key_down(Keys.SHIFT).send_keys(
                "e"
            ).key_up(Keys.SHIFT).key_up(modifier).perform()

        deadline = time.monotonic() + CAPTION_VERIFY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._image_alignment(position) == "center":
                return True
            time.sleep(0.25)
        return False

    def _image_alignment(self, position: int) -> str:
        """이미지 컴포넌트의 정렬 상태. 'left'·'center'·'right' 또는 'unknown'.

        스마트에디터는 정렬을 컴포넌트·섹션의 클래스(`…align-center` 형태)로 들고
        있다. 정확한 클래스 이름에 기대지 않고 align-{방향} 꼬리만 읽는다 — 네이버가
        접두어를 바꿔도 살아남게.
        """
        try:
            value = self.driver.execute_script(
                """
                const position = arguments[0];
                const container = document.querySelector('.se-main-container, .se-content');
                if (!container) return 'unknown';
                const components = container.querySelectorAll(
                  '.se-component.se-image, .se-component.se-imageStrip');
                const component = components[position];
                if (!component) return 'unknown';
                for (const el of [component, ...component.querySelectorAll('*')]) {
                  const name = typeof el.className === 'string' ? el.className : '';
                  const match = name.match(/align[-_]?(left|center|right)/i);
                  if (match) return match[1].toLowerCase();
                }
                return 'unknown';
                """,
                position,
            )
        except Exception:
            return "unknown"
        return value or "unknown"

    def _click_center_align_button(self) -> bool:
        """선택된 컴포넌트의 도구 막대에서 '가운데 정렬' 버튼을 찾아 누른다.

        클래스 이름을 못 박지 않는다 — 화면에 실제로 보이는 버튼 중에서 라벨·클래스·
        데이터 속성에 '가운데' 또는 align+center 힌트가 있는 것을 고른다(캡션 셀렉터와
        같은 생존 전략: 이름이 바뀌어도 뜻이 남는 쪽을 본다).
        """
        try:
            button = self.driver.execute_script(
                """
                const visible = el => {
                  const rect = el.getBoundingClientRect();
                  return rect.width > 1 && rect.height > 1;
                };
                for (const el of document.querySelectorAll('button, [role="button"]')) {
                  if (!visible(el)) continue;
                  const hint = [
                    el.getAttribute('data-name'), el.getAttribute('data-value'),
                    el.getAttribute('data-log'), el.getAttribute('aria-label'),
                    el.getAttribute('title'),
                    typeof el.className === 'string' ? el.className : '',
                    el.textContent,
                  ].join(' ');
                  if (/가운데/.test(hint) || (/align/i.test(hint) && /cent/i.test(hint))) {
                    return el;
                  }
                }
                return null;
                """
            )
        except Exception:
            return False
        if button is None:
            return False
        try:
            from selenium.webdriver.common.action_chains import ActionChains

            ActionChains(self.driver).move_to_element(button).click().perform()
            return True
        except Exception as error:
            logger.debug("가운데 정렬 버튼 클릭 실패: %s", error)
            return False

    def _align_diagnostics(self) -> str:
        """정렬 버튼을 못 찾았을 때 남길 실제 도구 막대 구조. 한 번의 실패로 고칠 수 있게."""
        try:
            names = self.driver.execute_script(
                """
                const visible = el => {
                  const rect = el.getBoundingClientRect();
                  return rect.width > 1 && rect.height > 1;
                };
                const names = [];
                for (const el of document.querySelectorAll('button, [role="button"]')) {
                  if (!visible(el)) continue;
                  const name = [
                    typeof el.className === 'string' ? el.className : '',
                    el.getAttribute('aria-label') || '', el.getAttribute('title') || '',
                  ].join('|');
                  if (name.replace(/\\|/g, '') && names.indexOf(name) < 0) names.push(name);
                }
                return names.slice(0, 25);
                """
            )
        except Exception:
            return "도구 막대 구조 확인 실패"
        return f"보이는 버튼: {names}"

    def _select_anchor_token(self, token: str) -> None:
        """토큰 문단을 하나 찾아 **실제 클릭 + 키보드**로 그 줄 전체(=토큰)를 선택한다.

        탐색은 정규화 비교다 — 네이버는 붙여넣은 문단에 zero-width 문자(\\u200b 등)를
        끼워 넣어서, 눈에는 토큰만 보여도 innerText는 토큰과 정확히 일치하지 않는다
        (실발행에서 '0개 발견'으로 터졌던 원인).

        선택은 JS Range/Selection이 아니라 진짜 마우스 클릭 + END·Shift+HOME이다.
        SmartEditor는 내부 캐럿 모델을 따로 들고 있고 실제 입력 이벤트로만 갱신한다 —
        프로그램적으로 DOM Selection만 잡으면 Ctrl+V가 내부 캐럿 위치(스캐폴드 붙여넣기
        직후라 문서 맨 끝)에 이미지를 꽂는다(실발행에서 이미지가 글 끝에 들어간 원인).
        토큰은 한 줄짜리 전용 문단이므로 END → Shift+HOME이 정확히 토큰만 선택한다.
        """
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        for attempt in range(1, ANCHOR_SELECT_ATTEMPTS + 1):
            paragraph = self._find_anchor_paragraph(token)
            # 클릭 좌표는 계산 순간의 위치다. 이미지·표가 아직 자리를 잡는 중이면 클릭이
            # 떨어지는 사이에 문서가 밀려 다른 문단이 눌린다 — 멈출 때까지 기다린다.
            self._wait_for_stable_position(paragraph)
            self._click_paragraph(paragraph)
            ActionChains(self.driver).send_keys(Keys.END).key_down(Keys.SHIFT).send_keys(
                Keys.HOME
            ).key_up(Keys.SHIFT).perform()
            time.sleep(0.2)  # 선택 반영 대기. 성공 판정은 DOM 선택 영역으로 한다.

            selection = self._selection_state(paragraph)
            if _selection_is_on_anchor(selection, token):
                return
            logger.warning(
                "앵커 선택이 이 문단이 아닙니다 (시도 %d/%d · 선택된 글자 %r · 같은 문단 %s) "
                "— 다시 시도합니다.",
                attempt,
                ANCHOR_SELECT_ATTEMPTS,
                str(selection.get("text"))[:40],
                selection.get("inside"),
            )
            time.sleep(0.4)

        raise RuntimeError(
            f"이미지 앵커 토큰을 선택하지 못했습니다 (토큰 {token} · "
            f"{ANCHOR_SELECT_ATTEMPTS}회 시도). 클릭이 다른 문단에 떨어지고 있습니다 — "
            "잘못된 위치에 이미지를 넣지 않도록 중단합니다."
        )

    def _selection_state(self, paragraph) -> dict:
        """지금 캐럿·선택이 **어느 문단에** 있는지 읽는다(판정은 _selection_is_on_anchor).

        붙여넣기 **전에** 확인하는 것이 핵심이다. 붙여넣은 뒤에 알아차리면 이미 엉뚱한
        자리에 이미지가 들어가 있어 되돌리기가 필요하다.
        """
        try:
            return self.driver.execute_script(
                """
                const el = arguments[0];
                const norm = s => (s || '')
                  .replace(/[\\u200b\\ufeff]/g, '')
                  .replace(/\\u00a0/g, ' ')
                  .replace(/\\s+/g, ' ')
                  .trim();
                const sel = document.getSelection();
                if (!sel || sel.rangeCount === 0) {
                  return {text: '', inside: false, collapsed: true, found: false};
                }
                const range = sel.getRangeAt(0);
                const node = range.commonAncestorContainer;
                return {
                  text: norm(sel.toString()),
                  inside: el === node || el.contains(node),
                  collapsed: !!range.collapsed,
                  found: true,
                };
                """,
                paragraph,
            ) or {"text": "", "inside": False, "collapsed": True, "found": False}
        except Exception as error:
            logger.warning("선택 영역을 확인하지 못했습니다: %s", error)
            return {"text": "", "inside": False, "collapsed": True, "found": False}

    def _wait_for_stable_position(self, element) -> None:
        """요소의 화면 위치가 더 이상 움직이지 않을 때까지 기다린다.

        이미지 로드·표 컴포넌트 변환이 끝나도 애니메이션이 남아 있을 수 있다. 두 번
        연속 같은 위치면 멈춘 것으로 본다. 시간 안에 안정되지 않아도 진행한다 —
        실제 선택 성공 여부는 _selection_state가 판정한다.
        """
        deadline = time.monotonic() + LAYOUT_SETTLE_TIMEOUT_SECONDS
        previous = None
        while time.monotonic() < deadline:
            try:
                # 여기서 다시 스크롤하면 안 된다. 매번 화면 중앙으로 맞추면 문서가 아무리
                # 밀려도 top이 늘 같은 값이라, '멈췄는가'를 재는 자가 스스로 답을 만든다.
                # 스크롤은 문단을 찾을 때 한 번만 하고(_find_anchor_paragraph), 여기서는
                # 그 뒤의 움직임만 본다.
                top = self.driver.execute_script(
                    "return Math.round(arguments[0].getBoundingClientRect().top);",
                    element,
                )
            except Exception:
                return
            if not isinstance(top, (int, float)):
                return  # 위치를 잴 수 없으면 기다릴 근거가 없다.
            if previous is not None and top == previous:
                return
            previous = top
            time.sleep(LAYOUT_SETTLE_INTERVAL_SECONDS)

    def _clickable_offset(self, paragraph) -> dict | None:
        """이 문단에 **실제로 닿는** 클릭 지점(중앙 기준 오프셋). 없으면 None.

        중앙 클릭이 늘 문단에 닿는 것이 아니다 — 방금 붙여넣은 큰 이미지 바로 아래
        문단은 이미지 컴포넌트의 오버레이(선택 테두리·캡션 영역)가 중앙을 덮고 있어,
        클릭이 이미지 쪽에 떨어진다(2026-08-07 실발행: 2번째 앵커에서 '클릭이 다른
        문단에 떨어지고 있습니다' 3회로 발행 중단). 그래서 클릭 전에 elementFromPoint로
        그 좌표의 최상단 요소가 이 문단인지 짚어 보고, 덮인 지점은 피한다.
        """
        try:
            point = self.driver.execute_script(
                """
                const el = arguments[0];
                const rect = el.getBoundingClientRect();
                if (rect.width < 2 || rect.height < 2) return null;
                const inside = target => !!target && (el === target || el.contains(target));
                for (const fx of [0.5, 0.15, 0.85, 0.05]) {
                  for (const fy of [0.5, 0.72, 0.28]) {
                    const x = rect.left + rect.width * fx;
                    const y = rect.top + rect.height * fy;
                    if (inside(document.elementFromPoint(x, y))) {
                      return {
                        x: Math.round(x - rect.left - rect.width / 2),
                        y: Math.round(y - rect.top - rect.height / 2),
                      };
                    }
                  }
                }
                return null;
                """,
                paragraph,
            )
        except Exception:
            return None
        # 모양을 검증한다 — 드라이버 대역(테스트)이나 예상 밖 반환이 그대로 흘러가
        # 클릭 경로를 죽이면 안 된다. 이 함수의 실패는 언제나 '중앙 클릭 폴백'이다.
        if (
            isinstance(point, dict)
            and isinstance(point.get("x"), (int, float))
            and isinstance(point.get("y"), (int, float))
        ):
            return point
        return None

    def _click_paragraph(self, paragraph) -> None:
        """앵커 문단을 실제 마우스로 클릭한다. 일시 실패는 한 번 재시도한다."""
        from selenium.webdriver.common.action_chains import ActionChains

        def click_once() -> None:
            # 문단에 실제로 닿는 지점을 골라 누른다. 짚이는 지점이 없으면(전부 덮임)
            # 중앙 클릭으로 폴백한다 — 성공 여부는 어차피 _selection_state가 판정하고,
            # 바깥 재시도가 남아 있다.
            offset = self._clickable_offset(paragraph)
            chain = ActionChains(self.driver)
            if offset:
                chain.move_to_element_with_offset(
                    paragraph, int(offset["x"]), int(offset["y"])
                )
            else:
                chain.move_to_element(paragraph)
            chain.pause(0.1).click().perform()

        try:
            click_once()
        except Exception as error:
            # 실발행에서 element not interactable(크기 0)로 발행 전체가 죽은 적이 있다 —
            # 레이아웃이 아직 안정되지 않은 순간의 일시 실패다. 다시 스크롤해 한 번만
            # 재시도한다. JS 클릭 폴백은 쓰지 않는다: 에디터 내부 캐럿은 실제 클릭으로만
            # 이 문단에 온다(폴백으로 넘어가면 이미지가 엉뚱한 위치에 꽂힌다).
            logger.warning("앵커 문단 클릭 실패 — 다시 스크롤해 한 번 재시도합니다: %s", error)
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", paragraph
            )
            time.sleep(0.5)
            click_once()
        time.sleep(0.2)  # 실제 클릭으로 에디터 내부 캐럿이 이 문단에 오도록 한다.

    def _find_anchor_paragraph(self, token: str):
        """토큰만 든 전용 문단을 정확히 하나 찾아 돌려준다. 아니면 예외."""
        result = self.driver.execute_script(
            """
            const token = arguments[0];
            const norm = s => (s || '')
              .replace(/[\\u200b\\ufeff]/g, '')
              .replace(/\\u00a0/g, ' ')
              .replace(/\\s+/g, ' ')
              .trim();
            const container = document.querySelector('.se-main-container, .se-content');
            if (!container) return {ok: false, count: 0, error: 'no-container'};
            const seen = new Set();
            const candidates = [];
            const selectors = ['.se-text-paragraph', '[contenteditable="true"] p'];
            for (const selector of selectors) {
              for (const el of container.querySelectorAll(selector)) {
                if (seen.has(el)) continue;
                seen.add(el);
                if (norm(el.innerText).indexOf(token) >= 0) candidates.push(el);
              }
            }
            // 자손 문단이 이미 후보면 조상 요소는 뺀다 (중첩 이중 매칭 방지).
            const hits = candidates.filter(
              el => !candidates.some(other => other !== el && el.contains(other))
            );
            if (hits.length !== 1) return {ok: false, count: hits.length};
            const el = hits[0];
            el.scrollIntoView({block: 'center'});
            return {ok: true, count: 1, dedicated: norm(el.innerText) === token, element: el};
            """,
            token,
        )
        if not result or not result.get("ok") or result.get("element") is None:
            count = (result or {}).get("count", 0)
            detail = (result or {}).get("error")
            raise RuntimeError(
                f"이미지 앵커 문단을 정확히 하나 찾지 못했습니다 "
                f"(토큰 {token} · {count}개 발견{f' · {detail}' if detail else ''})"
            )
        if not result.get("dedicated"):
            # 줄 선택(END→Shift+HOME)은 전용 문단에서만 안전하다. 토큰이 다른 텍스트와
            # 한 문단에 병합됐다면 이웃 글자까지 이미지로 덮어써 본문을 잃는다 — 중단.
            raise RuntimeError(
                f"이미지 앵커 문단에 토큰 외의 텍스트가 섞여 있습니다 (토큰 {token}). "
                "네이버가 문단을 병합한 것으로 보입니다 — 본문 손실을 막기 위해 중단합니다."
            )

        return result["element"]

    def _anchor_status(self, token: str) -> dict | None:
        try:
            return self.driver.execute_script(
                """
                const token = arguments[0];
                const norm = s => (s || '')
                  .replace(/[\\u200b\\ufeff]/g, '')
                  .replace(/\\u00a0/g, ' ')
                  .replace(/\\s+/g, ' ')
                  .trim();
                const container = document.querySelector('.se-main-container, .se-content');
                if (!container) return null;
                return {
                  // zero-width가 끼면 원문 indexOf는 놓친다 — 정규화한 텍스트에서 찾는다.
                  tokenPresent: norm(container.innerText).indexOf(token) >= 0,
                  imageCount: container.querySelectorAll(
                    '.se-component.se-image, .se-component.se-imageStrip').length,
                  inlineImageCount: container.querySelectorAll(
                    '.se-component.se-text img').length,
                };
                """,
                token,
            )
        except Exception:
            return None

    def _image_component_count(self) -> int:
        try:
            return int(
                self.driver.execute_script(
                    """
                    const container = document.querySelector('.se-main-container, .se-content');
                    if (!container) return 0;
                    return container.querySelectorAll(
                      '.se-component.se-image, .se-component.se-imageStrip').length;
                    """
                )
                or 0
            )
        except Exception:
            return 0

    def validate_publish_plan(self, plan: NaverPublishPlan) -> None:
        """발행 직전 DOM을 계획과 대조한다. 하나라도 어긋나면 예외 — 발행하면 안 된다."""
        summary = self._editor_summary()
        if summary is None:
            raise RuntimeError("에디터 본문 컨테이너를 찾지 못해 발행 전 검증을 할 수 없습니다.")
        try:
            _check_publish_plan(plan, summary)
        except RuntimeError as error:
            # 진단은 개수·URL·사유 분류만 남긴다 — 본문 텍스트·이미지 데이터는 로그에 싣지
            # 않는다. 사유 분류가 없으면 "이미지도 토큰도 블록도 맞는데 실패"만 남아
            # 무엇이 걸렸는지 알 수 없다(2026-08-10 실발행). 끼어듦·깨짐·폭초과 개수도
            # 그 줄에 함께 낸다 — 셋 다 이 줄에서 보이지 않아 용의선상에 오르지 못했다.
            kinds = getattr(error, "kinds", ())
            logger.warning(
                "발행 전 검증 실패 · URL %s · 이미지 %s/%d장"
                "(끼어듦 %s · 깨짐 %s · 폭초과 %s) · 토큰 잔존 %s개 · 블록 %d개 · 사유: %s",
                safe_url_for_log(self._current_url()),
                summary.get("imageCount"),
                len(plan.image_anchors),
                summary.get("inlineImageCount"),
                summary.get("brokenImageCount"),
                summary.get("oversizedImageCount"),
                summary.get("tokenCount"),
                len(summary.get("items") or []),
                _summarize_kinds(kinds),
            )
            raise
        logger.info(
            "[NAVER_PUBLISH] verify=ok images=%d blocks=%d — 발행 전 검증 통과",
            len(plan.image_anchors),
            len(plan.expected_text_blocks),
        )

    def _editor_summary(self) -> dict | None:
        """검증용 DOM 요약: 제목·컴포넌트 순서·이미지 수·잔존 토큰·문단 굵기."""
        try:
            summary = self.driver.execute_script(
                _EDITOR_SUMMARY_SCRIPT, ANCHOR_TOKEN_PATTERN.pattern
            )
        except Exception as error:
            logger.warning("에디터 상태 요약 실패: %s", error)
            return None
        return summary if isinstance(summary, dict) else None

    def _title_text(self) -> str:
        try:
            raw = self.driver.execute_script(
                """
                const root = document.querySelector('.se-documentTitle, .se-title-text');
                return root ? (root.innerText || '') : '';
                """
            )
        except Exception:
            return ""
        text = normalize_text(raw or "")
        return "" if text == "제목" else text  # 빈 제목 칸은 placeholder '제목'을 보여준다

    def _clear_clipboard(self) -> None:
        """보안: 발행 내용이 클립보드에 남지 않게 비운다 (OS 우선, 실패 시 브라우저).

        합성 모드는 클립보드에 아무것도 넣지 않았다 — 비우겠다고 빈 값을 쓰면 오히려
        서버에서 작업 중인 사람의 클립보드를 지우는 셈이라, 아무것도 하지 않는다.
        (auto가 도중에 클립보드로 갈아탔다면 그때부터는 클립보드를 썼으므로 비운다.)
        """
        if self._synthetic_active():
            return
        if not _os_clipboard_text(""):
            self.driver.switch_to.default_content()
            try:
                _write_browser_clipboard(self.driver, "")
            finally:
                self._switch_to_editor_frame()

    def _write_rich_clipboard(self, html: str) -> None:
        self.driver.set_script_timeout(15)
        result = self.driver.execute_async_script(
            """
            const html = arguments[0], done = arguments[arguments.length - 1];
            (async () => {
              try {
                const item = new ClipboardItem({
                  'text/html': new Blob([html], {type: 'text/html'}),
                  'text/plain': new Blob([html], {type: 'text/plain'})
                });
                await navigator.clipboard.write([item]);
                done({ok: true});
              } catch (error) {
                done({ok: false, error: String(error)});
              }
            })();
            """,
            html,
        )
        if not result or not result.get("ok"):
            reason = (result or {}).get("error", "unknown clipboard error")
            raise RuntimeError(f"HTML을 Chrome 클립보드에 넣지 못했습니다: {reason}")

    def save_draft(self, tags: list[str] | None = None) -> bool:
        """임시저장: 발행과 똑같이 진행하되, 마지막에 발행 대신 '저장'을 누른다.

        순서는 발행(publish)과 같다.

        1. 상단 바의 **발행**을 눌러 발행 패널을 연다 — 태그 편집 칸은 이 패널 안에만 있다.
        2. 태그를 넣는다.
        3. 패널 아래의 '발행' 확정 버튼 대신, 상단 바의 **저장**을 누른다.

        패널이 열려도 상단 바는 그대로 남아 있어 저장 버튼을 계속 누를 수 있다. 그래서
        태그까지 담은 채로 임시저장이 된다. 발행과 다른 것은 마지막에 누르는 버튼 하나뿐이다.

        저장·발행은 같은 상단 바에 있으므로 발행과 **같은 프레임**에서 찾는다. 예전에는
        여기서만 ``switch_to.default_content()``로 에디터 iframe 밖에 나가 버튼을 찾아,
        같은 자리에 있는 버튼을 영영 만나지 못했다.

        패널 아래의 '발행' 확정 버튼은 어떤 경우에도 누르지 않는다 — 임시저장을 부른
        사람이 글이 발행되는 것을 기대하지는 않기 때문이다.
        """
        from selenium.webdriver.common.by import By

        # 1~2. 태그를 넣으려면 발행 패널이 열려 있어야 한다. 태그는 비치명적이라, 패널을
        # 열지 못해도 본문만이라도 저장되도록 그냥 넘어간다(발행에서도 같은 태도다).
        cleaned = [t for t in (tags or []) if t and t.strip()]
        if cleaned:
            try:
                opener = self._wait_for_any(PUBLISH_OPEN_SELECTORS, "발행 버튼")
                self._click_button(opener, "발행 버튼(태그 입력용 패널 열기)")
                time.sleep(1)
                self._enter_tags(cleaned)
            except Exception as error:
                logger.warning("임시저장용 태그 입력 실패(본문만 저장합니다): %s", error)

        button = None
        try:
            button = self._wait_for_any(TEMP_SAVE_SELECTORS, "저장(임시저장) 버튼")
        except Exception:
            # 선택자가 안 맞을 때의 폴백: 상단 바에서 '저장'이라고 적힌 버튼을 글자로 찾는다.
            # 버튼에는 저장된 글 수가 함께 붙어 있어("저장 1") 정확히 일치하지는 않는다.
            for element in self.driver.find_elements(
                By.XPATH, "//*[self::button or self::a][contains(normalize-space(.), '저장')]"
            ):
                if element.is_displayed() and "발행" not in (element.text or ""):
                    button = element
                    break

        if button is None or "발행" in (button.text or ""):
            self._log_failure("임시저장")
            return False

        self._click_button(button, "저장(임시저장) 버튼")
        logger.info("네이버 에디터 저장(임시저장) 버튼 클릭 완료")
        time.sleep(3)
        return True

    def _enter_tags(self, tags: list[str]) -> None:
        """발행 패널이 열린 상태에서 네이버 '태그' 입력창에 태그를 넣는다.

        해시태그를 본문에 파란 글씨로 넣는 대신 여기서 진짜 네이버 태그로 등록한다.
        발행 패널은 발행 버튼을 누른 프레임(에디터 iframe) 안에 뜨므로 현재 프레임에서
        태그 입력창을 찾는다. 태그 입력은 비치명적이라 실패해도 발행은 계속한다.
        """
        from selenium.webdriver.common.keys import Keys

        cleaned = [t.lstrip("#").strip() for t in (tags or []) if t and t.lstrip("#").strip()]
        if not cleaned:
            return
        try:
            tag_input = self.driver.execute_script(
                """
                var inputs = document.querySelectorAll('input');
                for (var i = 0; i < inputs.length; i++) {
                  var el = inputs[i];
                  var hay = ((el.placeholder||'') + ' ' + (el.name||'') + ' ' +
                             (el.className||'') + ' ' + (el.getAttribute('aria-label')||'') +
                             ' ' + (el.id||'')).toLowerCase();
                  if (hay.indexOf('태그') >= 0 || hay.indexOf('tag') >= 0) return el;
                }
                return null;
                """
            )
        except Exception as error:
            logger.warning("네이버 태그 입력창 탐색 실패(건너뜀): %s", error)
            return
        if tag_input is None:
            logger.warning("네이버 태그 입력창을 찾지 못해 태그를 건너뜁니다: %s", cleaned)
            return
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", tag_input
            )
            time.sleep(0.3)
            for tag in cleaned[:30]:  # 네이버 태그는 최대 30개
                tag_input.send_keys(tag)
                tag_input.send_keys(Keys.ENTER)
                time.sleep(0.3)
            logger.info("네이버 태그 입력 완료: %s", cleaned)
        except Exception as error:
            logger.warning("네이버 태그 입력 실패(건너뜀): %s", error)

    def publish(self, tags: list[str] | None = None) -> str:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        opener = self._wait_for_any(PUBLISH_OPEN_SELECTORS, "발행 버튼")
        self._click_button(opener, "발행 버튼")
        logger.info("네이버 에디터 발행 버튼 클릭 완료")
        time.sleep(1)

        # 발행 패널이 열린 상태에서 태그를 먼저 넣고 확정한다.
        self._enter_tags(tags or [])

        def confirmation(_driver):
            for selector in PUBLISH_CONFIRM_SELECTORS:
                for element in _driver.find_elements(By.CSS_SELECTOR, selector):
                    text = (element.text or "").strip()
                    if element.is_displayed() and (text == "발행" or "발행" in text):
                        return element
            for element in _driver.find_elements(By.XPATH, "//button[normalize-space()='발행']"):
                if element.is_displayed() and element != opener:
                    return element
            return False

        try:
            confirm = WebDriverWait(self.driver, ELEMENT_TIMEOUT_SECONDS).until(confirmation)
        except Exception as error:
            self._log_failure("발행 확인")
            raise RuntimeError("발행 확인 버튼을 찾지 못했습니다.") from error
        self._click_button(confirm, "발행 확인 버튼")
        logger.info("네이버 에디터 최종 발행 버튼 클릭 완료")

        deadline = time.monotonic() + PAGE_LOAD_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(1)
            url = self.driver.current_url
            if "Redirect=Write" not in url and "/postwrite" not in url.lower():
                logger.info("[NAVER_PUBLISH] publish=ok url=%s", safe_url_for_log(url))
                return url
        self._log_failure("발행 후 글 주소 이동")
        raise RuntimeError("발행을 눌렀지만 글 주소로 이동하지 않았습니다. 브라우저에서 확인해 주세요.")

    def _click_button(self, element, what: str) -> None:
        from selenium.webdriver.common.action_chains import ActionChains

        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element
            )
            ActionChains(self.driver).move_to_element(element).pause(0.2).click().perform()
        except Exception:
            try:
                self.driver.execute_script("arguments[0].click();", element)
            except Exception as fallback_error:
                raise RuntimeError(f"{what}을 클릭하지 못했습니다.") from fallback_error

    def _log_controls(self, context: str) -> None:
        try:
            controls = self.driver.execute_script(
                """
                return Array.from(document.querySelectorAll('button,a,[role="button"]'))
                  .filter(el => { const r=el.getBoundingClientRect(); return r.width && r.height; })
                  .map(el => (el.innerText || el.getAttribute('aria-label') || '').trim())
                  .filter(Boolean).slice(0, 80);
                """
            )
            logger.warning("[%s] 화면 컨트롤 후보: %s", context, controls)
        except Exception as error:
            logger.debug("화면 컨트롤 진단 실패: %s", error)


# 발행 전 검증용 DOM 요약. 컴포넌트를 문서 순서대로 훑어 이미지/텍스트 항목 목록과
# 이미지 상태(수·본문 폭 초과·로드 실패), 잔존 앵커 토큰 수, 제목 텍스트를 모은다.
# 문단 굵기는 computed style로 판정한다 — 클래스 이름은 네이버가 바꿔도 굵기는 굵기다.
_EDITOR_SUMMARY_SCRIPT = """
const tokenPattern = arguments[0];
const norm = s => (s || '')
  .replace(/[\\u200b\\ufeff]/g, '')
  .replace(/\\u00a0/g, ' ')
  .replace(/\\s+/g, ' ')
  .trim();
const container = document.querySelector('.se-main-container, .se-content');
if (!container) return null;
const containerWidth = container.getBoundingClientRect().width;
const items = [];
let imageCount = 0, inlineImageCount = 0, oversizedImageCount = 0, brokenImageCount = 0;
for (const component of container.querySelectorAll('.se-component')) {
  const classes = component.className || '';
  if (classes.indexOf('se-documentTitle') >= 0) continue;
  const isImage = classes.indexOf('se-image') >= 0 || classes.indexOf('se-imageStrip') >= 0;
  if (isImage) {
    imageCount += 1;
    for (const img of component.querySelectorAll('img')) {
      if (!(img.complete && img.naturalWidth > 0)) brokenImageCount += 1;
      if (containerWidth && img.getBoundingClientRect().width > containerWidth + 2) {
        oversizedImageCount += 1;
      }
    }
    items.push({type: 'image'});
    continue;
  }
  if (classes.indexOf('se-text') >= 0 && component.querySelector('img')) inlineImageCount += 1;
  const paragraphs = component.querySelectorAll('.se-text-paragraph');
  if (!paragraphs.length) {
    const text = norm(component.innerText);
    if (text) items.push({type: 'text', text: text, allBold: false});
    continue;
  }
  const isTable = classes.indexOf('se-table') >= 0;
  for (const paragraph of paragraphs) {
    const text = norm(paragraph.innerText);
    if (!text) continue;
    // 표 안의 글은 굵기 검사에서 뺀다 — 네이버는 표 머리글을 항상 굵게 그린다.
    const inTable = isTable || !!paragraph.closest('td, th, table, .se-tableCell');
    let allBold = true;
    const walker = document.createTreeWalker(paragraph, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
      if (!norm(node.nodeValue)) continue;
      let element = node.parentElement, bold = false;
      while (element && element !== paragraph.parentElement) {
        const weight = window.getComputedStyle(element).fontWeight;
        if (weight === 'bold' || weight === 'bolder' || parseInt(weight, 10) >= 600) {
          bold = true;
          break;
        }
        element = element.parentElement;
      }
      if (!bold) { allBold = false; break; }
    }
    items.push({type: 'text', text: text, allBold: allBold, inTable: inTable});
  }
}
const bodyText = norm(container.innerText);
const tokenMatches = bodyText.match(new RegExp(tokenPattern, 'g'));
const titleRoot = document.querySelector('.se-documentTitle, .se-title-text');
return {
  title: titleRoot ? norm(titleRoot.innerText) : '',
  tokenCount: tokenMatches ? tokenMatches.length : 0,
  imageCount: imageCount,
  inlineImageCount: inlineImageCount,
  oversizedImageCount: oversizedImageCount,
  brokenImageCount: brokenImageCount,
  items: items,
};
"""


def _summarize_kinds(kinds: tuple[str, ...] | list[str]) -> str:
    """사유 분류를 '텍스트 블록 누락 2건, 굵기 번짐' 처럼 센다. 같은 사유가 여러 번 나온다."""
    if not kinds:
        return "알 수 없음"
    counted: dict[str, int] = {}
    for kind in kinds:
        counted[kind] = counted.get(kind, 0) + 1
    return ", ".join(kind if count == 1 else f"{kind} {count}건" for kind, count in counted.items())


class PublishPlanMismatch(RuntimeError):
    """발행 전 검증 실패.

    예외 문구에는 어긋난 자리를 알아보라고 본문 조각이 들어간다. 그 문구는 운영 로그에
    실을 수 없다(본문 유출). 그래서 **본문이 섞이지 않는 사유 분류**를 따로 들고 다닌다 —
    로그에는 이것만 남긴다. 예전에는 로그에 예외 **타입**만 찍혀(`RuntimeError`) 무엇이
    걸렸는지 알 길이 없었다(2026-08-10 실발행: 이미지 3/3·토큰 0·블록 40인데 실패).

    ``RuntimeError``를 그대로 물려받는다 — 이 예외를 잡던 자리는 손대지 않아도 된다.
    """

    def __init__(self, message: str, kinds: tuple[str, ...]) -> None:
        super().__init__(message)
        self.kinds = kinds


# 발행 전 검증 실패의 공식 이름(2026-08-19 지침의 권장 이름). 기존 코드·테스트는
# PublishPlanMismatch로 잡는다 — 같은 클래스다.
PublishVerificationError = PublishPlanMismatch


def _check_publish_plan(plan: NaverPublishPlan, summary: dict) -> None:
    """DOM 요약(_editor_summary)을 계획과 대조하는 순수 함수. 어긋난 항목을 모아 예외로 낸다.

    브라우저 없이도 검증 규칙을 테스트할 수 있도록 셀레니움과 분리돼 있다.
    """
    problems: list[str] = []
    # problems와 짝을 이루는 사유 분류. 본문 조각이 절대 들어가지 않는 고정 어휘다.
    kinds: list[str] = []

    def fail(kind: str, detail: str) -> None:
        kinds.append(kind)
        problems.append(detail)

    title = normalize_text(summary.get("title") or "")
    if title != plan.title:
        fail("제목 불일치", f"제목이 계획과 다릅니다 (에디터: {title!r} · 계획: {plan.title!r})")

    token_count = int(summary.get("tokenCount") or 0)
    if token_count:
        fail("앵커 토큰 잔존", f"이미지 앵커 토큰 {token_count}개가 본문에 남아 있습니다")

    image_count = int(summary.get("imageCount") or 0)
    if image_count != len(plan.image_anchors):
        fail(
            "이미지 수 불일치",
            f"이미지 수가 다릅니다 (계획 {len(plan.image_anchors)}장 · 실제 {image_count}장)",
        )
    inline_images = int(summary.get("inlineImageCount") or 0)
    if inline_images:
        fail("문단 안 이미지", f"텍스트 문단 안에 끼어든 이미지가 {inline_images}개 있습니다")
    broken_images = int(summary.get("brokenImageCount") or 0)
    if broken_images:
        fail("깨진 이미지", f"로드가 끝나지 않았거나 깨진 이미지가 {broken_images}개 있습니다")
    oversized_images = int(summary.get("oversizedImageCount") or 0)
    if oversized_images:
        fail("폭 초과 이미지", f"본문 폭을 넘는 이미지가 {oversized_images}개 있습니다")

    # 본문 텍스트를 문서 순서대로 이어 붙이고, 각 이미지 컴포넌트의 문자 위치를 기억한다.
    # 항목 사이 구분자는 공백 — 네이버가 문단을 쪼개도 이어 붙인 텍스트에서는 찾아진다.
    document_text = ""
    image_offsets: list[int] = []
    text_items: list[tuple[str, bool, bool]] = []
    for item in summary.get("items") or []:
        if item.get("type") == "image":
            image_offsets.append(len(document_text))
            continue
        text = normalize_text(item.get("text") or "")
        if not text:
            continue
        # inTable은 나중에 생긴 필드다. 없으면 표가 아닌 것으로 본다(예전 동작).
        text_items.append((text, bool(item.get("allBold")), bool(item.get("inTable"))))
        document_text += text + " "

    if text_items and plan.title and text_items[0][0] == plan.title:
        fail("제목 본문 중복", "제목이 본문 첫 블록에 중복으로 들어갔습니다")

    # 모든 기대 텍스트 블록이 같은 순서로 존재해야 한다.
    position = 0
    for expected in plan.expected_text_blocks:
        found = document_text.find(expected, position)
        if found < 0:
            if document_text.find(expected) < 0:
                fail("텍스트 블록 누락", f"본문에서 찾지 못한 텍스트 블록: {expected[:40]!r}")
            else:
                fail("텍스트 블록 순서", f"순서가 어긋난 텍스트 블록: {expected[:40]!r}")
            continue
        position = found + len(expected)

    # 이미지 위치: 각 앵커의 앞뒤 텍스트가 실제로 이미지 앞/뒤에 있어야 한다.
    if len(image_offsets) == len(plan.image_anchors):
        for anchor, offset in zip(plan.image_anchors, image_offsets, strict=True):
            previous_text = anchor.expected_previous_text
            if previous_text and previous_text not in document_text[:offset]:
                fail(
                    "이미지 앞 텍스트 없음",
                    f"{anchor.index + 1}번째 이미지 앞에 있어야 할 텍스트가 없습니다: "
                    f"{previous_text[:30]!r}",
                )
            next_text = anchor.expected_next_text
            if next_text and next_text not in document_text[offset:]:
                fail(
                    "이미지 뒤 텍스트 없음",
                    f"{anchor.index + 1}번째 이미지 뒤에 있어야 할 텍스트가 없습니다: "
                    f"{next_text[:30]!r}",
                )

    # 소제목 굵기 번짐: 일반 본문 문단이 통째로 굵게 렌더링되면 안 된다.
    # (문단 안의 의도된 부분 강조는 allBold가 아니므로 걸리지 않는다.)
    #
    # 표 안의 글은 보지 않는다. 네이버는 표 머리글을 항상 굵게 그리는데, 머리글이 짧으면
    # ('구분' 같은 한 단어) 본문 문단 어딘가에 그 글자가 들어 있다는 이유만으로 걸렸다 —
    # 실발행이 "일반 본문 문단이 통째로 굵게 표시됩니다: '구분'"으로 멈춘 원인이다.
    for text, all_bold, in_table in text_items:
        if not all_bold or in_table:
            continue
        if any(plain in text or text in plain for plain in plan.plain_paragraph_texts):
            fail("굵기 번짐", f"일반 본문 문단이 통째로 굵게 표시됩니다: {text[:40]!r}")

    if problems:
        raise PublishPlanMismatch(
            "발행 전 검증 실패 — " + " / ".join(problems), tuple(kinds)
        )
