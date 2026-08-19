"""이미지 출처 메타데이터(2026-08-11).

여기서 지키는 것은 두 가지다.

1. **검색 서비스는 출처가 아니다.** 네이버 이미지 검색으로 찾았다고 네이버가 출처가 되지
   않는다. 반대로 알 수 없는 원문 페이지를 이미지 주소에서 지어내지도 않는다.
2. **확인하지 못한 것을 '사용 가능'이라고 하지 않는다.** 라이선스 표기가 없으면 언제나
   unknown이다.

그리고 옛 문서 호환: imageSource가 없는 저장 문서가 그대로 읽혀야 한다.
"""

import io

import httpx
import pytest
import respx
from PIL import Image

from app.llm.image_origin import (
    display_source_name,
    generated_image_source,
    host_of,
    is_public_url,
    usage_status_for,
    web_photo_image_source,
    youtube_license,
)
from app.llm.live_adapters import _captioned_with_source
from app.llm.photo_search import (
    NAVER_IMAGE_SEARCH_URL,
    YOUTUBE_SEARCH_URL,
    YOUTUBE_VIDEOS_URL,
    NaverPhotoSearch,
    YouTubeThumbnailSearch,
)
from app.shared import (
    WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL,
    GeneratedPostImage,
    ImageSourceInfo,
    WebPhoto,
)

SEARCH_URL = NAVER_IMAGE_SEARCH_URL  # API HUB 경로에는 확장자가 없다(2026-08-11)


def jpeg_bytes(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 130, 140)).save(buffer, format="JPEG")
    return buffer.getvalue()


class TestDisplaySourceName:
    def test_the_page_host_wins_over_the_image_cdn(self):
        """게시자는 그림 파일을 날라 준 CDN이 아니라 그 그림이 실린 사이트다."""
        assert (
            display_source_name(
                page_url="https://www.yna.co.kr/view/AKR1",
                image_url="https://img.cdn.example/a.jpg",
            )
            == "yna.co.kr"
        )

    def test_a_known_site_gets_its_name(self):
        assert display_source_name(image_url="https://i.ytimg.com/vi/x/max.jpg") == "YouTube"

    def test_a_cdn_host_is_named_by_its_service_not_by_a_guessed_press(self):
        """CDN 호스트는 출처 이름이 될 수 없다(2026-08-11 후속 지시 — 실측된 캡션이
        전부 `출처: imgnews.naver.net`류였다). 그렇다고 호스트를 언론사 이름으로 바꿔
        적지도 않는다: imgnews.pstatic.net은 네이버 뉴스에 들어온 **모든** 언론사의
        사진을 나르므로 호스트만 보고 언론사를 단정하면 절반이 틀린다.

        그래서 이름은 그 호스트가 속한 **서비스**이고, 언론사는 그 이미지가 가리키는
        기사를 실제로 열어 본 경로에서만 적는다(naver_news_origin).
        """
        assert (
            display_source_name(image_url="https://imgnews.pstatic.net/image/001/a.jpg")
            == "네이버 뉴스"
        )

    def test_a_file_server_subdomain_is_reduced_to_the_site(self):
        """i2.ruliweb.com은 사이트 이름이 아니라 파일 서버다."""
        assert display_source_name(image_url="https://i2.ruliweb.com/a.webp") == "ruliweb.com"

    def test_nothing_usable_means_no_name(self):
        assert display_source_name(image_url="data:image/jpeg;base64,AAAA") == ""
        assert display_source_name(page_url=None, image_url=None) == ""

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.example.com/a", "example.com"),
            ("http://Example.COM/b", "example.com"),
            ("user-upload://ref-1", ""),
            ("", ""),
        ],
    )
    def test_host_normalisation(self, url, expected):
        assert display_source_name(image_url=url) == expected


class TestHostOf:
    def test_only_http_urls_have_hosts(self):
        assert host_of("https://a.example/x") == "a.example"
        assert host_of("user-upload://ref-1") == ""
        assert host_of("data:image/png;base64,AA") == ""

    def test_public_url_check(self):
        assert is_public_url("https://a.example/x")
        assert not is_public_url("user-upload://ref-1")
        assert not is_public_url(None)


class TestUsageStatus:
    def test_no_license_is_unknown_not_allowed(self):
        """CASE 4. 라이선스가 없다는 것은 '자유롭게 써도 된다'가 아니라 '모른다'는 뜻이다."""
        assert usage_status_for(None) == "unknown"
        assert usage_status_for("") == "unknown"
        assert usage_status_for("   ") == "unknown"

    def test_an_open_licence_is_allowed(self):
        assert usage_status_for("CC BY 4.0") == "allowed"
        assert usage_status_for("Public Domain") == "allowed"

    def test_a_reserved_notice_is_restricted(self):
        assert usage_status_for("무단전재 및 재배포 금지") == "restricted"
        assert usage_status_for("All Rights Reserved") == "restricted"

    def test_an_unrecognised_notice_stays_unknown(self):
        assert usage_status_for("사내 자료") == "unknown"


class TestYoutubeLicense:
    def test_creative_commons_is_confirmed_allowed(self):
        name, url, status = youtube_license("creativeCommon")
        assert status == "allowed"
        assert "CC BY" in name
        assert url.startswith("https://creativecommons.org/")

    def test_the_standard_licence_is_restricted(self):
        name, url, status = youtube_license("youtube")
        assert status == "restricted"
        assert name == "표준 YouTube 라이선스"
        assert url

    def test_no_value_means_unknown(self):
        """세부정보를 안 부른 경로. 모르는 것을 둘 중 하나로 정하지 않는다."""
        assert youtube_license(None) == (None, None, "unknown")
        assert youtube_license("") == (None, None, "unknown")


class TestWebPhotoImageSource:
    def test_a_naver_search_photo_keeps_the_page_empty(self):
        """네이버 이미지 검색은 원문 페이지를 주지 않는다 — 없는 것을 만들지 않는다."""
        photo = WebPhoto(
            data_url="data:image/jpeg;base64,AA",
            source_url="https://imgnews.pstatic.net/image/001/a.jpg",
            source_host="imgnews.pstatic.net",
            source_name="imgnews.pstatic.net",
        )

        info = web_photo_image_source(photo)

        assert info.source_type == "external"
        assert info.source_name == "imgnews.pstatic.net"
        assert info.source_page_url is None
        assert info.original_image_url == "https://imgnews.pstatic.net/image/001/a.jpg"
        assert info.license is None
        assert info.usage_status == "unknown"

    def test_a_youtube_thumbnail_points_at_the_video_page(self):
        """CASE 3. 원문(영상 페이지)이 실제로 있는 경로에서는 그것이 출처다."""
        photo = WebPhoto(
            data_url="data:image/jpeg;base64,AA",
            source_url="https://www.youtube.com/watch?v=abc",
            source_host="youtube.com",
            source_type=WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL,
            channel_title="연합뉴스TV",
            video_id="abc",
            source_name="연합뉴스TV",
            source_page_url="https://www.youtube.com/watch?v=abc",
            license="표준 YouTube 라이선스",
            license_url="https://www.youtube.com/static?template=terms",
            usage_status="restricted",
        )

        info = web_photo_image_source(
            photo, original_image_url="https://i.ytimg.com/vi/abc/maxresdefault.jpg"
        )

        assert info.source_name == "연합뉴스TV"
        assert info.source_page_url == "https://www.youtube.com/watch?v=abc"
        # 원본 '이미지' 주소는 영상 주소가 아니라 썸네일 파일 주소여야 한다.
        assert info.original_image_url == "https://i.ytimg.com/vi/abc/maxresdefault.jpg"
        assert info.usage_status == "restricted"

    def test_an_internal_url_is_not_an_original_image(self):
        """사용자 업로드 경로의 user-upload:// 는 밖에서 열리는 주소가 아니다."""
        photo = WebPhoto(
            data_url="data:image/jpeg;base64,AA",
            source_url="user-upload://ref-1",
            source_host="user-reference",
        )

        info = web_photo_image_source(photo)

        assert info.original_image_url is None
        assert info.source_page_url is None
        assert info.source_name == ""

    def test_a_generated_image_carries_no_website(self):
        """CASE 5. AI가 그린 이미지에 외부 웹사이트 출처를 붙이지 않는다."""
        info = generated_image_source()

        assert info.source_type == "generated"
        assert info.source_name == ""
        assert info.source_page_url is None
        assert info.original_image_url is None


class TestCaption:
    def test_the_confirmed_site_name_is_used(self):
        photo = WebPhoto(
            data_url="d",
            source_url="https://a.example/x.jpg",
            source_host="a.example",
            source_name="example.com",
        )
        assert _captioned_with_source(None, photo) == "출처: example.com"

    def test_it_falls_back_to_the_host(self):
        """옛 경로·이름을 확인하지 못한 사진은 예전 그대로 호스트를 적는다."""
        photo = WebPhoto(
            data_url="d", source_url="https://a.example/x.jpg", source_host="a.example"
        )
        assert _captioned_with_source(None, photo) == "출처: a.example"

    def test_a_generated_image_gets_no_source_line(self):
        assert _captioned_with_source("설명입니다", None) == "설명입니다"


class TestBackwardCompatibility:
    def test_an_old_document_without_image_source_still_loads(self):
        """CASE 6. 기존 DB 문서에는 imageSource가 없다 — 강제 migration 없이 읽혀야 한다."""
        stored = {
            "dataUrl": "data:image/jpeg;base64,AA",
            "altText": "옛 이미지",
            "prompt": "p",
            "provider": "openai",
            "model": "gpt-image-2",
            "generatedAt": "2026-01-01T00:00:00.000Z",
            "mimeType": "image/jpeg",
        }

        image = GeneratedPostImage.model_validate(stored)

        assert image.image_source is None
        # 다시 내보낼 때도 없는 필드를 만들어 넣지 않는다.
        assert "imageSource" not in image.to_wire()

    def test_a_minimal_old_image_still_loads(self):
        """url 하나만 있던 아주 오래된 모양도 필수 필드만 채우면 읽힌다."""
        image = GeneratedPostImage.model_validate(
            {
                "dataUrl": "data:image/jpeg;base64,AA",
                "altText": "",
                "prompt": "",
                "provider": "",
                "model": "",
                "generatedAt": "",
                "mimeType": "image/jpeg",
            }
        )
        assert image.image_source is None

    def test_a_new_document_round_trips(self):
        image = GeneratedPostImage(
            data_url="d",
            alt_text="a",
            prompt="p",
            provider="web-photo",
            model="a.example",
            generated_at="t",
            mime_type="image/jpeg",
            source="web",
            image_source=ImageSourceInfo(
                source_type="external",
                source_name="a.example",
                usage_status="unknown",
            ),
        )

        wire = image.to_wire()

        assert wire["imageSource"]["sourceType"] == "external"
        assert wire["imageSource"]["usageStatus"] == "unknown"
        restored = GeneratedPostImage.model_validate(wire)
        assert restored.image_source == image.image_source

    def test_an_old_web_photo_without_source_fields_still_loads(self):
        """WebPhoto도 마찬가지다 — 새 필드는 전부 기본값이 있다."""
        photo = WebPhoto.model_validate(
            {
                "dataUrl": "d",
                "sourceUrl": "https://a.example/x.jpg",
                "sourceHost": "a.example",
            }
        )
        assert photo.source_name == ""
        assert photo.source_page_url is None
        assert photo.usage_status == "unknown"


@pytest.mark.asyncio
@respx.mock
async def test_a_searched_photo_records_the_site_not_the_search_service():
    """검색으로 찾았어도 출처는 네이버가 아니라 그 이미지가 실려 있던 사이트다."""
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"title": "기사 사진", "link": "https://news.example/a.jpg"}]},
        )
    )
    respx.get("https://news.example/a.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1200, 800))
    )

    photos = await NaverPhotoSearch("id", "secret").find_photos("소재")

    assert photos[0].source_name == "news.example"
    # 원문 페이지는 검색 응답에 없다 — 이미지 주소에서 만들어내지 않는다.
    assert photos[0].source_page_url is None
    assert photos[0].usage_status == "unknown"


@pytest.mark.asyncio
@respx.mock
async def test_a_redirected_image_records_where_it_actually_came_from():
    """리다이렉트를 따라갔으면 실제로 그림을 받은 주소가 원본이다 — 중간 주소가 아니다."""
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"items": [{"title": "사진", "link": "https://short.example/r"}]}
        )
    )
    respx.get("https://short.example/r").mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.example/real.jpg"})
    )
    respx.get("https://cdn.example/real.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1200, 800))
    )

    photos = await NaverPhotoSearch("id", "secret").find_photos("소재")

    assert photos[0].source_url == "https://cdn.example/real.jpg"
    assert photos[0].source_name == "cdn.example"


@pytest.mark.asyncio
@respx.mock
async def test_a_youtube_thumbnail_records_the_channel_and_declared_licence():
    """유튜브는 원문 페이지·게시자·라이선스를 모두 사실로 말할 수 있는 유일한 경로다."""
    item = {
        "id": "vid1",
        "snippet": {"title": "전과자 EP.1", "channelTitle": "전과자 공식 채널"},
        "contentDetails": {"duration": "PT12M30S"},
        "status": {"license": "creativeCommon"},
    }
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": {"videoId": "vid1"}, "snippet": item["snippet"]}]}
        )
    )
    respx.get(YOUTUBE_VIDEOS_URL).mock(return_value=httpx.Response(200, json={"items": [item]}))
    respx.get("https://i.ytimg.com/vi/vid1/maxresdefault.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1280, 720))
    )

    photos = await YouTubeThumbnailSearch("key").find_photos(
        "전과자", program_name="전과자"
    )

    photo = photos[0]
    assert photo.source_name == "전과자 공식 채널"
    assert photo.source_page_url == "https://www.youtube.com/watch?v=vid1"
    assert photo.usage_status == "allowed"
    assert photo.license_url
    # status를 함께 요청하지 않으면 라이선스를 알 길이 없다 — part 문자열이 규격이다.
    details = next(
        call for call in respx.calls if str(call.request.url).startswith(YOUTUBE_VIDEOS_URL)
    )
    assert "status" in details.request.url.params["part"]


@pytest.mark.asyncio
@respx.mock
async def test_a_youtube_thumbnail_without_details_stays_unknown():
    """세부정보를 부르지 않는 경로(채점 없음)에서는 라이선스를 모른다 — 그렇게 적는다."""
    respx.get(YOUTUBE_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": {"videoId": "vid1"},
                        "snippet": {"title": "공략", "channelTitle": "게임채널"},
                    }
                ]
            },
        )
    )
    respx.get("https://i.ytimg.com/vi/vid1/maxresdefault.jpg").mock(
        return_value=httpx.Response(200, content=jpeg_bytes(1280, 720))
    )

    photos = await YouTubeThumbnailSearch("key").find_photos("게임 공략")

    assert photos[0].usage_status == "unknown"
    assert photos[0].license is None
    # 원문 페이지는 여전히 안다 — 라이선스를 모르는 것과 별개다.
    assert photos[0].source_page_url == "https://www.youtube.com/watch?v=vid1"
