"""Portable protection primitives for locally stored publishing credentials.

The file format deliberately keeps the platform and opaque Blog-it user scope in
clear text so they can be authenticated as associated data.  Account identifiers
and passwords are always ciphertext.  A deployment-provided 32-byte base64url key
uses AES-256-GCM and is portable between machines.  Windows can fall back to
current-user DPAPI when that key is absent, but other operating systems fail
closed instead of writing plaintext.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

POSTING_CREDENTIALS_KEY_ENV = "POSTING_CREDENTIALS_KEY"
CREDENTIAL_FORMAT_VERSION = 2
AES_SCHEME = "aes-256-gcm"
DPAPI_SCHEME = "dpapi-user"

_NONCE_BYTES = 12
_BASE64URL_KEY = re.compile(r"^[A-Za-z0-9_-]{43}=?$")
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class CredentialProtectionError(RuntimeError):
    """A credential cannot be protected or authenticated safely."""


@dataclass(frozen=True)
class CredentialScope:
    platform: str
    user_scope: str

    def aad(self, field: str) -> bytes:
        return (
            f"blog-it|posting-credential|v{CREDENTIAL_FORMAT_VERSION}|"
            f"{self.platform}|{self.user_scope}|{field}"
        ).encode()


def _decode_base64url(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise CredentialProtectionError("protected value is not base64url text")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError) as error:
        raise CredentialProtectionError(
            "protected value is not valid base64url"
        ) from error


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def posting_credentials_key(env: Mapping[str, str] | None = None) -> bytes | None:
    """Return the configured key only when it is canonical base64url for 32 bytes."""
    source = env if env is not None else os.environ
    configured = source.get(POSTING_CREDENTIALS_KEY_ENV, "")
    if not isinstance(configured, str) or not _BASE64URL_KEY.fullmatch(configured):
        return None
    try:
        decoded = _decode_base64url(configured)
    except CredentialProtectionError:
        return None
    if len(decoded) != 32:
        return None
    # Reject alternate encodings with non-zero unused padding bits.  A deployment
    # key has one canonical spelling (with an optional trailing ``=``), which
    # prevents visually different configuration values from naming the same key.
    return decoded if _encode_base64url(decoded) == configured.rstrip("=") else None


def has_portable_key(env: Mapping[str, str] | None = None) -> bool:
    return posting_credentials_key(env) is not None


def _configured_base_matches(profile_dir: Path, variable: str) -> bool:
    configured = (os.environ.get(variable) or "").strip()
    if not configured:
        return False
    base = Path(configured).expanduser().resolve()
    users_root = base.parent / f"{base.name}-users"
    return profile_dir == base or profile_dir.parent == users_root


def credential_scope(profile_dir: Path) -> CredentialScope:
    """Derive a portable platform/user binding from a profile directory.

    Normal profiles end in the stable SHA-256 scope created by config.py.  Hashing
    that final component once more avoids exposing even that identifier in metadata
    while keeping the AAD stable if the whole profile directory moves to a server.
    """
    resolved = profile_dir.expanduser().resolve()
    lowered_parts = tuple(part.lower() for part in resolved.parts)
    is_threads = any(
        "threads" in part for part in lowered_parts
    ) or _configured_base_matches(resolved, "THREADS_BROWSER_PROFILE_DIR")
    is_naver = any(
        "naver" in part for part in lowered_parts
    ) or _configured_base_matches(resolved, "NAVER_BROWSER_PROFILE_DIR")
    if is_threads and not is_naver:
        platform = "threads"
    elif is_naver and not is_threads:
        platform = "naver"
    else:
        # Tests and legacy management profiles may not carry a platform in their
        # path.  The value is still authenticated and cannot be changed afterward.
        platform = "posting"
    stable_component = resolved.name or "default"
    user_scope = hashlib.sha256(stable_component.encode("utf-8")).hexdigest()[:32]
    return CredentialScope(platform=platform, user_scope=user_scope)


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def active_scheme() -> str:
    configured = os.environ.get(POSTING_CREDENTIALS_KEY_ENV)
    if posting_credentials_key() is not None:
        return AES_SCHEME
    if configured not in (None, ""):
        # A typo in an explicitly supplied deployment key must never silently
        # create machine-bound DPAPI files that look ready for server migration.
        raise CredentialProtectionError(
            f"{POSTING_CREDENTIALS_KEY_ENV} is configured but is not canonical "
            "base64url for exactly 32 bytes"
        )
    if _windows_available():
        return DPAPI_SCHEME
    raise CredentialProtectionError(
        f"{POSTING_CREDENTIALS_KEY_ENV} must be base64url for exactly 32 bytes "
        "outside Windows"
    )


def protect_values(
    values: Mapping[str, str], scope: CredentialScope
) -> dict[str, object]:
    """Protect named fields into one authenticated v2 document."""
    scheme = active_scheme()
    document: dict[str, object] = {
        "version": CREDENTIAL_FORMAT_VERSION,
        "scheme": scheme,
        "platform": scope.platform,
        "userScope": scope.user_scope,
    }
    key = posting_credentials_key() if scheme == AES_SCHEME else None
    if key is not None:
        document["keyId"] = _key_id(key)

    for field, value in values.items():
        if not isinstance(value, str):
            raise CredentialProtectionError(f"{field} must be text")
        raw = value.encode("utf-8")
        if scheme == AES_SCHEME:
            assert key is not None
            nonce = os.urandom(_NONCE_BYTES)
            protected = nonce + AESGCM(key).encrypt(nonce, raw, scope.aad(field))
        else:
            protected = _dpapi_protect(raw, scope.aad(field))
        document[f"{field}Enc"] = _encode_base64url(protected)
    return document


def unprotect_values(
    document: Mapping[str, object], fields: tuple[str, ...], scope: CredentialScope
) -> dict[str, str]:
    """Authenticate metadata and decrypt all requested v2 fields."""
    if document.get("version") != CREDENTIAL_FORMAT_VERSION:
        raise CredentialProtectionError("unsupported credential format version")
    platform = document.get("platform")
    user_scope = document.get("userScope")
    if not isinstance(platform, str) or not hmac.compare_digest(
        platform, scope.platform
    ):
        raise CredentialProtectionError("credential platform binding does not match")
    if not isinstance(user_scope, str) or not hmac.compare_digest(
        user_scope, scope.user_scope
    ):
        raise CredentialProtectionError("credential user binding does not match")

    scheme = document.get("scheme")
    key: bytes | None = None
    if scheme == AES_SCHEME:
        key = posting_credentials_key()
        if key is None:
            raise CredentialProtectionError("portable credential key is unavailable")
        stored_key_id = document.get("keyId")
        if not isinstance(stored_key_id, str) or not hmac.compare_digest(
            stored_key_id, _key_id(key)
        ):
            raise CredentialProtectionError("portable credential key id does not match")
    elif scheme == DPAPI_SCHEME:
        if not _windows_available():
            raise CredentialProtectionError("Windows DPAPI credential is not portable")
    else:
        raise CredentialProtectionError("unsupported credential protection scheme")

    result: dict[str, str] = {}
    for field in fields:
        encoded = document.get(f"{field}Enc")
        if not isinstance(encoded, str):
            raise CredentialProtectionError(f"protected {field} is missing")
        payload = _decode_base64url(encoded)
        try:
            if scheme == AES_SCHEME:
                assert key is not None
                if len(payload) <= _NONCE_BYTES:
                    raise CredentialProtectionError(f"protected {field} is truncated")
                nonce, ciphertext = payload[:_NONCE_BYTES], payload[_NONCE_BYTES:]
                raw = AESGCM(key).decrypt(nonce, ciphertext, scope.aad(field))
            else:
                raw = _dpapi_unprotect(payload, scope.aad(field))
            result[field] = raw.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as error:
            raise CredentialProtectionError(
                f"protected {field} failed authentication"
            ) from error
    return result


def needs_rewrap(document: Mapping[str, object]) -> bool:
    """Whether a readable v2 DPAPI document can now become portable AES."""
    return (
        document.get("scheme") == DPAPI_SCHEME and posting_credentials_key() is not None
    )


def is_v2_document(document: object) -> bool:
    return (
        isinstance(document, dict)
        and document.get("version") == CREDENTIAL_FORMAT_VERSION
    )


def atomic_write_json(path: Path, document: Mapping[str, object]) -> None:
    """Replace a secret file atomically; a failed write leaves the old file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def unprotect_legacy_dpapi(payload: bytes) -> str:
    if not _windows_available():
        raise CredentialProtectionError(
            "legacy Windows DPAPI credential is not portable"
        )
    try:
        return _dpapi_unprotect(payload, None).decode("utf-8")
    except UnicodeDecodeError as error:
        raise CredentialProtectionError(
            "legacy DPAPI credential is not UTF-8"
        ) from error


def decode_legacy_plaintext(payload: str) -> str:
    """Read the historical `encryption=none` base64 value only for immediate migration."""
    try:
        return base64.b64decode(payload, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise CredentialProtectionError(
            "legacy plaintext credential is malformed"
        ) from error


def read_json(path: Path) -> dict[str, object]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CredentialProtectionError("credential file is unreadable") from error
    if not isinstance(parsed, dict):
        raise CredentialProtectionError("credential file is not a JSON object")
    return parsed


def _windows_available() -> bool:
    return sys.platform == "win32"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _input_blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, max(1, len(data)))
    blob = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _read_blob(blob: _Blob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi_protect(secret: bytes, entropy: bytes | None) -> bytes:
    if not _windows_available():
        raise CredentialProtectionError("Windows DPAPI is unavailable")
    source, source_buffer = _input_blob(secret)
    entropy_blob = entropy_buffer = None
    entropy_pointer = None
    if entropy is not None:
        entropy_blob, entropy_buffer = _input_blob(entropy)
        entropy_pointer = ctypes.byref(entropy_blob)
    output = _Blob()
    # Retain the backing buffers until CryptProtectData returns.
    _ = source_buffer, entropy_buffer
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source),
        None,
        entropy_pointer,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )
    if not ok:
        raise CredentialProtectionError("CryptProtectData failed")
    try:
        return _read_blob(output)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def _dpapi_unprotect(payload: bytes, entropy: bytes | None) -> bytes:
    if not _windows_available():
        raise CredentialProtectionError("Windows DPAPI is unavailable")
    source, source_buffer = _input_blob(payload)
    entropy_blob = entropy_buffer = None
    entropy_pointer = None
    if entropy is not None:
        entropy_blob, entropy_buffer = _input_blob(entropy)
        entropy_pointer = ctypes.byref(entropy_blob)
    output = _Blob()
    _ = source_buffer, entropy_buffer
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        entropy_pointer,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )
    if not ok:
        raise CredentialProtectionError("CryptUnprotectData failed")
    try:
        return _read_blob(output)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
