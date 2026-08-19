"""Crash-safe updates for local secret-bearing .env files.

An existing Windows file is replaced with ``ReplaceFileW`` rather than
``os.replace``.  Windows preserves the replaced file's DACL and other security
metadata with that API; silently falling back would risk broadening access.
"""

from __future__ import annotations

import ctypes
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from ctypes import wintypes
from pathlib import Path

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DACL_SECURITY_INFORMATION = 0x00000004
_UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000


def _validated_updates(updates: Mapping[str, str]) -> dict[str, str]:
    validated: dict[str, str] = {}
    for key, value in updates.items():
        if not isinstance(key, str) or not _ENV_KEY.fullmatch(key):
            raise ValueError("invalid environment variable name")
        if not isinstance(value, str) or any(character in value for character in "\r\n\0"):
            raise ValueError("environment variable values must be single-line text")
        validated[key] = value
    return validated


def _render_env(existing: str, updates: Mapping[str, str]) -> str:
    lines = existing.splitlines()
    written: set[str] = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[index] = f"{key}={updates[key]}"
            written.add(key)
    for key, value in updates.items():
        if key not in written:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def _replace_file_windows(target: Path, replacement: Path) -> None:
    """Atomically replace ``target`` while retaining its existing Windows DACL."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    replace_file.restype = wintypes.BOOL
    if not replace_file(str(target), str(replacement), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _copy_windows_dacl(source: Path, destination: Path) -> None:
    """Copy the existing secret file DACL before writing bytes to its temp file."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    get_security = advapi32.GetFileSecurityW
    get_security.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
    )
    get_security.restype = wintypes.BOOL
    set_security = advapi32.SetFileSecurityW
    set_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID)
    set_security.restype = wintypes.BOOL
    get_control = advapi32.GetSecurityDescriptorControl
    get_control.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        wintypes.LPDWORD,
    )
    get_control.restype = wintypes.BOOL

    required = wintypes.DWORD()
    get_security(
        str(source), _DACL_SECURITY_INFORMATION, None, 0, ctypes.byref(required)
    )
    if required.value == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(required.value)
    if not get_security(
        str(source),
        _DACL_SECURITY_INFORMATION,
        descriptor,
        required.value,
        ctypes.byref(required),
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
        raise ctypes.WinError(ctypes.get_last_error())
    protection = (
        _PROTECTED_DACL_SECURITY_INFORMATION
        if control.value & _SE_DACL_PROTECTED
        else _UNPROTECTED_DACL_SECURITY_INFORMATION
    )
    if not set_security(
        str(destination),
        _DACL_SECURITY_INFORMATION | protection,
        descriptor,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _replace_preserving_security(target: Path, replacement: Path) -> None:
    if os.name == "nt":
        if not target.exists():
            raise OSError("Windows environment file disappeared before DACL-preserving replace")
        # ReplaceFileW preserves the replaced file's DACL.  Do not fall back to
        # MoveFileEx/os.replace if ACL merge fails: keeping the old file is safer.
        _replace_file_windows(target, replacement)
        return
    os.replace(replacement, target)


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stat_snapshot(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def atomic_update_env(path: Path, updates: Mapping[str, str]) -> Path:
    """Update selected keys through a same-directory, flushed atomic replacement.

    Existing symlinks are refused.  Windows also refuses to create a new target:
    without an existing DACL there is no unambiguous ACL to preserve.
    """
    target = Path(path)
    validated = _validated_updates(updates)
    if target.is_symlink():
        raise OSError("refusing to replace a symlinked environment file")
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and not target.exists():
        raise OSError(
            "refusing to create a new Windows environment file; pre-create it "
            "with a restricted DACL"
        )
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    rendered = _render_env(existing, validated)
    previous_stat = target.stat() if target.exists() else None

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        if previous_stat is not None:
            if os.name == "nt":
                # mkstemp initially inherits the directory DACL.  Copy the
                # already-restricted target DACL before any secret bytes are written.
                _copy_windows_dacl(target, temporary)
            else:
                os.fchmod(descriptor, stat.S_IMODE(previous_stat.st_mode))
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = -1
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())

        if target.is_symlink():
            raise OSError("environment file became a symlink during update")
        if previous_stat is not None:
            current_stat = target.stat()
            if _stat_snapshot(current_stat) != _stat_snapshot(previous_stat):
                raise OSError("environment file changed during atomic update")
        _replace_preserving_security(target, temporary)
        _fsync_parent(target)
        return target
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
