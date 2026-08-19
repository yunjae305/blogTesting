"""카테고리 자동 분류 → 카테고리별 원고 설계 → 실물 이미지 → 품질 검증(2026-08-03).

지금까지 모든 글은 하나의 통합 문체로 쓰였다. 목적(정보 전달·비교)은 갈렸지만 '독자가
무엇을 궁금해하는가'는 갈리지 않아서, 책 소개가 저자와 출판사를 말하지 않고 자동차 글이
트림과 연식을 구분하지 않았다. 그리고 '실물을 지어내지 말라'는 규칙은 영상 콘텐츠에만
있어서, 같은 실수가 종류만 바꿔 반복됐다 — 프로그램 자리의 일반 강의실이 신메뉴 자리의
일반 햄버거, 특정 배우 자리의 닮은 모델, 특정 지점 자리의 비슷한 도시 사진이 됐다.

여기서 확인하는 것:

- A. 분류 — 카테고리·유형·실물 이미지 종류를 한 판정에서 함께 받는다.
- B. 카테고리별 지침 — 메인 하나가 구조를 정하고 보조는 보완만 한다.
- C. 특수 플래그 — 유형에서 파생되고 모델이 내리지 못한다.
- D. 실물 이미지 — 영상 밖의 상품·인물·장소도 확인된 이름으로 먼저 검색한다.
- E. 품질 검증 — 카테고리 필수 정보·실물 이미지·제목의 체험 약속·고위험 단정.
- F. 하위 호환 — 카테고리를 판정하지 못한 글은 예전과 똑같이 동작한다.

소재 문자열('리아 두툼새우 버거' 등)은 테스트 fixture로만 쓴다. 운영 코드는 특정 이름이
아니라 카테고리와 유형으로 동작해야 한다.
"""

import pytest

from app.llm.category_playbooks import (
    PLAYBOOKS,
    category_image_block,
    category_writing_block,
    playbook_for,
    required_facts_for,
)
from app.llm.parsing import content_entity_from_json
from app.llm.prompts import (
    card_plan_prompt,
    content_entity_block,
    content_plan_prompt,
    draft_prompt,
    reference_evidence_prompt,
    title_plan_prompt,
)
from app.llm.schemas import CONTENT_ENTITY_SCHEMA
from app.modules.draft.content_validation import (
    run_content_validations,
    validate_category_fit,
    validate_high_stakes_certainty,
    validate_real_entity_image_used,
)
from app.shared import (
    BLOG_CATEGORIES,
    BlogTaskInput,
    ContentEntityProfile,
    DraftFormat,
    DraftGenerationInput,
    FinalPost,
    GeneratedPostImage,
    ReferenceEvidenceProfile,
    RelatedPerson,
    SelectedIntentForDraft,
)

NOW = "2026-08-03T00:00:00.000Z"


# --- fixture: 세 종류의 소재 -------------------------------------------------

MENU = ContentEntityProfile(
    entity_type="BRAND_MENU_ITEM",
    primary_category="상품리뷰",
    secondary_category="맛집",
    writing_mode="신제품 정보형",
    canonical_name="리아 두툼새우 버거",
    brand="롯데리아",
    raw_keyword="롯데리아 리아 두툼새우 버거",
    natural_phrases=["롯데리아의 리아 두툼새우 버거"],
    requires_fresh_research=True,
    requires_real_images=True,
    real_image_type="OFFICIAL_PRODUCT_IMAGE",
    confidence=0.96,
)

GROUP = ContentEntityProfile(
    entity_type="IDOL_GROUP",
    primary_category="스타·연예인",
    writing_mode="인물 소개형",
    canonical_name="프로미스나인",
    related_people=[RelatedPerson(name="백지헌", relation="멤버")],
    raw_keyword="백지헌 프로미스나인",
    real_image_type="OFFICIAL_PERSON_PHOTO",
    confidence=0.9,
)

CLINIC = ContentEntityProfile(
    entity_type="MEDICAL_TOPIC",
    primary_category="건강·의학",
    writing_mode="증상 안내형",
    canonical_name="족저근막염",
    confidence=0.8,
)


def post(title="롯데리아 리아 두툼새우 버거, 출시 정보와 구성 정리", lead=None, body_extra=""):
    lead = lead or (
        "롯데리아의 리아 두툼새우 버거는 통새우 패티를 두 장 넣은 신메뉴입니다."
        " 단품과 세트 구성이 함께 나오며, 가격은 판매 채널에 따라 달라질 수 있습니다."
    )
    filler = "\n\n".join(
        f"{n}번 문단입니다. 공식 안내에서 확인된 구성과 판매 정보를 정리합니다."
        for n in range(1, 5)
    )
    markdown = f"# {title}\n\n{lead}\n\n## 구성과 가격\n\n{filler}{body_extra}"
    return FinalPost(
        title=title,
        body=f"{lead}\n\n{filler}{body_extra}",
        hashtags=["롯데리아"],
        html_content=f"<article><h1>{title}</h1><p>{lead}</p></article>",
        markdown_content=markdown,
    )


def evidence(entity=MENU, experience=False) -> ReferenceEvidenceProfile:
    return ReferenceEvidenceProfile(
        has_references=True,
        has_user_experience_evidence=experience,
        content_entity=entity,
    )


def image(source: str) -> GeneratedPostImage:
    return GeneratedPostImage(
        data_url="data:image/png;base64,AA==",
        alt_text="대표 이미지",
        prompt="",
        provider="stub",
        model="stub",
        generated_at=NOW,
        mime_type="image/png",
        source=source,
    )


def draft_input(entity=MENU, *, experience=False, topic="리아 두툼새우 버거") -> DraftGenerationInput:
    return DraftGenerationInput(
        post_id="post_1",
        user_id="user_1",
        input=BlogTaskInput(
            topic=topic,
            subject=None,
            purpose=["정보 전달"],
            keywords=["롯데리아 리아 두툼새우 버거"],
            target_reader="신메뉴를 먹어볼지 고민하는 사람",
        ),
        selected_intent=SelectedIntentForDraft(
            intent_id="intent_1",
            title="리아 두툼새우 버거의 구성과 가격이 궁금하다",
            target_reader="신메뉴를 먹어볼지 고민하는 사람",
            rationale="출시 직후 검색이 몰린다",
            keywords=["리아 두툼새우 버거 가격"],
            sources=None,
        ),
        prompt_version="m4@test",
        format=DraftFormat.HTML,
        raw_keywords=["롯데리아 리아 두툼새우 버거"],
        reference_evidence=(
            evidence(entity, experience=experience) if entity is not None else None
        ),
    )


# --- A. 분류 -----------------------------------------------------------------


class TestClassification:
    def test_every_blog_category_has_a_playbook(self):
        """카테고리를 늘리고 지침을 잊으면 그 글은 조용히 예전과 같아진다."""
        assert {p.category for p in PLAYBOOKS} == set(BLOG_CATEGORIES)

    def test_the_schema_asks_for_category_and_real_image_type(self):
        required = CONTENT_ENTITY_SCHEMA["required"]
        for field in (
            "primaryCategory",
            "secondaryCategory",
            "writingMode",
            "brand",
            "requiresFreshResearch",
            "requiresRealImages",
            "realImageType",
        ):
            assert field in required
        assert "상품리뷰" in CONTENT_ENTITY_SCHEMA["properties"]["primaryCategory"]["enum"]

    def test_the_prompt_lists_the_categories_and_says_the_main_one_decides(self):
        prompt = reference_evidence_prompt(draft_input())
        assert "카테고리 판정 규칙" in prompt
        assert "primaryCategory 하나가 글의 구조를 정한다" in prompt
        assert "두 카테고리를 반씩 섞은 글을 만들지 않는다" in prompt
        for name in ("상품리뷰", "맛집", "건강·의학", "IT·컴퓨터"):
            assert name in prompt

    def test_the_parser_reads_the_new_fields(self):
        entity = content_entity_from_json(
            {
                "contentEntity": {
                    "entityType": "BRAND_MENU_ITEM",
                    "primaryCategory": "상품리뷰",
                    "secondaryCategory": "맛집",
                    "writingMode": "신제품 정보형",
                    "canonicalName": "리아 두툼새우 버거",
                    "brand": "롯데리아",
                    "requiresFreshResearch": True,
                    "requiresRealImages": True,
                    "realImageType": "OFFICIAL_PRODUCT_IMAGE",
                    "confidence": 0.96,
                }
            },
            raw_keyword="롯데리아 리아 두툼새우 버거",
        )
        assert entity is not None
        assert entity.primary_category == "상품리뷰"
        assert entity.secondary_category == "맛집"
        assert entity.brand == "롯데리아"
        assert entity.requires_fresh_research is True
        assert entity.effective_real_image_type == "OFFICIAL_PRODUCT_IMAGE"

    def test_an_unknown_category_falls_back_to_none(self):
        """모르는 값이 그대로 흘러가면 지침 조회가 조용히 빗나간다."""
        entity = content_entity_from_json(
            {"contentEntity": {"entityType": "GENERAL_TOPIC", "primaryCategory": "여행"}}
        )
        assert entity is not None
        assert entity.primary_category == ""

    def test_a_secondary_equal_to_the_primary_is_dropped(self):
        entity = content_entity_from_json(
            {
                "contentEntity": {
                    "entityType": "GENERAL_TOPIC",
                    "primaryCategory": "맛집",
                    "secondaryCategory": "맛집",
                }
            }
        )
        assert entity is not None
        assert entity.secondary_category == ""


# --- B. 카테고리별 지침 -------------------------------------------------------


class TestCategoryPlaybook:
    def test_the_main_category_supplies_the_structure(self):
        block = category_writing_block(MENU)
        assert "이 글의 카테고리: 상품리뷰 (보조: 맛집)" in block
        # 상품리뷰의 필수 조사 항목과 구조가 그대로 실린다.
        assert "정식 상품명" in block and "기존 제품과 차이" in block
        assert "1. 상품 기본 정보" in block

    def test_the_secondary_category_only_adds_questions_and_bans(self):
        """보조 카테고리의 '권장 구조'는 실리지 않는다 — 두 구조를 이어 붙이면
        어느 쪽 독자도 답을 얻지 못한다."""
        block = category_writing_block(MENU)
        assert "보조 카테고리 '맛집'에서 추가로 챙길 것" in block
        # 맛집의 구조 1번 항목이 통째로 실리면 구조가 두 벌이 된다.
        assert "1. 매장 또는 메뉴 소개" not in block

    def test_a_different_category_asks_different_questions(self):
        book = playbook_for("문학·책")
        car = playbook_for("자동차")
        assert book is not None and car is not None
        assert "저자" in book.research_items
        assert "트림" in car.research_items
        assert set(book.reader_questions) != set(car.reader_questions)

    def test_the_content_plan_prompt_carries_the_category(self):
        prompt = content_plan_prompt(draft_input())
        assert "이 글의 카테고리: 상품리뷰" in prompt
        assert "정식 상품명" in prompt

    def test_the_draft_prompt_carries_the_category_and_the_self_check(self):
        prompt = draft_prompt(draft_input())
        assert "이 글의 카테고리: 상품리뷰" in prompt
        assert "'상품리뷰' 카테고리 자체 점검" in prompt
        assert "제목·본문·이미지가 모두 같은 상품인가" in prompt

    def test_the_title_prompt_points_at_the_reader_question(self):
        prompt = title_plan_prompt(draft_input())
        assert "'상품리뷰' 카테고리다" in prompt
        assert "어떤 상품인가" in prompt

    def test_the_image_block_ranks_official_images_first(self):
        block = category_image_block(MENU)
        assert "우선순위 1: 해당 상품의 공식 이미지" in block
        assert "같은 종류의 일반 상품 이미지로 대체" in block


# --- C. 특수 플래그 -----------------------------------------------------------


class TestSpecialFlags:
    @pytest.mark.parametrize(
        "entity_type,flag",
        [
            ("BRAND_MENU_ITEM", "is_real_product"),
            ("CAR_MODEL", "is_real_product"),
            ("IDOL_GROUP", "is_real_person_or_group"),
            ("SPORTS_PLAYER", "is_real_person_or_group"),
            ("RESTAURANT", "is_real_place"),
            ("TRAVEL_DESTINATION", "is_real_place"),
            ("YOUTUBE_PROGRAM", "is_media_content"),
            ("DRAMA_SERIES", "is_media_content"),
        ],
    )
    def test_flags_come_from_the_entity_type(self, entity_type, flag):
        entity = ContentEntityProfile(entity_type=entity_type, canonical_name="대상")
        assert getattr(entity, flag) is True

    def test_high_stakes_is_on_for_the_type_or_the_category(self):
        assert CLINIC.is_high_stakes is True
        # 유형은 일반 주제여도 카테고리가 건강·의학이면 켜진다.
        general = ContentEntityProfile(
            entity_type="GENERAL_TOPIC", primary_category="비즈니스·경제"
        )
        assert general.is_high_stakes is True
        assert MENU.is_high_stakes is False

    def test_the_model_cannot_turn_real_images_off(self):
        """실존 대상이라는 판정 자체가 이미 답이다. 모델은 올릴 수만 있다."""
        stubborn = MENU.model_copy(update={"requires_real_images": False})
        assert stubborn.wants_real_image is True

    def test_without_a_name_there_is_nothing_to_search_for(self):
        nameless = MENU.model_copy(update={"canonical_name": ""})
        assert nameless.effective_real_image_type == "NONE"
        assert nameless.wants_real_image is False

    def test_an_unknown_real_image_type_falls_back_to_the_entity_default(self):
        entity = GROUP.model_copy(update={"real_image_type": "NONE"})
        assert entity.effective_real_image_type == "OFFICIAL_PERSON_PHOTO"


# --- D. 실물 이미지 경로 -------------------------------------------------------


class TestRealImageRouting:
    def test_the_brand_leads_the_product_query(self):
        """'리아 두툼새우 버거'만으로는 어느 브랜드의 메뉴인지 갈리지 않는다."""
        assert MENU.search_queries()[0] == "롯데리아 리아 두툼새우 버거"

    def test_the_group_name_leads_the_member_query(self):
        assert GROUP.search_queries()[0] == "프로미스나인 백지헌"

    def test_the_card_prompt_forbids_replacing_a_real_product(self):
        prompt = card_plan_prompt(draft_input(), post(), 0, 0)
        assert "OFFICIAL_PRODUCT_IMAGE" in prompt
        assert "롯데리아 리아 두툼새우 버거" in prompt
        assert "타 브랜드의 비슷한 제품으로 대체하지 않는다" in prompt

    def test_a_person_card_must_show_the_person(self):
        prompt = card_plan_prompt(draft_input(GROUP), post(), 0, 0)
        assert "닮은 모델·이름 없는 일반인·생성 인물로 채우지 않는다" in prompt
        assert "subjectKind=REAL_NAMED_PERSON" in prompt

    def test_an_abstract_topic_gets_no_real_image_rule(self):
        plain = ContentEntityProfile(
            entity_type="GENERAL_TOPIC", primary_category="일상·생각", canonical_name="퇴근길"
        )
        assert plain.wants_real_image is False
        prompt = card_plan_prompt(draft_input(plain), post(), 0, 0)
        assert "실존 대상 글의 사진 규칙" not in prompt


# --- E. 품질 검증 -------------------------------------------------------------


class TestCategoryFit:
    def test_a_complete_product_article_passes(self):
        assert validate_category_fit(post(), MENU).status == "PASS"

    def test_a_missing_brand_warns(self):
        """상품리뷰인데 브랜드를 모른다면, 어느 브랜드의 무엇인지 모르는 채로 쓴 글이다."""
        result = validate_category_fit(post(), MENU.model_copy(update={"brand": ""}))
        assert result.status == "WARN"
        assert "브랜드·제작 주체" in result.details["unconfirmed"]

    def test_a_name_missing_from_the_lead_warns(self):
        vague = post(
            title="요즘 화제인 신메뉴 정리",
            lead="최근 패스트푸드 업계에서 새로운 메뉴가 여럿 나오고 있습니다.",
        )
        result = validate_category_fit(vague, MENU)
        assert result.status == "WARN"
        assert result.details["canonicalNameInLead"] is False

    def test_a_category_without_required_facts_only_checks_the_name(self):
        essay = ContentEntityProfile(
            entity_type="GENERAL_TOPIC", primary_category="일상·생각"
        )
        assert required_facts_for(essay) == ()
        assert validate_category_fit(post(), essay).status == "PASS"

    def test_without_a_category_the_check_is_skipped(self):
        assert validate_category_fit(post(), None).status == "SKIPPED"
        bare = ContentEntityProfile(entity_type="BRAND_MENU_ITEM", canonical_name="버거")
        assert validate_category_fit(post(), bare).status == "SKIPPED"


class TestRealEntityImageUsed:
    def test_a_web_photo_cover_passes(self):
        final = post().model_copy(update={"featured_image": image("web")})
        assert validate_real_entity_image_used(final, MENU).status == "PASS"

    def test_a_generated_cover_fails(self):
        final = post().model_copy(update={"featured_image": image("generated")})
        result = validate_real_entity_image_used(final, MENU)
        assert result.status == "FAIL"
        assert result.details["realImageType"] == "OFFICIAL_PRODUCT_IMAGE"

    def test_a_user_upload_counts_as_real(self):
        final = post().model_copy(update={"featured_image": image("reference")})
        assert validate_real_entity_image_used(final, MENU).status == "PASS"

    def test_youtube_content_is_left_to_the_other_check(self):
        """두 검사가 같은 글을 함께 잡지 않는다."""
        program = ContentEntityProfile(
            entity_type="YOUTUBE_PROGRAM", canonical_name="전과자", primary_category="방송"
        )
        final = post().model_copy(update={"featured_image": image("generated")})
        assert validate_real_entity_image_used(final, program).status == "SKIPPED"

    def test_an_abstract_topic_is_skipped(self):
        plain = ContentEntityProfile(entity_type="GENERAL_TOPIC", canonical_name="퇴근길")
        final = post().model_copy(update={"featured_image": image("generated")})
        assert validate_real_entity_image_used(final, plain).status == "SKIPPED"


class TestHighStakesCertainty:
    def test_a_guarantee_fails(self):
        bad = post(
            title="족저근막염 관리 방법",
            lead="이 스트레칭을 하면 반드시 낫습니다. 부작용이 없으니 걱정하지 않아도 됩니다.",
        )
        result = validate_high_stakes_certainty(bad, CLINIC)
        assert result.status == "FAIL"
        assert "반드시 낫" in result.details["phrases"]

    def test_a_careful_article_passes(self):
        careful = post(
            title="족저근막염이 의심될 때 확인할 것",
            lead="증상과 회복 속도는 사람마다 다릅니다. 통증이 2주 이상 이어지면 병원에서 확인하세요.",
        )
        assert validate_high_stakes_certainty(careful, CLINIC).status == "PASS"

    def test_an_ordinary_topic_is_skipped(self):
        """같은 표현도 일반 글에서는 과장일 뿐이다 — 그쪽은 낚시·과장 검사가 본다."""
        bad = post(lead="이 버거는 100% 보장된 맛입니다.")
        assert validate_high_stakes_certainty(bad, MENU).status == "SKIPPED"


# --- F. 하위 호환 -------------------------------------------------------------


class TestBackwardCompatibility:
    def test_an_old_document_without_a_category_keeps_the_old_prompts(self):
        """카테고리를 판정하지 못한 글의 프롬프트는 한 글자도 달라지지 않는다."""
        legacy = ContentEntityProfile(entity_type="GENERAL_TOPIC", canonical_name="캠핑")
        assert category_writing_block(legacy) == ""
        assert category_image_block(legacy) == ""
        assert content_entity_block(
            ReferenceEvidenceProfile(has_references=True, content_entity=legacy)
        ) == ""

    def test_an_old_entity_type_still_loads_and_keeps_its_flags(self):
        """MOVIE_OR_DRAMA·PLACE는 지금 더 잘게 나뉘지만 저장된 글이 그 값을 들고 있다."""
        old_film = ContentEntityProfile(
            entity_type="MOVIE_OR_DRAMA", canonical_name="어떤 영화"
        )
        assert old_film.is_media_content is True
        assert old_film.effective_real_image_type == "OFFICIAL_POSTER_OR_STILL"

        old_place = ContentEntityProfile(entity_type="PLACE", canonical_name="어떤 장소")
        assert old_place.is_real_place is True
        assert old_place.effective_real_image_type == "OFFICIAL_PLACE_PHOTO"

    def test_the_new_checks_are_skipped_without_an_entity(self):
        result = run_content_validations(post(), None, [], [])
        by_check = {c.check: c for c in result.checks}
        assert by_check["category_fit"].status == "SKIPPED"
        assert by_check["high_stakes_certainty"].status == "SKIPPED"

    def test_a_classified_article_runs_the_new_checks(self):
        result = run_content_validations(post(), None, [], [], evidence=evidence())
        by_check = {c.check: c for c in result.checks}
        assert by_check["category_fit"].status == "PASS"
        assert result.has_fail is False
