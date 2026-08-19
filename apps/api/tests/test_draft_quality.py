"""원고 품질 검사.

프롬프트는 본문 길이와 이미지 태그 개수와 해시태그 개수를 시키지만, 모델이 그것을 지켰는지
아무도 확인하지 않았다. 900자짜리 원고도 그대로 화면에 올라갔다는 뜻이다.

여기서 나누는 것이 이 검사의 핵심이다: 원고를 못 쓰게 만드는 것(치명적)과 코드가 이미
감당하는 것(경고). 이미지 태그를 빠뜨린 원고는 _with_inserted_images가 자리를 잡아 주므로,
그걸로 멀쩡한 원고를 버리면 그게 더 나쁘다.
"""

from app.modules.draft.quality import (
    MAX_PARAGRAPH_CHARS,
    MIN_BODY_CHARS,
    body_char_count,
    check_draft,
)
from app.shared import FinalPost

# 서로 다른 문단을 이어 붙인다. 예전 fixture는 한 문장을 150번 반복해서, 지금의
# 반복 검사에 그대로 걸린다 — 그건 검사가 잡아야 할 결함이지 정상 원고가 아니다.
LONG_BODY = "\n\n".join(
    f"{n}번 문단입니다. 항목{n} 준비과정{n} 사례{n} 판단기준{n} 연결설명{n}을 다룹니다. "
    f"독자{n}는 단계{n} 확인사항{n} 선택지{n} 이해흐름{n} 실행메모{n}를 얻습니다."
    for n in range(1, 26)
)


def build_post(**overrides) -> FinalPost:
    defaults = dict(
        title="제목",
        body=LONG_BODY,
        hashtags=["AI", "블로그", "글쓰기", "자동화", "생산성"],
        html_content=(
            f"<article><h2>소제목 하나</h2><p>{LONG_BODY}</p>"
            f"<h2>소제목 둘</h2><p>[[IMAGE: one]]</p><p>[[IMAGE: two]]</p></article>"
        ),
        markdown_content=f"# 제목\n\n{LONG_BODY}",
    )
    return FinalPost(**{**defaults, **overrides})


class TestFatal:
    def test_a_full_length_draft_passes(self):
        assert check_draft(build_post(), hashtag_count=5).ok

    def test_a_short_draft_is_refused(self):
        """모델이 900자를 써 보내면 그것은 원고가 아니다. 프롬프트는 1800자를 시켰다."""
        report = check_draft(build_post(body="짧은 원고." * 20), hashtag_count=5)

        assert not report.ok
        assert str(MIN_BODY_CHARS) in str(report)

    def test_minimum_body_length_can_follow_the_selected_article_length(self):
        report = check_draft(build_post(body=LONG_BODY[:1500]), hashtag_count=5, min_body_chars=1800)

        assert not report.ok
        assert "최소 1800자" in str(report)

    def test_an_empty_body_is_refused(self):
        assert not check_draft(build_post(body=""), hashtag_count=5).ok

    def test_a_missing_title_is_refused(self):
        assert not check_draft(build_post(title=" "), hashtag_count=5).ok

    def test_clickbait_is_refused(self):
        """"충격적", "역대급" 같은 표현은 프롬프트가 쓰지 말라고 했고, 네이버에서 저품질
        문서로 분류되기도 쉽다."""
        report = check_draft(
            build_post(body=f"{LONG_BODY} 이건 정말 역대급 결과입니다."), hashtag_count=5
        )

        assert not report.ok
        assert "역대급" in str(report)

    def test_repeated_sentences_are_refused(self):
        """같은 문장이 여러 번 통째로 반복되면 코드가 손볼 수 없으니 다시 생성한다."""
        repeated = "이 문장은 완전히 똑같이 여러 번 반복되는 충분히 긴 예시 문장입니다."
        body = LONG_BODY + "\n\n" + "\n\n".join([repeated] * 4)

        report = check_draft(build_post(body=body), hashtag_count=5)

        assert not report.ok
        assert "반복" in str(report)

    def test_repeated_three_word_phrases_are_refused(self):
        """문장이 달라도 같은 표현 덩어리가 계속 나오면 품질 결함으로 본다."""
        body = "\n\n".join(
            (
                f"실무 점검 기준, {n}번째 상황에서는 자료 확인과 담당자 공유를 먼저 진행합니다. "
                f"실무 점검 기준 덕분에 결정 흐름을 정리하고, 실무 점검 기준 안에서 다음 행동을 고릅니다."
            )
            for n in range(1, 22)
        )

        report = check_draft(build_post(body=body), hashtag_count=5)

        assert not report.ok
        assert "3-gram" in str(report)


class TestWarnings:
    """코드가 감당하는 것들. 알려는 주되, 원고를 버리지는 않는다."""

    def test_a_missing_image_tag_is_only_a_warning(self):
        post = build_post(html_content=f"<article><p>{LONG_BODY}</p></article>")

        report = check_draft(post, hashtag_count=5)

        assert report.ok  # 버리지 않는다 — _with_inserted_images가 자리를 잡아 준다
        assert report.warnings

    def test_a_wrong_hashtag_count_is_only_a_warning(self):
        report = check_draft(build_post(hashtags=["AI"]), hashtag_count=5)

        assert report.ok
        assert any("해시태그" in warning for warning in report.warnings)

    def test_a_long_paragraph_is_only_a_warning(self):
        """긴 문단은 모바일 가독성을 해치지만, 소제목이 <strong>으로 나올 수도 있어
        멀쩡한 원고를 버리지 않는다 — 강제는 프롬프트가 한다."""
        wall = "가" * (MAX_PARAGRAPH_CHARS + 100)
        report = check_draft(build_post(body=f"{LONG_BODY}\n\n{wall}"), hashtag_count=5)

        assert report.ok
        assert any("문단" in warning for warning in report.warnings)

    def test_missing_headings_is_only_a_warning(self):
        post = build_post(html_content=f"<article><p>{LONG_BODY}</p></article>")

        report = check_draft(post, hashtag_count=5)

        assert report.ok
        assert any("소제목" in warning for warning in report.warnings)

    def test_over_the_maximum_length_is_only_a_warning(self):
        """상한 초과는 프롬프트가 이미 막는다. 긴 글을 반려해 멀쩡한 내용을 버리지 않는다."""
        report = check_draft(build_post(), hashtag_count=5, max_body_chars=100)

        assert report.ok
        assert any("최대" in warning for warning in report.warnings)

    def test_body_character_count_ignores_internal_visual_markers_and_layout_whitespace(self):
        body = "본문 하나\n\n[[VISUAL: visual-1]]\n\n본문 둘 | 비교"

        assert body_char_count(body) == len("본문 하나 본문 둘 비교")

    def test_a_missing_trend_in_the_body_is_only_a_warning(self):
        """선택한 트렌드가 본문에 안 보이면 알려주되, 동의어로 풀었을 수 있어 반려하지 않는다."""
        report = check_draft(build_post(), hashtag_count=5, trend_title="워터밤")

        assert report.ok
        assert any("워터밤" in warning for warning in report.warnings)

    def test_a_trend_anchor_in_the_body_does_not_require_the_full_title_to_be_repeated(self):
        """완성형 제목 전체가 아니라 AIONA 같은 고유 소재어가 본문에 있으면 충분하다."""
        title = "AI 트렌드 핵심 AIONA, 지금 주목받는 배경과 변화 이유"
        post = build_post(body=f"{LONG_BODY}\n\nA.I.O.N.A가 주목받는 배경과 변화 이유를 살펴봅니다.")

        report = check_draft(post, hashtag_count=5, trend_title=title)

        assert report.ok
        assert not any("선택한 트렌드" in warning for warning in report.warnings)

    def test_generic_title_words_without_the_trend_anchor_still_warn(self):
        title = "AI 트렌드 핵심 AIONA, 지금 주목받는 배경과 변화 이유"
        post = build_post(body=f"{LONG_BODY}\n\n최신 트렌드가 주목받는 배경과 변화 이유입니다.")

        report = check_draft(post, hashtag_count=5, trend_title=title)

        assert report.ok
        assert any("AIONA" in warning for warning in report.warnings)

    def test_overused_cliches_are_only_a_warning(self):
        """상투구 과다는 알려주되(다양성 신호), 한두 번은 자연스러우므로 반려하지 않는다."""
        cliches = " 요즘 많은 분들이 궁금해하는 주제입니다. 오늘은 이것을 알아보겠습니다. 지금부터 살펴봅니다."
        report = check_draft(build_post(body=LONG_BODY + cliches), hashtag_count=5)

        assert report.ok
        assert any("상투" in warning for warning in report.warnings)
