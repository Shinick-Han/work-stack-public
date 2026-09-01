from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from workstack.storage.canonical import canonical_json_bytes, canonical_sha256
from workstack.storage.migration import (
    MAX_MIGRATION_RECEIPT_BYTES,
    StorageMigrationError,
    execute_v3_migration,
    plan_v3_migration,
    preview_v3_migration,
    resume_v3_migration,
    verify_v3_migration,
    verify_v3_migration_artifacts,
)
from workstack.storage.migration_source import V3_SOURCE_FILES
from workstack.storage.reader import read_v4


FIXTURE = Path(__file__).parent / "fixtures" / "store-v3" / "populated"
CREATED_AT = "2026-09-01T12:00:00Z"


class StorageMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)
        self.source = self.parent / "authority"
        shutil.copytree(FIXTURE, self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def source_bytes(self) -> dict[str, bytes]:
        return {name: (self.source / name).read_bytes() for name in V3_SOURCE_FILES}

    def test_preview_is_deterministic_and_non_mutating(self) -> None:
        before = self.source_bytes()

        first = preview_v3_migration(self.source, candidate_created_at=CREATED_AT)
        second = preview_v3_migration(self.source, candidate_created_at=CREATED_AT)

        self.assertEqual(first.paths, second.paths)
        self.assertEqual(first.conversion.conversion_digest, second.conversion.conversion_digest)
        self.assertFalse(first.paths.candidate_root.exists())
        self.assertFalse(first.paths.backup_path.exists())
        self.assertFalse(first.receipt_path.exists())
        self.assertEqual(before, self.source_bytes())

    def test_plan_reports_paths_and_source_digest_without_conversion_or_writes(self) -> None:
        before = self.source_bytes()

        plan = plan_v3_migration(self.source)

        self.assertRegex(plan.frozen.aggregate_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertFalse(plan.paths.candidate_root.exists())
        self.assertFalse(plan.paths.backup_path.exists())
        self.assertEqual(before, self.source_bytes())

    def test_execute_writes_verified_inactive_candidate_backup_and_receipt(self) -> None:
        before = self.source_bytes()

        execution = execute_v3_migration(self.source, candidate_created_at=CREATED_AT)
        verify_v3_migration(execution)

        self.assertTrue(execution.preview.paths.candidate_root.is_dir())
        self.assertTrue(execution.backup.path.is_file())
        self.assertTrue(execution.receipt_path.is_file())
        self.assertEqual(read_v4(execution.preview.paths.candidate_root).record_count, 7)
        self.assertEqual(execution.receipt["state"], "verified_candidate")
        self.assertEqual(
            execution.receipt["candidate"]["authority_digest"],
            execution.candidate_manifest.digest,
        )
        self.assertEqual(
            execution.receipt["runtime_evidence"],
            {
                "idempotency_ledger_digest": canonical_sha256(
                    execution.preview.conversion.idempotency_ledger
                ),
                "idempotency_record_count": 2,
            },
        )
        candidate_entries = {
            path.relative_to(execution.preview.paths.candidate_root).parts[0]
            for path in execution.preview.paths.candidate_root.iterdir()
        }
        self.assertNotIn("runtime", candidate_entries)
        self.assertFalse(
            any(
                "idempotency" in path.name
                for path in execution.preview.paths.candidate_root.rglob("*")
            )
        )
        self.assertEqual(before, self.source_bytes())
        verified = verify_v3_migration_artifacts(
            self.source,
            candidate_root=execution.preview.paths.candidate_root,
            backup_path=execution.backup.path,
            receipt_path=execution.receipt_path,
        )
        self.assertEqual(verified, execution.receipt)

    def test_expected_digest_guards_refuse_before_creating_artifacts(self) -> None:
        before = self.source_bytes()

        with self.assertRaises(StorageMigrationError) as caught:
            execute_v3_migration(
                self.source,
                candidate_created_at=CREATED_AT,
                expected_source_digest="sha256:" + "0" * 64,
            )

        self.assertEqual(caught.exception.code, "EXPECTED_SOURCE_DIGEST_MISMATCH")
        self.assertEqual(
            [path.name for path in self.parent.iterdir() if path != self.source], []
        )
        self.assertEqual(before, self.source_bytes())

    def test_same_source_and_timestamp_produce_byte_identical_candidate(self) -> None:
        first = execute_v3_migration(
            self.source,
            candidate_created_at=CREATED_AT,
            candidate_override="first-candidate",
            backup_override="first-backup.zip",
        )
        first_files = {
            path.relative_to(first.preview.paths.candidate_root).as_posix(): path.read_bytes()
            for path in first.preview.paths.candidate_root.rglob("*")
            if path.is_file()
        }
        first_receipt = first.receipt_path.read_bytes()

        second = execute_v3_migration(
            self.source,
            candidate_created_at=CREATED_AT,
            candidate_override="second-candidate",
            backup_override="second-backup.zip",
        )
        second_files = {
            path.relative_to(second.preview.paths.candidate_root).as_posix(): path.read_bytes()
            for path in second.preview.paths.candidate_root.rglob("*")
            if path.is_file()
        }

        self.assertEqual(first_files, second_files)
        self.assertEqual(first_receipt, second.receipt_path.read_bytes())
        self.assertEqual(first.backup.path.read_bytes(), second.backup.path.read_bytes())

    def test_failure_before_publication_cleans_candidate_staging_and_preserves_source(self) -> None:
        before = self.source_bytes()

        def fail(state: str) -> None:
            if state == "candidate_written":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            execute_v3_migration(
                self.source, candidate_created_at=CREATED_AT, fault_hook=fail
            )

        leftovers = [path for path in self.parent.iterdir() if ".staging-" in path.name]
        self.assertEqual(leftovers, [])
        self.assertFalse(any(path.is_dir() and "candidate" in path.name for path in self.parent.iterdir()))
        self.assertEqual(before, self.source_bytes())

    def test_preexisting_staging_directory_is_never_removed(self) -> None:
        preview = preview_v3_migration(self.source, candidate_created_at=CREATED_AT)
        migration_uid = preview.receipt_path.stem
        staging = preview.paths.candidate_root.with_name(
            preview.paths.candidate_root.name + f".staging-{migration_uid}"
        )
        staging.mkdir()
        marker = staging / "foreign.txt"
        marker.write_text("owned elsewhere", encoding="utf-8")

        with self.assertRaises(StorageMigrationError) as caught:
            execute_v3_migration(self.source, candidate_created_at=CREATED_AT)

        self.assertEqual(caught.exception.code, "CANDIDATE_STAGING_EXISTS")
        self.assertEqual(marker.read_text(encoding="utf-8"), "owned elsewhere")

    def test_failure_after_publication_retains_verifiable_artifacts_without_receipt(self) -> None:
        before = self.source_bytes()
        preview = preview_v3_migration(self.source, candidate_created_at=CREATED_AT)

        def fail(state: str) -> None:
            if state == "candidate_published":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            execute_v3_migration(
                self.source, candidate_created_at=CREATED_AT, fault_hook=fail
            )

        candidate = next(path for path in self.parent.iterdir() if path.is_dir() and "candidate" in path.name)
        self.assertGreater(read_v4(candidate).record_count, 0)
        self.assertFalse((candidate / "migrations").exists())
        self.assertEqual(before, self.source_bytes())

        resumed = resume_v3_migration(
            self.source,
            candidate_created_at=CREATED_AT,
            candidate_path=candidate,
            backup_path=preview.paths.backup_path,
            expected_source_digest=preview.frozen.aggregate_digest,
            expected_conversion_digest=preview.conversion.conversion_digest,
        )
        verify_v3_migration(resumed)
        self.assertTrue(resumed.receipt_path.is_file())

    def test_receipt_tamper_is_rejected(self) -> None:
        execution = execute_v3_migration(self.source, candidate_created_at=CREATED_AT)
        receipt = dict(execution.receipt)
        receipt["state"] = "activated"
        execution.receipt_path.write_bytes(canonical_json_bytes(receipt))

        with self.assertRaises(ValueError):
            verify_v3_migration(execution)

    def test_runtime_ledger_evidence_tamper_is_rejected_on_fresh_verification(self) -> None:
        execution = execute_v3_migration(self.source, candidate_created_at=CREATED_AT)
        receipt = copy.deepcopy(execution.receipt)
        receipt["runtime_evidence"]["idempotency_ledger_digest"] = "sha256:" + "0" * 64
        execution.receipt_path.write_bytes(canonical_json_bytes(receipt))

        with self.assertRaises(StorageMigrationError) as caught:
            verify_v3_migration_artifacts(
                self.source,
                candidate_root=execution.preview.paths.candidate_root,
                backup_path=execution.backup.path,
                receipt_path=execution.receipt_path,
            )
        self.assertEqual(caught.exception.code, "RECEIPT_EVIDENCE_MISMATCH")

    def test_every_fault_boundary_preserves_source_and_leaves_no_staging(self) -> None:
        states = (
            "lease_acquired",
            "source_frozen",
            "backup_verified",
            "candidate_written",
            "candidate_verified",
            "before_candidate_publish",
            "candidate_published",
            "receipt_written",
        )
        for state in states:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                parent = Path(temporary)
                source = parent / "authority"
                shutil.copytree(FIXTURE, source)
                before = {name: (source / name).read_bytes() for name in V3_SOURCE_FILES}

                def fail(current: str) -> None:
                    if current == state:
                        raise RuntimeError("injected")

                with self.assertRaisesRegex(RuntimeError, "injected"):
                    execute_v3_migration(
                        source, candidate_created_at=CREATED_AT, fault_hook=fail
                    )

                self.assertEqual(
                    before,
                    {name: (source / name).read_bytes() for name in V3_SOURCE_FILES},
                )
                self.assertEqual(
                    [path for path in parent.iterdir() if ".staging-" in path.name],
                    [],
                )

    def test_oversized_receipt_is_rejected_without_parsing(self) -> None:
        execution = execute_v3_migration(self.source, candidate_created_at=CREATED_AT)
        execution.receipt_path.write_bytes(b"{" + b"x" * MAX_MIGRATION_RECEIPT_BYTES)

        with self.assertRaises(StorageMigrationError) as caught:
            verify_v3_migration(execution)

        self.assertEqual(caught.exception.code, "RECEIPT_FILE_REJECTED")


if __name__ == "__main__":
    unittest.main()
