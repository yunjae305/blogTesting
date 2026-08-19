"""검증(M3) 자료에 관련된 **최신 기사**를 보탠다(2026-08-11 사용자 지시).

여기서 지키는 것은 세 가지다.

1. **최신순으로 묻는다.** 관련도순은 몇 년 전 기사를 1위로 준다.
2. **최신이 없으면 지어내지 않는다.** 창 밖의 기사를 쓰되 발행일을 숨기지 않는다.
3. **같은 보도자료를 받아쓴 기사는 한 건만 남긴다.** 자료 자리는 다 쓰면서 새로 알려
   주는 사실은 하나뿐인 묶음이 실제로 상위를 차지했다(실측).
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.llm.naver_news import (
    NAVER_NEWS_SEARCH_URL,
    RECENT_WINDOW_DAYS,
    NaverNewsResearch,
    extract_article_text,
    parse_pub_date,
    title_overlap,
)

NOW = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def rfc(when: datetime) -> str:
    return when.strftime("%a, %d %b %Y %H:%M:%S +0000")


def item(title: str, *, days_ago: float = 0, origin: str = "", naver: str = "") -> dict:
    when = NOW - timedelta(days=days_ago)
    index = abs(hash(title)) % 10_000
    return {
        "title": title,
        "originallink": origin or f"https://press{index}.example/a{index}",
        "link": naver or f"https://n.news.naver.com/article/001/{index:010d}",
        "description": f"{title} 요약",
        "pubDate": rfc(when),
    }


def payload(*items: dict) -> dict:
    return {"items": list(items)}


@pytest.fixture
def research():
    return NaverNewsResearch("client-id", "client-secret")


class TestRecency:
    @pytest.mark.asyncio
    @respx.mock
    async def test_it_asks_for_the_newest_first(self, research):
        route = respx.get(NAVER_NEWS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=payload(item("오늘 기사")))
        )
        respx.get(url__startswith="https://n.news.naver.com/").mock(
            return_value=httpx.Response(200, text="본문 없음")
        )

        await research.collect(["금리"], now=NOW)

        assert route.calls[0].request.url.params["sort"] == "date"

    @pytest.mark.asyncio
    @respx.mock
    async def test_recent_articles_come_before_old_ones(self, research):
        respx.get(NAVER_NEWS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=payload(
                    item("작년 기사", days_ago=400),
                    item("어제 기사", days_ago=1),
                    item("사흘 전 기사", days_ago=3),
                ),
            )
        )
        respx.get(url__startswith="https://n.news.naver.com/").mock(
            return_value=httpx.Response(200, text="본문 없음")
        )

        articles = await research.collect(["금리"], limit=3, now=NOW)

        # 창 안의 기사만, 최신 것부터. 창 밖 기사는 창 안에 하나라도 있으면 쓰지 않는다.
        assert [a.title for a in articles] == ["어제 기사", "사흘 전 기사"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_when_nothing_is_recent_it_says_the_date_instead_of_giving_up(
        self, research
    ):
        """최신이 없다고 자료를 비우지 않는다 — 대신 발행일이 함께 실려 낡음이 드러난다."""
        respx.get(NAVER_NEWS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=payload(item("오래된 기사", days_ago=RECENT_WINDOW_DAYS + 30))
            )
        )
        respx.get(url__startswith="https://n.news.naver.com/").mock(
            return_value=httpx.Response(200, text="본문 없음")
        )

        articles = await research.collect(["금리"], now=NOW)

        assert [a.title for a in articles] == ["오래된 기사"]
        assert articles[0].published_at  # 언제 것인지 반드시 남는다

    def test_an_unreadable_date_is_not_treated_as_now(self):
        assert parse_pub_date("") is None
        assert parse_pub_date("어제") is None
        assert parse_pub_date("Tue, 11 Aug 2026 14:37:00 +0900") is not None


class TestDuplicateStories:
    def test_the_same_press_release_reads_as_one_story(self):
        """실측한 제목 그대로. 띄어쓰기만 다른 같은 발표가 두 자리를 차지했었다."""
        assert (
            title_overlap(
                "카카오뱅크, 전국 17개 신보와 '이자지원 보증서 대출' 협약",
                "카카오뱅크, 이자지원 보증서대출 전국 어디서나 받는다",
            )
            >= 0.45
        )

    def test_different_stories_stay_different(self):
        """같은 소재의 다른 기사끼리는 붙지 않아야 한다(수능 D-100 기사끼리 0.23이었다)."""
        assert (
            title_overlap(
                "[대전교육소식] 오석진 대전교육감, 수능 D-100 현장 방문 외",
                '이충우 여주시장, 수능 D-100 수험생 응원..."건강이 가장 중요"',
            )
            < 0.45
        )

    @pytest.mark.asyncio
    @respx.mock
    async def test_only_one_of_a_wire_copy_cluster_is_kept(self, research):
        respx.get(NAVER_NEWS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=payload(
                    item("카카오뱅크, 전국 17개 신보와 '이자지원 보증서 대출' 협약"),
                    item("카카오뱅크, 이자지원 보증서대출 전국 어디서나 받는다", days_ago=0.01),
                    item("물가 잡으려다 경제 잡을라…2000조 가계부채 딜레마", days_ago=0.02),
                ),
            )
        )
        respx.get(url__startswith="https://n.news.naver.com/").mock(
            return_value=httpx.Response(200, text="본문 없음")
        )

        titles = [a.title for a in await research.collect(["금리"], limit=3, now=NOW)]

        assert len(titles) == 2
        assert titles[0].startswith("카카오뱅크, 전국 17개")
        assert "가계부채" in titles[1]


class TestArticleBody:
    def test_it_reads_the_article_body(self):
        html = (
            '<div id="dic_area" class="go_trans">'
            "<span class='end_photo_org'>사진 설명은 근거가 아니다</span>"
            "첫 문단이다. 여기에 사실이 있다.<br><br>둘째 문단도 충분히 길게 이어진다. " * 3
            + "</div>"
        )
        text = extract_article_text(html)
        assert "첫 문단이다" in text
        assert "사진 설명은 근거가 아니다" not in text

    def test_a_js_rendered_page_yields_nothing_instead_of_menu_text(self):
        """연예·스포츠 기사는 본문이 JS로 그려진다. 페이지 전체 텍스트로 물러나면
        메뉴·연관기사 제목이 '확인된 사실'로 섞인다."""
        assert extract_article_text("<html><body>연관기사 목록</body></html>") == ""
        assert extract_article_text("") == ""

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_failed_body_fetch_keeps_the_article_with_its_summary(self, research):
        """기사 하나를 못 읽었다고 그 기사를 버리지 않는다 — 요약이 근거를 대신한다."""
        respx.get(NAVER_NEWS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=payload(item("본문 못 읽는 기사")))
        )
        respx.get(url__startswith="https://n.news.naver.com/").mock(
            side_effect=httpx.ConnectError("연결 실패")
        )

        articles = await research.collect(["금리"], now=NOW)

        assert len(articles) == 1
        assert articles[0].excerpt == ""
        assert articles[0].snippet


class TestFailure:
    @pytest.mark.asyncio
    @respx.mock
    async def test_a_search_failure_returns_nothing_instead_of_raising(self, research):
        """보강 수집이 검증을 죽이지 않는다."""
        respx.get(NAVER_NEWS_SEARCH_URL).mock(return_value=httpx.Response(401, text="nope"))

        assert await research.collect(["금리"], now=NOW) == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_the_source_url_is_the_publisher_not_the_middleman(self, research):
        """출처는 언론사 원문 주소다(오늘 정한 원칙과 같다). 본문은 네이버 기사에서 읽는다."""
        respx.get(NAVER_NEWS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=payload(
                    item(
                        "기사",
                        origin="https://www.yna.co.kr/view/AKR1",
                        naver="https://n.news.naver.com/article/001/0000000001",
                    )
                ),
            )
        )
        body = respx.get("https://n.news.naver.com/article/001/0000000001").mock(
            return_value=httpx.Response(
                200, text='<div id="dic_area">' + "본문이 충분히 길게 이어진다. " * 10 + "</div>"
            )
        )

        [article] = await research.collect(["연합"], now=NOW)

        assert article.url == "https://www.yna.co.kr/view/AKR1"
        assert article.naver_url == "https://n.news.naver.com/article/001/0000000001"
        assert "본문이 충분히" in article.excerpt
        assert body.called
