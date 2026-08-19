"""소재 관련순의 화면 보장 — 최소 8개, 자동 보충, 커서 순환.

예전 흐름이 빈 화면을 만들던 두 지점을 여기서 못 박는다:
  1) 적격 후보가 0개일 때만 수집해서, 2개만 있으면 2개만 뜨고 '다른 후보 보기'로 0개가 됐다.
  2) exclude가 있으면 순환 복구가 막혀 한 바퀴 돌면 영구히 빈 화면이었다.
"""

from app.llm.contracts import TrendFetchInput
from app.llm.trends import AggregateTrendProvider, CollectedKeyword
from app.llm.trends.aggregate import MATERIAL_MIN_VISIBLE, MATERIAL_RESPONSE_SIZE
from app.llm.trends.material_store import (
    RELEVANCE_PROMPT_VERSION,
    InMemoryMaterialKeywordStore,
    MaterialKeyword,
    material_key,
)
from app.llm.trends.normalizer import normalize_keyword
from app.shared import (
    MATERIAL_RELATION_MIN_SUBJECT,
    BlogTaskInput,
    NaverEvidenceBasis,
    NaverTrendEvidence,
    RelationType,
    TrendEvidenceOrigin,
    TrendMode,
    TrendSource,
    TrendSourceEvidence,
)

MATERIAL = "배틀그라운드"


def fetch_input(**overrides) -> TrendFetchInput:
    return TrendFetchInput(
        post_id="post_1",
        user_id="user_1",
        input=BlogTaskInput(
            topic=MATERIAL,
            subject="게임",
            keywords=[],
            purpose=["사용법·가이드"],
            reference_materials=[],
        ),
        mode=TrendMode.MATERIAL_RELATED,
        **overrides,
    )


def other_material_input(topic: str, **overrides) -> TrendFetchInput:
    """MATERIAL 상수와 다른 소재로 요청을 만든다(모델이 모르는 표기 사례용)."""
    return TrendFetchInput(
        post_id="post_1",
        user_id="user_1",
        input=BlogTaskInput(
            topic=topic,
            subject=None,
            keywords=[],
            purpose=["정보 전달"],
            reference_materials=[],
        ),
        mode=TrendMode.MATERIAL_RELATED,
        **overrides,
    )


def stored(keyword: str, subject: float = 90.0, demand: float = 50.0) -> MaterialKeyword:
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


class RecordingCollector:
    """소재 시드로 불렸는지, 몇 번 불렸는지 기록한다."""

    def __init__(self, keywords: list[str], error: Exception | None = None):
        self.source = TrendSource.NAVER_DATALAB
        self._keywords = keywords
        self._error = error
        self.calls = 0

    async def collect(self, trend_input, limit, known=frozenset()):
        self.calls += 1
        if self._error:
            raise self._error
        return [
            CollectedKeyword(keyword=keyword, score=float(90 - index), rank=index + 1)
            for index, keyword in enumerate(self._keywords)
        ]


class ScoringRanker:
    """모든 키워드를 관련 있다고 판정한다 — 게이트가 아니라 개수 보장을 보는 테스트용."""

    def __init__(self, subject: float = 90.0):
        self._subject = subject
        self.scored: list[str] = []
        self.calls = 0

    async def rank_keywords(self, relevance_input):
        from app.llm.contracts import KeywordJudgment

        self.calls += 1
        self.scored.extend(relevance_input.keywords)
        return {
            word: KeywordJudgment(
                relevance=self._subject,
                subject_relevance=self._subject,
                relation_type=RelationType.DIRECT,
            )
            for word in relevance_input.keywords
        }


def provider_with(store=None, collector=None, ranker=None):
    return AggregateTrendProvider(
        [collector or RecordingCollector([])],
        ranker=ranker or ScoringRanker(),
        material_store=store or InMemoryMaterialKeywordStore(),
        rotate=lambda size: 0,
    )


class TestAutomaticTopUp:
    async def test_a_partially_filled_pool_still_triggers_collection(self):
        """스펙 §10.1 — 저장분이 2개뿐이면 보충한다.

        이것이 예전 흐름의 핵심 결함이었다: '적격 후보가 0개일 때만' 수집해서, 2개가 있으면
        화면에 2개만 뜨고 '다른 후보 보기'로 그 둘을 제외하면 0개가 됐다."""
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [stored(f"{MATERIAL} 저장분{index}") for index in range(2)],
        )
        collector = RecordingCollector([f"{MATERIAL} 수집분{index}" for index in range(20)])

        result = await provider_with(store=store, collector=collector).fetch_trends(
            fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        assert collector.calls == 1, "적격 후보가 2개뿐인데 보충 수집이 실행되지 않았다"
        assert len(result.trend_keywords) >= MATERIAL_MIN_VISIBLE

    async def test_an_empty_pool_collects_and_returns_at_least_the_minimum(self):
        """스펙 §10.2 — 저장분이 0개면 수집해서 검증된 후보 8개 이상을 낸다."""
        collector = RecordingCollector([f"{MATERIAL} 후보{index}" for index in range(20)])

        result = await provider_with(collector=collector).fetch_trends(
            fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        assert collector.calls == 1
        assert len(result.trend_keywords) >= MATERIAL_MIN_VISIBLE
        assert result.source == "external_api"

    async def test_only_new_keywords_are_scored_on_a_second_request(self):
        """스펙 §10.7 — 이미 채점된 후보는 다시 모델에 보내지 않는다.

        예전 캐시는 키에 전체 목록 digest가 들어 있어, 후보 하나만 늘어도 풀 전체를
        재채점했다. 소재 풀이 커질수록 매 요청이 비싸지는 구조였다."""
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [stored(f"{MATERIAL} 기존{index}") for index in range(40)],
        )
        ranker = ScoringRanker()
        collector = RecordingCollector([f"{MATERIAL} 신규{index}" for index in range(3)])
        provider = provider_with(store=store, collector=collector, ranker=ranker)

        await provider.fetch_trends(fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE))

        # 저장분 40개는 이미 채점돼 있으므로 모델에 실린 것이 없어야 한다.
        assert all("기존" not in keyword for keyword in ranker.scored)


class TestCursorRotation:
    async def test_five_consecutive_pages_never_repeat_while_candidates_remain(self):
        """스펙 §10.3 — 연속 호출이 매번 8개 이상을 내고, 풀이 남아 있는 동안 겹치지 않는다."""
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [stored(f"{MATERIAL} 후보{index}", demand=float(100 - index)) for index in range(80)],
        )
        provider = provider_with(store=store)

        cursor = None
        seen: list[set[str]] = []
        for _ in range(5):
            result = await provider.fetch_trends(
                fetch_input(max_keywords=MATERIAL_MIN_VISIBLE, cursor=cursor)
            )
            batch = {keyword.keyword for keyword in result.trend_keywords}
            assert len(batch) == MATERIAL_MIN_VISIBLE
            assert all(batch.isdisjoint(previous) for previous in seen)
            seen.append(batch)
            cursor = result.next_cursor

    async def test_exhausting_the_pool_cycles_instead_of_emptying(self):
        """스펙 §10.3 — 후보를 다 보면 빈 배열이 아니라 순환한다."""
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [stored(f"{MATERIAL} 후보{index}") for index in range(10)],
        )
        provider = provider_with(store=store)

        first = await provider.fetch_trends(fetch_input(max_keywords=MATERIAL_MIN_VISIBLE))
        second = await provider.fetch_trends(
            fetch_input(max_keywords=MATERIAL_MIN_VISIBLE, cursor=first.next_cursor)
        )
        third = await provider.fetch_trends(
            fetch_input(max_keywords=MATERIAL_MIN_VISIBLE, cursor=second.next_cursor)
        )

        assert third.cycled is True
        assert len(third.trend_keywords) == MATERIAL_MIN_VISIBLE
        assert third.trend_keywords != []

    async def test_a_new_cycle_does_not_repeat_the_batch_just_shown(self):
        """순환의 첫 배치가 직전 배치와 같으면 '다른 후보 보기'가 아무 일도 안 한 것처럼 보인다."""
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [stored(f"{MATERIAL} 후보{index}") for index in range(12)],
        )
        provider = provider_with(store=store)

        first = await provider.fetch_trends(fetch_input(max_keywords=MATERIAL_MIN_VISIBLE))
        second = await provider.fetch_trends(
            fetch_input(max_keywords=MATERIAL_MIN_VISIBLE, cursor=first.next_cursor)
        )
        cycled = await provider.fetch_trends(
            fetch_input(max_keywords=MATERIAL_MIN_VISIBLE, cursor=second.next_cursor)
        )

        assert cycled.cycled is True
        assert [k.keyword for k in cycled.trend_keywords] != [
            k.keyword for k in first.trend_keywords
        ]

    async def test_rotation_does_no_collection_or_scoring(self):
        """'다른 후보 보기'(커서)는 저장된 풀에서 창만 잘라 낸다 — 외부 검색·LLM 채점을
        다시 돌리지 않는다. 이게 회전이 느리던 원인이었다(적격 40개 미만이면 매 클릭마다
        수집이 다시 돌았다). 회전은 DB 풀이 목표치에 못 미쳐도 보충하지 않는다."""
        store = InMemoryMaterialKeywordStore()
        # 적격 20개(목표 40 미만) — 예전이라면 회전마다 보충이 돌던 조건이다.
        await store.save(
            material_key(MATERIAL),
            [stored(f"{MATERIAL} 후보{index}") for index in range(20)],
        )
        collector = RecordingCollector([])
        ranker = ScoringRanker()
        provider = provider_with(store=store, collector=collector, ranker=ranker)

        first = await provider.fetch_trends(fetch_input(max_keywords=MATERIAL_MIN_VISIBLE))
        # 최초 조회(커서 없음)는 보충을 시도한다 — 여기까지가 기준선이다.
        base_collector, base_ranker = collector.calls, ranker.calls

        rotated = await provider.fetch_trends(
            fetch_input(max_keywords=MATERIAL_MIN_VISIBLE, cursor=first.next_cursor)
        )

        assert len(rotated.trend_keywords) == MATERIAL_MIN_VISIBLE
        # 회전은 아무 외부 작업도 하지 않는다 — 호출 수가 기준선에서 늘지 않아야 한다.
        assert collector.calls == base_collector, "회전이 외부 수집을 다시 돌렸다"
        assert ranker.calls == base_ranker, "회전이 관련도 채점을 다시 호출했다"

    async def test_a_damaged_cursor_restarts_instead_of_failing(self):
        """커서는 서버가 만든 불투명 값이다. 망가진 값이 화면을 깨뜨려서는 안 된다."""
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [stored(f"{MATERIAL} 후보{index}") for index in range(10)],
        )

        result = await provider_with(store=store).fetch_trends(
            fetch_input(max_keywords=MATERIAL_MIN_VISIBLE, cursor="쓰레기값")
        )

        assert len(result.trend_keywords) == MATERIAL_MIN_VISIBLE

    async def test_explicit_excludes_do_not_empty_the_result(self):
        """스펙 §10.4 — exclude가 있어도 rescue가 막히지 않는다.

        예전에는 `not exclude_keywords`가 순환 복구의 조건이라, '다른 후보 보기'가 보낸
        exclude 때문에 복구가 봉쇄되고 결과가 0개가 됐다."""
        store = InMemoryMaterialKeywordStore()
        keywords = [f"{MATERIAL} 후보{index}" for index in range(10)]
        await store.save(material_key(MATERIAL), [stored(keyword) for keyword in keywords])

        result = await provider_with(store=store).fetch_trends(
            fetch_input(max_keywords=MATERIAL_MIN_VISIBLE, exclude_keywords=keywords)
        )

        assert result.trend_keywords != []
        assert len(result.trend_keywords) == MATERIAL_MIN_VISIBLE

    async def test_no_duplicates_inside_one_response(self):
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [stored(f"{MATERIAL} 후보{index}") for index in range(30)],
        )

        result = await provider_with(store=store).fetch_trends(
            fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        words = [keyword.keyword for keyword in result.trend_keywords]
        assert len(words) == len(set(words))


class TestRelationGate:
    """관계 유형 게이트. 2026-07-30에 **소재를 이름에 담은 후보**의 취급이 바뀌었다.

    바뀐 이유(실측): 소재가 `보지냐`(카보베르데 골키퍼 Vozinha의 음차)였을 때, 네이버 문서에서
    캐낸 `카보베르데`(언급 1위)·`카보베르데 골키퍼`·`Vozinha`가 전부 AMBIGUOUS 15~20점을 받아
    탈락했고 화면은 "관련 검색어를 찾지 못했습니다"였다. 모델이 모르는 표기에서는 늘 이렇게
    된다. AMBIGUOUS는 "무관하다"가 아니라 "판단할 수 없다"이므로 그렇게 취급한다.
    """

    async def test_forced_and_none_never_reach_the_screen(self):
        """스펙 §10.5 — 모델이 **명시적으로 거부한** 판정은 점수와 무관하게 막힌다.

        FORCED·NONE은 소재를 이름에 담고 있어도, 99점을 받아도 통과하지 못한다.
        """
        store = InMemoryMaterialKeywordStore()
        blocked = []
        for relation in (RelationType.FORCED, RelationType.NONE):
            item = stored(f"{MATERIAL} {relation.value}", subject=99.0)
            item.relation_type = relation
            blocked.append(item)
        allowed = stored(f"{MATERIAL} 감도 설정", subject=80.0)
        await store.save(material_key(MATERIAL), [*blocked, allowed])

        result = await provider_with(store=store).fetch_trends(
            fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        assert [k.keyword for k in result.trend_keywords] == [f"{MATERIAL} 감도 설정"]

    async def test_ambiguous_is_not_treated_as_unrelated(self):
        """모델이 "판단할 수 없다"고 답한 것은 무관하다는 증거가 아니다.

        소재를 이름에 담고 있으므로 보여준다 — 이것이 '보지냐'에서 화면이 비던 자리다.
        """
        store = InMemoryMaterialKeywordStore()
        unknown = stored(f"{MATERIAL} 판단불가", subject=15.0)
        unknown.relation_type = RelationType.AMBIGUOUS
        await store.save(material_key(MATERIAL), [unknown])

        result = await provider_with(store=store).fetch_trends(
            fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        assert [k.keyword for k in result.trend_keywords] == [f"{MATERIAL} 판단불가"]
        # 모델이 하지 않은 판단을 했다고 말하지 않는다.
        assert "함께 등장한" in result.trend_keywords[0].trend_reason

    async def test_the_score_floor_still_guards_candidates_without_the_material(self):
        """소재를 이름에 담지 않은 후보에는 유형별 하한이 그대로 적용된다.

        소재를 담은 후보는 하한을 넘지 않아도 보여준다 — 그것이 소재에 관한 검색어라는 것은
        코드가 이미 아는 사실이고, 확인을 위해 모델 점수를 요구할 이유가 없다.
        """
        store = InMemoryMaterialKeywordStore()
        weak_offtopic = stored("남미여행", subject=45.0)
        weak_offtopic.relation_type = RelationType.ADJACENT  # 45 < 60 → 탈락
        weak_but_named = stored(f"{MATERIAL} 약한직접", subject=60.0)
        weak_but_named.relation_type = RelationType.DIRECT  # 소재를 담았으므로 통과
        await store.save(material_key(MATERIAL), [weak_offtopic, weak_but_named])

        result = await provider_with(store=store).fetch_trends(
            fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        assert [k.keyword for k in result.trend_keywords] == [f"{MATERIAL} 약한직접"]

    async def test_an_unscored_candidate_that_names_the_material_is_shown(self):
        """채점 차례를 못 받은 후보도 소재를 담았으면 보여준다.

        예전에는 `is_scored`를 요구해서, 정답이 풀에 있어도 채점 30자리를 노이즈가 차지한
        요청에서는 화면이 비었다. 모델을 부르지 않고도 통과한다는 것이 요점이다.
        """
        store = InMemoryMaterialKeywordStore()
        unscored = MaterialKeyword(
            keyword=f"{MATERIAL} 미채점",
            normalized_keyword=normalize_keyword(f"{MATERIAL} 미채점"),
            source=TrendSource.NAVER_DATALAB,
        )
        await store.save(material_key(MATERIAL), [unscored])

        class SilentRanker:
            calls = 0

            async def rank_keywords(self, relevance_input):
                self.calls += 1
                return {}

        result = await provider_with(store=store, ranker=SilentRanker()).fetch_trends(
            fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        assert [k.keyword for k in result.trend_keywords] == [f"{MATERIAL} 미채점"]

    async def test_an_unscored_candidate_without_the_material_is_not_shown(self):
        """소재를 담지도 않았고 판정도 없으면 근거가 하나도 없다 — 그건 보여주지 않는다.

        단, 게이트를 통과한 후보가 하나도 없을 때는 마지막 단계(문서 동시 등장)가 이런
        후보까지 끌어올 수 있다. 그건 빈 화면을 내지 않기 위한 별개 경로이므로, 여기서는
        게이트를 통과할 다른 후보를 함께 넣어 그 경로가 열리지 않게 한다.
        """
        store = InMemoryMaterialKeywordStore()
        unscored = MaterialKeyword(
            keyword="세계여행",
            normalized_keyword=normalize_keyword("세계여행"),
            source=TrendSource.NAVER_DATALAB,
        )
        await store.save(material_key(MATERIAL), [unscored, stored(f"{MATERIAL} 감도 설정")])

        class SilentRanker:
            async def rank_keywords(self, relevance_input):
                return {}

        result = await provider_with(store=store, ranker=SilentRanker()).fetch_trends(
            fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        assert [k.keyword for k in result.trend_keywords] == [f"{MATERIAL} 감도 설정"]


class TestNeverEmptyWhenSomethingWasCollected:
    """모아 온 것이 있으면 화면이 비지 않는다 (2026-07-30 지시).

    소재가 모델이 모르는 표기일 때 판정이 전부 AMBIGUOUS로 떨어져 화면이 비는 문제를,
    관측된 사실(네이버 문서에서 몇 번 함께 등장했는지)로 메운다. 지어내지는 않는다 —
    수집된 것 안에서만 고른다.
    """

    async def test_the_top_cooccurring_keywords_fill_an_otherwise_empty_screen(self):
        store = InMemoryMaterialKeywordStore()
        # '보지냐' 풀의 실측 판정을 그대로 재현한다.
        rows = []
        for keyword, subject, demand in (
            ("카보베르데", 15.0, 100.0),
            ("카보베르데 골키퍼", 15.0, 61.5),
            ("Vozinha", 20.0, 45.4),
        ):
            item = stored(keyword, subject=subject, demand=demand)
            item.relation_type = RelationType.AMBIGUOUS
            rows.append(item)
        rejected = stored("칠레 명문", subject=5.0, demand=62.9)
        rejected.relation_type = RelationType.NONE
        await store.save(material_key("보지냐"), [*rows, rejected])

        result = await provider_with(store=store).fetch_trends(
            other_material_input("보지냐", max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        shown = [k.keyword for k in result.trend_keywords]
        assert shown, "모아 온 것이 있는데 화면이 비었다"
        # 언급 수 순서대로, 모델이 명시 거부한 것은 뒤로.
        assert shown[:3] == ["카보베르데", "카보베르데 골키퍼", "Vozinha"]
        assert shown.index("칠레 명문") > shown.index("Vozinha")

    async def test_ngram_fragments_of_one_phrase_take_a_single_slot(self):
        """한 문구에서 잘려 나온 조각이 여덟 자리를 다 차지하지 않는다.

        문서에서 캐낸 구절은 겹치는 n-gram이 같은 점수로 함께 올라온다('HERE'·'HERE WE'·
        'HERE WE GO'·'WE GO'). 순위가 같을 때만 더 온전한 쪽을 남기고, 순위가 다르면 정렬을
        그대로 따른다 — 길이를 순위보다 앞세우면 언급 1위가 밀려나고, 실측에서 그 자리를
        NONE 판정 후보가 차지했다.
        """
        store = InMemoryMaterialKeywordStore()
        rows = []
        for fragment in ("HERE", "HERE WE", "HERE WE GO", "WE GO"):
            item = stored(fragment, subject=15.0, demand=42.9)
            item.relation_type = RelationType.AMBIGUOUS
            rows.append(item)
        top = stored("카보베르데", subject=15.0, demand=100.0)
        top.relation_type = RelationType.AMBIGUOUS
        longer_but_weaker = stored("카보베르데 국가대표 골키퍼", subject=5.0, demand=40.8)
        longer_but_weaker.relation_type = RelationType.NONE
        await store.save(material_key("보지냐"), [*rows, top, longer_but_weaker])

        result = await provider_with(store=store).fetch_trends(
            other_material_input("보지냐", max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        shown = [k.keyword for k in result.trend_keywords]
        # 언급 수가 같은 조각 넷은 한 자리로 접히고, 남는 것은 가장 온전한 형태다.
        assert [word for word in shown if "HERE" in word or "GO" in word] == ["HERE WE GO"]
        # 언급 1위가 자리를 지킨다 — 더 긴 후보에게 밀리지 않는다.
        assert shown[0] == "카보베르데"
        # 언급 수가 다르면 접지 않는다. 순위(모델 거부는 뒤로)는 그대로 지킨다.
        assert shown.index("카보베르데") < shown.index("카보베르데 국가대표 골키퍼")

    async def test_tag_sources_do_not_fill_the_screen(self):
        """마지막 단계도 동시 등장이 관측된 소스만 쓴다 — 유튜브 태그는 여기 못 들어온다."""
        store = InMemoryMaterialKeywordStore()
        tag = stored("세계여행", subject=15.0, demand=99.0)
        tag.relation_type = RelationType.AMBIGUOUS
        tag.source = TrendSource.YOUTUBE
        tag.sources = [TrendSource.YOUTUBE]
        await store.save(material_key("보지냐"), [tag])

        result = await provider_with(store=store).fetch_trends(
            other_material_input("보지냐", max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        assert result.trend_keywords == []


class TestNoInventedCandidates:
    """후보가 모자라도 지어내지 않는다(2026-07-29).

    예전에는 검색이 8개를 못 채우면 LLM 확장을 최대 두 번 돌려 자리를 메웠다. 두 가지가
    잘못이었다. 화면에 올라간 '콜롬비아 여행'류는 그럴듯할 뿐 아무도 검색하지 않았을 수
    있고(§1: 일반어 앞에 소재를 임의로 붙이지 않는다), 그 확장 호출과 뒤따르는 재채점이
    직렬로 쌓여 한 요청이 100초를 넘겼다.
    """

    async def test_a_short_search_result_is_shown_as_is(self):
        collector = RecordingCollector([f"{MATERIAL} 검색분{index}" for index in range(2)])

        result = await provider_with(collector=collector).fetch_trends(
            fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        keywords = {keyword.keyword for keyword in result.trend_keywords}
        assert keywords == {f"{MATERIAL} 검색분0", f"{MATERIAL} 검색분1"}
        assert all(
            keyword.source != TrendSource.RELATED_EXPANSION
            for keyword in result.trend_keywords
        )

    async def test_collection_runs_at_most_twice(self):
        """초기 수집 1회 + 부족하면 확장 수집 1회. 그 이상은 돌지 않는다."""
        collector = RecordingCollector([f"{MATERIAL} 검색분{index}" for index in range(2)])

        await provider_with(collector=collector).fetch_trends(
            fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        assert collector.calls == 2

    async def test_a_full_pool_collects_once(self):
        """첫 수집으로 화면을 채우면 보충 회차는 아예 돌지 않는다."""
        collector = RecordingCollector(
            [f"{MATERIAL} 검색분{index}" for index in range(MATERIAL_MIN_VISIBLE + 2)]
        )

        result = await provider_with(collector=collector).fetch_trends(
            fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE)
        )

        assert collector.calls == 1
        assert len(result.trend_keywords) >= MATERIAL_MIN_VISIBLE

    async def test_a_stored_pool_that_fills_the_screen_calls_nothing(self):
        """같은 소재의 두 번째 요청은 외부 API도 모델도 부르지 않는다(캐시 경로)."""
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [stored(f"{MATERIAL} 저장분{index}") for index in range(MATERIAL_MIN_VISIBLE)],
        )
        collector = RecordingCollector([f"{MATERIAL} 검색분{index}" for index in range(4)])
        ranker = ScoringRanker()

        result = await provider_with(
            store=store, collector=collector, ranker=ranker
        ).fetch_trends(fetch_input(max_keywords=MATERIAL_RESPONSE_SIZE))

        assert collector.calls == 0, "저장분으로 채울 수 있는데 소스를 불렀다"
        assert ranker.calls == 0, "이미 채점된 풀을 다시 채점했다"
        assert result.source == "database"
        assert len(result.trend_keywords) == MATERIAL_MIN_VISIBLE


class TestTrendingIsUntouched:
    async def test_trending_never_reads_the_material_store(self):
        """스펙 §10.8 회귀 — 최신순은 소재 풀도, 소재 채점도 건드리지 않는다."""
        collector = RecordingCollector([f"트렌드{index}" for index in range(20)])
        ranker = ScoringRanker()
        provider = AggregateTrendProvider(
            [collector],
            ranker=ranker,
            material_store=InMemoryMaterialKeywordStore(),
            rotate=lambda size: 0,
        )

        result = await provider.fetch_trends(
            TrendFetchInput(
                post_id="post_1",
                user_id="user_1",
                input=BlogTaskInput(
                    topic=MATERIAL, subject="게임", keywords=[], reference_materials=[]
                ),
                mode=TrendMode.TRENDING,
                max_keywords=8,
            )
        )

        assert ranker.calls == 0, "최신순이 소재 관련도 채점을 호출했다"
        assert result.next_cursor is None, "최신순 응답에 소재 커서가 실렸다"
        assert len(result.trend_keywords) == 8


class MeasuringCollector(RecordingCollector):
    """measure_keywords까지 갖춘 네이버 수집기 — 보강 경로가 무엇을 쟀는지 기록한다."""

    def __init__(self, keywords: list[str] | None = None):
        super().__init__(keywords or [])
        self.measured: list[list[str]] = []

    async def measure_keywords(self, keywords):
        self.measured.append(list(keywords))
        return {
            keyword: TrendSourceEvidence(
                source=TrendSource.NAVER_DATALAB,
                observed_at="2026-08-10T00:00:00.000Z",
                data_origin=TrendEvidenceOrigin.NAVER_SEARCH_API,
                naver=NaverTrendEvidence(
                    total_news_count=120,
                    total_blog_count=340,
                    basis=NaverEvidenceBasis.SEARCH_API_TOTAL,
                ),
            )
            for keyword in keywords
        }


class TestStoredPoolEvidence:
    """근거 도입 전에 저장된 소재 풀도 화면에 나갈 때 수치를 갖는다.

    저장분만으로 8칸이 채워지는 소재는 수집(그때만 근거를 재던 자리)이 다시 돌지 않아,
    카드 전부가 "상세 지표는 새 수집 후 표시됩니다"로 영영 남았다(2026-08-10 사용자 지적
    — '아이언맨' 풀 실측). 화면에 나가는 창만 재고, 잰 결과는 저장돼 다시 재지 않는다."""

    async def test_a_stored_pool_without_evidence_is_measured_before_display(self):
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [
                stored(f"{MATERIAL} 저장분{index}", demand=float(100 - index))
                for index in range(10)
            ],
        )
        collector = MeasuringCollector()
        provider = provider_with(store=store, collector=collector)

        result = await provider.fetch_trends(fetch_input(max_keywords=MATERIAL_MIN_VISIBLE))

        # 화면에 나간 카드는 전부 네이버가 잰 근거를 지닌다.
        assert len(result.trend_keywords) == MATERIAL_MIN_VISIBLE
        assert all(
            (keyword.evidence_by_source or {}).get(TrendSource.NAVER_DATALAB.value)
            for keyword in result.trend_keywords
        )
        # 풀 전체(10개)가 아니라 화면에 나가는 창(8개)만 쟀고, 수집은 돌지 않았다.
        assert [len(batch) for batch in collector.measured] == [MATERIAL_MIN_VISIBLE]
        assert collector.calls == 0

    async def test_measured_evidence_is_saved_and_not_measured_again(self):
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [
                stored(f"{MATERIAL} 저장분{index}", demand=float(100 - index))
                for index in range(10)
            ],
        )
        collector = MeasuringCollector()
        provider = provider_with(store=store, collector=collector)

        await provider.fetch_trends(fetch_input(max_keywords=MATERIAL_MIN_VISIBLE))
        again = await provider.fetch_trends(fetch_input(max_keywords=MATERIAL_MIN_VISIBLE))

        # 같은 화면을 다시 열어도 다시 재지 않는다 — 첫 노출에서 저장됐기 때문이다.
        assert len(collector.measured) == 1
        assert all(keyword.evidence_by_source for keyword in again.trend_keywords)
        saved = await store.load(material_key(MATERIAL))
        assert sum(1 for item in saved if item.evidence_by_source) == MATERIAL_MIN_VISIBLE

    async def test_a_collector_without_measurement_still_serves_the_pool(self):
        """네이버 자격 증명이 없으면(측정 불가) 근거 없이도 화면은 나간다 — 지표는
        중립 문구로 남을 뿐, 저장 풀 재사용이 막혀서는 안 된다."""
        store = InMemoryMaterialKeywordStore()
        await store.save(
            material_key(MATERIAL),
            [stored(f"{MATERIAL} 저장분{index}") for index in range(10)],
        )
        provider = provider_with(store=store, collector=RecordingCollector([]))

        result = await provider.fetch_trends(fetch_input(max_keywords=MATERIAL_MIN_VISIBLE))

        assert len(result.trend_keywords) == MATERIAL_MIN_VISIBLE
