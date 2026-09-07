"""Wave 1 D1: pure Task deletion inventory / purge plan.

Planner is in-memory only. Tests may read the populated v3 fixture copy.
They must not open the live SSOT or bind port 8765.
"""

from __future__ import annotations

import copy
import json
import random
import unittest
from pathlib import Path
from unittest import mock

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.task_deletion_plan import (
    KNOWN_NON_REFERENCE_BODY_KEYS,
    TaskDeletionPlanError,
    plan_v3_task_deletion,
    plan_v4_task_deletion,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "store-v3" / "populated"
LIVE_SSOT = Path.home() / "WorkStack" / "SSOT" / "main"
CANDIDATE_CREATED_AT = "2026-09-01T12:00:00Z"
TARGET_ID = "T-0001"
NEIGHBOR_ID = "T-00010"
TARGET_UID = "9aaaf471-bf4f-59c7-9087-2be28e899cac"
TARGET_REVISION = 2
CHILD_ID = "T-0002"

FORBIDDEN_BODIES = (
    "Normalize the SSOT",
    "Preserve every authoritative field while normalizing storage.",
    "Keep the v3 source bytes unchanged.",
    "Please confirm the rollback owner.",
    "Readable canonical data is part of the recovery contract.",
    "Rollback verification needs an explicit owner before release.",
    "This continues the release-quality gate discussion and is safe demo content.",
    "Add rollback verification to the checklist",
    "Captured the field inventory",
    "Compare the v4 projection",
    "Inventory v3 fields",
    "Exercise parent and dependency references.",
    "Verify relationships",
    "Update the release quality gate and request review.",
)

EXPECTED_V3_OPS = [
    {"display_id": TARGET_ID, "op": "remove_task_record", "uid": TARGET_UID},
    {
        "index": 0,
        "op": "remove_task_owned_note",
        "source": "task.notes",
        "task_display_id": TARGET_ID,
    },
    {
        "field": "parent_id",
        "op": "rewrite_task_field",
        "remove": TARGET_ID,
        "subject_display_id": CHILD_ID,
    },
    {
        "field": "dependencies",
        "op": "rewrite_task_field",
        "remove": TARGET_ID,
        "subject_display_id": CHILD_ID,
    },
    {"note_id": "N-0001", "op": "rewrite_note_links", "remove": TARGET_ID},
    {"op": "remove_reply", "reply_id": "R-0001"},
    {"index": 0, "op": "remove_worklog_entry", "work_date": "2026-09-01"},
    {
        "op": "remove_work_session",
        "session_id": "WS-000001",
        "work_date": "2026-09-01",
    },
    {
        "capture_id": "C-0001",
        "field": "linked_task_ids",
        "op": "unlink_capture_field",
        "remove": TARGET_ID,
    },
    {
        "capture_id": "C-0001",
        "field": "task_hints",
        "op": "unlink_capture_field",
        "remove": TARGET_ID,
    },
    {
        "action_id": "A-5b744393a6b9fd10",
        "capture_id": "C-0001",
        "op": "unlink_capture_action",
    },
    {"event_id": "E-000002", "op": "remove_activity_event"},
    {"event_id": "E-000003", "op": "remove_activity_event"},
    {"fact_id": "PS-000001", "op": "remove_planning_event"},
    {"fact_id": "PS-000003", "op": "remove_planning_event"},
    {"key": "fixture.reply.0001", "op": "content_free_idempotency"},
]


def _assert_not_live_ssot() -> None:
    resolved = FIXTURE.resolve()
    live = LIVE_SSOT.resolve() if LIVE_SSOT.exists() else LIVE_SSOT
    if resolved == live or live in resolved.parents or resolved in live.parents:
        raise AssertionError("refusing to use live SSOT path: {0}".format(resolved))


def _load_populated() -> dict[str, dict]:
    _assert_not_live_ssot()
    documents = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURE.glob("*.json"))
    }
    if "backlog.json" not in documents:
        raise AssertionError("missing populated fixture backlog.json")
    return documents


def _freeze_ops(operations: list[dict]) -> list[dict]:
    payloads = [{key: item[key] for key in sorted(item)} for item in operations]
    payloads.sort(key=lambda item: canonical_json_bytes(item))
    return payloads


def _v4_physical(documents: dict[str, dict]) -> dict[str, object]:
    conversion = convert_v3_documents(
        documents, candidate_created_at=CANDIDATE_CREATED_AT
    )
    return {
        "records": {kind: [dict(item) for item in conversion.records[kind]] for kind in conversion.records},
        "streams": {kind: [dict(item) for item in conversion.streams[kind]] for kind in conversion.streams},
        "idempotency_ledger": copy.deepcopy(dict(conversion.idempotency_ledger)),
    }


def _shuffle_value(value: object, rng: random.Random) -> object:
    if isinstance(value, dict):
        items = [(key, _shuffle_value(child, rng)) for key, child in value.items()]
        rng.shuffle(items)
        return {key: child for key, child in items}
    if isinstance(value, list):
        items = [_shuffle_value(child, rng) for child in value]
        rng.shuffle(items)
        return items
    return value


def _plan_text(plan) -> str:
    return (plan.canonical_bytes() + plan.preview_canonical_bytes()).decode("utf-8")


class PopulatedV3InventoryTest(unittest.TestCase):
    def test_t0001_inventory_is_exact_and_content_minimal(self) -> None:
        documents = _load_populated()
        before = canonical_json_bytes(documents)
        plan = plan_v3_task_deletion(
            documents, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )
        self.assertEqual(canonical_json_bytes(documents), before)
        self.assertEqual(plan.layout, "v3")
        self.assertEqual(plan.target_display_id, TARGET_ID)
        self.assertEqual(plan.target_uid, TARGET_UID)
        self.assertEqual(plan.expected_revision, TARGET_REVISION)
        self.assertEqual(plan.display_id_high_water, 2)
        self.assertEqual(
            [dict(item) for item in plan.operations],
            _freeze_ops(EXPECTED_V3_OPS),
        )
        keys = {item["key"] for item in plan.operations if item["op"] == "content_free_idempotency"}
        self.assertEqual(keys, {"fixture.reply.0001"})
        text = _plan_text(plan)
        for body in FORBIDDEN_BODIES:
            self.assertNotIn(body, text)
        self.assertNotIn("task_title", text)
        preview = plan.preview()
        self.assertEqual(preview["rewritten_task_display_ids"], [CHILD_ID])
        self.assertEqual(preview["operation_counts"]["remove_task_record"], 1)
        self.assertEqual(preview["operation_counts"]["rewrite_note_links"], 1)


class CurrentShapeV4CoverageTest(unittest.TestCase):
    def test_converted_populated_covers_uid_and_stream_records(self) -> None:
        documents = _load_populated()
        physical = _v4_physical(documents)
        before = copy.deepcopy(physical)
        plan = plan_v4_task_deletion(
            physical, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )
        self.assertEqual(physical, before)
        self.assertEqual(plan.layout, "v4")
        self.assertEqual(plan.target_uid, TARGET_UID)
        self.assertEqual(plan.display_id_high_water, 2)
        ops = [dict(item) for item in plan.operations]
        by_op: dict[str, list[dict]] = {}
        for item in ops:
            by_op.setdefault(item["op"], []).append(item)
        self.assertEqual(
            by_op["remove_task_record"],
            [{"display_id": TARGET_ID, "op": "remove_task_record", "uid": TARGET_UID}],
        )
        rewrite_fields = {item["field"] for item in by_op["rewrite_task_field"]}
        self.assertEqual(rewrite_fields, {"parent_uid", "dependency_uids"})
        for item in by_op["rewrite_task_field"]:
            self.assertEqual(item["remove"], TARGET_UID)
            self.assertEqual(item["subject_display_id"], CHILD_ID)
        self.assertEqual(len(by_op["remove_task_owned_note"]), 1)
        self.assertEqual(by_op["remove_task_owned_note"][0]["source"], "records.notes")
        self.assertEqual(
            by_op["rewrite_note_links"],
            [{"note_id": "N-0001", "op": "rewrite_note_links", "remove": TARGET_ID}],
        )
        self.assertEqual(by_op["remove_reply"][0]["reply_id"], "R-0001")
        self.assertIn("reply_uid", by_op["remove_reply"][0])
        unlink_fields = {item["field"] for item in by_op["unlink_capture_field"]}
        self.assertEqual(unlink_fields, {"linked_task_uids", "task_hints"})
        self.assertEqual(
            by_op["unlink_capture_action"][0]["action_id"],
            "A-5b744393a6b9fd10",
        )
        event_ids = {item["event_id"] for item in by_op["remove_activity_event"]}
        self.assertEqual(event_ids, {"E-000002", "E-000003"})
        fact_ids = {item["fact_id"] for item in by_op["remove_planning_event"]}
        self.assertEqual(fact_ids, {"PS-000001", "PS-000003"})
        sessions = by_op["remove_work_session"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "WS-000001")
        self.assertEqual(sessions[0]["work_date"], "2026-09-01")
        entries = by_op["remove_worklog_entry"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["work_date"], "2026-09-01")
        self.assertIn("event_uid", entries[0])
        self.assertEqual(
            {item["key"] for item in by_op["content_free_idempotency"]},
            {"fixture.reply.0001"},
        )
        serialized = json.dumps(ops, ensure_ascii=False)
        self.assertNotIn("E-000001", serialized)
        self.assertNotIn("PS-000002", serialized)
        self.assertNotIn("fixture.capture.0001", serialized)
        text = _plan_text(plan)
        for body in FORBIDDEN_BODIES:
            self.assertNotIn(body, text)
        self.assertNotIn("task_title", text)


class FailClosedTest(unittest.TestCase):
    def test_missing_task_is_not_found(self) -> None:
        documents = _load_populated()
        before = canonical_json_bytes(documents)
        with self.assertRaises(TaskDeletionPlanError) as ctx:
            plan_v3_task_deletion(
                documents, task_id="T-9999", expected_revision=TARGET_REVISION
            )
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(canonical_json_bytes(documents), before)

    def test_revision_mismatch(self) -> None:
        documents = _load_populated()
        before = canonical_json_bytes(documents)
        with self.assertRaises(TaskDeletionPlanError) as ctx:
            plan_v3_task_deletion(documents, task_id=TARGET_ID, expected_revision=1)
        self.assertEqual(ctx.exception.code, "revision_mismatch")
        self.assertEqual(canonical_json_bytes(documents), before)

    def test_duplicate_identity(self) -> None:
        documents = _load_populated()
        documents["backlog.json"]["tasks"].append(
            copy.deepcopy(documents["backlog.json"]["tasks"][0])
        )
        before = canonical_json_bytes(documents)
        with self.assertRaises(TaskDeletionPlanError) as ctx:
            plan_v3_task_deletion(
                documents, task_id=TARGET_ID, expected_revision=TARGET_REVISION
            )
        self.assertEqual(ctx.exception.code, "duplicate_identity")
        self.assertEqual(canonical_json_bytes(documents), before)

    def test_malformed_reference_container(self) -> None:
        documents = _load_populated()
        documents["backlog.json"]["tasks"][1]["dependencies"] = TARGET_ID
        before = canonical_json_bytes(documents)
        with self.assertRaises(TaskDeletionPlanError) as ctx:
            plan_v3_task_deletion(
                documents, task_id=TARGET_ID, expected_revision=TARGET_REVISION
            )
        self.assertEqual(ctx.exception.code, "malformed_reference_container")
        self.assertEqual(canonical_json_bytes(documents), before)

    def test_unknown_unsafe_reference_refuses_partial_plan(self) -> None:
        documents = _load_populated()
        documents["backlog.json"]["tasks"][1]["related_tasks"] = [TARGET_ID]
        before = canonical_json_bytes(documents)
        with self.assertRaises(TaskDeletionPlanError) as ctx:
            plan_v3_task_deletion(
                documents, task_id=TARGET_ID, expected_revision=TARGET_REVISION
            )
        self.assertEqual(ctx.exception.code, "unknown_unsafe_reference")
        self.assertEqual(str(ctx.exception), "unknown_unsafe_reference")
        self.assertEqual(canonical_json_bytes(documents), before)

    def test_v4_unknown_reference_and_malformed_leave_input(self) -> None:
        physical = _v4_physical(_load_populated())
        physical["records"]["tasks"][0]["related_tasks"] = [TARGET_ID]
        before = copy.deepcopy(physical)
        with self.assertRaises(TaskDeletionPlanError) as ctx:
            plan_v4_task_deletion(
                physical, task_id=TARGET_ID, expected_revision=TARGET_REVISION
            )
        self.assertEqual(ctx.exception.code, "unknown_unsafe_reference")
        self.assertEqual(physical, before)
        physical["records"]["tasks"][0].pop("related_tasks")
        physical["records"]["tasks"][1]["dependency_uids"] = TARGET_UID
        before = copy.deepcopy(physical)
        with self.assertRaises(TaskDeletionPlanError) as ctx:
            plan_v4_task_deletion(
                physical, task_id=TARGET_ID, expected_revision=TARGET_REVISION
            )
        self.assertEqual(ctx.exception.code, "malformed_reference_container")
        self.assertEqual(physical, before)


class DeletionPlanDeterminismAndMinimizationTest(unittest.TestCase):
    def test_repeated_and_shuffled_inputs_are_byte_identical(self) -> None:
        documents = _load_populated()
        first = plan_v3_task_deletion(
            documents, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )
        second = plan_v3_task_deletion(
            documents, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(first.preview_canonical_bytes(), second.preview_canonical_bytes())
        shuffled = _shuffle_value(copy.deepcopy(documents), random.Random(20260904))
        third = plan_v3_task_deletion(
            shuffled, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )
        self.assertEqual(first.canonical_bytes(), third.canonical_bytes())
        physical = _v4_physical(documents)
        v4_first = plan_v4_task_deletion(
            physical, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )
        shuffled_physical = _shuffle_value(copy.deepcopy(physical), random.Random(17))
        v4_second = plan_v4_task_deletion(
            shuffled_physical, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )
        self.assertEqual(v4_first.canonical_bytes(), v4_second.canonical_bytes())

    def test_planner_does_not_open_files_or_read_clocks(self) -> None:
        documents = _load_populated()
        physical = _v4_physical(documents)

        def refuse_open(*_args, **_kwargs):
            raise AssertionError("planner opened a file")

        def refuse_clock(*_args, **_kwargs):
            raise AssertionError("planner read a clock")

        with mock.patch("builtins.open", refuse_open), mock.patch(
            "time.time", refuse_clock
        ), mock.patch("time.monotonic", refuse_clock):
            plan_v3_task_deletion(
                documents, task_id=TARGET_ID, expected_revision=TARGET_REVISION
            )
            plan_v4_task_deletion(
                physical, task_id=TARGET_ID, expected_revision=TARGET_REVISION
            )

    def test_serialized_plan_omits_bodies_and_input_stays_byte_equivalent(self) -> None:
        documents = _load_populated()
        before = canonical_json_bytes(documents)
        plan = plan_v3_task_deletion(
            documents, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )
        self.assertEqual(canonical_json_bytes(documents), before)
        text = _plan_text(plan)
        for body in FORBIDDEN_BODIES:
            self.assertNotIn(body, text)
        self.assertNotIn('"title"', plan.canonical_bytes().decode("utf-8"))
        self.assertNotIn('"detail"', plan.canonical_bytes().decode("utf-8"))
        self.assertNotIn('"text"', plan.canonical_bytes().decode("utf-8"))
        self.assertNotIn('"body"', plan.canonical_bytes().decode("utf-8"))
        poisoned = copy.deepcopy(documents)
        poisoned["notes.json"]["notes"][0]["related_tasks"] = [TARGET_ID]
        poisoned_before = canonical_json_bytes(poisoned)
        with self.assertRaises(TaskDeletionPlanError):
            plan_v3_task_deletion(
                poisoned, task_id=TARGET_ID, expected_revision=TARGET_REVISION
            )
        self.assertEqual(canonical_json_bytes(poisoned), poisoned_before)
        self.assertEqual(canonical_json_bytes(documents), before)


def _idempotency_keys(plan) -> set[str]:
    return {item["key"] for item in plan.operations if item["op"] == "content_free_idempotency"}


def _neighbor_v3_ledger_record() -> dict:
    return {
        "created_at": "2026-09-01T03:00:00Z",
        "key": "fixture.unrelated.T-00010",
        "method": "POST",
        "path": "/api/v1/tasks/T-00010",
        "request_digest": "sha256:" + ("e" * 64),
        "response_body": {
            "data": {"id": NEIGHBOR_ID},
            "meta": {"note": "substring T-0001 must not select this record"},
        },
        "response_status": 201,
    }


def _neighbor_v4_ledger_record() -> dict:
    return {
        "key": "fixture.unrelated.T-00010",
        "method": "POST",
        "path": "/api/v1/tasks/T-00010",
        "request_digest": "sha256:" + ("e" * 64),
        "response_status": 201,
        "created_at": "2026-09-01T03:00:00Z",
        "response_body": {"data": {"id": NEIGHBOR_ID}},
    }


class AdversarialReviewFixTest(unittest.TestCase):
    def test_allowlist_is_explicit_for_known_body_fields(self) -> None:
        required = {"title", "detail", "text", "body", "summary", "context", "task_title"}
        self.assertTrue(required <= set(KNOWN_NON_REFERENCE_BODY_KEYS))

    def test_v3_ledger_t00010_does_not_collide_with_t0001(self) -> None:
        documents = _load_populated()
        documents["activity.json"]["idempotency"].append(_neighbor_v3_ledger_record())
        before = canonical_json_bytes(documents)
        plan = plan_v3_task_deletion(
            documents, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )
        self.assertEqual(canonical_json_bytes(documents), before)
        self.assertEqual(_idempotency_keys(plan), {"fixture.reply.0001"})
        self.assertEqual(
            [dict(item) for item in plan.operations],
            _freeze_ops(EXPECTED_V3_OPS),
        )

    def test_v4_ledger_t00010_does_not_collide_with_t0001(self) -> None:
        physical = _v4_physical(_load_populated())
        physical["idempotency_ledger"]["records"].append(_neighbor_v4_ledger_record())
        before = copy.deepcopy(physical)
        plan = plan_v4_task_deletion(
            physical, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )
        self.assertEqual(physical, before)
        self.assertEqual(_idempotency_keys(plan), {"fixture.reply.0001"})

    def test_nested_v4_record_unknown_target_uid_is_refused(self) -> None:
        physical = _v4_physical(_load_populated())
        physical["records"]["captures"][0]["wrapper"] = {
            "mystery_task_uid": TARGET_UID
        }
        before = copy.deepcopy(physical)
        with self.assertRaises(TaskDeletionPlanError) as ctx:
            plan_v4_task_deletion(
                physical, task_id=TARGET_ID, expected_revision=TARGET_REVISION
            )
        self.assertEqual(ctx.exception.code, "unknown_unsafe_reference")
        self.assertEqual(physical, before)

    def test_nested_v4_stream_unknown_target_uid_is_refused(self) -> None:
        physical = _v4_physical(_load_populated())
        physical["streams"]["activity"][0]["wrapper"] = {
            "mystery_task_uid": TARGET_UID
        }
        before = copy.deepcopy(physical)
        with self.assertRaises(TaskDeletionPlanError) as ctx:
            plan_v4_task_deletion(
                physical, task_id=TARGET_ID, expected_revision=TARGET_REVISION
            )
        self.assertEqual(ctx.exception.code, "unknown_unsafe_reference")
        self.assertEqual(physical, before)

    def test_body_prose_mentioning_t0001_is_not_a_structural_reference(self) -> None:
        documents = _load_populated()
        documents["backlog.json"]["tasks"][1]["detail"] = (
            "Coordinate with T-0001 after the review without a structural link."
        )
        documents["notes.json"]["notes"][0]["text"] = (
            "Readable canonical data mentions T-0001 only in prose."
        )
        documents["replies.json"]["replies"][0]["body"] = (
            "Please confirm the rollback owner for T-0001 in narrative text."
        )
        before = canonical_json_bytes(documents)
        plan = plan_v3_task_deletion(
            documents, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )
        self.assertEqual(canonical_json_bytes(documents), before)
        self.assertEqual(
            [dict(item) for item in plan.operations],
            _freeze_ops(EXPECTED_V3_OPS),
        )
