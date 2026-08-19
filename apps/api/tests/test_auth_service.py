"""service.test.ts, plus the hash-format
compatibility test the original had no reason to need.
"""

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import secrets

import pytest

from app.errors import AuthError
from app.modules.auth.repository import InMemoryUserRepository
from app.modules.auth.service import (
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    AuthService,
    verify_password,
)

CREDENTIALS = {"email": "writer@blog-it.test", "password": "password123", "nickname": "라이터"}


@pytest.fixture
def repository():
    return InMemoryUserRepository()


@pytest.fixture
def service(repository):
    return AuthService(repository=repository)


async def test_issues_a_session_on_signup_and_never_stores_the_raw_password(service, repository):
    session = await service.sign_up(CREDENTIALS)

    assert session.user.email == CREDENTIALS["email"]
    assert session.user.nickname == "라이터"
    assert len(session.access_token) > 0

    stored = await repository.find_by_email(CREDENTIALS["email"])
    assert stored is not None
    assert stored.nickname == "라이터"
    assert CREDENTIALS["password"] not in stored.password_hash
    assert stored.password_hash.startswith(f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$")


async def test_backfill_fills_empty_nickname_but_keeps_existing(service, repository):
    # 닉네임 없이 만들어진 옛 계정을 흉내낸다.
    from app.modules.auth.service import hash_password
    from app.shared import User

    await repository.create(
        User(
            user_id="u_old",
            email="old@blog-it.test",
            nickname="",
            password_hash=hash_password("password123"),
            created_at="1970-01-01T00:00:00.000Z",
            updated_at="1970-01-01T00:00:00.000Z",
        )
    )

    await service.backfill_nickname("old@blog-it.test", "데모 계정")
    filled = await repository.find_by_email("old@blog-it.test")
    assert filled.nickname == "데모 계정"

    # 이미 닉네임이 있으면 덮어쓰지 않는다.
    await service.backfill_nickname("old@blog-it.test", "다른 이름")
    kept = await repository.find_by_email("old@blog-it.test")
    assert kept.nickname == "데모 계정"


async def test_signup_requires_a_nickname(service):
    with pytest.raises(AuthError) as excinfo:
        await service.sign_up({"email": "no-nick@blog-it.test", "password": "password123"})
    assert excinfo.value.code == "VALIDATION_FAILED"


async def test_signup_trims_and_caps_nickname(service):
    session = await service.sign_up(
        {"email": "spacey@blog-it.test", "password": "password123", "nickname": "  닉  "}
    )
    assert session.user.nickname == "닉"

    with pytest.raises(AuthError) as excinfo:
        await service.sign_up(
            {"email": "long@blog-it.test", "password": "password123", "nickname": "가" * 31}
        )
    assert excinfo.value.code == "VALIDATION_FAILED"


async def test_rejects_a_duplicate_email_regardless_of_casing(service):
    await service.sign_up(CREDENTIALS)

    with pytest.raises(AuthError) as excinfo:
        await service.sign_up({**CREDENTIALS, "email": "Writer@Blog-It.test"})
    assert excinfo.value.code == "EMAIL_ALREADY_EXISTS"


async def test_rejects_a_password_shorter_than_8_characters(service):
    with pytest.raises(AuthError) as excinfo:
        await service.sign_up({**CREDENTIALS, "password": "short"})
    assert excinfo.value.code == "VALIDATION_FAILED"


async def test_logs_in_with_correct_credentials_and_rejects_a_wrong_password(service):
    await service.sign_up(CREDENTIALS)

    session = await service.log_in(CREDENTIALS)
    assert session.user.email == CREDENTIALS["email"]

    with pytest.raises(AuthError) as excinfo:
        await service.log_in({**CREDENTIALS, "password": "wrongpassword"})
    assert excinfo.value.code == "INVALID_CREDENTIALS"


async def test_reports_an_unknown_email_as_invalid_credentials(service):
    with pytest.raises(AuthError) as excinfo:
        await service.log_in(CREDENTIALS)
    assert excinfo.value.code == "INVALID_CREDENTIALS"


async def test_rate_limits_repeated_failures_without_storing_the_email(
    service, monkeypatch
):
    from app.modules.auth import service as service_module

    monkeypatch.setattr(service_module, "MAX_ACCOUNT_FAILURES", 2)
    for _ in range(2):
        with pytest.raises(AuthError) as excinfo:
            await service.log_in(CREDENTIALS, client_key="203.0.113.10")
        assert excinfo.value.code == "INVALID_CREDENTIALS"

    with pytest.raises(AuthError) as excinfo:
        await service.log_in(CREDENTIALS, client_key="203.0.113.10")
    assert excinfo.value.code == "RATE_LIMITED"
    assert all(CREDENTIALS["email"] not in key for key in service._login_rate_limiter._failures)


async def test_account_rate_limit_cannot_be_bypassed_by_changing_client_ip(
    service, monkeypatch
):
    from app.modules.auth import service as service_module

    monkeypatch.setattr(service_module, "MAX_ACCOUNT_FAILURES", 2)
    for client_key in ("203.0.113.10", "198.51.100.20"):
        with pytest.raises(AuthError) as excinfo:
            await service.log_in(CREDENTIALS, client_key=client_key)
        assert excinfo.value.code == "INVALID_CREDENTIALS"

    with pytest.raises(AuthError) as excinfo:
        await service.log_in(CREDENTIALS, client_key="192.0.2.30")
    assert excinfo.value.code == "RATE_LIMITED"


async def test_concurrent_login_burst_counts_in_flight_attempts(
    repository, monkeypatch
):
    """A second request must see the first reservation before its hash finishes."""

    from app.modules.auth import service as service_module

    monkeypatch.setattr(service_module, "MAX_ACCOUNT_FAILURES", 1)
    started = threading.Event()
    release = threading.Event()

    def slow_failure(_password: str, _stored_hash: str) -> bool:
        started.set()
        release.wait(timeout=5)
        return False

    monkeypatch.setattr(service_module, "verify_password", slow_failure)
    limited = AuthService(repository=repository)
    first = asyncio.create_task(
        limited.log_in(CREDENTIALS, client_key="203.0.113.10")
    )
    assert await asyncio.to_thread(started.wait, 2)

    with pytest.raises(AuthError) as caught:
        await limited.log_in(CREDENTIALS, client_key="203.0.113.10")
    assert caught.value.code == "RATE_LIMITED"

    release.set()
    with pytest.raises(AuthError) as first_error:
        await first
    assert first_error.value.code == "INVALID_CREDENTIALS"
    assert limited._login_rate_limiter._in_flight == {}


async def test_unexpected_login_failure_releases_the_rate_limit_reservation(
    repository, monkeypatch
):
    original = repository.find_by_email

    async def broken_lookup(_email: str):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(repository, "find_by_email", broken_lookup)
    limited = AuthService(repository=repository)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await limited.log_in(CREDENTIALS, client_key="203.0.113.10")
    assert limited._login_rate_limiter._in_flight == {}

    monkeypatch.setattr(repository, "find_by_email", original)
    with pytest.raises(AuthError) as caught:
        await limited.log_in(CREDENTIALS, client_key="203.0.113.10")
    assert caught.value.code == "INVALID_CREDENTIALS"


async def test_signup_quota_is_per_client(repository, monkeypatch):
    from app.modules.auth import service as service_module

    monkeypatch.setattr(service_module, "MAX_SIGNUP_ATTEMPTS_PER_CLIENT", 1)
    limited = AuthService(repository=repository)
    await limited.sign_up(CREDENTIALS, client_key="203.0.113.10")

    with pytest.raises(AuthError) as caught:
        await limited.sign_up(
            {**CREDENTIALS, "email": "second@blog-it.test"},
            client_key="203.0.113.10",
        )
    assert caught.value.code == "RATE_LIMITED"

    other = await limited.sign_up(
        {**CREDENTIALS, "email": "other-client@blog-it.test"},
        client_key="198.51.100.20",
    )
    assert other.user.email == "other-client@blog-it.test"


async def test_password_hash_queue_rejects_excess_work_and_recovers(
    repository, monkeypatch
):
    from app.modules.auth import service as service_module

    monkeypatch.setattr(service_module, "MAX_PASSWORD_HASH_WORK_ITEMS", 1)
    started = threading.Event()
    release = threading.Event()
    original_hash = service_module.hash_password

    def slow_hash(password: str) -> str:
        started.set()
        release.wait(timeout=5)
        return original_hash(password)

    monkeypatch.setattr(service_module, "hash_password", slow_hash)
    limited = AuthService(repository=repository)
    first = asyncio.create_task(
        limited.sign_up(CREDENTIALS, client_key="203.0.113.10")
    )
    assert await asyncio.to_thread(started.wait, 2)

    with pytest.raises(AuthError) as caught:
        await limited.sign_up(
            {**CREDENTIALS, "email": "queue-full@blog-it.test"},
            client_key="198.51.100.20",
        )
    assert caught.value.code == "RATE_LIMITED"

    release.set()
    session = await first
    assert session.user.email == CREDENTIALS["email"]
    assert limited._password_hash_gate._admitted == 0


async def test_authenticates_a_bearer_token_and_rejects_malformed_or_unknown_ones(service):
    session = await service.sign_up(CREDENTIALS)

    user = await service.authenticate(f"Bearer {session.access_token}")
    assert user.user_id == session.user.user_id

    # A bare token with no scheme, an unknown token, and no header at all.
    for header in (session.access_token, "Bearer nope", None):
        with pytest.raises(AuthError) as excinfo:
            await service.authenticate(header)
        assert excinfo.value.code == "UNAUTHORIZED"


async def test_rejects_an_expired_token(repository):
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    expiring = AuthService(
        repository=repository,
        now=lambda: clock["now"],
        session_ttl=timedelta(seconds=60),
    )

    session = await expiring.sign_up(CREDENTIALS)
    clock["now"] = datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc)

    with pytest.raises(AuthError) as excinfo:
        await expiring.authenticate(f"Bearer {session.access_token}")
    assert excinfo.value.code == "UNAUTHORIZED"


async def test_logout_does_not_invalidate_the_token_on_the_server(service):
    """무상태 토큰으로 바꾸며 **의도적으로** 잃은 능력이다. 실수로 깨지지 않게 못 박아 둔다.

    로그아웃은 브라우저가 자기 토큰을 지우는 것이고, 서버에는 무효화할 기록이 없다
    (auth_sessions 컬렉션을 없앴다 — token.py 참고). 그래서 복사된 토큰은 만료까지 살아
    있다. 이 테스트가 실패한다면 무효화 수단이 다시 생겼다는 뜻이고, 그때는 이 문서를
    고칠 게 아니라 그 수단을 설명해야 한다.
    """
    session = await service.sign_up(CREDENTIALS)
    await service.log_out(f"Bearer {session.access_token}")

    user = await service.authenticate(f"Bearer {session.access_token}")
    assert user.user_id == session.user.user_id


async def test_an_expired_token_is_rejected(repository):
    """무효화가 없으므로 만료가 유일한 안전장치다 — 이건 반드시 지켜져야 한다."""
    from datetime import timedelta

    issued = datetime(2026, 1, 1, tzinfo=timezone.utc)
    service = AuthService(
        repository=repository, now=lambda: issued, session_ttl=timedelta(hours=1)
    )
    session = await service.sign_up(CREDENTIALS)

    later = AuthService(repository=repository, now=lambda: issued + timedelta(hours=2))
    with pytest.raises(AuthError) as caught:
        await later.authenticate(f"Bearer {session.access_token}")
    assert caught.value.code == "UNAUTHORIZED"


async def test_a_token_works_on_any_instance_without_shared_storage(repository):
    """토큰이 무상태라 서버를 늘려도 발급한 곳과 검증하는 곳이 같을 필요가 없다.

    저장소를 공유하던 시절의 요구사항을 서명이 대신한다 — 두 인스턴스가 같은 비밀키만
    보면 된다.
    """
    first = AuthService(repository=repository)
    second = AuthService(repository=repository)

    session = await first.sign_up(CREDENTIALS)

    user = await second.authenticate(f"Bearer {session.access_token}")
    assert user.user_id == session.user.user_id


async def test_a_tampered_token_is_rejected(repository):
    """서명이 없으면 사용자 id를 바꿔치기해 남의 계정이 된다 — 무상태 토큰의 핵심 위험이다."""
    service = AuthService(repository=repository)
    session = await service.sign_up(CREDENTIALS)

    version, payload, expiry, signature = session.access_token.split(".")
    forged = f"{version}.{payload}.{int(expiry) + 60 * 60 * 24 * 365}.{signature}"

    with pytest.raises(AuthError) as caught:
        await service.authenticate(f"Bearer {forged}")
    assert caught.value.code == "UNAUTHORIZED"


def test_a_restart_keeps_everyone_logged_in(monkeypatch):
    """서명 키는 파일에 남아 **재시작을 넘긴다**(2026-08-06).

    예전에는 프로세스마다 난수라 재시작이 곧 전원 로그아웃이었다. 개발 중에는 파일 하나만
    바뀌어도 서버가 다시 뜨는데, 그때마다 조용히 로그아웃된 사용자가 로그인 화면의
    기본값(데모 계정)으로 다시 들어가 **다른 계정**으로 앱을 보게 됐다.
    """
    import tempfile

    from app.modules.auth import token as token_module

    monkeypatch.delenv("AUTH_TOKEN_SECRET", raising=False)
    # 이 테스트는 **개발 환경의** 파일 기반 키 경로를 검증한다. 그런데 앞선 테스트가
    # create_app()으로 .env를 로드하면 그 안의 APP_ENV=production이 os.environ에 남아
    # (load_env_file은 진짜 환경만 존중하고 정리하지 않는다), production 분기가 먼저
    # RuntimeError를 낸다. 실행 순서·개발 PC의 .env 내용과 무관하게 돌도록 여기서 박는다.
    monkeypatch.setenv("APP_ENV", "development")
    # pytest의 tmp_path 대신 직접 만든다 — 이 PC에서는 tmp_path 픽스처가 권한 오류를 낸다.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / ".auth-secret"
        monkeypatch.setattr(token_module, "_secret_path", lambda: path)

        first = token_module._load_or_create_secret()
        # 재시작 = 모듈이 다시 불리는 것. 같은 자리를 보면 같은 키가 나와야 한다.
        assert token_module._load_or_create_secret() == first
        assert path.is_file()


def test_production_requires_an_external_signing_secret(monkeypatch):
    from app.modules.auth import token as token_module

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AUTH_TOKEN_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="AUTH_TOKEN_SECRET"):
        token_module._load_or_create_secret()


def test_rejects_a_short_configured_signing_secret(monkeypatch):
    from app.modules.auth import token as token_module

    monkeypatch.setenv("AUTH_TOKEN_SECRET", "too-short")

    with pytest.raises(RuntimeError, match="32"):
        token_module._load_or_create_secret()


async def test_signup_does_not_write_a_user_when_the_signing_secret_is_invalid(
    repository, monkeypatch
):
    from app.modules.auth import token as token_module

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AUTH_TOKEN_SECRET", raising=False)
    monkeypatch.setattr(token_module, "_SECRET", None)
    isolated = AuthService(repository=repository)

    with pytest.raises(RuntimeError, match="AUTH_TOKEN_SECRET"):
        await isolated.sign_up(CREDENTIALS, client_key="203.0.113.10")
    assert await repository.find_by_email(CREDENTIALS["email"]) is None


async def test_a_changed_signing_key_invalidates_every_token(repository, monkeypatch):
    """키가 바뀌면(파일을 지웠거나 다른 서버) 이전 토큰은 전부 무효다.

    토큰을 서버가 따로 저장하지 않으므로, 강제 로그아웃의 유일한 수단이기도 하다.
    """
    from app.modules.auth import token as token_module

    service = AuthService(repository=repository)
    session = await service.sign_up(CREDENTIALS)
    assert (await service.authenticate(f"Bearer {session.access_token}")).user_id

    monkeypatch.setattr(token_module, "_SECRET", secrets.token_hex(32).encode("utf-8"))

    with pytest.raises(AuthError) as caught:
        await service.authenticate(f"Bearer {session.access_token}")
    assert caught.value.code == "UNAUTHORIZED"


def test_verifies_a_hash_produced_by_the_typescript_server():
    """Existing accounts must keep working after the port. This hash was
    generated by the Node implementation's hashPassword() for "demo1234"."""
    node_hash = (
        "scrypt$b46e436b33c8d4bf53040fb111aab946$"
        "1d2519ec63c6bdb3f0915e82e6fc3485f370b6244aecadefbdfd7afeb0fd6bc6"
        "c7b6727b9f3f78d2127558180cb9b6d14d4f5da7f626b1979cc228756b3d1bed"
    )

    assert verify_password("demo1234", node_hash) is True
    assert verify_password("wrongpassword", node_hash) is False


async def test_successful_login_upgrades_a_legacy_scrypt_hash(service, repository):
    import hashlib

    from app.shared import User

    salt = bytes.fromhex("01" * 16)
    key = hashlib.scrypt(
        b"password123",
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=64,
        maxmem=64 * 1024 * 1024,
    )
    legacy = f"scrypt${salt.hex()}${key.hex()}"
    await repository.create(
        User(
            user_id="u_legacy",
            email="legacy@blog-it.test",
            nickname="레거시",
            password_hash=legacy,
            created_at="1970-01-01T00:00:00.000Z",
            updated_at="1970-01-01T00:00:00.000Z",
        )
    )

    await service.log_in(
        {"email": "legacy@blog-it.test", "password": "password123"}
    )

    stored = await repository.find_by_email("legacy@blog-it.test")
    assert stored is not None
    assert stored.password_hash.startswith(f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$")
    assert verify_password("password123", stored.password_hash)
    assert stored.updated_at != "1970-01-01T00:00:00.000Z"


def test_rejects_a_malformed_stored_hash():
    for malformed in [
        "",
        "notscrypt$aa$bb",
        "scrypt$onlyonepart",
        "scrypt$zz$zz",
        f"scrypt$999999999$8$1${'00' * 16}${'00' * 64}",
        f"scrypt$32768$8$3${'00' * 1000}${'00' * 64}",
    ]:
        assert verify_password("demo1234", malformed) is False
