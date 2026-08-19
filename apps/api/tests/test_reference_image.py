"""참고자료 이미지를 Anthropic이 받는 형태로 만드는 imaging.prepare_anthropic_image.

핵심: 선언된 mime이 아니라 실제 바이트로 형식을 판별하고(잘못 붙은 mime이 400을 냈다),
네이티브 4종(JPEG·PNG·GIF·WebP)은 그대로, 그 외는 PNG로 변환해 '모든 이미지 형식'을 받는다.
"""

import base64
import io

from app.llm.imaging import prepare_anthropic_image, to_edit_input_png
from PIL import Image, PngImagePlugin


def _b64(image: Image.Image, fmt: str) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _sample(mode: str = "RGB", size=(32, 24)) -> Image.Image:
    return Image.new(mode, size, (123, 200, 60))


def test_native_jpeg_is_reencoded_without_metadata_even_when_small():
    data = _b64(_sample(), "JPEG")
    media_type, encoded = prepare_anthropic_image(data)
    assert media_type == "image/jpeg"
    assert encoded != data
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        assert image.format == "JPEG"
        assert image.getexif() == {}


def test_native_png_is_reencoded_without_metadata_even_when_small():
    data = _b64(_sample("RGBA"), "PNG")
    media_type, encoded = prepare_anthropic_image(data)
    assert media_type == "image/png"
    assert encoded != data
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        assert image.format == "PNG"
        assert image.size == (32, 24)


def test_native_webp_is_detected_even_if_declared_as_jpeg():
    """실측 사고 재현: WebP 바이트가 image/jpeg로 붙어 와도, 바이트로 판별해 webp로 보낸다."""
    data = _b64(_sample(), "WEBP")
    media_type, encoded = prepare_anthropic_image(data)
    assert media_type == "image/webp"
    assert base64.b64decode(encoded)[:4] == b"RIFF"


def test_native_gif_is_flattened_to_metadata_free_png():
    data = _b64(_sample("P"), "GIF")
    media_type, encoded = prepare_anthropic_image(data)
    assert media_type == "image/png"
    assert base64.b64decode(encoded)[:8] == b"\x89PNG\r\n\x1a\n"


def test_non_native_bmp_is_converted_to_png():
    """Anthropic이 못 받는 형식(BMP)은 PNG로 변환해 '모든 이미지'를 지원한다."""
    data = _b64(_sample(), "BMP")
    media_type, encoded = prepare_anthropic_image(data)
    assert media_type == "image/png"
    # 변환된 바이트는 실제 PNG여야 한다.
    raw = base64.b64decode(encoded)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    with Image.open(io.BytesIO(raw)) as converted:
        assert converted.format == "PNG"


def test_non_native_tiff_is_converted_to_png():
    data = _b64(_sample(), "TIFF")
    media_type, encoded = prepare_anthropic_image(data)
    assert media_type == "image/png"
    assert base64.b64decode(encoded)[:8] == b"\x89PNG\r\n\x1a\n"


def test_oversized_image_is_downscaled_on_conversion():
    """변환 경로는 긴 변을 1568px로 줄여 용량·토큰을 아낀다."""
    data = _b64(_sample(size=(4000, 3000)), "PNG")
    _media_type, encoded = prepare_anthropic_image(data)
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as converted:
        assert max(converted.size) <= 1568


def test_oversized_native_jpeg_is_also_downscaled():
    """네이티브라고 4K 사진을 그대로 보내지 않는다 — 입력 토큰·업로드를 함께 줄인다."""
    data = _b64(_sample(size=(4000, 3000)), "JPEG")
    media_type, encoded = prepare_anthropic_image(data)
    assert media_type == "image/jpeg"
    assert encoded != data
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as converted:
        assert max(converted.size) <= 1568


def test_undecodable_data_returns_none():
    """열 수 없는 데이터는 None — 첨부만 건너뛰고 생성은 계속한다."""
    assert prepare_anthropic_image(base64.b64encode(b"not an image").decode()) is None


def test_invalid_base64_returns_none():
    assert prepare_anthropic_image("!!!not base64!!!") is None


def test_exif_orientation_is_applied_and_all_exif_is_removed():
    source = Image.new("RGB", (40, 20), (20, 80, 160))
    exif = Image.Exif()
    exif[274] = 6  # 90° clockwise
    exif[270] = "private camera note"
    buffer = io.BytesIO()
    source.save(buffer, "JPEG", exif=exif)

    media_type, encoded = prepare_anthropic_image(
        base64.b64encode(buffer.getvalue()).decode("ascii")
    )

    assert media_type == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as clean:
        assert clean.size == (20, 40)
        assert clean.getexif() == {}
        assert "exif" not in clean.info


def test_png_text_chunks_are_removed_before_provider_upload():
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private", "secret value")
    buffer = io.BytesIO()
    _sample("RGBA").save(buffer, "PNG", pnginfo=metadata)

    _media_type, encoded = prepare_anthropic_image(
        base64.b64encode(buffer.getvalue()).decode("ascii")
    )

    with Image.open(io.BytesIO(base64.b64decode(encoded))) as clean:
        assert "private" not in clean.info


def test_edit_input_is_oriented_and_metadata_free_png():
    source = Image.new("RGB", (40, 20), (20, 80, 160))
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "do not upload"
    buffer = io.BytesIO()
    source.save(buffer, "JPEG", exif=exif)

    clean_raw = to_edit_input_png(base64.b64encode(buffer.getvalue()).decode("ascii"))

    assert clean_raw is not None
    with Image.open(io.BytesIO(clean_raw)) as clean:
        assert clean.format == "PNG"
        assert clean.size == (20, 40)
        assert clean.getexif() == {}
        assert "exif" not in clean.info


def test_pixel_bomb_is_rejected_before_provider_upload(monkeypatch):
    monkeypatch.setattr("app.shared.image_bytes.MAX_SOURCE_IMAGE_PIXELS", 100)
    data = _b64(_sample(size=(20, 20)), "PNG")

    assert prepare_anthropic_image(data) is None
    assert to_edit_input_png(data) is None


def test_provider_reference_path_uses_the_public_image_normalizer(monkeypatch):
    calls = []

    def normalize(raw, *, max_edge, output_format):
        calls.append((raw, max_edge, output_format))
        return b"metadata-free", "image/jpeg"

    monkeypatch.setattr("app.llm.imaging.normalize_image_bytes", normalize)

    media_type, encoded = prepare_anthropic_image(
        base64.b64encode(b"external reference").decode("ascii")
    )

    assert media_type == "image/jpeg"
    assert base64.b64decode(encoded) == b"metadata-free"
    assert calls == [(b"external reference", 1568, "provider")]


def test_edit_reference_path_uses_the_public_image_normalizer(monkeypatch):
    calls = []

    def normalize(raw, *, max_edge, output_format):
        calls.append((raw, max_edge, output_format))
        return b"clean-png", "image/png"

    monkeypatch.setattr("app.llm.imaging.normalize_image_bytes", normalize)

    cleaned = to_edit_input_png(base64.b64encode(b"reference").decode("ascii"))

    assert cleaned == b"clean-png"
    assert calls == [(b"reference", 1024, "png")]


# --- 카드 계획의 usesReferenceImage → CardBrief.uses_reference 파싱 ---


def _card(card_id: str, uses_reference):
    card = {
        "cardId": card_id,
        "cardType": "SECTION_CARD",
        "articleClaim": "메챠카멜레온은 배경에 숨는다.",
        "scene": {"mainSubject": "메챠카멜레온"},
        "necessityScore": 80,
    }
    if uses_reference is not None:
        card["usesReferenceImage"] = uses_reference
    return card


def test_card_plan_parses_uses_reference_flag():
    """카드뉴스 문구가 없는 신규 사진 계획도 기존 저장 모델로 파싱된다."""
    from app.llm.parsing import card_plan_from_json

    plan = card_plan_from_json(
        {
            "designSystem": {},
            "cards": [
                _card("card-1", True),
                _card("card-2", False),
                _card("card-3", None),  # 플래그 누락 → 안전하게 False
            ],
        }
    )
    assert [card.uses_reference for card in plan.cards] == [True, False, False]
    assert all(card.headline_lines == [] for card in plan.cards)


def test_new_photo_plan_schema_does_not_request_card_news_fields():
    from app.llm.schemas import CARD_PLAN_SCHEMA

    assert CARD_PLAN_SCHEMA["required"] == ["cards"]
    assert "designSystem" not in CARD_PLAN_SCHEMA["properties"]
    card_schema = CARD_PLAN_SCHEMA["properties"]["cards"]["items"]
    assert "headlineLines" not in card_schema["properties"]
    assert "eyebrow" not in card_schema["properties"]
