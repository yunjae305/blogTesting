"""브랜드 자료 저장과 모델에게 줄 설명 만들기.

여기 없는 것: 실제 Mongo. 검증기는 `scripts/init_mongo.py`에 있고 이 테스트는 메모리
저장소로 돈다. 그래서 **검증기와 모델이 어긋나면 여기서는 잡히지 않는다** — 필드를 더할
때는 init_mongo.py의 required도 함께 봐야 한다(CLAUDE.md의 알려진 함정).
"""

import json

import pytest

from app.errors import BlogTaskError
from app.modules.brand import (
    BRAND_MATERIAL_ORIGIN,
    BrandService,
    InMemoryBrandRepository,
    brand_brief,
    brand_reference_materials,
    merge_brand_materials,
)
from app.shared import BrandAudience, BrandDocument, BrandProfile


def service() -> BrandService:
    return BrandService(InMemoryBrandRepository())


def body(**overrides) -> dict:
    return {"name": "AIONA", **overrides}


@pytest.mark.asyncio
class TestSaving:
    async def test_the_smallest_useful_record_is_just_a_name(self):
        brand = await service().create_brand("user_1", body())

        assert brand.name == "AIONA"
        assert brand.brand_id.startswith("brand_")
        assert brand.description is None

    async def test_a_record_without_a_name_is_refused(self):
        with pytest.raises(BlogTaskError, match="이름"):
            await service().create_brand("user_1", body(name="  "))

    async def test_every_problem_is_reported_at_once(self):
        """칸이 많은 화면이다. 하나 고칠 때마다 다시 저장해 보게 하면 지친다."""
        with pytest.raises(BlogTaskError) as raised:
            await service().create_brand(
                "user_1", {"name": "", "links": [{"url": "aiona.kr"}]}
            )

        assert "이름" in str(raised.value)
        assert "http" in str(raised.value)

    async def test_a_link_without_http_is_refused(self):
        with pytest.raises(BlogTaskError, match="http"):
            await service().create_brand(
                "user_1", body(links=[{"label": "홈", "url": "aiona.kr"}])
            )

    async def test_an_image_must_be_a_data_url(self):
        """바깥 주소를 그대로 두면 발행 뒤 이미지가 깨진다(원고 이미지와 같은 이유)."""
        with pytest.raises(BlogTaskError, match="data:image"):
            await service().create_brand(
                "user_1", body(images=[{"label": "로고", "dataUrl": "https://aiona.kr/logo.png"}])
            )

    async def test_a_data_url_image_is_kept(self):
        brand = await service().create_brand(
            "user_1",
            body(images=[{"label": "로고", "dataUrl": "data:image/png;base64,AAAA"}]),
        )

        assert brand.images[0].data_url.startswith("data:image/")


@pytest.mark.asyncio
class TestReadingBack:
    async def test_only_my_records_come_back(self):
        # 2026-08-19부터 목록에는 기본 브랜드(AIONA)가 함께 있다 — 그것까지 남의 것과
        # 섞이지 않는지 본다. 이름이 아니라 **소유자**로 확인해야 하는 이유다.
        api = service()
        await api.create_brand("user_1", body(name="내 브랜드"))
        await api.create_brand("user_2", body(name="다른 회사"))

        mine = await api.list_brands("user_1")

        assert {b.user_id for b in mine} == {"user_1"}
        assert "내 브랜드" in [b.name for b in mine]
        assert "다른 회사" not in [b.name for b in mine]

    async def test_someone_elses_record_is_not_found(self):
        api = service()
        brand = await api.create_brand("user_2", body())

        with pytest.raises(BlogTaskError, match="찾을 수 없습니다"):
            await api.get_brand("user_1", brand.brand_id)

    async def test_editing_keeps_the_created_time(self):
        """고칠 때마다 새로 찍히면 '언제 만든 자료인지'를 잃는다."""
        api = service()
        brand = await api.create_brand("user_1", body())

        updated = await api.update_brand("user_1", brand.brand_id, body(description="고친 소개"))

        assert updated.created_at == brand.created_at
        assert updated.description == "고친 소개"

    async def test_the_light_view_keeps_text_but_drops_attachments(self):
        """자료 편집 첫 화면용 가벼운 조회(2026-08-07).

        전체 문서는 base64 첨부까지 2MB라 Atlas에서 20초 넘게 걸렸다(실측 22~25초).
        텍스트 필드는 그대로 오고, 이미지·문서만 비어 있어야 한다 — 화면이 이것으로
        먼저 열리고 첨부는 전체 조회가 뒤따라 채운다.
        """
        api = service()
        brand = await api.create_brand(
            "user_1",
            body(
                description="AI 교육 회사",
                features="교육, 컨설팅",
                links=[{"label": "홈", "url": "https://aiona.kr"}],
                documents=[
                    {"section": "description", "name": "소개.txt", "kind": "TEXT", "value": "소개 전문"}
                ],
                images=[{"label": "로고", "dataUrl": "data:image/png;base64,AAAA"}],
            ),
        )

        light = await api.get_brand_light("user_1", brand.brand_id)

        assert light.name == "AIONA"
        assert light.description == "AI 교육 회사"
        assert light.features == "교육, 컨설팅"
        assert [link.url for link in light.links] == ["https://aiona.kr"]
        assert light.images == [] and light.documents == []
        # 가벼운 조회가 원본을 바꾸면 안 된다 — 전체 조회에는 첨부가 그대로 있다.
        full = await api.get_brand("user_1", brand.brand_id)
        assert len(full.images) == 1 and len(full.documents) == 1

    async def test_the_light_view_of_a_missing_brand_is_not_found(self):
        with pytest.raises(BlogTaskError, match="찾을 수 없습니다"):
            await service().get_brand_light("user_1", "brand_none")


class TestTheBriefGivenToTheModel:
    def _profile(self, **overrides) -> BrandProfile:
        return BrandProfile(
            brand_id="brand_1",
            user_id="user_1",
            name="AIONA",
            created_at="2026-08-05T00:00:00.000Z",
            updated_at="2026-08-05T00:00:00.000Z",
            **overrides,
        )

    def test_empty_sections_are_left_out(self):
        """'서비스: (없음)' 같은 줄을 주면 모델이 그것을 사실로 받아 쓴다."""
        brief = brand_brief(self._profile())

        assert "AIONA" in brief
        assert "제공 서비스" not in brief
        assert "쓰지 말 것" not in brief

    def test_what_is_filled_in_shows_up(self):
        brief = brand_brief(
            self._profile(
                description="AI 교육 회사입니다.",
                features="실무 교육, 챗봇 구축",
                audiences=[BrandAudience(category="기업·사업자", types=["중소기업"])],
            )
        )

        assert "AI 교육 회사입니다." in brief
        assert "핵심 기능·서비스:\n실무 교육, 챗봇 구축" in brief
        assert "주요 고객: 기업·사업자(중소기업)" in brief

    def test_the_tone_is_not_set_here(self):
        """말투는 사용자 설정의 페르소나가 정한다. 두 군데서 정하면 서로 어긋난다."""
        brief = brand_brief(self._profile(description="소개"))

        assert "말투" not in brief


class TestTheInputForABrandPost:
    """브랜드를 고른 글의 참고자료(2026-08-11 — 브랜드 글쓰기를 새 글 작성에 통합).

    **새 프롬프트를 만들지 않는다.** 브랜드 자료를 기존 참고자료(referenceMaterials)로
    실어 보내면 자료 수집·제목·원고가 손댈 것 없이 그대로 처리하고, 원고 품질 검수와
    타깃 조건도 이 글에 똑같이 적용된다.

    소재·목적·연령은 브랜드가 정하지 않는다. 예전 브랜드 화면은 소재 문장까지 만들어
    ``{키워드} — {브랜드}와(과) 엮어서``로 덮어썼지만, 이제 소재는 사용자가 소재 단계에서
    직접 적는다 — 브랜드가 하는 일은 자기 자료를 얹는 것뿐이다.
    """

    def _profile(self, **overrides) -> BrandProfile:
        return BrandProfile(
            brand_id="brand_1",
            user_id="user_1",
            name="AIONA",
            created_at="2026-08-05T00:00:00.000Z",
            updated_at="2026-08-05T00:00:00.000Z",
            **overrides,
        )

    def test_the_brand_goes_in_as_a_reference_material(self):
        materials = merge_brand_materials(
            self._profile(description="AI 교육 회사입니다."), []
        )
        texts = [m for m in materials if m["type"] == "TEXT"]

        assert len(texts) == 1
        assert "AI 교육 회사입니다." in texts[0]["value"]

    def test_links_and_images_come_along_in_their_own_types(self):
        materials = merge_brand_materials(
            self._profile(
                links=[{"label": "홈페이지", "url": "https://aiona.kr/"}],
                images=[{"label": "로고", "dataUrl": "data:image/png;base64,AAAA"}],
            ),
            [],
        )
        kinds = [m["type"] for m in materials]

        assert kinds == ["TEXT", "URL", "IMAGE"]
        assert materials[1]["value"] == "https://aiona.kr/"
        assert materials[2]["value"].startswith("data:image/")

    def test_brand_materials_are_marked_so_they_can_be_replaced(self):
        """표시가 없으면 다시 저장할 때마다 같은 자료가 한 벌씩 늘어난다."""
        materials = brand_reference_materials(self._profile(description="소개"))

        assert all(m["origin"] == BRAND_MATERIAL_ORIGIN for m in materials)

    def test_the_user_materials_survive_and_come_after(self):
        mine = {"type": "TEXT", "value": "내가 적은 메모"}
        materials = merge_brand_materials(self._profile(description="소개"), [mine])

        assert materials[-1] == mine
        assert materials[0]["origin"] == BRAND_MATERIAL_ORIGIN

    def test_saving_again_does_not_stack_a_second_copy(self):
        """화면은 저장돼 있던 목록을 그대로 돌려보낸다 — 걷어내지 않으면 두 벌이 된다."""
        profile = self._profile(description="소개")
        first = merge_brand_materials(profile, [{"type": "TEXT", "value": "내 메모"}])

        second = merge_brand_materials(profile, first)

        assert second == first

    def test_clearing_the_brand_removes_its_materials(self):
        """브랜드 선택을 풀면 그 자료도 함께 빠진다 — 사용자 자료만 남는다."""
        mine = {"type": "TEXT", "value": "내 메모"}
        with_brand = merge_brand_materials(self._profile(description="소개"), [mine])

        assert merge_brand_materials(None, with_brand) == [mine]


class TestImageLimitsFitInOneRequest:
    """이미지 상한이 요청 본문 상한과 맞는지.

    이미지는 data URL(base64)로 **한 요청에 통째로** 실려 간다. 상한을 따로 정하면
    화면은 10장까지 된다고 해 놓고 4장째에서 413으로 잘린다 — 실제로 그랬다.
    """

    def test_a_full_set_of_images_fits_under_the_body_limit(self):
        from app.http.routes import MAX_JSON_BODY_BYTES
        from app.shared import BrandLimits

        # base64는 원본보다 약 4/3 크다. 여유를 보지 않고 최악만 따져도 들어가야 한다.
        worst_case = BrandLimits.MAX_IMAGES * BrandLimits.MAX_IMAGE_BYTES * 4 // 3

        assert worst_case < MAX_JSON_BODY_BYTES

    def test_an_image_over_the_limit_is_refused_with_the_reason(self):
        from app.modules.brand import validate_brand_body
        from app.shared import BrandLimits

        too_big = "data:image/png;base64," + "A" * (BrandLimits.MAX_IMAGE_BYTES * 4 // 3 + 100)

        with pytest.raises(BlogTaskError) as raised:
            validate_brand_body({"name": "AIONA", "images": [{"dataUrl": too_big}]})

        # 몇 KB까지인지, 왜 그런지가 문구에 있어야 사용자가 줄일 수 있다.
        assert "KB" in str(raised.value)
        assert "장" in str(raised.value)


class TestTheThreeWritingSections:
    """서술 칸 셋은 주제만 나누고 안은 줄글이다.

    처음에는 서비스·고객을 "한 줄에 하나씩" 목록으로 받았다. 정리해 둔 글을 쪼개 넣어야
    해서 불편했고(보통 쉼표로 이어 쓴다), 그렇다고 통째로 한 칸에 몰면 무엇을 적어야
    할지 알 수 없었다. 형식은 강요하지 않고 주제만 나눈다.
    """

    def test_commas_and_line_breaks_are_both_left_alone(self):
        from app.modules.brand import validate_brand_body

        cleaned = validate_brand_body(
            {"name": "AIONA", "features": "실무 교육, 챗봇 구축\n도입 컨설팅"}
        )

        # 쪼개지도, 합치지도 않는다. 적은 그대로 모델에게 간다.
        assert cleaned["features"] == "실무 교육, 챗봇 구축\n도입 컨설팅"

    def test_a_blank_section_becomes_none_not_an_empty_string(self):
        """빈 문자열을 저장하면 브리프에 '핵심 기능:' 빈 줄이 실린다."""
        from app.modules.brand import validate_brand_body

        cleaned = validate_brand_body({"name": "AIONA", "features": "   "})

        assert cleaned["features"] is None

    def test_a_section_over_the_limit_says_which_one(self):
        from app.modules.brand import validate_brand_body
        from app.shared import BrandLimits

        with pytest.raises(BlogTaskError, match="핵심 기능"):
            validate_brand_body(
                {"name": "AIONA", "features": "가" * (BrandLimits.MAX_SECTION_LENGTH + 1)}
            )


class TestPickingTheAudience:
    """주요 고객은 자유 입력이 아니라 **고른 것**이다(대분류 → 유형).

    자유 입력을 없앤 이유: 사람마다 "중소기업"·"중기"·"SMB"로 달리 적어 프롬프트가
    들쭉날쭉해졌고, 무엇을 적어야 할지 몰라 비워 두는 칸이 됐다.

    여기 없는 것: 연령대·글 목적·이번 글의 키워드. 그 셋은 글마다 달라서 작성 화면에서
    받는다 — 브랜드 자료에 박아 두면 모든 글이 같은 대상을 향하게 된다.
    """

    def test_picked_categories_and_types_are_kept(self):
        from app.modules.brand import validate_brand_body

        cleaned = validate_brand_body(
            {
                "name": "AIONA",
                "audiences": [
                    {"category": "기업·사업자", "types": ["중소기업", "스타트업"]},
                ],
            }
        )

        assert cleaned["audiences"][0].category == "기업·사업자"
        assert cleaned["audiences"][0].types == ["중소기업", "스타트업"]

    def test_a_value_outside_the_catalog_is_refused(self):
        """임의 문자열이 들어오면 자유 입력을 없앤 목적이 무너진다."""
        from app.modules.brand import validate_brand_body

        with pytest.raises(BlogTaskError, match="모르는 고객 대분류"):
            validate_brand_body({"name": "A", "audiences": [{"category": "우주인", "types": []}]})

        with pytest.raises(BlogTaskError, match="없는 유형"):
            validate_brand_body(
                {"name": "A", "audiences": [{"category": "교육기관", "types": ["대기업"]}]}
            )

    def test_a_category_with_no_types_is_dropped(self):
        """대분류만 켜고 아래를 비운 상태는 화면에서 쉽게 만들어진다.

        그대로 저장하면 프롬프트에 "기업·사업자()" 같은 빈 껍데기가 실린다.
        """
        from app.modules.brand import validate_brand_body

        cleaned = validate_brand_body(
            {"name": "A", "audiences": [{"category": "기업·사업자", "types": []}]}
        )

        assert cleaned["audiences"] == []

    def test_free_text_is_kept_only_for_the_other_type(self):
        """'기타'를 고르지 않았는데 남은 글자는 화면에 보이지 않는다 — 저장하면 안 된다."""
        from app.modules.brand import validate_brand_body

        with_other = validate_brand_body(
            {
                "name": "A",
                "audiences": [
                    {"category": "기업·사업자", "types": ["기타"], "other": "협동조합"}
                ],
            }
        )
        without_other = validate_brand_body(
            {
                "name": "A",
                "audiences": [
                    {"category": "기업·사업자", "types": ["중소기업"], "other": "협동조합"}
                ],
            }
        )

        assert with_other["audiences"][0].other == "협동조합"
        assert without_other["audiences"][0].other is None

    def test_the_brief_spells_the_picks_out_as_a_sentence(self):
        """모델은 글로 읽는다. 저장 구조가 아니라 문장이 나가야 한다."""
        profile = BrandProfile(
            brand_id="b",
            user_id="u",
            name="AIONA",
            created_at="x",
            updated_at="x",
            audiences=[
                BrandAudience(category="기업·사업자", types=["중소기업", "스타트업"]),
                BrandAudience(category="교육기관", types=["대학·대학원", "기타"], other="직업훈련기관"),
            ],
        )

        brief = brand_brief(profile)

        # '기타'라는 낱말은 빼고 직접 적은 것만 싣는다 — 모델에게 "기타"는 아무 뜻도 아니다.
        assert "주요 고객: 기업·사업자(중소기업, 스타트업) / 교육기관(대학·대학원, 직업훈련기관)" in brief
        assert "기타" not in brief

    def test_the_catalog_offers_other_everywhere(self):
        """어느 대분류를 골라도 직접 적을 길이 있어야 한다."""
        from app.shared import AUDIENCE_CATALOG, AUDIENCE_OTHER

        assert all(AUDIENCE_OTHER in types for types in AUDIENCE_CATALOG.values())


class TestUploadedDocuments:
    """텍스트·PDF 파일을 브랜드 자료로 올리는 것.

    줄글로 옮기기 어려운 회사 소개서를 파일째 두려는 것이다. 새 통로는 만들지 않았다 —
    이 저장소의 파이프라인이 이미 TEXT·PDF 참고자료를 읽고, PDF는 프롬프트를 만들 때
    안의 글자를 뽑는다(llm/prompts.py).
    """

    def test_text_is_kept_as_letters_and_pdf_as_a_data_url(self):
        from app.modules.brand import validate_brand_body

        cleaned = validate_brand_body(
            {
                "name": "AIONA",
                "documents": [
                    {
                        "section": "description",
                        "name": "회사소개.txt",
                        "kind": "TEXT",
                        "value": "AI 교육 회사입니다.",
                    },
                    {
                        "section": "features",
                        "name": "서비스.pdf",
                        "kind": "PDF",
                        "value": "data:application/pdf;base64,JVBERi0x",
                    },
                ],
            }
        )

        assert [(d.name, d.kind) for d in cleaned["documents"]] == [
            ("회사소개.txt", "TEXT"),
            ("서비스.pdf", "PDF"),
        ]

    def test_a_pdf_that_is_not_a_pdf_is_refused(self):
        from app.modules.brand import validate_brand_body

        with pytest.raises(BlogTaskError, match="PDF 형식이 아닙니다"):
            validate_brand_body(
                {
                    "name": "A",
                    "documents": [
                        {
                            "section": "description",
                            "name": "x.pdf",
                            "kind": "PDF",
                            "value": "data:image/png;base64,AA",
                        }
                    ],
                }
            )

    def test_an_unknown_kind_is_refused(self):
        from app.modules.brand import validate_brand_body

        with pytest.raises(BlogTaskError, match="모르는 문서 종류"):
            validate_brand_body(
                {
                    "name": "A",
                    "documents": [
                        {"section": "description", "name": "x.docx", "kind": "DOCX", "value": "x"}
                    ],
                }
            )

    def test_documents_go_out_as_reference_materials_with_their_own_type(self):
        """파이프라인이 TEXT와 PDF를 다르게 읽는다. 종류를 뭉개면 PDF에서 글자를 못 뽑는다."""
        profile = BrandProfile(
            brand_id="b",
            user_id="u",
            name="AIONA",
            created_at="x",
            updated_at="x",
            documents=[
                BrandDocument(
                    section="description",
                    name="회사소개.txt",
                    kind="TEXT",
                    value="AI 교육 회사입니다.",
                ),
                BrandDocument(
                    section="features",
                    name="서비스.pdf",
                    kind="PDF",
                    value="data:application/pdf;base64,JVBERi0x",
                ),
            ],
        )

        materials = merge_brand_materials(profile, [])
        kinds = [(m["type"], m["name"]) for m in materials]

        # 이름에 어느 칸의 자료인지 붙는다 — 파일 이름만으로는 모델이 짐작해야 한다.
        assert ("TEXT", "브랜드 소개 자료 - 회사소개.txt") in kinds
        assert ("PDF", "핵심 기능·서비스 자료 - 서비스.pdf") in kinds

    def test_a_document_must_say_which_section_it_belongs_to(self):
        """어느 칸의 자료인지 없으면 모델에게 무엇이라고 알려 줄지 정할 수 없다."""
        from app.modules.brand import validate_brand_body

        with pytest.raises(BlogTaskError, match="모르는 자료 위치"):
            validate_brand_body(
                {
                    "name": "A",
                    "documents": [
                        {"section": "audience", "name": "x.txt", "kind": "TEXT", "value": "y"}
                    ],
                }
            )


class TestTheAttachmentBudget:
    """이미지와 PDF의 **합계**를 본다.

    낱개 상한만 두면 합이 요청 본문 상한(16MB)을 넘는다. 이미지 10장(10MB)에 PDF 4MB
    하나만 더해도 base64로 18MB가 되어 413으로 잘린다 — 화면은 다 받아 놓고 저장
    버튼에서 통째로 실패하는 셈이다.
    """

    def test_the_budget_fits_under_the_request_limit(self):
        from app.http.routes import MAX_JSON_BODY_BYTES
        from app.shared import BrandLimits

        assert BrandLimits.MAX_ATTACHMENT_TOTAL_BYTES * 4 // 3 < MAX_JSON_BODY_BYTES

    def test_images_and_pdfs_are_weighed_together(self):
        from app.modules.brand import validate_brand_body
        from app.shared import BrandLimits

        # 각각은 낱개 상한 **안에** 있다. 합쳐야만 예산을 넘는다.
        def data_url(prefix: str, megabytes: float) -> str:
            return prefix + "A" * int(megabytes * 1024 * 1024 * 4 / 3)

        image = data_url("data:image/png;base64,", 0.9)   # 상한 1MB 아래
        pdf = data_url("data:application/pdf;base64,", 3)  # 상한 4MB 아래

        with pytest.raises(BlogTaskError, match="합쳐") as raised:
            validate_brand_body(
                {
                    "name": "A",
                    "images": [{"dataUrl": image} for _ in range(BrandLimits.MAX_IMAGES)],
                    "documents": [
                        {"section": "description", "name": "a.pdf", "kind": "PDF", "value": pdf}
                    ],
                }
            )

        # 지금 몇 MB인지 알려 줘야 어디를 줄일지 정할 수 있다.
        assert "MB" in str(raised.value)


class TestTheListDoesNotCarryTheHeavyFields:
    """브랜드 고르기 화면이 왜 오래 멈춰 있었나.

    `GET /brands`는 브랜드 **전체**를 준다 — 이미지·문서의 base64까지. 실측으로 브랜드
    하나가 2.0MB였고(이미지 9장) 그중 2.0MB가 이미지였다. 그런데 목록 화면이 그리는
    것은 이름과 한 줄 소개뿐이다.
    """

    async def test_the_summary_drops_the_base64_but_keeps_the_counts(self):
        from app.shared import BrandListItem

        repository = InMemoryBrandRepository()
        await repository.upsert(
            BrandProfile(
                brand_id="brand_1",
                user_id="user_1",
                name="AIONA",
                description="AI 교육 회사입니다.",
                created_at="2026-08-06T00:00:00.000Z",
                updated_at="2026-08-06T00:00:00.000Z",
                links=[{"label": "홈", "url": "https://aiona.kr/"}],
                images=[
                    {"label": f"사진 {i}", "dataUrl": "data:image/png;base64," + "A" * 40_000}
                    for i in range(9)
                ],
                documents=[
                    BrandDocument(
                        section="description", name="소개.txt", kind="TEXT", value="본문"
                    )
                ],
            )
        )

        items = await BrandService(repository).list_brand_items("user_1")

        assert {type(item) for item in items} == {BrandListItem}
        # 목록에는 기본 브랜드도 함께 있다(2026-08-19). 심어 둔 것을 id로 집는다.
        wire = next(item for item in items if item.brand_id == "brand_1").to_wire()
        # 무거운 것은 아예 없다.
        assert "images" not in wire and "documents" not in wire
        assert "data:image" not in json.dumps(wire)
        # 무엇이 들어 있는 브랜드인지는 개수로 알 수 있어야 한다.
        assert (wire["imageCount"], wire["documentCount"], wire["linkCount"]) == (9, 1, 1)
        assert wire["name"] == "AIONA"

    async def test_the_summary_is_far_smaller_than_the_full_profile(self):
        """숫자로 못 박는다 — 나중에 무거운 필드가 목록에 다시 붙으면 여기서 걸린다."""
        repository = InMemoryBrandRepository()
        await repository.upsert(
            BrandProfile(
                brand_id="brand_1",
                user_id="user_1",
                name="AIONA",
                created_at="2026-08-06T00:00:00.000Z",
                updated_at="2026-08-06T00:00:00.000Z",
                images=[
                    {"label": f"사진 {i}", "dataUrl": "data:image/png;base64," + "A" * 100_000}
                    for i in range(9)
                ],
            )
        )
        service = BrandService(repository)

        # 목록에는 기본 브랜드(AIONA)도 함께 있다. 그 자료의 소개·기준표가 요약에 실리는
        # 것은 정상이므로, 재는 것은 **심어 둔 브랜드 한 건**이다 — 무거운 필드가 목록에
        # 다시 붙었는지를 보려는 것이지 목록 전체 크기를 보려는 것이 아니다.
        def one(items):
            return next(item for item in items if item.brand_id == "brand_1")

        full = len(json.dumps(one(await service.list_brands("user_1")).to_wire()))
        summary = len(json.dumps(one(await service.list_brand_items("user_1")).to_wire()))

        assert full > 800_000
        assert summary < 1_000


class TestABrandFullOfMaterialCanStillStartAPost:
    """자료를 다 채운 브랜드로도 글이 만들어지는지.

    증상: 브랜드 글쓰기 화면이 "지금은 가져올 트렌드 키워드가 없습니다"만 띄우고,
    토스트로 `referenceMaterials must have at most 10 items`가 떴다.

    원인: 트렌드 목록은 **글이 있어야** 부를 수 있어서 화면이 먼저 빈 글을 만드는데,
    그 글의 참고자료에 브랜드 자료가 펼쳐져 들어간다(소개 1 + 주소 + 문서 + 이미지).
    작성 화면용 상한 10개로 재니, 주소를 11개만 적어 둬도 글 생성이 422로 죽고
    트렌드 호출까지 가지 못했다. 즉 트렌드가 아니라 글 준비가 막힌 것이다.
    """

    def _loaded_profile(self) -> BrandProfile:
        from app.shared import BrandLimits

        return BrandProfile(
            brand_id="brand_1",
            user_id="user_1",
            name="AIONA",
            description="AI 교육 회사입니다.",
            created_at="2026-08-05T00:00:00.000Z",
            updated_at="2026-08-05T00:00:00.000Z",
            links=[
                {"label": f"주소 {i}", "url": f"https://aiona.kr/{i}"}
                for i in range(BrandLimits.MAX_LINKS)
            ],
            documents=[
                BrandDocument(
                    section="description", name=f"소개-{i}.txt", kind="TEXT", value=f"본문 {i}"
                )
                for i in range(BrandLimits.MAX_DOCUMENTS)
            ],
            images=[
                {"label": f"이미지 {i}", "dataUrl": "data:image/png;base64,AAAA"}
                for i in range(BrandLimits.MAX_IMAGES)
            ],
        )

    def test_a_full_brand_expands_past_the_write_screen_limit(self):
        """자료를 다 채우면 참고자료가 수십 개가 된다 — 이게 막히던 이유다.

        2026-08-11에 사용자 쪽 개수 제한이 사라져(용량만 본다) '화면 상한을 넘는다'는
        비교는 뜻을 잃었다. 지키려는 것은 그대로다: **브랜드 자료가 한 건도 잘리지
        않는다.**
        """
        materials = merge_brand_materials(self._loaded_profile(), [])

        assert len(materials) >= 30

    def test_the_brand_limit_covers_a_full_brand_plus_what_the_user_adds(self):
        from app.modules.blog_task.validation import MAX_REFERENCE_MATERIALS
        from app.modules.brand import BRAND_POST_MAX_REFERENCE_MATERIALS

        materials = merge_brand_materials(self._loaded_profile(), [])

        # 브랜드 자료를 다 채우고도, 입력 단계에서 직접 넣는 몫이 그대로 남아야 한다.
        assert len(materials) + MAX_REFERENCE_MATERIALS == BRAND_POST_MAX_REFERENCE_MATERIALS

    async def test_creating_a_post_with_a_brand_no_longer_fails_on_the_ten_item_limit(self):
        """라우트까지 통과하는지 본다 — 상한을 넘겨주는 걸 빠뜨리면 여기서 잡힌다.

        2026-08-11부터 브랜드 글도 `POST /posts`로 만든다(브랜드 전용 경로 없음).
        """
        from types import SimpleNamespace

        from httpx import ASGITransport, AsyncClient

        from app.main import create_app
        from app.modules.auth.repository import InMemoryUserRepository
        from app.modules.auth.service import AuthService
        from app.modules.blog_task.repository import InMemoryBlogTaskRepository
        from app.modules.blog_task.service import BlogTaskService

        auth_service = AuthService(InMemoryUserRepository())
        signed_up = await auth_service.sign_up(
            {"email": "brand@example.com", "password": "password123", "nickname": "작성자"}
        )

        brand_repository = InMemoryBrandRepository()
        profile = self._loaded_profile().model_copy(
            update={"user_id": signed_up.user.user_id}
        )
        await brand_repository.upsert(profile)

        app = create_app()
        app.state.services = SimpleNamespace(
            auth_service=auth_service,
            brand_service=BrandService(brand_repository),
            blog_task_service=BlogTaskService(InMemoryBlogTaskRepository(), None, None),
            # 저장 뒤 키워드 선행 수집이 붙어 있다. 이 테스트가 보는 것은 저장이라
            # 아무 일도 하지 않는 것으로 세워 둔다.
            trend_service=SimpleNamespace(start_keyword_prefetch=lambda _task: None),
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/posts",
                json={
                    "topic": "AIONA 신규 기능",
                    "purpose": ["제품·서비스 홍보"],
                    "subjectCategory": "브랜드·기업",
                    "brandId": profile.brand_id,
                    "referenceMaterials": [{"type": "TEXT", "value": "내가 적은 메모"}],
                },
                headers={"authorization": f"Bearer {signed_up.access_token}"},
            )

        assert response.status_code == 201, response.text
        saved = response.json()["data"]["input"]
        materials = saved["referenceMaterials"]
        # 브랜드 자료가 하나도 잘리지 않고 다 실리고, 사용자 메모도 함께 남는다.
        assert len(materials) == len(merge_brand_materials(profile, [])) + 1
        assert sum(1 for m in materials if m["type"] == "URL") == 20
        assert materials[-1]["value"] == "내가 적은 메모"
        # 소재는 브랜드가 아니라 사용자가 정한다.
        assert saved["topic"] == "AIONA 신규 기능"
        assert saved["subjectCategory"] == "브랜드·기업"
