"""Upgrading a collection store to schema 5, and refusing to when it must not.

Every fixture here is a directory this test wrote itself: a v1, v2, v3 or v5
document set with synthetic identities. Nothing reads a real home directory and
no test installs, restores or migrates live data.

The suite is written against the two questions the upgrade has to answer
separately: what version is already on disk, and what does that version's
documents mean. Getting those confused is what a widened roster does, so the
frozen historical sets are asserted here too.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from workstack import store_report_migration
from workstack.store import (
    DEFAULTS,
    MIGRATION_BACKUP_DIR,
    STORE_SCHEMA_VERSION,
    Store,
    StoreCorruptError,
    _compact_json,
)
from workstack.service import WorkStack
from workstack.store_document_validation import validate_document_values
from workstack.knowledge_ledger_document import KNOWLEDGE_DOCUMENT_NAME
from workstack.store_rosters import (
    REPORTS_DOCUMENT_NAME,
    V1_DOCUMENT_NAMES,
    V2_DOCUMENT_NAMES,
    V3_DOCUMENT_NAMES,
    V5_DOCUMENT_NAMES,
    V6_DOCUMENT_NAMES,
)


WORKSPACE_UID = "0f50a123-3da8-4c82-8f16-8ee1a57260c4"
EMPTY_REPORTS = {"version": 1, "reports": [], "idempotency": []}


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_compact_json(value)).hexdigest()


class MigrationCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def materialize(self, values: dict[str, Any]) -> None:
        for name, value in values.items():
            self._write(self.root / name, value)

    def auxiliary(self) -> dict[str, Any]:
        return {
            "okr.json": {"version": 1, "objectives": []},
            "worklog.json": {"version": 1, "days": {}},
            "notes.json": {"version": 1, "notes": []},
            "captures.json": {"version": 1, "captures": []},
            "replies.json": {"version": 1, "replies": []},
        }

    def v1_values(self) -> dict[str, Any]:
        values = self.auxiliary()
        values.update({
            "workspace.json": {"version": 1, "id": WORKSPACE_UID, "name": "Legacy"},
            "backlog.json": {
                "version": 1,
                "tasks": [{"id": "T-0001", "title": "Migrated", "status": "open"}],
            },
            "activity.json": {"version": 1, "activity": [], "idempotency": []},
        })
        return values

    def v2_values(self) -> dict[str, Any]:
        values = self.auxiliary()
        values.update({
            "workspace.json": {"version": 2, "id": WORKSPACE_UID, "name": "Legacy"},
            "backlog.json": {"version": 2, "tasks": []},
            "store-meta.json": {
                "version": 1,
                "store_schema_version": 2,
                "migration": {
                    "id": "workstack.store.v2",
                    "origin": "fresh",
                    "source_sha256": None,
                },
            },
            "activity.json": {"version": 1, "activity": [], "idempotency": []},
        })
        return values

    def v3_values(self) -> dict[str, Any]:
        """A real v3 authority, taken from a fresh v5 store and stepped back.

        The store this build ships can only write v5, so a genuine v3 fixture
        has to be constructed: the nine payloads a v5 store holds are already
        v3 payloads, and the metadata is the v5 record without its reports
        evidence and with the version it had.
        """

        with tempfile.TemporaryDirectory() as scratch:
            Store(Path(scratch)).initialize()
            values = {
                name: json.loads(
                    (Path(scratch) / name).read_text(encoding="utf-8")
                )
                for name in V3_DOCUMENT_NAMES
            }
        metadata = values["store-meta.json"]
        metadata["store_schema_version"] = 3
        del metadata["migrations"]["reports"]
        del metadata["migrations"]["knowledge"]
        return values

    def documents(self) -> dict[str, Any]:
        return {
            name: json.loads((self.root / name).read_text(encoding="utf-8"))
            for name in V6_DOCUMENT_NAMES
        }

    def backups(self) -> list[Path]:
        directory = self.root / MIGRATION_BACKUP_DIR
        return sorted(directory.iterdir()) if directory.is_dir() else []

    def prebackup_target(self, detected: int, values: dict[str, Any]) -> Path:
        return self.root / MIGRATION_BACKUP_DIR / "workstack-premigration-v{}-{}.zip".format(
            detected, digest(values)[7:23]
        )

    def _fail_after(self, store: Store, written: int):  # type: ignore[no-untyped-def]
        """Let `written` documents land, then interrupt the replacement loop."""

        original = store._atomic_write_locked
        seen: list[str] = []

        def failing(path: Path, value: Any) -> None:
            if len(seen) >= written:
                raise OSError("injected migration interruption")
            seen.append(path.name)
            original(path, value)

        return failing

    def v3_values_with_task(self) -> dict[str, Any]:
        """A v3 authority whose backlog actually carries a task.

        A released manifest records a task baseline beside its file digests,
        and an empty backlog cannot tell whether that baseline is checked. The
        task is created through the service so its revision, uid and planning
        status head are the ones a real installation holds, not ones this test
        invented.
        """

        with tempfile.TemporaryDirectory() as scratch:
            service = WorkStack(Store(Path(scratch)))
            service.add_task("Held task")
            values = {
                name: json.loads(
                    (Path(scratch) / name).read_text(encoding="utf-8")
                )
                for name in V3_DOCUMENT_NAMES
            }
        metadata = values["store-meta.json"]
        metadata["store_schema_version"] = 3
        del metadata["migrations"]["reports"]
        del metadata["migrations"]["knowledge"]
        return values

    def released_task_baseline(self) -> dict[str, Any]:
        """The task baseline a released build recorded for the bytes on disk."""

        backlog = json.loads((self.root / "backlog.json").read_text(encoding="utf-8"))
        return {
            task["id"]: {
                "revision": task["revision"],
                "digest": digest(task),
            }
            for task in backlog["tasks"]
        }

    def write_released_manifest(
        self,
        store: Store,
        values: dict[str, Any],
        *,
        generation: int = 7,
        workspace_id: str | None = None,
    ) -> Path:
        """The runtime manifest a released schema-3 build left behind.

        It is nine real file hashes, the task baseline those bytes mean and
        `store_schema_version: 3`, written where the released build wrote it,
        so the upgrade meets the baseline a real installation actually has
        rather than one this build could produce. `workspace_id` overrides only
        the identity, which is how a manifest belonging to another workspace is
        placed beside documents whose digests all still agree.
        """

        manifest = {
            "version": 1,
            "workspace_id": workspace_id or values["workspace.json"]["id"],
            "store_schema_version": 3,
            "generation": generation,
            "files": {
                name: "sha256:"
                + hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                for name in sorted(V3_DOCUMENT_NAMES)
            },
            "tasks": self.released_task_baseline(),
        }
        store.store_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._write(store.store_manifest_path, manifest)
        return store.store_manifest_path

    def assert_left_at_v3(self, before: dict[str, bytes]) -> None:
        """Nothing was backed up, journalled, published or rewritten."""

        self.assertFalse((self.root / ".workstack-journal.json").exists())
        self.assertFalse((self.root / REPORTS_DOCUMENT_NAME).exists())
        self.assertEqual(self.backups(), [])
        self.assertEqual(
            {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES},
            before,
        )


class FreshStoreTest(MigrationCase):
    def test_a_fresh_store_is_the_current_schema_with_its_full_roster(self) -> None:
        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertEqual(STORE_SCHEMA_VERSION, 6)
        self.assertEqual(readiness.migration_origin, "fresh")
        written = {item.name for item in self.root.iterdir() if item.suffix == ".json"}
        self.assertEqual(written, set(V6_DOCUMENT_NAMES))
        self.assertEqual(len(DEFAULTS), 11)

    def test_a_fresh_store_writes_the_default_reports_document(self) -> None:
        Store(self.root).initialize()

        self.assertEqual(self.documents()[REPORTS_DOCUMENT_NAME], EMPTY_REPORTS)

    def test_fresh_evidence_names_no_source(self) -> None:
        Store(self.root).initialize()

        migrations = self.documents()["store-meta.json"]["migrations"]
        self.assertEqual(
            set(migrations),
            {"identity", "planning_status", "reports", "knowledge"},
        )
        self.assertEqual(
            migrations["reports"],
            {"id": "workstack.reports.v5", "origin": "fresh", "source_sha256": None},
        )

    def test_a_fresh_store_takes_no_migration_backup(self) -> None:
        Store(self.root).initialize()

        self.assertEqual(self.backups(), [])


class HistoricalUpgradeTest(MigrationCase):
    def test_a_v1_store_upgrades_and_keeps_its_v1_evidence(self) -> None:
        values = self.v1_values()
        self.materialize(values)

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertEqual(readiness.migration_origin, "migrated_v1")
        migrations = self.documents()["store-meta.json"]["migrations"]
        self.assertEqual(
            migrations["identity"]["id"], "workstack.store.v1-to-v2"
        )
        # The digest names the v1 documents that were detected, so a rollback
        # and the evidence point at the same bytes.
        self.assertEqual(migrations["reports"]["source_sha256"], digest(values))
        self.assertEqual(migrations["reports"]["origin"], "migrated_v1")
        self.assertEqual(self.documents()[REPORTS_DOCUMENT_NAME], EMPTY_REPORTS)

    def test_a_v1_backlog_still_gains_uid_revision_and_planning_facts(self) -> None:
        self.materialize(self.v1_values())

        Store(self.root).initialize()

        documents = self.documents()
        task = documents["backlog.json"]["tasks"][0]
        self.assertEqual(documents["backlog.json"]["version"], 3)
        self.assertEqual(task["revision"], 0)
        self.assertRegex(task["status_fact_id"], r"^PS-[0-9]{6,}$")
        self.assertEqual(documents["activity.json"]["version"], 2)
        self.assertEqual(len(documents["activity.json"]["planning_status"]), 1)

    def test_a_v2_store_upgrades_and_keeps_its_v2_identity_record(self) -> None:
        values = self.v2_values()
        self.materialize(values)

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        migrations = self.documents()["store-meta.json"]["migrations"]
        self.assertEqual(migrations["identity"]["id"], "workstack.store.v2")
        self.assertEqual(migrations["identity"]["origin"], "fresh")
        self.assertEqual(migrations["planning_status"]["origin"], "migrated_v2")
        self.assertEqual(migrations["reports"]["source_sha256"], digest(values))

    def test_a_v3_store_upgrades_without_touching_its_nine_payloads(self) -> None:
        values = self.v3_values()
        self.materialize(values)
        before = {
            name: (self.root / name).read_bytes()
            for name in V3_DOCUMENT_NAMES
            if name != "store-meta.json"
        }

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        for name, body in before.items():
            with self.subTest(document=name):
                self.assertEqual(
                    json.loads((self.root / name).read_text(encoding="utf-8")),
                    json.loads(body.decode("utf-8")),
                )
        migrations = self.documents()["store-meta.json"]["migrations"]
        self.assertEqual(
            migrations["identity"], values["store-meta.json"]["migrations"]["identity"]
        )
        self.assertEqual(migrations["reports"]["origin"], "migrated_v3")
        self.assertEqual(migrations["reports"]["source_sha256"], digest(values))

    def test_the_upgrade_lands_in_one_journal_commit(self) -> None:
        self.materialize(self.v3_values())
        store = Store(self.root)
        commits: list[int] = []
        original = store.save_many

        def counted(writes, operation_id=None):  # type: ignore[no-untyped-def]
            commits.append(len(writes))
            return original(writes, operation_id=operation_id)

        store.save_many = counted  # type: ignore[method-assign]
        store.initialize()

        self.assertEqual(commits, [11])


class PreMigrationBackupTest(MigrationCase):
    def test_the_backup_holds_the_original_bytes_before_the_upgrade(self) -> None:
        values = self.v3_values()
        self.materialize(values)
        before = {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES}

        Store(self.root).initialize()

        archives = self.backups()
        self.assertEqual(len(archives), 1)
        import zipfile

        with zipfile.ZipFile(archives[0]) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(members.pop("manifest.json").decode("utf-8"))
        self.assertEqual(manifest["store_schema_version"], 3)
        self.assertEqual(members, before)

    def test_the_name_is_derived_from_the_version_and_the_source_digest(self) -> None:
        values = self.v3_values()
        self.materialize(values)

        Store(self.root).initialize()

        expected = "workstack-premigration-v3-{}.zip".format(digest(values)[7:23])
        self.assertEqual(self.backups()[0].name, expected)

    def test_a_second_attempt_reuses_the_same_file(self) -> None:
        self.materialize(self.v1_values())
        Store(self.root).initialize()
        first = self.backups()[0]
        body = first.read_bytes()
        stamp = first.stat().st_mtime_ns

        # Re-initializing an already upgraded store must not write another one.
        Store(self.root).initialize()

        self.assertEqual([item.name for item in self.backups()], [first.name])
        self.assertEqual(first.read_bytes(), body)
        self.assertEqual(first.stat().st_mtime_ns, stamp)

    def test_an_unrelated_file_under_the_backup_name_refuses(self) -> None:
        values = self.v3_values()
        self.materialize(values)
        directory = self.root / MIGRATION_BACKUP_DIR
        directory.mkdir()
        target = directory / "workstack-premigration-v3-{}.zip".format(
            digest(values)[7:23]
        )
        target.write_bytes(b"not the archive this migration packed")

        with self.assertRaises(StoreCorruptError):
            Store(self.root).initialize()

        self.assertEqual(target.read_bytes(), b"not the archive this migration packed")
        metadata = json.loads(
            (self.root / "store-meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["store_schema_version"], 3)

    def test_the_backup_directory_is_never_a_roster_member(self) -> None:
        self.assertNotIn(MIGRATION_BACKUP_DIR, DEFAULTS)
        self.assertNotIn(MIGRATION_BACKUP_DIR, V5_DOCUMENT_NAMES)


class PreMigrationBackupVerificationTest(MigrationCase):
    """The rollback archive is proved from the path it would be restored from."""

    def test_the_archive_is_verified_from_its_final_path_before_the_journal(
        self,
    ) -> None:
        values = self.v3_values()
        self.materialize(values)
        seen: list[tuple[Path, bool]] = []
        original = store_report_migration.verify_archive_file

        def spying(path: Path | str) -> Any:
            seen.append((Path(path), (self.root / ".workstack-journal.json").exists()))
            return original(path)

        with mock.patch.object(
            store_report_migration, "verify_archive_file", spying
        ):
            readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertEqual(seen, [(self.prebackup_target(3, values), False)])

    def test_an_archive_that_does_not_read_back_aborts_before_the_journal(self) -> None:
        values = self.v3_values()
        self.materialize(values)
        before = {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES}
        store = Store(self.root)
        original = store._atomic_write_bytes_locked

        def truncating(path: Path, value: bytes) -> None:
            original(path, value[: len(value) // 2])

        store._atomic_write_bytes_locked = truncating  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            StoreCorruptError, "rollback backup did not verify"
        ):
            store.initialize()

        self.assertFalse((self.root / ".workstack-journal.json").exists())
        self.assertFalse((self.root / REPORTS_DOCUMENT_NAME).exists())
        self.assertEqual(
            {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES},
            before,
        )

    def test_a_reused_existing_archive_is_verified_too(self) -> None:
        values = self.v3_values()
        self.materialize(values)
        bodies = {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES}
        packed = store_report_migration.pack_backup_archive(
            bodies,
            workspace_id=values["workspace.json"]["id"],
            store_schema_version=3,
            created=dt.datetime(1980, 1, 1, tzinfo=dt.timezone.utc),
        )
        target = self.prebackup_target(3, values)
        target.parent.mkdir()
        target.write_bytes(packed["body"])
        store = Store(self.root)
        written: list[bytes] = []
        original_write = store._atomic_write_bytes_locked

        def counted(path: Path, value: bytes) -> None:
            written.append(value)
            original_write(path, value)

        store._atomic_write_bytes_locked = counted  # type: ignore[method-assign]
        verified: list[Path] = []
        original_verify = store_report_migration.verify_archive_file

        def spying(path: Path | str) -> Any:
            verified.append(Path(path))
            return original_verify(path)

        with mock.patch.object(
            store_report_migration, "verify_archive_file", spying
        ):
            readiness = store.initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertEqual(written, [])
        self.assertEqual(verified, [target])
        self.assertEqual(target.read_bytes(), packed["body"])


class ReleasedRuntimeManifestTest(MigrationCase):
    """A released v3 authority arrives carrying the manifest its build wrote."""

    def test_a_v3_store_with_its_own_runtime_manifest_upgrades(self) -> None:
        values = self.v3_values()
        self.materialize(values)
        manifest_path = self.write_released_manifest(Store(self.root), values)

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        published = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(published["store_schema_version"], 6)
        self.assertEqual(set(published["files"]), set(V6_DOCUMENT_NAMES))
        self.assertGreater(published["generation"], 7)
        self.assertEqual(
            self.documents()["store-meta.json"]["migrations"]["reports"]["origin"],
            "migrated_v3",
        )

    def test_the_owned_upgrade_is_not_flagged_as_an_external_change(self) -> None:
        values = self.v3_values()
        self.materialize(values)
        self.write_released_manifest(Store(self.root), values)

        Store(self.root).initialize()

        status = Store(self.root).sync_status()
        self.assertEqual(status["state"], "in-sync")
        self.assertEqual(status["changed_files"], [])

    def test_a_partial_replacement_recovers_to_one_valid_v5_generation(self) -> None:
        values = self.v3_values()
        self.materialize(values)
        manifest_path = self.write_released_manifest(Store(self.root), values)
        interrupted = Store(self.root)
        interrupted._atomic_write_locked = self._fail_after(  # type: ignore[method-assign]
            interrupted, 3
        )
        with self.assertRaisesRegex(OSError, "injected migration interruption"):
            interrupted.initialize()
        self.assertTrue((self.root / ".workstack-journal.json").exists())

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertFalse((self.root / ".workstack-journal.json").exists())
        written = {item.name for item in self.root.iterdir() if item.suffix == ".json"}
        self.assertEqual(written, set(V6_DOCUMENT_NAMES))
        published = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(published["store_schema_version"], 6)
        self.assertEqual(set(published["files"]), set(V6_DOCUMENT_NAMES))
        self.assertEqual(Store(self.root).sync_status()["state"], "in-sync")

    def test_an_unowned_change_under_an_old_manifest_is_never_laundered(self) -> None:
        values = self.v3_values()
        self.materialize(values)
        self.write_released_manifest(Store(self.root), values)
        self._write(
            self.root / "notes.json",
            {"version": 1, "notes": [{"text": "edited outside Work Stack"}]},
        )
        before = {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES}
        message = "upgrade source changed outside Work Stack"

        # A retry must refuse the same way: a refusal that leaves a replayable
        # journal behind would adopt the unowned bytes on the next open.
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                with self.assertRaisesRegex(StoreCorruptError, message):
                    Store(self.root).initialize()

        self.assertFalse((self.root / ".workstack-journal.json").exists())
        self.assertFalse((self.root / REPORTS_DOCUMENT_NAME).exists())
        self.assertEqual(self.backups(), [])
        self.assertEqual(
            {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES},
            before,
        )

    def test_a_malformed_old_manifest_refuses_without_upgrading(self) -> None:
        cases = (
            ("roster", lambda value: value["files"].pop("notes.json")),
            ("digest", lambda value: value["files"].update({"notes.json": "sha256:x"})),
            ("version", lambda value: value.update(store_schema_version=4)),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                values = self.v3_values()
                self.materialize(values)
                store = Store(self.root)
                path = self.write_released_manifest(store, values)
                manifest = json.loads(path.read_text(encoding="utf-8"))
                mutate(manifest)
                self._write(path, manifest)

                with self.assertRaises(StoreCorruptError):
                    Store(self.root).initialize()

                self.assertFalse((self.root / REPORTS_DOCUMENT_NAME).exists())
                metadata = json.loads(
                    (self.root / "store-meta.json").read_text(encoding="utf-8")
                )
                self.assertEqual(metadata["store_schema_version"], 3)


class ForeignManifestBaselineTest(MigrationCase):
    """Hash-equal files do not make a released manifest this store's baseline."""

    FOREIGN_UID = "dce4234f-2304-4c1e-93a5-d4abe00e8a76"

    def refuses_twice(self, message: str) -> None:
        """A refusal has to survive the retry, or the second open adopts it."""

        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                with self.assertRaisesRegex(StoreCorruptError, message):
                    Store(self.root).initialize()

    def test_a_manifest_naming_another_workspace_refuses_before_any_backup(
        self,
    ) -> None:
        values = self.v3_values()
        self.materialize(values)
        manifest_path = self.write_released_manifest(
            Store(self.root), values, workspace_id=self.FOREIGN_UID
        )
        self.assertNotEqual(self.FOREIGN_UID, values["workspace.json"]["id"])
        before = {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES}

        self.refuses_twice("manifest belongs to another workspace")

        self.assert_left_at_v3(before)
        metadata = json.loads(
            (self.root / "store-meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["store_schema_version"], 3)
        published = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(published["workspace_id"], self.FOREIGN_UID)
        self.assertEqual(published["store_schema_version"], 3)

    def test_a_hash_valid_manifest_claiming_another_schema_refuses(self) -> None:
        values = self.v3_values_with_task()
        self.materialize(values)
        path = self.write_released_manifest(Store(self.root), values)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["store_schema_version"] = 2
        self._write(path, manifest)
        before_manifest = path.read_bytes()
        before = {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES}
        self.assertEqual(manifest["tasks"], self.released_task_baseline())
        for name, body in before.items():
            self.assertEqual(manifest["files"][name], "sha256:" + hashlib.sha256(body).hexdigest())

        self.refuses_twice("manifest schema does not match the detected source")

        self.assert_left_at_v3(before)
        self.assertEqual(path.read_bytes(), before_manifest)

    def test_a_task_baseline_the_held_backlog_does_not_mean_refuses(self) -> None:
        values = self.v3_values()
        self.materialize(values)
        path = self.write_released_manifest(Store(self.root), values)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["tasks"], {})
        manifest["tasks"] = {"T-0001": {"revision": 0, "digest": "sha256:" + "0" * 64}}
        self._write(path, manifest)
        before = {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES}

        self.refuses_twice("task baseline does not match its manifest")

        self.assert_left_at_v3(before)

    def test_a_tampered_backlog_with_a_repaired_digest_still_refuses(self) -> None:
        """The recorded file digest can be repaired; the baseline beside it cannot."""

        values = self.v3_values_with_task()
        self.materialize(values)
        path = self.write_released_manifest(Store(self.root), values)
        recorded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(list(recorded["tasks"]), ["T-0001"])

        tampered = copy.deepcopy(values["backlog.json"])
        tampered["tasks"][0]["title"] = "Retitled outside Work Stack"
        self._write(self.root / "backlog.json", tampered)
        recorded["files"]["backlog.json"] = (
            "sha256:"
            + hashlib.sha256((self.root / "backlog.json").read_bytes()).hexdigest()
        )
        self._write(path, recorded)
        before = {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES}

        self.refuses_twice("task baseline does not match its manifest")

        self.assert_left_at_v3(before)

    def test_a_released_v3_baseline_with_a_real_task_upgrades(self) -> None:
        values = self.v3_values_with_task()
        self.materialize(values)
        manifest_path = self.write_released_manifest(Store(self.root), values)
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertEqual(readiness.workspace_uid, values["workspace.json"]["id"])
        published = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(published["workspace_id"], values["workspace.json"]["id"])
        self.assertEqual(published["store_schema_version"], 6)
        self.assertEqual(published["tasks"], recorded["tasks"])
        self.assertEqual(
            self.documents()["backlog.json"]["tasks"], values["backlog.json"]["tasks"]
        )
        self.assertEqual(Store(self.root).sync_status()["state"], "in-sync")

    def test_a_recovered_partial_upgrade_keeps_the_owned_identity(self) -> None:
        """The recovery pass meets the same foreign manifest and must still refuse."""

        values = self.v3_values_with_task()
        self.materialize(values)
        manifest_path = self.write_released_manifest(Store(self.root), values)
        interrupted = Store(self.root)
        interrupted._atomic_write_locked = self._fail_after(  # type: ignore[method-assign]
            interrupted, 3
        )
        with self.assertRaisesRegex(OSError, "injected migration interruption"):
            interrupted.initialize()
        self.assertTrue((self.root / ".workstack-journal.json").exists())

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertEqual(readiness.workspace_uid, values["workspace.json"]["id"])
        self.assertFalse((self.root / ".workstack-journal.json").exists())
        published = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(published["workspace_id"], values["workspace.json"]["id"])
        self.assertEqual(set(published["files"]), set(V6_DOCUMENT_NAMES))


class ArchiveImageBindingTest(MigrationCase):
    """Verification, its digest and the rollback path name one set of bytes."""

    CORRUPT = b"PK\x03\x04 this is no longer an archive"

    def acquisitions_of(self, target: Path):  # type: ignore[no-untyped-def]
        """Count every acquisition of one path's bytes, at the real read seam.

        `zipfile` and `Path.read_bytes` both reach the filesystem through the
        same opener, so a verifier that parses the container from the path and
        then digests the path again is two acquisitions of two possibly
        different files. One held image is one acquisition.
        """

        resolved = target.resolve()
        opened: list[Path] = []
        original = io.open

        def opening(file: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                candidate = Path(file).resolve()
            except (TypeError, ValueError, OSError):
                candidate = None
            if candidate == resolved:
                opened.append(candidate)
            return original(file, *args, **kwargs)

        return mock.patch.object(io, "open", opening), opened

    def swapping_read(self, target: Path, replacement: bytes):  # type: ignore[no-untyped-def]
        """Replace `target` on disk the moment its own bytes are first read.

        The hook sits on the real read, after the archive this attempt packed
        was persisted and verified, so what is exercised is a rollback image
        that stops being the verified one while the migration still believes
        it holds it.
        """

        resolved = target.resolve()
        original = Path.read_bytes
        reads: list[int] = []

        def reading(self: Path) -> bytes:
            body = original(self)
            if self.resolve() == resolved:
                reads.append(len(body))
                if len(reads) == 1:
                    Path.write_bytes(resolved, replacement)
            return body

        return reading, reads

    def packed_v3(self) -> tuple[dict[str, Any], dict[str, Any]]:
        values = self.v3_values()
        self.materialize(values)
        bodies = {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES}
        packed = store_report_migration.pack_backup_archive(
            bodies,
            workspace_id=values["workspace.json"]["id"],
            store_schema_version=3,
            created=dt.datetime(1980, 1, 1, tzinfo=dt.timezone.utc),
        )
        return values, packed

    def test_the_verifier_acquires_the_archive_exactly_once(self) -> None:
        values, packed = self.packed_v3()
        target = self.root / "held.zip"
        target.write_bytes(packed["body"])
        patcher, opened = self.acquisitions_of(target)

        with patcher:
            verified = store_report_migration.verify_archive_file(target)

        self.assertEqual(len(opened), 1)
        self.assertEqual(verified.digest, packed["digest"])
        self.assertEqual(verified.path, target.resolve())
        self.assertEqual(verified.store_schema_version, 3)
        self.assertEqual(verified.workspace_id, values["workspace.json"]["id"])

    def test_a_v3_and_a_current_archive_both_verify_from_their_own_path(self) -> None:
        _, v3_packed = self.packed_v3()
        Store(self.root).initialize()
        workspace_uid = self.documents()["workspace.json"]["id"]
        v5_packed = store_report_migration.pack_backup_archive(
            {name: (self.root / name).read_bytes() for name in V6_DOCUMENT_NAMES},
            workspace_id=workspace_uid,
            store_schema_version=STORE_SCHEMA_VERSION,
            created=dt.datetime(1980, 1, 1, tzinfo=dt.timezone.utc),
        )
        cases = (
            (3, v3_packed, len(V3_DOCUMENT_NAMES)),
            (STORE_SCHEMA_VERSION, v5_packed, len(V6_DOCUMENT_NAMES)),
        )
        for version, packed, count in cases:
            with self.subTest(version=version):
                path = self.root / "archive-v{}.zip".format(version)
                path.write_bytes(packed["body"])
                verified = store_report_migration.verify_archive_file(path)
                self.assertEqual(verified.store_schema_version, version)
                self.assertEqual(verified.digest, packed["digest"])
                self.assertEqual(verified.file_count, count)
                self.assertEqual(verified.workspace_id, workspace_uid)
                self.assertEqual(verified.path, path.resolve())

    def test_a_swap_after_verification_aborts_before_the_journal(self) -> None:
        values = self.v3_values()
        self.materialize(values)
        before = {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES}
        target = self.prebackup_target(3, values)
        reading, reads = self.swapping_read(target, self.CORRUPT)

        with mock.patch.object(Path, "read_bytes", reading):
            with self.assertRaisesRegex(
                StoreCorruptError, "rollback backup did not verify"
            ):
                Store(self.root).initialize()

        self.assertEqual(len(reads), 2)
        self.assertFalse((self.root / ".workstack-journal.json").exists())
        self.assertFalse((self.root / REPORTS_DOCUMENT_NAME).exists())
        self.assertEqual(
            {name: (self.root / name).read_bytes() for name in V3_DOCUMENT_NAMES},
            before,
        )
        self.assertEqual(target.read_bytes(), self.CORRUPT)
        with self.assertRaises(ValueError):
            store_report_migration.verify_archive_file(target)


class InterruptionTest(MigrationCase):
    def test_a_crash_before_the_journal_leaves_the_original_generation(self) -> None:
        values = self.v3_values()
        self.materialize(values)
        store = Store(self.root)

        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("injected pre-backup interruption")

        store._atomic_write_bytes_locked = refuse  # type: ignore[method-assign]
        with self.assertRaisesRegex(OSError, "injected pre-backup interruption"):
            store.initialize()

        self.assertFalse((self.root / ".workstack-journal.json").exists())
        self.assertFalse((self.root / REPORTS_DOCUMENT_NAME).exists())
        metadata = json.loads(
            (self.root / "store-meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["store_schema_version"], 3)

    def test_a_crash_between_replacements_replays_to_the_full_roster(self) -> None:
        self.materialize(self.v1_values())
        interrupted = Store(self.root)
        interrupted._atomic_write_locked = self._fail_after(  # type: ignore[method-assign]
            interrupted, 3
        )
        with self.assertRaisesRegex(OSError, "injected migration interruption"):
            interrupted.initialize()
        self.assertTrue((self.root / ".workstack-journal.json").exists())

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertFalse((self.root / ".workstack-journal.json").exists())
        written = {item.name for item in self.root.iterdir() if item.suffix == ".json"}
        self.assertEqual(written, set(V6_DOCUMENT_NAMES))

    def test_a_crash_after_the_commit_but_before_the_manifest_reopens_as_five(
        self,
    ) -> None:
        self.materialize(self.v3_values())
        interrupted = Store(self.root)

        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("injected manifest interruption")

        interrupted._write_committed_manifest_locked = refuse  # type: ignore[method-assign]
        with self.assertRaisesRegex(OSError, "injected manifest interruption"):
            interrupted.initialize()

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertFalse((self.root / ".workstack-journal.json").exists())
        written = {item.name for item in self.root.iterdir() if item.suffix == ".json"}
        self.assertEqual(written, set(V6_DOCUMENT_NAMES))


class SettledStoreTest(MigrationCase):
    def test_reopening_a_current_store_migrates_nothing(self) -> None:
        Store(self.root).initialize()
        before = {name: (self.root / name).read_bytes() for name in V6_DOCUMENT_NAMES}

        readiness = Store(self.root).initialize()

        self.assertEqual(readiness.schema_version, 6)
        self.assertEqual(readiness.migration_origin, "fresh")
        self.assertEqual(
            {name: (self.root / name).read_bytes() for name in V6_DOCUMENT_NAMES},
            before,
        )
        self.assertEqual(self.backups(), [])

    def test_a_newer_schema_refuses_and_writes_nothing(self) -> None:
        Store(self.root).initialize()
        metadata_path = self.root / "store-meta.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["store_schema_version"] = 7
        self._write(metadata_path, metadata)
        before = {name: (self.root / name).read_bytes() for name in V6_DOCUMENT_NAMES}

        with self.assertRaisesRegex(StoreCorruptError, "newer than this Work Stack"):
            Store(self.root).initialize()

        self.assertEqual(
            {name: (self.root / name).read_bytes() for name in V6_DOCUMENT_NAMES},
            before,
        )
        self.assertEqual(self.backups(), [])

    def test_a_corrupt_reports_document_refuses_the_store(self) -> None:
        Store(self.root).initialize()
        self._write(self.root / REPORTS_DOCUMENT_NAME, {"version": 2, "reports": []})

        with self.assertRaisesRegex(StoreCorruptError, "reports.json schema is invalid"):
            Store(self.root).initialize()

    def test_a_report_bound_to_another_workspace_refuses(self) -> None:
        Store(self.root).initialize()
        documents = self.documents()
        workspace_uid = documents["workspace.json"]["id"]
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
        self.assertNotEqual(workspace_uid, report["workspace_uid"])
        self._write(
            self.root / REPORTS_DOCUMENT_NAME,
            {"version": 1, "reports": [report], "idempotency": []},
        )

        with self.assertRaisesRegex(StoreCorruptError, "reports.json schema is invalid"):
            Store(self.root).initialize()


class HistoricalSeamTest(MigrationCase):
    """The seam judges decoded mappings and touches nothing else."""

    def test_each_supported_version_is_admitted_as_itself(self) -> None:
        for version, values in (
            (1, self.v1_values()),
            (2, self.v2_values()),
            (3, self.v3_values()),
        ):
            with self.subTest(schema_version=version):
                readiness = validate_document_values(values, schema_version=version)
                self.assertEqual(readiness.schema_version, version)
                self.assertEqual(readiness.workspace_uid, values["workspace.json"]["id"])

    def test_the_seam_does_not_mutate_what_it_judges(self) -> None:
        values = self.v1_values()
        before = copy.deepcopy(values)

        validate_document_values(values, schema_version=1)

        self.assertEqual(values, before)

    def test_a_version_this_build_does_not_know_is_refused(self) -> None:
        for version in (0, 4, 6, True, "3", None):
            with self.subTest(schema_version=repr(version)):
                with self.assertRaises(StoreCorruptError):
                    validate_document_values(self.v3_values(), schema_version=version)

    def test_documents_of_the_wrong_version_are_refused(self) -> None:
        with self.assertRaises(StoreCorruptError):
            validate_document_values(self.v1_values(), schema_version=3)
        with self.assertRaises(StoreCorruptError):
            validate_document_values(self.v3_values(), schema_version=5)

    def test_the_seam_reaches_no_path(self) -> None:
        empty = Path(self.temporary.name) / "untouched"
        empty.mkdir()

        validate_document_values(self.v3_values(), schema_version=3)

        self.assertEqual(list(empty.iterdir()), [])


class FrozenHistoricalSetsTest(unittest.TestCase):
    def test_the_historical_rosters_did_not_move_with_this_build(self) -> None:
        self.assertEqual(len(V1_DOCUMENT_NAMES), 8)
        self.assertEqual(len(V2_DOCUMENT_NAMES), 9)
        self.assertEqual(len(V3_DOCUMENT_NAMES), 9)
        self.assertEqual(len(V5_DOCUMENT_NAMES), 10)
        self.assertEqual(len(V6_DOCUMENT_NAMES), 11)
        self.assertNotIn(REPORTS_DOCUMENT_NAME, V3_DOCUMENT_NAMES)
        self.assertEqual(V5_DOCUMENT_NAMES - V3_DOCUMENT_NAMES, {REPORTS_DOCUMENT_NAME})
        self.assertEqual(
            V6_DOCUMENT_NAMES - V5_DOCUMENT_NAMES, {KNOWLEDGE_DOCUMENT_NAME}
        )
        self.assertEqual(frozenset(DEFAULTS), V6_DOCUMENT_NAMES)


if __name__ == "__main__":
    unittest.main()
