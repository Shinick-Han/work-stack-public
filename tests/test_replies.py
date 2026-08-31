from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack.capture import canonical_digest, fingerprint_for, source_key_for
from workstack.service import (
    DomainError,
    ReplyReceiptConflictError,
    WorkStack,
)
from workstack.store import Store


CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def fixture(name: str = "capture-packet-v1.fixture.json") -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def percent_encoding_layers(value: str, total_layers: int) -> str:
    for _ in range(total_layers - 1):
        value = value.replace("%", "%25")
    return value


def microsoft_packet(provider: str = "microsoft-outlook") -> dict:
    packet = fixture()
    if provider == "microsoft-teams":
        packet["source"].update(
            {
                "provider": "microsoft-teams",
                "resource_type": "teams.channelMessage",
                "connection_ref": "tenant:demo",
                "container_ref": "channel:19:demo@thread.v2",
                "object_ref": "message:1740000000000",
                "version_ref": "etag:W/1234567890",
                "web_url": "https://teams.microsoft.com/l/message/19%3Ademo%40thread.v2/1740000000000",
            }
        )
        packet["provenance"]["allowed_tools"] = [
            "m365.teams.read",
            "workstack.capture.write",
        ]
        packet["source_key"] = source_key_for(packet["source"])
        packet["source"]["fingerprint"] = fingerprint_for(packet["source"])
    return packet


class ReplyServiceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.stack = WorkStack(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def linked_source(self, provider: str = "microsoft-outlook") -> tuple[dict, dict]:
        capture = self.stack.ingest_capture(
            microsoft_packet(provider), "reply.ingest.{}".format(provider)
        )["body"]["data"]
        task = self.stack.add_task("Reply source task")
        capture = self.stack.link_capture(
            capture["id"], task["id"], "reply.link.{}".format(provider)
        )["body"]["data"]
        return task, capture

    def approve(
        self,
        task: dict,
        capture: dict,
        key: str = "reply.approve.0001",
        body: str = "Thanks. I will send the revised review by Friday.",
    ) -> dict:
        return self.stack.approve_reply(
            {
                "task_id": task["id"],
                "capture_id": capture["id"],
                "body": body,
                "approved": True,
            },
            key,
        )["body"]["data"]

    @staticmethod
    def receipt(reply: dict, outcome: str = "sent") -> dict:
        value = {
            "schema_version": "1.0",
            "reply_id": reply["id"],
            "provider": reply["provider"],
            "outcome": outcome,
            "occurred_at": "2026-08-29T00:15:00Z",
            "body_digest": reply["body_digest"],
            "target_digest": reply["target_digest"],
        }
        if outcome == "sent":
            value.update(
                {
                    "remote_message_ref": "message:opaque-reply-001",
                    "web_url": "https://outlook.office.com/mail/deeplink/read/opaque-reply-001",
                }
            )
        else:
            value["error_code"] = "delivery_{}".format(outcome)
        return value

    def test_approval_derives_and_freezes_source_target_and_digests(self):
        task, capture = self.linked_source()
        body = "Thanks.\nI will send the revision Friday."
        reply = self.approve(task, capture, body=body)

        expected_target = {
            field: capture["source"][field]
            for field in (
                "resource_type",
                "connection_ref",
                "container_ref",
                "object_ref",
                "version_ref",
            )
        }
        self.assertEqual(reply["capture_revision"], capture["revision"])
        self.assertEqual(reply["target"], expected_target)
        self.assertEqual(reply["target_digest"], canonical_digest(expected_target))
        self.assertEqual(reply["body_digest"], canonical_digest(body))
        self.assertEqual(reply["capability"], "outlook.reply")
        self.assertEqual(reply["state"], "approved")
        self.assertEqual(set(self.store.load("replies.json")), {"version", "replies"})

        updated_packet = microsoft_packet()
        updated_packet["source"]["version_ref"] = "change-key:demo-v2"
        updated_packet["source"]["retrieved_at"] = "2026-08-29T09:00:00Z"
        updated_packet["source"]["fingerprint"] = fingerprint_for(updated_packet["source"])
        updated_capture = self.stack.ingest_capture(
            updated_packet, "reply.ingest.updated"
        )["body"]["data"]
        self.assertGreater(updated_capture["revision"], reply["capture_revision"])
        frozen = self.store.load("replies.json")["replies"][0]
        self.assertEqual(frozen["capture_revision"], capture["revision"])
        self.assertEqual(frozen["target"], expected_target)
        self.assertEqual(frozen["target_digest"], canonical_digest(expected_target))

        detail = self.stack.task_detail(task["id"])
        self.assertEqual([item["id"] for item in detail["replies"]], [reply["id"]])
        reply_events = [
            event for event in detail["activity"] if event["type"].startswith("reply.")
        ]
        self.assertEqual(len(reply_events), 1)
        event_json = json.dumps(reply_events)
        self.assertNotIn(body, event_json)
        self.assertNotIn('"target"', event_json)

        reply_idempotency = [
            record
            for record in self.store.load("activity.json")["idempotency"]
            if record["key"] == "reply.approve.0001"
        ]
        self.assertEqual(
            reply_idempotency[0]["response_ref"],
            {"kind": "reply", "id": reply["id"]},
        )
        self.assertNotIn(body, json.dumps(reply_idempotency))
        self.assertNotIn('"target"', json.dumps(reply_idempotency))

        other_task = self.stack.add_task("Another task sharing the capture")
        self.stack.link_capture(
            capture["id"], other_task["id"], "reply.link.other-task"
        )
        other_detail = self.stack.task_detail(other_task["id"])
        self.assertEqual(other_detail["replies"], [])
        self.assertFalse(
            any(event["type"].startswith("reply.") for event in other_detail["activity"])
        )

        replay = self.stack.approve_reply(
            {
                "task_id": task["id"],
                "capture_id": capture["id"],
                "body": body,
                "approved": True,
            },
            "reply.approve.0001",
        )
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(len(self.store.load("replies.json")["replies"]), 1)

    def test_teams_capture_derives_teams_capability(self):
        task, capture = self.linked_source("microsoft-teams")
        reply = self.approve(task, capture)
        self.assertEqual(reply["provider"], "microsoft-teams")
        self.assertEqual(reply["capability"], "teams.reply")
        receipt = self.receipt(reply)
        receipt["provider"] = "microsoft-teams"
        receipt["web_url"] = (
            "https://teams.microsoft.com/l/message/"
            "19%3Aopaque_thread%40thread.v2/1740000000000"
        )
        applied = self.stack.apply_reply_receipt(
            reply["id"], receipt, "reply.teams.receipt"
        )
        self.assertEqual(applied["body"]["data"]["state"], "sent")

    def test_receipt_is_terminal_duplicate_safe_and_activity_is_minimal(self):
        task, capture = self.linked_source()
        reply = self.approve(task, capture)
        receipt = self.receipt(reply)

        applied = self.stack.apply_reply_receipt(
            reply["id"], receipt, "reply.receipt.0001"
        )
        self.assertEqual(applied["body"]["data"]["state"], "sent")

        replay = self.stack.apply_reply_receipt(
            reply["id"], receipt, "reply.receipt.0001"
        )
        self.assertTrue(replay["body"]["meta"]["replayed"])
        duplicate = self.stack.apply_reply_receipt(
            reply["id"], receipt, "reply.receipt.0002"
        )
        self.assertTrue(duplicate["body"]["meta"]["duplicate"])

        detail = self.stack.task_detail(task["id"])
        terminal_events = [
            event for event in detail["activity"] if event["type"] == "reply.sent"
        ]
        self.assertEqual(len(terminal_events), 1)
        terminal_json = json.dumps(terminal_events)
        self.assertNotIn("message:opaque-reply-001", terminal_json)
        self.assertNotIn("web_url", terminal_json)
        self.assertNotIn(reply["body"], terminal_json)

        changed = copy.deepcopy(receipt)
        changed["outcome"] = "unknown"
        changed.pop("remote_message_ref")
        changed.pop("web_url")
        changed["error_code"] = "delivery_unconfirmed"
        with self.assertRaises(ReplyReceiptConflictError):
            self.stack.apply_reply_receipt(
                reply["id"], changed, "reply.receipt.0003"
            )

    def test_sent_contract_fixture_matches_the_frozen_digest_algorithm(self):
        task, capture = self.linked_source()
        reply = self.approve(task, capture)
        receipt = fixture("reply-receipt-v1.sent.fixture.json")
        self.assertEqual(receipt["reply_id"], reply["id"])
        self.assertEqual(receipt["body_digest"], reply["body_digest"])
        self.assertEqual(receipt["target_digest"], reply["target_digest"])
        applied = self.stack.apply_reply_receipt(
            reply["id"], receipt, "reply.fixture.sent"
        )
        self.assertEqual(applied["body"]["data"]["state"], "sent")

    def test_receipt_rejects_mismatched_identity_provider_and_digests(self):
        task, capture = self.linked_source()
        reply = self.approve(task, capture)
        base = self.receipt(reply)
        cases = {
            "reply_id": "R-9999",
            "provider": "microsoft-teams",
            "body_digest": "sha256:" + "1" * 64,
            "target_digest": "sha256:" + "2" * 64,
        }
        for index, (field, value) in enumerate(cases.items(), start=1):
            with self.subTest(field=field):
                changed = copy.deepcopy(base)
                changed[field] = value
                with self.assertRaises(ReplyReceiptConflictError) as raised:
                    self.stack.apply_reply_receipt(
                        reply["id"], changed, "reply.mismatch.{:04d}".format(index)
                    )
                self.assertIn(field, raised.exception.details["fields"])
        stored = self.store.load("replies.json")["replies"][0]
        self.assertEqual(stored["state"], "approved")
        self.assertIsNone(stored["receipt"])

    def test_unknown_is_terminal_and_has_no_retry_transition(self):
        task, capture = self.linked_source()
        reply = self.approve(task, capture)
        unknown = self.receipt(reply, "unknown")
        applied = self.stack.apply_reply_receipt(
            reply["id"], unknown, "reply.unknown.0001"
        )
        self.assertEqual(applied["body"]["data"]["state"], "unknown")

        sent = self.receipt(reply, "sent")
        with self.assertRaises(ReplyReceiptConflictError):
            self.stack.apply_reply_receipt(
                reply["id"], sent, "reply.unknown.0002"
            )
        self.assertEqual(
            self.store.load("replies.json")["replies"][0]["state"], "unknown"
        )

    def test_unlinked_unsupported_and_unsafe_approval_inputs_are_rejected(self):
        capture = self.stack.ingest_capture(
            microsoft_packet(), "reply.reject.ingest"
        )["body"]["data"]
        task = self.stack.add_task("Not linked")
        base = {
            "task_id": task["id"],
            "capture_id": capture["id"],
            "body": "Safe approved text",
            "approved": True,
        }
        with self.assertRaises(DomainError):
            self.stack.approve_reply(base, "reply.reject.unlinked")

        self.stack.link_capture(capture["id"], task["id"], "reply.reject.link")
        raw_headers = "\n".join(
            ["From" + ": synthetic sender", "To" + ": synthetic recipient", "source text"]
        )
        cases = (
            {**base, "approved": False},
            {**base, "body": "<" + "p>HTML is not accepted</p>"},
            {**base, "body": "Bearer " + "a" * 26},
            {**base, "body": raw_headers},
            {**base, "body": "x" * 12_001},
            {**base, "target": {"object_ref": "browser-choice"}},
            {**base, "provider": "microsoft-outlook"},
            {**base, "recipients": ["not-accepted"]},
            {**base, "token": "not-accepted"},
            {**base, "raw": "not-accepted"},
        )
        for index, request in enumerate(cases, start=1):
            with self.subTest(index=index), self.assertRaises(DomainError):
                self.stack.approve_reply(
                    request, "reply.reject.{:04d}".format(index)
                )

        manual = self.stack.ingest_capture(
            fixture("capture-packet-v1.manual.fixture.json"),
            "reply.reject.manual.ingest",
        )["body"]["data"]
        self.stack.link_capture(manual["id"], task["id"], "reply.reject.manual.link")
        with self.assertRaises(DomainError):
            self.stack.approve_reply(
                {
                    "task_id": task["id"],
                    "capture_id": manual["id"],
                    "body": "Manual sources cannot authorize a reply.",
                    "approved": True,
                },
                "reply.reject.manual.approve",
            )
        self.assertEqual(self.store.load("replies.json")["replies"], [])

    def test_receipt_schema_rejects_raw_tokens_recipients_and_unsafe_values(self):
        task, capture = self.linked_source()
        reply = self.approve(task, capture)
        base = self.receipt(reply)
        raw_canary = "RAW_" + "CANARY_DO_NOT_STORE"
        cases = (
            {**base, "raw": {"connector": "dump"}},
            {**base, "token": "secret"},
            {**base, "recipients": ["not-accepted"]},
            {**base, "remote_message_ref": "Bearer " + "a" * 26},
            {**base, "remote_message_ref": "Bearer%20" + "a" * 26},
            {
                **base,
                "remote_message_ref": percent_encoding_layers(
                    "access%5Ftoken%3D" + "a" * 20, 11
                ),
            },
            {
                **base,
                "remote_message_ref": "access%5Ftoken%3D" + "a" * 20,
            },
            {
                **base,
                "remote_message_ref": "recipient" + "@" + "example.invalid",
            },
            {
                **base,
                "remote_message_ref": "recipient%40" + "example.invalid",
            },
            {**base, "remote_message_ref": "To:opaque-recipient"},
            {**base, "remote_message_ref": "To%3Aopaque-recipient"},
            {**base, "remote_message_ref": "recipient:alice"},
            {**base, "remote_message_ref": "recipients=alice+bob"},
            {**base, "remote_message_ref": "recipient%3Aalice"},
            {**base, "remote_message_ref": '{"id":"connector-dump"}'},
            {
                **base,
                "remote_message_ref": "id:%7B%22connector%22%3A%22dump%22%7D",
            },
            {**base, "remote_message_ref": "id:%3Cb%3Eraw%3C%2Fb%3E"},
            {
                **base,
                "remote_message_ref": "id:%52%41%57%5FCANARY%5FDO%5FNOT%5FSTORE",
            },
            {**base, "remote_message_ref": "https://outlook.office.com/message/1"},
            {**base, "remote_message_ref": raw_canary},
            {**base, "web_url": "https://example.invalid/message/1"},
            {
                **base,
                "web_url": "https://outlook.office.com/mail/item?access_token=secret-value",
            },
            {
                **base,
                "web_url": (
                    "https://outlook.office.com/mail/read?to="
                    + "recipient"
                    + "@"
                    + "example.invalid"
                ),
            },
            {
                **base,
                "web_url": (
                    "https://outlook.office.com/mail/read?recipients=alice+bob"
                ),
            },
            {
                **base,
                "web_url": (
                    "https://outlook.office.com/mail/read?recipient%3Dalice"
                ),
            },
            {
                **base,
                "web_url": (
                    "https://outlook.office.com/mail/"
                    + "recipient"
                    + "@"
                    + "example.invalid"
                ),
            },
            {
                **base,
                "web_url": (
                    "https://outlook.office.com/mail/read?to="
                    "recipient%40example.invalid"
                ),
            },
            {
                **base,
                "web_url": (
                    "https://teams.microsoft.com/l/message/"
                    "recipient%40example.invalid/1740000000000"
                ),
            },
            {
                **base,
                "web_url": "https://outlook.office.com/mail/%52%41%57_CANARY_DO_NOT_STORE",
            },
            {**base, "error_code": "not symbolic whitespace"},
        )
        for index, receipt in enumerate(cases, start=1):
            with self.subTest(index=index), self.assertRaises(DomainError):
                self.stack.apply_reply_receipt(
                    reply["id"], receipt, "reply.schema.{:04d}".format(index)
                )
        self.assertEqual(
            self.store.load("replies.json")["replies"][0]["state"], "approved"
        )

    def test_remote_message_reference_accepts_graph_and_teams_opaque_ids(self):
        accepted = (
            "message:opaque-reply-001",
            "AAMkAGI2TAAA=AbCdEf0123456789_+/=",
            "19:opaque_thread@thread.v2",
            percent_encoding_layers("message%3Aopaque-five-layers", 5),
        )
        task, capture = self.linked_source()
        for index, remote_ref in enumerate(accepted, start=1):
            with self.subTest(remote_ref=remote_ref):
                reply = self.approve(
                    task,
                    capture,
                    key="reply.remote.approve.{:04d}".format(index),
                )
                receipt = self.receipt(reply)
                receipt["remote_message_ref"] = remote_ref
                applied = self.stack.apply_reply_receipt(
                    reply["id"],
                    receipt,
                    "reply.remote.receipt.{:04d}".format(index),
                )
                self.assertEqual(
                    applied["body"]["data"]["receipt"]["remote_message_ref"],
                    remote_ref,
                )

    def test_reply_and_activity_recover_together_without_duplicate_approval(self):
        task, capture = self.linked_source()
        request = {
            "task_id": task["id"],
            "capture_id": capture["id"],
            "body": "Approved response survives recovery.",
            "approved": True,
        }
        key = "reply.approve.atomic"
        original_atomic_write = self.store._atomic_write_locked

        class SimulatedProcessCrash(BaseException):
            pass

        def crash_after_reply(path: Path, value: object) -> None:
            original_atomic_write(path, value)
            if path == self.store.path("replies.json"):
                raise SimulatedProcessCrash()

        with mock.patch.object(
            self.store, "_atomic_write_locked", side_effect=crash_after_reply
        ):
            with self.assertRaises(SimulatedProcessCrash):
                self.stack.approve_reply(request, key)

        self.assertTrue(self.store.journal_path.exists())
        partial_replies = json.loads(
            self.store.path("replies.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(partial_replies["replies"]), 1)
        partial_activity = json.loads(
            self.store.path("activity.json").read_text(encoding="utf-8")
        )
        self.assertFalse(
            any(event["type"] == "reply.approved" for event in partial_activity["activity"])
        )

        replay = self.stack.approve_reply(request, key)
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertFalse(self.store.journal_path.exists())
        activity = self.store.load("activity.json")
        self.assertEqual(
            sum(event["type"] == "reply.approved" for event in activity["activity"]), 1
        )
        self.assertEqual(sum(record["key"] == key for record in activity["idempotency"]), 1)
        self.assertEqual(len(self.store.load("replies.json")["replies"]), 1)


class CaptureTaskRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def test_task_capture_and_activity_recover_as_one_journal_operation(self):
        capture = self.stack.ingest_capture(
            microsoft_packet(), "capture.task.ingest"
        )["body"]["data"]
        key = "capture.task.atomic"
        original_atomic_write = self.store._atomic_write_locked

        class SimulatedProcessCrash(BaseException):
            pass

        def crash_after_capture(path: Path, value: object) -> None:
            original_atomic_write(path, value)
            if path == self.store.path("captures.json"):
                raise SimulatedProcessCrash()

        with mock.patch.object(
            self.store, "_atomic_write_locked", side_effect=crash_after_capture
        ):
            with self.assertRaises(SimulatedProcessCrash):
                self.stack.create_task_from_capture(
                    capture["id"],
                    {
                        "title": "Task directly from source",
                        "detail": "No extracted action required.",
                        "priority": "P1",
                    },
                    key,
                )

        self.assertTrue(self.store.journal_path.exists())
        replay = self.stack.create_task_from_capture(
            capture["id"],
            {
                "title": "Task directly from source",
                "detail": "No extracted action required.",
                "priority": "P1",
            },
            key,
        )
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertFalse(self.store.journal_path.exists())

        tasks = self.stack.list_tasks(status="all")
        stored_capture = self.store.load("captures.json")["captures"][0]
        activity = self.store.load("activity.json")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(stored_capture["converted_task_ids"], [tasks[0]["id"]])
        self.assertEqual(stored_capture["status"], "converted")
        self.assertEqual(
            sum(event["type"] == "capture.task_created" for event in activity["activity"]),
            1,
        )
        self.assertEqual(
            sum(record["key"] == key for record in activity["idempotency"]), 1
        )
        self.assertEqual(len(self.stack.list_tasks(status="all")), 1)


if __name__ == "__main__":
    unittest.main()
