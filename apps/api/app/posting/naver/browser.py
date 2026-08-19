"""크롬 실행과 드라이버 생성, 네이버 세션 쿠키 확인, 브라우저 스레드 헬퍼.

네이버에는 글쓰기 API가 없어 사용자의 PC에서 실제 Chrome을 연다. 브라우저 프로필은
저장소 루트의 ``.naver-profile``에 유지하고, 로그인 정보는 세션이 없을 때만 쓴다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .constants import CLOSE_FLUSH_SECONDS, PAGE_LOAD_TIMEOUT_SECONDS, SESSION_COOKIES

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------- 프로필 차례
#
# **Chrome 프로필 하나는 한 번에 한 창만 쓸 수 있다.** 그런데 브라우저 작업은 호출마다
# 새 스레드를 띄우므로(`_in_browser_thread`), 발행 두 건이 겹치면 둘 다 같은
# `.naver-profile`로 Chrome을 열려 하고 나중 것이 "user data directory is already in
# use"로 죽는다.
#
# 2026-08-07 실사용에서 그대로 났다: 예약 두 건의 원고가 나란히 완성돼(준비는 최대 3편
# 동시다) 35초 간격으로 발행이 시작됐고, 앞 건이 아직 Chrome을 붙들고 있어 뒤 건이
# 실패했다. 화면에는 '추가 인증이 필요합니다'로만 보여서, 이미 2단계 인증을 마친
# 사용자가 왜 또 인증을 하라는지 알 수 없었다.
#
# 워커에도 '발행은 하나씩' 장치가 있지만 그것만으로는 부족하다 — 예약 발행과 사용자가
# 화면에서 누른 발행, 설정 화면의 로그인은 서로 다른 경로라 그 장치를 지나지 않는다.
# **정말 하나뿐인 자원(프로필)에 자물쇠를 건다.** 그러면 어느 경로로 들어와도 차례를
# 기다린다.
_PROFILE_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()

# 차례를 기다리는 최대 시간(초). 앞 작업이 끝나기를 기다리는 것이 목적이므로 넉넉해야
# 한다 — 발행 한 건이 1~2분이고, 설정 화면의 로그인은 사람이 2단계 인증을 마칠 때까지
# 최대 7분을 기다린다(constants.SETTINGS_LOGIN_TIMEOUT_SECONDS). 그보다 길게 잡되,
# 영원히 기다리지는 않는다: 어딘가에서 자물쇠를 놓지 못하면 발행이 조용히 멈춘다.
PROFILE_WAIT_SECONDS = 600.0

# 기다린 것이 이만큼을 넘으면 로그에 남긴다. 발행이 늦은 이유가 '차례를 기다렸다'인지
# '네이버가 느렸다'인지 나중에 가릴 수 있어야 한다.
PROFILE_WAIT_LOG_SECONDS = 1.0


class ProfileBusy(RuntimeError):
    """앞 브라우저 작업이 너무 오래 프로필을 붙들고 있다.

    이것은 **추가 인증이 아니다.** 부르는 쪽이 그렇게 구분해 알리도록 따로 둔다.
    """


def _profile_lock(profile_dir) -> threading.Lock:
    key = str(profile_dir)
    with _PROFILE_LOCKS_GUARD:
        lock = _PROFILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROFILE_LOCKS[key] = lock
        return lock


@contextmanager
def use_profile(profile_dir):
    """이 프로필로 Chrome을 쓰는 동안 붙잡는다. **브라우저 스레드 안에서 부른다.**

    창을 여는 순간부터 닫거나 열어 둔 채 넘길 때까지가 한 구간이다 — 창을 만든 뒤에만
    잡으면 '앞 창이 아직 안 닫혔는데 뒤 작업이 새 창을 만드는' 바로 그 틈이 남는다.
    """
    lock = _profile_lock(profile_dir)
    started = time.monotonic()
    if not lock.acquire(timeout=PROFILE_WAIT_SECONDS):
        raise ProfileBusy(
            "앞선 네이버 작업이 아직 Chrome을 쓰고 있어 차례를 기다리다 멈췄습니다."
            f" ({PROFILE_WAIT_SECONDS / 60:.0f}분) 열려 있는 자동화 Chrome 창을 닫고"
            " 다시 시도해 주세요."
        )
    waited = time.monotonic() - started
    if waited >= PROFILE_WAIT_LOG_SECONDS:
        logger.info("네이버 Chrome 차례를 %.1f초 기다렸습니다.", waited)
    try:
        yield
    finally:
        lock.release()


@dataclass
class NaverConfig:
    blog_id: str
    profile_dir: Path
    api_origin: str
    username: str | None = None
    password: str | None = None
    # 라이브 뷰(화면 중계) 등록에 쓴다. 관리용 CLI처럼 사용자 없는 호출은 None이고,
    # 그때는 중계 없이 예전 그대로 동작한다.
    user_id: str | None = None

    @property
    def has_session(self) -> bool:
        """Chrome 쿠키 DB가 있을 때만 저장된 세션으로 본다.

        credentials.json이나 blog_id 파일만 있는 빈 프로필을 연결됨으로 표시하지 않는다.
        Selenium/Chrome과 과거 Playwright 프로필에서 쓰는 위치를 모두 인정한다.
        """
        candidates = (
            self.profile_dir / "Cookies",
            self.profile_dir / "Default" / "Cookies",
            self.profile_dir / "Default" / "Network" / "Cookies",
        )
        return any(path.is_file() and path.stat().st_size > 0 for path in candidates)

    @property
    def can_log_in(self) -> bool:
        return bool(self.username and self.password)


class _NeedsHuman(Exception):
    """네이버의 사람 확인이 제한 시간 안에 끝나지 않았다."""


class _BrowserUnavailable(Exception):
    """Chrome 또는 ChromeDriver를 시작할 수 없다."""


def _settle(future: "asyncio.Future[T]", value: T | None, error: BaseException | None) -> None:
    """브라우저 스레드의 결과를 기다리던 요청에 돌려준다.

    요청이 이미 끊겼으면(취소) 조용히 버린다 — 끝난 Future에 값을 넣으면 이벤트 루프에
    InvalidStateError가 쌓인다.
    """
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(value)  # type: ignore[arg-type]


async def _in_browser_thread(work: Callable[[], T]) -> T:
    """동기 Selenium 작업을 FastAPI 이벤트 루프 밖에서 실행한다.

    **데몬 스레드에서 돌린다.** ``asyncio.to_thread``의 기본 실행기는 논-데몬 워커라
    인터프리터가 끝날 때 그 스레드를 **join**한다(atexit은 무한정, asyncio Runner는
    300초). 그런데 설정 화면의 네이버 로그인은 사람이 2단계 인증을 마칠 때까지 최대
    7분을 기다린다 — 그동안 Ctrl+C를 눌러도 서버가 죽지 않았다(2026-08-06 사용자 신고:
    "터미널에서 서버 강제종료도 안돼"). 데몬 스레드는 종료를 붙잡지 않는다.

    열려 있던 Chrome은 서버가 끝나도 남는다. 그것은 원래 이 경로의 정책이고(사용자가
    결과를 눈으로 확인한다), 다음 실행이 시작할 때 ``_release_kept_open_browser``와
    프로필 사용 중 안내가 처리한다.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def run() -> None:
        try:
            result = work()
        except BaseException as error:  # noqa: BLE001 — 그대로 요청에 돌려준다
            payload: tuple[T | None, BaseException | None] = (None, error)
        else:
            payload = (result, None)
        try:
            loop.call_soon_threadsafe(_settle, future, *payload)
        except RuntimeError:
            # 루프가 이미 닫혔다(서버 종료). 돌려줄 곳이 없으니 조용히 끝낸다.
            pass

    threading.Thread(target=run, name="naver-browser", daemon=True).start()
    return await future


def _chrome_candidates() -> tuple[Path, ...]:
    system = platform.system()
    if system == "Windows":
        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        return tuple(
            Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            for root in roots
            if root
        )
    if system == "Darwin":
        return (
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
    return (
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    )


def _find_chrome_binary() -> Path | None:
    configured = (os.environ.get("NAVER_CHROME_BINARY") or "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if path.is_file():
            return path
        raise _BrowserUnavailable(f"NAVER_CHROME_BINARY 경로에 Chrome이 없습니다: {path}")
    return next((path for path in _chrome_candidates() if path.is_file()), None)


def _chrome_major_version(binary: Path | None) -> int | None:
    if platform.system() == "Windows":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon") as key:
                version, _ = winreg.QueryValueEx(key, "version")
            return int(str(version).split(".", 1)[0])
        except Exception:
            # undetected-chromedriver가 크롬을 스스로 찾을 수도 있으니, 브라우저를 거부하지
            # 말고 실행 파일 탐색과 최종적으로 None으로 넘어간다.
            pass
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=5
        )
        match = re.search(r"(\d+)\.", result.stdout)
        return int(match.group(1)) if match else None
    except Exception:
        return None


# 정상적인 Chrome ``Default/Preferences``는 14~20KB다. 이 선을 넘으면 아래 이스케이프
# 폭증에 걸린 것으로 보고 지운다 — 정상 파일이 걸릴 여지가 없도록 넉넉히 잡았다.
_PREFERENCES_MAX_BYTES = 5 * 1024 * 1024


def _reset_bloated_preferences(profile_dir: Path) -> None:
    """비정상적으로 커진 ``Default/Preferences``를 지운다.

    undetected-chromedriver는 실행할 때마다 이 파일을 latin1로 읽어 ``json.dump``로 다시
    쓴다(``handle_prefs``와 exit_type 보정 두 곳). ``json.dump``는 기본이
    ``ensure_ascii=True``라 한글 한 글자가 ``\\uXXXX`` 6바이트로 부풀고, 그때 생긴
    역슬래시가 다음 실행에서 또 이스케이프된다. **발행할 때마다 파일이 몇 배씩 자란다.**

    실제로 한 프로필이 14KB에서 2,416MB(17만 배)까지 자랐고, 그 상태에서는 크롬이
    창도 못 띄운 채 81초 뒤 "cannot connect to chrome"으로 실패했다. 같은 PC의 다른
    프로필은 2.7초에 떴다 — 팀원 PC에서만 되던 것이 이 차이였다.

    지워도 로그인은 풀리지 않는다. 네이버 세션은 ``Cookies``·``Login Data``에 따로 있고
    이 파일에는 화면 설정만 들어 있다. 크롬이 다음 실행에서 새로 만든다.
    """
    prefs = profile_dir / "Default" / "Preferences"
    try:
        size = prefs.stat().st_size
    except OSError:
        return
    if size <= _PREFERENCES_MAX_BYTES:
        return
    try:
        prefs.unlink()
    except OSError as error:
        # 지우지 못해도 실행은 계속한다 — 느릴 뿐 막힌 것은 아니고, 여기서 발행을
        # 중단하면 원인과 무관한 실패가 된다.
        logger.warning("커진 Chrome Preferences를 지우지 못했습니다: %s", error)
        return
    logger.warning(
        "Chrome 프로필 설정 파일이 %.0fMB로 커져 초기화했습니다(로그인 세션은 유지) | %s",
        size / 1e6,
        profile_dir.name,
    )


def bring_to_front(driver) -> None:
    """방금 연 Chrome 창을 **다른 창 위로 올린다.**

    발행은 사용자의 PC에서 실제 Chrome을 열어 진행한다. 그런데 그 창을 띄우는 것은
    백그라운드에서 도는 서버 프로세스라, 사용자가 다른 프로그램을 쓰고 있으면 창이
    **뒤에서 열려** 작업 표시줄만 깜빡였다 — 사용자에게는 "발행이 안 돌고 있다"로
    보인다(2026-08-06 신고: "크롬창이 띄워지면서 발행이 되어야 하는데 다른 작업을
    하거나 다른 탭을 넘어가면 안 보인다").

    세 가지를 차례로 시도한다. 뒤로 갈수록 강하고, 앞의 것이 통하면 그대로 끝난다.

    1. ``Page.bringToFront`` — Chrome DevTools의 공식 방법. 창 안에서 이 탭을 앞으로.
    2. ``maximize_window`` — 최소화돼 열린 창을 펴 준다.
    3. Windows에서는 창 자체를 z-order 맨 위로 올린다(아래 ``_raise_window_windows``).

    **무엇 하나 실패해도 발행은 계속한다.** 창이 뒤에 있는 것은 불편이고, 여기서
    예외를 던지면 그 불편이 발행 실패가 된다.
    """
    try:
        driver.execute_cdp_cmd("Page.bringToFront", {})
    except Exception as error:  # noqa: BLE001
        logger.debug("Page.bringToFront 실패(무시): %s", error)
    try:
        driver.maximize_window()
    except Exception as error:  # noqa: BLE001
        logger.debug("maximize_window 실패(무시): %s", error)
    if platform.system() == "Windows":
        _raise_window_windows(driver)


def _raise_window_windows(driver) -> None:
    """Windows에서 Chrome 창을 z-order 맨 위로 올린다.

    Windows는 **포그라운드 잠금** 때문에 백그라운드 프로세스가 다른 앱의 포커스를
    빼앗는 것을 막는다(그래서 위의 CDP만으로는 창이 뒤에 남는다). 그래서 포커스를
    빼앗는 대신 **z-order만** 올린다: ``SetWindowPos``로 맨 앞에 세우고 곧바로
    '항상 위' 속성을 뗀다. 사용자가 타이핑하던 곳의 키보드 포커스는 그대로 두고
    창만 보이게 하는, 널리 쓰이는 방법이다.

    창 손잡이는 **Chrome 프로세스의 것만** 고른다. 제목으로 찾으면 사용자가 따로
    열어 둔 Chrome 창을 잘못 올릴 수 있다.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as error:  # noqa: BLE001 - Windows가 아니거나 ctypes가 없다
        logger.debug("창 올리기를 건너뜁니다: %s", error)
        return

    pid = _chrome_pid(driver)
    if pid is None:
        return

    user32 = ctypes.windll.user32
    HWND_TOP, HWND_NOTOPMOST, HWND_TOPMOST = 0, -2, -1
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040
    SW_RESTORE = 9
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    raised = 0

    enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def _visit(hwnd, _lparam):
        nonlocal raised
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        # 최상위 창만 본다 — 툴팁·숨은 창까지 올리면 화면이 어지러워진다.
        if owner.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        # 잠깐 '항상 위'로 올렸다가 곧바로 뗀다. 이렇게 하면 포커스를 빼앗지 않고도
        # 다른 창들 앞에 서고, '항상 위'인 채로 남지도 않는다.
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, flags)
        raised += 1
        return True

    try:
        user32.EnumWindows(enum_proc(_visit), 0)
    except Exception as error:  # noqa: BLE001 - 창을 못 올려도 발행은 계속한다
        logger.debug("Chrome 창 올리기 실패(무시): %s", error)
        return
    if raised:
        logger.info("발행용 Chrome 창을 앞으로 올렸습니다(창 %d개).", raised)
    else:
        logger.debug("올릴 Chrome 창을 찾지 못했습니다(pid=%s).", pid)


def _chrome_pid(driver) -> int | None:
    """드라이버가 띄운 **브라우저** 프로세스의 pid. 못 알아내면 None.

    ``driver.browser_pid``는 undetected_chromedriver가 채워 두는 값이다. 없으면
    Selenium이 들고 있는 서비스 프로세스에서 찾는다.
    """
    for source in (
        lambda: getattr(driver, "browser_pid", None),
        lambda: getattr(getattr(driver, "service", None), "process", None).pid,
    ):
        try:
            pid = source()
        except Exception:  # noqa: BLE001
            continue
        if isinstance(pid, int) and pid > 0:
            return pid
    return None


def _kill_orphan_profile_chrome(profile_dir) -> int:
    """이 프로필을 명령줄에 단 chrome.exe만 골라 강제 종료한다. 죽인 개수를 돌려준다.

    이전 백엔드가 확인용으로 열어 둔 크롬은 재시작 후 **고아**가 된다 — 새 백엔드의
    keep-open 목록에 없어 아무도 못 닫고, 프로필을 잠근 채 남아 다음 발행이
    '프로필이 사용 중입니다'로 죽는다. 프로필 경로가 명령줄에 있는 프로세스만 고르므로
    사용자의 개인 크롬(다른 프로필)은 절대 해당되지 않는다. 강제 종료라 그 창의 쿠키
    플러시는 없지만, 고아 창은 어차피 정상 종료시킬 길이 없다(드라이버가 죽었다).
    """
    if platform.system() != "Windows":
        return 0
    marker = str(profile_dir).replace("'", "''")
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$found = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{marker}*' }}; "
                "$found | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
                "($found | Measure-Object).Count",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        count = int((result.stdout or "0").strip().splitlines()[-1] or 0)
    except Exception as error:
        logger.warning("고아 크롬 정리 실패: %s", type(error).__name__)
        return 0
    if count:
        # SingletonLock이 풀릴 시간을 준다 — 곧바로 다시 띄우면 같은 오류가 난다.
        time.sleep(2.0)
        logger.info("프로필을 잠근 고아 크롬 %d개를 정리했습니다.", count)
    return count


def _create_driver(config: NaverConfig, headless: bool = False):
    try:
        import undetected_chromedriver as uc
    except ImportError as error:
        raise _BrowserUnavailable(
            "Selenium 자동화 패키지가 없습니다. pip install -r apps/api/requirements.txt"
        ) from error

    config.profile_dir.mkdir(parents=True, exist_ok=True)
    _reset_bloated_preferences(config.profile_dir)
    binary = _find_chrome_binary()

    def build_options():
        # 재시도마다 새로 만든다 — undetected-chromedriver는 options 객체를 소비해서
        # 같은 것을 두 번 넘기면 거부한다.
        options = uc.ChromeOptions()
        if binary:
            options.binary_location = str(binary)
        options.add_argument("--start-maximized")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=ko-KR")
        options.add_argument("--log-level=3")
        # 로그인 대기·붙여넣기 도중 'tab crashed'가 나는 것을 줄인다. 공유 메모리(/dev/shm)
        # 부족과 GPU 프로세스가 흔한 원인이라, 자동화용 Chrome에서는 안정성을 우선한다.
        # Chrome sandbox를 끄면 브라우저 취약점이 곧 서비스 계정 침해로 이어진다. 격리된
        # CI에서 꼭 필요한 경우에만 명시적으로 opt-in한다.
        if (os.environ.get("BLOGIT_CHROME_NO_SANDBOX") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        # 라이브 뷰(화면 중계)용: 창이 다른 창에 가려지거나 최소화돼도 크롬이 계속 그리게
        # 한다. 기본값은 가려진 창의 렌더링을 멈추므로, 중계 화면이 '실제 크롬 창을 앞에
        # 띄워야만' 갱신됐다(2026-08-18 로컬 실측). 서버에서는 발행 크롬이 늘 뒤에 깔리므로
        # 이 둘이 없으면 중계가 사실상 정지 화면이 된다.
        options.add_argument("--disable-features=CalculateNativeWinOcclusion")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_experimental_option(
            "prefs",
            {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
                "autofill.profile_enabled": False,
            },
        )
        if headless:
            options.add_argument("--headless=new")
        return options

    def launch():
        return uc.Chrome(
            options=build_options(),
            user_data_dir=str(config.profile_dir),
            version_main=_chrome_major_version(binary),
            headless=headless,
            use_subprocess=True,
        )

    def profile_locked(error: Exception) -> bool:
        message = str(error).lower()
        return (
            "user data directory is already in use" in message
            or "cannot connect" in message
        )

    try:
        driver = launch()
    except Exception as error:
        # 프로필이 잠겨 있으면 대부분 **고아 크롬**이다: 이전 백엔드가 확인용으로 열어
        # 둔 창은 재시작 후 아무도 모르는 채 프로필을 잠근다(2026-08-18 실사용 — 발행을
        # 누르면 고아 창에 '새 탭'만 뜨고 발행이 죽었다). 그 프로필을 쓰는 크롬만 골라
        # 정리하고 한 번 다시 띄운다. 사용자의 개인 크롬은 다른 프로필이라 건드리지 않는다.
        if not profile_locked(error):
            raise _BrowserUnavailable(
                f"Chrome을 시작하지 못했습니다: {str(error) or type(error).__name__}"
            ) from error
        if not _kill_orphan_profile_chrome(config.profile_dir):
            raise _NeedsHuman(
                "네이버 자동화용 Chrome 프로필이 사용 중입니다. 열린 자동화 Chrome 창을 "
                "닫은 뒤 다시 시도해 주세요."
            ) from error
        logger.warning("프로필을 잠근 고아 자동화 Chrome을 정리하고 다시 시도합니다.")
        try:
            driver = launch()
        except Exception as retry_error:
            if profile_locked(retry_error):
                raise _NeedsHuman(
                    "네이버 자동화용 Chrome 프로필이 사용 중입니다. 열린 자동화 Chrome "
                    "창을 닫은 뒤 다시 시도해 주세요."
                ) from retry_error
            raise _BrowserUnavailable(
                f"Chrome을 시작하지 못했습니다: {str(retry_error) or type(retry_error).__name__}"
            ) from retry_error

    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
    try:
        for origin in ("https://blog.naver.com", "https://nid.naver.com"):
            driver.execute_cdp_cmd(
                "Browser.grantPermissions",
                {
                    "origin": origin,
                    "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"],
                },
            )
    except Exception as error:
        logger.warning("네이버 클립보드 권한 자동 허용 실패: %s", error)
    # 화면이 있는 실행에서만. headless는 올릴 창이 없다.
    #
    # POSTING_BRING_TO_FRONT=0이면 창을 앞으로 올리지 않는다 — 발행 에이전트의 예약
    # 발행이 쓴다(설계 D4: 예약 시각에 크롬이 화면을 뺏으면 안 된다). 기본은 지금까지와
    # 같이 올린다.
    if not headless and (os.environ.get("POSTING_BRING_TO_FRONT") or "1") != "0":
        bring_to_front(driver)
    return driver


def _cookie_names(driver) -> set[str]:
    try:
        return {cookie.get("name", "") for cookie in driver.get_cookies()}
    except Exception:
        return set()


def close_browser(driver) -> None:
    """브라우저를 **정상 종료**한다. 쿠키를 디스크에 남기려면 이렇게 닫아야 한다.

    ``driver.quit()``만 부르면 안 된다. undetected-chromedriver의 ``quit()``은
    ``service.process.kill()``과 ``os.kill(browser_pid, 15)``를 부르는데, Windows에서
    후자는 TerminateProcess다 — **강제 종료**다. 크롬은 종료할 때 쿠키를 디스크에 쓰는데
    그 기회를 못 얻는다.

    실제로 재 봤다(같은 쿠키를 심고 각각 닫은 뒤 프로필의 Cookies DB를 열어 확인)::

        강제 종료(driver.quit)        → 디스크에 남은 쿠키 0개
        정상 종료(Browser.close 먼저) → 디스크에 남은 쿠키 1개

    그래서 로그인 세션이 한 번도 저장되지 않았고, 발행할 때마다 새로 로그인했으며,
    네이버가 요구할 때마다 2단계 인증을 다시 받아야 했다(2026-08-05 실사용).

    CDP ``Browser.close``는 크롬에게 스스로 닫으라고 시킨다. 그 뒤 ``quit()``으로
    드라이버 프로세스를 정리한다 — 이미 닫힌 브라우저를 한 번 더 죽이는 것은 무해하다.
    """
    # 이 창을 중계하던 라이브 뷰 세션을 먼저 내린다 — 닫힌 크롬을 계속 중계 대상으로
    # 두면 화면 목록에 유령이 남는다.
    try:
        from ..live_view import hub

        hub.unregister_driver(driver)
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Browser.close", {})
        # 크롬이 쿠키·로컬 저장소를 쓰고 빠져나갈 시간을 준다. 바로 quit()하면 그 사이에
        # 드라이버가 프로세스를 죽여 같은 문제가 난다.
        time.sleep(CLOSE_FLUSH_SECONDS)
    except Exception as error:
        logger.warning("브라우저를 정상 종료하지 못했습니다(세션이 안 남을 수 있습니다): %s", error)
    try:
        driver.quit()
    except Exception:
        pass


def _dismiss_stray_alert(driver) -> str | None:
    """떠 있는 자바스크립트 알림창을 닫고 그 문구를 돌려준다(없으면 None).

    알림창이 열려 있으면 **Selenium의 모든 명령이 막힌다**
    (``UnexpectedAlertPresentException``). 실제로 로그인 뒤 ``blog.naver.com/{blog_id}``에서
    "게시물이 삭제되었거나 다른 페이지로 변경되었습니다"가 떠서 그다음 동작이 전부
    멈췄다(2026-08-05 실사용). 닫기만 하고 발행은 계속한다 — 이 안내는 우리가 하려는
    일과 무관하다.
    """
    try:
        alert = driver.switch_to.alert
        text = (alert.text or "").strip()
        alert.accept()
    except Exception:
        return None
    return text or "(내용 없음)"


def _has_live_session(driver) -> bool:
    return all(name in _cookie_names(driver) for name in SESSION_COOKIES)
