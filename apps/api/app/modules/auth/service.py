"""인증.service.ts.

새 비밀번호는 비용을 함께 기록한
`scrypt$<N>$<r>$<p>$<saltHex>$<keyHex>` 형식으로 단방향 해시한다. 옛 Node 형식
`scrypt$<saltHex>$<keyHex>`(N=16384/r=8/p=1)도 검증하고, 성공 로그인 때 새 형식으로
자동 승격한다. 비밀번호는 복호 가능한 AES로 저장하지 않는다.
"""

import asyncio
import hashlib
import hmac
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable

from app.errors import AuthError
from app.shared.ids import new_user_id
from app.shared import AuthSession, PublicUser, User, to_public_user

from .repository import UserRepository, normalize_email
from .token import (
    InvalidToken,
    ensure_signing_secret,
    issue as issue_token,
    verify as verify_token,
)

SALT_BYTES = 16
KEY_BYTES = 64
MIN_PASSWORD_LENGTH = 8
MAX_NICKNAME_LENGTH = 30
MAX_EMAIL_LENGTH = 254
MAX_PASSWORD_LENGTH = 1_024
DEFAULT_SESSION_TTL = timedelta(days=7)
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
MAX_ACCOUNT_FAILURES = 8
MAX_CLIENT_FAILURES = 80
MAX_RATE_LIMIT_KEYS = 4_096
SIGNUP_ATTEMPT_WINDOW = timedelta(minutes=15)
MAX_SIGNUP_ATTEMPTS_PER_CLIENT = 10

# 인증된 사용자를 다시 조회하지 않고 재사용하는 시간(초). authenticate 참고.
# 화면 하나가 한꺼번에 던지는 요청 묶음을 합치는 것이 목적이라 짧다.
AUTH_USER_CACHE_SECONDS = 5.0
MAX_CACHED_USERS = 256

# OWASP 권고 조합 중 N=2^15/r=8/p=3을 사용한다.
SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 3
# 128 * N * r = 32 MiB. OpenSSL 내부 여유까지 넉넉히 둔다.
SCRYPT_MAXMEM = 128 * 1024 * 1024
LEGACY_SCRYPT_N = 16384
LEGACY_SCRYPT_R = 8
LEGACY_SCRYPT_P = 1
PASSWORD_HASH_CONCURRENCY = 2
# 실행 중인 작업까지 포함한 프로세스 단위 상한. semaphore만 두면 실행은 두 개여도
# 대기 coroutine은 무한히 쌓여 인증 폭주가 메모리를 소진할 수 있다.
MAX_PASSWORD_HASH_WORK_ITEMS = 16

# 어떤 사용자도 등록할 수 없는 값의 해시. 모르는 이메일의 로그인 처리 시간을 맞추는 데만
# 쓴다. 응답 시간으로 계정 존재 여부가 드러나지 않도록.
DUMMY_PASSWORD_HASH = (
    f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
    f"{'00' * SALT_BYTES}${'00' * KEY_BYTES}"
)


def _derive(
    password: str,
    salt: bytes,
    keylen: int,
    *,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=keylen,
        maxmem=SCRYPT_MAXMEM,
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    return (
        f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
        f"{salt.hex()}${_derive(password, salt, KEY_BYTES).hex()}"
    )


def verify_password(password: str, stored_hash: str) -> bool:
    parts = stored_hash.split("$")
    if len(parts) == 3:
        algorithm, salt_hex, key_hex = parts
        n, r, p = LEGACY_SCRYPT_N, LEGACY_SCRYPT_R, LEGACY_SCRYPT_P
    elif len(parts) == 6:
        algorithm, n_raw, r_raw, p_raw, salt_hex, key_hex = parts
        try:
            n, r, p = int(n_raw), int(r_raw), int(p_raw)
        except ValueError:
            return False
    else:
        return False
    if (
        algorithm != "scrypt"
        or not salt_hex
        or not key_hex
        or n < LEGACY_SCRYPT_N
        or r <= 0
        or p <= 0
        or n > SCRYPT_N
        or r > SCRYPT_R
        or p > SCRYPT_P
    ):
        return False

    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
    except ValueError:
        return False
    if len(salt) != SALT_BYTES or len(expected) != KEY_BYTES:
        return False

    try:
        derived = _derive(password, salt, len(expected), n=n, r=r, p=p)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)


def password_hash_needs_upgrade(stored_hash: str) -> bool:
    parts = stored_hash.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return True
    try:
        return tuple(map(int, parts[1:4])) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    except ValueError:
        return True


def read_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    parts = authorization_header.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _validate_credentials(body: Any, *, enforce_password_length: bool) -> tuple[str, str]:
    if not isinstance(body, dict):
        raise AuthError("VALIDATION_FAILED", "request body must be a JSON object")

    email = body.get("email")
    password = body.get("password")

    if not isinstance(email, str) or not EMAIL_PATTERN.match(email.strip()):
        raise AuthError("VALIDATION_FAILED", "a valid email is required")
    if len(email.strip()) > MAX_EMAIL_LENGTH:
        raise AuthError("VALIDATION_FAILED", f"email must be at most {MAX_EMAIL_LENGTH} characters")
    if not isinstance(password, str) or not password:
        raise AuthError("VALIDATION_FAILED", "password is required")
    if enforce_password_length and len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            "VALIDATION_FAILED", f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError(
            "VALIDATION_FAILED", f"password must be at most {MAX_PASSWORD_LENGTH} characters"
        )

    return normalize_email(email), password


def _validate_nickname(body: Any) -> str:
    """회원가입 때만 받는다. 표시·구분용 이름이라 필수이고, 앞뒤 공백은 다듬는다."""
    nickname = body.get("nickname") if isinstance(body, dict) else None
    if not isinstance(nickname, str) or not nickname.strip():
        raise AuthError("VALIDATION_FAILED", "nickname is required")
    trimmed = nickname.strip()
    if len(trimmed) > MAX_NICKNAME_LENGTH:
        raise AuthError(
            "VALIDATION_FAILED", f"nickname must be at most {MAX_NICKNAME_LENGTH} characters"
        )
    return trimmed


@dataclass(frozen=True)
class _LoginReservation:
    keys: tuple[tuple[str, int], tuple[str, int]]


class _PasswordHashGate:
    """Bound both active password hashes and the number waiting for a slot."""

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(PASSWORD_HASH_CONCURRENCY)
        self._lock = asyncio.Lock()
        self._admitted = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._lock:
            if self._admitted >= MAX_PASSWORD_HASH_WORK_ITEMS:
                raise AuthError(
                    "RATE_LIMITED",
                    "too many authentication operations; try again shortly",
                )
            self._admitted += 1

        acquired = False
        try:
            await self._semaphore.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                self._semaphore.release()
            async with self._lock:
                self._admitted = max(0, self._admitted - 1)


class _SignupRateLimiter:
    """Process-local client quota for the CPU- and write-heavy signup path."""

    def __init__(self) -> None:
        self._attempts: dict[str, list[datetime]] = {}
        self._lock = asyncio.Lock()

    async def consume(self, client_key: str, now: datetime) -> None:
        key = hashlib.sha256(client_key.encode("utf-8")).hexdigest()
        cutoff = now - SIGNUP_ATTEMPT_WINDOW
        async with self._lock:
            recent = [value for value in self._attempts.get(key, []) if value > cutoff]
            if len(recent) >= MAX_SIGNUP_ATTEMPTS_PER_CLIENT:
                raise AuthError("RATE_LIMITED", "too many signup attempts; try again later")
            recent.append(now)
            self._attempts[key] = recent

            if len(self._attempts) > MAX_RATE_LIMIT_KEYS:
                self._attempts = {
                    digest: [value for value in values if value > cutoff]
                    for digest, values in self._attempts.items()
                    if any(value > cutoff for value in values)
                }
                while len(self._attempts) > MAX_RATE_LIMIT_KEYS:
                    self._attempts.pop(next(iter(self._attempts)))


class _LoginRateLimiter:
    """이메일 원문을 보관하지 않는 프로세스 단위 로그인 실패 제한기."""

    def __init__(self) -> None:
        self._failures: dict[str, list[datetime]] = {}
        self._in_flight: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _keys(email: str, client_key: str) -> tuple[tuple[str, int], tuple[str, int]]:
        client_digest = hashlib.sha256(client_key.encode("utf-8")).hexdigest()
        # 계정 제한은 IP와 무관해야 한다. 둘을 함께 해시하면 공격자가 IP만 바꿔 같은
        # 계정의 제한을 우회할 수 있다. 원문 이메일은 메모리에 남기지 않는다.
        account_digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
        return (
            (f"account:{account_digest}", MAX_ACCOUNT_FAILURES),
            (f"client:{client_digest}", MAX_CLIENT_FAILURES),
        )

    def _release_locked(self, reservation: _LoginReservation) -> None:
        for key, _limit in reservation.keys:
            count = self._in_flight.get(key, 0)
            if count <= 1:
                self._in_flight.pop(key, None)
            else:
                self._in_flight[key] = count - 1

    async def reserve(
        self, email: str, client_key: str, now: datetime
    ) -> _LoginReservation:
        """Reserve an attempt before hashing so concurrent bursts count immediately."""

        cutoff = now - LOGIN_FAILURE_WINDOW
        keys = self._keys(email, client_key)
        async with self._lock:
            for key, limit in keys:
                recent = [value for value in self._failures.get(key, []) if value > cutoff]
                if recent:
                    self._failures[key] = recent
                else:
                    self._failures.pop(key, None)
                if len(recent) + self._in_flight.get(key, 0) >= limit:
                    raise AuthError("RATE_LIMITED", "too many login attempts; try again later")
            for key, _limit in keys:
                self._in_flight[key] = self._in_flight.get(key, 0) + 1
        return _LoginReservation(keys=keys)

    async def failed(self, reservation: _LoginReservation, now: datetime) -> None:
        async with self._lock:
            self._release_locked(reservation)
            for key, _limit in reservation.keys:
                self._failures.setdefault(key, []).append(now)
            if len(self._failures) > MAX_RATE_LIMIT_KEYS:
                cutoff = now - LOGIN_FAILURE_WINDOW
                self._failures = {
                    key: [value for value in values if value > cutoff]
                    for key, values in self._failures.items()
                    if any(value > cutoff for value in values)
                }
                # 최근 키만으로 상한을 넘기는 분산 공격에서도 메모리가 무한히 자라지 않게
                # 삽입 순서가 가장 오래된 항목부터 제한한다.
                while len(self._failures) > MAX_RATE_LIMIT_KEYS:
                    self._failures.pop(next(iter(self._failures)))

    async def succeeded(self, reservation: _LoginReservation) -> None:
        account_key = reservation.keys[0][0]
        async with self._lock:
            self._release_locked(reservation)
            self._failures.pop(account_key, None)

    async def release(self, reservation: _LoginReservation) -> None:
        """Release an attempt when lookup/hashing fails unexpectedly or is cancelled."""

        async with self._lock:
            self._release_locked(reservation)


class AuthService:
    def __init__(
        self,
        repository: UserRepository,
        now: Callable[[], datetime] | None = None,
        session_ttl: timedelta = DEFAULT_SESSION_TTL,
    ):
        self._repository = repository
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._session_ttl = session_ttl
        self._password_hash_gate = _PasswordHashGate()
        self._login_rate_limiter = _LoginRateLimiter()
        self._signup_rate_limiter = _SignupRateLimiter()
        # 방금 확인한 사용자. userId → (확인 시각, PublicUser). authenticate 참고.
        self._recent_users: dict[str, tuple[datetime, PublicUser]] = {}

    async def sign_up(
        self, raw_body: Any, *, client_key: str = "unknown"
    ) -> AuthSession:
        email, password = _validate_credentials(raw_body, enforce_password_length=True)
        nickname = _validate_nickname(raw_body)

        # Alternate callers may construct AuthService without the FastAPI lifespan. Verify the
        # signer before any persistent write so a bad production secret cannot create half-finished
        # accounts whose request ended with 500.
        ensure_signing_secret()
        await self._signup_rate_limiter.consume(client_key, self._now())

        if await self._repository.find_by_email(email):
            raise AuthError("EMAIL_ALREADY_EXISTS", "email already registered")

        timestamp = _iso(self._now())
        async with self._password_hash_gate.slot():
            password_hash = await asyncio.to_thread(hash_password, password)
        user = await self._repository.create(
            User(
                user_id=new_user_id(),
                email=email,
                nickname=nickname,
                password_hash=password_hash,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        return await self._issue_session(user)

    async def backfill_nickname(self, email: str, nickname: str) -> None:
        """이미 있는 계정에 닉네임이 비어 있으면 채운다. 사용자가 이미 정한 닉네임은
        덮어쓰지 않는다. 닉네임 도입 전에 만들어진 데모 계정을 시작 때 메꾸는 데 쓴다."""
        user = await self._repository.find_by_email(normalize_email(email))
        if user is not None and not user.nickname:
            await self._repository.set_nickname(user.user_id, nickname)

    async def log_in(self, raw_body: Any, *, client_key: str = "unknown") -> AuthSession:
        email, password = _validate_credentials(raw_body, enforce_password_length=False)
        reservation = await self._login_rate_limiter.reserve(
            email, client_key, self._now()
        )
        try:
            user = await self._repository.find_by_email(email)
            stored_hash = user.password_hash if user else DUMMY_PASSWORD_HASH
            upgraded: str | None = None
            async with self._password_hash_gate.slot():
                matches = await asyncio.to_thread(verify_password, password, stored_hash)
                if user and matches and password_hash_needs_upgrade(user.password_hash):
                    upgraded = await asyncio.to_thread(hash_password, password)

            if not user or not matches:
                await self._login_rate_limiter.failed(reservation, self._now())
                reservation = None
                raise AuthError("INVALID_CREDENTIALS", "email or password is incorrect")

            await self._login_rate_limiter.succeeded(reservation)
            reservation = None

            if upgraded is not None:
                updated_at = _iso(self._now())
                await self._repository.update_password_hash(user.user_id, upgraded, updated_at)
                user = user.model_copy(
                    update={"password_hash": upgraded, "updated_at": updated_at}
                )

            return await self._issue_session(user)
        finally:
            if reservation is not None:
                await self._login_rate_limiter.release(reservation)

    async def authenticate(self, authorization_header: str | None) -> PublicUser:
        token = read_bearer_token(authorization_header)
        if not token:
            raise AuthError("UNAUTHORIZED", "missing bearer token")

        try:
            user_id = verify_token(token, self._now())
        except InvalidToken as error:
            raise AuthError("UNAUTHORIZED", "invalid access token") from error

        # 서명이 맞아도 사용자를 확인한다. 토큰은 무효화할 수 없으므로(token.py 참고),
        # 탈퇴한 계정의 토큰을 막는 것은 이 조회뿐이다.
        #
        # **다만 몇 초 동안은 방금 본 결과를 다시 쓴다.** 화면 하나를 여는 데 요청이
        # 대여섯 개 동시에 나가고(예약 화면은 네 개를 한 번에 부른다), 그 전부가 같은
        # 사용자 문서를 각자 한 번씩 읽었다 — 원격 Mongo에서는 그 왕복이 요청마다
        # 그대로 얹힌다. 캐시는 그 한 묶음을 합치는 정도이지 세션을 대신하지 않는다.
        #
        # 잃는 것: 탈퇴한 계정의 토큰이 최대 이 시간만큼 더 통한다. 그래서 짧게 잡았다
        # (AUTH_USER_CACHE_SECONDS). 로그인·회원가입은 이 길을 타지 않는다.
        now = self._now()
        cached = self._recent_users.get(user_id)
        if cached and (now - cached[0]).total_seconds() < AUTH_USER_CACHE_SECONDS:
            return cached[1]

        user = await self._repository.find_by_user_id(user_id)
        if not user:
            # 사라진 계정은 캐시에서도 지운다 — 남겨 두면 만료까지 통과한다.
            self._recent_users.pop(user_id, None)
            raise AuthError("UNAUTHORIZED", "user no longer exists")

        public = to_public_user(user)
        self._recent_users[user_id] = (now, public)
        # 프로세스 하나가 상대하는 사용자 수만큼만 쌓이지만, 상한은 둔다.
        if len(self._recent_users) > MAX_CACHED_USERS:
            oldest = min(self._recent_users, key=lambda key: self._recent_users[key][0])
            self._recent_users.pop(oldest, None)
        return public

    async def log_out(self, authorization_header: str | None) -> None:
        """서버가 할 일이 없다 — 토큰을 무상태로 바꾼 뒤로 무효화할 저장소가 없다.

        엔드포인트는 남긴다. 클라이언트는 이 응답을 받고 자기 토큰을 지우며, 나중에
        무효화 목록을 들이더라도 호출 지점은 그대로 쓸 수 있다.
        """
        _ = read_bearer_token(authorization_header)

    async def _issue_session(self, user: User) -> AuthSession:
        issued_at = self._now()
        expires_at = issued_at + self._session_ttl
        access_token = issue_token(user.user_id, expires_at)

        return AuthSession(
            user=to_public_user(user),
            access_token=access_token,
            issued_at=_iso(issued_at),
            expires_at=_iso(expires_at),
        )


def _iso(value: datetime) -> str:
    """Node의 Date.toISOString() 형식: 항상 UTC, 밀리초, 끝에 Z."""
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
