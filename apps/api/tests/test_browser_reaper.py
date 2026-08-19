"""열어 둔 발행 크롬의 TTL 정리(작업 D).

'다음 발행이 이전 창을 닫는' 기존 정책은 발행이 계속되는 프로필에만 닿는다. 사용자
10명이 각자 한 번씩 발행하고 떠나면 크롬 10대가 영원히 남는다 — 그 꼬리를 리퍼가
줍는지, 그리고 **보고 있는 창(라이브 뷰 구독)과 이미 교체된 창은 건드리지 않는지**를
검증한다.
"""

import pytest

from app.posting import browser_reaper
from app.posting.live_view import hub as live_view_hub


class _Driver:
    pass


@pytest.fixture
def reaper(monkeypatch):
    """리퍼를 격리한다: 추적 초기화, 스레드 금지, close_browser 기록."""
    closed: list[object] = []
    monkeypatch.setattr(browser_reaper, "_tracked", {})
    monkeypatch.setattr(browser_reaper, "_ensure_thread", lambda: None)
    monkeypatch.setattr(
        "app.posting.naver.browser.close_browser", lambda driver: closed.append(driver)
    )
    monkeypatch.setattr(live_view_hub, "has_watchers", lambda _driver: False)
    monkeypatch.setenv("KEEP_OPEN_BROWSER_TTL_MINUTES", "15")
    return closed


def test_a_young_window_survives(reaper):
    store: dict = {}
    driver = _Driver()
    browser_reaper.mark_kept_open(store, "profile-a", driver)
    assert browser_reaper.sweep() == 0
    assert store == {"profile-a": driver}
    assert reaper == []


def test_an_expired_window_is_closed_and_forgotten(reaper, monkeypatch):
    store: dict = {}
    driver = _Driver()
    browser_reaper.mark_kept_open(store, "profile-a", driver)
    import time

    late = time.monotonic() + 16 * 60
    assert browser_reaper.sweep(now=late) == 1
    assert store == {}
    assert reaper == [driver]
    # 두 번 쓸지 않는다 — 추적에서도 빠졌다.
    assert browser_reaper.sweep(now=late) == 0


def test_a_watched_window_is_left_alone(reaper, monkeypatch):
    """라이브 뷰로 보고 있는 창은 2단계 인증을 끝내는 중일 수 있다 — 닫지 않는다."""
    store: dict = {}
    driver = _Driver()
    browser_reaper.mark_kept_open(store, "profile-a", driver)
    monkeypatch.setattr(live_view_hub, "has_watchers", lambda _driver: True)
    import time

    late = time.monotonic() + 16 * 60
    assert browser_reaper.sweep(now=late) == 0
    assert store == {"profile-a": driver}
    # 보고 있는 동안은 시각이 미뤄진다 — 구독이 끝난 뒤 TTL만큼 더 산다.
    monkeypatch.setattr(live_view_hub, "has_watchers", lambda _driver: False)
    assert browser_reaper.sweep(now=late + 60) == 0
    assert browser_reaper.sweep(now=late + 16 * 60) == 1


def test_a_replaced_window_is_not_closed(reaper):
    """다음 발행이 이미 닫고 새 창을 놓았다 — 리퍼가 새 창을 닫으면 발행이 죽는다."""
    store: dict = {}
    old, new = _Driver(), _Driver()
    browser_reaper.mark_kept_open(store, "profile-a", old)
    store["profile-a"] = new  # 다음 발행이 교체했다(옛 창은 그쪽 코드가 닫았다)
    import time

    assert browser_reaper.sweep(now=time.monotonic() + 16 * 60) == 0
    assert store == {"profile-a": new}
    assert reaper == []


def test_a_profile_in_use_is_skipped(reaper):
    """발행이 프로필 잠금을 쥔 동안은 닫지 않는다 — 종료 중인 크롬과 새 발행이
    부딪히면 '프로필이 사용 중입니다'로 발행이 죽는다."""
    from app.posting.naver.browser import _profile_lock

    store: dict = {}
    driver = _Driver()
    browser_reaper.mark_kept_open(store, "profile-busy", driver)
    import time

    lock = _profile_lock("profile-busy")
    assert lock.acquire(blocking=False)
    try:
        assert browser_reaper.sweep(now=time.monotonic() + 16 * 60) == 0
        assert store == {"profile-busy": driver}
    finally:
        lock.release()
    # 잠금이 풀리면 다음 바퀴에 닫힌다.
    assert browser_reaper.sweep(now=time.monotonic() + 16 * 60) == 1


def test_losing_the_pop_race_means_not_closing(reaper):
    """검사와 pop 사이에 발행 스레드가 먼저 집어 갔으면 그쪽이 닫는다 — 같은 창을
    두 스레드가 닫으면 쿠키 플러시 2초 사이에 강제 종료가 끼어 세션이 안 남는다."""

    class _RacingStore(dict):
        def pop(self, key, default=None):
            super().pop(key, None)
            return "다른-스레드가-집어-갔다"

    store = _RacingStore()
    driver = _Driver()
    browser_reaper.mark_kept_open(store, "profile-a", driver)
    import time

    assert browser_reaper.sweep(now=time.monotonic() + 16 * 60) == 0
    assert reaper == []


def test_ttl_zero_disables_the_reaper(reaper, monkeypatch):
    monkeypatch.setenv("KEEP_OPEN_BROWSER_TTL_MINUTES", "0")
    store: dict = {}
    browser_reaper.mark_kept_open(store, "profile-a", _Driver())
    import time

    assert browser_reaper.sweep(now=time.monotonic() + 10**6) == 0
    assert len(store) == 1
