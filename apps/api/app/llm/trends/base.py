"""M2 트렌드 수집기들이 공유하는 조각들.

각 수집기는 정확히 한 소스와만 통신하고, 그 소스 고유의 척도로 채점된 키워드를
반환한다. 이를 비교하거나 합치는 것은 수집기가 아니라 AggregateTrendProvider의
일이다.
"""

from dataclasses import dataclass
from typing import Protocol

import httpx

from app.llm.contracts import TrendFetchInput
from app.shared import TrendSource, TrendSourceEvidence

REQUEST_TIMEOUT = httpx.Timeout(4.0)

@dataclass
class CollectedKeyword:
    """단일 소스가 매긴 순위 그대로의 키워드 하나.

    `score`는 그 소스 고유의 척도 위에 있다 — 검색량, 조회수, 언급 수 — 따라서 같은
    소스에서 나온 다른 키워드와 비교할 때만 의미가 있다.

    `category`는 관련도 채점 단계에서만 채워진다(이 단계도 판단 결과를
    CollectedKeyword로 캐시한다); 소스에서 막 나온 키워드에는 아직 없다.

    소재/목적/페르소나 부분 점수도 같은 이유로 여기 있다: 관련도 판단 캐시가
    CollectedKeyword를 직렬화 단위로 쓰므로, 게이트·필터가 쓰는 점수들을 함께
    실어야 재채점 없이 복원된다. 소스에서 막 나온 키워드에는 없다(None).
    """

    keyword: str
    score: float
    rank: int
    category: str | None = None
    subject_relevance: float | None = None
    purpose_relevance: float | None = None
    persona_relevance: float | None = None
    # 이전 관련도 캐시와의 역호환 필드. 최신순은 소재별 채점을 호출하지 않는다.
    blendability: float | None = None
    # 이 키워드를 관측한 출처의 실제 근거(검색량·조회수·문서 수 등). 수집기가 채우며,
    # 근거를 만들 수 없는 경로(옛 캐시·근거 없는 소스)에서는 None으로 남는다 — 기본값을
    # 두어 기존 생성 코드가 전부 그대로 동작한다.
    evidence: TrendSourceEvidence | None = None


class TrendCollector(Protocol):
    source: TrendSource

    async def collect(
        self,
        trend_input: TrendFetchInput,
        limit: int | None,
        known: frozenset[str] = frozenset(),
    ) -> list[CollectedKeyword]: ...


def seed_queries(trend_input: TrendFetchInput, limit: int = 3) -> list[str]:
    """사용자 자신의 단어들. 이 소재를 둘러싸고 무엇이 게시되고 있는지 키워드 검색
    소스에 묻는 데 쓴다. 실시간 트렌드 엔드포인트를 가진 소스(구글, 유튜브)는 이를
    무시한다."""
    raw = [
        trend_input.input.topic,
        trend_input.input.subject,
        *(trend_input.input.keywords or []),
    ]

    seen: set[str] = set()
    seeds: list[str] = []
    for value in raw:
        text = (value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        seeds.append(text)
    return seeds[:limit]
