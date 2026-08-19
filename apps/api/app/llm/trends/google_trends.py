"""구글 트렌드 — 'Trending now' 페이지를 브라우저로 직접 읽는다.

SerpApi와 공식 RSS는 더 이상 쓰지 않는다(2026-08-07 결정). 둘 다 화면이 필요한 것을 주지
못했기 때문이다:

  - **SerpApi**는 무료 플랜이 월 250건이고 실제로 소진됐다(측정: this_month_usage 250/250,
    plan_searches_left 0). 크레딧이 0이면 검색량·상승률·상승 시작 시각을 받을 길이 없다.
  - **공식 RSS**는 키가 필요 없지만 `<title>`과 `<ht:approx_traffic>`뿐이다 — 상승률도,
    상승 시작 시각도, 급상승 활성 여부도 없다. 카드의 세 줄 중 한 줄밖에 못 채운다.

같은 데이터를 구글이 자기 페이지에는 다 그려 준다. 브라우저로 열면 넷을 모두 얻는다
(2026-08-07 실측, geo=KR):

    키워드 '김민재' · 검색량 '5천+' · 증가율 '1,000%' · 시작 '3시간 전' · 상태 '활성'

느려지지도 않았다. 헤드리스 Chrome 기동 ~1.1초 + 페이지 렌더 대기 ~2.0초 + 추출 ~0.2초
= **약 3.3초**로, SerpApi가 실패해 RSS로 넘어가던 경로(측정 2.9초)와 같은 자리다. 관건은
두 가지다: 예시 코드가 쓰던 `time.sleep(5)` 대신 행이 나타나는 순간까지만 기다리는 명시적
대기를 쓰는 것, 그리고 셀 하나하나를 `find_element`로 왕복하지 않고 JS 한 번으로 표 전체를
긁어 오는 것(셀별 왕복은 실측 1.0초, JS 일괄은 0.2초).

브라우저는 **일회용 프로필**로 띄운다. 네이버 발행이 쓰는 프로필(`.naver-profile`)을 건드리면
"user data directory is already in use"로 서로를 죽인다 — 트렌드는 로그인이 필요 없으므로
프로필을 공유할 이유가 없다.

Chrome이 없거나 크롤이 실패하면 이 소스는 빈 손으로 돌아온다. 폴백은 두지 않는다(위 결정).
그때는 네이버·유튜브가 패널을 채우고, 실패 사실은 로그에 남는다 — AggregateTrendProvider가
소스 하나의 실패를 이미 견딘다.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from app.llm.contracts import TrendFetchInput
from app.shared import (
    GoogleTrendEvidence,
    TrendEvidenceOrigin,
    TrendMode,
    TrendSource,
    TrendSourceEvidence,
)

from .base import REQUEST_TIMEOUT, CollectedKeyword, seed_queries
from .normalizer import normalize_keyword, repair_spacing
from .text import is_low_quality_keyword, is_noun_phrase

logger = logging.getLogger(__name__)

TRENDING_URL = "https://trends.google.com/trending"

# 소재 연관 검색어를 주는 유일한 구글 경로(2026-08-07 실측). Trends explore는 브라우저로
# 열어도 429이고, 급상승 표는 전국 단위라 소재 관련 후보가 나오지 않는다.
SUGGEST_URL = "https://suggestqueries.google.com/complete/search"

# 몇 시간 범위의 급상승을 볼지. 24시간이 기본이다 — 4시간은 22행, 24·48시간은 25행이
# 돌아왔고(실측), 24시간이 "오늘 뜬 것"이라는 최신순의 뜻에 가장 가깝다. 페이지는 한 번에
# 25행까지만 그리므로 이 값을 늘려도 개수는 늘지 않는다.
TRENDING_WINDOW_HOURS = 24

# 표와 셀의 위치. 구글이 클래스 이름을 바꾸면 여기만 고치면 된다 — 2026-08-07 실측값이다.
ROW_SELECTOR = "tbody[jsname='cC57zf'] tr"
KEYWORD_SELECTOR = "div.mZ3RIc"
VOLUME_SELECTOR = "div.lqv0Cb"
INCREASE_SELECTOR = "div.TXt85b"
STARTED_SELECTOR = "div.vdw3Ld"
STATUS_SELECTOR = "div.UQMqQd"

# 페이지가 렌더될 때까지의 상한. 실측 2초대이고, 넘으면 이번 수집에서 구글은 빈 손이다.
RENDER_TIMEOUT_SECONDS = 20.0
PAGE_LOAD_TIMEOUT_SECONDS = 30.0

# 표 전체를 한 번에 긁는다. 셀마다 WebDriver를 왕복하면 25행 × 5셀 = 125번의 왕복이 되고,
# 그것만으로 1초가 든다(실측).
_EXTRACT_ROWS_JS = f"""
const pick = (row, selector) => {{
  const found = row.querySelector(selector);
  return found ? found.textContent.trim() : null;
}};
return Array.from(document.querySelectorAll({ROW_SELECTOR!r})).map((row) => ({{
  keyword: pick(row, {KEYWORD_SELECTOR!r}),
  volume: pick(row, {VOLUME_SELECTOR!r}),
  increase: pick(row, {INCREASE_SELECTOR!r}),
  started: pick(row, {STARTED_SELECTOR!r}),
  status: pick(row, {STATUS_SELECTOR!r}),
}}));
"""

# 한국어 수량 단위. "5천+" → 5000, "50만+" → 500000.
_VOLUME_UNITS = {"억": 100_000_000, "만": 10_000, "천": 1_000}
_VOLUME = re.compile(r"([\d,.]+)\s*(억|만|천)?")
_PERCENT = re.compile(r"([\d,.]+)\s*%")
_RELATIVE_TIME = re.compile(r"(\d+)\s*(분|시간|일)\s*전")


@dataclass
class TrendingRow:
    """페이지에서 읽은 한 행. 값은 아직 화면 표기 그대로다(파싱은 아래에서)."""

    keyword: str
    volume: str | None = None
    increase: str | None = None
    started: str | None = None
    status: str | None = None


def _number(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def search_volume(text: str | None) -> float | None:
    """'5천+' → 5000, '50만+' → 500000, '100+' → 100. 못 읽으면 만들지 않고 None."""
    if not text:
        return None
    match = _VOLUME.search(text)
    if not match:
        return None
    base = _number(match.group(1))
    if base is None:
        return None
    return base * _VOLUME_UNITS.get(match.group(2) or "", 1)


def increase_percentage(text: str | None) -> float | None:
    """'1,000%' → 1000.0."""
    if not text:
        return None
    match = _PERCENT.search(text)
    return _number(match.group(1)) if match else None


def started_at(text: str | None, observed_at: datetime) -> str | None:
    """'3시간 전'·'어제' 같은 상대 표기를 절대 시각(UTC ISO)으로.

    화면은 다시 상대 시각으로 바꿔 보여주지만, 저장은 절대 시각이어야 한다 — '3시간 전'을
    그대로 저장하면 하루 뒤에 읽었을 때도 여전히 '3시간 전'이라고 말하게 된다.
    """
    if not text:
        return None
    value = text.strip()
    if "어제" in value:
        delta = timedelta(days=1)
    elif "그저께" in value:
        delta = timedelta(days=2)
    else:
        match = _RELATIVE_TIME.search(value)
        if not match:
            return None
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {
            "분": timedelta(minutes=amount),
            "시간": timedelta(hours=amount),
            "일": timedelta(days=amount),
        }[unit]
    return (observed_at - delta).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_active(text: str | None) -> bool | None:
    """급상승이 지금도 진행 중인가.

    실측 표기는 둘이다: 진행 중이면 '활성', 끝났으면 'N시간 동안 지속됨'. 어느 쪽으로도
    읽히지 않으면 None — 모른다는 것을 '아니다'로 바꾸지 않는다.
    """
    if not text:
        return None
    if "비활성" in text:
        return False
    if "활성" in text:
        return True
    if "지속" in text:
        return False
    return None


def _scrape_trending_rows(country: str, hours: int) -> list[dict[str, Any]]:
    """헤드리스 Chrome으로 페이지를 열고 표를 통째로 긁는다(블로킹 — 스레드에서 부른다)."""
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError as error:
        raise RuntimeError(
            "Selenium이 없습니다. pip install -r apps/api/requirements.txt"
        ) from error

    options = webdriver.ChromeOptions()
    # 네이버 발행이 쓰는 것과 같은 Chrome이다. 경로를 지정해 둔 환경에서는 그대로 따른다.
    binary = (os.environ.get("NAVER_CHROME_BINARY") or "").strip()
    if binary and os.path.isfile(binary):
        options.binary_location = binary
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ko-KR")
    options.add_argument("--log-level=3")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    # 표의 글자만 필요하다. 이미지를 끄면 렌더가 눈에 띄게 빨라진다.
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )

    # user_data_dir을 주지 않는다 — Chrome이 일회용 프로필을 만들고, 끝나면 사라진다.
    # 네이버 발행 프로필을 공유하면 두 작업이 서로를 "already in use"로 죽인다.
    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
        driver.get(f"{TRENDING_URL}?geo={country}&hours={hours}")
        # sleep으로 시간을 때우지 않는다 — 행이 나타나는 순간 넘어간다.
        WebDriverWait(driver, RENDER_TIMEOUT_SECONDS).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ROW_SELECTOR))
        )
        rows = driver.execute_script(_EXTRACT_ROWS_JS)
    finally:
        # 실패했더라도 브라우저는 반드시 닫는다. 남으면 다음 수집마다 하나씩 쌓인다.
        try:
            driver.quit()
        except Exception as error:  # noqa: BLE001 - 닫기 실패가 수집 결과를 바꾸지는 않는다
            logger.debug("구글 트렌드: 브라우저 종료 실패(무시) - %s", error)
    return rows if isinstance(rows, list) else []


class GoogleTrendsCollector:
    source = TrendSource.GOOGLE_TRENDS

    def __init__(
        self,
        hours: int = TRENDING_WINDOW_HOURS,
        scrape: Callable[[str, int], list[dict[str, Any]]] | None = None,
    ):
        self._hours = hours
        # 테스트는 브라우저 대신 표 내용을 직접 넣는다. 파싱·필터·근거 구성은 같은 코드를 탄다.
        self._scrape = scrape or _scrape_trending_rows
        # 브라우저는 한 번에 하나만 띄운다. 동시 요청마다 Chrome이 하나씩 뜨면
        # 메모리도 시간도 그만큼 곱해진다.
        self._lock = asyncio.Lock()

    async def collect(
        self,
        trend_input: TrendFetchInput,
        limit: int | None,
        known: frozenset[str] = frozenset(),
    ) -> list[CollectedKeyword]:
        # 소재 관련순은 급상승 표로 답할 수 없는 질문이다. 그 표는 **전국** 급상승이라
        # 소재가 무엇이든 같은 목록이고, 실제로 '참이슬'로 조회해도 소재를 담은 후보가
        # 0개였다(실측). 소재별 데이터를 주는 Trends explore는 브라우저로 열어도 429다.
        # 남은 경로는 자동완성이며, 거기서 얻는 것은 키워드뿐이다 — 수치는 없다.
        if trend_input.mode == TrendMode.MATERIAL_RELATED:
            return await self._material_suggestions(trend_input, limit, known)

        country = trend_input.country or "KR"
        async with self._lock:
            # Selenium은 블로킹이다. 이벤트 루프에서 돌리면 그동안 다른 소스의 타임아웃
            # 타이머까지 멈춘다(네이버 구절 추출과 같은 이유).
            raw = await asyncio.to_thread(self._scrape, country, self._hours)

        observed_at = datetime.now(timezone.utc)
        rows = [row for row in map(_to_row, raw) if row is not None]
        logger.info("구글 트렌드: 페이지에서 %d개 수집 (키 없이, geo=%s)", len(rows), country)
        return self._to_candidates(rows, observed_at, limit, known)

    async def _material_suggestions(
        self,
        trend_input: TrendFetchInput,
        limit: int | None,
        known: frozenset[str],
    ) -> list[CollectedKeyword]:
        """구글 자동완성으로 소재 연관 검색어를 모은다.

        자동완성은 사람들이 실제로 많이 친 질의를 인기순으로 돌려준다 — 소재 '참이슬'에
        '참이슬 도수·참이슬 가격·참이슬 칼로리'가 이 순서로 온다(실측). 키 없이 요청 하나면
        되고 빠르다.

        **수치는 없다.** 검색량도 상승률도 주지 않으므로 여기서는 근거를 만들지 않고
        (evidence=None) 순위만 점수로 옮긴다. 카드의 세 줄은 aggregate가 이 키워드를
        네이버에 물어 채운다 — 구글이 제안하고 네이버가 확인하는 구조이며, 화면도 두 출처를
        함께 표시한다. 없는 숫자를 지어내는 것보다 잰 곳을 밝히는 편이 정직하다.
        """
        seeds = seed_queries(trend_input)
        topic = (trend_input.input.topic or "").strip()
        if not topic:
            return []
        # 소재 자체와, 뒤에 공백을 붙인 질의(이어지는 말을 더 많이 준다), 그리고 사용자가
        # 직접 적은 키워드까지. 같은 소재라도 질의가 다르면 다른 제안이 온다.
        queries = list(dict.fromkeys([topic, f"{topic} ", *seeds]))

        suggestions: list[str] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            results = await asyncio.gather(
                *(self._suggest(client, query) for query in queries),
                return_exceptions=True,
            )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("구글 자동완성: 요청 실패 - %s", result)
                continue
            for suggestion in result:
                key = suggestion.lower()
                if key in seen:
                    continue
                seen.add(key)
                suggestions.append(suggestion)

        candidates: list[CollectedKeyword] = []
        for index, suggestion in enumerate(suggestions):
            keyword = repair_spacing(suggestion.strip())
            # 소재를 그대로 되풀이한 제안('참이슬')은 후보가 아니다. 소재 메아리·기계적
            # 조합은 aggregate의 노출 필터가 한 번 더 거른다.
            if not keyword or keyword.strip().lower() == topic.lower():
                continue
            if is_low_quality_keyword(keyword):
                continue
            # 자동완성은 인기순이다. 앞자리일수록 높은 점수를 준다(_normalize가 소스별
            # 범위를 맞춘다). 절대적인 검색량이 아니므로 근거로는 싣지 않는다.
            candidates.append(
                CollectedKeyword(
                    keyword=keyword,
                    score=float(100 - index),
                    rank=len(candidates) + 1,
                    evidence=None,
                )
            )

        candidates.sort(
            key=lambda candidate: (
                normalize_keyword(candidate.keyword) in known,
                -candidate.score,
                candidate.rank,
            )
        )
        logger.info(
            "구글 자동완성: 소재 '%s' 연관 검색어 %d개 (수치 없음 - 네이버로 보강)",
            topic,
            len(candidates),
        )
        return candidates[:limit]

    @staticmethod
    async def _suggest(client: httpx.AsyncClient, query: str) -> list[str]:
        """자동완성 한 질의. 응답은 ``[질의, [제안...], ...]`` 꼴의 JSON 배열이다."""
        response = await client.get(
            SUGGEST_URL,
            params={"client": "firefox", "hl": "ko", "gl": "kr", "q": query},
            headers={"user-agent": "Mozilla/5.0"},
        )
        if response.is_error:
            raise RuntimeError(f"Google suggest failed with {response.status_code}")
        payload = json.loads(response.text)
        if not isinstance(payload, list) or len(payload) < 2:
            return []
        return [item for item in payload[1] if isinstance(item, str) and item.strip()]

    def _to_candidates(
        self,
        rows: list[TrendingRow],
        observed_at: datetime,
        limit: int | None,
        known: frozenset[str],
    ) -> list[CollectedKeyword]:
        """표기 복원·중복 제거·명사구 판정·순위. 예전 두 경로가 공유하던 단계 그대로다."""
        observed_iso = observed_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        candidates: list[CollectedKeyword] = []
        seen: set[str] = set()
        for index, row in enumerate(rows):
            # 구글은 한 낱말인 고유명사도 형태소 단위로 끊어 보낸다("다이 소", "황강 댐").
            # 화면·제목·원고까지 그대로 흘러가는 표기이므로 여기서 되돌린다.
            keyword = repair_spacing(row.keyword.strip())
            if not keyword or keyword.lower() in seen:
                continue
            seen.add(keyword.lower())

            # 통째 검색어로 도착하므로 사람들이 입력한 것이 그대로 들어온다 — 동사, 조사까지.
            if not is_noun_phrase(keyword):
                continue

            volume = search_volume(row.volume)
            increase = increase_percentage(row.increase)
            # 점수는 예전과 같은 자리를 쓴다: 검색량이 있으면 그것, 없으면 상승률, 둘 다
            # 없으면 순위 램프. _normalize가 소스별 범위를 알아서 맞춘다.
            score = volume if volume is not None else increase
            if score is None:
                score = float(100 - index)

            candidates.append(
                CollectedKeyword(
                    keyword=keyword,
                    score=score,
                    rank=len(candidates) + 1,
                    evidence=TrendSourceEvidence(
                        source=TrendSource.GOOGLE_TRENDS,
                        observed_at=observed_iso,
                        data_origin=TrendEvidenceOrigin.GOOGLE_TRENDS_WEB,
                        google=GoogleTrendEvidence(
                            active=is_active(row.status),
                            search_volume=volume,
                            increase_percentage=increase,
                            started_at=started_at(row.started, observed_at),
                            feed_type=TrendEvidenceOrigin.GOOGLE_TRENDS_WEB,
                        ),
                    ),
                )
            )

        # 이미 저장된 풀에 있는 키워드(known)는 뒤로 미룬다 — 매일 비슷한 상위권이 반복돼
        # 새 트렌드가 상위 limit 안에 들 기회를 뺏기지 않도록(text.to_collected와 같은 규칙).
        candidates.sort(
            key=lambda candidate: (
                normalize_keyword(candidate.keyword) in known,
                -candidate.score,
                candidate.rank,
            )
        )
        return candidates[:limit]


def _to_row(raw: Any) -> TrendingRow | None:
    """페이지에서 온 한 줄을 모델로. 키워드가 없는 줄(머리글·빈 행)은 버린다."""
    if not isinstance(raw, dict):
        return None
    keyword = raw.get("keyword")
    if not isinstance(keyword, str) or not keyword.strip():
        return None

    def text(field: str) -> str | None:
        value = raw.get(field)
        return value if isinstance(value, str) and value.strip() else None

    return TrendingRow(
        keyword=keyword,
        volume=text("volume"),
        increase=text("increase"),
        started=text("started"),
        status=text("status"),
    )
