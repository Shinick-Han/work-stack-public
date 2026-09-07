"""Verifying and restoring archives across the schema-5 boundary.

Verification has to be read-only: the released verifier used to judge a backup
by unpacking it and initializing it, which on a v5 build would migrate the very
copy it was asked to judge and then fail its own comparison. These tests hold
that line, and they hold the other half too — an older archive is *converted*
on restore rather than dropped, byte for byte, onto a fresh v5 store.

Every archive here is packed by this test from documents it wrote itself.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
import warnings
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from workstack import maintenance, store_report_migration
from workstack.maintenance import (
    BackupValidationError,
    backup_store,
    restore_store,
    verify_backup,
)
from workstack.store import Store
from workstack.store_rosters import (
    REPORTS_DOCUMENT_NAME,
    V3_DOCUMENT_NAMES,
    V5_DOCUMENT_NAMES,
)


EMPTY_REPORTS = {"version": 1, "reports": [], "idempotency": []}
PACKED_AT = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone.utc)


class RestoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.archives = self.base / "archives"
        self.archives.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fresh_store(self, name: str) -> Path:
        root = self.base / name
        Store(root).initialize()
        return root

    def _v3_bodies(self, root: Path) -> tuple[dict[str, bytes], str]:
        """A genuine v3 document set, stepped back from a v5 authority.

        This build cannot write a v3 store any more, so a v3 fixture has to be
        constructed rather than manufactured by calling initialize.
        """

        metadata = json.loads((root / "store-meta.json").read_text(encoding="utf-8"))
        workspace_uid = json.loads(
            (root / "workspace.json").read_text(encoding="utf-8")
        )["id"]
        metadata["store_schema_version"] = 3
        del metadata["migrations"]["reports"]
        bodies = {
            name: (root / name).read_bytes()
            for name in V3_DOCUMENT_NAMES
            if name != "store-meta.json"
        }
        bodies["store-meta.json"] = (
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        return bodies, workspace_uid

    def _pack(
        self, bodies: dict[str, bytes], workspace_uid: str, schema_version: int, name: str
    ) -> Path:
        packed = store_report_migration.pack_backup_archive(
            bodies,
            workspace_id=workspace_uid,
            store_schema_version=schema_version,
            created=PACKED_AT,
        )
        target = self.archives / name
        target.write_bytes(packed["body"])
        return target

    def v3_archive(self, name: str = "v3.zip") -> tuple[Path, str]:
        root = self._fresh_store("source-v3")
        bodies, workspace_uid = self._v3_bodies(root)
        return self._pack(bodies, workspace_uid, 3, name), workspace_uid

    def v5_archive(self, name: str = "v5.zip") -> tuple[Path, str]:
        root = self._fresh_store("source-v5")
        return backup_store(root, self.archives).path, json.loads(
            (root / "workspace.json").read_text(encoding="utf-8")
        )["id"]

    def documents(self, root: Path) -> dict[str, Any]:
        return {
            name: json.loads((root / name).read_text(encoding="utf-8"))
            for name in V5_DOCUMENT_NAMES
        }

    def json_bytes(self, root: Path) -> dict[str, bytes]:
        return {
            path.name: path.read_bytes() for path in sorted(root.glob("*.json"))
        }

    def overwrite_authoritative_store(self, source: Path, destination: Store) -> None:
        source_store = Store(source)
        for name in V5_DOCUMENT_NAMES:
            destination.path(name).write_bytes(source_store.path(name).read_bytes())
        destination.store_manifest_path.write_bytes(
            source_store.store_manifest_path.read_bytes()
        )

    def repacked(self, source: Path, name: str, manifest_change: Any) -> Path:
        """Rewrite one archive's manifest, leaving every payload member alone."""

        with zipfile.ZipFile(source) as opened:
            members = {member: opened.read(member) for member in opened.namelist()}
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
        manifest_change(manifest)
        members["manifest.json"] = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        target = self.archives / name
        with zipfile.ZipFile(target, "w") as archive:
            for member, body in members.items():
                archive.writestr(member, body)
        return target

    @contextmanager
    def recorded_member_reads(self) -> Iterator[list[str]]:
        """Every archive member this build actually decompresses, in order."""

        names: list[str] = []
        original = zipfile.ZipFile.read

        def recording(archive: zipfile.ZipFile, name: Any, pwd: Any = None) -> bytes:
            names.append(name if isinstance(name, str) else name.filename)
            return original(archive, name, pwd)

        with mock.patch.object(zipfile.ZipFile, "read", recording):
            yield names


class ReadOnlyVerificationTest(RestoreCase):
    def test_verifying_a_v3_archive_creates_nothing_and_changes_nothing(self) -> None:
        archive, workspace_uid = self.v3_archive()
        before = archive.read_bytes()
        neighbours = sorted(item.name for item in self.archives.iterdir())

        verified = verify_backup(archive)

        self.assertEqual(verified.workspace_id, workspace_uid)
        self.assertEqual(verified.file_count, 9)
        self.assertEqual(archive.read_bytes(), before)
        self.assertEqual(
            sorted(item.name for item in self.archives.iterdir()), neighbours
        )

    def test_verifying_a_v3_archive_does_not_upgrade_its_members(self) -> None:
        archive, _uid = self.v3_archive()

        verify_backup(archive)

        with zipfile.ZipFile(archive) as opened:
            members = set(opened.namelist())
            metadata = json.loads(opened.read("store-meta.json").decode("utf-8"))
        self.assertNotIn(REPORTS_DOCUMENT_NAME, members)
        self.assertEqual(metadata["store_schema_version"], 3)
        self.assertNotIn("reports", metadata["migrations"])

    def test_a_v5_archive_verifies_with_ten_members(self) -> None:
        archive, workspace_uid = self.v5_archive()

        verified = verify_backup(archive)

        self.assertEqual(verified.file_count, 10)
        self.assertEqual(verified.workspace_id, workspace_uid)

    def test_an_unsupported_or_newer_schema_refuses(self) -> None:
        root = self._fresh_store("source-unsupported")
        bodies = {name: (root / name).read_bytes() for name in V5_DOCUMENT_NAMES}
        workspace_uid = json.loads(
            (root / "workspace.json").read_text(encoding="utf-8")
        )["id"]
        good = self._pack(bodies, workspace_uid, 5, "claims.zip")
        with zipfile.ZipFile(good) as opened:
            members = {name: opened.read(name) for name in opened.namelist()}
        for claimed in (0, 4, 6, 99):
            with self.subTest(store_schema_version=claimed):
                manifest = json.loads(members["manifest.json"].decode("utf-8"))
                manifest["store_schema_version"] = claimed
                target = self.archives / "claims-{}.zip".format(claimed)
                with zipfile.ZipFile(target, "w") as archive:
                    for name, body in members.items():
                        if name == "manifest.json":
                            body = json.dumps(
                                manifest, ensure_ascii=False, separators=(",", ":"),
                                sort_keys=True,
                            ).encode("utf-8")
                        archive.writestr(name, body)
                with self.assertRaisesRegex(
                    BackupValidationError, "schema version is unsupported"
                ):
                    verify_backup(target)


class ArchiveAdmissionOrderTest(RestoreCase):
    """The header and the roster are admitted before a payload is decompressed."""

    def test_an_unknown_newer_or_v4_claim_refuses_before_any_payload(self) -> None:
        archive, _uid = self.v5_archive("order-source.zip")
        for claimed in (0, 4, 6, 99):
            with self.subTest(store_schema_version=claimed):
                target = self.repacked(
                    archive,
                    "order-{}.zip".format(claimed),
                    lambda value, claimed=claimed: value.update(
                        store_schema_version=claimed
                    ),
                )
                with self.recorded_member_reads() as read_names:
                    with self.assertRaisesRegex(
                        BackupValidationError, "schema version is unsupported"
                    ):
                        verify_backup(target)
                self.assertEqual(read_names, ["manifest.json"])

    def test_a_roster_that_does_not_match_the_claim_refuses_before_any_payload(
        self,
    ) -> None:
        archive, _uid = self.v5_archive("roster-source.zip")
        target = self.repacked(
            archive,
            "roster-claims-three.zip",
            lambda value: value.update(store_schema_version=3),
        )

        with self.recorded_member_reads() as read_names:
            with self.assertRaisesRegex(
                BackupValidationError, "member set is invalid"
            ):
                verify_backup(target)

        self.assertEqual(read_names, ["manifest.json"])

    def test_a_verified_archive_reads_its_manifest_first_then_its_roster(self) -> None:
        archive, _uid = self.v3_archive("order-v3.zip")

        with self.recorded_member_reads() as read_names:
            verified = verify_backup(archive)

        self.assertEqual(verified.file_count, 9)
        self.assertEqual(read_names[0], "manifest.json")
        self.assertEqual(sorted(read_names[1:]), sorted(V3_DOCUMENT_NAMES))

    def test_duplicate_directory_and_oversized_members_still_refuse_first(self) -> None:
        archive, _uid = self.v5_archive("defence-source.zip")
        with zipfile.ZipFile(archive) as opened:
            members = {name: opened.read(name) for name in opened.namelist()}
        duplicate = self.archives / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate, "w") as target:
                for name, body in members.items():
                    target.writestr(name, body)
                target.writestr("notes.json", members["notes.json"])
        directory = self.archives / "directory.zip"
        with zipfile.ZipFile(directory, "w") as target:
            for name, body in members.items():
                target.writestr(name, body)
            target.writestr("nested/", b"")

        for path, message in (
            (duplicate, "member set is invalid"),
            (directory, "contains an invalid member"),
        ):
            with self.subTest(path=path.name):
                with self.recorded_member_reads() as read_names:
                    with self.assertRaisesRegex(BackupValidationError, message):
                        verify_backup(path)
                self.assertEqual(read_names, [])


class RestoreConversionTest(RestoreCase):
    def test_restoring_a_v3_archive_produces_a_coherent_v5_authority(self) -> None:
        archive, workspace_uid = self.v3_archive()
        destination = self.base / "restored-v3"

        receipt = restore_store(archive, destination)

        self.assertEqual(receipt.workspace_id, workspace_uid)
        readiness = Store(destination).initialize()
        self.assertEqual(readiness.schema_version, 5)
        self.assertEqual(readiness.workspace_uid, workspace_uid)
        documents = self.documents(destination)
        self.assertEqual(documents[REPORTS_DOCUMENT_NAME], EMPTY_REPORTS)
        migrations = documents["store-meta.json"]["migrations"]
        self.assertEqual(documents["store-meta.json"]["store_schema_version"], 5)
        self.assertEqual(migrations["reports"]["origin"], "migrated_v3")
        self.assertRegex(
            migrations["reports"]["source_sha256"], r"^sha256:[0-9a-f]{64}$"
        )

    def test_a_restored_v3_authority_is_not_v3_metadata_on_a_v5_roster(self) -> None:
        archive, _uid = self.v3_archive()
        destination = self.base / "restored-coherent"

        restore_store(archive, destination)

        written = {
            item.name for item in destination.iterdir() if item.suffix == ".json"
        }
        self.assertEqual(written, set(V5_DOCUMENT_NAMES))
        metadata = self.documents(destination)["store-meta.json"]
        self.assertEqual(set(metadata["migrations"]), {"identity", "planning_status", "reports"})

    def test_restoring_a_v5_archive_round_trips(self) -> None:
        archive, workspace_uid = self.v5_archive()
        destination = self.base / "restored-v5"

        restore_store(archive, destination)

        readiness = Store(destination).initialize()
        self.assertEqual(readiness.schema_version, 5)
        self.assertEqual(readiness.workspace_uid, workspace_uid)
        self.assertEqual(
            self.documents(destination)[REPORTS_DOCUMENT_NAME], EMPTY_REPORTS
        )

    def test_an_empty_destination_adopts_the_archive_identity(self) -> None:
        archive, workspace_uid = self.v5_archive()
        destination = self.base / "adopting"
        destination.mkdir()

        restore_store(archive, destination)

        self.assertEqual(
            self.documents(destination)["workspace.json"]["id"], workspace_uid
        )


class RestoreGuardTest(RestoreCase):
    def test_an_occupied_destination_still_needs_replace(self) -> None:
        archive, _uid = self.v5_archive()
        destination = self._fresh_store("occupied")

        with self.assertRaisesRegex(
            BackupValidationError, "destination already contains a Work Stack store"
        ):
            restore_store(archive, destination)

    def test_replacing_still_requires_a_safety_backup_directory(self) -> None:
        archive, _uid = self.v5_archive()
        destination = self._fresh_store("replaced")

        with self.assertRaisesRegex(
            BackupValidationError, "safety backup directory is required"
        ):
            restore_store(archive, destination, replace=True)

    def test_replacing_the_same_workspace_writes_the_safety_backup_first(self) -> None:
        source = self._fresh_store("replace-source")
        archive = backup_store(source, self.archives).path
        workspace_uid = self.documents(source)["workspace.json"]["id"]
        destination = self.base / "replaced-ok"
        restore_store(archive, destination)
        Store(destination).save(
            "notes.json", {"version": 1, "notes": [{"text": "diverged"}]}
        )
        safety = self.base / "safety"

        receipt = restore_store(
            archive, destination, replace=True, safety_backup_dir=safety
        )

        self.assertIsNotNone(receipt.safety_backup)
        self.assertTrue(Path(str(receipt.safety_backup)).is_file())
        self.assertEqual(
            self.documents(destination)["workspace.json"]["id"], workspace_uid
        )
        self.assertEqual(self.documents(destination)["notes.json"]["notes"], [])

    def test_replacing_a_different_workspace_refuses_before_any_backup(self) -> None:
        archive, archive_uid = self.v5_archive()
        destination = self._fresh_store("foreign-destination")
        destination_uid = self.documents(destination)["workspace.json"]["id"]
        self.assertNotEqual(archive_uid, destination_uid)
        before = {
            path.name: path.read_bytes() for path in sorted(destination.glob("*.json"))
        }
        safety = self.base / "never-written-safety"

        with self.assertRaisesRegex(
            BackupValidationError, "destination holds a different workspace"
        ):
            restore_store(
                archive, destination, replace=True, safety_backup_dir=safety
            )

        self.assertEqual(
            {path.name: path.read_bytes() for path in sorted(destination.glob("*.json"))},
            before,
        )
        self.assertEqual(
            self.documents(destination)["workspace.json"]["id"], destination_uid
        )
        self.assertFalse(safety.exists())

    def test_a_foreign_swap_after_the_identity_check_refuses_without_writes(self) -> None:
        archive, archive_uid = self.v5_archive()
        destination = self.base / "same-then-swapped"
        restore_store(archive, destination)
        foreign = self._fresh_store("interleaved-foreign")
        foreign_uid = self.documents(foreign)["workspace.json"]["id"]
        self.assertNotEqual(archive_uid, foreign_uid)
        foreign_files = self.json_bytes(foreign)
        foreign_manifest = Store(foreign).store_manifest_path.read_bytes()
        safety = self.base / "never-written-after-swap"
        original = maintenance._existing_destination_generation

        def swap_after_identity(store: Store) -> maintenance._HeldDestinationGeneration:
            held = original(store)
            self.assertEqual(held.workspace_id, archive_uid)
            self.overwrite_authoritative_store(foreign, store)
            return held

        with mock.patch.object(
            maintenance,
            "_existing_destination_generation",
            side_effect=swap_after_identity,
        ):
            with self.assertRaisesRegex(
                BackupValidationError, "destination holds a different workspace"
            ):
                restore_store(
                    archive, destination, replace=True, safety_backup_dir=safety
                )

        self.assertEqual(self.json_bytes(destination), foreign_files)
        self.assertEqual(
            Store(destination).store_manifest_path.read_bytes(), foreign_manifest
        )
        self.assertEqual(
            self.documents(destination)["workspace.json"]["id"], foreign_uid
        )
        self.assertFalse(safety.exists())

    def test_a_v3_archive_never_replaces_a_foreign_v5_destination(self) -> None:
        archive, archive_uid = self.v3_archive("foreign-v3.zip")
        destination = self._fresh_store("foreign-v3-destination")
        self.assertNotEqual(
            archive_uid, self.documents(destination)["workspace.json"]["id"]
        )
        before = {
            path.name: path.read_bytes() for path in sorted(destination.glob("*.json"))
        }

        with self.assertRaisesRegex(
            BackupValidationError, "destination holds a different workspace"
        ):
            restore_store(
                archive,
                destination,
                replace=True,
                safety_backup_dir=self.base / "unused-safety",
            )

        self.assertEqual(
            {path.name: path.read_bytes() for path in sorted(destination.glob("*.json"))},
            before,
        )

    def test_a_report_bound_to_another_workspace_never_verifies(self) -> None:
        root = self._fresh_store("source-bad-report")
        workspace_uid = json.loads(
            (root / "workspace.json").read_text(encoding="utf-8")
        )["id"]
        report = {
            "uid": "00000001-abcd-4000-8000-000000000000",
            "workspace_uid": "11111111-abcd-4000-8000-000000000000",
            "template": "daily-v1",
            "period": {"kind": "day", "date": "2026-09-06"},
            "source_digest": "sha256:" + "0" * 64,
            "source_generated_at": "2026-09-06T11:00:00Z",
            "state": "draft",
            "revision": 1,
            "archived_from_state": None,
            "archived_at": None,
            "archive_note": None,
            "revisions": [{
                "content_revision": 1,
                "document_revision": 1,
                "markdown": "# body",
                "authored_at": "2026-09-06T12:00:00Z",
                "note": None,
            }],
            "created_at": "2026-09-06T12:00:00Z",
            "updated_at": "2026-09-06T12:00:00Z",
        }
        bodies = {name: (root / name).read_bytes() for name in V5_DOCUMENT_NAMES}
        bodies[REPORTS_DOCUMENT_NAME] = json.dumps(
            {"version": 1, "reports": [report], "idempotency": []},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        archive = self._pack(bodies, workspace_uid, 5, "foreign-report.zip")

        with self.assertRaisesRegex(
            BackupValidationError, "failed semantic validation"
        ):
            verify_backup(archive)


if __name__ == "__main__":
    unittest.main()
