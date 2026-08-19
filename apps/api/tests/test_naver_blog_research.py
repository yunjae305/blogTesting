"""네이버 블로그 보강 수집 — 검색 API로 찾고 모바일판에서 본문을 뽑아 M3 자료에 합친다.

2026-08-10 사용자 결정: "네이버 블로그의 글도 가져오면 자료 수집이 더 많아질 것 같아."
구글 기반 수집(M3)이 잘 못 잡는 네이버 생태계의 실사용 후기를 근거로 보강한다.
어떤 실패도 검증을 죽이면 안 된다 — 보강은 없던 일이 될 수 있어야 한다.
"""

import httpx
import respx

from app.llm.live_adapters import GeminiResearchAnalyzer
from app.llm.naver_blog import (
    NAVER_BLOG_SEARCH_URL,
    NaverBlogPost,
    NaverBlogResearch,
    extract_post_text,
    strip_search_markup,
    to_mobile_url,
)
from app.llm.provider_config import RoleConfig
from app.shared import SearchSource


class TestMobileUrl:
    """데스크톱 blog.naver.com은 본문이 iframe 안에 있는 껍데기다 — 모바일판으로 바꿔야
    본문이 읽힌다."""

    def test_a_desktop_link_becomes_the_mobile_one(self):
        assert (
            to_mobile_url("https://blog.naver.com/foodlover/223456789012")
            == "https://m.blog.naver.com/foodlover/223456789012"
        )

    def test_an_old_postview_link_is_converted_too(self):
        assert (
            to_mobile_url(
                "https://blog.naver.com/PostView.naver?blogId=foodlover&logNo=223456789012"
            )
            == "https://m.blog.naver.com/foodlover/223456789012"
        )

    def test_a_mobile_link_stays_mobile(self):
        assert (
            to_mobile_url("http://m.blog.naver.com/foodlover/223456789012")
            == "https://m.blog.naver.com/foodlover/223456789012"
        )

    def test_non_naver_and_broken_links_are_refused(self):
        assert to_mobile_url("https://example.com/foodlover/1") is None
        assert to_mobile_url("https://blog.naver.com/only-id") is None
        assert to_mobile_url("ftp://blog.naver.com/a/1") is None
        assert to_mobile_url("") is None


class TestPostTextExtraction:
    def test_smart_editor_body_is_extracted_without_markup(self):
        page = (
            "<html><body><div class='wrap'>"
            '<div class="se-main-container">'
            "<script>var tracker=1;</script>"
            "<p>강릉 &amp; 속초 카페를 직접 다녀왔습니다. " + "실사용 후기입니다. " * 10 + "</p>"
            "<p>주차는 <b>2시간 무료</b>였습니다.</p>"
            "</div></body></html>"
        )
        text = extract_post_text(page)
        assert text is not None
        assert "강릉 & 속초 카페" in text
        assert "주차는 2시간 무료" in text
        assert "tracker" not in text
        assert "<" not in text

    def test_a_page_without_a_body_container_is_refused(self):
        """전체 페이지 텍스트로 물러나지 않는다 — 메뉴·댓글이 '사실'로 섞인다."""
        assert extract_post_text("<html><body><nav>메뉴</nav>본문 없음</body></html>") is None

    def test_a_too_short_body_is_refused(self):
        page = '<div class="se-main-container"><p>짧다.</p></div>'
        assert extract_post_text(page) is None

    def test_the_excerpt_is_capped(self):
        page = '<div class="se-main-container"><p>' + ("가" * 5000) + "</p></div>"
        text = extract_post_text(page)
        assert text is not None and len(text) <= 2000

    def test_search_markup_is_stripped(self):
        assert strip_search_markup("<b>강릉</b> 카페 &quot;후기&quot;") == '강릉 카페 "후기"'


def search_item(index: int, link: str | None = None) -> dict:
    return {
        "title": f"<b>강릉 카페</b> 후기 {index}",
        "link": link or f"https://blog.naver.com/user{index}/22345678901{index}",
        "description": f"직접 다녀온 <b>후기</b> {index}",
        "bloggername": f"블로거{index}",
        "postdate": "20260808",
    }


def post_page() -> str:
    return (
        '<div class="se-main-container"><p>'
        + "직접 다녀와서 확인한 내용입니다. " * 12
        + "</p></div>"
    )


class TestCollect:
    @respx.mock
    async def test_posts_come_with_their_mobile_body(self):
        respx.get(NAVER_BLOG_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json={"items": [search_item(1), search_item(2)]}
            )
        )
        respx.get("https://m.blog.naver.com/user1/223456789011").mock(
            return_value=httpx.Response(200, text=post_page())
        )
        # 둘째 글은 본문을 못 연다 — 그 글만 조용히 빠진다.
        respx.get("https://m.blog.naver.com/user2/223456789012").mock(
            return_value=httpx.Response(404)
        )

        posts = await NaverBlogResearch("id", "secret").collect(["강릉 카페"])

        assert len(posts) == 1
        assert posts[0].title == "강릉 카페 후기 1"
        assert posts[0].url == "https://blog.naver.com/user1/223456789011"
        assert "직접 다녀와서 확인한" in posts[0].excerpt
        assert posts[0].snippet == "직접 다녀온 후기 1"

    @respx.mock
    async def test_a_search_failure_returns_what_was_already_found(self):
        respx.get(NAVER_BLOG_SEARCH_URL).mock(return_value=httpx.Response(500))

        posts = await NaverBlogResearch("id", "secret").collect(["강릉 카페", "속초"])

        assert posts == []

    @respx.mock
    async def test_the_second_query_runs_only_when_the_first_found_nothing(self):
        search = respx.get(NAVER_BLOG_SEARCH_URL).mock(
            side_effect=[
                httpx.Response(200, json={"items": []}),
                httpx.Response(200, json={"items": [search_item(3)]}),
            ]
        )
        respx.get("https://m.blog.naver.com/user3/223456789013").mock(
            return_value=httpx.Response(200, text=post_page())
        )

        posts = await NaverBlogResearch("id", "secret").collect(["없는말", "강릉 카페"])

        assert search.call_count == 2
        assert len(posts) == 1


def role() -> RoleConfig:
    return RoleConfig(
        role="M3_SEARCH",
        label="검증",
        provider="gemini",
        model="m",
        api_key_env="K",
        api_key="k",
        has_credentials=True,
    )


class TestReferenceUrlNormalisation:
    def test_a_desktop_naver_reference_url_is_sent_as_mobile(self):
        """사용자가 참고자료로 넣은 데스크톱 블로그 주소는 껍데기(iframe)라, URL Context가
        본문을 읽도록 모바일판으로 바꿔 보낸다. 다른 주소는 그대로다."""
        from app.llm.live_adapters import _reference_urls
        from app.llm.contracts import WebSearchAnalysisInput
        from app.shared import BlogTaskInput, ReferenceMaterial, ReferenceMaterialType

        urls = _reference_urls(
            WebSearchAnalysisInput(
                post_id="post_1",
                user_id="user_1",
                input=BlogTaskInput(
                    topic="강릉",
                    keywords=[],
                    reference_materials=[
                        ReferenceMaterial(
                            type=ReferenceMaterialType.URL,
                            value="https://blog.naver.com/user1/223456789011",
                        ),
                        ReferenceMaterial(
                            type=ReferenceMaterialType.URL,
                            value="https://example.com/notice",
                        ),
                    ],
                ),
                prompt_version="m3-intent@v1.0",
            )
        )

        assert urls == [
            "https://m.blog.naver.com/user1/223456789011",
            "https://example.com/notice",
        ]


class TestAnalyzerMerge:
    """분석기가 블로그 글을 브리핑·출처에 합치는 규칙."""

    def test_posts_join_the_briefing_and_the_source_list(self):
        analyzer = GeminiResearchAnalyzer(role(), role())
        sources = [SearchSource(title="기존", url="https://example.com/a", snippet="s")]
        posts = [
            NaverBlogPost(
                title="강릉 카페 후기",
                url="https://blog.naver.com/user1/1",
                snippet="요약",
                excerpt="직접 확인한 내용",
                posted_at="20260808",
            )
        ]

        summary, merged = analyzer._merged_naver_blog("브리핑", sources, [posts])

        assert [s.url for s in merged] == [
            "https://example.com/a",
            "https://blog.naver.com/user1/1",
        ]
        assert "네이버 블로그 실사용 글" in summary
        assert "직접 확인한 내용" in summary
        assert "문장을 그대로 옮기지 않는다" in summary

    def test_duplicate_urls_and_failures_change_nothing(self):
        analyzer = GeminiResearchAnalyzer(role(), role())
        sources = [SearchSource(title="기존", url="https://blog.naver.com/user1/1", snippet="s")]
        duplicated = [
            NaverBlogPost(title="같은 글", url="https://blog.naver.com/user1/1", snippet="", excerpt="x")
        ]

        assert analyzer._merged_naver_blog("브리핑", sources, [duplicated]) == (
            "브리핑",
            sources,
        )
        # 보강 실패(예외)도 검증을 건드리지 않는다.
        assert analyzer._merged_naver_blog("브리핑", sources, [RuntimeError("죽음")]) == (
            "브리핑",
            sources,
        )

    async def test_the_whole_flow_carries_blog_posts_into_the_summary_call(self):
        """수집(스텁)과 병렬로 블로그를 모아, 요약 호출이 받는 브리핑·출처에 실리는지."""

        class StubBlogResearch:
            async def collect(self, queries, limit=3):
                assert queries[0] == "강릉 카페"  # 고른 트렌드 키워드가 첫 질의다
                return [
                    NaverBlogPost(
                        title="실사용 후기",
                        url="https://blog.naver.com/user9/9",
                        snippet="요약",
                        excerpt="본문 발췌",
                    )
                ]

        analyzer = GeminiResearchAnalyzer(role(), role(), blog_research=StubBlogResearch())

        async def collected(_input):
            return "브리핑", [SearchSource(title="구글", url="https://g.example/1", snippet="s")], False, []

        captured: dict = {}

        async def summarize(analysis_input, summary, sources, successful_reference_urls):
            captured["summary"] = summary
            captured["sources"] = sources
            raise RuntimeError("요약은 이 테스트의 관심사가 아니다")

        analyzer._collect_research = collected
        analyzer._summarize_intent = summarize

        from app.shared import BlogTaskInput
        from app.llm.contracts import WebSearchAnalysisInput

        result = await analyzer.search_and_analyze(
            WebSearchAnalysisInput(
                post_id="post_1",
                user_id="user_1",
                input=BlogTaskInput(topic="강릉 여행", keywords=["카페"]),
                prompt_version="m3-intent@v1.0",
                selected_keywords=["강릉 카페"],
            )
        )

        # 요약이 실패해도 자료로 폴백하고, 그 자료에 블로그 글이 들어 있다.
        assert "네이버 블로그 실사용 글" in captured["summary"]
        assert [s.url for s in captured["sources"]] == [
            "https://g.example/1",
            "https://blog.naver.com/user9/9",
        ]
        assert result.collected_source_count == 2
