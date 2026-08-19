"""Inspect or migrate local Naver/Threads credential files to protected v2.

The default mode is read-only and reports aggregate counts only.  It never prints
profile paths, account identifiers, passwords, ciphertext, or key material.

Run from the repository root::

    python apps/api/scripts/migrate_posting_credentials.py
    python apps/api/scripts/migrate_posting_credentials.py --apply --require-aes

For a Windows Server move, set ``POSTING_CREDENTIALS_KEY`` to canonical
base64url for exactly 32 random bytes on both machines, then use the second
command before copying the profile directories.  ``--require-aes`` refuses to
leave machine/user-bound DPAPI files behind.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(API_ROOT))

from app.config import load_env_file
from app.posting.credential_crypto import (
    AES_SCHEME,
    CREDENTIAL_FORMAT_VERSION,
    DPAPI_SCHEME,
    POSTING_CREDENTIALS_KEY_ENV,
    has_portable_key,
)
from app.posting.config import BLOG_ID_FILE, _remembered_blog_id
from app.posting.credentials import (
    CREDENTIALS_FILE,
    SESSION_ACCOUNT_FILE,
    load_credentials,
    session_account,
)


@dataclass(frozen=True)
class MigrationResult:
    discovered_profiles: int
    credentials: Counter[str]
    sessions: Counter[str]
    blog_ids: Counter[str]
    migrated_credentials: int = 0
    migrated_sessions: int = 0
    migrated_blog_ids: int = 0
    failures: int = 0


def _platform_bases() -> tuple[Path, Path]:
    naver = (os.environ.get("NAVER_BROWSER_PROFILE_DIR") or "").strip()
    threads = (os.environ.get("THREADS_BROWSER_PROFILE_DIR") or "").strip()
    return (
        Path(naver).expanduser().resolve()
        if naver
        else REPOSITORY_ROOT / ".naver-profile",
        Path(threads).expanduser().resolve()
        if threads
        else REPOSITORY_ROOT / ".threads-profile",
    )


def discover_profiles(explicit_roots: Iterable[Path] = ()) -> tuple[Path, ...]:
    """Find only profile directories that already contain a managed secret file."""
    supplied = tuple(Path(root).expanduser().resolve() for root in explicit_roots)
    bases = supplied or _platform_bases()
    candidates: set[Path] = set()

    for base in bases:
        candidates.add(base)
        if not supplied:
            users_root = base.parent / f"{base.name}-users"
            try:
                candidates.update(
                    path for path in users_root.iterdir() if path.is_dir()
                )
            except OSError:
                pass
        elif base.is_dir():
            # An explicit root may be either one profile or a directory containing
            # profile directories.  Limit discovery to one level to avoid walking
            # browser caches and cookie stores.
            try:
                candidates.update(path for path in base.iterdir() if path.is_dir())
            except OSError:
                pass

    managed = {
        candidate
        for candidate in candidates
        if (candidate / CREDENTIALS_FILE).is_file()
        or (candidate / SESSION_ACCOUNT_FILE).is_file()
        or (candidate / BLOG_ID_FILE).is_file()
    }
    return tuple(sorted(managed, key=lambda path: str(path).casefold()))


def _classify_json(path: Path, *, session: bool) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    stripped = raw.strip()
    if not stripped:
        return "empty"
    if session and not stripped.startswith("{"):
        return "legacy-plaintext"
    try:
        document = json.loads(stripped)
    except json.JSONDecodeError:
        return "malformed"
    if not isinstance(document, dict):
        return "malformed"
    if document.get("version") == CREDENTIAL_FORMAT_VERSION:
        scheme = document.get("scheme")
        if scheme == AES_SCHEME:
            return "v2-aes"
        if scheme == DPAPI_SCHEME:
            return "v2-dpapi"
        return "v2-unsupported"
    if session:
        return "unsupported"
    encryption = document.get("encryption")
    if encryption == "dpapi":
        return "legacy-dpapi"
    if encryption == "none":
        return "legacy-none"
    return "unsupported"


def inspect_profiles(profiles: Sequence[Path]) -> MigrationResult:
    credential_counts: Counter[str] = Counter()
    session_counts: Counter[str] = Counter()
    blog_id_counts: Counter[str] = Counter()
    for profile in profiles:
        credentials_path = profile / CREDENTIALS_FILE
        session_path = profile / SESSION_ACCOUNT_FILE
        blog_id_path = profile / BLOG_ID_FILE
        if credentials_path.is_file():
            credential_counts[_classify_json(credentials_path, session=False)] += 1
        if session_path.is_file():
            session_counts[_classify_json(session_path, session=True)] += 1
        if blog_id_path.is_file():
            blog_id_counts[_classify_json(blog_id_path, session=True)] += 1
    return MigrationResult(
        discovered_profiles=len(profiles),
        credentials=credential_counts,
        sessions=session_counts,
        blog_ids=blog_id_counts,
    )


def _is_required_v2(path: Path, require_aes: bool) -> bool:
    classification = _classify_json(path, session=path.name == SESSION_ACCOUNT_FILE)
    return (
        classification == "v2-aes"
        if require_aes
        else classification in {"v2-aes", "v2-dpapi"}
    )


def migrate_profiles(
    profiles: Sequence[Path], *, require_aes: bool = False
) -> MigrationResult:
    """Migrate readable files and report counts without disclosing their owners."""
    before = inspect_profiles(profiles)
    migrated_credentials = 0
    migrated_sessions = 0
    migrated_blog_ids = 0
    failures = 0

    for profile in profiles:
        credentials_path = profile / CREDENTIALS_FILE
        if credentials_path.is_file():
            original = _classify_json(credentials_path, session=False)
            credentials = load_credentials(profile)
            if credentials is None or not _is_required_v2(
                credentials_path, require_aes
            ):
                failures += 1
            elif original != _classify_json(credentials_path, session=False):
                migrated_credentials += 1

        session_path = profile / SESSION_ACCOUNT_FILE
        if session_path.is_file():
            original = _classify_json(session_path, session=True)
            account = session_account(profile)
            if account is None or not _is_required_v2(session_path, require_aes):
                failures += 1
            elif original != _classify_json(session_path, session=True):
                migrated_sessions += 1

        blog_id_path = profile / BLOG_ID_FILE
        if blog_id_path.is_file():
            original = _classify_json(blog_id_path, session=True)
            blog_id = _remembered_blog_id(profile)
            if not blog_id or not _is_required_v2(blog_id_path, require_aes):
                failures += 1
            elif original != _classify_json(blog_id_path, session=True):
                migrated_blog_ids += 1

    return MigrationResult(
        discovered_profiles=before.discovered_profiles,
        credentials=before.credentials,
        sessions=before.sessions,
        blog_ids=before.blog_ids,
        migrated_credentials=migrated_credentials,
        migrated_sessions=migrated_sessions,
        migrated_blog_ids=migrated_blog_ids,
        failures=failures,
    )


def _counts_text(counts: Counter[str]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{name}={counts[name]}" for name in sorted(counts))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or atomically migrate posting credentials without printing secrets."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="decrypt readable legacy/v2 files and atomically rewrite them with active protection",
    )
    parser.add_argument(
        "--require-aes",
        action="store_true",
        help=f"require a valid {POSTING_CREDENTIALS_KEY_ENV} and verify every migrated file is AES",
    )
    parser.add_argument(
        "--root",
        action="append",
        type=Path,
        default=[],
        help="explicit profile or one-level profile root (repeatable)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_env_file()
    if args.require_aes and not has_portable_key():
        print(
            f"ERROR: {POSTING_CREDENTIALS_KEY_ENV} must be canonical base64url for exactly "
            "32 bytes; no files were changed.",
            file=sys.stderr,
        )
        return 2

    profiles = discover_profiles(args.root)
    result = (
        migrate_profiles(profiles, require_aes=args.require_aes)
        if args.apply
        else inspect_profiles(profiles)
    )
    mode = "apply" if args.apply else "dry-run"
    print(f"mode={mode} profiles={result.discovered_profiles}")
    print(f"credentials: {_counts_text(result.credentials)}")
    print(f"sessions: {_counts_text(result.sessions)}")
    print(f"blog_ids: {_counts_text(result.blog_ids)}")
    if args.apply:
        print(
            f"migrated_credentials={result.migrated_credentials} "
            f"migrated_sessions={result.migrated_sessions} "
            f"migrated_blog_ids={result.migrated_blog_ids} failures={result.failures}"
        )
    elif result.discovered_profiles:
        print(
            "No files changed. Re-run with --apply after reviewing these aggregate counts."
        )
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
