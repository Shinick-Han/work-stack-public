from __future__ import annotations

import http.client
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from workstack.capture import fingerprint_for, source_key_for
from workstack.maintenance import verify_backup
from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import Store, StoreLockedError


CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.stack = WorkStack(self.store)
        self.task = self.stack.add_task("API task", priority="P1")
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.port = self.server.actual_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
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
        status, raw, response_headers = self.request_bytes(
            method, path, outgoing, actual_headers
        )
        return status, json.loads(raw.decode("utf-8")), response_headers

    def request_bytes(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.casefold(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, raw, response_headers

    def browser_headers(self) -> dict[str, str]:
        status, session, _ = self.request("GET", "/api/v1/session")
        self.assertEqual(status, 200)
        return {
            "Origin": "http://127.0.0.1:{}".format(self.port),
            "X-WorkStack-CSRF": session["data"]["csrf_token"],
            "Content-Type": "application/json",
        }

    def test_storage_status_and_verified_backup_download_are_read_only(self):
        status, storage, _ = self.request("GET", "/api/v1/storage")
        self.assertEqual(status, 200)
        self.assertEqual(
            storage["data"]["workspace_id"],
            self.store.load("workspace.json")["id"],
        )
        self.assertEqual(storage["data"]["backup_format"], "workstack-backup-v1")
        self.assertTrue(storage["data"]["restore_requires_shutdown"])
        self.assertEqual(storage["data"]["remote_protocol_version"], 1)
        self.assertRegex(storage["data"]["product_version"], r"^\d+\.\d+\.\d+")

        before = {
            path.name: path.read_bytes()
            for path in Path(self.temporary.name).glob("*.json")
        }
        headers = self.browser_headers()
        encoded = json.dumps({"confirmed": True}, separators=(",", ":")).encode("utf-8")
        status, body, response_headers = self.request_bytes(
            "POST", "/api/v1/maintenance/backup", encoded, headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["content-type"], "application/zip")
        self.assertRegex(
            response_headers["content-disposition"],
            r'^attachment; filename="workstack-backup-[0-9TZ]+-[0-9a-f]{8}\.zip"$',
        )
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        self.assertEqual(response_headers["x-workstack-backup-digest"], digest)

        archive = Path(self.temporary.name) / "download.zip"
        archive.write_bytes(body)
        self.assertEqual(verify_backup(archive).digest, digest)
        self.assertEqual(
            before,
            {path.name: path.read_bytes() for path in Path(self.temporary.name).glob("*.json")},
        )

        status, invalid, _ = self.request(
            "POST",
            "/api/v1/maintenance/backup",
            {"confirmed": False},
            self.browser_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_body")

    def create_linked_capture(self, key_prefix: str = "api.reply") -> dict:
        packet = json.loads(
            (CONTRACTS / "capture-packet-v1.fixture.json").read_text(encoding="utf-8")
        )
        headers = self.browser_headers()
        headers["Idempotency-Key"] = key_prefix + ".ingest"
        status, created, _ = self.request("POST", "/api/v1/captures", packet, headers)
        self.assertEqual(status, 201)
        capture = created["data"]
        headers = self.browser_headers()
        headers["Idempotency-Key"] = key_prefix + ".link"
        status, linked, _ = self.request(
            "POST",
            "/api/v1/captures/{}/link".format(capture["id"]),
            {"task_id": self.task["id"]},
            headers,
        )
        self.assertEqual(status, 200)
        return linked["data"]

    def test_workspace_task_detail_and_revision_conflict(self):
        status, workspace, headers = self.request("GET", "/api/v1/workspace")
        self.assertEqual(status, 200)
        projected = workspace["data"]["tasks"][0]
        self.assertIn("uid", projected)
        self.assertEqual(projected["revision"], 0)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertNotIn("access-control-allow-origin", headers)

        mutation_headers = self.browser_headers()
        status, changed, _ = self.request(
            "PATCH",
            "/api/v1/tasks/{}".format(self.task["id"]),
            {"status": "started", "revision": 0},
            mutation_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed["data"]["revision"], 1)
        status_facts = [
            fact
            for fact in self.store.load("activity.json")["planning_status"]
            if fact.get("task_id") == self.task["id"]
        ]
        self.assertEqual(len(status_facts), 2)
        self.assertEqual(status_facts[-1]["prior_status"], "open")
        self.assertEqual(status_facts[-1]["status"], "started")
        status, conflict, _ = self.request(
            "PATCH",
            "/api/v1/tasks/{}".format(self.task["id"]),
            {"status": "done", "revision": 0},
            mutation_headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "revision_conflict")

        status, detail, _ = self.request("GET", "/api/v1/tasks/{}".format(self.task["id"]))
        self.assertEqual(status, 200)
        self.assertEqual(detail["data"]["task"]["status"], "started")
        self.assertTrue(detail["data"]["activity"])

    def test_v1_task_notes_and_subtasks_are_revision_guarded(self):
        headers = self.browser_headers()
        task_id = self.task["id"]

        status, missing, _ = self.request(
            "POST",
            "/api/v1/tasks/{}/notes".format(task_id),
            {"text": "Capture the rollback owner.", "revision": 0},
            headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(missing["error"]["code"], "idempotency_key_required")

        headers["Idempotency-Key"] = "api.task.note.0001"
        status, noted, _ = self.request(
            "POST",
            "/api/v1/tasks/{}/notes".format(task_id),
            {"text": "Capture the rollback owner.", "revision": 0},
            headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(noted["data"]["revision"], 1)
        self.assertEqual(noted["data"]["notes"][-1]["text"], "Capture the rollback owner.")
        self.assertFalse(noted["meta"]["replayed"])

        status, replayed_note, _ = self.request(
            "POST",
            "/api/v1/tasks/{}/notes".format(task_id),
            {"text": "Capture the rollback owner.", "revision": 0},
            headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(replayed_note["meta"]["replayed"])
        self.assertEqual(replayed_note["data"], noted["data"])

        status, conflict, _ = self.request(
            "POST",
            "/api/v1/tasks/{}/notes".format(task_id),
            {"text": "Different note", "revision": 0},
            headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")

        headers["Idempotency-Key"] = "api.task.subtask.stale.0001"
        status, stale, _ = self.request(
            "POST",
            "/api/v1/tasks/{}/subtasks".format(task_id),
            {"title": "Draft checklist", "priority": "P1", "revision": 0},
            headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(stale["error"]["code"], "revision_conflict")

        headers["Idempotency-Key"] = "api.task.subtask.0001"
        status, added, _ = self.request(
            "POST",
            "/api/v1/tasks/{}/subtasks".format(task_id),
            {"title": "Draft checklist", "priority": "P1", "revision": 1},
            headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(added["data"]["revision"], 2)
        self.assertEqual(added["data"]["subtasks"][0]["id"], "S-1")
        self.assertEqual(added["data"]["subtasks"][0]["status"], "open")
        self.assertFalse(added["meta"]["replayed"])

        status, replayed_subtask, _ = self.request(
            "POST",
            "/api/v1/tasks/{}/subtasks".format(task_id),
            {"title": "Draft checklist", "priority": "P1", "revision": 1},
            headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(replayed_subtask["meta"]["replayed"])
        self.assertEqual(replayed_subtask["data"], added["data"])

        status, completed, _ = self.request(
            "PATCH",
            "/api/v1/tasks/{}/subtasks/S-1".format(task_id),
            {"status": "done", "revision": 2},
            headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(completed["data"]["revision"], 3)
        self.assertEqual(completed["data"]["subtasks"][0]["status"], "done")

        status, invalid, _ = self.request(
            "POST",
            "/api/v1/tasks/{}/notes".format(task_id),
            {"text": "No revision"},
            {**self.browser_headers(), "Idempotency-Key": "api.task.note.invalid.0001"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_body")

    def test_v1_workspace_actions_are_strict_and_project_immediately(self):
        headers = self.browser_headers()

        status, missing, _ = self.request(
            "POST",
            "/api/v1/objectives",
            {"objective": "Ship a usable planning loop", "quarter": "2026-Q3"},
            headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(missing["error"]["code"], "idempotency_key_required")

        headers["Idempotency-Key"] = "api.objective.create.0001"
        status, created_objective, _ = self.request(
            "POST",
            "/api/v1/objectives",
            {"objective": "Ship a usable planning loop", "quarter": "2026-Q3"},
            headers,
        )
        self.assertEqual(status, 201)
        self.assertFalse(created_objective["meta"]["replayed"])
        self.assertEqual(created_objective["data"]["id"], "O-1")
        self.assertEqual(created_objective["data"]["quarter"], "2026-Q3")

        status, replayed_objective, _ = self.request(
            "POST",
            "/api/v1/objectives",
            {"objective": "Ship a usable planning loop", "quarter": "2026-Q3"},
            headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(replayed_objective["meta"]["replayed"])
        self.assertEqual(replayed_objective["data"], created_objective["data"])

        status, conflict, _ = self.request(
            "POST",
            "/api/v1/objectives",
            {"objective": "Different objective", "quarter": "2026-Q3"},
            headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")

        headers["Idempotency-Key"] = "api.note.create.0001"
        status, created_note, _ = self.request(
            "POST",
            "/api/v1/notes",
            {"text": "Decision context", "links": ["O-1", self.task["id"]]},
            headers,
        )
        self.assertEqual(status, 201)
        self.assertFalse(created_note["meta"]["replayed"])
        self.assertEqual(created_note["data"]["id"], "N-0001")
        self.assertEqual(created_note["data"]["links"], ["O-1", self.task["id"]])

        status, replayed_note, _ = self.request(
            "POST",
            "/api/v1/notes",
            {"text": "Decision context", "links": ["O-1", self.task["id"]]},
            headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(replayed_note["meta"]["replayed"])
        self.assertEqual(replayed_note["data"], created_note["data"])

        status, workspace, _ = self.request("GET", "/api/v1/workspace")
        self.assertEqual(status, 200)
        self.assertEqual(workspace["data"]["objectives"][0]["id"], "O-1")
        self.assertEqual(workspace["data"]["notes"][0]["id"], "N-0001")
        self.assertTrue(any(edge["source"] == "N-0001" for edge in workspace["data"]["edges"]))

        status, invalid, _ = self.request(
            "POST",
            "/api/v1/objectives",
            {"objective": "Unknown field", "quarter": "2026-Q3", "extra": True},
            {**headers, "Idempotency-Key": "api.objective.invalid.0001"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_body")

    def test_v1_objective_hub_is_revision_guarded_idempotent_and_auditable(self):
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "api.objective.hub.create.0001"
        status, created, _ = self.request(
            "POST",
            "/api/v1/objectives",
            {"objective": "Make execution reviewable", "quarter": "2026-Q3"},
            headers,
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["data"]["revision"], 0)

        status, detail, _ = self.request("GET", "/api/v1/objectives/O-1")
        self.assertEqual(status, 200)
        self.assertEqual(detail["data"]["objective"]["id"], "O-1")
        self.assertEqual(detail["data"]["objective"]["revision"], 0)
        self.assertEqual(detail["data"]["tasks"], [])

        kr_headers = self.browser_headers()
        kr_headers["Idempotency-Key"] = "api.objective.hub.kr.0001"
        kr_body = {"text": "Review five real work days", "target": "5 days", "revision": 0}
        status, added, _ = self.request(
            "POST", "/api/v1/objectives/O-1/key-results", kr_body, kr_headers
        )
        self.assertEqual(status, 201)
        self.assertEqual(added["data"]["revision"], 1)
        self.assertEqual(added["data"]["key_results"][0]["id"], "KR-1")

        status, replayed, _ = self.request(
            "POST", "/api/v1/objectives/O-1/key-results", kr_body, kr_headers
        )
        self.assertEqual(status, 200)
        self.assertTrue(replayed["meta"]["replayed"])
        self.assertEqual(replayed["data"], added["data"])

        status, stale, _ = self.request(
            "PATCH",
            "/api/v1/objectives/O-1/key-results/KR-1",
            {"progress": 40, "status": "active", "revision": 0},
            self.browser_headers(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(stale["error"]["code"], "revision_conflict")

        status, updated, _ = self.request(
            "PATCH",
            "/api/v1/objectives/O-1/key-results/KR-1",
            {
                "text": "Review seven real work days",
                "target": "7 days",
                "progress": 40,
                "status": "active",
                "revision": 1,
            },
            self.browser_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["data"]["revision"], 2)
        self.assertEqual(
            updated["data"]["key_results"][0]["text"],
            "Review seven real work days",
        )
        self.assertEqual(updated["data"]["key_results"][0]["target"], "7 days")
        self.assertEqual(updated["data"]["key_results"][0]["progress"], 40)

        status, closed, _ = self.request(
            "PATCH",
            "/api/v1/objectives/O-1",
            {
                "objective": "Make execution reviewable every week",
                "quarter": "2026-Q4",
                "status": "done",
                "revision": 2,
            },
            self.browser_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(closed["data"]["objective"], "Make execution reviewable every week")
        self.assertEqual(closed["data"]["quarter"], "2026-Q4")
        self.assertEqual(closed["data"]["status"], "done")
        self.assertEqual(closed["data"]["revision"], 3)

        status, detail, _ = self.request("GET", "/api/v1/objectives/O-1")
        self.assertEqual(status, 200)
        event_types = [event["type"] for event in detail["data"]["activity"]]
        self.assertIn("key_result.created", event_types)
        self.assertIn("key_result.updated", event_types)
        self.assertIn("objective.updated", event_types)
        objective_update = next(
            event for event in detail["data"]["activity"] if event["type"] == "objective.updated"
        )
        self.assertEqual(
            objective_update["details"]["fields"], ["objective", "quarter", "status"]
        )
        self.assertNotIn("Make execution reviewable every week", str(objective_update))

        status, empty, _ = self.request(
            "PATCH",
            "/api/v1/objectives/O-1",
            {"revision": 3},
            self.browser_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(empty["error"]["code"], "invalid_body")

        status, invalid_type, _ = self.request(
            "PATCH",
            "/api/v1/objectives/O-1",
            {"objective": 42, "revision": 3},
            self.browser_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid_type["error"]["code"], "invalid_body")

        status, invalid, _ = self.request(
            "PATCH",
            "/api/v1/objectives/O-1",
            {"status": "done", "revision": 3, "extra": True},
            self.browser_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_body")

    def test_v1_search_is_ranked_bounded_and_privacy_projected(self):
        objective = self.stack.add_objective("Release confidence", "2026-Q3")
        self.stack.add_key_result(objective["id"], "Reduce escaped defects", "< 3")
        self.stack.patch_task(
            self.task["id"],
            {"detail": "Release checklist and rollback evidence", "revision": 0},
        )
        self.stack.add_note("Release decision record", [self.task["id"], objective["id"]])

        status, result, _ = self.request("GET", "/api/v1/search?q=release&limit=20")
        self.assertEqual(status, 200)
        self.assertEqual(result["data"]["query"], "release")
        self.assertLessEqual(len(result["data"]["items"]), 20)
        kinds = {item["kind"] for item in result["data"]["items"]}
        self.assertTrue({"task", "objective", "note"}.issubset(kinds))
        self.assertEqual(result["data"]["items"][0]["id"], self.task["id"])
        for item in result["data"]["items"]:
            self.assertEqual(
                set(item),
                {"kind", "id", "title", "subtitle", "target_kind", "target_id"},
            )

        status, bounded, _ = self.request("GET", "/api/v1/search?q=release&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(bounded["data"]["items"]), 1)

        status, invalid, _ = self.request("GET", "/api/v1/search?q=x")
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_query")

        status, invalid, _ = self.request("GET", "/api/v1/search?q=release&extra=1")
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_query")

    def test_v1_note_rejects_non_array_links(self):
        headers = self.browser_headers()
        status, invalid, _ = self.request(
            "POST",
            "/api/v1/notes",
            {"text": "Bad links", "links": "T-0001"},
            {**headers, "Idempotency-Key": "api.note.invalid.0001"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_body")

    def test_v1_daily_review_is_strict_idempotent_and_readable(self):
        headers = self.browser_headers()
        checkin_body = {"date": "2026-08-30", "time": "09:15"}

        status, missing, _ = self.request(
            "POST", "/api/v1/review/checkin", checkin_body, headers
        )
        self.assertEqual(status, 400)
        self.assertEqual(missing["error"]["code"], "idempotency_key_required")

        headers["Idempotency-Key"] = "api.review.checkin.0001"
        status, checked_in, _ = self.request(
            "POST", "/api/v1/review/checkin", checkin_body, headers
        )
        self.assertEqual(status, 201)
        self.assertFalse(checked_in["meta"]["replayed"])
        self.assertEqual(checked_in["data"], {"date": "2026-08-30", "start_time": "09:15"})

        status, replayed, _ = self.request(
            "POST", "/api/v1/review/checkin", checkin_body, headers
        )
        self.assertEqual(status, 200)
        self.assertTrue(replayed["meta"]["replayed"])
        self.assertEqual(replayed["data"], checked_in["data"])

        entry_body = {
            "date": "2026-08-30",
            "task_id": self.task["id"],
            "done": ["Closed the mutation safety gate"],
            "next": ["Open the Daily Review loop"],
            "blockers": [],
        }

        headers["Idempotency-Key"] = "api.review.entry.0001"
        status, created, _ = self.request(
            "POST", "/api/v1/review/entries", entry_body, headers
        )
        self.assertEqual(status, 201)
        self.assertFalse(created["meta"]["replayed"])
        self.assertEqual(created["data"]["task_id"], self.task["id"])
        self.assertEqual(created["data"]["done"], entry_body["done"])

        status, replayed, _ = self.request(
            "POST", "/api/v1/review/entries", entry_body, headers
        )
        self.assertEqual(status, 200)
        self.assertTrue(replayed["meta"]["replayed"])
        self.assertEqual(replayed["data"], created["data"])

        status, conflict, _ = self.request(
            "POST",
            "/api/v1/review/entries",
            {**entry_body, "done": ["Different"]},
            headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")

        status, review, _ = self.request(
            "GET", "/api/v1/review?date=2026-08-30&days=7"
        )
        self.assertEqual(status, 200)
        self.assertEqual(review["data"]["day"]["start_time"], "09:15")
        self.assertEqual(len(review["data"]["day"]["entries"]), 1)
        self.assertEqual(len(review["data"]["weekly"]["projects"]), 1)
        self.assertEqual(self.stack.get_task(self.task["id"])["revision"], 0)

        status, invalid, _ = self.request(
            "GET", "/api/v1/review?date=2026-08-30&days=0"
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_query")

    def test_v1_work_session_flow_is_strict_and_conflicts_are_explicit(self):
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "api.work-session.start.0001"
        status, started, _ = self.request(
            "POST", "/api/v1/work-sessions", {"task_id": self.task["id"]}, headers
        )
        self.assertEqual(status, 201)
        session_id = started["data"]["id"]
        self.assertEqual(started["data"]["state"], "running")

        status, projection, _ = self.request("GET", "/api/v1/work-sessions")
        self.assertEqual(status, 200)
        self.assertEqual(projection["data"]["current"]["id"], session_id)

        status, invalid, _ = self.request(
            "POST",
            "/api/v1/work-sessions/{}/pause".format(session_id),
            {"unexpected": True},
            {**self.browser_headers(), "Idempotency-Key": "api.work-session.pause.bad"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_body")

        headers = self.browser_headers()
        headers["Idempotency-Key"] = "api.work-session.stop.0001"
        status, stopped, _ = self.request(
            "POST", "/api/v1/work-sessions/{}/stop".format(session_id), {}, headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(stopped["data"]["worklog_state"], "pending")

        headers = self.browser_headers()
        headers["Idempotency-Key"] = "api.work-session.worklog.0001"
        status, recorded, _ = self.request(
            "POST",
            "/api/v1/work-sessions/{}/worklog".format(session_id),
            {"done": ["Finished the focused step"], "next": [], "blockers": []},
            headers,
        )
        self.assertEqual(status, 201)
        self.assertEqual(recorded["data"]["session_id"], session_id)

        status, projection, _ = self.request("GET", "/api/v1/work-sessions")
        self.assertEqual(status, 200)
        self.assertIsNone(projection["data"]["current"])
        self.assertEqual(projection["data"]["pending"], [])

    def test_health_is_content_free_ready_and_request_correlated(self):
        status, health, headers = self.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(health, {"data": {"api_version": "v1", "status": "ready"}})
        self.assertRegex(headers["x-workstack-request-id"], r"^[0-9a-f]{16}$")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertNotIn("workspace", json.dumps(health).casefold())

        _, _, second_headers = self.request("GET", "/api/v1/health")
        self.assertNotEqual(
            headers["x-workstack-request-id"],
            second_headers["x-workstack-request-id"],
        )

    def test_v1_task_creation_is_idempotent_strict_and_retires_legacy_post(self):
        headers = self.browser_headers()
        body = {"title": "One browser intent", "priority": "P1"}

        status, missing, _ = self.request("POST", "/api/v1/tasks", body, headers)
        self.assertEqual(status, 400)
        self.assertEqual(missing["error"]["code"], "idempotency_key_required")

        headers["Idempotency-Key"] = "api.task.create.0001"
        status, created, _ = self.request("POST", "/api/v1/tasks", body, headers)
        self.assertEqual(status, 201)
        self.assertFalse(created["meta"]["replayed"])
        self.assertEqual(created["data"]["title"], body["title"])
        status, replayed, _ = self.request("POST", "/api/v1/tasks", body, headers)
        self.assertEqual(status, 200)
        self.assertTrue(replayed["meta"]["replayed"])
        self.assertEqual(replayed["data"], created["data"])

        status, conflict, _ = self.request(
            "POST", "/api/v1/tasks", {"title": "Different"}, headers
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
        self.assertEqual(len(self.stack.list_tasks(status="all")), 2)

        validation_headers = self.browser_headers()
        validation_headers["Idempotency-Key"] = "api.task.create.0002"
        status, invalid, _ = self.request(
            "POST", "/api/v1/tasks", {"title": "Valid", "unknown": True}, validation_headers
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_request")
        self.assertEqual(len(self.stack.list_tasks(status="all")), 2)

        status, retired, _ = self.request(
            "POST", "/api/tasks", {"title": "Second writer"}, self.browser_headers()
        )
        self.assertEqual(status, 410)
        self.assertEqual(retired["error"]["code"], "legacy_task_writer_disabled")
        self.assertEqual(len(self.stack.list_tasks(status="all")), 2)

    def test_v1_task_scheduling_fields_are_strict_revision_guarded_and_projected(self):
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "api.task.schedule.0001"
        body = {
            "title": "Plan the leadership review",
            "scheduled": "2026-09-02",
            "due": "2026-09-04",
            "estimate_minutes": 90,
        }

        status, created, _ = self.request("POST", "/api/v1/tasks", body, headers)
        self.assertEqual(status, 201)
        task = created["data"]
        self.assertEqual(task["scheduled"], "2026-09-02")
        self.assertEqual(task["due"], "2026-09-04")
        self.assertEqual(task["estimate_minutes"], 90)

        status, changed, _ = self.request(
            "PATCH",
            "/api/v1/tasks/{}".format(task["id"]),
            {"scheduled": None, "estimate_minutes": 120, "revision": 0},
            self.browser_headers(),
        )
        self.assertEqual(status, 200)
        self.assertIsNone(changed["data"]["scheduled"])
        self.assertEqual(changed["data"]["estimate_minutes"], 120)
        self.assertEqual(changed["data"]["revision"], 1)

        for index, invalid_body in enumerate(
            (
                {"title": "Invalid date", "scheduled": "2026-9-2"},
                {"title": "Invalid estimate", "estimate_minutes": True},
                {"title": "Too large", "estimate_minutes": 1441},
            ),
            start=2,
        ):
            invalid_headers = self.browser_headers()
            invalid_headers["Idempotency-Key"] = "api.task.schedule.{:04d}".format(index)
            status, invalid, _ = self.request(
                "POST", "/api/v1/tasks", invalid_body, invalid_headers
            )
            self.assertEqual(status, 400)
            self.assertEqual(invalid["error"]["code"], "invalid_request")

        status, invalid_patch, _ = self.request(
            "PATCH",
            "/api/v1/tasks/{}".format(task["id"]),
            {"estimate_minutes": 0, "revision": 1},
            self.browser_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid_patch["error"]["code"], "invalid_request")

        backlog = self.store.load("backlog.json")
        backlog["tasks"][0].pop("scheduled", None)
        backlog["tasks"][0].pop("estimate_minutes", None)
        self.store.save("backlog.json", backlog)
        status, workspace, _ = self.request("GET", "/api/v1/workspace")
        self.assertEqual(status, 200)
        projected = next(item for item in workspace["data"]["tasks"] if item["id"] == self.task["id"])
        self.assertIsNone(projected["scheduled"])
        self.assertIsNone(projected["estimate_minutes"])

    def test_capture_agent_auth_idempotency_and_size_limit(self):
        packet = json.loads(
            (CONTRACTS / "capture-packet-v1.fixture.json").read_text(encoding="utf-8")
        )
        status, denied, _ = self.request(
            "POST",
            "/api/v1/captures",
            packet,
            {"Idempotency-Key": "api.ingest.0001"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"]["code"], "origin_required")

        agent_headers = {
            "Authorization": "Bearer " + self.server.capture_token,
            "Idempotency-Key": "api.ingest.0001",
            "Content-Type": "application/json",
        }
        status, created, _ = self.request(
            "POST", "/api/v1/captures", packet, agent_headers
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["data"]["status"], "inbox")
        status, replayed, _ = self.request(
            "POST", "/api/v1/captures", packet, agent_headers
        )
        self.assertEqual(status, 200)
        self.assertTrue(replayed["meta"]["replayed"])

        changed = json.loads(json.dumps(packet))
        changed["normalized"]["summary"] += " changed"
        status, conflict, _ = self.request(
            "POST", "/api/v1/captures", changed, agent_headers
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")

        too_large_headers = dict(agent_headers)
        too_large_headers["Idempotency-Key"] = "api.ingest.large"
        status, error, _ = self.request(
            "POST", "/api/v1/captures", b"{" + b"x" * (64 * 1024), too_large_headers
        )
        self.assertEqual(status, 413)
        self.assertEqual(error["error"]["code"], "body_too_large")

    def test_capture_api_rejects_credential_material_without_persistence(self):
        packet = json.loads(
            (CONTRACTS / "capture-packet-v1.fixture.json").read_text(encoding="utf-8")
        )
        packet["source"]["connection_ref"] = "Bearer " + "a" * 32
        packet["source_key"] = source_key_for(packet["source"])
        packet["source"]["fingerprint"] = fingerprint_for(packet["source"])
        headers = {
            "Authorization": "Bearer " + self.server.capture_token,
            "Idempotency-Key": "api.ingest.credential",
            "Content-Type": "application/json",
        }
        status, error, _ = self.request(
            "POST", "/api/v1/captures", packet, headers
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "credential_material_suspected")
        self.assertEqual(self.store.load("captures.json")["captures"], [])
        self.assertFalse(
            any(
                item["key"] == "api.ingest.credential"
                for item in self.store.load("activity.json")["idempotency"]
            )
        )

    def test_capture_browser_mutations_require_csrf_and_idempotency(self):
        packet = json.loads(
            (CONTRACTS / "capture-packet-v1.manual.fixture.json").read_text(encoding="utf-8")
        )
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "browser.capture.1"
        status, created, _ = self.request("POST", "/api/v1/captures", packet, headers)
        self.assertEqual(status, 201)
        capture_id = created["data"]["id"]

        no_key = self.browser_headers()
        status, error, _ = self.request(
            "POST", "/api/v1/captures/{}/link".format(capture_id),
            {"task_id": self.task["id"]},
            no_key,
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "idempotency_key_required")

        headers = self.browser_headers()
        headers["Idempotency-Key"] = "browser.capture.2"
        status, linked, _ = self.request(
            "POST", "/api/v1/captures/{}/link".format(capture_id),
            {"task_id": self.task["id"]}, headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(linked["data"]["linked_task_ids"], [self.task["id"]])

    def test_generic_capture_task_api_is_strict_and_idempotent(self):
        packet = json.loads(
            (CONTRACTS / "capture-packet-v1.fixture.json").read_text(encoding="utf-8")
        )
        headers = self.browser_headers()
        headers["Idempotency-Key"] = "api.capture.task.ingest"
        status, created, _ = self.request("POST", "/api/v1/captures", packet, headers)
        self.assertEqual(status, 201)
        path = "/api/v1/captures/{}/task".format(created["data"]["id"])
        task_input = {
            "intent_id": "11111111-1111-4111-8111-111111111111",
            "title": "Task directly from the source",
            "detail": "The capture is the task basis.",
            "priority": "P1",
            "tags": ["source"],
        }

        status, error, _ = self.request(
            "POST", path, task_input, self.browser_headers()
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "idempotency_key_required")

        headers = self.browser_headers()
        headers["Idempotency-Key"] = "api.capture.task.create"
        status, task, _ = self.request("POST", path, task_input, headers)
        self.assertEqual(status, 201)
        self.assertEqual(task["data"]["title"], task_input["title"])
        self.assertEqual(task["data"]["context_count"], 1)
        status, replay, _ = self.request("POST", path, task_input, headers)
        self.assertEqual(status, 200)
        self.assertTrue(replay["meta"]["replayed"])

        fresh_headers = self.browser_headers()
        fresh_headers["Idempotency-Key"] = "api.capture.task.fresh-retry"
        status, recovered, _ = self.request("POST", path, task_input, fresh_headers)
        self.assertEqual(status, 200)
        self.assertTrue(recovered["meta"]["intent_replayed"])
        self.assertEqual(recovered["data"]["uid"], task["data"]["uid"])
        self.assertEqual(len(self.stack.list_tasks(status="all")), 2)

        status, conflict, _ = self.request(
            "POST", path, {**task_input, "title": "Different task"}, headers
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")

        intent_conflict_headers = self.browser_headers()
        intent_conflict_headers["Idempotency-Key"] = "api.capture.task.intent-conflict"
        status, intent_conflict, _ = self.request(
            "POST", path, {**task_input, "title": "Different task"}, intent_conflict_headers
        )
        self.assertEqual(status, 409)
        self.assertEqual(intent_conflict["error"]["code"], "idempotency_conflict")

        invalid_headers = self.browser_headers()
        invalid_headers["Idempotency-Key"] = "api.capture.task.invalid"
        status, invalid, _ = self.request(
            "POST", path, {**task_input, "recipient": "browser-choice"}, invalid_headers
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_request")
        self.assertEqual(len(self.stack.list_tasks(status="all")), 2)

    def test_reply_api_requires_browser_approval_and_strict_matching_receipt(self):
        capture = self.create_linked_capture()
        approval = {
            "task_id": self.task["id"],
            "capture_id": capture["id"],
            "body": "Thanks. I will send the revised review by Friday.",
            "approved": True,
        }

        status, denied, _ = self.request(
            "POST",
            "/api/v1/replies",
            approval,
            {"Idempotency-Key": "api.reply.approve"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"]["code"], "origin_required")

        no_key = self.browser_headers()
        status, error, _ = self.request("POST", "/api/v1/replies", approval, no_key)
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "idempotency_key_required")

        headers = self.browser_headers()
        headers["Idempotency-Key"] = "api.reply.approve"
        status, created, _ = self.request("POST", "/api/v1/replies", approval, headers)
        self.assertEqual(status, 201)
        reply = created["data"]
        self.assertEqual(reply["state"], "approved")
        self.assertEqual(reply["capture_revision"], capture["revision"])
        self.assertEqual(
            set(reply["target"]),
            {
                "resource_type",
                "connection_ref",
                "container_ref",
                "object_ref",
                "version_ref",
            },
        )
        self.assertNotIn("schema_version", reply)

        status, replay, _ = self.request("POST", "/api/v1/replies", approval, headers)
        self.assertEqual(status, 200)
        self.assertTrue(replay["meta"]["replayed"])
        self.assertEqual(replay["data"]["id"], reply["id"])
        status, conflict, _ = self.request(
            "POST",
            "/api/v1/replies",
            {**approval, "body": "A different approved body."},
            headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")

        receipt = {
            "schema_version": "1.0",
            "reply_id": reply["id"],
            "provider": reply["provider"],
            "outcome": "sent",
            "occurred_at": "2026-08-29T00:15:00Z",
            "body_digest": reply["body_digest"],
            "target_digest": reply["target_digest"],
            "remote_message_ref": "message:opaque-reply-001",
            "web_url": "https://outlook.office.com/mail/deeplink/read/opaque-reply-001",
        }
        receipt_path = "/api/v1/replies/{}/receipt".format(reply["id"])
        mismatch_headers = self.browser_headers()
        mismatch_headers["Idempotency-Key"] = "api.reply.mismatch"
        status, mismatch, _ = self.request(
            "POST",
            receipt_path,
            {**receipt, "target_digest": "sha256:" + "f" * 64},
            mismatch_headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(mismatch["error"]["code"], "reply_receipt_conflict")

        receipt_headers = self.browser_headers()
        receipt_headers["Idempotency-Key"] = "api.reply.receipt"
        status, applied, _ = self.request(
            "POST", receipt_path, receipt, receipt_headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(applied["data"]["state"], "sent")
        status, replayed, _ = self.request(
            "POST", receipt_path, receipt, receipt_headers
        )
        self.assertEqual(status, 200)
        self.assertTrue(replayed["meta"]["replayed"])

        status, detail, _ = self.request("GET", "/api/v1/tasks/{}".format(self.task["id"]))
        self.assertEqual(status, 200)
        self.assertEqual(detail["data"]["replies"][0]["state"], "sent")
        reply_events = [
            item
            for item in detail["data"]["activity"]
            if item["type"].startswith("reply.")
        ]
        self.assertEqual([item["type"] for item in reply_events], ["reply.approved", "reply.sent"])
        self.assertNotIn(approval["body"], json.dumps(reply_events))
        self.assertNotIn("message:opaque-reply-001", json.dumps(reply_events))

    def test_reply_api_rejects_browser_target_raw_token_recipient_and_runner_routes(self):
        capture = self.create_linked_capture("api.reject")
        base = {
            "task_id": self.task["id"],
            "capture_id": capture["id"],
            "body": "Approved plain text",
            "approved": True,
        }
        forbidden = (
            {"target": {"object_ref": "chosen"}},
            {"provider": "microsoft-outlook"},
            {"raw": {"connector": "dump"}},
            {"token": "oauth-material"},
            {"recipients": ["not-accepted"]},
        )
        for index, extra in enumerate(forbidden, start=1):
            with self.subTest(extra=next(iter(extra))):
                headers = self.browser_headers()
                headers["Idempotency-Key"] = "api.reply.reject.{:04d}".format(index)
                status, error, _ = self.request(
                    "POST", "/api/v1/replies", {**base, **extra}, headers
                )
                self.assertEqual(status, 400)
                self.assertEqual(error["error"]["code"], "invalid_request")
        self.assertEqual(self.store.load("replies.json")["replies"], [])

        for path in (
            "/api/v1/replies/R-0001/retry",
            "/api/v1/runner/heartbeat",
            "/api/v1/oob/jobs/lease",
        ):
            with self.subTest(path=path):
                status, error, _ = self.request(
                    "POST", path, {}, self.browser_headers()
                )
                self.assertEqual(status, 404)
                self.assertEqual(error["error"]["code"], "not_found")

    def test_every_legacy_mutation_requires_origin_and_csrf(self):
        session_headers = self.browser_headers()
        valid_origin = session_headers["Origin"]
        valid_csrf = session_headers["X-WorkStack-CSRF"]
        mutations = (
            ("POST", "/api/tasks", {"title": "Must stay blocked"}),
            ("POST", "/api/objectives", {"objective": "Must stay blocked"}),
            (
                "POST",
                "/api/worklog",
                {"task_id": self.task["id"], "done": ["Must stay blocked"]},
            ),
            ("POST", "/api/notes", {"text": "Must stay blocked"}),
            (
                "PATCH",
                "/api/tasks/{}".format(self.task["id"]),
                {"status": "started", "revision": 0},
            ),
        )
        boundary_cases = (
            ({}, "origin_required"),
            (
                {
                    "Origin": "https://example.invalid",
                    "X-WorkStack-CSRF": valid_csrf,
                },
                "invalid_origin",
            ),
            ({"Origin": valid_origin}, "invalid_csrf"),
            (
                {"Origin": valid_origin, "X-WorkStack-CSRF": "wrong-csrf-token"},
                "invalid_csrf",
            ),
        )
        for method, path, body in mutations:
            for headers, expected_code in boundary_cases:
                with self.subTest(method=method, path=path, expected=expected_code):
                    status, error, _ = self.request(method, path, body, headers)
                    self.assertEqual(status, 403)
                    self.assertEqual(error["error"]["code"], expected_code)

        self.assertEqual(len(self.stack.list_tasks(status="all")), 1)
        self.assertEqual(self.stack.get_task(self.task["id"])["status"], "open")
        self.assertEqual(self.stack.list_objectives(status="all"), [])
        self.assertEqual(self.stack.list_worklog()["days"], {})
        self.assertEqual(self.store.load("notes.json")["notes"], [])

    def test_legacy_mutations_are_retired_after_the_valid_browser_boundary(self):
        headers = self.browser_headers()
        cases = (
            (
                "POST",
                "/api/tasks",
                {"title": "Retired task"},
                "legacy_task_writer_disabled",
            ),
            (
                "POST",
                "/api/objectives",
                {"objective": "Retired objective"},
                "legacy_writer_disabled",
            ),
            (
                "POST",
                "/api/worklog",
                {"task_id": self.task["id"], "done": ["Retired update"]},
                "legacy_writer_disabled",
            ),
            (
                "POST",
                "/api/notes",
                {"text": "Retired note"},
                "legacy_writer_disabled",
            ),
            (
                "PATCH",
                "/api/tasks/{}".format(self.task["id"]),
                {"status": "started", "revision": 0},
                "legacy_writer_disabled",
            ),
        )
        before_task = self.stack.get_task(self.task["id"])
        for method, path, body, expected_code in cases:
            with self.subTest(method=method, path=path):
                status, error, _ = self.request(method, path, body, headers)
                self.assertEqual(status, 410)
                self.assertEqual(error["error"]["code"], expected_code)

        self.assertEqual(self.stack.get_task(self.task["id"]), before_task)
        self.assertEqual(len(self.stack.list_tasks(status="all")), 1)
        self.assertEqual(self.stack.list_objectives(status="all"), [])
        self.assertEqual(self.stack.list_worklog()["days"], {})
        self.assertEqual(self.store.load("notes.json")["notes"], [])

    def test_actual_port_host_validation_and_server_lease(self):
        status, error, _ = self.request(
            "GET", "/api/v1/session", headers={"Host": "127.0.0.1:1"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "invalid_host")
        with self.assertRaises(StoreLockedError):
            WorkStack(Store(self.store.root))

    def test_explicit_ssh_forward_port_preserves_loopback_host_and_origin_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            forwarded_stack = WorkStack(Store(Path(directory)))
            forwarded = create_server(
                forwarded_stack, "127.0.0.1", 0, public_port=18765
            )
            thread = threading.Thread(target=forwarded.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", forwarded.actual_port, timeout=5
                )
                connection.request(
                    "GET", "/api/v1/session", headers={"Host": "127.0.0.1:18765"}
                )
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                connection.close()
                self.assertEqual(response.status, 200)

                body = json.dumps({"title": "Forwarded intent"}).encode("utf-8")
                connection = http.client.HTTPConnection(
                    "127.0.0.1", forwarded.actual_port, timeout=5
                )
                connection.request(
                    "POST",
                    "/api/v1/tasks",
                    body=body,
                    headers={
                        "Host": "127.0.0.1:18765",
                        "Origin": "http://127.0.0.1:18765",
                        "X-WorkStack-CSRF": payload["data"]["csrf_token"],
                        "Idempotency-Key": "api.ssh-forward.0001",
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                response.read()
                connection.close()
                self.assertEqual(response.status, 201)
            finally:
                forwarded.shutdown()
                forwarded.server_close()
                thread.join(timeout=5)

    def test_cli_capture_and_ordinary_writer_forward_through_the_running_owner(self):
        packet_path = CONTRACTS / "capture-packet-v1.manual.fixture.json"
        command = [
            sys.executable,
            "run_work_stack.py",
            "--data-dir",
            str(self.store.root),
            "capture",
            "ingest",
            "--stdin",
        ]
        forwarded = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            input=packet_path.read_bytes(),
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(forwarded.returncode, 0, forwarded.stderr.decode(errors="replace"))
        response = json.loads(forwarded.stdout.decode("utf-8"))
        self.assertEqual(response["data"]["source"]["provider"], "manual")
        self.assertEqual(len(self.stack.list_captures("all")), 1)

        before = len(self.stack.list_tasks(status="all"))
        ordinary = [
            sys.executable,
            "run_work_stack.py",
            "--data-dir",
            str(self.store.root),
            "backlog",
            "add",
            "Written through the running owner",
        ]
        # Ordinary CLI writers are routed through the advertised running owner
        # (T2 legacy owner compatibility ruling; 706bbfe / ac9a4c7): the write
        # succeeds, is committed by the owner, and never takes the local lease.
        owned = subprocess.run(
            ordinary,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(owned.returncode, 0, owned.stderr.decode(errors="replace"))
        self.assertEqual(owned.stderr, b"")
        created = json.loads(owned.stdout.decode("utf-8"))
        self.assertEqual(created["title"], "Written through the running owner")
        self.assertEqual(created["status"], "open")
        tasks = self.stack.list_tasks(status="all")
        self.assertEqual(len(tasks), before + 1)
        self.assertIn(created["id"], {task["id"] for task in tasks})

        # Without owner metadata the legacy direct path is taken and is still
        # refused by the server-held writer lease; nothing is written locally.
        self.store.server_info_path.unlink()
        blocked = subprocess.run(
            ordinary,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            timeout=15,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("already owned", blocked.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(len(self.stack.list_tasks(status="all")), before + 1)


if __name__ == "__main__":
    unittest.main()
