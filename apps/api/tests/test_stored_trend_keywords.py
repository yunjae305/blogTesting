"""DB에 쌓인 트렌드 키워드를 글 없이 읽는 통로(GET /trends/keywords).

왜 따로 있나. `/posts/{id}/trends/recommend`는 **글 하나**에 맞춰 키워드를 모으고
관련도를 채점하므로 글이 먼저 있어야 한다. 그래서 브랜드 글쓰기 화면은 목록을 보려고
빈 글을 먼저 만들었고, 그 빈 글이 브랜드 자료 검증에 걸리자 키워드 목록까지 함께
죽었다(2026-08-06). "지금 뭐가 쌓여 있나"를 보는 데 글이 필요할 이유가 없다.
"""

import time
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.auth.repository import InMemoryUserRepository
from app.modules.auth.service import AuthService
from app.modules.blog_task.repository import InMemoryBlogTaskRepository
from app.modules.trend.keyword_store import (
    InMemoryStoredTrendKeywordRepository,
    interleave_by_source,
)
from app.modules.trend.service import TrendService

NOW = time.time()


def _document(keyword: str, source: str, score: float, age_days: float = 0.0) -> dict:
    return {
        "_id": f"key_{keyword}",
        "keyword": keyword,
        "source": source,
        "score": score,
        "at": NOW - age_days * 86400,
        "seq": 1,
    }


def _store(documents: list[dict]) -> InMemoryStoredTrendKeywordRepository:
    return InMemoryStoredTrendKeywordRepository(documents)


def _service(documents: list[dict]) -> TrendService:
    return TrendService(
        repository=InMemoryBlogTaskRepository(),
        trend_provider=None,
        topic_generator=None,
        stored_keywords=_store(documents),
    )


class TestReadingWhatIsStored:
    async def test_it_returns_stored_keywords_without_any_post(self):
        """글도 브랜드도 없이 읽힌다 — 이게 이 통로의 존재 이유다."""
        service = _service([_document("폭염", "NAVER_DATALAB", 100.0)])

        keywords = await service.list_stored_keywords(12)

        assert [k.keyword for k in keywords] == ["폭염"]
        assert keywords[0].source.value == "NAVER_DATALAB"
        # 수집 시각은 epoch로 저장돼 있다. 화면은 ISO 문자열을 읽는다.
        assert keywords[0].collected_at.endswith("Z")

    async def test_one_source_does_not_fill_the_panel(self):
        """실측으로 소스마다 쌓인 양이 크게 다르다(네이버 200 · 구글 129 · 유튜브 32).
        점수순으로만 자르면 많이 쌓인 소스가 목록을 독차지한다."""
        documents = [_document(f"네이버{i}", "NAVER_DATALAB", 100.0) for i in range(20)]
        documents += [_document("구글", "GOOGLE_TRENDS", 50.0)]
        documents += [_document("유튜브", "YOUTUBE", 40.0)]

        keywords = await _service(documents).list_stored_keywords(6)

        sources = {k.source.value for k in keywords}
        assert sources == {"NAVER_DATALAB", "GOOGLE_TRENDS", "YOUTUBE"}

    async def test_rank_is_renumbered_to_the_final_position(self):
        """순위는 저장하지 않는다. 번갈아 뽑은 뒤의 자리가 순위다."""
        documents = [
            _document("가", "GOOGLE_TRENDS", 50.0),
            _document("나", "NAVER_DATALAB", 90.0),
        ]

        keywords = await _service(documents).list_stored_keywords(12)

        assert [k.rank for k in keywords] == [1, 2]

    async def test_the_limit_is_capped(self):
        """상한이 없으면 쌓인 것이 통째로 실려 나간다(실측 361건)."""
        from app.modules.trend.keyword_store import MAX_LIMIT

        documents = [_document(f"kw{i}", "NAVER_DATALAB", 50.0) for i in range(MAX_LIMIT + 30)]

        keywords = await _service(documents).list_stored_keywords(9999)

        assert len(keywords) == MAX_LIMIT

    async def test_a_broken_document_is_skipped_not_fatal(self):
        """옛 형식·깨진 문서 하나가 목록 전체를 죽이면 안 된다."""
        documents = [
            {"_id": "key1", "keyword": "정상", "source": "YOUTUBE", "score": 50.0, "at": NOW},
            {"_id": "key2", "keyword": "출처없음", "source": "MYSPACE", "score": 50.0, "at": NOW},
            {"_id": "key3", "source": "YOUTUBE", "score": 50.0, "at": NOW},
        ]

        keywords = await _service(documents).list_stored_keywords(12)

        assert [k.keyword for k in keywords] == ["정상"]

    async def test_shuffle_still_mixes_the_sources(self):
        """'다른 키워드 보기'가 한 소스로 쏠리면 안 된다 — 섞되 번갈아 뽑는 성질은 지킨다."""
        documents = [_document(f"네이버{i}", "NAVER_DATALAB", 100.0 - i) for i in range(30)]
        documents += [_document(f"구글{i}", "GOOGLE_TRENDS", 100.0 - i) for i in range(30)]

        keywords = await _service(documents).list_stored_keywords(10, shuffle=True)

        sources = {k.source.value for k in keywords}
        assert sources == {"NAVER_DATALAB", "GOOGLE_TRENDS"}
        assert len(keywords) == 10

    def test_interleave_stops_when_the_buckets_run_dry(self):
        """요청한 수보다 쌓인 것이 적어도 무한 루프에 빠지지 않는다."""
        from app.shared import TrendKeyword, TrendSource

        keywords = [
            TrendKeyword(
                trend_keyword_id="key1",
                keyword="하나",
                source=TrendSource.YOUTUBE,
                rank=0,
                score=1.0,
                collected_at="2026-08-06T00:00:00.000Z",
            )
        ]

        assert len(interleave_by_source(keywords, 50)) == 1


class TestTheRoute:
    async def _app(self, documents: list[dict]):
        auth_service = AuthService(InMemoryUserRepository())
        signed_up = await auth_service.sign_up(
            {"email": "trend@example.com", "password": "password123", "nickname": "작성자"}
        )
        app = create_app()
        app.state.services = SimpleNamespace(
            auth_service=auth_service,
            trend_service=_service(documents),
        )
        return app, signed_up.access_token

    async def test_the_route_serves_stored_keywords(self):
        app, token = await self._app(
            [
                _document("폭염", "NAVER_DATALAB", 100.0),
                _document("장례", "GOOGLE_TRENDS", 90.0),
            ]
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/trends/keywords?limit=12", headers={"authorization": f"Bearer {token}"}
            )

        assert response.status_code == 200, response.text
        keywords = response.json()["trendKeywords"]
        assert {k["keyword"] for k in keywords} == {"폭염", "장례"}
        # 화면이 읽는 필드가 그대로 있어야 한다.
        assert keywords[0]["trendKeywordId"]
        assert keywords[0]["source"]

    async def test_it_needs_a_login_but_not_a_post(self):
        app, _token = await self._app([_document("폭염", "NAVER_DATALAB", 100.0)])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/trends/keywords")

        assert response.status_code == 401

    async def test_a_bad_limit_is_a_validation_error_not_a_500(self):
        app, token = await self._app([_document("폭염", "NAVER_DATALAB", 100.0)])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/trends/keywords?limit=열두개", headers={"authorization": f"Bearer {token}"}
            )

        assert response.status_code == 400, response.text
        assert response.json()["errorCode"] == "VALIDATION_FAILED"
