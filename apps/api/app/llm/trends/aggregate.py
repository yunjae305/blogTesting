"""자격 증명이 있는 모든 소스로 요청을 펼쳐 하나의 순위 집합으로 합친다.

기존 단일 소스 방식으로는 할 수 없던 세 가지:

- 키워드가 실제로 나온 소스를 함께 담아, UI가 출처를 표시할 수 있다;
- 실패하거나 자격 증명이 없는 소스는 패널을 깨뜨리지 않고 결과만 저하시킨다;
- 소스를 번갈아 섞어, 사용자가 보는 몇 장의 카드가 구글만 네 줄이 아니라
  모든 소스를 아우른다.
"""

import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from hashlib import sha1
from collections import Counter
from math import ceil
from typing import Callable, Sequence

from app.llm.contracts import TrendFetchInput, TrendFetchResult
from app.shared import (
    MATERIAL_RELATION_MIN_SUBJECT,
    RelationType,
    TrendKeyword,
    TrendMode,
    TrendSource,
    TrendSourceEvidence,
)

from app.llm.contracts import (
    KeywordJudgment,
    KeywordRelevanceInput,
    KeywordRelevanceRanker,
)

from app.shared.format import now_iso

from .base import CollectedKeyword, TrendCollector, seed_queries
from .cache import POOL_TTL_SECONDS, CachedPool, InMemoryPoolCache, PoolCache
from .exposure import (
    ExposureSignature,
    InMemoryTrendExposureStore,
    TrendExposureStore,
)
from .material_store import (
    RELEVANCE_PROMPT_VERSION,
    InMemoryMaterialKeywordStore,
    MaterialKeyword,
    MaterialKeywordStore,
    material_key,
)
from .normalizer import normalize_keyword
from .similarity import (
    KeywordSignature,
    are_similar,
    jaccard_similarity,
    keyword_signature,
    keyword_tokens,
    naturalness_score,
)
from .text import is_low_quality_keyword

logger = logging.getLogger(__name__)

DEFAULT_MAX_KEYWORDS = 4

# 회전이 각 소스의 관련도순 풀에서 어디까지 내려갈 수 있는지.
#
# 이전 시도는 60점 이상인 키워드를 패널에 고정했는데, 이는 고정 문제를 그대로
# 되살렸다: 보통 60을 넘는 키워드가 정확히 하나뿐이라 새로고침 때마다 첫 카드에
# 눌러앉았다 — 가장 뜨거운 것을 고정할 때의 바로 그 문제다. 가장 관련도 높은 몇 개
# 안에서 회전하면 화면에 보이는 것이 읽을 만하면서도 계속 바뀐다.
ROTATION_WINDOW = 8
MIN_RELEVANCE_FOR_STRICT_PICK = 30.0

# 최신순 '다른 후보 보기'(shuffle)의 표본 창: 인기순 상위 이 비율 안에서만 무작위로 뽑는다.
# 전체 풀 무작위는 한물간 하위 후보까지 섞여서, "진짜 뜨는 것 위주로 매번 다른 조합"이라는
# 버튼의 약속에 맞게 상위 구간으로 좁혔다(창 하한은 표시 개수의 2배 — _merge 참고).
TRENDING_SHUFFLE_TOP_FRACTION = 0.2

# 추천어(최신순, TRENDING)는 소재 관련도 LLM을 호출하지 않는다. 지금 실제로 뜨는 트렌드면
# 소재가 '빵'이든 'AIONA'든 같은 시점에는 같은 결과가 나와야 하고, 첫 진입 화면이 소재별
# 채점을 기다릴 이유도 없다. 소재 메아리·저품질 같은 결정적 코드 필터만 적용하며, 소재 적합성은
# 별도의 소재 관련순(MATERIAL_RELATED)이 책임진다.

# 소재 단어에 기계적으로 붙는 접미사. "빵추천"(빵+추천), "국내여행추천"(국내여행+추천)처럼 소재
# 뒤에 이것만 남으면 자연스러운 검색어가 아니라 기계적 메아리라 제외한다. "빵집"(집)·"소금빵"·
# "빵집 추천"(집이 남음)·"빵지순례"는 남긴다 — 소재 뒤에 실제 의미어가 남기 때문.
MECHANICAL_ECHO_SUFFIXES = ("추천", "인기", "순위", "모음", "리스트", "정리", "총정리", "베스트")

# 소재를 되풀이할 뿐인 메아리("AI AIONA", "생성형 AI")를 걸러낼 때 소재명과 함께 붙는 범용어.
# 이것들을 떼고 남은 의미 토큰이 소재(topic) 토큰의 부분집합이거나, 아무것도 안 남으면 메아리다(§9).
_ECHO_FILLER = {
    "ai",
    "생성형",
    "최신",
    "글로벌",
    "기반",
    "트렌드",
    "이슈",
    "변화",
    "기술",
    "플랫폼",
    "도구",
    "서비스",
    "솔루션",
    "시스템",
    "콘텐츠",
    "관련",
    # 발굴 질의의 축이 그대로 캐여 나온 것. '콜롬비아 업데이트'로 검색한 문서에서 '업데이트'가
    # 언급 1위(117회)로 올라왔다 — 소재가 무엇이든 나오고, 그 자체로는 아무 내용이 없다.
    "업데이트",
}
# 관련도 채점 한 호출에 싣는 키워드 수. 풀 전체(250여 개)를 한 번에 채점하면 출력이
# 수천 토큰이라 수십 초 걸린다 — 조각으로 나눠 병렬 호출하면 벽시계 시간이 가장 느린
# 조각 하나(수 초)로 줄어든다. 2단계 첫 진입이 느리던 주범.
RELEVANCE_CHUNK_SIZE = 60

# 수집하기로 풀을 합쳐 키울 때의 소스당 상한. 무한정 자라면 관련도 채점(풀 전체를 한 번에
# 모델에 보낸다)이 비싸지므로, 새 수집분 우선으로 자른다.
POOL_MERGE_CAP = 200
# 노출 이력 보관 기간과 사용자·글·모드당 개수 상한. 값은 환경변수로 덮어쓸 수 있다
# (app/config.py) — 저장소가 Redis로 나가면서 운영 중 조정이 필요해졌다.
HISTORY_TTL_SECONDS = 24 * 60 * 60.0
HISTORY_MAX_ENTRIES = 120


def _exposure_signature(keyword: str) -> ExposureSignature:
    """키워드 하나의 노출 지문. 세 축 중 하나만 겹쳐도 '이미 보여준 것'으로 본다."""
    signature = keyword_signature(keyword)
    return ExposureSignature(
        normalized=signature.normalized,
        token_set_signature=signature.token_set_signature,
        cluster_id=signature.cluster_id,
    )


def _is_newer_observation(
    candidate: TrendSourceEvidence, current: TrendSourceEvidence
) -> bool:
    """같은 출처의 근거가 둘일 때 어느 쪽을 남길지 — observedAt이 더 최신인 쪽.
    ISO 문자열이라 사전순 비교가 시간순 비교다. 관측 시각이 없는 쪽은 항상 진다."""
    return (candidate.observed_at or "") >= (current.observed_at or "")


def _known_keywords(cached: CachedPool | None) -> frozenset[str]:
    """이미 저장된 풀의 정규화 키워드 집합. 수집기에 넘겨 뒷순위로 미루게 한다.

    수집기는 새 키워드를 앞에, 이미 아는 키워드를 뒤에 두고 돌려준다 — 저장 병합
    (_merge_pools)이 앞선 것부터 담으므로, 저장 상한(POOL_MERGE_CAP)에 걸리더라도
    실제로 새로운 키워드가 밀려나지 않는다."""
    if cached is None:
        return frozenset()
    return frozenset(normalize_keyword(item.keyword) for item in cached.keywords)


STALE_BACKGROUND_MIN_INTERVAL = 30.0

# 소스별 수집 제한 시간. 넘으면 그 소스의 기여는 **통째로** 버려진다.
#
# 네이버·유튜브가 8초인 이유는 실측이다. 두 소스는 HTTP만 하지 않는다 — 돌아온 문서에서
# 구절을 캐내는 CPU 작업이 붙는다.
#
#   네이버: 시드 4~8개 × 검색 4종(HTTP 0.2초, 병렬) + 문서 500개에서 구절 5,500개 추출
#           0.6~2.5초
#   유튜브: search.list → videos.list 두 번 왕복 + 추출 = 1.5~4.4초(첫 호출이 가장 느리다)
#
# 3~4초는 HTTP만 있던 시절의 값이다. 그래서 '콜롬비아'에서 네이버가 찾아 온 소재 포함 후보
# 482개(콜롬비아 원두·톨리마·투마코…)가 매 요청 폐기되고 저장 풀에는 유튜브 태그만 남았다 —
# 소재 관련순에 무관 키워드만 뜨던 진짜 원인이다. 제한 시간은 상한일 뿐이라, 올려도 성공하는
# 요청이 더 느려지지는 않는다(버려지던 것이 쓰이게 된다).
SOURCE_TIMEOUTS = {
    # 구글은 트렌드 페이지를 브라우저로 읽는다: 헤드리스 Chrome 기동 ~1.1초 + 렌더 대기
    # ~2.0초 + 추출 ~0.2초 = 실측 3.3초. 6초로는 첫 실행(드라이버 내려받기)이나 느린 PC를
    # 못 견딘다. 넉넉히 두어도 성공하는 요청이 느려지지는 않는다 — 상한일 뿐이다.
    TrendSource.GOOGLE_TRENDS: 25.0,
    TrendSource.NAVER_DATALAB: 8.0,
    TrendSource.YOUTUBE: 8.0,
    TrendSource.INSTAGRAM: 3.0,
}

SENTENCE_FRAGMENT_MARKERS = (
    "따르면",
    "관련해",
    "예정이라고",
    "밝혔다",
    "웬만한",
    "통합전산망에",
)

# 라운드로빈 순서. 구글이 먼저: 진짜 실시간 트렌드 피드를 가진 유일한 소스라
# 첫 카드를 차지한다.
SOURCE_ORDER = [
    TrendSource.GOOGLE_TRENDS,
    TrendSource.NAVER_DATALAB,
    TrendSource.YOUTUBE,
    TrendSource.INSTAGRAM,
]


@dataclass
class _TrendPerformance:
    request_type: str
    started: float = field(default_factory=time.perf_counter)
    redis_connect_ms: float = 0.0
    cache_read_ms: float = 0.0
    google_fetch_ms: float = 0.0
    naver_fetch_ms: float = 0.0
    youtube_fetch_ms: float = 0.0
    instagram_fetch_ms: float = 0.0
    merge_ms: float = 0.0
    normalize_ms: float = 0.0
    filter_ms: float = 0.0
    relevance_ms: float = 0.0
    cluster_ms: float = 0.0
    select_ms: float = 0.0
    cache_write_ms: float = 0.0
    cache_statuses: list[str] = field(default_factory=list)
    refreshing: bool = False
    raw_candidates: int = 0
    valid_candidates: int = 0
    unique_clusters: int = 0
    history_excluded: int = 0
    final_keywords: list[str] = field(default_factory=list)
    # 소재 관련순 구간별 시간. 100초가 어디서 났는지 로그 한 줄로 짚을 수 있어야 한다.
    material_collect_ms: float = 0.0
    material_prefilter_ms: float = 0.0
    material_topup_ms: float = 0.0
    # 저장 풀 재사용 화면의 근거 보강(네이버 측정)에 쓴 시간. 첫 노출에서만 발생하고
    # 결과가 저장되므로 같은 키워드에 다시 쌓이지 않는다.
    material_measure_ms: float = 0.0
    material_prefiltered: int = 0
    # 사전 필터를 통과한 후보 중 이름에 소재가 든 것. 보충 회차를 돌릴지 여기서 정한다 —
    # 채점 전에 알 수 있는 신호라, 판단에 모델 왕복을 한 번 더 쓰지 않는다.
    material_subject_candidates: int = 0
    # 실제로 밖에 나간 횟수. cache_hit을 추측이 아니라 사실로 적기 위한 것이다.
    material_source_fetches: int = 0
    material_score_calls: int = 0

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000

    @property
    def cache_status(self) -> str:
        statuses = set(self.cache_statuses)
        if "miss" in statuses:
            return "miss"
        if "stale" in statuses:
            return "stale"
        if "fresh" in statuses:
            return "fresh"
        return "miss"


def _monthly_trend_score(hotness: float, source_count: int) -> float:
    """최신순 트렌드 점수를 계산한다.

    최신순은 소재별 관련도 채점을 쓰지 않는다. 외부 소스마다 지표가 달라 소스 고유 점수는
    먼저 40~100 범위의 hotness로 정규화하고, 여러 플랫폼에서 함께 확인된 키워드에
    cross_source 가산점을 준다. 관련도 항을 제거한 뒤에도 1~100 스케일을 유지하도록 남은
    두 신호를 재정규화했다.
    """
    cross_source = 100.0 if source_count >= 2 else 45.0
    score = hotness * 0.65 + cross_source * 0.35
    return round(max(1.0, min(100.0, score)), 1)


# 소재 관련순 화면이 지키는 개수들.
#
# MIN_VISIBLE(8)은 "화면이 비지 않았다"고 말할 수 있는 최소치다. RESPONSE_SIZE(16)는 한 번에
# 내려주는 크기로, 화면은 8개를 먼저 보이고 '더 보기'로 나머지를 편다 — 한 번의 왕복으로 두
# 배치를 확보해 '더 보기'가 서버를 다시 부르지 않게 한다.
MATERIAL_MIN_VISIBLE = 8
MATERIAL_RESPONSE_SIZE = 16
# 수집을 몇 번까지 돌릴지. 0회차는 기본 소재 질의, 1회차는 소재 중심 질의를 넓힌 보충
# 수집이다(=보충 1회). 예전에는 3회였고 2·3회차가 LLM 확장이라, 한 요청에 모델 왕복이
# 다섯 번 넘게 직렬로 쌓여 100초를 넘겼다. 두 번 다 검색으로 못 채우면 있는 만큼만
# 보여준다 — 무관 키워드로 자리를 채우는 것보다 적게 보여주는 편이 낫다.
MATERIAL_MAX_COLLECT_ROUNDS = 2

# 이름에 소재가 없는 후보에게 "소재와 함께 관측됐다"는 근거를 인정하는 소스.
#
# 이 집합은 세 곳에서 같은 뜻으로 쓰인다: 수집 사전 필터, 노출 게이트, 그리고 보충 수집이
# 캐시를 우회하는 소스(네이버는 sort=date라 되부르면 실제로 다른 문서가 온다).
#
# 네이버 검색만 남긴 것은 측정 결과다. '콜롬비아'로 유튜브를 검색하면 돌아오는 태그
# 1,251개가 전부 방송사·채널 상투어였다(KBS 스포츠·조별리그·손흥민·세계여행). 태그는
# 그 영상이 무엇에 관한지가 아니라 채널이 늘 붙이는 말이라, "소재를 물어봤더니 돌아왔다"가
# 근거가 되지 못한다 — 화면에 뜬 '남미여행'·'세계여행'이 정확히 이 경로였다. 인스타그램
# 해시태그도 성격이 같다.
#
# 반면 네이버 검색 후보는 문서 본문에서 캐낸 구절이고 점수가 곧 몇 개 문서에서 함께
# 나왔는지다 — 소재와의 동시 등장이 실제로 관측된 값이다(§1 "실제 수집 데이터에서 소재와
# 반복적으로 함께 등장").
#
# 대가는 정직하게 적어 둔다: 유튜브 태그 중 소재를 담지 않은 진짜 관련어(배틀그라운드의
# '에란겔' 같은)도 함께 떨어진다. 측정된 대안은 무관 태그 100%를 받아들이는 것이었다.
# 소재를 담은 태그·제목 구절('배틀그라운드 감도')은 그대로 통과한다.
SUBJECT_EVIDENCE_SOURCES = frozenset({TrendSource.NAVER_DATALAB})

# 이름에 소재가 들어 있지 않은 후보에 적용하는 더 엄격한 게이트.
#
# '보고타'·'메데인'은 소재명을 담지 않아도 콜롬비아 그 자체에 속한 고유 대상이고,
# '세계여행'·'여행유튜버'는 콜롬비아를 검색한 문서에 함께 나왔을 뿐인 광역어다. 코드가
# 둘을 구분할 방법은 없지만 모델은 관계 유형으로 구분한다 — 전자는 DIRECT/ADJACENT,
# 후자는 CONTEXTUAL이다. 그래서 소재를 담지 않은 후보에게는 상황 연결(CONTEXTUAL)을
# 허용하지 않고 소재 점수 하한도 함께 올린다.
MATERIAL_OFFTOPIC_RELATIONS = frozenset({RelationType.DIRECT, RelationType.ADJACENT})
MATERIAL_OFFTOPIC_MIN_SUBJECT = float(os.getenv("TREND_MATERIAL_OFFTOPIC_MIN", "60"))

# 모델이 **명시적으로 거부한** 판정. 이것만 "무관"으로 취급한다.
#
# AMBIGUOUS는 여기 없다. 그것은 "무관하다"가 아니라 "판단할 수 없다"는 뜻이고, 둘을 같이
# 묶는 것이 소재 관련순이 통째로 비던 원인이었다. 소재가 `보지냐`(카보베르데 골키퍼
# Vozinha의 음차)였을 때, 네이버 문서에서 캐낸 `카보베르데`(언급 1위)·`카보베르데 골키퍼`·
# `Vozinha`가 모두 AMBIGUOUS 15~20점을 받아 전부 탈락했다 — 정답이 풀에 있는데 화면은
# 비었다. 모델이 모르는 표기(외래어 음차·신조어·낯선 고유명사)에서는 늘 이렇게 된다.
# 모델이 모른다고 답할 때는, 관측된 사실(그 문서들이 이 말을 몇 번 함께 했는지)이 판단의
# 근거가 되어야 한다.
MATERIAL_REJECTED_RELATIONS = frozenset({RelationType.NONE, RelationType.FORCED})

# 게이트가 하나도 통과시키지 못했을 때 마지막으로 채우는 개수.
MATERIAL_FALLBACK_VISIBLE = 8

# 한 요청에서 관련도 채점에 보낼 수 있는 신규 후보 수의 상한.
#
# 예전에는 풀 전체(최대 120개)가 채점 대상이라 60개짜리 조각이 두 개씩 나갔고, 보충
# 회차마다 그것이 되풀이됐다. 코드 사전 필터를 통과한 상위 30개만 보내면 조각 하나로
# 끝난다(RELEVANCE_CHUNK_SIZE=60) — 한 요청에 모델 호출 한 번이다.
MATERIAL_LLM_BATCH_LIMIT = 30



@dataclass(frozen=True)
class _CursorState:
    next_cursor: str
    has_more: bool
    cycled: bool


def _material_window(
    eligible: Sequence["MaterialKeyword"], cursor: str | None, limit: int
) -> tuple[list["MaterialKeyword"], _CursorState]:
    """커서 위치에서 limit개를 잘라 낸다. 끝에 닿으면 순환한다 — 빈 배열을 반환하지 않는다.

    커서는 "<offset>.<cycle>" 꼴의 불투명 문자열이다. cycle을 함께 싣는 이유는 순환할 때
    순서를 바꾸기 위해서다: 같은 순서로 다시 돌면 '다른 후보 보기'가 첫 배치를 그대로
    되돌려줘 버튼이 아무 일도 안 한 것처럼 보인다. cycle 수만큼 시작점을 어긋나게 해
    새 순환의 첫 배치가 직전 배치와 겹치지 않게 한다.

    풀이 limit보다 작으면 순환해도 같은 것이 나올 수밖에 없다. 그건 후보가 그것뿐이라는
    뜻이고, 빈 화면보다는 같은 후보를 다시 보여주는 편이 정직하다.
    """
    total = len(eligible)
    if total == 0:
        return [], _CursorState(next_cursor="0.0", has_more=False, cycled=False)

    offset, cycle = _parse_cursor(cursor)
    cycled = False
    if offset >= total:
        # 한 바퀴 돌았다. 순환을 하나 올리고 처음부터 — 단, 시작점을 어긋나게 한다.
        cycle += 1
        offset = 0
        cycled = True

    ordered = _rotated(list(eligible), cycle)
    window = ordered[offset : offset + limit]
    # 풀이 limit보다 작아 창이 모자라면 앞에서 이어 붙인다. 8개를 보여줄 수 있는데
    # 커서 위치 때문에 3개만 보여주는 일이 없게 한다.
    if len(window) < min(limit, total):
        window += ordered[: min(limit, total) - len(window)]

    next_offset = offset + len(window)
    return window, _CursorState(
        next_cursor=f"{next_offset}.{cycle}",
        has_more=next_offset < total,
        cycled=cycled,
    )


def _parse_cursor(cursor: str | None) -> tuple[int, int]:
    """망가진 커서는 오류가 아니라 '처음부터'로 본다 — 커서 때문에 화면이 깨지지 않게."""
    if not cursor:
        return 0, 0
    offset, _, cycle = cursor.partition(".")
    try:
        return max(0, int(offset)), max(0, int(cycle or 0))
    except ValueError:
        return 0, 0


def _rotated(pool: list["MaterialKeyword"], cycle: int) -> list["MaterialKeyword"]:
    """순환 회차만큼 시작점을 어긋나게 한 목록. 0회차는 관련도 순서 그대로다."""
    if cycle <= 0 or not pool:
        return pool
    shift = (cycle * MATERIAL_MIN_VISIBLE) % len(pool)
    return pool[shift:] + pool[:shift]


def _mentions_subject(keyword: str, subject_compact: str) -> bool:
    """키워드 안에 소재가 그대로 들어 있는가. '콜롬비아 커피' → True, '세계여행' → False."""
    if not subject_compact:
        return False
    return subject_compact in _compact_keyword(keyword)


def _has_subject_evidence(sources: Sequence[TrendSource]) -> bool:
    """소재를 담지 않은 이 후보에게 동시 등장 근거가 있는가(SUBJECT_EVIDENCE_SOURCES).

    수집 시점(_material_prefilter)과 노출 시점(_material_eligible)이 **같은 규칙**을 써야
    한다. 수집만 막으면 이미 저장된 옛 후보가 계속 화면에 오르고, 노출만 막으면 쓸 수 없는
    후보를 계속 저장하고 채점한다.
    """
    return any(source in SUBJECT_EVIDENCE_SOURCES for source in sources)


def _material_eligible(item: "MaterialKeyword", subject_compact: str) -> bool:
    """소재 관련순 노출 자격. 소재를 이름에 담았는지에 따라 판정하는 방식이 다르다.

    **1티어 — 이름에 소재가 들어 있으면 모델 판정을 기다리지 않는다.** `콜롬비아 원두`가
    콜롬비아에 관한 검색어라는 것은 코드가 이미 아는 사실이고, 확인을 위해 모델을 부를
    이유가 없다(요청서 §1의 첫 기준이 "소재를 직접 포함"이다). 여기서 빼는 것은 모델이
    **명시적으로 거부한** 경우뿐이다(NONE·FORCED). 예전에는 `is_scored`를 요구해서, 채점
    차례를 못 받은 후보가 풀에 있어도 화면이 비었다.

    **2티어 — 소재를 담지 않은 후보**는 더 좁은 게이트를 통과해야 한다
    (MATERIAL_OFFTOPIC_RELATIONS + 동시 등장 근거). '보고타'는 콜롬비아에 속한 대상이지만
    '세계여행'은 어느 여행 글에나 있는 광역어이고, 그 구분은 모델의 관계 유형만이 안다.

    **관계 유형만으로는 부족하다는 것이 실측으로 드러났다.** 저장된 콜롬비아 풀에서
    '남미여행'은 DIRECT 85점, '세계여행'은 ADJACENT 60점을 받아 유형 게이트를 통과했다.
    소재를 담지 않은 후보에게는 모델 판정에 앞서 **동시 등장 근거**를 요구한다 — 유튜브
    태그처럼 채널 상투어일 수 있는 것은 소재를 이름에 담을 때만 통과한다.

    이 게이트는 이미 저장된 풀에도 적용된다. 무관 후보가 들어와 채점까지 끝난 옛 문서를
    지우지 않고도 화면에서 막을 수 있어야 하기 때문이다.
    """
    if item.relation_type in MATERIAL_REJECTED_RELATIONS:
        return False

    if _mentions_subject(item.keyword, subject_compact):
        return True

    # 여기서부터는 소재를 이름에 담지 않은 후보다. 판정이 없으면 근거가 없다.
    if not item.is_scored or item.relation_type is None:
        return False
    if not _has_subject_evidence(item.sources or [item.source]):
        return False
    if item.relation_type not in MATERIAL_OFFTOPIC_RELATIONS:
        return False
    # 유형별 하한과 함께 적용한다 — 소재를 담지 않은 후보가 담은 후보보다 낮은 문턱을
    # 넘는 일은 없어야 한다(DIRECT의 유형 하한 70이 여기 하한 60보다 높다).
    minimum = max(
        MATERIAL_RELATION_MIN_SUBJECT.get(item.relation_type, 0.0),
        MATERIAL_OFFTOPIC_MIN_SUBJECT,
    )
    return (item.subject_relevance or 0.0) >= minimum


def _eligible_material(
    pool: Sequence["MaterialKeyword"],
    trend_input: TrendFetchInput,
) -> list["MaterialKeyword"]:
    """게이트를 통과하고 화면에 나갈 수 있는 후보를, 관련도 내림차순으로.

    노출 필터(저품질·문장 조각·소재 메아리·기계적 조합)를 여기서 함께 건다 — 적격 집계와
    실제 노출이 같은 집합을 봐야 "적격은 있는데 화면은 빈" 교착이 생기지 않는다.
    유사 중복은 클러스터 하나당 가장 자연스러운 것 하나만 남긴다.
    """
    topic_tokens = _subject_echo_tokens(trend_input)
    subject_compact = _compact_keyword(trend_input.input.topic)

    survivors: list[tuple["MaterialKeyword", KeywordSignature]] = []
    for item in pool:
        if not _material_eligible(item, subject_compact):
            continue
        if not _passes_display_filters(item.keyword, topic_tokens, subject_compact):
            continue
        signature = keyword_signature(item.keyword)
        existing = next(
            (
                index
                for index, (_, other) in enumerate(survivors)
                if are_similar(signature, other)
            ),
            None,
        )
        if existing is None:
            survivors.append((item, signature))
            continue
        # 같은 뜻의 두 표기 중 더 자연스러운 쪽을 남긴다. "배틀그라운드 초보 공략"과
        # "배틀그라운드 무기 추천"처럼 검색 의도가 다른 것은 토큰이 달라 애초에 여기
        # 오지 않는다 — 표기만 다른 중복에만 걸린다.
        current, current_signature = survivors[existing]
        if naturalness_score(item.keyword, signature) > naturalness_score(
            current.keyword, current_signature
        ):
            survivors[existing] = (item, signature)

    # 소재를 이름에 담은 후보를 먼저 보여준다. '콜롬비아 여행'과 '남미여행'이 둘 다 통과할
    # 때, 직접 관련 후보가 있는데 간접 후보를 앞세울 이유가 없다. 같은 묶음 안에서는
    # 저장소가 매긴 관련도 순서를 그대로 둔다(정렬이 안정적이라 유지된다).
    survivors.sort(key=lambda pair: 0 if _mentions_subject(pair[0].keyword, subject_compact) else 1)
    return _interleave_by_source([item for item, _ in survivors])


def _interleave_by_source(pool: list["MaterialKeyword"]) -> list["MaterialKeyword"]:
    """출처를 번갈아 뽑아, 한 화면이 한 소스로 덮이지 않게 한다.

    최신순은 선택 단계에서 출처별 상한으로 균형을 맞추지만(_merge), 소재 관련순은 관련도
    순서를 그대로 잘라 쓰므로 그 장치가 없었다 — 소재 '참이슬'에서 유튜브가 16칸 중 12칸을
    가져가고 네이버가 4칸이던 실측이 그 결과다. 출처별 순서(관련도)는 그대로 두고 뽑는
    차례만 번갈아 준다.

    커서 로테이션이 이 목록을 잘라 쓰므로 순서는 결정적이어야 한다 — 정렬이 아니라 고정된
    라운드로빈이라 같은 풀이면 언제나 같은 순서다.
    """
    by_source: dict[TrendSource, list["MaterialKeyword"]] = {}
    for item in pool:
        by_source.setdefault(item.source, []).append(item)
    if len(by_source) <= 1:
        return pool

    # 소스 차례는 SOURCE_ORDER를 따른다(구글 먼저) — 목록에 없는 출처는 뒤에 붙인다.
    order = sorted(
        by_source,
        key=lambda source: (
            SOURCE_ORDER.index(source) if source in SOURCE_ORDER else len(SOURCE_ORDER)
        ),
    )
    interleaved: list["MaterialKeyword"] = []
    for index in range(max(len(bucket) for bucket in by_source.values())):
        for source in order:
            bucket = by_source[source]
            if index < len(bucket):
                interleaved.append(bucket[index])
    return interleaved


def _cooccurrence_material(
    pool: Sequence["MaterialKeyword"],
    trend_input: TrendFetchInput,
) -> list["MaterialKeyword"]:
    """게이트가 하나도 통과시키지 못했을 때의 마지막 단계 — 관측된 동시 등장으로 채운다.

    왜 필요한가: 소재가 모델이 모르는 표기일 때 판정이 전부 AMBIGUOUS로 떨어지고, 그러면
    화면이 빈다. `보지냐`(카보베르데 골키퍼 Vozinha)가 그랬다 — 풀에 `카보베르데`가 언급
    1위로 들어와 있는데도 화면은 "관련 검색어를 찾지 못했습니다"였다. 모델이 모른다고 답하는
    것은 무관하다는 증거가 아니다.

    무엇을 근거로 삼는가: **네이버 검색 문서에서 몇 번 함께 등장했는지**(demand_score)다.
    소재를 질의로 넣어 받은 문서들이 반복해서 말한 단어이므로, 요청서 §1의 세 번째 기준
    ("실제 수집 데이터에서 소재와 반복적으로 함께 등장")을 그대로 만족한다. 태그·실시간
    피드에서 온 것은 여기 들어오지 못한다(동시 등장이 관측된 소스가 아니다).

    모델이 **명시적으로 거부한** 후보(NONE·FORCED)는 뒤로 미룬다 — 앞자리는 판단이 없는
    후보(AMBIGUOUS·미채점)에게 준다. 지어내지는 않는다: 수집된 것 안에서만 고른다.
    """
    topic_tokens = _subject_echo_tokens(trend_input)
    subject_compact = _compact_keyword(trend_input.input.topic)

    candidates = [
        item
        for item in pool
        if _has_subject_evidence(item.sources or [item.source])
        and _passes_display_filters(item.keyword, topic_tokens, subject_compact)
    ]
    def rank(item: "MaterialKeyword") -> tuple[int, float]:
        return (
            1 if item.relation_type in MATERIAL_REJECTED_RELATIONS else 0,
            -item.demand_score,
        )

    candidates.sort(key=lambda item: (*rank(item), item.normalized_keyword))

    # 같은 구절에서 잘려 나온 n-gram을 접는다. 접지 않으면 여덟 자리를 한 문구가 차지한다
    # ('HERE'·'HERE WE'·'HERE WE GO'·'WE GO'가 실측에서 네 자리를 먹었다).
    #
    # 조각인지 아닌지를 가르는 신호는 **언급 수가 같은지**다. 한 구절을 자른 조각들은 같은
    # 문서에서 함께 세어지므로 언급 수가 동일하다(위 넷이 전부 42.9). 반면 '카보베르데'(100)와
    # '카보베르데 골키퍼'(61.5)는 수가 다르고, 실제로 서로 다른 검색어다 — 후자는 이 글의
    # 핵심 키워드이므로 접어서 버리면 안 된다. 그래서 **순위가 같을 때만** 접고, 그때 더
    # 온전한 쪽(토큰이 많은 쪽)을 남긴다.
    #
    # 이 규칙은 마지막 단계에만 적용한다. 정상 경로(_eligible_material)는 '콜롬비아 여행'과
    # '콜롬비아 음식'처럼 토큰이 겹치는 후보를 함께 보여줘야 한다.
    picked: list[tuple["MaterialKeyword", KeywordSignature, set[str]]] = []
    for item in candidates:
        signature = keyword_signature(item.keyword)
        tokens = set(signature.tokens)
        overlap = next(
            (
                index
                for index, (other, other_signature, other_tokens) in enumerate(picked)
                if rank(item) == rank(other)
                and (are_similar(signature, other_signature) or (tokens & other_tokens))
            ),
            None,
        )
        if overlap is None:
            picked.append((item, signature, tokens))
            continue
        _, _, current_tokens = picked[overlap]
        if len(tokens) > len(current_tokens):
            picked[overlap] = (item, signature, tokens)
    return [item for item, _, _ in picked[:MATERIAL_FALLBACK_VISIBLE]]


def _source_log_label(source: TrendSource) -> str:
    """로그에 찍는 소스 이름. 실제로 부르는 API의 이름이어야 한다.

    NAVER_DATALAB은 저장된 문서와 화면 라벨이 쓰는 값이라 그대로 두지만(값을 바꾸면
    쌓여 있는 소재 풀이 통째로 못 읽힌다), 이제 DataLab은 호출하지 않는다 — 네이버
    검색 API만 쓴다. 로그가 부르지도 않는 API 이름을 말하면 병목을 찾을 때 헤맨다.
    """
    return "NAVER_SEARCH" if source == TrendSource.NAVER_DATALAB else source.value


def _material_prefilter(
    by_source: dict[TrendSource, list[CollectedKeyword]],
    trend_input: TrendFetchInput,
    known: set[str],
    limit: int,
) -> tuple[list["MaterialKeyword"], int]:
    """수집분을 LLM에 보내기 전에 코드로 걸러 낸다.

    두 가지를 동시에 해결한다.

    - **품질**: 동시 등장 근거가 없는 소스(구글 트렌드 실시간 피드, 유튜브·인스타그램의
      태그)의 후보는 이름에 소재가 들어 있지 않으면 버린다. 근거가 없는 후보이기 때문이고,
      이것들이 저장까지 되면 관계 유형 게이트가 한 번 실수할 때마다 화면에 올라온다
      (SUBJECT_EVIDENCE_SOURCES 주석의 측정 결과 참고).
    - **속도**: 저품질·문장 조각·소재 메아리·기계적 조합·중복을 여기서 떨어뜨리고 상위
      `limit`개만 남긴다. 예전에는 걸러지지 않은 후보까지 전부 채점 대상이라, 모델에
      보내는 양이 그대로 대기 시간이 됐다.

    순서는 소재를 담은 후보 먼저, 그다음 수집 점수 순이다 — 잘려 나가는 것은 언제나
    근거가 약한 쪽이어야 한다.

    후보 목록과 함께 '이름에 소재가 든 후보의 수'를 돌려준다. 보충 회차를 돌릴지 정하는
    신호이며, 채점 전에 알 수 있어야 모델 왕복을 한 번 더 쓰지 않는다.
    """
    topic_tokens = _subject_echo_tokens(trend_input)
    subject_compact = _compact_keyword(trend_input.input.topic)

    scored: list[tuple[int, float, "MaterialKeyword"]] = []
    seen = set(known)
    for source, collected in by_source.items():
        has_evidence = _has_subject_evidence([source])
        for item in collected:
            keyword = item.keyword.strip()
            normalized = normalize_keyword(keyword)
            if not normalized or normalized in seen:
                continue
            mentions = _mentions_subject(keyword, subject_compact)
            if not has_evidence and not mentions:
                continue
            if not _passes_display_filters(keyword, topic_tokens, subject_compact):
                continue
            seen.add(normalized)
            scored.append(
                (
                    0 if mentions else 1,
                    -item.score,
                    MaterialKeyword(
                        keyword=keyword,
                        normalized_keyword=normalized,
                        source=source,
                        sources=[source],
                        demand_score=item.score,
                        collected_at=time.time(),
                        # 수집기가 관측한 근거를 저장까지 실어 보낸다. 없으면 빈 dict.
                        evidence_by_source=(
                            {source.value: item.evidence} if item.evidence else {}
                        ),
                    ),
                )
            )

    scored.sort(key=lambda entry: (entry[0], entry[1], entry[2].normalized_keyword))
    kept = scored[:limit]
    return [item for _, _, item in kept], sum(1 for rank, _, _ in kept if rank == 0)


def _to_trend_keywords(
    pool: Sequence["MaterialKeyword"], collected_at: str
) -> list[TrendKeyword]:
    """저장소 모델 → 화면 모델. 판정 근거(관계 유형·축별 점수)를 그대로 실어 보낸다."""
    keywords: list[TrendKeyword] = []
    per_source: Counter[TrendSource] = Counter()
    for index, item in enumerate(pool):
        per_source[item.source] += 1
        signature = keyword_signature(item.keyword)
        subject = item.subject_relevance or 0.0
        keywords.append(
            TrendKeyword(
                trend_keyword_id=f"trend_{item.source.value.lower()}_{per_source[item.source]}",
                keyword=item.keyword,
                normalized_keyword=signature.normalized,
                tokens=list(signature.tokens),
                token_set_signature=signature.token_set_signature,
                cluster_id=signature.cluster_id,
                source=item.source,
                sources=list(item.sources or [item.source]),
                rank=index + 1,
                score=subject,
                trend_score=subject,
                hotness=round(item.demand_score, 1),
                quality_score=100.0,
                final_score=subject,
                trend_reason=_material_reason(item),
                connection_idea=None,
                period="소재 관련 검색어",
                relevance=item.relevance,
                subject_relevance=item.subject_relevance,
                purpose_relevance=item.purpose_relevance,
                persona_relevance=item.persona_relevance,
                is_eligible=True,
                relation_type=item.relation_type,
                category=item.category,
                evidence_by_source=item.evidence_by_source or None,
                collected_at=collected_at,
            )
        )
    return keywords


def _material_reason(item: "MaterialKeyword") -> str:
    """왜 이 키워드가 소재와 관련 있다고 판단했는지. 관계 유형을 사람 말로 옮긴다.

    판정이 없거나 '판단 불가'인 후보에게 관계 유형의 문장을 붙이지 않는다 — 모델이 하지
    않은 판단을 했다고 말하는 것이기 때문이다. 그 경우 근거는 관측된 동시 등장이다.
    """
    if not item.is_scored or item.relation_type == RelationType.AMBIGUOUS:
        return "소재를 검색한 문서에서 함께 등장한 검색어입니다."
    described = {
        RelationType.DIRECT: "소재를 직접 다루는 검색어입니다.",
        RelationType.ADJACENT: "이 소재를 찾는 사람이 함께 검색하는 주제입니다.",
        RelationType.CONTEXTUAL: "소재를 둘러싼 상황으로 자연스럽게 이어지는 주제입니다.",
    }.get(item.relation_type or RelationType.DIRECT, "소재와 관련이 확인된 검색어입니다.")
    if item.source == TrendSource.RELATED_EXPANSION:
        # 관측된 수요가 아니라는 사실을 숨기지 않는다.
        return f"{described} (검색 결과가 아니라 소재에서 확장한 후보입니다.)"
    return described


def _passes_display_filters(keyword: str, topic_tokens: set[str], subject_compact: str) -> bool:
    """_merge가 노출 전에 거는 사전 필터(저품질·문장 조각·소재 메아리·기계적 조합)와 같은 판정.

    eligible_in_store가 이 필터 없이 원시 풀을 세면, 화면에는 못 나가는 키워드(예: 'AI
    AIONA' — 소재 관련도 게이트는 통과하지만 소재 메아리)가 적격으로 집계돼 수집 폴백이 막히고
    패널이 영구히 빈다. 적격 판정과 노출은 같은 집합을 봐야 한다."""
    signature = keyword_signature(keyword)
    if not signature.normalized:
        return False
    if is_low_quality_keyword(keyword) or _is_sentence_fragment(keyword):
        return False
    return not (
        _is_subject_echo(signature, topic_tokens)
        or _is_mechanical_echo(keyword, subject_compact)
    )


def _mode_trend_score(
    mode: TrendMode, hotness: float, relevance: float | None, source_count: int
) -> float:
    """모드별 최종 점수(리스트 순위가 이 값을 쓴다).

    추천어(최신순): 소재 관련도를 **넣지 않는다** — 리스트는 소재와
    무관한 실시간 인기순이어야 하고, 같은 시점이면 소재가 무엇이든 결과가 거의 같아야 하기
    때문이다(§4·§6). '추천' 배지도 최신순 안에서는 트렌드 점수가 가장 높은 키워드에 붙는다.

    소재 관련어: **소재 관련도 내림차순** — 탭 이름 그대로다(2026-07-22 결정: "관련된
    키워드면 다 보여주되 관련도 높은 순"). 게이트(_is_eligible)가 '아무 상관 없음'을 이미
    걸렀으므로, 여기서 받는 relevance는 소재 축 점수다(_merge가 모드에 맞는 축을 넘긴다).
    동점은 entry_score의 tie(검색 관심도 hotness)가 가른다. 판정이 없는 후보는 게이트에서
    이미 빠졌으므로 relevance=None은 방어적 최저값으로만 처리한다.
    """
    if mode == TrendMode.MATERIAL_RELATED:
        return round(max(1.0, min(100.0, relevance if relevance is not None else 1.0)), 1)
    return _monthly_trend_score(hotness, source_count)


def _subject_echo_tokens(trend_input: TrendFetchInput) -> set[str]:
    """메아리 판정의 기준이 되는 소재(제품명) 토큰. 제목(topic)만 쓴다 — 소재 설명이나 키워드까지
    넣으면 '멀티 LLM' 같은 실제 관련 개념까지 메아리로 오인한다."""
    return set(keyword_tokens(trend_input.input.topic))


def _is_subject_echo(signature: KeywordSignature, topic_tokens: set[str]) -> bool:
    """소재를 되풀이할 뿐인 키워드인가.

    'AI AIONA', '생성형 AIONA'처럼 범용어를 떼면 소재명(topic)만 남거나(=소재의 부분집합), 'AI
    트렌드', '생성형 AI'처럼 아무 의미 토큰도 안 남으면(순수 범용어) 메아리다. 두 모드 모두에서
    제외한다(§7·§9): 추천어에서는 트렌드가 아니고, 소재 관련어에서는 소재의 재배열일 뿐이다.
    """
    tokens = set(signature.tokens)
    if not tokens:
        return False
    meaningful = tokens - _ECHO_FILLER
    if not meaningful:
        return True
    if not topic_tokens:
        return False
    return meaningful.issubset(topic_tokens)


def _compact_keyword(value: str) -> str:
    """공백·기호를 떼고 소문자로. 소재-접미사 메아리 판정의 부분문자열 비교에 쓴다."""
    return re.sub(r"[^0-9a-z가-힣]", "", (value or "").lower())


def _strip_mechanical_suffixes(text: str) -> str:
    """뒤에서부터 기계적 접미사(추천·인기·순위…)를 벗겨 남는 부분을 돌려준다."""
    changed = True
    while changed and text:
        changed = False
        for suffix in MECHANICAL_ECHO_SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)]
                changed = True
                break
    return text


def _is_mechanical_echo(keyword: str, subject_compact: str) -> bool:
    """소재 단어 뒤에 기계적 접미사만 붙은 조합('빵추천', '국내여행추천')인가.

    소재가 맨 앞에 오고, 그 뒤에 남는 것이 오로지 추천/인기/순위 같은 접미사뿐일 때만 True다.
    '빵집'(집)·'소금빵'(소재가 앞이 아님)·'빵집 추천'(집이 남음)·'빵지순례'는 소재 뒤에 실제
    의미어가 남으므로 False — 자연스러운 검색어라 살린다. 소재가 한 글자('빵')여도 동작한다.
    """
    if not subject_compact:
        return False
    compact = _compact_keyword(keyword)
    if not compact.startswith(subject_compact) or compact == subject_compact:
        return False
    remainder = compact[len(subject_compact):]
    return bool(remainder) and _strip_mechanical_suffixes(remainder) == ""


def _trend_reason(source_count: int, relevance: float | None) -> str:
    source_text = "여러 출처에서 동시에 확인된" if source_count >= 2 else "현재 트렌드 피드에서 확인된"
    if relevance is not None and relevance >= 60:
        return f"{source_text} 키워드이며, 입력한 소재와 자연스럽게 연결됩니다."
    if relevance is not None and relevance < MIN_RELEVANCE_FOR_STRICT_PICK:
        return f"{source_text} 키워드이지만, 입력한 소재와의 연결성은 낮습니다."
    return f"{source_text} 키워드입니다."


def _is_sentence_fragment(value: str) -> bool:
    text = (value or "").strip()
    return any(marker in text for marker in SENTENCE_FRAGMENT_MARKERS)


def _normalize(pool: Sequence[CollectedKeyword]) -> dict[str, float]:
    """한 소스의 고유 점수를 공용 1-100 범위로 매핑한다.

    검색량, 조회수, 언급 수는 원시 숫자로는 비교할 수 없다 — 5만 점을 받은 구글
    키워드가 50점을 받은 인스타그램 해시태그보다 천 배 더 뜨거운 건 아니다.
    """
    if not pool:
        return {}

    scores = [item.score for item in pool]
    low, high = min(scores), max(scores)
    span = high - low
    step = 60.0 / (len(pool) - 1) if len(pool) > 1 else 0.0

    normalized: dict[str, float] = {}
    for index, item in enumerate(pool):
        if span > 0:
            normalized[item.keyword] = 40.0 + 60.0 * (item.score - low) / span
        else:
            # 의미 있는 점수 없이 순위만 매기는 소스 — 위치를 쓴다.
            normalized[item.keyword] = 100.0 - index * step
    return normalized


def _rescore_pool(pool: list[CollectedKeyword]) -> list[CollectedKeyword]:
    """수집 직후 소스 고유 점수를 40~100 정규화 점수로 바꿔 저장한다.

    점수 채점 방식을 하나로 확실하게: DB(trend_keywords)의 score는 어느 소스든
    **그 수집 회차 안에서의 상대 인기(40~100, 100=1위급)**다. 구글 검색량 50000,
    네이버 순위 램프 80, 유튜브 언급 가중 7.08처럼 단위가 제각각인 원시 값을 그대로
    저장하지 않는다 — 화면 순위(상승도 0.65+교차 0.35)와 '최신순' 정렬이 이 값을 쓴다.
    """
    normalized = _normalize(pool)
    return [
        CollectedKeyword(
            keyword=item.keyword,
            score=round(normalized.get(item.keyword, item.score), 1),
            rank=item.rank,
            category=item.category,
            # 점수는 정규화해도 원본 근거(검색량·조회수·문서 수)는 그대로 싣는다 —
            # 근거를 여기서 흘리면 저장·화면까지 아무것도 남지 않는다.
            evidence=item.evidence,
        )
        for item in pool
    ]


def _merge_pools(
    new: Sequence[CollectedKeyword],
    old: Sequence[CollectedKeyword],
    cap: int = POOL_MERGE_CAP,
) -> list[CollectedKeyword]:
    """새 수집분을 앞에 두고, 기존 풀에서 아직 없는 키워드를 뒤에 이어 붙인다.

    수집하기(force_collect)가 쓰는 병합이다. rank는 합쳐진 순서로 다시 매긴다 —
    _normalize가 점수 우선·순위 폴백으로 동작하므로, 소스 고유 점수는 그대로 두고
    순위만 일관되게 만들면 된다.

    근거는 새 수집분 것이 이긴다(더 최신 관측). 단, 새 항목에 근거가 없으면 기존
    풀의 근거를 이어받는다 — 없는 것이 있는 것을 지우면 안 된다.
    """
    old_evidence = {
        normalize_keyword(item.keyword): item.evidence for item in old if item.evidence
    }
    seen = {normalize_keyword(item.keyword) for item in new}
    seen.discard("")
    merged = []
    for item in new:
        if item.evidence is None:
            inherited = old_evidence.get(normalize_keyword(item.keyword))
            if inherited is not None:
                item = CollectedKeyword(
                    keyword=item.keyword,
                    score=item.score,
                    rank=item.rank,
                    category=item.category,
                    evidence=inherited,
                )
        merged.append(item)
    for item in old:
        norm = normalize_keyword(item.keyword)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        merged.append(item)

    return [
        CollectedKeyword(
            keyword=item.keyword,
            score=item.score,
            rank=index + 1,
            category=item.category,
            evidence=item.evidence,
        )
        for index, item in enumerate(merged[:cap])
    ]


def _cache_key(source: TrendSource, trend_input: TrendFetchInput) -> str:
    """풀이 실제로 무엇에 의존하는지.

    추천어(TRENDING)는 소재와 무관한 '지금 뜨는 트렌드' 공용 풀이다 — 소재 시드를 키에서
    빼서 모든 글이 같은 풀을 공유하고, trend_keywords에 누적된 것을 재수집 없이 재사용한다
    (자동 재수집은 30일마다, 즉시 갱신은 '수집하기'). 예전엔 시드가 키에 들어가 소재가
    바뀔 때마다 캐시를 못 찾고 매번 소스 API를 다시 불렀다.

    소재 관련어(MATERIAL_RELATED)는 네이버·인스타그램이 사용자 자신의 단어로 검색하므로,
    주제가 다른 두 글이 같은 풀을 공유하지 않도록 시드를 키에 넣는다.

    보충 회차(widen_material)는 넓힌 질의로 다른 문서를 보는 다른 수집이라 키도 다르다.
    같은 키를 쓰면 0회차가 방금 채운 캐시에 걸려 보충이 같은 결과를 되받는다. 다만 질의가
    실제로 바뀌는 소스는 네이버 검색뿐이다 — 유튜브·구글은 회차와 무관하게 같은 것을
    돌려주므로, 그쪽까지 키를 갈라 두면 같은 답을 받으려고 유튜브 할당량(검색 100유닛)과
    SerpApi 크레딧을 회차마다 한 번씩 더 쓴다.
    """
    country = trend_input.country or ""
    category = trend_input.category or ""
    seeds = (
        "|".join(seed_queries(trend_input))
        if trend_input.mode == TrendMode.MATERIAL_RELATED
        else ""
    )
    scope = (
        ":widen"
        if trend_input.widen_material and source in SUBJECT_EVIDENCE_SOURCES
        else ""
    )
    return f"trend:pool:{source.value}:{country}:{category}:{seeds}{scope}"


def _bare_pool_key(source: TrendSource) -> str:
    """추천어 공용 풀 키(국가·카테고리·시드 비움). MongoPoolCache가 이 형태의 키만
    trend_keywords 컬렉션(키워드당 문서 하나)에 영속한다. 소재 관련순의 DB 우선 조회와
    수집 폴백의 upsert가 이 키를 쓴다."""
    return f"trend:pool:{source.value}:::"


class AggregateTrendProvider:
    """소스로 요청을 펼치고, 응답을 캐시하며, 그 일부를 회전하는 창으로 내보낸다.

    이전에는 아무것도 캐시하지 않아, 제목 스텝을 열 때마다 매번 외부 API 네 곳을
    호출했다 — 1분 전에 키워드를 수집한 글이든, 새로고침을 누를 때마다든 마찬가지였다.
    트렌드 키워드는 그렇게 빨리 바뀌지 않고, SerpApi는 검색당 과금한다.

    캐시는 화면의 카드 네 장이 아니라 수집된 풀을 보관한다. 그것이 캐시와 정지의
    차이다: 새로고침은 여전히 패널을 바꾼다. 원래 패널을 바꾸는 것은 재수집이 아니라
    남는 후보를 회전하는 것이었기 때문이다.
    """

    def __init__(
        self,
        collectors: Sequence[TrendCollector],
        rotate: Callable[[int], int] | None = None,
        cache: PoolCache | None = None,
        ttl_seconds: float = POOL_TTL_SECONDS,
        ranker: KeywordRelevanceRanker | None = None,
        sampler: Callable[[Sequence, int], list] | None = None,
        exposure: TrendExposureStore | None = None,
        material_store: MaterialKeywordStore | None = None,
    ):
        self._collectors = list(collectors)
        # 각 소스의 풀에서 창을 회전하는 것이 새로고침 때 다른 집합을 보여준다.
        self._rotate = rotate or (lambda size: random.randrange(size) if size else 0)
        # 최신순 '다른 후보 보기'(shuffle)의 무작위 표본 추출. 테스트가 결정적 표본을
        # 주입할 수 있게 함수로 둔다.
        self._sample = sampler or random.sample
        self._cache = cache or InMemoryPoolCache()
        self._ttl = ttl_seconds
        self._ranker = ranker
        # 소재별 관련 키워드 풀. Mongo가 붙으면 use_material_store로 교체된다.
        self._material_store: MaterialKeywordStore = (
            material_store or InMemoryMaterialKeywordStore()
        )
        # 노출 이력. 기본은 메모리지만 운영에서는 Redis 저장소를 주입한다 — 메모리에 두면
        # 재시작마다 초기화되고 API 서버를 늘리는 순간 서버마다 다른 이력을 보게 된다.
        self._exposure: TrendExposureStore = exposure or InMemoryTrendExposureStore(
            HISTORY_TTL_SECONDS, HISTORY_MAX_ENTRIES
        )
        self._refreshing: set[str] = set()
        self._last_background_refresh: dict[str, float] = {}
        # 소재 관련순 수집 폴백의 단일화(§중복 호출 방지): 같은 소재+목적+페르소나 조합의
        # 동시 요청은 수집 작업 하나를 공유한다. 완료 후의 재사용은 시드 캐시·DB가 맡는다.
        self._material_inflight: dict[str, asyncio.Task] = {}

    def use_exposure_store(self, exposure: TrendExposureStore) -> None:
        """조립 후 노출 이력 저장소를 교체한다.

        LLM 프로바이더는 Redis 연결보다 먼저 만들어지므로, 연결이 되면 여기로 Redis
        저장소를 주입한다(use_pool_cache와 같은 이유)."""
        self._exposure = exposure

    def use_material_store(self, store: MaterialKeywordStore) -> None:
        """조립 후 소재 키워드 저장소를 교체한다(use_pool_cache와 같은 이유 — Mongo 연결이
        LLM 프로바이더 생성보다 뒤에 이뤄진다)."""
        self._material_store = store

    def use_pool_cache(self, cache: PoolCache) -> None:
        """조립 후 캐시를 교체한다. Mongo 연결이 LLM 프로바이더 생성보다 뒤에 이뤄지므로,
        연결이 되면 영속 캐시(MongoPoolCache)를 여기로 주입해 수집분을 DB에 누적한다."""
        self._cache = cache

    async def fetch_trends(self, trend_input: TrendFetchInput) -> TrendFetchResult:
        # 소재 관련순은 흐름이 다르다: 저장된 풀(DB)을 먼저 보고, 소재 관련도 게이트를
        # 통과한 적격 후보가 하나도 없을 때만 수집한다. 수집하기(force_collect)는
        # DB 조회를 건너뛰고 바로 수집한다.
        if trend_input.mode == TrendMode.MATERIAL_RELATED:
            return await self._fetch_material(trend_input)
        collected_at = now_iso()
        limit = trend_input.max_keywords or DEFAULT_MAX_KEYWORDS
        seen_before = await self._exposure.has_any(self._history_key(trend_input))
        request_type = "refresh" if trend_input.exclude_keywords or seen_before else "initial"
        perf = _TrendPerformance(request_type=request_type)

        # 최신순도 DB 우선이다: 저장된 공용 풀(trend_keywords)을 그대로 보여준다. 신선도
        # (TTL)로 자동 재수집하지 않는다 — 소스 API를 부르는 것은 저장분이 아예 없을 때
        # (첫 실행)와 '수집하기'(force_collect)뿐이고, 수집분은 풀에 합쳐져 DB에 남는다.
        by_source: dict[TrendSource, list[CollectedKeyword]] = {}
        if not trend_input.force_collect:
            by_source = await self._stored_pools(perf)
        if not by_source or trend_input.force_collect:
            results = await asyncio.gather(
                *(self._collect(collector, trend_input, perf) for collector in self._collectors),
                return_exceptions=True,
            )
            by_source = {}
            for result in results:
                if isinstance(result, BaseException):
                    logger.warning("트렌드 수집 작업 실패 - %s", result)
                    continue
                source, pool = result
                if pool:
                    by_source[source] = pool

        perf.raw_candidates = sum(len(pool) for pool in by_source.values())
        raw_counts = {source.value: len(pool) for source, pool in by_source.items()}
        unique_norms = {
            normalize_keyword(item.keyword)
            for pool in by_source.values()
            for item in pool
            if normalize_keyword(item.keyword)
        }

        # 최신순은 수집된 실시간 인기 신호를 그대로 보여준다. 소재·목적·페르소나별 LLM
        # 채점은 이 화면의 순위나 노출 조건에 필요하지 않고, 새 소재마다 캐시 키가 달라져
        # 첫 진입을 붙잡던 가장 큰 병목이었다. 소재 관련순은 위에서 _fetch_material로 분기해
        # 필요한 경우에만 동기 채점한다.
        merge_start = time.perf_counter()
        keywords = await self._merge(by_source, limit, collected_at, trend_input, perf)
        perf.merge_ms = (time.perf_counter() - merge_start) * 1000
        if not keywords:
            # 소재 관련어는 소재와 직접 관련된 후보가 없으면 빈 패널이 정상이다(무관 키워드로
            # 채우지 않는다). 단, 수집 자체가 전부 실패해 원자료가 없으면 오류이므로 알린다.
            if trend_input.mode != TrendMode.MATERIAL_RELATED or not by_source:
                raise RuntimeError("no trend source returned any keyword")
        # 수집하기(force_collect)의 응답은 화면에 그리지 않는다 — 여기서 이력에 넣으면
        # 사용자가 본 적 없는 키워드 16개가 노출된 것으로 기록돼 회전에서 빠져 버린다.
        if not trend_input.force_collect:
            await self._remember_history(trend_input, keywords)
        perf.final_keywords = [keyword.keyword for keyword in keywords]
        logger.info(
            "[Trend Debug] raw candidates=%s, normalized=%d, history_exclude=%d, final=%s",
            raw_counts,
            len(unique_norms),
            perf.history_excluded,
            perf.final_keywords,
        )
        logger.info(
            "[Trend Performance] request_type=%s redis_connect_ms=%.1f cache_read_ms=%.1f "
            "google_fetch_ms=%.1f naver_fetch_ms=%.1f youtube_fetch_ms=%.1f "
            "merge_ms=%.1f normalize_ms=%.1f filter_ms=%.1f relevance_ms=%.1f "
            "cluster_ms=%.1f select_ms=%.1f cache_write_ms=%.1f total_ms=%.1f "
            "cache_status=%s raw_candidates=%d valid_candidates=%d unique_clusters=%d "
            "history_excluded=%d final_keywords=%s",
            perf.request_type,
            perf.redis_connect_ms,
            perf.cache_read_ms,
            perf.google_fetch_ms,
            perf.naver_fetch_ms,
            perf.youtube_fetch_ms,
            perf.merge_ms,
            perf.normalize_ms,
            perf.filter_ms,
            perf.relevance_ms,
            perf.cluster_ms,
            perf.select_ms,
            perf.cache_write_ms,
            perf.elapsed_ms(),
            perf.cache_status,
            perf.raw_candidates,
            perf.valid_candidates,
            perf.unique_clusters,
            perf.history_excluded,
            perf.final_keywords,
        )

        return TrendFetchResult(
            trend_keywords=keywords,
            collected_at=collected_at,
            mode=trend_input.mode,
            cache_status=perf.cache_status,
            refreshing=perf.refreshing,
        )

    async def _fetch_material(self, trend_input: TrendFetchInput) -> TrendFetchResult:
        """소재 관련순: 소재 전용 풀 → 관계 유형 게이트 → 부족하면 보충 → 커서로 순환.

        예전 흐름이 빈 화면을 만들던 두 지점을 모두 바꿨다:

        1) **적격 후보가 0개일 때만 수집했다.** 2개만 있으면 보충하지 않아 화면에 2개가 떴고,
           '다른 후보 보기'로 그 둘을 제외하면 0개가 됐다. 이제는 목표치에 못 미치면 몇 개가
           있든 보충한다(_topup_material).
        2) **exclude가 있으면 순환 복구가 막혔다.** 후보를 한 바퀴 다 보면 되돌아올 길이
           없어 영구히 빈 화면이었다. 이제 위치는 커서가 들고, 끝에 닿으면 순서를 섞어
           다시 처음부터 낸다(cycled=True) — 빈 배열을 반환하는 경로가 없다.

        무관 키워드로 자리를 채우지는 않는다. 8개를 채우지 못하는 유일한 경우는 소재 관련
        후보가 세상에 그만큼 없을 때이고, 그때는 있는 만큼만 보여준다.

        수집을 언제 도는지가 처리 시간을 정한다. 예전에는 목표 풀 크기(40)에 못 미치면
        돌았는데, 적격 후보가 40개까지 가는 소재는 거의 없어 **저장된 풀이 멀쩡해도 매
        요청마다** 수집 1회 + LLM 확장 2회 + 채점 3회가 직렬로 돌았다. 이제 기준은 화면을
        채울 수 있는 최소치(8)다 — 저장분으로 8개가 나오면 외부 API도 모델도 부르지 않는다.
        """
        collected_at = now_iso()
        limit = trend_input.max_keywords or MATERIAL_RESPONSE_SIZE
        perf = _TrendPerformance(request_type="material")

        # '다른 후보 보기'는 커서만 보내 저장된 풀 안에서 회전하는 요청이다. 이때는 보충도
        # 채점도 하지 않는다 — 풀은 최초 조회에서 이미 확정했다.
        is_rotation = trend_input.cursor is not None and not trend_input.force_collect

        key = material_key(trend_input.input.topic)
        pool = await self._material_store.load(key) if key else []
        origin = "database" if pool else "external_api"

        # 저장분 중 아직 채점되지 않은 것만 채점한다(증분). 이미 다 채점돼 있으면 모델을
        # 부르지 않는다 — 캐시 요청이 빨라야 하는 이유가 여기다.
        #
        # force_collect('트렌드 새로 수집')일 때는 여기서 채점하지 않는다. 어차피 아래에서
        # 수집이 돌고, 수집 뒤에 한 번 더 채점하므로 한 요청에 모델 왕복이 두 번 쌓인다 —
        # 실측 llm_score_ms=53762(30개 × 2회)의 절반이 이 앞선 채점이었다. 수집을 돌릴지는
        # force_collect가 이미 정해 놓았으므로 여기서 eligible을 정확히 알 필요도 없다.
        if not is_rotation and not trend_input.force_collect:
            pool = await self._score_material(key, pool, trend_input, perf)
        eligible = _eligible_material(pool, trend_input)

        # 수집: 화면을 채울 수 없을 때만 돈다. force_collect('트렌드 새로 수집')는 이미
        # 충분해도 한 번은 돈다 — 사용자가 명시적으로 더 모으라고 누른 것이기 때문이다.
        #
        # **수집을 모두 끝낸 뒤에 한 번만 채점한다.** 예전에는 회차마다 채점을 돌려 모델
        # 왕복이 직렬로 쌓였다 — 두 번째 회차가 새 후보 한두 개를 위해 채점 한 번을 더
        # 쓰는 식이었다. 보충을 돌릴지는 채점 없이도 알 수 있는 신호(사전 필터를 통과한
        # 소재 포함 후보 수)로 정한다.
        rounds = 0
        if not is_rotation and (
            trend_input.force_collect or len(eligible) < MATERIAL_MIN_VISIBLE
        ):
            added = await self._topup_material(key, pool, trend_input, perf, round_index=0)
            rounds = 1

            if (
                MATERIAL_MAX_COLLECT_ROUNDS > 1
                and perf.material_subject_candidates < MATERIAL_MIN_VISIBLE
            ):
                # 방금 저장한 것을 known으로 삼아 같은 후보를 두 번 담지 않는다.
                pool = await self._material_store.load(key)
                added += await self._topup_material(
                    key, pool, trend_input, perf, round_index=1
                )
                rounds = 2

            if added:
                origin = "external_api"
                pool = await self._material_store.load(key)
                pool = await self._score_material(key, pool, trend_input, perf)
                eligible = _eligible_material(pool, trend_input)

        # 마지막 단계: 게이트가 하나도 통과시키지 못했으면 관측된 동시 등장으로 채운다.
        # 모아 온 것이 있는데 화면이 비는 일은 없어야 한다 — 모델이 소재를 모른다는 이유로
        # 정답('보지냐'에 대한 '카보베르데')까지 함께 버려지던 자리다.
        fallback = False
        if not eligible:
            eligible = _cooccurrence_material(pool, trend_input)
            fallback = bool(eligible)
            if fallback:
                logger.info(
                    "소재 관련순: 관계 판정으로는 통과한 후보가 없어 문서 동시 등장 상위 %d개로"
                    " 채웁니다 (소재=%s)",
                    len(eligible),
                    trend_input.input.topic,
                )

        window, cursor_state = _material_window(eligible, trend_input.cursor, limit)

        # 근거 도입 전에 저장된 소재 풀은 재사용될 때 지표 없이 나가, 카드 전부가
        # "상세 지표는 새 수집 후 표시됩니다"였다 — 저장분만으로 화면이 채워지는 소재는
        # 수집(그때만 근거를 재던 자리)이 다시 돌 일이 없어 영영 그대로였다. 그래서
        # 화면에 나가는 창만 여기서 보강한다: 요청당 최대 limit개(기본 8)이고, 잰 결과는
        # 풀에 저장돼 같은 키워드를 다시 재지 않는다(회전으로 돌아와도 저장분을 쓴다).
        unmeasured = [item for item in window if not item.evidence_by_source]
        if unmeasured:
            measure_start = time.perf_counter()
            await self._measure_missing_evidence(unmeasured)
            gained = [item for item in unmeasured if item.evidence_by_source]
            if gained and key:
                await self._material_store.save(key, gained)
            perf.material_measure_ms += (time.perf_counter() - measure_start) * 1000

        keywords = _to_trend_keywords(window, collected_at)

        # 노출 이력은 계속 남긴다 — 소재 확장 프롬프트가 "이미 보여준 것"을 참고해 새 얼굴을
        # 만들 때 쓴다. 다만 노출 이력이 후보를 **제외하지는** 않는다: 그 방식이 바로 풀을
        # 한 바퀴 돌면 화면이 비던 원인이었고, 지금은 커서가 순서를 책임진다.
        if not trend_input.force_collect:
            await self._remember_history(trend_input, keywords)

        # 어디에서 시간을 썼는지가 한 줄에 보여야 한다. 예전 로그는 total_ms만 있어서
        # 100초가 수집 때문인지 채점 때문인지 알 수 없었다. topup_rounds는 '보충' 횟수라
        # 첫 수집은 세지 않는다(0회차 = 초기 수집).
        logger.info(
            "[Trend Material] material=%s origin=%s source_collect_ms=%.0f prefilter_ms=%.0f"
            " llm_score_ms=%.0f topup_ms=%.0f measure_ms=%.0f total_ms=%.0f pool=%d"
            " prefiltered=%d eligible=%d shown=%d topup_rounds=%d cache_hit=%s cursor=%s→%s"
            " cycled=%s fallback=%s",
            trend_input.input.topic,
            origin,
            perf.material_collect_ms,
            perf.material_prefilter_ms,
            perf.relevance_ms,
            perf.material_topup_ms,
            perf.material_measure_ms,
            perf.elapsed_ms(),
            len(pool),
            perf.material_prefiltered,
            len(eligible),
            len(keywords),
            max(0, rounds - 1),
            # 밖에 나가지 않았고 모델도 부르지 않았다는 사실. 회차 수로 추측하지 않는다.
            "true"
            if perf.material_source_fetches == 0 and perf.material_score_calls == 0
            else "false",
            trend_input.cursor,
            cursor_state.next_cursor,
            cursor_state.cycled,
            "cooccurrence" if fallback else "none",
        )
        return TrendFetchResult(
            trend_keywords=keywords,
            collected_at=collected_at,
            mode=trend_input.mode,
            cache_status=perf.cache_status,
            refreshing=perf.refreshing,
            source=origin,
            next_cursor=cursor_state.next_cursor,
            pool_size=len(eligible),
            has_more=cursor_state.has_more,
            cycled=cursor_state.cycled,
        )

    async def _score_material(
        self,
        key: str,
        pool: list[MaterialKeyword],
        trend_input: TrendFetchInput,
        perf: _TrendPerformance,
    ) -> list[MaterialKeyword]:
        """아직 채점되지 않은 키워드만 골라 관련도를 매기고 저장한다(증분 채점).

        예전 캐시는 키에 '전체 키워드 목록의 digest'가 들어 있어 후보 하나만 추가돼도 캐시가
        통째로 빗나갔고, 그때마다 풀 전체를 다시 모델에 보냈다. 판정을 키워드 문서에 저장하면
        새 것만 보내면 된다.

        한 요청에서 보내는 양에는 상한을 둔다(MATERIAL_LLM_BATCH_LIMIT). 수집 단계에서 이미
        코드로 걸렀으므로 보통은 상한에 닿지 않지만, 사전 필터가 없던 시절에 쌓인 풀(최대
        120개)을 만나면 상한이 없을 때 조각 두 개가 나가 대기 시간이 두 배가 된다.
        """
        pending = [item for item in pool if not item.is_scored]
        if not pending or not self._ranker:
            return pool
        if len(pending) > MATERIAL_LLM_BATCH_LIMIT:
            # 소재를 이름에 담은 후보부터, 그다음 수요 점수 순. 남은 것은 다음 요청에서
            # 이어서 채점한다.
            #
            # 수요 점수만으로 자르면 소재 포함 후보가 굶는다. 콜롬비아에서 노이즈('자궁경부암'
            # 44점)가 정답('콜롬비아 원두' 12점)보다 언급 수가 높아, 채점 30자리를 노이즈가
            # 차지하고 정답은 판정 없이 남았다.
            subject_compact = _compact_keyword(trend_input.input.topic)
            pending = sorted(
                pending,
                key=lambda item: (
                    0 if _mentions_subject(item.keyword, subject_compact) else 1,
                    -item.demand_score,
                ),
            )[:MATERIAL_LLM_BATCH_LIMIT]

        relevance_start = time.perf_counter()
        perf.material_score_calls += 1
        judgments = await self._rank_in_chunks(
            trend_input, [item.keyword for item in pending]
        )
        perf.relevance_ms += (time.perf_counter() - relevance_start) * 1000
        if not judgments:
            return pool

        now = time.time()
        scored: list[MaterialKeyword] = []
        for item in pending:
            judgment = judgments.get(item.keyword)
            if judgment is None:
                continue
            item.relation_type = judgment.relation_type
            item.subject_relevance = (
                judgment.subject_relevance
                if judgment.subject_relevance is not None
                else judgment.relevance
            )
            item.purpose_relevance = judgment.purpose_relevance
            item.persona_relevance = judgment.persona_relevance
            item.relevance = judgment.relevance
            item.category = judgment.category
            item.prompt_version = RELEVANCE_PROMPT_VERSION
            item.verified_at = now
            scored.append(item)

        if scored:
            await self._material_store.save(key, scored)
        logger.info("소재 관련도: 신규 %d개만 채점 (풀 %d개)", len(scored), len(pool))
        return pool

    async def _topup_material(
        self,
        key: str,
        pool: list[MaterialKeyword],
        trend_input: TrendFetchInput,
        perf: _TrendPerformance,
        round_index: int,
    ) -> int:
        """소재를 검색해 후보를 보충한다. 0회차는 기본 질의, 1회차는 넓힌 질의다.

        **모델이 지어낸 키워드는 더 이상 쓰지 않는다.** 예전 1·2회차는 LLM 확장이었고,
        그것이 '콜롬비아 여행'처럼 그럴듯하지만 아무도 검색하지 않았을 수 있는 조합을
        만들어 냈다. 후보는 네이버 검색·유튜브 결과에서 실제로 관측된 것이어야 한다 —
        모자라면 지어내는 대신 적게 보여준다.

        보충 회차는 질의만 넓힌다: 소재 자체와 사용자 키워드는 그대로 두고, 목적 축 대신
        어느 소재에나 검색 수요가 있는 넓은 축을 붙여 다른 문서를 끌어온다. 질의는 발굴용일
        뿐 후보가 아니다 — 최종 키워드는 검색 결과 문서에서 캐낸다.
        """
        known = {item.normalized_keyword for item in pool}

        collect_start = time.perf_counter()
        by_source = await self._collect_material_pool(
            trend_input, perf, widen=round_index > 0
        )
        # 두 구간이 겹치지 않게 나눠 센다: 0회차는 초기 수집, 그다음은 보충이다.
        elapsed = (time.perf_counter() - collect_start) * 1000
        if round_index == 0:
            perf.material_collect_ms += elapsed
        else:
            perf.material_topup_ms += elapsed

        prefilter_start = time.perf_counter()
        fresh, subject_named = _material_prefilter(
            by_source, trend_input, known, MATERIAL_LLM_BATCH_LIMIT
        )
        perf.material_prefilter_ms += (time.perf_counter() - prefilter_start) * 1000
        perf.material_prefiltered += len(fresh)
        perf.material_subject_candidates += subject_named

        await self._measure_missing_evidence(fresh)

        if fresh:
            await self._material_store.save(key, fresh)
        return len(fresh)

    async def _measure_missing_evidence(self, pool: list[MaterialKeyword]) -> None:
        """수치가 없는 후보를 네이버에 물어 채운다.

        구글 자동완성이 내놓은 소재 연관 키워드가 이 경우다 — 소재와의 관련성은 분명하지만
        (구글이 '참이슬'에 이어 붙인 검색어다) 검색량·상승률을 주지 않는다. 카드의 세 줄을
        비워 두는 대신, 그 키워드를 네이버에 직접 검색해 잰다. 제안한 곳과 잰 곳이 다르므로
        화면은 두 출처를 함께 표시한다 — 구글 로고 옆에 네이버 수치를 몰래 붙이지 않는다.

        두 자리에서 돈다: 수집 직후(_topup_material)와, 근거 없이 저장돼 있던 풀을
        화면에 내보내기 직전(_fetch_material의 창 단위 보강). 어느 쪽이든 결과가 풀에
        저장되므로 같은 키워드를 화면을 열 때마다 다시 재지는 않는다.
        """
        targets = [item for item in pool if not item.evidence_by_source]
        if not targets:
            return
        measurer = next(
            (
                collector
                for collector in self._collectors
                if collector.source == TrendSource.NAVER_DATALAB
                and hasattr(collector, "measure_keywords")
            ),
            None,
        )
        if measurer is None:
            # 네이버 자격 증명이 없으면 잴 방법이 없다. 근거 없이 두고, 화면이 중립 문구로
            # 대신한다 — 다른 출처의 수치를 가져다 붙이지 않는다.
            return

        try:
            measured = await measurer.measure_keywords([item.keyword for item in targets])
        except Exception as error:  # noqa: BLE001 - 보강 실패가 수집을 죽여선 안 된다
            logger.warning("소재 관련순: 네이버 수치 보강 실패 - %s", error)
            return

        for item in targets:
            evidence = measured.get(item.keyword)
            if evidence is not None:
                item.evidence_by_source = {TrendSource.NAVER_DATALAB.value: evidence}

    async def _stored_pools(
        self, perf: _TrendPerformance
    ) -> dict[TrendSource, list[CollectedKeyword]]:
        """저장된 공용 풀(소스별 bare 키)을 DB/캐시에서 읽는다. 소스 API 호출 없음.

        근거가 있는 후보만 남긴다 — 최신순 카드는 출처별 지표 세 줄로 자신을 설명하는데,
        근거를 싣기 전에 수집된 옛 후보는 그 자리를 "상세 지표는 새 수집 후 표시됩니다"로
        채우기만 하고 화면에 나가서는 안 된다. 회전('다른 키워드 보기')이 풀 전체에서
        무작위로 뽑으므로, 선택 단계에서 뒤로 미루는 것만으로는 부족했다.

        한 출처에 근거 있는 후보가 **하나도** 없으면 있는 그대로 쓴다. 그 출처를 통째로
        지우면 화면에서 사라지는데, 설명이 덜 붙은 카드라도 보여주는 편이 빈자리보다 낫다.
        """
        pools: dict[TrendSource, list[CollectedKeyword]] = {}
        for collector in self._collectors:
            cache_start = time.perf_counter()
            cached = await self._cache.get(_bare_pool_key(collector.source))
            perf.cache_read_ms += (time.perf_counter() - cache_start) * 1000
            if cached and cached.keywords:
                perf.cache_statuses.append("fresh" if cached.is_fresh(self._ttl) else "stale")
                explained = [item for item in cached.keywords if item.evidence is not None]
                pools[collector.source] = explained or cached.keywords
            else:
                perf.cache_statuses.append("miss")
        return pools

    async def _collect_material_pool(
        self,
        trend_input: TrendFetchInput,
        perf: _TrendPerformance,
        widen: bool = False,
    ) -> dict[TrendSource, list[CollectedKeyword]]:
        """수집 폴백. 같은 소재+목적+페르소나 조합의 동시 요청은 수집 작업 하나를 공유한다
        (§중복 호출 방지). 반복 요청은 시드 캐시(TTL 30일)와 DB 저장분이 흡수하므로 같은
        입력으로 소스 API가 거듭 불리지 않는다.

        `widen`은 보충 회차다. 넓힌 질의는 다른 문서를 끌어오므로 같은 소재라도 앞 회차와
        다른 수집 작업이며, 진행 중인 작업을 공유해서는 안 된다(flight_key에 함께 넣는다).
        """
        blog_input = trend_input.input
        flight_key = sha1(
            "\x00".join(
                [
                    blog_input.topic,
                    blog_input.subject or "",
                    ",".join(blog_input.purpose or []),
                    trend_input.persona or "",
                    "widen" if widen else "",
                ]
            ).encode("utf-8")
        ).hexdigest()
        task = self._material_inflight.get(flight_key)
        if task is None:
            task = asyncio.create_task(
                self._collect_material_sources(trend_input, perf, widen=widen)
            )
            self._material_inflight[flight_key] = task
            task.add_done_callback(lambda _t: self._material_inflight.pop(flight_key, None))
        return await task

    async def _collect_material_sources(
        self,
        trend_input: TrendFetchInput,
        perf: _TrendPerformance,
        widen: bool = False,
    ) -> dict[TrendSource, list[CollectedKeyword]]:
        # 보충 수집은 **언제나 소스를 실제로 부른다**(force_collect).
        #
        # 이 경로는 "저장분으로 화면을 채울 수 없다"고 판정됐을 때만 불린다. 그런데 소스
        # 시드 캐시는 TTL 30일이라, 캐시가 신선하면 앞선 수집과 똑같은 목록이 돌아오고 그
        # 전부가 이미 저장돼 있어(known) 새 후보가 0개였다 — 보충이 구조적으로 아무 일도
        # 하지 못하는 상태였고, 그래서 후보 2개짜리 화면이 '다른 후보 보기'로도 '트렌드
        # 새로 수집'으로도 영구히 그대로였다. 캐시는 8개를 채울 수 있을 때 값을 하고
        # (그때는 이 경로에 오지 않는다), 못 채울 때는 우회해야 값을 한다.
        #
        # 되부르는 것은 네이버 검색뿐이다(_collect가 판단) — 구글 트렌드 실시간 피드와
        # 유튜브는 회차와 무관하게 같은 목록이라 새로 부를 이유가 없다.
        #
        # 보충 회차는 넓힌 질의로 돈다. 이 표식 하나가 네이버의 발굴 질의와 소스 캐시 키를
        # 함께 바꾼다 — 수집기 프로토콜은 그대로다.
        trend_input = trend_input.model_copy(
            update={"widen_material": widen, "force_collect": True}
        )

        # 소스는 이미 병렬이다(gather) — 순차 대기가 병목이었던 적은 없다. 구글 트렌드도
        # 계속 부른다: 실시간 피드에 '콜롬비아 축구'처럼 소재를 담은 검색어가 실제로 오를
        # 수 있고, 소재와 무관한 나머지는 _material_prefilter가 저장 전에 떨어뜨린다.
        # 소요 시간은 부른 쪽(_topup_material)이 회차별로 나눠 센다.
        results = await asyncio.gather(
            *(self._collect(collector, trend_input, perf) for collector in self._collectors),
            return_exceptions=True,
        )
        by_source: dict[TrendSource, list[CollectedKeyword]] = {}
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("트렌드 수집 작업 실패 - %s", result)
                continue
            source, pool = result
            if pool:
                by_source[source] = pool
        # 수집분을 공용 풀(trend_keywords)에 넣지 않는다. 예전에는 여기서 upsert했고, 그래서
        # '배틀그라운드 감도 설정' 같은 특정 소재에서만 의미 있는 키워드가 최신순 공용 풀에
        # 쌓여 아무 관계 없는 사용자에게 노출됐다. 소재 수집분은 소재 전용 저장소로만 간다
        # (_topup_material → MaterialKeywordStore).
        return by_source

    async def _rank_in_chunks(
        self,
        trend_input: TrendFetchInput,
        words: Sequence[str],
    ) -> dict[str, "KeywordJudgment"]:
        """풀을 60개씩 나눠 병렬로 채점한다. 실패한 조각은 버리고 성공분만 합친다 —
        일부 점수라도 없는 것보다 낫고, 전부 실패하면 빈 dict(뜨거움 순 폴백)이다."""
        chunks = [
            list(words[start : start + RELEVANCE_CHUNK_SIZE])
            for start in range(0, len(words), RELEVANCE_CHUNK_SIZE)
        ]
        results = await asyncio.gather(
            *(
                self._ranker.rank_keywords(
                    KeywordRelevanceInput(
                        input=trend_input.input,
                        keywords=chunk,
                        as_of=now_iso(),
                        persona=trend_input.persona,
                    )
                )
                for chunk in chunks
            ),
            return_exceptions=True,
        )
        judgments: dict[str, KeywordJudgment] = {}
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("트렌드 관련도: 채점 조각 실패 - %s", result)
                continue
            judgments.update(result)
        return judgments

    async def _collect(
        self, collector: TrendCollector, trend_input: TrendFetchInput, perf: _TrendPerformance
    ) -> tuple[TrendSource, list[CollectedKeyword]]:
        source = collector.source
        key = _cache_key(source, trend_input)
        started = time.perf_counter()

        def done(
            status: str, pool: list[CollectedKeyword]
        ) -> tuple[TrendSource, list[CollectedKeyword]]:
            """소스 하나가 얼마나 걸려 몇 개를 줬는지. 소재 관련순 병목 추적용."""
            if trend_input.mode == TrendMode.MATERIAL_RELATED:
                logger.info(
                    "[Trend Source] source=%s status=%s duration_ms=%.0f count=%d",
                    _source_log_label(source),
                    status,
                    (time.perf_counter() - started) * 1000,
                    len(pool),
                )
            return source, pool

        cache_start = time.perf_counter()
        cached = await self._cache.get(key)
        perf.cache_read_ms += (time.perf_counter() - cache_start) * 1000
        # '새로운 키워드 보기'(exclude만 보내고 force_collect 아님)는 소스 API를 다시 부르지
        # 않고, DB에 쌓인 풀에서 아직 안 본 키워드를 돌려 보여준다(제외는 아래 선택 단계에서
        # 적용). 소스를 다시 부르는 것은 '수집하기'(force_collect)뿐이다.
        #
        # 소재 관련순에서 되부르는 것은 네이버 검색뿐이다. 네이버는 최신 게시물부터 보므로
        # (sort=date) 다시 부르면 실제로 다른 문서가 온다. 반면 구글 트렌드 실시간 피드는
        # 소재가 콜롬비아든 빵이든 같은 목록이고, 유튜브도 같은 소재에 같은 영상을 준다 —
        # 되불러도 후보는 늘지 않고 SerpApi 크레딧과 유튜브 할당량(검색 100유닛)만 나간다.
        # 최신순의 '트렌드 새로 수집'은 그대로 전 소스를 되부른다.
        force_refresh = trend_input.force_collect and (
            trend_input.mode != TrendMode.MATERIAL_RELATED
            or source in SUBJECT_EVIDENCE_SOURCES
        )

        if cached and cached.is_fresh(self._ttl) and not force_refresh:
            perf.cache_statuses.append("fresh")
            logger.info(
                "트렌드 %s: %s 캐시 사용 (%.0f초 전 수집, %d개) - API 호출 없음",
                _source_log_label(source),
                self._cache.name,
                cached.age_seconds,
                len(cached.keywords),
            )
            return done("cache", cached.keywords)
        if cached and cached.is_fresh(self._ttl) and force_refresh:
            logger.info(
                "트렌드 %s: 수집하기 요청 - 캐시를 우회하고 새로 수집해 풀에 합칩니다",
                _source_log_label(source),
            )
        if cached and not cached.is_fresh(self._ttl) and not force_refresh:
            perf.cache_statuses.append("stale")
            perf.refreshing = True
            logger.info(
                "트렌드 %s: %s stale 캐시 사용 (%.0f초 전 수집, %d개) - 백그라운드 API 갱신",
                _source_log_label(source),
                self._cache.name,
                cached.age_seconds,
                len(cached.keywords),
            )
            self._schedule_source_refresh(collector, trend_input, key, _known_keywords(cached))
            return done("stale-cache", cached.keywords)

        perf.cache_statuses.append("miss")
        perf.material_source_fetches += 1
        fetch_start = time.perf_counter()
        # limit=None: 소스가 내주는 것을 전부 가져온다(예전의 소스당 20개 상한 제거 —
        # 상한이 있으면 이미 저장된 상위권 키워드가 매번 그 자리를 다시 채워, 수집해도
        # 풀이 거의 자라지 않았다). known(저장된 풀)은 수집기가 뒷순위로 미루는 데 쓴다.
        known = _known_keywords(cached)
        try:
            pool = await asyncio.wait_for(
                collector.collect(trend_input, None, known=known),
                timeout=SOURCE_TIMEOUTS.get(source, 3.0),
            )
        except asyncio.TimeoutError:
            error = TimeoutError(f"{source.value} timed out")
            if cached:
                logger.warning(
                    "트렌드 %s: 수집 타임아웃 - %.0f초 전 캐시로 대체합니다",
                    _source_log_label(source),
                    cached.age_seconds,
                )
                return done("timeout-cache", cached.keywords)
            logger.warning("트렌드 %s: 수집 타임아웃", _source_log_label(source))
            return done("timeout", [])
        except Exception as error:
            # 죽은 소스 하나가 패널 전체를 끌어내려선 안 된다. 만료된 풀이라도 없는
            # 것보다는 낫기에, 실패하기 시작한 소스는 패널에서 사라지는 대신 마지막으로
            # 반환한 것을 계속 내보낸다.
            if cached:
                logger.warning(
                    "트렌드 %s: 수집 실패(%s) - %.0f초 전 캐시로 대체합니다",
                    _source_log_label(source),
                    error,
                    cached.age_seconds,
                )
                return done("error-cache", cached.keywords)

            logger.warning("트렌드 %s: 수집 실패 - %s", _source_log_label(source), error)
            return done("error", [])
        finally:
            fetch_ms = (time.perf_counter() - fetch_start) * 1000
            self._record_fetch_ms(perf, source, fetch_ms)

        logger.info("트렌드 %s: 새로 수집 (%d개) - API 호출", _source_log_label(source), len(pool))
        if pool:
            # 저장 전 점수를 소스 내 40~100 상대 인기로 통일한다(점수 채점 방식 단일화).
            pool = _rescore_pool(pool)
            # 수집은 풀을 교체하지 않고 키운다: 새 수집분을 앞에, 기존 풀에서 겹치지
            # 않는 것을 뒤에 합친다. 교체하면 소스가 비슷한 목록을 돌려줄 때(트렌드는
            # 몇 분 사이 크게 안 바뀐다) 수집하기가 아무것도 더하지 못한다. 상한 없는
            # 수집분도 여기서 POOL_MERGE_CAP으로 잘려 저장이 무한정 커지지 않는다.
            pool = _merge_pools(pool, cached.keywords if cached else [])
            write_start = time.perf_counter()
            await self._cache.set(key, pool)
            perf.cache_write_ms += (time.perf_counter() - write_start) * 1000
        return done("success", pool)

    def _record_fetch_ms(
        self, perf: _TrendPerformance, source: TrendSource, fetch_ms: float
    ) -> None:
        if source == TrendSource.GOOGLE_TRENDS:
            perf.google_fetch_ms += fetch_ms
        elif source == TrendSource.NAVER_DATALAB:
            perf.naver_fetch_ms += fetch_ms
        elif source == TrendSource.YOUTUBE:
            perf.youtube_fetch_ms += fetch_ms
        elif source == TrendSource.INSTAGRAM:
            perf.instagram_fetch_ms += fetch_ms

    def _schedule_source_refresh(
        self,
        collector: TrendCollector,
        trend_input: TrendFetchInput,
        key: str,
        known: frozenset[str],
    ) -> None:
        if key in self._refreshing:
            return
        now = time.monotonic()
        if now - self._last_background_refresh.get(key, 0.0) < STALE_BACKGROUND_MIN_INTERVAL:
            return
        self._last_background_refresh[key] = now
        self._refreshing.add(key)
        asyncio.create_task(self._refresh_source(collector, trend_input, key, known))

    async def _refresh_source(
        self,
        collector: TrendCollector,
        trend_input: TrendFetchInput,
        key: str,
        known: frozenset[str],
    ) -> None:
        source = collector.source
        started = time.perf_counter()
        try:
            pool = await asyncio.wait_for(
                collector.collect(trend_input, None, known=known),
                timeout=SOURCE_TIMEOUTS.get(source, 3.0),
            )
            if pool:
                await self._cache.set(key, _rescore_pool(pool))
            logger.info(
                "트렌드 %s: 백그라운드 갱신 완료 (%d개, %.1fms)",
                source.value,
                len(pool),
                (time.perf_counter() - started) * 1000,
            )
        except Exception as error:
            logger.warning("트렌드 %s: 백그라운드 갱신 실패 - %s", source.value, error)
        finally:
            self._refreshing.discard(key)

    async def _merge(
        self,
        by_source: dict[TrendSource, list[CollectedKeyword]],
        limit: int,
        collected_at: str,
        trend_input: TrendFetchInput,
        perf: _TrendPerformance,
    ) -> list[TrendKeyword]:
        # _merge는 이제 최신순 전용이다. 소재 관련순은 _fetch_material이 소재 전용 저장소에서
        # 곧바로 화면 모델을 만들므로 여기 들어오지 않는다 — 두 모드가 한 함수 안에서 갈리던
        # 분기를 걷어냈다. 최신순은 소재 관련도 LLM 채점을 하지 않는다.
        normalize_start = time.perf_counter()
        entries: list[tuple[TrendSource, CollectedKeyword, float, KeywordSignature]] = []
        sources_by_cluster: dict[str, set[TrendSource]] = {}
        # 클러스터별 출처 근거. 같은 키워드가 여러 출처에서 확인되면 근거를 출처별로
        # 전부 보존한다 — 숫자를 더해 가상의 통합 검색량을 만들지 않는다. 같은 출처의
        # 근거가 여러 개면 관측이 더 최신인 쪽을 남긴다.
        evidence_by_cluster: dict[str, dict[str, TrendSourceEvidence]] = {}
        for source in SOURCE_ORDER:
            pool = by_source.get(source)
            if not pool:
                continue

            scores = _normalize(pool)
            for item in pool:
                signature = keyword_signature(item.keyword)
                if not signature.normalized:
                    continue
                sources_by_cluster.setdefault(signature.cluster_id, set()).add(source)
                if item.evidence is not None:
                    bucket = evidence_by_cluster.setdefault(signature.cluster_id, {})
                    current = bucket.get(source.value)
                    if current is None or _is_newer_observation(item.evidence, current):
                        bucket[source.value] = item.evidence
                entries.append((source, item, scores[item.keyword], signature))
        perf.normalize_ms += (time.perf_counter() - normalize_start) * 1000

        mode = trend_input.mode
        topic_tokens = _subject_echo_tokens(trend_input)
        subject_compact = _compact_keyword(trend_input.input.topic)

        def entry_score(
            entry: tuple[TrendSource, CollectedKeyword, float, KeywordSignature]
        ) -> tuple[float, float]:
            source, _item, hotness, signature = entry
            source_count = len(sources_by_cluster.get(signature.cluster_id, {source}))
            final = _mode_trend_score(mode, hotness, None, source_count)
            # 합성 점수가 같으면 실제 트렌드 강도(hotness)가 큰 후보를 먼저 둔다.
            return (final, hotness)

        ranked_entries = sorted(entries, key=lambda entry: tuple(-v for v in entry_score(entry)))
        window_size = min(len(ranked_entries), ROTATION_WINDOW)
        # 최신순은 상위 창 안에서 회전해 새로고침마다 다른 얼굴을 보여준다.
        offset = self._rotate(window_size) if window_size else 0
        head = ranked_entries[:window_size]
        ordered_entries = head[offset:] + head[:offset] + ranked_entries[window_size:]

        excluded_norms, excluded_token_sets, excluded_clusters = await self._exclusion_sets(
            trend_input
        )
        # 최신순 '다른 후보 보기'(shuffle): 노출 이력·exclude로 거르지 않는다 — 이력 제외
        # 방식은 풀을 한 바퀴 돌면 남는 후보가 말라붙어 버튼이 죽는다. 중복 노출을 허용하고
        # 매번 풀 전체에서 무작위로 뽑는다(아래 선택 단계).
        ignore_history = trend_input.shuffle and mode == TrendMode.TRENDING

        filtered: list[tuple[TrendSource, CollectedKeyword, float, KeywordSignature]] = []
        blocked_by_history: list[tuple[TrendSource, CollectedKeyword, float, KeywordSignature]] = []
        seen_norms: set[str] = set()
        for entry in ordered_entries:
            source, item, _hotness, signature = entry
            if signature.normalized in seen_norms:
                continue
            if is_low_quality_keyword(item.keyword) or _is_sentence_fragment(item.keyword):
                continue
            # 소재-메아리(AI AIONA, 생성형 AI …)와 기계적 조합(빵추천, 국내여행추천)은 두 모드 모두에서
            # 제외한다(§7·§9·§17): 추천어에서는 트렌드가 아니고, 소재 관련어에서는 소재의 재배열일 뿐이다.
            if _is_subject_echo(signature, topic_tokens) or _is_mechanical_echo(
                item.keyword, subject_compact
            ):
                continue
            if not ignore_history and self._is_excluded(
                signature, excluded_norms, excluded_token_sets, excluded_clusters
            ):
                perf.history_excluded += 1
                blocked_by_history.append(entry)
                continue
            seen_norms.add(signature.normalized)
            # 최신순: 소재별 LLM 판정 없이 지금 실제로 뜨는 트렌드를 노출한다.
            # 소재 메아리·저품질은 위의 결정적 코드 필터가 이미 제거했다.
            filtered.append(entry)
        # 미노출 후보가 패널을 채우기에 모자라면 — 새로고침으로 풀을 한 바퀴 다 본 상태 —
        # 노출 이력을 비우고 순환을 다시 시작한다. 클라이언트가 명시적으로 exclude를 보낸
        # 경우는 예외다: 방금 본 화면을 그대로 다시 내주지 않기 위한 요청이라, 여기서
        # 되살리면 그 뜻을 뒤집는다. (소재 관련순의 순환 봉쇄 문제는 이 경로가 아니라
        # 커서 로테이션이 해결한다 — _material_window 참고.)
        if (
            len(filtered) < limit
            and blocked_by_history
            and not trend_input.exclude_keywords
        ):
            await self._exposure.clear(self._history_key(trend_input))
            filtered.extend(blocked_by_history[: limit * 2])

        perf.valid_candidates = len(filtered)

        cluster_start = time.perf_counter()
        clustered: list[tuple[TrendSource, CollectedKeyword, float, KeywordSignature]] = []
        for entry in filtered:
            existing_index = next(
                (
                    index
                    for index, candidate in enumerate(clustered)
                    if are_similar(entry[3], candidate[3])
                ),
                None,
            )
            if existing_index is None:
                clustered.append(entry)
                continue

            current = clustered[existing_index]
            if (
                entry_score(entry) > entry_score(current)
                or (
                    entry_score(entry) == entry_score(current)
                    and naturalness_score(entry[1].keyword, entry[3])
                    > naturalness_score(current[1].keyword, current[3])
                )
            ):
                clustered[existing_index] = entry
        perf.cluster_ms += (time.perf_counter() - cluster_start) * 1000
        perf.unique_clusters = len({entry[3].cluster_id for entry in clustered})

        select_start = time.perf_counter()

        def final_score(entry: tuple[TrendSource, CollectedKeyword, float, KeywordSignature]) -> float:
            source, _item, hotness, signature = entry
            return _mode_trend_score(
                mode,
                hotness,
                None,
                len(sources_by_cluster.get(signature.cluster_id, {source})),
            )

        picked: list[tuple[TrendSource, CollectedKeyword, float, KeywordSignature]] = []
        remaining = list(clustered)
        source_counts: Counter[TrendSource] = Counter()
        # 한 소스가 패널을 독점하지 못하게 하는 상한.
        #
        # 예전에는 표시 개수의 3/5(16개면 9개)여서, 한 소스가 절반 넘게 차지해도 통과했다.
        # 이제는 **실제로 후보를 낸 소스 수로 나눈 몫**이다 — 세 소스가 후보를 냈으면
        # 16개를 6/5/5로 나눠 담는다. 후보가 모자란 소스가 있으면 아래 마지막 채움이
        # 남는 자리를 메우므로, 균형을 맞추다가 화면이 비는 일은 없다.
        contributing = len({entry[0] for entry in clustered}) or 1
        source_cap = max(1, ceil(limit / contributing))

        if ignore_history:
            # shuffle('다른 키워드 보기'): 후보 전체가 아니라 '지금 진짜 뜨는' 인기순 상위
            # 구간에서만 무작위 표본을 뽑는다(2026-07-22 결정 — 전체 풀 무작위는 한물간 하위
            # 후보까지 섞였다). 창 = 상위 20%(TRENDING_SHUFFLE_TOP_FRACTION)이되, 표시 개수의
            # 2배보다 좁으면 누를 때마다 같은 얼굴이라 무작위의 의미가 없으므로 그 이상을
            # 보장한다. 표시만 인기순으로 정렬해 최신순 뷰의 의미(뜨거운 순서)는 유지한다.
            # MMR·분야 상한은 건너뛴다: 표본 자체가 무작위라 다양성을 따로 강제할 필요가 없다.
            #
            # **표본은 근거 있는 후보 안에서 뽑는다.** 아래 선택 단계는 설명할 수 있는 후보를
            # 먼저 채우는데(explained/unexplained), 이 무작위 경로는 그 단계를 통째로
            # 건너뛰어 저장 풀에 남은 옛 후보(근거가 붙기 전 수집분)를 그대로 집어 왔다 —
            # 첫 진입 화면은 멀쩡한데 '다른 키워드 보기'나 '새 키워드 찾기'를 누르면 16칸 중
            # 13칸이 "상세 지표는 새 수집 후 표시됩니다"였던 실측이 이 자리다('새 키워드
            # 찾기'도 수집 뒤에 같은 shuffle 요청을 한 번 더 보낸다).
            #
            # 근거 있는 후보만으로 화면을 못 채울 때는 나머지를 함께 본다 — 설명이 덜 붙은
            # 카드라도 보여주는 편이 빈자리보다 낫다는 선택 단계의 원칙과 같다.
            explained_pool = [entry for entry in clustered if entry[1].evidence is not None]
            shuffle_pool = explained_pool if len(explained_pool) >= limit else clustered
            by_heat = sorted(shuffle_pool, key=lambda entry: tuple(-v for v in entry_score(entry)))
            window = max(limit * 2, ceil(len(by_heat) * TRENDING_SHUFFLE_TOP_FRACTION))
            hot_slice = by_heat[:window]
            chosen = self._sample(hot_slice, min(limit, len(hot_slice))) if hot_slice else []
            picked = sorted(chosen, key=lambda entry: tuple(-v for v in entry_score(entry)))
            remaining = []

        def fill_from(pool: list) -> None:
            """MMR로 pool에서 골라 담는다(출처 상한·유사 중복 회피는 그대로)."""
            while pool and len(picked) < limit:
                best_index = -1
                best_score = float("-inf")
                for index, entry in enumerate(pool):
                    if source_counts[entry[0]] >= source_cap:
                        continue
                    if any(are_similar(entry[3], chosen[3]) for chosen in picked):
                        continue
                    max_similarity = max(
                        (jaccard_similarity(entry[3].tokens, chosen[3].tokens) for chosen in picked),
                        default=0.0,
                    )
                    order_bonus = max(0.0, (ROTATION_WINDOW - min(index, ROTATION_WINDOW)) * 12.0)
                    mmr_score = final_score(entry) * 0.75 + order_bonus - max_similarity * 25.0
                    if mmr_score > best_score:
                        best_score = mmr_score
                        best_index = index
                if best_index < 0:
                    return
                chosen = pool.pop(best_index)
                picked.append(chosen)
                source_counts[chosen[0]] += 1

        # 근거(검색량·조회수·문서 수)가 있는 후보를 먼저 채운다.
        #
        # 화면은 카드마다 "왜 이 키워드인지"를 세 줄로 설명하기로 했는데, 저장 풀에는 근거가
        # 붙기 전에 수집된 옛 후보가 함께 산다(측정: trend_keywords 390건 중 173건이 근거
        # 없음). 점수만으로 고르면 그 옛 후보들이 자리를 차지해 "상세 지표는 새 수집 후
        # 표시됩니다" 카드가 화면을 덮었다. 설명할 수 있는 후보가 있는데 설명할 수 없는
        # 후보를 앞세울 이유가 없다.
        #
        # 근거 없는 후보를 **버리지는 않는다** — 근거 있는 것만으로 화면을 못 채우면 뒤이어
        # 채운다. 적게 보여주는 것보다 설명이 덜 붙은 카드라도 보여주는 편이 낫다.
        explained = [entry for entry in remaining if entry[1].evidence is not None]
        unexplained = [entry for entry in remaining if entry[1].evidence is None]
        fill_from(explained)
        fill_from(unexplained)
        remaining = explained + unexplained

        if len(picked) < limit:
            for entry in remaining:
                if any(are_similar(entry[3], chosen[3]) for chosen in picked):
                    continue
                picked.append(entry)
                if len(picked) >= limit:
                    break

        perf.select_ms += (time.perf_counter() - select_start) * 1000

        per_source: Counter[TrendSource] = Counter()
        keywords: list[TrendKeyword] = []
        for index, (source, item, score, signature) in enumerate(picked):
            per_source[source] += 1
            sources = sorted(
                sources_by_cluster.get(signature.cluster_id, {source}),
                key=lambda trend_source: SOURCE_ORDER.index(trend_source)
                if trend_source in SOURCE_ORDER
                else len(SOURCE_ORDER),
            )
            trend_score = _mode_trend_score(mode, score, None, len(sources))
            keywords.append(
                TrendKeyword(
                    trend_keyword_id=f"trend_{source.value.lower()}_{per_source[source]}",
                    keyword=item.keyword.strip(),
                    normalized_keyword=signature.normalized,
                    tokens=list(signature.tokens),
                    token_set_signature=signature.token_set_signature,
                    cluster_id=signature.cluster_id,
                    source=source,
                    sources=sources,
                    rank=index + 1,
                    score=trend_score,
                    trend_score=trend_score,
                    # 정규화된 실시간 상승도(점수식의 hotness 항). 화면의 "최신순"(지금 가장
                    # 뜨거운 순)은 합성 점수가 아니라 이 값으로 정렬한다.
                    hotness=round(score, 1),
                    quality_score=100.0,
                    final_score=trend_score,
                    trend_reason=_trend_reason(len(sources), None),
                    connection_idea=None,
                    period="최근 30일 및 실시간 상승",
                    relevance=None,
                    subject_relevance=None,
                    purpose_relevance=None,
                    persona_relevance=None,
                    is_eligible=None,
                    category=None,
                    # 대표 출처가 카드의 3줄 지표를 그리고, 보조 출처 근거도 함께 실린다.
                    # 근거가 없으면(옛 캐시) None — 화면이 중립 문구로 대신한다.
                    evidence_by_source=evidence_by_cluster.get(signature.cluster_id),
                    collected_at=collected_at,
                )
            )
        return keywords

    def _history_key(self, trend_input: TrendFetchInput) -> str:
        # 추천어와 소재 관련어의 노출 이력을 분리한다(§14): 한쪽 탭에서 새로고침해도 다른 탭이
        # 방금 보여준 키워드까지 제외해 버리면, 탭을 여는 순간 후보가 부족해진다.
        return f"{trend_input.user_id}:{trend_input.post_id}:{trend_input.mode.value}"

    async def _exclusion_sets(
        self, trend_input: TrendFetchInput
    ) -> tuple[set[str], set[str], set[str]]:
        """이번 요청에서 제외할 키워드 지문. 클라이언트가 보낸 exclude와 저장된 노출 이력."""
        exposure = await self._exposure.sets(self._history_key(trend_input))

        for keyword in trend_input.exclude_keywords:
            exposure.add(_exposure_signature(keyword))

        return exposure.normalized, exposure.token_sets, exposure.clusters

    def _is_excluded(
        self,
        signature: KeywordSignature,
        normalized: set[str],
        token_sets: set[str],
        clusters: set[str],
    ) -> bool:
        return (
            signature.normalized in normalized
            or bool(signature.token_set_signature and signature.token_set_signature in token_sets)
            or signature.cluster_id in clusters
        )

    async def _remember_history(
        self, trend_input: TrendFetchInput, keywords: Sequence[TrendKeyword]
    ) -> None:
        """방금 화면에 내보낸 키워드를 노출 이력에 남긴다.

        중복 제거·개수 상한·만료는 저장소가 원자적으로 처리한다 — 읽고 나서 쓰는 사이에
        다른 요청이 끼어들어 같은 키워드가 두 번 기록되던 여지를 없앤다.
        """
        await self._exposure.remember(
            self._history_key(trend_input),
            [_exposure_signature(keyword.keyword) for keyword in keywords],
        )
