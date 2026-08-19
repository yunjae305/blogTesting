"""자동 발행 설정을 읽어오는 곳.

실제로 동작할 수 있을 때만 켜진다. 켜져 있는데 아무것도 하지 않는 자동 발행
옵션은 가짜 발행기와 다를 게 없다 — SUCCESS와 blog.example.com URL을 돌려주지만
글은 아무 데도 올라가지 않았다.
"""

import hashlib
import logging
import os
from pathlib import Path

from .credential_crypto import (
    CredentialProtectionError,
    atomic_write_json,
    credential_scope,
    is_v2_document,
    needs_rewrap,
    protect_values,
    read_json,
    unprotect_values,
)
from .naver import NaverConfig

logger = logging.getLogger(__name__)

BLOG_ID_ENV = "NAVER_BLOG_ID"
PROFILE_DIR_ENV = "NAVER_BROWSER_PROFILE_DIR"

# 에디터가 가져가는 이미지를 API가 서빙하므로, 붙여넣기를 하는 브라우저에서
# 접근할 수 있어야 한다 — 같은 기기다.
DEFAULT_PORT = 3000


# 블로그 아이디는 DB가 아니라 세션에 속한다. 그 쿠키가 로그인한 계정의 아이디이며,
# 둘은 함께일 때만 의미가 있다.
BLOG_ID_FILE = "blog_id"
BLOG_ID_FIELD = "blogId"


def _remembered_blog_id(profile_dir: Path) -> str:
    marker = profile_dir / BLOG_ID_FILE
    if not marker.is_file():
        return ""
    try:
        raw = marker.read_text(encoding="utf-8").strip()
        if not raw:
            return ""
        if raw.startswith("{"):
            document = read_json(marker)
            if not is_v2_document(document):
                raise CredentialProtectionError("unsupported blog id marker version")
            blog_id = unprotect_values(
                document, (BLOG_ID_FIELD,), credential_scope(profile_dir)
            )[BLOG_ID_FIELD].strip()
            if blog_id and needs_rewrap(document):
                remember_blog_id(profile_dir, blog_id)
            return blog_id

        # 예전 평문 표식은 안전한 v2 교체가 성공한 뒤에만 호출자에게 돌려준다.
        remember_blog_id(profile_dir, raw)
        return raw
    except (CredentialProtectionError, OSError, UnicodeDecodeError, ValueError) as error:
        logger.warning(
            "네이버 블로그 식별자를 안전하게 읽거나 마이그레이션하지 못했습니다 (%s).",
            type(error).__name__,
        )
        return ""


def remember_blog_id(profile_dir: Path, blog_id: str) -> None:
    normalized = (blog_id or "").strip()
    if not normalized:
        return
    document = protect_values(
        {BLOG_ID_FIELD: normalized}, credential_scope(profile_dir)
    )
    atomic_write_json(profile_dir / BLOG_ID_FILE, document)


# 발행할 때 **실제로 열린** 블로그 주소. 위 blog_id가 설정에서 받은 값(대개 네이버
# 아이디)인 반면, 이쪽은 브라우저가 도착한 주소에서 읽은 사실이다.
#
# 둘은 다를 수 있다. 네이버 블로그 주소는 아이디와 같은 것이 기본이지만 바꿀 수 있어서,
# 아이디가 `win-z`인데 주소가 `aiona_it`인 계정이 실제로 있었다. 아이디로 만든 주소는
# "게시물이 삭제되었거나 다른 페이지로 변경되었습니다"로 막힌다(2026-08-05 실사용).
BLOG_ADDRESS_FILE = "blog_address"


def observed_blog_address(profile_dir: Path, username: str | None = None) -> str:
    """확인된 블로그 주소. **그것을 배운 계정과 다른 계정이면 빈 문자열이다.**

    배운 주소는 그때 로그인해 있던 계정의 것이다. 설정에서 다른 계정으로 바꾼 직후에는
    아직 로그아웃 전이라 파일이 남아 있는데, 그대로 쓰면 **새 계정으로 이전 계정의
    블로그 주소에 들어가려 한다** — 실제로 그래서 "삭제되었거나 존재하지 않는
    게시물입니다" 안내창을 만났다(2026-08-05 실사용).
    """
    marker = profile_dir / BLOG_ADDRESS_FILE
    try:
        address = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not address or not (username or "").strip():
        return address
    from .credentials import session_account

    owner = session_account(profile_dir)
    if owner and owner.strip().lower() != username.strip().lower():
        return ""
    return address


def remember_blog_address(profile_dir: Path, address: str) -> None:
    """발행하며 알게 된 실제 블로그 주소를 적어 둔다. 다음 발행은 여기로 바로 간다."""
    if not (address or "").strip():
        return
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / BLOG_ADDRESS_FILE).write_text(address.strip(), encoding="utf-8")
    except OSError as error:
        # 못 적어도 발행은 된다. 다음에도 폴백 경로를 한 번 더 거칠 뿐이다.
        logger.warning("블로그 주소를 기억하지 못했습니다: %s", error)


def forget_blog_address(profile_dir: Path) -> None:
    """계정이 바뀌면 지운다 — 이전 계정의 블로그 주소는 새 계정과 아무 상관이 없다."""
    try:
        (profile_dir / BLOG_ADDRESS_FILE).unlink()
    except OSError:
        pass


def naver_profile_dir(user_id: str | None = None) -> Path:
    """네이버 로그인 정보와 세션을 보관하는 DB 밖의 로컬 경로.

    API에서 사용할 때는 Blog-it ``user_id``를 넘겨 로그인 사용자마다 완전히 다른
    Chrome 프로필을 사용한다. 경로에는 원래 사용자 ID 대신 해시를 써서 경로 삽입과
    로컬 식별자 노출을 막는다. user_id가 없는 호출은 관리용 CLI의 기존 프로필을 쓴다.
    """
    configured = (os.environ.get(PROFILE_DIR_ENV) or "").strip()
    base = (
        Path(configured).resolve()
        if configured
        else (Path(__file__).resolve().parents[4] / ".naver-profile").resolve()
    )
    if not user_id:
        return base

    user_scope = hashlib.sha256(user_id.strip().encode("utf-8")).hexdigest()[:24]
    return base.parent / f"{base.name}-users" / user_scope


def naver_config_from_env(
    username: str | None = None,
    password: str | None = None,
    user_id: str | None = None,
) -> NaverConfig | None:
    """네이버 자동 발행이 설정되지 않았으면 None.

    인자는 사용자가 방금 설정에서 입력한 값이다. 로그인 식별자·비밀번호는 ``.env``에서
    읽지 않는다. 관리 CLI는 같은 프로필의 v2 암호화 자격증명을 읽고, API 사용자는 설정
    화면에서 받은 값을 넘긴다. 비밀번호는 DB에 닿지 않는다 — posting.credentials 참고.

    블로그 주소는 아무에게도 묻지 않는다. 네이버 블로그는 기본적으로
    blog.naver.com/<네이버 아이디>에 있기 때문이다. **다만 주소는 바꿀 수 있어서 아이디와
    다를 수 있다** — 그래서 한 번 발행해 실제 주소를 알게 되면 그것을 먼저 쓴다
    (``observed_blog_address``). NAVER_BLOG_ID는 사용자 없는 관리용 CLI에서만 쓰이며,
    API 사용자는 로컬로 격리된 상태를 유지한다.
    """
    profile_dir = naver_profile_dir(user_id)

    # 사용자 없는 관리 CLI도 평문 NAVER_ID/NAVER_PASSWORD로 되돌아가지 않는다. 같은
    # 프로필에 저장된 v2만 복호화하며, 없으면 Chrome에서 사람이 직접 로그인한다.
    if not user_id:
        from .credentials import load_credentials

        remembered = load_credentials(profile_dir)
        if remembered is not None:
            if not (username or "").strip():
                username = remembered.username
                password = password or remembered.password
            elif username.strip() == remembered.username and not password:
                password = remembered.password

    normalized_username = (username or "").strip()

    # 실제로 확인한 주소가 있으면 그것이 아이디보다 우선한다. 아이디로 만든 주소가
    # 막히는 계정이 있고, 그 사실은 발행해 봐야만 알 수 있다.
    blog_id = (
        (
            observed_blog_address(profile_dir, normalized_username)
            or normalized_username
            or _remembered_blog_id(profile_dir)
        )
        if user_id
        else (
            os.environ.get(BLOG_ID_ENV)
            or observed_blog_address(profile_dir, normalized_username)
            or normalized_username
            or _remembered_blog_id(profile_dir)
        )
    ).strip()
    if not blog_id:
        return None

    port = os.environ.get("PORT") or DEFAULT_PORT
    return NaverConfig(
        blog_id=blog_id,
        profile_dir=profile_dir,
        api_origin=f"http://localhost:{port}",
        username=normalized_username or None,
        password=password or None,
        user_id=user_id or None,
    )
