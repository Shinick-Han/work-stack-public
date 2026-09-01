from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack.service import (
    DomainError,
    IdempotencyConflictError,
    RevisionConflictError,
    WorkStack,
)
from workstack.store import DEFAULTS, Store
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.intent_v4_repository import V4IntentRepository
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.objective_v4_repository import V4ObjectiveRepository
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority
from workstack.storage.task_repository import V4TaskRepository

from tests.test_storage_intent_dual_backend import _write_conversion


NOW = "2026-09-01T12:00:00Z"
TODAY = "2026-09-01"


class Wave5ServiceFixture:
    def __init__(self, base: Path) -> None:
        self.v3 = WorkStack(Store(base / "v3"))
        with self.legacy_clock():
            self.seed_task = self.v3.add_task("Service command boundary")
        documents = {name: self.v3.store.load(name) for name in DEFAULTS}
        self.conversion = convert_v3_documents(
            documents, candidate_created_at="2026-09-01T00:00:00Z"
        )
        self.authority = base / "v4"
        self.authority.mkdir()
        _write_conversion(self.authority, self.conversion)
        self.runtime = resolve_runtime_authority(
            self.authority,
            base / "runtime",
            str(self.conversion.store["workspace_uid"]),
        )
        self.runtime.runtime_root.mkdir(parents=True)
        self.runtime.idempotency_path.write_bytes(
            canonical_json_bytes(dict(self.conversion.idempotency_ledger))
        )
        publish_runtime_manifest(
            self.runtime.manifest_path,
            build_v4_manifest(read_v4(self.authority), generation=0),
            expected_digest=None,
        )
        uids = iter(
            f"bbbbbbbb-bbbb-4bbb-8bbb-{index:012x}"
            for index in range(1, 80)
        )
        self.intents = V4IntentRepository(
            self.runtime,
            enable_v4_intents=True,
            now=lambda: NOW,
            uid_factory=lambda: next(uids),
        )
        self.objectives = V4ObjectiveRepository(
            self.runtime,
            enable_v4_objectives=True,
            now=lambda: NOW,
            uid_factory=lambda: next(uids),
        )
        self.tasks = V4TaskRepository(
            self.authority,
            self.runtime,
            task_note_source_indexes=self.conversion.task_note_source_indexes,
            clock=lambda: NOW,
            enable_v4_task_commands=True,
        )
        self.v4 = WorkStack(
            Store(base / "v4-shell"),
            intent_commands=self.intents,
            objective_commands=self.objectives,
            task_commands=self.tasks,
        )

    @staticmethod
    def legacy_clock():
        return _LegacyClock()

    def legacy(self, method: str, *args, **kwargs):
        with self.legacy_clock():
            return getattr(self.v3, method)(*args, **kwargs)


class _LegacyClock:
    def __enter__(self):
        self.today = mock.patch("workstack.service.today", return_value=TODAY)
        self.now = mock.patch("workstack.service.utc_now", return_value=NOW)
        self.today.start()
        self.now.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.now.stop()
        self.today.stop()


class Wave5ServiceCommandBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Wave5ServiceFixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_public_parity(self, method: str, *args, **kwargs):
        legacy = self.fixture.legacy(method, *args, **kwargs)
        normalized = getattr(self.fixture.v4, method)(*args, **kwargs)
        self.assertEqual(normalized, legacy)
        return normalized

    def test_slice2_note_checkin_and_worklog_use_public_workstack_methods(self) -> None:
        note = {"text": "  Decision  ", "links": ["t-0001", "T-0001", ""]}
        self.assert_public_parity(
            "create_note_v1", note, "service.note.slice2.0001",
            path="/api/v1/notes",
        )
        checkin = {"date": TODAY, "time": "09:30"}
        self.assert_public_parity(
            "checkin_v1", checkin, "service.checkin.slice2.0001",
            path="/api/v1/review/checkin",
        )
        worklog = {
            "date": TODAY,
            "task_id": self.fixture.seed_task["id"].lower(),
            "done": [" shipped "],
            "next": ["verify"],
            "blockers": [""],
        }
        self.assert_public_parity(
            "add_worklog_v1", worklog, "service.worklog.slice2.0001",
            path="/api/v1/review/entries",
        )

    def test_slice3_objective_and_key_result_preserve_envelopes_and_revision(self) -> None:
        objective_body = {
            "objective": "  Ship repository integration  ",
            "quarter": "2026-Q3",
        }
        objective = self.assert_public_parity(
            "create_objective_v1",
            objective_body,
            "service.objective.slice3.0001",
            path="/api/v1/objectives",
        )["body"]["data"]
        kr_body = {"text": "  Verify parity  ", "target": "100% ", "revision": 0}
        result = self.assert_public_parity(
            "add_key_result_v1",
            objective["id"],
            kr_body,
            "service.kr.slice3.0001",
            path=f"/api/v1/objectives/{objective['id']}/key-results",
        )
        self.assertEqual(result["body"]["data"]["revision"], 1)

    def test_slice4_task_create_and_scalar_patch_use_public_methods(self) -> None:
        created = self.assert_public_parity(
            "create_task_v1",
            {
                "title": "  Scalar Task  ",
                "detail": "  detail  ",
                "priority": "P1",
                "due": "2030-01-02",
                "scheduled": "2030-01-01",
                "estimate_minutes": 30,
            },
            "service.task.slice4.0001",
            path="/api/v1/tasks",
        )["body"]["data"]
        changed = self.assert_public_parity(
            "patch_task",
            created["id"],
            {
                "revision": 0,
                "title": "Updated Scalar Task",
                "detail": "updated detail",
                "priority": "P0",
                "due": None,
                "scheduled": "2030-02-01",
                "estimate_minutes": 45,
            },
        )
        self.assertEqual(changed["revision"], 1)

    def test_each_slice_maps_backend_errors_to_existing_public_types(self) -> None:
        checkin = {"date": TODAY, "time": "09:30"}
        key = "service.error.intent.0001"
        self.fixture.v4.checkin_v1(checkin, key)
        with self.assertRaises(IdempotencyConflictError):
            self.fixture.v4.checkin_v1(
                {"date": TODAY, "time": "10:30"}, key
            )

        objective = self.fixture.v4.create_objective_v1(
            {"objective": "Revision", "quarter": "2026-Q3"},
            "service.error.objective.0001",
        )["body"]["data"]
        with self.assertRaises(RevisionConflictError):
            self.fixture.v4.add_key_result_v1(
                objective["id"],
                {"text": "stale", "target": "0", "revision": 1},
                "service.error.kr.0001",
                path=f"/api/v1/objectives/{objective['id']}/key-results",
            )

        with self.assertRaises(DomainError) as invalid:
            self.fixture.v4.create_task_v1(
                {"title": ""}, "service.error.task.0001"
            )
        self.assertEqual(invalid.exception.code, "invalid_request")

    def test_default_and_partial_injection_are_independently_revertible(self) -> None:
        default = WorkStack(Store(Path(self.temporary.name) / "default"))
        self.assertIsNone(default.intent_commands)
        self.assertIsNone(default.objective_commands)
        self.assertIsNone(default.task_commands)

        partial = WorkStack(
            Store(Path(self.temporary.name) / "partial"),
            intent_commands=self.fixture.intents,
        )
        self.assertIs(partial.intent_commands, self.fixture.intents)
        self.assertIsNone(partial.objective_commands)
        self.assertIsNone(partial.task_commands)


if __name__ == "__main__":
    unittest.main()
