"""서명된 무상태 액세스 토큰.

예전에는 발급한 토큰을 `auth_sessions` 컬렉션에 저장하고 요청마다 조회했다. 컬렉션을
줄이려고 서버가 아무것도 저장하지 않는 방식으로 바꿨다 — 토큰 안에 사용자 id와 만료
시각을 담고, 위조를 막는 서명을 붙인다. 검증은 서명 확인과 만료 확인뿐이라 DB를 보지
않는다.

무엇을 잃었는지 분명히 해 둔다: **발급한 토큰을 서버가 무효화할 수 없다.** 로그아웃은
브라우저의 토큰을 지우는 것이고, 유출된 토큰은 만료(7일)까지 유효하다. 비밀번호를
바꿔도 다른 기기가 즉시 끊기지 않는다. 저장소를 없앤 대가이며, 되돌리려면 토큰 무효화
목록을 어딘가에 두어야 한다 — 그건 다시 저장소다.

개발 환경의 서명 키는 **첫 실행 때 만들어 저장소 루트의 `.auth-secret`에 둔다.** 그 뒤로는
그 파일을 읽으므로 **서버를 다시 띄워도 로그인이 유지된다.** 운영 환경은 파일 fallback을
허용하지 않고 외부 secret store가 주입한 `AUTH_TOKEN_SECRET`을 요구한다.

예전에는 프로세스마다 난수였다(2026-07-28 결정, "설정할 것이 없다"가 이유였다). 그 대가가
**재시작 = 전원 로그아웃**이었는데, 개발 중에는 파일 하나만 바뀌어도 `--reload`가 서버를
다시 띄운다. 그때마다 브라우저는 조용히 로그아웃되고, 사용자는 로그인 화면의 기본값
(데모 계정)으로 다시 들어가 **다른 계정**으로 앱을 보게 됐다 — 저장해 둔 네이버 계정도,
걸어 둔 예약도 그 계정에는 없으니 화면이 "설정에서 Naver 계정을 먼저 저장해 주세요"라고
말했다(2026-08-06 실사용). 위 docstring이 열어 둔 두 길 중 '파일에 저장'을 택한다.

무엇을 잃는지도 적어 둔다: 개발용 파일을 읽을 수 있는 사람은 토큰을 위조할 수 있다.
그래서 저장소 밖으로 나가지 않게 `.gitignore`에 넣는다. 개발 키를 갈아 끼우고 싶으면
(전원 로그아웃) 그 파일을 지우면 된다. 환경변수 `AUTH_TOKEN_SECRET`를 주면 파일 대신
그 값을 쓰며, 운영과 다중 서버에서는 이 경로만 허용한다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path

SECRET_FILE = ".auth-secret"


def _secret_path() -> Path:
    """`.env`가 있는 곳(=저장소 루트) 옆에 둔다. 못 찾으면 지금 폴더."""
    from app.config import find_env_file

    env_file = find_env_file()
    return (env_file.parent if env_file else Path.cwd()) / SECRET_FILE


def _load_or_create_secret() -> bytes:
    """저장된 키를 읽고, 개발 환경에서만 없으면 권한을 제한해 새로 만든다."""
    configured = (os.environ.get("AUTH_TOKEN_SECRET") or "").strip()
    if configured:
        secret = configured.encode("utf-8")
        if len(secret) < 32:
            raise RuntimeError("AUTH_TOKEN_SECRET은 최소 32바이트여야 합니다.")
        return secret

    if (os.environ.get("APP_ENV") or "development").strip().lower() == "production":
        raise RuntimeError(
            "운영 환경에서는 AUTH_TOKEN_SECRET을 외부 secret store에서 주입해야 합니다."
        )

    path = _secret_path()
    try:
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            secret = saved.encode("utf-8")
            if len(secret) < 32:
                raise RuntimeError(f"{path.name}의 서명 키가 너무 짧습니다.")
            return secret
    except OSError:
        pass

    created = secrets.token_hex(32)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(created)
            output.flush()
            os.fsync(output.fileno())
        if os.name != "nt":
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except FileExistsError:
        saved = path.read_text(encoding="utf-8").strip()
        if len(saved.encode("utf-8")) < 32:
            raise RuntimeError(f"{path.name}의 서명 키가 너무 짧습니다.") from None
        return saved.encode("utf-8")
    except OSError as error:
        raise RuntimeError(f"로그인 서명 키를 {path}에 안전하게 저장하지 못했습니다.") from error
    return created.encode("utf-8")


# FastAPI lifespan이 .env를 읽기 전에 이 모듈이 import되므로 첫 사용까지 로드를 미룬다.
_SECRET: bytes | None = None

TOKEN_VERSION = "v1"


class InvalidToken(Exception):
    """서명 불일치·형식 오류·만료. 호출부는 모두 같은 401로 취급한다 — 어느 쪽인지
    알려 주면 토큰을 맞춰 보는 쪽에 힌트가 된다."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def initialize_signing_secret() -> bytes:
    """Load the signing root eagerly and fail before the application accepts work.

    ``app.main`` calls this during lifespan startup, after ``.env`` has been
    loaded.  Keeping this explicit avoids creating a user and only then finding
    out that production has no signing secret when the response token is issued.
    """

    global _SECRET
    _SECRET = _load_or_create_secret()
    return _SECRET


def ensure_signing_secret() -> bytes:
    """Return the initialized root, loading it defensively for direct service use."""

    global _SECRET
    if _SECRET is None:
        _SECRET = _load_or_create_secret()
    return _SECRET


def _sign(payload: str) -> str:
    secret = ensure_signing_secret()
    return _b64(hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest())


def sign_internal(purpose: str, payload: str) -> str:
    """같은 키를 쓰되 access token과 도메인을 분리한 내부 capability 서명."""
    return _sign(f"internal:{purpose}:{payload}")


def verify_internal(purpose: str, payload: str, signature: str) -> bool:
    return bool(signature) and hmac.compare_digest(
        sign_internal(purpose, payload), signature
    )


def issue(user_id: str, expires_at: datetime) -> str:
    """`v1.<userId>.<만료epoch>.<서명>`. 사용자 id에 '.'가 들어가도 깨지지 않도록 인코딩한다."""
    payload = f"{TOKEN_VERSION}.{_b64(user_id.encode('utf-8'))}.{int(expires_at.timestamp())}"
    return f"{payload}.{_sign(payload)}"


def verify(token: str, now: datetime) -> str:
    """서명과 만료를 확인하고 사용자 id를 돌려준다. 아니면 InvalidToken.

    서명 비교는 compare_digest로 한다 — 앞자리부터 다른 토큰이 더 빨리 거절되면 그
    시간차가 서명을 한 바이트씩 맞춰 보는 단서가 된다.
    """
    parts = (token or "").split(".")
    if len(parts) != 4 or parts[0] != TOKEN_VERSION:
        raise InvalidToken("malformed token")

    payload = ".".join(parts[:3])
    if not hmac.compare_digest(_sign(payload), parts[3]):
        raise InvalidToken("bad signature")

    try:
        user_id = _unb64(parts[1]).decode("utf-8")
        expires_at = datetime.fromtimestamp(int(parts[2]), UTC)
    except (ValueError, UnicodeDecodeError) as error:
        raise InvalidToken("unreadable payload") from error

    if expires_at <= now:
        raise InvalidToken("expired")
    return user_id
