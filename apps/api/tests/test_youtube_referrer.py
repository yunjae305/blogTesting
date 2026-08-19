"""유튜브 키의 HTTP 리퍼러 제한(2026-08-12).

새로 발급한 키에 '애플리케이션 제한 = HTTP 리퍼러'가 걸려 있었다. 서버 호출에는 Referer
헤더가 없어 유튜브에 닿기도 전에 전부 403이었다(API_KEY_HTTP_REFERRER_BLOCKED). 콘솔을
고칠 수 없는 상황이라, 등록된 리퍼러를 우리가 실어 보내는 것으로 맞췄다.

여기서 지키는 것은 **호출부 어느 하나도 헤더를 빠뜨리지 않는 것**이다. 한 곳이 빠지면 그
경로만 조용히 403으로 죽고, 로그에는 "수집 실패"만 남는다.
"""

import httpx
import pytest
import respx

from app.llm.contracts import TrendFetchInput
from app.llm.photo_search import (
    YOUTUBE_SEARCH_URL,
    YOUTUBE_VIDEOS_URL,
    YouTubeThumbnailSearch,
)
from app.llm.provider_config import resolve_llm_config
from app.llm.trends import YouTubeTrendCollector
from app.llm.youtube_api import DEFAULT_YOUTUBE_API_REFERRER, api_headers
from app.shared import BlogTaskInput, TrendMode

TRENDS_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
TRENDS_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


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
    return TrendFetchInput(post_id="post_1", user_id="user_1", input=blog_input, **overrides)


def video(title: str) -> dict:
    return {"id": "vid1", "snippet": {"title": title, "tags": [title]}}


class TestTheHeaderItself:
    def test_no_referrer_means_no_header(self):
        """제한이 없는 키에는 헤더를 지어내지 않는다."""
        assert api_headers(None) == {}
        assert api_headers("") == {}

    def test_the_referrer_becomes_a_referer_header(self):
        assert api_headers("http://localhost:5173/") == {"Referer": "http://localhost:5173/"}


class TestTheTrendCollectorCarriesIt:
    @pytest.mark.asyncio
    @respx.mock
    async def test_the_popular_chart_call_carries_the_referer(self):
        route = respx.get(TRENDS_VIDEOS_URL).mock(
            return_value=httpx.Response(200, json={"items": [video("배그")]})
        )

        await YouTubeTrendCollector("key").collect(fetch_input(), None)

        assert route.calls[0].request.headers["referer"] == DEFAULT_YOUTUBE_API_REFERRER

    @pytest.mark.asyncio
    @respx.mock
    async def test_the_material_search_calls_carry_it_too(self):
        """소재 관련순은 search.list와 videos.list 둘 다 부른다 — 둘 다 실려야 한다."""
        search = respx.get(TRENDS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json={"items": [{"id": {"videoId": "vid1"}}]}
            )
        )
        videos = respx.get(TRENDS_VIDEOS_URL).mock(
            return_value=httpx.Response(200, json={"items": [video("감도 설정")]})
        )

        await YouTubeTrendCollector("key").collect(
            fetch_input(mode=TrendMode.MATERIAL_RELATED), None
        )

        assert search.calls[0].request.headers["referer"] == DEFAULT_YOUTUBE_API_REFERRER
        assert videos.calls[0].request.headers["referer"] == DEFAULT_YOUTUBE_API_REFERRER

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_custom_referrer_is_used_as_given(self):
        route = respx.get(TRENDS_VIDEOS_URL).mock(
            return_value=httpx.Response(200, json={"items": [video("배그")]})
        )

        await YouTubeTrendCollector("key", "https://blog.example/").collect(fetch_input(), None)

        assert route.calls[0].request.headers["referer"] == "https://blog.example/"


class TestTheThumbnailSearchCarriesIt:
    @pytest.mark.asyncio
    @respx.mock
    async def test_the_search_call_carries_the_referer(self):
        search = respx.get(YOUTUBE_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json={"items": [{"id": {"videoId": "vid1"}, "snippet": {"title": "회차"}}]},
            )
        )
        respx.get("https://i.ytimg.com/vi/vid1/maxresdefault.jpg").mock(
            return_value=httpx.Response(404)
        )

        await YouTubeThumbnailSearch("key").find_photos("전과자")

        assert search.calls[0].request.headers["referer"] == DEFAULT_YOUTUBE_API_REFERRER

    @pytest.mark.asyncio
    @respx.mock
    async def test_the_detail_call_carries_it_too(self):
        """채점 경로(program_name)는 videos.list를 한 번 더 부른다."""
        respx.get(YOUTUBE_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json={"items": [{"id": {"videoId": "vid1"}, "snippet": {"title": "전과자 EP.1"}}]},
            )
        )
        detail = respx.get(YOUTUBE_VIDEOS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "vid1",
                            "snippet": {"title": "전과자 EP.1", "channelTitle": "전과자"},
                            "contentDetails": {"duration": "PT12M30S"},
                        }
                    ]
                },
            )
        )
        respx.get("https://i.ytimg.com/vi/vid1/maxresdefault.jpg").mock(
            return_value=httpx.Response(404)
        )

        await YouTubeThumbnailSearch("key").find_photos("전과자", program_name="전과자")

        assert detail.calls[0].request.headers["referer"] == DEFAULT_YOUTUBE_API_REFERRER

    @pytest.mark.asyncio
    @respx.mock
    async def test_the_thumbnail_download_does_not_carry_it(self):
        """썸네일 파일은 구글 API가 아니다 — 우리 리퍼러를 남의 서버에 흘리지 않는다."""
        respx.get(YOUTUBE_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json={"items": [{"id": {"videoId": "vid1"}, "snippet": {"title": "회차"}}]},
            )
        )
        download = respx.get("https://i.ytimg.com/vi/vid1/maxresdefault.jpg").mock(
            return_value=httpx.Response(404)
        )

        await YouTubeThumbnailSearch("key").find_photos("전과자")

        assert "referer" not in download.calls[0].request.headers


class TestTheSetting:
    def test_the_default_is_used_when_the_env_is_empty(self):
        config = resolve_llm_config({"YOUTUBE_API_KEY": "key"})

        assert config.trend.youtube_api_referrer == DEFAULT_YOUTUBE_API_REFERRER

    def test_the_env_overrides_it(self):
        config = resolve_llm_config(
            {"YOUTUBE_API_KEY": "key", "YOUTUBE_API_REFERRER": "https://blog.example/"}
        )

        assert config.trend.youtube_api_referrer == "https://blog.example/"
