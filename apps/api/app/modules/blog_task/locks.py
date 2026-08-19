"""작업 임차(lease) — 같은 작업을 두 프로세스가 동시에 돌리지 않게 한다.

API 서버를 두 대로 늘리면 같은 글에 대한 요청이 서로 다른 서버로 갈 수 있다. 멱등성 키가
1차 방어지만, 키가 기록되기 전에 두 요청이 겹치면 둘 다 통과한다. 그때 M4가 두 번 돌면
이미지 모델까지 두 벌 호출되고 결과 하나는 버려진다 — 비용과 시간이 그대로 낭비다.

락이 아니라 **임차**인 이유: 워커가 죽으면 락을 풀 사람이 없다. 만료 시간을 반드시 걸어
두고, 살아 있는 동안 주기적으로 갱신한다. 그래서 임차가 없다는 것은 곧 "그 작업을 잡고
있던 프로세스가 죽었다"는 뜻이고, 복구 스위퍼가 그것을 근거로 삼는다.

해제는 소유자 토큰을 확인한 뒤에만 한다. 만료된 뒤 다른 프로세스가 잡은 임차를 앞선
프로세스가 뒤늦게 해제해 버리면, 그 순간부터 두 워커가 같은 작업을 돌게 된다. 확인과
삭제 사이에 틈이 없어야 하므로 Lua로 한 번에 처리한다.
"""

import asyncio
import logging
import uuid
from typing import Protocol

logger = logging.getLogger(__name__)

KEY_PREFIX = "blogit:job:lease"

# 임차 기간(초)과 갱신 주기. 기간이 짧을수록 죽은 워커의 작업을 빨리 회수하지만, 갱신이
# 한 번 늦으면 살아 있는 작업의 임차가 풀린다. 갱신은 기간의 3분의 1마다 한다.
DEFAULT_LEASE_SECONDS = 120
RENEW_DIVISOR = 3

# 내 토큰일 때만 지운다 / 갱신한다. 남의 임차를 건드리지 않기 위한 것이다.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


def lease_key(post_id: str, phase: str) -> str:
    return f"{KEY_PREFIX}:{post_id}:{phase}"


class JobLease(Protocol):
    async def acquire(self, key: str) -> str | None: ...
    async def release(self, key: str, token: str) -> None: ...
    async def renew(self, key: str, token: str) -> bool: ...
    async def is_held(self, key: str) -> bool: ...


class NoOpJobLease:
    """Redis가 없는 배포용. 항상 잡히고, 아무것도 붙들지 않는다.

    프로세스가 하나뿐이면 중복 실행이 애초에 생기지 않으므로 예전과 같은 동작이다.
    ``is_held``가 항상 False인 것은 복구 스위퍼에서 의미가 있다: 재시작 직후 진행 중이던
    작업은 그 프로세스와 함께 죽었으므로 전부 회수 대상이다.
    """

    async def acquire(self, key: str) -> str | None:
        return "no-op"

    async def release(self, key: str, token: str) -> None:
        return None

    async def renew(self, key: str, token: str) -> bool:
        return True

    async def is_held(self, key: str) -> bool:
        return False


def _default_lease_dir() -> "Path":
    """저장소 루트의 ``.job-leases`` (gitignore 대상). .trend-cache와 같은 자리다."""
    from pathlib import Path

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").is_dir() or (parent / "apps").is_dir():
            return parent / ".job-leases"
    return here.parent / ".job-leases"


class FileJobLease:
    """Redis 없는 **단일 PC** 배포용 파일 임차.

    NoOpJobLease는 아무것도 막지 않는다 — '프로세스가 하나'라는 가정이 깨지는 순간
    (--reload 재시작 겹침, 서버 이중 실행) 같은 배치를 두 워커가 동시에 돌린다.
    2026-08-04 실사용에서 예약 작업 두 개가 1초 간격으로 동시에 시작돼 크롬 프로필
    충돌(추가 인증 요구)까지 이어졌다. 같은 PC의 프로세스끼리는 파일이 Redis 노릇을
    할 수 있다: 획득은 O_CREAT|O_EXCL(원자적 생성), 만료는 파일에 적힌 시각이다.

    Redis 구현과 같은 원칙을 따른다 — 파일을 읽지 못하면 '남이 잡고 있다'가 아니라
    상황에 맞는 안전한 쪽(획득 실패·살아 있음)으로 본다.
    """

    def __init__(
        self,
        directory=None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        clock=None,
    ):
        import time
        from pathlib import Path

        self._dir = Path(directory) if directory else _default_lease_dir()
        self._seconds = lease_seconds
        self._clock = clock or time.time

    @property
    def lease_seconds(self) -> int:
        return self._seconds

    def _path(self, key: str):
        import hashlib

        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self._dir / f"{digest}.lease"

    def _read(self, key: str) -> dict | None:
        import json

        try:
            return json.loads(self._path(key).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            # 반쯤 쓰인 파일 등. 내용을 모르면 '잡혀 있다'로 본다 — 놓아 버리는 것보다 안전하다.
            return {"token": "", "expiresAt": self._clock() + self._seconds}

    def _try_create(self, key: str, token: str) -> bool:
        import json
        import os

        payload = json.dumps({"token": token, "expiresAt": self._clock() + self._seconds})
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._path(key)), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError as error:
            # 디스크 문제로 임차를 못 만들면 Redis 구현과 같이 '잡은 것'으로 계속한다 —
            # 중복 실행 위험보다 서비스 정지가 나쁘다.
            logger.warning("작업 임차: 파일 생성 실패(%s). 잡은 것으로 계속합니다.", error)
            return True
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return True

    async def acquire(self, key: str) -> str | None:
        token = uuid.uuid4().hex
        if self._try_create(key, token):
            return token
        current = self._read(key)
        if current is not None and float(current.get("expiresAt", 0)) > self._clock():
            return None
        # 만료된 임차 — 잡고 있던 프로세스가 죽었다. 지우고 **한 번만** 다시 만든다.
        # 지움과 생성 사이의 좁은 틈에 다른 프로세스가 끼면 O_EXCL이 실패한다(양보).
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError:
            return None
        return token if self._try_create(key, token) else None

    async def release(self, key: str, token: str) -> None:
        current = self._read(key)
        if current is None or current.get("token") != token:
            # 내 것이 아니다(만료 뒤 남이 잡았다) — 건드리지 않는다. Redis 구현과 같다.
            return
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as error:
            logger.warning("작업 임차: 파일 해제 실패(%s). 만료를 기다립니다.", error)

    async def renew(self, key: str, token: str) -> bool:
        import json

        current = self._read(key)
        if current is None or current.get("token") != token:
            return False
        try:
            self._path(key).write_text(
                json.dumps({"token": token, "expiresAt": self._clock() + self._seconds}),
                encoding="utf-8",
            )
            return True
        except OSError as error:
            logger.warning("작업 임차: 파일 갱신 실패(%s).", error)
            return False

    async def is_held(self, key: str) -> bool:
        current = self._read(key)
        return current is not None and float(current.get("expiresAt", 0)) > self._clock()


class RedisJobLease:
    """Redis SET NX EX 기반 임차.

    Redis에 문제가 생기면 임차를 **잡은 것으로 친다**. 여기서 막아 버리면 Redis가 흔들릴
    때 원고 생성이 통째로 멈춘다 — 중복 실행 위험보다 서비스 정지가 나쁘다. 다만 그 사실을
    로그에 남긴다.
    """

    def __init__(self, client, lease_seconds: int = DEFAULT_LEASE_SECONDS):
        self._client = client
        self._seconds = lease_seconds

    @property
    def lease_seconds(self) -> int:
        return self._seconds

    async def acquire(self, key: str) -> str | None:
        token = uuid.uuid4().hex
        try:
            acquired = await self._client.set(key, token, ex=self._seconds, nx=True)
        except Exception as error:
            logger.warning("작업 임차: Redis 접근 실패(획득: %s). 잡은 것으로 계속합니다.", error)
            return token
        return token if acquired else None

    async def release(self, key: str, token: str) -> None:
        try:
            await self._client.eval(_RELEASE_SCRIPT, 1, key, token)
        except Exception as error:
            # 해제에 실패해도 임차는 만료로 풀린다 — 영구히 남지 않는다.
            logger.warning("작업 임차: 해제 실패(%s). 만료를 기다립니다.", error)

    async def renew(self, key: str, token: str) -> bool:
        try:
            return bool(await self._client.eval(_RENEW_SCRIPT, 1, key, token, self._seconds))
        except Exception as error:
            logger.warning("작업 임차: 갱신 실패(%s).", error)
            return False

    async def is_held(self, key: str) -> bool:
        """지금 이 작업을 붙들고 있는 프로세스가 있는가. 복구 스위퍼가 쓴다."""
        try:
            return await self._client.exists(key) > 0
        except Exception as error:
            # 확인이 안 되면 '살아 있다'고 본다. 멀쩡히 돌고 있는 작업을 실패로 만드는 것보다,
            # 죽은 작업이 한 번 더 남아 있는 편이 안전하다.
            logger.warning("작업 임차: 상태 확인 실패(%s). 살아 있는 것으로 봅니다.", error)
            return True


class HeldLease:
    """잡은 임차를 작업이 끝날 때까지 붙들고 있는 핸들.

    작업이 임차 기간보다 오래 걸리면(M4 + 이미지가 실제로 그렇다) 도중에 임차가 풀려
    다른 프로세스가 같은 작업을 집어 갈 수 있다. 그래서 살아 있는 동안 주기적으로 갱신한다.
    """

    def __init__(self, lease: JobLease, key: str, token: str, renew_interval: float):
        self._lease = lease
        self._key = key
        self._token = token
        self._interval = renew_interval
        self._heartbeat: asyncio.Task | None = None

    async def __aenter__(self) -> "HeldLease":
        self._heartbeat = asyncio.create_task(self._keep_alive())
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except asyncio.CancelledError:
                pass
        await self._lease.release(self._key, self._token)

    async def _keep_alive(self) -> None:
        """작업이 끝날 때까지(취소될 때까지) 갱신한다. 한 번 실패해도 그만두지 않는다.

        예전에는 갱신이 한 번 실패하면 여기서 끝냈다. 그러면 **아직 돌고 있는** 작업의
        임차가 그대로 만료돼, 복구 스위퍼 눈에 '죽은 작업'으로 보인다(2026-08-05에 실제로
        원고 생성 4분째인 글이 그렇게 회수됐다). 갱신에 실패하면 다시 잡는 것이 맞다 —
        이 작업은 지금도 돌고 있고, 임차는 그 사실을 알리는 표시이기 때문이다.
        """
        warned = False
        while True:
            await asyncio.sleep(self._interval)
            if await self._lease.renew(self._key, self._token):
                warned = False
                continue
            # 만료됐거나 임차가 사라졌다. 비어 있으면 다시 잡히고, 남이 잡고 있으면 None이다.
            token = await self._lease.acquire(self._key)
            if token is not None:
                self._token = token
                warned = False
                continue
            if not warned:
                # 남이 잡고 있다 — 중복 실행을 의심할 근거는 남긴다. 다만 계속 시도한다:
                # 그쪽이 끝나면 이 작업이 아직 살아 있다는 표시를 다시 세워야 한다.
                logger.warning("작업 임차 갱신 실패 — 다른 프로세스가 잡고 있을 수 있습니다.")
                warned = True


async def hold(lease: JobLease, key: str) -> HeldLease | None:
    """임차를 잡으면 핸들, 이미 남이 잡고 있으면 None."""
    token = await lease.acquire(key)
    if token is None:
        return None
    seconds = getattr(lease, "lease_seconds", DEFAULT_LEASE_SECONDS)
    return HeldLease(lease, key, token, max(1.0, seconds / RENEW_DIVISOR))
