from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from workstack.store import STORE_MANIFEST_VERSION, Store, _task_semantics
from workstack.store_rosters import REPORTS_DOCUMENT_NAME
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.migration import execute_v3_migration
from workstack.storage.migration_source import V3_SOURCE_FILES, freeze_v3_source
from workstack.storage.read_repository import (
    RepositoryReadError,
    V3WorkspaceRepository,
    V4WorkspaceRepository,
    WorkspaceRepository,
)
from workstack.storage.semantic import snapshot_from_v3_documents
from workstack.storage.migration_v3_lease import HISTORICAL_SCHEMA_VERSION


FIXTURE = Path(__file__).parent / "fixtures" / "store-v3" / "populated"
CREATED_AT = "2026-09-01T12:00:00Z"
WORKSPACE_UID = "11111111-1111-4111-8111-111111111111"


class StorageReadRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)
        self.source = self.parent / "authority"
        shutil.copytree(FIXTURE, self.source)
        self.runtime_environment = mock.patch.dict(
            os.environ,
            {"WORK_STACK_RUNTIME": str(self.parent / "runtime")},
        )
        self.runtime_environment.start()

    def tearDown(self) -> None:
        self.runtime_environment.stop()
        self.temporary.cleanup()

    def migrate(self):
        return execute_v3_migration(
            self.source,
            candidate_created_at=CREATED_AT,
        )

    @staticmethod
    def v4_repository(execution, **overrides):
        conversion = execution.preview.conversion
        arguments = {
            "idempotency_ledger": conversion.idempotency_ledger,
            "task_note_source_indexes": conversion.task_note_source_indexes,
            "generation": 7,
        }
        arguments.update(overrides)
        return V4WorkspaceRepository(
            execution.preview.paths.candidate_root,
            **arguments,
        )

    def test_populated_v3_and_migrated_v4_share_one_read_contract(self) -> None:
        v3_repository = V3WorkspaceRepository(Store(self.source))
        v3 = v3_repository.read()
        execution = self.migrate()
        v4_repository = self.v4_repository(execution)
        v4 = v4_repository.read()

        self.assertIsInstance(v3_repository, WorkspaceRepository)
        self.assertIsInstance(v4_repository, WorkspaceRepository)
        self.assertEqual(v3.snapshot.to_dict(), v4.snapshot.to_dict())
        self.assertEqual(v3.snapshot.digest, v4.snapshot.digest)
        self.assertEqual(v3.snapshot.search_documents(), v4.snapshot.search_documents())
        self.assertEqual(v3.snapshot.relation_edges(), v4.snapshot.relation_edges())
        self.assertEqual(v3.stamp.snapshot_digest, v4.stamp.snapshot_digest)
        self.assertEqual(v3.stamp.workspace_uid, v4.stamp.workspace_uid)
        self.assertEqual(v3.stamp.format_version, 3)
        self.assertEqual(v3.stamp.generation, 0)
        self.assertEqual(v3.stamp.workspace_uid, WORKSPACE_UID)
        self.assertEqual(v4.stamp.format_version, 4)
        self.assertEqual(v4.stamp.generation, 7)
        self.assertRegex(v3.stamp.authority_manifest_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(v4.stamp.authority_manifest_digest, r"^sha256:[0-9a-f]{64}$")

    def test_v4_runtime_ledger_is_detached_and_not_read_from_candidate(self) -> None:
        execution = self.migrate()
        ledger = copy.deepcopy(execution.preview.conversion.idempotency_ledger)
        repository = self.v4_repository(execution, idempotency_ledger=ledger)
        ledger["records"].clear()

        result = repository.read()

        self.assertEqual(len(result.snapshot.to_dict()["idempotency"]), 2)
        self.assertFalse(
            any(
                "idempotency" in path.name
                for path in execution.preview.paths.candidate_root.rglob("*")
            )
        )
        self.assertFalse(hasattr(repository, "save"))
        self.assertFalse(hasattr(repository, "write"))

    def test_v4_runtime_ledger_must_match_and_resolve_against_authority(self) -> None:
        execution = self.migrate()
        mismatch = copy.deepcopy(execution.preview.conversion.idempotency_ledger)
        mismatch["workspace_uid"] = str(uuid.uuid4())
        with self.assertRaises(RepositoryReadError) as caught:
            self.v4_repository(execution, idempotency_ledger=mismatch).read()
        self.assertEqual(caught.exception.code, "RUNTIME_LEDGER_WORKSPACE_MISMATCH")

        unresolved = copy.deepcopy(execution.preview.conversion.idempotency_ledger)
        referenced = next(
            record for record in unresolved["records"] if "response_ref" in record
        )
        referenced["response_ref"]["record_uid"] = str(uuid.uuid4())
        with self.assertRaises(RepositoryReadError) as caught:
            self.v4_repository(execution, idempotency_ledger=unresolved).read()
        self.assertEqual(caught.exception.code, "RUNTIME_LEDGER_REPLY_UNRESOLVED")


class StorageReadRepositoryHistoricalV3Tests(unittest.TestCase):
    """Genuine nine-file v3 reads under the schema-5 build, without relabeling."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)
        self.source = self.parent / "authority"
        shutil.copytree(FIXTURE, self.source)
        self.runtime = tempfile.TemporaryDirectory()
        self.runtime_environment = mock.patch.dict(
            os.environ, {"WORK_STACK_RUNTIME": self.runtime.name}
        )
        self.runtime_environment.start()

    def tearDown(self) -> None:
        self.runtime_environment.stop()
        self.runtime.cleanup()
        self.temporary.cleanup()

    def source_bytes(self) -> dict[str, bytes]:
        return {name: (self.source / name).read_bytes() for name in V3_SOURCE_FILES}

    def repository(self) -> V3WorkspaceRepository:
        return V3WorkspaceRepository(Store(self.source))

    def expected_generation0_digest(self, store: Store) -> str:
        frozen = freeze_v3_source(self.source)
        documents = {
            name: json.loads((self.source / name).read_text(encoding="utf-8"))
            for name in V3_SOURCE_FILES
        }
        manifest = {
            "version": STORE_MANIFEST_VERSION,
            "workspace_id": documents["workspace.json"]["id"],
            "store_schema_version": HISTORICAL_SCHEMA_VERSION,
            "generation": 0,
            "files": {artifact.name: artifact.sha256 for artifact in frozen.artifacts},
            "tasks": _task_semantics(documents["backlog.json"]),
        }
        return store._manifest_digest(manifest)

    def assert_refused(self, code: str, before: dict[str, bytes]) -> RepositoryReadError:
        with self.assertRaises(RepositoryReadError) as caught:
            self.repository().read()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(before, self.source_bytes())
        return caught.exception

    def test_genuine_v3_read_keeps_bytes_and_binds_generation0_without_schema5(self) -> None:
        before = self.source_bytes()
        store = Store(self.source)
        with mock.patch.object(store, "consistent_read") as consistent_read:
            with mock.patch.object(store, "sync_status") as sync_status:
                result = V3WorkspaceRepository(store).read()

        consistent_read.assert_not_called()
        sync_status.assert_not_called()
        self.assertEqual(before, self.source_bytes())
        self.assertFalse((self.source / REPORTS_DOCUMENT_NAME).exists())
        self.assertFalse(store.store_manifest_path.exists())
        self.assertEqual(result.stamp.format_version, 3)
        self.assertEqual(result.stamp.generation, 0)
        self.assertEqual(result.stamp.workspace_uid, WORKSPACE_UID)
        self.assertEqual(
            result.stamp.authority_manifest_digest,
            self.expected_generation0_digest(store),
        )
        second = V3WorkspaceRepository(Store(self.source)).read()
        self.assertEqual(result.stamp, second.stamp)
        self.assertEqual(result.snapshot.digest, second.snapshot.digest)
        self.assertEqual(before, self.source_bytes())

    def test_the_writer_lease_is_held_for_the_read_and_released_after(self) -> None:
        original = snapshot_from_v3_documents
        contended: list[bool] = []

        def probe(documents):
            competitor = Store(self.source)
            handle = competitor.try_acquire_writer_lease()
            competitor.release_writer_lease(handle)
            contended.append(handle is None)
            return original(documents)

        with mock.patch(
            "workstack.storage.read_v3_snapshot.snapshot_from_v3_documents",
            probe,
        ):
            self.repository().read()

        self.assertEqual(contended, [True])
        successor = Store(self.source)
        handle = successor.try_acquire_writer_lease()
        self.assertIsNotNone(handle)
        successor.release_writer_lease(handle)

    def test_a_competing_store_writer_refuses_the_read(self) -> None:
        before = self.source_bytes()
        competitor = Store(self.source)
        handle = competitor.try_acquire_writer_lease()
        self.assertIsNotNone(handle)
        try:
            self.assert_refused("SOURCE_WRITER_LEASE_UNAVAILABLE", before)
        finally:
            competitor.release_writer_lease(handle)

    def test_a_pending_recovery_journal_is_refused_and_never_replayed(self) -> None:
        before = self.source_bytes()
        journal = self.source / ".workstack-journal.json"
        body = b'{"writes": []}'
        journal.write_bytes(body)

        self.assert_refused("SOURCE_RECOVERY_JOURNAL_PENDING", before)
        self.assertEqual(journal.read_bytes(), body)

    def test_a_newer_roster_document_is_refused_as_newer_than_v3(self) -> None:
        before = self.source_bytes()
        reports = self.source / REPORTS_DOCUMENT_NAME
        reports.write_bytes(
            canonical_json_bytes({"version": 1, "reports": [], "idempotency": []})
        )

        self.assert_refused("SOURCE_SCHEMA_NEWER_THAN_V3", before)
        self.assertEqual(
            reports.read_bytes(),
            canonical_json_bytes({"version": 1, "reports": [], "idempotency": []}),
        )

    def test_metadata_claiming_a_newer_schema_is_refused(self) -> None:
        path = self.source / "store-meta.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["store_schema_version"] = 5
        path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        before = self.source_bytes()

        self.assert_refused("SOURCE_NOT_VERSION3", before)

    def test_v4_candidate_layout_is_refused_without_fallback(self) -> None:
        execution = execute_v3_migration(self.source, candidate_created_at=CREATED_AT)
        candidate = execution.preview.paths.candidate_root
        before = {
            path.name: path.read_bytes()
            for path in candidate.iterdir()
            if path.is_file() and not path.name.startswith(".")
        }

        with self.assertRaises(RepositoryReadError) as caught:
            V3WorkspaceRepository(Store(candidate)).read()

        self.assertEqual(caught.exception.code, "SOURCE_FILE_MISSING")
        after = {
            path.name: path.read_bytes()
            for path in candidate.iterdir()
            if path.is_file() and not path.name.startswith(".")
        }
        self.assertEqual(before, after)

    def test_an_extra_json_file_is_refused_as_mixed_authority(self) -> None:
        extra = self.source / "future.json"
        extra.write_bytes(b"{}\n")
        before = self.source_bytes()

        self.assert_refused("EXTRA_AUTHORITATIVE_FILE", before)
        self.assertEqual(extra.read_bytes(), b"{}\n")

    def test_v5_initialized_store_is_refused_not_relabeled_as_v3(self) -> None:
        from workstack.service import WorkStack

        WorkStack(Store(self.source))
        self.assertTrue((self.source / REPORTS_DOCUMENT_NAME).exists())

        with self.assertRaises(RepositoryReadError) as caught:
            V3WorkspaceRepository(Store(self.source)).read()

        self.assertEqual(caught.exception.code, "SOURCE_SCHEMA_NEWER_THAN_V3")

    def test_existing_v3_runtime_manifest_binds_generation_and_digest(self) -> None:
        store = Store(self.source)
        frozen = freeze_v3_source(self.source)
        documents = {
            name: json.loads((self.source / name).read_text(encoding="utf-8"))
            for name in V3_SOURCE_FILES
        }
        manifest = {
            "version": STORE_MANIFEST_VERSION,
            "workspace_id": WORKSPACE_UID,
            "store_schema_version": HISTORICAL_SCHEMA_VERSION,
            "generation": 7,
            "files": {artifact.name: artifact.sha256 for artifact in frozen.artifacts},
            "tasks": _task_semantics(documents["backlog.json"]),
        }
        store.store_manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        before = self.source_bytes()

        result = V3WorkspaceRepository(store).read()

        self.assertEqual(before, self.source_bytes())
        self.assertEqual(result.stamp.generation, 7)
        self.assertEqual(
            result.stamp.authority_manifest_digest,
            store._manifest_digest(manifest),
        )
        self.assertNotEqual(
            result.stamp.authority_manifest_digest,
            self.expected_generation0_digest(store),
        )

    def test_existing_v5_runtime_manifest_is_not_treated_as_v3(self) -> None:
        store = Store(self.source)
        frozen = freeze_v3_source(self.source)
        files = {artifact.name: artifact.sha256 for artifact in frozen.artifacts}
        files[REPORTS_DOCUMENT_NAME] = "sha256:" + "0" * 64
        manifest = {
            "version": STORE_MANIFEST_VERSION,
            "workspace_id": WORKSPACE_UID,
            "store_schema_version": 5,
            "generation": 1,
            "files": files,
            "tasks": {},
        }
        store.store_manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        before = self.source_bytes()

        self.assert_refused("SOURCE_SCHEMA_NEWER_THAN_V3", before)

    def test_source_changed_during_freeze_is_refused(self) -> None:
        before = self.source_bytes()
        original = freeze_v3_source

        def mutate(root, *, limits=None):
            frozen = original(root, limits=limits)
            path = self.source / "notes.json"
            path.write_bytes(path.read_bytes() + b"\n")
            return frozen

        with mock.patch(
            "workstack.storage.read_v3_snapshot.freeze_v3_source",
            mutate,
        ):
            with self.assertRaises(RepositoryReadError) as caught:
                self.repository().read()

        self.assertEqual(caught.exception.code, "SOURCE_CHANGED_AFTER_FREEZE")
        self.assertNotEqual(before, self.source_bytes())


if __name__ == "__main__":
    unittest.main()
