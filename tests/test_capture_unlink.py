from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from workstack.capture_observations import OBSERVATION_SCHEMA
from workstack.capture_unlink_policy import (
    CaptureUnlinkPlan,
    CaptureUnlinkPolicyError,
    CaptureUnlinkUndoPlan,
    UNLINK_INVALID_REVISION,
    UNLINK_REVISION_CONFLICT,
    UNLINK_REVISION_EXHAUSTED,
    UNLINK_UNDO_CONFLICT,
    admit_displayed_capture_revision,
    advance_capture_revision,
    plan_capture_unlink,
    plan_capture_unlink_undo,
)
from workstack.capture_unlink_receipt import (
    EVENT_TYPE as UNLINK_RECEIPT_EVENT,
    capture_row_digest,
    serialize_receipt,
    validate_receipt,
)
from workstack.cli_capabilities import EXCLUSIONS
from workstack.knowledge_verification_protocol import REQUEST_SCHEMA, RESULT_SCHEMA
from workstack.service import (
    CaptureUnlinkUndoConflictError,
    DomainError,
    IdempotencyConflictError,
    NotFoundError,
    RevisionConflictError,
    RevisionExhaustedError,
    WorkStack,
)
from workstack.store import MAX_REVISION, Store
from workstack.store_document_validation import MAX_REVISION as DOCUMENT_MAX_REVISION


ROOT = Path(__file__).resolve().parents[1]
PACKET = ROOT / "contracts" / "capture-packet-v1.fixture.json"


def _packet() -> dict:
    return json.loads(PACKET.read_text(encoding="utf-8"))


def _capture_reasons(detail: dict, capture_id: str, task_id: str) -> list[str] | None:
    for item in detail["context"]:
        if item.get("ref", {}).get("kind") != "capture" or item.get("id") != capture_id:
            continue
        for connection in item["connections"]:
            target = connection["target"]
            if target.get("kind") == "task" and target.get("id") == task_id:
                return list(connection["reasons"])
    return None


def _unlinked_events(store: Store) -> list[dict]:
    return [
        event
        for event in store.load("activity.json")["activity"]
        if event.get("type") == "capture.unlinked"
    ]


def _receipt_events(store: Store) -> list[dict]:
    return [
        event
        for event in store.load("activity.json")["activity"]
        if event.get("type") == UNLINK_RECEIPT_EVENT
    ]


def _undone_events(store: Store) -> list[dict]:
    return [
        event
        for event in store.load("activity.json")["activity"]
        if event.get("type") == "capture.unlink_undone"
    ]


def _observation_record(workspace_uid: str, capture_id: str, revision: int) -> dict:
    verification_id = "00000000-0000-4000-8000-000000000001"
    accepted_at = "2026-09-09T12:00:10Z"
    nas_version = "sha256-" + ("a" * 64)
    return {
        "schema": OBSERVATION_SCHEMA,
        "capture_digest": "sha256:" + ("ab" * 32),
        "verifier_alias": "od-primary",
        "accepted_at": accepted_at,
        "request": {
            "schema": REQUEST_SCHEMA,
            "verification_id": verification_id,
            "binding": {
                "workspace_uid": workspace_uid,
                "capture_id": capture_id,
                "capture_revision": revision,
            },
            "connection": {
                "alias": "od-primary",
                "upstream_workspace_uid": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
                "policy_revision": 7,
            },
            "corpus_refs": ["engineering"],
            "evidence": [
                {
                    "document_ref": "docalpha0001",
                    "source_type": "nas.file",
                    "expected_source_version": nas_version,
                }
            ],
            "requested_at": "2026-09-09T12:00:00Z",
            "expires_at": "2026-09-09T12:01:00Z",
        },
        "result": {
            "schema": RESULT_SCHEMA,
            "verification_id": verification_id,
            "checked_at": accepted_at,
            "evidence": [
                {
                    "document_ref": "docalpha0001",
                    "source_type": "nas.file",
                    "expected_source_version": nas_version,
                    "observed_source_version": nas_version,
                    "status": "current",
                    "code": "hash_matched",
                }
            ],
        },
    }


class CaptureUnlinkPolicyTest(unittest.TestCase):
    def test_displayed_revision_refuses_bool_float_and_out_of_range(self) -> None:
        self.assertEqual(admit_displayed_capture_revision(0), 0)
        self.assertEqual(admit_displayed_capture_revision(MAX_REVISION), MAX_REVISION)
        for value in (True, False, 1.0, "1", -1, MAX_REVISION + 1):
            with self.subTest(value=value):
                with self.assertRaises(CaptureUnlinkPolicyError) as raised:
                    admit_displayed_capture_revision(value)
                self.assertEqual(raised.exception.code, UNLINK_INVALID_REVISION)

    def test_cas_runs_before_absent_link_and_inbox_only_when_linked_and_empty(self) -> None:
        with self.assertRaises(CaptureUnlinkPolicyError) as stale:
            plan_capture_unlink(
                linked_task_ids=(),
                converted_task_ids=(),
                status="linked",
                task_id="T-0001",
                stored_revision=1,
                displayed_revision=0,
            )
        self.assertEqual(stale.exception.code, UNLINK_REVISION_CONFLICT)

        absent = plan_capture_unlink(
            linked_task_ids=("T-0002",),
            converted_task_ids=(),
            status="linked",
            task_id="T-0001",
            stored_revision=1,
            displayed_revision=1,
        )
        self.assertEqual(
            absent,
            CaptureUnlinkPlan(
                duplicate=True, linked_task_ids=("T-0002",), status="linked", mutate=False
            ),
        )

        last = plan_capture_unlink(
            linked_task_ids=("T-0001",),
            converted_task_ids=(),
            status="linked",
            task_id="T-0001",
            stored_revision=2,
            displayed_revision=2,
        )
        self.assertEqual(last.status, "inbox")
        self.assertTrue(last.mutate)
        self.assertEqual(last.linked_task_ids, ())

        dismissed = plan_capture_unlink(
            linked_task_ids=("T-0001",),
            converted_task_ids=(),
            status="dismissed",
            task_id="T-0001",
            stored_revision=3,
            displayed_revision=3,
        )
        self.assertEqual(dismissed.status, "dismissed")

        converted = plan_capture_unlink(
            linked_task_ids=("T-0001",),
            converted_task_ids=("T-0001",),
            status="converted",
            task_id="T-0001",
            stored_revision=4,
            displayed_revision=4,
        )
        self.assertEqual(converted.status, "converted")
        self.assertEqual(converted.linked_task_ids, ())

    def test_undo_plan_restores_sorted_link_and_conflicts_on_digest_or_present_link(
        self,
    ) -> None:
        with self.assertRaises(CaptureUnlinkPolicyError) as stale:
            plan_capture_unlink_undo(
                linked_task_ids=(),
                stored_revision=2,
                displayed_revision=1,
                after_revision=2,
                stored_digest="sha256:" + ("aa" * 32),
                after_digest="sha256:" + ("aa" * 32),
                task_id="T-0001",
                status_before="linked",
            )
        self.assertEqual(stale.exception.code, UNLINK_REVISION_CONFLICT)

        digest = "sha256:" + ("aa" * 32)
        with self.assertRaises(CaptureUnlinkPolicyError) as changed:
            plan_capture_unlink_undo(
                linked_task_ids=(),
                stored_revision=2,
                displayed_revision=2,
                after_revision=2,
                stored_digest="sha256:" + ("bb" * 32),
                after_digest=digest,
                task_id="T-0001",
                status_before="linked",
            )
        self.assertEqual(changed.exception.code, UNLINK_UNDO_CONFLICT)

        with self.assertRaises(CaptureUnlinkPolicyError) as present:
            plan_capture_unlink_undo(
                linked_task_ids=("T-0001",),
                stored_revision=2,
                displayed_revision=2,
                after_revision=2,
                stored_digest=digest,
                after_digest=digest,
                task_id="T-0001",
                status_before="linked",
            )
        self.assertEqual(present.exception.code, UNLINK_UNDO_CONFLICT)

        restored = plan_capture_unlink_undo(
            linked_task_ids=("T-0002",),
            stored_revision=2,
            displayed_revision=2,
            after_revision=2,
            stored_digest=digest,
            after_digest=digest,
            task_id="T-0001",
            status_before="linked",
        )
        self.assertEqual(
            restored,
            CaptureUnlinkUndoPlan(linked_task_ids=("T-0001", "T-0002"), status="linked"),
        )
        with self.assertRaises(CaptureUnlinkPolicyError) as exhausted:
            advance_capture_revision(MAX_REVISION)
        self.assertEqual(exhausted.exception.code, UNLINK_REVISION_EXHAUSTED)


class CaptureUnlinkStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.stack = WorkStack(self.store)
        ingested = self.stack.ingest_capture(_packet(), "unlink.ingest.0001")
        self.capture = ingested["body"]["data"]
        self.first = self.stack.add_task("First linked task")
        self.second = self.stack.add_task("Second linked task")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _link(self, task_id: str, key: str) -> dict:
        return self.stack.link_capture(self.capture["id"], task_id, key)["body"]["data"]

    def test_unlink_drops_only_the_named_explicit_link_and_returns_inbox(self) -> None:
        linked = self._link(self.first["id"], "unlink.link.one")
        source = dict(linked["source"])
        notes_before = self.store.load("notes.json")
        tasks_before = [task["id"] for task in self.store.load("backlog.json")["tasks"]]

        result = self.stack.unlink_capture(
            self.capture["id"], self.first["id"], linked["revision"], "unlink.key.one"
        )
        body = result["body"]
        self.assertEqual(result["status"], 200)
        self.assertFalse(body["meta"]["duplicate"])
        self.assertEqual(body["data"]["linked_task_ids"], [])
        self.assertEqual(body["data"]["status"], "inbox")
        self.assertEqual(body["data"]["revision"], linked["revision"] + 1)
        self.assertEqual(body["data"]["source"], source)
        self.assertEqual(body["data"]["converted_task_ids"], [])

        stored = self.store.load("captures.json")["captures"][0]
        self.assertEqual(stored["id"], self.capture["id"])
        self.assertEqual(stored["status"], "inbox")
        self.assertEqual(stored["linked_task_ids"], [])
        self.assertEqual(stored["source"], source)
        self.assertEqual(
            [task["id"] for task in self.store.load("backlog.json")["tasks"]],
            tasks_before,
        )
        self.assertEqual(self.store.load("notes.json"), notes_before)
        self.assertTrue(any(item["id"] == self.capture["id"] for item in self.stack.list_captures("inbox")))
        self.assertIsNone(
            _capture_reasons(
                self.stack.task_detail(self.first["id"]), self.capture["id"], self.first["id"]
            )
        )
        events = _unlinked_events(self.store)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["capture_id"], self.capture["id"])
        self.assertEqual(events[0]["task_id"], self.first["id"])
        self.assertNotIn("source", events[0])
        self.assertFalse(events[0].get("details"))
        self.assertIn("undo_receipt_id", body["meta"])
        receipts = _receipt_events(self.store)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["details"].keys(), {"receipt"})
        self.assertEqual(receipts[0]["capture_id"], self.capture["id"])
        self.assertEqual(receipts[0]["task_id"], self.first["id"])

    def test_other_task_links_stay_and_absent_link_is_duplicate(self) -> None:
        self._link(self.first["id"], "unlink.link.first")
        linked = self._link(self.second["id"], "unlink.link.second")
        removed = self.stack.unlink_capture(
            self.capture["id"], self.first["id"], linked["revision"], "unlink.drop.first"
        )["body"]["data"]
        self.assertEqual(removed["linked_task_ids"], [self.second["id"]])
        self.assertEqual(removed["status"], "linked")
        self.assertEqual(
            _capture_reasons(
                self.stack.task_detail(self.second["id"]), self.capture["id"], self.second["id"]
            ),
            ["capture-link"],
        )

        duplicate = self.stack.unlink_capture(
            self.capture["id"], self.first["id"], removed["revision"], "unlink.absent"
        )
        self.assertTrue(duplicate["body"]["meta"]["duplicate"])
        self.assertNotIn("undo_receipt_id", duplicate["body"]["meta"])
        self.assertEqual(duplicate["body"]["data"]["revision"], removed["revision"])
        self.assertEqual(len(_unlinked_events(self.store)), 1)
        self.assertEqual(len(_receipt_events(self.store)), 1)
        self.assertEqual(
            self.store.load("captures.json")["captures"][0]["linked_task_ids"],
            [self.second["id"]],
        )

    def test_stale_revision_writes_nothing_even_when_the_link_is_absent(self) -> None:
        linked = self._link(self.first["id"], "unlink.link.cas")
        before = self.store.load("captures.json")
        activity_before = self.store.load("activity.json")
        with self.assertRaises(RevisionConflictError) as present:
            self.stack.unlink_capture(
                self.capture["id"], self.first["id"], linked["revision"] - 1, "unlink.stale.link"
            )
        self.assertEqual(present.exception.code, "revision_conflict")
        with self.assertRaises(RevisionConflictError):
            self.stack.unlink_capture(
                self.capture["id"], self.second["id"], 0, "unlink.stale.absent"
            )
        self.assertEqual(self.store.load("captures.json"), before)
        self.assertEqual(self.store.load("activity.json")["activity"], activity_before["activity"])
        self.assertEqual(
            [item["key"] for item in self.store.load("activity.json")["idempotency"]],
            [item["key"] for item in activity_before["idempotency"]],
        )

    def test_same_key_replays_after_mutation_and_changed_body_conflicts(self) -> None:
        linked = self._link(self.first["id"], "unlink.link.replay")
        first = self.stack.unlink_capture(
            self.capture["id"], self.first["id"], linked["revision"], "unlink.replay.key"
        )
        replay = self.stack.unlink_capture(
            self.capture["id"], self.first["id"], linked["revision"], "unlink.replay.key"
        )
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(replay["body"]["data"]["revision"], first["body"]["data"]["revision"])
        self.assertEqual(
            replay["body"]["meta"]["undo_receipt_id"],
            first["body"]["meta"]["undo_receipt_id"],
        )
        self.assertEqual(len(_unlinked_events(self.store)), 1)
        self.assertEqual(len(_receipt_events(self.store)), 1)
        with self.assertRaises(IdempotencyConflictError):
            self.stack.unlink_capture(
                self.capture["id"], self.second["id"], linked["revision"], "unlink.replay.key"
            )

    def test_dual_reason_keeps_conversion_card_and_conversion_only_is_unmodified(self) -> None:
        converted = self.stack.create_task_from_capture(
            self.capture["id"],
            {"title": "Converted from the capture"},
            "unlink.convert.dual",
        )["body"]["data"]
        linked = self.stack.link_capture(
            self.capture["id"], converted["id"], "unlink.link.dual"
        )["body"]["data"]
        self.assertIn(converted["id"], linked["linked_task_ids"])
        self.assertIn(converted["id"], linked["converted_task_ids"])
        self.assertEqual(
            _capture_reasons(
                self.stack.task_detail(converted["id"]), self.capture["id"], converted["id"]
            ),
            ["capture-link", "capture-conversion"],
        )
        action_ids = {
            item["id"]: item.get("task_id")
            for item in self.store.load("captures.json")["captures"][0]["normalized"]["action_items"]
        }

        dropped = self.stack.unlink_capture(
            self.capture["id"], converted["id"], linked["revision"], "unlink.drop.dual"
        )["body"]["data"]
        self.assertNotIn(converted["id"], dropped["linked_task_ids"])
        self.assertEqual(dropped["converted_task_ids"], linked["converted_task_ids"])
        self.assertEqual(dropped["status"], "converted")
        self.assertEqual(
            _capture_reasons(
                self.stack.task_detail(converted["id"]), self.capture["id"], converted["id"]
            ),
            ["capture-conversion"],
        )
        self.assertEqual(
            {
                item["id"]: item.get("task_id")
                for item in self.store.load("captures.json")["captures"][0]["normalized"]["action_items"]
            },
            action_ids,
        )

        only = self.stack.unlink_capture(
            self.capture["id"], converted["id"], dropped["revision"], "unlink.conversion.only"
        )
        self.assertTrue(only["body"]["meta"]["duplicate"])
        self.assertEqual(only["body"]["data"]["converted_task_ids"], dropped["converted_task_ids"])
        self.assertEqual(only["body"]["data"]["revision"], dropped["revision"])
        self.assertEqual(only["body"]["data"]["status"], "converted")
        self.assertEqual(len(_unlinked_events(self.store)), 1)

    def test_dismissed_status_survives_unlink_of_the_last_explicit_link(self) -> None:
        linked = self._link(self.first["id"], "unlink.link.dismiss")
        dismissed = self.stack.dismiss_capture(self.capture["id"], "unlink.dismiss")["body"]["data"]
        result = self.stack.unlink_capture(
            self.capture["id"], self.first["id"], dismissed["revision"], "unlink.after.dismiss"
        )["body"]["data"]
        self.assertEqual(result["status"], "dismissed")
        self.assertEqual(result["linked_task_ids"], [])
        self.assertEqual(result["revision"], dismissed["revision"] + 1)
        listed = self.stack.list_captures("dismissed")
        self.assertEqual(listed[0]["id"], self.capture["id"])

    def test_unknown_ids_and_bool_revision_write_nothing(self) -> None:
        before = self.store.load("captures.json")
        with self.assertRaises(NotFoundError):
            self.stack.unlink_capture("C-9999", self.first["id"], 0, "unlink.missing.capture")
        with self.assertRaises(NotFoundError):
            self.stack.unlink_capture(self.capture["id"], "T-9999", 0, "unlink.missing.task")
        with self.assertRaises(DomainError) as invalid:
            self.stack.unlink_capture(self.capture["id"], self.first["id"], True, "unlink.bool")
        self.assertEqual(invalid.exception.code, "invalid_request")
        self.assertEqual(self.store.load("captures.json"), before)

    def test_gui_only_exclusion_names_the_unlink_route(self) -> None:
        surfaces = {item.surface: item.kind for item in EXCLUSIONS}
        self.assertEqual(surfaces["POST /api/v1/captures/{id}/unlink"], "http_post")
        self.assertEqual(surfaces["POST /api/v1/captures/{id}/undo-unlink"], "http_post")
        self.assertEqual(DOCUMENT_MAX_REVISION, MAX_REVISION)

    def test_max_revision_unlink_refuses_without_receipt_or_row_mutation(self) -> None:
        linked = self._link(self.first["id"], "unlink.max.link")
        captures = self.store.load("captures.json")
        captures["captures"][0]["revision"] = MAX_REVISION
        self.store.save("captures.json", captures)
        before = {
            path.name: path.read_bytes()
            for path in Path(self.temporary.name).iterdir()
            if path.is_file()
        }
        with self.assertRaises(RevisionExhaustedError) as raised:
            self.stack.unlink_capture(
                self.capture["id"],
                self.first["id"],
                MAX_REVISION,
                "unlink.max.drop",
            )
        self.assertEqual(raised.exception.code, "revision_exhausted")
        self.assertEqual(
            before,
            {
                path.name: path.read_bytes()
                for path in Path(self.temporary.name).iterdir()
                if path.is_file()
            },
        )
        stored = self.store.load("captures.json")["captures"][0]
        self.assertEqual(stored["revision"], MAX_REVISION)
        self.assertEqual(stored["linked_task_ids"], [self.first["id"]])
        self.assertEqual(_receipt_events(self.store), [])
        self.assertEqual(_unlinked_events(self.store), [])


class CaptureUnlinkUndoStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.stack = WorkStack(self.store)
        ingested = self.stack.ingest_capture(_packet(), "undo.ingest.0001")
        self.capture = ingested["body"]["data"]
        self.first = self.stack.add_task("First undo task")
        self.second = self.stack.add_task("Second undo task")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _link(self, task_id: str, key: str) -> dict:
        return self.stack.link_capture(self.capture["id"], task_id, key)["body"]["data"]

    def _unlink(self, task_id: str, revision: int, key: str) -> dict:
        return self.stack.unlink_capture(self.capture["id"], task_id, revision, key)

    def test_undo_restores_linked_inbox_dismissed_and_converted_states(self) -> None:
        linked = self._link(self.first["id"], "undo.link.linked")
        source = dict(linked["source"])
        actions_before = {
            item["id"]: item.get("task_id")
            for item in self.store.load("captures.json")["captures"][0]["normalized"][
                "action_items"
            ]
        }
        removed = self._unlink(self.first["id"], linked["revision"], "undo.drop.linked")
        receipt_id = removed["body"]["meta"]["undo_receipt_id"]
        restored = self.stack.undo_capture_unlink(
            self.capture["id"],
            receipt_id,
            removed["body"]["data"]["revision"],
            "undo.restore.linked",
        )
        body = restored["body"]
        self.assertEqual(restored["status"], 200)
        self.assertFalse(body["meta"]["duplicate"])
        self.assertEqual(body["data"]["status"], "linked")
        self.assertEqual(body["data"]["linked_task_ids"], [self.first["id"]])
        self.assertEqual(body["data"]["source"], source)
        self.assertEqual(body["data"]["revision"], removed["body"]["data"]["revision"] + 1)
        stored = self.store.load("captures.json")["captures"][0]
        self.assertEqual(stored["status"], "linked")
        self.assertEqual(stored["linked_task_ids"], [self.first["id"]])
        self.assertEqual(
            {
                item["id"]: item.get("task_id")
                for item in stored["normalized"]["action_items"]
            },
            actions_before,
        )
        undone = _undone_events(self.store)
        self.assertEqual(len(undone), 1)
        self.assertFalse(undone[0].get("details"))
        self.assertEqual(undone[0]["task_id"], self.first["id"])

        self._link(self.second["id"], "undo.link.second")
        current = self.store.load("captures.json")["captures"][0]
        dropped_one = self._unlink(
            self.first["id"], current["revision"], "undo.drop.one-of-two"
        )
        self.assertEqual(dropped_one["body"]["data"]["status"], "linked")
        restored_two = self.stack.undo_capture_unlink(
            self.capture["id"],
            dropped_one["body"]["meta"]["undo_receipt_id"],
            dropped_one["body"]["data"]["revision"],
            "undo.restore.one-of-two",
        )["body"]["data"]
        self.assertEqual(
            restored_two["linked_task_ids"],
            sorted([self.first["id"], self.second["id"]]),
        )
        self.assertEqual(restored_two["status"], "linked")

        dismissed = self.stack.dismiss_capture(self.capture["id"], "undo.dismiss")[
            "body"
        ]["data"]
        dropped_dismissed = self._unlink(
            self.first["id"], dismissed["revision"], "undo.drop.dismissed"
        )
        self.assertEqual(dropped_dismissed["body"]["data"]["status"], "dismissed")
        restored_dismissed = self.stack.undo_capture_unlink(
            self.capture["id"],
            dropped_dismissed["body"]["meta"]["undo_receipt_id"],
            dropped_dismissed["body"]["data"]["revision"],
            "undo.restore.dismissed",
        )["body"]["data"]
        self.assertEqual(restored_dismissed["status"], "dismissed")
        self.assertIn(self.first["id"], restored_dismissed["linked_task_ids"])

        converted = self.stack.create_task_from_capture(
            self.capture["id"],
            {"title": "Converted for undo"},
            "undo.convert",
        )["body"]["data"]
        linked_converted = self.stack.link_capture(
            self.capture["id"], converted["id"], "undo.link.converted"
        )["body"]["data"]
        converted_ids = list(linked_converted["converted_task_ids"])
        dropped_converted = self._unlink(
            converted["id"], linked_converted["revision"], "undo.drop.converted"
        )["body"]
        self.assertEqual(dropped_converted["data"]["status"], "converted")
        restored_converted = self.stack.undo_capture_unlink(
            self.capture["id"],
            dropped_converted["meta"]["undo_receipt_id"],
            dropped_converted["data"]["revision"],
            "undo.restore.converted",
        )["body"]["data"]
        self.assertEqual(restored_converted["status"], "converted")
        self.assertIn(converted["id"], restored_converted["linked_task_ids"])
        self.assertEqual(restored_converted["converted_task_ids"], converted_ids)

    def test_same_key_undo_replays_and_a_second_intent_conflicts(self) -> None:
        linked = self._link(self.first["id"], "undo.link.replay")
        removed = self._unlink(self.first["id"], linked["revision"], "undo.drop.replay")
        first = self.stack.undo_capture_unlink(
            self.capture["id"],
            removed["body"]["meta"]["undo_receipt_id"],
            removed["body"]["data"]["revision"],
            "undo.replay.key",
        )
        replay = self.stack.undo_capture_unlink(
            self.capture["id"],
            removed["body"]["meta"]["undo_receipt_id"],
            removed["body"]["data"]["revision"],
            "undo.replay.key",
        )
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(replay["body"]["data"]["revision"], first["body"]["data"]["revision"])
        self.assertEqual(len(_undone_events(self.store)), 1)
        with self.assertRaises(RevisionConflictError) as second:
            self.stack.undo_capture_unlink(
                self.capture["id"],
                removed["body"]["meta"]["undo_receipt_id"],
                removed["body"]["data"]["revision"],
                "undo.replay.other",
            )
        self.assertEqual(second.exception.code, "revision_conflict")
        with self.assertRaises(IdempotencyConflictError):
            self.stack.undo_capture_unlink(
                self.capture["id"],
                removed["body"]["meta"]["undo_receipt_id"],
                first["body"]["data"]["revision"],
                "undo.replay.key",
            )

    def test_already_present_link_digest_change_missing_and_replaced_task(self) -> None:
        linked = self._link(self.first["id"], "undo.link.conflict")
        removed = self._unlink(self.first["id"], linked["revision"], "undo.drop.conflict")
        receipt_id = removed["body"]["meta"]["undo_receipt_id"]
        revision = removed["body"]["data"]["revision"]

        present = self.store.load("captures.json")
        present["captures"][0]["linked_task_ids"] = [self.first["id"]]
        self.store.save("captures.json", present)
        with self.assertRaises(CaptureUnlinkUndoConflictError) as linked_again:
            self.stack.undo_capture_unlink(
                self.capture["id"], receipt_id, revision, "undo.conflict.link"
            )
        self.assertEqual(linked_again.exception.code, "capture_unlink_undo_conflict")
        self.assertNotIn("source", linked_again.exception.details)
        self.assertNotIn("body", linked_again.exception.details)

        present["captures"][0]["linked_task_ids"] = []
        present["captures"][0]["normalized"]["summary"] = "tampered post-image"
        self.store.save("captures.json", present)
        with self.assertRaises(CaptureUnlinkUndoConflictError) as digest:
            self.stack.undo_capture_unlink(
                self.capture["id"], receipt_id, revision, "undo.conflict.digest"
            )
        self.assertEqual(digest.exception.code, "capture_unlink_undo_conflict")

        original_summary = linked["normalized"]["summary"]
        restored_row = self.store.load("captures.json")
        restored_row["captures"][0]["normalized"]["summary"] = original_summary
        restored_row["captures"][0]["linked_task_ids"] = []
        self.store.save("captures.json", restored_row)

        replacement_uid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        backlog = self.store.load("backlog.json")
        activity = self.store.load("activity.json")
        for task in backlog["tasks"]:
            if task["id"] == self.first["id"]:
                task["uid"] = replacement_uid
        for fact in activity.get("planning_status", []):
            if fact.get("task_id") == self.first["id"]:
                fact["task_uid"] = replacement_uid
        self.store.save_many({"backlog.json": backlog, "activity.json": activity})
        with self.assertRaises(CaptureUnlinkUndoConflictError) as replaced:
            self.stack.undo_capture_unlink(
                self.capture["id"], receipt_id, revision, "undo.conflict.uid"
            )
        self.assertEqual(replaced.exception.code, "capture_unlink_undo_conflict")

        backlog = self.store.load("backlog.json")
        activity = self.store.load("activity.json")
        kept_facts = [
            copy.deepcopy(fact)
            for fact in activity.get("planning_status", [])
            if fact.get("task_id") != self.first["id"]
        ]
        for index, fact in enumerate(kept_facts, start=1):
            fact["id"] = "PS-{:06d}".format(index)
        heads = {fact["task_id"]: fact["id"] for fact in kept_facts}
        backlog["tasks"] = [
            task for task in backlog["tasks"] if task["id"] != self.first["id"]
        ]
        for task in backlog["tasks"]:
            task["status_fact_id"] = heads[task["id"]]
        activity["planning_status"] = kept_facts
        self.store.save_many({"backlog.json": backlog, "activity.json": activity})
        with self.assertRaises(NotFoundError):
            self.stack.undo_capture_unlink(
                self.capture["id"], receipt_id, revision, "undo.missing.task"
            )

        with self.assertRaises(NotFoundError):
            self.stack.undo_capture_unlink(
                self.capture["id"],
                "44444444-4444-4444-8444-444444444444",
                revision,
                "undo.missing.receipt",
            )

    def test_observations_stay_byte_equal_and_do_not_bump_capture_revision(self) -> None:
        linked = self._link(self.first["id"], "undo.link.obs")
        removed = self._unlink(self.first["id"], linked["revision"], "undo.drop.obs")
        capture = self.store.load("captures.json")["captures"][0]
        revision_after_unlink = capture["revision"]
        workspace_uid = self.store.load("workspace.json")["id"]
        self.store.record_capture_observation(
            _observation_record(workspace_uid, capture["id"], revision_after_unlink)
        )
        after_observation = self.store.load("captures.json")
        self.assertEqual(after_observation["captures"][0]["revision"], revision_after_unlink)
        observations = copy.deepcopy(after_observation["observations"])
        restored = self.stack.undo_capture_unlink(
            self.capture["id"],
            removed["body"]["meta"]["undo_receipt_id"],
            revision_after_unlink,
            "undo.restore.obs",
        )
        self.assertEqual(restored["body"]["data"]["status"], "linked")
        after_undo = self.store.load("captures.json")
        self.assertEqual(after_undo["observations"], observations)
        self.assertEqual(after_undo["captures"][0]["revision"], revision_after_unlink + 1)

    def test_max_revision_undo_refuses_without_inverse_or_ledger_write(self) -> None:
        linked = self._link(self.first["id"], "undo.max.link")
        removed = self._unlink(self.first["id"], linked["revision"], "undo.max.drop")
        receipt_id = removed["body"]["meta"]["undo_receipt_id"]
        captures = self.store.load("captures.json")
        capture = captures["captures"][0]
        capture["revision"] = MAX_REVISION
        activity = self.store.load("activity.json")
        for event in activity["activity"]:
            if event.get("type") != UNLINK_RECEIPT_EVENT:
                continue
            blob = json.loads(event["details"]["receipt"])
            if blob.get("receipt_id") != receipt_id:
                continue
            blob["after_revision"] = MAX_REVISION
            blob["before_revision"] = MAX_REVISION - 1
            blob["after_digest"] = capture_row_digest(capture)
            event["details"]["receipt"] = serialize_receipt(validate_receipt(blob)).decode(
                "utf-8"
            )
        self.store.save_many({"captures.json": captures, "activity.json": activity})
        before = {
            path.name: path.read_bytes()
            for path in Path(self.temporary.name).iterdir()
            if path.is_file()
        }
        with self.assertRaises(RevisionExhaustedError) as raised:
            self.stack.undo_capture_unlink(
                self.capture["id"], receipt_id, MAX_REVISION, "undo.max.restore"
            )
        self.assertEqual(raised.exception.code, "revision_exhausted")
        self.assertEqual(
            before,
            {
                path.name: path.read_bytes()
                for path in Path(self.temporary.name).iterdir()
                if path.is_file()
            },
        )
        self.assertEqual(_undone_events(self.store), [])
        stored = self.store.load("captures.json")["captures"][0]
        self.assertEqual(stored["revision"], MAX_REVISION)
        self.assertEqual(stored["linked_task_ids"], [])

    def test_ambiguous_same_id_receipt_cannot_authorize_undo(self) -> None:
        linked = self._link(self.first["id"], "undo.ambig.link")
        removed = self._unlink(self.first["id"], linked["revision"], "undo.ambig.drop")
        receipt_id = removed["body"]["meta"]["undo_receipt_id"]
        activity = self.store.load("activity.json")
        receipt_event = next(
            event
            for event in activity["activity"]
            if event.get("type") == UNLINK_RECEIPT_EVENT
        )
        pretty = copy.deepcopy(receipt_event)
        pretty["id"] = "E-009999"
        pretty["details"] = {
            "receipt": json.dumps(json.loads(receipt_event["details"]["receipt"]), indent=2)
        }
        activity["activity"].append(pretty)
        self.store.save("activity.json", activity)
        before = {
            path.name: path.read_bytes()
            for path in Path(self.temporary.name).iterdir()
            if path.is_file()
        }
        with self.assertRaises(DomainError) as raised:
            self.stack.undo_capture_unlink(
                self.capture["id"],
                receipt_id,
                removed["body"]["data"]["revision"],
                "undo.ambig.restore",
            )
        self.assertEqual(raised.exception.code, "invalid_request")
        self.assertEqual(
            before,
            {
                path.name: path.read_bytes()
                for path in Path(self.temporary.name).iterdir()
                if path.is_file()
            },
        )
        self.assertEqual(_undone_events(self.store), [])
        self.assertEqual(
            self.store.load("captures.json")["captures"][0]["linked_task_ids"],
            [],
        )

    def test_atomic_undo_save_does_not_leave_a_half_applied_inverse(self) -> None:
        from tests.test_storage_capture_reply_contract import (
            InterruptingV3Store,
            SimulatedCommitInterruption,
        )

        root = Path(self.temporary.name) / "atomic"
        store = InterruptingV3Store(root)
        stack = WorkStack(store)
        capture = stack.ingest_capture(_packet(), "undo.atomic.ingest")["body"]["data"]
        task = stack.add_task("Atomic undo task")
        linked = stack.link_capture(capture["id"], task["id"], "undo.atomic.link")[
            "body"
        ]["data"]
        removed = stack.unlink_capture(
            capture["id"], task["id"], linked["revision"], "undo.atomic.drop"
        )
        store.interrupt_next_commit()
        with self.assertRaises(SimulatedCommitInterruption):
            stack.undo_capture_unlink(
                capture["id"],
                removed["body"]["meta"]["undo_receipt_id"],
                removed["body"]["data"]["revision"],
                "undo.atomic.restore",
            )
        recovered = WorkStack(InterruptingV3Store(root))
        documents = {
            "captures": recovered.store.load("captures.json"),
            "activity": recovered.store.load("activity.json"),
        }
        stored = documents["captures"]["captures"][0]
        undone = [
            event
            for event in documents["activity"]["activity"]
            if event.get("type") == "capture.unlink_undone"
        ]
        restored = stored["linked_task_ids"] == [task["id"]] and stored["status"] == "linked"
        self.assertEqual(bool(undone), restored)
        if restored:
            self.assertEqual(len(undone), 1)
            self.assertFalse(undone[0].get("details"))
        else:
            self.assertEqual(stored["linked_task_ids"], [])
            self.assertEqual(stored["status"], "inbox")
