"""네이버 검색 API — 최근 블로그/뉴스/카페/지식iN 글에서 키워드를 캐낸다.

네이버에는 트렌드 키워드 엔드포인트가 없다. 대신 한국인이 최근 며칠간 게시한 것에 대한
검색 API가 있어, 거기서 후보를 발굴하고 언급된 문서 수로 순위를 매긴다.

DataLab은 더 이상 부르지 않는다. 예전에는 상위 다섯 개를 DataLab의 상대 검색량으로 다시
정렬했는데, 이 프로젝트가 쓰기로 한 외부 API는 SerpApi Google Trends·네이버 검색·YouTube
Data 셋뿐이다(2026-07-29 결정). 다섯 개의 순서를 바꾸자고 요청 하나를 더 쓰는 값어치도
없었다 — 문서 언급 수는 이미 그 자체로 관심도 신호다.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Sequence

import httpx

from app.llm.contracts import TrendFetchInput
from app.llm.naver_api import SEARCH_URL_TEMPLATE, auth_headers
from app.shared import (
    NaverEvidenceBasis,
    NaverTrendEvidence,
    TrendEvidenceOrigin,
    TrendMode,
    TrendSource,
    TrendSourceEvidence,
)

from .base import REQUEST_TIMEOUT, CollectedKeyword, seed_queries
from .text import (
    compact_for_match,
    concrete_phrases,
    count_keywords,
    match_terms,
    material_phrases,
    mentions_keyword,
    to_collected,
)

logger = logging.getLogger(__name__)

# NAVER API HUB 이관(2026-08-11). 주소·헤더 규격은 app.llm.naver_api 한 곳에서 만든다.
NAVER_SEARCH_URL = SEARCH_URL_TEMPLATE

# '최근 24시간 확인 뉴스'의 집계 창. 뉴스 pubDate만 시각 단위라 이 필터를 걸 수 있다
# (블로그 postdate는 날짜 단위라 24시간 수치로 표현하지 않는다).
RECENT_NEWS_WINDOW = timedelta(hours=24)

# 발굴 경로의 기준값. '이번 검색 API 수집 표본에서 확인된 문서 수'라는 뜻이며,
# 네이버 전체 검색량·전체 게시물 수가 아니다.
NAVER_EVIDENCE_BASIS = NaverEvidenceBasis.SEARCH_API_SAMPLE

# 보강(measure) 경로가 키워드마다 읽어 오는 문서 수. 최근 24시간 글을 세는 데만 쓰므로
# 크게 둘 이유가 없다 — 총수는 응답의 total이 알려 준다.
MEASURE_DISPLAY = 50

# 보강 요청의 동시 실행 수. 키워드 하나에 뉴스·블로그 두 요청이 나가므로, 키워드 15개면
# 30개다. 발굴과 같은 이유로 한꺼번에 던지지 않는다(429).
MAX_CONCURRENT_MEASURES = 6

# 429(초당 호출 한도)를 만났을 때 물러났다 다시 던지기까지의 대기(초). 트렌드 수집이
# 호출 예산을 막 소진한 직후에 보강을 재기 시작하면 첫 응답이 통째로 429였다(실측
# 0/10개 측정). 한도는 초 단위로 풀리므로 짧게 두 번 물러나면 대부분 살아난다.
MEASURE_429_BACKOFF_SECONDS = (0.7, 1.5)

# 근거를 계산할 상위 키워드 수. 한 번의 수집이 수천 개의 후보를 캐낼 수 있는데, 저장 병합이
# 어차피 상위 POOL_MERGE_CAP(200)개만 남기므로 그 이상은 계산해도 버려진다. 문서 500개와
# 대조하는 작업이라 상한이 없으면 수백만 번의 비교가 된다.
EVIDENCE_KEYWORD_LIMIT = 200

# 키워드를 캐낼 검색 종류. 각각 성격이 달라 함께 쓰면 발굴 폭이 넓어진다:
#   - blog        : 블로그 글 — 후기·정보성 글에서 자리잡은 주제어
#   - news        : 뉴스 — 시의성 있는 사건·이슈 키워드
#   - cafearticle : 카페글 — 맘카페·취미카페 등 생활 밀착 화제(blog/news가 놓치는 것)
#   - kin         : 지식iN 질문 — 사람들이 "지금 뭘 묻는지", 곧 실시간 관심사
# shop(쇼핑)은 네이버 개발자센터 종료(2026-08-01)로 뺐다. book·학술정보도 같은 시점 종료라 쓰지 않는다.
SEARCH_KINDS = ("blog", "news", "cafearticle", "kin")
DISPLAY = 50
DISCOVERY_QUERY_LIMIT = 8

# 동시에 열어 두는 네이버 요청 수. 시드 8개 × 검색 4종 = 32개를 한꺼번에 던지면 네이버가
# 일부를 429(Too Many Requests)로 돌려보낸다 — 실측에서 16개 중 6개가 그랬고, 그만큼의
# 문서가 그대로 사라져 후보가 줄었다. 8개씩 나눠 보내도 네 묶음이면 끝나므로(요청당
# 200~300ms) 벌이는 시간보다 잃는 문서가 크다.
MAX_CONCURRENT_REQUESTS = 8


def _seasonal_queries(today=None) -> list[str]:
    """출력 키워드가 아니라 발굴용 질의.

    네이버에는 "실시간 트렌드" 키워드 피드가 없어, 사용자의 토픽만 검색하면 일반적인
    주제어만 캐낸다. 이 넓고 날짜를 반영한 질의들은 계절 행사, 스포츠, 출시, 쇼핑에
    관한 최근 한국 글을 끌어온다; 실제 키워드는 여전히 API 결과에서만 나온다.
    """
    today = today or datetime.now(timezone.utc).date()
    month = today.month
    queries = [
        f"{month}월 행사",
        f"{month}월 축제",
        "개봉 영화",
        "팝업스토어",
        "프로야구",
        "신작 게임",
    ]
    if month in (6, 7, 8):
        queries = [
            "여름 페스티벌",
            "여름 공연",
            "서울 여름 행사",
            "여름휴가",
            "장마 폭염",
            *queries,
        ]
    elif month in (9, 10, 11):
        queries = [
            "가을 축제",
            "단풍 여행",
            "추석 행사",
            *queries,
        ]
    elif month in (12, 1, 2):
        queries = [
            "겨울 축제",
            "연말 공연",
            "크리스마스 행사",
            "스키장",
            *queries,
        ]
    else:
        queries = [
            "봄 축제",
            "벚꽃 행사",
            "봄 나들이",
            *queries,
        ]

    return _unique(queries)[:DISCOVERY_QUERY_LIMIT]


# 글 목적별 검색 의도 축. 소재 관련순의 발굴 질의를 만들 때 쓴다.
#
# 왜 목적별인가: 스펙은 "모든 소재에 같은 접미사를 기계적으로 붙이지 말고 소재 종류에 맞는
# 검색 의도를 쓰라"고 한다. 그런데 '소재 종류'를 코드가 확실히 아는 방법은 없다 — 사용자가
# 카테고리를 고르지 않기 때문이다. 대신 **사용자가 실제로 알려 준 것**을 쓴다: 글 목적과
# 사용자가 직접 적은 키워드다. '사용법·가이드'로 쓰는 글과 '후기·리뷰'로 쓰는 글은 같은
# 소재라도 사람들이 찾는 것이 다르다.
#
# 그리고 이 축들은 **출력 키워드가 아니라 발굴 질의**다. 최종 키워드는 검색 결과 문서의
# 제목·설명에서 캐내므로("배틀그라운드 사용법"으로 검색해 "배틀그라운드 감도 설정"을 얻는다),
# 축이 기계적이어도 결과가 기계적이 되지는 않는다. 소재명+접미사가 그대로 남는 조합은
# aggregate의 기계적 메아리 필터가 따로 제거한다.
_PURPOSE_QUERY_AXES: dict[str, tuple[str, ...]] = {
    "입문·소개": ("소개", "기능", "처음"),
    "사용법·가이드": ("사용법", "설정", "초보"),
    "후기·리뷰 작성": ("후기", "장단점", "실사용"),
    "비교·추천": ("비교", "차이", "종류"),
    "문제 해결": ("오류", "문제", "해결"),
    "정보 전달": ("정리", "업데이트", "종류"),
    "일상·경험 공유": ("후기", "일상"),
    "트렌드·이슈 소개": ("업데이트", "근황", "논란"),
    "제품·서비스 홍보": ("가격", "기능", "출시"),
}

# 목적을 고르지 않았거나 목록에 없는 목적일 때의 축. 어느 소재에나 검색 수요가 있는
# 최소한만 둔다 — 여기서 폭을 넓히면 소재와 먼 문서까지 끌어와 발굴이 흐려진다.
_DEFAULT_QUERY_AXES = ("업데이트", "후기", "종류")

# 보충 수집(widen_material)의 축. 첫 수집으로 화면을 채우지 못했을 때만 쓴다.
#
# 목적 축과 겹치지 않는 다른 검색 의도를 골라, 같은 소재라도 **다른 문서**를 끌어오게 한다.
# 이것들도 발굴 질의일 뿐 후보가 아니다 — '콜롬비아 정보'로 검색해 '보고타 치안'을 얻는
# 것이 목적이고, '콜롬비아 정보'라는 조합 자체를 후보로 쓰지는 않는다. 소재 앞에 일반어를
# 임의로 붙여 만든 키워드는 aggregate의 기계적 메아리 필터가 따로 제거한다.
_TOPUP_QUERY_AXES = ("정보", "가격", "일정", "특징", "방법", "주의사항", "종류")


@dataclass
class NaverDocument:
    """이번 수집이 읽은 문서 하나. 예전에는 제목+설명 문자열만 남기고 종류·링크·날짜를
    버렸는데, 그 정보가 있어야 '키워드가 어떤 문서에서 확인됐는지'를 셀 수 있다."""

    kind: str  # blog | news | cafearticle | kin
    title: str
    description: str
    link: str
    published_at: datetime | None
    # 키워드 추출에 넘기는 결합 문자열(제목+설명). 추출기가 HTML 태그를 알아서 걷어낸다.
    text: str
    # 같은 문자열을 공백·기호 없이 붙여 놓은 대조용 표기. 키워드가 이 문서에 등장했는지를
    # 조사·띄어쓰기에 걸리지 않고 판정한다(text.mentions_keyword).
    compact: str


def _parse_document_date(kind: str, item: dict) -> datetime | None:
    """검색 결과의 게시 시각. 못 읽으면 만들지 않고 None.

    - news: pubDate가 RFC 2822("Tue, 05 Aug 2026 09:00:00 +0900") — 시각 단위.
    - blog·cafearticle: postdate가 "YYYYMMDD" — 날짜 단위(KST 자정으로 둔다).
    - kin: 날짜 필드가 없다.
    """
    if kind == "news":
        raw = item.get("pubDate")
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    raw = item.get("postdate")
    if not isinstance(raw, str) or len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return datetime(
            int(raw[0:4]), int(raw[4:6]), int(raw[6:8]),
            tzinfo=timezone(timedelta(hours=9)),
        )
    except ValueError:
        return None


def _unique(values) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        text = (value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        unique.append(text)
    return unique


def material_queries(trend_input) -> list[str]:
    """소재 관련순의 발굴 질의. 계절·일반 질의는 넣지 않는다.

    순서가 중요하다 — 앞의 질의일수록 많은 문서를 가져오므로, 소재 자체와 사용자가 직접
    적은 키워드를 앞에 두고 목적 기반 축을 뒤에 붙인다. 사용자가 적은 키워드는 이미 소재
    종류를 반영한 신호라 어떤 축보다 정확하다.
    """
    blog_input = trend_input.input
    topic = (blog_input.topic or "").strip()
    if not topic:
        return []

    # 보충 회차는 목적 축 대신 넓은 축을 쓴다. 같은 축으로 다시 검색하면 첫 수집과 같은
    # 문서가 돌아와 후보가 하나도 늘지 않는다.
    if getattr(trend_input, "widen_material", False):
        axes = list(_TOPUP_QUERY_AXES)
    else:
        axes = []
        for purpose in blog_input.purpose or []:
            axes.extend(_PURPOSE_QUERY_AXES.get(purpose, ()))
        if not axes:
            axes = list(_DEFAULT_QUERY_AXES)

    return _unique(
        [
            topic,
            *(blog_input.keywords or []),
            *(f"{topic} {axis}" for axis in axes),
        ]
    )[:DISCOVERY_QUERY_LIMIT]


class NaverTrendCollector:
    source = TrendSource.NAVER_DATALAB

    def __init__(self, client_id: str, client_secret: str):
        self._headers = auth_headers(client_id, client_secret)

    async def collect(
        self,
        trend_input: TrendFetchInput,
        limit: int | None,
        known: frozenset[str] = frozenset(),
    ) -> list[CollectedKeyword]:
        user_seeds = seed_queries(trend_input)
        # 추천어(TRENDING)는 소재와 무관한 '지금 뜨는 것'이라, 소재 단어가 아니라 계절·일반
        # 질의로만 발굴한다 — 그래야 저장되는 풀이 특정 소재에 편향되지 않고 실시간 인기
        # 키워드 전반이 된다. 소재 관련어는 계절 질의를 **전혀 쓰지 않는다**: 예전에는 소재
        # 시드 두 개에 계절 질의 여섯 개를 섞어, 발굴된 문서 대부분이 소재와 무관한 계절
        # 글이었고 그래서 소재 관련 후보가 늘 몇 개뿐이었다.
        if trend_input.mode == TrendMode.MATERIAL_RELATED:
            seeds = material_queries(trend_input)
        else:
            seeds = _seasonal_queries()
        if not seeds:
            return []

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT, headers=self._headers
        ) as client:
            documents = await self._recent_documents(client, seeds)
            if not documents:
                return []

            # 모드마다 추출기가 다르다. 최신순은 무엇이 트렌드인지 코드가 알 방법이
            # 없어 알려진 이슈어(CONCRETE_ANCHORS)를 기준으로 삼는 concrete_phrases가
            # 맞다. 소재 관련순에서는 그 목록이 재앙이다 — 소재가 목록에 없으면 캐낸
            # 구절이 하나도 통과하지 못해 후보가 늘 1~2개로 끝났다. 소재 관련성 판단은
            # 관련도 채점(LLM)에 맡기고 여기서는 형태·품질만 본다.
            extractor = (
                material_phrases
                if trend_input.mode == TrendMode.MATERIAL_RELATED
                else concrete_phrases
            )

            # 구절 추출은 순수 CPU 작업이고 문서 500개에서 5,000개 넘는 구절을 만든다
            # (실측 0.6~2.5초). 이벤트 루프 안에서 돌리면 그 시간 동안 **다른 소스의
            # 타임아웃 타이머까지 멈춘다** — 유튜브가 3초 예산인데 6.1초에 취소되며
            # 기여가 0개가 된 실측 사례가 있다. 스레드로 내보내면 각 소스의 예산이
            # 제 뜻대로 동작한다.
            counts = await asyncio.to_thread(
                count_keywords,
                [doc.text for doc in documents],
                exclude=user_seeds,
                extractor=extractor,
                min_documents=1,
            )
            # 순서는 문서 언급 수 그대로다(to_collected). DataLab 재정렬은 제거했다 —
            # 모듈 docstring 참고.
            collected = to_collected(counts, limit, known)

            # 각 키워드가 실제로 등장한 문서를 센다. 판정은 추출 결과가 아니라 본문 대조다 —
            # 추출기는 '폭염이 계속되면서'처럼 조사가 붙은 표기를 통째로 버리므로, 그것으로
            # 세면 문서가 분명히 말한 키워드를 놓친다(mentions_keyword docstring 참고).
            observed_at = datetime.now(timezone.utc)
            await asyncio.to_thread(
                self._attach_evidence, collected[:EVIDENCE_KEYWORD_LIMIT], documents, observed_at
            )
            return collected

    def _attach_evidence(
        self,
        collected: list[CollectedKeyword],
        documents: list[NaverDocument],
        observed_at: datetime,
    ) -> None:
        """상위 키워드에 근거를 붙인다. 문서 수백 개와 대조하는 CPU 작업이라 스레드에서 돈다."""
        for item in collected:
            terms = match_terms(item.keyword)
            found = [doc for doc in documents if mentions_keyword(terms, doc.compact)]
            item.evidence = self._evidence(found, len(documents), observed_at)

    def _evidence(
        self, found: list[NaverDocument], sampled_count: int, observed_at: datetime
    ) -> TrendSourceEvidence | None:
        """키워드가 등장한 문서들에서 근거 수치를 계산한다.

        전부 '이번 API 수집 표본에서 실제 확인한 고유 문서 수'다 — 네이버 전체 검색량·
        전체 게시물 수가 아니며, 화면 문구와 basis 값이 그 사실을 밝힌다. 뉴스만
        pubDate가 시각 단위라 24시간 필터를 걸고, 블로그 postdate는 날짜 단위라
        '이번 수집 확인' 수치로만 센다.
        """
        if not found:
            return None

        def unique_count(docs: list[NaverDocument]) -> int:
            seen: set[str] = set()
            count = 0
            for doc in docs:
                key = doc.link or f"__no_link_{id(doc)}"
                if key in seen:
                    continue
                seen.add(key)
                count += 1
            return count

        news_cutoff = observed_at - RECENT_NEWS_WINDOW
        recent_news = [
            doc
            for doc in found
            if doc.kind == "news" and doc.published_at and doc.published_at >= news_cutoff
        ]
        blogs = [doc for doc in found if doc.kind == "blog"]

        return TrendSourceEvidence(
            source=TrendSource.NAVER_DATALAB,
            observed_at=observed_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            data_origin=TrendEvidenceOrigin.NAVER_SEARCH_API,
            naver=NaverTrendEvidence(
                recent_news_count=unique_count(recent_news),
                collected_blog_count=unique_count(blogs),
                collected_related_content_count=unique_count(found),
                sampled_document_count=sampled_count,
                basis=NAVER_EVIDENCE_BASIS,
            ),
        )

    async def measure_keywords(
        self, keywords: Sequence[str]
    ) -> dict[str, TrendSourceEvidence]:
        """키워드 **자체**를 네이버에 물어 수치를 잰다(보강 경로).

        발굴 경로(collect)와 재는 것이 다르다. 저쪽은 계절·소재 질의로 모아 온 표본 안에서
        그 키워드가 몇 번 보였는지를 세므로, 표본에 없으면 0이고 표본 크기에 갇힌다. 여기서는
        키워드를 질의로 넣어 **네이버가 세어 준 검색 결과 총수**를 받으므로 키워드끼리 비교가
        되고, 표본 상한이 없다. 그래서 basis를 달리 저장하고 화면 문구도 달라진다.

        구글 자동완성이 내놓은 소재 연관 키워드처럼 **수치가 없는 후보**에 근거를 붙이는
        자리다. 결과가 소재 풀에 저장되므로 같은 키워드를 화면을 열 때마다 다시 재지는
        않는다 — 수집 직후와, 근거 없이 저장돼 있던 풀의 첫 노출 때만 불린다.
        """
        if not keywords:
            return {}

        gate = asyncio.Semaphore(MAX_CONCURRENT_MEASURES)
        observed_at = datetime.now(timezone.utc)

        async def count(client: httpx.AsyncClient, kind: str, keyword: str):
            async with gate:
                # 429면 슬롯을 쥔 채 잠깐 쉬었다 다시 던진다 — 쥐고 자는 동안 전체 요청
                # 속도도 함께 낮아져 폭주가 스스로 잦아든다. 마지막 시도(delay=None)의
                # 429는 그대로 아래 오류 처리로 흘러가 해당 키워드만 포기한다.
                for delay in (*MEASURE_429_BACKOFF_SECONDS, None):
                    response = await client.get(
                        NAVER_SEARCH_URL.format(kind=kind),
                        params={
                            "query": keyword,
                            "display": str(MEASURE_DISPLAY),
                            "sort": "date",
                        },
                    )
                    if response.status_code != 429 or delay is None:
                        break
                    await asyncio.sleep(delay)
            if response.is_error:
                raise RuntimeError(f"Naver {kind} measure failed with {response.status_code}")
            payload = response.json() if response.text else None
            if not isinstance(payload, dict):
                return 0, []
            items = payload.get("items")
            items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
            total = payload.get("total")
            return (int(total) if isinstance(total, int) else 0), items

        async def measure(client: httpx.AsyncClient, keyword: str):
            news_total, news_items = await count(client, "news", keyword)
            blog_total, _ = await count(client, "blog", keyword)
            cutoff = observed_at - RECENT_NEWS_WINDOW
            recent = [
                item
                for item in news_items
                if (_parse_document_date("news", item) or observed_at - timedelta(days=365))
                >= cutoff
            ]
            return keyword, TrendSourceEvidence(
                source=TrendSource.NAVER_DATALAB,
                observed_at=observed_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                data_origin=TrendEvidenceOrigin.NAVER_SEARCH_API,
                naver=NaverTrendEvidence(
                    total_news_count=news_total,
                    total_blog_count=blog_total,
                    recent_document_count=len(recent),
                    # 날짜순 응답을 세는 것이라 표본 상한에 걸릴 수 있다. 그때는 화면이
                    # "50건+"라고 적어야 한다 — 정확히 50건이라고 말하면 거짓이다.
                    recent_hit_cap=len(recent) >= MEASURE_DISPLAY,
                    basis=NaverEvidenceBasis.SEARCH_API_TOTAL,
                ),
            )

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT, headers=self._headers
        ) as client:
            results = await asyncio.gather(
                *(measure(client, keyword) for keyword in keywords),
                return_exceptions=True,
            )

        measured: dict[str, TrendSourceEvidence] = {}
        for result in results:
            if isinstance(result, BaseException):
                # 한 키워드를 못 재도 나머지는 쓴다 — 근거가 없으면 화면이 중립 문구로 대신한다.
                logger.warning("네이버 보강: 키워드 측정 실패 - %s", result)
                continue
            keyword, evidence = result
            measured[keyword] = evidence
        logger.info("네이버 보강: %d/%d개 키워드 측정", len(measured), len(keywords))
        return measured

    async def _recent_documents(
        self, client: httpx.AsyncClient, seeds: list[str]
    ) -> list[NaverDocument]:
        """시드별로 네이버가 가장 최근 게시한 것의 제목·스니펫·종류·링크·날짜."""

        gate = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async def fetch(kind: str, seed: str) -> list[NaverDocument]:
            async with gate:
                response = await client.get(
                    NAVER_SEARCH_URL.format(kind=kind),
                    params={"query": seed, "display": str(DISPLAY), "sort": "date"},
                )
            if response.status_code in (401, 403):
                # 자격 증명은 정상이지만 네이버 애플리케이션에 이 API 권한이 부여되지
                # 않았다. 그렇다고 알린다: 그러지 않으면 증상은 원인을 가리키는 것 없이
                # 그저 "네이버 키워드가 절대 안 나온다"일 뿐이다.
                raise RuntimeError(
                    f"Naver 검색 API 인증 실패 ({response.status_code}: {response.text}). "
                    f"NAVER API HUB 콘솔에서 발급한 Client ID/Secret인지, 그 Application에 "
                    f"{kind} 검색이 선택돼 있는지 확인해야 한다 — 옛 개발자센터 키는 "
                    f"이 주소에서 동작하지 않는다."
                )
            if response.is_error:
                raise RuntimeError(
                    f"Naver {kind} search failed with {response.status_code}: {response.text}"
                )

            payload = response.json() if response.text else None
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                return []

            documents: list[NaverDocument] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                title = item.get("title")
                title = title if isinstance(title, str) else ""
                description = item.get("description")
                description = description if isinstance(description, str) else ""
                link = item.get("link")
                link = link if isinstance(link, str) else ""
                text = " ".join(part for part in (title, description) if part)
                documents.append(
                    NaverDocument(
                        kind=kind,
                        title=title,
                        description=description,
                        link=link,
                        published_at=_parse_document_date(kind, item),
                        text=text,
                        compact=compact_for_match(text),
                    )
                )
            return documents

        results = await asyncio.gather(
            *(fetch(kind, seed) for kind in SEARCH_KINDS for seed in seeds),
            return_exceptions=True,
        )

        documents: list[NaverDocument] = []
        seen_links: set[str] = set()
        failures: list[BaseException] = []
        for result in results:
            if isinstance(result, BaseException):
                # 시드 하나가 비어 돌아와도 견딜 수 있다 — 다른 시드들이 순위를 매길
                # 만큼의 텍스트를 여전히 담고 있다.
                failures.append(result)
                continue
            # 같은 문서가 두 시드에서 함께 돌아오면 링크 기준으로 한 번만 센다 — 순위와
            # 근거 수치가 같은 문서로 두 번 부풀지 않게 한다.
            for doc in result:
                if doc.link and doc.link in seen_links:
                    continue
                if doc.link:
                    seen_links.add(doc.link)
                documents.append(doc)

        # 모든 요청이 실패한 것은 인기 없는 소재가 아니라 깨진 자격 증명이나 누락된
        # 권한이다. 예외를 던지면 aggregate가 네이버를 조용히 패널에서 빼는 대신 그
        # 소스에 대해 로그를 남긴다.
        if failures and not documents:
            raise failures[0]
        return documents
