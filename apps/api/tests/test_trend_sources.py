"""M2 trend collection: one collector per source, merged by AggregateTrendProvider."""

import asyncio
import json
from collections import Counter

import httpx
import pytest
import respx

from app.llm import TrendFetchInput
from app.llm.contracts import KeywordJudgment
from app.llm.trends import (
    AggregateTrendProvider,
    CollectedKeyword,
    InMemoryPoolCache,
    RedisPoolCache,
    create_pool_cache,
    GoogleTrendsCollector,
    InstagramTrendCollector,
    NaverTrendCollector,
    YouTubeTrendCollector,
)
from app.llm.trends.material_store import (
    MATERIAL_TARGET_POOL_SIZE,
    RELEVANCE_PROMPT_VERSION,
    InMemoryMaterialKeywordStore,
    MaterialKeyword,
    material_key,
)
from app.llm.trends.aggregate import (
    _bare_pool_key,
    _compact_keyword,
    _is_mechanical_echo,
    _is_subject_echo,
    _subject_echo_tokens,
)
from app.llm.trends.normalizer import normalize_keyword
from app.llm.trends.naver import _seasonal_queries
from app.llm.trends.similarity import are_similar, keyword_signature
from app.llm.trends.text import concrete_phrases, is_noun_phrase, to_collected, tokenize
from app.shared import (
    MATERIAL_RELATION_MIN_SUBJECT,
    BlogTaskInput,
    RelationType,
    TrendMode,
    TrendSource,
)


class TestTokenize:
    """Slicing Korean on a regex hands back inflected verbs and adjectives that
    look like words but name nothing — the panel showed "다양한" and "되는" as
    trends. No stopword list can cover them: every verb has dozens of forms."""

    def test_drops_inflected_verbs_and_adjectives(self):
        found = tokenize("다양한 국내 여행지가 되는 곳들을 소개하는 좋은 방법")
        assert "다양한" not in found
        assert "되는" not in found
        assert "소개하는" not in found
        assert "좋은" not in found

    def test_keeps_compound_nouns_and_names_whole(self):
        # The reason Kiwi is asked about whole words rather than used to split the
        # text: its own segmentation would cut 민생지원금 into 민생 + 지원금.
        assert "민생지원금" in tokenize("민생지원금 신청이 시작됐다")
        assert "안유진" in tokenize("안유진 무대 화제")
        assert "식중독" in tokenize("여름철 식중독 주의보")

    def test_drops_words_with_a_particle_stuck_to_them(self):
        # "분야의" reached the panel as a selected keyword. It is 분야 + 의 — a noun
        # with a 조사 on it, which is a fragment, not a thing anyone searches for.
        found = tokenize("카페 분야의 트렌드, 여행을 제품이 서울에서")
        assert "분야의" not in found
        assert "여행을" not in found
        assert "제품이" not in found
        assert {"카페", "트렌드"} <= set(found)

    def test_keeps_nouns_kiwi_alone_would_misread(self):
        # 안내 parses as 안(MAG) + 내(VV) + 어(EF) on Kiwi's first guess; its
        # runner-up reading is the noun, which is why more than one is considered.
        assert "안내" in tokenize("접수 안내")
        assert "사용기" in tokenize("사용기 공유")

    def test_drops_a_verb_kiwi_would_guess_a_noun_reading_for(self):
        # 뜨는 is 뜨(VV) + 는(ETM). Kiwi also offers 뜨는(NNG) — its guess for a word
        # it does not know — but at a far worse score, which is what tells the two
        # apart from a genuinely ambiguous noun like 안내.
        assert "뜨는" not in tokenize("지금 뜨는 것")

    def test_drops_category_nouns_that_name_a_genre_not_a_trend(self):
        # Real nouns, so the morphology check passes them — but "게임" tops YouTube
        # every single day and tells a writer nothing.
        found = tokenize("게임 영화 드라마 리그오브레전드")
        assert "리그오브레전드" in found
        assert not {"게임", "영화", "드라마"} & set(found)

    def test_drops_broad_field_nouns_that_flood_the_panel(self):
        # §9: Naver mines these out of the user's own subject text — a post about AI
        # development turns up 개발, 모델, 데이터, 코딩 every time, none a real trend.
        found = tokenize("민생지원금 개발 강의 데이터 모델 플랫폼 코딩 시스템 정보기술 가상 상속")
        assert "민생지원금" in found
        assert not {
            "개발",
            "강의",
            "데이터",
            "모델",
            "플랫폼",
            "코딩",
            "시스템",
            "정보기술",
            "가상",
            "상속",
        } & set(found)

    def test_drops_platform_and_institution_names_from_whole_queries(self):
        assert not is_noun_phrase("NAVER")
        assert not is_noun_phrase("Google Trends")
        assert not is_noun_phrase("강원특별자치도청")
        assert is_noun_phrase("워터밤")

    def test_naver_discovery_keeps_search_phrases_not_fragments(self):
        text = (
            "2026 워터밤 서울 일정 공개, 한강 야외수영장 운영 시작. "
            "올리브영 여름 세일과 스파이더맨 개봉 영화 소식"
        )

        found = concrete_phrases(text)

        assert "워터밤 서울" in found
        assert "한강 야외수영장" in found
        assert "올리브영 여름 세일" in found
        assert not {"서울", "일정", "운영"} & set(found)


class TestNormalizeKeyword:
    """Two keywords are the same when normalize_keyword agrees on them. Used both to
    collapse a keyword two sources spell differently and to recognise a
    recently-shown keyword coming back under a slightly different spelling (§6)."""

    def test_spacing_and_case_do_not_make_a_new_keyword(self):
        assert normalize_keyword("리그 오브 레전드") == normalize_keyword("리그오브레전드")
        assert normalize_keyword("AIONA") == normalize_keyword("aiona")
        assert normalize_keyword("K-리그!") == normalize_keyword("K리그")

    def test_well_known_synonyms_collapse(self):
        canonical = normalize_keyword("리그오브레전드")
        assert normalize_keyword("롤") == canonical
        assert normalize_keyword("LOL") == canonical
        assert normalize_keyword("리그 오브 레전드") == canonical

    def test_a_leading_year_is_dropped(self):
        assert normalize_keyword("2026 워터밤") == normalize_keyword("워터밤")

    def test_a_noun_ending_in_a_particle_like_syllable_is_left_whole(self):
        # No 조사 stripping — "여름휴가" must stay "여름휴가", not be chewed to "여름휴".
        assert normalize_keyword("여름휴가") == "여름휴가"
        assert normalize_keyword("사과") == "사과"

    def test_distinct_keywords_stay_distinct(self):
        assert normalize_keyword("워터밤") != normalize_keyword("불꽃축제")


class TestKeywordSimilarity:
    def test_word_order_does_not_make_a_new_keyword(self):
        first = keyword_signature("폭염 장마")
        second = keyword_signature("장마 폭염")

        assert first.token_set_signature == "장마|폭염"
        assert second.token_set_signature == "장마|폭염"
        assert are_similar(first, second)

    def test_same_entity_variants_share_a_cluster(self):
        signatures = [
            keyword_signature("스파이더맨"),
            keyword_signature("스파이더맨 신작"),
            keyword_signature("스파이더맨 영화"),
            keyword_signature("스파이더맨 개봉"),
        ]

        assert len({signature.cluster_id for signature in signatures}) == 1


class TestSeasonalDiscoveryQueries:
    def test_summer_queries_expand_beyond_the_users_subject(self):
        from datetime import date

        queries = _seasonal_queries(date(2026, 7, 15))

        assert "여름 페스티벌" in queries
        assert "서울 여름 행사" in queries
        assert "7월 행사" in queries
        assert "개봉 영화" in queries


def fetch_input(**overrides) -> TrendFetchInput:
    return TrendFetchInput(
        post_id="post_1",
        user_id="user_1",
        input=BlogTaskInput(
            topic="AIONA",
            subject="IT·디지털",
            keywords=["후기·리뷰 작성"],
            reference_materials=[],
        ),
        **overrides,
    )


class FakeCollector:
    """Stands in for a real source so the merge logic can be tested without HTTP."""

    def __init__(self, source: TrendSource, keywords: list[str], error: Exception | None = None):
        self.source = source
        self._keywords = keywords
        self._error = error
        self.calls = 0
        self.last_known: frozenset[str] = frozenset()

    async def collect(
        self,
        trend_input: TrendFetchInput,
        limit: int,
        known: frozenset[str] = frozenset(),
    ) -> list[CollectedKeyword]:
        self.calls += 1
        self.last_known = known
        if self._error:
            raise self._error
        return [
            CollectedKeyword(keyword=keyword, score=float(len(self._keywords) - index), rank=index + 1)
            for index, keyword in enumerate(self._keywords[:limit])
        ]


class TestToCollectedKnownPriority:
    """Sorting straight by frequency let words that are already in the stored
    pool (evergreen seasonal terms, for example) win the top-`limit` slots on
    every single collection — so 20 raw candidates could add only a handful
    of genuinely new keywords to the pool. `known` pushes them behind fresh
    candidates instead."""

    def test_new_keywords_fill_the_quota_before_known_ones(self):
        counts = Counter({"이미아는키워드": 50, "새키워드1": 10, "새키워드2": 9, "새키워드3": 8})
        known = frozenset({"이미아는키워드"})

        result = to_collected(counts, limit=3, known=known)

        assert [item.keyword for item in result] == ["새키워드1", "새키워드2", "새키워드3"]

    def test_known_keywords_still_fill_remaining_slots_when_new_ones_run_out(self):
        counts = Counter({"이미아는키워드": 50, "새키워드": 10})
        known = frozenset({"이미아는키워드"})

        result = to_collected(counts, limit=3, known=known)

        # Only one new candidate exists — the known one still fills the second slot
        # rather than leaving the pool smaller than the corpus actually supports.
        assert [item.keyword for item in result] == ["새키워드", "이미아는키워드"]

    def test_no_known_keywords_behaves_like_plain_frequency_sort(self):
        counts = Counter({"가": 1, "나": 3, "다": 2})

        result = to_collected(counts, limit=3)

        assert [item.keyword for item in result] == ["나", "다", "가"]


def trending_page(*rows: dict) -> "callable":
    """트렌드 페이지가 돌려주는 표를 대신한다. 실제 DOM에서 긁히는 것과 같은 모양이다
    (2026-08-07 실측: keyword·volume·increase·started·status)."""

    def scrape(country: str, hours: int) -> list[dict]:
        scrape.calls.append((country, hours))
        return list(rows)

    scrape.calls = []
    return scrape


def row(keyword: str, volume: str = "5천+", **overrides) -> dict:
    return {
        "keyword": keyword,
        "volume": volume,
        "increase": "1,000%",
        "started": "3시간 전",
        "status": "trending_up\n활성",
        **overrides,
    }


class TestGoogleTrendsCollector:
    """구글은 키를 쓰지 않고 트렌드 페이지를 브라우저로 읽는다(2026-08-07 전환).

    브라우저는 여기서 띄우지 않는다 — 표 내용을 직접 넣고, 파싱·필터·순위는 실제와 같은
    코드를 태운다. 브라우저가 실제로 이 표를 준다는 것은 크롤 탐침으로 따로 확인했다.
    """

    async def test_reads_the_trending_table_and_dedupes(self):
        scrape = trending_page(
            row("민생지원금", "1만+"), row("식중독", "5천+"), row("식중독", "4천+")
        )

        result = await GoogleTrendsCollector(scrape=scrape).collect(fetch_input(), 8)

        assert scrape.calls == [("KR", 24)]
        assert [item.keyword for item in result] == ["민생지원금", "식중독"]
        # 점수는 검색량이다: '1만+' → 10000, '5천+' → 5000.
        assert [item.score for item in result] == [10000.0, 5000.0]

    async def test_needs_no_api_key_at_all(self):
        """SerpApi 크레딧이 0이어도(실측 250/250 소진) 최신순이 비지 않는다."""
        result = await GoogleTrendsCollector(scrape=trending_page(row("민생지원금"))).collect(
            fetch_input(), 8
        )

        assert [item.keyword for item in result] == ["민생지원금"]

    async def test_a_failing_crawl_is_raised_so_the_source_drops_out(self):
        """폴백은 없다(SerpApi·RSS 폐기). 실패는 올려서 aggregate가 이 소스를 빼게 한다 —
        조용히 빈 목록을 돌려주면 '구글에 트렌드가 없다'와 구분되지 않는다."""

        def broken(country: str, hours: int) -> list[dict]:
            raise RuntimeError("Chrome을 시작하지 못했습니다")

        with pytest.raises(RuntimeError, match="Chrome"):
            await GoogleTrendsCollector(scrape=broken).collect(fetch_input(), 8)

    async def test_pushes_already_known_keywords_behind_new_ones(self):
        """A term already sitting in the stored pool (e.g. still trending from a
        prior collection) used to win the truncation to `limit` every time,
        crowding out anything genuinely new even when it was ranked lower."""
        scrape = trending_page(row("민생지원금", "1만+"), row("식중독", "100+"))

        result = await GoogleTrendsCollector(scrape=scrape).collect(
            fetch_input(), 1, known=frozenset({normalize_keyword("민생지원금")})
        )

        assert [item.keyword for item in result] == ["식중독"]

    async def test_rejoins_proper_nouns_google_split_into_morphemes(self):
        """구글은 한 낱말인 고유명사도 끊어 보낸다 — 화면에 "다이 소"로 나갔다.

        띄어쓰기가 맞는 값("한화 대 KIA")이 같은 표에 함께 오므로, 고치는 것은
        표에 등재된 것뿐이고 나머지는 손대지 않는다."""
        scrape = trending_page(
            row("다이 소", "1만+"),
            row("황강 댐", "8천+"),
            row("한화 대 KIA", "5천+"),
            row("어린이 배우", "2천+"),
        )

        result = await GoogleTrendsCollector(scrape=scrape).collect(fetch_input(), 8)

        assert [item.keyword for item in result] == [
            "다이소",
            "황강댐",
            "한화 대 KIA",
            "어린이 배우",
        ]

    async def test_a_row_with_no_metrics_still_becomes_a_candidate(self):
        """수치가 없다고 키워드를 버리지 않는다 — 순위 램프로 점수를 주고, 근거만 비운다."""
        scrape = trending_page(
            {"keyword": "민생지원금", "volume": None, "increase": None,
             "started": None, "status": None}
        )

        result = await GoogleTrendsCollector(scrape=scrape).collect(fetch_input(), 8)

        assert [item.keyword for item in result] == ["민생지원금"]
        google = result[0].evidence.google
        assert google.search_volume is None
        assert google.increase_percentage is None
        assert google.started_at is None
        assert google.active is None


class TestNaverTrendCollector:
    @respx.mock
    async def test_mines_recent_posts_and_ranks_by_mention_count(self):
        respx.get(url__startswith="https://naverapihub.apigw.ntruss.com/search/v1/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {"title": "<b>워터밤 서울</b> 일정", "description": "여름 페스티벌"},
                        {"title": "한강 야외수영장 개장", "description": "서울 여름 행사"},
                        {"title": "한강 야외수영장 운영", "description": "물놀이"},
                    ]
                },
            )
        )

        result = await NaverTrendCollector("test-id", "test-secret").collect(fetch_input(), 8)

        # 두 문서가 말한 쪽이 한 문서가 말한 쪽보다 앞선다.
        keywords = [item.keyword for item in result]
        assert keywords[0] == "한강 야외수영장"
        assert "워터밤 서울" in keywords

    async def test_never_calls_datalab(self):
        """이 프로젝트가 쓰는 외부 API는 구글 트렌드·네이버 검색·유튜브 셋뿐이다.

        DataLab 재정렬은 상위 다섯 개의 순서만 바꾸면서 요청을 하나 더 썼다. respx가
        등록되지 않은 호스트를 막으므로, DataLab을 부르면 이 테스트가 그 자리에서 깨진다.
        """
        with respx.mock(assert_all_called=False) as router:
            search = router.get(url__startswith="https://naverapihub.apigw.ntruss.com/search/v1/").mock(
                return_value=httpx.Response(
                    200,
                    json={"items": [{"title": "올리브영 여름 세일", "description": "여름 뷰티 쇼핑"}]},
                )
            )
            datalab = router.post("https://naverapihub.apigw.ntruss.com/search-trend/v1/search").mock(
                return_value=httpx.Response(200, json={"results": []})
            )

            result = await NaverTrendCollector("test-id", "test-secret").collect(
                fetch_input(), 8
            )

        assert search.called
        assert datalab.call_count == 0
        assert "올리브영 여름 세일" in {item.keyword for item in result}

    @respx.mock
    async def test_names_the_missing_search_scope_instead_of_returning_nothing(self):
        """A Naver app with only DataLab enabled answers 401 to every search. The
        old code swallowed it and reported zero keywords, so the panel just
        quietly lost Naver with nothing to explain why."""
        respx.get(url__startswith="https://naverapihub.apigw.ntruss.com/search/v1/").mock(
            return_value=httpx.Response(
                401,
                json={"errorMessage": "Scope Status Invalid", "errorCode": "024"},
            )
        )

        with pytest.raises(RuntimeError, match="검색 API 인증 실패"):
            await NaverTrendCollector("test-id", "test-secret").collect(fetch_input(), 8)

    @respx.mock
    async def test_never_echoes_the_users_own_words_back_as_a_trend(self):
        respx.get(url__startswith="https://naverapihub.apigw.ntruss.com/search/v1/").mock(
            return_value=httpx.Response(
                200, json={"items": [{"title": "AIONA 사용기", "description": "후기·리뷰 작성 방법"}]}
            )
        )

        result = await NaverTrendCollector("test-id", "test-secret").collect(fetch_input(), 8)

        keywords = [item.keyword for item in result]
        assert "AIONA" not in keywords
        assert "후기" not in keywords
        assert keywords == []


class TestYouTubeTrendCollector:
    @respx.mock
    async def test_mines_tags_off_the_korean_trending_chart(self):
        route = respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {"snippet": {"title": "안유진 무대", "tags": ["안유진", "아이브"]}},
                        {"snippet": {"title": "안유진 직캠", "tags": ["안유진"]}},
                        {"snippet": {"title": "안유진 라이브", "tags": ["안유진", "월드컵"]}},
                        {"snippet": {"title": "축구 하이라이트", "tags": ["월드컵", "아이브"]}},
                    ]
                },
            )
        )

        result = await YouTubeTrendCollector("test-youtube-credential").collect(fetch_input(), 8)
        keywords = [item.keyword for item in result]

        params = route.calls[0].request.url.params
        assert params["chart"] == "mostPopular"
        assert params["regionCode"] == "KR"
        # Three of the four trending videos tag it — that is a theme.
        assert keywords == ["안유진"]
        # Two videos each. One artist uploading a clip and its performance cut can
        # reach two; it is not enough to call a trend.
        assert "아이브" not in keywords
        assert "월드컵" not in keywords

    @respx.mock
    async def test_falls_back_to_the_title_when_a_video_has_no_tags(self):
        respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {"snippet": {"title": "김부장 7화 예고편", "tags": []}},
                        {"snippet": {"title": "김부장 결말 vs 원작", "tags": []}},
                        {"snippet": {"title": "김부장 리뷰 모음", "tags": []}},
                    ]
                },
            )
        )

        result = await YouTubeTrendCollector("test-youtube-credential").collect(fetch_input(), 8)
        keywords = [item.keyword for item in result]

        assert keywords == ["김부장"]
        # Episode markers, English particles and publishing furniture are not trends.
        for noise in ("7화", "vs", "예고편", "결말", "원작"):
            assert noise not in keywords


class TestInstagramTrendCollector:
    @respx.mock
    async def test_reads_hashtags_co_occurring_on_top_media(self):
        respx.get(url__startswith="https://graph.facebook.com/v25.0/ig_hashtag_search").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "17843712345"}]})
        )
        respx.get(url__startswith="https://graph.facebook.com/v25.0/17843712345/top_media").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"caption": "여름 준비 #여름휴가 #제주도"},
                        {"caption": "바다 #여름휴가 #호캉스"},
                    ]
                },
            )
        )

        result = await InstagramTrendCollector(
            "test-token", "17841400000000000", "v25.0"
        ).collect(fetch_input(), 8)

        assert result[0].keyword == "여름휴가"
        assert {"제주도", "호캉스"} <= {item.keyword for item in result}

    @respx.mock
    async def test_raises_when_every_lookup_fails_so_a_bad_token_is_visible(self):
        respx.get(url__startswith="https://graph.facebook.com/v25.0/ig_hashtag_search").mock(
            return_value=httpx.Response(400, json={"error": {"message": "invalid token"}})
        )

        with pytest.raises(RuntimeError, match="Instagram"):
            await InstagramTrendCollector("bad-token", "17841400000000000", "v25.0").collect(
                fetch_input(), 8
            )


class TestAggregateTrendProvider:
    async def test_interleaves_the_sources_so_no_one_source_fills_the_panel(self):
        provider = AggregateTrendProvider(
            [
                FakeCollector(TrendSource.GOOGLE_TRENDS, ["민생지원금", "식중독", "미군"]),
                FakeCollector(TrendSource.NAVER_DATALAB, ["참교육", "명태균", "매매"]),
                FakeCollector(TrendSource.YOUTUBE, ["안유진", "건강"]),
            ],
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=4))

        assert [item.source for item in result.trend_keywords] == [
            TrendSource.GOOGLE_TRENDS,
            TrendSource.NAVER_DATALAB,
            TrendSource.YOUTUBE,
            TrendSource.GOOGLE_TRENDS,
        ]
        assert [item.keyword for item in result.trend_keywords] == [
            "민생지원금",
            "참교육",
            "안유진",
            "식중독",
        ]
        assert [item.rank for item in result.trend_keywords] == [1, 2, 3, 4]

    async def test_ids_are_namespaced_per_source(self):
        provider = AggregateTrendProvider(
            [
                FakeCollector(TrendSource.GOOGLE_TRENDS, ["민생지원금", "식중독"]),
                FakeCollector(TrendSource.YOUTUBE, ["안유진"]),
            ],
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=3))

        assert sorted(item.trend_keyword_id for item in result.trend_keywords) == [
            "trend_google_trends_1",
            "trend_google_trends_2",
            "trend_youtube_1",
        ]

    async def test_refreshing_rotates_the_window_so_the_cards_actually_change(self):
        collectors = [
            FakeCollector(TrendSource.GOOGLE_TRENDS, ["민생지원금", "식중독", "미군", "매매"])
        ]

        first = await AggregateTrendProvider(collectors, rotate=lambda size: 0).fetch_trends(
            fetch_input(max_keywords=3)
        )
        second = await AggregateTrendProvider(collectors, rotate=lambda size: 2).fetch_trends(
            fetch_input(max_keywords=3)
        )

        assert [item.keyword for item in first.trend_keywords] == ["민생지원금", "식중독", "미군"]
        assert [item.keyword for item in second.trend_keywords] != [
            item.keyword for item in first.trend_keywords
        ]
        assert second.trend_keywords[0].keyword == "미군"

    async def test_every_card_rotates_including_the_first(self):
        """The first card used to be pinned to the hottest keyword across all
        sources, so 참교육 sat there through every refresh of a post about AIONA."""
        keywords = ["민생지원금", "식중독", "미군", "매매", "참교육", "강풍", "연금", "미국"]

        seen: set[str] = set()
        for offset in range(4):
            collector = FakeCollector(TrendSource.GOOGLE_TRENDS, keywords)
            result = await AggregateTrendProvider(
                collector and [collector],
                rotate=lambda size, offset=offset: offset % size,
            ).fetch_trends(fetch_input(max_keywords=4))
            seen.add(result.trend_keywords[0].keyword)

        assert len(seen) > 1, "첫 카드가 고정되어 있다"

    async def test_a_dead_source_does_not_take_the_panel_down(self):
        provider = AggregateTrendProvider(
            [
                FakeCollector(TrendSource.GOOGLE_TRENDS, [], error=RuntimeError("429 rate limited")),
                FakeCollector(TrendSource.NAVER_DATALAB, ["참교육", "명태균"]),
            ],
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=4))

        assert [item.keyword for item in result.trend_keywords] == ["참교육", "명태균"]

    async def test_the_same_keyword_from_two_sources_collapses_to_one_card(self):
        provider = AggregateTrendProvider(
            [
                FakeCollector(TrendSource.GOOGLE_TRENDS, ["민생지원금"]),
                FakeCollector(TrendSource.NAVER_DATALAB, ["민생지원금", "참교육"]),
            ],
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=4))

        assert [item.keyword for item in result.trend_keywords] == ["민생지원금", "참교육"]
        assert result.trend_keywords[0].source == TrendSource.GOOGLE_TRENDS

    async def test_word_order_duplicates_collapse_to_one_card(self):
        collector = FakeCollector(
            TrendSource.GOOGLE_TRENDS,
            ["폭염 장마", "장마 폭염", "장마와 폭염", "워터밤 서울"],
        )

        result = await AggregateTrendProvider([collector], rotate=lambda size: 0).fetch_trends(
            fetch_input(max_keywords=4)
        )

        signatures = [keyword.token_set_signature for keyword in result.trend_keywords]
        assert signatures.count("장마|폭염") == 1

    async def test_same_topic_variants_and_sentence_fragments_collapse(self):
        collector = FakeCollector(
            TrendSource.GOOGLE_TRENDS,
            [
                "따르면 스파이더맨",
                "스파이더맨",
                "스파이더맨 신작",
                "스파이더맨 영화",
                "스파이더맨 개봉",
                "워터밤 서울",
            ],
        )

        result = await AggregateTrendProvider([collector], rotate=lambda size: 0).fetch_trends(
            fetch_input(max_keywords=4)
        )

        keywords = [keyword.keyword for keyword in result.trend_keywords]
        clusters = [keyword.cluster_id for keyword in result.trend_keywords]
        assert "따르면 스파이더맨" not in keywords
        assert sum(1 for keyword in keywords if "스파이더맨" in keyword) == 1
        assert len(clusters) == len(set(clusters))

    async def test_server_history_excludes_previous_clusters(self):
        collector = FakeCollector(
            TrendSource.GOOGLE_TRENDS,
            [
                "장마 폭염",
                "워터밤 서울",
                "올리브영 세일",
                "프로야구 올스타전",
                "스파이더맨 신작",
                "한강 야외수영장",
                "여름휴가",
                "팝업스토어",
            ],
        )
        provider = AggregateTrendProvider([collector], rotate=lambda size: 0)

        first = await provider.fetch_trends(fetch_input(max_keywords=4))
        second = await provider.fetch_trends(fetch_input(max_keywords=4))

        first_clusters = {keyword.cluster_id for keyword in first.trend_keywords}
        second_clusters = {keyword.cluster_id for keyword in second.trend_keywords}
        assert first_clusters.isdisjoint(second_clusters)

    async def test_a_spelling_variant_of_another_source_collapses_to_one_card(self):
        """"리그 오브 레전드" and "롤" are the one keyword — normalize_keyword sees
        through the spacing and the 롤/LOL synonym, so only one card carries it."""
        provider = AggregateTrendProvider(
            [
                FakeCollector(TrendSource.GOOGLE_TRENDS, ["리그 오브 레전드"]),
                FakeCollector(TrendSource.NAVER_DATALAB, ["롤", "참교육"]),
            ],
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=4))
        keywords = [item.keyword for item in result.trend_keywords]

        assert keywords == ["리그 오브 레전드", "참교육"]
        assert "롤" not in keywords

    async def test_a_recently_shown_keyword_is_not_offered_again(self):
        """The reason 새로고침 exists: the same cards must not come back. The client
        sends what it just showed, and those keywords are skipped (§7)."""
        collector = FakeCollector(
            TrendSource.GOOGLE_TRENDS, ["민생지원금", "식중독", "미군", "매매"]
        )
        provider = AggregateTrendProvider([collector], rotate=lambda size: 0)

        result = await provider.fetch_trends(
            fetch_input(max_keywords=3, exclude_keywords=["민생지원금"])
        )
        keywords = [item.keyword for item in result.trend_keywords]

        assert "민생지원금" not in keywords
        assert len(keywords) == 3

    async def test_low_quality_keywords_are_not_offered_even_from_cached_pools(self):
        collector = FakeCollector(
            TrendSource.GOOGLE_TRENDS,
            ["정보기술", "가상", "강원특별자치도청", "워터밤", "월드컵", "장마"],
        )

        result = await AggregateTrendProvider([collector], rotate=lambda size: 0).fetch_trends(
            fetch_input(max_keywords=3)
        )

        keywords = [item.keyword for item in result.trend_keywords]
        assert keywords == ["워터밤", "월드컵", "장마"]
        assert all(item.normalized_keyword for item in result.trend_keywords)
        assert all(item.trend_score for item in result.trend_keywords)
        assert all(item.trend_reason for item in result.trend_keywords)

    async def test_excluding_sees_through_spacing_and_synonyms(self):
        """A shown keyword coming back spelled differently is still excluded — the
        exclude set is matched on the normalized form, same as the dedup (§6, §7)."""
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, ["리그 오브 레전드", "참교육"])
        provider = AggregateTrendProvider([collector], rotate=lambda size: 0)

        # The client saw it as "롤" last time; it must not return as "리그 오브 레전드".
        result = await provider.fetch_trends(
            fetch_input(max_keywords=1, exclude_keywords=["롤"])
        )

        assert [k.keyword for k in result.trend_keywords] == ["참교육"]

    async def test_exclude_reaches_past_the_window_for_a_fresh_keyword(self):
        """When the whole rotation window is recently-shown, the reserve past it is
        drawn on rather than repeating a card."""
        pool = [f"키워드{n}" for n in range(12)]
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, pool)
        provider = AggregateTrendProvider([collector], rotate=lambda size: 0)

        # The first eight fill the rotation window; exclude every one of them.
        result = await provider.fetch_trends(
            fetch_input(max_keywords=3, exclude_keywords=[f"키워드{n}" for n in range(8)])
        )
        keywords = {k.keyword for k in result.trend_keywords}

        assert len(keywords) == 3
        assert keywords.isdisjoint({f"키워드{n}" for n in range(8)})

    async def test_the_immediately_previous_screen_is_never_returned(self):
        """A thin pool where every keyword was on the previous screen should not
        simply echo it back. The client can keep the old cards and show the error."""
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, ["키워드0", "키워드1"])
        provider = AggregateTrendProvider([collector], rotate=lambda size: 0)

        with pytest.raises(RuntimeError, match="no trend source"):
            await provider.fetch_trends(
                fetch_input(max_keywords=2, exclude_keywords=["키워드0", "키워드1"])
            )

    async def test_raises_when_every_source_comes_back_empty(self):
        provider = AggregateTrendProvider(
            [FakeCollector(TrendSource.GOOGLE_TRENDS, [], error=RuntimeError("down"))]
        )

        with pytest.raises(RuntimeError, match="no trend source"):
            await provider.fetch_trends(fetch_input())


class TestPoolCache:
    """Opening the 제목 step used to hit four external APIs every time — on a post
    whose keywords had been collected a minute earlier, and on every 새로고침."""

    async def test_a_second_open_reuses_the_collected_pool(self):
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, [f"키워드{n}" for n in range(8)])
        provider = AggregateTrendProvider([collector])

        await provider.fetch_trends(fetch_input())
        await provider.fetch_trends(fetch_input())

        assert collector.calls == 1

    async def test_deep_refresh_stays_on_the_stored_pool(self):
        """'새로운 키워드 보기'는 제외 목록이 아무리 쌓여도 소스 API를 다시 부르지 않는다 —
        DB에 쌓인 풀에서 아직 안 본 키워드를 돌려 보여준다. 소스를 다시 부르는 것은
        '수집하기'(force_collect)뿐이다."""
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, [f"키워드{n}" for n in range(20)])
        provider = AggregateTrendProvider([collector])

        await provider.fetch_trends(fetch_input())
        result = await provider.fetch_trends(
            fetch_input(max_keywords=4, exclude_keywords=[f"키워드{n}" for n in range(12)])
        )

        assert collector.calls == 1
        # 제외한 12개 대신 풀에 남은 안 본 키워드가 나온다.
        shown = {k.keyword for k in result.trend_keywords}
        assert shown and shown.isdisjoint({f"키워드{n}" for n in range(12)})

    async def test_refreshes_rotate_through_the_whole_collected_pool(self):
        """수집량 상한 없음: 소스가 44개를 주면 44개가 그대로 저장되고, 새로고침은 그
        전부를 다 보여줄 때까지 돈다(예전에는 소스당 20개로 잘라 나머지를 버렸다)."""
        pool = [f"구체키워드{n}" for n in range(44)]
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, pool)
        provider = AggregateTrendProvider([collector], rotate=lambda size: 0)

        shown: list[str] = []
        screens: list[list[str]] = []
        for _ in range(11):
            result = await provider.fetch_trends(
                fetch_input(max_keywords=4, exclude_keywords=shown)
            )
            screen = [item.keyword for item in result.trend_keywords]
            if screens:
                assert set(screen).isdisjoint(screens[-1])
            screens.append(screen)
            shown = [*screen, *shown]

        assert collector.calls == 1
        assert len(shown) == 44
        assert len({normalize_keyword(keyword) for keyword in shown}) == 44

    async def test_refresh_still_turns_the_panel_over_without_re_collecting(self):
        """Cache, not freeze. The surplus each source returns is what a refresh
        rotates through — that was always where the new cards came from."""
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, [f"키워드{n}" for n in range(8)])

        offsets = iter([0, 3, 5])
        provider = AggregateTrendProvider([collector], rotate=lambda size: next(offsets))

        seen = [
            {k.keyword for k in (await provider.fetch_trends(fetch_input())).trend_keywords}
            for _ in range(3)
        ]

        assert collector.calls == 1
        assert seen[0] != seen[1] != seen[2]

    async def test_refresh_restarts_the_cycle_after_the_pool_is_exhausted(self):
        """서버 이력이 풀 전체를 덮으면, 새로고침이 멈춘 것처럼 같은 목록을 되풀이하는
        대신 이력을 비우고 순환을 다시 시작한다 — 재수집 없이."""
        collector = FakeCollector(
            TrendSource.GOOGLE_TRENDS, [f"구체키워드{n}" for n in range(8)]
        )
        provider = AggregateTrendProvider([collector], rotate=lambda size: 0)

        first = await provider.fetch_trends(fetch_input(max_keywords=4))
        second = await provider.fetch_trends(fetch_input(max_keywords=4))
        third = await provider.fetch_trends(fetch_input(max_keywords=4))

        assert len(first.trend_keywords) == 4
        assert len(second.trend_keywords) == 4
        # 풀 8개를 두 화면에 다 보여준 뒤에도 세 번째 새로고침이 빈 손으로 돌아오지 않는다.
        assert len(third.trend_keywords) == 4
        assert collector.calls == 1

    async def test_collect_button_fetches_sources_again_and_grows_the_pool(self):
        """수집하기(force_collect): 신선한 캐시가 있어도 소스를 다시 부르고, 새 수집분을
        기존 풀에 합쳐 저장한다. 응답은 화면에 그리지 않으므로 노출 이력에도 남지 않아,
        다음 새로고침에서 새 키워드가 그대로 나온다."""
        collector = FakeCollector(
            TrendSource.GOOGLE_TRENDS,
            ["장마 폭염", "워터밤 서울", "올리브영 세일", "프로야구 올스타전"],
        )
        provider = AggregateTrendProvider([collector], rotate=lambda size: 0)

        await provider.fetch_trends(fetch_input(max_keywords=4))
        assert collector.calls == 1

        # 소스에 새 트렌드가 올라온 뒤 수집하기를 누른다.
        collector._keywords = ["스파이더맨 신작", "한강 야외수영장", "여름휴가", "팝업스토어"]
        await provider.fetch_trends(fetch_input(max_keywords=4, force_collect=True))
        assert collector.calls == 2

        # 새로고침(수집 없음): 합쳐진 풀에서, 이미 보여준 것을 빼고 새 키워드가 나온다.
        third = await provider.fetch_trends(fetch_input(max_keywords=4))
        assert collector.calls == 2
        assert {k.keyword for k in third.trend_keywords} == {
            "스파이더맨 신작",
            "한강 야외수영장",
            "여름휴가",
            "팝업스토어",
        }

    async def test_collect_passes_the_stored_pool_as_known_to_the_collector(self):
        """The pool already on file is handed to the collector so it can push
        those keywords behind fresh ones instead of a plain frequency sort
        re-picking the same top 20 every call (§ 팀원 PC에서 풀이 작게 보이던 원인)."""
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, ["장마 폭염", "워터밤 서울"])
        provider = AggregateTrendProvider([collector], rotate=lambda size: 0)

        await provider.fetch_trends(fetch_input(max_keywords=4))
        assert collector.last_known == frozenset()

        collector._keywords = ["스파이더맨 신작", "한강 야외수영장"]
        await provider.fetch_trends(fetch_input(max_keywords=4, force_collect=True))

        assert collector.last_known == {
            normalize_keyword("장마 폭염"),
            normalize_keyword("워터밤 서울"),
        }

    async def test_trending_shares_a_pool_across_topics(self):
        """추천어는 소재 무관 공용 풀 — 다른 소재의 글도 같은 풀을 재사용해 재수집하지 않는다
        (trend_keywords에 누적된 것을 그대로 쓴다). 소재가 바뀔 때마다 매번 API를 부르지 않는다."""
        collector = FakeCollector(TrendSource.NAVER_DATALAB, [f"키워드{n}" for n in range(8)])
        provider = AggregateTrendProvider([collector])

        await provider.fetch_trends(fetch_input())
        other = fetch_input()
        other.input.topic = "완전히 다른 소재"
        await provider.fetch_trends(other)

        assert collector.calls == 1

    async def test_material_related_does_not_share_a_pool_across_topics(self):
        """소재 관련어는 사용자 단어로 검색하므로, 주제가 다르면 다른 풀을 쓴다."""
        collector = FakeCollector(TrendSource.NAVER_DATALAB, [f"키워드{n}" for n in range(8)])
        provider = AggregateTrendProvider([collector])

        await provider.fetch_trends(fetch_input(mode=TrendMode.MATERIAL_RELATED))
        after_first_topic = collector.calls
        assert after_first_topic >= 1

        other = fetch_input(mode=TrendMode.MATERIAL_RELATED)
        other.input.topic = "완전히 다른 소재"
        await provider.fetch_trends(other)

        # 호출 횟수가 아니라 "두 번째 소재가 자기 수집을 했다"가 요점이다. 회차 수는
        # 보충 정책이 정하고(초기 수집 + 부족하면 확장 수집 1회), 여기서 볼 것이 아니다.
        assert collector.calls > after_first_topic

    async def test_trending_stays_on_the_stored_pool_even_past_the_ttl(self):
        """최신순은 DB 우선이다: 저장된 풀이 얼마나 오래됐든 자동 재수집하지 않는다.
        소스 API를 다시 부르는 것은 '수집하기'(force_collect)뿐이다."""
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, [f"키워드{n}" for n in range(8)])
        now = [0.0]
        provider = AggregateTrendProvider(
            [collector], cache=InMemoryPoolCache(clock=lambda: now[0]), ttl_seconds=600.0
        )

        await provider.fetch_trends(fetch_input())
        assert collector.calls == 1

        now[0] = 601.0
        await provider.fetch_trends(fetch_input())
        assert collector.calls == 1

        await provider.fetch_trends(fetch_input(force_collect=True))
        assert collector.calls == 2

    async def test_a_source_that_starts_failing_keeps_serving_its_last_pool(self):
        """저장된 풀이 있으면 소스가 죽어도 패널이 비지 않는다 — DB 우선이라 소스를
        부르지도 않는다."""
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, [f"키워드{n}" for n in range(8)])
        now = [0.0]
        provider = AggregateTrendProvider(
            [collector], cache=InMemoryPoolCache(clock=lambda: now[0]), ttl_seconds=10.0
        )

        first = await provider.fetch_trends(fetch_input())
        assert first.trend_keywords

        collector._error = RuntimeError("SerpApi quota exceeded")
        now[0] = 100.0

        stale = await provider.fetch_trends(fetch_input())
        assert {k.keyword for k in stale.trend_keywords}
        assert collector.calls == 1


class TestNounPhrase:
    """Google Trends hands back whole search queries, not words, and they were used
    verbatim — so whatever people typed came through."""

    def test_keeps_a_phrase_that_is_all_nouns(self):
        assert is_noun_phrase("코스피 폭락")
        assert is_noun_phrase("이재명 대통령")
        assert is_noun_phrase("민생지원금")
        # The two-letter floor that tokenize applies to prose would throw this away,
        # but a search query saying "AI" means AI.
        assert is_noun_phrase("AI 검색")

    def test_drops_a_phrase_with_a_verb_or_a_particle_in_it(self):
        assert not is_noun_phrase("비 오는 날")
        assert not is_noun_phrase("날씨가 좋다")
        assert not is_noun_phrase("다양한 방법")

    def test_drops_a_phrase_carrying_filler(self):
        # 오늘 and 지금 are nouns, and name nothing.
        assert not is_noun_phrase("오늘 날씨")
        assert not is_noun_phrase("지금 뜨는 것")

    def test_drops_dates(self):
        assert not is_noun_phrase("7월 14일")


class FakeRedis:
    """Enough of the redis asyncio client to prove what we store and read back."""

    def __init__(self, fail: bool = False):
        self.store: dict[str, str] = {}
        self.fail = fail

    async def get(self, key):
        if self.fail:
            raise ConnectionError("Connection refused")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("Connection refused")
        self.store[key] = value


class TestRedisPoolCache:
    async def test_a_pool_survives_a_restart(self):
        """The point of Redis over process memory: the cache is not the process."""
        redis = FakeRedis()
        keywords = [f"키워드{n}" for n in range(8)]

        first = FakeCollector(TrendSource.GOOGLE_TRENDS, keywords)
        await AggregateTrendProvider([first], cache=RedisPoolCache(redis)).fetch_trends(
            fetch_input()
        )
        assert first.calls == 1

        # A brand new provider, as a restarted process would build.
        second = FakeCollector(TrendSource.GOOGLE_TRENDS, keywords)
        result = await AggregateTrendProvider(
            [second], cache=RedisPoolCache(redis)
        ).fetch_trends(fetch_input())

        assert second.calls == 0
        assert result.trend_keywords

    async def test_redis_being_down_does_not_take_trend_collection_down(self):
        """A cache is an optimisation. The sources are still there."""
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, [f"키워드{n}" for n in range(8)])
        provider = AggregateTrendProvider(
            # 실제 디스크 캐시를 건드리지 않도록 폴백은 격리된 메모리 캐시로.
            [collector], cache=RedisPoolCache(FakeRedis(fail=True), fallback=InMemoryPoolCache())
        )

        result = await provider.fetch_trends(fetch_input())

        assert result.trend_keywords
        assert collector.calls == 1

    async def test_what_lands_in_redis_is_readable(self):
        redis = FakeRedis()
        collector = FakeCollector(TrendSource.NAVER_DATALAB, ["제주", "여름휴가"])
        await AggregateTrendProvider([collector], cache=RedisPoolCache(redis)).fetch_trends(
            fetch_input()
        )

        [(key, raw)] = redis.store.items()
        payload = json.loads(raw)

        assert key.startswith("trend:pool:NAVER_DATALAB:")
        assert [item["keyword"] for item in payload["pool"]] == ["제주", "여름휴가"]
        assert "at" in payload


class TestStartupHonesty:
    """시작 로그가 "캐시: Redis"라고 말하면 정말 Redis여야 한다.

    from_url은 연결하지 않는다 — 첫 명령에서야 연결한다. 그래서 Redis를 꺼 둔 채로도
    시작 로그는 "캐시: Redis"라고 말했고, 진실은 한참 뒤 첫 트렌드 수집에서야 드러났다.
    거짓인 로그는 없느니만 못하다.
    """

    def test_it_says_redis_only_when_redis_answers(self, monkeypatch):
        import redis

        class Answering:
            def ping(self):
                return True

        monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda *a, **k: Answering()))

        assert create_pool_cache("redis://localhost:6379").name == "Redis"

    def test_it_says_memory_when_redis_is_not_there(self, monkeypatch):
        import redis

        class Refusing:
            def ping(self):
                raise ConnectionError("Connection refused")

        monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda *a, **k: Refusing()))

        cache = create_pool_cache("redis://localhost:6379")

        # Redis가 죽으면 디스크로 저하한다 — 메모리로 떨어지면 재시작마다 다시 수집한다.
        assert cache.name == "디스크(Redis 연결 실패)"

    def test_no_redis_url_is_disk_cache(self):
        # Redis가 없으면 디스크 파일 캐시를 쓴다 — 서버를 껐다 켜도 수집분이 남는다.
        assert create_pool_cache(None).name == "디스크"

    async def test_disk_cache_survives_a_new_instance(self, tmp_path):
        # 재시작을 흉내: 다른 인스턴스가 같은 폴더에서 저장분을 읽어 온다.
        from app.llm.trends.base import CollectedKeyword
        from app.llm.trends.cache import DiskPoolCache

        now = [1000.0]
        keywords = [CollectedKeyword(keyword="정보처리기사", score=90.0, rank=1, category="교육")]
        await DiskPoolCache(tmp_path, clock=lambda: now[0]).set("key", keywords)

        now[0] = 1300.0  # 5분 뒤 (TTL 1시간 안)
        reloaded = await DiskPoolCache(tmp_path, clock=lambda: now[0]).get("key")

        assert reloaded is not None
        assert [k.keyword for k in reloaded.keywords] == ["정보처리기사"]
        assert reloaded.is_fresh()


class StubRanker:
    """Scores (and optionally categorises) by fixed tables so ordering and the
    category spread can be asserted on.

    소재/목적/페르소나 부분 점수는 따로 주지 않으면 종합 점수를 그대로 쓴다 — 실제 채점도
    "모든 축에 고루 관련"이 기본형이고, 축별 게이트·필터 테스트만 표를 따로 준다.
    결합 가능성(blendability)은 이전 응답과의 호환을 위해 스텁에 남겨 둔다."""

    def __init__(
        self,
        scores: dict[str, float],
        error: Exception | None = None,
        default: float | None = None,
        categories: dict[str, str] | None = None,
        subject_scores: dict[str, float] | None = None,
        purpose_scores: dict[str, float] | None = None,
        persona_scores: dict[str, float] | None = None,
        blendability_scores: dict[str, float] | None = None,
        relation_types: dict[str, RelationType] | None = None,
    ):
        self._scores = scores
        # 표에 없는 키워드의 기본 점수. 확장 후보처럼 이름을 미리 알 수 없는 경우에 쓴다.
        self._default = default
        self._error = error
        self._categories = categories or {}
        self._subject = subject_scores
        self._purpose = purpose_scores
        self._persona = persona_scores
        self._blendability = blendability_scores
        self._relations = relation_types
        self.calls = 0
        self.last_input = None

    def _axis(self, table: dict[str, float] | None, word: str) -> float:
        base = self._scores.get(word, self._default if self._default is not None else 0.0)
        return table.get(word, base) if table is not None else base

    def _relation(self, word: str) -> RelationType:
        """관계 유형을 따로 주지 않으면 소재 점수에서 유도한다.

        실제 채점에서도 둘은 함께 움직인다(관계 유형이 소재 점수의 상한을 정한다). 유형별
        하한(MATERIAL_RELATION_MIN_SUBJECT)과 같은 경계를 쓰므로, 점수만 지정한 테스트는
        "그 점수라면 당연히 이 유형"인 판정을 받는다. 유형 자체를 검증하는 테스트만
        relation_types로 직접 지정한다."""
        if self._relations is not None and word in self._relations:
            return self._relations[word]
        subject = self._axis(self._subject, word)
        for relation, minimum in MATERIAL_RELATION_MIN_SUBJECT.items():
            if subject >= minimum:
                return relation
        return RelationType.NONE

    async def rank_keywords(self, relevance_input):
        self.calls += 1
        self.last_input = relevance_input
        if self._error:
            raise self._error
        return {
            word: KeywordJudgment(
                relevance=self._scores.get(
                    word, self._default if self._default is not None else 0.0
                ),
                category=self._categories.get(word),
                subject_relevance=self._axis(self._subject, word),
                purpose_relevance=self._axis(self._purpose, word),
                persona_relevance=self._axis(self._persona, word),
                blendability=(
                    self._blendability.get(word) if self._blendability is not None else None
                ),
                relation_type=self._relation(word),
            )
            for word in relevance_input.keywords
        }


def scored_material(
    keyword: str,
    subject: float,
    demand: float = 50.0,
    relation: RelationType | None = None,
) -> MaterialKeyword:
    """이미 채점이 끝난 소재 키워드. 저장분을 재사용하는 경로를 검증할 때 쓴다.

    관계 유형을 생략하면 소재 점수에서 유도한다(StubRanker._relation과 같은 규칙) — 실제
    채점에서도 둘은 함께 움직이므로, 점수만 지정한 테스트가 모순된 판정을 만들지 않는다."""
    if relation is None:
        relation = next(
            (
                candidate
                for candidate, minimum in MATERIAL_RELATION_MIN_SUBJECT.items()
                if subject >= minimum
            ),
            RelationType.NONE,
        )
    return MaterialKeyword(
        keyword=keyword,
        normalized_keyword=normalize_keyword(keyword),
        source=TrendSource.NAVER_DATALAB,
        sources=[TrendSource.NAVER_DATALAB],
        demand_score=demand,
        relation_type=relation,
        subject_relevance=subject,
        relevance=subject,
        prompt_version=RELEVANCE_PROMPT_VERSION,
    )


class TestKeywordRelevance:
    """Trending is not relevant. Google reports what all of Korea is searching for,
    which is why the panel recommended 참교육 to someone writing about AIONA."""

    async def test_the_recommended_card_is_the_one_that_fits_the_subject(self):
        collector = FakeCollector(
            TrendSource.NAVER_DATALAB, ["참교육", "노코드", "강풍", "생산성"]
        )
        # 참교육 is the hottest — FakeCollector scores by position — and the least
        # relevant. 소재 관련어(MATERIAL) 탭에서만 관련도가 순위를 정한다: 추천어는 트렌드
        # 강도로만 뽑으므로 참교육이 앞선다.
        ranker = StubRanker({"참교육": 5, "노코드": 95, "강풍": 2, "생산성": 80})

        result = await AggregateTrendProvider([collector], ranker=ranker).fetch_trends(
            fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED)
        )

        assert result.trend_keywords[0].keyword == "노코드"
        assert result.trend_keywords[0].relevance == 95

    async def test_trending_never_scores_by_material_even_on_repeated_reads(self):
        """최신순은 소재가 무엇이든 실시간 인기만 보여주므로 LLM 채점을 호출하지 않는다."""
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, [f"키워드{n}" for n in range(8)])
        ranker = StubRanker({})
        provider = AggregateTrendProvider([collector], ranker=ranker)

        await provider.fetch_trends(fetch_input())
        await provider.fetch_trends(fetch_input())
        await provider.fetch_trends(fetch_input())

        assert ranker.calls == 0
        assert collector.calls == 1

    async def test_trending_does_not_create_or_refresh_a_relevance_cache(self):
        """첫 진입부터 새로고침까지 관련도 캐시 자체를 읽거나 만들지 않는다."""
        collector = FakeCollector(
            TrendSource.GOOGLE_TRENDS, [f"구체키워드{n}" for n in range(8)]
        )
        ranker = StubRanker({f"구체키워드{n}": 70 for n in range(8)})
        provider = AggregateTrendProvider([collector], ranker=ranker)

        first = await provider.fetch_trends(fetch_input(max_keywords=4))
        result = await provider.fetch_trends(fetch_input(max_keywords=4))

        assert ranker.calls == 0
        assert first.refreshing is False and result.refreshing is False
        assert not any(
            key.startswith("trend:relevance:")
            for key in provider._cache._entries  # type: ignore[attr-defined]
        )
        assert len(result.trend_keywords) == 4

    async def test_an_unavailable_ranker_does_not_affect_trending(self):
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, ["참교육", "노코드"])
        ranker = StubRanker({}, error=RuntimeError("anthropic 529"))

        result = await AggregateTrendProvider([collector], ranker=ranker).fetch_trends(
            fetch_input()
        )

        # 최신순은 랭커를 호출하지 않고 인기 신호만 사용한다.
        assert [k.keyword for k in result.trend_keywords] == ["참교육", "노코드"]
        assert all(k.relevance is None for k in result.trend_keywords)
        assert ranker.calls == 0

    async def test_the_cards_come_from_the_hottest_end_of_the_pool(self):
        """최신순 회전은 관련도가 아니라 실시간 인기 상위 창 안에서만 움직인다."""
        pool = [f"키워드{n}" for n in range(20)]
        # FakeCollector의 앞 8개가 인기 상위 회전 창이다.
        scores = {f"키워드{n}": (90 - n) if n < 8 else 3 for n in range(20)}
        ranker = StubRanker(scores)

        for offset in range(8):
            collector = FakeCollector(TrendSource.GOOGLE_TRENDS, pool)
            result = await AggregateTrendProvider(
                [collector], rotate=lambda size, offset=offset: offset % size, ranker=ranker
            ).fetch_trends(fetch_input(max_keywords=4))

            for keyword in result.trend_keywords:
                assert int(keyword.keyword.removeprefix("키워드")) < 8
            assert ranker.calls == 0

    async def test_the_hottest_keyword_is_not_pinned_to_the_first_card(self):
        """인기 상위 창을 회전하므로 같은 카드가 첫 자리에 계속 고정되지 않는다."""
        pool = [f"키워드{n}" for n in range(20)]
        ranker = StubRanker({f"키워드{n}": (90 - n) if n < 8 else 3 for n in range(20)})

        first_cards = set()
        for offset in range(8):
            collector = FakeCollector(TrendSource.GOOGLE_TRENDS, pool)
            result = await AggregateTrendProvider(
                [collector], rotate=lambda size, offset=offset: offset % size, ranker=ranker
            ).fetch_trends(fetch_input(max_keywords=4))
            first_cards.add(result.trend_keywords[0].keyword)

        assert len(first_cards) > 1, "첫 카드가 여전히 고정이다"

    async def test_trending_cards_have_no_material_relevance_score(self):
        """최신순 응답은 소재별 관련도 값을 만들지 않고 인기 상위 창만 회전한다."""
        pool = ["참교육", "강풍", "연금", "미군", "식중독", "매매", "안유진", "김한석"]
        ranker = StubRanker(dict.fromkeys(pool, 5.0))

        first_cards = set()
        for offset in range(4):
            collector = FakeCollector(TrendSource.GOOGLE_TRENDS, pool)
            result = await AggregateTrendProvider(
                [collector], rotate=lambda size, offset=offset: offset % size, ranker=ranker
            ).fetch_trends(fetch_input(max_keywords=4))
            first_cards.add(result.trend_keywords[0].keyword)
            assert all(k.relevance is None for k in result.trend_keywords)

        assert ranker.calls == 0
        assert len(first_cards) > 1, "최신순 첫 카드가 고정됐다"


class TestCategoryDiversity:
    """최신순은 카테고리 LLM을 건너뛰고, 소재 관련순만 채점 메타데이터를 받는다."""

    async def test_trending_skips_llm_category_classification(self):
        collector = FakeCollector(
            TrendSource.GOOGLE_TRENDS, ["롤", "페이커", "티원", "월드컵", "손흥민", "워터밤"]
        )
        ranker = StubRanker(
            {"롤": 95, "페이커": 90, "티원": 85, "월드컵": 80, "손흥민": 75, "워터밤": 70},
            categories={
                "롤": "게임·IT",
                "페이커": "게임·IT",
                "티원": "게임·IT",
                "월드컵": "스포츠·대회",
                "손흥민": "스포츠·대회",
                "워터밤": "공연·축제·행사",
            },
        )

        result = await AggregateTrendProvider(
            [collector], rotate=lambda size: 0, ranker=ranker
        ).fetch_trends(fetch_input(max_keywords=4))

        assert ranker.calls == 0
        assert [k.keyword for k in result.trend_keywords] == ["롤", "페이커", "티원", "월드컵"]
        assert all(k.category is None for k in result.trend_keywords)

    async def test_diversity_never_forces_a_much_less_relevant_keyword(self):
        """Diversity reshuffles within the relevant window; it does not reach past it
        to drag up a low-trend keyword just because its category is missing (§12).

        소재 관련어(MATERIAL) 탭에서만 관련도 하한이 후보를 제한한다 — 추천어는 관련도로
        거르지 않으므로 이 불변식이 성립하는 곳은 소재 관련어다."""
        pool = [f"키워드{n}" for n in range(12)]
        # The first eight (the rotation window) are all one field and relevant; the
        # unique-category keywords sit far down the pool, barely trending.
        scores = {f"키워드{n}": (90 - n) if n < 8 else 5 for n in range(12)}
        categories = {f"키워드{n}": ("게임·IT" if n < 8 else "음식·맛집") for n in range(12)}
        ranker = StubRanker(scores, categories=categories)

        result = await AggregateTrendProvider(
            [FakeCollector(TrendSource.GOOGLE_TRENDS, pool)],
            rotate=lambda size: 0,
            ranker=ranker,
        ).fetch_trends(fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED))

        keywords = {k.keyword for k in result.trend_keywords}
        assert all(k.category == "게임·IT" for k in result.trend_keywords)
        assert keywords.isdisjoint({f"키워드{n}" for n in range(8, 12)})

    async def test_no_categories_leaves_selection_exactly_as_before(self):
        """When scoring returns no categories, the diversity step must be a no-op so
        the source-interleaving order is untouched."""
        provider = AggregateTrendProvider(
            [
                FakeCollector(TrendSource.GOOGLE_TRENDS, ["민생지원금", "식중독", "미군"]),
                FakeCollector(TrendSource.NAVER_DATALAB, ["참교육", "명태균", "매매"]),
                FakeCollector(TrendSource.YOUTUBE, ["안유진", "건강"]),
            ],
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(fetch_input(max_keywords=4))

        assert [item.keyword for item in result.trend_keywords] == [
            "민생지원금",
            "참교육",
            "안유진",
            "식중독",
        ]


class TestSeasonalContext:
    async def test_the_current_date_is_handed_to_the_relevance_model(self):
        """The model cannot judge what is in season without knowing the date, so the
        collection date rides along with the keywords (§10)."""
        collector = FakeCollector(TrendSource.NAVER_DATALAB, ["워터밤", "장마"])
        ranker = StubRanker({"워터밤": 80, "장마": 70})

        await AggregateTrendProvider([collector], ranker=ranker).fetch_trends(
            fetch_input(mode=TrendMode.MATERIAL_RELATED)
        )

        assert ranker.last_input is not None
        assert ranker.last_input.as_of, "관련도 채점에 현재 날짜가 전달되지 않았다"


class TestSubjectEcho:
    """소재를 되풀이할 뿐인 키워드(메아리) 판정은 관련도 점수가 아니라 토큰으로 한다: 소재명을
    범용어와 조합한 것은 트렌드도 관련 개념도 아니다(§9)."""

    def test_a_keyword_that_is_just_the_product_name_dressed_up_is_an_echo(self):
        topic_tokens = _subject_echo_tokens(fetch_input())  # topic="AIONA"
        for echo in ("AI AIONA", "AIONA AI", "생성형 AIONA", "AIONA 트렌드", "AIONA 기반 AI"):
            assert _is_subject_echo(keyword_signature(echo), topic_tokens), echo

    def test_a_string_of_only_generic_words_is_an_echo(self):
        # "생성형 AI", "AI 트렌드"는 소재명조차 없지만 순수 범용어라 트렌드가 아니다.
        topic_tokens = _subject_echo_tokens(fetch_input())
        for generic in ("생성형 AI", "AI 트렌드", "AI 기반", "최신 AI 플랫폼"):
            assert _is_subject_echo(keyword_signature(generic), topic_tokens), generic

    def test_a_real_related_concept_is_not_an_echo(self):
        # 소재와 관련은 있지만 소재명의 재배열이 아닌 개념은 남긴다 — 소재 관련어 탭의 알맹이.
        topic_tokens = _subject_echo_tokens(fetch_input())
        for concept in ("멀티 LLM", "AI 에이전트", "ChatGPT", "노코드", "업무 자동화"):
            assert not _is_subject_echo(keyword_signature(concept), topic_tokens), concept

    def test_a_current_trend_unrelated_to_the_subject_is_not_an_echo(self):
        topic_tokens = _subject_echo_tokens(fetch_input())
        for trend in ("워터밤", "여름휴가", "프로야구 올스타전"):
            assert not _is_subject_echo(keyword_signature(trend), topic_tokens), trend


class TestMechanicalEcho:
    """소재 단어에 추천·인기 같은 접미사만 기계적으로 붙인 조합은 자연스러운 검색어가 아니다.
    그러나 소재 뒤에 실제 의미어가 남는 것은 진짜 검색어이므로 살린다(예: '빵집 추천')."""

    def test_subject_plus_a_mechanical_suffix_is_an_echo(self):
        subject = _compact_keyword("빵")
        for echo in ("빵추천", "빵인기", "빵순위", "빵추천모음", "빵 추천"):
            assert _is_mechanical_echo(echo, subject), echo

    def test_a_multiword_subject_mechanical_combo_is_an_echo(self):
        subject = _compact_keyword("국내여행")
        for echo in ("국내여행추천", "국내여행 추천", "국내여행인기순위"):
            assert _is_mechanical_echo(echo, subject), echo

    def test_a_natural_search_phrase_is_kept(self):
        subject = _compact_keyword("빵")
        # 소재 뒤에 실제 의미어(집·소금·지순례)가 남거나, 소재가 앞이 아니면 자연스러운 검색어다.
        for keep in ("빵집", "빵집 추천", "소금빵 맛집", "빵지순례", "서울 빵집", "베이글 맛집"):
            assert not _is_mechanical_echo(keep, subject), keep


class TestTrendModes:
    """추천어(TRENDING)는 실시간 인기만, 소재 관련어(MATERIAL_RELATED)는 관련도 채점을
    사용한다. 화면이 소재 메아리와 NAVER로 도배되던 문제(§1)를 여기서 잡는다."""

    def _bug_scenario(self):
        """AIONA(=AI 제품) 버그 재현: 구글은 실제 트렌드를, 네이버는 소재 메아리와 관련 개념
        하나를 준다. relevance는 메아리(90+)를 관련 개념(80), 무관 트렌드(<10)보다 높게 매긴다."""
        collectors = [
            FakeCollector(TrendSource.GOOGLE_TRENDS, ["워터밤", "여름휴가"]),
            FakeCollector(
                TrendSource.NAVER_DATALAB,
                ["AI AIONA", "생성형 AI", "멀티 LLM", "AIONA 트렌드"],
            ),
        ]
        ranker = StubRanker(
            {
                "워터밤": 6,
                "여름휴가": 8,
                "AI AIONA": 95,
                "생성형 AI": 90,
                "멀티 LLM": 80,
                "AIONA 트렌드": 92,
            }
        )
        return collectors, ranker

    async def test_trending_keeps_real_trends_regardless_of_subject(self):
        """최신순(추천어): 지금 실제로 뜨는 트렌드는 소재와 무관해도 노출한다(§4·§6). 워터밤·
        여름휴가는 소재(AIONA)와 관련이 없지만 실제 트렌드이므로 남고, 소재 메아리(AI AIONA·생성형
        AI·AIONA 트렌드)만 제외한다. 소재 관련도로 실제 트렌드를 거르지 않는다."""
        collectors, ranker = self._bug_scenario()

        result = await AggregateTrendProvider(collectors, ranker=ranker).fetch_trends(
            fetch_input(max_keywords=4, mode=TrendMode.TRENDING)
        )
        keywords = [k.keyword for k in result.trend_keywords]

        assert result.mode == TrendMode.TRENDING
        # 소재 메아리는 어떤 경우에도 안 나온다.
        for echo in ("AI AIONA", "생성형 AI", "AIONA 트렌드"):
            assert echo not in keywords
        # 소재와 무관해도 실제 트렌드(워터밤·여름휴가)는 관련 개념(멀티 LLM)과 함께 노출된다.
        assert set(keywords) == {"멀티 LLM", "워터밤", "여름휴가"}

    async def test_material_keeps_only_subject_related_and_drops_echoes(self):
        """소재 관련어: 소재명 재배열(메아리)은 빼고, 소재 관련도 게이트를 통과한 개념만
        노출한다. 무관한 실시간 트렌드(워터밤)는 여기 오지 않는다."""
        collectors, ranker = self._bug_scenario()

        result = await AggregateTrendProvider(collectors, ranker=ranker).fetch_trends(
            fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED)
        )
        keywords = [k.keyword for k in result.trend_keywords]

        assert result.mode == TrendMode.MATERIAL_RELATED
        assert "멀티 LLM" in keywords
        for echo in ("AI AIONA", "생성형 AI", "AIONA 트렌드"):
            assert echo not in keywords
        # 소재와 무관한 트렌드는 소재 관련도 하한(30 = '아무 상관 없음' 경계)에 못 미쳐 제외된다.
        assert "워터밤" not in keywords and "여름휴가" not in keywords
        assert result.trend_keywords[0].keyword == "멀티 LLM"

    async def test_the_two_modes_keep_separate_exposure_history(self):
        """한 탭에서 새로고침해도 다른 탭이 방금 보여준 키워드까지 제외하지 않는다(§14):
        노출 이력을 모드별로 나눈다."""
        collectors, ranker = self._bug_scenario()
        provider = AggregateTrendProvider(collectors, ranker=ranker, rotate=lambda size: 0)

        await provider.fetch_trends(fetch_input(mode=TrendMode.TRENDING))
        # 소재 관련어를 열면, 추천어에서 방금 보여준 이력에 막히지 않고 멀티 LLM이 나와야 한다.
        material = await provider.fetch_trends(
            fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED)
        )

        assert "멀티 LLM" in [k.keyword for k in material.trend_keywords]

    async def test_material_returns_fewer_rather_than_padding_with_unrelated(self):
        """소재 관련어: 관련 후보가 적으면 무관 키워드로 16개를 채우지 않고 적은 개수만 반환한다.
        소재 관련도 30 미만('아무 상관 없음')은 노출하지 않는다."""
        collector = FakeCollector(
            TrendSource.NAVER_DATALAB, ["노코드", "워터밤", "여름휴가", "프로야구"]
        )
        ranker = StubRanker({"노코드": 85, "워터밤": 5, "여름휴가": 4, "프로야구": 3})

        result = await AggregateTrendProvider(
            [collector], ranker=ranker, rotate=lambda size: 0
        ).fetch_trends(fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED))

        # 관련도 30+는 노코드뿐이다. 무관 트렌드로 4개를 채우지 않는다.
        assert [k.keyword for k in result.trend_keywords] == ["노코드"]

    async def test_material_returns_empty_instead_of_unrelated_when_nothing_relates(self):
        """소재와 직접 관련된 후보가 하나도 없으면 빈 패널이 정상이다 — 오류를 내거나 무관
        키워드로 채우지 않는다(추천어와 달리 빈 결과를 허용한다)."""
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, ["워터밤", "여름휴가"])
        ranker = StubRanker({"워터밤": 5, "여름휴가": 8})

        result = await AggregateTrendProvider([collector], ranker=ranker).fetch_trends(
            fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED)
        )

        assert result.trend_keywords == []
        assert result.mode == TrendMode.MATERIAL_RELATED

    async def test_material_ranks_by_demand_among_equally_related(self):
        """소재 관련순(§B): 소재 관련도가 같으면 검색 관심도(demand=정규화된 소스 신호)가 높은
        키워드가 앞선다. demand는 네이버 검색량 재정렬 순위 등 관측 가능한 관심도 대체 지표다."""
        # 둘 다 관련도 80으로 같다. demand는 소스 순위로 갈린다(FakeCollector는 앞쪽이 높은 점수).
        collector = FakeCollector(TrendSource.NAVER_DATALAB, ["소금빵 맛집", "베이글 맛집"])
        ranker = StubRanker({"소금빵 맛집": 80, "베이글 맛집": 80})

        result = await AggregateTrendProvider(
            [collector], ranker=ranker, rotate=lambda size: 0
        ).fetch_trends(fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED))

        # 관련도가 같으니 검색 관심도가 높은(소스 상위) '소금빵 맛집'이 먼저 온다.
        assert [k.keyword for k in result.trend_keywords] == ["소금빵 맛집", "베이글 맛집"]

    async def test_material_gate_blocks_high_demand_low_relevance(self):
        """관심도(demand)가 아무리 높아도 소재 관련도가 '아무 상관 없음'(30 미만)이면 아예
        노출되지 않는다 — 점수를 합산해 상쇄하는 방식이 아니라 게이트라서다."""
        # 관련도 높은 '노코드'(90)는 demand가 낮고(뒤 순위), 무관한 '워터밤'(25)은 demand가
        # 가장 높다(앞 순위). 워터밤은 소재 하한(30)에 못 미쳐 제외되고, 노코드만 남는다.
        collector = FakeCollector(TrendSource.NAVER_DATALAB, ["워터밤", "노코드"])
        ranker = StubRanker({"워터밤": 25, "노코드": 90})

        result = await AggregateTrendProvider(
            [collector], ranker=ranker, rotate=lambda size: 0
        ).fetch_trends(fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED))

        assert [k.keyword for k in result.trend_keywords] == ["노코드"]


class TestMaterialSubjectGate:
    """소재 관련순 = 소재와 관련된 키워드면 전부, 관련도 높은 순 (2026-07-22 개편).

    게이트는 LLM 소재 관련도 하나뿐이고, 하한(기본 30)은 루브릭의 '아무 상관 없음' 경계다.
    예전의 소재 AND 목적 AND 페르소나 게이트는 니치 소재에서 후보를 전멸시켜 폐기했다.
    정렬은 소재 관련도 내림차순(동점은 검색 관심도), 1위가 추천이다."""

    async def test_a_keyword_unrelated_to_the_subject_is_excluded(self):
        """'아무 상관 없음'(소재 관련도 30 미만) 판정만 노출에서 빠진다."""
        collector = FakeCollector(TrendSource.NAVER_DATALAB, ["무관실검", "모두관련"])
        ranker = StubRanker({"무관실검": 5, "모두관련": 80})

        result = await AggregateTrendProvider(
            [collector], ranker=ranker, rotate=lambda size: 0
        ).fetch_trends(fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED))

        assert [k.keyword for k in result.trend_keywords] == ["모두관련"]

    async def test_purpose_and_persona_no_longer_gate_material_keywords(self):
        """글 목적·페르소나 점수가 낮아도 소재와 관련이 있으면 노출한다 — 옛 AND 게이트가
        니치 소재에서 후보를 전멸시키던 문제의 반대 방향 고정. 두 점수는 툴팁 표기용으로만
        채점된다."""
        collector = FakeCollector(TrendSource.NAVER_DATALAB, ["축불일치", "모두관련"])
        ranker = StubRanker(
            {"축불일치": 95, "모두관련": 80},
            purpose_scores={"축불일치": 10},
            persona_scores={"축불일치": 5},
        )

        result = await AggregateTrendProvider(
            [collector], ranker=ranker, rotate=lambda size: 0
        ).fetch_trends(fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED))

        # 둘 다 노출되고, 소재 관련도가 높은 쪽(95)이 먼저다.
        assert [k.keyword for k in result.trend_keywords] == ["축불일치", "모두관련"]

    async def test_eligible_keywords_sort_by_subject_relevance_not_stored_score(self):
        """소재 관련순의 정렬 축은 저장된 수요 점수가 아니라 소재 관련도다 — 탭 이름 그대로."""
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key("AIONA"),
            [
                scored_material("자동화", subject=60.0, demand=99.0),
                scored_material("노코드", subject=95.0, demand=10.0),
            ],
        )
        collector = FakeCollector(TrendSource.NAVER_DATALAB, ["다른것"])
        provider = AggregateTrendProvider(
            [collector], ranker=StubRanker({}), material_store=store, rotate=lambda size: 0
        )

        result = await provider.fetch_trends(
            fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED)
        )

        assert [k.keyword for k in result.trend_keywords] == ["노코드", "자동화"]

    async def test_a_stored_material_pool_serves_without_calling_sources(self):
        """소재 풀이 목표치를 채우고 있으면 소스 API를 부르지 않는다 — 같은 소재의 두 번째
        글이 수집·채점을 처음부터 다시 하지 않는다는 것이 소재 단위 저장의 목적이다."""
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key("AIONA"),
            [
                scored_material(f"관련키워드{index}", subject=90.0, demand=50.0)
                for index in range(MATERIAL_TARGET_POOL_SIZE)
            ],
        )
        collector = FakeCollector(TrendSource.NAVER_DATALAB, ["다른것"])
        provider = AggregateTrendProvider(
            [collector], ranker=StubRanker({}), material_store=store, rotate=lambda size: 0
        )

        result = await provider.fetch_trends(
            fetch_input(max_keywords=8, mode=TrendMode.MATERIAL_RELATED)
        )

        assert collector.calls == 0, "저장분이 충분한데 소스 API를 불렀다"
        assert result.source == "database"
        assert len(result.trend_keywords) == 8

    async def test_material_collection_never_pollutes_the_shared_trending_pool(self):
        """소재 수집분은 소재 전용 저장소에만 쌓인다(스펙 §6).

        예전에는 공용 trend_keywords에도 upsert해서, '배틀그라운드 감도 설정'처럼 특정
        소재에서만 의미 있는 키워드가 아무 관계 없는 사용자의 최신순 패널에 노출됐다."""
        cache = InMemoryPoolCache()
        await cache.set(
            _bare_pool_key(TrendSource.NAVER_DATALAB),
            [CollectedKeyword(keyword="워터밤", score=95.0, rank=1)],
        )
        store = InMemoryMaterialKeywordStore()
        collector = FakeCollector(TrendSource.NAVER_DATALAB, ["노코드", "자동화"])
        provider = AggregateTrendProvider(
            [collector],
            cache=cache,
            ranker=StubRanker({"노코드": 90, "자동화": 80}),
            material_store=store,
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(
            fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED)
        )

        assert {k.keyword for k in result.trend_keywords} == {"노코드", "자동화"}

        # 공용 풀은 그대로 — 소재발 키워드가 한 개도 섞이지 않았다.
        shared = await cache.get(_bare_pool_key(TrendSource.NAVER_DATALAB))
        assert shared is not None
        assert [k.keyword for k in shared.keywords] == ["워터밤"]

        # 소재 풀에는 쌓였다 — 다음 요청이 재사용한다.
        assert {item.keyword for item in await store.load(material_key("AIONA"))} == {
            "노코드",
            "자동화",
        }

    async def test_a_stored_echo_only_pool_still_falls_back_to_collection(self):
        """저장분에서 게이트를 통과하는 유일한 후보가 소재 메아리('AI AIONA')면 — 화면에는
        못 나가는 키워드다 — 적격 0개로 보고 수집 폴백을 실행해야 한다. 적격 집계가 노출
        필터를 무시하면 '적격 있음'으로 오판해 수집도 안 하고 패널이 영구히 빈다."""
        cache = InMemoryPoolCache()
        await cache.set(
            _bare_pool_key(TrendSource.NAVER_DATALAB),
            [
                CollectedKeyword(keyword="AI AIONA", score=95.0, rank=1),
                CollectedKeyword(keyword="워터밤", score=90.0, rank=2),
            ],
        )
        collector = FakeCollector(TrendSource.NAVER_DATALAB, ["노코드"])
        # 메아리는 모든 축에서 고득점(소재명을 되풀이하니 당연하다), 무관 트렌드는 낙제.
        ranker = StubRanker({"AI AIONA": 95, "워터밤": 5, "노코드": 90})
        provider = AggregateTrendProvider(
            [collector], cache=cache, ranker=ranker, rotate=lambda size: 0
        )

        result = await provider.fetch_trends(
            fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED)
        )

        assert collector.calls >= 1, "메아리뿐인 저장분인데 수집 폴백이 실행되지 않았다"
        assert result.source == "external_api"
        assert [k.keyword for k in result.trend_keywords] == ["노코드"]

    async def test_concurrent_material_requests_share_one_collection(self):
        """같은 소재+목적+페르소나 조합의 동시 요청은 수집 작업 하나를 공유한다(§4)."""

        class SlowCollector(FakeCollector):
            async def collect(self, trend_input, limit, known=frozenset()):
                await asyncio.sleep(0.01)
                return await super().collect(trend_input, limit, known)

        collector = SlowCollector(TrendSource.NAVER_DATALAB, ["노코드"])
        ranker = StubRanker({"노코드": 90})
        provider = AggregateTrendProvider([collector], ranker=ranker, rotate=lambda size: 0)

        first, second = await asyncio.gather(
            provider.fetch_trends(fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED)),
            provider.fetch_trends(fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED)),
        )

        # 한 회차에 수집은 한 번이다. 회차가 둘(초기 + 확장)이므로 두 요청이 작업을
        # 공유하지 않았다면 네 번 불렸을 것이다.
        assert collector.calls == 2, "동일 입력의 동시 요청이 수집을 따로 실행했다"
        assert {k.keyword for k in first.trend_keywords} == {"노코드"}
        assert {k.keyword for k in second.trend_keywords} == {"노코드"}

    async def test_collection_failure_shows_nothing_instead_of_inventing(self):
        """외부 API가 전부 죽으면 보여줄 것이 없다 — 모델에게 후보를 지어내게 하지 않는다.

        예전에는 검색이 실패하면 LLM 확장으로 화면을 채웠다. 'AIONA 활용법'처럼 그럴듯하지만
        아무도 검색하지 않았을 수 있는 조합이 '소재 관련 검색어'로 올라갔고, 그 확장 호출이
        요청마다 두 번씩 직렬로 돌아 100초의 절반을 차지했다. 후보는 실제로 관측된 것이어야
        한다(2026-07-29)."""
        collector = FakeCollector(
            TrendSource.NAVER_DATALAB, ["노코드"], error=RuntimeError("naver 500")
        )
        provider = AggregateTrendProvider(
            [collector],
            ranker=StubRanker({}, default=90),
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(
            fetch_input(max_keywords=8, mode=TrendMode.MATERIAL_RELATED)
        )

        assert result.trend_keywords == []

    async def test_total_failure_returns_empty_instead_of_raising(self):
        """수집도 확장도 불가능하면 보여줄 것이 없다. 그래도 예외를 던지지는 않는다 —
        소재 관련순에서 오류 화면은 사용자가 할 수 있는 일이 없는 막다른 길이고, 빈 목록은
        '다른 후보 보기'로 다시 시도할 수 있다."""
        collector = FakeCollector(
            TrendSource.GOOGLE_TRENDS, ["노코드"], error=RuntimeError("serpapi 500")
        )
        provider = AggregateTrendProvider([collector], ranker=StubRanker({}))

        result = await provider.fetch_trends(
            fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED)
        )

        assert result.trend_keywords == []

    async def test_persona_reaches_the_relevance_judgment(self):
        """페르소나가 판정 입력까지 배선된다 — 페르소나를 바꾸면 판정 캐시 키도 바뀐다."""
        collector = FakeCollector(TrendSource.NAVER_DATALAB, ["노코드"])
        ranker = StubRanker({"노코드": 90})
        provider = AggregateTrendProvider([collector], ranker=ranker)

        await provider.fetch_trends(
            fetch_input(
                max_keywords=4, mode=TrendMode.MATERIAL_RELATED, persona="친근한 동네 리뷰어"
            )
        )

        assert ranker.last_input is not None
        assert ranker.last_input.persona == "친근한 동네 리뷰어"


class TestTrendingWithoutRelevance:
    """최신순은 소재별 관련도·결합 가능성 채점을 완전히 건너뛴다."""

    async def test_trending_ignores_material_specific_blendability(self):
        """소재별 결합 가능성 표가 있어도 최신순은 호출하지 않고 실시간 키워드를 유지한다."""
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, ["장례", "여름휴가"])
        ranker = StubRanker(
            {"장례": 3, "여름휴가": 20},
            blendability_scores={"장례": 5, "여름휴가": 80},
        )

        result = await AggregateTrendProvider(
            [collector], ranker=ranker, rotate=lambda size: 0
        ).fetch_trends(fetch_input(max_keywords=4, mode=TrendMode.TRENDING))
        keywords = [k.keyword for k in result.trend_keywords]

        assert keywords == ["장례", "여름휴가"]
        assert ranker.calls == 0

    async def test_trending_outputs_no_relevance_fields(self):
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, ["장례", "여름휴가"])
        ranker = StubRanker({"장례": 3, "여름휴가": 20})  # blendability 표 없음 → None

        result = await AggregateTrendProvider(
            [collector], ranker=ranker, rotate=lambda size: 0
        ).fetch_trends(fetch_input(max_keywords=4, mode=TrendMode.TRENDING))
        keywords = [k.keyword for k in result.trend_keywords]

        assert "장례" in keywords and "여름휴가" in keywords
        assert ranker.calls == 0
        assert all(k.relevance is None for k in result.trend_keywords)

    async def test_shuffle_also_skips_relevance_scoring(self):
        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, ["장례", "여름휴가", "냉면 맛집"])
        ranker = StubRanker(
            {"장례": 3, "여름휴가": 20, "냉면 맛집": 15},
            blendability_scores={"장례": 5, "여름휴가": 80, "냉면 맛집": 70},
        )
        provider = AggregateTrendProvider(
            [collector],
            ranker=ranker,
            rotate=lambda size: 0,
            sampler=lambda pool, size: list(pool)[:size],
        )

        result = await provider.fetch_trends(
            fetch_input(max_keywords=4, mode=TrendMode.TRENDING, shuffle=True)
        )
        keywords = [k.keyword for k in result.trend_keywords]

        assert set(keywords) == {"장례", "여름휴가", "냉면 맛집"}
        assert ranker.calls == 0


class TestTrendingShuffle:
    """최신순 '다른 후보 보기'(shuffle): 노출 이력을 무시하고 **인기순 상위 구간**(상위 20%,
    최소 표시 개수의 2배)에서 무작위 표본을 뽑는다(중복 노출 허용, 2026-07-22 결정). 이력
    제외 방식은 풀을 한 바퀴 돌면 후보가 말라붙어 버튼이 죽었고, 전체 풀 무작위는 한물간
    하위 후보까지 섞였다 — '진짜 뜨는 것 위주로 매번 다른 조합'이 버튼의 약속이다."""

    def _provider(self, pool_size: int = 20, sampler=None):
        collector = FakeCollector(
            TrendSource.GOOGLE_TRENDS, [f"실검{n}" for n in range(pool_size)]
        )
        return (
            AggregateTrendProvider(
                [collector], rotate=lambda size: 0, sampler=sampler
            ),
            collector,
        )

    async def test_shuffle_ignores_exclude_and_history(self):
        """화면에 나온 키워드를 전부 exclude로 보내고 이력이 쌓여 있어도, shuffle 요청은
        풀 전체에서 다시 뽑아 빈손으로 돌아가지 않는다."""
        provider, _ = self._provider(sampler=lambda seq, k: list(seq)[:k])

        first = await provider.fetch_trends(fetch_input(max_keywords=16))
        shown = [k.keyword for k in first.trend_keywords]
        assert len(shown) == 16

        again = await provider.fetch_trends(
            fetch_input(max_keywords=16, shuffle=True, exclude_keywords=shown)
        )

        # 이력·exclude 방식이었다면 20-16=4개만 남았을 상황. shuffle은 16개를 다시 채운다.
        assert len(again.trend_keywords) == 16

    async def test_shuffle_draws_from_a_window_wider_than_the_screen(self):
        """표본 창은 화면 크기보다 넓다(최소 표시 개수의 2배) — 창이 화면 크기와 같으면
        누를 때마다 같은 얼굴이라 무작위의 의미가 없다."""
        seen_sizes: list[int] = []

        def sampler(seq, k):
            seen_sizes.append(len(seq))
            return list(seq)[:k]

        provider, _ = self._provider(sampler=sampler)
        await provider.fetch_trends(fetch_input(max_keywords=8, shuffle=True))

        # 풀 20개, 표시 8개 → 창 = max(8×2, ceil(20×0.2)) = 16.
        assert seen_sizes and seen_sizes[0] == 16

    async def test_shuffle_draws_only_from_the_hot_top_slice(self):
        """표본은 풀 전체가 아니라 인기순 상위 구간에서만 뽑힌다 — 전체 풀 무작위 시절처럼
        한물간 하위 후보가 섞이지 않는다. (1회 수집은 소스당 20개 상한이라, 운영처럼 DB에
        누적된 큰 풀을 시딩해 검증한다.)"""
        cache = InMemoryPoolCache()
        await cache.set(
            _bare_pool_key(TrendSource.GOOGLE_TRENDS),
            [
                CollectedKeyword(keyword=f"실검{n}", score=float(200 - n), rank=n + 1)
                for n in range(200)
            ],
        )
        captured: list[list] = []

        def sampler(seq, k):
            captured.append(list(seq))
            return list(seq)[:k]

        collector = FakeCollector(TrendSource.GOOGLE_TRENDS, ["안쓰임"])
        provider = AggregateTrendProvider(
            [collector], cache=cache, rotate=lambda size: 0, sampler=sampler
        )
        result = await provider.fetch_trends(fetch_input(max_keywords=16, shuffle=True))

        assert len(result.trend_keywords) == 16
        # 풀 200개, 표시 16개 → 창 = max(16×2, ceil(200×0.2)) = 40 — 풀 전체(200)가 아니다.
        window = captured[0]
        assert len(window) == 40
        # 창은 인기(hotness) 내림차순으로 잘라낸 상위 조각이다.
        hotnesses = [entry[2] for entry in window]
        assert hotnesses == sorted(hotnesses, reverse=True)

    async def test_shuffle_result_is_still_displayed_hottest_first(self):
        """구성은 무작위지만 표시는 인기순 — 최신순 뷰의 의미(뜨거운 순서)는 유지한다."""
        # 샘플러가 일부러 순서를 뒤집어 돌려줘도, 결과는 인기(hotness) 내림차순으로 나온다.
        provider, _ = self._provider(sampler=lambda seq, k: list(reversed(list(seq)))[:k])

        result = await provider.fetch_trends(fetch_input(max_keywords=8, shuffle=True))

        hotness = [k.hotness for k in result.trend_keywords]
        assert hotness == sorted(hotness, reverse=True)

    async def test_shuffle_does_not_touch_material_mode(self):
        """소재 관련순은 결정적 순서(1위=추천)가 스펙이라 shuffle 플래그를 무시한다."""
        calls: list[int] = []

        def sampler(seq, k):
            calls.append(len(seq))
            return list(seq)[:k]

        collector = FakeCollector(TrendSource.NAVER_DATALAB, ["노코드", "자동화"])
        ranker = StubRanker({"노코드": 90, "자동화": 70})
        provider = AggregateTrendProvider(
            [collector], ranker=ranker, rotate=lambda size: 0, sampler=sampler
        )

        result = await provider.fetch_trends(
            fetch_input(max_keywords=4, mode=TrendMode.MATERIAL_RELATED, shuffle=True)
        )

        assert not calls, "소재 관련순에서 무작위 표본이 쓰였다"
        assert [k.keyword for k in result.trend_keywords] == ["노코드", "자동화"]
