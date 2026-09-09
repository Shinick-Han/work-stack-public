from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack.mutation_notice import (
    derive_mutation_uid,
    derive_notice_id,
    serialize_notice,
    validate_notice,
)
from workstack.mutation_receipts import unkeyed_status_key
from workstack.service import WorkSessionConflictError, WorkStack
from workstack.store import Store
from workstack.store_document_validation import REPORTS_DEFAULT
from workstack.knowledge_ledger_document import (
    KNOWLEDGE_DEFAULT,
    KNOWLEDGE_DOCUMENT_NAME,
)
from workstack.store_rosters import (
    REPORTS_DOCUMENT_NAME,
    V3_DOCUMENT_NAMES,
    V3_DOCUMENT_ORDER,
    V5_DOCUMENT_NAMES,
    V6_DOCUMENT_NAMES,
)
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.intent_contract import IntentContractError
from workstack.storage.intent_v4_repository import (
    V4IntentRepository,
    V4IntentRepositoryError,
)
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest, read_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.objective_v4_repository import V4ObjectiveRepository
from workstack.storage.planning_v4_repository import V4PlanningRepository
from workstack.storage.read_repository import V4WorkspaceRepository
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority
from workstack.storage.task_repository import TaskRepositoryError
from workstack.storage.work_session_v4_repository import V4WorkSessionRepository
from workstack.task_display_id import FIELD as TASK_DISPLAY_ID_HIGH_WATER
from workstack.checkpoint_change import CheckpointChangeError, build_checkpoint_facts


NOW = "2026-09-01T12:00:00Z"
TODAY = "2026-09-01"


def _write_conversion(root: Path, conversion) -> None:
    def write(relative: str, body: bytes) -> None:
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    write("store.json", canonical_json_bytes(dict(conversion.store)))
    write("workspace.json", canonical_json_bytes(dict(conversion.workspace)))
    for kind, records in conversion.records.items():
        for record in records:
            uid = str(record["uid"])
            write(
                f"records/{kind}/{uid[:2]}/{uid}.json",
                canonical_json_bytes(dict(record)),
            )
    segments: dict[tuple[str, str], list[dict]] = {}
    for kind, events in conversion.streams.items():
        for event in events:
            key = kind, str(event["created_at"])[:7]
            segments.setdefault(key, []).append(dict(event))
    for (kind, month), events in sorted(segments.items()):
        body = b"".join(
            canonical_json_bytes(event) + b"\n"
            for event in sorted(events, key=lambda item: item["sequence"])
        )
        write(f"streams/{kind}/{month}.ndjson", body)


class _RejectingCheckpointFacts:
    """A checkpoint facts builder that records its call and refuses with one error."""

    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.calls: list[dict] = []

    def __call__(self, **arguments):
        self.calls.append(arguments)
        raise self.failure


class StorageIntentDualBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.v3_root = base / "v3"
        self.v3 = WorkStack(Store(self.v3_root))
        with mock.patch(
            "workstack.service.utc_now", return_value="2026-09-01T00:00:00Z"
        ), mock.patch("workstack.service.today", return_value=TODAY):
            self.task = self.v3.add_task("Intent boundary")
        # The conversion source is the historical v3 document set, named by the
        # frozen roster. Handing it this build's wider DEFAULTS would feed a
        # v3-to-v4 conversion a document v3 never had.
        documents = {name: self.v3.store.load(name) for name in V3_DOCUMENT_ORDER}
        self.conversion = convert_v3_documents(
            documents, candidate_created_at="2026-09-01T00:00:00Z"
        )
        self.v4_root = base / "v4"
        self.v4_root.mkdir()
        _write_conversion(self.v4_root, self.conversion)
        self.runtime = resolve_runtime_authority(
            self.v4_root,
            base / "runtime",
            str(self.conversion.store["workspace_uid"]),
        )
        self.runtime.runtime_root.mkdir(parents=True)
        baseline = build_v4_manifest(read_v4(self.v4_root), generation=0)
        publish_runtime_manifest(
            self.runtime.manifest_path, baseline, expected_digest=None
        )
        self.runtime.idempotency_path.write_bytes(
            canonical_json_bytes(dict(self.conversion.idempotency_ledger))
        )
        uids = iter(
            f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012x}" for index in range(1, 65)
        )
        self.clock = [NOW]
        self.v4 = V4IntentRepository(
            self.runtime,
            enable_v4_intents=True,
            checkpoint_facts=build_checkpoint_facts,
            now=lambda: self.clock[0],
            uid_factory=lambda: next(uids),
        )
        self.v4_objectives = V4ObjectiveRepository(
            self.runtime,
            enable_v4_objectives=True,
            now=lambda: self.clock[0],
            uid_factory=lambda: next(uids),
        )
        self.v4_planning = V4PlanningRepository(
            self.v4_root,
            self.runtime,
            enable_v4_planning=True,
            task_note_source_indexes=self.conversion.task_note_source_indexes,
            clock=lambda: self.clock[0],
        )
        self.v4_sessions = V4WorkSessionRepository(
            self.runtime,
            enable_v4_work_sessions=True,
            now=lambda: self.clock[0],
            today=lambda: self.clock[0][:10],
            uid_factory=lambda: next(uids),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _legacy(self, method: str, *args, **kwargs):
        # The notice wrapper reads its own wall clock, so freezing only the
        # service clock would leave an exact event oracle nondeterministic.
        with mock.patch(
            "workstack.service.today", return_value=self.clock[0][:10]
        ), mock.patch(
            "workstack.service.utc_now", return_value=self.clock[0]
        ), mock.patch(
            "workstack.mutation_service._utc_now", return_value=self.clock[0]
        ):
            return getattr(self.v3, method)(*args, **kwargs)

    def _v4_documents(self) -> dict:
        ledger = json.loads(self.runtime.idempotency_path.read_text(encoding="utf-8"))
        return V4WorkspaceRepository(
            self.v4_root,
            idempotency_ledger=ledger,
            task_note_source_indexes=self.conversion.task_note_source_indexes,
            generation=read_runtime_manifest(self.runtime.manifest_path).generation,
        ).read().snapshot.to_v3_documents()

    def _assert_current_reports_document_is_unobserved_and_empty(self) -> None:
        """Name the one current document the shared projection cannot carry.

        The v4 reverse projection reconstructs the v3 document set, so the
        roster it can be compared against is the frozen v3 one. Narrowing the
        comparison that way is only honest while the difference is accounted
        for by name and by content: schema 5 added exactly reports.json, and in
        this dual-backend scenario neither backend writes a report, so the
        legacy store still holds the empty current default. A report appearing
        here would fail this assertion instead of slipping past a roster that
        no longer mentions it. Schema 6 then added exactly knowledge.json, and
        the same rule applies to it: neither backend issues a knowledge
        request, so the empty owner ledger is what the legacy store must hold.
        """
        self.assertEqual(V5_DOCUMENT_NAMES - V3_DOCUMENT_NAMES, {REPORTS_DOCUMENT_NAME})
        self.assertEqual(
            V6_DOCUMENT_NAMES - V5_DOCUMENT_NAMES, {KNOWLEDGE_DOCUMENT_NAME}
        )
        self.assertEqual(
            REPORTS_DEFAULT, self.v3.store.load(REPORTS_DOCUMENT_NAME)
        )
        self.assertEqual(
            KNOWLEDGE_DEFAULT, self.v3.store.load(KNOWLEDGE_DOCUMENT_NAME)
        )

    def _assert_shared_projection_parity(self, snapshot: dict) -> None:
        """Compare every shared projection except the activity event list.

        The roster is the frozen v3 document set, written out by name, rather
        than this build's mutable DEFAULTS: the v4 reverse projection produces
        v3 documents, so measuring it against whatever the current schema
        happens to contain would report a schema change as a parity failure.
        The one document the current schema adds is checked separately, so a
        newly shared document still cannot go unobserved. Task records live in
        backlog.json, so status, revision, updated_at and status_fact_id are
        still compared exactly here.
        """
        self.assertEqual(set(V3_DOCUMENT_NAMES), set(snapshot))
        self._assert_current_reports_document_is_unobserved_and_empty()
        for name in sorted(V3_DOCUMENT_NAMES):
            if name == "activity.json":
                continue
            if name == "workspace.json":
                self._assert_workspace_parity(snapshot[name])
                continue
            if name == "store-meta.json":
                self._assert_metadata_parity(snapshot[name])
                continue
            self.assertEqual(self.v3.store.load(name), snapshot[name], name)
        legacy_activity = self.v3.store.load("activity.json")
        self.assertEqual(
            legacy_activity["planning_status"],
            snapshot["activity.json"]["planning_status"],
        )
        # Runtime ledger order is deliberately canonicalized by (created_at,
        # key); v3 insertion order is not part of replay behavior.
        self.assertEqual(
            {item["key"]: item for item in legacy_activity["idempotency"]},
            {
                item["key"]: item
                for item in snapshot["activity.json"]["idempotency"]
            },
        )

    def _assert_metadata_parity(self, projected: dict) -> None:
        """Compare store-meta.json across the one difference the versions make.

        The reverse projection reconstructs the v3 document set, so its
        metadata record is a v3 one by construction
        (workstack/storage/semantic.py), while the legacy backend is a store
        this build wrote and carries the current collection version. Requiring
        each side to declare exactly its own version, and the two evidence
        records both versions have to be identical, is stricter than leaving
        the document unobserved: a drifted identity or planning-status record
        still fails here, and a released default that silently stopped being
        the current collection schema fails too.
        """
        legacy = self.v3.store.load("store-meta.json")
        self.assertEqual(legacy["version"], projected["version"])
        self.assertEqual(3, projected["store_schema_version"])
        self.assertEqual({"identity", "planning_status"}, set(projected["migrations"]))
        self.assertEqual(6, legacy["store_schema_version"])
        self.assertEqual(
            {"identity", "planning_status", "reports", "knowledge"},
            set(legacy["migrations"]),
        )
        for name in sorted(projected["migrations"]):
            self.assertEqual(
                legacy["migrations"][name], projected["migrations"][name], name
            )

    def _assert_workspace_parity(self, projected: dict) -> None:
        """Compare workspace.json across its one designed v4 relocation.

        The Task display-ID high water is v3 workspace metadata but v4
        store.json metadata, so the reverse projection legitimately cannot
        carry it (workstack/storage/migration_conversion.py). Comparing every
        remaining field exactly and the water at its real v4 authority is
        stricter than leaving the document unobserved.
        """
        legacy = self.v3.store.load("workspace.json")
        water = legacy.pop(TASK_DISPLAY_ID_HIGH_WATER, None)
        self.assertEqual(legacy, projected, "workspace.json")
        self.assertEqual(
            water,
            read_v4(self.v4_root).store.get(TASK_DISPLAY_ID_HIGH_WATER),
            TASK_DISPLAY_ID_HIGH_WATER,
        )

    def _assert_semantic_parity(self) -> None:
        snapshot = self._v4_documents()
        self._assert_shared_projection_parity(snapshot)
        legacy_activity = self.v3.store.load("activity.json")
        self.assertEqual(legacy_activity["activity"], snapshot["activity.json"]["activity"])

    def _v4_activity_state(self) -> tuple:
        """The v4 activity stream as persisted and as reverse-projected."""
        return (
            tuple(read_v4(self.v4_root).streams["activity"]),
            self._v4_documents()["activity.json"]["activity"],
        )

    def _persistent_bytes(self) -> dict:
        """Every byte either backend has persisted, plus v4 runtime state."""
        state: dict[str, bytes] = {}
        for label, root in (
            ("v3", self.v3_root),
            ("v4", self.v4_root),
            ("runtime", self.runtime.runtime_root),
        ):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(root).as_posix()
                    state["{}/{}".format(label, relative)] = path.read_bytes()
        return state

    def _expected_status_notice(self) -> dict:
        """The single committed notice this open -> started transition may add."""
        workspace_uid = str(self.conversion.store["workspace_uid"])
        self.assertEqual(workspace_uid, self.v3.store.readiness.workspace_uid)
        task_uid = str(self.task["uid"])
        key = "ts:{}:0".format(task_uid)
        self.assertEqual(key, unkeyed_status_key(task_uid, 0))
        return {
            "actor": "local.user",
            "after_revision": 1,
            "before_revision": 0,
            "commit_state": "committed",
            "entity_kind": "task",
            "entity_uid": task_uid,
            "format": "workstack.mutation-notice",
            "idempotency_key": key,
            "mutation_uid": derive_mutation_uid(
                workspace_uid=workspace_uid, idempotency_key=key
            ),
            "notice_id": derive_notice_id(
                workspace_uid=workspace_uid, idempotency_key=key
            ),
            "operation": "task.status",
            "schema_version": 1,
            "source": "cli",
            "status_after": "started",
            "status_before": "open",
            "summary": "Task status open to started",
            "undoable": True,
            "workspace_uid": workspace_uid,
        }

    def _assert_parity_with_one_v3_status_notice(
        self, v3_activity_before: list, v4_activity_before: tuple
    ) -> None:
        """Prove the only activity delta is one authorized v3 status notice.

        This is narrower than filtering mutation.notice away: notices are a
        v3-only capability here, so v4 activity must still equal its exact
        captured baseline and every other projection must match exactly.
        """
        snapshot = self._v4_documents()
        self._assert_shared_projection_parity(snapshot)
        self.assertEqual(v4_activity_before, self._v4_activity_state())
        events = self.v3.store.load("activity.json")["activity"]
        self.assertEqual(v3_activity_before, events[: len(v3_activity_before)])
        self.assertEqual(len(v3_activity_before) + 1, len(events))
        notice = self._expected_status_notice()
        self.assertEqual(
            {
                "created_at": NOW,
                "details": {"notice": serialize_notice(notice).decode("utf-8")},
                "id": "E-000001",
                "task_id": self.task["id"],
                "type": "mutation.notice",
            },
            events[-1],
        )
        self.assertEqual(
            notice, validate_notice(json.loads(events[-1]["details"]["notice"]))
        )

    def _assert_replay_without_generation_change(
        self, v3_method: str, v4_method: str, body: dict, key: str
    ) -> None:
        legacy_first = self._legacy(v3_method, body, key)
        normalized_first = getattr(self.v4, v4_method)(body, key)
        self.assertEqual(legacy_first, normalized_first)
        generation = read_runtime_manifest(self.runtime.manifest_path).generation

        legacy_replay = self._legacy(v3_method, body, key)
        normalized_replay = getattr(self.v4, v4_method)(body, key)
        self.assertEqual(legacy_replay, normalized_replay)
        self.assertEqual(200, normalized_replay["status"])
        self.assertTrue(normalized_replay["body"]["meta"]["replayed"])
        self.assertEqual(
            generation, read_runtime_manifest(self.runtime.manifest_path).generation
        )
        self._assert_semantic_parity()

    def test_standalone_note_matches_v3_response_replay_and_state(self) -> None:
        self._assert_replay_without_generation_change(
            "create_note_v1",
            "create_note",
            {"text": "  Decision  ", "links": [self.task["id"], "t-0001", ""]},
            "note.dual.0001",
        )

    def test_checkin_matches_v3_response_replay_and_state(self) -> None:
        self._assert_replay_without_generation_change(
            "checkin_v1",
            "checkin",
            {"date": TODAY, "time": "09:30"},
            "checkin.dual.0001",
        )

    def test_worklog_entry_matches_v3_response_replay_and_state(self) -> None:
        self._assert_replay_without_generation_change(
            "add_worklog_v1",
            "add_worklog",
            {
                "date": TODAY,
                "task_id": self.task["id"].lower(),
                "done": ["  shipped  "],
                "next": ["verify"],
                "blockers": [""],
            },
            "worklog.dual.0001",
        )

    def test_v4_backend_is_default_off_and_conflicts_fail_without_writes(self) -> None:
        with self.assertRaises(V4IntentRepositoryError):
            V4IntentRepository(self.runtime, now=lambda: NOW)
        with self.assertRaises(V4IntentRepositoryError):
            V4ObjectiveRepository(
                self.runtime,
                now=lambda: NOW,
                uid_factory=lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
        body = {"date": TODAY, "time": "09:30"}
        self.v4.checkin(body, "checkin.conflict.0001")
        generation = read_runtime_manifest(self.runtime.manifest_path).generation
        with self.assertRaises(IntentContractError) as caught:
            self.v4.checkin({"date": TODAY, "time": "10:30"}, "checkin.conflict.0001")
        self.assertEqual("IDEMPOTENCY_KEY_CONFLICT", caught.exception.code)
        self.assertEqual(
            generation, read_runtime_manifest(self.runtime.manifest_path).generation
        )

    def test_objective_create_matches_v3_revision_activity_and_replay(self) -> None:
        body = {"objective": "  Ship normalized storage  ", "quarter": "2026-Q3"}
        key = "objective.dual.0001"
        legacy = self._legacy("create_objective_v1", body, key)
        normalized = self.v4_objectives.create_objective(body, key)
        self.assertEqual(legacy, normalized)
        generation = read_runtime_manifest(self.runtime.manifest_path).generation

        self.assertEqual(
            self._legacy("create_objective_v1", body, key),
            self.v4_objectives.create_objective(body, key),
        )
        self.assertEqual(
            generation, read_runtime_manifest(self.runtime.manifest_path).generation
        )
        self._assert_semantic_parity()

    def test_stale_key_result_revision_fails_before_v4_activity_or_ledger_write(self) -> None:
        objective_body = {"objective": "Revision boundary", "quarter": "2026-Q3"}
        legacy = self._legacy(
            "create_objective_v1", objective_body, "objective.revision.0001"
        )
        self.v4_objectives.create_objective(
            objective_body, "objective.revision.0001"
        )
        objective_id = legacy["body"]["data"]["id"]
        path = f"/api/v1/objectives/{objective_id}/key-results"
        stale = {"text": "Must fail", "target": "0", "revision": 1}
        generation = read_runtime_manifest(self.runtime.manifest_path).generation
        ledger_before = self.runtime.idempotency_path.read_bytes()
        activity_before = tuple(read_v4(self.v4_root).streams["activity"])

        with self.assertRaisesRegex(Exception, "revision is stale"):
            self._legacy(
                "add_key_result_v1",
                objective_id,
                stale,
                "key.result.stale.0001",
                path=path,
            )
        with self.assertRaises(IntentContractError) as caught:
            self.v4_objectives.add_key_result(
                objective_id,
                stale,
                "key.result.stale.0001",
                path=path,
            )
        self.assertEqual("OBJECTIVE_REVISION_CONFLICT", caught.exception.code)
        self.assertEqual(
            generation, read_runtime_manifest(self.runtime.manifest_path).generation
        )
        self.assertEqual(ledger_before, self.runtime.idempotency_path.read_bytes())
        self.assertEqual(activity_before, tuple(read_v4(self.v4_root).streams["activity"]))

    def test_key_result_matches_v3_revision_activity_and_replays_after_advance(self) -> None:
        objective_body = {"objective": "Normalized storage", "quarter": "2026-Q3"}
        legacy_objective = self._legacy(
            "create_objective_v1", objective_body, "objective.seed.0001"
        )
        normalized_objective = self.v4_objectives.create_objective(
            objective_body, "objective.seed.0001"
        )
        self.assertEqual(legacy_objective, normalized_objective)
        objective_id = legacy_objective["body"]["data"]["id"]
        path = f"/api/v1/objectives/{objective_id}/key-results"
        body = {"text": "  Measure adoption  ", "target": "100% ", "revision": 0}
        key = "key.result.dual.0001"
        legacy = self._legacy(
            "add_key_result_v1", objective_id, body, key, path=path
        )
        normalized = self.v4_objectives.add_key_result(
            objective_id, body, key, path=path
        )
        self.assertEqual(legacy, normalized)
        self.assertEqual(1, normalized["body"]["data"]["revision"])
        generation = read_runtime_manifest(self.runtime.manifest_path).generation

        replayed_legacy = self._legacy(
            "add_key_result_v1", objective_id, body, key, path=path
        )
        replayed_v4 = self.v4_objectives.add_key_result(
            objective_id, body, key, path=path
        )
        self.assertEqual(replayed_legacy, replayed_v4)
        self.assertEqual(200, replayed_v4["status"])
        self.assertEqual(
            generation, read_runtime_manifest(self.runtime.manifest_path).generation
        )
        self._assert_semantic_parity()

    def test_planning_status_matches_v3_revision_fact_and_noop(self) -> None:
        v3_activity_before = self.v3.store.load("activity.json")["activity"]
        v4_activity_before = self._v4_activity_state()
        self.assertEqual([], v3_activity_before)

        legacy = self._legacy(
            "set_task_status", self.task["id"], "started", 0
        )
        normalized = self.v4_planning.set_task_status(
            self.task["id"], "started", 0
        )
        self.assertEqual(legacy, normalized)
        self.assertEqual(1, legacy["revision"])
        generation = read_runtime_manifest(self.runtime.manifest_path).generation
        ledger = self.runtime.idempotency_path.read_bytes()
        committed = self._persistent_bytes()

        self.assertEqual(
            self._legacy("set_task_status", self.task["id"], "started", 1),
            self.v4_planning.set_task_status(self.task["id"], "started", 1),
        )
        # The same-status request is a true no-op on both sides, so the one
        # authorized notice asserted below cannot have been appended twice.
        self.assertEqual(committed, self._persistent_bytes())
        self.assertEqual(
            generation, read_runtime_manifest(self.runtime.manifest_path).generation
        )
        self.assertEqual(ledger, self.runtime.idempotency_path.read_bytes())
        self.assertEqual(1, self.v3.get_task(self.task["id"])["revision"])
        self._assert_parity_with_one_v3_status_notice(
            v3_activity_before, v4_activity_before
        )

    def test_planning_invalid_or_stale_transition_fails_without_write(self) -> None:
        generation = read_runtime_manifest(self.runtime.manifest_path).generation
        with self.assertRaises(TaskRepositoryError) as stale:
            self.v4_planning.set_task_status(self.task["id"], "done", 1)
        self.assertEqual("revision_conflict", stale.exception.code)
        with self.assertRaises(TaskRepositoryError) as invalid:
            self.v4_planning.set_task_status(self.task["id"], "invalid", 0)
        self.assertEqual("status_invalid", invalid.exception.code)
        self.assertEqual(
            generation, read_runtime_manifest(self.runtime.manifest_path).generation
        )

    def test_work_session_full_lifecycle_matches_v3_and_replays(self) -> None:
        self.clock[0] = "2026-09-01T09:00:00Z"
        body = {"task_id": self.task["id"]}
        legacy_start = self._legacy(
            "start_work_session_v1", body, "session.flow.start"
        )
        normalized_start = self.v4_sessions.start(body, "session.flow.start")
        self.assertEqual(legacy_start, normalized_start)
        session_id = legacy_start["body"]["data"]["id"]
        generation = read_runtime_manifest(self.runtime.manifest_path).generation
        self.assertEqual(
            self._legacy("start_work_session_v1", body, "session.flow.start"),
            self.v4_sessions.start(body, "session.flow.start"),
        )
        self.assertEqual(
            generation, read_runtime_manifest(self.runtime.manifest_path).generation
        )

        for instant, action, key in (
            ("2026-09-01T09:10:00Z", "pause", "session.flow.pause"),
            ("2026-09-01T09:15:00Z", "resume", "session.flow.resume"),
            ("2026-09-01T09:35:00Z", "stop", "session.flow.stop"),
        ):
            self.clock[0] = instant
            self.assertEqual(
                self._legacy(
                    "transition_work_session_v1", session_id, action, {}, key
                ),
                self.v4_sessions.transition(session_id, action, {}, key),
            )

        # Replay precedes the state-machine guard: the original pause remains
        # safely replayable even after the session has advanced to stopped.
        generation = read_runtime_manifest(self.runtime.manifest_path).generation
        self.assertEqual(
            self._legacy(
                "transition_work_session_v1",
                session_id,
                "pause",
                {},
                "session.flow.pause",
            ),
            self.v4_sessions.transition(
                session_id, "pause", {}, "session.flow.pause"
            ),
        )
        self.assertEqual(
            generation, read_runtime_manifest(self.runtime.manifest_path).generation
        )

        self.clock[0] = "2026-09-01T09:40:00Z"
        entry = {
            "done": ["  Prepared the brief  "],
            "next": ["Verify evidence"],
            "blockers": [],
        }
        legacy_recorded = self._legacy(
            "record_work_session_v1", session_id, entry, "session.flow.record"
        )
        normalized_recorded = self.v4_sessions.record_worklog(
            session_id, entry, "session.flow.record"
        )
        self.assertEqual(legacy_recorded, normalized_recorded)
        generation = read_runtime_manifest(self.runtime.manifest_path).generation
        self.assertEqual(
            self._legacy(
                "record_work_session_v1", session_id, entry, "session.flow.record"
            ),
            self.v4_sessions.record_worklog(
                session_id, entry, "session.flow.record"
            ),
        )
        self.assertEqual(
            generation, read_runtime_manifest(self.runtime.manifest_path).generation
        )
        self.assertEqual(0, self.v3.get_task(self.task["id"])["revision"])
        self._assert_semantic_parity()

    def test_invalid_session_transition_is_fail_closed(self) -> None:
        body = {"task_id": self.task["id"]}
        legacy = self._legacy(
            "start_work_session_v1", body, "session.invalid.start"
        )
        normalized = self.v4_sessions.start(body, "session.invalid.start")
        self.assertEqual(legacy, normalized)
        session_id = legacy["body"]["data"]["id"]
        generation = read_runtime_manifest(self.runtime.manifest_path).generation
        ledger = self.runtime.idempotency_path.read_bytes()

        with self.assertRaises(WorkSessionConflictError):
            self._legacy(
                "transition_work_session_v1",
                session_id,
                "resume",
                {},
                "session.invalid.resume",
            )
        with self.assertRaises(IntentContractError) as conflict:
            self.v4_sessions.transition(
                session_id, "resume", {}, "session.invalid.resume"
            )
        self.assertEqual("WORK_SESSION_TRANSITION_CONFLICT", conflict.exception.code)
        self.assertEqual(
            generation, read_runtime_manifest(self.runtime.manifest_path).generation
        )
        self.assertEqual(ledger, self.runtime.idempotency_path.read_bytes())

    def test_planning_and_work_session_backends_are_default_off(self) -> None:
        with self.assertRaises(TaskRepositoryError):
            V4PlanningRepository(
                self.v4_root, self.runtime, clock=lambda: NOW
            )
        with self.assertRaises(V4IntentRepositoryError):
            V4WorkSessionRepository(
                self.runtime,
                now=lambda: NOW,
                uid_factory=lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )

    def _worklog_body(self) -> dict:
        return {
            "date": TODAY,
            "task_id": self.task["id"],
            "done": ["shipped"],
            "next": ["verify"],
            "blockers": [],
        }

    def _v4_state(self) -> tuple:
        physical = read_v4(self.v4_root)
        return (
            read_runtime_manifest(self.runtime.manifest_path).generation,
            self.runtime.idempotency_path.read_bytes(),
            tuple(physical.streams["worklog"]),
            tuple(physical.streams["activity"]),
        )

    def test_worklog_entry_refuses_without_checkpoint_facts_builder_and_writes_nothing(self) -> None:
        # Seed one accepted entry so the streams the refusal must leave alone are non-empty.
        self.v4.add_worklog(self._worklog_body(), "worklog.facts.0001")
        before = self._v4_state()
        unwired = V4IntentRepository(
            self.runtime,
            enable_v4_intents=True,
            now=lambda: self.clock[0],
            uid_factory=lambda: "bbbbbbbb-bbbb-4bbb-8bbb-000000000001",
        )
        body = self._worklog_body()

        with self.assertRaises(V4IntentRepositoryError) as caught:
            unwired.add_worklog(body, "worklog.facts.0002")
        self.assertEqual("CHECKPOINT_FACTS_UNAVAILABLE", caught.exception.code)
        self.assertEqual(before, self._v4_state())

        # The key never reached the ledger: the wired backend commits it fresh instead of replaying.
        committed = self.v4.add_worklog(body, "worklog.facts.0002")
        self.assertEqual(201, committed["status"])
        self.assertFalse(committed["body"]["meta"]["replayed"])

        # Only worklog entries need the builder; a check-in on the unwired backend still commits.
        checkin = unwired.checkin({"date": TODAY, "time": "09:30"}, "checkin.facts.0001")
        self.assertEqual(201, checkin["status"])

    def test_worklog_entry_maps_builder_value_errors_to_checkpoint_facts_invalid(self) -> None:
        self.v4.add_worklog(self._worklog_body(), "worklog.facts.0001")
        before = self._v4_state()
        body = self._worklog_body()
        failures = (
            ("worklog.facts.0002", ValueError("builder refused the entry")),
            ("worklog.facts.0003", CheckpointChangeError()),
        )
        for key, failure in failures:
            with self.subTest(error=type(failure).__name__):
                builder = _RejectingCheckpointFacts(failure)
                rejecting = V4IntentRepository(
                    self.runtime,
                    enable_v4_intents=True,
                    now=lambda: self.clock[0],
                    uid_factory=lambda: "bbbbbbbb-bbbb-4bbb-8bbb-000000000001",
                    checkpoint_facts=builder,
                )
                with self.assertRaises(V4IntentRepositoryError) as caught:
                    rejecting.add_worklog(body, key)
                self.assertEqual("CHECKPOINT_FACTS_INVALID", caught.exception.code)
                self.assertIs(failure, caught.exception.__cause__)
                self.assertEqual(
                    [
                        {
                            "workspace_uid": str(self.conversion.store["workspace_uid"]),
                            "idempotency_key": key,
                            "date": TODAY,
                            "entry": {
                                "task_id": self.task["id"],
                                "task": self.task["title"],
                                "done": ["shipped"],
                                "next": ["verify"],
                                "blockers": [],
                            },
                            "ordinal": 1,
                            "prior_entries": [{"task_id": self.task["id"]}],
                            "origin": None,
                        }
                    ],
                    builder.calls,
                )
                self.assertEqual(before, self._v4_state())

        # Neither refused key reached the ledger: both commit fresh (201, not a 200 replay).
        for key, _ in failures:
            committed = self.v4.add_worklog(body, key)
            self.assertEqual(201, committed["status"])
            self.assertFalse(committed["body"]["meta"]["replayed"])


if __name__ == "__main__":
    unittest.main()
