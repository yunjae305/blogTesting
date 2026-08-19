from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from app.posting import credential_crypto as crypto
from app.posting import credentials as credentials_module
from app.posting.credential_crypto import (
    AES_SCHEME,
    CREDENTIAL_FORMAT_VERSION,
    DPAPI_SCHEME,
    POSTING_CREDENTIALS_KEY_ENV,
    CredentialProtectionError,
    atomic_write_json,
    posting_credentials_key,
)
from app.posting.credentials import (
    CREDENTIALS_FILE,
    SESSION_ACCOUNT_FILE,
    NaverCredentials,
    load_credentials,
    remember_session_account,
    save_credentials,
    saved_username,
    session_account,
)
from scripts import migrate_posting_credentials as migration

TEST_KEY_BYTES = bytes(range(32))
TEST_KEY = base64.urlsafe_b64encode(TEST_KEY_BYTES).decode("ascii").rstrip("=")
OTHER_KEY = (
    base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode("ascii").rstrip("=")
)


@pytest.fixture(autouse=True)
def _isolated_key_environment(monkeypatch):
    monkeypatch.delenv(POSTING_CREDENTIALS_KEY_ENV, raising=False)


def _use_aes(monkeypatch, key: str = TEST_KEY) -> None:
    monkeypatch.setenv(POSTING_CREDENTIALS_KEY_ENV, key)


def _document(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _install_fake_dpapi(monkeypatch) -> None:
    def protect(secret: bytes, entropy: bytes | None) -> bytes:
        assert entropy is not None
        return hashlib.sha256(entropy).digest() + secret[::-1]

    def unprotect(payload: bytes, entropy: bytes | None) -> bytes:
        assert entropy is not None
        digest, protected = payload[:32], payload[32:]
        if digest != hashlib.sha256(entropy).digest():
            raise CredentialProtectionError("fake DPAPI entropy mismatch")
        return protected[::-1]

    monkeypatch.setattr(crypto, "_windows_available", lambda: True)
    monkeypatch.setattr(crypto, "_dpapi_protect", protect)
    monkeypatch.setattr(crypto, "_dpapi_unprotect", unprotect)


def test_key_must_be_canonical_base64url_for_exactly_32_bytes():
    assert (
        posting_credentials_key({POSTING_CREDENTIALS_KEY_ENV: TEST_KEY})
        == TEST_KEY_BYTES
    )
    assert (
        posting_credentials_key({POSTING_CREDENTIALS_KEY_ENV: f"{TEST_KEY}="})
        == TEST_KEY_BYTES
    )
    assert posting_credentials_key({POSTING_CREDENTIALS_KEY_ENV: ""}) is None
    assert (
        posting_credentials_key(
            {
                POSTING_CREDENTIALS_KEY_ENV: base64.urlsafe_b64encode(bytes(31)).decode(
                    "ascii"
                )
            }
        )
        is None
    )
    assert (
        posting_credentials_key(
            {
                POSTING_CREDENTIALS_KEY_ENV: base64.urlsafe_b64encode(bytes(33)).decode(
                    "ascii"
                )
            }
        )
        is None
    )
    assert (
        posting_credentials_key({POSTING_CREDENTIALS_KEY_ENV: f" {TEST_KEY}"}) is None
    )
    assert (
        posting_credentials_key({POSTING_CREDENTIALS_KEY_ENV: TEST_KEY[:-1] + "*"})
        is None
    )

    canonical_zero = base64.urlsafe_b64encode(bytes(32)).decode("ascii").rstrip("=")
    # The last two bits are padding.  Some decoders accept this alternate spelling
    # as the same bytes; configuration parsing deliberately does not.
    assert (
        posting_credentials_key(
            {POSTING_CREDENTIALS_KEY_ENV: canonical_zero[:-1] + "B"}
        )
        is None
    )


def test_aes_v2_encrypts_both_fields_and_round_trips(monkeypatch, tmp_path):
    _use_aes(monkeypatch)
    profile = tmp_path / "naver-profile-users" / "user-a"
    username = "account-that-must-not-appear"
    password = "password-that-must-not-appear!"

    save_credentials(profile, NaverCredentials(username, password))

    path = profile / CREDENTIALS_FILE
    document = _document(path)
    serialized = path.read_text(encoding="utf-8")
    assert document["version"] == CREDENTIAL_FORMAT_VERSION
    assert document["scheme"] == AES_SCHEME
    assert "usernameEnc" in document and "passwordEnc" in document
    assert "username" not in document and "password" not in document
    assert username not in serialized and password not in serialized
    assert load_credentials(profile) == NaverCredentials(username, password)
    assert saved_username(profile) == username


def test_aes_uses_fresh_nonce_for_every_field_and_write(monkeypatch, tmp_path):
    _use_aes(monkeypatch)
    profile = tmp_path / "threads-profile-users" / "user-a"
    value = NaverCredentials("same-account", "same-password")

    save_credentials(profile, value)
    first = _document(profile / CREDENTIALS_FILE)
    save_credentials(profile, value)
    second = _document(profile / CREDENTIALS_FILE)

    assert first["usernameEnc"] != first["passwordEnc"]
    assert first["usernameEnc"] != second["usernameEnc"]
    assert first["passwordEnc"] != second["passwordEnc"]


def test_field_swap_fails_authentication_without_logging_secrets(
    monkeypatch, tmp_path, caplog
):
    _use_aes(monkeypatch)
    profile = tmp_path / "naver-profile" / "user-a"
    username = "aad-user-secret"
    password = "aad-password-secret"
    save_credentials(profile, NaverCredentials(username, password))
    path = profile / CREDENTIALS_FILE
    document = _document(path)
    document["usernameEnc"], document["passwordEnc"] = (
        document["passwordEnc"],
        document["usernameEnc"],
    )
    atomic_write_json(path, document)

    assert load_credentials(profile) is None
    assert username not in caplog.text
    assert password not in caplog.text


@pytest.mark.parametrize("binding", ["user", "platform"])
def test_copying_ciphertext_to_another_scope_fails(monkeypatch, tmp_path, binding):
    _use_aes(monkeypatch)
    source = tmp_path / "naver-profile-users" / "user-a"
    target = (
        tmp_path / "naver-profile-users" / "user-b"
        if binding == "user"
        else tmp_path / "threads-profile-users" / "user-a"
    )
    save_credentials(source, NaverCredentials("scope-user", "scope-password"))
    target.mkdir(parents=True)
    shutil.copyfile(source / CREDENTIALS_FILE, target / CREDENTIALS_FILE)

    assert load_credentials(target) is None


def test_aes_file_is_portable_when_platform_and_user_scope_are_preserved(
    monkeypatch, tmp_path
):
    _use_aes(monkeypatch)
    source = tmp_path / "workstation" / "naver-profile-users" / "user-a"
    target = tmp_path / "windows-server" / "naver-profile-users" / "user-a"
    expected = NaverCredentials("portable-user", "portable-password")
    save_credentials(source, expected)
    target.mkdir(parents=True)
    shutil.copyfile(source / CREDENTIALS_FILE, target / CREDENTIALS_FILE)

    assert load_credentials(target) == expected


def test_wrong_portable_key_cannot_decrypt(monkeypatch, tmp_path):
    _use_aes(monkeypatch)
    profile = tmp_path / "naver-profile-users" / "user-a"
    save_credentials(profile, NaverCredentials("key-user", "key-password"))

    _use_aes(monkeypatch, OTHER_KEY)
    assert load_credentials(profile) is None


def test_non_windows_without_valid_key_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "_windows_available", lambda: False)
    profile = tmp_path / "naver-profile-users" / "user-a"

    with pytest.raises(CredentialProtectionError):
        save_credentials(profile, NaverCredentials("plain-user", "plain-password"))
    assert not (profile / CREDENTIALS_FILE).exists()

    remember_session_account(profile, "plain-session-user")
    assert not (profile / SESSION_ACCOUNT_FILE).exists()


def test_missing_key_falls_back_to_user_dpapi_on_windows(monkeypatch, tmp_path):
    _install_fake_dpapi(monkeypatch)
    profile = tmp_path / "threads-profile-users" / "user-a"
    username = "account-must-not-appear"
    password = "password-must-not-appear"

    save_credentials(profile, NaverCredentials(username, password))

    document = _document(profile / CREDENTIALS_FILE)
    serialized = json.dumps(document)
    assert document["scheme"] == DPAPI_SCHEME
    assert "usernameEnc" in document and "passwordEnc" in document
    assert username not in serialized and password not in serialized
    assert load_credentials(profile) == NaverCredentials(username, password)


def test_invalid_explicit_key_fails_closed_even_on_windows(monkeypatch, tmp_path):
    monkeypatch.setenv(POSTING_CREDENTIALS_KEY_ENV, "not-a-32-byte-key")
    _install_fake_dpapi(monkeypatch)
    profile = tmp_path / "naver-profile-users" / "user-a"

    with pytest.raises(CredentialProtectionError, match="configured but"):
        save_credentials(
            profile, NaverCredentials("invalid-key-user", "invalid-key-password")
        )
    assert not (profile / CREDENTIALS_FILE).exists()

    remember_session_account(profile, "invalid-key-session")
    assert not (profile / SESSION_ACCOUNT_FILE).exists()


def test_legacy_none_auto_migrates_atomically_to_aes(monkeypatch, tmp_path):
    _use_aes(monkeypatch)
    profile = tmp_path / "naver-profile-users" / "user-a"
    profile.mkdir(parents=True)
    username = "legacy-none-user"
    password = "legacy-none-password"
    legacy = {
        "username": username,
        "password": base64.b64encode(password.encode()).decode("ascii"),
        "encryption": "none",
    }
    (profile / CREDENTIALS_FILE).write_text(json.dumps(legacy), encoding="utf-8")

    assert load_credentials(profile) == NaverCredentials(username, password)
    migrated = _document(profile / CREDENTIALS_FILE)
    assert migrated["version"] == CREDENTIAL_FORMAT_VERSION
    assert migrated["scheme"] == AES_SCHEME
    assert username not in json.dumps(migrated) and password not in json.dumps(migrated)


def test_legacy_dpapi_auto_migrates_to_aes(monkeypatch, tmp_path):
    _use_aes(monkeypatch)
    profile = tmp_path / "threads-profile-users" / "user-a"
    profile.mkdir(parents=True)
    payload = b"legacy-dpapi-payload"
    legacy = {
        "username": "legacy-dpapi-user",
        "password": base64.b64encode(payload).decode("ascii"),
        "encryption": "dpapi",
    }
    (profile / CREDENTIALS_FILE).write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(
        credentials_module,
        "unprotect_legacy_dpapi",
        lambda candidate: "legacy-dpapi-password" if candidate == payload else "",
    )

    assert load_credentials(profile) == NaverCredentials(
        "legacy-dpapi-user", "legacy-dpapi-password"
    )
    assert _document(profile / CREDENTIALS_FILE)["scheme"] == AES_SCHEME


def test_legacy_plaintext_is_not_returned_if_secure_migration_is_impossible(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(crypto, "_windows_available", lambda: False)
    profile = tmp_path / "naver-profile-users" / "user-a"
    profile.mkdir(parents=True)
    path = profile / CREDENTIALS_FILE
    original = json.dumps(
        {
            "username": "legacy-user",
            "password": base64.b64encode(b"legacy-password").decode("ascii"),
            "encryption": "none",
        }
    )
    path.write_text(original, encoding="utf-8")

    assert load_credentials(profile) is None
    assert path.read_text(encoding="utf-8") == original


def test_legacy_secret_is_not_returned_if_atomic_migration_fails(monkeypatch, tmp_path):
    _use_aes(monkeypatch)
    profile = tmp_path / "naver-profile-users" / "user-a"
    profile.mkdir(parents=True)
    path = profile / CREDENTIALS_FILE
    original = json.dumps(
        {
            "username": "atomic-user",
            "password": base64.b64encode(b"atomic-password").decode("ascii"),
            "encryption": "none",
        }
    )
    path.write_text(original, encoding="utf-8")

    def fail_write(_path, _document):
        raise OSError("simulated migration failure")

    monkeypatch.setattr(credentials_module, "atomic_write_json", fail_write)
    assert load_credentials(profile) is None
    assert path.read_text(encoding="utf-8") == original


def test_v2_dpapi_rewraps_to_aes_when_portable_key_appears(monkeypatch, tmp_path):
    _install_fake_dpapi(monkeypatch)
    profile = tmp_path / "naver-profile-users" / "user-a"
    expected = NaverCredentials("rewrap-user", "rewrap-password")
    save_credentials(profile, expected)
    assert _document(profile / CREDENTIALS_FILE)["scheme"] == DPAPI_SCHEME

    _use_aes(monkeypatch)
    assert load_credentials(profile) == expected
    assert _document(profile / CREDENTIALS_FILE)["scheme"] == AES_SCHEME


def test_atomic_write_failure_preserves_previous_file(monkeypatch, tmp_path):
    path = tmp_path / "credentials.json"
    previous = b"previous-file-content"
    path.write_bytes(previous)

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        atomic_write_json(path, {"version": 2})

    assert path.read_bytes() == previous
    assert not tuple(tmp_path.glob(".credentials.json.*.tmp"))


def test_session_account_is_ciphertext_and_round_trips(monkeypatch, tmp_path):
    _use_aes(monkeypatch)
    profile = tmp_path / "threads-profile-users" / "user-a"
    username = "session-owner-must-not-appear"

    remember_session_account(profile, username)

    path = profile / SESSION_ACCOUNT_FILE
    document = _document(path)
    assert document["scheme"] == AES_SCHEME
    assert "accountEnc" in document and "account" not in document
    assert username not in path.read_text(encoding="utf-8")
    assert session_account(profile) == username


def test_blog_id_marker_is_ciphertext_and_legacy_plaintext_auto_migrates(
    monkeypatch, tmp_path
):
    from app.posting.config import BLOG_ID_FILE, _remembered_blog_id, remember_blog_id

    _use_aes(monkeypatch)
    profile = tmp_path / "naver-profile-users" / "user-a"
    profile.mkdir(parents=True)
    path = profile / BLOG_ID_FILE
    blog_id = "account-id-must-not-appear"
    path.write_text(blog_id, encoding="utf-8")

    assert _remembered_blog_id(profile) == blog_id
    assert _document(path)["scheme"] == AES_SCHEME
    assert blog_id not in path.read_text(encoding="utf-8")

    remember_blog_id(profile, "replacement-id")
    assert _remembered_blog_id(profile) == "replacement-id"
    assert "replacement-id" not in path.read_text(encoding="utf-8")


def test_legacy_plaintext_session_marker_auto_migrates(monkeypatch, tmp_path):
    _use_aes(monkeypatch)
    profile = tmp_path / "naver-profile-users" / "user-a"
    profile.mkdir(parents=True)
    path = profile / SESSION_ACCOUNT_FILE
    username = "legacy-session-owner"
    path.write_text(username, encoding="utf-8")

    assert session_account(profile) == username
    assert _document(path)["scheme"] == AES_SCHEME
    assert username not in path.read_text(encoding="utf-8")


def test_legacy_session_is_not_returned_without_protection(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "_windows_available", lambda: False)
    profile = tmp_path / "threads-profile-users" / "user-a"
    profile.mkdir(parents=True)
    path = profile / SESSION_ACCOUNT_FILE
    path.write_text("legacy-session-owner", encoding="utf-8")

    assert session_account(profile) is None
    assert path.read_text(encoding="utf-8") == "legacy-session-owner"


def test_migration_cli_is_secret_free_and_migrates_explicit_root(
    monkeypatch, tmp_path, capsys
):
    _use_aes(monkeypatch)
    profiles_root = tmp_path / "naver-profile-users"
    profile = profiles_root / "user-a"
    profile.mkdir(parents=True)
    username = "cli-user-must-not-print"
    password = "cli-password-must-not-print"
    credentials_path = profile / CREDENTIALS_FILE
    session_path = profile / SESSION_ACCOUNT_FILE
    blog_id_path = profile / "blog_id"
    credentials_path.write_text(
        json.dumps(
            {
                "username": username,
                "password": base64.b64encode(password.encode()).decode("ascii"),
                "encryption": "none",
            }
        ),
        encoding="utf-8",
    )
    session_path.write_text(username, encoding="utf-8")
    blog_id_path.write_text(username, encoding="utf-8")

    before_credentials = credentials_path.read_bytes()
    before_session = session_path.read_bytes()
    before_blog_id = blog_id_path.read_bytes()
    assert migration.main(["--root", str(profiles_root)]) == 0
    dry_output = capsys.readouterr()
    assert credentials_path.read_bytes() == before_credentials
    assert session_path.read_bytes() == before_session
    assert blog_id_path.read_bytes() == before_blog_id
    assert username not in dry_output.out and password not in dry_output.out
    assert str(profile) not in dry_output.out

    assert (
        migration.main(["--root", str(profiles_root), "--apply", "--require-aes"]) == 0
    )
    apply_output = capsys.readouterr()
    assert username not in apply_output.out and password not in apply_output.out
    assert str(profile) not in apply_output.out
    assert _document(credentials_path)["scheme"] == AES_SCHEME
    assert _document(session_path)["scheme"] == AES_SCHEME
    assert _document(blog_id_path)["scheme"] == AES_SCHEME


def test_migration_require_aes_rejects_invalid_key_without_changes(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv(POSTING_CREDENTIALS_KEY_ENV, "invalid")
    profile = tmp_path / "naver-profile-users" / "user-a"
    profile.mkdir(parents=True)
    path = profile / SESSION_ACCOUNT_FILE
    path.write_text("legacy-owner", encoding="utf-8")

    assert migration.main(["--root", str(profile), "--apply", "--require-aes"]) == 2
    output = capsys.readouterr()
    assert "legacy-owner" not in output.out + output.err
    assert path.read_text(encoding="utf-8") == "legacy-owner"


@pytest.mark.skipif(sys.platform != "win32", reason="real DPAPI is Windows-only")
def test_real_windows_user_scope_dpapi_round_trip(monkeypatch, tmp_path):
    profile = tmp_path / "naver-profile-users" / "user-a"
    expected = NaverCredentials("real-dpapi-user", "real-dpapi-password")

    save_credentials(profile, expected)

    document = _document(profile / CREDENTIALS_FILE)
    assert document["scheme"] == DPAPI_SCHEME
    assert load_credentials(profile) == expected


@pytest.mark.skipif(sys.platform != "win32", reason="legacy DPAPI is Windows-only")
def test_real_legacy_windows_dpapi_auto_migrates(monkeypatch, tmp_path):
    profile = tmp_path / "naver-profile-users" / "user-a"
    profile.mkdir(parents=True)
    username = "real-legacy-user"
    password = "real-legacy-password"
    payload = crypto._dpapi_protect(password.encode("utf-8"), None)
    path = profile / CREDENTIALS_FILE
    path.write_text(
        json.dumps(
            {
                "username": username,
                "password": base64.b64encode(payload).decode("ascii"),
                "encryption": "dpapi",
            }
        ),
        encoding="utf-8",
    )

    assert load_credentials(profile) == NaverCredentials(username, password)
    migrated = _document(path)
    assert migrated["scheme"] == DPAPI_SCHEME
    assert username not in json.dumps(migrated)
