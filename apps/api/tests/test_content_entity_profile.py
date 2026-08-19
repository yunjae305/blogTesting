"""소재 정체 판정(ContentEntityProfile)과 그 값이 파이프라인에 실리는 경로(2026-08-03).

여기서 확인하는 것.

- D. 동음이의어: 일반 명사와 프로그램 이름이 같은 소재를 영상 콘텐츠로 분류한다.
- 핵심 포맷과 보조 장면이 프롬프트에서 구분되어 실린다.
- 옛 문서·구형 어댑터(엔티티 정보 없음)는 예전과 한 글자도 달라지지 않는다.
"""

from app.llm.parsing import content_entity_from_json, reference_evidence_profile_from_json
from app.llm.prompts import content_entity_block, draft_prompt, reference_evidence_prompt
from app.shared import (
    BlogTaskInput,
    ContentEntityProfile,
    DraftFormat,
    DraftGenerationInput,
    ReferenceEvidenceProfile,
    RelatedPerson,
    SelectedIntentForDraft,
)

# '전과자'는 범죄 전력자를 뜻하는 일반 명사이면서 유튜브 웹예능의 이름이다. 사용자가 사람
# 이름과 함께 검색했다는 사실과 검색 출처가 그 의미를 확정한다.
PROGRAM_JSON = {
    "contentEntity": {
        "entityType": "YOUTUBE_PROGRAM",
        "canonicalName": "전과자",
        "platform": "YouTube",
        "officialChannel": "전과자 공식 채널",
        "relatedPeople": [{"name": "이창섭", "relation": "출연자"}],
        "coreFormat": "여러 대학의 학과를 찾아가 강의와 실습에 참여하는 학과 체험형 웹예능",
        "primaryActivities": ["학과 방문", "강의 참여", "실습 참여", "전공 탐색"],
        "secondaryActivities": ["학식 체험", "캠퍼스 이동", "학생들과의 대화"],
        "backgroundScenes": ["배식 줄", "이동 장면"],
        "officialVideoQueries": ["전과자 이창섭"],
        "naturalPhrases": ["유튜브 웹예능 전과자", "이창섭이 출연하는 전과자"],
        "forbiddenPhrases": [],
        "confidence": 0.9,
    }
}


def program_entity() -> ContentEntityProfile:
    entity = content_entity_from_json(PROGRAM_JSON, "창섭 전과자")
    assert entity is not None
    return entity


def draft_input(evidence: ReferenceEvidenceProfile | None) -> DraftGenerationInput:
    return DraftGenerationInput(
        post_id="post_1",
        user_id="user_1",
        input=BlogTaskInput(
            topic="전과자",
            subject=None,
            purpose=["정보 전달"],
            keywords=["정보 전달"],
            target_reader="대학 진학을 앞둔 학생",
        ),
        selected_intent=SelectedIntentForDraft(
            intent_id="intent_1",
            title="전과자는 어떤 프로그램인가",
            target_reader="대학 진학을 앞둔 학생",
            rationale="프로그램 소개를 찾는 검색 의도",
            keywords=["전과자 학과 체험"],
            sources=None,
        ),
        prompt_version="m4@test",
        format=DraftFormat.HTML,
        trend_title="이창섭이 대학 수업을 직접 듣는 유튜브 웹예능 전과자",
        raw_keywords=["창섭 전과자"],
        reference_evidence=evidence,
    )


class TestParsing:
    def test_a_homonym_is_classified_as_video_content(self):
        entity = program_entity()
        assert entity.entity_type == "YOUTUBE_PROGRAM"
        assert entity.is_media_content is True
        assert entity.canonical_name == "전과자"

    def test_the_raw_keyword_comes_from_code_not_the_model(self):
        """사용자가 무엇을 골랐는지는 이미 아는 값이다. 모델에 되묻지 않는다."""
        entity = program_entity()
        assert entity.raw_keyword == "창섭 전과자"

    def test_an_entity_combination_keyword_becomes_a_forbidden_phrase(self):
        assert "창섭 전과자" in program_entity().forbidden_phrases

    def test_a_natural_proper_noun_keyword_is_not_forbidden(self):
        entity = content_entity_from_json(
            {
                "contentEntity": {
                    "entityType": "PRODUCT_OR_SERVICE",
                    "canonicalName": "아이폰17",
                    "relatedPeople": [],
                }
            },
            "아이폰17",
        )
        assert entity is not None
        assert entity.forbidden_phrases == []

    def test_related_people_keep_their_relation(self):
        people = program_entity().related_people
        assert people == [RelatedPerson(name="이창섭", relation="출연자")]

    def test_a_missing_block_returns_none(self):
        """구형 어댑터·형식 오류: 엔티티 없이 예전 동작으로 진행한다."""
        assert content_entity_from_json({}, "창섭 전과자") is None

    def test_an_unknown_type_falls_back_to_general_topic(self):
        entity = content_entity_from_json(
            {"contentEntity": {"entityType": "NOT_A_TYPE", "canonicalName": "무언가"}},
            "",
        )
        assert entity is not None
        assert entity.entity_type == "GENERAL_TOPIC"
        assert entity.is_media_content is False

    def test_the_evidence_profile_carries_the_entity(self):
        profile = reference_evidence_profile_from_json(
            {
                "referenceEvidenceProfile": {
                    "primaryEntity": "전과자",
                    "brand": None,
                    "productCategory": None,
                    "confirmedAttributes": [],
                    "confirmedUseScenes": [],
                    "referenceImageRoles": [],
                    "sourceFacts": [],
                    "forbiddenClaims": [],
                    **PROGRAM_JSON,
                }
            },
            "창섭 전과자",
        )
        assert profile is not None
        assert profile.content_entity is not None
        assert profile.content_entity.entity_type == "YOUTUBE_PROGRAM"

    def test_old_documents_without_the_entity_still_load(self):
        """엔티티 필드가 없던 시절의 저장 문서도 그대로 읽힌다."""
        profile = ReferenceEvidenceProfile(has_references=True)
        assert profile.content_entity is None


class TestSearchQueries:
    def test_the_model_queries_come_first_then_code_built_ones(self):
        queries = program_entity().search_queries()
        assert queries[0] == "전과자 이창섭"
        assert "전과자 공식 채널" in " ".join(queries)
        assert queries[-1] == "전과자"

    def test_programs_want_official_thumbnails_but_films_want_posters(self):
        """유튜브·웹·방송 프로그램은 공식 채널의 영상 썸네일이 있다. 영화·드라마의 공식
        이미지는 포스터·스틸컷이라 유튜브가 아니라 일반 이미지 검색이 더 잘 찾는다."""
        assert program_entity().wants_official_youtube_thumbnail is True
        tv = program_entity().model_copy(update={"entity_type": "TV_PROGRAM"})
        assert tv.wants_official_youtube_thumbnail is True
        assert tv.is_media_content is True

        movie = program_entity().model_copy(update={"entity_type": "MOVIE"})
        assert movie.wants_official_youtube_thumbnail is False
        assert movie.is_media_content is True
        assert movie.effective_real_image_type == "OFFICIAL_POSTER_OR_STILL"

    def test_a_nameless_entity_cannot_be_searched(self):
        nameless = program_entity().model_copy(update={"canonical_name": ""})
        assert nameless.wants_official_youtube_thumbnail is False


class TestPromptBlock:
    def test_the_block_separates_core_format_from_side_scenes(self):
        block = content_entity_block(
            ReferenceEvidenceProfile(has_references=True, content_entity=program_entity())
        )
        assert "핵심 포맷(매 회차 반복되는 정체성)" in block
        assert "보조 활동(곁가지)" in block
        assert "부수 장면(중심으로 다루면 안 됨)" in block
        assert "학식 체험" in block

    def test_the_block_separates_the_raw_keyword_from_writing_phrases(self):
        block = content_entity_block(
            ReferenceEvidenceProfile(has_references=True, content_entity=program_entity())
        )
        assert "사용자가 고른 원본 검색어: '창섭 전과자'" in block
        assert "문장에 쓸 표현: 유튜브 웹예능 전과자" in block
        assert "쓰면 안 되는 표현: 창섭 전과자" in block

    def test_the_block_forbids_unverified_production_claims(self):
        block = content_entity_block(
            ReferenceEvidenceProfile(has_references=True, content_entity=program_entity())
        )
        assert "'대본이 없다'" in block

    def test_a_general_topic_produces_no_block(self):
        """일반 주제·옛 문서에서는 프롬프트가 예전과 한 글자도 달라지지 않는다."""
        assert content_entity_block(None) == ""
        assert content_entity_block(ReferenceEvidenceProfile(has_references=True)) == ""
        general = ContentEntityProfile(entity_type="GENERAL_TOPIC", canonical_name="캠핑")
        assert (
            content_entity_block(
                ReferenceEvidenceProfile(has_references=True, content_entity=general)
            )
            == ""
        )


class TestDraftPrompt:
    def test_the_draft_prompt_carries_the_entity_without_a_viewing_ban(self):
        prompt = draft_prompt(
            draft_input(
                ReferenceEvidenceProfile(
                    has_references=True,
                    has_user_experience_evidence=False,
                    content_entity=program_entity(),
                )
            )
        )
        assert "이 글의 소재 정체(검색으로 확인된 것)" in prompt
        # 2026-08-03 사용자 결정으로 시청 경험 조작 금지를 걷어냈다. 소재 정체 블록은
        # 그대로 실리고, 체험 서술을 막는 문장만 사라진 것을 못박는다.
        assert "실제 시청 경험 자료를 제공하지 않았다" not in prompt

    def test_a_draft_without_an_entity_has_no_new_blocks(self):
        prompt = draft_prompt(draft_input(None))
        assert "이 글의 소재 정체" not in prompt
        assert "실제 시청 경험 자료를 제공하지 않았다" not in prompt

    def test_the_thumbnail_copy_rule_targets_the_core_format(self):
        prompt = draft_prompt(
            draft_input(
                ReferenceEvidenceProfile(
                    has_references=True, content_entity=program_entity()
                )
            )
        )
        assert "썸네일 문구는 그 콘텐츠의 **핵심 포맷**을" in prompt


class TestEvidencePrompt:
    def test_the_prompt_asks_for_the_entity_and_shows_the_raw_keyword(self):
        prompt = reference_evidence_prompt(draft_input(None))
        assert "contentEntity 판정 규칙" in prompt
        assert "원본 검색 키워드: 창섭 전과자" in prompt
        assert "곁가지를 coreFormat에 넣지 않는다" in prompt

    def test_the_prompt_states_the_official_source_priority(self):
        prompt = reference_evidence_prompt(draft_input(None))
        assert "공식 홈페이지·공식 채널 > 공식 스토어" in prompt
        assert "후기와 커뮤니티의 주관적 표현을 공식 사실처럼 옮기지 않는다" in prompt

    def test_the_prompt_asks_for_the_category_first(self):
        """분류가 먼저다 — 카테고리가 정해져야 어떤 구조로 쓸지가 정해진다."""
        prompt = reference_evidence_prompt(draft_input(None))
        assert "카테고리 판정 규칙" in prompt
        assert "상품리뷰" in prompt and "맛집" in prompt
        assert "primaryCategory 하나가 글의 구조를 정한다" in prompt
