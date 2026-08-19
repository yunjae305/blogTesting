"""파일 임차(FileJobLease) — Redis 없는 단일 PC에서 프로세스 간 중복 실행을 막는다.

왜 생겼나(2026-08-04): REDIS_URL이 비어 NoOpJobLease가 쓰였는데, --reload 재시작 겹침
등으로 프로세스가 잠깐 둘이 되자 같은 배치의 예약 작업 두 개가 1초 간격으로 동시에
시작됐다(크롬 프로필 충돌 → 추가 인증 요구까지 이어졌다). 같은 PC에서는 파일이 Redis
노릇을 할 수 있다.

여기서 막는 것: 두 프로세스가 같은 키를 동시에 잡는 것, 죽은 프로세스의 임차가 영영
안 풀리는 것, 만료 뒤 남이 잡은 임차를 앞선 프로세스가 건드리는 것.
"""

import pytest

from app.modules.blog_task.locks import FileJobLease


class FakeClock:
    def __init__(self, start: float = 1_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def lease(tmp_path, clock, seconds: int = 120) -> FileJobLease:
    return FileJobLease(tmp_path, lease_seconds=seconds, clock=clock)


async def test_두_번째_획득은_거절된다(tmp_path, clock):
    """핵심 — NoOp였다면 둘 다 통과해 같은 작업이 두 번 돈다."""
    first = lease(tmp_path, clock)
    second = lease(tmp_path, clock)  # 다른 프로세스 흉내(같은 폴더를 본다)

    token = await first.acquire("blogit:scheduled:batch_1")

    assert token is not None
    assert await second.acquire("blogit:scheduled:batch_1") is None


async def test_다른_키는_서로_막지_않는다(tmp_path, clock):
    held = lease(tmp_path, clock)

    assert await held.acquire("batch_1") is not None
    assert await held.acquire("batch_2") is not None


async def test_해제하면_다시_잡을_수_있다(tmp_path, clock):
    held = lease(tmp_path, clock)
    token = await held.acquire("batch_1")

    await held.release("batch_1", token)

    assert await held.acquire("batch_1") is not None


async def test_만료된_임차는_죽은_프로세스의_것이므로_넘겨받는다(tmp_path, clock):
    """워커가 죽으면 임차를 풀 사람이 없다 — 만료가 유일한 회수 경로다."""
    dead = lease(tmp_path, clock)
    await dead.acquire("batch_1")

    clock.now += 121  # 임차 기간(120초)이 지났다

    alive = lease(tmp_path, clock)
    assert await alive.acquire("batch_1") is not None


async def test_갱신하면_만료가_뒤로_밀린다(tmp_path, clock):
    held = lease(tmp_path, clock)
    token = await held.acquire("batch_1")

    clock.now += 100
    assert await held.renew("batch_1", token) is True
    clock.now += 100  # 갱신 없이라면 벌써 만료(200 > 120)됐을 시간

    other = lease(tmp_path, clock)
    assert await other.acquire("batch_1") is None  # 갱신 덕에 아직 잡혀 있다


async def test_남의_토큰으로는_해제도_갱신도_안_된다(tmp_path, clock):
    """만료 뒤 남이 잡은 임차를 앞선 프로세스가 뒤늦게 풀면 그때부터 둘이 같이 돈다."""
    held = lease(tmp_path, clock)
    await held.acquire("batch_1")

    stranger = lease(tmp_path, clock)
    await stranger.release("batch_1", "남의-토큰")
    assert await stranger.renew("batch_1", "남의-토큰") is False

    # 여전히 잡혀 있다.
    assert await stranger.acquire("batch_1") is None


async def test_is_held는_만료를_반영한다(tmp_path, clock):
    held = lease(tmp_path, clock)
    await held.acquire("batch_1")

    assert await held.is_held("batch_1") is True
    clock.now += 121
    assert await held.is_held("batch_1") is False


async def test_깨진_임차_파일은_잡혀_있는_것으로_본다(tmp_path, clock):
    """내용을 모르면 놓아 버리는 쪽보다 잡혀 있다고 보는 쪽이 안전하다(중복 실행 방지)."""
    held = lease(tmp_path, clock)
    await held.acquire("batch_1")
    # 파일을 반쯤 쓰다 죽은 상황.
    next(tmp_path.glob("*.lease")).write_text("{깨진 json", encoding="utf-8")

    assert await held.acquire("batch_1") is None
    assert await held.is_held("batch_1") is True
