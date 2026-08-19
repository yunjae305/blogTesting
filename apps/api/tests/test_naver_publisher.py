"""Auto publishing to 네이버.

What is *not* here: a test that drives the real 스마트에디터. It needs a real Naver
account and a real login, and neither belongs in a test suite. The selectors in
naver/editor.py are therefore unverified against the live editor — they are the part
most likely to break, and the failure is loud (RuntimeError naming what was not found)
rather than a post that silently goes nowhere.

What is here is everything that can be checked without Naver: the publish plan
(스캐폴드 1회 붙여넣기 + 이미지 앵커)가 원고를 어떻게 바꾸는지, 에디터 호출 순서가
계약대로인지(본문은 한 번만, 이미지는 앵커 순서대로, 실패는 즉시 중단), 그리고 발행 전
DOM 검증 규칙이 잘못된 결과를 발행 직전에 막는지.
"""

import asyncio
import base64
import re
import threading
from pathlib import Path

import pytest

from app.posting import (
    NaverBrowserPublisher,
    NaverConfig,
    PublishJob,
    article_html,
    build_naver_publish_plan,
)
from app.posting.config import naver_config_from_env, naver_profile_dir
from app.posting import config as config_module
from app.posting.config import (
    forget_blog_address,
    observed_blog_address,
    remember_blog_address,
)
from app.posting.naver import SmartEditorOne, _in_browser_thread, _type_input_value
from app.posting.naver.browser import _dismiss_stray_alert, _reset_bloated_preferences
from app.posting.naver.login import NaverLogin
from app.posting.naver import publisher as publisher_module
from app.posting.credentials import (
    NaverCredentials,
    forget_session_account,
    remember_session_account,
    save_credentials,
    session_account,
)
from app.posting.naver.constants import ANCHOR_SELECT_ATTEMPTS
from app.posting.naver.constants import SESSION_COOKIES
from app.posting.naver.constants import WRITE_REDIRECT_URL
from app.posting.naver.editor import _selection_is_on_anchor
from app.posting.naver.editor import _check_publish_plan
from app.posting.naver.editor import _summarize_kinds
from app.posting.naver.plan import (
    ANCHOR_TOKEN_PATTERN,
    NAVER_TITLE_MAX_CHARS,
    NaverImageAnchor,
    NaverPlanError,
    naver_title,
)
from app.shared import (
    FinalPost,
    GeneratedPostImage,
    PostingMethod,
    PostingResultStatus,
)

NOW = "1970-01-01T00:00:00.000Z"


@pytest.fixture(autouse=True)
def clipboard_mode(monkeypatch):
    """이 파일의 붙여넣기 흐름 테스트는 클립보드 경로를 전제로 쓰였다. 기본 모드가
    synthetic으로 바뀌어(2026-08-19) 클립보드 모드를 명시한다 — synthetic 경로는
    test_synthetic_paste.py가 검증한다."""
    monkeypatch.setenv("NAVER_PASTE_MODE", "clipboard")

_TEST_POSTING_CREDENTIALS_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode(
    "ascii"
).rstrip("=")


@pytest.fixture(autouse=True)
def _portable_posting_credentials_key(monkeypatch):
    """Keep credential/session-marker tests portable on non-Windows CI."""
    monkeypatch.setenv("POSTING_CREDENTIALS_KEY", _TEST_POSTING_CREDENTIALS_KEY)


def build_image(index: int) -> GeneratedPostImage:
    return GeneratedPostImage(
        # 실제 base64 — 발행 계획이 바이트로 디코드해 앵커 교체용 이미지로 만든다.
        data_url=f"data:image/jpeg;base64,{base64.b64encode(f'image{index}'.encode()).decode()}",
        alt_text=f"이미지 {index}",
        prompt="p",
        provider="openai",
        model="gpt-image-2",
        generated_at=NOW,
        mime_type="image/jpeg",
        source="generated",
    )


def build_post(**overrides) -> FinalPost:
    images = [build_image(0), build_image(1)]
    defaults = dict(
        title="제목",
        body="본문",
        hashtags=["AI", "블로그"],
        images=images,
        featured_image=images[0],
        html_content=(
            f'<article><h1>제목</h1><figure><img src="{images[0].data_url}" alt="a" /></figure>'
            f'<p>첫 문단.</p><figure><img src="{images[1].data_url}" alt="b" /></figure></article>'
        ),
        markdown_content="# 제목",
    )
    return FinalPost(**{**defaults, **overrides})


def build_job(method: PostingMethod = PostingMethod.AUTO) -> PublishJob:
    return PublishJob(post_id="post_1", user_id="user_1", method=method, final_post=build_post())


class TestArticleHtml:
    """네이버 refuses base64 — "허용되지 않는 형식의 이미지가 있어 해당 이미지는
    제외됩니다" — and takes an image it can fetch. Pasting the stored data URLs is
    exactly the thing that does not work."""

    def test_the_images_become_urls_the_editor_can_fetch(self):
        post = build_post()

        html = article_html(post, "post_1", "http://localhost:3000")

        assert "data:image" not in html
        assert re.search(
            r'src="http://localhost:3000/posts/post_1/images/0\?exp=\d+&sig=[A-Za-z0-9_-]+"',
            html,
        )
        assert re.search(
            r'src="http://localhost:3000/posts/post_1/images/1\?exp=\d+&sig=[A-Za-z0-9_-]+"',
            html,
        )

    def test_the_hashtags_come_along(self):
        html = article_html(build_post(), "post_1", "http://localhost:3000")

        assert "#AI #블로그" in html

    def test_a_post_with_no_images_is_left_alone(self):
        post = build_post(images=None, featured_image=None, html_content="<article><p>글.</p></article>")

        html = article_html(post, "post_1", "http://localhost:3000")

        assert "<p>글.</p>" in html


class TestNaverTitle:
    """제목 칸에 들어갈 한 줄 정리. 장식만 걷어내고 뜻은 그대로 둔다."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # 마크다운 잔재
            ("**아이폰17 구매 전 확인할 점**", "아이폰17 구매 전 확인할 점"),
            ("## 아이폰17 정리", "아이폰17 정리"),
            ("`아이폰17` 정리", "아이폰17 정리"),
            # 감싼 따옴표·괄호
            ('"아이폰17 정리"', "아이폰17 정리"),
            ("“아이폰17 정리”", "아이폰17 정리"),
            ("『아이폰17 정리』", "아이폰17 정리"),
            # 앞머리 라벨
            ("[리뷰] 아이폰17 정리", "아이폰17 정리"),
            ("【정보】아이폰17 정리", "아이폰17 정리"),
            # 이모지·반복 구두점·말줄임
            ("아이폰17 정리 ✨🔥", "아이폰17 정리"),
            ("아이폰17 정말 좋을까???", "아이폰17 정말 좋을까?"),
            ("아이폰17 대박!!!", "아이폰17 대박!"),
            ("아이폰17 정리...", "아이폰17 정리"),
            # 공백·꼬리 구두점
            ("아이폰17   정리", "아이폰17 정리"),
            ("아이폰17 정리 .", "아이폰17 정리"),
            ("아이폰17 정리 -", "아이폰17 정리"),
            ("아이폰17 정리 , 가격까지", "아이폰17 정리, 가격까지"),
        ],
    )
    def test_decoration_is_removed(self, raw, expected):
        assert naver_title(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "아이폰17 vs 갤럭시 Z 폴드, 무엇이 다를까?",
            "아이폰17 정말 살 만한가!",
            "프로미스나인 멤버 구성과 현재 활동 정리",
            "리아 두툼새우 버거, 출시 정보와 메뉴 구성",
            "C_STYLE_2026 모델명은 그대로 둔다",
        ],
    )
    def test_a_clean_title_is_left_exactly_as_it_is(self, raw):
        """멀쩡한 제목을 건드리면 뜻이 바뀐다 — 물음표·느낌표·낱말 안 기호는 남는다."""
        assert naver_title(raw) == raw

    def test_a_quoted_phrase_inside_the_title_is_not_unwrapped(self):
        """감싼 따옴표만 벗긴다. 제목 안의 인용은 뜻이라 남겨야 한다."""
        assert naver_title('그가 말한 "진짜 이유"는 무엇일까') == '그가 말한 "진짜 이유"는 무엇일까'


class TestNaverPublishPlan:
    """본문은 앵커 토큰이 든 스캐폴드 하나로, 이미지는 실제 바이트로 — 순서는 원고 그대로.

    예전 article_segments는 이미지 기준으로 HTML을 정규식으로 잘라 번갈아 붙였고, 그
    방식이 커서·서식 상태에 따라 순서를 꼬았다. 계획은 그 분할 자체를 없앤다.
    """

    def test_the_scaffold_replaces_every_image_with_an_anchor_paragraph(self):
        plan = build_naver_publish_plan(build_post(), "post_1")

        assert "<img" not in plan.scaffold_html
        assert "data:image" not in plan.scaffold_html
        tokens = [anchor.token for anchor in plan.image_anchors]
        assert len(tokens) == 2 and len(set(tokens)) == 2
        for token in tokens:
            assert ANCHOR_TOKEN_PATTERN.fullmatch(token), token
            assert f"<p>{token}</p>" in plan.scaffold_html
        assert plan.image_anchors[0].image_bytes == b"image0"
        assert plan.image_anchors[1].image_bytes == b"image1"

    def test_the_title_h1_is_removed_from_the_body(self):
        """제목은 네이버 제목 칸에만 들어간다 — 본문에 남으면 두 번 보인다."""
        plan = build_naver_publish_plan(build_post(), "post_1")

        assert "<h1" not in plan.scaffold_html
        assert "제목" not in plan.expected_text_blocks
        assert plan.title == "제목"

    def test_another_h1_is_demoted_to_h2(self):
        post = build_post(
            html_content="<article><h1>제목</h1><p>본문.</p><h1>다른 큰제목</h1></article>",
            images=None,
            featured_image=None,
        )

        plan = build_naver_publish_plan(post, "post_1")

        assert "<h2>다른 큰제목</h2>" in plan.scaffold_html
        assert "<h1" not in plan.scaffold_html

    @pytest.mark.parametrize(
        "html",
        [
            "<article><h2>제목</h2><p>본문.</p></article>",
            "<article><p>제목</p><p>본문.</p></article>",
            "<article><p><strong>제목</strong></p><p>본문.</p></article>",
            # 두 번째 h1은 위 규칙에서 h2로 강등돼 본문에 남는다 — 그것도 맨 앞이면 뺀다.
            "<article><h1>제목</h1><h1>제목</h1><p>본문.</p></article>",
        ],
        ids=["h2", "paragraph", "bold-paragraph", "h1-twice"],
    )
    def test_a_repeated_title_at_the_top_is_removed_whatever_its_tag(self, html):
        """맨 앞에서 제목을 되풀이하면 태그가 무엇이든 뺀다.

        원고가 늘 `<h1>제목</h1>`으로 시작하지는 않는다. 비평 통합 재작성이 제목을 h2나
        문단으로 다시 적어 넣으면 예전 규칙(h1만 제거)을 빠져나가 본문 첫 줄이 제목과
        같아졌고, 발행 전 검증이 '제목 본문 중복'으로 글을 막았다 — 2026-08-10 실발행
        (GS25 글)이 발행 버튼도 눌러 보지 못하고 여기서 멈췄다.
        """
        post = build_post(html_content=html, images=None, featured_image=None)

        plan = build_naver_publish_plan(post, "post_1")

        assert plan.expected_text_blocks[0] != plan.title
        assert "제목" not in plan.expected_text_blocks
        assert plan.expected_text_blocks[0] == "본문."

    def test_a_second_title_behind_a_photo_caption_is_removed_too(self):
        """실제로 막혔던 모양 그대로(2026-08-10 예약 포스팅, 두 번 연속).

            <h1>제목</h1> → <figure><img></figure> → <p><em>출처: …</em></p> → <h1>제목</h1>

        앞의 h1은 파서가 빼고 뒤의 h1은 h2로 강등돼 남는데, 그 사이의 출처 문단은
        사진 설명으로 빠져나간다. 그래서 **캡션을 뺀 뒤에야** 무엇이 본문 첫 줄인지
        알 수 있다 — 파싱 시점의 '맨 앞'으로 판단하면 이 경우를 놓친다(실제로 놓쳤다).
        """
        image = build_image(0)
        post = build_post(
            images=[image],
            featured_image=image,
            html_content=(
                "<article><h1>제목</h1>"
                f'<figure><img src="{image.data_url}" alt="a" /></figure>'
                "<p><em>출처: example.com</em></p>"
                "<h1>제목</h1><p>첫 문단.</p></article>"
            ),
        )

        plan = build_naver_publish_plan(post, "post_1")

        # 출처는 본문이 아니라 사진 설명으로 갔고, 되풀이된 제목은 빠졌다.
        assert plan.image_anchors[0].caption == "출처: example.com"
        assert plan.expected_text_blocks == ("첫 문단.",)
        assert "제목" not in plan.scaffold_html

    def test_the_same_sentence_in_the_middle_of_the_body_is_kept(self):
        """맨 앞에서만 뺀다 — 글 한가운데의 같은 문장은 본문의 일부일 수 있다."""
        post = build_post(
            html_content="<article><h1>제목</h1><p>첫 문단.</p><p>제목</p></article>",
            images=None,
            featured_image=None,
        )

        plan = build_naver_publish_plan(post, "post_1")

        assert plan.expected_text_blocks == ("첫 문단.", "제목")

    def test_anchor_neighbours_follow_the_manuscript(self):
        plan = build_naver_publish_plan(build_post(), "post_1")

        first, second = plan.image_anchors
        assert first.expected_previous_text is None  # 글 맨 위 이미지
        assert first.expected_next_text == "첫 문단."
        assert second.expected_previous_text == "첫 문단."
        assert second.expected_next_text is None  # 글 끝 이미지

    def test_consecutive_images_get_distinct_anchors_in_order(self):
        img0, img1 = build_image(0), build_image(1)
        post = build_post(
            html_content=(
                f"<article><h1>제목</h1><p>앞 문단.</p>"
                f'<figure><img src="{img0.data_url}" alt="a" /></figure>'
                f'<figure><img src="{img1.data_url}" alt="b" /></figure>'
                f"<p>뒷 문단.</p></article>"
            )
        )

        plan = build_naver_publish_plan(post, "post_1")

        first, second = plan.image_anchors
        assert first.token != second.token
        assert plan.scaffold_html.index(first.token) < plan.scaffold_html.index(second.token)

    def test_a_caption_paragraph_moves_out_of_the_body_onto_its_anchor(self):
        """캡션은 네이버 '사진 설명' 칸으로 간다. 본문에도 남기면 출처가 두 번 찍힌다.

        (예전에는 스캐폴드에 문단으로 남겼다 — 사진과 떨어져 보이고 중복이었다.)
        """
        img = build_image(0)
        post = build_post(
            html_content=(
                f"<article><h1>제목</h1><h2>소제목</h2>"
                f'<figure><img src="{img.data_url}" alt="a" /></figure>'
                f'<p class="visual-caption"><em>캡션입니다.</em></p>'
                f"<p>본문 문단.</p></article>"
            ),
            images=[img],
            featured_image=img,
        )

        plan = build_naver_publish_plan(post, "post_1")

        anchor = plan.image_anchors[0]
        assert anchor.caption == "캡션입니다."
        assert anchor.expected_previous_text == "소제목"
        # 캡션이 빠진 자리의 다음 텍스트는 실제 본문 문단이다.
        assert anchor.expected_next_text == "본문 문단."
        assert f"<p>{anchor.token}</p><p>본문 문단.</p>" in plan.scaffold_html
        assert "캡션입니다." not in plan.scaffold_html

    def test_editor_only_attributes_are_stripped(self):
        post = build_post(
            html_content=(
                '<article><h1>제목</h1><p class="lead" style="color:red" data-x="1" '
                'aria-label="a">본문 <strong class="x">강조</strong>.</p></article>'
            ),
            images=None,
            featured_image=None,
        )

        plan = build_naver_publish_plan(post, "post_1")

        for needle in ("class=", "style=", "data-", "aria-"):
            assert needle not in plan.scaffold_html
        assert "<strong>강조</strong>" in plan.scaffold_html

    def test_script_content_is_dropped_entirely(self):
        post = build_post(
            html_content="<article><h1>제목</h1><p>본문.</p><script>alert(1)</script></article>",
            images=None,
            featured_image=None,
        )

        plan = build_naver_publish_plan(post, "post_1")

        assert "alert" not in plan.scaffold_html
        assert all("alert" not in block for block in plan.expected_text_blocks)

    def test_strong_does_not_cross_block_boundaries(self):
        """소제목·강조가 다음 문단으로 번지는 입력을 스캐폴드 단계에서 차단한다."""
        post = build_post(
            html_content="<article><h1>제목</h1><p><strong>굵게 시작<p>다음 문단</p></article>",
            images=None,
            featured_image=None,
        )

        plan = build_naver_publish_plan(post, "post_1")

        assert "<p><strong>굵게 시작</strong></p>" in plan.scaffold_html
        assert "<p>다음 문단</p>" in plan.scaffold_html

    def test_an_undecodable_image_is_an_error_not_a_silent_skip(self):
        post = build_post(
            html_content=(
                '<article><h1>제목</h1><figure>'
                '<img src="data:image/png;base64,%%%" alt="x" /></figure></article>'
            )
        )

        with pytest.raises(NaverPlanError):
            build_naver_publish_plan(post, "post_1")

    def test_a_non_data_image_src_is_refused(self):
        """외부 URL 이미지는 발행 후 네이버가 호스팅하지 않아 깨진다 — 조용히 넘기지 않는다."""
        post = build_post(
            html_content=(
                '<article><h1>제목</h1><figure>'
                '<img src="http://example.com/a.png" alt="x" /></figure></article>'
            )
        )

        with pytest.raises(NaverPlanError):
            build_naver_publish_plan(post, "post_1")

    def test_list_items_become_separate_expected_blocks(self):
        post = build_post(
            html_content="<article><h1>제목</h1><ul><li>하나</li><li>둘</li></ul></article>",
            images=None,
            featured_image=None,
        )

        plan = build_naver_publish_plan(post, "post_1")

        assert "하나" in plan.expected_text_blocks
        assert "둘" in plan.expected_text_blocks

    def test_plain_paragraphs_exclude_headings_and_fully_bold_ones(self):
        """굵기 번짐 검증 목록에는 '통째로 굵으면 안 되는' 문단만 들어간다."""
        post = build_post(
            html_content=(
                "<article><h1>제목</h1><h2>소제목</h2>"
                "<p><strong>통째로 굵은 문단</strong></p>"
                "<p>일반 문단 <strong>부분 강조</strong> 포함.</p></article>"
            ),
            images=None,
            featured_image=None,
        )

        plan = build_naver_publish_plan(post, "post_1")

        assert plan.plain_paragraph_texts == ("일반 문단 부분 강조 포함.",)

    def test_the_plan_title_is_the_cleaned_one_line(self):
        """제목 칸은 목록·검색 결과에 그대로 실리는 한 줄이다 — 장식이 남으면 안 된다."""
        post = build_post(
            title='**"[리뷰] 아이폰17 구매 전 확인할 점"**  ✨',
            html_content='<article><h1>제목</h1><p>첫 문단.</p></article>',
        )
        plan = build_naver_publish_plan(post, "post_1")
        assert plan.title == "아이폰17 구매 전 확인할 점"

    def test_cleaning_the_title_never_drops_a_word(self):
        """장식만 걷어낸다 — 낱말을 빼거나 줄여서 뜻을 바꾸지 않는다."""
        original = "외모지상주의로 시작된 박태준 작가 연결 세계관 한눈에 정리"
        plan = build_naver_publish_plan(build_post(title=original), "post_1")
        assert plan.title == original

    def test_the_body_h1_is_removed_even_when_it_carries_the_decoration(self):
        """본문 h1은 정리 **전** 제목이다. 정리본만 대조하면 h1이 남아 제목이 두 번 보인다."""
        decorated = '**"제목"**'
        post = build_post(
            title=decorated,
            html_content=f"<article><h1>{decorated}</h1><p>첫 문단.</p></article>",
        )
        plan = build_naver_publish_plan(post, "post_1")

        assert plan.title == "제목"
        assert "<h1" not in plan.scaffold_html
        assert "제목" not in plan.expected_text_blocks

    def test_a_title_longer_than_naver_allows_is_cut_on_a_word_boundary(self):
        """네이버가 조용히 자르면 붙여넣은 제목과 에디터 제목이 달라져 검증이 글을 막는다.
        우리가 먼저 자르면 둘이 같은 문자열이라 발행이 진행된다."""
        long_title = " ".join(["아주긴제목단어"] * 20)
        plan = build_naver_publish_plan(build_post(title=long_title), "post_1")

        assert len(plan.title) <= NAVER_TITLE_MAX_CHARS
        assert not plan.title.endswith(" ")
        assert plan.title.split()[0] == "아주긴제목단어"

    def test_a_title_that_is_only_a_label_is_left_alone(self):
        """라벨을 떼면 빈 제목이 되는 경우에는 떼지 않는다 — 빈 제목은 발행이 막힌다."""
        plan = build_naver_publish_plan(build_post(title="[리뷰]"), "post_1")
        assert plan.title == "[리뷰]"

    def test_tokens_differ_between_posts_but_are_stable_for_one(self):
        first = build_naver_publish_plan(build_post(), "post_1")
        second = build_naver_publish_plan(build_post(), "post_2")
        again = build_naver_publish_plan(build_post(), "post_1")

        assert first.image_anchors[0].token != second.image_anchors[0].token
        assert first.image_anchors[0].token == again.image_anchors[0].token

    def test_a_post_with_no_images_has_no_anchors(self):
        post = build_post(
            images=None, featured_image=None, html_content="<article><h1>제목</h1><p>글.</p></article>"
        )

        plan = build_naver_publish_plan(post, "post_1")

        assert plan.image_anchors == ()
        assert plan.scaffold_html == "<p>글.</p>"

    def test_text_blocks_are_separated_by_a_blank_paragraph(self):
        """실사례(2026-08-03): 발행된 글의 문단이 빈 줄 없이 딱 붙어 나왔다.

        블록을 구분자 없이 이어 붙여 `</p><p>`·`</p><h2>`가 그대로 들어간 탓이다.
        """
        post = build_post(
            images=None,
            featured_image=None,
            html_content=(
                "<article><h1>제목</h1><p>첫 문단.</p><h2>소제목</h2>"
                "<p>둘째 문단.</p></article>"
            ),
        )

        plan = build_naver_publish_plan(post, "post_1")

        assert plan.scaffold_html == (
            "<p>첫 문단.</p><p><br /></p><h2>소제목</h2><p><br /></p><p>둘째 문단.</p>"
        )
        # 빈 문단은 '내용'이 아니다 — 기대 텍스트·굵기 검증 목록을 오염시키면 안 된다.
        # (빈 문자열이 섞이면 굵기 번짐 검사에서 모든 문단이 오탐된다.)
        assert plan.expected_text_blocks == ("첫 문단.", "소제목", "둘째 문단.")
        assert plan.plain_paragraph_texts == ("첫 문단.", "둘째 문단.")
        assert "" not in plan.expected_text_blocks

    def test_image_anchors_keep_their_neighbours_tight(self):
        """앵커 주변에는 빈 문단을 넣지 않는다.

        캡션은 앵커 바로 다음 블록이어야 하고, 앵커를 클릭해 줄을 선택하는 삽입 경로는
        문서 높이 변화에 예민하다. 이미지 여백은 네이버가 알아서 준다.
        """
        img = build_image(0)
        post = build_post(
            html_content=(
                f"<article><h1>제목</h1><p>앞 문단.</p>"
                f'<figure><img src="{img.data_url}" alt="설명" /></figure>'
                f'<p class="visual-caption"><em>캡션입니다.</em></p>'
                f"<p>뒷 문단.</p></article>"
            ),
            images=[img],
            featured_image=img,
        )

        plan = build_naver_publish_plan(post, "post_1")

        token = plan.image_anchors[0].token
        assert plan.image_anchors[0].caption == "캡션입니다."
        assert f"<p>앞 문단.</p><p>{token}</p><p>뒷 문단.</p>" in plan.scaffold_html
        assert "<p><br /></p>" not in plan.scaffold_html

    def test_an_empty_body_is_refused(self):
        post = build_post(
            images=None, featured_image=None, html_content="<article><h1>제목</h1></article>"
        )

        with pytest.raises(NaverPlanError):
            build_naver_publish_plan(post, "post_1")

    def test_the_plain_text_fallback_carries_the_anchor_tokens(self):
        plan = build_naver_publish_plan(build_post(), "post_1")

        for anchor in plan.image_anchors:
            assert anchor.token in plan.scaffold_plain_text
        assert "첫 문단." in plan.scaffold_plain_text


class TestImageCaptionInPlan:
    """사진 출처는 본문 문단이 아니라 네이버 '사진 설명' 칸으로 간다."""

    SOURCE = "사진 출처: imgnews.naver.net (http://imgnews.naver.net/a.jpg)"

    def _post(self, caption: str | None = None):
        image = build_image(0)
        caption_html = (
            f'<p class="visual-caption"><em>{caption}</em></p>' if caption else ""
        )
        return build_post(
            images=[image],
            featured_image=image,
            html_content=(
                f'<article><h1>제목</h1>'
                f'<figure><img src="{image.data_url}" alt="사진" /></figure>'
                f"{caption_html}<p>첫 문단.</p></article>"
            ),
        )

    def test_the_caption_rides_on_the_anchor(self):
        plan = build_naver_publish_plan(self._post(self.SOURCE), "post_1")

        assert plan.image_anchors[0].caption == self.SOURCE

    def test_the_caption_is_not_left_in_the_body(self):
        """본문에도 남기면 사진 아래에 같은 출처가 두 번 찍힌다."""
        plan = build_naver_publish_plan(self._post(self.SOURCE), "post_1")

        assert self.SOURCE not in plan.scaffold_html
        assert self.SOURCE not in plan.scaffold_plain_text
        assert self.SOURCE not in plan.expected_text_blocks
        assert self.SOURCE not in plan.plain_paragraph_texts

    def test_the_paragraph_after_the_caption_is_still_the_anchor_neighbour(self):
        """캡션을 빼면서 이미지 뒤 텍스트 기준까지 잃으면 위치 검증이 헐거워진다."""
        plan = build_naver_publish_plan(self._post(self.SOURCE), "post_1")

        assert plan.image_anchors[0].expected_next_text == "첫 문단."
        assert "첫 문단." in plan.expected_text_blocks

    def test_an_image_without_a_caption_is_unchanged(self):
        plan = build_naver_publish_plan(self._post(None), "post_1")

        assert plan.image_anchors[0].caption is None
        assert plan.image_anchors[0].expected_next_text == "첫 문단."

    def test_an_italic_paragraph_that_follows_no_image_stays_in_the_body(self):
        image = build_image(0)
        post = build_post(
            images=[image],
            featured_image=image,
            html_content=(
                f'<article><h1>제목</h1><p><em>강조한 문단.</em></p>'
                f'<figure><img src="{image.data_url}" alt="사진" /></figure>'
                f"<p>첫 문단.</p></article>"
            ),
        )

        plan = build_naver_publish_plan(post, "post_1")

        assert "강조한 문단." in plan.expected_text_blocks
        assert plan.image_anchors[0].caption is None


class TestLegacyStickerMarkersInPlan:
    """과거 원고의 [[STICKER: 이름]] 마커가 발행물에 글자로 남지 않는다."""

    def _plan(self, html: str):
        return build_naver_publish_plan(
            build_post(html_content=html, images=None, featured_image=None), "post_1"
        )

    def test_a_marker_leaves_no_text_behind(self):
        plan = self._plan(
            "<article><h1>제목</h1><p>첫 문단.</p>"
            "<p>[[STICKER: 뿌듯]]</p><p>둘째 문단.</p></article>"
        )

        assert "STICKER" not in plan.scaffold_html
        assert "STICKER" not in plan.scaffold_plain_text
        assert all("STICKER" not in text for text in plan.expected_text_blocks)

    def test_all_markers_are_removed_from_the_body(self):
        html = (
            "<article><h1>제목</h1>"
            + "".join(
                f"<p>{n}번 문단.</p><p>[[STICKER: 이름{n}]]</p>" for n in range(1, 4)
            )
            + "</article>"
        )

        plan = self._plan(html)

        assert "STICKER" not in plan.scaffold_html

    def test_a_marker_before_any_text_is_removed(self):
        plan = self._plan(
            "<article><h1>제목</h1><p>[[STICKER: 설렘]]</p><p>본문.</p></article>"
        )

        assert "STICKER" not in plan.scaffold_html
        assert "본문." in plan.scaffold_plain_text

    def test_fullwidth_colon_and_lowercase_are_accepted(self):
        plan = self._plan(
            "<article><h1>제목</h1><p>본문.</p><p>[[sticker： 감동]]</p></article>"
        )

        assert "sticker" not in plan.scaffold_html.lower()
        assert plan.scaffold_plain_text == "본문."


class TestFillPublishPlan:
    """에디터 호출 계약: 본문은 한 번만 붙여넣고, 이미지는 앵커 순서대로, 실패는 즉시 중단."""

    def _plan(self, image_count: int = 2):
        html = "<article><h1>제목</h1><p>앞 문단.</p>"
        for index in range(image_count):
            html += f'<figure><img src="{build_image(index).data_url}" alt="i" /></figure>'
        html += "<p>뒷 문단.</p></article>"
        return build_naver_publish_plan(build_post(html_content=html), "post_1")

    def _editor(self, monkeypatch, order: list, fail_on: int | None = None) -> SmartEditorOne:
        editor = SmartEditorOne(None)
        monkeypatch.setattr(editor, "_paste_title", lambda title: order.append(f"title:{title}"))
        monkeypatch.setattr(editor, "_paste_scaffold", lambda plan: order.append("scaffold"))

        def replace(anchor, _plan=None):
            if fail_on is not None and anchor.index == fail_on:
                raise RuntimeError(f"{anchor.index + 1}번째 이미지 교체 실패")
            order.append(f"image:{anchor.index}")

        monkeypatch.setattr(editor, "_replace_anchor_with_image", replace)
        monkeypatch.setattr(editor, "_clear_clipboard", lambda: order.append("clear"))
        return editor

    def test_the_scaffold_is_pasted_exactly_once_before_any_image(self, monkeypatch):
        order: list[str] = []
        plan = self._plan(2)

        self._editor(monkeypatch, order).fill_publish_plan(plan)

        assert order == ["title:제목", "scaffold", "image:0", "image:1", "clear"]

    def test_one_failed_image_stops_everything(self, monkeypatch):
        """이미지가 빠진 글을 '성공'으로 발행하지 않는다 — 다음 이미지로 넘어가지도 않는다."""
        order: list[str] = []
        plan = self._plan(3)
        editor = self._editor(monkeypatch, order, fail_on=1)

        with pytest.raises(RuntimeError, match="2번째 이미지"):
            editor.fill_publish_plan(plan)

        assert "image:0" in order
        assert "image:2" not in order
        assert "clear" not in order


class TestFillImageCaption:
    """'사진 설명' 칸 입력 — 브라우저 없이 드라이버 스텁으로 계약만 확인한다.

    실발행이 여기서 죽었다: 캡션 줄은 이미지를 선택하기 전에는 크기가 0이라
    ``element not interactable ... has no size and location``이 났다. 그래서 (1) 이미지를
    먼저 클릭하고 (2) 화면에 보이는 요소만 고르고 (3) 실패해도 발행을 멈추지 않는다.
    """

    SOURCE = "사진 출처: i1.ruliweb.com (https://i1.ruliweb.com/ori/26/06/17/x.webp)"

    def _anchor(self, caption: str | None = SOURCE) -> NaverImageAnchor:
        return NaverImageAnchor(
            index=0,
            token="__BLOGIT_IMAGE_ABCDEF_001__",
            image_bytes=b"x",
            alt_text="사진",
            caption=caption,
            expected_previous_text=None,
            expected_next_text="첫 문단.",
        )

    def _editor(self, *, caption_found=True, pasted_text=SOURCE, image_missing_times=0):
        """스크립트 인자('image'/'caption')로 무엇을 물었는지 구분하는 드라이버 스텁.

        ``pasted_text``가 리스트면 검증 호출마다 앞에서부터 꺼내 쓴다(마지막 값 반복) —
        '처음엔 안 들어갔다가 키 입력 폴백 후 들어간' 흐름을 흉내 낼 수 있다.

        ``image_missing_times``는 이미지 컴포넌트가 아직 DOM에 자리를 못 잡은 횟수다 —
        교체 직후에 실제로 일어난다(2026-08-10 실발행).
        """

        class FakeDriver:
            def __init__(self):
                self.asked: list[str] = []
                self.image_misses = image_missing_times

            def execute_script(self, _script, *args):
                if len(args) == 2:
                    self.asked.append(args[1])
                    if args[1] == "image":
                        if self.image_misses > 0:
                            self.image_misses -= 1
                            return None
                        return "IMAGE"
                    return "CAPTION" if caption_found else None
                # 검증·진단 호출(인자 1개)
                if isinstance(pasted_text, list):
                    return pasted_text.pop(0) if len(pasted_text) > 1 else pasted_text[0]
                return pasted_text if isinstance(pasted_text, str) else []

        driver = FakeDriver()
        return SmartEditorOne(driver), driver

    def test_the_image_is_clicked_before_the_caption(self, monkeypatch):
        """캡션 줄은 이미지를 선택해야 비로소 크기를 갖는다 — 순서가 곧 버그 수정이다."""
        editor, driver = self._editor()
        clicked: list = []
        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains",
            lambda _d: _FakeChain(clicked),
        )
        pasted: list = []
        monkeypatch.setattr(
            "app.posting.naver.editor._os_clipboard_text",
            lambda text: pasted.append(text) or True,
        )
        monkeypatch.setattr(editor, "_paste_from_clipboard", lambda: clicked.append("paste"))

        assert editor._fill_image_caption(self._anchor(), 0) is True

        assert driver.asked == ["image", "caption"]
        assert clicked == ["move", "click", "perform", "move", "click", "perform", "paste"]
        assert pasted == [self.SOURCE]

    def test_an_image_that_is_not_drawn_yet_is_waited_for(self, monkeypatch):
        """교체 직후에는 이미지 컴포넌트가 아직 DOM에 없을 수 있다 — 기다린다.

        예전에는 한 번만 찾고 포기해서 앞쪽 이미지가 사진 설명을 통째로 잃었다
        (2026-08-10 실발행: 1·2번째는 "찾지 못해", 뒤로 갈수록 시간이 흘러 3번째만 성공).
        """
        editor, driver = self._editor(image_missing_times=2)
        clicked: list = []
        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains",
            lambda _d: _FakeChain(clicked),
        )
        monkeypatch.setattr("app.posting.naver.editor._os_clipboard_text", lambda _t: True)
        monkeypatch.setattr(editor, "_paste_from_clipboard", lambda: None)

        assert editor._fill_image_caption(self._anchor(), 0) is True
        # 두 번 헛치고 세 번째에 찾았다 — 포기하지 않았다.
        assert driver.asked.count("image") == 3

    def test_an_image_that_never_appears_does_not_stop_the_publish(self, monkeypatch):
        """끝내 못 찾아도 발행은 계속한다 — 출처가 빠지는 것보다 못 나가는 것이 나쁘다."""
        editor, _driver = self._editor(image_missing_times=99)
        monkeypatch.setattr("app.posting.naver.editor.CAPTION_IMAGE_TIMEOUT_SECONDS", 0)

        assert editor._fill_image_caption(self._anchor(), 0) is False

    def test_a_missing_caption_field_does_not_stop_the_publish(self, monkeypatch):
        """다 만든 글이 발행되지 못하는 것이 출처가 빠지는 것보다 나쁘다 — 경고만 남긴다."""
        editor, driver = self._editor(caption_found=False)
        clicked: list = []
        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains",
            lambda _d: _FakeChain(clicked),
        )
        monkeypatch.setattr("app.posting.naver.editor.CAPTION_FIELD_TIMEOUT_SECONDS", 0)

        assert editor._fill_image_caption(self._anchor(), 0) is False
        # 첫 대기가 실패하면 이미지를 한 번 더 클릭해 선택을 다시 시도한다.
        assert clicked.count("click") == 2
        assert driver.asked.count("caption") == 2

    def test_a_caption_that_did_not_land_is_reported_as_failure(self, monkeypatch):
        """붙여넣기가 조용히 흘렀는지 읽어서 확인한다 — 넣었다고 믿지 않는다."""
        editor, _driver = self._editor(pasted_text="사진 설명을 입력하세요.")
        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains",
            lambda _d: _FakeChain([]),
        )
        monkeypatch.setattr("app.posting.naver.editor._os_clipboard_text", lambda _t: True)
        monkeypatch.setattr(editor, "_paste_from_clipboard", lambda: None)
        monkeypatch.setattr("app.posting.naver.editor.CAPTION_VERIFY_TIMEOUT_SECONDS", 0)

        assert editor._fill_image_caption(self._anchor(), 0) is False

    def test_the_keystroke_fallback_overwrites_instead_of_appending(self, monkeypatch):
        """폴백이 그냥 치면 이미 들어간 캡션 뒤에 덧붙어 두 번 찍힌다(실측 2026-08-03).

        줄을 선택(END→Shift+HOME)한 뒤 쳐야 덮어쓴다.
        """
        editor, _driver = self._editor(
            pasted_text=["사진 설명을 입력하세요.", self.SOURCE]
        )
        chain_ops: list = []
        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains",
            lambda _d: _FakeChain(chain_ops),
        )
        monkeypatch.setattr("app.posting.naver.editor._os_clipboard_text", lambda _t: True)
        monkeypatch.setattr(editor, "_paste_from_clipboard", lambda: None)
        monkeypatch.setattr("app.posting.naver.editor.CAPTION_VERIFY_TIMEOUT_SECONDS", 0)

        assert editor._fill_image_caption(self._anchor(), 0) is True

        # 키를 치기 전에 줄을 선택한다(Shift 조합이 send_keys보다 먼저 온다).
        assert "key_down" in chain_ops
        assert chain_ops.index("key_down") < chain_ops.index("send_keys", chain_ops.index("key_up"))

    def test_a_failed_paste_falls_back_to_real_keystrokes(self, monkeypatch):
        """붙여넣기가 흐르면 실제 키 입력으로 한 번 더 — 실발행에서 캡션이 조용히 빠진 채
        발행됐던(se-caption 0개) 구멍을 막는다. 목적은 출처가 실리는 것이다."""
        editor, _driver = self._editor(
            pasted_text=["사진 설명을 입력하세요.", self.SOURCE]
        )
        chain_ops: list = []
        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains",
            lambda _d: _FakeChain(chain_ops),
        )
        monkeypatch.setattr("app.posting.naver.editor._os_clipboard_text", lambda _t: True)
        monkeypatch.setattr(editor, "_paste_from_clipboard", lambda: None)
        monkeypatch.setattr("app.posting.naver.editor.CAPTION_VERIFY_TIMEOUT_SECONDS", 0)

        assert editor._fill_image_caption(self._anchor(), 0) is True

        assert "send_keys" in chain_ops

    def test_a_selenium_error_never_escapes(self, monkeypatch):
        """이 단계의 예외가 발행 전체를 죽이면 안 된다(실발행이 그렇게 죽었다)."""
        editor, _driver = self._editor()

        def explode(_d):
            raise RuntimeError("element not interactable")

        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains", explode
        )

        assert editor._fill_image_caption(self._anchor(), 0) is False

    def test_an_image_without_a_caption_never_touches_the_field(self, monkeypatch):
        """캡션이 없는 이미지(생성 사진)는 예전 경로 그대로다."""
        editor = SmartEditorOne(None)
        calls: list = []
        monkeypatch.setattr(editor, "_fill_image_caption", lambda *a: calls.append(a))
        monkeypatch.setattr(editor, "_image_component_count", lambda: 0)
        monkeypatch.setattr(editor, "_select_anchor_token", lambda token: None)
        monkeypatch.setattr("app.posting.naver.editor._os_clipboard_image", lambda _b: True)
        monkeypatch.setattr(editor, "_paste_from_clipboard", lambda: None)
        monkeypatch.setattr(editor, "_anchor_status", lambda _t: {
            "tokenPresent": False, "imageCount": 1, "inlineImageCount": 0
        })

        editor._replace_anchor_with_image(self._anchor(caption=None))

        assert calls == []


class _FakeChain:
    """ActionChains 대역. 실제 입력 순서만 기록한다."""

    def __init__(self, log: list):
        self._log = log

    def move_to_element(self, _element):
        self._log.append("move")
        return self

    def pause(self, _seconds):
        self._log.append("pause")
        return self

    def click(self):
        self._log.append("click")
        return self

    def send_keys(self, *_keys):
        self._log.append("send_keys")
        return self

    def key_down(self, _key):
        self._log.append("key_down")
        return self

    def key_up(self, _key):
        self._log.append("key_up")
        return self

    def perform(self):
        self._log.append("perform")


class _AnchorDriver:
    """앵커 선택 경로의 드라이버 대역. 스크립트 종류별로 다른 값을 돌려준다.

    - 문단 탐색(인자: token) → 전용 문단 하나
    - 위치 측정/스크롤(인자: element) → 좌표 int(안정) 또는 None
    - 선택 확인(인자: element, token) → 선택 상태
    """

    TOKEN = "__BLOGIT_IMAGE_ABCDEF_001__"

    def __init__(self, selection_ok: bool = True, selected_text: str | None = None):
        self.rescrolled = 0
        self.selection_checks = 0
        self._selection_ok = selection_ok
        self._selected_text = selected_text

    def execute_script(self, script, *args):
        if "getSelection" in script:  # 선택 상태 조회
            self.selection_checks += 1
            # 실제 SmartEditor에서 관측되는 모양: 캐럿은 문단 안이고 DOM 선택은 접혀 있다.
            return {
                "text": self._selected_text or "",
                "inside": self._selection_ok,
                "collapsed": True,
                "found": True,
            }
        if "se-text-paragraph" in script:  # 문단 탐색(token)
            return {"ok": True, "count": 1, "dedicated": True, "element": "PARAGRAPH"}
        if "getBoundingClientRect" in script:  # 위치 측정 — 늘 같은 값 = 안정
            return 100
        if args:  # 클릭 재시도 직전의 scrollIntoView
            self.rescrolled += 1
            return None
        return 0  # 이미지 로드 대기: 남은 장수 0


class TestSelectAnchorTokenClickRetry:
    """앵커 문단 클릭의 일시 실패(element not interactable)가 발행 전체를 죽이면 안 된다.

    실발행 1차 시도가 여기서 통째로 실패했다(2026-07-31, HTMLDivElement has no size) —
    레이아웃이 안정되기 전의 크기 0 순간이다. 다시 스크롤해 실제 클릭을 한 번만
    재시도한다. JS 클릭 폴백은 쓰지 않는다: 내부 캐럿이 오지 않아 이미지가 엉뚱한
    위치에 꽂힌다.
    """

    def test_a_transient_click_failure_is_retried_once(self, monkeypatch):
        driver = _AnchorDriver()
        editor = SmartEditorOne(driver)
        performed: list = []
        state = {"failed": False}

        class FlakyChain(_FakeChain):
            """첫 번째 클릭 체인만 element not interactable로 실패한다."""

            def __init__(self, _driver):
                super().__init__(performed)
                self._ops: list = []

            def click(self):
                self._ops.append("click")
                return super().click()

            def perform(self):
                if "click" in self._ops and not state["failed"]:
                    state["failed"] = True
                    raise RuntimeError(
                        "element not interactable: [object HTMLDivElement] has "
                        "no size and location"
                    )
                super().perform()

        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains", FlakyChain
        )

        editor._select_anchor_token("__BLOGIT_IMAGE_ABCDEF_001__")

        assert state["failed"] is True
        assert driver.rescrolled == 1
        # 재시도 클릭 체인과 줄 선택(END→Shift+HOME) 체인이 실제로 수행됐다.
        assert performed.count("perform") == 2
        # 붙여넣기 전에 선택이 실제로 토큰인지 확인한다.
        assert driver.selection_checks == 1

    def test_a_click_that_landed_elsewhere_is_retried_then_refused(self, monkeypatch):
        """클릭이 다른 문단에 떨어지면 **붙여넣기 전에** 알아채고 다시 시도한다.

        실발행(2026-08-03): 표 뒤 3번째 앵커에서 클릭이 빗나가 이미지가 엉뚱한 자리에
        들어갔다. 붙여넣은 뒤에 알아차리면 이미 문서가 망가진 뒤다.
        """
        driver = _AnchorDriver(selection_ok=False)
        editor = SmartEditorOne(driver)
        monkeypatch.setattr(
            "selenium.webdriver.common.action_chains.ActionChains",
            lambda _d: _FakeChain([]),
        )
        monkeypatch.setattr("app.posting.naver.editor.time.sleep", lambda _s: None)

        with pytest.raises(RuntimeError, match="선택하지 못했습니다"):
            editor._select_anchor_token(_AnchorDriver.TOKEN)

        # 정해진 횟수만큼 다시 시도했고, 그 뒤에는 붙여넣지 않고 멈춘다.
        assert driver.selection_checks == ANCHOR_SELECT_ATTEMPTS


class TestSelectionIsOnAnchor:
    """붙여넣기 직전 판정 규칙. 실발행(2026-08-03)에서 이 규칙이 너무 엄해 발행이 통째로
    막혔다 — SmartEditor는 내부 선택 모델을 따로 들고 있어 DOM 선택이 접혀 있어도
    Ctrl+V는 그 줄을 덮어쓴다."""

    TOKEN = "__BLOGIT_IMAGE_ABCDEF_001__"

    def test_a_collapsed_caret_inside_the_paragraph_is_accepted(self):
        """실측된 정상 상태: 선택된 글자는 비어 있고 캐럿만 그 문단 안에 있다."""
        state = {"text": "", "inside": True, "collapsed": True, "found": True}
        assert _selection_is_on_anchor(state, self.TOKEN) is True

    def test_a_caret_in_another_paragraph_is_refused(self):
        """막으려는 것은 이것 하나다 — 클릭이 다른 문단에 떨어진 경우."""
        state = {"text": "", "inside": False, "collapsed": True, "found": True}
        assert _selection_is_on_anchor(state, self.TOKEN) is False

    def test_a_real_selection_must_be_the_token(self):
        """DOM 선택이 실제로 잡혔으면 그 글자가 토큰이어야 한다(이웃 글자 덮어쓰기 방지)."""
        good = {"text": TOKEN_WITH_ZERO_WIDTH, "inside": True, "collapsed": False, "found": True}
        bad = {"text": "다른 문단의 문장", "inside": True, "collapsed": False, "found": True}
        assert _selection_is_on_anchor(good, self.TOKEN) is True
        assert _selection_is_on_anchor(bad, self.TOKEN) is False

    def test_no_selection_at_all_is_refused(self):
        state = {"text": "", "inside": False, "collapsed": True, "found": False}
        assert _selection_is_on_anchor(state, self.TOKEN) is False


# 네이버가 붙여넣은 문단에 끼워 넣는 zero-width 문자가 섞인 형태.
TOKEN_WITH_ZERO_WIDTH = "​__BLOGIT_IMAGE_ABCDEF_001__​"


class TestLayoutSettleBeforeClick:
    """클릭 좌표를 잡기 전에 문서가 멈췄는지 확인한다 — 그런데 확인하면서 스크롤하면 안 된다."""

    def test_it_measures_without_re_centering(self, monkeypatch):
        """폴링마다 scrollIntoView를 부르면 문서가 밀려도 top이 늘 같아, 재는 자가
        스스로 '멈췄다'는 답을 만든다. 위치만 읽어야 한다."""
        scripts: list[str] = []
        positions = iter([100, 340, 340])

        class FakeDriver:
            def execute_script(self, script, *_args):
                scripts.append(script)
                return next(positions, 340)

        monkeypatch.setattr("app.posting.naver.editor.time.sleep", lambda _s: None)
        editor = SmartEditorOne(FakeDriver())

        editor._wait_for_stable_position("PARAGRAPH")

        assert scripts, "위치를 한 번도 읽지 않았다"
        assert all("scrollIntoView" not in script for script in scripts)
        # 100 → 340(움직임) → 340(같음)에서 멈춘다.
        assert len(scripts) == 3

    def test_an_unmeasurable_position_does_not_block(self, monkeypatch):
        """위치를 잴 수 없으면(테스트 스텁·예외) 기다릴 근거가 없다 — 바로 진행한다."""

        class FakeDriver:
            def __init__(self):
                self.calls = 0

            def execute_script(self, _script, *_args):
                self.calls += 1
                return None

        monkeypatch.setattr("app.posting.naver.editor.time.sleep", lambda _s: None)
        driver = FakeDriver()

        SmartEditorOne(driver)._wait_for_stable_position("PARAGRAPH")

        assert driver.calls == 1


class TestMisplacedPasteRecovery:
    """붙여넣기가 엉뚱한 곳에 들어갔을 때 되돌리고 다시 시도한다(2026-08-03 실발행)."""

    def _anchor(self):
        return NaverImageAnchor(
            index=2,
            token="__BLOGIT_IMAGE_ABCDEF_003__",
            image_bytes=b"x",
            alt_text="사진",
            caption=None,
            expected_previous_text=None,
            expected_next_text="다음 문단.",
        )

    def test_a_misplaced_paste_is_undone_and_retried(self, monkeypatch):
        editor = SmartEditorOne(None)
        counts = {"images": 0}
        events: list[str] = []

        # 1회차: 토큰이 남고 이미지만 늘어난다(어긋난 붙여넣기). 2회차: 정상 교체.
        state = {"attempt": 0}

        def paste():
            state["attempt"] += 1
            events.append(f"paste{state['attempt']}")
            counts["images"] += 1

        def anchor_status(_token):
            misplaced = state["attempt"] == 1
            return {
                "tokenPresent": misplaced,
                "imageCount": counts["images"],
                "inlineImageCount": 0,
            }

        def undo(_token, count_before, _plan=None):
            events.append("undo")
            counts["images"] = count_before
            return True

        monkeypatch.setattr(editor, "_image_component_count", lambda: counts["images"])
        monkeypatch.setattr(editor, "_wait_for_images_settled", lambda: events.append("settle"))
        monkeypatch.setattr(editor, "_select_anchor_token", lambda _t: events.append("select"))
        monkeypatch.setattr(editor, "_paste_from_clipboard", paste)
        monkeypatch.setattr(editor, "_anchor_status", anchor_status)
        monkeypatch.setattr(editor, "_undo_misplaced_paste", undo)
        monkeypatch.setattr("app.posting.naver.editor._os_clipboard_image", lambda _b: True)
        monkeypatch.setattr(
            "app.posting.naver.editor.IMAGE_PASTE_TIMEOUT_SECONDS", 0.2
        )

        editor._replace_anchor_with_image(self._anchor())

        # 어긋난 붙여넣기를 되돌리고, 다시 자리를 잡아 붙여넣어 성공했다.
        assert events == [
            "settle", "select", "paste1", "undo",
            "settle", "select", "paste2",
        ]

    def test_an_unrecoverable_misplacement_still_stops_the_publish(self, monkeypatch):
        """되돌리지 못하면 예전처럼 중단한다 — 위치가 어긋난 글을 발행하지 않는다."""
        editor = SmartEditorOne(None)
        monkeypatch.setattr(editor, "_image_component_count", lambda: 1)
        monkeypatch.setattr(editor, "_wait_for_images_settled", lambda: None)
        monkeypatch.setattr(editor, "_select_anchor_token", lambda _t: None)
        monkeypatch.setattr(editor, "_paste_from_clipboard", lambda: None)
        monkeypatch.setattr(
            editor,
            "_anchor_status",
            lambda _t: {"tokenPresent": True, "imageCount": 9, "inlineImageCount": 0},
        )
        monkeypatch.setattr(
            editor, "_undo_misplaced_paste", lambda _t, _c, _p=None: False
        )
        monkeypatch.setattr("app.posting.naver.editor._os_clipboard_image", lambda _b: True)
        monkeypatch.setattr(
            "app.posting.naver.editor.IMAGE_PASTE_TIMEOUT_SECONDS", 0.2
        )

        with pytest.raises(RuntimeError, match="앵커 위치가 아닌 곳에 삽입"):
            editor._replace_anchor_with_image(self._anchor())


class TestCheckPublishPlan:
    """발행 전 DOM 검증 규칙 — 브라우저 없이 DOM 요약 dict로 직접 검사한다.

    검증이 실패하면 예외가 나고, publisher는 저장·발행 버튼을 누르지 않는다.
    """

    def _plan(self):
        # 이미지(맨 위) → '첫 문단.' → 이미지(맨 끝) 구조의 계획.
        return build_naver_publish_plan(build_post(), "post_1")

    def _summary(self, plan, **overrides) -> dict:
        summary = {
            "title": plan.title,
            "tokenCount": 0,
            "imageCount": 2,
            "inlineImageCount": 0,
            "oversizedImageCount": 0,
            "brokenImageCount": 0,
            "items": [
                {"type": "image"},
                {"type": "text", "text": "첫 문단.", "allBold": False},
                {"type": "image"},
            ],
        }
        summary.update(overrides)
        return summary

    def test_a_matching_editor_passes(self):
        plan = self._plan()

        _check_publish_plan(plan, self._summary(plan))  # 예외 없음 == 통과

    def test_a_leftover_anchor_token_blocks_publishing(self):
        plan = self._plan()

        with pytest.raises(RuntimeError, match="토큰"):
            _check_publish_plan(plan, self._summary(plan, tokenCount=1))

    def test_a_missing_image_blocks_publishing(self):
        plan = self._plan()
        summary = self._summary(
            plan,
            imageCount=1,
            items=[{"type": "image"}, {"type": "text", "text": "첫 문단.", "allBold": False}],
        )

        with pytest.raises(RuntimeError, match="이미지 수"):
            _check_publish_plan(plan, summary)

    def test_an_image_out_of_position_blocks_publishing(self):
        """이미지 앞뒤에 있어야 할 텍스트가 제자리에 없으면 순서가 꼬인 것이다."""
        plan = self._plan()
        summary = self._summary(
            plan,
            items=[
                {"type": "image"},
                {"type": "image"},
                {"type": "text", "text": "첫 문단.", "allBold": False},
            ],
        )

        with pytest.raises(RuntimeError, match="이미지 앞"):
            _check_publish_plan(plan, summary)

    def test_a_duplicated_title_in_the_body_blocks_publishing(self):
        plan = self._plan()
        summary = self._summary(
            plan,
            items=[
                {"type": "text", "text": "제목", "allBold": True},
                {"type": "image"},
                {"type": "text", "text": "첫 문단.", "allBold": False},
                {"type": "image"},
            ],
        )

        with pytest.raises(RuntimeError, match="중복"):
            _check_publish_plan(plan, summary)

    def test_a_fully_bold_plain_paragraph_blocks_publishing(self):
        """소제목 굵기가 본문으로 번진 상태 — 부분 강조(allBold=False)는 걸리지 않는다."""
        plan = self._plan()
        summary = self._summary(
            plan,
            items=[
                {"type": "image"},
                {"type": "text", "text": "첫 문단.", "allBold": True},
                {"type": "image"},
            ],
        )

        with pytest.raises(RuntimeError, match="굵게"):
            _check_publish_plan(plan, summary)

    def test_missing_text_blocks_publishing(self):
        plan = self._plan()
        summary = self._summary(plan, items=[{"type": "image"}, {"type": "image"}])

        with pytest.raises(RuntimeError, match="찾지 못한"):
            _check_publish_plan(plan, summary)

    def test_an_image_inside_a_text_component_blocks_publishing(self):
        plan = self._plan()

        with pytest.raises(RuntimeError, match="끼어든"):
            _check_publish_plan(plan, self._summary(plan, inlineImageCount=1))

    def test_a_wrong_title_blocks_publishing(self):
        plan = self._plan()

        with pytest.raises(RuntimeError, match="제목"):
            _check_publish_plan(plan, self._summary(plan, title="엉뚱한 제목"))

    def test_the_failure_carries_reason_kinds_without_any_body_text(self):
        """실패는 **본문이 섞이지 않는 사유 분류**를 함께 들고 온다.

        예외 문구에는 어디가 어긋났는지 알아보라고 본문 조각이 들어간다 — 그것은 운영
        로그에 실을 수 없다. 로그에 예외 타입만 남기던 예전에는 "이미지도 토큰도 블록도
        맞는데 RuntimeError"만 보여 무엇이 걸렸는지 알 수 없었다(2026-08-10 실발행).
        """
        plan = self._plan()
        summary = self._summary(
            plan,
            items=[
                {"type": "image"},
                {"type": "text", "text": "첫 문단.", "allBold": True},
                {"type": "image"},
            ],
        )

        with pytest.raises(RuntimeError) as caught:
            _check_publish_plan(plan, summary)

        assert caught.value.kinds == ("굵기 번짐",)
        # 분류에는 본문이 들어가지 않는다 — 로그로 그대로 나가는 값이다.
        assert all("첫 문단" not in kind for kind in caught.value.kinds)

    def test_the_same_reason_twice_is_counted_not_repeated(self):
        assert _summarize_kinds(("텍스트 블록 누락",) * 2) == "텍스트 블록 누락 2건"
        assert _summarize_kinds(("굵기 번짐", "제목 불일치")) == "굵기 번짐, 제목 불일치"
        # 분류를 들고 오지 않는 예외(예전 코드·다른 RuntimeError)도 로그가 깨지지 않는다.
        assert _summarize_kinds(()) == "알 수 없음"


class TestBoldCheckIgnoresTables:
    """네이버는 표 머리글을 항상 굵게 그린다. 그것이 본문 굵기 번짐으로 오인되면 안 된다.

    실발행이 여기서 멈췄다: "일반 본문 문단이 통째로 굵게 표시됩니다: '구분'".
    표 머리글 '구분'이 본문 문단 안에 그 두 글자가 들어 있다는 이유만으로 걸렸다.
    """

    def _plan(self):
        image = build_image(0)
        return build_naver_publish_plan(
            build_post(
                images=[image],
                featured_image=image,
                html_content=(
                    f'<article><h1>제목</h1>'
                    f'<figure><img src="{image.data_url}" alt="a" /></figure>'
                    f"<p>구분에 따라 달라지는 내용을 설명하는 본문 문단입니다.</p>"
                    f"<table><thead><tr><th>구분</th><th>값</th></tr></thead>"
                    f"<tbody><tr><td>키</td><td>163cm</td></tr></tbody></table></article>"
                ),
            ),
            "post_1",
        )

    def _summary(self, plan, header_item: dict) -> dict:
        return {
            "title": plan.title,
            "tokenCount": 0,
            "imageCount": 1,
            "inlineImageCount": 0,
            "oversizedImageCount": 0,
            "brokenImageCount": 0,
            "items": [
                {"type": "image"},
                {
                    "type": "text",
                    "text": "구분에 따라 달라지는 내용을 설명하는 본문 문단입니다.",
                    "allBold": False,
                },
                header_item,
                # 표의 나머지 칸. 네이버는 머리글만 굵게 그린다.
                {"type": "text", "text": "값", "allBold": True, "inTable": True},
                {"type": "text", "text": "키", "allBold": False, "inTable": True},
                {"type": "text", "text": "163cm", "allBold": False, "inTable": True},
            ],
        }

    def test_a_bold_table_header_no_longer_blocks_publishing(self):
        plan = self._plan()
        header = {"type": "text", "text": "구분", "allBold": True, "inTable": True}

        _check_publish_plan(plan, self._summary(plan, header))  # 예외 없음 == 통과

    def test_a_body_paragraph_rendered_bold_is_still_caught(self):
        """표만 예외다 — 본문 문단이 통째로 굵어지는 진짜 증상은 그대로 잡는다."""
        plan = self._plan()
        bold_body = {
            "type": "text",
            "text": "구분에 따라 달라지는 내용을 설명하는 본문 문단입니다.",
            "allBold": True,
            "inTable": False,
        }
        summary = self._summary(plan, bold_body)

        with pytest.raises(RuntimeError, match="통째로 굵게"):
            _check_publish_plan(plan, summary)

    def test_an_old_summary_without_the_field_keeps_the_previous_behaviour(self):
        """inTable은 나중에 생긴 필드다. 없으면 예전처럼 표가 아닌 것으로 본다."""
        plan = self._plan()
        header = {"type": "text", "text": "구분", "allBold": True}

        with pytest.raises(RuntimeError, match="통째로 굵게"):
            _check_publish_plan(plan, self._summary(plan, header))


class TestSaveDraft:
    """임시저장이 실제로 어느 버튼을 누르는지.

    임시저장과 발행은 하는 일이 같고 누르는 버튼만 다르다. 예전 선택자는
    'tempsave'·'draft'를 찾았는데 실제 에디터의 버튼은 발행 옆의 '저장'이라,
    아무것도 누르지 못한 채 조용히 실패했다. 여기서 대상 버튼을 못박는다.
    """

    class FakeButton:
        def __init__(self, text, class_name=""):
            self.text = text
            self.class_name = class_name
            self.clicked = False

        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

        def get_attribute(self, name):
            return self.class_name if name == "class" else None

    def _editor(self, monkeypatch, buttons, frame="editor"):
        """발행이 도는 프레임(editor iframe)을 기본으로 둔다.

        버튼은 그 프레임 안에서만 보인다 — 실제 상단 바가 #mainFrame 안에 있기 때문이다.
        """

        class FakeSwitchTo:
            def __init__(self, driver):
                self.driver = driver

            def default_content(self):
                self.driver.frame = "default"

        class FakeDriver:
            def __init__(self):
                self.frame = frame
                self.switch_to = FakeSwitchTo(self)

            def find_elements(self, _by, query):
                if self.frame != "editor":
                    return []
                return [b for b in buttons if b.matches(query)]

        driver = FakeDriver()
        editor = SmartEditorOne(driver)
        monkeypatch.setattr("app.posting.naver.editor.time.sleep", lambda _s: None)
        monkeypatch.setattr(
            editor, "_click_button", lambda element, _what: setattr(element, "clicked", True)
        )
        return editor

    def test_selectors_target_the_save_button_not_publish(self):
        """선택자 자체를 못박는다.

        아래 save_draft 테스트들은 글자 폴백('저장')이 있어서 선택자가 틀려도 통과한다 —
        그래서 선택자는 여기서 따로 확인한다. 실제 상단 바의 저장 버튼은
        class에 save_btn, data-click-area에 tpb.save를 갖고, 발행은 publish_btn/
        tpb.publish다. 예전 선택자(tempsave·temp_save·draft)는 저장 버튼에 하나도
        걸리지 않아 임시저장이 조용히 실패했다.
        """
        import re

        from app.posting.naver.constants import PUBLISH_OPEN_SELECTORS, TEMP_SAVE_SELECTORS

        save = {"class": "save_btn__abc", "data-click-area": "tpb.save"}
        publish = {"class": "publish_btn__xyz", "data-click-area": "tpb.publish"}

        def matches(element, selector):
            m = re.match(r"button\[class\*='([^']+)'\]$", selector)
            if m:
                return m.group(1) in element.get("class", "")
            m = re.match(r"\[data-click-area='([^']+)'\]$", selector)
            if m:
                return element.get("data-click-area") == m.group(1)
            return False

        assert any(matches(save, s) for s in TEMP_SAVE_SELECTORS), (
            "임시저장 선택자가 실제 '저장' 버튼에 하나도 걸리지 않는다"
        )
        assert not any(matches(publish, s) for s in TEMP_SAVE_SELECTORS), (
            "임시저장 선택자가 발행 버튼을 물면 임시저장이 글을 발행해 버린다"
        )
        # 반대 방향도 확인 — 발행 선택자가 저장 버튼을 물면 발행이 저장만 하고 끝난다.
        assert not any(matches(save, s) for s in PUBLISH_OPEN_SELECTORS)

    def test_clicks_the_save_button_and_never_publish(self, monkeypatch):
        save = self.FakeButton("저장 1", "save_btn__abc")
        publish = self.FakeButton("발행", "publish_btn__xyz")
        save.matches = lambda q: "save_btn" in q or "tpb.save" in q or "저장" in q
        publish.matches = lambda q: "publish_btn" in q or "tpb.publish" in q

        editor = self._editor(monkeypatch, [save, publish])

        assert editor.save_draft() is True
        assert save.clicked is True
        assert publish.clicked is False

    def test_enters_tags_through_the_publish_panel_then_clicks_save(self, monkeypatch):
        """발행과 같은 순서로 가되 마지막 버튼만 다르다.

        발행 패널을 열어야 '태그 편집' 칸이 나오고, 패널이 열려도 상단 바의 저장은
        그대로 눌린다. 그래서 태그까지 담은 채 임시저장이 된다.
        """
        save = self.FakeButton("저장 2", "save_btn__abc")
        save.matches = lambda q: "save_btn" in q or "tpb.save" in q or "저장" in q
        opener = self.FakeButton("발행", "publish_btn__xyz")
        opener.matches = lambda q: "publish_btn" in q or "tpb.publish" in q

        editor = self._editor(monkeypatch, [save, opener])
        order: list[str] = []
        monkeypatch.setattr(
            editor, "_click_button", lambda el, what: (order.append(what), setattr(el, "clicked", True))
        )
        monkeypatch.setattr(editor, "_enter_tags", lambda tags: order.append(f"tags:{','.join(tags)}"))

        assert editor.save_draft(["AI", "블로그"]) is True
        # 패널 열기 → 태그 → 저장. 순서가 뒤집히면 태그 칸이 없는 채로 저장된다.
        assert order == [
            "발행 버튼(태그 입력용 패널 열기)",
            "tags:AI,블로그",
            "저장(임시저장) 버튼",
        ]
        assert save.clicked is True

    def test_saves_without_tags_when_the_panel_will_not_open(self, monkeypatch):
        """태그는 비치명적이다 — 패널을 못 열어도 본문은 저장한다."""
        save = self.FakeButton("저장 2", "save_btn__abc")
        save.matches = lambda q: "save_btn" in q or "tpb.save" in q or "저장" in q

        editor = self._editor(monkeypatch, [save])

        def no_panel(selectors, what):
            if "발행" in what:
                raise RuntimeError("발행 패널을 열지 못함")
            return save

        monkeypatch.setattr(editor, "_wait_for_any", no_panel)

        assert editor.save_draft(["AI"]) is True
        assert save.clicked is True

    def test_looks_for_the_button_in_the_same_frame_as_publish(self, monkeypatch):
        """프레임을 옮기지 않는다.

        저장과 발행은 같은 상단 바에 나란히 있고, 그 바는 에디터 iframe(#mainFrame)
        안에 있다. 예전에는 save_draft만 default_content()로 프레임 밖에 나가서
        버튼을 영영 찾지 못했다 — 이 테스트는 그 순간을 잡는다.
        """
        save = self.FakeButton("저장 1", "save_btn__abc")
        save.matches = lambda q: "save_btn" in q or "tpb.save" in q or "저장" in q

        editor = self._editor(monkeypatch, [save])
        # _wait_for_any가 15초를 기다리지 않도록, 프레임이 어긋나면 바로 실패시킨다.
        original = editor._wait_for_any

        def quick(selectors, what):
            if editor.driver.frame != "editor":
                raise RuntimeError("프레임 밖")
            return original(selectors, what)

        monkeypatch.setattr(editor, "_wait_for_any", quick)
        monkeypatch.setattr(editor, "_log_failure", lambda _what: None)

        assert editor.save_draft() is True
        assert editor.driver.frame == "editor", "save_draft가 에디터 프레임을 벗어났다"
        assert save.clicked is True

    def test_refuses_to_treat_the_publish_button_as_save(self, monkeypatch):
        """선택자가 발행 버튼을 물어 오면 누르지 않고 실패로 돌린다 — 임시저장을 부른
        사람이 글이 발행되기를 기대하지는 않는다."""
        publish = self.FakeButton("발행", "publish_btn__xyz")
        publish.matches = lambda _q: False

        editor = self._editor(monkeypatch, [publish])
        monkeypatch.setattr(editor, "_wait_for_any", lambda selectors, what: publish)
        monkeypatch.setattr(editor, "_log_failure", lambda _what: None)

        assert editor.save_draft() is False
        assert publish.clicked is False

    def test_falls_back_to_finding_the_save_button_by_its_text(self, monkeypatch):
        """선택자가 안 맞아도(에디터 개편) '저장'이라고 적힌 버튼으로 되찾는다."""
        save = self.FakeButton("저장 3", "")
        save.matches = lambda q: "저장" in q
        publish = self.FakeButton("발행", "")
        publish.matches = lambda q: "저장" in q  # XPath가 둘 다 물어 오는 최악의 경우

        editor = self._editor(monkeypatch, [save, publish])

        def boom(_selectors, _what):
            raise RuntimeError("선택자 불일치")

        monkeypatch.setattr(editor, "_wait_for_any", boom)

        assert editor.save_draft() is True
        assert save.clicked is True
        assert publish.clicked is False


class TestNaverBrowserPublisher:
    def test_draft_restore_popup_is_cancelled_inside_editor_frame(self, monkeypatch):
        class FakeButton:
            clicked = False

            def is_displayed(self):
                return not self.clicked

            def is_enabled(self):
                return True

            def click(self):
                self.clicked = True

        class FakeSwitchTo:
            def __init__(self, driver):
                self.driver = driver

            def default_content(self):
                self.driver.context = "default"

        class FakeDriver:
            def __init__(self, button):
                self.button = button
                self.context = "frame"
                self.switch_to = FakeSwitchTo(self)

            def find_elements(self, _by, xpath):
                if self.context == "frame" and "작성 중인 글이 있습니다" in xpath:
                    return [self.button]
                return []

        button = FakeButton()
        driver = FakeDriver(button)
        editor = SmartEditorOne(driver)
        monkeypatch.setattr(editor, "_switch_to_editor_frame", lambda: setattr(driver, "context", "frame"))
        monkeypatch.setattr("app.posting.naver.time.sleep", lambda _seconds: None)

        dismissed = editor._dismiss_draft_popup()

        assert dismissed is True
        assert button.clicked is True
        assert driver.context == "frame"

    def test_login_value_is_typed_one_character_at_a_time(self, monkeypatch):
        class FakeDriver:
            def __init__(self):
                self.input_characters: list[str] = []
                self.delay = None

            def set_script_timeout(self, _seconds):
                return None

            def execute_async_script(self, _script, element, value, delay):
                self.input_characters = list(value)
                self.delay = delay
                element.value = value
                return {"ok": True, "length": len(value)}

        class FakeElement:
            def __init__(self):
                self.value = ""

            def get_attribute(self, name: str):
                return "id" if name == "id" else self.value

            def click(self):
                return None

            def clear(self):
                self.value = ""

            def send_keys(self, value: str):
                self.value += value

        sleeps: list[float] = []
        driver = FakeDriver()
        element = FakeElement()
        monkeypatch.setattr("app.posting.naver.time.sleep", sleeps.append)

        _type_input_value(driver, element, "abc")

        from app.posting.naver.constants import (
            LOGIN_EVENT_DELAY_MILLISECONDS,
            LOGIN_FIELD_PAUSE_SECONDS,
        )

        assert driver.input_characters == ["a", "b", "c"]
        # 값은 상수를 따라간다 — 사람 속도로 바꾼 2026-08-18 이후 90ms 기준이고,
        # 실제 간격은 JS 안에서 글자마다 무작위 흔들림이 더해진다(봇 판정 회피).
        assert driver.delay == LOGIN_EVENT_DELAY_MILLISECONDS
        assert sleeps == [LOGIN_FIELD_PAUSE_SECONDS]

    async def test_it_says_so_when_there_is_no_way_in(self, tmp_path: Path):
        """No session and no credentials means no publishing. Saying SUCCESS here is
        the fake publisher all over again — it answered with a blog.example.com URL
        and the post had gone nowhere."""
        config = NaverConfig(
            blog_id="myblog",
            profile_dir=tmp_path / "never-logged-in",
            api_origin="http://localhost:3000",
        )

        result = await NaverBrowserPublisher(config).publish(build_job())

        assert result.result == PostingResultStatus.NEEDS_HUMAN
        assert result.post_url is None
        assert "설정" in result.error_message

    async def test_an_empty_profile_directory_is_not_a_session(self, tmp_path: Path):
        empty = tmp_path / "profile"
        empty.mkdir()
        config = NaverConfig(
            blog_id="myblog", profile_dir=empty, api_origin="http://localhost:3000"
        )

        assert config.has_session is False

    async def test_credentials_marker_alone_is_not_a_browser_session(self, tmp_path: Path):
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "credentials.json").write_text("{}", encoding="utf-8")
        (profile / "blog_id").write_text("myblog", encoding="utf-8")

        config = NaverConfig(
            blog_id="myblog", profile_dir=profile, api_origin="http://localhost:3000"
        )

        assert config.has_session is False

    async def test_selenium_chrome_cookie_database_counts_as_a_session(self, tmp_path: Path):
        cookies = tmp_path / "profile" / "Default" / "Network" / "Cookies"
        cookies.parent.mkdir(parents=True)
        cookies.write_bytes(b"sqlite")

        config = NaverConfig(
            blog_id="myblog", profile_dir=tmp_path / "profile", api_origin="http://localhost:3000"
        )

        assert config.has_session is True

    async def test_credentials_are_enough_to_start_without_a_session(self, tmp_path: Path):
        """First run: no profile yet, but it can log in and make one."""
        config = NaverConfig(
            blog_id="myblog",
            profile_dir=tmp_path / "fresh",
            api_origin="http://localhost:3000",
            username="someone",
            password="secret",
        )

        assert config.has_session is False
        assert config.can_log_in is True

    def test_local_browser_profiles_are_scoped_to_the_blogit_user(self):
        first = naver_profile_dir("user_1")
        second = naver_profile_dir("user_2")

        assert first != second
        assert first.parent.name == ".naver-profile-users"
        assert "user_1" not in str(first)

    def test_user_scoped_config_does_not_use_global_naver_account(self, monkeypatch):
        monkeypatch.setenv("NAVER_BLOG_ID", "global-account")
        monkeypatch.setenv("NAVER_ID", "global-account")
        monkeypatch.setenv("NAVER_PASSWORD", "global-password")

        config = naver_config_from_env(
            username="saved-account",
            password="saved-password",
            user_id="user_1",
        )

        assert config is not None
        assert config.blog_id == "saved-account"
        assert config.username == "saved-account"
        assert config.password == "saved-password"

    def test_management_config_ignores_plaintext_env_credentials(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("NAVER_BROWSER_PROFILE_DIR", str(tmp_path))
        monkeypatch.setenv("NAVER_BLOG_ID", "public-blog-address")
        monkeypatch.setenv("NAVER_ID", "must-not-be-read")
        monkeypatch.setenv("NAVER_PASSWORD", "must-not-be-read")

        config = naver_config_from_env()

        assert config is not None
        assert config.blog_id == "public-blog-address"
        assert config.username is None
        assert config.password is None

    def test_management_config_reads_only_the_v2_credential_file(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("NAVER_BROWSER_PROFILE_DIR", str(tmp_path))
        monkeypatch.setenv("NAVER_BLOG_ID", "public-blog-address")
        save_credentials(
            tmp_path,
            NaverCredentials(username="encrypted-account", password="encrypted-password"),
        )

        config = naver_config_from_env()

        assert config is not None
        assert config.username == "encrypted-account"
        assert config.password == "encrypted-password"

    async def test_it_only_handles_auto(self, tmp_path: Path):
        config = NaverConfig(
            blog_id="myblog",
            profile_dir=tmp_path,
            api_origin="http://localhost:3000",
            username="someone",
            password="secret",
        )

        result = await NaverBrowserPublisher(config).publish(build_job(PostingMethod.COPY))

        assert result.result == PostingResultStatus.FAIL

    async def test_a_broken_manuscript_fails_without_leaking_source_url(
        self, tmp_path: Path, caplog
    ):
        """계획을 만들 수 없는 원고(디코드 불가 이미지)는 브라우저를 열기도 전에 실패한다."""
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "Cookies").write_text("session", encoding="utf-8")
        config = NaverConfig(
            blog_id="myblog", profile_dir=profile, api_origin="http://localhost:3000"
        )
        job = PublishJob(
            post_id="post_1",
            user_id="user_1",
            method=PostingMethod.AUTO,
            final_post=build_post(
                html_content=(
                    '<article><h1>제목</h1><figure><img '
                    'src="https://example.invalid/a.png?sig=LEAKED_TOKEN" />'
                    "</figure></article>"
                )
            ),
        )

        result = await NaverBrowserPublisher(config).publish(job)

        assert result.result == PostingResultStatus.FAIL
        assert "원고" in result.error_message
        combined = result.error_message + caplog.text
        assert "LEAKED_TOKEN" not in combined
        assert "example.invalid" not in combined

    async def test_a_published_post_reports_its_url(self, tmp_path: Path, monkeypatch):
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "Cookies").write_text("session", encoding="utf-8")

        config = NaverConfig(
            blog_id="myblog", profile_dir=profile, api_origin="http://localhost:3000"
        )
        publisher = NaverBrowserPublisher(config)

        pasted: dict = {}

        async def fake_run(plan, tags=None, method=PostingMethod.AUTO) -> str:
            pasted["plan"] = plan
            pasted["tags"] = tags
            pasted["method"] = method
            return "https://blog.naver.com/myblog/223"

        monkeypatch.setattr(publisher, "_run", fake_run)

        result = await publisher.publish(build_job())

        assert result.result == PostingResultStatus.SUCCESS
        assert result.post_url == "https://blog.naver.com/myblog/223"
        plan = pasted["plan"]
        assert plan.title == "제목"
        # 이미지는 앵커의 실제 바이트로 넘어간다(localhost URL·data URL 아님).
        assert len(plan.image_anchors) == 2
        assert all(isinstance(a.image_bytes, bytes) and a.image_bytes for a in plan.image_anchors)
        assert "data:image" not in plan.scaffold_html
        assert "localhost:3000" not in plan.scaffold_html
        # 해시태그는 본문에 파란 글씨로 들어가지 않고 네이버 태그로 넘어간다.
        assert "#AI #블로그" not in plan.scaffold_html
        assert pasted["tags"] == ["AI", "블로그"]
        assert pasted["method"] == PostingMethod.AUTO

    async def test_a_draft_uses_the_same_content_but_stops_at_temporary_save(
        self, tmp_path: Path, monkeypatch
    ):
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "Cookies").write_text("session", encoding="utf-8")

        config = NaverConfig(
            blog_id="myblog", profile_dir=profile, api_origin="http://localhost:3000"
        )
        publisher = NaverBrowserPublisher(config)
        observed: dict = {}

        async def fake_run(plan, tags=None, method=PostingMethod.AUTO):
            observed["plan"] = plan
            observed["method"] = method
            return None

        monkeypatch.setattr(publisher, "_run", fake_run)

        result = await publisher.publish(build_job(PostingMethod.DRAFT))

        assert result.result == PostingResultStatus.SUCCESS
        assert result.post_url is None
        assert observed["plan"].title == "제목"
        assert observed["method"] == PostingMethod.DRAFT
        assert len(observed["plan"].image_anchors) == 2

    def test_draft_mode_validates_then_clicks_temporary_save_instead_of_publish(
        self, tmp_path: Path, monkeypatch
    ):
        import app.posting.naver.publisher as publisher_module

        events: list[str] = []

        class FakeDriver:
            def quit(self):
                events.append("quit")

        class FakeLogin:
            def __init__(self, _driver, _config):
                pass

            def ensure_logged_in(self):
                events.append("login")

        class FakeEditor:
            def __init__(self, _driver):
                pass

            def navigate(self, blog_id):
                events.append(f"navigate:{blog_id}")

            def fill_publish_plan(self, plan):
                events.append(f"fill:{plan.title}")

            def validate_publish_plan(self, plan):
                events.append("validate")

            def save_draft(self, tags=None):
                # 임시저장도 발행과 같은 태그를 받아야 한다 — 마지막에 누르는 버튼만 다르다.
                events.append(f"save_draft:{','.join(tags or [])}")
                return True

            def publish(self, _tags):
                raise AssertionError("임시저장 모드에서 최종 발행을 누르면 안 됩니다.")

        profile = tmp_path / "profile"
        config = NaverConfig(
            blog_id="myblog", profile_dir=profile, api_origin="http://localhost:3000"
        )
        publisher = NaverBrowserPublisher(config)
        kept_open: dict = {}

        monkeypatch.setattr(publisher_module, "_create_driver", lambda *_args: FakeDriver())
        monkeypatch.setattr(publisher_module, "NaverLogin", FakeLogin)
        monkeypatch.setattr(publisher_module, "SmartEditorOne", FakeEditor)
        monkeypatch.setattr(publisher_module, "_release_kept_open_browser", lambda _path: None)
        monkeypatch.setattr(publisher_module, "_KEPT_OPEN_BROWSERS", kept_open)

        plan = build_naver_publish_plan(build_post(), "post_1")
        post_url = publisher._run_sync(plan, ["태그"], PostingMethod.DRAFT)

        assert post_url is None
        # 검증이 저장보다 먼저다 — 순서가 뒤집히면 잘못된 글이 이미 저장된 뒤 검증하게 된다.
        assert events == ["login", "navigate:myblog", "fill:제목", "validate", "save_draft:태그"]
        assert str(profile) in kept_open

    def test_a_failed_validation_blocks_saving_and_publishing(
        self, tmp_path: Path, monkeypatch
    ):
        """검증 실패는 발행 중단이다 — 저장·발행 어느 버튼도 눌리지 않고 창은 남는다."""
        import app.posting.naver.publisher as publisher_module

        class FakeDriver:
            def quit(self):
                raise AssertionError("실패한 화면은 닫지 않고 남겨야 한다.")

        class FakeLogin:
            def __init__(self, _driver, _config):
                pass

            def ensure_logged_in(self):
                pass

        class FakeEditor:
            def __init__(self, _driver):
                pass

            def navigate(self, blog_id):
                pass

            def fill_publish_plan(self, plan):
                pass

            def validate_publish_plan(self, plan):
                raise RuntimeError("발행 전 검증 실패 — 이미지 수가 다릅니다")

            def save_draft(self, tags=None):
                raise AssertionError("검증이 실패한 글을 저장하면 안 됩니다.")

            def publish(self, _tags):
                raise AssertionError("검증이 실패한 글을 발행하면 안 됩니다.")

        profile = tmp_path / "profile"
        config = NaverConfig(
            blog_id="myblog", profile_dir=profile, api_origin="http://localhost:3000"
        )
        publisher = NaverBrowserPublisher(config)
        kept_open: dict = {}

        monkeypatch.setattr(publisher_module, "_create_driver", lambda *_args: FakeDriver())
        monkeypatch.setattr(publisher_module, "NaverLogin", FakeLogin)
        monkeypatch.setattr(publisher_module, "SmartEditorOne", FakeEditor)
        monkeypatch.setattr(publisher_module, "_release_kept_open_browser", lambda _path: None)
        monkeypatch.setattr(publisher_module, "_KEPT_OPEN_BROWSERS", kept_open)

        plan = build_naver_publish_plan(build_post(), "post_1")
        with pytest.raises(RuntimeError, match="검증 실패"):
            publisher._run_sync(plan, [], PostingMethod.AUTO)

        assert str(profile) in kept_open  # 사용자가 결과를 볼 수 있게 창을 붙잡아 둔다

    async def test_a_captcha_is_handed_to_the_person(self, tmp_path: Path, monkeypatch):
        """네이버 asking for a human is the check working. Automating past it is not
        something this should try to do — the window is left open for them."""
        from app.posting.naver import _NeedsHuman

        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "Cookies").write_text("session", encoding="utf-8")

        config = NaverConfig(
            blog_id="myblog", profile_dir=profile, api_origin="http://localhost:3000"
        )
        publisher = NaverBrowserPublisher(config)

        async def needs_human(plan, tags=None, method=PostingMethod.AUTO) -> str:
            raise _NeedsHuman("네이버가 추가 인증(캡차·2단계 인증)을 요구했습니다.")

        monkeypatch.setattr(publisher, "_run", needs_human)

        result = await publisher.publish(build_job())

        assert result.result == PostingResultStatus.NEEDS_HUMAN
        assert result.post_url is None
        assert "추가 인증" in result.error_message

    async def test_a_broken_editor_does_not_leak_selenium_details(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "Cookies").write_text("session", encoding="utf-8")

        config = NaverConfig(
            blog_id="myblog", profile_dir=profile, api_origin="http://localhost:3000"
        )
        publisher = NaverBrowserPublisher(config)

        async def broken(plan, tags=None, method=PostingMethod.AUTO) -> str:
            raise RuntimeError(
                "https://example.invalid/callback?access_token=LEAKED_TOKEN "
                r"C:\private\chrome-profile"
            )

        monkeypatch.setattr(publisher, "_run", broken)

        result = await publisher.publish(build_job())

        assert result.result == PostingResultStatus.FAIL
        assert "관리자에게 문의" in result.error_message
        combined = result.error_message + caplog.text
        assert "LEAKED_TOKEN" not in combined
        assert "example.invalid" not in combined
        assert "chrome-profile" not in combined
        assert "RuntimeError" in caplog.text


class TestBrowserThread:
    async def test_selenium_work_leaves_the_api_event_loop(self):
        api_thread = threading.get_ident()
        browser_thread = await _in_browser_thread(threading.get_ident)

        assert browser_thread != api_thread

    async def test_the_browser_thread_does_not_hold_the_process_open(self):
        """브라우저 스레드는 **데몬**이라 종료를 붙잡지 않는다.

        기본 실행기(``asyncio.to_thread``)의 워커는 논-데몬이라 인터프리터가 끝날 때
        join된다. 설정 화면의 네이버 로그인은 사람이 2단계 인증을 마칠 때까지 최대 7분을
        기다리므로, 그 사이 Ctrl+C로도 서버가 죽지 않았다(2026-08-06 사용자 신고).
        """
        assert await _in_browser_thread(lambda: threading.current_thread().daemon) is True

    async def test_a_failure_comes_back_to_the_caller(self):
        def broken() -> None:
            raise RuntimeError("네이버 창을 열지 못했습니다")

        with pytest.raises(RuntimeError, match="네이버 창을 열지 못했습니다"):
            await _in_browser_thread(broken)

    async def test_a_canceled_request_does_not_break_the_loop(self):
        """요청이 끊겨도 브라우저 스레드의 결과가 이벤트 루프를 깨지 않는다.

        끝난 Future에 값을 넣으면 InvalidStateError가 루프에 쌓인다. 사용자가 화면을
        떠나거나 서버가 내려가는 순간이 바로 그 상황이다.
        """
        started = threading.Event()
        release = threading.Event()

        def slow() -> str:
            started.set()
            release.wait(5)
            return "늦게 끝난 작업"

        task = asyncio.create_task(_in_browser_thread(slow))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 스레드가 뒤늦게 끝나도 조용해야 한다.
        release.set()
        await asyncio.sleep(0.1)


class TestBloatedPreferences:
    r"""undetected-chromedriver가 부풀린 Chrome 설정 파일을 실행 전에 되돌리는지.

    실제로 겪은 장애다. uc는 실행할 때마다 ``Default/Preferences``를 latin1로 읽어
    ``json.dump``(기본 ``ensure_ascii=True``)로 다시 쓴다. 한글이 매번 ``\uXXXX``로
    부풀고 역슬래시가 다시 이스케이프되면서, 한 프로필이 14KB에서 2,416MB까지 자랐다.
    그 상태에서는 크롬이 창도 못 띄우고 81초 뒤 연결 실패로 끝났다.
    """

    def test_a_bloated_preferences_file_is_reset(self, tmp_path: Path):
        profile = tmp_path / "profile"
        (profile / "Default").mkdir(parents=True)
        prefs = profile / "Default" / "Preferences"
        prefs.write_bytes(b"x" * (6 * 1024 * 1024))

        _reset_bloated_preferences(profile)

        assert not prefs.exists()

    def test_a_normal_preferences_file_is_left_alone(self, tmp_path: Path):
        """정상 파일은 14~20KB다. 멀쩡한 설정을 날리면 사용자 설정이 매번 초기화된다."""
        profile = tmp_path / "profile"
        (profile / "Default").mkdir(parents=True)
        prefs = profile / "Default" / "Preferences"
        prefs.write_text('{"profile": {"exit_type": null}}', encoding="utf-8")

        _reset_bloated_preferences(profile)

        assert prefs.read_text(encoding="utf-8") == '{"profile": {"exit_type": null}}'

    def test_the_login_session_survives_the_reset(self, tmp_path: Path):
        """세션은 Cookies·Login Data에 있다. 설정 파일을 지워도 로그인이 풀리면 안 된다."""
        profile = tmp_path / "profile"
        (profile / "Default" / "Network").mkdir(parents=True)
        (profile / "Default" / "Preferences").write_bytes(b"x" * (6 * 1024 * 1024))
        cookies = profile / "Default" / "Network" / "Cookies"
        cookies.write_bytes(b"sqlite-cookie-db")
        login = profile / "Default" / "Login Data"
        login.write_bytes(b"sqlite-login-db")

        _reset_bloated_preferences(profile)

        assert cookies.read_bytes() == b"sqlite-cookie-db"
        assert login.read_bytes() == b"sqlite-login-db"
        assert NaverConfig(
            blog_id="myblog", profile_dir=profile, api_origin="http://localhost:3000"
        ).has_session

    def test_a_profile_without_preferences_is_not_an_error(self, tmp_path: Path):
        """처음 만든 프로필에는 아직 파일이 없다. 그때 예외가 나면 첫 발행이 막힌다."""
        profile = tmp_path / "profile"
        profile.mkdir()

        _reset_bloated_preferences(profile)  # 예외가 나지 않으면 통과다


class TestTrustThisBrowser:
    """2단계 인증 화면에서 '이 브라우저는 2단계 인증 없이 로그인 합니다'를 켜는지.

    켜 두지 않으면 세션이 만료될 때마다 사람이 휴대폰 알림을 승인해야 하고, 그동안
    발행은 멈춘다. 새 기기 등록을 일부러 거절하는 것과는 방향이 반대인 동작이라
    (계정 보안 설정을 바꾼다) 사용자 요청으로 넣었고, 대상은 자동화 프로필뿐이다.
    """

    class FakeDriver:
        def __init__(self, found):
            self._found = found
            self.clicked = []

        def execute_script(self, script, *args):
            if args:                      # arguments[0].click() 폴백
                self.clicked.append(args[0])
                return None
            return self._found

    def _login(self, driver):
        config = NaverConfig(
            blog_id="myblog", profile_dir=Path("."), api_origin="http://localhost:3000"
        )
        return NaverLogin(driver, config)

    def test_an_unchecked_box_gets_clicked(self, monkeypatch):
        """마우스 클릭이 안 되는 마크업이면 JS 클릭으로 넘어가야 한다."""
        import selenium.webdriver.common.action_chains as action_chains

        class Unusable:
            def __init__(self, driver):
                raise RuntimeError("이 요소는 마우스로 누를 수 없습니다")

        monkeypatch.setattr(action_chains, "ActionChains", Unusable)
        driver = self.FakeDriver({"state": "off", "element": "checkbox-label"})

        assert self._login(driver)._trust_this_browser() is True
        assert driver.clicked == ["checkbox-label"]

    def test_an_already_checked_box_is_left_alone(self):
        """다시 누르면 켜져 있던 체크가 도로 꺼진다."""
        driver = self.FakeDriver({"state": "on"})

        assert self._login(driver)._trust_this_browser() is True
        assert driver.clicked == []

    def test_a_missing_box_is_not_an_error(self):
        """화면이 아직 안 그려졌을 수 있다. 여기서 예외가 나면 로그인 자체가 끊긴다."""
        driver = self.FakeDriver({"state": "missing"})

        assert self._login(driver)._trust_this_browser() is False
        assert driver.clicked == []

    def test_a_broken_page_does_not_stop_the_login(self):
        class Exploding:
            def execute_script(self, script, *args):
                raise RuntimeError("페이지가 바뀌었습니다")

        assert self._login(Exploding())._trust_this_browser() is False


class TestLoginFormAlreadyGone:
    """로그인 버튼이 안 보일 때, 화면이 이미 넘어간 것인지 가려내는지.

    실사용에서 겪은 것: 비밀번호를 넣는 사이 폼이 스스로 제출돼 2단계 인증 화면으로
    넘어갔는데, 그 화면에는 로그인 버튼이 없어 "네이버 로그인 버튼을 찾지 못했습니다"로
    발행이 끊겼다. 로그인은 잘 되고 있었다.
    """

    class FakeDriver:
        def __init__(self, url, fields=1, body=""):
            self.current_url = url
            self._fields = fields
            self._body = body

        def find_elements(self, by, selector):
            class Field:
                def is_displayed(self):
                    return True

            return [Field()] * self._fields

        def find_element(self, by, name):
            body = self._body

            class Body:
                text = body

            return Body()

    def _login(self, driver):
        config = NaverConfig(
            blog_id="myblog", profile_dir=Path("."), api_origin="http://localhost:3000"
        )
        return NaverLogin(driver, config)

    def test_leaving_the_login_host_means_the_form_is_done(self):
        driver = self.FakeDriver("https://blog.naver.com/myblog")

        assert self._login(driver)._login_form_is_gone() is True

    def test_the_two_factor_screen_counts_as_done(self):
        """주소는 그대로인데 화면만 바뀌는 경우다 — 실제로 이 모양이었다."""
        driver = self.FakeDriver(
            "https://nid.naver.com/nidlogin.login", body="2단계 인증 알림 발송 완료"
        )

        assert self._login(driver)._login_form_is_gone() is True

    def test_a_form_with_no_visible_fields_counts_as_done(self):
        driver = self.FakeDriver("https://nid.naver.com/nidlogin.login", fields=0)

        assert self._login(driver)._login_form_is_gone() is True

    def test_a_plain_login_form_is_not_done(self):
        """진짜로 화면이 바뀐 경우까지 기다리면 3분을 버리고 끝난다. 그때는 바로 실패다."""
        driver = self.FakeDriver(
            "https://nid.naver.com/nidlogin.login", body="아이디 또는 전화번호 비밀번호 로그인"
        )

        assert self._login(driver)._login_form_is_gone() is False

    def test_a_broken_page_is_not_treated_as_done(self):
        class Exploding:
            current_url = "https://nid.naver.com/nidlogin.login"

            def find_elements(self, by, selector):
                raise RuntimeError("페이지를 읽을 수 없습니다")

        assert self._login(Exploding())._login_form_is_gone() is False


class TestStrayAlert:
    """네이버가 띄우는 안내창을 닫고 계속하는지.

    알림창이 열려 있으면 Selenium의 **모든 명령이 막힌다**. 실제로 로그인 뒤 블로그
    홈에서 "게시물이 삭제되었거나 다른 페이지로 변경되었습니다"가 떠서 그다음 동작이
    전부 멈췄다. 이제 로그인도 에디터도 블로그 홈을 거치지 않고 글쓰기로 바로 간다.
    """

    class FakeAlert:
        def __init__(self, text):
            self.text = text
            self.accepted = False

        def accept(self):
            self.accepted = True

    def test_an_open_alert_is_closed_and_reported(self):
        alert = self.FakeAlert("게시물이 삭제되었거나 다른 페이지로 변경되었습니다.")

        class Driver:
            switch_to = type("S", (), {"alert": alert})()

        assert _dismiss_stray_alert(Driver()) == alert.text
        assert alert.accepted is True

    def test_no_alert_is_not_an_error(self):
        class Driver:
            @property
            def switch_to(self):
                raise RuntimeError("no such alert")

        assert _dismiss_stray_alert(Driver()) is None

    def test_an_empty_alert_still_counts_as_handled(self):
        """문구가 비어도 '닫았다'는 사실은 알려야 한다 — None이면 없었다는 뜻이 된다."""
        alert = self.FakeAlert("")

        class Driver:
            switch_to = type("S", (), {"alert": alert})()

        assert _dismiss_stray_alert(Driver()) == "(내용 없음)"
        assert alert.accepted is True


class TestNaverAccountSwitch:
    """설정에서 네이버 계정을 바꾸면 그 계정으로 발행되는지.

    실사용에서 겪은 것: Chrome 프로필은 블로그잇 사용자별로만 갈려서, 네이버 계정을
    여러 개 번갈아 쓰는 사람이 설정만 바꾸면 **예전 계정 세션이 그대로 살아 있어**
    그 계정으로 발행됐다. 세션이 누구 것인지 프로필에 적어 두고 대조한다.
    """

    def _login(self, profile: Path, username=None, password=None):
        config = NaverConfig(
            blog_id="myblog",
            profile_dir=profile,
            api_origin="http://localhost:3000",
            username=username,
            password=password,
        )
        return NaverLogin(object(), config)

    def test_the_same_account_keeps_the_session(self, tmp_path: Path):
        remember_session_account(tmp_path, "someone")

        assert self._login(tmp_path, "someone", "pw")._session_belongs_to_settings() is True

    def test_a_changed_account_does_not_keep_the_session(self, tmp_path: Path, caplog):
        """여기가 핵심이다. True를 돌려주면 남의 블로그에 발행한다."""
        remember_session_account(tmp_path, "old-account")

        assert self._login(tmp_path, "new-account", "pw")._session_belongs_to_settings() is False
        assert "old-account" not in caplog.text
        assert "new-account" not in caplog.text

    def test_the_account_comparison_ignores_letter_case(self, tmp_path: Path):
        remember_session_account(tmp_path, "SomeOne")

        assert self._login(tmp_path, "someone", "pw")._session_belongs_to_settings() is True

    def test_an_unknown_account_logs_in_once(self, tmp_path: Path):
        """기록이 없는 옛 프로필. 한 번 새로 로그인해 두면 이후로는 항상 맞는다."""
        assert self._login(tmp_path, "someone", "pw")._session_belongs_to_settings() is False

    def test_a_session_that_cannot_be_rebuilt_is_kept(self, tmp_path: Path):
        """비밀번호가 없으면 다시 만들 수 없다. 버리면 발행 자체가 불가능해진다."""
        assert self._login(tmp_path, "someone", None)._session_belongs_to_settings() is True

    def test_no_configured_id_means_nothing_to_compare(self, tmp_path: Path):
        remember_session_account(tmp_path, "someone")

        assert self._login(tmp_path)._session_belongs_to_settings() is True

    def test_the_account_is_recorded_after_a_successful_login(self, tmp_path: Path, monkeypatch):
        login = self._login(tmp_path, "someone", "pw")
        monkeypatch.setattr(login, "_ensure_logged_in", lambda: None)

        login.ensure_logged_in()

        assert session_account(tmp_path) == "someone"

    def test_a_manual_login_clears_the_record(self, tmp_path: Path, monkeypatch):
        """아이디를 모르는 채 사람이 직접 로그인한 경우다. 옛 기록을 남기면 거짓말이 된다."""
        remember_session_account(tmp_path, "old-account")
        login = self._login(tmp_path)
        monkeypatch.setattr(login, "_ensure_logged_in", lambda: None)

        login.ensure_logged_in()

        assert session_account(tmp_path) is None

    def test_forgetting_a_record_that_is_not_there_is_fine(self, tmp_path: Path):
        forget_session_account(tmp_path)  # 예외가 나지 않으면 통과다

        assert session_account(tmp_path) is None


class TestSignOutKeepsBrowserTrust:
    """계정을 바꿀 때 2단계 인증 면제까지 날리지 않는지.

    '이 브라우저는 2단계 인증 없이 로그인 합니다'로 얻은 신뢰는 쿠키에 들어 있다.
    로그아웃하면서 쿠키를 통째로 지우면, 계정을 번갈아 쓸 때마다 2단계 인증을 다시
    하게 된다 — 한 번만 하면 되도록 만든 의미가 사라진다.
    """

    class FakeDriver:
        def __init__(self, still_logged_in):
            self._still = still_logged_in
            self.visited = []
            self.cookies_wiped = False

        def get(self, url):
            self.visited.append(url)

        def get_cookies(self):
            return [{"name": name} for name in SESSION_COOKIES] if self._still else []

        def delete_all_cookies(self):
            self.cookies_wiped = True

        @property
        def switch_to(self):
            raise RuntimeError("no such alert")

    def _login(self, driver, profile):
        config = NaverConfig(
            blog_id="myblog",
            profile_dir=profile,
            api_origin="http://localhost:3000",
            username="new-account",
            password="pw",
        )
        return NaverLogin(driver, config)

    def test_a_clean_logout_leaves_the_cookies_alone(self, tmp_path: Path):
        driver = self.FakeDriver(still_logged_in=False)
        remember_session_account(tmp_path, "old-account")

        self._login(driver, tmp_path)._sign_out()

        assert driver.cookies_wiped is False
        assert any("logout" in url for url in driver.visited)
        assert session_account(tmp_path) is None

    def test_cookies_are_wiped_only_when_the_logout_did_not_work(self, tmp_path: Path):
        """남의 계정으로 발행하느니 2단계 인증을 다시 하는 편이 낫다."""
        driver = self.FakeDriver(still_logged_in=True)

        self._login(driver, tmp_path)._sign_out()

        assert driver.cookies_wiped is True


class TestEditorEntryFallbacks:
    """설정의 blog_id가 지금 로그인한 계정 것이 아닐 때도 글쓰기로 들어가는지.

    실사용에서 겪은 것: blog.naver.com/{blog_id}?Redirect=Write가 "게시물이 삭제되었거나
    다른 페이지로 변경되었습니다" 안내창으로 막혔다. 그 창을 닫으면 네이버가 블로그 홈에
    데려다 놓는데, 그 화면의 '글쓰기' 버튼은 GoBlogWrite.naver를 가리킨다 — 로그인한
    계정의 블로그로 알아서 옮겨 주는 주소다.
    """

    class FakeDriver:
        def __init__(self, enter_at):
            #: 이 주소에 도착했을 때만 에디터 프레임 진입에 성공한다.
            self._enter_at = enter_at
            self.current_url = "about:blank"
            self.visited = []

        def get(self, url):
            self.visited.append(url)
            self.current_url = url

        @property
        def switch_to(self):
            raise RuntimeError("no such alert")

    def _editor(self, driver, monkeypatch):
        editor = SmartEditorOne(driver)
        monkeypatch.setattr(
            editor, "_try_enter_editor_frame", lambda: driver.current_url == driver._enter_at
        )
        monkeypatch.setattr(editor, "_dismiss_draft_popup", lambda: None)
        monkeypatch.setattr(editor, "_wait_for_any", lambda *args: None)
        return editor

    def test_the_configured_blog_is_tried_first(self, monkeypatch):
        driver = self.FakeDriver("https://blog.naver.com/myblog?Redirect=Write")

        self._editor(driver, monkeypatch).navigate("myblog")

        assert driver.visited == ["https://blog.naver.com/myblog?Redirect=Write"]

    def test_a_mismatched_blog_id_falls_back_to_the_account_write_url(self, monkeypatch):
        """여기가 핵심이다. blog_id가 어긋나도 로그인한 계정의 글쓰기로 들어가야 한다."""
        driver = self.FakeDriver(WRITE_REDIRECT_URL)

        self._editor(driver, monkeypatch).navigate("wrong-blog")

        assert driver.visited == [
            "https://blog.naver.com/wrong-blog?Redirect=Write",
            WRITE_REDIRECT_URL,
        ]

    def test_the_write_button_is_the_last_resort(self, monkeypatch):
        """두 주소가 모두 막히면 화면의 '글쓰기' 버튼을 찾는다."""
        # 버튼으로 들어가도 도착지는 설정의 블로그여야 한다(_verify_blog_owner가 본다).
        entered = "https://blog.naver.com/myblog?Redirect=Write&entered=button"
        driver = self.FakeDriver(entered)
        editor = self._editor(driver, monkeypatch)

        def press_button(blog_id):
            driver.get(entered)

        monkeypatch.setattr(editor, "_enter_via_write_button", press_button)
        editor.navigate("myblog")

        assert driver.visited[:2] == [
            "https://blog.naver.com/myblog?Redirect=Write",
            WRITE_REDIRECT_URL,
        ]

    def test_every_route_failing_is_a_loud_error(self, monkeypatch):
        driver = self.FakeDriver("https://never.reached")
        editor = self._editor(driver, monkeypatch)
        monkeypatch.setattr(editor, "_enter_via_write_button", lambda blog_id: None)
        monkeypatch.setattr(editor, "_log_failure", lambda step: None)

        with pytest.raises(RuntimeError, match="글쓰기 화면에 진입하지 못했습니다"):
            editor.navigate("myblog")


class TestWriteButtonOnTheCurrentPage:
    """안내창을 닫고 도착한 화면에 이미 '글쓰기' 버튼이 있으면 거기서 들어가는지.

    그걸 두고 blog.naver.com/{blog_id}로 다시 가면 같은 안내창을 한 번 더 만난다.
    """

    def test_the_button_on_the_current_page_is_used_without_navigating(self, monkeypatch):
        class Driver:
            current_url = "https://section.blog.naver.com/BlogHome.naver"
            window_handles = ["one"]
            visited = []

            def get(self, url):
                self.visited.append(url)

        driver = Driver()
        editor = SmartEditorOne(driver)
        monkeypatch.setattr(editor, "_find_write_button", lambda: "글쓰기-버튼")
        pressed = []
        monkeypatch.setattr(editor, "_click_button", lambda button, name: pressed.append(button))

        editor._enter_via_write_button("myblog")

        assert pressed == ["글쓰기-버튼"]
        assert driver.visited == []   # 블로그 홈으로 다시 가지 않는다


class TestBlogAddressIsLearned:
    """네이버 아이디와 블로그 주소가 다를 때, 실제 주소를 배워서 기억하는지.

    실사용: 아이디는 `win-z`인데 블로그 주소는 `aiona_it`이었다. 네이버 블로그 주소는
    기본이 아이디와 같지만 **바꿀 수 있다.** 아이디로 만든 주소는 "게시물이 삭제되었거나
    다른 페이지로 변경되었습니다"로 막히고, GoBlogWrite 폴백으로 겨우 들어갔다.
    한 번 알아 두면 다음 발행은 첫 번째 경로에서 바로 들어간다.
    """

    def test_the_real_address_is_returned_from_navigate(self, monkeypatch):
        class Driver:
            current_url = "https://blog.naver.com/aiona_it?Redirect=Write&"

            def get(self, url):
                pass

            @property
            def switch_to(self):
                raise RuntimeError("no such alert")

        editor = SmartEditorOne(Driver())
        monkeypatch.setattr(editor, "_try_enter_editor_frame", lambda: True)
        monkeypatch.setattr(editor, "_dismiss_draft_popup", lambda: None)
        monkeypatch.setattr(editor, "_wait_for_any", lambda *args: None)

        assert editor.navigate("win-z") == "aiona_it"

    def test_a_learned_address_wins_over_the_naver_id(self, tmp_path: Path, monkeypatch):
        """다음 발행이 아이디로 만든(막히는) 주소로 다시 가면 안 된다."""
        monkeypatch.setattr(config_module, "naver_profile_dir", lambda user_id=None: tmp_path)
        remember_blog_address(tmp_path, "aiona_it")

        config = config_module.naver_config_from_env(
            username="win-z", password="pw", user_id="user-1"
        )

        assert config.blog_id == "aiona_it"
        assert config.username == "win-z"   # 로그인은 여전히 아이디로 한다

    def test_without_a_learned_address_the_naver_id_is_used(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(config_module, "naver_profile_dir", lambda user_id=None: tmp_path)

        config = config_module.naver_config_from_env(
            username="win-z", password="pw", user_id="user-1"
        )

        assert config.blog_id == "win-z"

    def test_an_empty_address_is_not_remembered(self, tmp_path: Path):
        remember_blog_address(tmp_path, "   ")

        assert observed_blog_address(tmp_path) == ""

    def test_changing_accounts_forgets_the_address(self, tmp_path: Path):
        """이전 계정의 블로그 주소는 새 계정과 상관이 없다. 남기면 남의 블로그로 간다."""
        remember_blog_address(tmp_path, "aiona_it")

        forget_blog_address(tmp_path)

        assert observed_blog_address(tmp_path) == ""


class TestLateAlertDoesNotKillTheLogin:
    """늦게 뜨는 안내창에 로그인 대기가 통째로 죽지 않는지.

    실사용: 안내창은 ``driver.get()`` 직후가 아니라 **로그인 리다이렉트 뒤에** 떴다.
    이동할 때 한 번 닫는 것으로는 못 잡고, 그 뒤로는 주소를 읽는 것조차
    UnexpectedAlertPresentException으로 막혀 크롬이 새 탭인 채 멈췄다.
    """

    def _login(self, driver):
        config = NaverConfig(
            blog_id="myblog", profile_dir=Path("."), api_origin="http://localhost:3000"
        )
        return NaverLogin(driver, config)

    def test_the_url_is_read_after_the_alert_is_closed(self):
        class Driver:
            def __init__(self):
                self.alert_open = True

            @property
            def current_url(self):
                if self.alert_open:
                    raise RuntimeError("unexpected alert open")
                return "https://blog.naver.com/myblog"

            @property
            def switch_to(driver_self):
                outer = driver_self

                class Alert:
                    text = "삭제되었거나 존재하지 않는 게시물입니다."

                    @staticmethod
                    def accept():
                        outer.alert_open = False

                return type("S", (), {"alert": Alert()})()

        driver = Driver()

        assert self._login(driver)._current_url_safely() == "https://blog.naver.com/myblog"
        assert driver.alert_open is False

    def test_an_unreadable_url_is_empty_not_an_exception(self):
        """주소를 못 읽는다고 예외를 올리면 로그인 대기 루프가 죽는다."""

        class Driver:
            @property
            def current_url(self):
                raise RuntimeError("창이 닫혔습니다")

            @property
            def switch_to(self):
                raise RuntimeError("no such alert")

        assert self._login(Driver())._current_url_safely() == ""


class TestALearnedAddressBelongsToItsAccount:
    """이전 계정에서 배운 블로그 주소를 새 계정에 쓰지 않는지.

    설정에서 계정을 바꾼 직후에는 아직 로그아웃 전이라 파일이 남아 있다. 그대로 쓰면
    새 계정으로 이전 계정의 블로그에 들어가려 하고, "삭제되었거나 존재하지 않는
    게시물입니다" 안내창을 만난다.
    """

    def test_the_address_is_used_for_the_account_that_learned_it(self, tmp_path: Path):
        remember_blog_address(tmp_path, "aiona_it")
        remember_session_account(tmp_path, "win-z")

        assert observed_blog_address(tmp_path, "win-z") == "aiona_it"

    def test_the_address_is_dropped_for_a_different_account(self, tmp_path: Path):
        """여기가 핵심이다. 돌려주면 새 계정이 남의 블로그 주소로 들어간다."""
        remember_blog_address(tmp_path, "aiona_it")
        remember_session_account(tmp_path, "win-z")

        assert observed_blog_address(tmp_path, "kyj315900") == ""

    def test_an_unknown_owner_keeps_the_address(self, tmp_path: Path):
        """누가 배운 것인지 모르면 버리지 않는다 — 첫 발행에서 폴백을 한 번 더 거칠 뿐이다."""
        remember_blog_address(tmp_path, "aiona_it")

        assert observed_blog_address(tmp_path, "kyj315900") == "aiona_it"


class TestSettingsLoginOpensTheEditor:
    """설정의 '저장하고 로그인'이 로그인에서 멈추지 않고 글쓰기까지 여는지.

    사용자가 보고 싶은 것은 '로그인됨'이 아니라 **발행할 준비가 된 화면**이다. 여기서
    실제 블로그 주소도 함께 배워 두면 첫 발행이 폴백 경로를 거치지 않는다.
    """

    def test_the_editor_is_opened_and_the_address_is_learned(self, tmp_path: Path, monkeypatch):
        opened: list[str] = []

        class FakeEditor:
            def __init__(self, driver):
                pass

            def navigate(self, blog_id):
                opened.append(blog_id)
                return "aiona_it"

        monkeypatch.setattr(publisher_module, "SmartEditorOne", FakeEditor)

        config = NaverConfig(
            blog_id="win-z", profile_dir=tmp_path, api_origin="http://localhost:3000"
        )
        driver = object()
        # 설정 로그인은 사람이 앞에 있어 대기 시간을 따로 넘긴다(human_wait_seconds).
        publisher_module.NaverLogin = lambda *_a, **_kw: type(
            "L", (), {"ensure_logged_in": lambda self: None}
        )()
        monkeypatch.setattr(publisher_module, "_has_live_session", lambda _d: True)
        monkeypatch.setattr(publisher_module, "_create_driver", lambda *_a: driver)
        monkeypatch.setattr(publisher_module, "_release_kept_open_browser", lambda _p: None)

        asyncio.run(publisher_module.log_in_and_store_session(config, headless=True))

        assert opened == ["win-z"]
        assert observed_blog_address(tmp_path) == "aiona_it"

    def test_a_failing_editor_does_not_undo_a_good_login(self, tmp_path: Path, monkeypatch):
        """세션은 이미 저장됐다. 여기서 실패를 올리면 성공한 로그인이 실패로 보고된다."""

        class BrokenEditor:
            def __init__(self, driver):
                pass

            def navigate(self, blog_id):
                raise RuntimeError("글쓰기 화면을 찾지 못했습니다")

        monkeypatch.setattr(publisher_module, "SmartEditorOne", BrokenEditor)
        config = NaverConfig(
            blog_id="win-z", profile_dir=tmp_path, api_origin="http://localhost:3000"
        )
        # 설정 로그인은 사람이 앞에 있어 대기 시간을 따로 넘긴다(human_wait_seconds).
        publisher_module.NaverLogin = lambda *_a, **_kw: type(
            "L", (), {"ensure_logged_in": lambda self: None}
        )()
        monkeypatch.setattr(publisher_module, "_has_live_session", lambda _d: True)
        monkeypatch.setattr(publisher_module, "_create_driver", lambda *_a: object())
        monkeypatch.setattr(publisher_module, "_release_kept_open_browser", lambda _p: None)

        asyncio.run(publisher_module.log_in_and_store_session(config, headless=True))
