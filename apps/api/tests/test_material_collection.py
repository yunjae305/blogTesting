"""소재 관련순의 수집 경로 — 최신순과 다른 질문을 던지는지.

최신순은 "지금 한국에서 뭐가 뜨나"를, 소재 관련순은 "이 소재로 사람들이 뭘 찾나"를 묻는다.
예전에는 둘이 사실상 같은 질의를 써서, 소재 관련순 후보 대부분이 계절 트렌드였다.
"""

import httpx
import respx

from app.llm.contracts import TrendFetchInput
from app.llm.trends import YouTubeTrendCollector
from app.llm.trends.naver import material_queries
from app.shared import BlogTaskInput, TrendMode


def fetch_input(**overrides) -> TrendFetchInput:
    blog_input = overrides.pop(
        "blog_input",
        BlogTaskInput(
            topic="배틀그라운드",
            subject="게임",
            keywords=[],
            purpose=["사용법·가이드"],
            reference_materials=[],
        ),
    )
    return TrendFetchInput(
        post_id="post_1", user_id="user_1", input=blog_input, **overrides
    )


class TestNaverMaterialQueries:
    """소재 관련순의 발굴 질의는 소재 중심이어야 한다."""

    def test_never_mixes_in_seasonal_queries(self):
        """예전에는 소재 시드 2개에 계절 질의 6개를 섞어, 발굴 문서 대부분이 소재와 무관한
        계절 글이었다 — 소재 관련 후보가 늘 몇 개뿐이던 직접 원인이다."""
        queries = material_queries(fetch_input())

        assert all("축제" not in query and "휴가" not in query for query in queries)
        assert all("배틀그라운드" in query or query == "배틀그라운드" for query in queries)

    def test_leads_with_the_material_itself(self):
        assert material_queries(fetch_input())[0] == "배틀그라운드"

    def test_the_top_up_round_widens_the_axes_without_leaving_the_material(self):
        """보충 회차는 다른 문서를 끌어와야 한다 — 같은 축으로 다시 검색하면 첫 수집과
        같은 결과가 돌아와 후보가 하나도 늘지 않는다.

        질의는 발굴용일 뿐 후보가 아니다: '배틀그라운드 정보'로 검색해 그 문서에서 키워드를
        캐낼 뿐, 그 조합 자체를 후보로 쓰지는 않는다.
        """
        first = material_queries(fetch_input())
        widened = material_queries(fetch_input(widen_material=True))

        # 소재 자체는 두 회차 모두 앞에 온다.
        assert first[0] == widened[0] == "배틀그라운드"
        # 축은 겹치지 않는다.
        assert set(first[1:]).isdisjoint(set(widened[1:]))
        # 넓혀도 소재를 벗어나지 않는다.
        assert all("배틀그라운드" in query for query in widened)

    def test_axes_follow_the_writing_purpose(self):
        """같은 소재라도 '사용법·가이드'로 쓸 때와 '문제 해결'로 쓸 때 사람들이 찾는 것이
        다르다. 코드가 소재 종류를 알 수는 없지만 글 목적은 사용자가 직접 알려준 신호다."""
        guide = material_queries(fetch_input())
        troubleshooting = material_queries(
            fetch_input(
                blog_input=BlogTaskInput(
                    topic="배틀그라운드",
                    subject="게임",
                    keywords=[],
                    purpose=["문제 해결"],
                    reference_materials=[],
                )
            )
        )

        assert "배틀그라운드 설정" in guide
        assert "배틀그라운드 오류" in troubleshooting
        assert "배틀그라운드 오류" not in guide

    def test_introduction_purpose_searches_for_identity_features_and_a_first_look(self):
        queries = material_queries(
            fetch_input(
                blog_input=BlogTaskInput(
                    topic="AIONA",
                    subject="멀티 LLM 서비스",
                    keywords=[],
                    purpose=["입문·소개"],
                    reference_materials=[],
                )
            )
        )

        assert "AIONA 소개" in queries
        assert "AIONA 기능" in queries
        assert "AIONA 처음" in queries

    def test_user_keywords_outrank_generated_axes(self):
        """사용자가 직접 적은 키워드는 이미 소재 종류를 반영한 신호라 어떤 축보다 정확하다."""
        queries = material_queries(
            fetch_input(
                blog_input=BlogTaskInput(
                    topic="배틀그라운드",
                    subject="게임",
                    keywords=["에란겔"],
                    purpose=["사용법·가이드"],
                    reference_materials=[],
                )
            )
        )

        assert queries.index("에란겔") < queries.index("배틀그라운드 사용법")

    def test_a_blank_topic_yields_no_queries(self):
        """소재가 없으면 발굴할 것이 없다 — 빈 질의로 API를 부르지 않는다."""
        assert (
            material_queries(
                fetch_input(
                    blog_input=BlogTaskInput(
                        topic="", subject=None, keywords=[], reference_materials=[]
                    )
                )
            )
            == []
        )


class TestYouTubeMaterialSearch:
    @respx.mock
    async def test_searches_for_the_material_instead_of_the_popular_chart(self):
        """인기 차트로는 답할 수 없는 질문이다 — 오늘 가장 많이 본 영상 50개에
        '배틀그라운드 감도'가 들어 있을 이유가 없다."""
        search = respx.get("https://www.googleapis.com/youtube/v3/search").mock(
            return_value=httpx.Response(
                200, json={"items": [{"id": {"videoId": "v1"}}, {"id": {"videoId": "v2"}}]}
            )
        )
        videos = respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {"snippet": {"title": "배틀그라운드 감도 설정 완벽 정리", "tags": ["감도 설정"]}},
                        {"snippet": {"title": "배틀그라운드 신규 무기", "tags": ["신규 무기"]}},
                    ]
                },
            )
        )

        result = await YouTubeTrendCollector("key").collect(
            fetch_input(mode=TrendMode.MATERIAL_RELATED), None
        )

        assert search.called
        params = search.calls[0].request.url.params
        assert params["q"] == "배틀그라운드"
        assert params["type"] == "video"
        assert params["order"] == "relevance"
        # search.list는 태그를 주지 않으므로 videos.list로 한 번 더 조회한다(1 unit).
        assert videos.called
        assert videos.calls[0].request.url.params["id"] == "v1,v2"
        assert {"감도 설정", "신규 무기"} <= {item.keyword for item in result}

    @respx.mock
    async def test_trending_still_reads_the_popular_chart(self):
        """회귀 방지 — 최신순 경로는 손대지 않았다."""
        search = respx.get("https://www.googleapis.com/youtube/v3/search").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        videos = respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {"snippet": {"title": f"영상{index}", "tags": ["워터밤"]}}
                        for index in range(3)
                    ]
                },
            )
        )

        await YouTubeTrendCollector("key").collect(fetch_input(mode=TrendMode.TRENDING), None)

        assert videos.calls[0].request.url.params["chart"] == "mostPopular"
        # 할당량 100배인 search.list는 최신순에서 절대 불리지 않는다.
        assert not search.called

    @respx.mock
    async def test_an_empty_search_does_not_call_the_detail_endpoint(self):
        respx.get("https://www.googleapis.com/youtube/v3/search").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        videos = respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        result = await YouTubeTrendCollector("key").collect(
            fetch_input(mode=TrendMode.MATERIAL_RELATED), None
        )

        assert result == []
        assert not videos.called
