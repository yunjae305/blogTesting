"""네이버 블로그 글을 자료 수집(M3)의 근거로 가져온다.

왜 필요한가(2026-08-10 사용자 결정). M3 수집은 Gemini + 구글 검색이라 네이버 블로그의
한국어 실사용 후기가 잘 안 잡힌다 — 맛집·제품 후기·생활 정보는 네이버 블로그에 몰려
있다. 네이버 공식 API에는 본문을 주는 것이 없으므로 두 단계로 간다:

1. **검색은 공식 API로.** 검색 API(블로그)가 제목·링크·요약 150자를 관련도순으로 준다.
   자격 증명은 트렌드·사진 검색과 같다(NAVER_CLIENT_ID/SECRET).
2. **본문은 모바일판에서.** 데스크톱 blog.naver.com은 본문이 iframe 안에 있는 껍데기라
   그대로 가져오면 빈 틀만 읽힌다. m.blog.naver.com은 본문이 HTML에 그대로 있다.

가져온 글은 **사실 확인 근거**로만 쓴다 — 원고 규칙(문장을 옮기지 않는다, 출처를
남긴다)은 기존 프롬프트가 이미 강제한다. 실패는 어떤 것도 검증(M3)을 죽이지 않는다:
글 하나가 안 열리면 그 글만 건너뛰고, 수집기 전체가 실패하면 없던 일이 된다.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from app.llm.http import shared_client
from app.llm.naver_api import auth_headers, search_url

logger = logging.getLogger(__name__)

# NAVER API HUB 이관(2026-08-11). 옛 개발자센터 주소로 부르면 401이다(naver_api 참고).
NAVER_BLOG_SEARCH_URL = search_url("blog")

# 검색 결과를 몇 개까지 받아 볼 것인가. 본문을 열 후보가 이 안에서 나온다.
SEARCH_DISPLAY = 10
# 근거로 실을 글 수의 기본값. 많을수록 좋은 게 아니다 — 요약 모델의 입력이 길어지고,
# 비슷한 후기 여러 개는 한 개만큼의 사실만 더한다.
DEFAULT_POST_LIMIT = 3
# 글 하나에서 근거로 가져올 본문 길이 상한. 블로그 후기는 서두·잡담이 길어 전체가
# 필요 없고, 요약 모델 입력도 지켜야 한다.
EXCERPT_CHARS = 2_000
# 이보다 짧으면 본문 추출이 실패했거나(껍데기) 근거 가치가 없는 글이다.
MIN_EXCERPT_CHARS = 80
# 본문 한 페이지를 기다리는 상한. 검증 전체(수집 30~45초)에 얹히는 값이라 짧게 둔다.
FETCH_TIMEOUT_SECONDS = 8.0

# 모바일판이 데스크톱 UA에 데스크톱으로 리다이렉트하는 일을 막는다.
_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Mobile Safari/537.36"
)

# blog.naver.com/{아이디}/{글번호} — 데스크톱·모바일 공통의 정규 경로.
_BLOG_PATH = re.compile(r"^/(?P<blog_id>[A-Za-z0-9_-]+)/(?P<log_no>\d+)/?$")

_TAG = re.compile(r"<[^>]+>")
_SCRIPT_BLOCK = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
# 스마트에디터 ONE 본문 컨테이너. 모바일 페이지 HTML에 본문이 이 안에 통째로 있다.
_SE_MAIN = re.compile(
    r'<div[^>]+class="[^"]*se-main-container[^"]*"[^>]*>(?P<body>.*)', re.DOTALL
)
# 구형 에디터의 모바일 본문 컨테이너.
_LEGACY_VIEW = re.compile(
    r'<div[^>]+id="viewTypeSelector"[^>]*>(?P<body>.*)', re.DOTALL
)


@dataclass(frozen=True)
class NaverBlogPost:
    """근거로 실을 네이버 블로그 글 하나."""

    title: str
    #: 사용자에게 보여줄 원본 주소(데스크톱). 출처 표기는 이 주소로 한다.
    url: str
    #: 요약(검색 API의 description). 검증 팝업의 snippet 자리에 들어간다.
    snippet: str
    #: 모바일판에서 뽑은 본문 발췌 — 요약 모델이 사실을 확인하는 근거.
    excerpt: str
    blogger: str = ""
    posted_at: str = ""


def strip_search_markup(value: str) -> str:
    """검색 API가 강조용으로 끼워 넣는 <b> 태그와 HTML 엔티티를 걷어낸다."""
    return html.unescape(_TAG.sub("", value or "")).strip()


def to_mobile_url(link: str) -> str | None:
    """네이버 블로그 주소를 본문이 읽히는 모바일판으로 바꾼다. 못 바꾸면 None.

    None은 '이 글은 본문을 못 가져온다'는 뜻이다 — 검색 API(블로그)는 네이버 블로그만
    돌려주지만, 형식이 낯선 주소를 억지로 열지 않는다.
    """
    try:
        parsed = urlparse((link or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.netloc or "").lower()
    if host not in ("blog.naver.com", "m.blog.naver.com"):
        return None

    match = _BLOG_PATH.match(parsed.path or "")
    if match:
        return (
            "https://m.blog.naver.com/"
            f"{match.group('blog_id')}/{match.group('log_no')}"
        )
    # 구형 PostView 주소: blog.naver.com/PostView.naver?blogId=..&logNo=..
    query = parse_qs(parsed.query or "")
    blog_id = (query.get("blogId") or [""])[0]
    log_no = (query.get("logNo") or [""])[0]
    if blog_id and log_no.isdigit():
        return f"https://m.blog.naver.com/{blog_id}/{log_no}"
    return None


def extract_post_text(page_html: str) -> str | None:
    """모바일 블로그 페이지에서 본문 텍스트를 뽑는다. 본문 컨테이너가 없으면 None.

    None이면 그 글은 근거로 쓰지 않는다 — 페이지 전체 텍스트로 물러나면 메뉴·댓글·추천
    글 제목이 '확인된 사실'로 섞여 들어간다.
    """
    if not page_html:
        return None
    match = _SE_MAIN.search(page_html) or _LEGACY_VIEW.search(page_html)
    if match is None:
        return None
    body = _SCRIPT_BLOCK.sub(" ", match.group("body"))
    # 블록 태그를 줄바꿈으로 바꿔 문단 경계를 살린 뒤 나머지 태그를 걷어낸다.
    body = re.sub(r"</(p|div|h[1-6]|li|section)>", "\n", body, flags=re.IGNORECASE)
    text = html.unescape(_TAG.sub(" ", body))
    # ​: 스마트에디터가 문단 사이에 끼워 넣는 zero-width space.
    text = re.sub("[ \\t\\u200b]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    if len(text) < MIN_EXCERPT_CHARS:
        return None
    return text[:EXCERPT_CHARS]


class NaverBlogResearch:
    """네이버 블로그 검색 + 본문 확보. 검증(M3)의 보강 수집기다."""

    def __init__(self, client_id: str, client_secret: str):
        self._headers = auth_headers(client_id, client_secret)

    async def collect(
        self, queries: list[str], limit: int = DEFAULT_POST_LIMIT
    ) -> list[NaverBlogPost]:
        """질의들을 순서대로 물어 본문까지 확보한 글을 최대 ``limit``개 돌려준다.

        첫 질의에서 채워지면 다음 질의는 부르지 않는다 — 질의는 넓히는 사다리이지
        합산이 아니다(같은 소재를 말만 바꾼 질의는 같은 글을 돌려준다).
        """
        posts: list[NaverBlogPost] = []
        seen_urls: set[str] = set()
        for query in [q.strip() for q in queries if q and q.strip()]:
            try:
                items = await self._search(query)
            except Exception as error:  # noqa: BLE001 - 보강 수집이 검증을 죽이지 않는다
                logger.warning("네이버 블로그 검색 실패 | '%s' - %s", query, error)
                return posts
            for item in items:
                if len(posts) >= limit:
                    return posts
                link = (item.get("link") or "").strip()
                mobile = to_mobile_url(link)
                if mobile is None or link in seen_urls:
                    continue
                seen_urls.add(link)
                excerpt = await self._fetch_excerpt(mobile)
                if excerpt is None:
                    continue
                posts.append(
                    NaverBlogPost(
                        title=strip_search_markup(item.get("title") or "") or "제목 없음",
                        url=link,
                        snippet=strip_search_markup(item.get("description") or ""),
                        excerpt=excerpt,
                        blogger=strip_search_markup(item.get("bloggername") or ""),
                        posted_at=(item.get("postdate") or "").strip(),
                    )
                )
            if posts:
                # 이 질의로 하나라도 건졌으면 충분하다. 다음 질의는 표현만 다른 같은
                # 소재라 새 글보다 중복이 나온다.
                return posts
        return posts

    async def _search(self, query: str) -> list[dict]:
        response = await shared_client().get(
            NAVER_BLOG_SEARCH_URL,
            headers=self._headers,
            params={"query": query, "display": str(SEARCH_DISPLAY), "sort": "sim"},
        )
        response.raise_for_status()
        items = response.json().get("items")
        return items if isinstance(items, list) else []

    async def _fetch_excerpt(self, mobile_url: str) -> str | None:
        """모바일 본문을 가져와 발췌를 만든다. 실패·빈약하면 None(그 글만 건너뜀)."""
        try:
            response = await shared_client().get(
                mobile_url,
                headers={"User-Agent": _MOBILE_USER_AGENT},
                timeout=FETCH_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
            response.raise_for_status()
        except Exception as error:  # noqa: BLE001 - 글 하나의 실패는 그 글만 건너뛴다
            logger.info("네이버 블로그 본문 확보 실패 | %s - %s", mobile_url, error)
            return None
        return extract_post_text(response.text)
