from __future__ import annotations

import json
import http.client
import tempfile
import threading
import unittest
from pathlib import Path

from workstack.service import WorkStack
from workstack.server import create_server
from workstack.store import DEFAULTS, Store, StoreExternalChangeError


class StoreSyncGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_manifest_records_identity_generation_and_all_authoritative_hashes(self) -> None:
        status = self.store.sync_status()
        self.assertEqual(status["state"], "in-sync")
        self.assertEqual(status["workspace_id"], self.stack.workspace_projection()["workspace"]["id"])
        self.assertGreaterEqual(status["generation"], 1)
        self.assertRegex(status["manifest_digest"], r"^sha256:[0-9a-f]{64}$")

        manifest = json.loads(self.store.store_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["files"]), set(DEFAULTS))
        self.assertTrue(
            all(
                isinstance(digest, str) and digest.startswith("sha256:")
                for digest in manifest["files"].values()
            )
        )

    def test_valid_direct_file_change_is_detected_and_freezes_writes(self) -> None:
        notes_path = self.root / "notes.json"
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
        notes["notes"].append({"id": "N-external", "title": "Agent", "body": "Changed"})
        notes_path.write_text(json.dumps(notes), encoding="utf-8")

        status = self.store.sync_status()
        self.assertEqual(status["state"], "external-change-detected")
        self.assertEqual(status["changed_files"], ["notes.json"])
        with self.assertRaises(StoreExternalChangeError):
            self.stack.add_task("Must not overwrite external bytes")
        self.assertEqual(
            json.loads(notes_path.read_text(encoding="utf-8"))["notes"][0]["id"],
            "N-external",
        )

    def test_partial_or_invalid_direct_change_is_reported_invalid(self) -> None:
        (self.root / "activity.json").unlink()
        status = self.store.sync_status()
        self.assertEqual(status["state"], "invalid")
        self.assertEqual(status["changed_files"], ["activity.json"])
        self.assertEqual(status["reason"], "authoritative store candidate failed validation")
        with self.assertRaises(StoreExternalChangeError):
            self.stack.add_task("Must remain frozen")

    def test_server_owned_commit_advances_generation_without_false_positive(self) -> None:
        before = self.store.sync_status()
        self.stack.add_task("Owned commit")
        after = self.store.sync_status()
        self.assertEqual(after["state"], "in-sync")
        self.assertEqual(after["changed_files"], [])
        self.assertGreater(after["generation"], before["generation"])
        events = self.store.sync_events()
        self.assertEqual(events["state"], "in-sync")
        self.assertTrue(any(event["type"] == "store.committed" for event in events["events"]))

    def test_direct_task_edit_without_revision_advance_is_invalid(self) -> None:
        task = self.stack.add_task("Original")
        backlog_path = self.root / "backlog.json"
        backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
        backlog["tasks"][0]["title"] = "Silently overwritten"
        self.assertEqual(backlog["tasks"][0]["revision"], task["revision"])
        backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

        status = self.store.sync_status()
        self.assertEqual(status["state"], "invalid")
        self.assertEqual(status["changed_files"], ["backlog.json"])

    def test_valid_external_candidate_requires_coordinate_matched_adoption(self) -> None:
        notes_path = self.root / "notes.json"
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
        notes["notes"].append({"id": "N-external", "title": "Agent", "body": "Changed"})
        notes_path.write_text(json.dumps(notes), encoding="utf-8")
        pending = self.store.sync_status()

        with self.assertRaises(StoreExternalChangeError):
            self.store.adopt_external_change(
                pending["generation"], "sha256:" + "0" * 64
            )
        adopted = self.store.adopt_external_change(
            pending["generation"], pending["manifest_digest"]
        )
        self.assertEqual(adopted["state"], "in-sync")
        self.assertGreater(adopted["generation"], pending["generation"])
        self.stack.add_task("Writes resume only after explicit adoption")

    def test_adoption_refuses_candidate_that_changed_after_review(self) -> None:
        notes_path = self.root / "notes.json"
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
        notes["notes"].append({"id": "N-one", "title": "First", "body": "Changed"})
        notes_path.write_text(json.dumps(notes), encoding="utf-8")
        reviewed = self.store.sync_status()
        notes["notes"].append({"id": "N-two", "title": "Second", "body": "Changed again"})
        notes_path.write_text(json.dumps(notes), encoding="utf-8")

        with self.assertRaises(StoreExternalChangeError):
            self.store.adopt_external_change(
                reviewed["generation"], reviewed["manifest_digest"]
            )
        self.assertEqual(self.store.sync_status()["state"], "external-change-detected")


class StoreSyncApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.actual_port, timeout=20
        )
        raw = (
            json.dumps(body, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        connection.request(method, path, body=raw, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        headers = {key.casefold(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, body, headers

    def test_status_and_event_surfaces_expose_content_free_invalidation(self) -> None:
        status_code, body, _ = self.request("/api/v1/sync/status")
        self.assertEqual(status_code, 200)
        status = json.loads(body)["data"]
        self.assertEqual(
            set(status),
            {
                "state",
                "workspace_id",
                "generation",
                "manifest_digest",
                "changed_files",
                "reason",
            },
        )
        self.assertEqual(status["state"], "in-sync")

        event_code, event_body, event_headers = self.request("/api/v1/events")
        self.assertEqual(event_code, 200)
        self.assertEqual(
            event_headers["content-type"], "text/event-stream; charset=utf-8"
        )
        self.assertIn(b"event: sync", event_body)
        self.assertIn(b'"state":"in-sync"', event_body)
        self.assertNotIn(b"Owned commit", event_body)

    def test_browser_can_explicitly_adopt_an_unchanged_valid_candidate(self) -> None:
        notes_path = self.root / "notes.json"
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
        notes["notes"].append({"id": "N-external", "title": "Agent", "body": "Changed"})
        notes_path.write_text(json.dumps(notes), encoding="utf-8")
        _, pending_body, _ = self.request("/api/v1/sync/status")
        pending = json.loads(pending_body)["data"]
        _, session_body, _ = self.request("/api/v1/session")
        csrf = json.loads(session_body)["data"]["csrf_token"]
        status_code, adopted_body, _ = self.request(
            "/api/v1/sync/adopt",
            method="POST",
            body={
                "expected_generation": pending["generation"],
                "expected_manifest_digest": pending["manifest_digest"],
            },
            headers={
                "Origin": "http://127.0.0.1:{}".format(self.server.actual_port),
                "X-WorkStack-CSRF": csrf,
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(json.loads(adopted_body)["data"]["state"], "in-sync")


if __name__ == "__main__":
    unittest.main()
