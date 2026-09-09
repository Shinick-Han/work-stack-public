from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from workstack.capture_unlink_receipt import (
    EVENT_TYPE as UNLINK_RECEIPT_EVENT,
    capture_row_digest,
    serialize_receipt,
    validate_receipt,
)
from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import MAX_REVISION, Store


CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


class CaptureUnlinkHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.stack = WorkStack(self.store)
        self.task = self.stack.add_task("Unlink HTTP task")
        self.other = self.stack.add_task("Unlink HTTP other")
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.port = self.server.actual_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        packet = json.loads(
            (CONTRACTS / "capture-packet-v1.fixture.json").read_text(encoding="utf-8")
        )
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.unlink.ingest"
        status, created, _ = self.request("POST", "/api/v1/captures", packet, headers)
        self.assertEqual(status, 201)
        self.capture = created["data"]
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.unlink.link"
        status, linked, _ = self.request(
            "POST",
            "/api/v1/captures/{}/link".format(self.capture["id"]),
            {"task_id": self.task["id"]},
            headers,
        )
        self.assertEqual(status, 200)
        self.linked = linked["data"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: dict | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict, dict[str, str]]:
        outgoing: bytes | None
        actual_headers = dict(headers or {})
        if isinstance(body, dict):
            outgoing = json.dumps(body, separators=(",", ":")).encode("utf-8")
            actual_headers.setdefault("Content-Type", "application/json")
        else:
            outgoing = body
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=outgoing, headers=actual_headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.casefold(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, json.loads(raw.decode("utf-8")), response_headers

    def browser_headers(self) -> dict[str, str]:
        status, session, _ = self.request("GET", "/api/v1/session")
        self.assertEqual(status, 200)
        return {
            "Origin": "http://127.0.0.1:{}".format(self.port),
            "X-WorkStack-CSRF": session["data"]["csrf_token"],
        }

    def unlink_path(self) -> str:
        return "/api/v1/captures/{}/unlink".format(self.capture["id"])

    def test_browser_unlink_requires_csrf_and_idempotency_and_refuses_capture_token(self) -> None:
        body = {"task_id": self.task["id"], "revision": self.linked["revision"]}
        status, denied, _ = self.request("POST", self.unlink_path(), body, {
            "Idempotency-Key": "http.unlink.no.origin",
            "Content-Type": "application/json",
        })
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"]["code"], "origin_required")

        token_headers = {
            "Authorization": "Bearer " + self.server.capture_token,
            "Idempotency-Key": "http.unlink.token",
            "Content-Type": "application/json",
        }
        status, token_denied, _ = self.request("POST", self.unlink_path(), body, token_headers)
        self.assertEqual(status, 403)
        self.assertEqual(token_denied["error"]["code"], "origin_required")

        status, missing, _ = self.request(
            "POST", self.unlink_path(), body, self.browser_headers()
        )
        self.assertEqual(status, 400)
        self.assertEqual(missing["error"]["code"], "idempotency_key_required")
        self.assertEqual(
            self.store.load("captures.json")["captures"][0]["linked_task_ids"],
            [self.task["id"]],
        )

    def test_cas_conflict_and_bool_revision_write_nothing(self) -> None:
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.unlink.bool"
        status, invalid, _ = self.request(
            "POST",
            self.unlink_path(),
            {"task_id": self.task["id"], "revision": True},
            headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_body")

        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.unlink.stale"
        before = self.store.load("captures.json")
        status, conflict, _ = self.request(
            "POST",
            self.unlink_path(),
            {"task_id": self.task["id"], "revision": 0},
            headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "revision_conflict")
        self.assertEqual(self.store.load("captures.json"), before)

    def test_successful_unlink_replay_conflict_and_task_patch_cannot_name_the_link(self) -> None:
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.unlink.ok"
        body = {"task_id": self.task["id"], "revision": self.linked["revision"]}
        status, removed, _ = self.request("POST", self.unlink_path(), body, headers)
        self.assertEqual(status, 200)
        self.assertFalse(removed["meta"]["duplicate"])
        self.assertEqual(removed["data"]["linked_task_ids"], [])
        self.assertEqual(removed["data"]["status"], "inbox")

        status, replayed, _ = self.request("POST", self.unlink_path(), body, headers)
        self.assertEqual(status, 200)
        self.assertTrue(replayed["meta"]["replayed"])
        self.assertEqual(replayed["data"]["revision"], removed["data"]["revision"])

        conflict_headers = self.browser_headers()
        conflict_headers["Idempotency-Key"] = "http.unlink.ok"
        status, changed, _ = self.request(
            "POST",
            self.unlink_path(),
            {"task_id": self.other["id"], "revision": self.linked["revision"]},
            conflict_headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(changed["error"]["code"], "idempotency_conflict")

        status, listed, _ = self.request("GET", "/api/v1/captures?status=all")
        self.assertEqual(status, 200)
        self.assertEqual(listed["data"][0]["id"], self.capture["id"])
        self.assertEqual(listed["data"][0]["source"], self.linked["source"])

        patch_headers = self.browser_headers()
        status, patch, _ = self.request(
            "PATCH",
            "/api/v1/tasks/{}".format(self.task["id"]),
            {"linked_task_ids": [], "revision": self.task["revision"]},
            patch_headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(patch["error"]["code"], "invalid_request")
        self.assertIn("linked_task_ids", patch["error"].get("details", {}).get("fields", []))

        status, detail, _ = self.request("GET", "/api/v1/tasks/{}".format(self.task["id"]))
        self.assertEqual(status, 200)
        self.assertTrue(any(item["id"] == self.task["id"] for item in [detail["data"]["task"]]))
        self.assertFalse(
            any(item.get("id") == self.capture["id"] for item in detail["data"]["context"])
        )
        events = [
            event
            for event in self.store.load("activity.json")["activity"]
            if event.get("type") == "capture.unlinked"
        ]
        self.assertEqual(len(events), 1)
        self.assertLessEqual(
            {"id", "type", "created_at", "capture_id", "task_id"},
            set(events[0]),
        )
        self.assertEqual(events[0]["capture_id"], self.capture["id"])
        self.assertEqual(events[0]["task_id"], self.task["id"])

    def test_max_revision_unlink_returns_revision_exhausted_and_writes_nothing(self) -> None:
        tampered = self.store.load("captures.json")
        tampered["captures"][0]["revision"] = MAX_REVISION
        self.store.save("captures.json", tampered)
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.unlink.max"
        status, body, _ = self.request(
            "POST",
            self.unlink_path(),
            {"task_id": self.task["id"], "revision": MAX_REVISION},
            headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "revision_exhausted")
        stored = self.store.load("captures.json")["captures"][0]
        self.assertEqual(stored["revision"], MAX_REVISION)
        self.assertEqual(stored["linked_task_ids"], [self.task["id"]])
        self.assertFalse(
            any(
                event.get("type") == "capture.unlinked"
                for event in self.store.load("activity.json")["activity"]
            )
        )


class CaptureUnlinkUndoHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.stack = WorkStack(self.store)
        self.task = self.stack.add_task("Undo HTTP task")
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.port = self.server.actual_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        packet = json.loads(
            (CONTRACTS / "capture-packet-v1.fixture.json").read_text(encoding="utf-8")
        )
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.undo.ingest"
        status, created, _ = self.request("POST", "/api/v1/captures", packet, headers)
        self.assertEqual(status, 201)
        self.capture = created["data"]
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.undo.link"
        status, linked, _ = self.request(
            "POST",
            "/api/v1/captures/{}/link".format(self.capture["id"]),
            {"task_id": self.task["id"]},
            headers,
        )
        self.assertEqual(status, 200)
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.undo.unlink"
        status, removed, _ = self.request(
            "POST",
            "/api/v1/captures/{}/unlink".format(self.capture["id"]),
            {"task_id": self.task["id"], "revision": linked["data"]["revision"]},
            headers,
        )
        self.assertEqual(status, 200)
        self.removed = removed

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: dict | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict, dict[str, str]]:
        outgoing: bytes | None
        actual_headers = dict(headers or {})
        if isinstance(body, dict):
            outgoing = json.dumps(body, separators=(",", ":")).encode("utf-8")
            actual_headers.setdefault("Content-Type", "application/json")
        else:
            outgoing = body
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=outgoing, headers=actual_headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.casefold(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, json.loads(raw.decode("utf-8")), response_headers

    def browser_headers(self) -> dict[str, str]:
        status, session, _ = self.request("GET", "/api/v1/session")
        self.assertEqual(status, 200)
        return {
            "Origin": "http://127.0.0.1:{}".format(self.port),
            "X-WorkStack-CSRF": session["data"]["csrf_token"],
        }

    def undo_path(self) -> str:
        return "/api/v1/captures/{}/undo-unlink".format(self.capture["id"])

    def test_browser_undo_requires_csrf_and_idempotency_and_refuses_extra_fields(
        self,
    ) -> None:
        body = {
            "receipt_id": self.removed["meta"]["undo_receipt_id"],
            "revision": self.removed["data"]["revision"],
        }
        status, denied, _ = self.request("POST", self.undo_path(), body, {
            "Idempotency-Key": "http.undo.no.origin",
            "Content-Type": "application/json",
        })
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"]["code"], "origin_required")

        status, missing, _ = self.request(
            "POST", self.undo_path(), body, self.browser_headers()
        )
        self.assertEqual(status, 400)
        self.assertEqual(missing["error"]["code"], "idempotency_key_required")

        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.undo.extra"
        status, extra, _ = self.request(
            "POST",
            self.undo_path(),
            {**body, "status_before": "linked"},
            headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(extra["error"]["code"], "invalid_body")
        self.assertEqual(
            self.store.load("captures.json")["captures"][0]["linked_task_ids"],
            [],
        )

    def test_undo_conflict_stale_revision_success_and_replay(self) -> None:
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.undo.stale"
        status, stale, _ = self.request(
            "POST",
            self.undo_path(),
            {
                "receipt_id": self.removed["meta"]["undo_receipt_id"],
                "revision": 0,
            },
            headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(stale["error"]["code"], "revision_conflict")

        tampered = self.store.load("captures.json")
        tampered["captures"][0]["linked_task_ids"] = [self.task["id"]]
        self.store.save("captures.json", tampered)
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.undo.conflict"
        status, conflict, _ = self.request(
            "POST",
            self.undo_path(),
            {
                "receipt_id": self.removed["meta"]["undo_receipt_id"],
                "revision": self.removed["data"]["revision"],
            },
            headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "capture_unlink_undo_conflict")
        self.assertNotIn("source", conflict["error"].get("details", {}))
        self.assertNotIn("body", conflict["error"].get("details", {}))

        tampered["captures"][0]["linked_task_ids"] = []
        self.store.save("captures.json", tampered)
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.undo.ok"
        body = {
            "receipt_id": self.removed["meta"]["undo_receipt_id"],
            "revision": self.removed["data"]["revision"],
        }
        status, restored, _ = self.request("POST", self.undo_path(), body, headers)
        self.assertEqual(status, 200)
        self.assertFalse(restored["meta"]["duplicate"])
        self.assertEqual(restored["data"]["status"], "linked")
        self.assertEqual(restored["data"]["linked_task_ids"], [self.task["id"]])

        status, replayed, _ = self.request("POST", self.undo_path(), body, headers)
        self.assertEqual(status, 200)
        self.assertTrue(replayed["meta"]["replayed"])
        self.assertEqual(replayed["data"]["revision"], restored["data"]["revision"])

    def test_max_revision_undo_returns_revision_exhausted_and_writes_nothing(self) -> None:
        captures = self.store.load("captures.json")
        capture = captures["captures"][0]
        capture["revision"] = MAX_REVISION
        activity = self.store.load("activity.json")
        receipt_id = self.removed["meta"]["undo_receipt_id"]
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
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "http.undo.max"
        status, body, _ = self.request(
            "POST",
            self.undo_path(),
            {"receipt_id": receipt_id, "revision": MAX_REVISION},
            headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "revision_exhausted")
        stored = self.store.load("captures.json")["captures"][0]
        self.assertEqual(stored["revision"], MAX_REVISION)
        self.assertEqual(stored["linked_task_ids"], [])
        self.assertFalse(
            any(
                event.get("type") == "capture.unlink_undone"
                for event in self.store.load("activity.json")["activity"]
            )
        )
