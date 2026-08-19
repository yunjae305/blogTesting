from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

import pytest

from app.modules.auth.email_crypto import EmailCipher
from scripts import migrate_email_encryption as migration


@dataclass
class _WriteResult:
    matched_count: int
    modified_count: int


class _Collection:
    def __init__(self, documents: list[dict], *, suppress_modified: bool = False):
        self.documents = {document["_id"]: deepcopy(document) for document in documents}
        self.suppress_modified = suppress_modified
        self.unset_calls = 0

    async def update_one(self, query: dict, update: dict) -> _WriteResult:
        document = self.documents.get(query.get("_id"))
        if document is None or any(document.get(key) != value for key, value in query.items()):
            return _WriteResult(0, 0)
        before = deepcopy(document)
        document.update(update.get("$set", {}))
        if "$unset" in update:
            self.unset_calls += 1
            for key in update["$unset"]:
                document.pop(key, None)
        modified = int(document != before)
        if self.suppress_modified:
            modified = 0
        return _WriteResult(1, modified)

    async def find_one(self, query: dict, _projection: dict) -> dict | None:
        document = self.documents.get(query.get("_id"))
        return deepcopy(document) if document is not None else None

    async def count_documents(self, query: dict) -> int:
        count = 0
        for document in self.documents.values():
            email_rule = query.get("email")
            if email_rule == {"$exists": True} and "email" not in document:
                continue
            excluded = query.get("_id", {}).get("$nin", [])
            if document.get("_id") in excluded:
                continue
            count += 1
        return count


@pytest.fixture
def cipher() -> EmailCipher:
    return EmailCipher(b"e" * 32, b"blind-index-test-key")


def _documents() -> list[dict]:
    return [
        {"_id": "doc-1", "userId": "user-1", "email": "One@Example.com"},
        {"_id": "doc-2", "userId": "user-2", "email": "two@example.com"},
    ]


async def test_apply_requires_an_explicit_offline_maintenance_confirmation():
    with pytest.raises(migration.MigrationSafetyError, match="maintenance-confirm"):
        await migration.migrate(apply=True)


async def test_snapshot_email_is_part_of_the_staging_cas(cipher):
    collection = _Collection(_documents())
    plans = migration._build_plans(cipher, _documents())
    collection.documents["doc-1"]["email"] = "changed@example.com"

    with pytest.raises(migration.MigrationSafetyError, match="스냅샷 이후 변경"):
        await migration._stage_encrypted_fields(collection, plans)

    assert collection.unset_calls == 0
    assert collection.documents["doc-1"]["email"] == "changed@example.com"


async def test_modified_count_is_checked_before_plaintext_can_be_removed(cipher):
    collection = _Collection(_documents(), suppress_modified=True)
    plans = migration._build_plans(cipher, _documents())

    with pytest.raises(migration.MigrationSafetyError, match="암호문 기록"):
        await migration._stage_encrypted_fields(collection, plans)

    assert collection.unset_calls == 0
    assert all("email" in document for document in collection.documents.values())


async def test_exact_ciphertext_mismatch_stops_before_any_plaintext_delete(cipher):
    collection = _Collection(_documents())
    plans = migration._build_plans(cipher, _documents())
    await migration._stage_encrypted_fields(collection, plans)
    collection.documents["doc-2"]["emailEnc"] = "v2:tampered"

    with pytest.raises(migration.MigrationSafetyError, match="계획과 달라"):
        await migration._verify_staged_documents(collection, cipher, plans)

    assert collection.unset_calls == 0
    assert all("email" in document for document in collection.documents.values())


async def test_new_plaintext_document_after_snapshot_stops_before_delete(cipher):
    collection = _Collection(_documents())
    plans = migration._build_plans(cipher, _documents())
    await migration._stage_encrypted_fields(collection, plans)
    collection.documents["doc-new"] = {
        "_id": "doc-new",
        "userId": "user-new",
        "email": "new@example.com",
    }

    with pytest.raises(migration.MigrationSafetyError, match="새 평문 이메일"):
        await migration._verify_staged_documents(collection, cipher, plans)

    assert collection.unset_calls == 0
    assert all("email" in document for document in collection.documents.values())


async def test_verified_rows_are_unset_only_with_the_exact_final_cas(cipher):
    collection = _Collection(_documents())
    plans = migration._build_plans(cipher, _documents())
    await migration._stage_encrypted_fields(collection, plans)
    await migration._verify_staged_documents(collection, cipher, plans)

    removed = await migration._unset_verified_plaintext(collection, plans)

    assert removed == 2
    assert collection.unset_calls == 2
    for plan in plans:
        document = collection.documents[plan.document_id]
        assert "email" not in document
        assert document["emailHash"] == plan.email_hash
        assert document["emailEnc"] == plan.email_enc
        assert cipher.decrypt(document["emailEnc"], context=plan.user_id) == plan.normalized_email


async def test_final_cas_mismatch_keeps_the_changed_row_plaintext(cipher):
    collection = _Collection(_documents())
    plans = migration._build_plans(cipher, _documents())
    await migration._stage_encrypted_fields(collection, plans)
    await migration._verify_staged_documents(collection, cipher, plans)
    collection.documents["doc-1"]["email"] = "raced@example.com"

    with pytest.raises(migration.MigrationSafetyError, match="평문을 지우지 않고"):
        await migration._unset_verified_plaintext(collection, plans)

    assert collection.unset_calls == 0
    assert collection.documents["doc-1"]["email"] == "raced@example.com"
