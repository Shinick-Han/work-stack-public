from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack.service import (
    DomainError,
    RevisionConflictError,
    WorkSessionConflictError,
    WorkStack,
)
from workstack.store import DEFAULTS, Store
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.planning_v4_repository import V4PlanningRepository
from workstack.storage.query_repository import WorkspaceQueryRepository
from workstack.storage.read_repository import (
    V3WorkspaceRepository,
    V4WorkspaceRepository,
)
from workstack.storage.reader import read_v4
from workstack.storage.repository import V4ReadOnlyStoreAdapter
from workstack.storage.runtime import resolve_runtime_authority
from workstack.storage.task_relationship_repository import (
    admit_experimental_v4_task_relationship_repository,
)
from workstack.storage.work_session_v4_repository import V4WorkSessionRepository

from tests.test_storage_intent_dual_backend import _write_conversion


NOW = "2026-09-01T12:00:00Z"
TODAY = "2026-09-01"


class Slice56Fixture:
    def __init__(self, base: Path) -> None:
        self.v3 = WorkStack(Store(base / "v3"))
        with self.legacy_clock():
            self.parent = self.v3.add_task("Parent")
            self.target = self.v3.add_task("Target")
            self.dependency = self.v3.add_task("Dependency")
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
        self.relationships = admit_experimental_v4_task_relationship_repository(
            self.authority,
            self.runtime,
            allow_v4_task_relationships=True,
            clock=lambda: NOW,
        )
        self.planning = V4PlanningRepository(
            self.authority,
            self.runtime,
            enable_v4_planning=True,
            task_note_source_indexes=self.conversion.task_note_source_indexes,
            clock=lambda: NOW,
        )
        uids = iter(
            f"cccccccc-cccc-4ccc-8ccc-{index:012x}"
            for index in range(1, 80)
        )
        self.sessions = V4WorkSessionRepository(
            self.runtime,
            enable_v4_work_sessions=True,
            now=lambda: NOW,
            today=lambda: TODAY,
            uid_factory=lambda: next(uids),
        )
        self.v4_read = V4WorkspaceRepository(
            self.authority,
            idempotency_ledger=self.conversion.idempotency_ledger,
            task_note_source_indexes=self.conversion.task_note_source_indexes,
            generation=0,
        )
        self.v4_query = WorkspaceQueryRepository(
            self.v4_read, base / "v4-projection"
        )
        read_store = V4ReadOnlyStoreAdapter(self.v4_read.read().snapshot)
        self.v4 = WorkStack(
            read_store,
            initialize=False,
            relationship_commands=self.relationships,
            planning_commands=self.planning,
            work_session_commands=self.sessions,
            query_commands=self.v4_query,
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


class Slice56ServiceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.fixture = Slice56Fixture(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_relationship_patch_and_logical_delete_match_public_v3_shape(self) -> None:
        request = {
            "revision": 0,
            "parent_id": self.fixture.parent["id"].lower(),
            "dependencies": [
                self.fixture.dependency["id"].lower(),
                self.fixture.dependency["id"],
            ],
        }
        legacy = self.fixture.legacy(
            "patch_task", self.fixture.target["id"], request
        )
        normalized = self.fixture.v4.patch_task(
            self.fixture.target["id"], request
        )
        self.assertEqual(normalized, legacy)

        deleted_v3 = self.fixture.legacy(
            "patch_task",
            self.fixture.target["id"],
            {"revision": 1, "status": "dropped"},
        )
        deleted_v4 = self.fixture.v4.patch_task(
            self.fixture.target["id"],
            {"revision": 1, "status": "dropped"},
        )
        self.assertEqual(deleted_v4, deleted_v3)
        self.assertEqual(deleted_v4["status"], "dropped")
        self.assertTrue(
            any(
                task["display_id"] == self.fixture.target["id"]
                for task in read_v4(self.fixture.authority).records["tasks"]
            )
        )

    def test_planning_patch_and_direct_transition_match_v3(self) -> None:
        legacy = self.fixture.legacy(
            "patch_task",
            self.fixture.target["id"],
            {"revision": 0, "status": "started"},
        )
        normalized = self.fixture.v4.patch_task(
            self.fixture.target["id"],
            {"revision": 0, "status": "started"},
        )
        self.assertEqual(normalized, legacy)

        second = Slice56Fixture(self.base / "direct")
        legacy_direct = second.legacy(
            "set_task_status", second.target["id"], "started", 0
        )
        normalized_direct = second.v4.set_task_status(
            second.target["id"], "started", 0
        )
        self.assertEqual(normalized_direct, legacy_direct)
        with self.assertRaises(RevisionConflictError):
            second.v4.set_task_status(second.target["id"], "done", 0)

    def test_work_session_lifecycle_and_projection_match_public_methods(self) -> None:
        body = {"task_id": self.fixture.target["id"].lower()}
        legacy_start = self.fixture.legacy(
            "start_work_session_v1", body, "service.session.start.0001"
        )
        normalized_start = self.fixture.v4.start_work_session_v1(
            body, "service.session.start.0001"
        )
        self.assertEqual(normalized_start, legacy_start)
        session_id = legacy_start["body"]["data"]["id"]

        for action in ("pause", "resume", "stop"):
            key = f"service.session.{action}.0001"
            path = f"/api/v1/work-sessions/{session_id}/{action}"
            legacy = self.fixture.legacy(
                "transition_work_session_v1",
                session_id,
                action,
                {},
                key,
                path=path,
            )
            normalized = self.fixture.v4.transition_work_session_v1(
                session_id, action, {}, key, path=path
            )
            self.assertEqual(normalized, legacy)

        self.assertEqual(
            self.fixture.v4.work_sessions_projection(),
            self.fixture.legacy("work_sessions_projection"),
        )
        worklog = {"done": ["done"], "next": ["next"], "blockers": []}
        path = f"/api/v1/work-sessions/{session_id}/worklog"
        legacy_log = self.fixture.legacy(
            "record_work_session_v1",
            session_id,
            worklog,
            "service.session.worklog.0001",
            path=path,
        )
        normalized_log = self.fixture.v4.record_work_session_v1(
            session_id,
            worklog,
            "service.session.worklog.0001",
            path=path,
        )
        self.assertEqual(normalized_log, legacy_log)
        self.assertEqual(
            self.fixture.v4.work_sessions_projection(),
            self.fixture.legacy("work_sessions_projection"),
        )

    def test_work_session_conflict_maps_to_existing_public_type(self) -> None:
        body = {"task_id": self.fixture.target["id"]}
        self.fixture.v4.start_work_session_v1(
            body, "service.session.conflict.first.0001"
        )
        with self.assertRaises(WorkSessionConflictError):
            self.fixture.v4.start_work_session_v1(
                body, "service.session.conflict.second.0001"
            )

    def test_query_search_and_workspace_graph_are_separate_optional_reads(self) -> None:
        v3_query = WorkspaceQueryRepository(
            V3WorkspaceRepository(self.fixture.v3.store),
            self.base / "v3-projection",
        )
        v3_service = WorkStack(
            self.fixture.v3.store, initialize=False, query_commands=v3_query
        )
        for query in ("Target", self.fixture.target["id"]):
            self.assertEqual(
                self.fixture.v4.search_projection(query, 30),
                v3_service.search_projection(query, 30),
            )
        self.assertEqual(
            self.fixture.v4.workspace_projection(),
            v3_service.workspace_projection(),
        )
        with self.assertRaises(DomainError):
            self.fixture.v4.search_projection("x", 30)

    def test_default_and_partial_injection_leave_other_v3_routes_unchanged(self) -> None:
        default = WorkStack(Store(self.base / "default"))
        self.assertIsNone(default.relationship_commands)
        self.assertIsNone(default.planning_commands)
        self.assertIsNone(default.work_session_commands)
        self.assertIsNone(default.query_commands)

        partial = WorkStack(
            Store(self.base / "partial"), planning_commands=self.fixture.planning
        )
        task = partial.add_task("v3 relationship remains available")
        changed = partial.patch_task(
            task["id"], {"revision": 0, "dependencies": []}
        )
        self.assertEqual(changed["revision"], 1)


if __name__ == "__main__":
    unittest.main()
