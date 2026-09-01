from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from workstack.storage.migration_source import (
    V3_SOURCE_BACKUP_MANIFEST,
    V3_SOURCE_FILES,
    V3MigrationSourceError,
    V3SourceLimits,
    create_verified_v3_backup,
    freeze_v3_source,
    verify_v3_backup,
    verify_v3_source_unchanged,
)


FIXTURE = Path(__file__).parent / "fixtures" / "store-v3" / "populated"


class V3MigrationSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "source"
        shutil.copytree(FIXTURE, self.root)
        self.output_root = Path(self.temporary.name) / "output"
        self.output_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def source_bytes(self) -> dict[str, bytes]:
        return {name: (self.root / name).read_bytes() for name in V3_SOURCE_FILES}

    def create_backup(self):
        frozen = freeze_v3_source(self.root)
        return create_verified_v3_backup(
            frozen,
            staging_path=self.output_root / "candidate.staging",
            output_path=self.output_root / "source.zip",
        )

    def assert_error(self, code: str, action) -> None:
        with self.assertRaises(V3MigrationSourceError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_freeze_is_deterministic_bounded_and_does_not_modify_source(self) -> None:
        before = self.source_bytes()

        first = freeze_v3_source(self.root)
        second = freeze_v3_source(self.root)

        self.assertEqual(first.artifacts, second.artifacts)
        self.assertEqual(first.aggregate_digest, second.aggregate_digest)
        self.assertRegex(first.aggregate_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(before, self.source_bytes())

    def test_backup_is_byte_deterministic_verified_and_source_stays_unchanged(self) -> None:
        before = self.source_bytes()
        first = self.create_backup()
        first_body = first.path.read_bytes()
        first.path.unlink()

        second = self.create_backup()
        verified = verify_v3_backup(second.path)

        self.assertEqual(first_body, second.path.read_bytes())
        self.assertEqual(verified.aggregate_digest, second.aggregate_digest)
        self.assertEqual(verified.archive_digest, second.archive_digest)
        self.assertFalse(second.staging_path.exists())
        self.assertEqual(before, self.source_bytes())

    def test_missing_extra_link_and_oversized_sources_fail_without_writes(self) -> None:
        before = self.source_bytes()
        (self.root / "notes.json").unlink()
        self.assert_error("SOURCE_FILE_MISSING", lambda: freeze_v3_source(self.root))
        (self.root / "notes.json").write_bytes(before["notes.json"])
        (self.root / "other.json").write_text("{}", encoding="utf-8")
        self.assert_error("EXTRA_AUTHORITATIVE_FILE", lambda: freeze_v3_source(self.root))
        (self.root / "other.json").unlink()
        limit = len((self.root / "activity.json").read_bytes()) - 1
        self.assert_error(
            "SOURCE_FILE_TOO_LARGE",
            lambda: freeze_v3_source(
                self.root, limits=V3SourceLimits(max_file_bytes=limit)
            ),
        )
        self.assertEqual(before, self.source_bytes())
        self.assertEqual(tuple(self.output_root.iterdir()), ())

        link = self.root / "untrusted-link"
        try:
            link.symlink_to(self.root / "notes.json")
        except OSError:
            self.skipTest("symbolic links require OS privileges")
        self.assert_error("LINK_REJECTED", lambda: freeze_v3_source(self.root))

    def test_changed_during_second_pass_fails_closed_and_preserves_source(self) -> None:
        before = self.source_bytes()
        original = __import__(
            "workstack.storage.migration_source", fromlist=["_read_source_pass"]
        )._read_source_pass
        calls = 0

        def changed(root: Path, limits: V3SourceLimits):
            nonlocal calls
            calls += 1
            result = original(root, limits)
            if calls == 1:
                result = dict(result)
                result["notes.json"] += b" "
            return result

        with mock.patch("workstack.storage.migration_source._read_source_pass", changed):
            self.assert_error(
                "SOURCE_CHANGED_DURING_FREEZE", lambda: freeze_v3_source(self.root)
            )
        self.assertEqual(before, self.source_bytes())
        self.assertEqual(tuple(self.output_root.iterdir()), ())

    def test_source_change_after_freeze_refuses_backup_without_source_rewrite(self) -> None:
        frozen = freeze_v3_source(self.root)
        notes = self.root / "notes.json"
        notes.write_bytes(notes.read_bytes() + b"\n")
        changed = self.source_bytes()

        self.assert_error(
            "SOURCE_CHANGED_AFTER_FREEZE",
            lambda: create_verified_v3_backup(
                frozen,
                staging_path=self.output_root / "candidate.staging",
                output_path=self.output_root / "source.zip",
            ),
        )
        self.assertEqual(changed, self.source_bytes())
        self.assertEqual(tuple(self.output_root.iterdir()), ())

    def test_staged_verification_failure_cleans_owned_files_and_preserves_source(self) -> None:
        before = self.source_bytes()
        frozen = freeze_v3_source(self.root)
        original = verify_v3_backup
        calls = 0

        def fail_once(path: Path, *, limits=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise V3MigrationSourceError("INJECTED_VERIFICATION_FAILURE")
            return original(path, limits=limits)

        with mock.patch("workstack.storage.migration_source.verify_v3_backup", fail_once):
            self.assert_error(
                "INJECTED_VERIFICATION_FAILURE",
                lambda: create_verified_v3_backup(
                    frozen,
                    staging_path=self.output_root / "candidate.staging",
                    output_path=self.output_root / "source.zip",
                ),
            )
        self.assertEqual(tuple(self.output_root.iterdir()), ())
        self.assertEqual(before, self.source_bytes())

    def test_tampered_backup_verification_fails_without_touching_source(self) -> None:
        before = self.source_bytes()
        artifact = self.create_backup()
        unpacked = self.output_root / "unpacked"
        with zipfile.ZipFile(artifact.path) as archive:
            archive.extractall(unpacked)
        notes = unpacked / "notes.json"
        notes.write_bytes(notes.read_bytes() + b"tampered")
        tampered = self.output_root / "tampered.zip"
        with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.write(unpacked / V3_SOURCE_BACKUP_MANIFEST, V3_SOURCE_BACKUP_MANIFEST)
            for name in V3_SOURCE_FILES:
                archive.write(unpacked / name, name)

        self.assert_error(
            "BACKUP_ARTIFACT_DIGEST_MISMATCH", lambda: verify_v3_backup(tampered)
        )
        self.assertEqual(before, self.source_bytes())

    def test_malformed_manifest_and_archive_roster_fail_content_free(self) -> None:
        before = self.source_bytes()
        malformed = self.output_root / "malformed.zip"
        with zipfile.ZipFile(malformed, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(V3_SOURCE_BACKUP_MANIFEST, json.dumps({"opaque_key": "PRIVATE"}))
            for name in V3_SOURCE_FILES:
                archive.writestr(name, before[name])
        with self.assertRaises(V3MigrationSourceError) as caught:
            verify_v3_backup(malformed)
        self.assertEqual(caught.exception.code, "BACKUP_MANIFEST_NOT_CANONICAL")
        self.assertNotIn("PRIVATE", str(caught.exception))

        extra = self.output_root / "extra.zip"
        with zipfile.ZipFile(extra, "w") as archive:
            archive.writestr("opaque-name.txt", b"PRIVATE")
        with self.assertRaises(V3MigrationSourceError) as caught:
            verify_v3_backup(extra)
        self.assertEqual(caught.exception.code, "BACKUP_ARCHIVE_ROSTER_INVALID")
        self.assertNotIn("opaque-name", str(caught.exception))
        self.assertEqual(before, self.source_bytes())

    def test_explicit_paths_must_not_exist_or_modify_source_tree(self) -> None:
        frozen = freeze_v3_source(self.root)
        existing = self.output_root / "existing.zip"
        existing.write_bytes(b"keep")
        self.assert_error(
            "BACKUP_OUTPUT_PATH_INVALID",
            lambda: create_verified_v3_backup(
                frozen,
                staging_path=self.output_root / "candidate.staging",
                output_path=existing,
            ),
        )
        self.assert_error(
            "BACKUP_OUTPUT_PATH_INVALID",
            lambda: create_verified_v3_backup(
                frozen,
                staging_path=self.output_root / "candidate.staging",
                output_path=self.root / "source.zip",
            ),
        )
        verify_v3_source_unchanged(frozen)
        self.assertEqual(existing.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
