"""발행 후 확인용으로 열어 둔 크롬을 일정 시간 뒤에 닫는다 (브라우저 정리).

발행·로그인이 끝나면 창을 닫지 않고 열어 둔다 — 사용자가 결과(또는 실패 화면)를
직접 봐야 하기 때문이다. 기존 정리 시점은 '그 프로필의 다음 발행 시작'뿐이었다.
사용자가 한 명일 때는 열린 창이 최대 1~2개였지만, 10명이면 **프로필마다 하나씩** —
발행이 뜸한 계정의 크롬(한 대 300~700MB)이 며칠씩 남아 서버 메모리를 갉아먹는다.

그래서 시간 제한을 더한다: 열어 둔 지 ``KEEP_OPEN_BROWSER_TTL_MINUTES``(기본 15분)를
넘긴 창은 백그라운드 리퍼가 닫는다. 다만 **라이브 뷰로 누가 보고 있는 창은 닫지
않는다** — 2단계 인증을 마저 끝내는 중일 수 있다(다음 바퀴에 다시 본다).

기존 '다음 발행이 닫는다' 정책은 그대로다 — 여기는 그 정책이 닿지 않는 꼬리
(더는 발행하지 않는 프로필)만 줍는다.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_SWEEP_INTERVAL_SECONDS = 60.0

_lock = threading.Lock()
# id(driver) → (그 드라이버를 들고 있는 keep-open dict, 그 안의 key, 열어 둔 시각)
_tracked: dict[int, tuple[dict, str, float]] = {}
_thread: threading.Thread | None = None


def _ttl_seconds() -> float:
    """0 이하로 두면 정리를 끈다(예전과 똑같이 다음 발행 때만 닫는다)."""
    raw = (os.environ.get("KEEP_OPEN_BROWSER_TTL_MINUTES") or "15").strip()
    try:
        return float(raw) * 60.0
    except ValueError:
        return 15 * 60.0


def mark_kept_open(store: dict, key: str, driver) -> None:
    """발행 코드가 창을 열어 둘 때 부른다. ``store[key] = driver``까지 대신 한다."""
    store[key] = driver
    with _lock:
        _tracked[id(driver)] = (store, key, time.monotonic())
    _ensure_thread()


def _ensure_thread() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(
            target=_run, name="kept-browser-reaper", daemon=True
        )
        _thread.start()


def _run() -> None:
    while True:
        time.sleep(_SWEEP_INTERVAL_SECONDS)
        try:
            sweep()
        except Exception as error:  # noqa: BLE001 — 리퍼가 죽으면 창이 다시 쌓인다
            logger.warning("열어 둔 크롬 정리 중 오류(계속합니다): %s", error)


def sweep(now: float | None = None) -> int:
    """수명이 지난 창을 닫는다. 닫은 개수를 돌려준다(테스트·진단용)."""
    from .live_view import hub as live_view_hub
    from .naver.browser import _profile_lock, close_browser

    ttl = _ttl_seconds()
    if ttl <= 0:
        return 0
    current = time.monotonic() if now is None else now
    closed = 0
    with _lock:
        entries = list(_tracked.items())
    for driver_id, (store, key, kept_at) in entries:
        driver = store.get(key)
        if driver is None or id(driver) != driver_id:
            # 다음 발행이 이미 닫고 새 창으로 바꿨다 — 추적만 걷어낸다.
            with _lock:
                _tracked.pop(driver_id, None)
            continue
        if current - kept_at < ttl:
            continue
        if live_view_hub.has_watchers(driver):
            # 사람이 보고 있는 화면이다(인증을 마저 끝내는 중일 수 있다). 이번에는
            # 두고, 시각을 미뤄 다음 바퀴에 다시 본다 — 보는 동안은 계속 산다.
            with _lock:
                _tracked[driver_id] = (store, key, current)
            continue
        # 닫는 몇 초 동안 프로필 잠금을 쥔다. 안 쥐면 그 사이에 시작한 같은 프로필의
        # 발행이 '아직 종료 중인 크롬'과 부딪혀 "프로필이 사용 중입니다"로 죽는다
        # (use_profile의 불변식: 여는 순간부터 닫힐 때까지가 한 구간). 발행이 잠금을
        # 쥐고 있으면 이번 바퀴는 건너뛴다 — 어차피 그 발행이 이 창을 닫는다.
        # (keep-open dict의 key가 곧 프로필 경로 문자열이다.)
        profile_lock = _profile_lock(key)
        if not profile_lock.acquire(blocking=False):
            continue
        try:
            # pop이 곧 소유권이다. 검사(get)와 pop 사이에 발행 스레드가 먼저 집어
            # 갔으면 그쪽이 닫는다 — 같은 창을 두 스레드가 닫으면 쿠키를 쓰는 2초
            # 유예 중에 강제 종료가 끼어 세션이 디스크에 안 남는다(close_browser 참고).
            if store.pop(key, None) is not driver:
                with _lock:
                    _tracked.pop(driver_id, None)
                continue
            with _lock:
                _tracked.pop(driver_id, None)
            close_browser(driver)
            closed += 1
            logger.info("열어 둔 발행 크롬을 %d분 만에 정리했습니다.", int(ttl // 60))
        finally:
            profile_lock.release()
    return closed
