"""썸네일 규격은 눈으로 봐서는 확인이 안 된다 — 1536×864인지, 문구가 안전 영역을
넘지 않았는지는 픽셀을 세어야 알 수 있다.
"""

import io

import pytest
from PIL import Image, ImageChops

from app.llm.imaging import (
    BODY_HEIGHT,
    BODY_WIDTH,
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    MAX_COPY_CHARS_PER_LINE,
    MAX_COPY_LINES,
    SAFE_AREA_LEFT,
    SAFE_AREA_RIGHT,
    _emphasis_segments,
    font_path,
    render_thumbnail,
    thumbnail_keyword_colors,
    thumbnail_lines,
    to_canvas,
)
from app.llm.imaging import LETTERBOX_COLOR

needs_font = pytest.mark.skipif(
    font_path() is None, reason="no Korean font on this machine — the copy cannot be drawn"
)


def photo(width: int = 1536, height: int = 1024, colour=(200, 200, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def opened(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def visible_change_box(plain: Image.Image, lettered: Image.Image):
    """문구·스크림처럼 눈에 띄는 변화만 잡는다. 썸네일(q95·4:4:4)과 비교본(q88·4:2:0)은
    인코딩이 달라 전면에 1~2 수준의 양자화 차이가 깔릴 수 있다 — 그 노이즈는 무시한다."""
    diff = ImageChops.difference(plain, lettered).convert("L")
    return diff.point(lambda p: p if p > 12 else 0).getbbox()


def test_a_body_image_comes_back_at_the_body_size_not_the_thumbnail_size():
    """본문은 1:1 크롭 제약이 없어 썸네일과 규격이 다르다."""
    assert opened(to_canvas(photo())).size == (BODY_WIDTH, BODY_HEIGHT)
    assert (BODY_WIDTH, BODY_HEIGHT) != (CANVAS_WIDTH, CANVAS_HEIGHT)


@pytest.mark.parametrize("size", [(1536, 1024), (1024, 1024), (1024, 1536), (2000, 500)])
def test_every_size_the_model_can_return_is_cropped_down_to_the_body_canvas(size):
    """모델은 우리 규격을 그리지 못한다. 무엇을 주든 규격으로 내려앉아야 한다."""
    assert opened(to_canvas(photo(*size))).size == (BODY_WIDTH, BODY_HEIGHT)


@pytest.mark.parametrize("size", [(1536, 1024), (1024, 1024), (1024, 1536), (2000, 500)])
def test_every_size_the_model_can_return_becomes_a_full_size_thumbnail(size):
    assert opened(render_thumbnail(photo(*size), [])).size == (CANVAS_WIDTH, CANVAS_HEIGHT)


@needs_font
def test_the_thumbnail_is_the_canvas_size_too():
    rendered = render_thumbnail(photo(), ["대표 문구", "두 줄까지"])

    assert opened(rendered).size == (CANVAS_WIDTH, CANVAS_HEIGHT)


@needs_font
def test_the_copy_never_touches_the_strips_that_get_cropped_away():
    """좌우 336px은 모바일에서 잘려 나간다. 거기 글자가 걸리면 문구가 반쪽만 남는다."""
    plain = opened(render_thumbnail(photo(), [])).convert("RGB")
    lettered = opened(render_thumbnail(photo(), ["열두자를전부채운문구다", "두번째줄도열두자다"])).convert("RGB")

    changed = visible_change_box(plain, lettered)

    assert changed is not None, "문구가 그려지지 않았다"
    left, _, right, _ = changed
    assert left >= SAFE_AREA_LEFT
    assert right <= SAFE_AREA_RIGHT


@needs_font
def test_copy_is_drawn_at_all():
    plain = opened(render_thumbnail(photo(), [])).convert("RGB")
    lettered = opened(render_thumbnail(photo(), ["핵심 문구"])).convert("RGB")

    assert visible_change_box(plain, lettered) is not None


def test_a_thumbnail_with_no_copy_is_still_a_valid_thumbnail():
    assert opened(render_thumbnail(photo(), [])).size == (CANVAS_WIDTH, CANVAS_HEIGHT)


def test_copy_that_breaks_the_rules_is_cut_to_fit():
    lines = thumbnail_lines(
        ["열두자를한참넘기는아주긴문구입니다", "두번째줄", "세번째줄", "네번째줄"], "제목"
    )

    assert len(lines) == MAX_COPY_LINES
    assert all(len(line) <= MAX_COPY_CHARS_PER_LINE for line in lines)


def test_compliant_copy_is_passed_through_untouched():
    """규격 안의 문구는 재줄바꿈 없이 모델이 정한 줄 나눔을 그대로 쓴다."""
    assert thumbnail_lines(["짧은 문구", "그대로 유지"], "제목") == ["짧은 문구", "그대로 유지"]


def test_over_long_copy_is_rewrapped_with_its_content_preserved():
    """예전에는 12자째에서 중간 절단해 뒷부분이 사라졌다("성능이 3배 빨라졌"). 이제는
    내용을 보존한 채 다시 줄바꿈한다."""
    lines = thumbnail_lines(["우리서비스성능이3배빨라졌다"], "제목")

    assert all(len(line) <= MAX_COPY_CHARS_PER_LINE for line in lines)
    assert "".join(lines).replace(" ", "") == "우리서비스성능이3배빨라졌다"


def test_a_long_word_in_the_fallback_title_is_carried_over_not_lost():
    """제목 폴백에서도 12자 초과 단어의 13자째부터가 조용히 소실되면 안 된다."""
    long_word = "가나다라마바사아자차카타파하"  # 14자
    lines = thumbnail_lines(None, long_word)

    assert lines == [long_word[:MAX_COPY_CHARS_PER_LINE], long_word[MAX_COPY_CHARS_PER_LINE:]]


def test_a_draft_with_no_copy_falls_back_to_the_title():
    """문구 없는 썸네일보다는 제목이라도 얹힌 썸네일이 낫다."""
    lines = thumbnail_lines(None, "블로그 자동화 실전 가이드")

    assert lines
    assert all(len(line) <= MAX_COPY_CHARS_PER_LINE for line in lines)
    assert lines[0].startswith("블로그")


def test_brand_and_finance_keywords_receive_their_own_semantic_colors():
    colors = thumbnail_keyword_colors(
        ["토스뱅크 통장이", "곧 파킹통장"],
        topic="토스뱅크 통장",
        accent_family="CYAN_NAVY",
    )

    assert list(colors) == ["토스뱅크", "파킹통장"]
    assert colors["토스뱅크"] != colors["파킹통장"]


def test_only_the_matching_noun_is_colored_not_its_korean_particle():
    colors = thumbnail_keyword_colors(
        ["토스뱅크 통장이"],
        topic="토스뱅크 통장",
        accent_family="CYAN_NAVY",
    )
    segments = _emphasis_segments("토스뱅크 통장이", colors)

    assert segments[0][0] == "토스뱅크"
    assert segments[0][1] == colors["토스뱅크"]
    assert "".join(text for text, _ in segments) == "토스뱅크 통장이"
    assert segments[-1][1] is None


def test_a_topic_that_is_not_in_the_copy_does_not_color_random_words():
    assert (
        thumbnail_keyword_colors(
            ["이것만 알면", "바로 해결"],
            topic="블로그 자동화",
            keywords=["AI"],
            accent_family="CYAN_NAVY",
        )
        == {}
    )


@needs_font
def test_the_rendered_title_contains_both_white_and_semantic_color_pixels():
    colors = thumbnail_keyword_colors(
        ["토스뱅크 통장이", "곧 파킹통장"],
        topic="토스뱅크 통장",
        accent_family="CYAN_NAVY",
    )
    rendered = opened(
        render_thumbnail(
            photo(colour=(220, 205, 180)),
            ["토스뱅크 통장이", "곧 파킹통장"],
            keyword_colors=colors,
        )
    ).convert("RGB")
    pixels = list(
        rendered.crop(
            (SAFE_AREA_LEFT, 240, SAFE_AREA_RIGHT, 630)
        ).get_flattened_data()
    )

    def near(pixel, target, tolerance=24):
        return all(
            abs(channel - expected) <= tolerance
            for channel, expected in zip(pixel, target, strict=True)
        )

    assert sum(near(pixel, (255, 255, 255)) for pixel in pixels) > 100
    assert (
        sum(near(pixel, color) for pixel in pixels for color in colors.values())
        > 100
    )


@needs_font
@pytest.mark.parametrize("copy_zone", ["BOTTOM_CENTER", "TOP_CENTER"])
def test_the_title_box_never_touches_the_top_or_bottom_edge(copy_zone):
    """제목 박스가 프레임 끝에 닿으면 '배치'가 아니라 '잘림'으로 보인다.

    실제로 그랬다: 인물 썸네일의 문구를 아래 띠로 내렸더니 두 줄짜리 제목의 박스가
    아래 여백 4px로 바닥에 붙어 잘린 띠처럼 보였다. 영역 높이가 박스(글자 + 상하 여백)
    보다 낮았고, 폰트 맞춤이 그 여백을 계산에 넣지 않았기 때문이다.
    """
    from app.shared import ThumbnailLayoutPlan

    lines = ["규정 비판이", "불붙인 거취설"]
    layout = ThumbnailLayoutPlan(
        layout="COPY_BOTTOM_SUBJECT_TOP",
        subject_zone="TOP_CENTER" if copy_zone == "BOTTOM_CENTER" else "BOTTOM_CENTER",
        copy_zone=copy_zone,
        copy_alignment="CENTER",
        copy_lines=lines,
        show_copy=True,
    )

    plain = opened(render_thumbnail(photo(), [], layout)).convert("RGB")
    lettered = opened(render_thumbnail(photo(), lines, layout)).convert("RGB")
    changed = visible_change_box(plain, lettered)

    assert changed is not None, "문구가 그려지지 않았다"
    left, top, right, bottom = changed
    assert top >= 8, f"박스가 위 가장자리에 붙었다(top={top})"
    assert bottom <= CANVAS_HEIGHT - 8, f"박스가 아래 가장자리에 붙었다(bottom={bottom})"
    # 가로는 예전처럼 안전 영역 안에서 가운데다.
    assert left >= SAFE_AREA_LEFT
    assert right <= SAFE_AREA_RIGHT
    center = (left + right) // 2
    assert abs(center - CANVAS_WIDTH // 2) <= 12, f"박스가 가운데가 아니다(center={center})"


@needs_font
def test_a_two_line_title_keeps_the_face_band_clear_in_a_person_thumbnail():
    """인물 썸네일의 문구는 아래 띠에만 있어야 한다 — 얼굴이 있는 위쪽 2/3는 비운다."""
    from app.modules.draft.editorial_style import thumbnail_layout_plan_for

    lines = ["규정 비판이", "불붙인 거취설"]
    layout = thumbnail_layout_plan_for(None, lines, face_safe=True)

    plain = opened(render_thumbnail(photo(), [], layout)).convert("RGB")
    lettered = opened(render_thumbnail(photo(), lines, layout)).convert("RGB")
    _, top, _, _ = visible_change_box(plain, lettered)

    assert top >= CANVAS_HEIGHT * 0.5, f"문구가 얼굴 영역까지 올라왔다(top={top})"


def height_coded_portrait(width: int = 1200, height: int = 1600) -> bytes:
    """세로로 긴 사진. 각 행의 빨강 값이 그 행의 y 위치를 담는다 — 결과의 첫 줄을 읽으면
    원본의 어디를 잘랐는지 픽셀로 알 수 있다."""
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        value = y * 255 // height
        for x in range(width):
            pixels[x, y] = (value, 0, 0)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def crop_top_of(data: bytes, source_height: int = 1600) -> int:
    """결과 사진의 첫 줄이 원본의 몇 번째 행이었는지(px)."""
    with Image.open(io.BytesIO(data)) as image:
        red = image.convert("RGB").getpixel((image.width // 2, 0))[0]
    return round(red * source_height / 255)


class TestVerticalCrop:
    """세로로 긴 인물 사진을 가운데로 자르면 얼굴이 프레임 밖으로 나간다."""

    # 1200x1600을 16:9로 자르면 675px가 남고, 위아래로 925px가 버려진다.
    DISCARDED = 1600 - round(1200 / (16 / 9))

    def test_a_generated_image_still_crops_from_the_middle(self):
        """생성 이미지의 기존 동작은 그대로다 — 프롬프트가 피사체를 가운데 두라고 요구한다."""
        top = crop_top_of(to_canvas(height_coded_portrait()))

        assert abs(top - self.DISCARDED * 0.5) <= 12

    def test_a_web_photo_keeps_the_top_where_the_face_is(self):
        from app.llm.imaging import FACE_CROP

        top = crop_top_of(to_canvas(height_coded_portrait(), FACE_CROP))

        assert abs(top - self.DISCARDED * FACE_CROP) <= 12
        # 가운데로 자를 때보다 위쪽에서 시작한다 — 그 차이가 얼굴이 남고 잘리고를 가른다.
        assert top < crop_top_of(to_canvas(height_coded_portrait()))

    def test_a_landscape_photo_is_unaffected_by_the_bias(self):
        """가로가 넓은 사진은 좌우를 자르므로 세로 위치가 결과를 바꾸지 않는다."""
        from app.llm.imaging import CENTER_CROP, FACE_CROP

        wide = height_coded_portrait(2400, 900)
        assert to_canvas(wide, FACE_CROP) == to_canvas(wide, CENTER_CROP)

    @needs_font
    def test_the_thumbnail_takes_the_same_bias(self):
        from app.llm.imaging import CENTER_CROP, FACE_CROP

        tall = height_coded_portrait()
        assert render_thumbnail(tall, ["문구"], None, None, FACE_CROP) != render_thumbnail(
            tall, ["문구"], None, None, CENTER_CROP
        )


class TestContainFit:
    """공식 영상 썸네일은 자르지 않는다(2026-08-03).

    원본에 이미 인물·로고·영상 제목 문구가 구워져 있어, 규격을 맞추려고 잘라내면 우리가
    그 정보를 지우는 셈이다. 16:9 원본은 여백 없이 딱 맞으므로 결과가 예전과 같고,
    4:3 원본만 위아래가 잘리는 대신 여백을 얻는다.
    """

    # 표시 띠의 두께. 1px 선은 축소·JPEG를 거치며 섞여 사라지므로, 잘림 여부를 재려면
    # 리샘플링을 견딜 만큼 두꺼워야 한다.
    _BAND = 24

    def side_marked(self, width: int, height: int) -> bytes:
        """좌우 끝에 붉은 띠를 둔 이미지. 가로가 잘렸는지 픽셀로 확인할 수 있다."""
        image = Image.new("RGB", (width, height), (10, 10, 10))
        for x in [*range(self._BAND), *range(width - self._BAND, width)]:
            for y in range(height):
                image.putpixel((x, y), (255, 0, 0))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def marked(self, width: int, height: int) -> bytes:
        """위·아래 끝에 붉은 띠를 둔 이미지. 잘렸는지 픽셀로 확인할 수 있다."""
        image = Image.new("RGB", (width, height), (10, 10, 10))
        for y in [*range(self._BAND), *range(height - self._BAND, height)]:
            for x in range(width):
                image.putpixel((x, y), (255, 0, 0))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def has_red(self, image: Image.Image) -> bool:
        return any(
            pixel[0] > 150 and pixel[1] < 110 and pixel[2] < 110
            for pixel in image.convert("RGB").get_flattened_data()
        )

    def test_a_four_three_thumbnail_keeps_its_top_and_bottom(self):
        rendered = opened(to_canvas(self.marked(640, 480), contain=True))
        assert rendered.size == (BODY_WIDTH, BODY_HEIGHT)
        assert self.has_red(rendered) is True

    def test_cropping_the_same_image_loses_them(self):
        """기본 동작(생성 이미지)은 예전 그대로다 — 잘라서 규격을 맞춘다."""
        rendered = opened(to_canvas(self.marked(640, 480)))
        assert rendered.size == (BODY_WIDTH, BODY_HEIGHT)
        assert self.has_red(rendered) is False

    def test_a_sixteen_nine_source_is_identical_either_way(self):
        cropped = opened(to_canvas(self.marked(1280, 720)))
        contained = opened(to_canvas(self.marked(1280, 720), contain=True))
        assert ImageChops.difference(cropped, contained).getbbox() is None

    def test_the_thumbnail_renderer_also_supports_contain(self):
        rendered = opened(render_thumbnail(self.marked(640, 480), [], contain=True))
        assert rendered.size == (CANVAS_WIDTH, CANVAS_HEIGHT)
        assert self.has_red(rendered) is True

    def test_a_wide_thumbnail_keeps_its_full_width_on_the_square_canvas(self):
        """대표 썸네일이 1:1(720×720)이 된 뒤의 실제 모양을 못 박는다.

        16:9 공식 썸네일을 정사각 캔버스에 크롭으로 넣으면 좌우가 28%씩 잘려 나간다 —
        원본에 구워진 문구·로고가 사라지는 자리다. contain은 폭을 그대로 살린다.
        """
        wide = self.side_marked(1280, 720)
        contained = opened(render_thumbnail(wide, [], contain=True))
        cropped = opened(render_thumbnail(wide, []))
        assert contained.size == (CANVAS_WIDTH, CANVAS_HEIGHT)
        # 좌우 끝 표시가 살아 있어야 '자르지 않았다'가 성립한다.
        assert self.has_red(contained) is True
        assert self.has_red(cropped) is False

    def test_the_leftover_band_is_a_blurred_backdrop_not_a_flat_bar(self):
        """남는 자리를 단색으로 두면 '규격을 못 맞춘 이미지'처럼 보인다.

        같은 사진을 꽉 채워 흐리게 깐 배경이라, 띠 자리에도 원본의 색이 남는다.
        """
        buffer = io.BytesIO()
        source = Image.new("RGB", (1280, 720), (10, 10, 10))
        # 위쪽 절반만 붉게 — 흐린 배경이면 띠에도 이 색이 번져 나온다.
        for y in range(360):
            for x in range(1280):
                source.putpixel((x, y), (220, 40, 40))
        source.save(buffer, format="PNG")

        rendered = opened(render_thumbnail(buffer.getvalue(), [], contain=True))
        top_band = rendered.convert("RGB").getpixel((CANVAS_WIDTH // 2, 20))

        assert top_band != LETTERBOX_COLOR
        # 어둡히되(원본보다 어둡다) 색은 남는다(회색이 아니다).
        assert top_band[0] > top_band[1] + 20
        assert top_band[0] < 220


class TestCropLoss:
    """얼마나 잘라야 규격이 되는가. 너무 많이 잘라야 하면 자르지 않는다(2026-08-05).

    디올 가방 사진에서 손잡이만 남은 원인이 여기 있었다 — 세로로 긴 상세컷을 16:9로
    자르면 세로의 절반 이상이 사라진다. 자른 것이 잘못이 아니라 얼마나 잘랐는가가 문제다.
    """

    def test_a_matching_ratio_loses_nothing(self):
        from app.llm.imaging import crop_loss

        assert crop_loss(BODY_WIDTH, BODY_HEIGHT, BODY_WIDTH, BODY_HEIGHT) == pytest.approx(0.0)

    def test_a_portrait_source_loses_more_than_half(self):
        from app.llm.imaging import crop_loss

        assert crop_loss(1000, 1500, BODY_WIDTH, BODY_HEIGHT) > 0.5

    def test_a_common_three_two_source_stays_under_the_limit(self):
        """모델이 돌려주는 3:2(1536×1024)는 예전처럼 잘려야 한다 — 여기까지 비율을
        보존하면 멀쩡한 생성 이미지에 흐린 띠가 생긴다."""
        from app.llm.imaging import MAX_CROP_LOSS, crop_loss

        assert crop_loss(1536, 1024, BODY_WIDTH, BODY_HEIGHT) < MAX_CROP_LOSS
        assert crop_loss(1536, 1024, CANVAS_WIDTH, CANVAS_HEIGHT) < MAX_CROP_LOSS

    def test_an_unreadable_size_does_not_claim_a_loss(self):
        from app.llm.imaging import crop_loss

        assert crop_loss(0, 0, BODY_WIDTH, BODY_HEIGHT) == 0.0

    def test_the_limit_turns_a_deep_crop_into_a_preserved_ratio(self):
        from app.llm.imaging import MAX_CROP_LOSS

        tall = height_coded_portrait(1000, 1500)
        assert to_canvas(tall, max_crop_loss=MAX_CROP_LOSS) == to_canvas(
            tall, contain=True
        )

    def test_a_shallow_crop_is_untouched_by_the_limit(self):
        from app.llm.imaging import MAX_CROP_LOSS

        wide = height_coded_portrait(1536, 1024)
        assert to_canvas(wide, max_crop_loss=MAX_CROP_LOSS) == to_canvas(wide)

    def test_the_thumbnail_renderer_honours_the_limit_too(self):
        from app.llm.imaging import MAX_CROP_LOSS

        tall = height_coded_portrait(1000, 1600)
        assert render_thumbnail(tall, [], max_crop_loss=MAX_CROP_LOSS) == (
            render_thumbnail(tall, [], contain=True)
        )
