"""YouTube Data API v3 — 두 가지 다른 질문을 던진다.

최신순(TRENDING)은 `chart=mostPopular`, 곧 유튜브 자체의 트렌드 피드를 읽는다. 창작자가
그 영상들에 단 태그가 유튜브가 노출하는 것 중 트렌드 키워드 목록에 가장 가깝다; 제목은
태그 없이 게시된 영상의 대체 수단이다.

소재 관련순(MATERIAL_RELATED)은 인기 차트로는 답할 수 없는 질문이다 — 오늘 한국에서 가장
많이 본 영상 50개에 '배틀그라운드 감도 설정'이 들어 있을 이유가 없다. 그래서 `search.list`로
소재를 직접 검색해 그 소재를 다루는 영상을 모으고, 같은 방식으로 태그·제목에서 키워드를
캐낸다. 두 경로가 쓰는 추출·집계 로직은 같고, 어떤 영상 집합을 보느냐만 다르다.

할당량 주의: `search.list`는 호출당 100 units로 `videos.list`(1 unit)의 100배다. 기본 일
할당량 10,000 기준으로 소재 수집은 하루 약 100회가 한계이므로, 소재 풀을 DB에 누적해
같은 소재를 다시 검색하지 않는 것이 이 소스를 살려 두는 전제다(material_store.py).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.llm.contracts import TrendFetchInput
from app.llm.youtube_api import DEFAULT_YOUTUBE_API_REFERRER, api_headers
from app.shared import (
    TrendEvidenceOrigin,
    TrendMode,
    TrendSource,
    TrendSourceEvidence,
    YouTubeTrendEvidence,
)

from .base import REQUEST_TIMEOUT, CollectedKeyword, seed_queries
from .text import (
    compact_for_match,
    count_keywords,
    match_terms,
    material_phrases,
    mentions_keyword,
    to_collected,
    tokenize,
)

YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
MAX_RESULTS = 50

# 소재 관련순 근거의 "최근 관련 영상" 집계 창(일). 화면의 '최근 7일 관련 영상 N개'가 이 값이다.
RECENT_WINDOW_DAYS = 7

# 7일 안에 아무것도 없을 때 넓혀 보는 창. 오래된 소재('참이슬'처럼 관련 영상이 대부분 몇 년
# 전인 것)에서 '최근 7일 관련 영상 없음'만 반복되는 것을 막는다 — 창을 넓혔다는 사실은
# 카드에 그대로 적힌다("최근 30일 관련 영상 12개").
WIDE_WINDOW_DAYS = 30

# 트렌드로 세기 전에 50개 트렌드 영상 중 몇 개가 한 태그를 공유해야 하는지. 1이면
# MV 하나의 태그 목록이 패널에 올라온다("BOY", "DON", "GIRL"); 2면 아티스트가 영상과
# 그 공연 클립을 올리는 것으로 충분하다. 세 개의 업로드가 같은 단어로 수렴하면 하나의
# 주제다.
MIN_VIDEOS_PER_KEYWORD = 3

# 소재 검색에서는 문턱을 1로 낮춘다. 인기 차트는 서로 무관한 영상 50개의 모음이라 여러
# 영상이 같은 단어로 수렴하는 것이 '주제'라는 신호지만, 소재 검색 결과는 이미 전부 같은
# 소재의 영상이다 — 여기서 3개를 요구하면 소재의 세부 주제(감도·사양·대회)가 전멸한다.
MIN_VIDEOS_PER_MATERIAL_KEYWORD = 1


@dataclass
class _VideoInfo:
    """수집한 영상 하나의 근거 계산에 필요한 조각. 통계가 없는 영상은 필드가 None이다."""

    video_id: str | None
    title: str
    text: str
    # 태그·제목을 공백·기호 없이 붙여 놓은 대조용 표기. 키워드가 이 영상에서 확인됐는지를
    # 조사·띄어쓰기에 걸리지 않고 판정한다(text.mentions_keyword).
    compact: str
    view_count: int | None
    published_at: datetime | None


def _parse_published_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_view_count(statistics: object) -> int | None:
    if not isinstance(statistics, dict):
        return None
    raw = statistics.get("viewCount")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _elapsed_hours(observed_at: datetime, published_at: datetime) -> float:
    """게시 후 경과시간(시간). 1시간 하한 — 방금 올라온 영상의 조회 속도가 무한대로 튀지 않게."""
    return max((observed_at - published_at).total_seconds() / 3600, 1.0)


class YouTubeTrendCollector:
    source = TrendSource.YOUTUBE

    def __init__(self, api_key: str, referrer: str | None = DEFAULT_YOUTUBE_API_REFERRER):
        self._api_key = api_key
        # 키에 걸린 HTTP 리퍼러 제한을 통과시키는 헤더. 제한이 없는 키에서는 무시된다.
        self._headers = api_headers(referrer)

    async def collect(
        self,
        trend_input: TrendFetchInput,
        limit: int | None,
        known: frozenset[str] = frozenset(),
    ) -> list[CollectedKeyword]:
        material = trend_input.mode == TrendMode.MATERIAL_RELATED
        # 헤더는 클라이언트에 한 번 건다 — 이 아래 세 호출(videos·search·videos)이 모두
        # 이 클라이언트를 쓰므로, 한 곳만 빠뜨려 그 경로만 403이 되는 일이 없다.
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=self._headers) as client:
            if material:
                items = await self._material_videos(client, trend_input)
            else:
                items = await self._most_popular(client, trend_input)

        if not items:
            return []

        # 중복 영상은 videoId 기준으로 제거한다 — 같은 영상이 두 번 오면 근거 계산
        # (최근 영상 수·평균)이 부풀려진다. id가 없는 항목은 그대로 둔다.
        videos: list[_VideoInfo] = []
        seen_ids: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            video_id = item.get("id") if isinstance(item.get("id"), str) else None
            if video_id and video_id in seen_ids:
                continue
            if video_id:
                seen_ids.add(video_id)

            snippet = item.get("snippet")
            snippet = snippet if isinstance(snippet, dict) else {}
            tags = snippet.get("tags")
            tags = [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []
            title = snippet.get("title")
            title = title if isinstance(title, str) else ""

            # 태그가 깨끗한 신호다 — 창작자가 맨 명사로 적는다. 다만 소재 검색에서는 제목도
            # 함께 읽는다: 검색 결과 영상은 이미 소재가 확정돼 있어 제목이 세부 주제를
            # 그대로 드러내고("배틀그라운드 감도 설정 완벽 정리"), 태그만 보면 그 세부가 사라진다.
            if material:
                text = " ".join([*tags, title]).strip()
            else:
                text = " ".join(tags) if tags else title

            videos.append(
                _VideoInfo(
                    video_id=video_id,
                    title=title,
                    text=text,
                    compact=compact_for_match(text),
                    view_count=_parse_view_count(item.get("statistics")),
                    published_at=_parse_published_at(snippet.get("publishedAt")),
                )
            )

        if not videos:
            return []

        total = len(videos)
        # 트렌드 차트 1위 영상이 50위 영상보다 더 많은 것을 말해준다. 소재 검색도
        # 관련도순으로 받으므로 같은 가중이 그대로 의미가 있다.
        weights = [1.0 + (total - index) / total for index in range(total)]

        # 소재 검색에서는 명사구를 캔다. 최신순은 단일 명사가 맞다 — 인기 차트의 태그는
        # 서로 무관한 영상들이 공유하는 '주제어'라 한 단어일수록 신호가 선명하다. 반면
        # 소재 검색 결과는 전부 같은 소재의 영상이라, 단일 명사로 쪼개면 '감도 설정'이
        # '감도'와 '설정'으로 흩어져 검색 의도가 사라진다(네이버 발굴과 같은 이유).
        # concrete_phrases가 아니라 material_phrases인 이유는 그 함수의 docstring 참고 —
        # 앵커 어휘를 요구하면 목록에 없는 소재에서 후보가 전멸한다.
        extractor = material_phrases if material else tokenize
        # 영상마다 한 번만 추출해 순위 집계와 키워드→영상 매핑이 같은 결과를 쓴다 —
        # 추출은 CPU 작업이라 두 번 돌리면 그대로 두 배가 된다.
        extracted = [extractor(video.text) for video in videos]

        counts = count_keywords(
            extracted,
            weights=weights,
            exclude=seed_queries(trend_input),
            extractor=lambda tokens: tokens,
            min_documents=(
                MIN_VIDEOS_PER_MATERIAL_KEYWORD if material else MIN_VIDEOS_PER_KEYWORD
            ),
        )
        collected = to_collected(counts, limit, known)

        # 각 키워드가 어떤 영상에서 확인됐는지. 판정은 추출 결과가 아니라 태그·제목 본문
        # 대조다 — 추출기는 조사가 붙은 제목 표기를 버리므로 그것으로 세면 실제보다 적게
        # 잡힌다(text.mentions_keyword). 근거는 이미 받아 둔 영상 묶음에서만 계산하고,
        # 키워드마다 추가 API 호출을 만들지 않는다.
        observed_at = datetime.now(timezone.utc)
        for item in collected:
            terms = match_terms(item.keyword)
            found = [video for video in videos if mentions_keyword(terms, video.compact)]
            item.evidence = self._evidence(found, material, observed_at)
        return collected

    def _evidence(
        self, found: list[_VideoInfo], material: bool, observed_at: datetime
    ) -> TrendSourceEvidence | None:
        """키워드가 발견된 영상들에서 조회 근거를 계산한다.

        대표 영상은 실제 viewCount가 가장 높은 영상이다. 평균 조회 속도는 누적 조회수를
        게시 후 경과시간으로 나눈 값이라 '실시간 조회 속도'가 아니며, 화면 문구도 반드시
        '업로드 후 시간당 평균'으로 쓴다. 날짜나 조회수가 없는 영상은 평균에서 제외하고,
        아무 지표도 계산할 수 없으면 근거를 만들지 않는다(None) — 0을 지어내지 않는다.
        """
        if not found:
            return None

        with_views = [video for video in found if video.view_count is not None]
        top = max(with_views, key=lambda video: video.view_count or 0, default=None)

        rated = [
            (video.view_count or 0) / _elapsed_hours(observed_at, video.published_at)
            for video in with_views
            if video.published_at is not None
        ]
        if material:
            average = round(sum(rated) / len(rated), 1) if rated else None

            def within(days: int) -> int:
                cutoff = observed_at - timedelta(days=days)
                return sum(
                    1 for video in found if video.published_at and video.published_at >= cutoff
                )

            # 7일이 기본이고, 그 안에 하나도 없으면 30일로 넓혀 다시 센다. 넓혔다는 사실은
            # 숨기지 않고 창 크기(recent_window_days)로 함께 실어 보내 카드가 "최근 30일
            # 관련 영상 12개"라고 적게 한다 — 7일이라고 적어 놓고 30일치를 세지 않는다.
            recent_window = RECENT_WINDOW_DAYS
            recent_count = within(RECENT_WINDOW_DAYS)
            if recent_count == 0:
                wide = within(WIDE_WINDOW_DAYS)
                if wide:
                    recent_window = WIDE_WINDOW_DAYS
                    recent_count = wide
        else:
            # 최신순 카드는 대표 영상 하나의 조회 속도를 말한다.
            average = (
                round((top.view_count or 0) / _elapsed_hours(observed_at, top.published_at), 1)
                if top is not None and top.published_at is not None
                else None
            )
            recent_count = None

        youtube = YouTubeTrendEvidence(
            top_video_id=top.video_id if top else None,
            top_video_title=top.title if top else None,
            top_view_count=top.view_count if top else None,
            top_video_published_at=(
                top.published_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
                if top is not None and top.published_at is not None
                else None
            ),
            average_views_per_hour=average,
            recent_video_count=recent_count,
            recent_window_days=recent_window if material else None,
        )
        if all(
            value is None
            for value in (
                youtube.top_view_count,
                youtube.average_views_per_hour,
                youtube.recent_video_count,
            )
        ):
            return None
        return TrendSourceEvidence(
            source=TrendSource.YOUTUBE,
            # 경과시간 계산에 쓴 그 시각을 그대로 적는다 — 지표와 관측 시각이 어긋나지 않게.
            observed_at=observed_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            data_origin=TrendEvidenceOrigin.YOUTUBE_API,
            youtube=youtube,
        )

    async def _most_popular(
        self, client: httpx.AsyncClient, trend_input: TrendFetchInput
    ) -> list[dict]:
        """최신순 경로 — 유튜브 인기 차트. 같은 한 번의 호출에 statistics를 함께 받아
        조회수 근거를 만든다(part만 넓혔을 뿐 호출 수·쿼터 비용은 그대로 1 unit이다)."""
        response = await client.get(
            YOUTUBE_VIDEOS_URL,
            params={
                "key": self._api_key,
                "part": "snippet,statistics",
                "chart": "mostPopular",
                "regionCode": trend_input.country or "KR",
                "maxResults": str(MAX_RESULTS),
            },
        )
        return self._items(response)

    async def _material_videos(
        self, client: httpx.AsyncClient, trend_input: TrendFetchInput
    ) -> list[dict]:
        """소재 관련순 경로 — 소재를 검색해 그 소재를 다루는 영상을 모은다.

        search.list는 스니펫에 태그를 주지 않으므로 videos.list로 한 번 더 조회한다
        (id를 한 번에 넘겨 호출 1회, 1 unit). 태그가 소재의 세부 주제를 가장 잘 드러내는
        신호라 이 추가 호출은 값어치가 있다.
        """
        seeds = seed_queries(trend_input)
        if not seeds:
            return []

        video_ids = await self._search_ids(client, trend_input, seeds[0])
        if not video_ids:
            return []

        items = await self._details(client, video_ids)

        # 최근 영상이 하나도 없으면 한 번 더 찾는다 — 짧은 형식(쇼츠)으로.
        #
        # 관련도순 검색은 소재를 가장 잘 다루는 영상을 주지만, '참이슬'처럼 오래된 소재는
        # 그 상위가 몇 년 전 CF·리뷰다. 그래서 카드마다 '최근 7일 관련 영상 없음'만 떴다.
        # 요즘 올라오는 것은 대개 짧은 형식이라, 그쪽을 따로 물어보면 최근 영상이 잡힌다.
        # 관련도순이라는 발굴 방식은 그대로 두고, **보태는** 검색이다.
        if not self._has_recent(items, RECENT_WINDOW_DAYS):
            recent_ids = await self._search_ids(
                client,
                trend_input,
                seeds[0],
                video_duration="short",
                published_after=RECENT_WINDOW_DAYS,
            )
            fresh = [video_id for video_id in recent_ids if video_id not in set(video_ids)]
            if fresh:
                items += await self._details(client, fresh)

        return items

    async def _search_ids(
        self,
        client: httpx.AsyncClient,
        trend_input: TrendFetchInput,
        query: str,
        video_duration: str | None = None,
        published_after: int | None = None,
    ) -> list[str]:
        params = {
            "key": self._api_key,
            "part": "snippet",
            "q": query,
            "type": "video",
            # 인기순이 아니라 관련도순 — 소재를 실제로 다루는 영상이 필요하지, 그
            # 소재를 스치듯 언급한 인기 영상이 필요한 게 아니다.
            "order": "relevance",
            "regionCode": trend_input.country or "KR",
            "relevanceLanguage": "ko",
            "maxResults": str(MAX_RESULTS),
        }
        if video_duration:
            params["videoDuration"] = video_duration
        if published_after:
            since = datetime.now(timezone.utc) - timedelta(days=published_after)
            params["publishedAfter"] = since.isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
        response = await client.get(YOUTUBE_SEARCH_URL, params=params)
        return [
            item["id"]["videoId"]
            for item in self._items(response)
            if isinstance(item.get("id"), dict) and isinstance(item["id"].get("videoId"), str)
        ]

    async def _details(self, client: httpx.AsyncClient, video_ids: list[str]) -> list[dict]:
        """search.list는 태그·통계를 주지 않으므로 videos.list로 한 번 더 조회한다
        (id를 한꺼번에 넘겨 호출 1회, 1 unit)."""
        detail = await client.get(
            YOUTUBE_VIDEOS_URL,
            params={
                "key": self._api_key,
                # statistics를 같은 호출에 함께 받는다 — 키워드마다 추가 호출을 만들지 않는다.
                "part": "snippet,statistics",
                "id": ",".join(video_ids),
                "maxResults": str(MAX_RESULTS),
            },
        )
        return self._items(detail)

    @staticmethod
    def _has_recent(items: list[dict], days: int) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for item in items:
            snippet = item.get("snippet") if isinstance(item, dict) else None
            published = _parse_published_at(
                (snippet or {}).get("publishedAt") if isinstance(snippet, dict) else None
            )
            if published is not None and published >= cutoff:
                return True
        return False

    @staticmethod
    def _items(response: httpx.Response) -> list[dict]:
        if response.is_error:
            raise RuntimeError(
                f"YouTube request failed with {response.status_code}: {response.text}"
            )
        payload = response.json() if response.text else None
        items = payload.get("items") if isinstance(payload, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
