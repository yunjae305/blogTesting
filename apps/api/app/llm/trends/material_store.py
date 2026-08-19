"""소재별 관련 키워드 풀의 저장소 — 공용 트렌드 풀과 분리된 자리.

왜 별도 컬렉션인가:

최신순이 쓰는 ``trend_keywords``는 "지금 한국에서 뜨는 것"이라는 **하나의 공용 풀**이다.
소재 관련 키워드는 성격이 정반대다 — '배틀그라운드 감도 설정'은 배틀그라운드 글에만
의미가 있고, 최신순 패널에 섞이면 아무 관계 없는 사용자에게 노출된다. 예전에는 소재
수집분을 공용 풀에 upsert해 두 성격이 한 컬렉션에 섞였고, 그래서 소재 메아리 필터가
최신순에서까지 계속 일해야 했다.

무엇을 저장하고 무엇을 저장하지 않는가:

문서의 키는 **소재**이지 사용자가 아니다(``materialKey``에 userId·postId를 넣지 않는다).
같은 소재로 글을 쓰는 모든 사용자가 한 번 검증한 풀을 재사용해야 수집·채점 비용이 글 수가
아니라 소재 수에 비례한다. 사용자별 노출 이력은 여기 두지 않고 TrendExposureStore가
따로 관리한다 — 여기 섞으면 "이 키워드는 누구에게 보여줬나"가 소재 지식과 뒤엉킨다.

참고자료 원문·사용자 메모·페르소나 전문은 저장하지 않는다. 소재를 식별하는 정규화 키와
키워드 자체에 대한 판단만 남긴다.

증분 채점:

관련도 판정(관계 유형·소재 점수)을 **키워드 문서에 함께** 저장한다. 그래서 새 후보가
30개 추가돼도 이미 채점된 것은 다시 모델에 보내지 않고, 새 키워드만 채점해 upsert한다.
예전 캐시는 키에 '전체 키워드 목록의 digest'가 들어가 한 개만 추가돼도 캐시가 통째로
빗나갔고, 그때마다 풀 전체를 재채점했다.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from app.shared import RelationType, TrendSource, TrendSourceEvidence

from .normalizer import normalize_keyword

logger = logging.getLogger(__name__)

MATERIAL_KEYWORDS_COLLECTION = "material_related_keywords"

# 채점 기준(프롬프트·루브릭)이 바뀌면 옛 점수는 새 기준으로 매긴 점수가 아니다. 문서에
# 함께 저장해 두고, 버전이 다른 판정은 '채점 안 된 것'으로 보아 다시 채점한다. 전체 삭제
# 없이 자연 무효화하는 방법이다.
RELEVANCE_PROMPT_VERSION = 5

# 한 소재가 보관할 수 있는 키워드 수 상한. 무한정 자라면 조회·정렬 비용이 커지고 오래된
# 저품질 후보가 쌓이기만 한다. 넘으면 관련도가 낮은 것부터 버린다.
MATERIAL_POOL_MAX_SIZE = 120

# 소재 풀을 이만큼 채우는 것을 목표로 보충 수집한다. 화면 한 배치(16)의 두 배 이상이라
# '다른 후보 보기'를 몇 번 눌러도 새 얼굴이 남아 있다.
MATERIAL_TARGET_POOL_SIZE = 40

_NON_WORD = re.compile(r"[^0-9a-z가-힣]")



def material_key(topic: str, subject: str | None = None) -> str:
    """소재를 식별하는 정규화 키.

    "배틀 그라운드"·"배틀그라운드"·"BATTLEGROUNDS"를 같은 소재로 묶으려면 표기 차이를
    지워야 한다. 다만 **확신할 수 있는 것만** 지운다 — 공백·대소문자·기호까지다.
    영문 표기와 한글 표기를 같은 소재로 합치는 것(배틀그라운드 ↔ BATTLEGROUNDS)은
    사전 없이는 확정할 수 없으므로 하지 않는다. 잘못 합치면 두 소재의 키워드가 서로에게
    새어 나가고, 그건 따로 두어 생기는 중복보다 훨씬 나쁘다.

    subject(소재 설명)는 키에 넣지 않는다. 같은 소재를 사람마다 다르게 설명하는데 그것이
    키에 들어가면 소재 풀이 설명문마다 쪼개져 재사용이라는 목적 자체가 사라진다.
    """
    return _NON_WORD.sub("", (topic or "").strip().lower())


@dataclass
class MaterialKeyword:
    """소재 풀에 저장된 키워드 하나와, 그에 대해 내려진 판단.

    판정 필드(relation_type·subject_relevance·verified_at·prompt_version)가 비어 있으면
    '아직 채점되지 않음'이며, 증분 채점의 대상이 된다.
    """

    keyword: str
    normalized_keyword: str
    source: TrendSource
    sources: list[TrendSource] = field(default_factory=list)
    # 소스가 관측한 관심도(네이버 검색량 재정렬 순위·유튜브 조회 규모)를 40~100으로
    # 정규화한 값. 관련도가 같을 때의 정렬 기준이다.
    demand_score: float = 50.0
    relation_type: RelationType | None = None
    subject_relevance: float | None = None
    purpose_relevance: float | None = None
    persona_relevance: float | None = None
    relevance: float | None = None
    category: str | None = None
    prompt_version: int | None = None
    collected_at: float = 0.0
    verified_at: float | None = None
    # 출처별 실제 수집 근거(키 = TrendSource 값). 근거가 생기기 전에 저장된 문서에는
    # 없으므로 기본은 빈 dict — 그때 화면은 지표 대신 중립 문구를 쓴다.
    evidence_by_source: dict[str, TrendSourceEvidence] = field(default_factory=dict)

    @property
    def is_scored(self) -> bool:
        """현재 채점 기준으로 판정이 끝났는가. 기준이 바뀌면(prompt_version 불일치)
        점수가 있어도 다시 채점해야 한다."""
        return (
            self.relation_type is not None
            and self.subject_relevance is not None
            and self.prompt_version == RELEVANCE_PROMPT_VERSION
        )


class MaterialKeywordStore(Protocol):
    async def load(self, key: str) -> list[MaterialKeyword]: ...

    async def save(self, key: str, keywords: Sequence[MaterialKeyword]) -> None: ...


class InMemoryMaterialKeywordStore:
    """Mongo가 없을 때(테스트·로컬 최초 구동)의 저장소. 프로세스와 함께 사라진다."""

    def __init__(self) -> None:
        self._pools: dict[str, dict[str, MaterialKeyword]] = {}

    @property
    def name(self) -> str:
        return "메모리"

    async def load(self, key: str) -> list[MaterialKeyword]:
        return _ranked(list(self._pools.get(key, {}).values()))

    async def save(self, key: str, keywords: Sequence[MaterialKeyword]) -> None:
        pool = self._pools.setdefault(key, {})
        for item in keywords:
            pool[item.normalized_keyword] = _merged(pool.get(item.normalized_keyword), item)
        self._pools[key] = {
            item.normalized_keyword: item for item in _within_cap(list(pool.values()))
        }


class MongoMaterialKeywordStore:
    """``material_related_keywords`` 컬렉션. 키워드 1개 = 문서 1개.

    (materialKey, normalizedKeyword) 유니크 인덱스가 중복 저장을 **인덱스 차원에서** 막는다.
    같은 소재의 같은 키워드가 다시 수집되면 새 문서를 만들지 않고 관측 시각·수요 점수·출처
    목록만 갱신한다(upsert).

    Mongo가 한 요청에서 실패해도 트렌드 화면이 함께 죽어서는 안 된다 — 저장소는 재사용을
    위한 것이지 유일한 근거가 아니므로, 실패는 로그만 남기고 빈 결과로 취급한다.
    """

    def __init__(self, db, collection: str = MATERIAL_KEYWORDS_COLLECTION):
        self._docs = db[collection]

    @property
    def name(self) -> str:
        return "MongoDB"

    async def load(self, key: str) -> list[MaterialKeyword]:
        try:
            raw = await self._docs.find(_pool_query(key)).to_list(length=None)
        except Exception as error:
            logger.warning("소재 키워드: Mongo 조회 실패 (%s) - 빈 풀로 처리", error)
            return []
        return _ranked([item for item in (_from_document(doc) for doc in raw) if item])

    async def save(self, key: str, keywords: Sequence[MaterialKeyword]) -> None:
        if not keywords:
            return
        try:
            from pymongo import UpdateOne

            operations = [
                UpdateOne(
                    {"materialKey": key, "normalizedKeyword": item.normalized_keyword},
                    {"$set": _to_document(key, item)},
                    upsert=True,
                )
                for item in keywords
            ]
            await self._docs.bulk_write(operations, ordered=False)
            await self._trim(key)
        except Exception as error:
            logger.warning("소재 키워드: Mongo 저장 실패 (%s)", error)

    async def _trim(self, key: str) -> None:
        """소재당 보관 상한을 지킨다 — 관련도가 낮은 것부터 버린다.

        오래된 것이 아니라 관련도가 낮은 것을 버리는 이유: 소재 관련성은 시간이 지나도
        잘 변하지 않는다("배틀그라운드 감도 설정"은 1년 뒤에도 관련 있다). 반면 관련도가
        낮은 후보는 처음부터 화면에 나갈 일이 없으므로 자리만 차지한다.

        **채점 전 후보를 먼저 버리지 않는다.** 예전에는 subjectRelevance 오름차순 한 번으로
        잘랐는데, Mongo의 오름차순은 null을 맨 앞에 두므로 **방금 수집해 아직 채점되지 않은
        후보가 가장 먼저 삭제**됐다. 풀이 상한(120개)까지 찬 소재에서는 새로 수집한 것이
        채점되기도 전에 사라져, 수집을 몇 번 돌려도 후보가 한 개도 늘지 않았다 — 무관
        키워드로 가득 찬 콜롬비아 풀이 스스로 회복하지 못한 이유다.
        """
        query = _pool_query(key)
        count = await self._docs.count_documents(query)
        excess = count - MATERIAL_POOL_MAX_SIZE
        if excess <= 0:
            return

        # 1단계: 채점이 끝난 후보 중 관련도가 낮은 것부터.
        ids = await self._ids_to_drop(
            {**query, "subjectRelevance": {"$ne": None}},
            [("subjectRelevance", 1), ("demandScore", 1)],
            excess,
        )
        # 2단계: 그래도 넘치면 채점 전 후보 중 오래 전에 관측된 것부터.
        if len(ids) < excess:
            ids += await self._ids_to_drop(
                {**query, "subjectRelevance": None},
                [("collectedAt", 1)],
                excess - len(ids),
            )
        if ids:
            await self._docs.delete_many({"_id": {"$in": ids}})

    async def _ids_to_drop(self, query: dict, order: list, limit: int) -> list:
        docs = (
            await self._docs.find(query, {"_id": 1}).sort(order).limit(limit).to_list(length=None)
        )
        return [doc["_id"] for doc in docs]


def _pool_query(key: str) -> dict:
    """풀 키(=materialKey)로 문서를 찾는 질의."""
    return {"materialKey": key}


def _ranked(pool: list[MaterialKeyword]) -> list[MaterialKeyword]:
    """소재 관련도 내림차순, 동점은 수요 점수 순. 채점 전(None)은 맨 뒤로 보낸다 —
    검증되지 않은 키워드가 검증된 것보다 앞설 근거가 없다."""
    return sorted(
        pool,
        key=lambda item: (
            -(item.subject_relevance if item.subject_relevance is not None else -1.0),
            -item.demand_score,
            item.normalized_keyword,
        ),
    )


def _within_cap(pool: list[MaterialKeyword]) -> list[MaterialKeyword]:
    """보관 상한을 지킨 풀. 버리는 순서는 MongoMaterialKeywordStore._trim과 같다 —
    채점된 것 중 관련도 낮은 것 먼저, 그래도 넘치면 채점 전 후보 중 오래된 관측 먼저.

    두 저장소가 같은 순서를 써야 테스트가 운영 동작을 대신 보여줄 수 있다."""
    if len(pool) <= MATERIAL_POOL_MAX_SIZE:
        return _ranked(pool)

    scored = [item for item in pool if item.is_scored]
    unscored = [item for item in pool if not item.is_scored]
    excess = len(pool) - MATERIAL_POOL_MAX_SIZE

    drop = min(excess, len(scored))
    if drop:
        scored = _ranked(scored)[: len(scored) - drop]
        excess -= drop
    if excess:
        # 관측이 최근인 것을 남긴다.
        unscored = sorted(unscored, key=lambda item: -item.collected_at)[
            : max(0, len(unscored) - excess)
        ]
    return _ranked([*scored, *unscored])


def _merged(existing: MaterialKeyword | None, incoming: MaterialKeyword) -> MaterialKeyword:
    """같은 키워드를 다시 만났을 때의 병합 규칙.

    이미 채점된 판정은 지우지 않는다 — 새 수집분에는 판정이 없으므로(수집 직후), 덮어쓰면
    매번 재채점하게 되어 증분 채점이라는 목적이 무너진다. 출처는 합집합으로 넓히고,
    수요 점수는 더 최근 관측을 택한다."""
    if existing is None:
        return incoming
    sources = list(dict.fromkeys([*existing.sources, *incoming.sources]))
    scored = incoming if incoming.is_scored else existing
    # 근거는 출처별로 합치되, 같은 출처는 관측(observedAt)이 더 최신인 쪽을 남긴다.
    # 새 수집분에 근거가 없다고 기존 근거를 지우지 않는다.
    evidence = dict(existing.evidence_by_source)
    for source_key, incoming_evidence in (incoming.evidence_by_source or {}).items():
        current = evidence.get(source_key)
        if current is None or (incoming_evidence.observed_at or "") >= (
            current.observed_at or ""
        ):
            evidence[source_key] = incoming_evidence
    return MaterialKeyword(
        keyword=existing.keyword,
        normalized_keyword=existing.normalized_keyword,
        source=existing.source,
        sources=sources,
        demand_score=max(existing.demand_score, incoming.demand_score),
        relation_type=scored.relation_type,
        subject_relevance=scored.subject_relevance,
        purpose_relevance=scored.purpose_relevance,
        persona_relevance=scored.persona_relevance,
        relevance=scored.relevance,
        category=scored.category or existing.category,
        prompt_version=scored.prompt_version,
        collected_at=existing.collected_at or incoming.collected_at,
        verified_at=scored.verified_at,
        evidence_by_source=evidence,
        # 문맥 축도 판정을 가진 쪽을 따른다 — 채점 결과 묶음이므로 subject 등과 같이 움직인다.
    )


def _to_document(key: str, item: MaterialKeyword) -> dict:
    return {
        # key는 이미 material_key(topic)로 정규화된 소재 키다. upsert 필터·_pool_query가
        # 이 key를 그대로 쓰므로 저장 필드도 같은 값이어야 조회가 맞는다. (소재 문맥 기능이
        # 제거되며 사라진 material_key_of를 여기서 호출해 저장이 NameError로 통째로 실패했다.)
        "materialKey": key,
        "keyword": item.keyword,
        "normalizedKeyword": item.normalized_keyword,
        "source": item.source.value,
        "sources": [source.value for source in (item.sources or [item.source])],
        "demandScore": item.demand_score,
        "relationType": item.relation_type.value if item.relation_type else None,
        "subjectRelevance": item.subject_relevance,
        "purposeRelevance": item.purpose_relevance,
        "personaRelevance": item.persona_relevance,
        "relevance": item.relevance,
        "category": item.category,
        # 문맥 한정 채점 축(§9·§14). BROAD 풀에서는 None.
        "promptVersion": item.prompt_version,
        "collectedAt": item.collected_at or time.time(),
        "verifiedAt": item.verified_at,
        "lastAccessedAt": time.time(),
        # 출처별 근거는 프론트가 받는 것과 같은 camelCase JSON으로 저장한다. 근거가 없으면
        # None — 옛 문서와 같은 모양이라 역호환 걱정이 없다.
        "evidenceBySource": (
            {
                source_key: evidence.model_dump(by_alias=True, mode="json")
                for source_key, evidence in item.evidence_by_source.items()
            }
            if item.evidence_by_source
            else None
        ),
    }


def _from_document(doc: dict) -> MaterialKeyword | None:
    keyword = doc.get("keyword")
    if not isinstance(keyword, str) or not keyword.strip():
        return None
    try:
        source = TrendSource(doc.get("source"))
    except ValueError:
        return None
    relation = doc.get("relationType")
    try:
        relation_type = RelationType(relation) if relation else None
    except ValueError:
        relation_type = None
    return MaterialKeyword(
        keyword=keyword,
        normalized_keyword=doc.get("normalizedKeyword") or normalize_keyword(keyword),
        source=source,
        sources=[
            candidate
            for candidate in (_source_or_none(value) for value in doc.get("sources") or [])
            if candidate
        ]
        or [source],
        demand_score=_number(doc.get("demandScore"), 50.0),
        relation_type=relation_type,
        subject_relevance=_number_or_none(doc.get("subjectRelevance")),
        purpose_relevance=_number_or_none(doc.get("purposeRelevance")),
        persona_relevance=_number_or_none(doc.get("personaRelevance")),
        relevance=_number_or_none(doc.get("relevance")),
        category=doc.get("category"),
        prompt_version=doc.get("promptVersion") if isinstance(doc.get("promptVersion"), int) else None,
        collected_at=_number(doc.get("collectedAt"), 0.0),
        verified_at=_number_or_none(doc.get("verifiedAt")),
        evidence_by_source=_evidence_from_document(doc.get("evidenceBySource")),
    )


def _evidence_from_document(raw: object) -> dict[str, TrendSourceEvidence]:
    """저장된 근거를 복원한다. 근거가 없거나(구버전 문서) 형식이 깨진 항목은 조용히
    비운다 — 근거는 부가 정보라, 이것 때문에 키워드 자체를 잃으면 안 된다."""
    if not isinstance(raw, dict):
        return {}
    evidence: dict[str, TrendSourceEvidence] = {}
    for source_key, value in raw.items():
        if not isinstance(source_key, str) or not isinstance(value, dict):
            continue
        try:
            evidence[source_key] = TrendSourceEvidence.model_validate(value)
        except Exception:
            continue
    return evidence


def _source_or_none(value: object) -> TrendSource | None:
    try:
        return TrendSource(value)
    except ValueError:
        return None


def _number(value: object, fallback: float) -> float:
    parsed = _number_or_none(value)
    return fallback if parsed is None else parsed


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
