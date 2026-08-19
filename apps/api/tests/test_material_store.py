"""소재별 관련 키워드 저장소 — 소재 단위 재사용과 증분 채점의 기반.

여기서 지키려는 두 가지:
  1) 풀의 키는 **소재**이지 사용자가 아니다 — 같은 소재의 두 번째 글이 수집·채점을
     처음부터 다시 하지 않는다.
  2) 이미 채점된 판정은 새 수집분이 덮어쓰지 않는다 — 덮어쓰면 매 요청 전량 재채점이라
     증분 채점이라는 목적 자체가 사라진다.
"""


from app.llm.trends.material_store import (
    MATERIAL_POOL_MAX_SIZE,
    RELEVANCE_PROMPT_VERSION,
    InMemoryMaterialKeywordStore,
    MaterialKeyword,
    _from_document,
    _to_document,
    material_key,
)
from app.shared import RelationType, TrendSource


def collected(keyword: str, demand: float = 50.0) -> MaterialKeyword:
    """수집 직후 상태 — 아직 채점되지 않은 후보."""
    return MaterialKeyword(
        keyword=keyword,
        normalized_keyword=keyword.replace(" ", ""),
        source=TrendSource.NAVER_DATALAB,
        sources=[TrendSource.NAVER_DATALAB],
        demand_score=demand,
    )


def scored(
    keyword: str,
    relation: RelationType = RelationType.DIRECT,
    subject: float = 90.0,
    demand: float = 50.0,
) -> MaterialKeyword:
    item = collected(keyword, demand)
    item.relation_type = relation
    item.subject_relevance = subject
    item.prompt_version = RELEVANCE_PROMPT_VERSION
    return item


class TestMaterialKey:
    """표기만 다른 같은 소재는 한 풀을 쓰고, 확신할 수 없는 것은 합치지 않는다."""

    def test_ignores_spacing_case_and_symbols(self):
        assert material_key("배틀 그라운드") == material_key("배틀그라운드")
        assert material_key(" 배틀그라운드! ") == material_key("배틀그라운드")

    def test_does_not_merge_scripts_it_cannot_confirm(self):
        """영문 표기를 한글 소재에 합치려면 사전이 필요하다. 잘못 합치면 두 소재의
        키워드가 서로에게 새어 나가는데, 그건 따로 두는 것보다 훨씬 나쁘다."""
        assert material_key("BATTLEGROUNDS") != material_key("배틀그라운드")

    def test_does_not_key_on_the_user_or_their_description(self):
        """같은 소재를 사람마다 다르게 설명한다. 설명이 키에 들어가면 소재 풀이 설명문
        수만큼 쪼개져 재사용이 불가능해진다."""
        assert material_key("배틀그라운드", "내가 즐기는 FPS") == material_key(
            "배틀그라운드", "요즘 뜨는 배틀로얄"
        )


class TestIncrementalScoring:
    async def test_a_new_collection_does_not_erase_an_existing_judgment(self):
        """수집기는 판정을 모른 채 키워드만 돌려준다. 그 상태로 덮어쓰면 이미 채점한
        키워드가 매번 '미채점'으로 되돌아가 전량 재채점하게 된다."""
        store = InMemoryMaterialKeywordStore()
        await store.save("bg", [scored("배틀그라운드 감도 설정", subject=88.0)])

        await store.save("bg", [collected("배틀그라운드 감도 설정")])

        [item] = await store.load("bg")
        assert item.relation_type is RelationType.DIRECT
        assert item.subject_relevance == 88.0
        assert item.is_scored

    async def test_only_unscored_keywords_are_reported_as_needing_a_score(self):
        store = InMemoryMaterialKeywordStore()
        await store.save("bg", [scored("배틀그라운드 맵"), collected("배틀그라운드 신규 총기")])

        pending = [item.keyword for item in await store.load("bg") if not item.is_scored]

        assert pending == ["배틀그라운드 신규 총기"]

    async def test_a_changed_rubric_invalidates_old_scores_without_deleting_them(self):
        """채점 기준이 바뀌면 옛 점수는 새 기준으로 매긴 점수가 아니다. 전체 삭제 대신
        버전 불일치로 '미채점' 취급해 자연 재채점되게 한다."""
        store = InMemoryMaterialKeywordStore()
        stale = scored("배틀그라운드 랭크")
        stale.prompt_version = RELEVANCE_PROMPT_VERSION - 1
        await store.save("bg", [stale])

        [item] = await store.load("bg")

        assert not item.is_scored
        assert item.subject_relevance == 90.0  # 값은 남아 있다 — 지우지 않고 무효화만 한다.


class TestPoolHygiene:
    async def test_the_same_keyword_is_never_stored_twice(self):
        store = InMemoryMaterialKeywordStore()
        await store.save("bg", [collected("배틀그라운드 맵")])
        await store.save("bg", [collected("배틀그라운드 맵")])

        assert len(await store.load("bg")) == 1

    async def test_sources_accumulate_across_collections(self):
        """같은 키워드를 네이버와 유튜브 양쪽에서 확인했다는 사실은 버리지 않는다 —
        교차 확인은 화면이 표시하는 근거다."""
        store = InMemoryMaterialKeywordStore()
        await store.save("bg", [collected("배틀그라운드 대회")])
        from_youtube = collected("배틀그라운드 대회")
        from_youtube.sources = [TrendSource.YOUTUBE]
        await store.save("bg", [from_youtube])

        [item] = await store.load("bg")
        assert set(item.sources) == {TrendSource.NAVER_DATALAB, TrendSource.YOUTUBE}

    async def test_pools_are_capped_by_dropping_the_least_related(self):
        """소재 관련성은 시간이 지나도 잘 변하지 않으므로 오래된 것이 아니라 관련도가
        낮은 것을 버린다 — 낮은 후보는 애초에 화면에 나갈 일이 없다."""
        store = InMemoryMaterialKeywordStore()
        await store.save(
            "bg",
            [
                scored(f"배틀그라운드 후보{index}", subject=float(index))
                for index in range(MATERIAL_POOL_MAX_SIZE + 10)
            ],
        )

        pool = await store.load("bg")

        assert len(pool) == MATERIAL_POOL_MAX_SIZE
        assert pool[0].subject_relevance == float(MATERIAL_POOL_MAX_SIZE + 9)
        assert min(item.subject_relevance for item in pool) == 10.0

    async def test_a_full_pool_does_not_eat_the_candidates_just_collected(self):
        """상한이 찬 풀에 새로 수집한 후보를 넣으면, 버려지는 것은 **채점된 저관련 후보**다.

        예전에는 관련도 오름차순 한 번으로 잘랐다. 채점 전 후보의 관련도는 없음(None)이라
        정렬 맨 앞에 서고, 그래서 **방금 수집한 것이 가장 먼저 삭제**됐다 — 채점될 기회조차
        없었다. 무관 키워드로 상한까지 찬 콜롬비아 풀이 수집을 몇 번 돌려도 회복하지 못한
        이유이며, 이 순서는 Mongo._trim도 같다.
        """
        store = InMemoryMaterialKeywordStore()
        await store.save(
            "콜롬비아",
            [
                scored(f"무관 후보{index}", subject=float(index + 1))
                for index in range(MATERIAL_POOL_MAX_SIZE)
            ],
        )

        await store.save("콜롬비아", [collected("콜롬비아 원두"), collected("콜롬비아 톨리마")])
        pool = await store.load("콜롬비아")

        assert len(pool) == MATERIAL_POOL_MAX_SIZE
        assert {"콜롬비아 원두", "콜롬비아 톨리마"} <= {item.keyword for item in pool}
        # 자리를 낸 것은 관련도가 가장 낮은 채점 후보 두 개다.
        assert "무관 후보0" not in {item.keyword for item in pool}
        assert "무관 후보1" not in {item.keyword for item in pool}

    async def test_unscored_candidates_rank_behind_verified_ones(self):
        store = InMemoryMaterialKeywordStore()
        await store.save("bg", [collected("미검증 후보"), scored("검증된 후보", subject=45.0)])

        pool = await store.load("bg")

        assert [item.keyword for item in pool] == ["검증된 후보", "미검증 후보"]


class TestDocumentSerialization:
    """Mongo 저장 직렬화(_to_document). 인메모리 저장소 테스트만으로는 이 경로가 검증되지
    않아, 저장이 통째로 실패하던 회귀(NameError: material_key_of)가 오래 숨어 있었다."""

    def test_materialkey_equals_the_key_used_by_the_query(self):
        """저장 필드 materialKey는 upsert 필터·_pool_query와 같은 key여야 조회가 맞는다.
        (이 값이 어긋나면 저장은 되어도 load가 못 찾아 소재 풀이 영영 비어 보인다.)"""
        key = material_key("나이키")
        doc = _to_document(key, scored("나이키 운동화", subject=88.0))
        assert doc["materialKey"] == key

    def test_to_document_does_not_raise_and_round_trips(self):
        """직렬화가 예외 없이 되고, 되읽으면 판정이 보존된다. 저장 경로의 이름 오류·
        누락을 여기서 잡는다(예전엔 save가 NameError로 조용히 실패해 새 소재가 안 쌓였다)."""
        item = scored("나이키 에어포스", relation=RelationType.ADJACENT, subject=72.0, demand=60.0)
        restored = _from_document(_to_document(material_key("나이키"), item))
        assert restored is not None
        assert restored.keyword == "나이키 에어포스"
        assert restored.relation_type is RelationType.ADJACENT
        assert restored.subject_relevance == 72.0
        assert restored.is_scored


class TestReuseAcrossPosts:
    async def test_a_second_post_on_the_same_material_reads_the_stored_pool(self):
        """materialKey에 userId·postId가 없다는 것의 실제 효과 — 다른 사용자의 두 번째
        글이 수집·채점 없이 같은 풀을 그대로 쓴다."""
        store = InMemoryMaterialKeywordStore()
        await store.save(material_key("배틀그라운드"), [scored("배틀그라운드 패치노트")])

        reused = await store.load(material_key("배틀 그라운드"))

        assert [item.keyword for item in reused] == ["배틀그라운드 패치노트"]

    async def test_different_materials_do_not_leak_into_each_other(self):
        store = InMemoryMaterialKeywordStore()
        await store.save(material_key("배틀그라운드"), [scored("배틀그라운드 맵")])
        await store.save(material_key("에어컨"), [scored("에어컨 전기세")])

        assert [item.keyword for item in await store.load(material_key("에어컨"))] == [
            "에어컨 전기세"
        ]
