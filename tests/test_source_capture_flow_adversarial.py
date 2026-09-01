from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from workstack.capture import CaptureValidationError
from workstack.service import SourceRevisionConflictError, WorkStack
from workstack.store import Store


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


def fixture(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


class SourceCaptureFlowAdversarialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.stack = WorkStack(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_explicit_retry_after_lost_task_response_does_not_duplicate_task(self) -> None:
        packet = fixture("capture-packet-v1.manual.fixture.json")
        first_capture = self.stack.ingest_capture(packet, "ui.ingest.first")["body"]["data"]
        task_fields = {
            "title": "Review the captured source",
            "detail": "The first response is lost after the server commits.",
            "priority": "P2",
        }

        operation_key = "ui.task.same-logical-operation"
        first = self.stack.create_task_from_capture(
            first_capture["id"], task_fields, operation_key
        )["body"]["data"]

        # Ingest finds the same capture, while the Source Capture dialog preserves the
        # task operation key across the explicit retry.
        retried_capture = self.stack.ingest_capture(
            packet, "ui.ingest.explicit-retry"
        )["body"]["data"]
        retry = self.stack.create_task_from_capture(
            retried_capture["id"], task_fields, operation_key
        )["body"]["data"]

        self.assertEqual(retry["id"], first["id"])
        self.assertEqual(len(self.stack.list_tasks(status="all")), 1)

    def test_attach_explicit_retry_with_a_new_key_is_semantically_duplicate_safe(self) -> None:
        capture = self.stack.ingest_capture(
            fixture("capture-packet-v1.manual.fixture.json"), "ui.attach.ingest"
        )["body"]["data"]
        task = self.stack.add_task("Existing task")

        first = self.stack.link_capture(capture["id"], task["id"], "ui.attach.first")
        retry = self.stack.link_capture(capture["id"], task["id"], "ui.attach.retry")

        self.assertEqual(first["body"]["data"]["linked_task_ids"], [task["id"]])
        self.assertEqual(retry["body"]["data"]["linked_task_ids"], [task["id"]])
        self.assertTrue(retry["body"]["meta"]["duplicate"])

    def test_source_dialog_task_then_action_button_does_not_create_second_task(self) -> None:
        packet = fixture("capture-packet-v1.manual.fixture.json")
        capture = self.stack.ingest_capture(packet, "ui.dialog.ingest")["body"]["data"]
        action = capture["normalized"]["action_items"][0]
        created = self.stack.create_task_from_capture(
            capture["id"],
            {
                "title": action["title"],
                "detail": action["detail"],
                "priority": action["priority"],
                "due": action["due"],
            },
            "ui.dialog.task",
        )["body"]["data"]

        action_result = self.stack.convert_capture_action(
            capture["id"], action["id"], [], "ui.card.action"
        )

        self.assertEqual(action_result["body"]["data"]["id"], created["id"])
        self.assertTrue(action_result["body"].get("meta", {}).get("duplicate"))
        self.assertEqual(len(self.stack.list_tasks(status="all")), 1)

    def test_changed_reviewed_content_is_rejected_instead_of_silently_discarded(self) -> None:
        packet = fixture("capture-packet-v1.manual.fixture.json")
        self.stack.ingest_capture(packet, "ui.content.first")
        changed = copy.deepcopy(packet)
        changed["normalized"]["summary"] = "The user reviewed and corrected this source summary."
        changed["normalized"]["context"] = "The corrected reviewed source must not be silently lost."

        with self.assertRaises(SourceRevisionConflictError):
            self.stack.ingest_capture(changed, "ui.content.retry")

        projected = self.stack.list_captures("all")[0]
        self.assertEqual(projected["normalized"]["summary"], packet["normalized"]["summary"])
        self.assertEqual(projected["normalized"]["context"], packet["normalized"]["context"])
        self.assertFalse(any(
            record.get("key") == "ui.content.retry"
            for record in self.store.load("activity.json")["idempotency"]
        ))

    def test_credential_bearing_microsoft_url_is_rejected_without_persistence(self) -> None:
        packet = fixture("capture-packet-v1.fixture.json")
        packet["source"]["web_url"] = (
            "https://outlook.office.com/mail/deeplink/read/demo"
            f"#{'_'.join(('access', 'token'))}={''.join(('abcdefgh', 'ijklmnop'))}"
        )

        with self.assertRaises(CaptureValidationError):
            self.stack.ingest_capture(packet, "ui.unsafe-url")

        self.assertEqual(self.store.load("captures.json")["captures"], [])
        self.assertEqual(self.store.load("activity.json")["idempotency"], [])


if __name__ == "__main__":
    unittest.main()
