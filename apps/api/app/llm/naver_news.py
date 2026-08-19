"""관련된 **최신 뉴스 기사**를 자료 수집(M3)의 근거로 가져온다(2026-08-11 사용자 지시).

왜 필요한가. M3 수집은 Gemini + 구글 검색(grounding)이고, 여기에 네이버 블로그 실사용
글이 보태져 있었다(naver_blog). 둘 다 '지금 무슨 일이 있었는가'에는 약하다 — 구글
grounding은 무엇이 잡힐지 우리가 정할 수 없고, 블로그는 후기·경험담이라 사건·발표·수치의
최신 상태를 말해 주지 않는다. 그래서 시의성이 중요한 글(신제품·정책·사건·일정)에서 자료가
낡은 채로 원고까지 흘러갔다.

무엇을 하나. 네이버 검색 API의 뉴스를 **최신순(sort=date)**으로 물어, 관련 기사 몇 건을
날짜와 함께 가져온다. 검색 API는 제목·요약·발행시각·주소만 주므로 본문은 네이버 뉴스
기사 페이지에서 읽는다(`#dic_area` — 그 안에 본문이 그대로 있다). 연예·스포츠 기사는
본문이 JS로 그려져 그 컨테이너가 없고, 그때는 검색 API의 요약을 근거로 쓴다.

**최신을 우선하되 없는 것을 지어내지 않는다.** 기본은 최근 RECENT_WINDOW_DAYS일 안의
기사이고, 그 안에 아무것도 없으면 검색이 준 최신 기사를 그대로 쓰되 **발행일을 항상 함께
싣는다** — 요약 모델과 사용자가 그 자료가 언제 것인지 보고 판단할 수 있어야 한다.

출처는 언론사 원문 주소(`originallink`)를 쓴다. 오늘 정한 원칙(출처는 CDN·중개자가 아니라
실제 원본)과 같고, 최신 기사라 그 주소가 살아 있을 가능성도 높다. 원문 주소가 없으면
네이버 기사 주소를 쓴다.

실패는 어떤 것도 검증(M3)을 죽이지 않는다: 기사 하나가 안 열리면 그 기사만 요약으로
대체하고, 수집기 전체가 실패하면 없던 일이 된다(호출부가 삼킨다).
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from app.llm.http import shared_client
from app.llm.naver_api import auth_headers, search_url

logger = logging.getLogger(__name__)

NAVER_NEWS_SEARCH_URL = search_url("news")

# 최신순으로 몇 건까지 받아 볼 것인가. 이 안에서 날짜 창을 통과한 것을 고른다.
SEARCH_DISPLAY = 20
# 근거로 실을 기사 수의 기본값. 같은 사건을 다룬 기사 여러 개는 한 개만큼의 사실만 더한다.
DEFAULT_ARTICLE_LIMIT = 4
# '최신'의 기본 창(일). 이 안에 기사가 있으면 그것만 쓴다.
RECENT_WINDOW_DAYS = 14
# 기사 하나에서 근거로 가져올 본문 길이 상한. 요약 모델 입력을 지킨다.
EXCERPT_CHARS = 1_500
# 이보다 짧으면 본문 추출이 실패한 것으로 본다(껍데기·JS 렌더 페이지).
MIN_EXCERPT_CHARS = 80
# 기사 한 편을 기다리는 상한. 검증 전체에 얹히는 값이라 짧게 둔다.
FETCH_TIMEOUT_SECONDS = 8.0

# 제목의 글자쌍이 이만큼 겹치면 같은 사건을 받아쓴 기사로 본다(0~1).
#
# 왜 필요한가. 실측(2026-08-11): '금리 인하'를 최신순으로 물으면 같은 카카오뱅크
# 보도자료를 받아쓴 기사가 상위 세 건을 모두 차지했다 — 자료 자리는 다 쓰면서 새로
# 알려 주는 사실은 하나뿐이다.
#
# 왜 낱말이 아니라 글자쌍인가. 언론사마다 띄어쓰기가 달라('보증서 대출' / '보증서대출')
# 낱말로 세면 같은 발표가 다른 기사로 통과한다. 같은 표본으로 잰 값:
#
#   같은 보도자료   0.50 ~ 0.59
#   다른 기사      0.00 ~ 0.24  (수능 D-100 기사끼리도 0.23)
#
# 두 무리가 0.24와 0.50 사이에서 갈라져, 문턱을 그 사이에 둔다.
DUPLICATE_TITLE_OVERLAP = 0.45

_TAG = re.compile(r"<[^>]+>")
_SCRIPT_BLOCK = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
# 네이버 뉴스 기사 본문 컨테이너. 데스크톱·모바일 모두 이 id를 쓴다.
_ARTICLE_BODY = re.compile(r'id="dic_area"[^>]*>(?P<body>.*)', re.DOTALL)
# 본문에 섞여 있는 사진 설명·기자 서명은 근거로 쓸 문장이 아니다.
_CAPTION_BLOCK = re.compile(
    r"""<(?P<tag>span|em)[^>]+class=["'][^"']*(?:end_photo_org|img_desc)[^"']*["'][^>]*>"""
    r".*?</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class NaverNewsArticle:
    """근거로 실을 뉴스 기사 하나."""

    title: str
    #: 출처로 표기할 주소. 언론사 원문(originallink)이 있으면 그것이다.
    url: str
    #: 검색 API의 요약(description). 검증 팝업의 snippet 자리에 들어간다.
    snippet: str
    #: 본문 발췌. 읽지 못했으면 빈 문자열이고, 그때는 요약이 근거를 대신한다.
    excerpt: str
    #: 발행 시각("2026-08-11 14:03"). 읽을 수 없으면 빈 문자열이다.
    published_at: str = ""
    #: 네이버 뉴스 기사 주소(본문을 읽은 곳). 원문 주소와 다를 수 있다.
    naver_url: str = ""


def strip_search_markup(value: str) -> str:
    """검색 API가 강조용으로 끼워 넣는 <b> 태그와 HTML 엔티티를 걷어낸다."""
    return html.unescape(_TAG.sub("", value or "")).strip()


def parse_pub_date(value: str) -> datetime | None:
    """검색 API의 pubDate(RFC 1123)를 시각으로. 읽을 수 없으면 None."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_WORD = re.compile(r"[0-9a-z가-힣]+")


def title_bigrams(title: str) -> set[str]:
    """제목의 글자쌍. 기호·띄어쓰기·대소문자 차이를 지운 뒤 두 글자씩 끊는다."""
    joined = "".join(_WORD.findall((title or "").lower()))
    if len(joined) < 2:
        return {joined} if joined else set()
    return {joined[index : index + 2] for index in range(len(joined) - 1)}


def title_overlap(left: str, right: str) -> float:
    """두 제목이 얼마나 같은가(0~1). 짧은 쪽을 기준으로 잰다 — 한쪽이 길다고 같은
    발표가 다른 기사로 통과하면 안 된다."""
    a, b = title_bigrams(left), title_bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def is_same_story(title: str, seen_titles: list[str]) -> bool:
    """이미 고른 기사와 같은 사건을 받아쓴 것인가."""
    return any(
        title_overlap(title, other) >= DUPLICATE_TITLE_OVERLAP for other in seen_titles
    )


def extract_article_text(page_html: str) -> str:
    """네이버 뉴스 기사 페이지에서 본문 텍스트를 뽑는다. 못 뽑으면 빈 문자열.

    빈 문자열은 '본문을 못 읽었다'는 뜻이지 '기사가 없다'는 뜻이 아니다 — 연예·스포츠
    기사는 본문이 JS로 그려져 여기서는 늘 빈 문자열이고, 그때는 검색 API의 요약을 쓴다.
    페이지 전체 텍스트로 물러나지 않는다: 메뉴·연관기사 제목이 '확인된 사실'로 섞인다.
    """
    if not page_html:
        return ""
    match = _ARTICLE_BODY.search(page_html)
    if match is None:
        return ""
    body = _SCRIPT_BLOCK.sub(" ", match.group("body"))
    body = _CAPTION_BLOCK.sub(" ", body)
    body = re.sub(r"</(p|div|br|h[1-6]|li)>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    text = html.unescape(_TAG.sub(" ", body))
    text = re.sub(r"[ \t​]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    if len(text) < MIN_EXCERPT_CHARS:
        return ""
    return text[:EXCERPT_CHARS]


class NaverNewsResearch:
    """네이버 뉴스 검색(최신순) + 본문 확보. 검증(M3)의 보강 수집기다."""

    def __init__(self, client_id: str, client_secret: str):
        self._headers = auth_headers(client_id, client_secret)

    async def collect(
        self,
        queries: list[str],
        limit: int = DEFAULT_ARTICLE_LIMIT,
        *,
        now: datetime | None = None,
    ) -> list[NaverNewsArticle]:
        """질의들을 순서대로 물어 최신 기사를 최대 ``limit``건 돌려준다.

        블로그 수집과 달리 **질의를 끝까지 돈다.** 뉴스는 질의마다 다른 사건이 잡히고
        (소재 이름 / 트렌드 키워드 / 사용자 키워드), 최근 기사가 한 질의에만 있는 일이
        흔하기 때문이다. 같은 기사는 주소로 걸러진다.
        """
        observed = now or datetime.now(timezone.utc)
        cutoff = observed - timedelta(days=RECENT_WINDOW_DAYS)

        recent: list[tuple[datetime | None, NaverNewsArticle]] = []
        older: list[tuple[datetime | None, NaverNewsArticle]] = []
        seen: set[str] = set()

        for query in [q.strip() for q in queries if q and q.strip()]:
            try:
                items = await self._search(query)
            except Exception as error:  # noqa: BLE001 - 보강 수집이 검증을 죽이지 않는다
                logger.warning("네이버 뉴스 검색 실패 | '%s' - %s", query, error)
                break
            for item in items:
                naver_url = (item.get("link") or "").strip()
                origin_url = (item.get("originallink") or "").strip()
                url = origin_url if origin_url.startswith(("http://", "https://")) else naver_url
                if not url or url in seen:
                    continue
                seen.add(url)
                published = parse_pub_date(item.get("pubDate") or "")
                article = NaverNewsArticle(
                    title=strip_search_markup(item.get("title") or "") or "제목 없음",
                    url=url,
                    snippet=strip_search_markup(item.get("description") or ""),
                    excerpt="",
                    published_at=(
                        published.astimezone().strftime("%Y-%m-%d %H:%M") if published else ""
                    ),
                    naver_url=naver_url,
                )
                bucket = recent if (published and published >= cutoff) else older
                bucket.append((published, article))

        # 최근 것부터. 날짜를 못 읽은 기사는 뒤로 민다 — 최신을 고르는 자리에서 '모름'을
        # 앞세울 이유가 없다.
        def newest_first(rows):
            """최신순으로 세우되, 같은 사건을 받아쓴 기사는 첫 건만 남긴다."""
            ordered = sorted(
                rows,
                key=lambda row: row[0] or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            kept: list[NaverNewsArticle] = []
            titles: list[str] = []
            for _, article in ordered:
                if is_same_story(article.title, titles):
                    continue
                kept.append(article)
                titles.append(article.title)
            return kept

        chosen = newest_first(recent)[:limit]
        if not chosen:
            # 최근 창에 아무것도 없다. 없는 것을 지어내는 대신 검색이 준 최신 기사를
            # 그대로 쓰되, 발행일이 함께 실리므로 낡았다는 사실이 숨겨지지 않는다.
            chosen = newest_first(older)[:limit]
            if chosen:
                logger.info(
                    "네이버 뉴스 보강 | 최근 %d일 기사가 없어 그 이전 기사 %d건을 씁니다",
                    RECENT_WINDOW_DAYS,
                    len(chosen),
                )
        if not chosen:
            return []

        return [await self._with_excerpt(article) for article in chosen]

    async def _search(self, query: str) -> list[dict]:
        response = await shared_client().get(
            NAVER_NEWS_SEARCH_URL,
            headers=self._headers,
            # 최신순이 이 수집기의 존재 이유다. 관련도순(sim)은 몇 년 전 기사를 1위로 준다.
            params={"query": query, "display": str(SEARCH_DISPLAY), "sort": "date"},
        )
        response.raise_for_status()
        items = response.json().get("items")
        return items if isinstance(items, list) else []

    async def _with_excerpt(self, article: NaverNewsArticle) -> NaverNewsArticle:
        """네이버 기사 페이지에서 본문을 읽어 채운다. 못 읽으면 요약만으로 남는다."""
        naver_url = article.naver_url
        if not naver_url.startswith(("http://", "https://")):
            return article
        try:
            response = await shared_client().get(
                naver_url,
                timeout=FETCH_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"user-agent": "Mozilla/5.0 (compatible; Blog-it/1.0)"},
            )
            response.raise_for_status()
        except Exception as error:  # noqa: BLE001 - 기사 하나의 실패는 요약으로 대체한다
            logger.info("뉴스 본문 확보 실패 | %s - %s", naver_url, error)
            return article
        excerpt = extract_article_text(response.text)
        if not excerpt:
            return article
        return NaverNewsArticle(
            title=article.title,
            url=article.url,
            snippet=article.snippet,
            excerpt=excerpt,
            published_at=article.published_at,
            naver_url=article.naver_url,
        )
