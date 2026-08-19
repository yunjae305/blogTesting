"""출처별 수집 근거(evidence): 수집기 보존 → 집계·병합 유지 → 캐시·DB 왕복.

카드의 3줄 지표는 전부 실제 API 응답에서 계산한 값이어야 한다. 이 파일은 그 값이
수집 시점에 만들어지고, 정규화·병합·직렬화를 거쳐도 사라지거나 지어내지지 않는지 본다.
"""

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import respx

from app.llm import TrendFetchInput
from app.llm.trends import (
    AggregateTrendProvider,
    CollectedKeyword,
    GoogleTrendsCollector,
    InMemoryPoolCache,
    MongoPoolCache,
    NaverTrendCollector,
    YouTubeTrendCollector,
)
from app.llm.trends.aggregate import _merge_pools, _rescore_pool
from app.llm.trends.cache import _decode, _encode
from app.llm.trends.google_trends import (
    increase_percentage,
    is_active,
    search_volume,
    started_at,
)
from app.llm.trends.text import compact_for_match, match_terms, mentions_keyword
from app.llm.trends.material_store import (
    InMemoryMaterialKeywordStore,
    MaterialKeyword,
    _from_document,
    _to_document,
)
from app.shared import (
    BlogTaskInput,
    GoogleTrendEvidence,
    NaverTrendEvidence,
    TrendEvidenceOrigin,
    TrendMode,
    TrendSource,
    TrendSourceEvidence,
    YouTubeTrendEvidence,
)


def fetch_input(topic: str = "AIONA", **overrides) -> TrendFetchInput:
    return TrendFetchInput(
        post_id="post_1",
        user_id="user_1",
        input=BlogTaskInput(
            topic=topic,
            subject="IT·디지털",
            keywords=[],
            reference_materials=[],
        ),
        **overrides,
    )


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def google_evidence(**overrides) -> TrendSourceEvidence:
    base = dict(
        source=TrendSource.GOOGLE_TRENDS,
        observed_at="2026-08-07T00:00:00.000Z",
        data_origin=TrendEvidenceOrigin.SERPAPI,
        google=GoogleTrendEvidence(
            active=True,
            search_volume=200000,
            increase_percentage=1000,
            started_at="2026-08-06T22:00:00.000Z",
            feed_type=TrendEvidenceOrigin.SERPAPI,
        ),
    )
    base.update(overrides)
    return TrendSourceEvidence(**base)


def scraped(*rows: dict):
    """트렌드 페이지 표를 대신한다(실제 DOM에서 긁히는 것과 같은 모양)."""

    def scrape(country: str, hours: int) -> list[dict]:
        return list(rows)

    return scrape


class TestGoogleFieldParsing:
    """페이지가 주는 것은 사람이 읽는 표기('5천+', '1,000%', '3시간 전')다. 저장은
    숫자·절대 시각이어야 한다 — 하루 뒤에 읽어도 '3시간 전'이라고 말하지 않도록."""

    def test_korean_volume_units(self):
        assert search_volume("5천+") == 5_000
        assert search_volume("50만+") == 500_000
        assert search_volume("1만+") == 10_000
        assert search_volume("100+") == 100
        assert search_volume("검색 2천+회") == 2_000
        assert search_volume(None) is None
        assert search_volume("—") is None

    def test_increase_percentage(self):
        assert increase_percentage("1,000%") == 1000.0
        assert increase_percentage("900%") == 900.0
        assert increase_percentage(None) is None
        assert increase_percentage("활성") is None

    def test_relative_start_time_becomes_absolute(self):
        observed = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
        assert started_at("3시간 전", observed) == "2026-08-07T09:00:00.000Z"
        assert started_at("40분 전", observed) == "2026-08-07T11:20:00.000Z"
        assert started_at("어제", observed) == "2026-08-06T12:00:00.000Z"
        # 읽을 수 없는 표기에 시각을 지어내지 않는다.
        assert started_at("방금", observed) is None
        assert started_at(None, observed) is None

    def test_active_status(self):
        # 실측 표기 두 가지.
        assert is_active("trending_up\n활성") is True
        assert is_active("timelapse\n3시간 동안 지속됨") is False
        # 모르는 표기는 '아니다'가 아니라 '모른다'로 둔다.
        assert is_active(None) is None
        assert is_active("???") is None


class TestGoogleEvidence:
    async def test_page_metrics_are_preserved_as_evidence(self):
        """크롤한 검색량·상승률·시작 시각·활성 여부를 그대로 근거로 남긴다."""
        scrape = scraped(
            {
                "keyword": "민생지원금",
                "volume": "20만+",
                "increase": "1,000%",
                "started": "2시간 전",
                "status": "trending_up\n활성",
            },
            # 검색량만 있는 행 — 없는 값은 만들지 않는다.
            {"keyword": "식중독", "volume": "5천+", "increase": None,
             "started": None, "status": None},
        )

        result = await GoogleTrendsCollector(scrape=scrape).collect(fetch_input(), 8)

        first = result[0]
        assert first.keyword == "민생지원금"
        evidence = first.evidence
        assert evidence.source == TrendSource.GOOGLE_TRENDS
        assert evidence.data_origin == TrendEvidenceOrigin.GOOGLE_TRENDS_WEB
        assert evidence.observed_at
        google = evidence.google
        assert google.active is True
        assert google.search_volume == 200_000
        assert google.increase_percentage == 1000.0
        # 상승 시작은 관측 시각에서 2시간을 뺀 절대 시각이다.
        assert _parse_iso(google.started_at) == _parse_iso(evidence.observed_at) - timedelta(
            hours=2
        )
        # SerpApi·RSS를 쓰지 않으므로 RSS 전용 필드는 비어 있다.
        assert google.approximate_traffic is None

        second = result[1]
        assert second.evidence.google.search_volume == 5_000
        assert second.evidence.google.active is None
        assert second.evidence.google.increase_percentage is None
        assert second.evidence.google.started_at is None

    async def test_an_inactive_row_is_recorded_as_inactive_not_unknown(self):
        scrape = scraped(
            {
                "keyword": "민생지원금",
                "volume": "5천+",
                "increase": "500%",
                "started": "3시간 전",
                "status": "timelapse\n3시간 동안 지속됨",
            }
        )

        result = await GoogleTrendsCollector(scrape=scrape).collect(fetch_input(), 8)

        assert result[0].evidence.google.active is False


class TestYouTubeEvidence:
    @respx.mock
    async def test_trending_keeps_view_stats_of_the_top_video(self):
        """statistics를 같은 호출에 받아, 대표(최고 조회) 영상의 실제 조회 지표를 남긴다."""
        now = datetime.now(timezone.utc)
        route = respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "vid1",
                            "snippet": {
                                "title": "안유진 무대",
                                "tags": ["안유진"],
                                "publishedAt": _iso(now - timedelta(hours=8)),
                            },
                            "statistics": {"viewCount": "1380000"},
                        },
                        {
                            "id": "vid2",
                            "snippet": {
                                "title": "안유진 직캠",
                                "tags": ["안유진"],
                                "publishedAt": _iso(now - timedelta(hours=20)),
                            },
                            "statistics": {"viewCount": "400000"},
                        },
                        {
                            "id": "vid3",
                            "snippet": {
                                "title": "안유진 라이브",
                                "tags": ["안유진"],
                                "publishedAt": _iso(now - timedelta(hours=30)),
                            },
                            "statistics": {"viewCount": "90000"},
                        },
                    ]
                },
            )
        )

        result = await YouTubeTrendCollector("test-credential").collect(fetch_input(), 8)

        assert route.call_count == 1  # 키워드마다 추가 호출을 만들지 않는다
        assert "statistics" in route.calls[0].request.url.params["part"]
        [keyword] = result
        assert keyword.keyword == "안유진"
        youtube = keyword.evidence.youtube
        assert keyword.evidence.data_origin == TrendEvidenceOrigin.YOUTUBE_API
        assert youtube.top_video_id == "vid1"
        assert youtube.top_view_count == 1380000
        # 평균은 '누적 조회수 ÷ 게시 후 경과시간'이다. 근거에 실린 두 시각으로 그대로
        # 재계산해 검산한다 — 실시간 조회 속도가 아니다.
        observed = _parse_iso(keyword.evidence.observed_at)
        published = _parse_iso(youtube.top_video_published_at)
        elapsed = max((observed - published).total_seconds() / 3600, 1.0)
        # **정확히 같기를 요구하지 않는다**(2026-08-12). 근거에 실린 observed_at은
        # 밀리초까지 잘려 저장되므로, 여기서 다시 나눈 값과 소수 첫째 자리에서 갈릴 수
        # 있다(실제로 172499.2 vs 172499.3으로 갈렸다). 확인하려는 것은 "저 두 시각으로
        # 계산한 값이 맞는가"이지 반올림 자릿수가 아니다.
        assert abs(youtube.average_views_per_hour - 1380000 / elapsed) < 1
        # 최신순에는 '최근 7일' 창이 없다.
        assert youtube.recent_video_count is None

    @respx.mock
    async def test_material_counts_recent_videos_and_averages_only_valid_ones(self):
        """소재 관련순: 7일 이내 고유 영상 수, 통계 없는 영상은 평균에서 제외, id 중복 제거."""
        now = datetime.now(timezone.utc)
        search = respx.get("https://www.googleapis.com/youtube/v3/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {"id": {"videoId": "vidA"}},
                        {"id": {"videoId": "vidB"}},
                        {"id": {"videoId": "vidD"}},
                    ]
                },
            )
        )
        videos = respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "vidA",
                            "snippet": {
                                "title": "배틀그라운드 감도 설정 정리",
                                "tags": ["감도 설정"],
                                "publishedAt": _iso(now - timedelta(days=2)),
                            },
                            "statistics": {"viewCount": "840000"},
                        },
                        {
                            "id": "vidB",
                            "snippet": {
                                "title": "배틀그라운드 감도 설정 강의",
                                "tags": ["감도 설정"],
                                "publishedAt": _iso(now - timedelta(days=10)),
                            },
                            "statistics": {"viewCount": "100000"},
                        },
                        # 같은 영상이 두 번 오면 videoId로 제거된다.
                        {
                            "id": "vidB",
                            "snippet": {
                                "title": "배틀그라운드 감도 설정 강의",
                                "tags": ["감도 설정"],
                                "publishedAt": _iso(now - timedelta(days=10)),
                            },
                            "statistics": {"viewCount": "100000"},
                        },
                        # 통계가 없는 영상 — 최근 수에는 들어가고 평균에서는 빠진다.
                        {
                            "id": "vidD",
                            "snippet": {
                                "title": "배틀그라운드 감도 설정 후기",
                                "tags": ["감도 설정"],
                                "publishedAt": _iso(now - timedelta(days=1)),
                            },
                        },
                    ]
                },
            )
        )

        result = await YouTubeTrendCollector("test-credential").collect(
            fetch_input(topic="배틀그라운드", mode=TrendMode.MATERIAL_RELATED), 8
        )

        assert search.call_count == 1 and videos.call_count == 1
        assert "statistics" in videos.calls[0].request.url.params["part"]
        found = next(item for item in result if item.keyword == "감도 설정")
        youtube = found.evidence.youtube
        assert youtube.top_video_id == "vidA"
        assert youtube.top_view_count == 840000
        assert youtube.recent_video_count == 2  # vidA(2일)·vidD(1일), 중복 vidB 제외
        assert youtube.recent_window_days == 7
        observed = _parse_iso(found.evidence.observed_at)
        rates = [
            840000 / max((observed - (now - timedelta(days=2))).total_seconds() / 3600, 1.0),
            100000 / max((observed - (now - timedelta(days=10))).total_seconds() / 3600, 1.0),
        ]
        assert youtube.average_views_per_hour == round(sum(rates) / len(rates), 1)

    @respx.mock
    async def test_a_video_titling_the_keyword_with_a_particle_still_counts(self):
        """태그가 없고 제목에 조사가 붙은 영상도 그 키워드의 근거에 든다.

        추출 결과로만 세면 이런 영상이 통째로 빠져, 실제로 가장 많이 본 영상이 대표
        영상 자리에 오지 못한다."""
        now = datetime.now(timezone.utc)
        respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": f"vid{index}",
                            "snippet": {
                                "title": "안유진 무대",
                                "tags": ["안유진"],
                                "publishedAt": _iso(now - timedelta(hours=10)),
                            },
                            "statistics": {"viewCount": "100000"},
                        }
                        for index in range(3)
                    ]
                    + [
                        {
                            "id": "vidTop",
                            "snippet": {
                                "title": "안유진이 부른 신곡 무대",
                                "publishedAt": _iso(now - timedelta(hours=5)),
                            },
                            "statistics": {"viewCount": "2000000"},
                        }
                    ]
                },
            )
        )

        result = await YouTubeTrendCollector("test-credential").collect(fetch_input(), 8)

        [keyword] = [item for item in result if item.keyword == "안유진"]
        assert keyword.evidence.youtube.top_video_id == "vidTop"
        assert keyword.evidence.youtube.top_view_count == 2_000_000

    @respx.mock
    async def test_no_view_stats_means_no_fabricated_numbers(self):
        """조회수 통계가 아예 없으면 근거를 만들지 않는다 — 0을 지어내지 않는다."""
        respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {"snippet": {"title": "안유진 무대", "tags": ["안유진"]}},
                        {"snippet": {"title": "안유진 직캠", "tags": ["안유진"]}},
                        {"snippet": {"title": "안유진 라이브", "tags": ["안유진"]}},
                    ]
                },
            )
        )

        result = await YouTubeTrendCollector("test-credential").collect(fetch_input(), 8)

        assert [item.keyword for item in result] == ["안유진"]
        assert result[0].evidence is None


class TestKeywordMention:
    """'이 문서가 그 키워드를 말했는가'의 판정.

    구절 추출 결과로 세던 때의 실패를 고정한다: 화면에 '이번 수집 확인 블로그 1건'이
    떴는데, 문서들은 분명히 그 키워드를 말하고 있었다 — 조사가 붙은 표기를 추출기가
    통째로 버렸기 때문이다.
    """

    def test_a_particle_stuck_to_the_keyword_is_still_a_mention(self):
        for text in ("한여름 폭염이 계속되면서", "연일 이어지는 폭염에 전력수요 최고치", "폭염 특보"):
            assert mentions_keyword(match_terms("폭염"), compact_for_match(text)), text

    def test_spacing_and_word_order_do_not_hide_a_mention(self):
        terms = match_terms("워터밤 서울")
        assert mentions_keyword(terms, compact_for_match("서울 워터밤 일정 공개"))
        assert mentions_keyword(terms, compact_for_match("워터 밤이 서울에서 열린다"))

    def test_every_word_must_appear_so_a_partial_hit_does_not_count(self):
        # '서울'만 나온 문서가 '워터밤 서울'의 근거로 끼어들면 수치가 부풀려진다.
        assert not mentions_keyword(
            match_terms("워터밤 서울"), compact_for_match("서울 한강 야외수영장 개장")
        )
        assert not mentions_keyword(match_terms("폭염"), compact_for_match("장마가 물러간다"))


class TestNaverEvidence:
    @respx.mock
    async def test_counts_documents_the_keyword_actually_appears_in(self):
        """kind·pubDate·link를 보존해 '이번 수집에서 실제 확인한 문서 수'를 센다."""
        now = datetime.now(timezone.utc)
        recent = format_datetime(now - timedelta(hours=2))
        stale = format_datetime(now - timedelta(hours=30))

        def responder(request: httpx.Request) -> httpx.Response:
            kind = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
            if kind == "news":
                items = [
                    {
                        "title": "워터밤 서울 일정",
                        "description": "여름 페스티벌",
                        "link": "https://news.example/1",
                        "pubDate": recent,
                    },
                    # 24시간 밖 뉴스 — 최근 뉴스 수에서 빠진다.
                    {
                        "title": "워터밤 서울 안전",
                        "description": "행사 안내",
                        "link": "https://news.example/2",
                        "pubDate": stale,
                    },
                    # 키워드가 없는 문서 — 아무 수치에도 들어가지 않는다.
                    {
                        "title": "다른 소식",
                        "description": "무관한 기사",
                        "link": "https://news.example/3",
                        "pubDate": recent,
                    },
                ]
            elif kind == "blog":
                items = [
                    {
                        "title": "워터밤 서울 후기",
                        "description": "다녀온 이야기",
                        "link": "https://blog.example/1",
                        "postdate": (now - timedelta(days=1)).strftime("%Y%m%d"),
                    },
                    # 같은 링크가 다시 오면 한 번만 센다.
                    {
                        "title": "워터밤 서울 후기",
                        "description": "다녀온 이야기",
                        "link": "https://blog.example/1",
                        "postdate": (now - timedelta(days=1)).strftime("%Y%m%d"),
                    },
                ]
            else:
                items = []
            return httpx.Response(200, json={"items": items})

        respx.get(url__startswith="https://naverapihub.apigw.ntruss.com/search/v1/").mock(
            side_effect=responder
        )

        result = await NaverTrendCollector("id", "secret").collect(fetch_input(), 8)

        found = next(item for item in result if item.keyword == "워터밤 서울")
        evidence = found.evidence
        assert evidence.data_origin == TrendEvidenceOrigin.NAVER_SEARCH_API
        naver = evidence.naver
        # 뉴스는 pubDate 24시간 필터: recent 1건만.
        assert naver.recent_news_count == 1
        # 블로그는 링크 중복 제거 후 1건.
        assert naver.collected_blog_count == 1
        # 관련 콘텐츠 = 키워드가 등장한 고유 문서 전부(뉴스 2 + 블로그 1).
        assert naver.collected_related_content_count == 3
        assert naver.basis == "SEARCH_API_SAMPLE"
        # 표본 크기: 링크 중복을 뺀 전체 수집 문서.
        assert naver.sampled_document_count == 4

    @respx.mock
    async def test_documents_that_mention_the_keyword_with_a_particle_are_counted(self):
        """실제 화면에서 '이번 수집 확인 블로그 1건'이 뜨던 자리.

        추출 결과로 세면 '폭염 특보 발효'만 잡히고 '폭염이 계속되면서'·'폭염에
        전력수요'는 빠진다 — 문서가 분명히 말한 키워드인데도. 이제 본문 대조로 센다.
        """
        recent = format_datetime(datetime.now(timezone.utc) - timedelta(hours=2))

        def responder(request: httpx.Request) -> httpx.Response:
            kind = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
            if kind != "news":
                return httpx.Response(200, json={"items": []})
            return httpx.Response(
                200,
                json={
                    "items": [
                        # 추출기가 '폭염'을 내놓는 유일한 문서 — 후보 목록에 오르는 근거.
                        {
                            "title": "폭염 특보 발효",
                            "description": "야외활동 자제",
                            "link": "https://news.example/1",
                            "pubDate": recent,
                        },
                        # 아래 둘은 추출기가 아무것도 내놓지 않지만 분명히 폭염을 말한다.
                        {
                            "title": "한여름 폭염이 계속되면서",
                            "description": "온열질환자 급증",
                            "link": "https://news.example/2",
                            "pubDate": recent,
                        },
                        {
                            "title": "연일 이어지는 폭염에",
                            "description": "전력수요 최고치",
                            "link": "https://news.example/3",
                            "pubDate": recent,
                        },
                        # 폭염을 말하지 않은 문서는 세지 않는다.
                        {
                            "title": "장마가 물러가고",
                            "description": "선선한 바람",
                            "link": "https://news.example/4",
                            "pubDate": recent,
                        },
                    ]
                },
            )

        respx.get(url__startswith="https://naverapihub.apigw.ntruss.com/search/v1/").mock(
            side_effect=responder
        )

        result = await NaverTrendCollector("id", "secret").collect(fetch_input(), 8)

        found = next(item for item in result if item.keyword == "폭염")
        assert found.evidence.naver.recent_news_count == 3
        assert found.evidence.naver.collected_related_content_count == 3


class TestEvidenceThroughAggregation:
    def test_rescore_pool_keeps_evidence(self):
        pool = [
            CollectedKeyword(keyword="민생지원금", score=200000, rank=1, evidence=google_evidence()),
            CollectedKeyword(keyword="식중독", score=5000, rank=2),
        ]

        rescored = _rescore_pool(pool)

        assert rescored[0].evidence is not None
        assert rescored[0].evidence.google.search_volume == 200000
        assert rescored[1].evidence is None

    def test_merge_pools_prefers_fresh_evidence_but_never_drops_old_ones(self):
        old = [
            CollectedKeyword(
                keyword="민생지원금",
                score=50,
                rank=1,
                evidence=google_evidence(observed_at="2026-08-01T00:00:00.000Z"),
            ),
            CollectedKeyword(keyword="식중독", score=40, rank=2, evidence=google_evidence()),
        ]
        new = [
            CollectedKeyword(
                keyword="민생지원금",
                score=60,
                rank=1,
                evidence=google_evidence(observed_at="2026-08-07T00:00:00.000Z"),
            ),
            # 새 수집분에 근거가 없으면 기존 근거를 이어받는다.
            CollectedKeyword(keyword="식중독", score=45, rank=2),
        ]

        merged = _merge_pools(new, old)

        by_keyword = {item.keyword: item for item in merged}
        assert by_keyword["민생지원금"].evidence.observed_at == "2026-08-07T00:00:00.000Z"
        assert by_keyword["식중독"].evidence is not None

    async def test_trending_response_carries_evidence_per_source_without_merging_numbers(self):
        """여러 출처에서 확인된 키워드는 출처별 근거를 따로 싣는다 — 숫자를 합치지 않는다."""

        class EvidenceCollector:
            def __init__(self, source, keyword, evidence):
                self.source = source
                self._keyword = keyword
                self._evidence = evidence

            async def collect(self, trend_input, limit, known=frozenset()):
                return [
                    CollectedKeyword(
                        keyword=self._keyword, score=90.0, rank=1, evidence=self._evidence
                    )
                ]

        naver_ev = TrendSourceEvidence(
            source=TrendSource.NAVER_DATALAB,
            observed_at="2026-08-07T00:00:00.000Z",
            data_origin=TrendEvidenceOrigin.NAVER_SEARCH_API,
            naver=NaverTrendEvidence(
                recent_news_count=184,
                collected_blog_count=63,
                collected_related_content_count=247,
                sampled_document_count=500,
                basis="SEARCH_API_SAMPLE",
            ),
        )
        provider = AggregateTrendProvider(
            [
                EvidenceCollector(TrendSource.GOOGLE_TRENDS, "민생지원금", google_evidence()),
                EvidenceCollector(TrendSource.NAVER_DATALAB, "민생지원금", naver_ev),
            ],
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=4))

        [keyword] = result.trend_keywords
        evidence = keyword.evidence_by_source
        assert set(evidence) == {"GOOGLE_TRENDS", "NAVER_DATALAB"}
        assert evidence["GOOGLE_TRENDS"].google.search_volume == 200000
        assert evidence["NAVER_DATALAB"].naver.recent_news_count == 184
        # 서로 다른 척도는 각자의 자리에만 있다 — 합산된 필드가 없다.
        assert evidence["NAVER_DATALAB"].google is None
        assert evidence["GOOGLE_TRENDS"].naver is None

    async def test_evidence_survives_the_pool_cache_roundtrip(self):
        """수집 → 캐시 저장 → 재조회까지 근거가 그대로다(재수집 없이)."""

        class OneShotCollector:
            source = TrendSource.GOOGLE_TRENDS

            def __init__(self):
                self.calls = 0

            async def collect(self, trend_input, limit, known=frozenset()):
                self.calls += 1
                return [
                    CollectedKeyword(
                        keyword="민생지원금", score=90.0, rank=1, evidence=google_evidence()
                    )
                ]

        collector = OneShotCollector()
        provider = AggregateTrendProvider([collector], rotate=lambda size: 0)

        await provider.fetch_trends(fetch_input(max_keywords=4))
        second = await provider.fetch_trends(fetch_input(max_keywords=4))

        assert collector.calls == 1
        [keyword] = [k for k in second.trend_keywords if k.keyword == "민생지원금"]
        assert keyword.evidence_by_source["GOOGLE_TRENDS"].google.increase_percentage == 1000

    async def test_material_flow_carries_evidence_to_the_screen_model(self):
        """소재 관련순: 수집 → 사전 필터 → 저장 → 화면 모델까지 evidenceBySource가 산다."""

        class MaterialCollector:
            source = TrendSource.NAVER_DATALAB

            async def collect(self, trend_input, limit, known=frozenset()):
                return [
                    CollectedKeyword(
                        keyword="배틀그라운드 감도",
                        score=80.0,
                        rank=1,
                        evidence=TrendSourceEvidence(
                            source=TrendSource.NAVER_DATALAB,
                            observed_at="2026-08-07T00:00:00.000Z",
                            data_origin=TrendEvidenceOrigin.NAVER_SEARCH_API,
                            naver=NaverTrendEvidence(
                                recent_news_count=3,
                                collected_blog_count=12,
                                collected_related_content_count=20,
                                sampled_document_count=100,
                                basis="SEARCH_API_SAMPLE",
                            ),
                        ),
                    )
                ]

        store = InMemoryMaterialKeywordStore()
        provider = AggregateTrendProvider(
            [MaterialCollector()], rotate=lambda size: 0, material_store=store
        )

        result = await provider.fetch_trends(
            fetch_input(topic="배틀그라운드", mode=TrendMode.MATERIAL_RELATED, max_keywords=8)
        )

        found = next(k for k in result.trend_keywords if k.keyword == "배틀그라운드 감도")
        assert found.evidence_by_source["NAVER_DATALAB"].naver.collected_blog_count == 12


class TestGoogleSuggestionsForMaterial:
    """급상승 표는 전국 단위라 소재 관련 후보가 나오지 않는다(실측: '참이슬'로 조회해도 0개).
    소재별 데이터를 주는 Trends explore는 브라우저로 열어도 429다. 남은 경로가 자동완성이다."""

    @respx.mock
    async def test_material_mode_uses_autocomplete_not_the_trending_page(self):
        route = respx.get(url__startswith="https://suggestqueries.google.com").mock(
            return_value=httpx.Response(
                200,
                json=["참이슬", ["참이슬 도수", "참이슬", "참이슬 가격", "참이슬 칼로리"]],
            )
        )

        def must_not_crawl(country, hours):
            raise AssertionError("소재 관련순에서 급상승 표를 크롤하면 안 된다")

        result = await GoogleTrendsCollector(scrape=must_not_crawl).collect(
            fetch_input(topic="참이슬", mode=TrendMode.MATERIAL_RELATED), 8
        )

        assert route.called
        keywords = [item.keyword for item in result]
        assert "참이슬 도수" in keywords
        assert "참이슬 가격" in keywords
        # 소재를 그대로 되풀이한 제안은 후보가 아니다.
        assert "참이슬" not in keywords

    @respx.mock
    async def test_suggestions_carry_no_fabricated_metrics(self):
        """자동완성은 검색량·상승률을 주지 않는다. 순위를 점수로 옮길 뿐, 근거는 만들지
        않는다 — 네이버 보강이 채운다."""
        respx.get(url__startswith="https://suggestqueries.google.com").mock(
            return_value=httpx.Response(200, json=["참이슬", ["참이슬 도수"]])
        )

        result = await GoogleTrendsCollector().collect(
            fetch_input(topic="참이슬", mode=TrendMode.MATERIAL_RELATED), 8
        )

        assert [item.keyword for item in result] == ["참이슬 도수"]
        assert result[0].evidence is None


class TestNaverMeasurement:
    """구글이 제안한 키워드처럼 수치가 없는 후보를 네이버에 직접 물어 재는 경로."""

    @respx.mock
    async def test_measures_totals_and_marks_the_basis(self):
        now = datetime.now(timezone.utc)

        def responder(request: httpx.Request) -> httpx.Response:
            kind = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
            if kind == "news":
                return httpx.Response(
                    200,
                    json={
                        "total": 12340,
                        "items": [
                            {"title": "참이슬 도수 정리", "link": "https://n/1",
                             "pubDate": format_datetime(now - timedelta(hours=2))},
                            {"title": "옛 기사", "link": "https://n/2",
                             "pubDate": format_datetime(now - timedelta(days=5))},
                        ],
                    },
                )
            return httpx.Response(200, json={"total": 45600, "items": []})

        respx.get(url__startswith="https://naverapihub.apigw.ntruss.com/search/v1/").mock(
            side_effect=responder
        )

        measured = await NaverTrendCollector("id", "secret").measure_keywords(["참이슬 도수"])

        naver = measured["참이슬 도수"].naver
        # 표본이 아니라 네이버가 세어 준 총수다 — 기준을 함께 저장해 화면 문구가 갈린다.
        assert naver.basis == "SEARCH_API_TOTAL"
        assert naver.total_news_count == 12340
        assert naver.total_blog_count == 45600
        # 최근 24시간 안에 올라온 것만 센다.
        assert naver.recent_document_count == 1
        assert naver.recent_hit_cap is False
        # 발굴 경로의 표본 수치는 섞이지 않는다.
        assert naver.collected_blog_count is None

    @respx.mock
    async def test_one_failing_keyword_does_not_lose_the_others(self):
        calls = {"n": 0}

        def responder(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if "실패" in request.url.params.get("query", ""):
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json={"total": 5, "items": []})

        respx.get(url__startswith="https://naverapihub.apigw.ntruss.com/search/v1/").mock(
            side_effect=responder
        )

        measured = await NaverTrendCollector("id", "secret").measure_keywords(
            ["실패 키워드", "정상 키워드"]
        )

        assert set(measured) == {"정상 키워드"}

    @respx.mock
    async def test_a_burst_limited_measure_retries_and_recovers(self, monkeypatch):
        """수집 직후의 429 폭주에서 전 키워드가 통째로 사라지던 자리(실측 0/10) —
        네이버 한도는 초 단위로 풀리므로 잠깐 물러났다 다시 던지면 살아난다."""
        from app.llm.trends import naver as naver_module

        monkeypatch.setattr(naver_module, "MEASURE_429_BACKOFF_SECONDS", (0.0,))
        first_seen: set[str] = set()

        def responder(request: httpx.Request) -> httpx.Response:
            marker = request.url.path + request.url.params.get("query", "")
            if marker not in first_seen:
                first_seen.add(marker)
                return httpx.Response(429, text="rate limited")
            return httpx.Response(200, json={"total": 77, "items": []})

        respx.get(url__startswith="https://naverapihub.apigw.ntruss.com/search/v1/").mock(
            side_effect=responder
        )

        measured = await NaverTrendCollector("id", "secret").measure_keywords(["참이슬 도수"])

        assert measured["참이슬 도수"].naver.total_news_count == 77
        assert measured["참이슬 도수"].naver.total_blog_count == 77

    @respx.mock
    async def test_a_persistent_429_still_fails_open(self, monkeypatch):
        """재시도로도 안 풀리는 429는 기존과 같다 — 그 키워드만 포기하고 화면은
        중립 문구로 남는다. 수치를 지어내지 않는다."""
        from app.llm.trends import naver as naver_module

        monkeypatch.setattr(naver_module, "MEASURE_429_BACKOFF_SECONDS", (0.0,))
        respx.get(url__startswith="https://naverapihub.apigw.ntruss.com/search/v1/").mock(
            return_value=httpx.Response(429, text="rate limited")
        )

        measured = await NaverTrendCollector("id", "secret").measure_keywords(["참이슬 도수"])

        assert measured == {}


class TestYouTubeRecencyEscalation:
    """'참이슬'처럼 관련 영상이 대부분 몇 년 전인 소재에서 '최근 7일 관련 영상 없음'만
    반복되던 자리. 쇼츠를 따로 찾아보고, 그래도 없으면 집계 창을 30일로 넓힌다."""

    @respx.mock
    async def test_searches_short_form_when_nothing_is_recent(self):
        now = datetime.now(timezone.utc)
        searches: list[dict] = []

        def search_responder(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            searches.append(params)
            if params.get("videoDuration") == "short":
                return httpx.Response(200, json={"items": [{"id": {"videoId": "short1"}}]})
            return httpx.Response(200, json={"items": [{"id": {"videoId": "old1"}}]})

        def videos_responder(request: httpx.Request) -> httpx.Response:
            ids = request.url.params.get("id", "").split(",")
            aged = {
                "old1": now - timedelta(days=900),
                "short1": now - timedelta(days=2),
            }
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": video_id,
                            "snippet": {
                                "title": "참이슬 도수 정리",
                                "tags": ["참이슬 도수"],
                                "publishedAt": _iso(aged[video_id]),
                            },
                            "statistics": {"viewCount": "100000"},
                        }
                        for video_id in ids
                        if video_id in aged
                    ]
                },
            )

        respx.get("https://www.googleapis.com/youtube/v3/search").mock(side_effect=search_responder)
        respx.get("https://www.googleapis.com/youtube/v3/videos").mock(side_effect=videos_responder)

        result = await YouTubeTrendCollector("key").collect(
            fetch_input(topic="참이슬", mode=TrendMode.MATERIAL_RELATED), 8
        )

        # 1차 관련도순 검색 뒤, 최근 영상이 없어 쇼츠를 한 번 더 찾았다.
        assert len(searches) == 2
        assert searches[0].get("order") == "relevance"
        assert "videoDuration" not in searches[0]
        assert searches[1]["videoDuration"] == "short"
        assert "publishedAfter" in searches[1]
        # 쇼츠에서 찾은 최근 영상이 7일 집계에 잡힌다.
        found = next(item for item in result if item.keyword == "참이슬 도수")
        assert found.evidence.youtube.recent_video_count == 1
        assert found.evidence.youtube.recent_window_days == 7

    @respx.mock
    async def test_widens_the_window_to_thirty_days_when_seven_finds_nothing(self):
        now = datetime.now(timezone.utc)
        respx.get("https://www.googleapis.com/youtube/v3/search").mock(
            return_value=httpx.Response(200, json={"items": [{"id": {"videoId": "v1"}}]})
        )
        respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "v1",
                            "snippet": {
                                "title": "참이슬 도수 정리",
                                "tags": ["참이슬 도수"],
                                # 7일 밖이지만 30일 안이다.
                                "publishedAt": _iso(now - timedelta(days=20)),
                            },
                            "statistics": {"viewCount": "50000"},
                        }
                    ]
                },
            )
        )

        result = await YouTubeTrendCollector("key").collect(
            fetch_input(topic="참이슬", mode=TrendMode.MATERIAL_RELATED), 8
        )

        found = next(item for item in result if item.keyword == "참이슬 도수")
        # 넓혔다는 사실을 숨기지 않는다 — 카드가 "최근 30일"이라고 적게 창을 함께 싣는다.
        assert found.evidence.youtube.recent_window_days == 30
        assert found.evidence.youtube.recent_video_count == 1


class TestPanelIsExplainableAndBalanced:
    """카드는 저마다 '왜 이 키워드인지'를 세 줄로 말해야 하고, 한 출처가 화면을 덮어서는
    안 된다. 실측에서 최신순은 16칸 중 2칸만 근거가 있었고, 소재 관련순은 유튜브가 12칸을
    가져갔다."""

    class _Fixed:
        """출처 하나가 정해진 키워드를 내는 수집기. 일부에만 근거를 붙인다."""

        def __init__(self, source, keywords, with_evidence=()):
            self.source = source
            self._keywords = keywords
            self._with_evidence = set(with_evidence)

        async def collect(self, trend_input, limit, known=frozenset()):
            return [
                CollectedKeyword(
                    keyword=keyword,
                    score=float(len(self._keywords) - index),
                    rank=index + 1,
                    evidence=(
                        google_evidence(observed_at=f"2026-08-07T00:00:{index:02d}.000Z")
                        if keyword in self._with_evidence
                        else None
                    ),
                )
                for index, keyword in enumerate(self._keywords)
            ]

    async def test_candidates_with_evidence_fill_the_panel_first(self):
        """근거 없는 옛 후보가 점수만으로 자리를 차지해 '상세 지표는 새 수집 후' 카드가
        화면을 덮던 자리. 설명할 수 있는 후보가 있으면 그쪽이 먼저다."""
        explained = ["워터밤 서울", "한강 야외수영장", "올리브영 세일", "프로야구 올스타전"]
        # 점수가 더 높은데 근거는 없는 후보들 — 예전에는 이쪽이 전부 이겼다.
        unexplained = [f"구체키워드{index}" for index in range(8)]
        collector = self._Fixed(
            TrendSource.GOOGLE_TRENDS, [*unexplained, *explained], with_evidence=explained
        )

        result = await AggregateTrendProvider([collector], rotate=lambda size: 0).fetch_trends(
            fetch_input(max_keywords=4)
        )

        shown = [keyword.keyword for keyword in result.trend_keywords]
        assert set(shown) == set(explained)
        assert all(keyword.evidence_by_source for keyword in result.trend_keywords)

    async def test_no_single_source_takes_over_the_panel(self):
        """세 소스가 후보를 내면 16칸을 나눠 갖는다 — 예전 상한은 3/5(9칸)라 한 소스가
        절반을 넘길 수 있었다."""
        provider = AggregateTrendProvider(
            [
                self._Fixed(TrendSource.GOOGLE_TRENDS, [f"구글키워드{n}" for n in range(20)]),
                self._Fixed(TrendSource.NAVER_DATALAB, [f"네이버키워드{n}" for n in range(20)]),
                self._Fixed(TrendSource.YOUTUBE, [f"유튜브키워드{n}" for n in range(20)]),
            ],
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=16))

        counts = Counter(keyword.source for keyword in result.trend_keywords)
        assert len(result.trend_keywords) == 16
        # 16 ÷ 3 = 6칸이 상한. 어느 소스도 그보다 많이 가져가지 못한다.
        assert max(counts.values()) <= 6
        assert set(counts) == {
            TrendSource.GOOGLE_TRENDS,
            TrendSource.NAVER_DATALAB,
            TrendSource.YOUTUBE,
        }

    async def test_a_thin_source_does_not_leave_the_panel_short(self):
        """균형을 맞추다가 화면이 비면 안 된다 — 후보가 적은 소스의 몫은 다른 소스가 채운다."""
        provider = AggregateTrendProvider(
            [
                self._Fixed(TrendSource.GOOGLE_TRENDS, [f"구글키워드{n}" for n in range(20)]),
                self._Fixed(TrendSource.YOUTUBE, ["유튜브키워드0"]),
            ],
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=8))

        assert len(result.trend_keywords) == 8

    async def test_rotation_keeps_showing_candidates_with_evidence(self):
        """'다른 키워드 보기'는 근거 있는 후보 안에서 섞는다.

        무작위 표본 경로가 선택 단계를 통째로 건너뛰어, 저장 풀에 남은 옛 후보(근거가 붙기
        전 수집분)를 그대로 집어 왔다 — 첫 진입 화면만 멀쩡하고 버튼을 누르면 대부분이
        "상세 지표는 새 수집 후 표시됩니다"였다. '새 키워드 찾기'도 수집 뒤에 같은 요청을
        한 번 더 보내므로 두 버튼이 함께 무너졌다.
        """
        explained = [f"근거있음{index}" for index in range(12)]
        # 점수는 이쪽이 더 높다 — 인기순만 보면 화면을 전부 차지한다.
        unexplained = [f"옛후보{index}" for index in range(30)]
        provider = AggregateTrendProvider(
            [
                self._Fixed(
                    TrendSource.GOOGLE_TRENDS,
                    [*unexplained, *explained],
                    with_evidence=explained,
                )
            ],
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=8, shuffle=True))

        assert len(result.trend_keywords) == 8
        assert all(keyword.evidence_by_source for keyword in result.trend_keywords)

    async def test_rotation_still_fills_the_panel_when_evidence_is_scarce(self):
        """근거 있는 후보가 화면보다 적으면 나머지도 함께 본다 — 적게 보여주지는 않는다."""
        provider = AggregateTrendProvider(
            [
                self._Fixed(
                    TrendSource.GOOGLE_TRENDS,
                    [*[f"옛후보{index}" for index in range(20)], "근거있음"],
                    with_evidence=["근거있음"],
                )
            ],
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=8, shuffle=True))

        assert len(result.trend_keywords) == 8

    async def test_stored_pool_stops_serving_candidates_without_evidence(self):
        """저장 풀에 근거 없는 옛 후보가 남아 있어도 최신순 화면에는 내보내지 않는다."""
        from app.llm.trends.aggregate import _bare_pool_key

        cache = InMemoryPoolCache()
        await cache.set(
            _bare_pool_key(TrendSource.GOOGLE_TRENDS),
            [
                # 점수는 더 높지만 설명할 수 없는 후보.
                CollectedKeyword(keyword="옛후보", score=99.0, rank=1, category=None),
                CollectedKeyword(
                    keyword="근거있음",
                    score=10.0,
                    rank=2,
                    category=None,
                    evidence=google_evidence(),
                ),
            ],
        )
        provider = AggregateTrendProvider(
            [self._Fixed(TrendSource.GOOGLE_TRENDS, ["수집하면안됨"])],
            rotate=lambda size: 0,
            cache=cache,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=4))

        assert [keyword.keyword for keyword in result.trend_keywords] == ["근거있음"]

    async def test_stored_pool_keeps_a_source_that_has_no_evidence_at_all(self):
        """근거 있는 후보가 하나도 없는 출처는 통째로 지우지 않는다 — 화면에서 사라진다."""
        from app.llm.trends.aggregate import _bare_pool_key

        cache = InMemoryPoolCache()
        await cache.set(
            _bare_pool_key(TrendSource.GOOGLE_TRENDS),
            [CollectedKeyword(keyword="옛후보", score=99.0, rank=1, category=None)],
        )
        provider = AggregateTrendProvider(
            [self._Fixed(TrendSource.GOOGLE_TRENDS, ["수집하면안됨"])],
            rotate=lambda size: 0,
            cache=cache,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=4))

        assert [keyword.keyword for keyword in result.trend_keywords] == ["옛후보"]

    def test_material_pool_alternates_between_sources(self):
        """소재 관련순은 관련도 순서를 그대로 잘라 써서, 한 소스가 앞자리를 독차지했다
        (실측: '참이슬'에서 유튜브 12칸 / 네이버 4칸)."""
        from app.llm.trends.aggregate import _interleave_by_source

        def item(keyword: str, source: TrendSource) -> MaterialKeyword:
            return MaterialKeyword(
                keyword=keyword,
                normalized_keyword=keyword.replace(" ", ""),
                source=source,
                sources=[source],
            )

        pool = [item(f"유튜브{n}", TrendSource.YOUTUBE) for n in range(6)] + [
            item(f"네이버{n}", TrendSource.NAVER_DATALAB) for n in range(3)
        ]

        interleaved = _interleave_by_source(pool)

        # 앞 6칸에 두 출처가 고루 든다(예전에는 유튜브 6개가 먼저 다 나갔다).
        head = [entry.source for entry in interleaved[:6]]
        assert head.count(TrendSource.NAVER_DATALAB) == 3
        assert head.count(TrendSource.YOUTUBE) == 3
        # 하나도 잃지 않는다 — 순서만 바꾼다.
        assert len(interleaved) == len(pool)
        assert {entry.keyword for entry in interleaved} == {entry.keyword for entry in pool}


class TestEvidenceSerialization:
    def test_cache_encode_decode_roundtrip_keeps_evidence(self):
        pool = [
            CollectedKeyword(keyword="민생지원금", score=90.0, rank=1, evidence=google_evidence())
        ]

        decoded = _decode(_encode(pool, at=1000.0), now=1005.0)

        evidence = decoded.keywords[0].evidence
        assert evidence.google.search_volume == 200000
        assert evidence.google.started_at == "2026-08-06T22:00:00.000Z"
        assert evidence.data_origin == TrendEvidenceOrigin.SERPAPI

    def test_cache_entries_written_before_evidence_still_decode(self):
        """구버전 캐시(evidence 키 자체가 없음)도 그대로 읽힌다 — 지표는 None일 뿐이다."""
        raw = json.dumps(
            {"at": 1000.0, "pool": [{"keyword": "민생지원금", "score": 90.0, "rank": 1}]}
        )

        decoded = _decode(raw, now=1005.0)

        assert decoded is not None
        assert decoded.keywords[0].keyword == "민생지원금"
        assert decoded.keywords[0].evidence is None

    def test_material_document_roundtrip_keeps_evidence(self):
        item = MaterialKeyword(
            keyword="배틀그라운드 감도",
            normalized_keyword="배틀그라운드감도",
            source=TrendSource.YOUTUBE,
            sources=[TrendSource.YOUTUBE],
            demand_score=70.0,
            collected_at=1000.0,
            evidence_by_source={
                "YOUTUBE": TrendSourceEvidence(
                    source=TrendSource.YOUTUBE,
                    observed_at="2026-08-07T00:00:00.000Z",
                    data_origin=TrendEvidenceOrigin.YOUTUBE_API,
                    youtube=YouTubeTrendEvidence(
                        top_video_id="vidA",
                        top_view_count=840000,
                        average_views_per_hour=18000.0,
                        recent_video_count=18,
                        recent_window_days=7,
                    ),
                )
            },
        )

        restored = _from_document(_to_document("배틀그라운드", item))

        youtube = restored.evidence_by_source["YOUTUBE"].youtube
        assert youtube.top_view_count == 840000
        assert youtube.recent_video_count == 18

    def test_material_document_without_evidence_still_loads(self):
        doc = _to_document(
            "배틀그라운드",
            MaterialKeyword(
                keyword="배틀그라운드 감도",
                normalized_keyword="배틀그라운드감도",
                source=TrendSource.YOUTUBE,
            ),
        )
        doc.pop("evidenceBySource", None)  # 근거 필드가 생기기 전의 문서 모양

        restored = _from_document(doc)

        assert restored is not None
        assert restored.evidence_by_source == {}

    async def test_material_store_keeps_the_newer_observation_per_source(self):
        store = InMemoryMaterialKeywordStore()

        def entry(observed_at: str, news: int) -> MaterialKeyword:
            return MaterialKeyword(
                keyword="배틀그라운드 감도",
                normalized_keyword="배틀그라운드감도",
                source=TrendSource.NAVER_DATALAB,
                sources=[TrendSource.NAVER_DATALAB],
                collected_at=1000.0,
                evidence_by_source={
                    "NAVER_DATALAB": TrendSourceEvidence(
                        source=TrendSource.NAVER_DATALAB,
                        observed_at=observed_at,
                        data_origin=TrendEvidenceOrigin.NAVER_SEARCH_API,
                        naver=NaverTrendEvidence(recent_news_count=news),
                    )
                },
            )

        await store.save("배틀그라운드", [entry("2026-08-07T00:00:00.000Z", 10)])
        # 더 오래된 관측이 나중에 와도 최신 근거를 덮지 않는다.
        await store.save("배틀그라운드", [entry("2026-08-01T00:00:00.000Z", 3)])

        [loaded] = await store.load("배틀그라운드")
        assert loaded.evidence_by_source["NAVER_DATALAB"].naver.recent_news_count == 10

        # 더 최신 관측은 갱신한다.
        await store.save("배틀그라운드", [entry("2026-08-08T00:00:00.000Z", 21)])
        [loaded] = await store.load("배틀그라운드")
        assert loaded.evidence_by_source["NAVER_DATALAB"].naver.recent_news_count == 21


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction):
        self._docs.sort(key=lambda d: d.get(field, 0), reverse=(direction == -1))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self._docs) if length is None else list(self._docs[:length])


class FakeCollection:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    @staticmethod
    def _match(doc, query) -> bool:
        for field, cond in query.items():
            if isinstance(cond, dict) and "$in" in cond:
                if doc.get(field) not in cond["$in"]:
                    return False
            elif doc.get(field) != cond:
                return False
        return True

    async def find_one(self, query):
        return next((d for d in self.docs.values() if self._match(d, query)), None)

    def find(self, query, projection=None):
        return FakeCursor([d for d in self.docs.values() if self._match(d, query)])

    async def count_documents(self, query):
        return sum(1 for d in self.docs.values() if self._match(d, query))

    async def update_one(self, query, update, upsert=False):
        _id = query["_id"]
        doc = self.docs.get(_id, {"_id": _id})
        doc.update(update.get("$set", {}))
        self.docs[_id] = doc

    async def insert_one(self, doc):
        from pymongo.errors import DuplicateKeyError

        if doc["_id"] in self.docs:
            raise DuplicateKeyError(f"dup key {doc['_id']}")
        self.docs[doc["_id"]] = dict(doc)

    async def delete_many(self, query):
        for key in [k for k, d in self.docs.items() if self._match(d, query)]:
            self.docs.pop(key, None)


class FakeDb:
    def __init__(self):
        self.cols: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self.cols.setdefault(name, FakeCollection())


class TestMongoPoolCacheEvidence:
    POOL_KEY = "trend:pool:GOOGLE_TRENDS:::"

    async def test_evidence_survives_mongo_roundtrip(self):
        cache = MongoPoolCache(FakeDb(), clock=lambda: 1000.0)
        await cache.set(
            self.POOL_KEY,
            [CollectedKeyword(keyword="민생지원금", score=90.0, rank=1, evidence=google_evidence())],
        )

        cached = await cache.get(self.POOL_KEY)

        assert cached.keywords[0].evidence.google.search_volume == 200000

    async def test_recollection_without_evidence_does_not_erase_the_stored_one(self):
        db = FakeDb()
        cache = MongoPoolCache(db, clock=lambda: 1000.0)
        await cache.set(
            self.POOL_KEY,
            [CollectedKeyword(keyword="민생지원금", score=90.0, rank=1, evidence=google_evidence())],
        )
        # 근거 없는 재수집(예: 옛 캐시에서 온 항목) — 점수·시각만 갱신된다.
        await cache.set(
            self.POOL_KEY, [CollectedKeyword(keyword="민생지원금", score=95.0, rank=1)]
        )

        cached = await cache.get(self.POOL_KEY)

        assert cached.keywords[0].score == 95.0
        assert cached.keywords[0].evidence is not None

    async def test_documents_written_before_evidence_still_load(self):
        db = FakeDb()
        db["trend_keywords"].docs["key1"] = {
            "_id": "key1",
            "keyword": "민생지원금",
            "source": "GOOGLE_TRENDS",
            "at": 1000.0,
            "score": 90.0,
            "seq": 1,
        }

        cached = await MongoPoolCache(db, clock=lambda: 1000.0).get(self.POOL_KEY)

        assert cached is not None
        assert cached.keywords[0].evidence is None
