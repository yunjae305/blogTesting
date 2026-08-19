"""Store Naver/Threads login material outside the shared database.

Version 2 encrypts both the account identifier and password.  A portable
``POSTING_CREDENTIALS_KEY`` uses AES-256-GCM; Windows without that key uses
current-user DPAPI.  Other platforms refuse to write.  Historical DPAPI and
``encryption=none`` files are read only long enough to atomically migrate them.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path

from .credential_crypto import (
    CredentialProtectionError,
    atomic_write_json,
    credential_scope,
    decode_legacy_plaintext,
    is_v2_document,
    needs_rewrap,
    protect_values,
    read_json,
    unprotect_legacy_dpapi,
    unprotect_values,
)

logger = logging.getLogger(__name__)

CREDENTIALS_FILE = "credentials.json"
SESSION_ACCOUNT_FILE = "session_account"


@dataclass
class NaverCredentials:
    # Kept for API compatibility: Threads uses the same two-field credential type.
    username: str
    password: str


def _save_credentials_v2(profile_dir: Path, credentials: NaverCredentials) -> None:
    scope = credential_scope(profile_dir)
    document = protect_values(
        {"username": credentials.username, "password": credentials.password}, scope
    )
    atomic_write_json(profile_dir / CREDENTIALS_FILE, document)


def save_credentials(profile_dir: Path, credentials: NaverCredentials) -> None:
    """Write only authenticated v2 ciphertext; never fall back to plaintext."""
    if not credentials.username or not credentials.password:
        raise CredentialProtectionError("username and password must not be empty")
    _save_credentials_v2(profile_dir, credentials)


def _legacy_credentials(stored: dict[str, object]) -> NaverCredentials:
    username = stored.get("username")
    encoded_password = stored.get("password")
    encryption = stored.get("encryption")
    if not isinstance(username, str) or not username:
        raise CredentialProtectionError("legacy username is missing")
    if not isinstance(encoded_password, str) or not encoded_password:
        raise CredentialProtectionError("legacy password is missing")

    if encryption == "dpapi":
        try:
            payload = base64.b64decode(encoded_password, validate=True)
        except ValueError as error:
            raise CredentialProtectionError(
                "legacy DPAPI value is not base64"
            ) from error
        password = unprotect_legacy_dpapi(payload)
    elif encryption == "none":
        # This compatibility branch must be followed by a successful v2 write.
        password = decode_legacy_plaintext(encoded_password)
    else:
        raise CredentialProtectionError("unsupported legacy credential encryption")
    return NaverCredentials(username=username, password=password)


def load_credentials(profile_dir: Path) -> NaverCredentials | None:
    path = profile_dir / CREDENTIALS_FILE
    if not path.is_file():
        return None

    try:
        stored = read_json(path)
        if is_v2_document(stored):
            values = unprotect_values(
                stored, ("username", "password"), credential_scope(profile_dir)
            )
            credentials = NaverCredentials(
                username=values["username"], password=values["password"]
            )
            if needs_rewrap(stored):
                _save_credentials_v2(profile_dir, credentials)
            return credentials

        # Legacy plaintext is never returned unless the atomic v2 migration succeeds.
        credentials = _legacy_credentials(stored)
        _save_credentials_v2(profile_dir, credentials)
        return credentials
    except (CredentialProtectionError, OSError, ValueError) as error:
        # Do not include identifiers, ciphertext, or exception repr from crypto libraries.
        logger.warning(
            "저장된 게시 계정 로그인 정보를 안전하게 읽거나 마이그레이션하지 못했습니다 "
            "(%s). 다시 저장해 주세요.",
            type(error).__name__,
        )
        return None


def saved_username(profile_dir: Path) -> str | None:
    """Return the identifier only after decrypting the protected credential file."""
    credentials = load_credentials(profile_dir)
    return credentials.username if credentials is not None else None


def _save_session_account_v2(profile_dir: Path, username: str) -> None:
    scope = credential_scope(profile_dir)
    document = protect_values({"account": username}, scope)
    atomic_write_json(profile_dir / SESSION_ACCOUNT_FILE, document)


def session_account(profile_dir: Path) -> str | None:
    """Read the account bound to the browser session and migrate old plaintext markers."""
    path = profile_dir / SESSION_ACCOUNT_FILE
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        stripped = raw.strip()
        if not stripped:
            return None

        # A v2 marker is JSON.  Historical markers were a single plaintext line.
        if stripped.startswith("{"):
            document = read_json(path)
            if not is_v2_document(document):
                raise CredentialProtectionError("unsupported session marker version")
            values = unprotect_values(
                document, ("account",), credential_scope(profile_dir)
            )
            account = values["account"].strip()
            if not account:
                return None
            if needs_rewrap(document):
                _save_session_account_v2(profile_dir, account)
            return account

        # Never keep using a readable legacy marker unless replacement succeeded.
        _save_session_account_v2(profile_dir, stripped)
        return stripped
    except (
        CredentialProtectionError,
        OSError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        logger.warning(
            "게시 브라우저 세션 계정 기록을 안전하게 읽거나 마이그레이션하지 못했습니다 "
            "(%s).",
            type(error).__name__,
        )
        return None


def remember_session_account(profile_dir: Path, username: str | None) -> None:
    """Persist the browser-session owner as ciphertext, never as an identifier line."""
    normalized = (username or "").strip()
    if not normalized:
        forget_session_account(profile_dir)
        return
    try:
        _save_session_account_v2(profile_dir, normalized)
    except (CredentialProtectionError, OSError, ValueError) as error:
        # Publishing may continue, but the next account switch will conservatively log in again.
        logger.warning(
            "게시 브라우저 세션 계정 기록을 암호화해 저장하지 못했습니다 (%s).",
            type(error).__name__,
        )


def forget_session_account(profile_dir: Path) -> None:
    try:
        (profile_dir / SESSION_ACCOUNT_FILE).unlink()
    except OSError:
        pass
