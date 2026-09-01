from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack.capture import canonical_digest
from workstack.service import (
    DomainError,
    IdempotencyConflictError,
    NotFoundError,
    ReplyReceiptConflictError,
    WorkStack,
)
from workstack.store import Store

from tests.test_storage_capture_reply_contract import (
    PACKET_FIXTURE,
    V4CaptureReplyBackend,
)


class ServiceHarness:
    def __init__(self, base: Path, version: int) -> None:
        self.version = version
        if version == 3:
            self.stack = WorkStack(Store(base / "v3"))
            self.task = self.stack.add_task("Service integration Task")
            self.repository = None
            return
        backend = V4CaptureReplyBackend(base / "v4-authority")
        self.repository = backend.repository
        self.task = backend.task
        self.stack = WorkStack(
            Store(base / "v4-service-shell"),
            capture_reply_commands=self.repository,
        )

    def documents(self):
        if self.version == 3:
            return {
                "captures.json": self.stack.store.load("captures.json"),
                "replies.json": self.stack.store.load("replies.json"),
                "activity.json": self.stack.store.load("activity.json"),
            }
        return self.repository.state_documents()


class ServiceCaptureReplyBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.runtime_environment = mock.patch.dict(
            os.environ, {"WORK_STACK_RUNTIME": str(self.base / "runtime")}
        )
        self.runtime_environment.start()
        self.packet = json.loads(PACKET_FIXTURE.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.runtime_environment.stop()
        self.temporary.cleanup()

    def harnesses(self):
        return (
            ServiceHarness(self.base / "one", 3),
            ServiceHarness(self.base / "two", 4),
        )

    @staticmethod
    def receipt(reply: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "reply_id": reply["id"],
            "provider": reply["provider"],
            "outcome": "sent",
            "occurred_at": "2026-08-29T00:15:00Z",
            "body_digest": reply["body_digest"],
            "target_digest": reply["target_digest"],
            "remote_message_ref": "message:service-integration-001",
            "web_url": (
                "https://outlook.office.com/mail/deeplink/read/"
                "service-integration-001"
            ),
        }

    def lifecycle(self, harness: ServiceHarness):
        packet_digest = canonical_digest(self.packet)
        ingested = harness.stack.ingest_capture(
            copy.deepcopy(self.packet),
            f"service.ingest.v{harness.version}.0001",
            packet_digest,
            path="/api/v1/captures",
        )
        capture = ingested["body"]["data"]
        linked = harness.stack.link_capture(
            capture["id"],
            harness.task["id"],
            f"service.link.v{harness.version}.0001",
            canonical_digest({"task_id": harness.task["id"]}),
            path=f"/api/v1/captures/{capture['id']}/link",
        )
        request = {
            "task_id": harness.task["id"],
            "capture_id": capture["id"],
            "body": "Approved through the public WorkStack method",
            "approved": True,
        }
        approved = harness.stack.approve_reply(
            request,
            f"service.approve.v{harness.version}.0001",
            canonical_digest(request),
            path="/api/v1/replies",
        )
        reply = approved["body"]["data"]
        receipt = self.receipt(reply)
        terminal = harness.stack.apply_reply_receipt(
            reply["id"],
            receipt,
            f"service.receipt.v{harness.version}.0001",
            canonical_digest(receipt),
            path=f"/api/v1/replies/{reply['id']}/receipt",
        )
        return ingested, linked, approved, terminal

    def test_public_workstack_methods_share_v3_v4_response_and_event_contract(self) -> None:
        for harness in self.harnesses():
            with self.subTest(version=harness.version):
                ingest, link, approval, terminal = self.lifecycle(harness)
                self.assertEqual((ingest["status"], link["status"]), (201, 200))
                self.assertEqual((approval["status"], terminal["status"]), (201, 200))
                self.assertEqual(ingest["body"]["data"]["status"], "inbox")
                self.assertEqual(link["body"]["data"]["status"], "linked")
                self.assertEqual(approval["body"]["data"]["state"], "approved")
                self.assertEqual(terminal["body"]["data"]["state"], "sent")
                documents = harness.documents()
                events = [
                    event["type"]
                    for event in documents["activity.json"]["activity"]
                    if event["type"].startswith(("capture.", "reply."))
                ]
                self.assertEqual(
                    events,
                    [
                        "capture.ingested",
                        "capture.linked",
                        "reply.approved",
                        "reply.sent",
                    ],
                )
                records = documents["activity.json"]["idempotency"]
                self.assertTrue(all(record["method"] == "POST" for record in records))

    def test_repository_errors_map_to_existing_public_service_errors(self) -> None:
        for harness in self.harnesses():
            with self.subTest(version=harness.version):
                key = f"service.conflict.v{harness.version}.0001"
                harness.stack.ingest_capture(copy.deepcopy(self.packet), key)
                changed = copy.deepcopy(self.packet)
                changed["normalized"]["summary"] += " conflict"
                with self.assertRaises(IdempotencyConflictError):
                    harness.stack.ingest_capture(changed, key)
                with self.assertRaises(NotFoundError):
                    harness.stack.link_capture(
                        "C-0001",
                        "T-9999",
                        f"service.notfound.v{harness.version}.0001",
                    )
                with self.assertRaises(DomainError) as invalid:
                    harness.stack.approve_reply(
                        {"approved": False},
                        f"service.invalid.v{harness.version}.0001",
                    )
                self.assertEqual(invalid.exception.code, "invalid_request")

    def test_receipt_conflict_maps_to_public_conflict_type(self) -> None:
        for harness in self.harnesses():
            with self.subTest(version=harness.version):
                _ingest, _link, approved, _terminal = self.lifecycle(harness)
                reply = approved["body"]["data"]
                conflicting = self.receipt(reply)
                conflicting["outcome"] = "failed"
                with self.assertRaises(ReplyReceiptConflictError):
                    harness.stack.apply_reply_receipt(
                        reply["id"],
                        conflicting,
                        f"service.receipt-conflict.v{harness.version}.0001",
                    )

    def test_default_constructor_does_not_enable_any_v4_command_backend(self) -> None:
        stack = WorkStack(Store(self.base / "default-v3"))
        self.assertIsNone(stack.capture_reply_commands)
        result = stack.ingest_capture(
            copy.deepcopy(self.packet), "service.default-v3.0001"
        )
        self.assertEqual(result["status"], 201)
        self.assertEqual(len(stack.store.load("captures.json")["captures"]), 1)


if __name__ == "__main__":
    unittest.main()
