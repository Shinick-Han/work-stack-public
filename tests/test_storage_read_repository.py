from __future__ import annotations

import copy
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from workstack.store import Store
from workstack.storage.migration import execute_v3_migration
from workstack.storage.read_repository import (
    RepositoryReadError,
    V3WorkspaceRepository,
    V4WorkspaceRepository,
    WorkspaceRepository,
)


FIXTURE = Path(__file__).parent / "fixtures" / "store-v3" / "populated"
CREATED_AT = "2026-09-01T12:00:00Z"


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


if __name__ == "__main__":
    unittest.main()
