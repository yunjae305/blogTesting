"""2단계 인증 코드 창구의 HTTP 계약.

여기서 막는 것: 화면이 부르는 경로가 서버에 없는 것(404가 폴링의 catch에 삼켜져 "팝업이
안 뜬다"로만 보인다), 남의 대기를 훔쳐보는 것, 그리고 코드가 응답에 새는 것.

실사용(2026-08-04)에서 팝업이 안 뜨는 원인을 찾을 때 이 층을 한 번도 테스트하지 않았다는
사실이 드러났다 — 브로커와 화면은 각각 테스트했는데 그 사이를 잇는 라우트가 비어 있었다.
"""

from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.auth.repository import InMemoryUserRepository
from app.modules.auth.service import AuthService
from app.posting.verification import broker

CREDENTIALS = {
    "email": "writer@blog-it.test",
    "password": "password123",
    "nickname": "라이터",
}


def _app():
    app = create_app()
    app.state.services = SimpleNamespace(
        auth_service=AuthService(repository=InMemoryUserRepository())
    )
    return app


async def _signed_in(client: AsyncClient) -> tuple[dict, str]:
    await client.post("/auth/signup", json=CREDENTIALS)
    login = await client.post(
        "/auth/login",
        json={"email": CREDENTIALS["email"], "password": CREDENTIALS["password"]},
    )
    session = login.json()
    return {"authorization": f"Bearer {session['accessToken']}"}, session["user"]["userId"]


async def test_the_polling_route_exists_and_says_nothing_is_pending():
    """화면이 2초마다 부르는 경로. 없으면 404가 조용히 삼켜져 팝업이 영영 안 뜬다."""
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        headers, _ = await _signed_in(client)
        response = await client.get("/posting/verification", headers=headers)

    assert response.status_code == 200
    assert response.json()["pending"] is None


async def test_a_pending_request_is_visible_to_its_owner():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        headers, user_id = await _signed_in(client)
        broker.request(
            user_id=user_id, post_id="post_1", channel="threads", prompt="코드를 넣어 주세요"
        )
        try:
            response = await client.get("/posting/verification", headers=headers)
        finally:
            broker.cancel(user_id)

    pending = response.json()["pending"]
    assert pending["postId"] == "post_1"
    assert pending["channel"] == "threads"
    assert pending["prompt"] == "코드를 넣어 주세요"


async def test_a_submitted_code_is_accepted_and_never_echoed_back():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        headers, user_id = await _signed_in(client)
        broker.request(user_id=user_id, post_id="post_1", channel="threads", prompt="코드")
        try:
            # 문자를 그대로 붙여넣는 경우가 많아 공백·하이픈은 흘려보낸다.
            response = await client.post(
                "/posting/verification", headers=headers, json={"code": "123 456"}
            )
        finally:
            broker.cancel(user_id)

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert "123456" not in response.text


async def test_submitting_without_a_pending_request_is_refused():
    """아무도 안 기다리는데 성공을 돌려주면 화면이 '넘겼다'고 잘못 말한다."""
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        headers, _ = await _signed_in(client)
        response = await client.post(
            "/posting/verification", headers=headers, json={"code": "123456"}
        )

    assert response.status_code == 409


async def test_an_empty_code_is_refused():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        headers, _ = await _signed_in(client)
        response = await client.post("/posting/verification", headers=headers, json={"code": "  "})

    assert response.status_code == 400


async def test_cancelling_ends_the_wait():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        headers, user_id = await _signed_in(client)
        broker.request(user_id=user_id, post_id="post_1", channel="threads", prompt="코드")
        response = await client.delete("/posting/verification", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"cancelled": True}


async def test_the_routes_require_a_login():
    """대기 내용에는 어떤 글을 발행 중인지가 담긴다 — 로그인 없이 보이면 안 된다."""
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get("/posting/verification")

    assert response.status_code == 401
