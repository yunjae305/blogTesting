from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from scripts import _secure_env_write as secure_env


def test_atomic_update_replaces_and_appends_without_duplicate_secret_keys(tmp_path: Path):
    target = tmp_path / ".env"
    target.write_text("KEEP=value\nTOKEN=old\n", encoding="utf-8")

    result = secure_env.atomic_update_env(
        target, {"TOKEN": "new-secret", "ACCOUNT_ID": "opaque-id"}
    )

    assert result == target
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines == ["KEEP=value", "TOKEN=new-secret", "ACCOUNT_ID=opaque-id"]
    assert not list(tmp_path.glob("..env.*.tmp"))


def test_failed_replace_keeps_original_and_removes_same_directory_temp(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / ".env"
    target.write_text("TOKEN=old\n", encoding="utf-8")
    seen: list[Path] = []

    def fail_replace(_target: Path, replacement: Path) -> None:
        seen.append(replacement)
        assert replacement.parent == target.parent
        assert replacement.read_text(encoding="utf-8") == "TOKEN=new\n"
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(secure_env, "_replace_preserving_security", fail_replace)

    with pytest.raises(OSError, match="simulated"):
        secure_env.atomic_update_env(target, {"TOKEN": "new"})

    assert target.read_text(encoding="utf-8") == "TOKEN=old\n"
    assert len(seen) == 1
    assert not seen[0].exists()


@pytest.mark.parametrize("value", ["line1\nINJECTED=value", "line1\rline2", "nul\0byte"])
def test_multiline_or_nul_values_are_rejected_without_touching_file(
    tmp_path: Path, value: str
):
    target = tmp_path / ".env"
    target.write_text("TOKEN=old\n", encoding="utf-8")

    with pytest.raises(ValueError, match="single-line"):
        secure_env.atomic_update_env(target, {"TOKEN": value})

    assert target.read_text(encoding="utf-8") == "TOKEN=old\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL preservation contract")
def test_new_windows_env_file_is_refused_without_a_preexisting_dacl(tmp_path: Path):
    target = tmp_path / ".env"

    with pytest.raises(OSError, match="pre-create.*restricted DACL"):
        secure_env.atomic_update_env(target, {"TOKEN": "secret"})

    assert not target.exists()
    assert not list(tmp_path.glob("..env.*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL preservation contract")
def test_existing_windows_dacl_survives_replace_file(tmp_path: Path):
    target = tmp_path / ".env"
    target.write_text("TOKEN=old\n", encoding="utf-8")
    identity = subprocess.run(
        ["whoami"], capture_output=True, text=True, check=True
    ).stdout.strip()
    restricted = subprocess.run(
        ["icacls", str(target), "/inheritance:r", "/grant:r", f"{identity}:F"],
        capture_output=True,
        text=True,
    )
    if restricted.returncode != 0:
        pytest.skip("test account cannot set a temporary-file DACL")

    before = subprocess.run(
        ["icacls", str(target)], capture_output=True, check=True
    ).stdout
    secure_env.atomic_update_env(target, {"TOKEN": "new"})
    after = subprocess.run(
        ["icacls", str(target)], capture_output=True, check=True
    ).stdout

    assert after == before
    assert target.read_text(encoding="utf-8") == "TOKEN=new\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL preservation contract")
def test_windows_temp_file_gets_target_dacl_before_secret_write(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / ".env"
    target.write_text("TOKEN=old\n", encoding="utf-8")
    identity = subprocess.run(
        ["whoami"], capture_output=True, text=True, check=True
    ).stdout.strip()
    restricted = subprocess.run(
        ["icacls", str(target), "/inheritance:r", "/grant:r", f"{identity}:F"],
        capture_output=True,
        text=True,
    )
    if restricted.returncode != 0:
        pytest.skip("test account cannot set a temporary-file DACL")

    def normalized_acl(path: Path) -> str:
        output = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, check=True
        ).stdout
        return re.sub(r"\s+", " ", output.replace(str(path), "<FILE>", 1)).strip()

    expected = normalized_acl(target)
    original_replace = secure_env._replace_preserving_security

    def inspect_then_replace(destination: Path, temporary: Path) -> None:
        assert normalized_acl(temporary) == expected
        original_replace(destination, temporary)

    monkeypatch.setattr(
        secure_env, "_replace_preserving_security", inspect_then_replace
    )

    secure_env.atomic_update_env(target, {"TOKEN": "new"})

    assert target.read_text(encoding="utf-8") == "TOKEN=new\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL preservation contract")
def test_windows_dacl_copy_failure_occurs_before_any_secret_bytes(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / ".env"
    target.write_text("TOKEN=old\n", encoding="utf-8")
    seen: list[Path] = []

    def fail_copy(_source: Path, temporary: Path) -> None:
        seen.append(temporary)
        assert temporary.read_bytes() == b""
        raise OSError("simulated DACL copy failure")

    monkeypatch.setattr(secure_env, "_copy_windows_dacl", fail_copy)

    with pytest.raises(OSError, match="DACL copy"):
        secure_env.atomic_update_env(target, {"TOKEN": "new-secret"})

    assert target.read_text(encoding="utf-8") == "TOKEN=old\n"
    assert len(seen) == 1
    assert not seen[0].exists()


# threads_setup.py도 이 helper를 썼지만 2026-08-10에 API 발행 경로와 함께 제거됐다.
@pytest.mark.parametrize(
    "module_name,updates",
    [
        (
            "instagram_setup",
            {
                "FACEBOOK_USER_ACCESS_TOKEN": "secret",
                "INSTAGRAM_BUSINESS_ACCOUNT_ID": "account",
            },
        ),
    ],
)
def test_setup_scripts_delegate_secret_writes_to_atomic_helper(
    monkeypatch, module_name: str, updates: dict[str, str]
):
    module = __import__(f"scripts.{module_name}", fromlist=[module_name])
    captured: dict = {}

    def fake_atomic(path: Path, provided: dict[str, str]) -> Path:
        captured.update(provided)
        return path

    monkeypatch.setattr(module, "atomic_update_env", fake_atomic)

    result = module.write_env("secret", "account")

    assert result.name == ".env"
    assert captured == updates
