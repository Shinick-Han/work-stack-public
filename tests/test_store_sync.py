from __future__ import annotations

import json
import http.client
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from workstack.service import WorkStack
from workstack.server import create_server
from workstack.store import (
    DEFAULTS,
    Store,
    StoreAdoptionConflictError,
    StoreExternalChangeError,
)


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

    def test_adoption_receipt_replays_exact_intent_after_restart(self) -> None:
        notes_path = self.root / "notes.json"
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
        notes["notes"].append({"id": "N-replay", "title": "Agent", "body": "Changed"})
        notes_path.write_text(json.dumps(notes), encoding="utf-8")
        pending = self.store.sync_status()

        first = self.store.adopt_external_change(
            pending["generation"], pending["manifest_digest"], "sync.restart.0001"
        )
        restarted = Store(self.root)
        WorkStack(restarted)
        replay = restarted.adopt_external_change(
            pending["generation"], pending["manifest_digest"], "sync.restart.0001"
        )

        self.assertEqual(replay, first)
        self.assertEqual(replay["state"], "in-sync")

    def test_adoption_key_reuse_for_another_candidate_conflicts(self) -> None:
        notes_path = self.root / "notes.json"
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
        notes["notes"].append({"id": "N-one", "title": "Agent", "body": "First"})
        notes_path.write_text(json.dumps(notes), encoding="utf-8")
        first = self.store.sync_status()
        self.store.adopt_external_change(
            first["generation"], first["manifest_digest"], "sync.reused.0001"
        )

        notes["notes"].append({"id": "N-two", "title": "Agent", "body": "Second"})
        notes_path.write_text(json.dumps(notes), encoding="utf-8")
        second = self.store.sync_status()
        with self.assertRaises(StoreAdoptionConflictError):
            self.store.adopt_external_change(
                second["generation"], second["manifest_digest"], "sync.reused.0001"
            )
        self.assertEqual(self.store.sync_status()["state"], "external-change-detected")

    def test_workspace_identity_rebind_is_explicit_content_preserving_and_audited(self) -> None:
        before_hashes = {
            name: self.store.path(name).read_bytes()
            for name in DEFAULTS
        }
        original_manifest = json.loads(
            self.store.store_manifest_path.read_text(encoding="utf-8")
        )
        workspace_path = self.root / "workspace.json"
        workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
        replacement_workspace_id = str(uuid.uuid4())
        workspace["id"] = replacement_workspace_id
        workspace_path.write_text(json.dumps(workspace), encoding="utf-8")

        pending = self.store.sync_status()
        self.assertEqual(pending["state"], "invalid")
        self.assertTrue(pending["rebind_available"])
        self.assertEqual(pending["candidate_workspace_id"], replacement_workspace_id)
        preview = self.store.workspace_rebind_preview()

        rebound = self.store.rebind_workspace_identity(
            confirmed=True,
            expected_manifest_workspace_id=preview["manifest_workspace_id"],
            expected_candidate_workspace_id=preview["candidate_workspace_id"],
            expected_manifest_digest=preview["manifest_digest"],
            expected_candidate_digest=preview["candidate_digest"],
            idempotency_key="sync.rebind.0001",
        )

        self.assertEqual(rebound["state"], "in-sync")
        self.assertEqual(rebound["workspace_id"], replacement_workspace_id)
        for name, previous in before_hashes.items():
            if name == "workspace.json":
                continue
            self.assertEqual(self.store.path(name).read_bytes(), previous)
        self.assertEqual(
            json.loads(workspace_path.read_text(encoding="utf-8"))["id"],
            replacement_workspace_id,
        )
        receipt = json.loads(self.store.sync_rebind_receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["previous_workspace_id"], original_manifest["workspace_id"])
        self.assertEqual(receipt["candidate_workspace_id"], replacement_workspace_id)
        self.assertEqual({record["name"] for record in receipt["authoritative_files"]}, set(DEFAULTS))
        quarantine = self.store.runtime_root / receipt["quarantined_manifest_file"]
        self.assertTrue(quarantine.is_file())
        self.assertEqual(
            json.loads(quarantine.read_text(encoding="utf-8"))["workspace_id"],
            original_manifest["workspace_id"],
        )
        self.stack.add_task("Writes resume after verified rebind")

    def test_workspace_identity_rebind_rejects_wrong_confirmation_and_replays(self) -> None:
        workspace_path = self.root / "workspace.json"
        workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
        replacement_workspace_id = str(uuid.uuid4())
        workspace["id"] = replacement_workspace_id
        workspace_path.write_text(json.dumps(workspace), encoding="utf-8")
        preview = self.store.workspace_rebind_preview()

        with self.assertRaises(StoreExternalChangeError):
            self.store.rebind_workspace_identity(
                confirmed=True,
                expected_manifest_workspace_id=preview["manifest_workspace_id"],
                expected_candidate_workspace_id=str(uuid.uuid4()),
                expected_manifest_digest=preview["manifest_digest"],
                expected_candidate_digest=preview["candidate_digest"],
                idempotency_key="sync.rebind.wrong",
            )
        first = self.store.rebind_workspace_identity(
            confirmed=True,
            expected_manifest_workspace_id=preview["manifest_workspace_id"],
            expected_candidate_workspace_id=replacement_workspace_id,
            expected_manifest_digest=preview["manifest_digest"],
            expected_candidate_digest=preview["candidate_digest"],
            idempotency_key="sync.rebind.replay",
        )
        restarted = Store(self.root)
        WorkStack(restarted)
        replay = restarted.rebind_workspace_identity(
            confirmed=True,
            expected_manifest_workspace_id=preview["manifest_workspace_id"],
            expected_candidate_workspace_id=replacement_workspace_id,
            expected_manifest_digest=preview["manifest_digest"],
            expected_candidate_digest=preview["candidate_digest"],
            idempotency_key="sync.rebind.replay",
        )
        self.assertEqual(replay, first)


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
                "candidate_workspace_id",
                "generation",
                "manifest_digest",
                "changed_files",
                "reason",
                "rebind_available",
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
                "Idempotency-Key": "sync.adopt.0001",
            },
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(json.loads(adopted_body)["data"]["state"], "in-sync")

    def test_browser_can_rebind_only_an_exact_workspace_identity_replacement(self) -> None:
        workspace_path = self.root / "workspace.json"
        workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
        replacement_workspace_id = str(uuid.uuid4())
        workspace["id"] = replacement_workspace_id
        workspace_path.write_text(json.dumps(workspace), encoding="utf-8")
        _, preview_body, _ = self.request("/api/v1/sync/rebind-preview")
        preview = json.loads(preview_body)["data"]
        _, session_body, _ = self.request("/api/v1/session")
        csrf = json.loads(session_body)["data"]["csrf_token"]

        status_code, rebound_body, _ = self.request(
            "/api/v1/sync/rebind-workspace",
            method="POST",
            body={
                "confirmed": True,
                "expected_manifest_workspace_id": preview["manifest_workspace_id"],
                "expected_candidate_workspace_id": preview["candidate_workspace_id"],
                "expected_manifest_digest": preview["manifest_digest"],
                "expected_candidate_digest": preview["candidate_digest"],
            },
            headers={
                "Origin": "http://127.0.0.1:{}".format(self.server.actual_port),
                "X-WorkStack-CSRF": csrf,
                "Content-Type": "application/json",
                "Idempotency-Key": "sync.rebind.api.0001",
            },
        )
        self.assertEqual(status_code, 200)
        rebound = json.loads(rebound_body)["data"]
        self.assertEqual(rebound["state"], "in-sync")
        self.assertEqual(rebound["workspace_id"], replacement_workspace_id)


if __name__ == "__main__":
    unittest.main()
