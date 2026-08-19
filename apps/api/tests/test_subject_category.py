"""소재 분야(2026-08-11).

왜 있는가. '오디세이'는 영화이고 게임이고 모니터다. 소재 글자만 받으면 모델이 어느 쪽인지
스스로 고르고, 사용자가 원한 분야와 다르면 제목·자료 수집·이미지가 전부 그 판단 위에
얹혀 뒤에서 되돌릴 수 없다. 그래서 사용자에게 직접 묻고, 그 답을 **조건으로** 싣는다.

여기서 지키는 것:
- 목록에 없는 값은 받지 않는다(자유 문자열이 프롬프트로 새어 나가지 않게).
- 보내지 않아도 된다 — 옛 클라이언트와 저장된 글이 그대로 돌아간다.
- 프롬프트 맨 앞 요약에 실려 모든 단계(제목·검증·설계·원고·이미지)가 같은 분야를 본다.
"""

import pytest

from app.errors import BlogTaskError
from app.llm.prompts import blog_input_summary
from app.modules.blog_task.validation import (
    validate_blog_task_input,
    validate_create_blog_task_request,
)
from app.shared import SUBJECT_CATEGORIES, BlogTaskInput

BODY = {
    "userId": "user_1",
    "topic": "오디세이",
    "purpose": ["정보 전달"],
}


class TestValidation:
    def test_a_listed_category_is_kept(self):
        result = validate_create_blog_task_request(
            {**BODY, "subjectCategory": "영화·드라마·방송"}
        )

        assert result.input.subject_category == "영화·드라마·방송"

    @pytest.mark.parametrize("category", SUBJECT_CATEGORIES)
    def test_every_listed_category_is_accepted(self, category):
        """화면의 버튼 12개가 전부 통과해야 한다 — 하나라도 막히면 그 버튼이 죽는다."""
        result = validate_blog_task_input({**BODY, "subjectCategory": category})

        assert result.subject_category == category

    def test_an_unlisted_category_is_refused(self):
        """자유 문자열을 받으면 프롬프트에 그대로 실려 모델이 뜻을 지어낸다."""
        with pytest.raises(BlogTaskError, match="subjectCategory"):
            validate_blog_task_input({**BODY, "subjectCategory": "아무거나"})

    def test_it_is_optional(self):
        """옛 클라이언트는 이 값을 보내지 않는다. 없이도 저장돼야 한다."""
        assert validate_blog_task_input(BODY).subject_category is None


class TestItReachesThePrompt:
    def test_the_chosen_field_is_stated_as_a_condition(self):
        summary = blog_input_summary(
            BlogTaskInput(
                topic="오디세이",
                keywords=["정보 전달"],
                purpose=["정보 전달"],
                subject_category="영화·드라마·방송",
            ),
            include_materials=False,
        )

        assert "주제: 영화·드라마·방송" in summary
        # 추정이 아니라 조건이라는 것이 문장으로 말해져야 한다 — 그냥 값만 적으면
        # 모델은 참고 정보 하나로 읽고 다른 분야로 넘어간다.
        assert "같은 이름의 다른 분야로 해석하지 않는다" in summary

    def test_an_old_post_without_a_category_reads_as_before(self):
        """옛 글은 이 값이 없다. 예전 문장 그대로 나가야 한다(프롬프트 표류 방지)."""
        summary = blog_input_summary(
            BlogTaskInput(topic="오디세이", keywords=["정보 전달"], purpose=["정보 전달"]),
            include_materials=False,
        )

        assert "주제: 지정 안 함" in summary
        assert "해석하지 않는다" not in summary

    def test_a_stored_subject_still_shows_when_there_is_no_category(self):
        """subject는 옛 입력이 쓰던 자리다. 카테고리가 없으면 그것을 그대로 적는다."""
        summary = blog_input_summary(
            BlogTaskInput(
                topic="오디세이",
                keywords=["정보 전달"],
                purpose=["정보 전달"],
                subject="IT·디지털",
            ),
            include_materials=False,
        )

        assert "주제: IT·디지털" in summary


class TestBackwardCompatibility:
    def test_an_old_stored_input_loads(self):
        """저장된 옛 문서에는 subjectCategory가 없다 — 강제 migration 없이 읽혀야 한다."""
        stored = {
            "topic": "옛 소재",
            "keywords": ["정보 전달"],
            "referenceMaterials": [],
        }

        blog_input = BlogTaskInput.model_validate(stored)

        assert blog_input.subject_category is None
        assert "subjectCategory" not in blog_input.to_wire()

    def test_an_old_reference_material_has_no_origin(self):
        """origin도 나중에 생긴 필드다. 없는 자료는 사용자가 넣은 것으로 다룬다."""
        blog_input = BlogTaskInput.model_validate(
            {
                "topic": "옛 소재",
                "keywords": ["정보 전달"],
                "referenceMaterials": [{"type": "TEXT", "value": "메모"}],
            }
        )

        assert blog_input.reference_materials[0].origin is None


class TestABrandPostIsAboutTheBrand:
    """브랜드로 쓰는 글(2026-08-11 사용자 결정 — 소재 칸과 브랜드 칸은 서로를 잠근다).

    브랜드에는 소개·핵심 기능·서비스가 이미 등록돼 있으므로 소재를 따로 적을 필요가 없다.
    그래서 화면은 브랜드를 고르면 소재 칸을 잠그고, 소재를 적으면 브랜드 칸을 잠근다.
    브랜드가 있다는 것은 곧 **그 브랜드가 글의 주인공**이라는 뜻이다.
    """

    def _input(self, **overrides) -> BlogTaskInput:
        return BlogTaskInput(
            topic="AIONA",
            keywords=["제품·서비스 홍보"],
            purpose=["제품·서비스 홍보"],
            **overrides,
        )

    def test_the_brand_is_named_as_the_subject(self):
        summary = blog_input_summary(
            self._input(brand_id="brand_1", brand_name="AIONA"), include_materials=False
        )

        assert "이 글의 주인공 브랜드: AIONA" in summary
        # 자료에 없는 것을 지어내지 말라는 말이 함께 있어야 한다 — 브랜드 글은 없는
        # 기능·수상을 만들어 내기 가장 쉬운 자리다.
        assert "자료에서 확인되는 것만 쓴다" in summary

    def test_no_brand_means_no_line(self):
        """브랜드를 안 쓴 글의 프롬프트는 예전 그대로여야 한다."""
        summary = blog_input_summary(self._input(), include_materials=False)

        assert "브랜드" not in summary

    def test_brand_images_are_marked_as_official_but_not_experience(self):
        """공식 이미지는 이 글 대상의 실물이지만, '써 봤다'는 근거는 아니다."""
        from app.llm.prompts import reference_evidence_prompt
        from app.shared import (
            DraftFormat,
            DraftGenerationInput,
            ReferenceMaterial,
            SelectedIntentForDraft,
        )

        draft_input = DraftGenerationInput(
            post_id="post_1",
            user_id="user_1",
            prompt_version="v1",
            format=DraftFormat.HTML,
            input=self._input(
                brand_name="AIONA",
                reference_materials=[
                    ReferenceMaterial(
                        type="IMAGE",
                        value="data:image/png;base64,AAAA",
                        name="AIONA 로고",
                        origin="brand",
                    ),
                    ReferenceMaterial(
                        type="IMAGE",
                        value="data:image/png;base64,BBBB",
                        name="내가 찍은 화면",
                    ),
                ],
            ),
            selected_intent=SelectedIntentForDraft(
                intent_id="intent_1",
                title="AIONA 소개",
                target_reader="도입을 검토하는 담당자",
                rationale="서비스를 알아보는 검색 의도",
            ),
        )

        prompt = reference_evidence_prompt(draft_input)

        assert "AIONA 로고 [브랜드가 등록한 공식 이미지" in prompt
        assert "써 봤다는 근거는 아니다" in prompt
        # 사용자가 직접 올린 사진에는 붙지 않는다.
        assert "내가 찍은 화면 [브랜드" not in prompt


class TestTheBrandFillsTheEmptyTopic:
    """브랜드로 쓰는 글은 소재가 비어서 온다 — 화면이 그 칸을 잠그기 때문이다.

    소재는 필수 값이라 서버가 브랜드 이름으로 채운다. 화면이 채워 보내지 않는 이유는,
    그러면 브랜드를 지운 뒤에도 그 이름이 소재로 남을 수 있어서다.
    """

    def _profile(self):
        from app.shared import BrandProfile

        return BrandProfile(
            brand_id="brand_1",
            user_id="user_1",
            name="AIONA",
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
        )

    def _brand_service(self):
        """브랜드 조회만 하는 자리. 2026-08-19부터 이 함수는 라우트가 아니라 브랜드
        모듈에 있다 — 자동 포스팅의 예약 작업도 같은 것을 써야 하기 때문이다."""
        from types import SimpleNamespace

        return SimpleNamespace(get_brand=self._get_brand)

    @pytest.mark.asyncio
    async def test_an_empty_topic_becomes_the_brand_name(self):
        from app.modules.brand import with_brand_materials

        payload, limit = await with_brand_materials(
            self._brand_service(), "user_1", {"topic": "", "brandId": "brand_1"}
        )

        assert payload["topic"] == "AIONA"
        assert payload["brandName"] == "AIONA"
        # 브랜드 자료가 펼쳐 들어가므로 상한도 함께 올라가야 한다.
        assert limit > 10

    @pytest.mark.asyncio
    async def test_a_typed_topic_is_left_alone(self):
        """브랜드가 소재를 덮어쓰지 않는다 — 소재를 적은 글은 그 소재가 주인공이다."""
        from app.modules.brand import with_brand_materials

        payload, _ = await with_brand_materials(
            self._brand_service(),
            "user_1",
            {"topic": "AIONA 신규 기능", "brandId": "brand_1"},
        )

        assert payload["topic"] == "AIONA 신규 기능"

    @pytest.mark.asyncio
    async def test_without_a_brand_nothing_is_filled(self):
        from app.modules.brand import with_brand_materials

        payload, limit = await with_brand_materials(None, "user_1", {"topic": "오디세이"})

        assert payload["topic"] == "오디세이"
        assert payload["brandName"] is None
        # 브랜드가 없으면 화면 기본 상한을 그대로 쓴다(2026-08-11에 그 값이
        # 10 → 사실상 무제한으로 바뀌었다 — 제한은 용량뿐이다).
        from app.modules.blog_task.validation import MAX_REFERENCE_MATERIALS

        assert limit == MAX_REFERENCE_MATERIALS

    async def _get_brand(self, user_id, brand_id):
        return self._profile()


class TestTheBrandMarkerSurvivesValidation:
    """origin 표시가 검증에서 사라지면 다시 저장할 때마다 브랜드 자료가 한 벌씩 쌓인다.

    실제로 그랬다 — 모델에는 필드를 더했는데 `_validate_reference_material`이 그 값을
    옮기지 않아, 저장된 문서에는 표시가 하나도 없었다(2026-08-11).
    """

    def test_the_brand_marker_is_kept(self):
        blog_input = validate_blog_task_input(
            {
                **BODY,
                "referenceMaterials": [
                    {"type": "TEXT", "value": "브랜드 자료", "origin": "brand"}
                ],
            }
        )

        assert blog_input.reference_materials[0].origin == "brand"

    def test_an_unknown_origin_is_dropped(self):
        """아는 값 하나만 통과시킨다 — 임의의 문자열이 저장 문서로 새지 않게."""
        blog_input = validate_blog_task_input(
            {
                **BODY,
                "referenceMaterials": [
                    {"type": "TEXT", "value": "내 메모", "origin": "지어낸 값"}
                ],
            }
        )

        assert blog_input.reference_materials[0].origin is None
