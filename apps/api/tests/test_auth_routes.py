"""Authentication HTTP contract tests."""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.auth.repository import InMemoryUserRepository
from app.modules.auth.service import AuthService


CREDENTIALS = {
    "email": "writer@blog-it.test",
    "password": "password123",
    "nickname": "라이터",
}


def _app_with_auth_service():
    app = create_app()
    app.state.services = SimpleNamespace(
        auth_service=AuthService(repository=InMemoryUserRepository())
    )
    return app


async def test_auth_me_returns_the_bare_public_user_for_a_valid_login_session():
    app = _app_with_auth_service()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        signup = await client.post("/auth/signup", json=CREDENTIALS)
        assert signup.status_code == 201
        assert signup.headers["cache-control"] == "no-store"

        login = await client.post(
            "/auth/login",
            json={"email": CREDENTIALS["email"], "password": CREDENTIALS["password"]},
        )
        assert login.status_code == 200
        assert login.headers["cache-control"] == "no-store"

        session = login.json()
        response = await client.get(
            "/auth/me",
            headers={"authorization": f"Bearer {session['accessToken']}"},
        )

    assert response.status_code == 200
    assert response.json() == session["user"]
    assert response.json() == {
        "userId": session["user"]["userId"],
        "email": CREDENTIALS["email"],
        "nickname": CREDENTIALS["nickname"],
        "createdAt": session["user"]["createdAt"],
        "updatedAt": session["user"]["updatedAt"],
    }
    assert "data" not in response.json()
    assert "accessToken" not in response.json()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("authorization", [None, "Bearer not-a-valid-token"])
async def test_auth_me_rejects_a_missing_or_invalid_bearer_token(authorization):
    app = _app_with_auth_service()
    transport = ASGITransport(app=app)
    headers = {"authorization": authorization} if authorization else {}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/auth/me", headers=headers)

    assert response.status_code == 401
    assert response.json()["success"] is False
    assert response.json()["errorCode"] == "UNAUTHORIZED"
    assert "data" not in response.json()
    assert response.headers["cache-control"] == "no-store"
