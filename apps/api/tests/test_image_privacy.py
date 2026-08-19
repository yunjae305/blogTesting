"""사진에 찍힌 개인정보를 검게 덮는다.

2026-08-07 신고: 야간 드라이브 글에 올린 주차장 사진에 **차량 번호판이 그대로 읽혔다.**
사용자가 올린 사진은 원본 그대로 글에 다시 실리기 때문이다.

여기서 재는 것은 '칠하기'다 — 어디를 칠할지 정하는 것은 모델이고(`llm/schemas.py`의
privateRegions), 그 좌표로 실제 픽셀이 지워지는지는 모델 없이 확인할 수 있어야 한다.
"""

import base64
import io

import pytest
from app.shared.image_bytes import to_data_url
from app.shared.image_privacy import (
    MASK_COLOR,
    MAX_REGION_AREA,
    PrivateRegion,
    mask_data_url,
    mask_regions,
)
from PIL import Image


def _photo(width: int = 200, height: int = 100, color=(220, 30, 30)) -> bytes:
    """전부 같은 색인 사진. 덮인 자리는 검정이 되므로 눈으로 셀 수 있다."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def _pixels(raw: bytes) -> Image.Image:
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _is_black(pixel, tolerance: int = 12) -> bool:
    """JPEG은 손실 압축이라 정확히 (0,0,0)이 아니다. 검은지만 본다."""
    return all(channel <= tolerance for channel in pixel)


class TestPaintingOverWhatShouldNotBePublished:
    def test_지정한_자리가_검게_지워진다(self):
        raw = _photo()
        # 오른쪽 아래 사분면.
        region = PrivateRegion(x=0.5, y=0.5, width=0.5, height=0.5, kind="번호판")

        masked = _pixels(mask_regions(raw, [region]))

        assert _is_black(masked.getpixel((150, 75))), "덮으라고 한 자리가 안 덮였다"

    def test_지정하지_않은_자리는_그대로다(self):
        """사진 전체를 검게 만들면 가린 것이 아니라 잃은 것이다."""
        raw = _photo(color=(220, 30, 30))
        region = PrivateRegion(x=0.6, y=0.6, width=0.3, height=0.3)

        masked = _pixels(mask_regions(raw, [region]))

        left_top = masked.getpixel((10, 10))
        assert not _is_black(left_top)
        assert left_top[0] > 150, "원본 색이 남아 있어야 한다"

    def test_덮은_픽셀은_되돌릴_수_없다(self):
        """블러·모자이크가 아니라 **지우기**다. 약한 블러에서 번호판을 복원하는 것은
        알려진 기법이라, 남은 정보가 0이어야 한다."""
        raw = _photo(width=100, height=100)
        region = PrivateRegion(x=0.0, y=0.0, width=1.0, height=0.5)

        masked = _pixels(mask_regions(raw, [region]))

        top_half = [masked.getpixel((x, y)) for x in range(0, 100, 7) for y in range(0, 40, 7)]
        assert all(_is_black(pixel) for pixel in top_half)
        # 색이 하나뿐이어야 한다 — 무늬가 남아 있으면 그 무늬가 정보다.
        assert len({pixel for pixel in top_half}) <= 2

    def test_여러_곳을_한꺼번에_덮는다(self):
        raw = _photo(width=200, height=200)
        regions = [
            PrivateRegion(x=0.0, y=0.0, width=0.2, height=0.2, kind="전화번호"),
            PrivateRegion(x=0.75, y=0.75, width=0.2, height=0.2, kind="생년월일"),
        ]

        masked = _pixels(mask_regions(raw, regions))

        assert _is_black(masked.getpixel((10, 10)))
        assert _is_black(masked.getpixel((190, 190)))
        assert not _is_black(masked.getpixel((100, 100)))

    def test_상자를_넉넉하게_넓혀_칠한다(self):
        """모델 좌표는 정확하지 않다. 상자가 조금 작아 글자가 남으면 덮은 의미가 없으므로,
        사방으로 여유를 준다."""
        raw = _photo(width=400, height=400)
        region = PrivateRegion(x=0.4, y=0.4, width=0.2, height=0.2)

        masked = _pixels(mask_regions(raw, [region]))

        # 상자 경계(0.4 * 400 = 160)보다 바깥인데도 덮여야 한다.
        assert _is_black(masked.getpixel((152, 200)))


class TestWhatItRefusesToDo:
    def test_이미지_전체를_덮으라는_상자는_실패로_구분한다(self):
        """그대로 칠할 수도, 개인정보가 남은 원본을 게시할 수도 없으므로 None이다."""
        raw = _photo()
        whole = PrivateRegion(x=0.0, y=0.0, width=1.0, height=1.0)

        assert mask_regions(raw, [whole]) is None

    def test_한계보다_조금_작은_상자는_통과한다(self):
        """상한이 '큰 상자는 다 버린다'가 되면 진짜 개인정보도 못 덮는다."""
        raw = _photo(width=100, height=100)
        area = MAX_REGION_AREA - 0.05
        region = PrivateRegion(x=0.0, y=0.0, width=1.0, height=area)

        masked = _pixels(mask_regions(raw, [region]))

        assert _is_black(masked.getpixel((50, 10)))

    def test_덮을_곳이_없으면_원본_그대로다(self):
        raw = _photo()

        assert mask_regions(raw, []) == raw

    def test_열_수_없는_이미지는_원본과_구분되는_실패다(self):
        """호출부가 해당 사진만 제외할 수 있어야 하며 원본 바이트를 게시하면 안 된다."""
        broken = "이건 이미지가 아니다".encode()
        region = PrivateRegion(x=0.1, y=0.1, width=0.2, height=0.2)

        assert mask_regions(broken, [region]) is None

    @pytest.mark.parametrize(
        "bad",
        [
            {"x": -0.1, "y": 0.1, "width": 0.2, "height": 0.2},
            {"x": 0.1, "y": 0.1, "width": 0.0, "height": 0.2},
            {"x": 1.4, "y": 0.1, "width": 0.2, "height": 0.2},
        ],
        ids=["음수 좌표", "너비 0", "1을 넘는 좌표"],
    )
    def test_말이_안_되는_상자는_애초에_만들어지지_않는다(self, bad):
        with pytest.raises(ValueError):
            PrivateRegion(**bad)


class TestTheDataUrlItHandsBack:
    def test_덮은_뒤에는_JPEG이라고_밝힌다(self):
        """PNG라고 적어 두면 받는 쪽이 PNG인 줄 알고 연다."""
        data_url = to_data_url(_photo(), "image/png")
        region = PrivateRegion(x=0.5, y=0.5, width=0.3, height=0.3)

        masked = mask_data_url(data_url, [region])

        assert masked.startswith("data:image/jpeg;base64,")
        assert masked != data_url

    def test_덮을_것이_없으면_받은_것을_그대로_돌려준다(self):
        """공연히 다시 인코딩하면 화질만 잃는다."""
        data_url = to_data_url(_photo(), "image/png")

        assert mask_data_url(data_url, []) == data_url

    def test_data_url이_아니면_실패로_구분한다(self):
        region = PrivateRegion(x=0.1, y=0.1, width=0.2, height=0.2)

        assert mask_data_url("https://example.com/a.jpg", [region]) is None

    def test_돌려준_data_url을_실제로_열어_보면_덮여_있다(self):
        data_url = to_data_url(_photo(width=200, height=200), "image/png")
        region = PrivateRegion(x=0.0, y=0.0, width=0.4, height=0.4, kind="번호판")

        masked = mask_data_url(data_url, [region])

        raw = base64.b64decode(masked.split(",", 1)[1])
        assert _is_black(_pixels(raw).getpixel((20, 20)))


def test_덮는_색은_완전한_검정이다():
    """회색으로 덮으면 밝기 대비로 글자가 비쳐 보일 수 있다."""
    assert MASK_COLOR == (0, 0, 0)
