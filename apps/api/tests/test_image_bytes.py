"""이미지를 저장할 모양으로 줄이고 되돌리는 것.

실측에서 출발했다(2026-08-06). 글 하나를 여는 데 오가는 것의 **85%가 이미지**였고
(이미지 970KB, 참고자료 144KB, 글자 104KB), 회선이 0.09MB/s라 그대로 대기 시간이었다.
이미지는 1200x675 JPEG, 장당 base64 204KB였다.
"""

import base64
import io

import pytest
from app.shared.image_bytes import (
    JPEG_QUALITY,
    MAX_IMAGE_WIDTH,
    data_url_parts,
    normalize_data_url,
    shrink,
    to_data_url,
)
from PIL import Image


def jpeg_data_url(width: int, height: int) -> str:
    """가로 width의 JPEG data URL. 단색이면 너무 잘 압축되므로 무늬를 넣는다."""
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for x in range(width):
        for y in range(height):
            pixels[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 3) % 256)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=95)
    return to_data_url(buffer.getvalue(), "image/jpeg")


def png_data_url(width: int, height: int) -> str:
    image = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    # 표·그래프와 비슷한 고대비 선을 넣는다.
    for x in range(0, width, 16):
        for y in range(height):
            image.putpixel((x, y), (10, 20, 30, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return to_data_url(buffer.getvalue(), "image/png")


class TestReadingADataUrl:
    def test_the_bytes_and_the_kind_come_apart(self):
        raw, mime = data_url_parts("data:image/png;base64,AAAA")

        assert raw == b"\x00\x00\x00"
        assert mime == "image/png"

    @pytest.mark.parametrize("value", ["", "https://example.com/a.png", "data:image/png"])
    def test_something_that_is_not_a_data_url_gives_no_bytes(self, value):
        """부르는 쪽이 '줄일 수 없는 것'으로 다룰 수 있어야 한다."""
        raw, _ = data_url_parts(value)

        assert raw == b""


class TestPuttingItBack:
    def test_the_data_url_looks_the_same_as_before(self):
        """발행 경로(네이버·스레드)에 넘어가는 값의 모양은 바뀌지 않는다."""
        assert to_data_url(b"\x00\x00\x00", "image/png") == "data:image/png;base64,AAAA"

    def test_a_missing_kind_falls_back_to_jpeg(self):
        assert to_data_url(b"\x00", "").startswith("data:image/jpeg;base64,")

    def test_a_round_trip_keeps_the_bytes(self):
        original = jpeg_data_url(120, 80)

        raw, mime = data_url_parts(original)

        assert to_data_url(raw, mime) == original


class TestNormalizingExternalImages:
    def test_a_small_image_is_oriented_reencoded_and_metadata_free(self):
        source = Image.new("RGB", (40, 20), (20, 80, 160))
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "private camera note"
        buffer = io.BytesIO()
        source.save(buffer, "JPEG", exif=exif)
        lied_about_type = to_data_url(buffer.getvalue(), "image/png")

        normalized = normalize_data_url(lied_about_type)

        assert normalized is not None
        raw, mime = data_url_parts(normalized)
        assert mime == "image/jpeg"
        assert raw != buffer.getvalue()
        with Image.open(io.BytesIO(raw)) as clean:
            assert clean.size == (20, 40)
            assert clean.getexif() == {}
            assert "exif" not in clean.info

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "https://example.com/private.jpg",
            "data:image/jpeg,not-base64",
            "data:image/jpeg;base64,!!!",
            "data:image/jpeg;base64,bm90IGFuIGltYWdl",
        ],
    )
    def test_malformed_external_data_fails_closed(self, value):
        assert normalize_data_url(value) is None

    def test_oversized_external_bytes_fail_closed(self, monkeypatch):
        monkeypatch.setattr("app.shared.image_bytes.MAX_SOURCE_IMAGE_BYTES", 16)
        external = png_data_url(4, 4)

        assert normalize_data_url(external) is None

    def test_oversized_base64_is_rejected_before_decoding(self, monkeypatch):
        monkeypatch.setattr("app.shared.image_bytes.MAX_SOURCE_IMAGE_BYTES", 3)

        def decode_must_not_run(*args, **kwargs):
            pytest.fail("oversized base64 must be rejected before allocation")

        monkeypatch.setattr("app.shared.image_bytes.base64.b64decode", decode_must_not_run)

        assert normalize_data_url("data:image/jpeg;base64,AAAAAAAA") is None


class TestShrinking:
    def test_a_wide_image_comes_back_smaller(self):
        """1200px는 블로그 본문 폭(800~900px)보다 크다 — 그만큼이 낭비다."""
        original = jpeg_data_url(1200, 675)
        raw_before, _ = data_url_parts(original)

        raw, mime = shrink(original)

        assert mime == "image/jpeg"
        assert len(raw) < len(raw_before)
        assert Image.open(io.BytesIO(raw)).width == MAX_IMAGE_WIDTH

    def test_the_height_keeps_its_proportion(self):
        """비율이 틀어지면 발행된 글의 사진이 늘어나 보인다."""
        raw, _ = shrink(jpeg_data_url(1200, 675))

        image = Image.open(io.BytesIO(raw))
        assert image.height == round(675 * MAX_IMAGE_WIDTH / 1200)

    def test_an_image_that_is_already_small_is_not_touched(self):
        """다시 인코딩하면 화질만 잃고 용량은 안 준다."""
        original = jpeg_data_url(600, 400)
        raw_before, mime_before = data_url_parts(original)

        raw, mime = shrink(original)

        assert raw == raw_before
        assert mime == mime_before

    def test_something_that_cannot_be_opened_is_left_alone(self):
        """못 여는 형식이라고 버리지 않는다 — 있는 그대로 저장한다."""
        broken = to_data_url(b"\x01\x02\x03\x04", "image/png")

        raw, mime = shrink(broken)

        assert raw == b"\x01\x02\x03\x04"
        assert mime == "image/png"

    def test_not_a_data_url_gives_nothing_to_store(self):
        raw, _ = shrink("https://example.com/a.png")

        assert raw == b""

    def test_the_saving_is_worth_it(self):
        """실측 기준: 장당 204KB → 78KB. 절반 아래로 떨어져야 이 작업이 의미가 있다."""
        original = jpeg_data_url(1200, 675)

        raw, _ = shrink(original)

        # base64로 담던 것과 비교한다 — 그것이 예전에 오가던 양이다.
        assert len(raw) < len(original) / 2

    def test_the_quality_setting_is_the_one_we_measured(self):
        assert (MAX_IMAGE_WIDTH, JPEG_QUALITY) == (900, 80)

    def test_a_wide_rendered_png_stays_lossless_png(self):
        """도표·표의 한글과 1px 선을 JPEG q80로 바꾸지 않는다."""
        raw, mime = shrink(png_data_url(960, 540))

        assert mime == "image/png"
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(io.BytesIO(raw)) as image:
            assert image.size == (MAX_IMAGE_WIDTH, round(540 * MAX_IMAGE_WIDTH / 960))

    def test_declared_mime_is_replaced_with_the_actual_small_image_format(self):
        png_raw, _mime = data_url_parts(png_data_url(320, 180))
        lied = to_data_url(png_raw, "image/jpeg")

        stored, mime = shrink(lied)

        assert stored == png_raw
        assert mime == "image/png"


class TestOldRowsStillRead:
    def test_base64_that_was_stored_before_is_still_a_data_url(self):
        """이관 전 행은 `dataUrl` 글자를 들고 있다. 그것도 그대로 읽혀야 한다."""
        stored = "data:image/png;base64," + base64.b64encode(b"hello").decode()

        raw, mime = data_url_parts(stored)

        assert raw == b"hello"
        assert to_data_url(raw, mime) == stored
