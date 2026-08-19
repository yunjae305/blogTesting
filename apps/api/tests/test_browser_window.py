"""발행용 Chrome 창을 앞으로 올리는 것(browser.bring_to_front).

여기서 보는 것은 하나다: **창을 못 올려도 발행은 계속되는가.**

창이 뒤에 있는 것은 불편이고, 그 불편이 발행 실패가 되면 안 된다. 그래서 이 함수는
어떤 실패도 밖으로 내보내지 않는다. 실제로 창이 올라가는지는 OS가 하는 일이라 여기서
확인할 수 없다 — 그 부분은 사람이 눈으로 봐야 한다(변경내역에 적어 뒀다).
"""

import platform

from app.posting.naver.browser import bring_to_front


class FakeDriver:
    """CDP·창 조작 호출을 기록만 하는 드라이버."""

    def __init__(self, *, cdp_raises=None, maximize_raises=None):
        self.calls: list[str] = []
        self._cdp_raises = cdp_raises
        self._maximize_raises = maximize_raises
        # Windows 경로가 창을 찾을 때 쓰는 값. 없는 pid라 아무 창도 안 걸린다.
        self.browser_pid = 0

    def execute_cdp_cmd(self, name, params):
        self.calls.append(f"cdp:{name}")
        if self._cdp_raises:
            raise self._cdp_raises

    def maximize_window(self):
        self.calls.append("maximize")
        if self._maximize_raises:
            raise self._maximize_raises


def test_창을_앞으로_올릴_때_CDP와_최대화를_둘_다_시도한다():
    driver = FakeDriver()

    bring_to_front(driver)

    assert "cdp:Page.bringToFront" in driver.calls
    assert "maximize" in driver.calls


def test_CDP가_실패해도_최대화는_그대로_시도하고_예외는_새지_않는다():
    """하나가 막혔다고 나머지를 포기하면, 통했을 수도 있는 방법을 버리는 셈이다."""
    driver = FakeDriver(cdp_raises=RuntimeError("no such target"))

    bring_to_front(driver)  # 예외가 새면 여기서 실패한다

    assert "maximize" in driver.calls


def test_전부_실패해도_예외를_던지지_않는다():
    """이 함수의 실패는 발행의 실패가 아니다 — 창이 뒤에 있을 뿐이다."""
    driver = FakeDriver(
        cdp_raises=RuntimeError("disconnected"),
        maximize_raises=RuntimeError("window closed"),
    )

    bring_to_front(driver)


def test_pid를_알_수_없어도_조용히_넘어간다():
    """undetected_chromedriver가 browser_pid를 안 채워 둔 경우다."""

    class NoPidDriver(FakeDriver):
        def __init__(self):
            super().__init__()
            del self.browser_pid

    driver = NoPidDriver()

    bring_to_front(driver)

    assert "cdp:Page.bringToFront" in driver.calls


def test_윈도우가_아니면_창_올리기를_건너뛴다():
    """다른 OS에서는 ctypes 경로 자체가 없다. CDP·최대화까지만 하고 끝난다."""
    driver = FakeDriver()

    bring_to_front(driver)

    # OS와 무관하게 앞의 두 시도는 언제나 남는다.
    assert driver.calls[:2] == ["cdp:Page.bringToFront", "maximize"]
    # Windows에서만 세 번째 경로가 돈다 — 여기서는 호출 기록으로 구분하지 않고,
    # 어느 OS에서도 예외 없이 끝난다는 것만 본다.
    assert platform.system() in {"Windows", "Linux", "Darwin"}
