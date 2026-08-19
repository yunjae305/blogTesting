"""출처 이름을 CDN 호스트가 아니라 실제 사이트·언론사로 적는다(2026-08-11 후속 지시).

저장된 글에서 실측한 캡션이 전부 파일 서버 이름이었다 — `출처: imgnews.naver.net`,
`출처: shop-phinf.pstatic.net`, `출처: i2.ruliweb.com`, `출처: cdn.instiz.net`.
여기서 못박는 계약은 세 가지다.

① 주소가 사이트를 말해 주면 사이트 이름을 적는다(CDN 하위 도메인·프록시를 걷어낸다).
② 원문 페이지는 **열어서 확인한 경우에만** 적는다 — 되만든 주소를 확인 없이 달지 않는다.
③ 언론사는 호스트로 추측하지 않는다. 그 기사 페이지가 스스로 밝힐 때만 적는다.
"""

import httpx
import pytest
import respx

from app.llm import image_origin
from app.llm.image_origin import (
    display_source_name,
    naver_news_article,
    naver_news_origin,
    registrable_domain,
    site_name_of,
    unwrap_proxy,
)

ARTICLE = "https://n.news.naver.com/article/213/0001288948"
NEWS_IMAGE = "http://imgnews.naver.net/image/213/2024/03/12/0001288948_001_2024.jpg"


@pytest.fixture(autouse=True)
def _clear_press_cache():
    """언론사명 캐시는 프로세스 전역이라 테스트끼리 새지 않게 비운다."""
    image_origin._press_names.clear()
    yield
    image_origin._press_names.clear()


class TestSiteName:
    """파일 서버 하위 도메인을 걷어 사이트만 남긴다."""

    @pytest.mark.parametrize(
        "host, expected",
        [
            ("i2.ruliweb.com", "ruliweb.com"),
            ("cdn.instiz.net", "instiz.net"),
            ("cdn.ppomppu.co.kr", "ppomppu.co.kr"),
            ("img.extmovie.com", "extmovie.com"),
            ("example.com", "example.com"),
            ("news.example.co.kr", "example.co.kr"),
        ],
    )
    def test_등록_도메인만_남는다(self, host, expected):
        assert registrable_domain(host) == expected

    def test_같은_사이트의_다른_파일_서버는_같은_이름이_된다(self):
        assert site_name_of("https://i2.ruliweb.com/a.webp") == "ruliweb.com"
        assert site_name_of("https://i3.ruliweb.com/b.webp") == "ruliweb.com"

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://imgnews.pstatic.net/image/001/a.jpg", "네이버 뉴스"),
            ("https://shop-phinf.pstatic.net/x.jpg", "네이버 쇼핑"),
            ("https://shop1.phinf.naver.net/x.jpg", "네이버 쇼핑"),
            ("https://postfiles.pstatic.net/x.png", "네이버 블로그"),
            ("https://post-phinf.pstatic.net/x.png", "네이버 포스트"),
            ("https://i.ytimg.com/vi/abc/maxresdefault.jpg", "YouTube"),
            ("https://blog.kakaocdn.net/dn/x.jpg", "티스토리"),
            ("https://pbs.twimg.com/media/x.jpg", "X(트위터)"),
        ],
    )
    def test_CDN_전용_도메인은_서비스_이름으로_적는다(self, url, expected):
        """등록 도메인을 남겨도 pstatic.net은 사이트 이름이 아니다."""
        assert site_name_of(url) == expected

    def test_주소가_아니면_이름이_없다(self):
        assert site_name_of("user-upload://reference-1") == ""
        assert site_name_of("data:image/png;base64,AA") == ""

    def test_원문_페이지가_이미지_CDN보다_먼저다(self):
        assert (
            display_source_name(
                page_url="https://www.yna.co.kr/view/AKR1",
                image_url="https://img.cdn.example/a.jpg",
            )
            == "yna.co.kr"
        )


class TestProxyUnwrap:
    """프록시 주소는 원본을 질의 문자열에 담아 둔다."""

    def test_네이버_프록시에서_원본_주소를_꺼낸다(self):
        assert unwrap_proxy(
            "https://search.pstatic.net/common/?src=http%3A%2F%2Fimg.ruliweb.com%2Fa.jpg"
        ) == "http://img.ruliweb.com/a.jpg"

    def test_다음_썸네일에서_원본_주소를_꺼낸다(self):
        assert unwrap_proxy(
            "https://img1.daumcdn.net/thumb/R658x0.q70/?fname=https%3A%2F%2Ft1.daumcdn.net%2Fb.jpg"
        ) == "https://t1.daumcdn.net/b.jpg"

    def test_프록시가_아니면_그대로_둔다(self):
        assert unwrap_proxy("https://i2.ruliweb.com/a.webp") == "https://i2.ruliweb.com/a.webp"

    def test_프록시를_푼_뒤의_출처가_원본_사이트다(self):
        """풀지 않으면 출처가 '네이버'가 된다 — 실제 원본은 감싸인 쪽이다."""
        assert (
            site_name_of(
                "https://search.pstatic.net/common/?src=https%3A%2F%2Fi2.ruliweb.com%2Fa.webp"
            )
            == "ruliweb.com"
        )


class TestNaverNewsArticle:
    """네이버 뉴스 이미지 주소는 언론사 코드와 기사 번호를 담고 있다."""

    def test_이미지_주소에서_기사_페이지를_되만든다(self):
        # 실측 표본(저장된 글): 되만든 주소가 실제로 200이었다.
        assert naver_news_article(
            "http://imgnews.naver.net/image/213/2024/03/12/0001288948_001_20240312115901544.jpg"
        ) == ARTICLE

    def test_새_호스트와_원본_경로_변형도_같이_읽는다(self):
        assert naver_news_article(
            "https://imgnews.pstatic.net/image/origin/445/2026/07/13/0000438544_001.jpg"
        ) == "https://n.news.naver.com/article/445/0000438544"

    def test_뉴스가_아닌_주소는_기사로_보지_않는다(self):
        assert naver_news_article("https://shop-phinf.pstatic.net/a/b/c.jpg") is None
        assert naver_news_article("https://imgnews.naver.net/photo/no-pattern.jpg") is None


class TestNaverNewsOrigin:
    """되만든 기사 주소는 열어서 확인한 경우에만 원문이 된다."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_기사가_열리면_주소와_언론사명을_모두_얻는다(self):
        respx.get(ARTICLE).mock(
            return_value=httpx.Response(
                200, text='<meta property="og:article:author" content="연합뉴스 | 네이버">'
            )
        )
        assert await naver_news_origin(NEWS_IMAGE) == (ARTICLE, "연합뉴스")

    @pytest.mark.asyncio
    @respx.mock
    async def test_언론사명이_없으면_주소만_얻는다(self):
        """연예·스포츠 기사는 본문이 JS로 그려져 언론사명이 HTML에 없다(실측)."""
        respx.get(ARTICLE).mock(
            return_value=httpx.Response(200, text="<html><body>스크립트로 그린다</body></html>")
        )
        assert await naver_news_origin(NEWS_IMAGE) == (ARTICLE, "")

    @pytest.mark.asyncio
    @respx.mock
    async def test_열리지_않는_주소는_버린다(self):
        """되만든 주소를 확인 없이 적으면 없는 페이지를 출처로 다는 셈이다."""
        respx.get(ARTICLE).mock(return_value=httpx.Response(404))
        assert await naver_news_origin(NEWS_IMAGE) == (None, "")

    @pytest.mark.asyncio
    @respx.mock
    async def test_확인_요청이_실패해도_사진을_버리지_않는다(self):
        respx.get(ARTICLE).mock(side_effect=httpx.ConnectError("연결 실패"))
        assert await naver_news_origin(NEWS_IMAGE) == (None, "")

    @pytest.mark.asyncio
    @respx.mock
    async def test_같은_언론사는_이름을_다시_읽지_않는다(self):
        """기사 존재 확인은 기사마다 하되, 언론사명 파싱은 코드당 한 번이면 된다."""
        respx.get(ARTICLE).mock(
            return_value=httpx.Response(
                200, text='<meta property="og:article:author" content="연합뉴스 | 네이버">'
            )
        )
        await naver_news_origin(NEWS_IMAGE)
        assert image_origin._press_names["213"] == "연합뉴스"

    @pytest.mark.asyncio
    @respx.mock
    async def test_뉴스가_아닌_사진은_기사를_확인하지_않는다(self):
        """respx는 등록되지 않은 호출에서 실패한다 — 호출이 없다는 것이 이 테스트다."""
        assert await naver_news_origin("https://i2.ruliweb.com/a.webp") == (None, "")
