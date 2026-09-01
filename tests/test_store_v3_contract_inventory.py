from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack.planning_status import validate_and_project
from workstack.store import DEFAULTS, Store


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "store-v3"
EXPECTED_SEMANTIC_DIGEST = (
    "sha256:cca698f3d4137f0f4220eaa22102c6625a1e0de439cea364e9c0bcca0f15b36f"
)


def _load_fixture(name: str) -> dict[str, dict]:
    root = FIXTURES / name
    return {
        filename: json.loads((root / filename).read_text(encoding="utf-8"))
        for filename in sorted(DEFAULTS)
    }


def _record_fields(records: list[dict]) -> set[str]:
    return {field for record in records for field in record}


def _semantic_snapshot(documents: dict[str, dict]) -> dict:
    backlog = documents["backlog.json"]
    activity = documents["activity.json"]
    return {
        "format": "workstack.v3.semantic-snapshot",
        "workspace": copy.deepcopy(documents["workspace.json"]),
        "tasks": copy.deepcopy(backlog["tasks"]),
        "planning_status": validate_and_project(backlog, activity),
        "objectives": copy.deepcopy(documents["okr.json"]["objectives"]),
        "worklog_days": copy.deepcopy(documents["worklog.json"]["days"]),
        "notes": copy.deepcopy(documents["notes.json"]["notes"]),
        "captures": copy.deepcopy(documents["captures.json"]["captures"]),
        "replies": copy.deepcopy(documents["replies.json"]["replies"]),
        "activity": copy.deepcopy(activity["activity"]),
        "idempotency": copy.deepcopy(activity["idempotency"]),
        "planning_facts": copy.deepcopy(activity["planning_status"]),
    }


def _canonical_digest(value: dict) -> str:
    body = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


class StoreV3ContractInventoryTest(unittest.TestCase):
    def _initialize_copy(self, fixture_name: str) -> tuple[Store, dict[str, str]]:
        documents = _load_fixture(fixture_name)
        root = Path(self.temporary.name) / fixture_name
        root.mkdir()
        for filename in documents:
            (root / filename).write_bytes((FIXTURES / fixture_name / filename).read_bytes())
        before = {
            filename: hashlib.sha256((root / filename).read_bytes()).hexdigest()
            for filename in documents
        }
        runtime = Path(self.temporary.name) / "runtime" / fixture_name
        with mock.patch.dict(os.environ, {"WORK_STACK_RUNTIME": str(runtime)}):
            store = Store(root)
            readiness = store.initialize()
        self.assertEqual(readiness.schema_version, 3)
        after = {
            filename: hashlib.sha256((root / filename).read_bytes()).hexdigest()
            for filename in documents
        }
        self.assertEqual(after, before)
        return store, before

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_empty_and_populated_fixtures_are_ready_without_source_mutation(self) -> None:
        empty, _ = self._initialize_copy("empty")
        populated, _ = self._initialize_copy("populated")

        self.assertEqual(empty.readiness.task_count, 0)
        self.assertEqual(populated.readiness.task_count, 2)
        self.assertEqual(
            populated.readiness.workspace_uid,
            "11111111-1111-4111-8111-111111111111",
        )

    def test_document_and_record_field_inventory_is_frozen(self) -> None:
        documents = _load_fixture("populated")
        self.assertEqual(set(documents), set(DEFAULTS))
        self.assertEqual(set(documents["workspace.json"]), {"version", "id", "name"})
        self.assertEqual(set(documents["backlog.json"]), {"version", "tasks"})
        self.assertEqual(
            set(documents["store-meta.json"]),
            {"version", "store_schema_version", "migrations"},
        )
        self.assertEqual(set(documents["okr.json"]), {"version", "objectives"})
        self.assertEqual(set(documents["worklog.json"]), {"version", "days"})
        self.assertEqual(set(documents["notes.json"]), {"version", "notes"})
        self.assertEqual(set(documents["captures.json"]), {"version", "captures"})
        self.assertEqual(set(documents["replies.json"]), {"version", "replies"})
        self.assertEqual(
            set(documents["activity.json"]),
            {"version", "activity", "idempotency", "planning_status"},
        )

        tasks = documents["backlog.json"]["tasks"]
        self.assertEqual(
            _record_fields(tasks),
            {
                "id", "uid", "title", "detail", "status", "priority", "due",
                "scheduled", "estimate_minutes", "tags", "objective_ids",
                "parent_id", "dependencies", "subtasks", "notes", "created",
                "updated_at", "revision", "status_fact_id",
            },
        )
        self.assertEqual(
            _record_fields(tasks[0]["subtasks"]),
            {"id", "title", "priority", "status"},
        )
        self.assertEqual(_record_fields(tasks[0]["notes"]), {"date", "text"})

        objectives = documents["okr.json"]["objectives"]
        self.assertEqual(
            _record_fields(objectives),
            {
                "id", "quarter", "objective", "status", "key_results", "created",
                "updated_at", "revision",
            },
        )
        self.assertEqual(
            _record_fields(objectives[0]["key_results"]),
            {"id", "text", "target", "progress", "status"},
        )

        days = list(documents["worklog.json"]["days"].values())
        self.assertEqual(_record_fields(days), {"start_time", "entries", "sessions"})
        self.assertEqual(
            _record_fields(days[0]["entries"]),
            {
                "task_id", "task", "done", "next", "blockers", "session_id",
                "duration_seconds",
            },
        )
        self.assertEqual(
            _record_fields(days[0]["sessions"]),
            {
                "id", "task_id", "task", "date", "state", "started_at",
                "updated_at", "segments", "worklog_state",
            },
        )
        self.assertEqual(
            _record_fields(days[0]["sessions"][0]["segments"]),
            {"started_at", "ended_at"},
        )
        self.assertEqual(
            _record_fields(documents["notes.json"]["notes"]),
            {"id", "text", "links", "created"},
        )

        capture = documents["captures.json"]["captures"][0]
        self.assertEqual(
            set(capture),
            {
                "id", "schema_version", "source_key", "source", "normalized",
                "task_hints", "provenance", "status", "linked_task_ids",
                "converted_task_ids", "revision", "created_at", "updated_at",
                "recent_revisions",
            },
        )
        self.assertEqual(
            set(capture["source"]),
            {
                "provider", "resource_type", "connection_ref", "container_ref",
                "object_ref", "version_ref", "display_title", "web_url",
                "retrieved_at", "fingerprint",
            },
        )
        self.assertEqual(
            set(capture["normalized"]),
            {"summary", "context", "action_items", "tags"},
        )
        self.assertEqual(
            _record_fields(capture["normalized"]["action_items"]),
            {"id", "title", "detail", "priority", "due", "task_id"},
        )
        self.assertEqual(
            set(capture["provenance"]),
            {
                "capture_mode", "adapter", "adapter_version", "model",
                "prompt_version", "redaction_policy_version", "tool_trace_digest",
                "allowed_tools", "raw_retained", "created_at",
            },
        )
        self.assertEqual(
            _record_fields(capture["recent_revisions"]),
            {
                "fingerprint", "version_ref", "retrieved_at", "provenance_digest",
                "redaction_policy_version",
            },
        )

        reply = documents["replies.json"]["replies"][0]
        self.assertEqual(
            set(reply),
            {
                "id", "task_id", "capture_id", "capture_revision", "provider",
                "capability", "target", "body", "body_digest", "target_digest",
                "state", "approved_at", "receipt", "created_at", "updated_at",
            },
        )
        self.assertEqual(
            set(reply["target"]),
            {
                "resource_type", "connection_ref", "container_ref", "object_ref",
                "version_ref",
            },
        )
        self.assertEqual(
            set(reply["receipt"]),
            {
                "schema_version", "reply_id", "provider", "outcome", "occurred_at",
                "body_digest", "target_digest", "remote_message_ref", "web_url",
            },
        )

        activity = documents["activity.json"]
        self.assertEqual(
            _record_fields(activity["activity"]),
            {"id", "type", "created_at", "details", "capture_id", "task_id", "reply_id"},
        )
        self.assertEqual(
            _record_fields(activity["idempotency"]),
            {
                "key", "method", "path", "request_digest", "response_status",
                "created_at", "response_body", "response_ref", "response_meta",
            },
        )
        self.assertEqual(
            _record_fields(activity["planning_status"]),
            {
                "id", "type", "task_id", "task_uid", "previous_fact_id",
                "prior_revision", "new_revision", "prior_status", "status",
                "created_at", "actor", "provenance",
            },
        )

    def test_semantic_snapshot_digest_and_status_projection_are_frozen(self) -> None:
        snapshot = _semantic_snapshot(_load_fixture("populated"))

        self.assertEqual(
            snapshot["planning_status"],
            {"T-0001": "started", "T-0002": "open"},
        )
        self.assertEqual(_canonical_digest(snapshot), EXPECTED_SEMANTIC_DIGEST)


if __name__ == "__main__":
    unittest.main()
