"""검색 자료 분류·관련도 파싱. 모델은 엉성한 값을 주므로 정규화가 핵심이다."""

from app.llm.parsing import sources_from_indexes, sources_value, strip_internal_notes


def test_parses_source_type_and_relevance():
    parsed = sources_value(
        [
            {
                "title": "공식 문서",
                "url": "https://official.example.com",
                "snippet": "요약",
                "sourceType": "OFFICIAL",
                "relevanceScore": 88,
            }
        ]
    )
    assert parsed[0].source_type == "OFFICIAL"
    assert parsed[0].relevance_score == 88


def test_lowercase_source_type_is_normalized():
    parsed = sources_value(
        [{"title": "T", "url": "https://example.com/u", "snippet": "s", "sourceType": "news"}]
    )
    assert parsed[0].source_type == "NEWS"


def test_unknown_source_type_becomes_blank():
    parsed = sources_value(
        [{"title": "T", "url": "https://example.com/u", "snippet": "s", "sourceType": "TWITTER"}]
    )
    assert parsed[0].source_type == ""


def test_relevance_is_clamped_and_defensive():
    parsed = sources_value(
        [
            {"title": "a", "url": "https://example.com/1", "snippet": "s", "relevanceScore": 250},
            {"title": "b", "url": "https://example.com/2", "snippet": "s", "relevanceScore": -5},
            {"title": "c", "url": "https://example.com/3", "snippet": "s", "relevanceScore": True},
            {"title": "d", "url": "https://example.com/4", "snippet": "s", "relevanceScore": "높음"},
        ]
    )
    assert [s.relevance_score for s in parsed] == [100, 0, 0, 0]


def test_missing_fields_use_safe_defaults():
    parsed = sources_value([{"title": "T", "url": "https://example.com/u", "snippet": "s"}])
    assert parsed[0].source_type == ""
    assert parsed[0].relevance_score == 0


def test_intent_schema_satisfies_openai_strict_structured_outputs():
    """INTENT_SCHEMA는 OpenAI /v1/responses에 strict=True로 그대로 보내진다.

    strict 모드는 모든 object에 additionalProperties:false를 요구하고, properties의
    **모든** 키가 required에 있어야 한다. 하나라도 빠지면 400으로 거절되어 M3 검증이
    매번 실패하고, 화면에는 "검색 결과가 없습니다"만 남는다 — 실제로 dataPoints를
    properties에만 추가하면서 이 사고가 났다. 값이 없을 수 있는 항목은 required에서 빼는
    대신 nullable로 표현한다.
    """
    from app.llm.schemas import INTENT_SCHEMA

    violations: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and isinstance(node.get("properties"), dict):
                missing = set(node["properties"]) - set(node.get("required") or [])
                if missing:
                    violations.append(f"{path}: required에 빠진 키 {sorted(missing)}")
                if node.get("additionalProperties") is not False:
                    violations.append(f"{path}: additionalProperties가 False가 아님")
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(INTENT_SCHEMA, "INTENT_SCHEMA")

    assert not violations, "OpenAI strict 위반:\n" + "\n".join(violations)


def test_a_data_point_without_a_unit_is_kept():
    """단위 없는 수치도 살아남아야 한다 — strict를 위해 unit을 nullable로 받기 때문."""
    parsed = sources_value(
        [
            {
                "title": "T",
                # 유효한 http URL이어야 한다 — 형식이 무효한 URL은 이제 출처로 받지 않는다.
                "url": "https://example.com/u",
                "snippet": "s",
                "dataPoints": [{"label": "방문자 수", "value": 1200, "unit": None}],
            }
        ]
    )
    assert parsed[0].data_points[0].unit is None
    assert parsed[0].data_points[0].value == 1200


# --- 사용자에게 보이는 문구에서 내부 처리 얘기 제거(strip_internal_notes) ---


def test_internal_note_sentence_is_removed_from_user_text():
    """모델이 rationale·summary에 내부 처리 얘기를 흘려도 사용자에게 보이면 안 된다.
    화면에서 본 실제 누출 문장을 그대로 재현한다."""
    text = (
        "배경의 색과 질감을 몸에 그려 숨는 방식과 시각적 코미디를 소개하는 입문형 주제다. "
        "제공된 출처는 모두 Snippet이 비어 있어 검증 가능한 요약을 작성할 수 없으므로 "
        "sources에서 제외했다."
    )
    cleaned = strip_internal_notes(text)
    assert "Snippet" not in cleaned
    assert "스니펫" not in cleaned
    assert "sources에서" not in cleaned
    # 멀쩡한 앞 문장은 그대로 남는다.
    assert cleaned.startswith("배경의 색과 질감을")


def test_clean_text_is_left_untouched():
    text = "직접 플레이하거나 독특한 숨는 방법을 찾는 초보 이용자를 위한 입문형 주제다."
    assert strip_internal_notes(text) == text


def test_internal_markers_are_case_insensitive_and_cover_variants():
    for bad in ("SNIPPET이 비었다.", "스니펫 없음.", "sourceIndex 3 참조.", "source index 오류."):
        assert strip_internal_notes(bad) == ""


def test_model_summary_with_internal_note_does_not_leak_into_snippet():
    """sources_from_indexes가 summary를 스니펫으로 쓸 때도 내부 얘기는 걸러진다."""
    from app.shared import SearchSource

    collected = [SearchSource(title="출처1", url="https://a.example/1", snippet="")]
    joined = sources_from_indexes(
        [
            {
                "sourceIndex": 1,
                "summary": "메챠카멜레온 위장 원리를 다루는 게임 소개. Snippet이 비어 있음.",
                "sourceType": "BLOG",
                "relevanceScore": 70,
                "dataPoints": [],
            }
        ],
        collected,
    )
    assert "Snippet" not in joined[0].snippet
    assert joined[0].snippet.startswith("메챠카멜레온 위장 원리")


def _source(url: str, score: int = 0):
    from app.shared import SearchSource

    return SearchSource(title=url, url=url, snippet="s", relevance_score=score)


def test_the_chosen_sources_are_topped_up_from_what_was_actually_collected():
    """정리 모델이 둘만 골라도, 검색이 찾아 둔 자료로 상한까지 채운다(2026-08-07).

    예전에는 고른 것만 그대로 썼다. 그러면 자료를 여덟 개 찾아 놓고도 화면에는 둘만 떴고,
    나머지는 어디에도 남지 않았다 — 사용자가 "자료가 너무 적게 수집된다"고 본 자리다.
    채우는 자료는 지어낸 것이 아니라 이미 검색으로 찾은 실제 출처다.
    """
    from app.llm.live_adapters import _sources_for_candidate

    chosen = [_source("https://a", 90), _source("https://b", 80)]
    collected = [_source("https://c"), _source("https://d"), _source("https://e")]

    # 고른 것이 앞, 채운 것이 뒤. 관련도 판단이 실린 쪽이 위에 있어야 한다.
    assert [s.url for s in _sources_for_candidate(chosen, collected)] == [
        "https://a",
        "https://b",
        "https://c",
        "https://d",
        "https://e",
    ]


def test_topping_up_never_repeats_a_source_already_chosen():
    """고른 자료가 수집 목록에도 있다 — 같은 URL이 두 줄로 보이면 안 된다."""
    from app.llm.live_adapters import _sources_for_candidate

    chosen = [_source("https://a", 90)]
    collected = [_source("https://a"), _source("https://b"), _source("https://c")]

    filled = _sources_for_candidate(chosen, collected)
    assert [s.url for s in filled] == ["https://a", "https://b", "https://c"]
    # 고른 쪽의 관련도 판단이 살아 있어야 한다(수집분 사본으로 덮이지 않는다).
    assert filled[0].relevance_score == 90


def test_topping_up_stops_at_the_cap():
    """상한(INTENT_SOURCE_MAX)을 넘겨 채우지 않는다 — 화면이 그만큼만 보여 준다."""
    from app.llm.live_adapters import INTENT_SOURCE_MAX, _sources_for_candidate

    chosen = [_source("https://a", 90)]
    collected = [_source(f"https://c{i}") for i in range(10)]

    assert len(_sources_for_candidate(chosen, collected)) == INTENT_SOURCE_MAX


def test_a_successful_user_url_is_pinned_before_model_chosen_sources():
    """직접 읽은 참고 URL은 모델이 다른 다섯 개를 골라도 원고 입력에서 사라지지 않는다."""
    from app.llm.live_adapters import _sources_for_candidate

    reference_url = "https://reference.example/article"
    chosen = [_source(f"https://chosen{i}.example", 90 - i) for i in range(5)]
    collected = [*chosen, _source(reference_url)]

    filled = _sources_for_candidate(chosen, collected, [reference_url])

    assert len(filled) == 6
    assert filled[0].url == reference_url
    assert [source.url for source in filled[1:]] == [source.url for source in chosen]


def test_all_ten_successful_user_urls_survive_the_candidate_cap():
    from app.llm.live_adapters import INTENT_SOURCE_MAX, _sources_for_candidate

    reference_urls = [f"https://reference{i}.example/article" for i in range(10)]
    collected = [_source(url) for url in reference_urls]
    chosen = [_source(f"https://chosen{i}.example", 90 - i) for i in range(5)]

    filled = _sources_for_candidate(chosen, [*collected, *chosen], reference_urls)

    assert INTENT_SOURCE_MAX == 10
    assert [source.url for source in filled] == reference_urls


def test_sources_are_never_empty_when_the_search_actually_found_something():
    """소재로 검색해 자료를 찾아 놓고도 요약 모델이 하나도 고르지 않으면, 화면은 '자료
    없음'이 되어 검색이 실패한 것처럼 읽힌다. 그럴 때는 수집한 실제 자료로 대신한다."""
    from app.llm.live_adapters import _sources_for_candidate

    collected = [_source("https://c"), _source("https://d")]

    assert [s.url for s in _sources_for_candidate([], collected)] == [
        "https://c",
        "https://d",
    ]


def test_nothing_is_invented_when_the_search_found_nothing():
    """수집된 자료가 정말 없으면 비어 있는 게 맞다 — URL을 지어내지 않는다."""
    from app.llm.live_adapters import _sources_for_candidate

    assert _sources_for_candidate([], []) == []


def test_more_than_ten_sources_are_cut_to_ten():
    from app.llm.live_adapters import INTENT_SOURCE_MAX, _sources_for_candidate

    chosen = [_source(f"https://source{n}.example", 90) for n in range(12)]

    assert len(_sources_for_candidate(chosen, [])) == INTENT_SOURCE_MAX
    # 대체 경로도 같은 상한을 지킨다.
    assert len(_sources_for_candidate([], chosen)) == INTENT_SOURCE_MAX


# --- 관련도 루브릭: 관계 유형별 점수 상한 (live_adapters._capped_by_relation) ---


def test_none_relation_cannot_carry_a_high_subject_score():
    """모델이 유형과 점수를 어긋나게 내도(NONE인데 80) 규격대로 깎여야 한다.
    상한이 프롬프트에만 있으면 모델이 흔들릴 때마다 무관 키워드가 소재 관련순으로 샌다."""
    from app.llm.live_adapters import _capped_by_relation

    subject, _purpose, relevance = _capped_by_relation("NONE", 80.0, 90.0, 88.0)

    assert subject == 15.0
    # 종합 점수도 같은 천장을 넘지 않는다 — 툴팁에 "소재 연관 88"이 남으면 안 된다.
    assert relevance == 15.0


def test_each_relation_type_has_its_ceiling():
    from app.llm.live_adapters import _capped_by_relation

    ceilings = {
        "DIRECT": 100.0,
        "ADJACENT": 89.0,
        "CONTEXTUAL": 69.0,
        "FORCED": 39.0,
        "NONE": 15.0,
        "AMBIGUOUS": 40.0,
    }
    for relation, ceiling in ceilings.items():
        subject, _purpose, _relevance = _capped_by_relation(relation, 100.0, 50.0, 100.0)
        assert subject == ceiling, relation


def test_direct_has_a_floor_so_the_type_and_score_cannot_disagree():
    """소재 자체(DIRECT)라고 판정해 놓고 낮은 점수를 주면 두 판단이 어긋난다."""
    from app.llm.live_adapters import _capped_by_relation

    subject, _purpose, _relevance = _capped_by_relation("DIRECT", 20.0, 50.0, 50.0)

    assert subject == 85.0


def test_an_unrelated_keyword_cannot_pass_on_purpose_alone():
    """소재와 무관한데 '글 목적에는 맞다'는 이유로 통과하는 길을 막는다(일관성 규칙 1·9)."""
    from app.llm.live_adapters import _capped_by_relation

    _subject, purpose, _relevance = _capped_by_relation("NONE", 10.0, 95.0, 10.0)

    assert purpose == 40.0


def test_blendability_is_not_capped_by_relation_type():
    """결합 가능성은 '관련 있나'가 아니라 '엮어 글을 쓸 수 있나'라는 다른 질문이다.
    소재와 무관한 키워드도 계절감으로 엮일 수 있어야 최신순(소재 무관 뷰)이 유지된다."""
    from app.llm.live_adapters import _capped_by_relation
    import inspect

    # 상한 함수는 blendability를 아예 인자로 받지 않는다 — 씌울 수 없게 해 둔 설계다.
    assert "blend" not in str(inspect.signature(_capped_by_relation)).lower()


def test_an_unknown_relation_type_leaves_scores_untouched():
    """구버전 응답이나 오타로 유형이 없으면 점수를 임의로 깎지 않는다."""
    from app.llm.live_adapters import _capped_by_relation

    subject, purpose, relevance = _capped_by_relation(None, 77.0, 88.0, 80.0)

    assert (subject, purpose, relevance) == (77.0, 88.0, 80.0)


# --- 구형(제목·URL 직접 서술) 출처 응답의 검증: 수집 목록에 실재하는 URL만 통과 ---


def test_rejects_invalid_and_uncollected_urls():
    from app.shared import SearchSource

    allowed = SearchSource(title="공식", url="https://example.com/official", snippet="원문")
    parsed = sources_value(
        [
            {"title": "가짜", "url": "https://invented.example/a", "snippet": "가짜"},
            {"title": "바꾼 제목", "url": allowed.url, "snippet": "바꾼 요약"},
            {"title": "무효", "url": "not-a-url", "snippet": ""},
        ],
        allowed_sources=[allowed],
    )

    assert parsed == [allowed]


def test_keeps_only_data_points_visible_in_the_grounded_snippet():
    from app.shared import SearchSource

    allowed = SearchSource(
        title="2026 이용 조사",
        url="https://example.com/report",
        snippet="2025년 이용률은 42%, 2026년 이용률은 67%로 조사됐다.",
    )
    parsed = sources_value(
        [
            {
                "title": "임의 제목",
                "url": allowed.url,
                "snippet": "임의 요약",
                "dataPoints": [
                    {"label": "2025년", "value": 42, "unit": "%"},
                    {"label": "2026년", "value": 99, "unit": "%"},
                ],
            }
        ],
        allowed_sources=[allowed],
    )

    assert [(point.label, point.value) for point in parsed[0].data_points] == [("2025년", 42.0)]
