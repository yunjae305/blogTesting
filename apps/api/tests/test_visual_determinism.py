"""시각자료 회전의 결정성 회귀 테스트 (PDF 7-3).

여기서 지키는 성질은 넷이다:
- 같은 글은 매번 같은 팔레트 — 세 장을 각각 부르므로 셋이 같은 값을 봐야 하고, 재시도해도
  같아야 한다. 난수를 쓰면 재시도마다 디자인이 흔들린다.
- 연속된 슬롯은 서로 다른 구도 — 이미지 호출이 서로 독립이라 모델은 다른 슬롯을 모른다.
  프롬프트로 "다르게 찍어라"라고 해도 수렴하므로 코드가 인덱스로 돌린다.
- 표가 끝나면 처음으로 돌아온다.
- 이미지 생성 계층(OpenAI)은 이번 Opus 5 전환의 대상이 아니다 — Anthropic 설정이 새지 않는다.
"""

import json

import httpx
import respx

from app.llm.prompts import (
    IMAGE_SHOT_ROTATION,
    IMAGE_VISUAL_STYLES,
    visual_style_for,
)
from app.modules.draft.editorial_style import pick, variation_seed_for


def shot(image_index: int) -> str:
    """운용 코드와 같은 인덱싱(prompts.card_scene_prompt / image_prompt)."""
    return IMAGE_SHOT_ROTATION[image_index % len(IMAGE_SHOT_ROTATION)]


class TestPaletteIsStablePerPost:
    def test_the_same_post_id_always_gives_the_same_palette(self):
        first = visual_style_for("post_abc")
        for _ in range(20):
            assert visual_style_for("post_abc") == first

    def test_a_retry_of_the_same_post_keeps_the_palette(self):
        # 재시도는 같은 post_id로 같은 함수를 다시 부르는 것이다. 값이 바뀌면 썸네일과 본문
        # 사진의 색이 어긋난다.
        before = visual_style_for("post_retry")
        after = visual_style_for("post_retry")
        assert before == after

    def test_all_three_slots_of_one_post_share_the_palette(self):
        palettes = {visual_style_for("post_shared") for _ in range(3)}
        assert len(palettes) == 1

    def test_different_posts_do_not_all_land_on_one_palette(self):
        picks = {visual_style_for(f"post_{index}") for index in range(60)}
        assert len(picks) == len(IMAGE_VISUAL_STYLES)

    def test_the_palette_always_comes_from_the_declared_pool(self):
        for index in range(40):
            assert visual_style_for(f"p{index}") in IMAGE_VISUAL_STYLES


class TestShotRotation:
    def test_neighbouring_slots_get_different_compositions(self):
        for index in range(1, len(IMAGE_SHOT_ROTATION) * 3):
            assert shot(index) != shot(index - 1), index

    def test_the_rotation_wraps_to_the_start(self):
        size = len(IMAGE_SHOT_ROTATION)
        assert shot(size) == shot(0)
        assert shot(size + 1) == shot(1)

    def test_one_post_with_three_body_photos_uses_three_distinct_shots(self):
        # 썸네일은 0, 본문 사진은 1..N이다(service가 그렇게 넘긴다).
        body = [shot(index) for index in (1, 2, 3)]
        assert len(set(body)) == 3

    def test_slots_do_not_pile_onto_one_composition(self):
        counts: dict[str, int] = {}
        for index in range(len(IMAGE_SHOT_ROTATION) * 4):
            counts[shot(index)] = counts.get(shot(index), 0) + 1
        # 완전 순환이므로 모든 구도가 정확히 같은 횟수 나온다.
        assert len(set(counts.values())) == 1
        assert len(counts) == len(IMAGE_SHOT_ROTATION)


class TestVariationSeed:
    def test_the_same_post_and_revision_pick_the_same_value(self):
        seed = variation_seed_for("post_1", 0, "LIFE_HOME", "STEP_BY_STEP_TUTORIAL")
        assert pick(("가", "나", "다"), seed, "colour") == pick(("가", "나", "다"), seed, "colour")

    def test_regenerating_can_choose_differently(self):
        first = variation_seed_for("post_1", 0, "LIFE_HOME", "STEP_BY_STEP_TUTORIAL")
        second = variation_seed_for("post_1", 1, "LIFE_HOME", "STEP_BY_STEP_TUTORIAL")
        assert first != second

    def test_a_different_category_changes_the_seed(self):
        assert variation_seed_for("post_1", 0, "LIFE_HOME", "A") != variation_seed_for(
            "post_1", 0, "TECH", "A"
        )

    def test_an_empty_category_still_produces_a_usable_seed(self):
        assert variation_seed_for("post_1", 0, "", "") == "post_1:0:OTHER:"


class TestImageLayerUntouched:
    """이미지 생성 계층은 이번 전환 대상이 아니다. Anthropic 전용 설정이 새지 않는지 본다."""

    @respx.mock
    async def test_the_openai_image_request_has_no_anthropic_options(self):
        from app.llm.contracts import PostImageGenerationInput
        from app.llm.live_adapters import BODY_IMAGE_SIZE, OpenAiPostImageGenerator
        from app.llm.provider_config import LlmProvider, LlmRole, RoleConfig
        from app.shared import BlogTaskInput, FinalPost, SelectedIntentForDraft

        route = respx.post("https://api.openai.com/v1/images/generations").mock(
            return_value=httpx.Response(
                200, json={"data": [{"b64_json": _tiny_png_base64()}]}
            )
        )
        role = RoleConfig(
            role=LlmRole.M5_IMAGE,
            label="m5-image",
            provider=LlmProvider.OPENAI,
            model="gpt-image-2",
            api_key_env="OPENAI_API_KEY",
            api_key="test-key",
            has_credentials=True,
        )
        await OpenAiPostImageGenerator(role).generate_post_image(
            PostImageGenerationInput(
                post_id="post_1",
                user_id="user_1",
                input=BlogTaskInput(topic="제습기 관리", keywords=["제습기"]),
                selected_intent=SelectedIntentForDraft(
                    intent_id="i1", title="제습기 관리", target_reader="1인 가구", rationale="근거"
                ),
                final_post=FinalPost(
                    title="제습기 관리 순서",
                    body="본문",
                    hashtags=["제습기"],
                    html_content="<p>본문</p>",
                ),
                prompt_version="m5-image@v3.1",
                image_index=1,
                total_images=3,
            )
        )
        body = json.loads(route.calls[0].request.content)
        # Opus 5용 설정이 이미지 요청으로 흘러가면 OpenAI가 알 수 없는 필드를 받는다.
        for leaked in ("output_config", "thinking", "effort"):
            assert leaked not in body, leaked
        assert body["model"] == "gpt-image-2"
        assert body["size"] == BODY_IMAGE_SIZE


def _tiny_png_base64() -> str:
    import base64
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1536, 1024), (200, 200, 200)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")
