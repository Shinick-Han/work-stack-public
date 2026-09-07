"""Wave 2 D2: v3 deletion transaction extras beyond the D0 API oracles."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import Store
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.task_deletion_plan import plan_v3_task_deletion
from workstack.storage.task_deletion_transaction import (
    TaskDeletionTransactionError,
    apply_v3_task_deletion_plan,
    commit_v3_hard_delete,
)
from workstack.storage.task_relationship_repository import (
    TaskRelationshipError,
    V4TaskRelationshipRepository,
)

from tests.test_task_drop_delete_contract import (
    TARGET_ID,
    TARGET_REVISION,
    _copy_populated_fixture,
    _json_files,
    _task_owned_history_count,
    _task_row,
)


NEIGHBOR_ID = "T-00010"
NEIGHBOR_UID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CAPTURE_SUMMARY = "Rollback verification needs an explicit owner before release."
CAPTURE_CONTEXT = "This continues the release-quality gate discussion and is safe demo content."
ACTION_TITLE = "Add rollback verification to the checklist"


class _MutatingApp:
    def __init__(self, mutate=None) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ws-d2-")
        self.base = Path(self.temporary.name)
        self.runtime = self.base / "runtime"
        self.runtime.mkdir()
        self._env_was = os.environ.get("WORK_STACK_RUNTIME")
        os.environ["WORK_STACK_RUNTIME"] = str(self.runtime)
        self.root = _copy_populated_fixture(self.base)
        if mutate is not None:
            mutate(self.root)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.server = create_server(self.stack, "127.0.0.1", 0)
        if self.server.actual_port == 8765:
            raise AssertionError("refusing live product port 8765")
        self.port = self.server.actual_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self._env_was is None:
            os.environ.pop("WORK_STACK_RUNTIME", None)
        else:
            os.environ["WORK_STACK_RUNTIME"] = self._env_was
        self.temporary.cleanup()

    def browser_headers(self) -> dict[str, str]:
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", "/api/v1/session")
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        return {
            "Origin": "http://127.0.0.1:{}".format(self.port),
            "X-WorkStack-CSRF": payload["data"]["csrf_token"],
            "Content-Type": "application/json",
        }

    def json(self, method: str, path: str, body=None, headers=None):
        import http.client

        actual = dict(headers or {})
        outgoing = None
        if body is not None:
            outgoing = json.dumps(body, separators=(",", ":")).encode("utf-8")
            actual.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=outgoing, headers=actual)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
            ctype = {key.casefold(): value for key, value in response.getheaders()}.get(
                "content-type", ""
            )
        finally:
            connection.close()
        if "application/json" not in ctype:
            return status, {"_raw": raw.decode("utf-8", errors="replace")}
        return status, json.loads(raw.decode("utf-8"))


def _inject_neighbor(root: Path) -> None:
    backlog_path = root / "backlog.json"
    activity_path = root / "activity.json"
    backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
    activity = json.loads(activity_path.read_text(encoding="utf-8"))
    backlog["tasks"].append(
        {
            "created": "2026-09-01",
            "dependencies": [],
            "detail": "Neighbor must survive T-0001 purge.",
            "due": None,
            "estimate_minutes": None,
            "id": NEIGHBOR_ID,
            "notes": [],
            "objective_ids": [],
            "parent_id": None,
            "priority": "P2",
            "revision": 0,
            "scheduled": None,
            "status": "open",
            "status_fact_id": "PS-000004",
            "subtasks": [],
            "tags": [],
            "title": "Unrelated neighbor",
            "uid": NEIGHBOR_UID,
            "updated_at": "2026-09-01",
        }
    )
    activity["planning_status"].append(
        {
            "actor": "local.user",
            "created_at": "2026-09-01T03:00:00Z",
            "id": "PS-000004",
            "new_revision": 0,
            "previous_fact_id": None,
            "prior_revision": None,
            "prior_status": None,
            "provenance": "api.v1",
            "status": "open",
            "task_id": NEIGHBOR_ID,
            "task_uid": NEIGHBOR_UID,
            "type": "task.planning_status",
        }
    )
    backlog_path.write_text(json.dumps(backlog, indent=2) + "\n", encoding="utf-8")
    activity_path.write_text(json.dumps(activity, indent=2) + "\n", encoding="utf-8")


def _scan_owned_target(root: Path, task_id: str) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.glob("*.json")):
        if path.name == "activity.json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            for event in payload.get("activity", []):
                if event.get("task_id") == task_id:
                    hits.append("activity:{0}".format(event.get("id")))
            for fact in payload.get("planning_status", []):
                if fact.get("task_id") == task_id:
                    hits.append("planning:{0}".format(fact.get("id")))
            continue
        blob = path.read_text(encoding="utf-8")
        if path.name == "backlog.json" and task_id in [
            task["id"] for task in json.loads(blob)["tasks"]
        ]:
            hits.append("backlog")
            continue
        if path.name in {"notes.json", "captures.json", "replies.json", "worklog.json"}:
            data = json.loads(blob)
            text = json.dumps(data)
            if path.name == "captures.json":
                for capture in data.get("captures", []):
                    if task_id in capture.get("linked_task_ids", []):
                        hits.append("capture-link")
                    if task_id in capture.get("task_hints", []):
                        hits.append("capture-hint")
                    for action in capture.get("normalized", {}).get("action_items", []):
                        if action.get("task_id") == task_id:
                            hits.append("capture-action")
                continue
            if task_id in text:
                hits.append(path.name)
    return hits


class V3TaskDeletionTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _MutatingApp()
        self.addCleanup(self.app.close)

    def _preview_token(self, client_request_id: str) -> str:
        headers = self.app.browser_headers()
        workspace_id = self.app.stack.workspace_projection()["workspace"]["id"]
        status, payload = self.app.json(
            "POST",
            "/api/v1/tasks/{0}/deletion-preview".format(TARGET_ID),
            {
                "revision": TARGET_REVISION,
                "workspace_uid": workspace_id,
                "client_request_id": client_request_id,
            },
            headers,
        )
        self.assertEqual(status, 200, payload)
        return payload["data"]["preview_token"]

    def test_preview_is_read_only(self) -> None:
        before = _json_files(self.app.root)
        generation = self.app.store.generation
        self._preview_token("d2-preview-readonly")
        self.assertEqual(_json_files(self.app.root), before)
        self.assertEqual(self.app.store.generation, generation)

    def test_drop_patch_never_routes_through_permanent_delete(self) -> None:
        before = _task_row(self.app.root, TARGET_ID)
        headers = self.app.browser_headers()
        status, payload = self.app.json(
            "PATCH",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {"status": "dropped", "revision": TARGET_REVISION},
            headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["status"], "dropped")
        after = _task_row(self.app.root, TARGET_ID)
        self.assertEqual(after["id"], TARGET_ID)
        self.assertEqual(after["title"], before["title"])
        self.assertEqual(after["notes"], before["notes"])

    def test_capture_body_survives_unlink(self) -> None:
        token = self._preview_token("d2-capture-body")
        headers = self.app.browser_headers()
        headers["If-Match"] = str(TARGET_REVISION)
        headers["Idempotency-Key"] = "d2-capture-body-del"
        status, payload = self.app.json(
            "DELETE",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {"preview_token": token, "confirm": TARGET_ID},
            headers,
        )
        self.assertEqual(status, 200, payload)
        captures = json.loads((self.app.root / "captures.json").read_text(encoding="utf-8"))
        capture = captures["captures"][0]
        self.assertEqual(capture["id"], "C-0001")
        self.assertEqual(capture["normalized"]["summary"], CAPTURE_SUMMARY)
        self.assertEqual(capture["normalized"]["context"], CAPTURE_CONTEXT)
        self.assertEqual(capture["normalized"]["action_items"][0]["title"], ACTION_TITLE)
        self.assertNotIn(TARGET_ID, capture["linked_task_ids"])
        self.assertNotIn(TARGET_ID, capture["task_hints"])
        self.assertIsNone(capture["normalized"]["action_items"][0]["task_id"])

    def test_zero_owned_content_and_dangling_task_refs(self) -> None:
        token = self._preview_token("d2-zero-owned")
        headers = self.app.browser_headers()
        headers["If-Match"] = str(TARGET_REVISION)
        headers["Idempotency-Key"] = "d2-zero-owned-del"
        status, payload = self.app.json(
            "DELETE",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {"preview_token": token, "confirm": TARGET_ID},
            headers,
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(_scan_owned_target(self.app.root, TARGET_ID), [])
        self.assertEqual(_task_owned_history_count(self.app.stack, TARGET_ID), 0)
        child = _task_row(self.app.root, "T-0002")
        self.assertIsNone(child.get("parent_id"))
        self.assertNotIn(TARGET_ID, child.get("dependencies") or [])
        notes = json.loads((self.app.root / "notes.json").read_text(encoding="utf-8"))
        self.assertEqual(notes["notes"][0]["links"], ["O-1"])

    def test_injected_write_failure_leaves_old_store(self) -> None:
        token = self._preview_token("d2-write-fail-preview")
        before = _json_files(self.app.root)
        generation = self.app.store.generation

        def boom() -> None:
            raise TaskDeletionTransactionError(
                "injected_write_failure", "injected_write_failure", 409
            )

        setattr(self.app.store, "_v3_deletion_before_commit", boom)
        headers = self.app.browser_headers()
        headers["If-Match"] = str(TARGET_REVISION)
        headers["Idempotency-Key"] = "d2-write-fail-del"
        status, payload = self.app.json(
            "DELETE",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {"preview_token": token, "confirm": TARGET_ID},
            headers,
        )
        self.assertNotEqual(status, 200)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "injected_write_failure")
        self.assertEqual(_json_files(self.app.root), before)
        self.assertEqual(self.app.store.generation, generation)
        self.assertEqual(_task_row(self.app.root, TARGET_ID)["id"], TARGET_ID)

    def test_response_loss_replay_keeps_generation(self) -> None:
        token = self._preview_token("d2-response-loss-preview")
        headers = self.app.browser_headers()
        headers["If-Match"] = str(TARGET_REVISION)
        headers["Idempotency-Key"] = "d2-response-loss-del"
        body = {"preview_token": token, "confirm": TARGET_ID}
        first_status, first = self.app.json(
            "DELETE", "/api/v1/tasks/{0}".format(TARGET_ID), body, headers
        )
        self.assertEqual(first_status, 200, first)
        generation = first["data"]["generation"]
        lost_status, lost = self.app.json(
            "DELETE", "/api/v1/tasks/{0}".format(TARGET_ID), body, headers
        )
        self.assertEqual(lost_status, 200)
        self.assertEqual(lost["data"], first["data"])
        self.assertEqual(self.app.store.generation, generation)

    def test_v4_hard_delete_remains_unsupported(self) -> None:
        with self.assertRaises(TaskRelationshipError) as caught:
            V4TaskRelationshipRepository.hard_delete_task(TARGET_ID, TARGET_REVISION)
        self.assertEqual(caught.exception.code, "task_hard_delete_unsupported")

    def test_apply_matches_planner_on_fixture_copy(self) -> None:
        documents = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in self.app.root.glob("*.json")
        }
        original = json.dumps(documents["captures.json"], sort_keys=True)
        plan = plan_v3_task_deletion(
            documents, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )
        applied = apply_v3_task_deletion_plan(documents, plan)
        ids = [task["id"] for task in applied["backlog.json"]["tasks"]]
        self.assertNotIn(TARGET_ID, ids)
        self.assertIn("T-0002", ids)
        self.assertEqual(
            json.dumps(documents["captures.json"], sort_keys=True),
            original,
        )


class NeighborTaskUnchangedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _MutatingApp(_inject_neighbor)
        self.addCleanup(self.app.close)

    def test_neighbor_t00010_survives_t0001_purge(self) -> None:
        before = _task_row(self.app.root, NEIGHBOR_ID)
        headers = self.app.browser_headers()
        workspace_id = self.app.stack.workspace_projection()["workspace"]["id"]
        status, payload = self.app.json(
            "POST",
            "/api/v1/tasks/{0}/deletion-preview".format(TARGET_ID),
            {
                "revision": TARGET_REVISION,
                "workspace_uid": workspace_id,
                "client_request_id": "d2-neighbor-preview",
            },
            headers,
        )
        self.assertEqual(status, 200, payload)
        headers["If-Match"] = str(TARGET_REVISION)
        headers["Idempotency-Key"] = "d2-neighbor-del"
        status, payload = self.app.json(
            "DELETE",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {"preview_token": payload["data"]["preview_token"], "confirm": TARGET_ID},
            headers,
        )
        self.assertEqual(status, 200, payload)
        after = _task_row(self.app.root, NEIGHBOR_ID)
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["uid"], before["uid"])
        self.assertEqual(after["title"], before["title"])
        self.assertEqual(after["detail"], before["detail"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["notes"], before["notes"])
        self.assertNotEqual(after["id"], TARGET_ID)


class HardDeleteWithoutHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _MutatingApp()
        self.addCleanup(self.app.close)

    def test_hard_delete_then_missing_task_is_not_found(self) -> None:
        commit_v3_hard_delete(self.app.store, TARGET_ID, TARGET_REVISION)
        self.assertEqual(_task_owned_history_count(self.app.stack, TARGET_ID), 0)
        with self.assertRaises(TaskDeletionTransactionError) as caught:
            commit_v3_hard_delete(self.app.store, TARGET_ID, TARGET_REVISION)
        self.assertEqual(caught.exception.code, "not_found")

    def test_hard_delete_preserves_high_water_and_skips_reused_ids(self) -> None:
        commit_v3_hard_delete(self.app.store, TARGET_ID, TARGET_REVISION)
        self.assertEqual(self.app.store.load("workspace.json")["task_display_id_high_water"], 2)
        created = self.app.stack.create_task_v1(
            {"title": "After delete"}, "d3b.hw.after-delete"
        )
        self.assertEqual(created["body"]["data"]["id"], "T-0003")
        self.assertEqual(self.app.store.load("workspace.json")["task_display_id_high_water"], 3)


class FailClosedPlanApplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ws-d2u3-")
        self.addCleanup(self.temporary.cleanup)
        self.root = _copy_populated_fixture(Path(self.temporary.name))
        self.documents = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in self.root.glob("*.json")
        }
        self.plan = plan_v3_task_deletion(
            self.documents, task_id=TARGET_ID, expected_revision=TARGET_REVISION
        )

    def _document_bytes(self) -> dict[str, bytes]:
        return {
            name: canonical_json_bytes(body) for name, body in self.documents.items()
        }

    def _refuse(self, plan) -> None:
        before = self._document_bytes()
        with self.assertRaises(TaskDeletionTransactionError) as caught:
            apply_v3_task_deletion_plan(self.documents, plan)
        error = caught.exception
        self.assertEqual(error.code, "invalid_plan")
        self.assertEqual(error.status, 400)
        self.assertEqual(str(error), "invalid_plan")
        self.assertEqual(error.details, {})
        self.assertEqual(self._document_bytes(), before)
        ids = [task["id"] for task in self.documents["backlog.json"]["tasks"]]
        self.assertIn(TARGET_ID, ids)

    def test_unknown_operation_refuses_before_deletion(self) -> None:
        poisoned = replace(
            self.plan,
            operations=self.plan.operations + ({"op": "future_unknown_operation"},),
        )
        self._refuse(poisoned)

    def test_operation_without_op_is_invalid_plan(self) -> None:
        poisoned = replace(
            self.plan,
            operations=self.plan.operations + ({"reply_id": "R-0001"},),
        )
        self._refuse(poisoned)

    def test_recognized_operation_missing_field_is_invalid_plan(self) -> None:
        poisoned = replace(
            self.plan,
            operations=self.plan.operations + ({"op": "remove_reply"},),
        )
        self._refuse(poisoned)

    def test_planner_plan_still_yields_existing_result(self) -> None:
        first = apply_v3_task_deletion_plan(self.documents, self.plan)
        second = apply_v3_task_deletion_plan(self.documents, self.plan)
        ids = [task["id"] for task in first["backlog.json"]["tasks"]]
        self.assertNotIn(TARGET_ID, ids)
        self.assertIn("T-0002", ids)
        self.assertEqual(
            {name: canonical_json_bytes(first[name]) for name in first},
            {name: canonical_json_bytes(second[name]) for name in second},
        )
        self.assertIn(
            TARGET_ID,
            [task["id"] for task in self.documents["backlog.json"]["tasks"]],
        )
