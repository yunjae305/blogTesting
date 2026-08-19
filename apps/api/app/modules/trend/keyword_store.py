"""이미 수집돼 DB에 쌓인 트렌드 키워드를 그대로 읽는다.

`recommend_topics`(M2)와 목적이 다르다. 그쪽은 **글 하나**에 맞춰 키워드를 모으고
관련도를 채점하므로 글이 먼저 있어야 하고, 소스 호출·모델 호출이 따라온다. 여기는
"지금 DB에 무엇이 쌓여 있나"만 답한다 — 글도, 외부 호출도, 모델도 없다.

브랜드 글쓰기 화면이 이걸 쓴다. 예전에는 키워드 목록을 보려고 **빈 글을 먼저 하나
만들었는데**(트렌드 추천이 글을 요구해서), 그 빈 글이 브랜드 자료 검증에 걸리면
키워드 목록까지 함께 죽었다. 목록을 보는 데 글이 필요할 이유가 없다.

컬렉션은 `MongoPoolCache`가 쓰는 것과 같다(`trend_keywords`, 키워드당 문서 하나:
``{_id: keyN, keyword, source, at, score, seq}``). 쓰는 쪽은 그대로 두고 읽기만 더한다.
"""

import random
import time
from datetime import datetime, timezone
from typing import Protocol

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.llm.trends.cache import POOL_TTL_SECONDS, _evidence_or_none
from app.shared import TrendKeyword, TrendSource

#: `MongoPoolCache`가 쓰는 컬렉션. 읽기 전용으로 같은 곳을 본다.
COLLECTION = "trend_keywords"

#: 한 번에 돌려줄 키워드 수의 상한. 화면은 12개쯤 쓰지만, 상한을 안 두면 361건이
#: 통째로 실려 나간다.
MAX_LIMIT = 50


def _epoch_to_iso(value: float) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _to_keyword(document: dict) -> TrendKeyword | None:
    """저장 문서를 화면이 아는 모양으로 바꾼다. 못 읽는 문서는 버린다(옛 형식 대비)."""
    try:
        source = TrendSource(str(document["source"]))
    except (KeyError, ValueError):
        return None
    keyword = str(document.get("keyword") or "").strip()
    if not keyword:
        return None
    try:
        score = float(document.get("score", 0.0))
        collected_at = float(document.get("at", 0.0))
    except (TypeError, ValueError):
        return None

    # 저장된 출처 근거가 있으면 함께 싣는다. 구버전 문서에는 없고, 그때는 화면이
    # 지표 대신 중립 문구를 쓴다 — 없는 수치를 지어내지 않는다.
    evidence = _evidence_or_none(document.get("evidence"))
    return TrendKeyword(
        trend_keyword_id=str(document.get("_id") or keyword),
        keyword=keyword,
        source=source,
        # 순위는 저장하지 않는다. 아래 interleave가 최종 자리로 다시 매긴다.
        rank=0,
        score=score,
        trend_score=score,
        evidence_by_source={source.value: evidence} if evidence else None,
        collected_at=_epoch_to_iso(collected_at),
    )


def interleave_by_source(
    keywords: list[TrendKeyword], limit: int, shuffle: bool = False
) -> list[TrendKeyword]:
    """소스를 번갈아 뽑는다 — 한 소스가 패널을 통째로 채우지 않게.

    DB에 쌓인 양이 소스마다 크게 다르다(실측: 네이버 200 · 구글 129 · 유튜브 32).
    점수순으로만 자르면 많이 쌓인 소스가 목록을 독차지한다. `AggregateTrendProvider`가
    수집 결과에 하는 것과 같은 이유다.

    ``shuffle``은 '다른 키워드 보기'용이다. 소스별로 섞은 뒤 같은 방식으로 번갈아 뽑아,
    소스가 고루 섞인 성질은 유지한 채 매번 다른 조합이 나오게 한다.
    """
    by_source: dict[str, list[TrendKeyword]] = {}
    for keyword in keywords:
        by_source.setdefault(keyword.source.value, []).append(keyword)

    for bucket in by_source.values():
        if shuffle:
            random.shuffle(bucket)
        else:
            bucket.sort(key=lambda k: -k.score)

    # 소스 순서도 고정한다 — 사전순이면 매번 같은 소스가 1번 자리를 가져간다.
    order = sorted(by_source)
    if shuffle:
        random.shuffle(order)

    picked: list[TrendKeyword] = []
    index = 0
    while len(picked) < limit:
        added = False
        for name in order:
            bucket = by_source[name]
            if index < len(bucket):
                picked.append(bucket[index])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        index += 1

    return [k.model_copy(update={"rank": i + 1}) for i, k in enumerate(picked)]


class StoredTrendKeywordRepository(Protocol):
    async def list_recent(self, limit: int, shuffle: bool = False) -> list[TrendKeyword]: ...


class InMemoryStoredTrendKeywordRepository:
    """Mongo 없이 뜨는 개발 실행·테스트용. 다른 저장소와 같은 규칙이다."""

    def __init__(self, documents: list[dict] | None = None):
        self._documents = documents or []

    async def list_recent(self, limit: int, shuffle: bool = False) -> list[TrendKeyword]:
        keywords = [k for k in map(_to_keyword, self._documents) if k is not None]
        return interleave_by_source(keywords, limit, shuffle)


class MongoStoredTrendKeywordRepository:
    def __init__(self, db: AsyncIOMotorDatabase, clock=time.time):
        self._collection = db[COLLECTION]
        self._clock = clock

    async def list_recent(self, limit: int, shuffle: bool = False) -> list[TrendKeyword]:
        """신선한(수집 후 TTL 안) 문서만 읽는다.

        `MongoPoolCache._get_keywords`와 같은 기준이다. 오래된 키워드를 섞어 내보내면
        "실시간 트렌드"라는 이름이 거짓말이 된다.
        """
        cutoff = self._clock() - POOL_TTL_SECONDS
        documents = await self._collection.find({"at": {"$gte": cutoff}}).to_list(length=None)
        keywords = [k for k in map(_to_keyword, documents) if k is not None]
        return interleave_by_source(keywords, limit, shuffle)
