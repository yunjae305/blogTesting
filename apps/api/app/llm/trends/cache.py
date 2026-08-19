"""수집된 키워드 풀을 요청 사이에 보관하는 곳.

풀은 신선한 기간의 두 배만큼 살아 있다. 신선도(POOL_TTL_SECONDS)는 소스를 다시
호출할지를 결정하고, 더 긴 만료 기간은 안전망이다. 그래서 실패하기 시작한 소스는
— SerpApi 할당량 소진, 네이버 401 — 패널에서 사라지는 대신 마지막으로 반환한 것을
계속 내보낸다.

그래서 항목은 저장소의 만료에 기대지 않고 자기 타임스탬프를 지닌다: 저장소는
"믿기엔 너무 오래됨"과 "보관하기엔 너무 오래됨"을 구분하지 못한다.
"""

import hashlib
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from pymongo.errors import DuplicateKeyError

from app.shared import TrendSourceEvidence

from .base import CollectedKeyword

logger = logging.getLogger(__name__)

# 소스를 다시 호출하기 전까지 풀을 최신으로 취급하는 기간 = 자동(백그라운드) 재수집 간격.
# 트렌드는 그렇게 빨리 바뀌지 않고 소스 API·관련도 채점(LLM)은 과금되므로, 자동 수집은
# 한 달에 한 번만 한다. 그 사이 화면 조회는 DB(Mongo)에 저장·누적된 풀에서 바로 서빙하고,
# 소스를 부르지 않는다. 더 최신 키워드가 필요하면 사용자가 '수집하기'로 즉시 소스를 다시
# 부른다(force_collect — 신선도를 무시하고 수집해 풀에 합친다).
POOL_TTL_SECONDS = 30 * 24 * 60 * 60.0

# 풀이 신선하지 않게 된 뒤에도 폴백으로 보관하는 기간(디스크/Redis). 자동 수집 간격보다
# 길게 둔다 — 그래야 소스가 실패해도 지난 수집분으로 패널을 채운다. Mongo 캐시는 만료가
# 없어 이 값과 무관하게 영구 보관한다.
POOL_KEEP_SECONDS = 60 * 24 * 60 * 60.0


@dataclass
class CachedPool:
    keywords: list[CollectedKeyword]
    age_seconds: float

    def is_fresh(self, ttl: float = POOL_TTL_SECONDS) -> bool:
        return self.age_seconds <= ttl


class PoolCache(Protocol):
    @property
    def name(self) -> str: ...

    async def get(self, key: str) -> CachedPool | None: ...
    async def set(self, key: str, keywords: list[CollectedKeyword]) -> None: ...


def _score_or_none(value: object) -> float | None:
    try:
        return None if value is None else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _evidence_to_json(evidence: TrendSourceEvidence | None) -> dict | None:
    """근거를 프론트가 받는 것과 같은 camelCase JSON으로. 없으면 None 그대로다."""
    return evidence.model_dump(by_alias=True, mode="json") if evidence else None


def _evidence_or_none(value: object) -> TrendSourceEvidence | None:
    """저장된 근거 복원. 구버전 항목에는 없고(None), 깨진 값은 근거만 조용히 버린다 —
    근거는 부가 정보라 이것 때문에 캐시 전체를 미스로 만들면 안 된다."""
    if not isinstance(value, dict):
        return None
    try:
        return TrendSourceEvidence.model_validate(value)
    except Exception:
        return None


def _encode(keywords: list[CollectedKeyword], at: float) -> str:
    return json.dumps(
        {
            "at": at,
            "pool": [
                {
                    "keyword": k.keyword,
                    "score": k.score,
                    "rank": k.rank,
                    "category": k.category,
                    # 관련도 판단 캐시가 부분 점수·결합 가능성을 함께 싣는다 —
                    # 게이트·필터가 재채점 없이 복원할 수 있게. 풀 캐시에서는 None이다.
                    "subjectRelevance": k.subject_relevance,
                    "purposeRelevance": k.purpose_relevance,
                    "personaRelevance": k.persona_relevance,
                    "blendability": k.blendability,
                    # 출처가 관측한 실제 근거. 직렬화를 거쳐도 사라지면 안 된다.
                    "evidence": _evidence_to_json(k.evidence),
                }
                for k in keywords
            ],
        },
        ensure_ascii=False,
    )


def _decode(raw: str, now: float) -> CachedPool | None:
    try:
        payload = json.loads(raw)
        return CachedPool(
            keywords=[
                CollectedKeyword(
                    keyword=item["keyword"],
                    score=float(item["score"]),
                    rank=int(item["rank"]),
                    # category·부분 점수가 생기기 전에 쓰인 항목에는 없다 — 그냥 None.
                    category=item.get("category"),
                    subject_relevance=_score_or_none(item.get("subjectRelevance")),
                    purpose_relevance=_score_or_none(item.get("purposeRelevance")),
                    persona_relevance=_score_or_none(item.get("personaRelevance")),
                    blendability=_score_or_none(item.get("blendability")),
                    evidence=_evidence_or_none(item.get("evidence")),
                )
                for item in payload["pool"]
            ],
            age_seconds=max(0.0, now - float(payload["at"])),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        # 잘못된 형식의 항목은 크래시가 아니라 캐시 미스로 본다.
        return None


class InMemoryPoolCache:
    """API 프로세스 안에 산다. 재시작하면 비워지고, 아무와도 공유하지 않는다."""

    @property
    def name(self) -> str:
        return "메모리"

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._entries: dict[str, str] = {}

    async def get(self, key: str) -> CachedPool | None:
        raw = self._entries.get(key)
        if raw is None:
            return None

        cached = _decode(raw, self._clock())
        if cached is None or cached.age_seconds > POOL_KEEP_SECONDS:
            self._entries.pop(key, None)
            return None
        return cached

    async def set(self, key: str, keywords: list[CollectedKeyword]) -> None:
        self._entries[key] = _encode(keywords, self._clock())


class DiskPoolCache:
    """디스크 파일에 저장돼 서버 재시작에도 살아남는다.

    Redis 없이 로컬에서 돌 때의 기본 캐시다. 메모리 캐시는 재시작하면 비워져 매번
    처음부터 수집했지만, 이건 파일로 남아 다음 실행에서 수집된 키워드가 바로 뜬다.
    키마다 파일 하나(키를 해시한 이름)라 동시 쓰기 충돌이 없다.
    """

    def __init__(self, directory: Path, clock: Callable[[], float] = time.time):
        self._dir = Path(directory)
        self._clock = clock

    @property
    def name(self) -> str:
        return "디스크"

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self._dir / f"{digest}.json"

    async def get(self, key: str) -> CachedPool | None:
        try:
            raw = await asyncio.to_thread(self._path(key).read_text, encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None

        cached = _decode(raw, self._clock())
        if cached is None or cached.age_seconds > POOL_KEEP_SECONDS:
            try:
                await asyncio.to_thread(self._path(key).unlink, missing_ok=True)
            except OSError:
                pass
            return None
        return cached

    async def set(self, key: str, keywords: list[CollectedKeyword]) -> None:
        try:
            await asyncio.to_thread(self._dir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(
                self._path(key).write_text,
                _encode(keywords, self._clock()),
                encoding="utf-8",
            )
        except OSError as error:
            logger.warning("trend cache: 디스크 캐시 쓰기 실패 (%s)", error)


class RedisPoolCache:
    """재시작에도 살아남고, 같은 Redis를 가리키는 모든 프로세스가 공유한다.

    Redis가 다운돼도 트렌드 수집이 함께 다운돼서는 안 된다 — 캐시는 최적화일 뿐,
    소스는 여전히 있다. 메모리로 저하되고, 그 사실을 매 요청이 아니라 한 번만 알린다.
    """

    def __init__(
        self,
        client,
        clock: Callable[[], float] = time.time,
        fallback: PoolCache | None = None,
    ):
        self._client = client
        self._clock = clock
        # Redis가 죽으면 디스크 캐시로 저하한다. 메모리로 떨어지면 재시작마다 다시
        # 수집하지만, 디스크로 떨어지면 재시작에도 수집분이 남는다. 테스트는 격리된
        # 폴백을 주입할 수 있다.
        self._fallback = fallback or DiskPoolCache(_default_disk_cache_dir(), clock)
        self._degraded = False
        self._retry_after = 0.0

    @property
    def name(self) -> str:
        # 한 번 저하되면 실제로는 디스크이고, 로그에 "Redis"라고 하는 것은 데이터가
        # 실제로 어디 있는지에 대한 거짓말이 된다.
        return "디스크(Redis 연결 실패)" if self._degraded else "Redis"

    def _degrade(self, error: Exception) -> None:
        if not self._degraded:
            self._degraded = True
            logger.warning(
                "trend cache: Redis is unreachable (%s). 디스크 캐시로 계속합니다.", error
            )
        self._retry_after = self._clock() + 45.0

    def _can_try_redis(self) -> bool:
        return not self._degraded or self._clock() >= self._retry_after

    def _recover(self) -> None:
        if self._degraded:
            logger.info("trend cache: Redis recovered. Redis 캐시를 다시 사용합니다.")
        self._degraded = False
        self._retry_after = 0.0

    async def get(self, key: str) -> CachedPool | None:
        if not self._can_try_redis():
            return await self._fallback.get(key)

        try:
            raw = await self._client.get(key)
        except Exception as error:
            self._degrade(error)
            return await self._fallback.get(key)

        self._recover()
        if raw is None:
            return None
        return _decode(raw if isinstance(raw, str) else raw.decode("utf-8"), self._clock())

    async def set(self, key: str, keywords: list[CollectedKeyword]) -> None:
        if not self._can_try_redis():
            await self._fallback.set(key, keywords)
            return

        try:
            await self._client.set(
                key, _encode(keywords, self._clock()), ex=int(POOL_KEEP_SECONDS)
            )
        except Exception as error:
            self._degrade(error)
            await self._fallback.set(key, keywords)
            return

        self._recover()


# Mongo에 누적 저장하는 키(=소스별 풀) 하나의 키워드 상한. 무한정 자라지 않게 새 수집분
# 우선으로 자른다. aggregate.POOL_MERGE_CAP과 같은 값이지만, cache는 aggregate를 임포트할 수
# 없어(순환) 여기에 따로 둔다.
MONGO_POOL_CAP = 200


def _bare_pool_source(key: str) -> str | None:
    """추천어 공용 풀 키 ``trend:pool:{SOURCE}:::``(국가·카테고리·시드 비움)에서 소스를 뽑는다.

    이 키는 소스만으로 완전히 정해지므로, Mongo 문서에 키를 따로 저장할 필요 없이 source
    필드만으로 그룹을 되찾을 수 있다. 시드가 붙는 풀(소재 관련어)이나 관련도 키는 None —
    DB가 아니라 디스크 캐시로 간다."""
    parts = key.split(":")
    if len(parts) == 6 and parts[:2] == ["trend", "pool"] and parts[3:] == ["", "", ""]:
        return parts[2]
    return None


class MongoPoolCache:
    """수집한 키워드를 MongoDB에 영속 저장한다.

    추천어 공용 풀(``trend:pool:{SOURCE}:::``)은 **키워드 하나당 문서 하나**로
    ``trend_keywords`` 컬렉션에 담는다: ``{_id: key1…, keyword, source, at, score, seq}``.
    그래서 DB에서 '어떤 소스의 어떤 키워드가 언제 수집됐는지'가 바로 보인다. 그룹은 source
    필드가 대신하므로 풀 키를 문서에 저장하지 않는다. 만료 없이 남고, 다시 수집하면 같은
    키워드는 시간(at)만 갱신되고 새 키워드는 추가돼(누적) 후보가 시간이 갈수록 많아진다.

    ``seq``는 _id의 숫자를 숫자 타입으로 함께 둔 발급용 정렬 키다. _id가 문자열이라
    사전순으로는 "key9" > "key10"이 되어 Mongo에서 '지금까지의 최대 번호'를 못 구한다 —
    counters 컬렉션 없이 다음 순번(max(seq)+1)을 발급하려면 문서 안에 숫자가 있어야 한다.

    score는 저장 전에 소스 내 40~100 상대 인기로 정규화돼 온다(aggregate._rescore_pool).
    소스마다 단위가 달라(검색량·순위 램프·언급 가중) 원시 값은 비교가 안 되기 때문이다.

    소재 관련어 풀(시드 포함 키)·관련도 점수 등 나머지 캐시는 DB에 둘 이유가 없어 디스크
    캐시로 넘긴다 — trend_keywords 하나만 남기고 부속 컬렉션을 만들지 않는다.

    Mongo가 한 요청에서 실패해도 트렌드 수집이 함께 죽어서는 안 된다 — 캐시는 최적화일
    뿐이므로 실패는 로그만 남기고 캐시 미스로 취급한다.
    """

    def __init__(
        self,
        db,
        clock: Callable[[], float] = time.time,
        keywords_collection: str = "trend_keywords",
        fallback: PoolCache | None = None,
    ):
        self._keywords = db[keywords_collection]
        # 관련도 등 내부 캐시 blob의 저장처. DB 컬렉션을 늘리지 않으려 디스크에 둔다.
        self._blob = fallback or DiskPoolCache(_default_disk_cache_dir())
        self._clock = clock
        # keyN 순번 발급(최대 seq 조회 → insert)은 원자적이지 않다. 소스 세 개가 병렬로
        # 저장하므로 프로세스 안에서는 락으로 직렬화하고, 드문 프로세스 간 충돌만
        # DuplicateKeyError 재시도로 푼다.
        self._id_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "MongoDB"

    async def get(self, key: str) -> CachedPool | None:
        source = _bare_pool_source(key)
        if source is not None:
            return await self._get_keywords(source)
        return await self._blob.get(key)

    async def set(self, key: str, keywords: list[CollectedKeyword]) -> None:
        source = _bare_pool_source(key)
        if source is not None:
            await self._set_keywords(source, keywords)
        else:
            await self._blob.set(key, keywords)

    # --- 풀 키: 키워드 1개 = 문서 1개 (trend_keywords, _id = key1, key2, …) ---

    async def _next_id(self) -> tuple[str, int]:
        """key1, key2, … 순번 id. 별도 카운터 컬렉션 없이, 문서에 함께 저장한 seq의 최댓값
        +1로 발급한다."""
        last = await self._keywords.find({}, {"seq": 1}).sort("seq", -1).limit(1).to_list(
            length=1
        )
        seq = (int(last[0].get("seq", 0)) if last else 0) + 1
        return f"key{seq}", seq

    async def _insert_with_next_id(self, document: dict) -> None:
        """순번 발급과 insert를 락으로 묶는다(프로세스 내 경합 제거). 프로세스 간 충돌은
        드물어 재시도 몇 번이면 풀린다."""
        for _ in range(10):
            async with self._id_lock:
                next_id, seq = await self._next_id()
                try:
                    await self._keywords.insert_one({**document, "_id": next_id, "seq": seq})
                    return
                except DuplicateKeyError:
                    continue
        logger.warning("trend cache: keyN 순번 발급이 반복 충돌해 포기 (%s)", document.get("keyword"))

    async def _get_keywords(self, source: str) -> CachedPool | None:
        try:
            docs = await self._keywords.find({"source": source}).to_list(length=None)
        except Exception as error:
            logger.warning("trend cache: Mongo get 실패 (%s) - 캐시 미스로 처리", error)
            return None
        scored: list[tuple[str, float, TrendSourceEvidence | None]] = []
        latest = 0.0
        now = self._clock()
        for doc in docs:
            try:
                collected_at = float(doc.get("at", 0.0))
                # 최신 문서 한 건이 컬렉션 전체를 신선하게 만들지 않도록 각 키워드의
                # 수집 시각을 따로 검사한다.
                if now - collected_at > POOL_TTL_SECONDS:
                    continue
                scored.append(
                    (
                        doc["keyword"],
                        float(doc["score"]),
                        # 구버전 문서에는 근거가 없다(None) — 그대로 정상 로딩된다.
                        _evidence_or_none(doc.get("evidence")),
                    )
                )
                latest = max(latest, collected_at)
            except (KeyError, TypeError, ValueError):
                continue
        if not scored:
            return None
        # 순위는 저장하지 않는다 — 점수순으로 다시 매긴다(aggregate가 어차피 재정규화한다).
        scored.sort(key=lambda item: -item[1])
        keywords = [
            CollectedKeyword(
                keyword=word, score=score, rank=index + 1, category=None, evidence=evidence
            )
            for index, (word, score, evidence) in enumerate(scored)
        ]
        return CachedPool(keywords=keywords, age_seconds=max(0.0, now - latest))

    async def _set_keywords(self, source: str, keywords: list[CollectedKeyword]) -> None:
        now = self._clock()
        try:
            for kw in keywords:
                # 같은 소스의 같은 키워드는 새로 만들지 않고 시간·점수만 갱신한다(누적).
                existing = await self._keywords.find_one(
                    {"source": source, "keyword": kw.keyword}
                )
                if existing is not None:
                    update: dict = {"at": now, "score": kw.score}
                    # 다시 수집돼 더 최신 관측 근거가 오면 갱신하고, 새 수집분에 근거가
                    # 없으면 기존 근거를 지우지 않고 그대로 둔다.
                    if kw.evidence is not None:
                        update["evidence"] = _evidence_to_json(kw.evidence)
                    await self._keywords.update_one(
                        {"_id": existing["_id"]},
                        {"$set": update},
                    )
                else:
                    document = {
                        "keyword": kw.keyword,
                        "source": source,
                        "at": now,
                        "score": kw.score,
                    }
                    if kw.evidence is not None:
                        document["evidence"] = _evidence_to_json(kw.evidence)
                    await self._insert_with_next_id(document)
            await self._trim(source)
        except Exception as error:
            logger.warning("trend cache: Mongo set 실패 (%s)", error)

    async def _trim(self, source: str) -> None:
        """소스당 키워드 수를 상한으로 묶는다 — 오래된(at 작은) 것부터 지운다."""
        count = await self._keywords.count_documents({"source": source})
        if count <= MONGO_POOL_CAP:
            return
        drop = count - MONGO_POOL_CAP
        stale = await self._keywords.find({"source": source}, {"_id": 1}).sort(
            "at", 1
        ).limit(drop).to_list(length=drop)
        ids = [doc["_id"] for doc in stale]
        if ids:
            await self._keywords.delete_many({"_id": {"$in": ids}})


def _default_disk_cache_dir() -> Path:
    """저장소 루트의 ``.trend-cache`` (gitignore). .naver-profile과 같은 자리다."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").is_dir() or (parent / "apps").is_dir():
            return parent / ".trend-cache"
    return here.parent / ".trend-cache"


def create_pool_cache(redis_url: str | None) -> PoolCache:
    """설정돼 있으면 Redis, 아니면 디스크 파일 캐시(재시작에도 유지)."""
    if not redis_url:
        return DiskPoolCache(_default_disk_cache_dir())

    try:
        from redis.asyncio import Redis
    except ImportError:
        logger.warning(
            "trend cache: REDIS_URL is set but the redis package is not installed. "
            "메모리 캐시로 계속합니다."
        )
        return InMemoryPoolCache()

    # from_url은 연결하지 않는다; 첫 명령이 연결한다. 그래서 Redis가 실행 중이지
    # 않아도 시작 로그는 "캐시: Redis"라고 했다 — 첫 트렌드 조회 때에야 알게 됐다.
    # 사실이 아닌 로그는 로그가 없는 것보다 나쁘므로, 여기서 ping을 보낸다.
    cache = RedisPoolCache(Redis.from_url(redis_url, decode_responses=True))

    try:
        from redis import Redis as SyncRedis

        SyncRedis.from_url(redis_url, socket_connect_timeout=2).ping()
    except Exception as error:
        # Redis 다운은 치명적이지 않다 — 캐시는 최적화이고 소스는 여전히 있다.
        # 그저 그 사실을 알리면 된다.
        cache._degrade(error)

    return cache
