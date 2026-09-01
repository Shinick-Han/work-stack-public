from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.migration import execute_v3_migration
from workstack.storage.reader import read_v4
from workstack.storage.v4_backup import (
    V4_BACKUP_MANIFEST,
    V4BackupError,
    create_v4_backup,
    restore_v4_backup,
    verify_v4_backup,
    write_v4_backup,
)


FIXTURE = Path(__file__).parent / "fixtures" / "store-v3" / "populated"
CREATED_AT = "2026-09-01T12:00:00Z"


def _members(body: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(body), "r") as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def _zip(members: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in members:
            archive.writestr(name, body)
    return output.getvalue()


def _replace_manifest(body: bytes, mutate) -> bytes:
    members = _members(body)
    manifest = json.loads(members[V4_BACKUP_MANIFEST].decode("utf-8"))
    mutate(manifest)
    members[V4_BACKUP_MANIFEST] = canonical_json_bytes(manifest)
    return _zip(sorted(members.items()))


class StorageV4BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)
        source = self.parent / "v3-source"
        shutil.copytree(FIXTURE, source)
        execution = execute_v3_migration(source, candidate_created_at=CREATED_AT)
        self.candidate = execution.preview.paths.candidate_root

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_backup_is_deterministic_content_free_and_excludes_runtime_state(self) -> None:
        (self.candidate / "index.sqlite").write_bytes(b"disposable index")
        (self.candidate / "journal-v2.json").write_bytes(b"disposable journal")
        (self.candidate / "idempotency-ledger.json").write_bytes(b"runtime ledger")

        first = create_v4_backup(self.candidate)
        second = create_v4_backup(self.candidate)

        self.assertEqual(first.body, second.body)
        self.assertEqual(first.digest, second.digest)
        members = _members(first.body)
        self.assertIn(V4_BACKUP_MANIFEST, members)
        self.assertFalse(any("migrations/" in name for name in members))
        self.assertNotIn("index.sqlite", members)
        self.assertNotIn("journal-v2.json", members)
        self.assertNotIn("idempotency-ledger.json", members)
        self.assertNotIn(b"Stabilize notification retries", members[V4_BACKUP_MANIFEST])
        with zipfile.ZipFile(io.BytesIO(first.body), "r") as archive:
            self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))

    def test_restore_to_new_or_empty_destination_revalidates_exact_authority(self) -> None:
        backup = create_v4_backup(self.candidate)
        destination = self.parent / "restored"

        receipt = restore_v4_backup(backup.body, destination)

        restored = read_v4(destination)
        self.assertEqual(build_v4_manifest(restored).digest, backup.authority_digest)
        self.assertEqual(receipt.authority_digest, backup.authority_digest)
        self.assertEqual(receipt.workspace_uid, backup.workspace_uid)
        self.assertFalse((destination / "migrations").exists())

        empty = self.parent / "empty-existing"
        empty.mkdir()
        second = restore_v4_backup(backup.body, empty)
        self.assertEqual(build_v4_manifest(read_v4(empty)).digest, second.authority_digest)

    def test_write_and_verify_archive_are_exclusive_and_non_mutating(self) -> None:
        destination = self.parent / "backup.zip"
        artifact = write_v4_backup(self.candidate, destination)
        verified = verify_v4_backup(destination)

        self.assertEqual(artifact.digest, verified.digest)
        self.assertEqual(artifact.authority_digest, verified.authority_digest)
        with self.assertRaisesRegex(V4BackupError, "BACKUP_DESTINATION_EXISTS"):
            write_v4_backup(self.candidate, destination)

    def test_restore_refuses_nonempty_destination_without_touching_it(self) -> None:
        backup = create_v4_backup(self.candidate)
        destination = self.parent / "occupied"
        destination.mkdir()
        marker = destination / "keep.txt"
        marker.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(V4BackupError, "RESTORE_DESTINATION_NOT_EMPTY"):
            restore_v4_backup(backup.body, destination)

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_traversal_duplicate_and_mixed_archives_are_rejected(self) -> None:
        backup = create_v4_backup(self.candidate)
        members = sorted(_members(backup.body).items())
        traversal = _zip(members + [("../escape.json", b"{}")])
        mixed = _zip(members + [("backlog.json", b"{}")])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            duplicate = _zip(members + [members[0]])

        for body in (traversal, mixed, duplicate):
            with self.subTest(size=len(body)):
                with self.assertRaises(V4BackupError):
                    verify_v4_backup(body)
        self.assertFalse((self.parent / "escape.json").exists())

    def test_future_manifest_and_tampered_authority_are_rejected(self) -> None:
        backup = create_v4_backup(self.candidate)
        future = _replace_manifest(
            backup.body, lambda value: value.update(schema_version=2)
        )
        members = _members(backup.body)
        members["store.json"] = members["store.json"] + b" "
        tampered = _zip(sorted(members.items()))

        with self.assertRaisesRegex(V4BackupError, "BACKUP_MANIFEST_UNSUPPORTED"):
            verify_v4_backup(future)
        with self.assertRaisesRegex(V4BackupError, "BACKUP_FILE_MISMATCH"):
            verify_v4_backup(tampered)


if __name__ == "__main__":
    unittest.main()
