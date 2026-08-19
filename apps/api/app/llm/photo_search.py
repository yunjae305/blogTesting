"""소재의 실제 사진을 웹에서 찾아온다(네이버 이미지 검색).

왜 필요한가. 이미지 모델은 실존 인물의 얼굴을 재현하지 못한다 — '프로미스나인 백지헌'을
아무리 강하게 요구해도 나오는 것은 그럴듯한 남이다. 이름을 프롬프트에 관통시키는
작업(m5-image@v3.2·v3.3)까지 마친 뒤에도 남은 문제가 그것이고, 이름만으로 못 그리는 것은
프롬프트로 풀 수 없다. 그래서 방향을 바꾼다: **그리지 말고 가져온다.**

가져온 사진은 생성 입력(image-to-image 기준)이 아니라 **결과 그 자체**로 쓴다. 편집
입력으로 넣으면 모델이 다시 '닮은 남'을 그리기 때문이다. 썸네일은 이 사진 위에 기존
render_thumbnail이 한글 문구를 얹고, 본문 사진은 to_canvas가 규격만 맞춘다.

출처. 네이버 이미지 검색은 이미지가 실린 페이지 URL을 주지 않고 이미지 URL(link)만 준다.
그 URL의 호스트는 대개 CDN(imgnews.naver.net·shop-phinf.pstatic.net)이라 사이트 이름이
아니므로, 이미지 주소에서 되찾을 수 있는 실제 원본 출처를 함께 담는다(image_origin,
2026-08-11 사용자 지시). 저작권·초상권은 이 코드가 판단할 수 있는 것이 아니므로,
**가져온 사진에는 언제나 출처 캡션을 붙인다** — 출처 없이 실리는 경로를 만들지 않는다.

2026-08-11부터 출처를 문자열이 아니라 **값으로** 들고 나간다(WebPhoto의 source_name·
source_page_url·license·usage_status, 판정은 app.llm.image_origin). 검색 서비스 이름을
출처로 적지 않는다는 원칙과, 확인할 수 없는 것은 비워 둔다는 원칙은 그대로다 — 라이선스를
확인할 수 있는 경로는 유튜브(videos.list status.license)뿐이라 나머지는 usage_status가
unknown이다.

원문 페이지는 네이버 이미지 검색 응답에 없다. 다만 **네이버 뉴스 사진은 예외**다: 이미지
주소에 언론사 코드·기사 번호가 들어 있어 기사 페이지를 되만들 수 있고, 되만든 주소를
실제로 열어 200을 확인한 경우에만 원문으로 삼는다(image_origin.naver_news_origin).
확인되면 그 페이지가 밝힌 언론사명이 출처 이름이 되고, 확인되지 않으면 주소 없이
'네이버 뉴스'로 남는다 — 없는 페이지를 출처로 달지 않는다.

자격 증명은 트렌드 수집이 쓰는 네이버 검색 API와 같다(NAVER_CLIENT_ID/SECRET). 새로 발급할
것이 없고, '검색' API 권한이 이미 켜져 있어야 하는 것도 같다.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
from urllib.parse import urlparse

import httpx
from PIL import Image

from app.llm.http import shared_client
from app.llm.image_origin import (
    NAVER_NEWS_SERVICE_NAME,
    display_source_name,
    naver_news_origin,
    youtube_license,
)
from app.llm.naver_api import auth_headers, search_url
from app.llm.youtube_api import DEFAULT_YOUTUBE_API_REFERRER, api_headers
from app.shared import WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL, WebPhoto

logger = logging.getLogger(__name__)

# NAVER API HUB 이관(2026-08-11). 옛 개발자센터 주소로 부르면 401이다(naver_api 참고).
NAVER_IMAGE_SEARCH_URL = search_url("image")
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
# 재생 시간을 받으려면 한 번 더 물어야 한다. search.list는 호출당 100 units인데 이쪽은
# 1 unit이라, 쇼츠와 정규 회차를 가르는 값을 얻는 비용으로는 사실상 공짜다.
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

# 검색 결과를 몇 개까지 받아 볼 것인가. 후보가 많을수록 규격 미달(너무 작은 이미지)·
# 내려받기 실패를 넘기고 쓸 만한 것을 고를 여지가 생긴다. 네이버 display 상한은 100이다.
SEARCH_DISPLAY = 20
# 내려받기 상한. 이보다 큰 응답은 버린다 — 원고 문서에 data URL로 실리므로 무한정 받으면
# 저장·전송이 그대로 무거워진다(Mongo 문서 16MB 한계 보호).
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
# 규격 미달 하한(2026-08-03 네이버 권장 규격 기준). 썸네일은 720×720 정사각으로,
# 본문은 900×506으로 맞춰지므로 목표 대비 1.25배까지의 확대만 허용한다 — 그보다 작은
# 사진은 뭉개진다. 짧은 변 하한 = 720/1.25 = 576.
MIN_EDGE = 576
# 16:9로 자른 뒤 남는 가로 폭의 하한 = 900/1.25 = 720. 세로로 긴 보도 사진은 짧은 변
# 하한은 넘지만 잘라 놓으면 폭이 좁아 본문 규격으로 크게 늘어난다 — 자른 결과를
# 기준으로 봐야 실제로 쓸 수 있는 사진만 남는다.
MIN_CROPPED_WIDTH = 720
# 본문 규격(900×506)의 비율.
TARGET_RATIO = 16 / 9


def cropped_width(width: int, height: int) -> int:
    """이 사진을 16:9로 자르면 남는 가로 폭(px)."""
    if width <= 0 or height <= 0:
        return 0
    return width if width / height <= TARGET_RATIO else round(height * TARGET_RATIO)


# --- 구도 적합성(2026-08-05) ---
#
# 왜 필요한가. 예전에는 검색 관련도 순서에서 규격을 통과한 **첫 장**을 그대로 썼다. 그래서
# 세로로 긴 쇼핑몰 상세컷이 1위면 그것이 실렸고, 16:9로 자르는 순간 대상의 절반이 사라졌다
# (디올 가방에서 손잡이만 남은 사례). 관련도만 보고 구도를 보지 않은 것이 원인이다.
#
# 여기서 잴 수 있는 것은 픽셀이 말해 주는 것뿐이다 — 사진 안에 무엇이 찍혔는지는 모른다.
# 그래서 '잘랐을 때 얼마나 잃는가'(구도)와 '얼마나 큰가'(해상도), 그리고 검색 결과 제목이
# 질의와 얼마나 겹치는가(대상 일치)만 본다. 판정할 수 없는 것을 판정한 척하지 않는다.

# 미리보기·발행 규격의 비율(본문 16:9). 이 비율에서 멀수록 잘려 나가는 면적이 커진다.
# 세로로 긴 사진은 여기서 크게 감점되고, 3:2·4:3 가로 사진은 거의 감점되지 않는다.
FRAMING_WEIGHT = 55.0
RESOLUTION_WEIGHT = 25.0
SUBJECT_MATCH_WEIGHT = 20.0

# 이 점수를 넘으면 더 찾지 않고 쓴다. 넘는 후보가 없으면 받은 것 중 가장 높은 것을 쓴다 —
# 구도가 아쉽다고 사진 없이 발행하지는 않는다.
GOOD_COMPOSITION_SCORE = 70.0


def framing_score(width: int, height: int) -> float:
    """16:9로 잘랐을 때 남는 면적 비율(0~100). 세로로 길수록 낮다."""
    if width <= 0 or height <= 0:
        return 0.0
    ratio = width / height
    kept = TARGET_RATIO / ratio if ratio > TARGET_RATIO else ratio / TARGET_RATIO
    return max(0.0, min(1.0, kept)) * 100.0


def resolution_score(width: int, height: int) -> float:
    """자른 뒤 폭이 본문 규격(1200px)을 얼마나 채우는가(0~100)."""
    if width <= 0 or height <= 0:
        return 0.0
    return max(0.0, min(1.0, cropped_width(width, height) / 1200)) * 100.0


def subject_match_score(title: str, query: str) -> float:
    """검색 결과 제목이 질의의 낱말을 얼마나 담고 있는가(0~100).

    네이버가 주는 것은 이미지가 실린 문서의 제목뿐이라 정밀한 판정은 못 한다. 질의
    낱말이 하나도 없는 후보를 뒤로 미는 정도로만 쓴다. 질의를 쪼갤 수 없으면 중립(50).
    """
    terms = [_norm(term) for term in (query or "").split() if _norm(term)]
    if not terms:
        return 50.0
    haystack = _norm(title or "")
    if not haystack:
        return 50.0
    hits = sum(1 for term in terms if term in haystack)
    return hits / len(terms) * 100.0


def composition_score(photo: WebPhoto) -> float:
    """이 사진을 우리 규격에 실었을 때의 적합성(0~100). 높을수록 먼저 쓴다."""
    framing = framing_score(photo.width, photo.height)
    resolution = resolution_score(photo.width, photo.height)
    match = subject_match_score(photo.title, photo.query)
    return (
        framing * FRAMING_WEIGHT
        + resolution * RESOLUTION_WEIGHT
        + match * SUBJECT_MATCH_WEIGHT
    ) / (FRAMING_WEIGHT + RESOLUTION_WEIGHT + SUBJECT_MATCH_WEIGHT)
# 이미지 한 장 내려받기 제한 시간(초). 여기서 오래 끌면 원고 전체가 그만큼 늦는다.
DOWNLOAD_TIMEOUT = 12.0
# 동시에 내려받을 장수.
MAX_CONCURRENT_DOWNLOADS = 4

_TAG = re.compile(r"<[^>]+>")


def _plain(value: str | None) -> str:
    """네이버가 질의어에 <b> 태그를 씌워 돌려준다. 캡션에 태그가 실리지 않게 벗긴다."""
    return _TAG.sub("", value or "").strip()


def _video_id_of(item: dict) -> str:
    """검색 결과 항목의 영상 id. search.list는 id가 dict({"videoId": ...})이고
    videos.list는 문자열이라, 두 응답을 섞어 쓰는 자리에서는 여기서 흡수한다."""
    raw = item.get("id") if isinstance(item, dict) else None
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        video_id = raw.get("videoId")
        return video_id.strip() if isinstance(video_id, str) else ""
    return ""


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except ValueError:
        return url


# --- 공식 회차 판정(2026-08-03) ---
#
# 왜 필요한가. 검색 관련도 순서를 그대로 믿으면, 소재가 일반 명사와 이름이 같은 콘텐츠에서
# 전혀 다른 영상이 1위로 올라온다('전과자'로 검색하면 범죄 관련 영상). 팬캠·리액션·재업로드도
# 관련도는 높다. 실제 프로그램 자리에 남의 2차 창작이 실리면, 일반 생성 이미지가 실리던
# 문제가 '그럴듯한 남의 영상'으로 바뀔 뿐 고쳐지지 않는다.
#
# 판정은 **확인된 이름과의 관계로만** 한다 — 특정 프로그램·인물 문자열이 코드에 없다.
# 그리고 정식 명칭을 모르는 일반 카드(program_name 없음)에는 이 판정을 걸지 않는다:
# 무엇과 대조할지가 없는데 제외 목록만 걸면 멀쩡한 영상이 제목 때문에 떨어진다.
EXCLUDED_TITLE_MARKERS = (
    "리액션",
    "reaction",
    "팬영상",
    "팬 영상",
    "팬캠",
    "fancam",
    "재업로드",
    "reupload",
    "패러디",
    "parody",
)
# 공식 채널로 볼 만한 표시. 채널명이 프로그램명을 담고 있으면 그것이 가장 강한 신호다.
OFFICIAL_CHANNEL_MARKERS = ("공식", "official", "studio", "스튜디오")
# 후보가 통과해야 하는 최소 점수. 정식 명칭이 제목에 있으면(3점) 후보다.
#
# 2026-08-10 사용자 결정("실제 인물 사진 가져와도 된다 — 출처만 제대로 남기면 된다")로
# 5→3으로 내렸다: 공식 채널·출연자 확인(+2점씩)까지 요구하니 실존 인물·콘텐츠의 실사
# 썸네일이 사실상 전부 문턱에서 떨어졌다(실측: 스파이더맨 글에서 질의 12개 × 후보 20개
# 전탈락). 출처는 캡션(채널명·영상 주소)이 책임지고, 엉뚱한 피사체·비실사는 픽셀을
# 실제로 보는 웹 사진 판정 게이트가 거른다. 팬캠·리액션·재업로드·패러디 탈락과 정식
# 명칭 없는 영상 탈락(동명 일반명사 방어의 바닥)은 그대로다.
MIN_RELEVANCE_SCORE = 3
# 쇼츠 판정 기준(초). 정규 회차 영상을 쇼츠보다 먼저 쓴다.
SHORTS_MAX_SECONDS = 60

_NORM = re.compile(r"[^0-9a-z가-힣]")
_ISO_DURATION = re.compile(
    r"^P(?:\d+D)?T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?$"
)


def _norm(text: str) -> str:
    return _NORM.sub("", (text or "").lower())


def duration_seconds(value: str) -> int:
    """ISO-8601 재생 시간(PT12M34S)을 초로. 읽을 수 없으면 0(판정에 쓰지 않는다)."""
    match = _ISO_DURATION.match((value or "").strip())
    if not match:
        return 0
    return (
        int(match.group("h") or 0) * 3600
        + int(match.group("m") or 0) * 60
        + int(match.group("s") or 0)
    )


def score_candidate(
    video: dict,
    *,
    program_name: str,
    person_names: list[str] | None = None,
    official_channel: str = "",
) -> int:
    """이 영상이 그 콘텐츠의 공식 회차일 가능성. 음수면 후보가 아니다.

    - 정식 명칭이 제목이나 채널명에 있어야 후보다(없으면 즉시 탈락).
    - 주요 출연자 이름이나 공식 채널이 함께 확인되면 점수가 오른다.
    - 2차 창작·재업로드 표시가 제목에 있으면 탈락.
    - 쇼츠는 감점한다. 정규 회차가 있으면 그쪽을 먼저 쓴다.
    """
    person_names = person_names or []
    snippet = video.get("snippet") if isinstance(video, dict) else None
    snippet = snippet if isinstance(snippet, dict) else {}
    title = snippet.get("title") if isinstance(snippet.get("title"), str) else ""
    channel = (
        snippet.get("channelTitle") if isinstance(snippet.get("channelTitle"), str) else ""
    )
    description = (
        snippet.get("description") if isinstance(snippet.get("description"), str) else ""
    )

    if any(marker in title.lower() for marker in EXCLUDED_TITLE_MARKERS):
        return -1

    program_norm = _norm(program_name)
    if not program_norm:
        return -1
    title_norm = _norm(title)
    channel_norm = _norm(channel)

    score = 0
    if program_norm in title_norm:
        score += 3
    elif program_norm in channel_norm:
        score += 2
    elif any(
        _norm(name) and _norm(name) in title_norm for name in person_names
    ):
        # 정식 명칭은 길어서 제목에 통째로 들어가지 않는 콘텐츠가 흔하다 — 실측:
        # '마블 시네마틱 유니버스(MCU) 스파이더맨 실사 영화 시리즈'가 제목에 그대로
        # 담긴 영상은 없고, 그래서 후보 240개가 전멸했다(2026-08-10). 확인된 인물·
        # 짧은 이름(원본 검색어 포함, 호출부가 넘긴다)이 제목에 있으면 그 콘텐츠의
        # 영상으로 본다. 명칭도 이름도 없으면 여전히 후보가 아니다(동명 일반명사 방어).
        score += 3
    else:
        # 정식 명칭이 어디에도 없으면 그 콘텐츠의 영상이라고 볼 근거가 없다.
        return -1

    text_norm = _norm(f"{title} {description}")
    if any(_norm(name) and _norm(name) in text_norm for name in person_names):
        score += 2

    if (official_channel and _norm(official_channel) in channel_norm) or (
        program_norm in channel_norm
    ):
        score += 2
    elif any(marker in channel.lower() for marker in OFFICIAL_CHANNEL_MARKERS):
        score += 1

    details = video.get("contentDetails")
    duration = (
        duration_seconds(details.get("duration", "")) if isinstance(details, dict) else 0
    )
    if 0 < duration <= SHORTS_MAX_SECONDS:
        score -= 2
    return score


class PhotoSearchError(RuntimeError):
    """검색 자체가 실패했다. 호출부는 이것을 잡아 생성 경로로 되돌린다 — 사진을 못
    구한 것이 원고를 버릴 이유는 아니다."""


class NaverPhotoSearch:
    """네이버 이미지 검색 + 내려받기. 트렌드 수집기와 같은 자격 증명을 쓴다."""

    def __init__(self, client_id: str, client_secret: str):
        self._headers = auth_headers(client_id, client_secret)

    async def find_photos(self, query: str, limit: int = 1) -> list[WebPhoto]:
        """``query``의 실제 사진을 최대 ``limit`` 장. 못 찾으면 빈 목록이다.

        검색 관련도 순서를 바탕으로 하되, 한 묶음 안에서는 **구도 적합성**이 높은 것을
        먼저 쓴다(composition_score) — 세로로 긴 상세컷은 우리 규격으로 자르면 대상의
        절반이 사라지므로, 관련도만 보고 첫 장을 집으면 손잡이만 남은 사진이 실린다.
        구도가 충분한 후보를 못 찾으면 다음 묶음까지 보고, 끝내 없으면 그중 가장 나은
        것을 쓴다 — 구도가 아쉽다고 사진 없이 발행하지는 않는다.

        규격 미달·내려받기 실패는 건너뛰고 다음 후보로 넘어가며, 후보가 떨어지면 구한
        만큼만 돌려준다.
        """
        query = (query or "").strip()
        if not query or limit < 1:
            return []

        items = await self._search(query)
        if not items:
            logger.info("웹 사진 검색 결과 없음 | '%s'", query)
            return []

        gate = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        photos: list[WebPhoto] = []
        fallback: WebPhoto | None = None
        seen_hosts: set[str] = set()
        # 구도 문턱을 못 넘은 후보들. 문턱을 넘는 사진을 끝내 못 찾았을 때 쓴다.
        spare: list[tuple[float, WebPhoto]] = []

        async def take(item: dict) -> WebPhoto | None:
            async with gate:
                return await self._download(item, query)

        def accept(photo: WebPhoto) -> bool:
            """이 사진을 결과에 넣는다. 이미 채웠으면 False."""
            # 같은 출처의 사진만 나열되면 한 기사에서 잘라 온 연속컷이 되기 쉽다.
            # 여러 장이 필요할 때는 출처를 흩는다. 기준은 호스트가 아니라 확인한 출처
            # 이름이다(2026-08-11) — i2.ruliweb.com과 i3.ruliweb.com은 파일 서버만 다른
            # 같은 사이트인데 호스트로 세면 서로 다른 출처로 통과한다.
            key = photo.source_name or photo.source_host
            if limit > 1 and key in seen_hosts:
                return False
            seen_hosts.add(key)
            photos.append(photo)
            return True

        # 후보를 앞에서부터 묶음으로 내려받는다. limit장을 채우면 남은 후보는 건드리지
        # 않는다 — 한 장만 필요한데 20장을 받아 오는 낭비를 막는다.
        for start in range(0, len(items), MAX_CONCURRENT_DOWNLOADS):
            batch = items[start : start + MAX_CONCURRENT_DOWNLOADS]
            downloaded = await asyncio.gather(*(take(item) for item in batch))
            usable: list[tuple[float, int, WebPhoto]] = []
            for order, photo in enumerate(downloaded):
                if photo is None:
                    continue
                if not photo.meets_spec:
                    # 규격 미달은 직접 싣지 못한다. 다만 규격을 통과한 사진이 하나도
                    # 없으면 첫 장을 생성 참고용으로 돌려준다.
                    if fallback is None:
                        fallback = photo
                    continue
                usable.append((composition_score(photo), order, photo))
            # 구도 점수가 높은 것부터. 같은 점수면 검색 관련도 순서를 그대로 따른다.
            for score, _, photo in sorted(usable, key=lambda row: (-row[0], row[1])):
                if score < GOOD_COMPOSITION_SCORE:
                    spare.append((score, photo))
                    continue
                if not accept(photo):
                    continue
                if len(photos) >= limit:
                    logger.info(
                        "웹 사진 확보 | '%s' - %d장 (%s)",
                        query,
                        len(photos),
                        ", ".join(p.source_name or p.source_host for p in photos),
                    )
                    return photos

        # 구도 문턱을 넘은 사진이 모자라면 아쉬운 것으로 채운다. 점수가 높은 순서다.
        if len(photos) < limit and spare:
            logger.info(
                "웹 사진 구도 문턱 미달 - 가장 나은 후보로 채움 | '%s' (%d개 검토, 최고 %.0f점)",
                query,
                len(spare),
                max(score for score, _ in spare),
            )
            for _, photo in sorted(spare, key=lambda row: -row[0]):
                accept(photo)
                if len(photos) >= limit:
                    break

        if not photos and fallback is not None:
            logger.info(
                "웹 사진 규격 미달만 발견 | '%s' - 생성 참고용으로 씁니다 (%dx%d)",
                query,
                fallback.width,
                fallback.height,
            )
            return [fallback]
        logger.info("웹 사진 확보 | '%s' - %d/%d장", query, len(photos), limit)
        return photos

    async def _search(self, query: str) -> list[dict]:
        client = shared_client()
        try:
            response = await client.get(
                NAVER_IMAGE_SEARCH_URL,
                headers=self._headers,
                # filter=large가 아니면 후보 대부분이 목록용 축소본(200~400px)이라
                # 규격 검사에서 다 떨어진다.
                params={
                    "query": query,
                    "display": str(SEARCH_DISPLAY),
                    "sort": "sim",
                    "filter": "large",
                },
            )
        except httpx.HTTPError as error:
            raise PhotoSearchError(f"네이버 이미지 검색 호출 실패: {error}") from error

        if response.status_code in (401, 403):
            # 트렌드 수집과 같은 진단이다. 401은 둘 중 하나다: 키가 틀렸거나, 그 키의
            # Application에 이미지 검색 API가 선택돼 있지 않다. 옛 개발자센터 키를
            # 그대로 두면 여기서 죽으므로 어느 쪽인지 알 수 있게 적는다.
            raise PhotoSearchError(
                f"네이버 이미지 검색 인증 실패({response.status_code}). NAVER API HUB "
                f"콘솔에서 발급한 Client ID/Secret인지, 그 Application에 이미지 검색이 "
                f"선택돼 있는지 확인해야 한다: {response.text}"
            )
        if response.is_error:
            raise PhotoSearchError(
                f"네이버 이미지 검색 실패 {response.status_code}: {response.text}"
            )

        payload = response.json() if response.text else None
        items = payload.get("items") if isinstance(payload, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def _download(self, item: dict, query: str) -> WebPhoto | None:
        return await _download_photo(
            (item.get("link") or "").strip(), query, _plain(item.get("title"))
        )


async def _download_photo(link: str, query: str, title: str) -> WebPhoto | None:
    """이미지 URL 하나를 내려받아 규격을 검사한다. 못 쓰면 None — 다음 후보로 넘어간다.

    네이버·유튜브가 같은 검사를 공유한다: 어떤 소스에서 왔든 원고에 실리는 규격
    (썸네일 720×720 정사각·본문 1200×675)은 같기 때문이다.
    """
    if not link.startswith(("http://", "https://")):
        return None

    client = shared_client()
    try:
        response = await client.get(
            link,
            timeout=DOWNLOAD_TIMEOUT,
            follow_redirects=True,
            # 이미지 서버가 브라우저가 아닌 요청을 막는 일이 흔하다. Referer까지
            # 요구하는 곳은 그대로 실패하게 두고 다음 후보로 넘어간다.
            headers={"user-agent": "Mozilla/5.0 (compatible; Blog-it/1.0)"},
        )
    except httpx.HTTPError as error:
        logger.debug("웹 사진 내려받기 실패 %s: %s", link, error)
        return None

    if response.is_error or not response.content:
        return None
    if len(response.content) > MAX_DOWNLOAD_BYTES:
        logger.debug("웹 사진 용량 초과 %s: %d바이트", link, len(response.content))
        return None

    # 리다이렉트를 따라갔다면 실제로 그림을 받은 주소가 원본 주소다. 검색 결과가 준 주소를
    # 그대로 적으면 밖에서 열리지 않는 중간 주소가 '원본'으로 남는다.
    resolved = str(response.url) if str(response.url).startswith(("http://", "https://")) else link

    raw = response.content
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            fmt = (image.format or "").upper()
    except Exception as error:  # PIL은 형식마다 다른 예외를 던진다
        logger.debug("웹 사진 열기 실패 %s: %s", link, error)
        return None

    if fmt not in ("JPEG", "PNG", "WEBP"):
        return None

    # 규격 미달 사진도 버리지 않는다 — 원고에 직접 싣지는 못하지만(확대하면 뭉개진다)
    # 이미지 생성의 참고 이미지로는 쓸 수 있다(meets_spec=False, 2026-08-03 사용자 결정).
    meets_spec = (
        min(width, height) >= MIN_EDGE
        and cropped_width(width, height) >= MIN_CROPPED_WIDTH
    )
    mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[fmt]

    # 출처 이름은 파일이 놓인 CDN 호스트가 아니라 사이트·서비스 이름이다(2026-08-11).
    # 네이버 뉴스 사진은 이미지 주소에 언론사 코드·기사 번호가 들어 있어 기사 페이지를
    # 되만들 수 있는데, **열어서 200을 확인한 경우에만** 원문으로 삼는다. 확인되면 그
    # 페이지가 밝힌 언론사명이 곧 원출처이고, 확인되지 않으면 주소 없이 '네이버 뉴스'로
    # 남는다 — 없는 페이지를 출처로 달지 않는다.
    source_name = display_source_name(image_url=resolved)
    source_page_url = None
    if source_name == NAVER_NEWS_SERVICE_NAME:
        article, press = await naver_news_origin(resolved)
        if article:
            source_page_url = article
            source_name = press or source_name

    return WebPhoto(
        data_url=f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
        source_url=resolved,
        source_host=_host(resolved),
        title=title or query,
        width=width,
        height=height,
        query=query,
        meets_spec=meets_spec,
        source_name=source_name,
        source_page_url=source_page_url,
    )


class YouTubeThumbnailSearch:
    """유튜브 영상 검색 → 썸네일 이미지 확보. ``find_photos`` 계약은 네이버와 같다.

    영상·방송·무대·게임 플레이처럼 '영상 콘텐츠의 한 장면'이 어울리는 카드를 위한
    소스다. maxres 썸네일(1280×720)만 쓴다 — search 응답의 high(480×360)는 16:9 크롭
    하한(960px)에 못 미쳐 규격 검사에서 어차피 떨어진다. maxres가 없는 영상(404)은
    건너뛰고 다음 영상으로 넘어간다.

    자격 증명은 트렌드 수집이 쓰는 YOUTUBE_API_KEY 그대로다. search.list는 호출당
    쿼터 100단위로 비싸므로 질의 하나당 한 번만 부른다.
    """

    def __init__(self, api_key: str, referrer: str | None = DEFAULT_YOUTUBE_API_REFERRER):
        self._api_key = api_key
        # 키에 걸린 HTTP 리퍼러 제한을 통과시키는 헤더(youtube_api 참고). 이 클래스는
        # 공용 클라이언트(shared_client)를 쓰므로 호출마다 붙인다 — 클라이언트에 걸면
        # 네이버·이미지 내려받기 요청에까지 리퍼러가 새어 나간다.
        self._youtube_headers = api_headers(referrer)

    async def find_photos(
        self,
        query: str,
        limit: int = 1,
        *,
        program_name: str = "",
        person_names: list[str] | None = None,
        official_channel: str = "",
    ) -> list[WebPhoto]:
        """``query``의 영상 썸네일을 최대 ``limit`` 장.

        추가 인자는 전부 키워드 전용에 기본값이 있어 ``PhotoSearch`` 계약
        (``find_photos(query, limit)``)을 그대로 만족한다 — 소스 사다리는 예전처럼 부른다.

        ``program_name``이 주어지면(소재가 실제 영상 콘텐츠로 확인된 글) 검색 순서를
        그대로 믿지 않고 공식 회차인지 채점한다. 정식 명칭을 모르는 일반 카드에서는
        무엇과 대조할지가 없으므로 예전처럼 관련도 순서를 따른다.
        """
        query = (query or "").strip()
        if not query or limit < 1:
            return []

        items = await self._search(query, scored=bool(program_name.strip()))
        if not items:
            logger.info("유튜브 썸네일 검색 결과 없음 | '%s'", query)
            return []

        if program_name.strip():
            items = self._ranked(
                items,
                program_name=program_name,
                person_names=person_names,
                official_channel=official_channel,
                query=query,
            )
            if not items:
                return []

        gate = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        photos: list[WebPhoto] = []

        async def take(item: dict) -> WebPhoto | None:
            video_id = _video_id_of(item)
            if not video_id:
                return None
            snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
            title = _plain(snippet.get("title") or "")
            channel = _plain(snippet.get("channelTitle") or "")
            async with gate:
                photo = await _download_photo(
                    f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg", query, title
                )
            if photo is None:
                return None
            # 출처는 썸네일 파일 주소가 아니라 영상 자체로 남긴다 — 생성 기록에서
            # 어느 영상인지 바로 따라갈 수 있고, 카드 간 중복 배제 키로도 고유하다.
            # source_type을 함께 남기는 것이 중요하다: 이 표시가 없으면 뒤 단계가
            # 공식 썸네일을 알아보지 못해 그 위에 제목 박스를 덮고 좌우를 잘라낸다.
            watch_url = f"https://www.youtube.com/watch?v={video_id}"
            # 영상이 스스로 밝힌 라이선스. videos.list를 부른 경로(채점)에서만 온다 —
            # 값이 없으면 usage_status는 unknown으로 남는다(모르는 것을 정하지 않는다).
            status = item.get("status") if isinstance(item.get("status"), dict) else {}
            license_name, license_url, usage = youtube_license(status.get("license"))
            return photo.model_copy(
                update={
                    "source_url": watch_url,
                    "source_host": "youtube.com",
                    "source_type": WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL,
                    "channel_title": channel,
                    "video_id": video_id,
                    # 게시자는 CDN(i.ytimg.com)이 아니라 그 영상을 올린 채널이다.
                    # 채널명을 못 받은 영상도 최소한 'YouTube'로 남는다.
                    "source_name": channel or "YouTube",
                    # 원문 페이지가 실제로 존재하는 유일한 경로 — 영상 페이지 그 자체.
                    "source_page_url": watch_url,
                    "license": license_name,
                    "license_url": license_url,
                    "usage_status": usage,
                }
            )

        for start in range(0, len(items), MAX_CONCURRENT_DOWNLOADS):
            batch = items[start : start + MAX_CONCURRENT_DOWNLOADS]
            for photo in await asyncio.gather(*(take(item) for item in batch)):
                # maxres(1280×720)는 규격을 통과하지만, 혹시 더 작은 변형이 오면
                # 직접 싣지 않는다 — 참고용 폴백은 네이버 사다리가 맡는다.
                if photo is None or not photo.meets_spec:
                    continue
                photos.append(photo)
                if len(photos) >= limit:
                    logger.info(
                        "유튜브 썸네일 확보 | '%s' - %d장", query, len(photos)
                    )
                    return photos

        logger.info("유튜브 썸네일 확보 | '%s' - %d/%d장", query, len(photos), limit)
        return photos

    def _ranked(
        self,
        items: list[dict],
        *,
        program_name: str,
        person_names: list[str] | None,
        official_channel: str,
        query: str,
    ) -> list[dict]:
        """공식 회차일 가능성이 높은 것부터. 문턱에 못 미치는 후보는 버린다."""
        scored = [
            (
                score_candidate(
                    item,
                    program_name=program_name,
                    person_names=person_names,
                    official_channel=official_channel,
                ),
                index,
                item,
            )
            for index, item in enumerate(items)
        ]
        # 점수가 높은 것부터, 같으면 검색 관련도 순서를 그대로 따른다.
        kept = [
            item
            for score, _, item in sorted(scored, key=lambda row: (-row[0], row[1]))
            if score >= MIN_RELEVANCE_SCORE
        ]
        if not kept:
            logger.info(
                "유튜브 후보가 공식 회차 문턱을 못 넘음 - 다른 소스로 진행 | '%s' (%d개 검토)",
                query,
                len(items),
            )
        return kept

    async def _search(self, query: str, scored: bool = False) -> list[dict]:
        """검색 결과. ``scored``면 재생 시간까지 채운다(쇼츠와 정규 회차를 가르려면 필요).

        search.list는 호출당 100 units라 질의 하나당 한 번만 부른다. videos.list는
        1 unit이라, 채점이 필요할 때만 한 번 더 부르는 비용은 사실상 없다.
        """
        items = await self._search_list(query)
        if not scored or not items:
            return items
        return await self._with_details(items)

    async def _with_details(self, items: list[dict]) -> list[dict]:
        """search.list 결과에 videos.list의 재생 시간·설명을 채운다. 실패하면 원본 그대로."""
        video_ids = [vid for vid in (_video_id_of(item) for item in items) if vid]
        if not video_ids:
            return items
        client = shared_client()
        try:
            response = await client.get(
                YOUTUBE_VIDEOS_URL,
                timeout=10.0,
                headers=self._youtube_headers,
                params={
                    "key": self._api_key,
                    # status를 함께 받는다(2026-08-11). 영상이 스스로 밝힌 라이선스
                    # (표준 / CC BY)가 여기 있고, 이용 조건을 **사실로** 말할 수 있는
                    # 유일한 경로다. part를 늘려도 videos.list는 여전히 1 unit이다.
                    "part": "snippet,contentDetails,status",
                    "id": ",".join(video_ids),
                    "maxResults": str(SEARCH_DISPLAY),
                },
            )
            if response.is_error:
                return items
            payload = response.json() if response.text else None
        except (httpx.HTTPError, ValueError) as error:
            # 세부정보는 채점의 보조 신호다. 못 받으면 쇼츠 감점만 죽고 나머지 판정은 돈다.
            logger.debug("유튜브 영상 세부정보 조회 실패(무시): %s", error)
            return items

        detail = payload.get("items") if isinstance(payload, dict) else None
        by_id = {
            item.get("id"): item
            for item in (detail if isinstance(detail, list) else [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        # 검색 관련도 순서를 유지한다 — videos.list는 id 순서를 보장하지 않는다.
        return [by_id.get(_video_id_of(item), item) for item in items]

    async def _search_list(self, query: str) -> list[dict]:
        client = shared_client()
        try:
            response = await client.get(
                YOUTUBE_SEARCH_URL,
                timeout=10.0,
                headers=self._youtube_headers,
                params={
                    "key": self._api_key,
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "maxResults": str(SEARCH_DISPLAY),
                    "relevanceLanguage": "ko",
                    "regionCode": "KR",
                },
            )
        except httpx.HTTPError as error:
            raise PhotoSearchError(f"유튜브 검색 호출 실패: {error}") from error

        if response.status_code in (401, 403):
            # 키가 죽었거나 일일 쿼터(기본 10,000단위, search.list는 100단위/회)를 다
            # 쓴 경우다. 사진 없이 계속 가면 되므로 진단만 명확히 남긴다.
            raise PhotoSearchError(
                f"유튜브 API 권한/쿼터 오류({response.status_code}): {response.text[:200]}"
            )
        if response.is_error:
            raise PhotoSearchError(
                f"유튜브 검색 실패 {response.status_code}: {response.text[:200]}"
            )

        payload = response.json() if response.text else None
        items = payload.get("items") if isinstance(payload, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
