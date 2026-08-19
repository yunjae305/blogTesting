"""외부 이미지의 **원출처**를 판정한다(2026-08-11).

왜 필요한가. 지금까지 가져온 사진의 출처는 캡션 문자열 하나로만 남았다 —
`"출처: imgnews.pstatic.net"`. 그 한 줄이 되는 순간 세 가지가 사라진다: 이미지가 실려
있던 **원문 페이지**, 그 사진이 **밖에서도 열리는 주소**, 그리고 **이용 조건을 확인할
방법**. 화면은 남은 한 줄을 자르는 것 말고 할 수 있는 일이 없고, 사용자는 이 사진을 써도
되는지 알 수 없다.

여기서 하는 일은 그 세 가지를 **확인할 수 있는 만큼만** 값으로 만드는 것이다.

원출처 우선순위(사용자 지시 2026-08-11):

1. 이미지가 실린 실제 원문 페이지
2. 그 페이지의 canonical URL
3. 이미지 메타데이터에 있는 출처
4. 검색 결과가 주는 source/page 정보

**검색 서비스 자체는 출처가 아니다.** 네이버 이미지 검색으로 기사 사진을 찾았다고 해서
'네이버'가 출처가 되지는 않는다.

**CDN 호스트도 출처가 아니다**(2026-08-11 후속 지시: "출처도 cdn이 아니라 실제 원본출처가
표기되어야 해"). 저장된 글에서 실측한 캡션이 전부 파일 서버 이름이었다 —
`출처: imgnews.naver.net`, `출처: shop-phinf.pstatic.net`, `출처: i2.ruliweb.com`,
`출처: cdn.instiz.net`. 독자가 그 이름으로 원본을 찾아갈 수 없다. 그래서 이미지 주소가
스스로 말해 주는 것을 읽어 사이트·서비스 이름까지 되찾는다:

- 프록시·썸네일 주소는 원본 주소를 질의 문자열에 통째로 담고 있다(`?src=`, `?fname=`).
- CDN 하위 도메인은 걷어 등록 도메인만 남긴다(i2.ruliweb.com → ruliweb.com). i2와 i3는
  파일 서버만 다른 같은 사이트다.
- 등록 도메인이 사이트가 아닌 CDN 전용 도메인(pstatic.net·phinf·ytimg·daumcdn)은 그
  서비스의 이름으로 적는다(네이버 뉴스·네이버 쇼핑·티스토리·YouTube…).

**여전히 호스트를 언론사 이름으로 바꿔 적지는 않는다.** imgnews.pstatic.net은 네이버
뉴스에 들어온 모든 언론사의 사진을 함께 나르므로, 호스트만 보고 언론사를 단정하면 절반은
틀린 출처가 된다. 언론사를 적는 유일한 길은 **그 이미지 주소가 가리키는 기사를 실제로
확인하는 것**이고, 그것이 ``naver_news_origin``이다: 이미지 경로의 언론사 코드(oid)·기사
번호(aid)로 기사 주소를 되만들어 **열어 보고 200일 때만** 쓰며, 언론사명도 추측이 아니라
그 페이지의 ``og:article:author``에서 읽는다. 열리지 않으면 주소를 버린다.

실측(2026-08-11): 저장된 글에서 꺼낸 이미지 주소 2건으로 되만든 기사 주소가 모두 200이고
기사 제목도 그 사진이 붙어 있던 맥락과 일치했다(디즈니+ 사진 → 디즈니+ 기사, SBS 드라마
사진 → SBS 드라마 기사). 언론사명은 일반 기사에서만 읽힌다 — 연예·스포츠 기사는 본문이
JS로 그려져 그 태그가 없고, 그때는 언론사명 없이 '네이버 뉴스'와 기사 주소만 남는다.

유튜브는 다르다. 영상 페이지가 곧 원문이고(watch 주소), 채널명이 곧 게시자이며,
videos.list의 `status.license`가 라이선스를 **명시**한다 — 유일하게 이용 조건을 사실로
말할 수 있는 경로다.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.llm.http import shared_client
from app.shared import (
    IMAGE_SOURCE_TYPE_EXTERNAL,
    IMAGE_SOURCE_TYPE_GENERATED,
    IMAGE_USAGE_ALLOWED,
    IMAGE_USAGE_RESTRICTED,
    IMAGE_USAGE_UNKNOWN,
    WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL,
    ImageSourceInfo,
)

logger = logging.getLogger(__name__)

# 호스트(또는 그 꼬리)가 이것으로 끝나면 그 서비스의 이름을 쓴다. CDN 전용 도메인은
# 등록 도메인을 남겨 봐야 사이트 이름이 아니기 때문이다(pstatic.net은 사이트가 아니다).
#
# 여기 있는 것은 **그 호스트가 어느 서비스의 파일 서버인가**라는 사실뿐이다. CDN을 언론사
# 이름으로 바꾸는 매핑(imgnews.pstatic.net → 연합뉴스)은 넣지 않는다 — 그 CDN은 네이버
# 뉴스에 들어온 모든 언론사의 사진을 함께 실어 나르므로 절반은 틀린 출처가 된다.
# 언론사는 기사를 실제로 확인해서만 적는다(naver_news_origin).
#
# 더 긴 꼬리가 먼저 걸리도록 긴 것부터 적는다.
SERVICE_BY_HOST_SUFFIX: tuple[tuple[str, str], ...] = (
    # 네이버 — 서비스마다 이미지 서버가 다르다.
    ("imgnews.naver.net", "네이버 뉴스"),
    ("imgnews.pstatic.net", "네이버 뉴스"),
    ("mimgnews.pstatic.net", "네이버 뉴스"),
    ("blogfiles.naver.net", "네이버 블로그"),
    ("blogfiles.pstatic.net", "네이버 블로그"),
    ("postfiles.pstatic.net", "네이버 블로그"),
    ("blogthumb.pstatic.net", "네이버 블로그"),
    ("blogpfthumb-phinf.pstatic.net", "네이버 블로그"),
    ("post-phinf.pstatic.net", "네이버 포스트"),
    ("post-phinf.naver.net", "네이버 포스트"),
    ("cafefiles.naver.net", "네이버 카페"),
    ("cafeptthumb-phinf.pstatic.net", "네이버 카페"),
    ("cafethumb.pstatic.net", "네이버 카페"),
    ("shopping-phinf.pstatic.net", "네이버 쇼핑"),
    ("phinf.naver.net", "네이버"),
    ("pstatic.net", "네이버"),
    ("naver.net", "네이버"),
    ("naver.com", "네이버"),
    # 유튜브 — 썸네일 파일 서버와 영상 페이지가 다른 도메인이다.
    ("ytimg.com", "YouTube"),
    ("youtube.com", "YouTube"),
    ("youtu.be", "YouTube"),
    # 카카오 — 티스토리 이미지가 카카오 CDN에 올라간다.
    ("blog.kakaocdn.net", "티스토리"),
    ("t1.daumcdn.net", "다음"),
    ("daumcdn.net", "다음"),
    ("kakaocdn.net", "카카오"),
    ("tistory.com", "티스토리"),
    # 해외 소셜
    ("twimg.com", "X(트위터)"),
    ("cdninstagram.com", "인스타그램"),
    ("fbcdn.net", "페이스북"),
    ("redd.it", "레딧"),
)

# 네이버 쇼핑 이미지 서버는 번호가 붙어 여러 개다(shop1.phinf.naver.net,
# shop-phinf.pstatic.net…). 꼬리 목록으로는 다 적을 수 없어 앞머리로 가른다.
_NAVER_SHOP_HOST = re.compile(r"^shop\w*[.-](?:phinf\.)?(?:naver\.net|pstatic\.net)$")

# 등록 도메인을 뽑을 때 두 칸을 더 봐야 하는 접미사(co.kr 등). 여기 없으면 두 칸이다.
MULTI_LABEL_SUFFIXES = frozenset(
    {
        "co.kr", "or.kr", "ne.kr", "go.kr", "re.kr", "pe.kr", "ac.kr", "hs.kr",
        "ms.kr", "es.kr", "sc.kr", "kg.kr",
        "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
        "co.uk", "org.uk", "ac.uk", "gov.uk",
        "com.cn", "net.cn", "org.cn", "com.tw", "com.hk", "com.au", "com.br",
    }
)

# 프록시 주소가 원본을 담아 두는 질의 이름. 네이버(src)·다음(fname)에서 실제로 쓰인다.
PROXY_PARAMS = ("src", "fname", "u", "url", "imgurl")
# 프록시가 프록시를 감싸는 경우까지만 푼다. 무한 루프 방지.
MAX_UNWRAP = 3

# 네이버 뉴스 이미지 경로: /image/{oid}/{yyyy}/{mm}/{dd}/{aid}_...
# origin(원본 크기) 등 중간 경로가 끼는 변형이 있어 사이를 느슨하게 둔다.
_NAVER_NEWS_PATH = re.compile(
    r"/image/(?:origin/)?(\d{3})/\d{4}/\d{2}/\d{2}/(\d{6,})_"
)
_NAVER_NEWS_HOSTS = ("imgnews.naver.net", "imgnews.pstatic.net", "mimgnews.pstatic.net")
NAVER_NEWS_SERVICE_NAME = "네이버 뉴스"

# 기사 페이지가 언론사명을 내주는 자리. 일반 기사는 여기에 "연합뉴스 | 네이버"처럼 적힌다
# (실측 2026-08-11). 연예·스포츠 기사에는 없고, 그때는 언론사명 없이 기사 주소만 남는다.
_PRESS_META = re.compile(
    r'<meta[^>]+property="og:article:author"[^>]+content="([^"]+)"', re.IGNORECASE
)
ARTICLE_LOOKUP_TIMEOUT = 4.0

# 유튜브가 영상마다 밝히는 라이선스(videos.list status.license). 두 값뿐이다.
YOUTUBE_LICENSE_STANDARD = "youtube"
YOUTUBE_LICENSE_CREATIVE_COMMONS = "creativeCommon"

_CC_BY_URL = "https://creativecommons.org/licenses/by/3.0/"
_YOUTUBE_TERMS_URL = "https://www.youtube.com/static?template=terms"


def host_of(url: str) -> str:
    """URL의 호스트. 읽을 수 없으면 빈 문자열 — 주소가 아닌 값을 이름처럼 쓰지 않는다."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    host = (parsed.netloc or "").strip().lower().split("@")[-1].split(":")[0]
    return host


def is_public_url(url: str | None) -> bool:
    """밖에서도 열리는 http(s) 주소인가. data URL·내부 스킴(user-upload://)은 아니다."""
    return bool((url or "").strip().startswith(("http://", "https://")))


def unwrap_proxy(url: str) -> str:
    """프록시·썸네일 주소에 실려 있는 원본 주소를 꺼낸다. 없으면 받은 주소 그대로.

    네이버 `search.pstatic.net/common/?src=http%3A%2F%2F…`와 다음
    `img1.daumcdn.net/thumb/R658x0.q70/?fname=http%3A%2F%2F…`가 이 형태다. 풀지 않으면
    출처가 '네이버'·'다음'이 되는데, 그 사진의 실제 출처는 감싸인 쪽이다.
    """
    current = (url or "").strip()
    for _ in range(MAX_UNWRAP):
        try:
            query = parse_qs(urlparse(current).query)
        except ValueError:
            return current
        nested = ""
        for name in PROXY_PARAMS:
            values = query.get(name) or []
            candidate = unquote(values[0]).strip() if values else ""
            if candidate.startswith(("http://", "https://")):
                nested = candidate
                break
        if not nested or nested == current:
            return current
        current = nested
    return current


def registrable_domain(host: str) -> str:
    """호스트에서 등록 도메인만 남긴다(i2.ruliweb.com → ruliweb.com).

    두 칸이 기본이고, co.kr처럼 두 칸이 접미사인 경우에만 세 칸을 남긴다. 공개 접미사
    목록 전체를 들고 있지는 않으므로 완벽하지 않다 — 다만 여기서 하려는 일(파일 서버
    하위 도메인 걷어내기)에는 충분하고, 틀려도 CDN 호스트명보다 나쁘지 않다.
    """
    labels = [label for label in (host or "").split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _service_name(host: str) -> str:
    if _NAVER_SHOP_HOST.match(host):
        return "네이버 쇼핑"
    for suffix, name in SERVICE_BY_HOST_SUFFIX:
        if host == suffix or host.endswith("." + suffix):
            return name
    return ""


def site_name_of(url: str) -> str:
    """주소 하나가 가리키는 **사이트·서비스 이름**. 읽을 수 없으면 빈 문자열.

    프록시를 먼저 풀고, 아는 서비스면 그 이름을, 아니면 등록 도메인을 준다. 등록 도메인은
    사실이다 — 그것을 언론사·브랜드 이름으로 바꿔 적는 순간 추측이 된다.
    """
    host = host_of(unwrap_proxy(url or ""))
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return _service_name(host) or registrable_domain(host) or host


def display_source_name(*, page_url: str | None = None, image_url: str | None = None) -> str:
    """이 이미지가 '어디에 실려 있었나'를 사람이 읽는 이름으로.

    원문 페이지를 아는 경우 그쪽 호스트가 먼저다(이미지 CDN은 게시자가 아니다). 아는
    서비스는 그 이름으로, 나머지는 등록 도메인이다 — CDN 하위 도메인(i2.ruliweb.com)을
    그대로 적으면 사이트 이름이 아니고, 언론사 이름으로 바꿔 적으면 추측이 된다.
    """
    for candidate in (page_url, image_url):
        name = site_name_of(candidate or "")
        if name:
            return name
    return ""


def naver_news_article(url: str) -> str | None:
    """네이버 뉴스 이미지 주소에서 기사 페이지 주소를 되만든다. 아니면 None.

    되만들기만 한다 — 이 주소가 실제로 열리는지는 ``naver_news_origin``이 확인한다.
    """
    target = unwrap_proxy(url or "")
    host = host_of(target)
    if not any(host == known or host.endswith("." + known) for known in _NAVER_NEWS_HOSTS):
        return None
    match = _NAVER_NEWS_PATH.search(urlparse(target).path)
    if not match:
        return None
    return f"https://n.news.naver.com/article/{match.group(1)}/{match.group(2)}"


# 언론사 코드(oid) → (기사 주소가 열렸는가, 언론사명). 언론사는 기사마다 바뀌지 않으므로
# 코드당 한 번만 확인하면 된다. 프로세스 메모리라 재시작하면 다시 확인한다.
_press_names: dict[str, str] = {}


async def naver_news_origin(image_url: str) -> tuple[str | None, str]:
    """네이버 뉴스 사진의 (확인된 기사 주소, 언론사명).

    기사 주소는 **실제로 열어 200을 받은 경우에만** 돌려준다 — 되만든 주소를 확인 없이
    적으면 없는 페이지를 출처로 다는 셈이다. 언론사명도 추측하지 않고 그 페이지의
    ``og:article:author``에서 읽는다(연예·스포츠 기사에는 없어 빈 문자열이 된다).

    뉴스 사진이 아니거나 확인에 실패하면 ``(None, "")``. 실패가 사진을 버릴 이유는
    아니므로 예외를 올리지 않는다 — 출처 이름은 '네이버 뉴스'로 남는다.
    """
    article = naver_news_article(image_url)
    if not article:
        return (None, "")

    oid = article.rsplit("/", 2)[-2]
    cached = _press_names.get(oid)

    try:
        response = await shared_client().get(
            article,
            timeout=ARTICLE_LOOKUP_TIMEOUT,
            follow_redirects=True,
            headers={"user-agent": "Mozilla/5.0 (compatible; Blog-it/1.0)"},
        )
    except (httpx.HTTPError, ValueError) as error:
        logger.debug("기사 확인 실패(무시) %s: %s", article, error)
        return (None, "")

    if response.is_error:
        # 되만든 주소가 열리지 않는다. 없는 페이지를 출처로 달지 않는다.
        logger.debug("되만든 기사 주소가 열리지 않음 %s: %d", article, response.status_code)
        return (None, "")

    if cached is not None:
        return (article, cached)
    found = _PRESS_META.search(response.text)
    # "연합뉴스 | 네이버" — 앞의 언론사명만 쓴다.
    name = found.group(1).split("|")[0].strip() if found else ""
    _press_names[oid] = name
    return (article, name)


def youtube_license(status_license: str | None) -> tuple[str | None, str | None, str]:
    """유튜브 영상의 라이선스 → (표기, 확인 페이지, 이용 가능 여부).

    영상이 CC BY로 공개돼 있으면 그것은 **확인된 사실**이라 allowed로 적는다. 표준
    라이선스는 재사용이 열려 있지 않으므로 restricted다. 값을 못 받았으면(세부정보 조회를
    건너뛴 경로) unknown — 모르는 것을 둘 중 하나로 정하지 않는다.
    """
    value = (status_license or "").strip()
    if value == YOUTUBE_LICENSE_CREATIVE_COMMONS:
        return ("크리에이티브 커먼즈 저작자 표시(CC BY)", _CC_BY_URL, IMAGE_USAGE_ALLOWED)
    if value == YOUTUBE_LICENSE_STANDARD:
        return ("표준 YouTube 라이선스", _YOUTUBE_TERMS_URL, IMAGE_USAGE_RESTRICTED)
    return (None, None, IMAGE_USAGE_UNKNOWN)


def usage_status_for(license_name: str | None) -> str:
    """라이선스 표기만 있을 때의 이용 가능 여부.

    **확인되지 않으면 언제나 unknown이다.** 여기서 allowed가 나오는 것은 표기가 공개
    라이선스를 스스로 밝힌 경우뿐이다 — 표기가 없다는 것은 '자유롭게 써도 된다'는 뜻이
    아니라 '모른다'는 뜻이다.
    """
    text = (license_name or "").strip().lower()
    if not text:
        return IMAGE_USAGE_UNKNOWN
    if any(
        marker in text
        for marker in ("cc by", "cc0", "creative commons", "public domain", "퍼블릭 도메인")
    ):
        return IMAGE_USAGE_ALLOWED
    if any(
        marker in text
        for marker in ("all rights reserved", "무단전재", "무단 전재", "재배포 금지")
    ):
        return IMAGE_USAGE_RESTRICTED
    return IMAGE_USAGE_UNKNOWN


def generated_image_source() -> ImageSourceInfo:
    """이미지 모델이 그린 사진의 출처. **외부 출처를 붙이지 않는다.**

    우리가 만든 이미지라 남의 사이트 이름이 들어갈 자리가 없다. 화면은 이 값을 보고
    'AI 생성 이미지'로만 구분한다.
    """
    return ImageSourceInfo(
        source_type=IMAGE_SOURCE_TYPE_GENERATED,
        source_name="",
        usage_status=IMAGE_USAGE_ALLOWED,
    )


def web_photo_image_source(photo, *, original_image_url: str | None = None) -> ImageSourceInfo:
    """가져온 사진 한 장의 구조화된 출처.

    ``photo``는 ``WebPhoto``다(순환 import를 피하려고 타입을 강제하지 않는다). 검색 단계가
    확인해 둔 값을 그대로 옮기고, 비어 있는 것은 비워 둔다 — 이 함수는 사실을 옮기는
    자리이지 채우는 자리가 아니다.

    ``original_image_url``은 호출부가 이미 계산해 둔 **밖에서도 열리는 이미지 주소**다
    (유튜브는 watch 주소가 아니라 i.ytimg 썸네일 주소라 호출부만 안다). 주지 않으면
    사진의 이미지 주소가 공개 주소일 때만 쓴다.
    """
    page_url = (getattr(photo, "source_page_url", "") or "").strip() or None
    image_url = (getattr(photo, "source_url", "") or "").strip()
    if photo.source_type == WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL and not page_url:
        # 유튜브는 source_url이 곧 영상(원문) 페이지다.
        page_url = image_url if is_public_url(image_url) else None

    if original_image_url is None and is_public_url(image_url):
        # 유튜브가 아닌 웹 이미지에서는 source_url이 이미지 파일 주소 그대로다.
        original_image_url = (
            None if photo.source_type == WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL else image_url
        )

    name = (getattr(photo, "source_name", "") or "").strip()
    if not name:
        name = display_source_name(page_url=page_url, image_url=image_url)

    license_name = (getattr(photo, "license", None) or "").strip() or None
    license_url = (getattr(photo, "license_url", None) or "").strip() or None
    status = (getattr(photo, "usage_status", "") or "").strip() or IMAGE_USAGE_UNKNOWN

    return ImageSourceInfo(
        source_type=IMAGE_SOURCE_TYPE_EXTERNAL,
        source_name=name,
        source_page_url=page_url if is_public_url(page_url) else None,
        original_image_url=original_image_url if is_public_url(original_image_url) else None,
        license=license_name,
        license_url=license_url,
        usage_status=status,
    )
