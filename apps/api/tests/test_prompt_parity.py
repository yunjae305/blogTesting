"""Characterization test: 한국어 프롬프트는 한 글자도 바뀌면 안 된다.
실제로 나가는 요청 본문을 고정 fixture와 바이트 단위로 비교한다. If a prompt string drifts during
or after the port, the model's output drifts with it — silently. This is the
only thing that catches that.
"""

import json
from pathlib import Path

import httpx
import respx

from app.llm.live_adapters import AnthropicDraftGenerator
from app.llm.provider_config import LlmProvider, LlmRole, RoleConfig
from app.shared import (
    BlogTaskInput,
    DraftFormat,
    DraftGenerationInput,
    DraftGenerationSettings,
    ReferenceMaterial,
    ReferenceMaterialType,
    SearchSource,
    SelectedIntentForDraft,
)

FIXTURES = Path(__file__).parent / "fixtures"

ROLE = RoleConfig(
    role=LlmRole.M4_DRAFT,
    label="M4 draft generation",
    provider=LlmProvider.ANTHROPIC,
    model="claude-opus-5",
    api_key_env="ANTHROPIC_API_KEY",
    api_key="test-key",
    has_credentials=True,
)

# Must mirror the input in scripts/dump_node_prompt.mjs exactly.
DRAFT_INPUT = DraftGenerationInput(
    post_id="post_1",
    user_id="user_1",
    prompt_version="m4-draft@v1.0",
    format=DraftFormat.MARKDOWN,
    style="짧은 문장",
    input=BlogTaskInput(
        topic="AIONA",
        subject="IT·디지털",
        purpose=["후기·리뷰 작성"],
        keywords=["후기·리뷰 작성"],
        target_reader="실무자",
        reader_age_range="30s",
        reader_knowledge_level="중급",
        reference_materials=[
            ReferenceMaterial(type=ReferenceMaterialType.URL, value="https://aiona.kr/"),
            ReferenceMaterial(type=ReferenceMaterialType.TEXT, value="메모 내용"),
        ],
    ),
    selected_intent=SelectedIntentForDraft(
        intent_id="i1",
        title="AIONA 실무 가이드",
        target_reader="실무자",
        rationale="실무 적용 관점",
        sources=[SearchSource(title="출처1", url="https://example.com/1", snippet="요약1")],
    ),
    settings=DraftGenerationSettings(
        hashtag_count=7,
        default_persona="페르소나",
        custom_persona_name="이름",
        custom_persona_description="설명",
        custom_persona="커스텀",
    ),
)

TOOL_RESPONSE = {
    "content": [
        {
            "type": "tool_use",
            "name": "return_blog_draft",
            "input": {
                "finalPost": {
                    "title": "T",
                    "body": "B",
                    "hashtags": ["a"],
                    "htmlContent": "<p>B</p>",
                    "markdownContent": "# T",
                }
            },
        }
    ]
}


async def _capture_request_body() -> dict:
    with respx.mock:
        route = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json=TOOL_RESPONSE)
        )
        await AnthropicDraftGenerator(ROLE).generate_draft(DRAFT_INPUT)
        return json.loads(route.calls[0].request.content)


async def test_draft_prompt_matches_the_node_original_exactly():
    # 이제는 완전한 node 원본이 아니다: 본문 길이 지시 한 줄은 의도적으로 바꿨다
    # (미팅 3.17 "보통 2,000자가 짧다" → 길이를 범위로). AI 티가 나는 문체를 줄이려고
    # 문장 리듬·절제된 헤지 표현·기준 시점 표기·정형화된 결론 문구 금지 지시도 추가했다
    # (2026-07-24). 2026-07-28에는 고정 골격("현재 상황 → 불편 → 질문 → 소재 소개")을
    # 아키타입별 구조로 바꾸고, 사람다운 문장 리듬 규칙·페르소나 표현 강도·참고자료 근거
    # 블록을 넣었다 — 소재만 달라지고 글의 골격은 반복되던 문제를 프롬프트 층에서 끊는다.
    # 2026-08-05 미팅 점검표로 셋을 더했다: AI 답변형 문구 금지(7번), 연령대 관심축과
    # 그것을 본문 구조까지 내리는 규칙(4·5번), 사용자 자료 우선순위(9번 — 이름이 겹치는
    # 소재에서 어느 대상이 맞는지 가르는 유일한 일반 규칙이다).
    # 2026-08-07: 연령대 지침을 통째로 바꿨다. 사용자가 고른 연령이 **읽는 사람**의
    # 나이인데 그것을 말하지 않아 모델이 화자의 나이로 읽었고, 제목에 '20대의 시각으로
    # 본 후기'가 나왔다(사용자 신고). 이제 누구의 나이인지 못 박고, 연령대별로 말투·
    # 문장 길이·예시 상황·설명 순서를 함께 준다.
    # 2026-08-07(2차): 종결 문체 유지 규칙을 넣었다. '같은 종결 어미 연속 금지'를 모델이
    # 문체 전환('~습니다'→'~요')으로 풀어 한 글 안에서 말투가 갈라졌다(사용자 신고).
    # 픽스처가 그 변경들을 반영하며, 이 테스트는 그 외 프롬프트가 원치 않게 바뀌는 것을
    # 계속 막는 스냅샷 가드다.
    body = await _capture_request_body()
    actual = body["messages"][0]["content"]
    expected = (FIXTURES / "node_draft_prompt.txt").read_text(encoding="utf-8").rstrip("\r\n")

    assert actual == expected


async def test_draft_does_not_resend_a_reference_image_after_evidence_extraction():
    """원본 이미지는 근거 추출에서만 보내고 원고 단계에서는 텍스트 프로필만 사용한다."""
    with_image = DRAFT_INPUT.model_copy(
        update={
            "input": DRAFT_INPUT.input.model_copy(
                update={
                    "reference_materials": [
                        *DRAFT_INPUT.input.reference_materials,
                        ReferenceMaterial(
                            type=ReferenceMaterialType.IMAGE,
                            name="private.png",
                            value=_image_data_url("PNG", "image/png"),
                        ),
                    ]
                }
            )
        }
    )
    with respx.mock:
        route = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json=TOOL_RESPONSE)
        )
        await AnthropicDraftGenerator(ROLE).generate_draft(with_image)
        body = json.loads(route.calls[0].request.content)

    assert isinstance(body["messages"][0]["content"], str)


async def test_draft_request_envelope_matches_the_node_original():
    """2026-07-30 Opus 5 전환으로 봉투가 바뀌었다: temperature·thinking이 빠지고
    output_config.effort가 들어왔다. fixture가 그 변경을 반영하며, 이 테스트는 그 밖의
    요청 옵션이 원치 않게 바뀌는 것을 계속 막는다."""
    body = await _capture_request_body()
    expected = json.loads((FIXTURES / "node_draft_body.json").read_text(encoding="utf-8"))

    assert body["model"] == expected["model"]
    assert body["max_tokens"] == expected["max_tokens"]
    assert body["output_config"] == expected["output_config"]
    assert body["system"] == expected["system"]
    assert body["tools"] == expected["tools"]
    assert body["tool_choice"] == expected["tool_choice"]
    # Opus 5는 이 세 필드를 400으로 거절한다. 없다는 것을 fixture가 아니라 여기서 못 박는다.
    assert "temperature" not in body
    assert "top_p" not in body
    assert "top_k" not in body
    # thinking은 기본 ON이므로 필드를 넣지 않는다(생략 = adaptive).
    assert "thinking" not in body


def _image_data_url(fmt: str, declared_mime: str) -> str:
    import base64
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buffer, format=fmt)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:{declared_mime};base64,{encoded}"


def test_image_reference_is_sent_as_an_anthropic_image_block():
    material = ReferenceMaterial(
        type=ReferenceMaterialType.IMAGE,
        name="screen.png",
        value=_image_data_url("PNG", "image/png"),
    )

    content = AnthropicDraftGenerator._message_content("prompt", [material])

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "prompt"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/png"


def test_photo_plan_can_limit_reference_context_to_the_first_image():
    materials = [
        ReferenceMaterial(
            type=ReferenceMaterialType.IMAGE,
            name=f"screen-{index}.png",
            value=_image_data_url("PNG", "image/png"),
        )
        for index in range(2)
    ]

    content = AnthropicDraftGenerator._message_content(
        "prompt", materials, max_images=1
    )

    assert isinstance(content, list)
    assert [part["type"] for part in content] == ["text", "image"]


def test_mislabeled_webp_is_sent_with_its_real_media_type():
    """실측 사고: WebP가 image/jpeg로 잘못 붙어 와도, 실제 바이트로 판별해 webp로 보낸다
    (그대로 image/jpeg로 보내면 Anthropic이 400으로 거절해 원고 생성이 죽었다)."""
    material = ReferenceMaterial(
        type=ReferenceMaterialType.IMAGE,
        name="photo.jpg",
        value=_image_data_url("WEBP", "image/jpeg"),
    )

    content = AnthropicDraftGenerator._message_content("prompt", [material])

    assert isinstance(content, list)
    assert content[1]["source"]["media_type"] == "image/webp"


def test_non_native_image_is_converted_and_sent_as_png():
    """Anthropic 비지원 형식(BMP)도 PNG로 변환해 첨부한다 — 모든 이미지 형식 지원."""
    material = ReferenceMaterial(
        type=ReferenceMaterialType.IMAGE,
        name="scan.bmp",
        value=_image_data_url("BMP", "image/bmp"),
    )

    content = AnthropicDraftGenerator._message_content("prompt", [material])

    assert isinstance(content, list)
    assert content[1]["source"]["media_type"] == "image/png"


def test_unreadable_image_is_skipped_not_fatal():
    """열 수 없는 이미지는 첨부만 생략하고 프롬프트는 그대로 나간다(생성을 막지 않는다)."""
    import base64

    material = ReferenceMaterial(
        type=ReferenceMaterialType.IMAGE,
        name="broken.png",
        value=f"data:image/png;base64,{base64.b64encode(b'not an image').decode()}",
    )

    content = AnthropicDraftGenerator._message_content("prompt", [material])

    # 첨부가 생략되면 content는 리스트가 아니라 프롬프트 문자열 그대로다.
    assert content == "prompt"
