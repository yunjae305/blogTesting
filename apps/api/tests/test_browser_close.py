"""브라우저를 닫을 때 쿠키가 디스크에 남는지.

실사용에서 겪은 것: 어느 프로필에도 로그인 쿠키가 없었다. 그래서 발행할 때마다 새로
로그인했고, 네이버가 요구할 때마다 2단계 인증을 다시 받아야 했다.

원인은 undetected-chromedriver의 ``quit()``이다. ``service.process.kill()``과
``os.kill(browser_pid, 15)``를 부르는데 Windows에서 후자는 TerminateProcess다 —
**강제 종료**라 크롬이 쿠키를 쓸 기회를 못 얻는다. 실측으로 확인했다::

    강제 종료(driver.quit)        → 디스크에 남은 쿠키 0개
    정상 종료(Browser.close 먼저) → 디스크에 남은 쿠키 1개
"""

from app.posting.naver.browser import close_browser


class FakeDriver:
    def __init__(self, *, close_fails: bool = False, quit_fails: bool = False):
        self.calls: list[str] = []
        self._close_fails = close_fails
        self._quit_fails = quit_fails

    def execute_cdp_cmd(self, command, params):
        self.calls.append(command)
        if self._close_fails:
            raise RuntimeError("브라우저가 이미 닫혔습니다")

    def quit(self):
        self.calls.append("quit")
        if self._quit_fails:
            raise RuntimeError("드라이버가 이미 죽었습니다")


class TestClosingTheBrowser:
    def test_the_browser_is_asked_to_close_itself_before_the_driver_quits(self, monkeypatch):
        """순서가 뒤집히면 쿠키가 사라진다 — quit()이 크롬을 죽여 버린다."""
        monkeypatch.setattr("app.posting.naver.browser.time.sleep", lambda _s: None)
        driver = FakeDriver()

        close_browser(driver)

        assert driver.calls == ["Browser.close", "quit"]

    def test_the_driver_still_quits_when_the_graceful_close_fails(self, monkeypatch):
        """이미 닫힌 브라우저에도 부른다. 여기서 멈추면 드라이버 프로세스가 남는다."""
        monkeypatch.setattr("app.posting.naver.browser.time.sleep", lambda _s: None)
        driver = FakeDriver(close_fails=True)

        close_browser(driver)

        assert "quit" in driver.calls

    def test_a_failing_quit_is_not_an_error(self, monkeypatch):
        """발행의 finally에서 불린다. 여기서 예외가 나면 진짜 결과가 묻힌다."""
        monkeypatch.setattr("app.posting.naver.browser.time.sleep", lambda _s: None)

        close_browser(FakeDriver(close_fails=True, quit_fails=True))  # 예외가 없으면 통과다


class TestEveryExitUsesIt:
    """종료 지점이 하나라도 남으면 그 경로에서만 세션이 사라진다 — 찾기 어렵다."""

    def test_no_publishing_code_calls_quit_directly(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "app" / "posting"
        offenders = []
        for path in root.rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "driver.quit()" not in line:
                    continue
                # close_browser 자신은 예외다(그 안에서 부르는 것이 정상이다).
                if path.name == "browser.py":
                    continue
                offenders.append(f"{path.name}:{number}")

        assert offenders == [], f"close_browser 대신 quit()을 직접 부르는 곳: {offenders}"
