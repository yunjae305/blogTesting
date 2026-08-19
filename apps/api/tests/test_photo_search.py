"""웹 사진 검색(네이버 이미지 검색) — 규격 미달·실패 후보를 넘기고 쓸 것만 고른다."""

import base64
import io

import httpx
import pytest
import respx
from PIL import Image

from app.llm import image_origin
from app.llm.photo_search import (
    MIN_CROPPED_WIDTH,
    MIN_EDGE,
    NAVER_IMAGE_SEARCH_URL,
    YOUTUBE_SEARCH_URL,
    NaverPhotoSearch,
    PhotoSearchError,
    YouTubeThumbnailSearch,
)

SEARCH_URL = NAVER_IMAGE_SEARCH_URL  # API HUB 경로에는 확장자가 없다(2026-08-11)


def jpeg_bytes(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 130, 140)).save(buffer, format="JPEG")
    return buffer.getvalue()


def search_payload(*links: str) -> dict:
    return {
        "items": [
            {"title": f"<b>백지헌</b> 사진 {index}", "link": link}
            for index, link in enumerate(links)
        ]
    }


@pytest.fixture
def search():
    return NaverPhotoSearch("client-id", "client-secret")


@pytest.mark.asyncio
@respx.mock
async def test_it_returns_the_first_usable_photo(search):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=search_payload("https://news.example/a.jpg"))
    )
    respx.get("https://news.example/a.jpg").mock(
        return_value=httpx.Response(
            200, content=jpeg_bytes(1200, 800), headers={"content-type": "image/jpeg"}
        )
    )

    photos = await search.find_photos("백지헌")

    assert len(photos) == 1
    photo = photos[0]
    assert photo.source_url == "https://news.example/a.jpg"
    assert photo.source_host == "news.example"
    assert photo.width == 1200 and photo.height == 800
    assert photo.data_url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(photo.data_url.split(",", 1)[1]) == jpeg_bytes(1200, 800)


@pytest.mark.asyncio
@respx.mock
async def test_the_naver_bold_tag_never_reaches_the_title(search):
    """네이버는 질의어에 <b>를 씌워 돌려준다. 캡션·제목에 태그가 실리면 안 된다."""
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=search_payload("https://news.example/a.jpg"))
    )
    respx.get("https://news.example/a.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1200, 800))
    )

    photos = await search.find_photos("백지헌")

    assert photos[0].title == "백지헌 사진 0"


@pytest.mark.asyncio
@respx.mock
async def test_only_small_results_return_a_reference_only_photo(search):
    """규격 사진이 하나도 없으면 미달 사진을 참고용(meets_spec=False)으로 돌려준다 —
    직접 싣지는 못해도 이미지 생성의 시각 기준이 된다(2026-08-03 사용자 결정)."""
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=search_payload("https://a.example/tiny.jpg"))
    )
    respx.get("https://a.example/tiny.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(320, 180))
    )

    photos = await search.find_photos("백지헌")

    assert len(photos) == 1
    assert photos[0].meets_spec is False


@pytest.mark.asyncio
@respx.mock
async def test_a_photo_smaller_than_the_thumbnail_is_skipped(search):
    """작은 사진은 규격 사진이 있으면 쓰지 않는다 — 확대하면 뭉개진다."""
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_payload("https://a.example/tiny.jpg", "https://b.example/big.jpg"),
        )
    )
    respx.get("https://a.example/tiny.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(MIN_EDGE - 1, MIN_EDGE - 1))
    )
    respx.get("https://b.example/big.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1600, 900))
    )

    photos = await search.find_photos("백지헌")

    assert [p.source_host for p in photos] == ["b.example"]


@pytest.mark.asyncio
@respx.mock
async def test_a_download_failure_moves_on_to_the_next_candidate(search):
    """이미지 서버가 막는 일은 흔하다. 한 장이 막혔다고 사진을 포기하지 않는다."""
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_payload("https://blocked.example/x.jpg", "https://ok.example/y.jpg"),
        )
    )
    respx.get("https://blocked.example/x.jpg").mock(return_value=httpx.Response(403))
    respx.get("https://ok.example/y.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1200, 800))
    )

    photos = await search.find_photos("백지헌")

    assert [p.source_host for p in photos] == ["ok.example"]


@pytest.mark.asyncio
@respx.mock
async def test_something_that_is_not_an_image_is_skipped(search):
    """검색 결과의 link가 언제나 이미지인 것은 아니다(에러 페이지·리다이렉트 HTML)."""
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json=search_payload("https://a.example/not-image.jpg")
        )
    )
    respx.get("https://a.example/not-image.jpg").mock(
        return_value=httpx.Response(200, content=b"<html>not found</html>")
    )

    assert await search.find_photos("백지헌") == []


@pytest.mark.asyncio
@respx.mock
async def test_several_photos_come_from_different_hosts(search):
    """여러 장이 한 기사에서 잘라 온 연속컷이 되지 않게 출처를 흩는다."""
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_payload(
                "https://a.example/1.jpg",
                "https://a.example/2.jpg",
                "https://b.example/3.jpg",
            ),
        )
    )
    for url in ("https://a.example/1.jpg", "https://a.example/2.jpg", "https://b.example/3.jpg"):
        respx.get(url).mock(return_value=httpx.Response(200, content=jpeg_bytes(1200, 800)))

    photos = await search.find_photos("백지헌", limit=2)

    assert [p.source_host for p in photos] == ["a.example", "b.example"]


@pytest.mark.asyncio
@respx.mock
async def test_photos_carry_the_real_origin_not_the_cdn_host(search):
    """사진에는 되찾은 실제 원본 출처가 함께 실린다(2026-08-11 사용자 지시).

    네이버 뉴스 이미지 주소는 언론사 코드와 기사 번호를 담고 있어 기사 페이지를
    되만들 수 있다 — CDN 호스트(imgnews.naver.net)는 사이트 이름이 아니다.
    """
    # 언론사 코드는 다른 테스트와 겹치지 않게 둔다 — 언론사명 캐시가 프로세스 전역이다.
    link = "https://imgnews.pstatic.net/image/421/2026/08/11/0004567890_001_x.jpg"
    article = "https://n.news.naver.com/article/421/0004567890"
    image_origin._press_names.pop("421", None)
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=search_payload(link)))
    respx.get(link).mock(return_value=httpx.Response(200, content=jpeg_bytes(1200, 800)))
    respx.get(article).mock(
        return_value=httpx.Response(
            200, text='<meta property="og:article:author" content="뉴스1 | 네이버">'
        )
    )

    photos = await search.find_photos("백지헌")

    assert photos[0].source_host == "imgnews.pstatic.net"  # 파일이 놓인 곳은 그대로 남는다
    assert photos[0].source_name == "뉴스1"
    assert photos[0].source_page_url == article


@pytest.mark.asyncio
@respx.mock
async def test_the_same_site_on_different_file_servers_counts_once(search):
    """i2/i3.ruliweb.com은 파일 서버만 다른 같은 사이트다 — 출처를 흩을 때 하나로 센다."""
    links = (
        "https://i2.ruliweb.com/1.jpg",
        "https://i3.ruliweb.com/2.jpg",
        "https://cdn.instiz.net/3.jpg",
    )
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=search_payload(*links))
    )
    for url in links:
        respx.get(url).mock(return_value=httpx.Response(200, content=jpeg_bytes(1200, 800)))

    photos = await search.find_photos("백지헌", limit=2)

    assert [p.source_name for p in photos] == ["ruliweb.com", "instiz.net"]


@pytest.mark.asyncio
@respx.mock
async def test_a_missing_search_permission_says_how_to_fix_it(search):
    """인증이 막히면 여기서만 조용히 실패한다 — 어디를 봐야 하는지 메시지가 말해야 한다.

    2026-08-11 API HUB 이관 뒤로는 원인이 둘이다: 옛 개발자센터 키를 그대로 뒀거나,
    그 Application에 이미지 검색이 선택돼 있지 않거나.
    """
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(403, text="not permitted"))

    with pytest.raises(PhotoSearchError) as error:
        await search.find_photos("백지헌")

    assert "검색" in str(error.value)
    assert "API HUB" in str(error.value)


@pytest.mark.asyncio
@respx.mock
async def test_no_results_is_an_empty_list_not_an_error(search):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    assert await search.find_photos("있을 리 없는 소재") == []


@pytest.mark.asyncio
async def test_an_empty_query_never_calls_the_api(search):
    """질의가 비었으면 호출 자체를 하지 않는다(respx 없이 도는 것이 증거다)."""
    assert await search.find_photos("   ") == []


@pytest.mark.asyncio
@respx.mock
async def test_a_tall_portrait_that_survives_the_crop_too_small_is_skipped(search):
    """640x960 보도 사진은 하한은 넘지만 16:9로 자르면 640x360뿐이다 — 썸네일로
    2.4배 늘어나 얼굴이 뭉개진다. 자른 뒤를 기준으로 봐야 한다."""
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_payload("https://a.example/tall.jpg", "https://b.example/big.jpg"),
        )
    )
    respx.get("https://a.example/tall.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(640, 960))
    )
    respx.get("https://b.example/big.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1200, 1642))
    )

    photos = await search.find_photos("백지헌")

    assert [p.source_host for p in photos] == ["b.example"]


@pytest.mark.asyncio
@respx.mock
async def test_a_portrait_large_enough_for_the_900px_body_is_usable(search):
    """본문 최종 폭은 900px다. 16:9 크롭 뒤 720px가 남으면 확대가 1.25배라 허용한다.

    예전 1200px 출력용 960px 하한을 유지하면 충분히 선명한 실사진도 버리고 더 느리고
    비싼 AI 생성으로 내려갔다.
    """
    assert MIN_CROPPED_WIDTH == 720
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json=search_payload("https://a.example/usable-portrait.jpg")
        )
    )
    respx.get("https://a.example/usable-portrait.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(800, 1000))
    )

    photos = await search.find_photos("백지헌")

    assert len(photos) == 1
    assert photos[0].meets_spec is True


@pytest.mark.asyncio
@respx.mock
async def test_it_asks_naver_for_large_images_only(search):
    """filter를 빼면 후보 대부분이 목록용 축소본이라 규격 검사에서 다 떨어진다."""
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    await search.find_photos("백지헌")

    assert route.calls[0].request.url.params["filter"] == "large"


class TestComposition:
    """구도 적합성(2026-08-05). 관련도 순서만 믿으면 세로로 긴 상세컷이 1위일 때 그것이
    실리고, 16:9로 자르는 순간 대상의 절반이 사라진다."""

    def photo(self, width: int, height: int, title: str = "레이디 디올 가방") -> object:
        from app.shared import WebPhoto

        return WebPhoto(
            data_url="data:image/jpeg;base64,AA==",
            source_url="https://a.example/x.jpg",
            source_host="a.example",
            title=title,
            query="레이디 디올 가방",
            width=width,
            height=height,
            meets_spec=True,
        )

    def test_a_sixteen_nine_photo_loses_nothing(self):
        from app.llm.photo_search import framing_score

        assert framing_score(1600, 900) == pytest.approx(100.0)

    def test_a_portrait_photo_scores_far_lower(self):
        from app.llm.photo_search import framing_score

        assert framing_score(1000, 1500) < framing_score(1500, 1000)

    def test_a_portrait_photo_falls_below_the_threshold(self):
        from app.llm.photo_search import GOOD_COMPOSITION_SCORE, composition_score

        assert composition_score(self.photo(1000, 1500)) < GOOD_COMPOSITION_SCORE
        assert composition_score(self.photo(1600, 900)) >= GOOD_COMPOSITION_SCORE

    def test_a_title_that_names_something_else_scores_lower(self):
        from app.llm.photo_search import composition_score

        assert composition_score(self.photo(1600, 900, "디올 향수 신제품")) < (
            composition_score(self.photo(1600, 900))
        )

    def test_an_unsplittable_query_stays_neutral(self):
        from app.llm.photo_search import subject_match_score

        assert subject_match_score("무엇이든", "") == 50.0


@pytest.mark.asyncio
@respx.mock
async def test_a_well_framed_photo_wins_over_a_more_relevant_tall_one(search):
    """검색 1위가 세로로 긴 상세컷이면 예전에는 그것이 실렸다. 같은 묶음 안에서는
    구도가 좋은 쪽을 먼저 쓴다 — 손잡이만 남은 가방 사진이 나온 자리다."""
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_payload("https://tall.example/a.jpg", "https://wide.example/b.jpg"),
        )
    )
    respx.get("https://tall.example/a.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1000, 1500))
    )
    respx.get("https://wide.example/b.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1600, 900))
    )

    photos = await search.find_photos("백지헌")

    assert [p.source_host for p in photos] == ["wide.example"]


@pytest.mark.asyncio
@respx.mock
async def test_a_poorly_framed_photo_is_still_better_than_none(search):
    """구도가 아쉽다고 사진 없이 발행하지는 않는다 — 문턱을 넘는 후보가 없으면
    받은 것 중 가장 나은 것을 쓴다."""
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json=search_payload("https://tall.example/a.jpg")
        )
    )
    respx.get("https://tall.example/a.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1000, 1500))
    )

    photos = await search.find_photos("백지헌")

    assert [p.source_host for p in photos] == ["tall.example"]
    assert photos[0].meets_spec is True


def test_the_crop_width_is_measured_on_the_sixteen_by_nine_result():
    from app.llm.photo_search import cropped_width

    assert cropped_width(1920, 1080) == 1920  # 이미 16:9 — 그대로
    assert cropped_width(640, 960) == 640  # 세로 사진 — 가로가 그대로 남고 위아래가 잘린다
    assert cropped_width(3000, 1000) == 1778  # 가로가 넓다 — 좌우가 잘린다
    assert cropped_width(0, 0) == 0


# ── 유튜브 썸네일 소스 ─────────────────────────────────────────────────────────


def youtube_payload(*video_ids: str) -> dict:
    return {
        "items": [
            {"id": {"videoId": vid}, "snippet": {"title": f"영상 {vid}"}}
            for vid in video_ids
        ]
    }


@pytest.fixture
def youtube():
    return YouTubeThumbnailSearch("api-key")


@pytest.mark.asyncio
@respx.mock
async def test_youtube_returns_the_maxres_thumbnail_as_a_photo(youtube):
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=youtube_payload("vid1"))
    )
    respx.get("https://i.ytimg.com/vi/vid1/maxresdefault.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1280, 720))
    )

    photos = await youtube.find_photos("게임 공략")

    assert len(photos) == 1
    photo = photos[0]
    # 출처는 썸네일 파일이 아니라 영상 자체다 — 생성 기록에서 따라갈 수 있어야 한다.
    assert photo.source_url == "https://www.youtube.com/watch?v=vid1"
    assert photo.source_host == "youtube.com"
    assert photo.title == "영상 vid1"
    assert photo.width == 1280 and photo.height == 720


@pytest.mark.asyncio
@respx.mock
async def test_a_video_without_maxres_is_skipped_for_the_next_one(youtube):
    """maxres 썸네일이 없는 영상은 404다 — high(480×360)는 규격 미달이라 쓰지 않고
    다음 영상으로 넘어간다."""
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=youtube_payload("no-maxres", "ok"))
    )
    respx.get("https://i.ytimg.com/vi/no-maxres/maxresdefault.jpg").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://i.ytimg.com/vi/ok/maxresdefault.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1280, 720))
    )

    photos = await youtube.find_photos("게임 공략")

    assert [p.source_url for p in photos] == ["https://www.youtube.com/watch?v=ok"]


@pytest.mark.asyncio
@respx.mock
async def test_a_youtube_quota_error_says_what_happened(youtube):
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(403, text="quotaExceeded")
    )

    with pytest.raises(PhotoSearchError) as error:
        await youtube.find_photos("게임 공략")

    assert "쿼터" in str(error.value)


@pytest.mark.asyncio
@respx.mock
async def test_youtube_no_results_is_an_empty_list(youtube):
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    assert await youtube.find_photos("있을 리 없는 소재") == []


@pytest.mark.asyncio
async def test_youtube_empty_query_never_calls_the_api(youtube):
    assert await youtube.find_photos("   ") == []


# --- 공식 회차 판정(2026-08-03 병합) ----------------------------------------
#
# 검색 관련도 순서를 그대로 믿으면 실제 프로그램 자리에 남의 2차 창작이나 이름만 같은
# 다른 영상이 실린다. 정식 명칭을 아는 글(program_name)에서만 채점이 켜지고, 모르는
# 일반 카드에서는 예전 동작 그대로다.

from app.llm.photo_search import (  # noqa: E402
    MIN_RELEVANCE_SCORE,
    YOUTUBE_VIDEOS_URL,
    duration_seconds,
    score_candidate,
)


def video(vid, title, channel="어떤채널", duration="PT12M30S", description=""):
    return {
        "id": vid,
        "snippet": {"title": title, "channelTitle": channel, "description": description},
        "contentDetails": {"duration": duration},
    }


class TestScoreCandidate:
    def test_the_official_episode_clears_the_bar(self):
        official = video(
            "v1", "전과자 EP.12 이창섭이 항공운항과 실습에 참여했다", "전과자 공식 채널"
        )
        assert (
            score_candidate(official, program_name="전과자", person_names=["이창섭"])
            >= MIN_RELEVANCE_SCORE
        )

    def test_the_program_name_in_the_title_clears_the_bar_alone(self):
        """2026-08-10 사용자 결정 — 공식 채널·출연자 확인이 없어도 정식 명칭이 제목에
        있으면 후보다. 출처는 캡션(채널명·영상 주소)이 책임지고, 엉뚱한 피사체는 픽셀을
        실제로 보는 판정 게이트가 거른다."""
        loose = video("v2", "전과자 하이라이트 모음", "잡다한채널")
        assert (
            score_candidate(loose, program_name="전과자", person_names=["이창섭"])
            >= MIN_RELEVANCE_SCORE
        )

    def test_a_video_without_the_program_name_is_rejected(self):
        unrelated = video(
            "v3", "출소자의 사회 복귀 이야기", "뉴스채널", description="재범률 통계"
        )
        assert score_candidate(unrelated, program_name="전과자", person_names=["이창섭"]) < 0

    def test_fan_edits_and_reactions_are_rejected(self):
        for title in ("전과자 이창섭 리액션 모음", "전과자 팬캠 모음", "전과자 재업로드"):
            assert (
                score_candidate(video("v", title, "전과자 공식 채널"), program_name="전과자")
                < 0
            ), title

    def test_shorts_score_below_a_full_episode(self):
        full = video("v4", "전과자 EP.13 실습 현장", "전과자 공식 채널")
        short = video("v5", "전과자 EP.13 실습 현장", "전과자 공식 채널", duration="PT45S")
        assert score_candidate(short, program_name="전과자") < score_candidate(
            full, program_name="전과자"
        )

    def test_without_a_program_name_nothing_can_be_judged(self):
        assert score_candidate(video("v6", "아무 영상"), program_name="") < 0


class TestDurationSeconds:
    @pytest.mark.parametrize(
        "value,expected",
        [("PT45S", 45), ("PT12M30S", 750), ("PT1H2M3S", 3723), ("", 0), ("주말", 0)],
    )
    def test_iso_durations(self, value, expected):
        assert duration_seconds(value) == expected


def scored_search_payload(*videos):
    """search.list는 id가 dict, videos.list는 문자열이다 — 실제 응답 모양을 그대로 쓴다."""
    return {
        "items": [
            {"id": {"videoId": item["id"]}, "snippet": item["snippet"]} for item in videos
        ]
    }


@pytest.mark.asyncio
@respx.mock
async def test_youtube_details_keep_items_without_a_video_id_in_place(youtube):
    missing_id = {"id": {}, "snippet": {"title": "식별자 없는 검색 결과"}}
    original = video("official", "전과자 EP.12", "전과자 공식 채널")
    search_item = {"id": {"videoId": "official"}, "snippet": original["snippet"]}
    respx.get(YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"items": [original]})
    )

    detailed = await youtube._with_details([missing_id, search_item])

    assert detailed == [missing_id, original]


@pytest.mark.asyncio
@respx.mock
async def test_a_fan_edit_ranked_first_loses_to_the_official_episode(youtube):
    """이 테스트가 채점을 옮겨 심은 이유 그 자체다 — 관련도 1위가 팬캠일 수 있다."""
    fan = video("fan", "전과자 이창섭 팬캠 모음", "팬계정", duration="PT3M00S")
    official = video(
        "official", "전과자 EP.12 이창섭 항공운항과 실습", "전과자 공식 채널"
    )
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=scored_search_payload(fan, official))
    )
    respx.get(YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"items": [fan, official]})
    )
    respx.get("https://i.ytimg.com/vi/official/maxresdefault.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1280, 720))
    )

    photos = await youtube.find_photos(
        "전과자 이창섭", program_name="전과자", person_names=["이창섭"]
    )

    assert [p.video_id for p in photos] == ["official"]


@pytest.mark.asyncio
@respx.mock
async def test_an_official_thumbnail_is_marked_as_such(youtube):
    """이 표시가 없으면 뒤 단계가 공식 썸네일을 알아보지 못해 제목 박스를 덮는다."""
    official = video("v1", "전과자 EP.1 이창섭", "전과자 공식 채널")
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=scored_search_payload(official))
    )
    respx.get(YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"items": [official]})
    )
    respx.get("https://i.ytimg.com/vi/v1/maxresdefault.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1280, 720))
    )

    photo = (
        await youtube.find_photos("전과자", program_name="전과자", person_names=["이창섭"])
    )[0]

    assert photo.source_type == "YOUTUBE_THUMBNAIL"
    assert photo.channel_title == "전과자 공식 채널"
    assert photo.video_id == "v1"
    assert photo.source_url == "https://www.youtube.com/watch?v=v1"


@pytest.mark.asyncio
@respx.mock
async def test_nothing_clears_the_bar_means_no_photo(youtube):
    """문턱을 넘는 후보가 없으면 빈 목록 — 상위 사다리가 네이버·생성으로 내려간다."""
    unrelated = video("v9", "출소자 재범률 다큐", "뉴스채널")
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=scored_search_payload(unrelated))
    )
    respx.get(YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"items": [unrelated]})
    )

    assert await youtube.find_photos("전과자", program_name="전과자") == []


@pytest.mark.asyncio
@respx.mock
async def test_a_generic_card_keeps_the_old_relevance_order(youtube):
    """정식 명칭을 모르면 채점하지 않는다 — 제외 목록만 걸면 멀쩡한 영상이 떨어진다."""
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=youtube_payload("vid1"))
    )
    respx.get("https://i.ytimg.com/vi/vid1/maxresdefault.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1280, 720))
    )

    photos = await youtube.find_photos("게임 공략")

    # videos.list는 부르지 않는다(채점이 없으므로 재생 시간이 필요 없다).
    assert [p.video_id for p in photos] == ["vid1"]
    assert not any(call.request.url.host == "www.googleapis.com" and "videos" in str(call.request.url) for call in respx.calls)

    def test_a_person_name_in_the_title_anchors_without_the_formal_name(self):
        """정식 명칭이 길어 제목에 통째로 안 담기는 콘텐츠(2026-08-10 실측 — '마블
        시네마틱 유니버스(MCU) 스파이더맨 실사 영화 시리즈'로 후보 240개 전멸) —
        확인된 인물·짧은 이름이 제목에 있으면 후보다."""
        clip = video("v7", "스파이더맨 명장면 모음", "영화클립채널")
        assert (
            score_candidate(
                clip,
                program_name="마블 시네마틱 유니버스(MCU) 스파이더맨 실사 영화 시리즈",
                person_names=["톰 홀랜드", "스파이더맨"],
            )
            >= MIN_RELEVANCE_SCORE
        )
