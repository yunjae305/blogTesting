"""스레드(Threads) 브라우저 발행 — 네이버처럼 사용자가 자기 계정으로 로그인해 쓴다.

API 방식(threads.py)은 Meta 앱 검수 전에는 Threads Tester로 초대한 계정만 연결할 수
있어 "서비스 이용자 모두"가 불가능하다. 이 경로는 사용자별 Chrome 프로필에 로그인
세션을 유지하고(네이버와 같은 구조), threads.net을 직접 조작해 발행한다.

Meta의 DOM은 빌드마다 바뀌는 난독화 클래스라 클래스 셀렉터를 쓸 수 없다. 그래서
조작 지점을 최소로 줄인다:
  1. 공식 공유 인텐트 URL(/intent/post?text=…)로 작성창을 **텍스트가 채워진 채** 연다
     — 작성창 열기·텍스트 입력을 DOM 조작 없이 끝낸다.
  2. 남는 조작은 '게시' 버튼 클릭 하나다(문구·역할 기반 탐색).
  3. 게시 후 프로필에서 글이 실제로 보이는지 확인해야 성공이다(fail-closed).

로그인: 설정에 저장한 스레드 자격증명(사용자 이름·전화번호·이메일 중 하나 + 비밀번호,
네이버와 같은 DPAPI 로컬 보관)이 있으면 로그인 폼에 자동 입력한다. Meta가 2단계 인증을
요구하면 **앱 화면에 코드를 물어보고 대신 입력한다**(posting/verification.py) — 예전에는
사용자가 이 창을 직접 만져야 해서 사람이 없는 예약 발행이 불가능했다. 통과하면 세션이
프로필에 남아 다음부터는 로그인 단계를 통째로 건너뛴다.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from app.shared import PostingMethod, PostingResultStatus

from .credentials import (
    forget_session_account,
    remember_session_account,
    session_account,
)
from .browser_reaper import mark_kept_open
from .live_view import hub as live_view_hub
from .naver.browser import (
    close_browser,
    _create_driver,
    _in_browser_thread,
    _NeedsHuman,
    _profile_lock,
)
from .publisher import PublishJob, PublishResult
from .threads_split import ThreadPiece, decode_data_url, publish_pieces_for
from .url_safety import safe_url_for_log

logger = logging.getLogger(__name__)

THREADS_ORIGIN = "https://www.threads.net"
# 로그인 완료를 기다리는 시간 — 사람이 아이디·비밀번호(때로 2단계 인증)를 칠 시간이다.
LOGIN_TIMEOUT_SECONDS = 180
COMPOSER_TIMEOUT_SECONDS = 30
# 로그인 폼(비밀번호 칸)이 렌더될 때까지 기다리는 시간. threads.net → threads.com
# 리다이렉트와 React 렌더가 모두 이 안에서 끝나야 한다.
LOGIN_FORM_TIMEOUT_SECONDS = 20.0
PUBLISH_CONFIRM_TIMEOUT_SECONDS = 30
# 작성창이 닫힌 뒤 브라우저가 나머지를 다 올릴 때까지 두는 시간. 작성창이 닫히는 것은
# "접수됐다"이지 "다 올라갔다"가 아니다 — 여기서 안 기다리고 페이지를 떠나면 아직
# 날아가지 않은 요청이 취소된다(_let_the_publish_finish 참고).
PUBLISH_SETTLE_BASE_SECONDS = 6.0
PUBLISH_SETTLE_PER_THREAD_SECONDS = 2.5
PUBLISH_SETTLE_PER_IMAGE_SECONDS = 2.0
PUBLISH_SETTLE_MAX_SECONDS = 60.0
# '게시'를 한 번 누른 뒤 작성창이 닫히는지 지켜보는 시간. 이 안에 안 닫히면 눌리지
# 않은 것으로 보고 다음 클릭 방법으로 넘어간다(최종 판정은 _wait_for_composer_closed).
#
# **짧게 잡으면 안 된다.** 처음엔 4초였는데, 스레드 6개에 사진까지 붙은 글은 게시가
# 4초 안에 끝나지 않는다. 그러면 이미 **발행 중인데** 게시를 한 번 더 누르게 된다
# (2026-08-04, 작성창에는 6개가 다 들어갔는데 첫 개만 올라간 일의 유력한 원인).
POST_CLICK_CONFIRM_SECONDS = 20.0
PROFILE_DIR_ENV = "THREADS_BROWSER_PROFILE_DIR"
THREAD_IMAGE_TEMP_PREFIX = "blogit-threads-"
THREAD_IMAGE_STALE_SECONDS = 24 * 60 * 60

# 2단계 인증: 앱 화면에서 코드를 받기까지 기다리는 시간과, 제출 뒤 화면이 넘어가기를
# 지켜보는 시간. 대기 시간은 사용자가 문자를 확인해 옮겨 적는 시간이라 넉넉히 둔다.
VERIFICATION_WAIT_SECONDS = 180.0
VERIFICATION_SETTLE_SECONDS = 12.0

# 화면(코드 입력창)이 코드 대신 보내는 지시. 코드가 안 올 때 '코드 재전송' 버튼을
# 대신 눌러 달라는 뜻이다 — 시도 횟수를 소모하지 않는다(코드를 틀린 게 아니다).
RESEND_CODE_SENTINEL = "__RESEND__"
# '백업 코드 사용' 화면으로 넘어가 달라는 지시. 화면의 버튼을 대신 누른다.
BACKUP_SWITCH_SENTINEL = "__BACKUP__"
# 뒤로(←) 화살표를 눌러 달라는 지시 — 백업 코드 화면에서 인증번호 화면으로 되돌아갈 때.
BACK_SENTINEL = "__BACK__"
# 백업 코드는 8자리, 문자 코드는 6자리다. 8자리가 들어오면 '백업 코드 사용' 화면으로
# 먼저 넘어가서 넣는다 — 문자가 안 올 때 사용자가 백업 코드로 통과하는 길이다.
BACKUP_CODE_LENGTH = 8

# 사람이 손을 옮기는 정도의 사이 간격. 폼에 값을 넣고 곧바로 다음 동작을 하면
# (0.3초) 기계 티가 난다 — 봇 판정을 피하는 것이 로그인 성공률이다(2026-08-18).
HUMAN_PAUSE_SECONDS = 0.9
# '백업 코드 사용'을 누른 뒤 입력 화면이 그려지기를 기다리는 시간.
BACKUP_SWITCH_SETTLE_SECONDS = 1.5

# 2단계 인증 코드 입력칸을 찾는 단서. Meta의 클래스명은 난독화돼 있어 쓸 수 없고,
# 입력칸의 **성격**을 나타내는 속성만 본다. 앞선 것부터 확실한 순서다.
_VERIFICATION_INPUT_SELECTORS = (
    "input[autocomplete='one-time-code']",
    "input[name='verificationCode']",
    "input[name='security_code']",
    "input[name*='confirmation']",
    "input[id*='security_code']",
    "input[inputmode='numeric']",
    # 2026-08-04 실측 화면의 자리표시자·라벨. 한국어 UI 기준이라 영어도 함께 본다.
    "input[placeholder*='보안 코드']",
    "input[aria-label*='보안 코드']",
    "input[placeholder*='Security code']",
    "input[aria-label*='Security code']",
    # '백업 코드 사용'으로 넘어간 화면의 입력칸(실측 2026-08-18 스크린샷).
    "input[placeholder*='백업 코드']",
    "input[aria-label*='백업 코드']",
    "input[placeholder*='Backup code']",
)

# 글자를 받는 입력 타입. 이 밖(체크박스·라디오·파일 등)은 코드 칸 후보가 아니다.
_TEXTUAL_INPUT_TYPES = {"text", "tel", "number", "search", ""}

# 로그인 폼의 칸이 달고 있는 autocomplete 값. 실측(2026-08-04)한 진단 로그가
# `type=text autocomplete=username / type=password autocomplete=current-password`였다 —
# 코드 칸은 이 값을 달지 않으므로, 뒤 배경에 로그인 폼이 남아 있어도 이것으로 갈라낸다.
_LOGIN_AUTOCOMPLETE_VALUES = {"username", "current-password", "new-password", "email"}

# 화면이 스스로 '2단계 인증'이라고 말하는 문구. DOM 속성은 빌드마다 바뀌지만 사용자에게
# 보이는 글자는 안정적이다 — 이것이 잡히면 코드 칸을 찾는 기준을 느슨하게 해도 된다.
_VERIFICATION_SCREEN_MARKERS = (
    "2단계 인증",
    "보안 코드",
    "인증 코드",
    "백업 코드",
    "two-factor",
    "security code",
    "confirmation code",
    "backup code",
)

# '스레드에 추가'를 누른 뒤 칸이 늘기를 기다리는 시간과, 눌러 볼 후보의 최대 개수.
# 글자로 찾으면 조상까지 걸리므로 첫 후보가 빗나갈 수 있다 — 몇 개까지 눌러 본다.
ADD_THREAD_GROW_SECONDS = 6.0
ADD_THREAD_MAX_CANDIDATES = 4
# 방금 친 글이 반영되고 다음 자리표시자가 그려질 시간. 이것 없이 바로 누르면 2번째
# 스레드 추가가 가끔 실패했다(2026-08-04 사용자 보고).
ADD_THREAD_SETTLE_SECONDS = 1.0
# 후보를 전부 눌러 봐도 안 되면 잠깐 쉬었다 다시 돈다.
ADD_THREAD_MAX_ROUNDS = 2
# 이미지 미리보기가 붙기를 기다리는 시간과, 붙은 뒤 더 두는 시간. 업로드가 끝나기 전에
# '게시'를 누르면 사진이 빠진 채 올라간다(2026-08-04 실사용에서 그렇게 됐다).
IMAGE_UPLOAD_TIMEOUT_SECONDS = 30.0
IMAGE_UPLOAD_SETTLE_SECONDS = 2.5
# 모달을 확인한 직후에는 파일 입력칸이 DOM에는 있지만 아직 배선 전일 수 있다(React가
# 리스너를 붙이는 중). 실사용(2026-08-04): 모달 대기를 넣은 뒤에도 첫 전송이 등록되지
# 않았고 재시도에서 붙었다 — 잠깐 두었다 넣는 것이 첫 시도 성공률을 올리는 지렛대다.
FILE_INPUT_SETTLE_SECONDS = 1.0
# 첫 전송의 미리보기 대기. 미리보기는 로컬에서 즉시 그려지므로 오래 기다릴 이유가 없다 —
# 짧게 보고 안 붙었으면 빨리 다시 넣는다(재시도는 IMAGE_UPLOAD_TIMEOUT_SECONDS만큼 본다).
# 30초를 다 기다린 뒤 재시도하면 성공해도 발행이 30초 늦는다(2026-08-04 실사용 로그).
IMAGE_UPLOAD_FIRST_WAIT_SECONDS = 10.0

# 발행 뒤 확인용으로 열어 둔 브라우저를 프로필별로 하나만 붙잡아 둔다(네이버와 동일).
_KEPT_OPEN_BROWSERS: dict = {}


def _is_button(element) -> bool:
    """누를 수 있는 요소인가(버튼 태그이거나 role='button')."""
    try:
        if (element.tag_name or "").lower() == "button":
            return True
        return element.get_attribute("role") == "button"
    except Exception:
        return False


def _contains(ancestor, descendant) -> bool:
    """``ancestor``가 ``descendant``를 품고 있는가.

    글자로 요소를 찾으면 조상까지 전부 걸린다. 가장 안쪽만 남기려면 포함 관계를 봐야 한다.
    """
    try:
        return bool(
            ancestor.parent.execute_script(
                "return arguments[0] !== arguments[1] && arguments[0].contains(arguments[1]);",
                ancestor,
                descendant,
            )
        )
    except Exception:
        return False


def _release_kept_open_browser(profile_dir: Path) -> None:
    driver = _KEPT_OPEN_BROWSERS.pop(str(profile_dir), None)
    if driver is not None:
        # 쿠키를 디스크에 남기려면 정상 종료해야 한다(close_browser 참고).
        close_browser(driver)


@dataclass
class ThreadsBrowserConfig:
    # naver.browser._create_driver가 요구하는 것은 profile_dir뿐이다(덕 타이핑).
    profile_dir: Path


def threads_profile_dir(user_id: str | None = None) -> Path:
    """스레드 로그인 세션을 보관하는 사용자별 Chrome 프로필 경로 (naver_profile_dir와 동형)."""
    configured = (os.environ.get(PROFILE_DIR_ENV) or "").strip()
    base = (
        Path(configured).resolve()
        if configured
        else (Path(__file__).resolve().parents[4] / ".threads-profile").resolve()
    )
    if not user_id:
        return base
    user_scope = hashlib.sha256(user_id.strip().encode("utf-8")).hexdigest()[:24]
    return base.parent / f"{base.name}-users" / user_scope


def has_threads_session(profile_dir: Path) -> bool:
    """브라우저를 띄우지 않고 세션 존재를 추정한다 — Chrome 쿠키 DB 파일 기준.

    NaverConfig.has_session과 같은 한계를 공유한다: 파일이 있다고 세션이 유효하다는
    보장은 없다(만료 가능). 설정 화면의 상태 표시용이다.
    """
    candidates = (
        profile_dir / "Cookies",
        profile_dir / "Default" / "Cookies",
        profile_dir / "Default" / "Network" / "Cookies",
    )
    return any(path.is_file() and path.stat().st_size > 0 for path in candidates)


def _is_owned_thread_temp_dir(path: Path) -> bool:
    """삭제 대상을 OS temp 바로 아래의 우리 prefix 디렉터리로만 제한한다."""
    try:
        resolved = path.resolve(strict=False)
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    except OSError:
        return False
    return resolved.parent == temp_root and resolved.name.startswith(THREAD_IMAGE_TEMP_PREFIX)


def _remove_thread_temp_dir(path: Path) -> bool:
    if not _is_owned_thread_temp_dir(path):
        logger.error("안전 범위를 벗어난 Threads 임시 경로는 삭제하지 않습니다.")
        return False
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return True
    except OSError as error:
        logger.warning("Threads 임시 이미지 정리 실패 (%s)", type(error).__name__)
        return False
    return True


def cleanup_stale_thread_image_dirs(
    *, max_age_seconds: float = THREAD_IMAGE_STALE_SECONDS, now: float | None = None
) -> int:
    """비정상 종료 때 남은 평문 이미지 복사본만 제한적으로 지운다."""
    try:
        root = Path(tempfile.gettempdir()).resolve(strict=True)
    except OSError as error:
        logger.warning(
            "Threads 임시 이미지 루트를 확인하지 못했습니다 (%s)",
            type(error).__name__,
        )
        return 0
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    for candidate in root.glob(f"{THREAD_IMAGE_TEMP_PREFIX}*"):
        try:
            if not candidate.is_dir() or candidate.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        if _remove_thread_temp_dir(candidate):
            removed += 1
    if removed:
        logger.info("비정상 종료 뒤 남은 Threads 임시 이미지 폴더 %d개를 정리했습니다.", removed)
    return removed


async def log_in_and_store_threads_session(
    profile_dir: Path, headless: bool = False, user_id: str | None = None
) -> None:
    """스레드에 한 번 로그인해 이 PC 프로필에 세션을 저장한다.

    설정 화면에서 부른다. 발행 도중에 2단계 인증을 만나면 그 발행이 통째로 멈추므로,
    **사람이 앉아 있을 때 미리 한 번 로그인해 두는 편이 낫다.** 네이버의
    ``log_in_and_store_session``과 같은 자리다.

    보이는 창으로 열고 **끝나도 닫지 않는다.** 사용자가 어느 계정으로 들어갔는지 눈으로
    확인해야 하고, 추가 확인(2단계 인증)이 남았으면 그 창에서 마저 끝내야 한다. 다음
    발행이 시작될 때 이 창을 닫고 새로 연다(발행 경로와 같은 정책).
    """
    from .credentials import load_credentials

    config = ThreadsBrowserConfig(profile_dir=profile_dir)
    credentials = load_credentials(profile_dir)

    def connect() -> None:
        # 프로필은 한 번에 한 창만 쓸 수 있다 — 열어 둔 것이 있으면 먼저 닫는다.
        # 닫기·열기 구간은 프로필 잠금 안이다. 리퍼(browser_reaper)가 같은 창을 닫는
        # 몇 초와 겹치면 '아직 종료 중인 크롬'과 부딪혀 프로필 충돌로 죽는다.
        with _profile_lock(str(profile_dir)):
            _release_kept_open_browser(profile_dir)
            driver = _create_driver(config, headless)
        # 로그인 화면을 웹으로 중계한다 — 외부 PC 사용자가 2단계 인증·추가 확인을
        # 직접 본다. 등록 실패는 로그인을 막지 않는다.
        if user_id:
            live_view_hub.register(user_id, "threads", driver, "스레드 로그인", kind="login")
        try:
            # user_id를 넘겨야 2단계 인증에서 **앱의 코드 입력창**(verification broker)이
            # 뜬다. 예전에는 설정 로그인에 안 넘겨서, 발행 때만 뜨던 코드 창이 정작
            # 로그인을 미리 해 두는 이 자리에서는 안 떴다.
            ThreadsBrowserPublisher(headless=headless)._ensure_logged_in(
                driver, credentials, profile_dir=profile_dir, user_id=user_id or ""
            )
            if not ThreadsBrowserPublisher._has_session(driver):
                raise _NeedsHuman("스레드 로그인 세션 쿠키를 확인하지 못했습니다.")
        finally:
            if headless:
                # 쿠키를 디스크에 남기려면 정상 종료해야 한다(close_browser 참고).
                close_browser(driver)
            else:
                # 로그인 흐름이 끝났다 — 창은 남겨도 중계 목록에서는 내린다.
                live_view_hub.mark_idle(driver)
                # 다음 발행이 닫거나, 일정 시간이 지나면 리퍼가 닫는다(browser_reaper).
                mark_kept_open(_KEPT_OPEN_BROWSERS, str(profile_dir), driver)

    await _in_browser_thread(connect)


def _intent_url(text: str) -> str:
    """작성창을 텍스트가 채워진 채 여는 공식 공유 인텐트 URL."""
    return f"{THREADS_ORIGIN}/intent/post?text={quote(text, safe='')}"


class ThreadsBrowserPublisher:
    """사용자별 브라우저 세션으로 threads.net에 글 하나를 게시한다."""

    def __init__(self, headless: bool = False):
        self._headless = headless
        # 원고 이미지를 풀어 둘 임시 폴더. 첫 업로드 때 만든다.
        self._image_dir: Path | None = None

    def _cleanup_image_dir(self) -> None:
        image_dir, self._image_dir = self._image_dir, None
        if image_dir is not None:
            _remove_thread_temp_dir(image_dir)

    async def publish(self, job: PublishJob) -> PublishResult:
        if job.method != PostingMethod.AUTO:
            return PublishResult(
                result=PostingResultStatus.FAIL,
                error_message="스레드는 즉시 발행만 지원합니다 (임시저장 없음)",
            )
        pieces = [piece for piece in publish_pieces_for(job) if piece.text or piece.images]
        if not pieces:
            return PublishResult(
                result=PostingResultStatus.FAIL,
                error_message="스레드에 실을 텍스트가 없습니다.",
            )

        from .credentials import load_credentials

        config = ThreadsBrowserConfig(profile_dir=threads_profile_dir(job.user_id))
        credentials = load_credentials(config.profile_dir)
        logger.info(
            "스레드 브라우저 발행 시작 | %s - 스레드 %d개 · 이미지 %d장",
            job.post_id,
            len(pieces),
            sum(len(piece.images) for piece in pieces),
        )
        try:
            post_url = await _in_browser_thread(
                lambda: self._run_sync(
                    config,
                    pieces,
                    credentials,
                    user_id=job.user_id,
                    post_id=job.post_id,
                    topic=(job.threads_topic or "").strip(),
                )
            )
        except _NeedsHuman as error:
            return PublishResult(
                result=PostingResultStatus.NEEDS_HUMAN, error_message=str(error)
            )
        except Exception as error:
            # Selenium 메시지에는 방문 URL/query와 로컬 프로필 경로가 포함될 수 있다.
            logger.warning(
                "스레드 브라우저 발행 실패 | %s - %s",
                job.post_id,
                type(error).__name__,
            )
            return PublishResult(
                result=PostingResultStatus.FAIL,
                error_message=(
                    "스레드 발행에 실패했습니다. 잠시 후 다시 시도하거나 "
                    "관리자에게 문의해 주세요."
                ),
            )
        logger.info("스레드 브라우저 발행 완료 | %s → %s", job.post_id, post_url)
        return PublishResult(result=PostingResultStatus.SUCCESS, post_url=post_url)

    def _run_sync(
        self,
        config: ThreadsBrowserConfig,
        pieces: list[ThreadPiece],
        credentials=None,
        *,
        user_id: str = "",
        post_id: str = "",
        topic: str = "",
    ) -> str | None:
        cleanup_stale_thread_image_dirs()
        # 닫기·열기 구간은 프로필 잠금 안이다(위 로그인 경로와 같은 이유 — 리퍼와의 경쟁).
        with _profile_lock(str(config.profile_dir)):
            _release_kept_open_browser(config.profile_dir)
            driver = _create_driver(config, self._headless)
        # 발행 화면을 웹으로 중계한다(네이버와 같은 정책).
        if user_id:
            live_view_hub.register(user_id, "threads", driver, "스레드 발행", kind="publish")
        try:
            self._ensure_logged_in(
                driver,
                credentials,
                user_id=user_id,
                post_id=post_id,
                profile_dir=config.profile_dir,
            )
            # 첫 스레드는 공유 인텐트 URL이 채워 준다(DOM 조작 없이 작성창이 열린다).
            driver.get(_intent_url(pieces[0].text))
            self._attach_images(driver, 1, pieces[0].images)
            self._append_remaining_threads(driver, pieces[1:])
            self._set_topic(driver, topic)
            button = self._wait_for_post_button(driver)
            self._click_post(driver, button)
            self._wait_for_composer_closed(driver)
            self._let_the_publish_finish(driver, pieces)
            # 확인 조각은 첫 스레드에서 뽑는다 — 프로필에 가장 먼저 보이는 글이다.
            post_url = self._verify_on_profile(driver, pieces[0].text)
            return post_url
        finally:
            self._cleanup_image_dir()
            # 성공·실패와 관계없이 창을 열어 둔다 — 결과(또는 실패 화면)를 사용자가
            # 직접 본다. 다음 발행이 시작될 때 닫는다(네이버와 같은 정책).
            if self._headless:
                # 쿠키를 디스크에 남기려면 정상 종료해야 한다(close_browser 참고).
                close_browser(driver)
            else:
                # 발행 흐름이 끝났다 — 창은 남겨도 중계 목록에서는 내린다.
                live_view_hub.mark_idle(driver)
                mark_kept_open(_KEPT_OPEN_BROWSERS, str(config.profile_dir), driver)

    # --- 로그인 --------------------------------------------------------------

    @staticmethod
    def _has_session(driver) -> bool:
        try:
            return any(
                cookie.get("name") == "sessionid" and cookie.get("value")
                for cookie in driver.get_cookies()
            )
        except Exception:
            return False

    @staticmethod
    def _session_belongs_to_settings(profile_dir: Path | None, credentials) -> bool:
        """살아 있는 세션이 설정에 저장된 스레드 계정 것인지.

        네이버 쪽 판단과 같은 규칙이다(``naver/login.py`` 참고). 프로필은 블로그잇
        사용자별로만 갈리므로, 스레드 계정을 바꿔도 **예전 계정 세션이 그대로 살아 있어**
        그 계정으로 올라간다. 그것을 막는다.

        자격증명이 없으면(사람이 직접 로그인) 비교할 대상도, 다시 로그인할 방법도 없으니
        세션을 그대로 쓴다. 기록이 없으면 한 번 새로 로그인해 기록을 세운다.
        """
        wanted = ((getattr(credentials, "username", "") or "")).strip()
        if profile_dir is None or not wanted:
            return True
        current = session_account(profile_dir)
        if current is None:
            logger.info("이 Chrome 프로필의 스레드 계정을 알 수 없어 한 번 새로 로그인합니다.")
            return False
        if current.strip().lower() == wanted.lower():
            return True
        logger.info("설정의 스레드 계정과 브라우저 세션 계정이 달라 새로 로그인합니다.")
        return False

    @staticmethod
    def _sign_out(driver, profile_dir: Path | None) -> None:
        """이전 계정의 세션을 끊는다.

        네이버와 달리 **쿠키를 지운다.** 네이버에서 쿠키를 아낀 이유는 '이 브라우저는
        2단계 인증 없이 로그인 합니다'로 얻은 신뢰가 거기 있어서인데, 스레드의 2단계
        인증은 그런 체크박스가 아니라 **코드 입력**이고 그 코드는 앱 화면으로 받아
        자동으로 넣는다(``_solve_verification``). 즉 쿠키를 지워도 사람 손이 더 들지 않는다.
        """
        if profile_dir is not None:
            forget_session_account(profile_dir)
        try:
            driver.delete_all_cookies()
        except Exception as error:
            logger.warning(
                "이전 스레드 세션을 정리하지 못했습니다 (%s)",
                type(error).__name__,
            )

    def _ensure_logged_in(
        self,
        driver,
        credentials=None,
        *,
        user_id: str = "",
        post_id: str = "",
        profile_dir: Path | None = None,
    ) -> None:
        """세션이 없으면 로그인한다 — 저장된 자격증명이 있으면 자동 입력, 없으면 수동.

        성공 판정은 세션 쿠키다. Meta가 2단계 인증을 걸면 **앱 화면에 코드를 물어보고**
        받은 값을 대신 입력한다(``_solve_verification``) — 예전에는 사용자가 자동화가 띄운
        Chrome 창을 직접 만져야 했고, 사람이 없는 예약 발행에서는 아예 불가능했다.

        코드를 못 받으면(시간 초과·취소·연속 오답) NEEDS_HUMAN으로 넘긴다. 창은 열린 채
        남으므로 사용자가 직접 마무리할 수도 있다.
        """
        driver.get(f"{THREADS_ORIGIN}/login")
        if self._has_session(driver):
            # 세션이 있다고 바로 쓰지 않는다. **설정의 계정 것일 때만** 건너뛴다.
            if self._session_belongs_to_settings(profile_dir, credentials):
                logger.info("저장된 스레드 세션으로 로그인 없이 진행합니다.")
                return
            self._sign_out(driver, profile_dir)
            driver.get(f"{THREADS_ORIGIN}/login")

        if credentials is not None:
            self._fill_login_form(driver, credentials)
        else:
            # INFO로 흘리면 "왜 자동 입력이 안 되지"로 헤맨다(2026-08-04 실사용). 자동화가
            # 할 수 있는 일이 없다는 뜻이므로 WARNING으로 남기고, 실패 문구에도 실어 보낸다.
            logger.warning(
                "저장된 스레드 로그인 정보가 없습니다 — 설정 화면에서 저장하면 다음부터 "
                "자동으로 입력합니다. 지금은 열린 창에서 직접 로그인해 주세요."
            )

        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        described = False
        while time.monotonic() < deadline:
            time.sleep(1)
            if self._has_session(driver):
                logger.info("스레드 로그인 확인 — 발행을 계속합니다.")
                # 어느 계정으로 로그인됐는지 적어 둔다. 적지 않으면 다음 실행에서 이
                # 프로필의 쿠키가 누구 것인지 알 수 없어, 설정만 바꾼 사람이 예전
                # 계정으로 올리게 된다.
                if profile_dir is not None:
                    remember_session_account(
                        profile_dir, getattr(credentials, "username", None)
                    )
                time.sleep(1)
                return
            # 2단계 인증 화면이면 사람을 기다리지 말고 앱 화면에 코드를 물어본다.
            if user_id and self._verification_code_field(driver) is not None:
                if self._solve_verification(driver, user_id=user_id, post_id=post_id):
                    # 코드가 통했는지는 위의 세션 검사가 다음 바퀴에서 확인한다.
                    continue
                raise _NeedsHuman(
                    "스레드 2단계 인증을 통과하지 못했습니다. "
                    "설정 화면에서 Threads '로그인' 버튼을 눌러 인증을 끝낸 뒤 다시 발행해 주세요."
                )
            # 세션도 없고 아는 화면도 아니다. 한 번만 화면을 적어 둔다 — 선택자가 실제
            # Meta 화면과 어긋났을 때 무엇을 보고 고쳐야 하는지가 이 한 줄에 달렸다.
            if not described and time.monotonic() > deadline - LOGIN_TIMEOUT_SECONDS + 10:
                described = True
                self._describe_unknown_screen(driver)
        raise _NeedsHuman(
            "스레드 로그인이 완료되지 않았습니다. "
            "설정 화면에서 Threads '로그인' 버튼을 눌러 로그인을 끝낸 뒤 다시 발행해 주세요."
            if credentials is not None
            else "저장된 스레드 로그인 정보가 없어 자동 로그인을 하지 못했습니다. "
            "설정 화면에서 스레드 아이디·비밀번호를 저장한 뒤 다시 시도해 주세요."
        )

    # --- 2단계 인증 ----------------------------------------------------------

    @staticmethod
    def _dialog_scope(driver):
        """요소를 찾을 범위. 열려 있는 모달이 있으면 그 안, 없으면 화면 전체.

        스레드의 2단계 인증과 글 작성창은 **모달**로 뜨고, 그 뒤 화면은 그대로 남아 있다.
        Selenium은 다른 요소에 가려진 것도 `is_displayed()`를 True로 보기 때문에 화면
        전체를 뒤지면 뒤 배경의 요소를 집는다. 실제로 두 번 겪었다(2026-08-04).

        - 2단계 인증: 뒤에 남은 비밀번호 칸을 보고 "아직 로그인 단계"라 판단해 코드를
          영영 물어보지 않았다.
        - 글 작성창: 뒤 피드의 '게시' 버튼을 먼저 집었고, 그 좌표를 누르니 모달 뒷배경이
          눌려 "스레드를 삭제하시겠어요?"가 떴다.

        모달이 여러 개면 **마지막(가장 위)** 것을 쓴다.
        """
        from selenium.webdriver.common.by import By

        try:
            visible = [
                dialog
                for dialog in driver.find_elements(By.CSS_SELECTOR, "[role='dialog']")
                if dialog.is_displayed()
            ]
            if visible:
                return visible[-1]
        except Exception:
            pass
        return driver

    def _verification_code_field(self, driver):
        """2단계 인증 코드 입력칸. 아니면 None.

        Meta의 DOM은 클래스가 난독화돼 있어 **입력칸의 성격**으로 찾는다. 찾는 범위는
        2단계 인증 모달 안이고(`_dialog_scope`), 모달이 없으면 화면 전체다.

        순서가 중요하다.

        1. **이름표로 찾기가 먼저다.** '보안 코드' 자리표시자처럼 코드 칸임이 분명한
           단서가 있으면 그것을 쓴다 — 뒤 배경에 로그인 폼이 남아 있든 말든 상관없다.
           예전에는 비밀번호 검사를 먼저 해서, 모달을 `[role='dialog']`로 못 찾은 화면에서는
           **눈앞에 코드 칸이 있는데도** 뒤의 비밀번호 칸 때문에 None을 돌려줬다
           (2026-08-04 실사용: 스레드 2단계 인증 팝업이 떴는데 우리 팝업이 안 떴다).
        2. 이름표로 못 찾았을 때만 비밀번호 검사를 한다. 이 검사는 **구조 탐색이 로그인
           폼의 아이디 칸을 코드 칸으로 착각하는 것**을 막는 장치다.
        3. 마지막으로 구조 탐색(글자 입력칸이 하나뿐이면 그것).
        """
        from selenium.webdriver.common.by import By

        try:
            scope = self._dialog_scope(driver)
            for selector in _VERIFICATION_INPUT_SELECTORS:
                for element in scope.find_elements(By.CSS_SELECTOR, selector):
                    if element.is_displayed() and element.is_enabled():
                        return element

            candidates = self._code_candidates(scope)
            # 화면이 스스로 '2단계 인증'이라고 말하면 그 말을 믿는다. 이름표가 어떻든
            # 이 화면의 입력칸은 코드 칸이다.
            if self._says_verification(scope):
                return candidates[0] if len(candidates) == 1 else None

            if any(
                element.is_displayed()
                for element in scope.find_elements(By.CSS_SELECTOR, "input[type='password']")
            ):
                return None
            return candidates[0] if len(candidates) == 1 else None
        except Exception:
            return None

    @staticmethod
    def _says_verification(scope) -> bool:
        """화면 글자에 '2단계 인증' 같은 문구가 있는가.

        DOM 속성은 Meta가 빌드할 때마다 바뀌지만 **사용자에게 보이는 글자는 안정적이다.**
        실측 화면에는 제목 '2단계 인증'과 자리표시자 '보안 코드'가 있었다.
        """
        try:
            from selenium.webdriver.common.by import By

            text = getattr(scope, "text", None)
            if text is None:
                text = scope.find_element(By.TAG_NAME, "body").text
            lowered = (text or "").lower()
            return any(marker.lower() in lowered for marker in _VERIFICATION_SCREEN_MARKERS)
        except Exception:
            return False

    @staticmethod
    def _code_candidates(scope):
        """코드 칸이 될 수 있는 입력들. 로그인 폼의 아이디·비밀번호는 뺀다.

        속성 이름으로 코드 칸을 **찾는** 것은 Meta가 마크업을 바꿀 때마다 빗나간다. 대신
        **아닌 것을 걸러내는** 쪽은 안정적이다: 로그인 칸은 `autocomplete=username` /
        `current-password`를 달고 있고(2026-08-04 진단 로그로 확인), 코드 칸은 달지 않는다.

        그래서 2단계 인증 모달이 로그인 폼 위에 겹쳐 떠 있어도 남는 후보는 코드 칸 하나다.
        후보가 둘 이상이면 무엇이 코드인지 알 수 없으므로 손대지 않는다 — 엉뚱한 칸에
        코드를 넣느니 사람이 처리하는 편이 낫다.
        """
        from selenium.webdriver.common.by import By

        candidates = []
        for element in scope.find_elements(By.CSS_SELECTOR, "input"):
            try:
                if not (element.is_displayed() and element.is_enabled()):
                    continue
                if (element.get_attribute("type") or "text").lower() not in _TEXTUAL_INPUT_TYPES:
                    continue
                autocomplete = (element.get_attribute("autocomplete") or "").lower()
                if autocomplete in _LOGIN_AUTOCOMPLETE_VALUES:
                    continue
                candidates.append(element)
            except Exception:
                continue
        return candidates

    def _click_dialog_button(self, driver, *labels: str) -> bool:
        """모달 안에서 문구로 버튼을 찾아 누른다(가장 안쪽 우선). 못 찾으면 False.

        '코드 재전송'·'백업 코드 사용'처럼 Meta의 클래스가 아니라 **사용자에게 보이는
        글자**로 찾는 버튼용이다(_add_thread_candidates와 같은 생존 전략).
        """
        from selenium.webdriver.common.by import By

        xpath = ".//*[" + " or ".join(
            f"normalize-space()='{label}'" for label in labels
        ) + "]"
        try:
            matches = [
                element
                for element in self._dialog_scope(driver).find_elements(By.XPATH, xpath)
                if element.is_displayed()
            ]
        except Exception:
            return False

        def innermost_first(elements: list) -> list:
            return sorted(
                elements,
                key=lambda element: sum(1 for other in elements if _contains(element, other)),
            )

        buttons = [element for element in matches if _is_button(element)]
        others = [element for element in matches if element not in buttons]
        for candidate in innermost_first(buttons) + innermost_first(others):
            try:
                self._click(driver, candidate)
                return True
            except Exception:
                continue
        return False

    def _click_dialog_back(self, driver) -> bool:
        """모달 왼쪽 위의 뒤로(←) 화살표를 누른다. 못 찾으면 False.

        백업 코드 화면에서 인증번호 화면으로 되돌아갈 때 쓴다(2026-08-18 사용자 요청).
        이 버튼은 **글자가 없는 아이콘**이라 문구 탐색(_click_dialog_button)이 안 통한다
        — aria-label(뒤로/돌아가기/Back)을 먼저 보고, 없으면 모달 안에서 글자 없는 버튼
        중 문서 순서 첫 번째를 누른다(실측 화면에서 왼쪽 위 화살표가 유일한 무문구 버튼).
        """
        from selenium.webdriver.common.by import By

        try:
            scope = self._dialog_scope(driver)
            buttons = [
                element
                for element in scope.find_elements(
                    By.CSS_SELECTOR, "button, [role='button'], [role='link']"
                )
                if element.is_displayed()
            ]
        except Exception:
            return False

        def label_of(element) -> str:
            try:
                aria = element.get_attribute("aria-label") or ""
                return f"{aria} {(element.text or '')}".strip().lower()
            except Exception:
                return ""

        # 1) 이름표가 '뒤로'라고 말하는 버튼이 가장 확실하다.
        for element in buttons:
            label = label_of(element)
            if label and any(marker in label for marker in ("뒤로", "돌아가", "back", "이전")):
                try:
                    self._click(driver, element)
                    return True
                except Exception:
                    continue
        # 2) 글자 없는 버튼(아이콘) 중 첫 번째 — 화살표는 모달 맨 앞에 있다.
        for element in buttons:
            try:
                if not (element.text or "").strip():
                    self._click(driver, element)
                    return True
            except Exception:
                continue
        return False

    def _solve_verification(self, driver, *, user_id: str, post_id: str) -> bool:
        """앱 화면에서 코드를 받아 입력한다. 통과 신호를 보면 True.

        오답이면 화면에 다시 물어본다(``MAX_ATTEMPTS``까지). 사용자가 창을 닫거나 시간이
        지나면 False — 호출부가 NEEDS_HUMAN으로 끝낸다.

        코드가 안 올 때의 두 갈래(2026-08-18 사용자 요청 — 실사용에서 문자가 늦거나
        아예 안 왔다):

        - 화면이 ``RESEND_CODE_SENTINEL``을 보내면 **'코드 재전송' 버튼을 대신 누른다.**
          시도 횟수를 소모하지 않는다 — 코드를 틀린 게 아니다.
        - 8자리 코드가 오면 백업 코드다. **'백업 코드 사용'으로 먼저 넘어가서** 넣는다.
        """
        from .verification import MAX_ATTEMPTS, broker

        attempt = 1
        prompt = (
            "스레드(인스타그램) 2단계 인증 코드를 입력해 주세요. "
            "문자가 안 오면 '코드 재전송'을 누르거나 백업 코드(8자리)를 넣어 주세요."
        )
        while attempt <= MAX_ATTEMPTS:
            field = self._verification_code_field(driver)
            if field is None:
                # 기다리는 사이 화면이 넘어갔다 — 이미 통과한 것으로 본다.
                return True

            request = broker.request(
                user_id=user_id,
                post_id=post_id,
                channel="threads",
                prompt=prompt,
                attempt=attempt,
            )
            code = broker.wait(request, timeout=VERIFICATION_WAIT_SECONDS)
            if not code:
                return False

            if code == RESEND_CODE_SENTINEL:
                if self._click_dialog_button(driver, "코드 재전송", "재전송", "Resend code"):
                    logger.info("스레드 2단계 인증 '코드 재전송'을 눌렀습니다.")
                    prompt = "새 코드를 보냈습니다. 받은 코드를 입력해 주세요."
                else:
                    logger.warning("'코드 재전송' 버튼을 찾지 못했습니다 — 계속 기다립니다.")
                    prompt = "재전송 버튼을 찾지 못했습니다. 받은 코드를 그대로 입력해 주세요."
                time.sleep(HUMAN_PAUSE_SECONDS)
                continue

            if code == BACKUP_SWITCH_SENTINEL:
                # 화면의 '백업 코드 사용'을 대신 누른다 — 사용자는 백업 코드만 넣으면 된다.
                if self._click_dialog_button(driver, "백업 코드 사용", "백업 코드", "Use backup code"):
                    logger.info("스레드 2단계 인증 '백업 코드 사용'을 눌렀습니다.")
                    prompt = "백업 코드(8자리)를 입력해 주세요."
                    time.sleep(BACKUP_SWITCH_SETTLE_SECONDS)
                else:
                    logger.warning("'백업 코드 사용' 버튼을 찾지 못했습니다 — 계속 기다립니다.")
                    prompt = "'백업 코드 사용' 버튼을 찾지 못했습니다. 받은 코드를 그대로 입력해 주세요."
                continue

            if code == BACK_SENTINEL:
                # 뒤로(←) — 백업 코드 화면에서 인증번호 입력 화면으로 되돌아간다.
                if self._click_dialog_back(driver):
                    logger.info("스레드 2단계 인증 화면에서 뒤로(←)를 눌렀습니다.")
                    prompt = "인증번호 입력 화면으로 돌아왔습니다. 받은 코드를 입력해 주세요."
                    time.sleep(BACKUP_SWITCH_SETTLE_SECONDS)
                else:
                    logger.warning("뒤로(←) 버튼을 찾지 못했습니다 — 계속 기다립니다.")
                    prompt = "뒤로 버튼을 찾지 못했습니다. 중계 화면에서 직접 눌러 주세요."
                continue

            if len(code) == BACKUP_CODE_LENGTH and self._click_dialog_button(
                driver, "백업 코드 사용", "백업 코드", "Use backup code"
            ):
                # 백업 코드 입력 화면이 그려질 시간을 주고 칸을 다시 찾는다. 이미 백업
                # 화면이면 버튼이 없어 이 분기를 지나지 않고 그대로 넣는다.
                logger.info("백업 코드(8자리)를 받아 '백업 코드 사용' 화면으로 넘어갑니다.")
                time.sleep(BACKUP_SWITCH_SETTLE_SECONDS)
                field = self._verification_code_field(driver) or field

            if not self._type_verification_code(driver, field, code):
                attempt += 1
                continue

            # 제출 뒤 화면이 넘어가기를 잠깐 기다린다. 세션이 생겼거나 입력칸이 사라졌으면
            # 통과다. 그대로면 오답이므로 다음 바퀴에서 다시 물어본다.
            settle = time.monotonic() + VERIFICATION_SETTLE_SECONDS
            while time.monotonic() < settle:
                time.sleep(1)
                if self._has_session(driver):
                    return True
                if self._verification_code_field(driver) is None:
                    return True
            logger.info("스레드 2단계 인증 코드가 통하지 않았습니다 (%d회차).", attempt)
            attempt += 1
            prompt = "코드가 맞지 않습니다. 새로 받은 코드를 다시 입력해 주세요."

        return False

    def _describe_unknown_screen(self, driver) -> None:
        """로그인도 2단계 인증도 아닌 화면의 입력칸을 적는다(진단 전용).

        값은 절대 찍지 않는다 — 사용자가 입력한 아이디·비밀번호가 로그에 남으면 안 된다.
        보는 것은 '어떤 성격의 칸이 있었나'뿐이고, 그것이 선택자를 고칠 근거다.
        """
        from selenium.webdriver.common.by import By

        try:
            fields = []
            for element in driver.find_elements(By.CSS_SELECTOR, "input"):
                if not element.is_displayed():
                    continue
                attrs = {
                    key: element.get_attribute(key)
                    for key in ("type", "name", "id", "autocomplete", "inputmode", "aria-label")
                }
                fields.append(
                    " ".join(f"{k}={v}" for k, v in attrs.items() if v)
                    or "(속성 없음)"
                )
            logger.info(
                "스레드 로그인 대기 중 알 수 없는 화면 | url=%s | 보이는 입력칸 %d개: %s",
                safe_url_for_log(driver.current_url),
                len(fields),
                " / ".join(fields[:6]) or "없음",
            )
        except Exception as error:
            logger.info("스레드 화면 진단 실패 (%s)", type(error).__name__)

    def _type_verification_code(self, driver, field, code: str) -> bool:
        """코드를 칸에 넣고 제출한다. 넣지 못했으면 False."""
        from selenium.webdriver.common.keys import Keys

        from .naver.login import _type_input_value

        try:
            _type_input_value(driver, field, code)
            time.sleep(HUMAN_PAUSE_SECONDS)
            # 확인 버튼은 난독화 DOM이라 Enter 제출을 우선한다(로그인 폼과 같은 이유).
            field.send_keys(Keys.ENTER)
            logger.info("스레드 2단계 인증 코드를 입력했습니다 — 확인 대기 중.")
            return True
        except Exception as error:
            logger.warning(
                "스레드 2단계 인증 코드 입력 실패 (%s)",
                type(error).__name__,
            )
            return False

    def _wait_for_login_form(self, driver):
        """비밀번호 칸이 화면에 뜰 때까지 기다린다. 못 찾으면 None.

        `driver.get()`은 문서 로드에서 돌아오는데 threads.com은 React 앱이라 그 시점에는
        입력칸이 **아직 DOM에 없다**. 예전에는 곧바로 찾아서 늘 빈손이었고, 그래서 아이디·
        비밀번호가 한 번도 입력되지 않은 채 "직접 로그인해 주세요"로 넘어갔다
        (2026-08-04 실사용에서 확인).

        `www.threads.net`으로 들어가면 `threads.com`으로 리다이렉트되므로 그 시간도 여기서
        함께 흡수한다.
        """
        from selenium.webdriver.common.by import By

        deadline = time.monotonic() + LOGIN_FORM_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                for element in driver.find_elements(By.CSS_SELECTOR, "input[type='password']"):
                    if element.is_displayed() and element.is_enabled():
                        return element
            except Exception:
                pass
            time.sleep(0.5)
        return None

    def _fill_login_form(self, driver, credentials) -> None:
        """저장된 자격증명을 로그인 폼에 입력하고 제출한다. best-effort —
        폼을 못 찾으면(화면 변형) 수동 로그인 안내로 넘어간다."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        from .naver.login import _type_input_value

        try:
            # 비밀번호 칸이 뜨는 것을 폼이 준비된 신호로 본다 — 아이디 칸만 보고 판단하면
            # 검색창 같은 다른 text 입력을 잡을 수 있다.
            password_input = self._wait_for_login_form(driver)
            username_input = next(
                (
                    element
                    for selector in (
                        "input[autocomplete='username']",
                        "input[type='text']",
                        "input:not([type='password'])",
                    )
                    for element in driver.find_elements(By.CSS_SELECTOR, selector)
                    if element.is_displayed()
                ),
                None,
            )
            if username_input is None or password_input is None:
                logger.info(
                    "스레드 로그인 폼을 찾지 못했습니다 — 열린 창에서 직접 로그인해 주세요."
                )
                self._describe_unknown_screen(driver)
                return
            _type_input_value(driver, username_input, credentials.username)
            # 사람은 칸을 옮길 때 잠깐 멈춘다 — 아이디에서 비밀번호로 바로 잇지 않는다.
            time.sleep(HUMAN_PAUSE_SECONDS)
            _type_input_value(driver, password_input, credentials.password)
            time.sleep(HUMAN_PAUSE_SECONDS)
            # '로그인' 버튼은 난독화 DOM이라 Enter 제출을 우선한다.
            password_input.send_keys(Keys.ENTER)
            logger.info("저장된 스레드 로그인 정보를 입력했습니다 — 로그인 확인 대기 중.")
        except Exception as error:
            logger.warning(
                "스레드 로그인 자동 입력 실패(직접 로그인해 주세요) (%s)",
                type(error).__name__,
            )

    # --- 게시 ----------------------------------------------------------------

    # --- 연속 스레드 채우기 --------------------------------------------------

    def _append_remaining_threads(self, driver, rest: list[ThreadPiece]) -> None:
        """두 번째 이후 스레드를 작성창에 이어 붙인다. 하나도 없으면 아무것도 하지 않는다.

        **'스레드에 추가'를 누를 때마다 칸이 하나씩 늘어난다**(2026-08-04 사용자 실측).
        한 번은 이것을 '이미 놓인 빈 칸'으로 보고 누르지 않게 고쳤는데, 그러면 칸이 늘지
        않아 첫 스레드에만 글이 들어갔다. 지금은 누르는 쪽이 주 경로다.

        마지막에 '게시'를 한 번 누르면 전부 연속 게시되므로 답글 체인을 돌 필요가 없다.

        **부분 게시를 만들지 않는다.** 중간에 실패하면 예외로 올려 발행 전체를 실패시킨다 —
        5개짜리 글이 2개만 올라가는 것이 조용한 성공보다 나쁘다(fail-closed).
        """
        if not rest:
            return
        total = len(rest) + 1
        # **순번이 아니라 요소 자체를 기억한다.** 예전에는 `boxes[index - 1]`로 집었는데,
        # 작성창 안에 글칸이 아닌 입력 요소가 하나라도 섞이면(주제 고르는 칸 등) 순번이
        # 통째로 밀려 엉뚱한 요소에 글을 넣는다. 그러면 **조용히** 앞 스레드만 올라간다 —
        # 실사용에서 6개짜리 글이 오류 없이 2개만 게시됐다(2026-08-04).
        used = self._boxes_already_written(driver)
        for index, piece in enumerate(rest, start=2):
            box = self._wait_for_composer_box(driver, index, used)
            self._type_into_composer(driver, box, piece.text)
            self._confirm_text_landed(driver, index, box, piece.text)
            used.append(box)
            self._attach_images(driver, index, piece.images)
            logger.info("스레드 %d/%d 입력 완료", index, total)

    def _confirm_text_landed(self, driver, index: int, box, text: str) -> None:
        """방금 넣은 글이 그 칸에 실제로 들어갔는가. 아니면 발행 전체를 멈춘다.

        빈 칸으로 게시되면 그 스레드만 조용히 사라진다 — 어디가 비었는지 사용자는 모른다.
        그러느니 이유를 대고 멈추는 편이 낫다(fail-closed).
        """
        expected = (text.strip().splitlines() or [""])[0][:20].strip()
        if not expected:
            return
        for _ in range(3):
            time.sleep(0.4)
            try:
                if expected in (box.text or ""):
                    return
            except Exception:
                break
        self._describe_composer(driver)
        raise RuntimeError(
            f"{index}번째 스레드에 글이 들어가지 않았습니다 — 입력칸을 잘못 집었을 수 "
            "있습니다. 일부만 올라간 글을 남기지 않도록 발행을 중단합니다."
        )

    def _composer_boxes(self, driver) -> list:
        """작성창 안의 글 입력칸들. 순서가 곧 스레드 순서다.

        Threads 작성창은 `contenteditable` div다(input이 아니다). 난독화 클래스 대신
        역할(`role='textbox'`)로 찾고, 범위는 작성 모달 안으로 좁힌다 — 배경 피드에도
        입력칸이 있어서(`새로운 소식이 있나요?`) 화면 전체를 뒤지면 그것을 집는다.
        """
        from selenium.webdriver.common.by import By

        try:
            scope = self._dialog_scope(driver)
            return [
                box
                for box in scope.find_elements(
                    By.CSS_SELECTOR, "div[contenteditable='true'], [role='textbox']"
                )
                if box.is_displayed()
            ]
        except Exception:
            return []

    def _wait_for_composer_box(self, driver, index: int, used: list):
        """아직 쓰지 않은 입력칸을 확보한다 — 없으면 '스레드에 추가'를 눌러 만든다.

        ``used``는 이미 글을 넣은 칸들이다. 순번으로 집지 않고 **써 본 적 없는 칸**을
        찾는 이유는 위 `_append_remaining_threads` 주석에 적었다.

        비어 있는 칸이 이미 있으면 누르지 않는다 — 또 누르면 빈 칸이 하나 남는다.
        """
        box = self._fresh_box(driver, used)
        if box is not None:
            return box

        # 방금 친 글이 반영되기 전에 버튼을 찾으면 자리표시자가 아직 없을 수 있다.
        # 실사용에서 2번째 스레드 추가가 가끔 실패한 이유로 의심되는 것이다(2026-08-04).
        time.sleep(ADD_THREAD_SETTLE_SECONDS)
        for attempt in range(1, ADD_THREAD_MAX_ROUNDS + 1):
            for candidate in self._add_thread_candidates(driver):
                self._click(driver, candidate)
                box = self._poll_for_fresh_box(driver, used, ADD_THREAD_GROW_SECONDS)
                if box is not None:
                    return box
                logger.info(
                    "'스레드에 추가'를 눌렀지만 %d번째 칸이 생기지 않았습니다 — 다른 후보를 시도합니다.",
                    index,
                )
            if attempt < ADD_THREAD_MAX_ROUNDS:
                logger.info("%d번째 칸 만들기를 다시 시도합니다 (%d회차).", index, attempt + 1)
                time.sleep(ADD_THREAD_SETTLE_SECONDS)

        self._describe_composer(driver)
        raise RuntimeError(
            f"{index}번째 스레드 입력칸을 찾지 못했습니다 "
            f"(현재 {len(self._composer_boxes(driver))}개) — 작성창 구조가 바뀐 것 같습니다."
        )

    def _boxes_already_written(self, driver) -> list:
        """이미 글이 들어 있는 칸들 — 첫 스레드는 인텐트 URL이 채워 뒀다.

        첫 칸을 '목록의 0번'으로 잡으면 안 된다. 글칸이 아닌 입력 요소가 목록 앞에 섞이면
        그것을 첫 스레드로 착각하고, 진짜 첫 스레드 위에 두 번째 글을 덮어쓴다.
        """
        boxes = self._composer_boxes(driver)
        written = []
        for box in boxes:
            try:
                if (box.text or "").strip():
                    written.append(box)
            except Exception:
                continue
        # 아무 칸도 읽지 못했으면 첫 칸을 첫 스레드로 본다(인텐트가 채운 자리).
        return written or boxes[:1]

    def _fresh_box(self, driver, used: list):
        """아직 글을 넣지 않은 입력칸. 없으면 None.

        **이미 쓴 칸보다 뒤에서** 찾는다 — 글칸은 문서 순서대로 늘어나므로, 앞쪽에 섞인
        다른 입력 요소(주제 고르는 칸 등)를 새 글칸으로 착각하지 않는다.
        """
        boxes = self._composer_boxes(driver)
        start = 0
        for position, box in enumerate(boxes):
            if box in used:
                start = position + 1
        for box in boxes[start:]:
            if box not in used:
                return box
        return None

    def _poll_for_fresh_box(self, driver, used: list, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            box = self._fresh_box(driver, used)
            if box is not None:
                return box
            time.sleep(0.3)
        return None

    def _wait_for_composer_modal(self, driver, timeout: float) -> bool:
        """작성 **모달**이 뜰 때까지 기다린다. 이미지 첨부가 작성창을 기다릴 때 쓴다.

        입력칸 개수로 판정하면 안 된다 — 모달이 뜨기 전에는 `_composer_boxes`가 화면
        전체로 물러나 배경 피드의 입력칸("새로운 소식이 있나요?")을 세고, 그 순간 파일
        입력칸도 배경 것을 집는다. 그러면 사진이 아무 데도 붙지 않은 채 조용히 사라진다
        (2026-08-04 실사용: 미리보기 30초 경고 뒤 게시물에 사진이 없었다).
        """
        deadline = time.monotonic() + timeout
        while True:
            if self._composer_open(driver):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.3)

    def _add_thread_candidates(self, driver) -> list:
        """'스레드에 추가'를 누를 후보들 — 확실한 순서다(가장 안쪽 버튼 먼저).

        글자로 찾으면 그 글자를 품은 **조상까지 전부** 걸린다. 문서 순서 그대로 첫 번째를
        쓰면 가장 바깥 래퍼를 집는데, 좌표 클릭은 그 래퍼의 가운데 빈 공간을 눌러 아무 일도
        일어나지 않는다. 그래서 버튼 역할을 가진 것을 먼저, 그중에서도 안쪽을 먼저 쓴다.
        """
        from selenium.webdriver.common.by import By

        # `.//`로 시작해야 범위 제한이 실제로 먹는다 — `//`는 문서 전체를 뒤진다.
        xpath = ".//*[normalize-space()='스레드에 추가' or normalize-space()='Add to thread']"
        try:
            matches = [
                element
                for element in self._dialog_scope(driver).find_elements(By.XPATH, xpath)
                if element.is_displayed()
            ]
        except Exception:
            return []

        def innermost_first(elements: list) -> list:
            # 다른 후보를 품고 있을수록 바깥이다 — 품은 개수가 적은 것부터.
            return sorted(
                elements,
                key=lambda element: sum(1 for other in elements if _contains(element, other)),
            )

        buttons = [element for element in matches if _is_button(element)]
        others = [element for element in matches if element not in buttons]
        return (innermost_first(buttons) + innermost_first(others))[:ADD_THREAD_MAX_CANDIDATES]

    def _describe_composer(self, driver) -> None:
        """작성창의 구조를 한 줄로 남긴다(진단 전용). 글 내용은 싣지 않는다."""
        from selenium.webdriver.common.by import By

        try:
            scope = self._dialog_scope(driver)
            labels = []
            for element in scope.find_elements(By.CSS_SELECTOR, "[role='button'], button"):
                if not element.is_displayed():
                    continue
                name = (
                    element.get_attribute("aria-label") or (element.text or "").strip() or "(이름 없음)"
                )
                labels.append(name[:20])
            logger.info(
                "작성창 진단 | 입력칸 %d개 · 버튼 %d개: %s",
                len(self._composer_boxes(driver)),
                len(labels),
                " / ".join(labels[:12]) or "없음",
            )
        except Exception as error:
            logger.info("작성창 진단 실패 (%s)", type(error).__name__)

    def _type_into_composer(self, driver, box, text: str) -> None:
        """contenteditable 칸에 글을 넣는다.

        `_type_input_value`(네이버용)는 `input.value`를 다루므로 contenteditable에는 쓸 수
        없다. 여기서는 실제 키 입력을 쓴다 — Threads는 React라 값을 직접 꽂으면 상태가
        갱신되지 않아 게시할 때 빈 글로 나간다.

        줄바꿈은 Shift+Enter다. 그냥 Enter는 문단 대신 **새 스레드**를 만들 수 있다.
        """
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        self._click(driver, box)
        actions = ActionChains(driver)
        for index, line in enumerate(text.splitlines() or [""]):
            if index:
                actions.key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT)
            if line:
                actions.send_keys(line)
        actions.perform()

    # --- 이미지 첨부 ----------------------------------------------------------

    def _attach_images(self, driver, index: int, data_urls: tuple[str, ...]) -> None:
        """``index``번째 스레드에 원고의 이미지를 올린다. best-effort.

        작성창의 파일 입력칸(`input[type='file']`)에 경로를 `send_keys`로 넣는다 — 파일
        선택 대화상자는 OS 창이라 Selenium이 만질 수 없지만, 입력칸에 직접 넣으면 대화상자
        없이 업로드된다(숨어 있어도 된다).

        **작성 모달이 뜬 것을 확인한 뒤에만 넣는다**(_wait_for_composer_modal 주석 참고).
        넣었는데 미리보기가 하나도 안 붙으면 입력칸을 새로 찾아 한 번 더 넣는다 —
        미리보기는 로컬에서 즉시 그려지므로, 시간 안에 안 붙었다는 것은 느린 업로드가
        아니라 파일이 등록되지 않았다는 뜻이다(다시 넣어도 중복되지 않는다).

        **못 올려도 발행을 막지 않는다.** 글이 통째로 안 올라가는 것보다 그림 없이
        올라가는 편이 낫고, 실패하면 진단 로그에 실제 입력칸 구조가 남아 고칠 단서가 된다.
        """
        if not data_urls:
            return
        paths = self._write_image_files(index, data_urls)
        if not paths:
            return
        if not self._wait_for_composer_modal(driver, COMPOSER_TIMEOUT_SECONDS / 2):
            logger.warning(
                "%d번째 스레드의 이미지 %d장을 올리지 못했습니다 — 작성 모달이 뜨지 않았습니다.",
                index,
                len(paths),
            )
            return
        # 모달이 방금 떴다면 파일 입력칸이 아직 배선 전일 수 있다 — 잠깐 두었다 넣는다.
        time.sleep(FILE_INPUT_SETTLE_SECONDS)
        for attempt, wait_seconds in (
            (1, IMAGE_UPLOAD_FIRST_WAIT_SECONDS),
            (2, IMAGE_UPLOAD_TIMEOUT_SECONDS),
        ):
            field = self._file_input_for(driver, index)
            if field is None:
                logger.warning(
                    "%d번째 스레드의 이미지 %d장을 올리지 못했습니다 — 파일 입력칸을 찾지 못했습니다.",
                    index,
                    len(paths),
                )
                self._describe_composer(driver)
                return
            before = self._uploaded_preview_count(driver)
            try:
                # 여러 장은 줄바꿈으로 구분해 한 번에 넣는다(Selenium의 다중 파일 규약).
                field.send_keys("\n".join(str(path) for path in paths))
            except Exception as error:
                logger.warning(
                    "%d번째 스레드 이미지 업로드 실패(그림 없이 계속합니다) (%s)",
                    index,
                    type(error).__name__,
                )
                return
            if self._wait_for_previews(driver, index, before, len(paths), wait_seconds):
                return
            attached = self._uploaded_preview_count(driver) - before
            if attached > 0:
                # 일부는 붙었다 — 다시 넣으면 붙은 장이 두 번 올라간다. 여기서 멈춘다.
                logger.warning(
                    "스레드 %d의 이미지가 %d장 중 %d장만 붙었습니다 — 나머지 없이 게시될 수 있습니다.",
                    index,
                    len(paths),
                    attached,
                )
                self._describe_composer(driver)
                self._describe_file_inputs(driver)
                return
            if attempt == 1:
                logger.info(
                    "스레드 %d의 이미지 미리보기가 붙지 않았습니다 — 파일 입력칸을 다시 찾아 한 번 더 넣습니다.",
                    index,
                )
                # 첫 전송이 등록되지 않은 순간의 입력칸 구조가 재발 원인을 밝힐 단서다.
                self._describe_file_inputs(driver)
        logger.warning(
            "스레드 %d의 이미지 미리보기가 %.0f초 안에 붙지 않았습니다(%d장 시도, 재시도 포함) — "
            "그림 없이 게시될 수 있습니다.",
            index,
            IMAGE_UPLOAD_TIMEOUT_SECONDS,
            len(paths),
        )
        self._describe_composer(driver)
        self._describe_file_inputs(driver)

    def _wait_for_previews(
        self, driver, index: int, before: int, expected: int, timeout: float
    ) -> bool:
        """미리보기가 붙을 때까지 기다린다. 붙었으면 True다.

        게시된 글에서 사진이 빠져 있었다(2026-08-04 실사용). 업로드가 끝나기 전에 '게시'를
        누르면 그렇게 된다. 그래서 `send_keys` 뒤에 고정 시간을 자는 대신 **미리보기가
        실제로 붙었는지**를 본다. 안 붙었을 때의 처리(재시도·경고)는 _attach_images가 한다.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if self._uploaded_preview_count(driver) - before >= expected:
                # 붙은 뒤에도 스레드 서버가 처리 중일 수 있다 — 잠깐 더 둔다.
                time.sleep(IMAGE_UPLOAD_SETTLE_SECONDS)
                logger.info("스레드 %d에 이미지 %d장을 올렸습니다.", index, expected)
                return True
        return False

    def _describe_file_inputs(self, driver) -> None:
        """파일 입력칸의 구조를 한 줄로 남긴다(진단 전용).

        미리보기가 안 붙는 실패는 엉뚱한 입력칸에 넣었을 가능성이 크다 — 다음 실측에서
        어떤 칸이 몇 개 있었는지가 남아야 선택 규칙을 고칠 수 있다. 파일 내용은 싣지 않는다.
        """
        from selenium.webdriver.common.by import By

        try:
            fields = self._dialog_scope(driver).find_elements(By.CSS_SELECTOR, "input[type='file']")
            details = " / ".join(
                f"accept={field.get_attribute('accept') or '?'}"
                f"·multiple={'yes' if field.get_attribute('multiple') else 'no'}"
                for field in fields[:4]
            )
            logger.info("파일 입력칸 진단 | %d개%s", len(fields), f": {details}" if details else "")
        except Exception as error:
            logger.info("파일 입력칸 진단 실패 (%s)", type(error).__name__)

    def _uploaded_preview_count(self, driver) -> int:
        """작성창에 붙은 **올린 사진**의 수. 전체 `img`를 세면 안 된다.

        2026-08-04, 작성창을 실제로 열어 잰 값이다(진단 스크립트는 확인 후 지웠다):

            ① 작성창을 연 직후      img 2개 — {'https': 2}
            ② '스레드에 추가' 뒤     img 3개 — {'https': 3}
            ③ 스레드 4개·사진 2장 뒤  img 7개 — {'https': 5, 'blob': 2}

        올린 파일의 미리보기는 `blob:`이고 아바타는 CDN(`https:`)인데, **칸이 늘 때마다
        아바타가 하나씩 는다**(2→3→5). 그래서 전체 개수로 판정하면 사진이 하나도 안
        올라갔는데 올라간 것으로 착각한다 — 사진이 가끔 빠지던 이유다.
        """
        from selenium.webdriver.common.by import By

        try:
            images = self._dialog_scope(driver).find_elements(By.CSS_SELECTOR, "img")
        except Exception:
            return 0
        uploaded = 0
        for image in images:
            try:
                source = image.get_attribute("src") or ""
            except Exception:
                continue
            if source.startswith("blob:") or source.startswith("data:"):
                uploaded += 1
        return uploaded

    def _write_image_files(self, index: int, data_urls: tuple[str, ...]) -> list[Path]:
        """data URL을 임시 파일로 푼다. 못 푸는 장은 건너뛴다(경고만 남긴다)."""
        if self._image_dir is None:
            self._image_dir = Path(tempfile.mkdtemp(prefix=THREAD_IMAGE_TEMP_PREFIX))
        paths: list[Path] = []
        for order, url in enumerate(data_urls, start=1):
            decoded = decode_data_url(url)
            if decoded is None:
                logger.warning("%d번째 스레드의 이미지 %d장째를 읽지 못했습니다 — 건너뜁니다.", index, order)
                continue
            raw, suffix = decoded
            path = self._image_dir / f"thread{index:02d}-{order:02d}{suffix}"
            path.write_bytes(raw)
            paths.append(path)
        return paths

    def _file_input_for(self, driver, index: int):
        """``index``번째 스레드가 쓸 파일 입력칸. **작성 모달 안에서만** 찾는다.

        모달이 없으면 None이다 — 화면 전체로 물러나면 배경 피드의 파일 입력칸을 집고,
        거기 넣은 사진은 미리보기도 게시물도 되지 않은 채 사라진다(2026-08-04 실사용
        의심 원인). 그럴 바에는 못 찾았다고 알리는 편이 낫다.

        칸마다 하나씩 있으면 순번이 맞고, 작성창에 하나뿐이면(현재 편집 중인 칸을 따라감)
        그것을 쓴다. 숨어 있는 입력칸도 `send_keys`는 받으므로 `is_displayed()`로 거르지
        않는다 — 거르면 실제로 쓸 수 있는 칸을 놓친다.
        """
        from selenium.webdriver.common.by import By

        scope = self._dialog_scope(driver)
        if scope is driver:
            return None
        try:
            fields = scope.find_elements(By.CSS_SELECTOR, "input[type='file']")
        except Exception:
            return None
        if not fields:
            return None
        return fields[index - 1] if len(fields) >= index else fields[-1]


    # --- 커뮤니티 또는 주제 ------------------------------------------------------

    def _set_topic(self, driver, topic: str) -> None:
        """'커뮤니티 또는 주제'에 글의 주제를 넣는다. best-effort.

        주제는 분류 태그일 뿐이라 못 넣었다고 발행을 막지 않는다 — 빠져도 글 자체는
        온전히 올라간다.
        """
        if not topic.strip():
            return
        field = self._find_menu_item(driver, ("커뮤니티 또는 주제", "Add a topic"))
        if field is None:
            logger.info("'커뮤니티 또는 주제' 칸을 찾지 못했습니다 — 주제 없이 진행합니다.")
            return
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            from selenium.webdriver.common.keys import Keys

            self._click(driver, field)
            time.sleep(0.4)
            ActionChains(driver).send_keys(topic.strip()).perform()
            time.sleep(0.8)
            # 목록에서 첫 후보를 고른다. 새 주제를 만드는 화면도 같은 조작으로 확정된다.
            ActionChains(driver).send_keys(Keys.ENTER).perform()
            logger.info("스레드 주제를 '%s'로 설정했습니다.", topic.strip())
        except Exception as error:
            logger.info(
                "스레드 주제 설정 실패(주제 없이 진행합니다) (%s)",
                type(error).__name__,
            )

    def _find_menu_item(self, driver, labels: tuple[str, ...]):
        """글자로 찾는 메뉴 항목 중 **가장 안쪽** 요소. 없으면 None.

        메뉴는 작성 모달 **밖에** 뜰 수 있어 화면 전체에서 찾는다(모달로 좁히면 놓친다).
        조상까지 잡히므로 가장 안쪽만 남긴다 — 안 그러면 화면 전체가 클릭 대상이 된다.
        """
        from selenium.webdriver.common.by import By

        condition = " or ".join(
            f"contains(normalize-space(), '{label}')" for label in labels
        )
        try:
            matches = [
                element
                for element in driver.find_elements(By.XPATH, f"//*[{condition}]")
                if element.is_displayed()
            ]
        except Exception:
            return None
        innermost = [
            element
            for element in matches
            if not any(_contains(element, other) for other in matches if other != element)
        ]
        return innermost[0] if innermost else None

    def _wait_for_post_button(self, driver):
        """작성 다이얼로그의 '게시' 버튼. 난독화 클래스 대신 문구·역할로 찾는다."""
        from selenium.webdriver.common.by import By

        # normalize-space 정확 일치 — '게시물' 같은 다른 요소를 물지 않는다.
        #
        # `.//`로 시작해야 한다. `//`로 시작하면 요소에 대고 호출해도 **문서 전체**를
        # 뒤지므로, 아래에서 범위를 모달로 좁혀도 아무 효과가 없다.
        xpath = (
            ".//*[self::button or @role='button']"
            "[normalize-space()='게시' or normalize-space()='Post']"
        )
        deadline = time.monotonic() + COMPOSER_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            # 배경 피드에도 '게시' 버튼이 있다. 작성 모달 안에서만 찾는다 — 뒤 버튼을
            # 집으면 그 좌표 클릭이 모달 뒷배경을 눌러 "삭제하시겠어요?"가 뜬다
            # (2026-08-04 실사용).
            for element in self._dialog_scope(driver).find_elements(By.XPATH, xpath):
                try:
                    if element.is_displayed() and element.is_enabled():
                        return element
                except Exception:
                    continue
            time.sleep(0.5)
        raise RuntimeError(
            "스레드 작성창의 '게시' 버튼을 찾지 못했습니다. 스레드 화면이 바뀌었거나 "
            "작성창이 열리지 않았습니다 — 열린 창을 확인해 주세요."
        )

    @staticmethod
    def _click(driver, element) -> None:
        from selenium.webdriver.common.action_chains import ActionChains

        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element
            )
            ActionChains(driver).move_to_element(element).pause(0.2).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click();", element)

    def _click_post(self, driver, button) -> None:
        """'게시'를 누르고, 눌리지 않았으면 다른 방법으로 다시 시도한다.

        실사용(2026-08-04)에서 게시가 눌리지 않았다. 원인이 될 수 있는 것이 둘이다.
        하나는 클릭 좌표에 다른 요소가 겹쳐 있는 경우(ActionChains는 좌표를 누른다),
        다른 하나는 React가 합성 클릭을 흘리는 경우다.

        그래서 **좌표 클릭 → 요소 직접 클릭** 순으로 시도하고, 작성창이 닫히는지로
        성공을 판정한다. 닫히지 않으면 다음 방법으로 넘어간다.

        **다시 누르기 전에 충분히 기다린다.** 스레드 여러 개에 사진까지 붙은 글은 게시가
        몇 초로 끝나지 않는다. 짧게 기다렸다 또 누르면 발행 중인 것을 한 번 더 누르는
        셈이 된다(POST_CLICK_CONFIRM_SECONDS 주석 참고).
        """
        attempts = (
            ("좌표 클릭", lambda: self._click(driver, button)),
            ("요소 클릭", lambda: driver.execute_script("arguments[0].click();", button)),
        )
        for name, attempt in attempts:
            try:
                attempt()
            except Exception as error:
                logger.info("게시 %s 실패 (%s)", name, type(error).__name__)
                continue
            if self._composer_gone(driver, timeout=POST_CLICK_CONFIRM_SECONDS):
                logger.info("게시 버튼을 눌렀습니다 (%s).", name)
                return
            logger.info(
                "게시 %s 뒤 %.0f초가 지나도 작성창이 그대로입니다 — 다음 방법을 시도합니다.",
                name,
                POST_CLICK_CONFIRM_SECONDS,
            )

        # 여기까지 왔으면 눌리지 않은 것이다. 아래 _wait_for_composer_closed가
        # 최종 판정을 하고, 그때 실패 사유가 사용자에게 전달된다.
        self._describe_composer(driver)

    def _composer_gone(self, driver, *, timeout: float) -> bool:
        """작성창이 닫혔는가. 게시가 접수된 신호다."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.4)
            if not self._composer_open(driver):
                return True
        return False

    def _composer_open(self, driver) -> bool:
        """작성 모달이 열려 있는가.

        `_composer_boxes`로 판정하면 안 된다 — 모달이 없을 때 그 함수는 화면 전체로
        물러나고, 배경 피드의 '새로운 소식이 있나요?' 입력칸을 세어 **영원히 열려 있다**고
        답한다. 여기서는 `[role='dialog']` 안에 입력칸이 있는지만 본다.
        """
        from selenium.webdriver.common.by import By

        try:
            for dialog in driver.find_elements(By.CSS_SELECTOR, "[role='dialog']"):
                if not self._safe_displayed(dialog):
                    continue
                boxes = dialog.find_elements(
                    By.CSS_SELECTOR, "div[contenteditable='true'], [role='textbox']"
                )
                if any(self._safe_displayed(box) for box in boxes):
                    return True
        except Exception:
            # 화면을 읽지 못하면 "열려 있다"고 단정하지 않는다 — 최종 판정은 프로필이 한다.
            return False
        return False

    def _wait_for_composer_closed(self, driver) -> None:
        """작성창이 닫히기를 기다린다. **닫힘 판정은 힌트일 뿐 최종 판정이 아니다.**

        예전에는 여기서 화면 **전체**의 '게시' 버튼을 세고, 하나라도 남아 있으면 실패로
        올렸다. 그런데 작성창이 닫힌 뒤에도 뒤 피드에 '게시' 버튼이 남아 있어서, **실제로는
        게시된 글**을 "작성창이 닫히지 않았습니다"로 실패 보고했다(2026-08-04 실사용 —
        사용자가 프로필에서 글을 확인해 알려 줬다).

        지금은 작성 **모달**이 사라졌는지만 본다. 그래도 판정이 어긋날 수 있으므로 여기서는
        예외를 올리지 않는다 — 게시 여부의 최종 판정은 프로필에서 글을 찾는
        `_verify_on_profile`이 한다(그쪽이 fail-closed다).
        """
        if self._composer_gone(driver, timeout=PUBLISH_CONFIRM_TIMEOUT_SECONDS):
            return
        logger.warning(
            "게시 후에도 작성창이 남아 있습니다 — 프로필에서 글을 찾아 실제 게시 여부를 확인합니다."
        )

    @staticmethod
    def _safe_displayed(element) -> bool:
        try:
            return element.is_displayed()
        except Exception:
            return False

    def _let_the_publish_finish(self, driver, pieces: list[ThreadPiece]) -> None:
        """작성창이 닫힌 뒤에도 브라우저가 하던 일을 끝낼 시간을 준다.

        **작성창이 닫히는 것은 "접수됐다"이지 "다 올라갔다"가 아니다.** 스레드는 모달을
        먼저 닫고 나머지 스레드와 사진을 뒤에서 계속 올린다. 그런데 바로 다음 단계
        (`_verify_on_profile`)가 `driver.get(프로필)`으로 **페이지를 떠나 버려서**, 아직
        날아가지 않은 요청이 그대로 취소됐다.

        사용자가 본 두 증상이 이것 하나로 설명된다(2026-08-04):
        "작성 시에 스레드 추가해서 내용이 잘 들어가는데 정작 게시하면 스레드 하나만
        올라가네. 또한 이미지들도 추가가 안되는 경우도 있고."

        기다리는 시간은 올릴 것의 양에 맞춘다 — 스레드가 많고 사진이 많을수록 길다.
        """
        images = sum(len(piece.images) for piece in pieces)
        seconds = min(
            PUBLISH_SETTLE_MAX_SECONDS,
            PUBLISH_SETTLE_BASE_SECONDS
            + PUBLISH_SETTLE_PER_THREAD_SECONDS * max(0, len(pieces) - 1)
            + PUBLISH_SETTLE_PER_IMAGE_SECONDS * images,
        )
        logger.info(
            "게시 접수됨 — 스레드 %d개·사진 %d장이 다 올라가도록 %.0f초 기다립니다.",
            len(pieces),
            images,
            seconds,
        )
        time.sleep(seconds)

    def _verify_on_profile(self, driver, text: str) -> str | None:
        """프로필에서 방금 올린 글이 실제로 보여야 성공이다.

        스레드는 게시가 비동기라(잠깐 뒤 피드 반영) 몇 초 재시도한다. 확인 조각은
        글의 첫 줄(제목) 앞부분 — 인텐트로 넣은 텍스트 그대로다.

        **확인은 새 탭에서 한다.** 발행하던 탭을 그대로 `driver.get`으로 옮기면, 아직
        올라가는 중인 나머지 스레드와 사진 요청이 통째로 취소된다(위
        `_let_the_publish_finish` 주석 참고). 원래 탭은 건드리지 않는다.
        """
        from selenium.webdriver.common.by import By

        snippet = text.splitlines()[0][:30].strip()
        profile_href = None
        for element in driver.find_elements(By.CSS_SELECTOR, "a[href^='/@']"):
            href = element.get_attribute("href") or ""
            if href:
                profile_href = href
                break
        if profile_href is None:
            logger.warning("프로필 링크를 찾지 못해 게시 확인을 건너뜁니다(작성창 닫힘까지는 확인됨).")
            return None

        try:
            driver.switch_to.new_window("tab")
        except Exception as error:
            # 새 탭을 못 열면 예전처럼 같은 탭에서 확인한다 — 확인을 건너뛰는 것보다 낫다.
            logger.info(
                "확인용 새 탭을 열지 못해 같은 탭에서 확인합니다 (%s)",
                type(error).__name__,
            )

        deadline = time.monotonic() + PUBLISH_CONFIRM_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            driver.get(profile_href)
            time.sleep(2)
            if snippet and snippet in (driver.page_source or ""):
                for element in driver.find_elements(By.CSS_SELECTOR, "a[href*='/post/']"):
                    href = element.get_attribute("href") or ""
                    if href:
                        return href
                return profile_href
        raise RuntimeError(
            "게시 후 프로필에서 글을 확인하지 못했습니다. 발행되지 않았을 수 있습니다 — "
            "열린 창에서 확인해 주세요."
        )
