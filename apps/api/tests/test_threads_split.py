"""완성된 원고를 그대로 연속 스레드로 나누는 규칙.

여기서 막는 것: 원고를 요약하거나 다시 쓰는 것, 한도(500자)를 넘긴 스레드가 나가는 것,
원고에 있던 이미지가 사라지는 것, 마크다운 기호가 글자로 보이는 것.
"""

import base64

import pytest

from app.posting.threads_split import (
    MAX_IMAGES_PER_THREAD,
    THREAD_TEXT_LIMIT,
    cover_image_of,
    decode_data_url,
    plain_text,
    split_final_post,
    split_to_limit,
)
from app.shared import FinalPost, GeneratedPostImage

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()
JPG_DATA_URL = "data:image/jpeg;base64," + base64.b64encode(PNG_BYTES).decode()


def build_image(data_url: str = PNG_DATA_URL) -> GeneratedPostImage:
    return GeneratedPostImage(
        data_url=data_url,
        alt_text="설명",
        prompt="a scene",
        provider="test",
        model="test",
        generated_at="2026-08-04T00:00:00.000Z",
        mime_type="image/png",
    )


def build_post(**overrides) -> FinalPost:
    defaults = dict(
        title="원고 제목",
        body="",
        hashtags=[],
        html_content="<article></article>",
        markdown_content="본문 문단입니다.",
    )
    return FinalPost(**{**defaults, **overrides})


class TestTheArticleIsCarriedNotRewritten:
    """가장 중요한 규칙 — 글자를 바꾸지 않는다.

    2026-08-04 사용자: "요약하지말고 생성된 원고 그대로 스레드에 게시하게 하고 싶어".
    그 전에는 500자 요약이었고, 그다음에는 LLM이 2~5개로 다시 썼다. 둘 다 문장이 사라졌다.
    """

    def test_every_sentence_survives(self):
        body = "\n\n".join(f"{n}번째 문단입니다. 여기 담긴 문장은 그대로 실려야 합니다." for n in range(1, 12))
        post = build_post(markdown_content=body)

        joined = " ".join(piece.text for piece in split_final_post(post))

        for n in range(1, 12):
            assert f"{n}번째 문단입니다." in joined

    def test_the_title_leads_the_first_thread(self):
        pieces = split_final_post(build_post())

        assert pieces[0].text.startswith("원고 제목")

    def test_the_title_is_not_repeated_when_the_body_opens_with_it(self):
        """실사용(2026-08-04): 게시된 글에 제목이 두 번 나왔다.

        원고 본문이 제목과 같은 소제목으로 시작하는 경우다 — 네이버 발행도 같은 이유로
        제목과 같은 <h1>을 지운다.
        """
        post = build_post(markdown_content="# 원고 제목\n\n본문 문단입니다.")

        joined = " ".join(piece.text for piece in split_final_post(post))

        assert joined.count("원고 제목") == 1
        assert "본문 문단입니다." in joined

    def test_a_later_heading_that_matches_the_title_is_kept(self):
        """지우는 것은 **맨 앞** 하나뿐이다 — 본문 중간의 같은 문구는 글의 일부다."""
        post = build_post(markdown_content="여는 문단입니다.\n\n## 원고 제목\n\n닫는 문단입니다.")

        joined = " ".join(piece.text for piece in split_final_post(post))

        assert joined.count("원고 제목") == 2

    def test_hashtags_close_the_last_thread(self):
        post = build_post(hashtags=["AI", "#자동화"])

        pieces = split_final_post(post)

        assert pieces[-1].text.endswith("#AI #자동화")

    def test_a_short_article_stays_one_thread(self):
        post = build_post(markdown_content="한 문단짜리 짧은 원고입니다.", hashtags=[])

        pieces = split_final_post(post)

        assert len(pieces) == 1
        assert pieces[0].text == "원고 제목\n\n한 문단짜리 짧은 원고입니다."

    def test_an_empty_article_yields_nothing(self):
        """빈 원고는 빈 목록이다 — 발행기가 '실을 텍스트가 없습니다'로 거절한다."""
        assert split_final_post(build_post(title="", markdown_content="")) == []


class TestTheLimitIsAlwaysKept:
    """한도를 넘기면 스레드가 글을 통째로 거절한다 — 한도는 코드가 지킨다."""

    def test_no_thread_exceeds_the_limit(self):
        body = "\n\n".join(f"{n}번 문단입니다. " + "채움 문장입니다. " * 12 for n in range(1, 9))

        pieces = split_final_post(build_post(markdown_content=body))

        assert len(pieces) > 1
        assert all(len(piece.text) <= THREAD_TEXT_LIMIT for piece in pieces)

    def test_a_paragraph_longer_than_the_limit_is_split_at_a_sentence(self):
        long_paragraph = "문장 하나입니다. " * 80

        chunks = split_to_limit(long_paragraph)

        assert all(len(chunk) <= THREAD_TEXT_LIMIT for chunk in chunks)
        # 문장 중간에서 끊기지 않는다.
        assert all(chunk.endswith("다.") for chunk in chunks)

    def test_a_single_sentence_longer_than_the_limit_is_cut(self):
        """마침표가 없는 덩어리(표·긴 인용)도 발행을 막지 않는다."""
        blob = "가" * 1200

        chunks = split_to_limit(blob)

        assert all(len(chunk) <= THREAD_TEXT_LIMIT for chunk in chunks)
        assert "".join(chunks) == blob

    def test_paragraphs_are_packed_not_scattered(self):
        """한도가 남는데 문단마다 새 스레드를 만들면 스레드가 쓸데없이 늘어난다."""
        body = "\n\n".join(["짧은 문단입니다."] * 6)

        pieces = split_final_post(build_post(markdown_content=body, hashtags=[]))

        assert len(pieces) == 1


class TestImagesRideAlong:
    def test_an_image_joins_the_thread_it_appears_in(self):
        body = f"첫 문단입니다.\n\n![설명]({PNG_DATA_URL})\n\n둘째 문단입니다."

        pieces = split_final_post(build_post(markdown_content=body))

        assert [PNG_DATA_URL] == [url for piece in pieces for url in piece.images]

    def test_the_cover_image_leads_the_first_thread(self):
        post = build_post(featured_image=build_image(JPG_DATA_URL))

        pieces = split_final_post(post)

        assert pieces[0].images[0] == JPG_DATA_URL

    def test_the_image_markup_does_not_stay_in_the_text(self):
        body = f"앞 문단입니다.\n\n![설명]({PNG_DATA_URL})\n\n뒷 문단입니다."

        joined = " ".join(piece.text for piece in split_final_post(build_post(markdown_content=body)))

        assert "data:image" not in joined
        assert "![" not in joined

    def test_too_many_images_spill_into_the_next_thread(self):
        # 서로 다른 사진이어야 한다 — 같은 data URL은 한 번만 올라간다(중복 제거).
        body = "\n\n".join(
            f"![설명{n}](data:image/png;base64,{base64.b64encode(PNG_BYTES + bytes([n])).decode()})"
            for n in range(MAX_IMAGES_PER_THREAD + 2)
        )

        pieces = split_final_post(build_post(markdown_content=body, title="", hashtags=[]))

        assert all(len(piece.images) <= MAX_IMAGES_PER_THREAD for piece in pieces)
        assert sum(len(piece.images) for piece in pieces) == MAX_IMAGES_PER_THREAD + 2

    def test_the_caption_does_not_survive_as_orphan_text(self):
        """캡션은 사진과 함께 보라고 붙인 설명이다 — 사진 없이 글자만 남으면 뜬금없다.

        실사용(2026-08-04): 게시된 글에 `출처 : "imgnews.naver.net"`만 덩그러니 남았다.
        원고 마크다운의 이미지 블록이 `![alt](…)\\n*출처 : …*` 한 덩어리이기 때문이다.
        """
        body = f'앞 문단입니다.\n\n![설명]({PNG_DATA_URL})\n*출처 : "imgnews.naver.net"*\n\n뒷 문단입니다.'

        pieces = split_final_post(build_post(markdown_content=body))
        joined = " ".join(piece.text for piece in pieces)

        assert "출처" not in joined
        assert "앞 문단입니다." in joined and "뒷 문단입니다." in joined
        # 사진 자체는 그대로 올라간다.
        assert PNG_DATA_URL in [url for piece in pieces for url in piece.images]

    def test_a_cover_already_in_the_body_is_not_uploaded_twice(self):
        """표지가 본문 첫머리에 또 박혀 있는 원고가 많다 — 같은 사진을 두 번 올리지 않는다."""
        body = f"![표지]({JPG_DATA_URL})\n\n본문 문단입니다."
        post = build_post(markdown_content=body, featured_image=build_image(JPG_DATA_URL))

        urls = [url for piece in split_final_post(post) for url in piece.images]

        assert urls == [JPG_DATA_URL]

    def test_the_same_image_twice_in_the_body_is_uploaded_once(self):
        body = f"![a]({PNG_DATA_URL})\n\n가운데 문단입니다.\n\n![b]({PNG_DATA_URL})"

        urls = [url for piece in split_final_post(build_post(markdown_content=body)) for url in piece.images]

        assert urls == [PNG_DATA_URL]

    def test_leftover_image_placeholders_never_reach_threads(self):
        body = "앞 문단. [[IMAGE: a scene | alt=설명]] 뒷 문단. [[VISUAL: chart-1]] 끝."

        joined = " ".join(piece.text for piece in split_final_post(build_post(markdown_content=body)))

        assert "[[" not in joined
        assert "앞 문단." in joined and "뒷 문단." in joined


class TestMarkdownDecorationIsStripped:
    """스레드에는 서식이 없다 — 기호를 두면 `## 제목`이 글자로 보인다."""

    @pytest.mark.parametrize(
        "source,expected",
        [
            ("## 소제목입니다", "소제목입니다"),
            ("**굵은 글씨**입니다", "굵은 글씨입니다"),
            ("*기울임*입니다", "기울임입니다"),
            ("__굵게__입니다", "굵게입니다"),
            ("`코드`입니다", "코드입니다"),
            ("> 인용문입니다", "인용문입니다"),
            ("[링크 글자](https://example.com)입니다", "링크 글자입니다"),
        ],
    )
    def test_symbols_go_but_words_stay(self, source: str, expected: str):
        assert plain_text(source) == expected

    def test_a_lone_underscore_is_left_alone(self):
        """밑줄 하나는 벗기지 않는다 — `snake_case_이름`이 붙어 버린다.

        기울임은 `*한 개*`로도 쓸 수 있으니 손해가 없다.
        """
        assert plain_text("설정은 article_length_targets 입니다") == (
            "설정은 article_length_targets 입니다"
        )

    def test_line_breaks_inside_a_paragraph_survive(self):
        """원고의 줄 나눔은 스레드에서도 그대로 보여야 한다."""
        assert plain_text("첫 줄\n둘째 줄") == "첫 줄\n둘째 줄"

    def test_naver_sticker_markers_never_reach_threads(self):
        """[[STICKER: …]]는 네이버 발행 전용 자리 표식이다(2026-08-10) —
        스레드에는 스티커가 없으므로 글자로도 남으면 안 된다."""
        source = "첫 문단.\n\n[[STICKER: 뿌듯]]\n\n둘째 문단."

        text = plain_text(source)

        assert "STICKER" not in text and "뿌듯" not in text
        assert "첫 문단." in text and "둘째 문단." in text

    def test_a_body_without_markdown_still_works(self):
        """옛 문서에는 markdownContent가 없다 — body로 물러선다."""
        post = build_post(markdown_content=None, body="옛 문서의 본문입니다.")

        assert "옛 문서의 본문입니다." in split_final_post(post)[0].text


class TestTablesBecomeReadableLists:
    """마크다운 표는 스레드에서 렌더링되지 않는다 — `|` 기호가 글자 그대로 올라갔다
    (2026-08-04 사용자 보고). 행마다 첫 칸을 제목 줄로, 나머지를 "머리글: 값"으로 푼다.
    """

    TABLE = (
        "| 자료 종류 | 확인에 적합한 것 | 주의점 |\n"
        "| --- | --- | --- |\n"
        "| 유튜브 공개 영상 | 방송 형태, 활동 모습 | 전체 이력은 안 보임 |\n"
        "| 위키 정리 문서 | 프로필, 연혁 순서 | 편집 시점에 따라 변동 |"
    )

    def test_rows_become_labeled_lines_and_pipes_disappear(self):
        text = plain_text(self.TABLE)

        assert "|" not in text
        assert "---" not in text
        assert "• 유튜브 공개 영상" in text
        assert "확인에 적합한 것: 방송 형태, 활동 모습" in text
        assert "주의점: 전체 이력은 안 보임" in text
        assert "• 위키 정리 문서" in text

    def test_every_cell_word_survives(self):
        """자르는 규칙과 같은 원칙 — 낱말을 버리지 않는다(첫 열 머리글만 예외)."""
        text = plain_text(self.TABLE)

        for cell in ("프로필, 연혁 순서", "편집 시점에 따라 변동", "확인에 적합한 것", "주의점"):
            assert cell in text

    def test_text_around_the_table_is_untouched(self):
        source = f"표로 정리하면 이렇다.\n{self.TABLE}\n표는 여기까지다."
        text = plain_text(source)

        assert text.startswith("표로 정리하면 이렇다.")
        assert text.endswith("표는 여기까지다.")

    def test_a_pipe_in_prose_is_not_mistaken_for_a_table(self):
        """구분선(`---`) 줄이 없으면 표가 아니다 — 문장을 재조립하면 안 된다."""
        source = "A | B 중 하나를 고르세요"

        assert plain_text(source) == source

    def test_emphasis_inside_cells_is_still_stripped(self):
        source = "| 항목 | 내용 |\n| --- | --- |\n| **강조** | `코드`값 |"
        text = plain_text(source)

        assert "• 강조" in text
        assert "내용: 코드값" in text

    def test_empty_cells_are_skipped_without_stray_labels(self):
        source = "| 항목 | 내용 | 비고 |\n| --- | --- | --- |\n| 첫째 |  | 비고만 있음 |"
        text = plain_text(source)

        assert "• 첫째" in text
        assert "내용:" not in text
        assert "비고: 비고만 있음" in text

    def test_a_table_flows_into_threads_without_pipes(self):
        """발행 경로 전체를 통과해도 기호가 남지 않는다."""
        post = build_post(markdown_content=f"여는 문단입니다.\n\n{self.TABLE}\n\n닫는 문단입니다.")

        joined = " ".join(piece.text for piece in split_final_post(post))

        assert "|" not in joined
        assert "유튜브 공개 영상" in joined
        assert "닫는 문단입니다." in joined


class TestDecodingImages:
    def test_a_png_data_url_becomes_bytes(self):
        decoded = decode_data_url(PNG_DATA_URL)

        assert decoded is not None
        raw, suffix = decoded
        assert raw == PNG_BYTES
        assert suffix == ".png"

    def test_jpeg_gets_a_jpg_suffix(self):
        assert decode_data_url(JPG_DATA_URL)[1] == ".jpg"

    @pytest.mark.parametrize(
        "value",
        [
            "https://example.com/a.png",  # 외부 URL은 파일로 풀 수 없다
            "data:image/png,not-base64",  # base64가 아니다
            "data:image/png;base64,",  # 내용이 없다
            "data:image/png;base64,@@@",  # 깨진 payload
        ],
    )
    def test_what_cannot_be_decoded_returns_none(self, value: str):
        """그림 한 장이 깨졌다고 예외를 올리지 않는다 — 발행기가 그 장만 건너뛴다."""
        assert decode_data_url(value) is None


class TestCoverImage:
    """스레드 전용 원고에 붙일 표지 그림 한 장(2026-08-06).

    전용 원고는 글을 새로 쓰므로 본문 속 그림이 어느 스레드에 붙을지 알 수 없다.
    그래도 사진이 하나도 없으면 스레드에서 눈에 띄지 않으므로 표지만 첫 스레드에 붙인다.
    """

    def test_featured_image_is_the_cover(self):
        post = build_post(featured_image=build_image())

        assert cover_image_of(post) == PNG_DATA_URL

    def test_without_a_featured_image_the_first_one_in_the_body_is_used(self):
        post = build_post(markdown_content=f"문단.\n\n![그림]({JPG_DATA_URL})\n\n다음 문단.")

        assert cover_image_of(post) == JPG_DATA_URL

    def test_a_post_without_pictures_has_no_cover(self):
        assert cover_image_of(build_post()) is None

    def test_no_post_no_cover(self):
        assert cover_image_of(None) is None
