"""소재 관련순이 무관 키워드를 통과시키던 문제의 회귀 고정 (2026-07-29).

사용자가 소재로 '콜롬비아'를 넣었을 때 화면에 뜬 것:

    남미여행 · 세계여행 · 여행유튜버 · 대한민국 · 2026월드컵

콜롬비아를 다루는 검색어는 하나도 없었다. 통과 경로는 둘이었다.

1. **구글 트렌드 실시간 피드가 소재 풀에 그대로 들어갔다.** SerpApi Trending Now는 소재별
   연관어 API가 아니라 "지금 한국에서 뜨는 검색어"다 — 콜롬비아를 물어본 적이 없는데
   돌아온 '대한민국'·'2026월드컵'이 소재 관련 후보로 저장됐다.
2. **소재를 이름에 담지 않은 후보의 게이트가 느슨했다.** '콜롬비아'로 네이버를 검색하면
   그 문서 안에 '세계여행'·'여행유튜버'도 함께 나온다. 그것들은 콜롬비아에 속한 대상이
   아니라 어느 여행 글에나 있는 광역어인데, 상황 연결(CONTEXTUAL) 30점만 넘으면 통과했다.
"""

from app.llm.contracts import BlogTaskInput, KeywordJudgment, TrendFetchInput
from app.llm.trends import AggregateTrendProvider, CollectedKeyword
from app.llm.trends.aggregate import MATERIAL_LLM_BATCH_LIMIT
from app.llm.trends.material_store import InMemoryMaterialKeywordStore, material_key
from app.shared import RelationType, TrendMode, TrendSource

MATERIAL = "콜롬비아"


def fetch_input(**overrides) -> TrendFetchInput:
    return TrendFetchInput(
        post_id="post_1",
        user_id="user_1",
        input=BlogTaskInput(
            topic=MATERIAL,
            subject="여행",
            keywords=[],
            purpose=["정보 전달"],
            reference_materials=[],
        ),
        mode=TrendMode.MATERIAL_RELATED,
        **overrides,
    )


class Collector:
    def __init__(self, source: TrendSource, keywords: list[str]):
        self.source = source
        self._keywords = keywords
        self.calls = 0

    async def collect(self, trend_input, limit, known=frozenset()):
        self.calls += 1
        return [
            CollectedKeyword(keyword=word, score=float(90 - index), rank=index + 1)
            for index, word in enumerate(self._keywords)
        ]


class Ranker:
    """관계 유형과 소재 점수를 표로 준다. 표에 없으면 '아무 상관 없음'이다."""

    def __init__(self, table: dict[str, tuple[RelationType, float]]):
        self._table = table
        self.calls = 0
        self.batch_sizes: list[int] = []

    async def rank_keywords(self, relevance_input):
        self.calls += 1
        self.batch_sizes.append(len(relevance_input.keywords))
        judgments = {}
        for word in relevance_input.keywords:
            relation, subject = self._table.get(word, (RelationType.NONE, 5.0))
            judgments[word] = KeywordJudgment(
                relevance=subject,
                subject_relevance=subject,
                relation_type=relation,
            )
        return judgments


def provider(collectors, ranker, store=None):
    return AggregateTrendProvider(
        collectors,
        ranker=ranker,
        material_store=store or InMemoryMaterialKeywordStore(),
        rotate=lambda size: 0,
    )


class TestGoogleTrendsIsNotAMaterialSource:
    async def test_realtime_trending_keywords_never_enter_the_material_pool(self):
        """'대한민국'·'2026월드컵'은 콜롬비아를 물어봐서 나온 답이 아니다.

        모델이 이것들에 후한 점수를 줘도 통과하면 안 된다 — 그래서 랭커는 전부
        DIRECT 90점으로 판정하도록 해 두고, 코드 게이트만으로 막히는지 본다.
        """
        google = Collector(
            TrendSource.GOOGLE_TRENDS, ["대한민국", "2026월드컵", "세계여행"]
        )
        ranker = Ranker(
            {
                word: (RelationType.DIRECT, 90.0)
                for word in ("대한민국", "2026월드컵", "세계여행")
            }
        )
        store = InMemoryMaterialKeywordStore()

        result = await provider([google], ranker, store).fetch_trends(
            fetch_input(max_keywords=8)
        )

        assert result.trend_keywords == []
        # 저장까지 가지 않는다 — 다음 요청이 이것들을 다시 꺼내 오면 안 된다.
        assert await store.load(material_key(MATERIAL)) == []
        # 채점에도 보내지 않는다(무관한 것을 채점하느라 쓰는 시간이 그대로 대기 시간이다).
        assert ranker.calls == 0

    async def test_a_trending_keyword_that_names_the_material_is_kept(self):
        """실시간 피드에 '콜롬비아 축구'가 오르면 그것은 진짜 신호다."""
        google = Collector(
            TrendSource.GOOGLE_TRENDS, ["콜롬비아 축구", "대한민국", "2026월드컵"]
        )
        ranker = Ranker({"콜롬비아 축구": (RelationType.DIRECT, 90.0)})

        result = await provider([google], ranker).fetch_trends(fetch_input(max_keywords=8))

        assert [k.keyword for k in result.trend_keywords] == ["콜롬비아 축구"]


class TestOffTopicGate:
    async def test_generic_travel_words_are_excluded(self):
        """'세계여행'·'여행유튜버'는 콜롬비아 문서에 함께 나올 뿐 콜롬비아가 아니다.

        모델이 이것들을 상황 연결(CONTEXTUAL)로 보는 것 자체는 맞다 — 예전에는 그 유형의
        하한이 30점이라 그대로 통과했다. 소재를 이름에 담지 않은 후보에는 상황 연결을
        허용하지 않는다.
        """
        naver = Collector(
            TrendSource.NAVER_DATALAB,
            ["콜롬비아 여행", "콜롬비아 치안", "세계여행", "여행유튜버", "해외여행"],
        )
        ranker = Ranker(
            {
                "콜롬비아 여행": (RelationType.DIRECT, 95.0),
                "콜롬비아 치안": (RelationType.DIRECT, 88.0),
                "세계여행": (RelationType.CONTEXTUAL, 55.0),
                "여행유튜버": (RelationType.CONTEXTUAL, 45.0),
                "해외여행": (RelationType.CONTEXTUAL, 60.0),
            }
        )

        result = await provider([naver], ranker).fetch_trends(fetch_input(max_keywords=8))

        assert [k.keyword for k in result.trend_keywords] == [
            "콜롬비아 여행",
            "콜롬비아 치안",
        ]

    async def test_a_place_that_belongs_to_the_material_survives(self):
        """'보고타 여행'은 소재명을 담지 않아도 콜롬비아 그 자체에 속한 대상이다.

        코드는 '보고타'가 콜롬비아의 수도인 줄 모른다. 모델이 관계 유형으로 갈라 준다 —
        광역어는 CONTEXTUAL, 소재에 속한 대상은 DIRECT/ADJACENT다.
        """
        naver = Collector(
            TrendSource.NAVER_DATALAB, ["보고타 여행", "메데인 여행", "세계여행"]
        )
        ranker = Ranker(
            {
                "보고타 여행": (RelationType.DIRECT, 85.0),
                "메데인 여행": (RelationType.ADJACENT, 75.0),
                "세계여행": (RelationType.CONTEXTUAL, 55.0),
            }
        )

        result = await provider([naver], ranker).fetch_trends(fetch_input(max_keywords=8))

        assert {k.keyword for k in result.trend_keywords} == {"보고타 여행", "메데인 여행"}

    async def test_a_weakly_scored_adjacent_word_still_needs_a_high_bar(self):
        """'남미여행'은 인접 주제지만 콜롬비아를 다루지는 않는다. 하한을 넘지 못하면 빠진다."""
        naver = Collector(TrendSource.NAVER_DATALAB, ["콜롬비아 커피", "남미여행"])
        ranker = Ranker(
            {
                "콜롬비아 커피": (RelationType.DIRECT, 92.0),
                # ADJACENT 기본 하한(40)은 넘지만 소재를 담지 않은 후보의 하한(60)에는 못 미친다.
                "남미여행": (RelationType.ADJACENT, 45.0),
            }
        )

        result = await provider([naver], ranker).fetch_trends(fetch_input(max_keywords=8))

        assert [k.keyword for k in result.trend_keywords] == ["콜롬비아 커피"]

    async def test_the_material_itself_comes_first(self):
        """직접 관련 후보가 있으면 간접 후보를 앞세우지 않는다.

        '남미여행'이 하한을 넘어 살아남더라도, 소재를 이름에 담은 후보가 먼저다 —
        관련도 점수만으로 정렬하면 간접 후보가 첫 카드를 차지할 수 있다.
        """
        naver = Collector(TrendSource.NAVER_DATALAB, ["남미여행", "콜롬비아 여행"])
        ranker = Ranker(
            {
                # 일부러 간접 후보에 더 높은 점수를 준다.
                "남미여행": (RelationType.ADJACENT, 95.0),
                "콜롬비아 여행": (RelationType.DIRECT, 80.0),
            }
        )

        result = await provider([naver], ranker).fetch_trends(fetch_input(max_keywords=8))

        assert [k.keyword for k in result.trend_keywords] == ["콜롬비아 여행", "남미여행"]

    async def test_a_stored_pool_full_of_off_topic_keywords_is_still_gated(self):
        """이미 저장·채점까지 끝난 옛 후보도 화면에서 막힌다.

        게이트가 수집 단계에만 있으면, 사전 필터가 없던 시절에 쌓인 풀(콜롬비아 소재에
        '대한민국'이 들어 있는 그 풀)이 그대로 계속 노출된다.
        """
        from app.llm.trends.material_store import MaterialKeyword, RELEVANCE_PROMPT_VERSION
        from app.llm.trends.normalizer import normalize_keyword

        def legacy(keyword: str, relation: RelationType, subject: float) -> MaterialKeyword:
            return MaterialKeyword(
                keyword=keyword,
                normalized_keyword=normalize_keyword(keyword),
                source=TrendSource.GOOGLE_TRENDS,
                sources=[TrendSource.GOOGLE_TRENDS],
                demand_score=90.0,
                relation_type=relation,
                subject_relevance=subject,
                relevance=subject,
                prompt_version=RELEVANCE_PROMPT_VERSION,
            )

        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [
                legacy("대한민국", RelationType.CONTEXTUAL, 55.0),
                legacy("2026월드컵", RelationType.CONTEXTUAL, 50.0),
                legacy("여행유튜버", RelationType.CONTEXTUAL, 45.0),
                legacy("콜롬비아 음식", RelationType.DIRECT, 90.0),
            ],
        )
        naver = Collector(TrendSource.NAVER_DATALAB, [])

        result = await provider([naver], Ranker({}), store).fetch_trends(
            fetch_input(max_keywords=8)
        )

        assert [k.keyword for k in result.trend_keywords] == ["콜롬비아 음식"]


class TestTagSourcesAreNotEvidence:
    """유튜브 태그는 "소재를 물어봤더니 돌아왔다"의 근거가 아니다 (2026-07-30).

    화면에 '남미여행'·'세계여행' 둘만 남던 후속 증상의 원인이다. 저장된 콜롬비아 풀
    120개를 열어 보니 '콜롬비아'가 든 키워드가 **하나도 없었고**, 전부 유튜브가 '콜롬비아'
    검색으로 가져온 방송 영상의 태그였다(KBS 스포츠·조별리그·손흥민·2026월드컵). 태그는
    그 영상이 무엇에 관한지가 아니라 채널이 늘 붙이는 말이다.

    실측된 판정: '남미여행' DIRECT 85점, '세계여행' ADJACENT 60점. 관계 유형 게이트로는
    막을 수 없었다 — 모델 판정보다 앞서 동시 등장 근거를 요구해야 한다.
    """

    async def test_youtube_tags_that_do_not_name_the_material_are_excluded(self):
        youtube = Collector(
            TrendSource.YOUTUBE, ["남미여행", "세계여행", "KBS 스포츠", "조별리그"]
        )
        # 모델이 후하게 준 실측 판정을 그대로 쓴다. 코드 게이트만으로 막혀야 한다.
        ranker = Ranker(
            {
                "남미여행": (RelationType.DIRECT, 85.0),
                "세계여행": (RelationType.ADJACENT, 60.0),
                "KBS 스포츠": (RelationType.ADJACENT, 70.0),
                "조별리그": (RelationType.DIRECT, 75.0),
            }
        )
        store = InMemoryMaterialKeywordStore()

        result = await provider([youtube], ranker, store).fetch_trends(
            fetch_input(max_keywords=8)
        )

        assert result.trend_keywords == []
        assert await store.load(material_key(MATERIAL)) == []
        assert ranker.calls == 0, "쓸 수 없는 후보를 채점하느라 시간을 쓰지 않는다"

    async def test_a_youtube_tag_that_names_the_material_is_kept(self):
        """'콜롬비아 여행 브이로그'의 태그·제목 구절은 그대로 통과한다."""
        youtube = Collector(TrendSource.YOUTUBE, ["콜롬비아 여행", "세계여행"])
        ranker = Ranker({"콜롬비아 여행": (RelationType.DIRECT, 90.0)})

        result = await provider([youtube], ranker).fetch_trends(fetch_input(max_keywords=8))

        assert [k.keyword for k in result.trend_keywords] == ["콜롬비아 여행"]

    async def test_a_stored_youtube_tag_is_excluded_even_with_a_high_score(self):
        """이미 저장·채점된 유튜브 태그도 화면에서 막힌다 — 실제로 쌓여 있는 풀이 그것이다."""
        from app.llm.trends.material_store import MaterialKeyword, RELEVANCE_PROMPT_VERSION
        from app.llm.trends.normalizer import normalize_keyword

        def stored(keyword: str, source: TrendSource, relation: RelationType, subject: float):
            return MaterialKeyword(
                keyword=keyword,
                normalized_keyword=normalize_keyword(keyword),
                source=source,
                sources=[source],
                demand_score=80.0,
                relation_type=relation,
                subject_relevance=subject,
                relevance=subject,
                prompt_version=RELEVANCE_PROMPT_VERSION,
            )

        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [
                stored("남미여행", TrendSource.YOUTUBE, RelationType.DIRECT, 85.0),
                stored("세계여행", TrendSource.YOUTUBE, RelationType.ADJACENT, 60.0),
                # 네이버가 문서에서 캐낸 것은 같은 점수라도 근거가 있다.
                stored("보고타", TrendSource.NAVER_DATALAB, RelationType.DIRECT, 85.0),
            ],
        )

        result = await provider(
            [Collector(TrendSource.NAVER_DATALAB, [])], Ranker({}), store
        ).fetch_trends(fetch_input(max_keywords=8))

        assert [k.keyword for k in result.trend_keywords] == ["보고타"]


class TestRefillReachesTheSources:
    """후보가 모자라 도는 보충 수집은 실제로 소스를 불러야 한다 (2026-07-30).

    소스 시드 캐시는 TTL이 30일이다. 보충이 그 신선한 캐시를 그대로 받으면 앞선 수집과
    똑같은 목록이 돌아오고, 그 전부가 이미 저장돼 있어(known) 새 후보가 0개다 — 보충이
    구조적으로 아무 일도 하지 못한다. 그래서 후보 2개짜리 화면이 '다른 후보 보기'로도
    '트렌드 새로 수집'으로도 영구히 그대로였다.
    """

    async def test_a_fresh_source_cache_does_not_block_the_top_up(self):
        naver = Collector(TrendSource.NAVER_DATALAB, [f"{MATERIAL} 여행"])
        ranker = Ranker({f"{MATERIAL} 여행": (RelationType.DIRECT, 95.0)})
        aggregate = provider([naver], ranker)

        await aggregate.fetch_trends(fetch_input(max_keywords=8))
        after_first = naver.calls
        await aggregate.fetch_trends(fetch_input(max_keywords=8))

        assert after_first >= 1
        assert naver.calls > after_first, "신선한 캐시가 보충을 막았다"

    async def test_sources_that_cannot_answer_about_the_material_are_not_re_called(self):
        """유튜브·구글은 회차와 무관하게 같은 답을 준다 — 되불러도 후보는 늘지 않고
        SerpApi 크레딧과 유튜브 할당량(검색 100유닛)만 나간다."""
        naver = Collector(TrendSource.NAVER_DATALAB, [f"{MATERIAL} 여행"])
        youtube = Collector(TrendSource.YOUTUBE, ["세계여행"])
        google = Collector(TrendSource.GOOGLE_TRENDS, ["대한민국"])
        ranker = Ranker({f"{MATERIAL} 여행": (RelationType.DIRECT, 95.0)})
        aggregate = provider([naver, youtube, google], ranker)

        await aggregate.fetch_trends(fetch_input(max_keywords=8))
        youtube_calls, google_calls = youtube.calls, google.calls
        await aggregate.fetch_trends(fetch_input(max_keywords=8))

        # 한 소재에 한 번씩만. 보충 회차도 캐시를 쓴다.
        assert youtube.calls == youtube_calls == 1
        assert google.calls == google_calls == 1


class TestNaverBudget:
    def test_the_naver_budget_covers_phrase_extraction(self):
        """네이버 제한 시간은 HTTP만이 아니라 구절 추출까지 덮어야 한다.

        실측(콜롬비아): HTTP 0.2초(요청 16~32개, 병렬) + 문서 500개에서 구절 5,500개 추출
        0.6~2.5초. 3초였을 때는 소재 포함 후보 482개(콜롬비아 원두·톨리마·투마코…)가 매
        요청 폐기돼 저장 풀에 유튜브 태그만 남았다.
        """
        from app.llm.trends.aggregate import SOURCE_TIMEOUTS

        assert SOURCE_TIMEOUTS[TrendSource.NAVER_DATALAB] >= 8.0


class TestScoringBatch:
    async def test_the_model_sees_one_capped_batch(self):
        """코드 필터를 통과한 상위 후보만, 한 번에 보낸다.

        예전에는 풀 전체(최대 120개)가 채점 대상이라 60개짜리 조각이 두 개씩 나갔고,
        보충 회차마다 그것이 되풀이됐다. 대기 시간의 대부분이 여기였다.
        """
        naver = Collector(
            TrendSource.NAVER_DATALAB, [f"{MATERIAL} 후보{index}" for index in range(80)]
        )
        ranker = Ranker(
            {f"{MATERIAL} 후보{index}": (RelationType.DIRECT, 90.0) for index in range(80)}
        )

        await provider([naver], ranker).fetch_trends(fetch_input(max_keywords=8))

        assert ranker.calls == 1, "채점 호출이 한 번을 넘었다"
        assert ranker.batch_sizes == [MATERIAL_LLM_BATCH_LIMIT]

    async def test_a_top_up_round_does_not_add_a_second_scoring_call(self):
        """보충 수집이 돌아도 채점은 한 번이다.

        예전에는 회차마다 채점을 돌렸다. 보충 회차가 새 후보 한두 개를 더할 때도 모델
        왕복을 온전히 한 번 더 써서, 그 한 번이 요청 시간의 절반을 차지했다. 수집을 모두
        끝낸 뒤 한 번만 채점한다.
        """
        naver = Collector(TrendSource.NAVER_DATALAB, [f"{MATERIAL} 여행", f"{MATERIAL} 커피"])
        ranker = Ranker(
            {
                f"{MATERIAL} 여행": (RelationType.DIRECT, 95.0),
                f"{MATERIAL} 커피": (RelationType.DIRECT, 90.0),
            }
        )

        result = await provider([naver], ranker).fetch_trends(fetch_input(max_keywords=8))

        # 첫 수집이 8개를 못 채웠으니 보충이 돌았다.
        assert naver.calls == 2
        # 그래도 채점은 한 번이다.
        assert ranker.calls == 1
        assert len(result.trend_keywords) == 2
