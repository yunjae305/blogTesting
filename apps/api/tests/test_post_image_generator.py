"""M5의 실제 어댑터. 모델이 준 사진이 규격 이미지가 되어 나오는지까지가 M5의 일이다."""

import base64
import io
import json

import httpx
import pytest
import respx
from PIL import Image

from app.llm.contracts import PostImageGenerationInput
from app.llm.imaging import (
    BODY_HEIGHT,
    BODY_WIDTH,
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    font_path,
    thumbnail_keyword_colors,
)
from app.llm.live_adapters import (
    BODY_IMAGE_QUALITY,
    BODY_IMAGE_SIZE,
    COVER_IMAGE_QUALITY,
    COVER_IMAGE_SIZE,
    LEGACY_LANDSCAPE_IMAGE_SIZE,
    OpenAiPostImageGenerator,
)
from app.llm.provider_config import LlmProvider, LlmRole, RoleConfig
from app.shared import (
    BlogTaskInput,
    CardBrief,
    CardScene,
    FinalPost,
    SelectedIntentForDraft,
    WebPhoto,
)
from app.shared.image_bytes import UnsafeImageError, data_url_parts, shrink

ROLE = RoleConfig(
    role=LlmRole.M5_IMAGE,
    label="M5 image generation",
    provider=LlmProvider.OPENAI,
    model="gpt-image-2",
    api_key_env="OPENAI_API_KEY",
    api_key="test-key",
    has_credentials=True,
)

ENDPOINT = "https://api.openai.com/v1/images/generations"


def model_photo() -> str:
    """모델이 실제로 돌려주는 것 — 우리 규격이 아니라 1536×1024다."""
    buffer = io.BytesIO()
    Image.new("RGB", (1536, 1024), (180, 170, 160)).save(buffer, format="JPEG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def build_input(**overrides) -> PostImageGenerationInput:
    defaults = dict(
        post_id="post_1",
        user_id="user_1",
        input=BlogTaskInput(topic="블로그 자동화", keywords=["AI"]),
        selected_intent=SelectedIntentForDraft(
            intent_id="i1", title="제목", target_reader="실무자", rationale="근거"
        ),
        final_post=FinalPost(
            title="블로그 자동화 실전 가이드",
            body="본문",
            hashtags=["AI"],
            html_content="<p>본문</p>",
        ),
        prompt_version="m5-image@v1.0",
        image_index=0,
        total_images=3,
    )
    return PostImageGenerationInput(**{**defaults, **overrides})


async def generate(image_input: PostImageGenerationInput):
    with respx.mock:
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": model_photo()}]})
        )
        return await OpenAiPostImageGenerator(ROLE).generate_post_image(image_input)


def size_of(data_url: str) -> tuple[int, int]:
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw)).size


async def test_what_the_model_returns_is_not_what_the_post_gets():
    """모델은 본문 규격(1280×720)을 그리지 못한다. 규격을 맞추는 것은 어댑터의 몫이다."""
    image = await generate(build_input(content_prompt="a desk"))

    assert size_of(image.data_url) == (BODY_WIDTH, BODY_HEIGHT)
    assert image.mime_type == "image/jpeg"
    # alt 필드가 없는 옛 태그는 영어 장면 묘사가 alt 폴백이다.
    assert image.alt_text == "a desk"


async def test_the_korean_alt_field_wins_over_the_english_scene():
    """한국어 글의 alt는 한국어여야 한다 — 영어 장면 묘사는 이미지 모델용이다."""
    image = await generate(
        build_input(content_prompt="a tidy desk setup", content_alt="정돈된 책상 위 작업 공간")
    )

    assert image.alt_text == "정돈된 책상 위 작업 공간"
    # 한국어 alt가 이미지 모델 프롬프트로 새면 안 된다(모델은 한글을 그리려 든다).
    assert "정돈된" not in image.prompt


@pytest.mark.skipif(font_path() is None, reason="no Korean font on this machine")
async def test_the_cover_comes_back_lettered():
    plain = await generate(build_input(content_prompt="a desk"))
    cover = await generate(
        build_input(is_thumbnail=True, thumbnail_copy=["대표 문구", "두 줄까지"])
    )

    assert size_of(cover.data_url) == (CANVAS_WIDTH, CANVAS_HEIGHT)
    # 같은 사진을 받았는데 결과가 다르다면, 다른 것은 얹힌 문구뿐이다.
    assert cover.data_url != plain.data_url
    assert "대표 문구 두 줄까지" in cover.alt_text


@pytest.mark.skipif(font_path() is None, reason="no Korean font on this machine")
async def test_the_cover_adapter_colors_only_matching_semantic_keywords():
    lines = ["토스뱅크 통장이", "곧 파킹통장"]
    task_input = BlogTaskInput(topic="토스뱅크 통장", keywords=["파킹통장"])
    cover = await generate(
        build_input(
            input=task_input,
            is_thumbnail=True,
            thumbnail_copy=lines,
            thumbnail_accent_family="CYAN_NAVY",
        )
    )
    expected = thumbnail_keyword_colors(
        lines,
        topic=task_input.topic,
        keywords=task_input.keywords,
        accent_family="CYAN_NAVY",
    )
    raw = base64.b64decode(cover.data_url.split(",", 1)[1])
    pixels = list(Image.open(io.BytesIO(raw)).convert("RGB").get_flattened_data())

    def near(pixel, target, tolerance=24):
        return all(
            abs(channel - value) <= tolerance
            for channel, value in zip(pixel, target, strict=True)
        )

    assert all(
        sum(near(pixel, color) for pixel in pixels) > 100
        for color in expected.values()
    )
    assert cover.alt_text.endswith("토스뱅크 통장이 곧 파킹통장")


async def test_the_cover_is_never_asked_to_draw_the_korean_itself():
    """모델은 자연 사진만 만들고, 한글 가독성은 후처리 렌더러가 책임진다."""
    cover = await generate(build_input(is_thumbnail=True, thumbnail_copy=["대표 문구"]))

    assert "대표 문구" not in cover.prompt
    assert "No readable text" in cover.prompt
    assert "is responsible for copy readability" in cover.prompt
    assert "do not draw a panel, banner, gradient overlay or vignette" in cover.prompt
    assert "semi-transparent black title box" in cover.prompt
    assert "defining silhouette and important details" in cover.prompt
    assert "low-detail band" not in cover.prompt
    assert "mid-tone or darker" not in cover.prompt


async def test_the_cover_leaves_the_copy_side_clear_instead_of_centring_everything():
    """'피사체 중앙 + 문구 중앙'을 폐기했다는 것을 프롬프트에서 확인한다.

    문구가 왼쪽에 놓이는 배치라면 이미지 모델은 오른쪽에 피사체를 두고 왼쪽을 비워야 한다 —
    그러지 않으면 인물 얼굴이나 제품 위에 글자가 올라간다.
    """
    from app.shared import ThumbnailLayoutPlan

    layout = ThumbnailLayoutPlan(
        layout="COPY_LEFT_SUBJECT_RIGHT",
        subject_zone="RIGHT_CENTER",
        copy_zone="LEFT_CENTER",
        copy_alignment="LEFT",
        copy_lines=["대표 문구"],
        show_copy=True,
    )
    cover = await generate(
        build_input(is_thumbnail=True, thumbnail_copy=["대표 문구"], thumbnail_layout=layout)
    )

    assert "RIGHT half of the centre square" in cover.prompt
    assert "LEFT half as quiet, uncluttered background" in cover.prompt


async def test_a_copy_free_cover_is_composed_as_a_complete_photograph():
    """문구 없는 썸네일도 정상 결과다. 그때는 빈자리를 남기라고 하지 않는다."""
    from app.shared import ThumbnailLayoutPlan

    layout = ThumbnailLayoutPlan(
        layout="NO_COPY_EDITORIAL_PHOTO", copy_mode="NONE", show_copy=False
    )
    cover = await generate(
        build_input(is_thumbnail=True, thumbnail_copy=["대표 문구"], thumbnail_layout=layout)
    )

    assert "No copy will be added to this image" in cover.prompt
    assert "Do not reserve blank space" in cover.prompt
    # 글자를 얹지 않았으므로 alt에도 문구가 없다.
    assert "대표 문구" not in cover.alt_text


async def test_brand_marks_are_preserved_only_when_the_product_is_the_subject():
    """로고 전면 금지를 조건부로 바꿨다. 가짜 로고 금지는 그대로다."""
    generic = await generate(build_input(content_prompt="a desk"))
    assert "No readable text, letters, numbers, logos" in generic.prompt

    branded = await generate(
        build_input(
            content_prompt="a shoe on a bench",
            preserve_brand_marks=True,
            subject_identity="Nike Air Max 90 화이트",
            fidelity_requirements=["화이트와 그레이 중심 색상", "측면 스우시"],
        )
    )
    assert "keep the brand marks that are already present" in branded.prompt
    assert "Do NOT redraw, restyle, relabel" in branded.prompt
    assert "Nike Air Max 90 화이트" in branded.prompt
    assert "측면 스우시" in branded.prompt


async def test_planned_body_photo_requests_a_near_final_medium_source():
    body_photo = CardBrief(
        card_id="photo-1",
        card_type="SECTION_CARD",
        section_id="section-1",
        article_claim="본문에 실제로 있는 문장",
        visual_purpose="실제 사용 장면",
        scene=CardScene(
            main_subject="hands preparing ingredients",
            action="slicing vegetables",
            setting="a lived-in Korean home kitchen",
        ),
        necessity_score=90,
    )
    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": model_photo()}]})
        )
        image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
            build_input(card=body_photo, image_index=1)
        )

    payload = json.loads(route.calls[0].request.content)
    assert payload["size"] == BODY_IMAGE_SIZE
    assert payload["quality"] == BODY_IMAGE_QUALITY
    assert payload["output_format"] == "jpeg"
    assert size_of(image.data_url) == (BODY_WIDTH, BODY_HEIGHT)
    assert "square editorial card" not in image.prompt
    assert "do not reserve space for text" in image.prompt


async def test_planned_thumbnail_uses_a_square_high_quality_source():
    cover = CardBrief(
        card_id="thumbnail",
        card_type="THUMBNAIL",
        article_claim="블로그 자동화 실전 가이드",
        visual_purpose="글의 주제를 한눈에 전달",
        scene=CardScene(
            main_subject="a focused creator at a desk",
            action="reviewing a draft",
            setting="a calm Korean home office",
        ),
        necessity_score=100,
    )
    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": model_photo()}]})
        )
        image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
            build_input(
                card=cover,
                is_thumbnail=True,
                thumbnail_copy=["대표 문구"],
            )
        )

    payload = json.loads(route.calls[0].request.content)
    assert payload["size"] == COVER_IMAGE_SIZE
    assert payload["quality"] == COVER_IMAGE_QUALITY
    assert size_of(image.data_url) == (CANVAS_WIDTH, CANVAS_HEIGHT)
    assert "720x720 square" in image.prompt
    assert "quiet, uncluttered background" in image.prompt or "centre of the frame" in image.prompt
    assert "do not draw a panel, banner" in image.prompt or "Do not reserve blank space" in image.prompt


# --- 참고 이미지 기반 생성(image-to-image) ---

EDIT_ENDPOINT = "https://api.openai.com/v1/images/edits"


def _reference_data_url() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (30, 120, 200)).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


async def test_reference_image_uses_the_edits_endpoint():
    """참고 이미지가 실리면 일반 생성이 아니라 image-to-image(편집) 엔드포인트로 생성한다."""
    with respx.mock:
        generations = respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": model_photo()}]})
        )
        edits = respx.post(EDIT_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": model_photo()}]})
        )
        image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
            build_input(content_prompt="a desk", reference_image=_reference_data_url())
        )

    assert edits.call_count == 1
    assert generations.call_count == 0  # 편집 경로로 갔으므로 일반 생성은 호출되지 않는다
    assert size_of(image.data_url) == (BODY_WIDTH, BODY_HEIGHT)
    # 멀티파트로 참고 이미지 파일이 실렸다.
    assert b"reference.png" in edits.calls[0].request.content


async def test_unreadable_reference_falls_back_to_generation():
    """참고 이미지를 열 수 없으면 일반 텍스트→이미지 생성으로 되돌아간다(생성을 막지 않는다)."""
    bad_reference = "data:image/png;base64," + base64.b64encode(b"not an image").decode()
    with respx.mock:
        generations = respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": model_photo()}]})
        )
        edits = respx.post(EDIT_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": model_photo()}]})
        )
        image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
            build_input(content_prompt="a desk", reference_image=bad_reference)
        )

    assert generations.call_count == 1
    assert edits.call_count == 0
    assert size_of(image.data_url) == (BODY_WIDTH, BODY_HEIGHT)


# --- 일시적 과부하 재시도(_post_json) ---
# gemini·anthropic·openai가 수요 급증 때 내는 5xx/429는 결정적 실패가 아니라 재시도로 넘겨야
# 한다. 실패는 검증·원고·이미지가 공유하는 _post_json에서 나므로 여기서 대표로 검증한다.


@pytest.fixture(autouse=True)
def _instant_retry(monkeypatch):
    """재시도 사이 백오프 대기를 없앤다 — 테스트가 실제 초 단위로 자지 않게."""

    async def _no_sleep(attempt, retry_after):
        return None

    monkeypatch.setattr("app.llm.live_adapters._sleep_before_retry", _no_sleep)


async def test_a_transient_500_is_retried_and_then_succeeds():
    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            side_effect=[
                httpx.Response(
                    500, json={"error": {"message": "high demand", "code": "api_error"}}
                ),
                httpx.Response(200, json={"data": [{"b64_json": model_photo()}]}),
            ]
        )
        image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
            build_input(content_prompt="a desk")
        )

    assert route.call_count == 2  # 500 한 번, 성공 한 번
    assert size_of(image.data_url) == (BODY_WIDTH, BODY_HEIGHT)


async def test_a_dropped_connection_is_retried_and_names_what_broke(caplog):
    """회선이 끊기면 httpx 예외가 **메시지 없이** 올라온다.

    그대로 찍으면 로그가 "provider 연결 오류 (1/4) - 재시도: "에서 끝나 원인을 알 수
    없었다(사용자 보고 2026-08-11). 종류 이름과 어느 provider인지는 언제나 남아야 한다.
    """
    import logging

    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            side_effect=[
                httpx.ReadError(""),  # 메시지 없는 연결 끊김 — 실제로 이렇게 올라온다
                httpx.Response(200, json={"data": [{"b64_json": model_photo()}]}),
            ]
        )
        with caplog.at_level(logging.WARNING, logger="app.llm.live_adapters"):
            image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
                build_input(content_prompt="a desk")
            )

    assert route.call_count == 2
    assert size_of(image.data_url) == (BODY_WIDTH, BODY_HEIGHT)
    [record] = [r for r in caplog.records if "provider 연결 오류" in r.getMessage()]
    message = record.getMessage()
    assert "ReadError" in message  # 무엇이 끊겼는지
    assert "openai" in message  # 어느 provider인지
    assert not message.rstrip().endswith("재시도:")


async def test_an_html_503_is_checked_before_json_and_retried():
    """프록시 HTML 오류는 JSONDecodeError가 아니라 5xx 재시도 경로를 타야 한다."""
    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            side_effect=[
                httpx.Response(503, text="<html>temporarily unavailable</html>"),
                httpx.Response(200, json={"data": [{"b64_json": model_photo()}]}),
            ]
        )
        image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
            build_input(content_prompt="a desk")
        )

    assert route.call_count == 2
    assert size_of(image.data_url) == (BODY_WIDTH, BODY_HEIGHT)


async def test_a_stale_schema_rejecting_custom_size_falls_back_once():
    """공식 규격을 모르는 중간 API schema에서도 표준 가로 규격으로 계속 생성한다."""
    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            side_effect=[
                httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Invalid value for size",
                            "param": "size",
                            "code": "invalid_value",
                        }
                    },
                ),
                httpx.Response(200, json={"data": [{"b64_json": model_photo()}]}),
            ]
        )
        image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
            build_input(content_prompt="a desk")
        )

    sizes = [json.loads(call.request.content)["size"] for call in route.calls]
    assert sizes == [BODY_IMAGE_SIZE, LEGACY_LANDSCAPE_IMAGE_SIZE]
    assert size_of(image.data_url) == (BODY_WIDTH, BODY_HEIGHT)


async def test_the_final_body_jpeg_is_not_encoded_again_when_stored():
    image = await generate(build_input(content_prompt="a desk"))
    original, original_mime = data_url_parts(image.data_url)

    stored, stored_mime = shrink(image.data_url)

    assert (stored, stored_mime) == (original, original_mime)


async def test_repeated_overload_eventually_fails_after_exhausting_retries():
    from app.llm.live_adapters import MAX_REQUEST_ATTEMPTS, LiveAdapterError

    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            return_value=httpx.Response(529, json={"error": {"message": "overloaded"}})
        )
        with pytest.raises(LiveAdapterError):
            await OpenAiPostImageGenerator(ROLE).generate_post_image(build_input())

    # 한도만큼 시도하고 나서야 포기한다(무한 재시도가 아니다).
    assert route.call_count == MAX_REQUEST_ATTEMPTS


async def test_a_deterministic_401_is_not_retried():
    from app.llm.live_adapters import LiveAdapterError

    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            return_value=httpx.Response(401, json={"error": {"message": "invalid api key"}})
        )
        with pytest.raises(LiveAdapterError):
            await OpenAiPostImageGenerator(ROLE).generate_post_image(build_input())

    # 잘못된 키는 재시도해도 소용없으므로 곧바로 실패한다.
    assert route.call_count == 1


async def test_a_named_character_stays_the_subject_of_the_thumbnail_request():
    """소재가 고유 캐릭터면 어댑터가 실제로 보내는 프롬프트에 그 캐릭터가 못 박혀 있다."""
    cover = await generate(
        build_input(
            input=BlogTaskInput(topic="스파이더맨", keywords=["스파이더맨"]),
            is_thumbnail=True,
            thumbnail_copy=["스파이더맨"],
            subject_identity="Spider-Man",
            subject_kind="FICTIONAL_CHARACTER",
            must_show_subject=True,
        )
    )

    assert "The primary named subject is exactly: Spider-Man" in cover.prompt
    assert "generic superhero" in cover.prompt
    assert "cityscape" in cover.prompt
    # 캐릭터 식별에 필요한 비문자형 문양은 남기되, 문자·로고·워터마크는 계속 금지한다.
    assert "chest emblem" in cover.prompt
    assert "No readable text" in cover.prompt
    assert "no studio or publisher logos" in cover.prompt


async def test_a_named_real_person_is_not_swapped_for_a_generic_professional():
    body = await generate(
        build_input(
            input=BlogTaskInput(topic="손흥민", keywords=["손흥민"]),
            content_prompt="a footballer on a training pitch",
            subject_identity="Son Heung-min 손흥민",
            subject_kind="REAL_NAMED_PERSON",
            must_show_subject=True,
        )
    )

    assert "The named real person is exactly: Son Heung-min 손흥민" in body.prompt
    assert "generic person with the same occupation" in body.prompt
    assert "awards, trophies, matches" in body.prompt
    # 사람에게 제품용 충실도 문구를 쓰지 않는다.
    assert "same colour, same silhouette" not in body.prompt


async def test_a_generic_role_subject_keeps_the_previous_prompt():
    body = await generate(
        build_input(
            input=BlogTaskInput(topic="헬스 트레이너", keywords=["헬스"]),
            content_prompt="a trainer correcting a squat",
            subject_kind="GENERIC_PERSON_ROLE",
        )
    )

    assert "The primary named subject" not in body.prompt
    assert "No readable text, letters, numbers, logos" in body.prompt


def web_photo(width: int = 1600, height: int = 900, host: str = "news.example") -> WebPhoto:
    """웹에서 찾아온 실제 사진. 모델 출력과 구분되게 다른 색으로 만든다."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 90, 200)).save(buffer, format="JPEG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return WebPhoto(
        data_url=f"data:image/jpeg;base64,{encoded}",
        source_url=f"https://{host}/photo.jpg",
        source_host=host,
        title="사진 제목",
        width=width,
        height=height,
        query="프로미스나인 백지헌",
    )


async def test_a_web_photo_replaces_the_generation_call_entirely():
    """실존 인물 자리에는 생성물이 아니라 사진이 들어간다. 모델을 부르지 않는 것이 핵심이다 —
    부르면 '닮은 남'이 돌아온다."""
    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": model_photo()}]})
        )
        image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
            build_input(content_prompt="a stage", web_photo=web_photo())
        )

    assert route.call_count == 0
    assert image.source == "web"
    assert image.provider == "web-photo"
    assert image.model == "news.example"
    # 규격 맞추기는 생성 경로와 똑같이 한다.
    assert size_of(image.data_url) == (BODY_WIDTH, BODY_HEIGHT)


async def test_a_web_photo_carries_its_source_caption():
    """웹에서 가져온 사진은 출처를 캡션으로 싣는다(2026-08-10 사용자 지시 — 네이버
    이미지 출처 표기 규칙. 2026-07-31/08-03의 '자동 표기 안 함' 결정을 대체한다).
    캡션은 네이버 발행 시 사진 캡션 필드에 그대로 들어간다."""
    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(content_prompt="a stage", web_photo=web_photo(host="press.example"))
    )

    assert image.caption == "출처: press.example"


async def test_a_youtube_thumbnail_names_the_channel_and_video(  # noqa: D103
):
    """유튜브 썸네일은 채널명과 원본 영상 주소까지 적는다 — '링크 첨부가 더 확실하다'는
    표기 규칙을 따를 수 있는 유일한 웹 출처다(source_url이 watch 주소)."""
    from app.shared import WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL

    photo = web_photo(host="i.ytimg.com").model_copy(
        update={
            "source_type": WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL,
            "channel_title": "채널A 뉴스",
            "source_url": "https://www.youtube.com/watch?v=abc123",
        }
    )
    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(content_prompt="a stage", web_photo=photo)
    )

    assert image.caption == "출처: YouTube 채널A 뉴스 — https://www.youtube.com/watch?v=abc123"
    # 주소를 괄호로 감싸지 않는다 — 자동 링크가 닫는 괄호를 삼켜 500이 난다(2026-08-11).
    assert not image.caption.endswith(")")


async def test_the_caption_names_the_origin_not_the_cdn():
    """CDN 호스트가 아니라 되찾은 실제 원본 출처를 적는다(2026-08-11 사용자 지시).

    실측(저장된 글): '출처: imgnews.naver.net'이 그대로 실려 있었다 — 파일 서버 이름이라
    독자가 그것으로 원본을 찾아갈 수 없다. photo_search가 되찾아 둔 언론사명과 기사
    주소가 있으면 그것이 캡션이다.
    """
    photo = web_photo(host="imgnews.naver.net").model_copy(
        update={
            "source_name": "연합뉴스",
            "source_page_url": "https://n.news.naver.com/article/001/0015000000",
        }
    )
    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(content_prompt="a stage", web_photo=photo)
    )

    assert image.caption == "출처: 연합뉴스 — https://n.news.naver.com/article/001/0015000000"
    assert "imgnews" not in image.caption


async def test_the_source_url_is_the_last_thing_in_the_caption():
    """주소 뒤에 글자가 붙으면 사용자가 클릭했을 때 링크가 깨진다(2026-08-11 사용자 신고).

    옛 형식은 `출처: 뉴스1 (https://n.news.naver.com/article/421/0007909336)`이었다.
    캡션을 자동으로 링크로 바꾸는 쪽이 닫는 괄호까지 주소에 넣으면 네이버가 **500과
    "페이지를 찾을 수 없습니다"**를 돌려준다(실측으로 재현 — 괄호 없는 같은 주소는 200).
    카드 설명이 함께 실릴 때도 주소는 맨 끝이어야 한다.
    """
    photo = web_photo(host="imgnews.naver.net").model_copy(
        update={
            "source_name": "뉴스1",
            "source_page_url": "https://n.news.naver.com/article/421/0007909336",
        }
    )
    card = CardBrief(
        card_id="c1",
        card_type="SECTION_CARD",
        section_id="section-1",
        article_claim="본문에 실제로 있는 문장",
        visual_purpose="장면",
        scene=CardScene(
            main_subject="a reporter at a desk",
            action="reviewing notes",
            setting="a newsroom",
        ),
        necessity_score=90,
        caption="2024년 11월 기준",
    )
    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(content_prompt="a stage", web_photo=photo, card=card)
    )

    assert image.caption == (
        "2024년 11월 기준 · 출처: 뉴스1 — https://n.news.naver.com/article/421/0007909336"
    )
    assert image.caption.endswith("0007909336")  # 주소가 끝이다
    assert "(https" not in image.caption  # 괄호로 감싸지 않는다


async def test_a_site_without_a_recovered_page_prints_only_the_site():
    """원본 페이지를 못 되찾은 사진은 사이트 이름만 적는다 — 없는 주소를 지어내지 않는다."""
    photo = web_photo(host="i2.ruliweb.com").model_copy(
        update={"source_name": "ruliweb.com"}
    )
    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(content_prompt="a stage", web_photo=photo)
    )

    assert image.caption == "출처: ruliweb.com"


async def test_a_web_photo_keeps_its_original_image_url():
    """원고 복사가 로컬 서버 주소 대신 쓸 **원본 이미지 주소**를 저장한다(2026-08-10).

    로컬 주소는 이 PC에서만 열린다 — 네이버 에디터가 서버에서 이미지를 끌어갈 때도,
    벨로그·티스토리에 붙여넣을 때도 죽은 링크였다('존재하지 않는 이미지입니다' 실사례).
    """
    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(content_prompt="a stage", web_photo=web_photo(host="press.example"))
    )

    assert image.source_url == web_photo(host="press.example").source_url


async def test_a_youtube_thumbnail_stores_the_image_url_not_the_watch_page():
    """유튜브의 source_url은 영상(watch) 주소라 <img src>로 못 쓴다 — 내려받을 때 쓴
    i.ytimg 이미지 주소를 video_id로 되만들어 저장한다."""
    from app.shared import WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL

    photo = web_photo(host="i.ytimg.com").model_copy(
        update={
            "source_type": WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL,
            "channel_title": "채널A 뉴스",
            "source_url": "https://www.youtube.com/watch?v=abc123",
            "video_id": "abc123",
        }
    )
    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(content_prompt="a stage", web_photo=photo)
    )

    assert image.source_url == "https://i.ytimg.com/vi/abc123/maxresdefault.jpg"


async def test_a_generated_image_has_no_source_url():
    """생성 이미지는 원본이 없다 — None이어야 복사가 로컬 엔드포인트로 폴백한다."""
    with respx.mock:
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": model_photo()}]})
        )
        image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
            build_input(content_prompt="a stage")
        )

    assert image.source_url is None


async def test_a_card_caption_still_survives_on_a_web_photo():
    """카드가 준비한 설명(기준 시점 등)은 웹 사진에서도 그대로 남고, 출처가 그 뒤에
    붙는다."""
    card = CardBrief(
        card_id="cover",
        card_type="THUMBNAIL",
        article_claim="주장",
        visual_purpose="목적",
        scene=CardScene(main_subject="s", action="a", setting="t"),
        alt_text="대체 텍스트",
        necessity_score=100,
        caption="2026년 7월 기준",
    )
    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(card=card, web_photo=web_photo(host="press.example"))
    )

    assert image.caption == "2026년 7월 기준 · 출처: press.example"


@pytest.mark.skipif(font_path() is None, reason="no Korean font on this machine")
async def test_the_thumbnail_copy_still_lands_on_a_web_photo():
    """사진을 가져와도 한글 문구를 얹는 것은 그대로 우리 몫이다."""
    cover = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(
            is_thumbnail=True,
            thumbnail_copy=["이름은 그대로", "달라진 건 인원"],
            web_photo=web_photo(),
        )
    )

    assert size_of(cover.data_url) == (CANVAS_WIDTH, CANVAS_HEIGHT)
    assert "이름은 그대로 달라진 건 인원" in cover.alt_text
    assert cover.source == "web"


async def test_an_unreadable_web_photo_falls_back_to_generation():
    """사진을 열 수 없다고 원고를 버리지 않는다 — 예전 경로로 되돌아간다."""
    broken = web_photo().model_copy(update={"data_url": "https://example.com/not-a-data-url"})
    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": model_photo()}]})
        )
        image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
            build_input(content_prompt="a stage", web_photo=broken)
        )

    assert route.call_count == 1
    assert image.source == "generated"


async def test_an_unreadable_user_reference_never_falls_back_to_generation():
    """REUSED 원본이 깨졌다면 로고·문구가 다른 임의 생성물로 대체하지 않는다."""
    broken = web_photo(host="user-reference").model_copy(
        update={
            "data_url": "https://example.com/not-a-data-url",
            "source_url": "user-upload://reference-image-1",
        }
    )
    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": model_photo()}]})
        )
        with pytest.raises(UnsafeImageError):
            await OpenAiPostImageGenerator(ROLE).generate_post_image(
                build_input(content_prompt="a package", web_photo=broken)
            )

    assert route.call_count == 0


async def test_a_web_photo_of_a_person_is_not_cropped_at_all():
    """보도 사진은 세로로 길고 얼굴이 위쪽에 있다. 가운데를 자르면 눈이 잘려 나간다.

    2026-08-05에는 위쪽을 남기는 크롭(FACE_CROP)으로 얼굴을 지켰다. 2026-08-13부터는
    **웹 사진을 아예 자르지 않는다**(사용자 지시: "워터마크가 짤리잖아"). 얼굴도 언론사
    로고도 프레임 가장자리의 문구도 그대로 남는다.
    """
    from app.llm.imaging import CENTER_CROP, to_canvas

    # 행마다 색이 다른 세로 사진. 단색이면 어디를 잘라도 결과가 같아 비교가 되지 않는다.
    source = Image.new("RGB", (1200, 1642))
    pixels = source.load()
    for y in range(1642):
        for x in range(1200):
            pixels[x, y] = (y * 255 // 1642, 90, 200)
    tall = io.BytesIO()
    source.save(tall, format="JPEG", quality=95)
    raw = tall.getvalue()
    photo = WebPhoto(
        data_url="data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
        source_url="https://press.example/p.jpg",
        source_host="press.example",
        width=1200,
        height=1642,
    )

    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(
            content_prompt="a stage",
            web_photo=photo,
            subject_kind="REAL_NAMED_PERSON",
            subject_identity="백지헌",
            must_show_subject=True,
        )
    )
    rendered = base64.b64decode(image.data_url.split(",", 1)[1])

    assert rendered == to_canvas(raw, CENTER_CROP, contain=True)
    # 잘린 결과와 달라야 한다 — 같으면 보존이 걸리지 않은 것이다.
    assert rendered != to_canvas(raw, CENTER_CROP)
    assert size_of(image.data_url) == (BODY_WIDTH, BODY_HEIGHT)


def _tall_product_photo() -> tuple[bytes, WebPhoto]:
    """세로로 긴 제품 상세컷(1000×1500). 쇼핑몰 사진의 흔한 비율이다.

    행마다 색이 다르다 — 단색이면 어디를 잘라도 결과가 같아 비교가 되지 않는다.
    """
    source = Image.new("RGB", (1000, 1500))
    pixels = source.load()
    for y in range(1500):
        for x in range(1000):
            pixels[x, y] = (y * 255 // 1500, 90, 200)
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG", quality=95)
    raw = buffer.getvalue()
    return raw, WebPhoto(
        data_url="data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
        source_url="https://shop.example/bag.jpg",
        source_host="shop.example",
        title="레이디 디올 미디엄 백",
        query="레이디 디올 핸드백",
        width=1000,
        height=1500,
    )


async def test_a_web_photo_of_a_product_is_not_cropped_from_the_top():
    """제품 사진에 얼굴 크롭을 쓰면 가방은 손잡이만, 버거는 포장지 윗면만 남는다.

    실제로 디올 글에서 손잡이만 크게 실린 사진이 나왔다 — 얼굴이 없는 사진에서 위쪽
    22%만 남길 이유가 없다.
    """
    from app.llm.imaging import FACE_CROP, to_canvas

    raw, photo = _tall_product_photo()
    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(content_prompt="a handbag on a table", web_photo=photo)
    )
    rendered = base64.b64decode(image.data_url.split(",", 1)[1])

    assert rendered != to_canvas(raw, FACE_CROP)


async def test_a_tall_product_photo_keeps_the_whole_object():
    """비율을 지키느라 대상의 전체 형태를 잃을 만큼 잘라야 하면 자르지 않는다.

    1000×1500을 16:9로 자르면 세로의 59%가 사라진다 — 가방 몸통이 프레임 밖으로 나가는
    바로 그 크기다. 그런 사진은 비율을 보존하고 남는 자리를 흐린 배경으로 메운다.
    """
    from app.llm.imaging import CENTER_CROP, to_canvas

    raw, photo = _tall_product_photo()
    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(content_prompt="a handbag on a table", web_photo=photo)
    )
    rendered = base64.b64decode(image.data_url.split(",", 1)[1])

    assert rendered == to_canvas(raw, CENTER_CROP, contain=True)
    assert rendered != to_canvas(raw, CENTER_CROP)
    # 규격은 그대로다 — 잘리지 않을 뿐 크기가 달라지지는 않는다.
    assert size_of(image.data_url) == (BODY_WIDTH, BODY_HEIGHT)


async def test_even_a_detail_card_keeps_the_whole_web_photo():
    """부분 확대를 계획한 카드도 자르지 않는다(2026-08-13).

    예전에는 이 카드만 예외로 두고 잘랐다 — '그 사진은 잘려도 되는 사진'이라는 이유였다.
    그런데 남의 사진에는 가장자리에 워터마크·로고가 구워져 있고, 계획이 부분 확대를
    바라는지와 그것을 지워도 되는지는 다른 질문이다. 확대는 우리가 만든 이미지에서 한다.
    """
    from app.llm.imaging import CENTER_CROP, to_canvas

    raw, photo = _tall_product_photo()
    detail = CardBrief(
        card_id="photo-2",
        card_type="SECTION_CARD",
        article_claim="가죽 표면의 카나주 퀼팅이 촘촘하다",
        visual_purpose="퀼팅 결의 촘촘함",
        scene=CardScene(main_subject="quilted leather surface"),
        photo_role="PRODUCT_DETAIL",
        framing="CLOSE_UP",
    )
    assert detail.show_complete_subject is False

    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(card=detail, web_photo=photo)
    )
    rendered = base64.b64decode(image.data_url.split(",", 1)[1])

    assert rendered == to_canvas(raw, CENTER_CROP, contain=True)
    assert rendered != to_canvas(raw, CENTER_CROP)


async def test_a_web_photo_does_not_pretend_to_have_been_generated():
    """생성 프롬프트를 그대로 남기면 기록이 거짓말을 한다 — 이 사진은 그것으로 만들어진
    것이 아니다."""
    image = await OpenAiPostImageGenerator(ROLE).generate_post_image(
        build_input(content_prompt="a stage", web_photo=web_photo())
    )

    assert "프로미스나인 백지헌" in image.prompt
    assert "https://news.example/photo.jpg" in image.prompt
    assert "a stage" not in image.prompt
