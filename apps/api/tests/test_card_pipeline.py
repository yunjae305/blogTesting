"""저장 호환 카드 계획을 쓰는 자연 사진 파이프라인(M5 v2.1).

스펙의 핵심 규격을 코드 수준에서 고정한다:
- 이미지 수는 사진 계획(필요성 채점)이 정하며 썸네일만 있는 글도 정상
- 썸네일 정확히 1장, 80점 미만·원고에 없는 주장(articleClaim)·중복 장면은 제외
- 본문은 텍스트 없는 와이드 사진, 썸네일 문구만 코드가 국소 합성
"""

import base64
import io

import pytest
from PIL import Image

from app.llm.contracts import PostImageGenerationInput
from app.llm.parsing import card_plan_from_json
from app.llm.prompts import card_scene_prompt, image_prompt
from app.modules.blog_task.repository import InMemoryBlogTaskRepository
from app.modules.draft.card_selection import (
    MAX_TOTAL_IMAGES,
    assign_numbers,
    claim_in_article,
    select_cards,
)
from app.modules.draft.service import DraftService
from app.modules.draft.reference_evidence import build_profile
from app.shared.image_bytes import normalize_data_url
from app.shared import (
    BlogTaskInput,
    CardBrief,
    CardDesignSystem,
    CardScene,
    DraftGenerationResult,
    FinalPost,
    GeneratedPostImage,
    PlannedVisual,
    ReferenceMaterial,
    ReferenceMaterialType,
    VisualCardPlan,
    VisualDataPoint,
    WebPhoto,
)

from test_draft_service import NOW, build_task

# 두 개의 소제목 섹션과 카드가 인용할 실제 문장을 담은, 품질 검사를 통과하는 본문.
CLAIM_1 = "AIONA는 여러 AI 모델을 한 화면에서 전환하며 쓸 수 있습니다"
CLAIM_2 = "요금제는 사용량 기준이라 가벼운 사용자는 무료 범위로 충분합니다"
_FILLER = "\n\n".join(
    f"{n}번 문단입니다. 항목{n} 준비과정{n} 사례{n} 판단기준{n} 연결설명{n}을 다룹니다. "
    f"독자{n}는 단계{n} 확인사항{n} 선택지{n} 이해흐름{n} 실행메모{n}를 얻습니다."
    for n in range(1, 31)
)
BODY = f"{CLAIM_1}. {CLAIM_2}. {_FILLER}"
MARKDOWN = (
    f"# 카드 테스트 글\n\n도입 문단입니다. {CLAIM_1}.\n\n"
    f"## 첫 번째 소제목\n\n첫 섹션 문단입니다.\n\n"
    f"## 두 번째 소제목\n\n{CLAIM_2}. 둘째 섹션 문단입니다.\n\n{_FILLER}"
)
HTML = (
    "<article><h1>카드 테스트 글</h1><p>도입</p>"
    f"<h2>첫 번째 소제목</h2><p>첫 섹션 문단입니다.</p>"
    f"<h2>두 번째 소제목</h2><p>{CLAIM_2}.</p><p>{_FILLER}</p></article>"
)

DRAFT = DraftGenerationResult(
    prompt_version="m4-draft@v1.1",
    provider="stub",
    model="stub",
    generated_at=NOW,
    final_post=FinalPost(
        title="카드 테스트 글",
        body=BODY,
        hashtags=["a"] * 5,
        html_content=HTML,
        markdown_content=MARKDOWN,
        thumbnail_copy=["원고 대표", "문구"],
    ),
)


def brief(
    card_id: str,
    card_type: str = "SECTION_CARD",
    section_id: str | None = "section-1",
    score: float = 90.0,
    claim: str = CLAIM_1,
    subject: str | None = None,
    setting: str | None = None,
    image_source: str = "",
    photo_role: str = "IN_USE_SCENE",
    framing: str = "MEDIUM",
    visual_subject: str = "",
) -> CardBrief:
    return CardBrief(
        card_id=card_id,
        card_type=card_type,
        section_id=section_id,
        article_claim=claim,
        visual_purpose="독자가 실제 사용 장면을 그릴 수 있게",
        eyebrow="한눈에 보는 핵심",
        headline_lines=["여러 AI를", "한 화면에서"],
        emphasis_words=["한 화면"],
        summary_lines=["구독 전 무료 범위부터 확인하세요"],
        icon_type="check",
        scene=CardScene(
            main_subject=subject or f"a laptop scene for {card_id}",
            action="switching between AI models",
            setting=setting or f"a Korean home office desk {card_id}",
        ),
        alt_text=f"{card_id} 카드",
        necessity_score=score,
        image_source=image_source,
        photo_role=photo_role,
        framing=framing,
        visual_subject=visual_subject,
    )


def plan(*cards: CardBrief) -> VisualCardPlan:
    return VisualCardPlan(
        design_system=CardDesignSystem(
            primary_color="#1F2A44", accent_color="#FFC845"
        ),
        cards=list(cards),
    )


def jpeg_data_url(color=(120, 130, 140), size=(64, 64)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class CardPlanningGenerator:
    """카드 계획까지 지원하는 M4 스텁."""

    def __init__(self, card_plan: VisualCardPlan | None, result: DraftGenerationResult = DRAFT):
        self._plan = card_plan
        self.result = result
        self.captured: list = []
        self.plan_args: list = []

    async def generate_draft(self, draft_input):
        self.captured.append(draft_input)
        return self.result

    async def generate_reference_evidence(self, draft_input):
        profile = build_profile(draft_input.input.reference_materials)
        return profile.model_copy(
            update={
                "reference_image_roles": [
                    role.model_copy(update={"privacy_scanned": True})
                    for role in profile.reference_image_roles
                ]
            }
        )

    async def generate_visual_card_plan(
        self, draft_input, final_post, rendered_visual_count, reference_image_count
    ):
        self.plan_args.append((rendered_visual_count, reference_image_count))
        return self._plan


class SceneImageGenerator:
    """실제로 디코딩되는 JPEG 장면을 돌려주는 M5 스텁. fail_ids의 카드는 항상 실패한다."""

    def __init__(self, fail_ids: set[str] | None = None):
        self.calls: list[PostImageGenerationInput] = []
        self.fail_ids = fail_ids or set()

    async def generate_post_image(self, image_input):
        self.calls.append(image_input)
        if image_input.card is not None and image_input.card.card_id in self.fail_ids:
            raise RuntimeError("image model unavailable")
        # 카드마다 배경 장면이 다르다(실제로는 프롬프트가 달라 이미지가 달라진다). 호출
        # 순번으로 배경색을 달리해 이를 흉내 낸다 — 순번 배지를 없앤 뒤에도 서로 다른 카드가
        # 바이트까지 같아져 dedupe로 합쳐지지 않게 한다.
        tone = 90 + (len(self.calls) * 30) % 150
        return GeneratedPostImage(
            data_url=jpeg_data_url(color=(tone, 130, 140)),
            alt_text=image_input.card.alt_text if image_input.card else "alt",
            prompt="p",
            provider="openai",
            model="gpt-image-2",
            generated_at=NOW,
            mime_type="image/jpeg",
            source="generated",
        )


def build_card_service(
    generator, images, photo_search=None, youtube_search=None, final_reviewer=None
):
    repository = InMemoryBlogTaskRepository()
    service = DraftService(
        repository=repository,
        draft_generator=generator,
        post_image_generator=images,
        photo_search=photo_search,
        youtube_photo_search=youtube_search,
        final_reviewer=final_reviewer,
    )
    return service, repository


class TestClaimInArticle:
    def test_exact_and_normalized_matches_pass(self):
        assert claim_in_article(CLAIM_1, BODY)
        assert claim_in_article("AIONA는  여러 AI 모델을 한 화면에서 전환하며 쓸 수 있습니다!", BODY)

    def test_a_fabricated_claim_fails(self):
        assert not claim_in_article("AIONA는 국내 점유율 1위를 기록했습니다", BODY)

    def test_a_lightly_reworded_claim_passes_by_word_overlap(self):
        assert claim_in_article("여러 AI 모델을 한 화면에서 전환하며 쓸 수 있다", BODY)


class TestCardSelection:
    def test_no_thumbnail_invalidates_the_plan(self):
        assert select_cards(plan(brief("card-1")), BODY, [], 0) is None

    def test_below_80_and_fabricated_claims_are_dropped(self):
        selected = select_cards(
            plan(
                brief("cover", card_type="THUMBNAIL", section_id=None),
                brief("card-ok", section_id="section-1", score=88),
                brief("card-low", section_id="section-2", score=79),
                brief(
                    "card-fake",
                    section_id="section-3",
                    score=95,
                    claim="원고에 없는 수상 경력이 있습니다",
                ),
            ),
            BODY,
            [],
            0,
        )
        assert [card.card_id for card in selected.body_cards] == ["card-ok"]

    def test_duplicate_scenes_and_sections_keep_only_the_best(self):
        selected = select_cards(
            plan(
                brief("cover", card_type="THUMBNAIL", section_id=None),
                brief("card-1", section_id="section-1", score=90, subject="same desk", setting="same room"),
                brief("card-2", section_id="section-2", score=85, subject="same desk", setting="same room"),
                brief("card-3", section_id="section-2", score=80, subject="other scene"),
                brief("card-4", section_id="section-2", score=78, subject="third scene"),
            ),
            BODY,
            [],
            0,
        )
        # card-2는 card-1과 같은 장면이라 빠지고, 그 자리는 다른 장면의 card-3이 차지한다.
        # card-4는 card-3과 같은 섹션 — 점수 높은 card-3만 남는다.
        assert [card.card_id for card in selected.body_cards] == ["card-1", "card-3"]

    def test_total_budget_caps_at_six_dropping_the_lowest_scores(self):
        briefs = [brief("cover", card_type="THUMBNAIL", section_id=None)] + [
            brief(f"card-{n}", section_id=f"section-{n}", score=95 - n, subject=f"scene {n}")
            for n in range(1, 8)
        ]
        selected = select_cards(plan(*briefs), BODY, [], 0)
        assert selected.total == MAX_TOTAL_IMAGES
        # 점수 내림차순 상위 5장만 본문 카드로 남는다.
        assert [card.card_id for card in selected.body_cards] == [
            "card-1",
            "card-2",
            "card-3",
            "card-4",
            "card-5",
        ]

    def test_article_length_caps_the_total_image_count(self):
        """이미지 총량은 호출자가 준 max_total이 자른다 — 썸네일이 먼저 자리를 차지한다."""
        briefs = [brief("cover", card_type="THUMBNAIL", section_id=None)] + [
            brief(f"card-{n}", section_id=f"section-{n}", score=95 - n, subject=f"scene {n}")
            for n in range(1, 5)
        ]

        medium = select_cards(plan(*briefs), BODY, [], 0, max_total=3)
        assert medium.total == 3
        assert [card.card_id for card in medium.body_cards] == ["card-1", "card-2"]

        short = select_cards(plan(*briefs), BODY, [], 0, max_total=1)
        assert short.total == 1
        assert short.body_cards == []
        assert short.visuals == []
        assert short.reference_count == 0

    def test_the_length_image_ranges_are_what_the_user_decided(self):
        """2026-08-07 사용자 결정: 짧게 2장·중간 3장 **고정**(썸네일 포함).

        2026-08-03의 범위(짧게 2~3·중간 3~5)에서 다시 고정으로 — 이미지 생성이 원고
        단계에서 가장 긴 축이고 장수에 비례해서, 5분 목표에 맞춰 줄였다.
        옛 설정의 long과 미설정은 중간으로 폴백한다."""
        from app.llm.prompts import length_total_image_cap, length_total_image_range

        assert length_total_image_range("short") == (2, 2)
        assert length_total_image_range("medium") == (3, 3)
        assert length_total_image_range("long") == (3, 3)
        assert length_total_image_range(None) == (3, 3)
        # cap은 범위의 최댓값이다 — 선정 단계가 자르는 기준. 고정이라 최소와 같다.
        assert length_total_image_cap("short") == 2
        assert length_total_image_cap("medium") == 3

    def test_resolve_visual_budget_caps_photos_but_not_rendered_visuals(self):
        """2026-08-03 사용자 결정: 사진 총량은 길이 규격이 정하지만, 표·그래프는 그
        규격과 무관하게 AI 판단(근거 원칙)만 따른다."""
        from app.modules.draft.visual_policy import DEFAULT_POLICY, resolve_visual_budget

        medium = resolve_visual_budget(None, policy=DEFAULT_POLICY, total_image_cap=3)
        assert medium.thumbnail == 1
        assert medium.body_photos_max <= 2
        assert medium.reference_images_max <= 2
        assert medium.rendered_visuals_max == DEFAULT_POLICY.rendered_max

        short = resolve_visual_budget(None, policy=DEFAULT_POLICY, total_image_cap=1)
        # 짧게도 사진만 0이고, 표·그래프는 근거가 있으면 실릴 수 있다.
        assert (short.body_photos_max, short.reference_images_max) == (0, 0)
        assert short.rendered_visuals_max == DEFAULT_POLICY.rendered_max

    def test_rendered_visuals_do_not_consume_the_photo_budget(self):
        """표·그래프는 사진 예산 밖이다 — 사진 규격(첨부 포함)과 별개로 실린다."""
        visual = PlannedVisual(
            visual_id="visual-1",
            type="BAR_CHART",
            title="비교",
            section_id="section-2",
            data=[VisualDataPoint(label="A", value=1), VisualDataPoint(label="B", value=2)],
            source="기관",
        )
        briefs = [brief("cover", card_type="THUMBNAIL", section_id=None)] + [
            brief(f"card-{n}", section_id=f"section-{n + 2}", score=95 - n, subject=f"s{n}")
            for n in range(1, 6)
        ]
        selected = select_cards(plan(*briefs), BODY, [visual], 2)
        # 사진 예산 5 = 첨부 2 + 사진 카드 3. 표·그래프 1은 예산 밖에서 함께 실린다.
        assert len(selected.visuals) == 1
        assert selected.reference_count == 2
        assert len(selected.body_cards) == 3
        assert selected.total == MAX_TOTAL_IMAGES + 1

    def test_a_photo_card_yields_to_a_chart_on_the_same_section(self):
        visual = PlannedVisual(
            visual_id="visual-1",
            type="BAR_CHART",
            title="비교",
            section_id="section-1",
            data=[VisualDataPoint(label="A", value=1), VisualDataPoint(label="B", value=2)],
            source="기관",
        )
        selected = select_cards(
            plan(
                brief("cover", card_type="THUMBNAIL", section_id=None),
                brief("card-1", section_id="section-1", score=99),
            ),
            BODY,
            [visual],
            0,
        )
        assert selected.body_cards == []
        assert [v.visual_id for v in selected.visuals] == ["visual-1"]

    def test_numbers_follow_placement_order_and_renumber_after_a_drop(self):
        visual = PlannedVisual(
            visual_id="visual-1",
            type="BAR_CHART",
            title="비교",
            section_id="section-2",
            data=[VisualDataPoint(label="A", value=1), VisualDataPoint(label="B", value=2)],
            source="기관",
        )
        selected = select_cards(
            plan(
                brief("cover", card_type="THUMBNAIL", section_id=None),
                brief("card-1", section_id="section-1", score=90, subject="s1"),
                brief("card-3", section_id="section-3", score=85, subject="s3"),
            ),
            BODY,
            [visual],
            1,
        )
        # 썸네일 1 → 첨부 2 → 섹션 순서: card-1(3) → visual-1(4) → card-3(5).
        assert selected.card_numbers["cover"] == 1
        assert selected.reference_numbers == [2]
        assert selected.card_numbers["card-1"] == 3
        assert selected.visual_numbers["visual-1"] == 4
        assert selected.card_numbers["card-3"] == 5
        assert selected.total == 5

        # card-1이 생성 실패로 빠지면 건너뜀 없이 다시 매긴다.
        selected.body_cards = [c for c in selected.body_cards if c.card_id != "card-1"]
        assign_numbers(selected)
        assert selected.visual_numbers["visual-1"] == 3
        assert selected.card_numbers["card-3"] == 4
        assert selected.total == 4


class TestCardScenePrompt:
    def test_body_prompt_is_a_natural_wide_photo_without_card_chrome(self):
        task = build_task()
        image_input = PostImageGenerationInput(
            post_id="post_1",
            user_id="user_1",
            input=task.input,
            selected_intent={
                "intentId": "i1",
                "title": "T",
                "targetReader": "실무자",
                "rationale": "R",
            },
            final_post=DRAFT.final_post,
            prompt_version="m5-image@v2.0",
            image_index=1,
            total_images=4,
            card=brief("card-1"),
            design=CardDesignSystem(),
        )
        prompt = image_prompt(image_input)
        assert "natural editorial photograph" in prompt
        assert "Body-photo composition" in prompt
        assert "1200x688" in prompt
        assert "1536x1024 fallback" in prompt
        assert "900x506" in prompt
        assert "central 16:9 safe area" in prompt
        assert "do not reserve space for text" in prompt
        assert "No card-news layout" in prompt
        assert "square editorial card" not in prompt
        assert DRAFT.final_post.title not in prompt

    def test_thumbnail_card_prompt_keeps_the_banner_safe_area_rules(self):
        task = build_task()
        image_input = PostImageGenerationInput(
            post_id="post_1",
            user_id="user_1",
            input=task.input,
            selected_intent={
                "intentId": "i1",
                "title": "T",
                "targetReader": "실무자",
                "rationale": "R",
            },
            final_post=DRAFT.final_post,
            prompt_version="m5-image@v2.0",
            image_index=0,
            total_images=4,
            is_thumbnail=True,
            card=brief("cover", card_type="THUMBNAIL", section_id=None),
            design=CardDesignSystem(),
        )
        prompt = card_scene_prompt(image_input)
        # 2026-08-03 네이버 권장 규격: 대표 썸네일은 1:1 정사각(720×720)이다.
        assert "720x720 square" in prompt
        assert "without edge cropping" in prompt
        assert "left and right thirds" not in prompt
        assert "subject in the planned zone" in prompt
        # 피사체 자리를 계획이 지정하고, 문구는 그 반대편에 얹힌다.
        assert "centre of the frame" in prompt
        assert "low-detail band" not in prompt
        assert "do not draw a panel, banner" in prompt or "Do not reserve blank space" in prompt


class TestCardPipelineEndToEnd:
    async def test_photos_are_planned_generated_and_placed(self):
        generator = CardPlanningGenerator(
            plan(
                brief("cover", card_type="THUMBNAIL", section_id=None),
                brief("card-1", section_id="section-1", score=90, subject="scene one"),
                brief("card-2", section_id="section-2", score=85, subject="scene two", claim=CLAIM_2),
            )
        )
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        post = updated.final_post
        # 썸네일 1 + 필요성이 충분한 본문 사진 2.
        assert len(post.images) == 3
        assert post.featured_image.data_url == post.images[0].data_url
        # 카드 계획이 결과에 저장돼 어떤 근거로 만들었는지 추적된다.
        assert updated.draft_generation_result.card_plan is not None
        # 모든 이미지 호출에 카드 브리프·디자인 시스템이 실렸다.
        assert all(call.card is not None and call.design is not None for call in images.calls)
        assert [call.is_thumbnail for call in images.calls].count(True) == 1
        # 카드는 자기 섹션(소제목) 아래에 배치된다.
        first_heading = post.markdown_content.index("## 첫 번째 소제목")
        second_heading = post.markdown_content.index("## 두 번째 소제목")
        first_image = post.markdown_content.index("![", first_heading)
        assert first_heading < first_image < second_heading
        # 카드뉴스 headline이 아니라 원고가 정한 표지 문구가 남는다.
        assert post.thumbnail_copy == ["원고 대표", "문구"]

    async def test_a_failing_body_photo_is_dropped_without_duplicate_outer_retries(self):
        generator = CardPlanningGenerator(
            plan(
                brief("cover", card_type="THUMBNAIL", section_id=None),
                brief("card-1", section_id="section-1", score=90, subject="scene one"),
                brief("card-2", section_id="section-2", score=85, subject="scene two", claim=CLAIM_2),
            )
        )
        images = SceneImageGenerator(fail_ids={"card-2"})
        service, repository = build_card_service(generator, images)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        # provider 계층이 재시도를 맡으므로 서비스는 고비용 이미지 요청을 한 번만 보낸다.
        card2_calls = [c for c in images.calls if c.card and c.card.card_id == "card-2"]
        assert len(card2_calls) == 1
        # 실패한 자리는 이제 비워 두지 않는다 — 대상 없는 분위기 사진이 계획 장수를
        # 지킨다(2026-08-10 사용자: "총 3장이라 해놨으면서 이미지를 하나만 만들었어").
        assert len(updated.final_post.images) == 3
        filler_calls = [
            call for call in images.calls if call.card is None and not call.is_thumbnail
        ]
        assert len(filler_calls) == 1
        assert filler_calls[0].suppress_topic_anchor is True

    async def test_a_plan_without_thumbnail_falls_back_to_thumbnail_only(self):
        generator = CardPlanningGenerator(plan(brief("card-1", section_id="section-1")))
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        # 계획 실패를 관련 없는 본문 사진으로 채우지 않는다.
        assert len(images.calls) == 1
        assert all(call.card is None for call in images.calls)

    async def test_a_thumbnail_only_plan_is_valid(self):
        generator = CardPlanningGenerator(
            plan(brief("cover", card_type="THUMBNAIL", section_id=None))
        )
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert len(images.calls) == 1
        assert images.calls[0].card.card_id == "cover"
        assert len(updated.final_post.images) == 1

    async def test_failed_planned_thumbnail_gets_one_generic_thumbnail_fallback(self):
        generator = CardPlanningGenerator(
            plan(brief("cover", card_type="THUMBNAIL", section_id=None))
        )
        images = SceneImageGenerator(fail_ids={"cover"})
        service, repository = build_card_service(generator, images)
        await repository.create(build_task())

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert len(images.calls) == 2
        assert images.calls[0].card.card_id == "cover"
        assert images.calls[1].card is None
        assert images.calls[1].is_thumbnail
        assert len(updated.final_post.images) == 1


# --- 핵심 시각 대상(고유 캐릭터·실제 인물)이 이미지까지 살아남는가 ---
#
# 스파이더맨 글에서 거미줄·도시 야경만, 손흥민 글에서 이름 없는 축구선수만 나오던 문제를
# 막는 규격이다. 계획(subjectKind·mustShowSubject·subjectIdentity) → 파싱 → 서비스 →
# 최종 이미지 프롬프트까지 정체성이 끊기는 지점이 없어야 한다.


def named_brief(
    card_id: str,
    *,
    kind: str,
    identity: str | None,
    subject: str,
    card_type: str = "THUMBNAIL",
    section_id: str | None = None,
    must_show: bool = True,
    claim: str = CLAIM_1,
) -> CardBrief:
    base = brief(
        card_id, card_type=card_type, section_id=section_id, claim=claim, subject=subject
    )
    # model_copy는 검증기를 돌리지 않는다. 실제 경로(파싱·저장)와 같은 검증을 지나게
    # model_validate로 다시 세운다 — 규칙을 우회한 카드로 테스트하면 규칙을 시험하지 못한다.
    return CardBrief.model_validate(
        {
            **base.model_dump(),
            "subject_kind": kind,
            "subject_identity": identity,
            "must_show_subject": must_show,
        }
    )


def image_input_for(
    card: CardBrief, *, topic: str, is_thumbnail: bool = True, **overrides
) -> PostImageGenerationInput:
    """서비스가 카드로 만드는 것과 같은 모양의 이미지 입력(정체성 필드 포함)."""
    defaults = dict(
        post_id="post_1",
        user_id="user_1",
        input=BlogTaskInput(topic=topic, keywords=[topic]),
        selected_intent={
            "intentId": "i1",
            "title": "T",
            "targetReader": "독자",
            "rationale": "R",
        },
        final_post=DRAFT.final_post,
        prompt_version="m5-image@v3.2",
        image_index=0 if is_thumbnail else 1,
        total_images=2,
        is_thumbnail=is_thumbnail,
        card=card,
        design=CardDesignSystem(),
        subject_identity=card.subject_identity,
        subject_kind=card.subject_kind,
        must_show_subject=card.must_show_subject,
    )
    return PostImageGenerationInput(**{**defaults, **overrides})


def plan_json(**card_overrides) -> dict:
    """모델이 돌려주는 모양의 카드 계획 JSON 한 장."""
    card = {
        "cardId": "cover",
        "cardType": "THUMBNAIL",
        "sectionId": None,
        "sectionHeading": None,
        "articleClaim": CLAIM_1,
        "visualPurpose": "독자가 이 글의 대상을 한눈에 알아보게",
        "scene": {"mainSubject": "a subject", "action": "standing", "setting": "a street"},
        "altText": "대체 텍스트",
        "necessityScore": 100,
        "usesReferenceImage": False,
        "referenceId": None,
        "photoRole": "PRODUCT_HERO",
        "subjectIdentity": None,
        "generatedOrReused": "GENERATED",
    }
    card.update(card_overrides)
    return {"cards": [card]}


class TestNamedSubjectPlanParsing:
    def test_a_character_card_is_normalized_to_must_show_even_if_the_model_says_no(self):
        """모델이 mustShowSubject=false로 내려도 코드가 True로 못 박는다."""
        parsed = card_plan_from_json(
            plan_json(
                subjectKind="FICTIONAL_CHARACTER",
                mustShowSubject=False,
                subjectIdentity="Spider-Man 스파이더맨",
                scene={"mainSubject": "Spider-Man perched on a rooftop railing"},
            )
        )

        card = parsed.cards[0]
        assert card.subject_kind == "FICTIONAL_CHARACTER"
        assert card.must_show_subject is True
        assert "Spider-Man" in card.subject_identity

    def test_a_real_person_card_keeps_the_name(self):
        parsed = card_plan_from_json(
            plan_json(
                subjectKind="REAL_NAMED_PERSON",
                mustShowSubject=True,
                subjectIdentity="Son Heung-min 손흥민",
                scene={"mainSubject": "Son Heung-min during a training session"},
            )
        )

        card = parsed.cards[0]
        assert card.subject_kind == "REAL_NAMED_PERSON"
        assert card.must_show_subject is True

    def test_an_old_card_without_the_new_fields_still_reads(self):
        """이미 저장된 카드 계획에는 두 필드가 없다 — 예전과 같은 값으로 읽혀야 한다."""
        parsed = card_plan_from_json(plan_json())

        card = parsed.cards[0]
        assert card.subject_kind == "NON_PERSON"
        assert card.must_show_subject is False

    def test_a_generic_role_card_may_leave_the_identity_empty(self):
        parsed = card_plan_from_json(
            plan_json(
                subjectKind="GENERIC_PERSON_ROLE",
                mustShowSubject=False,
                subjectIdentity=None,
                scene={"mainSubject": "a personal trainer correcting a squat"},
            )
        )

        card = parsed.cards[0]
        assert card.subject_kind == "GENERIC_PERSON_ROLE"
        assert card.must_show_subject is False
        assert card.subject_identity is None


class TestNamedSubjectImagePrompt:
    def test_spider_man_is_the_main_subject_and_cannot_be_replaced(self):
        card = named_brief(
            "cover",
            kind="FICTIONAL_CHARACTER",
            identity="Spider-Man",
            subject="Spider-Man perched on a rooftop railing",
        )

        prompt = card_scene_prompt(image_input_for(card, topic="스파이더맨"))

        assert "The primary named subject is exactly: Spider-Man" in prompt
        assert "dominant in the frame" in prompt
        # 주변 소재로 대체하지 못하게 하는 금지 목록.
        for banned in (
            "generic superhero",
            "costume-inspired anonymous model",
            "cosplayer",
            "symbol-only composition",
            "cityscape",
            "comic book",
            "poster",
        ):
            assert banned in prompt
        # 소재 자체도 함께 전달돼, 계획 장면이 미끄러져도 되돌아갈 곳이 있다.
        assert "스파이더맨" in prompt
        # 배우·작품 버전을 임의로 고정하지 않는다.
        assert "widely recognised look" in prompt

    def test_batman_keeps_the_costume_emblem_but_never_poster_copy(self):
        card = named_brief(
            "cover",
            kind="FICTIONAL_CHARACTER",
            identity="Batman",
            subject="Batman standing on a rain-wet rooftop ledge",
        )

        prompt = card_scene_prompt(image_input_for(card, topic="배트맨"))

        assert "The primary named subject is exactly: Batman" in prompt
        assert "generic superhero" in prompt
        assert "silhouette or back view" in prompt
        # 캐릭터 식별에 필요한 비문자형 문양은 지우지 않는다(가슴 문양·마스크 문양).
        assert "chest emblem" in prompt
        assert "mask pattern" in prompt
        # 그 예외가 문자·포스터·워터마크 허용으로 번지지 않는다.
        assert "No readable text" in prompt
        assert "no readable work titles" in prompt
        assert "no poster or cover copy" in prompt
        assert "no watermarks" in prompt
        assert "no studio or publisher logos" in prompt

    def test_a_real_person_is_shown_as_themselves_without_invented_events(self):
        card = named_brief(
            "cover",
            kind="REAL_NAMED_PERSON",
            identity="Son Heung-min 손흥민",
            subject="Son Heung-min in a training session",
        )

        prompt = card_scene_prompt(image_input_for(card, topic="손흥민"))

        assert "PRIMARY IDENTITY REQUIREMENT" in prompt
        assert "The named real person is exactly: Son Heung-min 손흥민" in prompt
        assert "Show the actual named person, Son Heung-min 손흥민" in prompt
        for banned in (
            "anonymous model",
            "look-alike",
            "generic person with the same occupation",
        ):
            assert banned in prompt
        # 원고에 없는 우승·트로피·특정 경기를 만들지 않는다.
        assert "awards, trophies, matches" in prompt
        assert "use a neutral editorial portrait" in prompt
        # 사람에게 제품용 문구("exact object")를 쓰지 않는다.
        assert "same colour, same silhouette" not in prompt
        # 캐릭터 복장 문양 예외는 실제 인물에게 열리지 않는다.
        assert "chest emblem" not in prompt

    def test_a_generic_role_gets_no_named_person_directive(self):
        card = named_brief(
            "cover",
            kind="GENERIC_PERSON_ROLE",
            identity=None,
            subject="a personal trainer correcting a squat in a real gym",
            must_show=False,
        )

        prompt = card_scene_prompt(image_input_for(card, topic="헬스 트레이너"))

        assert "The primary named subject" not in prompt
        assert "a personal trainer correcting a squat in a real gym" in prompt
        # 기존 자연 인물 생성 규칙과 문자 금지 규칙이 그대로다.
        assert "People are allowed when the scene genuinely calls for them" in prompt
        assert "No readable text, letters, numbers, logos" in prompt

    def test_a_product_subject_keeps_the_existing_fidelity_wording(self):
        card = named_brief(
            "cover",
            kind="NON_PERSON",
            identity="LG 그램 16 화이트",
            subject="a thin white laptop open on a desk",
            must_show=False,
        )

        prompt = card_scene_prompt(
            image_input_for(
                card,
                topic="노트북 구매 가이드",
                fidelity_requirements=["화이트 알루미늄 바디", "16인치 화면"],
            )
        )

        assert "The subject is specifically: LG 그램 16 화이트" in prompt
        assert "same type, same colour, same silhouette" in prompt
        assert "Preserve these confirmed details exactly" in prompt
        assert "화이트 알루미늄 바디" in prompt
        # 사람을 억지로 주요 피사체로 만들지 않는다.
        assert "The primary named subject" not in prompt
        assert "No readable text, letters, numbers, logos" in prompt


class TestNamedSubjectWithReferenceImages:
    def test_reference_fields_survive_alongside_the_named_subject(self):
        """참고 이미지 흐름(referenceId·usesReferenceImage·REUSED·충실도)이 그대로다."""
        parsed = card_plan_from_json(
            plan_json(
                subjectKind="REAL_NAMED_PERSON",
                mustShowSubject=True,
                subjectIdentity="Son Heung-min 손흥민",
                scene={"mainSubject": "Son Heung-min on the pitch"},
                usesReferenceImage=True,
                referenceId="reference-image-2",
                generatedOrReused="REUSED",
                productFidelityRequirements=["흰색 원정 유니폼"],
            )
        )

        card = parsed.cards[0]
        assert card.uses_reference is True
        assert card.reference_id == "reference-image-2"
        assert card.generated_or_reused == "REUSED"
        assert card.product_fidelity_requirements == ["흰색 원정 유니폼"]
        assert card.subject_identity == "Son Heung-min 손흥민"
        assert card.subject_kind == "REAL_NAMED_PERSON"

    def test_the_name_survives_without_any_reference_image(self):
        parsed = card_plan_from_json(
            plan_json(
                subjectKind="FICTIONAL_CHARACTER",
                mustShowSubject=True,
                subjectIdentity="Iron Man",
                scene={"mainSubject": "Iron Man landing in a hangar"},
                usesReferenceImage=False,
                referenceId=None,
            )
        )

        card = parsed.cards[0]
        assert card.uses_reference is False
        assert card.reference_id is None
        prompt = card_scene_prompt(image_input_for(card, topic="아이언맨"))
        assert "The primary named subject is exactly: Iron Man" in prompt


class TestNamedSubjectCardValidation:
    def test_a_thumbnail_that_only_shows_the_surroundings_invalidates_the_plan(self):
        cover = named_brief(
            "cover",
            kind="FICTIONAL_CHARACTER",
            identity="Spider-Man",
            subject="a city skyline at night with webs on a wall",
        )
        notes: list[str] = []

        assert select_cards(plan(cover), BODY, [], 0, notes) is None
        assert any("주변 장면만" in note for note in notes)

    def test_a_real_person_card_cannot_exist_without_a_name(self):
        """이름 없는 실존 인물 카드는 만들어질 수 없다 — 그 카드로 그릴 수 있는 그림은
        '그 직업의 아무나'뿐이라 모델 단계에서 거부한다."""
        with pytest.raises(ValueError):
            named_brief(
                "cover",
                kind="REAL_NAMED_PERSON",
                identity=None,
                subject="a football player",
            )

    def test_a_character_thumbnail_without_an_identity_invalidates_the_plan(self):
        cover = named_brief(
            "cover", kind="FICTIONAL_CHARACTER", identity=None, subject="a masked hero"
        )
        notes: list[str] = []

        assert select_cards(plan(cover), BODY, [], 0, notes) is None
        assert any("subjectIdentity가 비어 있음" in note for note in notes)

    def test_a_body_card_without_the_name_is_dropped_and_the_plan_survives(self):
        cover = named_brief(
            "cover",
            kind="FICTIONAL_CHARACTER",
            identity="Spider-Man",
            subject="Spider-Man crouched on a rooftop",
        )
        vague = named_brief(
            "card-1",
            kind="FICTIONAL_CHARACTER",
            identity="Spider-Man",
            subject="a masked person in a red suit",
            card_type="SECTION_CARD",
            section_id="section-1",
        )
        notes: list[str] = []

        selected = select_cards(plan(cover, vague), BODY, [], 0, notes)

        assert selected is not None
        assert selected.body_cards == []
        # 이름이 사라지고 사람의 '종류'만 남은 계획이라는 사유가 남는다.
        assert any("card-1" in note and "일반 인물 표현만" in note for note in notes)

    def test_a_named_card_that_actually_shows_the_subject_passes(self):
        cover = named_brief(
            "cover",
            kind="FICTIONAL_CHARACTER",
            identity="Spider-Man",
            subject="Spider-Man crouched on a rooftop",
        )
        good = named_brief(
            "card-1",
            kind="FICTIONAL_CHARACTER",
            identity="Spider-Man",
            subject="a close view of the Spider-Man suit on the shoulder",
            card_type="SECTION_CARD",
            section_id="section-1",
        )

        selected = select_cards(plan(cover, good), BODY, [], 0)

        assert selected is not None
        assert [card.card_id for card in selected.body_cards] == ["card-1"]

    def test_generic_and_non_person_plans_are_untouched(self):
        """기존 소재(직업·제품)의 선정 결과는 이번 검증으로 바뀌지 않는다."""
        selected = select_cards(
            plan(
                brief("cover", card_type="THUMBNAIL", section_id=None),
                brief("card-1", section_id="section-1"),
            ),
            BODY,
            [],
            0,
        )

        assert selected is not None
        assert [card.card_id for card in selected.body_cards] == ["card-1"]


class TestNamedSubjectFallback:
    async def test_a_failed_character_thumbnail_keeps_its_identity_in_the_fallback(self):
        """계획 썸네일이 실패해도 일반 폴백이 이름 없는 인물로 대체하지 않는다."""
        generator = CardPlanningGenerator(
            plan(
                named_brief(
                    "cover",
                    kind="FICTIONAL_CHARACTER",
                    identity="Spider-Man",
                    subject="Spider-Man crouched on a rooftop",
                )
            )
        )
        images = SceneImageGenerator(fail_ids={"cover"})
        service, repository = build_card_service(generator, images)
        await repository.create(
            with_reference_photo(
                build_task(input=BlogTaskInput(topic="스파이더맨", keywords=["스파이더맨"]))
            )
        )

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        # 고유 대상 카드는 같은 인물로 단순 구도 재시도를 한 번 더 한다(2회) — 그다음이 폴백.
        planned_calls = [call for call in images.calls if call.card is not None]
        assert len(planned_calls) == 2
        assert planned_calls[1].simplified_identity_retry is True
        assert planned_calls[1].subject_identity == "Spider-Man"

        fallback = images.calls[-1]
        assert fallback.card is None and fallback.is_thumbnail
        assert fallback.subject_kind == "FICTIONAL_CHARACTER"
        assert fallback.subject_identity == "Spider-Man"
        assert fallback.must_show_subject is True
        prompt = image_prompt(fallback)
        assert "The primary named subject is exactly: Spider-Man" in prompt
        assert "generic superhero" in prompt

    async def test_a_rejected_character_plan_still_carries_the_name_to_the_fallback(self):
        """계획 전체가 규격에 걸려 버려져도 '이 글은 그 캐릭터 글'이라는 사실은 남는다."""
        generator = CardPlanningGenerator(
            plan(
                named_brief(
                    "cover",
                    kind="FICTIONAL_CHARACTER",
                    identity="Batman",
                    subject="a bat-shaped symbol projected on clouds",
                )
            )
        )
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(
            with_reference_photo(
                build_task(input=BlogTaskInput(topic="배트맨", keywords=["배트맨"]))
            )
        )

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert len(images.calls) == 1
        fallback = images.calls[0]
        assert fallback.card is None and fallback.is_thumbnail
        assert fallback.subject_kind == "FICTIONAL_CHARACTER"
        assert fallback.subject_identity == "Batman"
        assert "The primary named subject is exactly: Batman" in image_prompt(fallback)

    async def test_the_planned_scene_carries_the_identity_fields_to_the_image_input(self):
        generator = CardPlanningGenerator(
            plan(
                named_brief(
                    "cover",
                    kind="REAL_NAMED_PERSON",
                    identity="Son Heung-min 손흥민",
                    subject="Son Heung-min warming up on the pitch",
                )
            )
        )
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(
            with_reference_photo(
                build_task(input=BlogTaskInput(topic="손흥민", keywords=["손흥민"]))
            )
        )

        await service.generate_draft("post_1", {})

        call = images.calls[0]
        assert call.subject_kind == "REAL_NAMED_PERSON"
        assert call.must_show_subject is True
        assert call.subject_identity == "Son Heung-min 손흥민"

    async def test_an_ordinary_topic_keeps_the_previous_image_behaviour(self):
        """사람이 아닌 소재는 예전과 같다 — 정체성 강제도, 사람 추가도 없다."""
        generator = CardPlanningGenerator(
            plan(brief("cover", card_type="THUMBNAIL", section_id=None))
        )
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(
            build_task(input=BlogTaskInput(topic="노트북 구매 가이드", keywords=["노트북"]))
        )

        await service.generate_draft("post_1", {})

        call = images.calls[0]
        assert call.prompt_version == "m5-image@v3.7"
        assert call.subject_kind == "NON_PERSON"
        assert call.must_show_subject is False
        assert "The primary named subject" not in image_prompt(call)


class TestReusedReferenceContract:
    """REUSED는 정확한 검증 완료 사진의 로컬 배치다. 다른 사진/AI 생성은 폴백이 아니다."""

    @staticmethod
    def reused_cover(reference_id: str) -> CardBrief:
        return brief(
            "cover", card_type="THUMBNAIL", section_id=None
        ).model_copy(
            update={
                "uses_reference": True,
                "reference_id": reference_id,
                "generated_or_reused": "REUSED",
            }
        )

    @staticmethod
    def task_with_photo(photo: str):
        task = build_task()
        return task.model_copy(
            update={
                "input": task.input.model_copy(
                    update={
                        "reference_materials": [
                            ReferenceMaterial(
                                type=ReferenceMaterialType.IMAGE,
                                name="reference.jpg",
                                value=photo,
                            )
                        ]
                    }
                )
            }
        )

    async def test_missing_exact_reference_never_uses_another_photo_or_generation(self):
        """ref-2를 지목했는데 ref-1만 있으면 첫 사진으로 되돌리거나 새로 그리지 않는다."""
        generator = CardPlanningGenerator(
            plan(self.reused_cover("reference-image-2"))
        )
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(self.task_with_photo(jpeg_data_url()))

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert updated.final_post.featured_image is None
        assert updated.final_post.thumbnail_copy == []
        assert images.calls == []

    async def test_local_render_failure_never_falls_back_to_a_generated_thumbnail(self):
        generator = CardPlanningGenerator(
            plan(self.reused_cover("reference-image-1"))
        )
        images = SceneImageGenerator(fail_ids={"cover"})
        service, repository = build_card_service(generator, images)
        await repository.create(self.task_with_photo(jpeg_data_url()))

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert updated.final_post.featured_image is None
        assert len(images.calls) == 1
        assert images.calls[0].card.card_id == "cover"
        assert images.calls[0].web_photo is not None
        assert images.calls[0].reference_image is None
        assert not any(call.card is None for call in images.calls)


# --- 실존 인물이 이미지에 실제로 등장하는가(2026-07-31 재현 사례) ---
#
# 재현: 소재 '프로미스나인' + 키워드 '백지헌' → 썸네일과 본문에 백지헌이 아닌 이름 없는
# 여성이 나왔다. 이름이 **키워드에만** 있었고 그 키워드가 카드 계획 프롬프트에 실리지
# 않았다. 아래는 그 경로를 끝에서 끝까지 고정한다.


def person_task(topic: str, keywords: list[str], title: str = "제목"):
    """실존 인물 글의 입력. 제목은 원고 스텁(DRAFT)의 것을 그대로 쓴다."""
    return build_task(input=BlogTaskInput(topic=topic, keywords=keywords))


def with_reference_photo(task, name: str = "person.jpg"):
    """인물 참고 사진을 붙인다 — 참고 근거가 없는 인물·캐릭터는 이름을 들고 생성하지
    않으므로(2026-08-10), 이름이 생성 요청까지 가는 흐름을 검증하려면 이것이 전제다."""
    return task.model_copy(
        update={
            "input": task.input.model_copy(
                update={
                    "reference_materials": [
                        ReferenceMaterial(
                            type=ReferenceMaterialType.IMAGE,
                            name=name,
                            value=jpeg_data_url(),
                        )
                    ]
                }
            )
        }
    )


class TestPlanPromptCarriesTheDesignsPhotoDecision:
    """구조 설계가 사진을 계획한 자리를 사진 계획 단계가 알게 한다(2026-08-11).

    지금까지 설계의 visualType은 코드 렌더링 자료(표·차트)에만 쓰였고, 설계가 PHOTO를
    지정한 섹션은 사진 계획에 전달되지 않아 그냥 사라졌다 — 같은 모델이 같은 글에 대해
    두 번 계획하면서 앞의 판단을 물려받지 않았다.
    """

    @staticmethod
    def _input(*sections):
        from app.shared import (
            ContentPlan,
            ContentPlanSection,
            DraftFormat,
            DraftGenerationInput,
            SelectedIntentForDraft,
        )

        return DraftGenerationInput(
            post_id="post_1",
            user_id="user_1",
            input=BlogTaskInput(topic="에어프라이어", purpose=["정보 전달"], keywords=[]),
            selected_intent=SelectedIntentForDraft(
                intent_id="i1", title="t", target_reader="자취생", rationale="r"
            ),
            prompt_version="m4-draft@v2.0",
            format=DraftFormat.MARKDOWN,
            content_plan=ContentPlan(
                target_reader="자취생",
                reader_problem="p",
                reader_question="q",
                article_promise="a",
                content_angle="c",
                sections=[
                    ContentPlanSection(
                        section_id=section_id,
                        heading=heading,
                        question="q",
                        purpose="문제 제기",
                        visual_type=visual_type,
                        visual_reason=reason,
                    )
                    for section_id, heading, visual_type, reason in sections
                ],
            ),
        )

    def test_a_section_planned_for_a_photo_is_marked(self):
        from app.llm.prompts import card_plan_prompt

        draft_input = self._input(
            ("s1", "사용 전 준비", "PHOTO", "실제 조리 장면이 있어야 순서가 이해된다"),
            ("s2", "가격 비교", "TABLE", "수치 비교표"),
            ("s3", "마무리", "NONE", None),
        )

        prompt = card_plan_prompt(draft_input, DRAFT.final_post, 0, 0)

        assert "[s1] 사용 전 준비  ※ 설계 판단: PHOTO 필요 — 실제 조리 장면" in prompt
        # 코드가 그리는 자료(표·차트)는 사진 계획이 볼 것이 아니다.
        assert "[s2] 가격 비교" in prompt and "TABLE 필요" not in prompt
        assert "[s3] 마무리" in prompt and "NONE" not in prompt

    def test_the_design_is_a_hint_not_an_order(self):
        """설계는 본문이 쓰이기 전 판단이다. 완성된 본문이 기준이라고 못박는다."""
        from app.llm.prompts import card_plan_prompt

        prompt = card_plan_prompt(
            self._input(("s1", "사용 전 준비", "PHOTO", "이유")), DRAFT.final_post, 0, 0
        )

        assert "참고만 하고 따르지는 않아도 된다" in prompt
        assert "표시가 없는 섹션이라도 본문이 요구하면 계획해도 된다" in prompt


class TestRealNamedPersonPlanPrompt:
    def test_the_plan_prompt_carries_the_keyword_that_names_the_person(self):
        """키워드에만 있는 인물명이 계획 단계까지 도달한다 — 재현 사례의 원인."""
        from app.llm.prompts import card_plan_prompt
        from app.shared import DraftFormat, DraftGenerationInput, SelectedIntentForDraft

        draft_input = DraftGenerationInput(
            post_id="post_1",
            user_id="user_1",
            input=BlogTaskInput(
                topic="프로미스나인", purpose=["정보 전달"], keywords=["백지헌"]
            ),
            selected_intent=SelectedIntentForDraft(
                intent_id="i1", title="t", target_reader="팬", rationale="r"
            ),
            prompt_version="m4-draft@v2.0",
            format=DraftFormat.MARKDOWN,
        )

        prompt = card_plan_prompt(draft_input, DRAFT.final_post, 0, 0)

        assert "- 키워드: 백지헌" in prompt
        assert "- 소재: 프로미스나인" in prompt
        # 그룹 소재 + 멤버 키워드의 판정 규칙과 금지 표현이 함께 실린다.
        assert "핵심 인물이 소재가 아니라 키워드에만 있을 수 있다" in prompt
        assert "REAL_NAMED_PERSON" in prompt
        assert "a young woman" in prompt and "an idol-like woman" in prompt
        # 그룹만 입력된 글을 멤버 한 명으로 좁히지 않는다.
        assert "멤버 한 명으로 임의로" in prompt


class TestRealNamedPersonPipeline:
    async def test_baek_jiheon_is_the_subject_of_every_stage(self):
        """테스트 1 — 프로미스나인 + 백지헌(참고 사진 있음 — 이름 생성의 전제)."""
        cover = named_brief(
            "cover",
            kind="REAL_NAMED_PERSON",
            identity="백지헌 Baek Jiheon",
            subject="Baek Jiheon of fromis_9, the real member herself",
        )
        generator = CardPlanningGenerator(plan(cover))
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(with_reference_photo(person_task("프로미스나인", ["백지헌"])))

        await service.generate_draft("post_1", {})

        call = images.calls[0]
        assert call.subject_kind == "REAL_NAMED_PERSON"
        assert call.subject_identity == "백지헌 Baek Jiheon"
        assert call.must_show_subject is True

        prompt = image_prompt(call)
        assert "The named real person is exactly: 백지헌 Baek Jiheon" in prompt
        assert "Show the actual named person, 백지헌 Baek Jiheon" in prompt
        for banned in (
            "anonymous model",
            "look-alike",
            "generic idol-like person",
            "generic singer or performer",
            "generic person with the same occupation",
            "similar mood or styling",
        ):
            assert banned in prompt
        assert "Do not silently substitute another person" in prompt

    async def test_a_named_person_thumbnail_keeps_the_title_box_off_the_face(self):
        """테스트 8 — 제목 박스가 얼굴 위에 앉지 않는다(참고 사진 있음)."""
        cover = named_brief(
            "cover",
            kind="REAL_NAMED_PERSON",
            identity="백지헌 Baek Jiheon",
            subject="Baek Jiheon of fromis_9, the real member herself",
        )
        generator = CardPlanningGenerator(plan(cover))
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(with_reference_photo(person_task("프로미스나인", ["백지헌"])))

        updated = await service.generate_draft("post_1", {})

        layout = updated.draft_generation_result.thumbnail_layout_plan
        assert layout.copy_zone == "BOTTOM_CENTER"
        assert layout.subject_zone == "TOP_CENTER"
        # 문구 영역과 피사체 영역이 실제로 겹치지 않는다(코드가 계산한다).
        from app.llm.imaging import zones_overlap

        assert not zones_overlap(layout.copy_zone, layout.subject_zone)
        prompt = image_prompt(images.calls[0])
        assert "the face is large, sharp, well lit and unobstructed" in prompt
        assert "upper two thirds" in prompt

    async def test_a_non_person_thumbnail_keeps_the_centre_title_box(self):
        """테스트 6 — 사람이 아닌 소재의 썸네일 합성은 예전 그대로다."""
        generator = CardPlanningGenerator(
            plan(brief("cover", card_type="THUMBNAIL", section_id=None))
        )
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(person_task("노트북 구매 가이드", ["노트북"]))

        updated = await service.generate_draft("post_1", {})

        layout = updated.draft_generation_result.thumbnail_layout_plan
        assert layout.copy_zone == "CENTER"
        assert layout.subject_zone == "CENTER"

    async def test_person_reference_images_reach_the_generation_request(self):
        """테스트 9 — 참고 이미지가 URL로만 남지 않고 실제 요청에 실린다."""
        cover = named_brief(
            "cover",
            kind="REAL_NAMED_PERSON",
            identity="백지헌 Baek Jiheon",
            subject="Baek Jiheon of fromis_9, the real member herself",
        )
        generator = CardPlanningGenerator(plan(cover))
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        photo = jpeg_data_url()
        task = person_task("프로미스나인", ["백지헌"])
        task = task.model_copy(
            update={
                "input": task.input.model_copy(
                    update={
                        "reference_materials": [
                            ReferenceMaterial(
                                type=ReferenceMaterialType.IMAGE,
                                name="jiheon.jpg",
                                value=photo,
                            )
                        ]
                    }
                )
            }
        )
        await repository.create(task)

        await service.generate_draft("post_1", {})

        call = images.calls[0]
        assert call.reference_person_images == [normalize_data_url(photo)]
        # 편집 기준(image-to-image)으로도 실린다 — 얼굴의 유일한 근거이기 때문이다.
        assert call.reference_image == normalize_data_url(photo)
        prompt = image_prompt(call)
        assert "Use the supplied reference image(s) only to preserve the identity" in prompt
        assert "Create a new composition rather than copying the original" in prompt

    async def test_a_person_without_references_is_never_generated_by_name(self):
        """테스트 7 — 참고 사진이 없는 실존 인물은 이름을 들고 생성하지 않는다(2026-08-10
        사용자 지시 "차단 자체를 해결"). 이름만으로는 그 사람을 그릴 수 없다 — 안전
        시스템이 거절하거나(카드당 수십~백여 초) '닮은 남'이 나온다. 이름 없는 인물로
        바꾸지 않는 계약은 그대로다: 인물인 척하는 생성이 아예 없고, 대표 이미지는
        인물이 아닌 일반 썸네일 폴백으로 채운다."""
        cover = named_brief(
            "cover",
            kind="REAL_NAMED_PERSON",
            identity="백지헌 Baek Jiheon",
            subject="Baek Jiheon of fromis_9, the real member herself",
        )
        generator = CardPlanningGenerator(plan(cover))
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        # 그 사람 이름이 실린 생성 호출이 하나도 없다 — 계획 카드도, 폴백도.
        assert all(
            call.subject_identity != "백지헌 Baek Jiheon" for call in images.calls
        )
        # 대표 이미지는 이름 없는 일반 폴백으로 채웠다.
        assert any(call.card is None for call in images.calls)
        assert updated.final_post.images

    async def test_a_body_photo_of_the_person_is_filled_without_the_identity(self):
        """생성할 수 없는 인물 본문 사진을 **다른 사람 사진으로 메우지 않는** 계약은
        그대로다 — 대신 자리를 비워 두지도 않는다. 계획이 약속한 장수는 대상 없는
        분위기 사진(정체성·소재명 억제)으로 채운다(2026-08-10 사용자: "총 3장이라
        해놨으면서 이미지를 하나만 만들었어")."""
        cover = named_brief(
            "cover",
            kind="REAL_NAMED_PERSON",
            identity="백지헌 Baek Jiheon",
            subject="Baek Jiheon of fromis_9, the real member herself",
        )
        body = named_brief(
            "photo-1",
            kind="REAL_NAMED_PERSON",
            identity="백지헌 Baek Jiheon",
            subject="Baek Jiheon singing on a small stage",
            card_type="SECTION_CARD",
            section_id="section-1",
        )
        generator = CardPlanningGenerator(plan(cover, body))
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        # 썸네일(이름 없는 폴백) + 본문 채움(대상 없는 분위기 사진) = 2장.
        assert len(updated.final_post.images) == 2
        # 어느 호출에도 그 사람 이름이 실리지 않는다 — 계획 카드도, 폴백도, 채움도.
        assert all(
            call.subject_identity != "백지헌 Baek Jiheon" for call in images.calls
        )
        # 본문 채움 호출은 정체성 억제 상태다(카드 없이, 이름·소재 앵커 없이).
        filler_calls = [
            call for call in images.calls if call.card is None and not call.is_thumbnail
        ]
        assert filler_calls
        assert all(call.subject_identity is None for call in filler_calls)
        assert all(call.suppress_topic_anchor for call in filler_calls)


class TestNamedPersonPromptsBySubject:
    """테스트 2·3·5 — 손흥민·아이유·헬스 트레이너."""

    def test_son_heung_min_is_not_a_generic_footballer(self):
        card = named_brief(
            "cover",
            kind="REAL_NAMED_PERSON",
            identity="손흥민 Son Heung-min",
            subject="Son Heung-min 손흥민 himself on a training pitch",
        )

        prompt = card_scene_prompt(image_input_for(card, topic="손흥민"))

        assert "The named real person is exactly: 손흥민 Son Heung-min" in prompt
        assert "generic person with the same occupation" in prompt
        # 원고에 없는 우승·트로피·경기 장면을 만들지 않는다.
        assert "awards, trophies, matches" in prompt

    def test_iu_is_not_a_generic_female_singer(self):
        card = named_brief(
            "cover",
            kind="REAL_NAMED_PERSON",
            identity="아이유 IU",
            subject="아이유 IU herself at a press event",
        )

        prompt = card_scene_prompt(image_input_for(card, topic="아이유"))

        assert "The named real person is exactly: 아이유 IU" in prompt
        assert "generic singer or performer" in prompt
        assert "anonymous model" in prompt

    def test_a_gym_trainer_stays_a_generic_role(self):
        card = named_brief(
            "cover",
            kind="GENERIC_PERSON_ROLE",
            identity=None,
            subject="a personal trainer correcting a squat in a real gym",
            must_show=False,
        )

        prompt = card_scene_prompt(image_input_for(card, topic="헬스 트레이너"))

        assert card.subject_kind == "GENERIC_PERSON_ROLE"
        assert card.must_show_subject is False
        assert "PRIMARY IDENTITY REQUIREMENT" not in prompt
        assert "the face is large, sharp" not in prompt


class TestGroupWithoutAMember:
    """테스트 4 — 그룹명만 입력된 글은 멤버 한 명으로 임의 고정하지 않는다."""

    async def test_a_group_only_topic_is_not_pinned_to_one_member(self):
        cover = brief("cover", card_type="THUMBNAIL", section_id=None).model_copy(
            update={"scene": CardScene(main_subject="the fromis_9 group on stage")}
        )
        generator = CardPlanningGenerator(plan(cover))
        images = SceneImageGenerator()
        service, repository = build_card_service(generator, images)
        await repository.create(person_task("프로미스나인", []))

        await service.generate_draft("post_1", {})

        call = images.calls[0]
        assert call.subject_kind == "NON_PERSON"
        assert call.must_show_subject is False
        assert "PRIMARY IDENTITY REQUIREMENT" not in image_prompt(call)


class TestSubjectFieldNormalization:
    """§4 정규화 규칙 — 저장 모델과 이미지 입력이 같은 규칙을 쓴다."""

    def test_a_real_person_without_a_name_is_refused_everywhere(self):
        with pytest.raises(ValueError):
            CardBrief.model_validate(
                {
                    "cardId": "cover",
                    "cardType": "THUMBNAIL",
                    "articleClaim": CLAIM_1,
                    "visualPurpose": "p",
                    "scene": {"mainSubject": "a singer"},
                    "subjectKind": "REAL_NAMED_PERSON",
                }
            )
        with pytest.raises(ValueError):
            image_input_for(
                brief("cover", card_type="THUMBNAIL", section_id=None),
                topic="아이유",
                subject_kind="REAL_NAMED_PERSON",
                subject_identity=None,
            )

    def test_must_show_is_forced_on_for_named_kinds(self):
        card = CardBrief.model_validate(
            {
                "cardId": "cover",
                "cardType": "THUMBNAIL",
                "articleClaim": CLAIM_1,
                "visualPurpose": "p",
                "scene": {"mainSubject": "아이유 IU herself"},
                "subjectKind": "REAL_NAMED_PERSON",
                "subjectIdentity": "아이유 IU",
                "mustShowSubject": False,
                "identityConfidence": 0.93,
            }
        )

        assert card.must_show_subject is True
        assert card.identity_confidence == 0.93

    def test_old_documents_still_read_with_the_previous_defaults(self):
        card = CardBrief.model_validate(
            {
                "cardId": "cover",
                "cardType": "THUMBNAIL",
                "articleClaim": CLAIM_1,
                "visualPurpose": "p",
                "scene": {"mainSubject": "a laptop on a desk"},
            }
        )

        assert card.subject_kind == "NON_PERSON"
        assert card.must_show_subject is False
        assert card.identity_confidence == 0.0
        assert card.reference_person_images == []
        # 구도 필드가 늘어난 뒤에도 옛 문서는 그대로 읽힌다(2026-08-05).
        assert card.visual_subject == ""
        assert card.framing == "FULL_SUBJECT"  # THUMBNAIL이므로 전체 형태
        assert card.show_complete_subject is True


class TestFramingNormalization:
    """구도 규칙(2026-08-05). 계획 모델이 무엇을 내려도 코드가 마지막에 못 박는다 —
    디올 글에서 대표 사진에 손잡이만 실린 것이 이 장치가 없어서였다."""

    def card(self, **overrides) -> CardBrief:
        payload = {
            "cardId": "photo-1",
            "cardType": "SECTION_CARD",
            "articleClaim": CLAIM_1,
            "visualPurpose": "p",
            "scene": {"mainSubject": "a handbag on a table"},
            **overrides,
        }
        return CardBrief.model_validate(payload)

    def test_the_cover_is_always_the_whole_subject(self):
        card = self.card(
            cardType="THUMBNAIL", framing="CLOSE_UP", photoRole="PRODUCT_DETAIL"
        )
        assert card.framing == "FULL_SUBJECT"
        assert card.show_complete_subject is True

    def test_a_close_up_without_a_detail_role_is_pulled_back(self):
        """문단이 디테일을 설명하지 않는데 확대하면 무엇인지 알 수 없는 사진이 된다."""
        card = self.card(framing="CLOSE_UP", photoRole="IN_USE_SCENE")
        assert card.framing == "MEDIUM"
        assert card.show_complete_subject is True

    def test_a_detail_role_may_keep_its_close_up(self):
        card = self.card(framing="CLOSE_UP", photoRole="PRODUCT_DETAIL")
        assert card.framing == "CLOSE_UP"
        assert card.show_complete_subject is False

    def test_a_hero_shot_defaults_to_the_whole_subject(self):
        assert self.card(photoRole="PRODUCT_HERO").framing == "FULL_SUBJECT"

    def test_an_unknown_value_falls_back_instead_of_failing(self):
        assert self.card(framing="EXTREME_MACRO").framing == "MEDIUM"


class TestFramingReachesTheImagePrompt:
    """구도가 계획에만 남고 이미지 프롬프트에 도달하지 않으면 아무것도 달라지지 않는다."""

    def prompt_for(self, **overrides) -> str:
        card = brief("photo-1", **overrides)
        return image_prompt(image_input_for(card, topic="디올", is_thumbnail=False))

    def test_the_whole_subject_rule_is_appended(self):
        prompt = self.prompt_for()
        assert "REQUIRED FRAMING" in prompt
        assert "entirely inside the frame" in prompt
        assert "FORBIDDEN COMPOSITIONS" in prompt
        assert "only a handle" in prompt

    def test_a_detail_card_gets_the_detail_rule_instead(self):
        prompt = self.prompt_for(photo_role="PRODUCT_DETAIL", framing="CLOSE_UP")
        assert "deliberate detail shot" in prompt
        assert "entirely inside the frame" not in prompt
        # 디테일이라도 금지 구도는 그대로다 — 프레임 밖으로 나가면 안 된다.
        assert "FORBIDDEN COMPOSITIONS" in prompt

    def test_the_visual_subject_is_named_to_the_image_model(self):
        prompt = self.prompt_for(visual_subject="레이디 디올 핸드백 한 점의 전체 모습")
        assert "레이디 디올 핸드백 한 점의 전체 모습" in prompt

    def test_a_close_up_shot_rotation_is_skipped_for_whole_subject_photos(self):
        """뒤에 붙는 규칙만으로는 앞의 'close-up on hands and objects'를 이기지 못한다."""
        from app.llm.prompts import shot_specification

        assert "close-up" not in shot_specification(1, "MEDIUM")
        assert "close-up" in shot_specification(1, "CLOSE_UP")

    def test_a_planned_close_up_distance_is_dropped_when_it_contradicts(self):
        card = brief("photo-1")
        card.scene.camera_distance = "extreme close-up"
        assert "Camera distance" not in image_prompt(
            image_input_for(card, topic="디올", is_thumbnail=False)
        )

        detail = brief("photo-2", photo_role="PRODUCT_DETAIL", framing="CLOSE_UP")
        detail.scene.camera_distance = "extreme close-up"
        assert "Camera distance: extreme close-up" in image_prompt(
            image_input_for(detail, topic="디올", is_thumbnail=False)
        )


class FakePhotoSearch:
    """웹 사진 검색 스텁. 어떤 질의로 불렸는지가 이 테스트들의 관심사다."""

    def __init__(
        self,
        results: dict[str, int] | None = None,
        error: Exception | None = None,
        reference_results: dict[str, int] | None = None,
    ):
        # results: 질의 → 규격 통과 사진 장수. reference_results: 질의 → 규격 미달
        # 사진 장수(실제 검색기는 규격 사진이 없을 때만 미달 사진을 참고용으로 돌려준다).
        self._results = results or {}
        self._reference_results = reference_results or {}
        self._error = error
        self.queries: list[tuple[str, int]] = []

    async def find_photos(self, query: str, limit: int = 1):
        self.queries.append((query, limit))
        if self._error is not None:
            raise self._error

        def photo(index: int, meets_spec: bool) -> WebPhoto:
            return WebPhoto(
                data_url=jpeg_data_url(color=(10 * index, 90, 200)),
                source_url=f"https://host{index}.example/{query}.jpg",
                source_host=f"host{index}.example",
                title=query,
                width=1600 if meets_spec else 320,
                height=900 if meets_spec else 180,
                query=query,
                meets_spec=meets_spec,
            )

        available = self._results.get(query, 0)
        if available:
            return [photo(index, True) for index in range(min(available, limit))]
        fallback = self._reference_results.get(query, 0)
        return [photo(0, False)] if fallback else []


class TestImageSourceParsing:
    def test_image_source_is_parsed_and_unknown_values_fall_back(self):
        def raw_card(source):
            return {
                "cardId": "cover",
                "cardType": "THUMBNAIL",
                "articleClaim": "주장",
                "scene": {"mainSubject": "a scene", "action": "a", "setting": "s"},
                "necessityScore": 100,
                "imageSource": source,
            }

        parsed = card_plan_from_json(
            {"cards": [raw_card("youtube_thumbnail"), raw_card("만들어낸값")]}
        )

        assert parsed.cards[0].image_source == "YOUTUBE_THUMBNAIL"
        # 모르는 값은 빈 문자열 — 코드 기본 사다리(네이버→유튜브→생성)로 처리한다.
        assert parsed.cards[1].image_source == ""


class TestWebPhotoForRealPeople:
    """실존 인물은 그리지 말고 가져온다 — 이름만으로는 모델이 그 얼굴을 만들지 못한다."""

    def person_plan(self, *, with_body: bool = False):
        # 정체성은 실제 계획이 만드는 모양(한글+로마자)이다. 장면 묘사가 영어라, 이름이
        # mainSubject에 살아 있는지 보는 검증을 지나려면 로마자가 함께 있어야 한다.
        cover = named_brief(
            "cover",
            kind="REAL_NAMED_PERSON",
            identity="백지헌 Baek Jiheon",
            subject="Baek Jiheon of fromis_9, the real member herself",
        )
        if not with_body:
            return plan(cover)
        body = named_brief(
            "photo-1",
            kind="REAL_NAMED_PERSON",
            identity="백지헌 Baek Jiheon",
            subject="Baek Jiheon on stage",
            card_type="SECTION_CARD",
            section_id="section-1",
            claim=CLAIM_2,
        )
        return plan(cover, body)

    async def test_the_thumbnail_uses_a_real_photo_instead_of_a_generated_lookalike(self):
        search = FakePhotoSearch({"프로미스나인 백지헌": 1})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(self.person_plan()), images, search
        )
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        # 그룹명과 멤버명을 함께 물어본다 — 동명이인이 걸리는 것을 줄인다.
        assert search.queries[0][0] == "프로미스나인 백지헌"
        cover_call = images.calls[0]
        assert cover_call.web_photo is not None
        assert cover_call.web_photo.source_host == "host0.example"
        # 정체성 정보는 그대로 실려 간다(사진을 못 열어 생성으로 되돌아갈 수 있으므로).
        assert cover_call.subject_identity == "백지헌 Baek Jiheon"

    async def test_body_cards_of_the_same_person_get_their_own_photos(self):
        search = FakePhotoSearch({"프로미스나인 백지헌": 2})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(self.person_plan(with_body=True)), images, search
        )
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        await service.generate_draft("post_1", {})

        # 썸네일 1장 + 인물 본문 카드 1장 = 2장, 게이트 예비 +2 = 4장을 요청한다.
        assert search.queries[0] == ("프로미스나인 백지헌", 4)
        hosts = [call.web_photo.source_host for call in images.calls if call.web_photo]
        assert hosts == ["host0.example", "host1.example"]

    async def test_an_uploaded_photo_keeps_the_old_path_and_never_searches(self):
        """올린 사진이 더 나은 근거다. 업로드 사진의 기존 쓰임을 여기서 바꾸지 않는다."""
        search = FakePhotoSearch({"프로미스나인 백지헌": 1})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(self.person_plan()), images, search
        )
        photo = jpeg_data_url()
        task = person_task("프로미스나인", ["백지헌"])
        task = task.model_copy(
            update={
                "input": task.input.model_copy(
                    update={
                        "reference_materials": [
                            ReferenceMaterial(
                                type=ReferenceMaterialType.IMAGE,
                                name="jiheon.jpg",
                                value=photo,
                            )
                        ]
                    }
                )
            }
        )
        await repository.create(task)

        await service.generate_draft("post_1", {})

        assert search.queries == []
        assert images.calls[0].web_photo is None
        assert images.calls[0].reference_person_images == [normalize_data_url(photo)]

    async def test_when_the_person_is_not_found_the_query_widens_to_the_topic(self):
        """그 사람을 못 구해도 소재와 무관한 그림은 내보내지 않는다."""
        search = FakePhotoSearch({"프로미스나인": 1})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(self.person_plan()), images, search
        )
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        await service.generate_draft("post_1", {})

        # 한글 이름이 먼저다 — 한국어 검색 API에 로마자를 섞으면 질의가 나빠진다.
        assert [query for query, _ in search.queries] == [
            "프로미스나인 백지헌",
            "백지헌",
            "백지헌 Baek Jiheon",
            "프로미스나인",
        ]
        assert images.calls[0].web_photo.query == "프로미스나인"

    async def test_a_search_failure_still_finishes_the_article(self):
        """검색·네트워크 실패는 원고를 버릴 이유가 아니다 — 예전 생성 경로로 간다."""
        search = FakePhotoSearch(error=RuntimeError("네이버 검색 API 권한 없음"))
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(self.person_plan()), images, search
        )
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert images.calls[0].web_photo is None
        # 첫 질의에서 실패하면 더 두드리지 않는다(같은 이유로 또 실패한다).
        assert len(search.queries) == 1

    async def test_a_generic_article_also_searches_the_web_first(self):
        """2026-08-03 사용자 결정: 모든 사진은 웹 검색이 먼저다. 고유 대상이 없으면
        소재로 묻고, 찾으면 그 사진을 쓴다."""
        search = FakePhotoSearch({"노트북 구매 가이드": 1})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(plan(brief("cover", card_type="THUMBNAIL", section_id=None))),
            images,
            search,
        )
        await repository.create(person_task("노트북 구매 가이드", ["노트북"]))

        await service.generate_draft("post_1", {})

        # 자리 1 + 게이트 예비 2 = 3장을 요청한다(오판 시 예비가 잇는다, 2026-08-10).
        assert search.queries == [("노트북 구매 가이드", 3)]
        assert images.calls[0].web_photo is not None
        assert images.calls[0].web_photo.query == "노트북 구매 가이드"

    async def test_generic_body_cards_share_one_topic_search_without_duplicates(self):
        """일반 카드들은 소재 질의 하나로 묶어 요청하고, 서로 다른 사진을 받는다."""
        search = FakePhotoSearch({"노트북 구매 가이드": 3})
        images = SceneImageGenerator()
        cards = plan(
            brief("cover", card_type="THUMBNAIL", section_id=None),
            brief("card-1", section_id="section-1", score=90, subject="scene one"),
            brief("card-2", section_id="section-2", score=85, subject="scene two", claim=CLAIM_2),
        )
        service, repository = build_card_service(
            CardPlanningGenerator(cards), images, search
        )
        await repository.create(person_task("노트북 구매 가이드", ["노트북"]))

        await service.generate_draft("post_1", {})

        # 썸네일+본문 2장 = 자리 3 + 예비 2 = 5장이지만 호출 상한(MAX_WEB_PHOTOS=4)에 잘린다.
        assert search.queries == [("노트북 구매 가이드", 4)]
        hosts = [call.web_photo.source_host for call in images.calls if call.web_photo]
        assert len(hosts) == 3 and len(set(hosts)) == 3

    async def test_a_generic_search_miss_still_generates_the_images(self):
        """웹에서 못 구한 카드는 예전처럼 이미지 모델이 생성한다."""
        search = FakePhotoSearch({})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(plan(brief("cover", card_type="THUMBNAIL", section_id=None))),
            images,
            search,
        )
        await repository.create(person_task("노트북 구매 가이드", ["노트북"]))

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert images.calls[0].web_photo is None
        assert len(updated.final_post.images) == 1

    async def test_without_a_search_client_the_pipeline_is_unchanged(self):
        """네이버 자격 증명이 없는 설치에서도 예전과 똑같이 돈다."""
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(self.person_plan()), images, None
        )
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert images.calls[0].web_photo is None

    async def test_a_youtube_card_uses_the_youtube_thumbnail_first(self):
        """카드가 YOUTUBE_THUMBNAIL을 골랐으면 유튜브 썸네일을 먼저 찾는다."""
        naver = FakePhotoSearch({"노트북 구매 가이드": 1})
        youtube = FakePhotoSearch({"노트북 구매 가이드": 1})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(
                plan(
                    brief(
                        "cover",
                        card_type="THUMBNAIL",
                        section_id=None,
                        image_source="YOUTUBE_THUMBNAIL",
                    )
                )
            ),
            images,
            naver,
            youtube,
        )
        await repository.create(person_task("노트북 구매 가이드", ["노트북"]))

        await service.generate_draft("post_1", {})

        assert youtube.queries == [("노트북 구매 가이드", 3)]
        assert naver.queries == []
        assert images.calls[0].web_photo is not None

    async def test_a_youtube_miss_falls_back_to_naver_then_generation(self):
        """유튜브에서 못 구하면 네이버로, 거기서도 못 구하면 생성으로 내려간다."""
        naver = FakePhotoSearch({})
        youtube = FakePhotoSearch({})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(
                plan(
                    brief(
                        "cover",
                        card_type="THUMBNAIL",
                        section_id=None,
                        image_source="YOUTUBE_THUMBNAIL",
                    )
                )
            ),
            images,
            naver,
            youtube,
        )
        await repository.create(person_task("노트북 구매 가이드", ["노트북"]))

        updated = await service.generate_draft("post_1", {})

        assert [q for q, _ in youtube.queries] == ["노트북 구매 가이드"]
        assert [q for q, _ in naver.queries] == ["노트북 구매 가이드"]
        assert images.calls[0].web_photo is None
        assert updated.status.value == "READY_TO_PUBLISH"

    async def test_an_ai_generated_card_still_searches_first(self):
        """AI_GENERATED 판정이어도 검색이 항상 먼저다(2026-08-03 사용자 결정) —
        실제 사진이 있으면 생성하지 않고 그 사진을 쓴다."""
        naver = FakePhotoSearch({"노트북 구매 가이드": 1})
        youtube = FakePhotoSearch({})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(
                plan(
                    brief(
                        "cover",
                        card_type="THUMBNAIL",
                        section_id=None,
                        image_source="AI_GENERATED",
                    )
                )
            ),
            images,
            naver,
            youtube,
        )
        await repository.create(person_task("노트북 구매 가이드", ["노트북"]))

        await service.generate_draft("post_1", {})

        assert naver.queries == [("노트북 구매 가이드", 3)]
        assert images.calls[0].web_photo is not None

    async def test_a_named_subject_overrides_an_ai_generated_choice(self):
        """실존 인물 카드가 AI_GENERATED로 와도 검색으로 바로잡는다 — 생성 모델은
        그 사람을 그리지 못한다."""
        naver = FakePhotoSearch({"프로미스나인 백지헌": 1})
        images = SceneImageGenerator()
        cover = named_brief(
            "cover",
            kind="REAL_NAMED_PERSON",
            identity="백지헌 Baek Jiheon",
            subject="Baek Jiheon of fromis_9, the real member herself",
        ).model_copy(update={"image_source": "AI_GENERATED"})
        service, repository = build_card_service(
            CardPlanningGenerator(plan(cover)), images, naver
        )
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        await service.generate_draft("post_1", {})

        assert naver.queries[0][0] == "프로미스나인 백지헌"
        assert images.calls[0].web_photo is not None

    async def test_a_sub_spec_photo_becomes_the_generation_reference(self):
        """규격 미달 사진은 직접 싣지 않는다. 대신 생성 호출의 참고 이미지가 된다 —
        생성조차 웹 검색 결과를 참고한다(2026-08-03 사용자 결정)."""
        naver = FakePhotoSearch(reference_results={"노트북 구매 가이드": 1})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(
                plan(brief("cover", card_type="THUMBNAIL", section_id=None))
            ),
            images,
            naver,
        )
        await repository.create(person_task("노트북 구매 가이드", ["노트북"]))

        await service.generate_draft("post_1", {})

        cover_call = images.calls[0]
        assert cover_call.web_photo is None  # 미달 사진을 결과로 쓰지 않는다
        assert cover_call.reference_image is not None  # 대신 시각 기준이 된다
        assert cover_call.reference_image.startswith("data:image/jpeg")

    async def test_a_failed_card_plan_still_searches_for_the_cover_photo(self):
        """계획이 버려져 폴백으로 내려가도 대표 썸네일은 웹 사진을 먼저 찾는다 —
        계획이 없으면 고유 대상도 없으므로 소재로 묻는다."""
        search = FakePhotoSearch({"프로미스나인": 1})
        images = SceneImageGenerator()
        # 계획을 아예 만들지 못한 글(어댑터가 None을 준 경우)이 폴백 경로다.
        service, repository = build_card_service(CardPlanningGenerator(None), images, search)
        task = person_task("프로미스나인", ["백지헌"])
        await repository.create(task)

        await service.generate_draft("post_1", {})

        assert search.queries == [("프로미스나인", 3)]
        assert images.calls[0].card is None
        assert images.calls[0].web_photo is not None


class TestWebPhotoSubjectGate:
    """웹 검색 사진은 싣기 전에 그림을 실제로 보고 거른다(2026-08-07).

    실사례: '닷사이 23' 글의 대표 썸네일에 검색 페이지의 애니 일러스트가 실렸다 —
    검색 선정은 픽셀을 못 보고 제목·구도·해상도만 재기 때문이다. 그림을 보는
    판정자(OpenAI 2차 검토기)가 피사체 불일치·비실사를 거르고, 걸러진 자리는 기존
    생성 폴백이 메운다.
    """

    class Reviewer:
        """verify_photo_subjects만 가진 판정자 스텁."""

        def __init__(self, keeps=None, error=None):
            self.calls: list[tuple[str, int]] = []
            self._keeps = keeps
            self._error = error

        async def verify_photo_subjects(self, topic, photos):
            self.calls.append((topic, len(photos)))
            if self._error is not None:
                raise self._error
            return self._keeps if self._keeps is not None else [False] * len(photos)

    def _service(self, reviewer):
        search = FakePhotoSearch({"프로미스나인 백지헌": 1})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(TestWebPhotoForRealPeople().person_plan()),
            images,
            search,
            final_reviewer=reviewer,
        )
        return service, repository, images

    async def test_a_rejected_web_photo_falls_back_to_generation(self):
        reviewer = self.Reviewer()  # 전부 탈락
        service, repository, images = self._service(reviewer)
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert reviewer.calls == [("프로미스나인", 1)]
        # 걸러진 사진은 결과로도, 생성의 참고로도 쓰지 않는다 — 생성 폴백 경로다.
        assert images.calls[0].web_photo is None

    async def test_a_kept_web_photo_is_used_as_before(self):
        reviewer = self.Reviewer(keeps=[True])
        service, repository, images = self._service(reviewer)
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        await service.generate_draft("post_1", {})

        assert reviewer.calls == [("프로미스나인", 1)]
        assert images.calls[0].web_photo is not None

    async def test_a_failing_gate_keeps_the_photo(self):
        """판정 하나가 사진을 잃게 하면 안 된다 — 실패는 전부 통과다."""
        reviewer = self.Reviewer(error=RuntimeError("판정자 죽음"))
        service, repository, images = self._service(reviewer)
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert images.calls[0].web_photo is not None

    async def test_a_reviewer_without_the_method_keeps_the_old_path(self):
        """구형 검토기(판정 메서드 없음)가 붙은 배포도 예전 그대로 돈다."""
        service, repository, images = self._service(object())
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        await service.generate_draft("post_1", {})

        assert images.calls[0].web_photo is not None

    async def test_a_rejected_photo_is_replaced_by_a_gated_spare(self):
        """게이트가 배정 사진을 떨어뜨리면 같은 그룹의 예비가 그 자리를 잇는다.

        2026-08-10 실사례: 후보가 자리 수만큼뿐이라, 진짜 개봉 포스터를 '합성 문구'로
        오판한 한 번에 소재 사진이 전멸하고 생성 폴백만 남았다. 예비도 같은 게이트를
        통과해야 실린다.
        """

        class SequenceReviewer:
            def __init__(self):
                self.calls: list[int] = []

            async def verify_photo_subjects(self, topic, photos):
                self.calls.append(len(photos))
                # 첫 판정(배정 사진)은 탈락, 두 번째 판정(예비 후보)은 통과.
                return [len(self.calls) > 1] * len(photos)

        reviewer = SequenceReviewer()
        # 자리 1 + 예비 2 = 3장을 받는다. host0이 배정되고 host1·host2가 예비다.
        search = FakePhotoSearch({"프로미스나인 백지헌": 3})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(TestWebPhotoForRealPeople().person_plan()),
            images,
            search,
            final_reviewer=reviewer,
        )
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        # 배정 1장 판정 → 탈락 → 예비 2장 판정.
        assert reviewer.calls == [1, 2]
        assert images.calls[0].web_photo is not None
        assert images.calls[0].web_photo.source_host == "host1.example"

    def test_leftover_pool_excludes_used_and_duplicate_urls(self):
        """빈 본문 자리를 채울 예비 풀 — 이미 실린 사진과 그룹 간 중복은 세지 않는다."""
        from app.modules.draft.service import _leftover_spare_photos

        def photo(n: int) -> WebPhoto:
            return WebPhoto(
                data_url="data:image/jpeg;base64,AA==",
                source_url=f"https://host{n}.example/p.jpg",
                source_host=f"host{n}.example",
                query="q",
                width=1600,
                height=900,
                meets_spec=True,
            )

        p1, p2, p3 = photo(1), photo(2), photo(3)
        pool = _leftover_spare_photos(
            {0: [p1, p2], 1: [p1, p2, p3]},  # 두 그룹이 p1·p2를 공유
            p1,  # 대표로 이미 실림
            {0: p2},  # 본문 0번에 이미 실림
        )
        assert [photo.source_url for photo in pool] == [p3.source_url]

    async def test_no_spares_means_the_old_generation_fallback(self):
        """예비가 없으면(후보가 배정분뿐) 예전 그대로 생성 폴백이다 — 판정을 더 부르지 않는다."""
        reviewer = self.Reviewer()  # 전부 탈락
        search = FakePhotoSearch({"프로미스나인 백지헌": 1})
        images = SceneImageGenerator()
        service, repository = build_card_service(
            CardPlanningGenerator(TestWebPhotoForRealPeople().person_plan()),
            images,
            search,
            final_reviewer=reviewer,
        )
        await repository.create(person_task("프로미스나인", ["백지헌"]))

        await service.generate_draft("post_1", {})

        assert reviewer.calls == [("프로미스나인", 1)]
        assert images.calls[0].web_photo is None


class TestWebPhotoQueries:
    """검색 질의를 만드는 규칙. 한국어 검색 API에 무엇을 넣는가가 사진의 질을 가른다."""

    def queries(self, topic: str, identity: str | None, visual_subject: str = ""):
        from app.modules.draft.service import _web_photo_queries
        from app.modules.draft.card_selection import NamedSubject

        task = person_task(topic, [topic])
        subject = (
            NamedSubject(kind="REAL_NAMED_PERSON", identity=identity) if identity else None
        )
        return _web_photo_queries(task, subject, None, visual_subject)

    def test_the_visual_subject_is_asked_before_the_bare_topic(self):
        """소재만으로 물으면 브랜드 전체가 걸린다 — '디올'에는 향수도 매장도 있다.
        문단이 말하는 것은 '레이디 디올 핸드백'이다(2026-08-05)."""
        assert self.queries("디올", None, "레이디 디올 핸드백") == [
            "레이디 디올 핸드백",
            "디올",
        ]

    def test_a_visual_subject_without_the_topic_gets_it_prefixed_first(self):
        """'카메라 모듈'만 물으면 어느 기기인지 알 수 없다 — 소재를 붙여 먼저 묻는다."""
        assert self.queries("아이폰 17", None, "후면 카메라 모듈") == [
            "아이폰 17 후면 카메라 모듈",
            "후면 카메라 모듈",
            "아이폰 17",
        ]

    def test_a_visual_subject_that_already_names_the_topic_is_not_doubled(self):
        assert self.queries("롯데리아", None, "롯데리아 리아 두툼새우 버거") == [
            "롯데리아 리아 두툼새우 버거",
            "롯데리아",
        ]

    def test_no_visual_subject_leaves_the_old_ladder_untouched(self):
        assert self.queries("디올", None) == ["디올"]

    def test_the_korean_name_comes_before_the_romanised_one(self):
        assert self.queries("프로미스나인", "백지헌 Baek Jiheon") == [
            "프로미스나인 백지헌",
            "백지헌",
            "백지헌 Baek Jiheon",
            "프로미스나인",
        ]

    def test_a_name_already_inside_the_topic_is_not_repeated(self):
        """'손흥민' 글의 인물이 손흥민이면 '손흥민 손흥민'을 묻지 않는다."""
        assert self.queries("손흥민", "손흥민 Son Heung-min") == [
            "손흥민",
            "손흥민 Son Heung-min",
        ]

    def test_an_identity_without_hangul_is_used_as_is(self):
        """한글 표기가 없는 정체성(영어권 캐릭터명)은 원래 문자열이 곧 검색어다."""
        assert self.queries("마블 영화", "Spider-Man") == [
            "마블 영화 Spider-Man",
            "Spider-Man",
            "마블 영화",
        ]

    def test_without_a_named_subject_only_the_topic_remains(self):
        assert self.queries("노트북 구매 가이드", None) == ["노트북 구매 가이드"]


class ModerationBlockedGenerator(SceneImageGenerator):
    """fail_ids 카드를 OpenAI 안전 차단(moderation_blocked)처럼 거절한다."""

    async def generate_post_image(self, image_input):
        if image_input.card is not None and image_input.card.card_id in self.fail_ids:
            self.calls.append(image_input)
            raise RuntimeError(
                "provider request failed with 400: Your request was rejected by the "
                "safety system (moderation_blocked)"
            )
        return await super().generate_post_image(image_input)


class TestSafetyBlockedIdentity:
    """안전 차단(moderation)은 구도가 아니라 이름 때문에 나는 결정적 실패다 — 같은 이름
    재시도 4번이 이미지 단계를 8분으로 만들던 자리(2026-08-10 실측, 스파이더맨 글)."""

    def setup_method(self):
        from app.modules.draft.service import _safety_blocked_identities

        _safety_blocked_identities.clear()

    async def test_a_safety_block_stops_same_name_retries_and_goes_nameless(self):
        """참고 사진이 있어 이름을 실었는데 차단됐다 — 발견은 1회로 끝난다. 같은 이름의
        단순 구도 재시도도, 이름 유지 폴백도 없고, 대표 이미지는 이름 없는 폴백 한 번으로
        채운다."""
        cover = named_brief(
            "cover",
            kind="FICTIONAL_CHARACTER",
            identity="테스트캐릭터 Spider-Test",
            subject="Spider-Test swinging between buildings",
        )
        generator = CardPlanningGenerator(plan(cover))
        images = ModerationBlockedGenerator(fail_ids={"cover"})
        service, repository = build_card_service(generator, images)
        task = person_task("스파이더테스트", [])
        task = task.model_copy(
            update={
                "input": task.input.model_copy(
                    update={
                        "reference_materials": [
                            ReferenceMaterial(
                                type=ReferenceMaterialType.IMAGE,
                                name="spider-test.jpg",
                                value=jpeg_data_url(),
                            )
                        ]
                    }
                )
            }
        )
        await repository.create(task)

        updated = await service.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        named_calls = [
            call
            for call in images.calls
            if call.subject_identity == "테스트캐릭터 Spider-Test"
        ]
        assert len(named_calls) == 1, "차단된 이름으로는 딱 한 번만 물어야 한다"
        # 이름 없는 폴백(카드 계획 밖 생성)이 대표 이미지를 채웠다.
        assert any(call.card is None for call in images.calls)
        assert updated.final_post is not None

    async def test_a_blocked_identity_skips_generation_on_the_next_run(self):
        """같은 대상의 두 번째 생성(재생성·다른 글)은 참고 사진이 있어도 차단된 이름으로
        생성 호출 자체를 하지 않는다 — 첫 발견의 수십 초조차 다시 쓰지 않는다."""
        blocked_identity = "테스트캐릭터2 Web-Slinger"
        cover = named_brief(
            "cover",
            kind="FICTIONAL_CHARACTER",
            identity=blocked_identity,
            subject="Web-Slinger on a rooftop",
        )

        def referenced_task():
            base = person_task("웹슬링어", [])
            return base.model_copy(
                update={
                    "input": base.input.model_copy(
                        update={
                            "reference_materials": [
                                ReferenceMaterial(
                                    type=ReferenceMaterialType.IMAGE,
                                    name="web-slinger.jpg",
                                    value=jpeg_data_url(),
                                )
                            ]
                        }
                    )
                }
            )

        first_images = ModerationBlockedGenerator(fail_ids={"cover"})
        service, repository = build_card_service(
            CardPlanningGenerator(plan(cover)), first_images
        )
        await repository.create(referenced_task())
        await service.generate_draft("post_1", {})

        second_images = ModerationBlockedGenerator(fail_ids={"cover"})
        service2, repository2 = build_card_service(
            CardPlanningGenerator(plan(cover)), second_images
        )
        await repository2.create(referenced_task())
        updated = await service2.generate_draft("post_1", {})

        assert updated.status.value == "READY_TO_PUBLISH"
        assert all(
            call.subject_identity != blocked_identity for call in second_images.calls
        ), "차단 이력이 있는 이름은 생성 호출에 실리지 않아야 한다"
