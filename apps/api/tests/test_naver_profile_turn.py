"""Chrome 프로필은 한 번에 한 창만 쓴다 — 그 차례를 지키는가.

2026-08-07 신고: 예약 두 건('제미나이'·'올리브영')을 걸었더니 앞 건은 정상 발행됐는데
뒤 건이 '추가 인증이 필요합니다'로 멈췄다. 사용자는 이미 설정에서 로그인하고 2단계
인증까지 마친 상태였다.

실제로는 인증 문제가 아니었다. 로그의 시각이 겹친다:

    10:25:39  '제미나이' 네이버 발행을 시작합니다
    10:26:14  '올리브영' 네이버 발행을 시작합니다   ← 앞 건이 아직 돌고 있다
    10:26:43  '제미나이' 네이버 발행이 완료되었습니다
    10:27:02  '올리브영' 발행에 추가 인증이 필요합니다

원고 준비는 최대 3편까지 동시에 돌아서 두 원고가 35초 차이로 완성됐고, 발행도 그대로
겹쳤다. 브라우저 작업은 호출마다 새 스레드를 띄우므로(`_in_browser_thread`) 둘 다 같은
`.naver-profile`로 Chrome을 열려 했고, 뒤 건이 "user data directory is already in use"로
죽었다.
"""

import threading
import time

import pytest

from app.posting.naver.browser import (
    PROFILE_WAIT_SECONDS,
    ProfileBusy,
    use_profile,
)


class TestOneChromePerProfile:
    def test_같은_프로필은_한_번에_하나만_들어간다(self):
        """이 테스트가 이 수정의 핵심이다 — 겹치면 뒤 건이 죽는다."""
        inside: list[str] = []
        overlapped: list[bool] = []
        busy = threading.Event()

        def work(name: str, hold: float) -> None:
            with use_profile("/프로필/하나"):
                overlapped.append(bool(inside))
                inside.append(name)
                busy.set()
                time.sleep(hold)
                inside.remove(name)

        first = threading.Thread(target=work, args=("제미나이", 0.3))
        first.start()
        busy.wait(timeout=2)  # 앞 건이 확실히 들어간 뒤에 뒤 건을 시작한다
        second = threading.Thread(target=work, args=("올리브영", 0.0))
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)

        assert overlapped == [False, False], "두 발행이 같은 프로필에서 겹쳤다"

    def test_앞_작업이_끝나면_뒤_작업이_들어간다(self):
        """막는 것이 목적이 아니다 — 차례를 기다렸다가 **발행돼야** 한다."""
        done: list[str] = []

        def work(name: str, hold: float) -> None:
            with use_profile("/프로필/차례"):
                time.sleep(hold)
                done.append(name)

        first = threading.Thread(target=work, args=("제미나이", 0.2))
        first.start()
        time.sleep(0.05)
        second = threading.Thread(target=work, args=("올리브영", 0.0))
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)

        assert done == ["제미나이", "올리브영"], "뒤 건이 발행되지 않았다"

    def test_프로필이_다르면_서로_기다리지_않는다(self):
        """자물쇠는 프로필마다다. 계정이 둘이면 동시에 돌아도 된다."""
        entered = threading.Event()

        def hold_first() -> None:
            with use_profile("/프로필/가"):
                entered.set()
                time.sleep(0.4)

        thread = threading.Thread(target=hold_first)
        thread.start()
        entered.wait(timeout=2)

        started = time.monotonic()
        with use_profile("/프로필/나"):
            waited = time.monotonic() - started

        thread.join(timeout=5)
        assert waited < 0.2, "다른 프로필인데 기다렸다"

    def test_예외가_나도_자물쇠를_놓는다(self):
        """발행이 실패해도 다음 발행이 영영 막히면 안 된다."""
        with pytest.raises(RuntimeError):
            with use_profile("/프로필/예외"):
                raise RuntimeError("발행 실패")

        started = time.monotonic()
        with use_profile("/프로필/예외"):
            pass
        assert time.monotonic() - started < 0.2


class TestWhenTheWaitIsTooLong:
    def test_너무_오래_기다리면_ProfileBusy다(self, monkeypatch):
        """영원히 기다리지 않는다 — 그러면 발행이 조용히 멈춘 것처럼 보인다."""
        monkeypatch.setattr("app.posting.naver.browser.PROFILE_WAIT_SECONDS", 0.05)
        entered = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with use_profile("/프로필/오래"):
                entered.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=hold)
        thread.start()
        entered.wait(timeout=2)

        try:
            with pytest.raises(ProfileBusy):
                with use_profile("/프로필/오래"):
                    pass
        finally:
            release.set()
            thread.join(timeout=5)

    def test_기다리는_시간이_로그인_대기보다_길다(self):
        """설정 화면의 로그인은 사람이 2단계 인증을 마칠 때까지 기다린다. 그보다 짧으면
        '로그인하는 동안 예약 발행이 죽는' 새 사고가 생긴다."""
        from app.posting.naver.constants import SETTINGS_LOGIN_TIMEOUT_SECONDS

        assert PROFILE_WAIT_SECONDS > SETTINGS_LOGIN_TIMEOUT_SECONDS


def test_ProfileBusy는_추가_인증이_아니다():
    """발행기가 이것을 NEEDS_HUMAN으로 돌려주면 사용자는 인증을 다시 하게 된다.
    인증을 다시 해도 풀리지 않는 문제다 — 그래서 종류를 갈라 둔다."""
    from app.posting.naver.browser import _NeedsHuman

    assert not issubclass(ProfileBusy, _NeedsHuman)
