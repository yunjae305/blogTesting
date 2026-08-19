"""트렌드 노출 이력 저장소.

이력이 API 프로세스 메모리에 있으면 두 가지가 깨진다. 재시작하면 사라져 방금 본 키워드가
다시 뜨고, API 서버를 두 대로 늘리는 순간 서버마다 다른 이력을 봐서 '다른 후보 보기'가
서버에 따라 다르게 동작한다. 그래서 이력을 프로세스 바깥(Redis)에 둔다.

자료구조는 키 하나당 Sorted Set 하나다. member는 키워드 시그니처, score는 노출 시각이다.

- 노출 기록: ZADD — 같은 시그니처를 다시 노출하면 새 항목이 생기는 게 아니라 시각만
  갱신된다. 동시 요청이 겹쳐도 중복 기록이 생기지 않는다(원자적).
- 이력 조회: ZRANGEBYSCORE cutoff..+inf — 보관 기간이 지난 항목은 애초에 읽히지 않는다.
- 정리: 오래된 항목(ZREMRANGEBYSCORE)과 상한 초과분(ZREMRANGEBYRANK)을 지우고 키 자체에도
  만료를 건다. 셋을 한 파이프라인으로 묶어 읽기와 쓰기 사이에 다른 요청이 끼어들지 못하게 한다.

Redis가 죽어도 트렌드 패널이 함께 죽어서는 안 된다. 조회가 실패하면 '이력 없음'으로 보고
계속 간다 — 같은 키워드가 다시 보일 수는 있어도 패널이 비지는 않는다. 빈 패널보다 낫다.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence

logger = logging.getLogger(__name__)

# 시그니처 세 조각을 한 member 문자열로 잇는 구분자. 정규화된 키워드·토큰집합·클러스터 id
# 어디에도 나오지 않는 제어문자라 값과 부딪히지 않는다.
_SEPARATOR = "\x1f"

# 키 앞머리. 같은 Redis를 다른 용도와 나눠 쓸 때 서로의 키를 밟지 않도록 서비스 이름부터 붙인다.
KEY_PREFIX = "blogit:trend:exposure"


@dataclass(frozen=True)
class ExposureSignature:
    """노출된 키워드 하나의 지문. 세 축 중 하나만 겹쳐도 '이미 보여준 것'으로 본다."""

    normalized: str
    token_set_signature: str
    cluster_id: str

    def encode(self) -> str:
        return _SEPARATOR.join(
            (self.normalized, self.token_set_signature, self.cluster_id)
        )

    @staticmethod
    def decode(raw: str) -> "ExposureSignature | None":
        parts = raw.split(_SEPARATOR)
        if len(parts) != 3:
            # 형식이 깨진 항목은 크래시가 아니라 '이력 없음'으로 본다.
            return None
        return ExposureSignature(parts[0], parts[1], parts[2])


@dataclass
class ExposureSets:
    """제외 판정에 쓰는 축별 집합. 비어 있으면 아무것도 걸러내지 않는다."""

    normalized: set[str] = field(default_factory=set)
    token_sets: set[str] = field(default_factory=set)
    clusters: set[str] = field(default_factory=set)

    def add(self, signature: ExposureSignature) -> None:
        if signature.normalized:
            self.normalized.add(signature.normalized)
        if signature.token_set_signature:
            self.token_sets.add(signature.token_set_signature)
        if signature.cluster_id:
            self.clusters.add(signature.cluster_id)


class TrendExposureStore(Protocol):
    @property
    def name(self) -> str: ...

    async def has_any(self, key: str) -> bool: ...

    async def sets(self, key: str) -> ExposureSets: ...

    async def remember(self, key: str, signatures: Sequence[ExposureSignature]) -> None: ...

    async def clear(self, key: str) -> None: ...


class InMemoryTrendExposureStore:
    """프로세스 안에 산다. 재시작하면 비고 아무와도 공유하지 않는다.

    개발·테스트용이고, Redis가 설정되지 않았을 때의 폴백이다. 운영에서 이걸 쓰면 서버를
    늘리는 순간 서버마다 다른 이력을 보게 된다.
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.time,
    ):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        # key -> [(시그니처, 노출 시각)] — 최신이 앞
        self._entries: dict[str, list[tuple[ExposureSignature, float]]] = {}

    @property
    def name(self) -> str:
        return "메모리"

    def _live(self, key: str) -> list[tuple[ExposureSignature, float]]:
        cutoff = self._clock() - self._ttl
        entries = [item for item in self._entries.get(key, []) if item[1] >= cutoff]
        if entries:
            self._entries[key] = entries
        else:
            self._entries.pop(key, None)
        return entries

    async def has_any(self, key: str) -> bool:
        return bool(self._live(key))

    async def sets(self, key: str) -> ExposureSets:
        result = ExposureSets()
        for signature, _at in self._live(key):
            result.add(signature)
        return result

    async def remember(self, key: str, signatures: Sequence[ExposureSignature]) -> None:
        entries = self._live(key)
        now = self._clock()
        seen = {signature for signature, _at in entries}
        for signature in signatures:
            if signature in seen:
                continue
            seen.add(signature)
            entries.insert(0, (signature, now))
        self._entries[key] = entries[: self._max]

    async def clear(self, key: str) -> None:
        self._entries.pop(key, None)


class RedisTrendExposureStore:
    """Redis Sorted Set에 저장한다. 재시작에도 남고 모든 API 프로세스가 같은 이력을 본다.

    Redis가 끊기면 폴백(기본: 메모리)으로 저하하고 그 사실을 한 번만 알린다. 매 요청마다
    같은 경고를 찍으면 진짜 문제가 로그에 묻힌다.
    """

    # 저하 후 다시 Redis를 시도하기까지의 간격(초). 끊긴 동안 매 요청이 연결을 재시도하며
    # 지연을 만들지 않게 한다.
    RETRY_INTERVAL_SECONDS = 45.0

    def __init__(
        self,
        client,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.time,
        fallback: TrendExposureStore | None = None,
    ):
        self._client = client
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._fallback = fallback or InMemoryTrendExposureStore(ttl_seconds, max_entries, clock)
        self._degraded = False
        self._retry_after = 0.0

    @property
    def name(self) -> str:
        # 저하된 상태에서 "Redis"라고 하는 것은 데이터가 실제로 어디 있는지에 대한 거짓말이다.
        return "메모리(Redis 연결 실패)" if self._degraded else "Redis"

    def _degrade(self, operation: str, error: Exception) -> None:
        if not self._degraded:
            self._degraded = True
            # 키·사용자 식별자는 남기지 않는다 — 원인 파악에 필요한 것은 어느 동작이
            # 왜 실패했는지다.
            logger.warning(
                "trend exposure: Redis 접근 실패(%s: %s). 메모리 이력으로 계속합니다.",
                operation,
                error,
            )
        self._retry_after = self._clock() + self.RETRY_INTERVAL_SECONDS

    def _can_try_redis(self) -> bool:
        return not self._degraded or self._clock() >= self._retry_after

    def _recover(self) -> None:
        if self._degraded:
            logger.info("trend exposure: Redis 복구. 공유 이력을 다시 사용합니다.")
        self._degraded = False
        self._retry_after = 0.0

    async def has_any(self, key: str) -> bool:
        """이 키로 노출한 적이 한 번이라도 있는가.

        '첫 수집인지'를 가리는 데 쓴다. 전체 항목을 읽어 올 필요가 없으므로 개수만 센다.
        """
        if not self._can_try_redis():
            return await self._fallback.has_any(key)
        try:
            count = await self._client.zcount(
                _namespaced(key), self._clock() - self._ttl, "+inf"
            )
        except Exception as error:
            self._degrade("조회", error)
            return await self._fallback.has_any(key)
        self._recover()
        return bool(count)

    async def sets(self, key: str) -> ExposureSets:
        if not self._can_try_redis():
            return await self._fallback.sets(key)

        cutoff = self._clock() - self._ttl
        try:
            members = await self._client.zrangebyscore(_namespaced(key), cutoff, "+inf")
        except Exception as error:
            self._degrade("조회", error)
            return await self._fallback.sets(key)

        self._recover()
        result = ExposureSets()
        for member in members:
            raw = member if isinstance(member, str) else member.decode("utf-8")
            signature = ExposureSignature.decode(raw)
            if signature is not None:
                result.add(signature)
        return result

    async def remember(self, key: str, signatures: Sequence[ExposureSignature]) -> None:
        if not signatures:
            return
        if not self._can_try_redis():
            await self._fallback.remember(key, signatures)
            return

        now = self._clock()
        namespaced = _namespaced(key)
        try:
            pipe = self._client.pipeline(transaction=True)
            # 같은 시그니처면 새 항목이 아니라 점수(노출 시각)만 갱신된다 — 동시 요청이
            # 겹쳐도 중복 기록이 생기지 않는 이유다.
            pipe.zadd(namespaced, {signature.encode(): now for signature in signatures})
            # 보관 기간이 지난 항목을 걷어낸다.
            pipe.zremrangebyscore(namespaced, "-inf", now - self._ttl)
            # 개수 상한: 오래된 쪽(점수가 낮은 쪽)부터 잘라낸다. 키 하나가 무한정 자라지 않는다.
            pipe.zremrangebyrank(namespaced, 0, -(self._max + 1))
            # 키 자체에도 만료를 건다 — 다시 찾아오지 않는 사용자의 키가 영원히 남지 않게.
            pipe.expire(namespaced, int(self._ttl))
            await pipe.execute()
        except Exception as error:
            self._degrade("기록", error)
            await self._fallback.remember(key, signatures)
            return

        self._recover()

    async def clear(self, key: str) -> None:
        if not self._can_try_redis():
            await self._fallback.clear(key)
            return
        try:
            await self._client.delete(_namespaced(key))
        except Exception as error:
            self._degrade("삭제", error)
            await self._fallback.clear(key)
            return
        self._recover()


def _namespaced(key: str) -> str:
    return f"{KEY_PREFIX}:{key}"
