"""Wave 0 D0 / D4A: Drop vs Delete contract.

Characterization class proves Drop remains PATCH ``status=dropped``. After D2,
v3 preview / guarded DELETE / repository ``hard_delete_task`` purge on a
fixture copy; v4 ``hard_delete_task`` still raises
``task_hard_delete_unsupported``. After D4A the GUI labels are Drop Task and
Delete permanently.

GUI oracle class encodes plan §8.3 Drop vs Delete permanently (D4A). Those
tests are green after D4A. v3 API oracles are green after D2.

Destructive checks use a copy of ``tests/fixtures/store-v3/populated`` only.
They must not open the live SSOT or bind port 8765.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import Store
from workstack.storage.task_relationship_repository import (
    TaskRelationshipError,
    V3TaskRelationshipAdapter,
    V4TaskRelationshipRepository,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "store-v3" / "populated"
LIVE_SSOT = Path.home() / "WorkStack" / "SSOT" / "main"
DIALOG = ROOT / "frontend" / "src" / "features" / "tasks" / "TaskActionsDialog.tsx"
DIALOG_TEST = ROOT / "frontend" / "src" / "features" / "tasks" / "TaskActionsDialog.test.tsx"
RELATIONSHIP_TEST = ROOT / "tests" / "test_storage_task_relationship_repository.py"
RELATIONSHIP_SRC = ROOT / "workstack" / "storage" / "task_relationship_repository.py"
SERVICE_SRC = ROOT / "workstack" / "service.py"
TARGET_ID = "T-0001"
TARGET_REVISION = 2


def _assert_not_live_ssot(path: Path) -> None:
    resolved = path.resolve()
    live = LIVE_SSOT.resolve() if LIVE_SSOT.exists() else LIVE_SSOT
    if resolved == live or live in resolved.parents or resolved in live.parents:
        raise AssertionError("refusing to use live SSOT path: {0}".format(resolved))


def _copy_populated_fixture(directory: Path) -> Path:
    _assert_not_live_ssot(directory)
    if not FIXTURE.is_dir():
        raise AssertionError("missing fixture copy source: {0}".format(FIXTURE))
    target = directory / "ssot"
    shutil.copytree(FIXTURE, target)
    _assert_not_live_ssot(target)
    return target


def _json_files(root: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(root.glob("*.json"))}


def _task_row(root: Path, task_id: str) -> dict:
    backlog = json.loads((root / "backlog.json").read_text(encoding="utf-8"))
    for task in backlog["tasks"]:
        if task["id"] == task_id:
            return task
    raise AssertionError("task {0} missing from fixture copy backlog".format(task_id))


class _FixtureApp:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ws-d0-")
        self.base = Path(self.temporary.name)
        self.runtime = self.base / "runtime"
        self.runtime.mkdir()
        self._env_was = os.environ.get("WORK_STACK_RUNTIME")
        os.environ["WORK_STACK_RUNTIME"] = str(self.runtime)
        self.root = _copy_populated_fixture(self.base)
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

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        actual = dict(headers or {})
        outgoing: bytes | None = None
        if body is not None:
            outgoing = json.dumps(body, separators=(",", ":")).encode("utf-8")
            actual.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=outgoing, headers=actual)
            response = connection.getresponse()
            raw = response.read()
            response_headers = {key.casefold(): value for key, value in response.getheaders()}
            status = response.status
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError) as error:
            # 1.0.7 has no do_DELETE; sending a body can reset the socket.
            # Treat transport refusal as unsupported, not as an ERROR.
            return 501, str(error).encode("utf-8"), {}
        finally:
            connection.close()
        return status, raw, response_headers

    def browser_headers(self) -> dict[str, str]:
        status, raw, _headers = self.request("GET", "/api/v1/session")
        if status != 200:
            raise AssertionError("session failed: {0} {1!r}".format(status, raw))
        payload = json.loads(raw.decode("utf-8"))
        return {
            "Origin": "http://127.0.0.1:{}".format(self.port),
            "X-WorkStack-CSRF": payload["data"]["csrf_token"],
            "Content-Type": "application/json",
        }

    def json(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        status, raw, response_headers = self.request(method, path, body, headers)
        if "application/json" not in response_headers.get("content-type", ""):
            return status, {"_raw": raw.decode("utf-8", errors="replace")}
        return status, json.loads(raw.decode("utf-8"))


class CharacterizeDeleteIsDrop107Test(unittest.TestCase):
    """UI Drop remains Drop; v3 purge is live. These tests must pass on HEAD."""

    def setUp(self) -> None:
        self.app = _FixtureApp()
        self.addCleanup(self.app.close)

    def test_frozen_relationship_test_names_delete_as_dropped_and_hard_delete_unsupported(self) -> None:
        source = RELATIONSHIP_TEST.read_text(encoding="utf-8")
        self.assertIn(
            "def test_delete_is_dropped_transition_and_never_physical_erase",
            source,
        )
        self.assertIn("self.assertTrue(deleted.logically_deleted)", source)
        self.assertIn("self.assertTrue(backend.task_exists(target))", source)
        self.assertIn('"task_hard_delete_unsupported"', source)
        dialog_test = DIALOG_TEST.read_text(encoding="utf-8")
        self.assertIn(
            "requires the exact Task ID before deleting active work while preserving a dropped projection",
            dialog_test,
        )
        self.assertIn("{ status: 'dropped', revision: task.revision }", dialog_test)

    def test_task_actions_dialog_delete_task_label_patches_dropped(self) -> None:
        source = DIALOG.read_text(encoding="utf-8")
        self.assertIn("Drop Task", source)
        self.assertIn("Drop Task…", source)
        self.assertIn("Delete permanently", source)
        self.assertIn("api.patchTask(task.id, { status: 'dropped', revision: task.revision })", source)
        self.assertNotIn("Delete Task", source)
        service = SERVICE_SRC.read_text(encoding="utf-8")
        self.assertIn('patch.get("status") == "dropped"', service)
        self.assertIn("stack.relationship_commands.delete_task(", service)

    def test_hard_delete_task_purges_v3_and_stays_unsupported_on_v4(self) -> None:
        adapter = V3TaskRelationshipAdapter(self.app.stack)
        adapter.hard_delete_task(TARGET_ID, TARGET_REVISION)
        backlog = json.loads((self.app.root / "backlog.json").read_text(encoding="utf-8"))
        self.assertNotIn(TARGET_ID, [task["id"] for task in backlog["tasks"]])
        with self.assertRaises(TaskRelationshipError) as caught_v4:
            V4TaskRelationshipRepository.hard_delete_task(TARGET_ID, TARGET_REVISION)
        self.assertEqual(caught_v4.exception.code, "task_hard_delete_unsupported")
        src = RELATIONSHIP_SRC.read_text(encoding="utf-8")
        self.assertEqual(src.count("task_hard_delete_unsupported"), 1)
        self.assertIn('{"revision": revision, "status": "dropped"}', src)

    def test_http_patch_dropped_leaves_task_body_and_dropped_filter_can_reshow(self) -> None:
        before = _task_row(self.app.root, TARGET_ID)
        self.assertEqual(before["status"], "open")
        self.assertTrue(before["notes"])
        headers = self.app.browser_headers()
        status, payload = self.app.json(
            "PATCH",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {"status": "dropped", "revision": TARGET_REVISION},
            headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["status"], "dropped")
        self.assertEqual(payload["data"]["id"], TARGET_ID)
        after = _task_row(self.app.root, TARGET_ID)
        self.assertEqual(after["id"], TARGET_ID)
        self.assertEqual(after["title"], before["title"])
        self.assertEqual(after["notes"], before["notes"])
        self.assertEqual(after["uid"], before["uid"])
        dropped = self.app.stack.list_tasks(status="dropped")
        self.assertIn(TARGET_ID, [task["id"] for task in dropped])
        active = self.app.stack.list_tasks(status="active")
        self.assertNotIn(TARGET_ID, [task["id"] for task in active])
        get_status, get_payload = self.app.json("GET", "/api/v1/tasks/{0}".format(TARGET_ID))
        self.assertEqual(get_status, 200)
        self.assertEqual(get_payload["data"]["task"]["status"], "dropped")

    def test_deletion_preview_post_is_read_only(self) -> None:
        before = _json_files(self.app.root)
        headers = self.app.browser_headers()
        status, payload = self.app.json(
            "POST",
            "/api/v1/tasks/{0}/deletion-preview".format(TARGET_ID),
            {
                "revision": TARGET_REVISION,
                "workspace_uid": self.app.stack.workspace_projection()["workspace"]["id"],
                "client_request_id": "d0-preview-1",
            },
            headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["preview_token"])
        self.assertEqual(_json_files(self.app.root), before)

    def test_http_delete_without_valid_preview_writes_nothing(self) -> None:
        before = _json_files(self.app.root)
        headers = self.app.browser_headers()
        headers["If-Match"] = str(TARGET_REVISION)
        headers["Idempotency-Key"] = "d0-delete-characterize"
        status, raw, _headers = self.app.request(
            "DELETE",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {
                "preview_token": "missing",
                "confirm": TARGET_ID,
            },
            headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(_task_row(self.app.root, TARGET_ID)["id"], TARGET_ID)
        self.assertEqual(_json_files(self.app.root), before)
        self.assertTrue(raw)


class DropDeleteContractOracleTest(unittest.TestCase):
    """Plan §8.3 GUI oracles. Green after D4A."""

    def setUp(self) -> None:
        self.app = _FixtureApp()
        self.addCleanup(self.app.close)

    def test_oracle_drop_task_and_delete_permanently_are_distinct_actions(self) -> None:
        source = DIALOG.read_text(encoding="utf-8")
        self.assertIn("Drop Task", source)
        self.assertIn("Delete permanently", source)
        self.assertNotIn("Delete Task", source)
        self.assertIn(
            "api.patchTask(task.id, { status: 'dropped', revision: task.revision })",
            source,
        )
        self.assertNotRegex(
            source,
            r"Delete Task[\s\S]*status:\s*'dropped'",
        )

    def test_oracle_drop_is_named_drop_not_delete_and_remains_filterable(self) -> None:
        source = DIALOG.read_text(encoding="utf-8")
        if "Drop Task" not in source or "Delete Task" in source:
            self.fail(
                "plan §8.3 Drop requires a Drop Task action; 1.0.7 only offers "
                "Delete Task that PATCHes status=dropped"
            )
        headers = self.app.browser_headers()
        status, payload = self.app.json(
            "PATCH",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {"status": "dropped", "revision": TARGET_REVISION},
            headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["status"], "dropped")
        self.assertTrue(_task_row(self.app.root, TARGET_ID)["notes"])
        dropped = self.app.stack.list_tasks(status="dropped")
        self.assertEqual([task["id"] for task in dropped if task["id"] == TARGET_ID], [TARGET_ID])


class DropDeleteV3ApiOracleTest(unittest.TestCase):
    """v3 preview / DELETE / repository oracles. Green after D2."""

    def setUp(self) -> None:
        self.app = _FixtureApp()
        self.addCleanup(self.app.close)

    def _fail_if_unsupported(self, status: int, payload: dict, context: str) -> None:
        if status in {404, 405, 501}:
            self.fail(
                "{0}: HTTP {1} {2!r}; unsupported is not success (plan §8.3)".format(
                    context, status, payload
                )
            )

    def _preview(self, client_request_id: str) -> str:
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
        self._fail_if_unsupported(status, payload, "POST deletion-preview")
        self.assertEqual(status, 200)
        token = payload["data"]["preview_token"]
        self.assertTrue(token)
        return token

    def test_oracle_deletion_preview_api_exists(self) -> None:
        headers = self.app.browser_headers()
        workspace_id = self.app.stack.workspace_projection()["workspace"]["id"]
        status, payload = self.app.json(
            "POST",
            "/api/v1/tasks/{0}/deletion-preview".format(TARGET_ID),
            {
                "revision": TARGET_REVISION,
                "workspace_uid": workspace_id,
                "client_request_id": "d0-oracle-preview",
            },
            headers,
        )
        self._fail_if_unsupported(status, payload, "POST deletion-preview")
        self.assertEqual(status, 200)
        data = payload["data"]
        for key in (
            "preview_token",
            "task",
            "removed_task_owned_records",
            "modified_references",
            "unlinked_captures",
            "backup",
            "store_digest",
        ):
            self.assertIn(key, data)
        self.assertTrue(data["preview_token"])

    def test_oracle_permanent_delete_requires_revision_idempotency_preview_and_confirm(self) -> None:
        headers = self.app.browser_headers()
        before = _json_files(self.app.root)
        status, payload = self.app.json(
            "DELETE",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {"confirm": TARGET_ID},
            headers,
        )
        self._fail_if_unsupported(
            status, payload, "DELETE without If-Match/Idempotency-Key/preview"
        )
        self.assertIn(status, {400, 409, 422})
        self.assertEqual(_json_files(self.app.root), before)

        guarded = dict(headers)
        guarded["If-Match"] = str(TARGET_REVISION)
        guarded["Idempotency-Key"] = "d0-oracle-delete-guards"
        status, payload = self.app.json(
            "DELETE",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {
                "preview_token": "oracle-preview-token",
                "confirm": TARGET_ID,
            },
            guarded,
        )
        self._fail_if_unsupported(status, payload, "DELETE with required headers")
        self.assertIn(
            "preview_token",
            json.dumps(payload),
            msg="committed delete must bind a preview token",
        )

    def test_oracle_http_delete_is_hard_purge_not_drop(self) -> None:
        token = self._preview("d0-oracle-purge-preview")
        headers = self.app.browser_headers()
        headers["If-Match"] = str(TARGET_REVISION)
        headers["Idempotency-Key"] = "d0-oracle-purge"
        status, payload = self.app.json(
            "DELETE",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {
                "preview_token": token,
                "confirm": TARGET_ID,
            },
            headers,
        )
        self._fail_if_unsupported(status, payload, "DELETE purge")
        self.assertEqual(status, 200)
        ids = [task["id"] for task in json.loads((self.app.root / "backlog.json").read_text(encoding="utf-8"))["tasks"]]
        self.assertNotIn(TARGET_ID, ids)
        get_status, get_payload = self.app.json("GET", "/api/v1/tasks/{0}".format(TARGET_ID))
        self.assertEqual(get_status, 404)
        dropped = self.app.stack.list_tasks(status="dropped")
        self.assertNotIn(TARGET_ID, [task["id"] for task in dropped])
        self.assertNotEqual(get_payload.get("data", {}).get("status"), "dropped")

    def test_oracle_confirm_mismatch_stale_revision_and_backup_failure_write_nothing(self) -> None:
        headers = self.app.browser_headers()
        before = _json_files(self.app.root)
        backup_token = self._preview("d0-backup-fail-preview")
        cases = (
            (
                "confirm mismatch",
                {"If-Match": str(TARGET_REVISION), "Idempotency-Key": "d0-bad-confirm"},
                {"preview_token": "oracle-preview-token", "confirm": "WRONG-ID"},
            ),
            (
                "stale revision",
                {"If-Match": "0", "Idempotency-Key": "d0-stale-rev"},
                {"preview_token": "oracle-preview-token", "confirm": TARGET_ID},
            ),
            (
                "backup failure",
                {
                    "If-Match": str(TARGET_REVISION),
                    "Idempotency-Key": "d0-backup-fail",
                    "X-WorkStack-Force-Backup-Failure": "1",
                },
                {"preview_token": backup_token, "confirm": TARGET_ID},
            ),
        )
        for label, extra_headers, body in cases:
            with self.subTest(label=label):
                request_headers = dict(headers)
                request_headers.update(extra_headers)
                status, payload = self.app.json(
                    "DELETE",
                    "/api/v1/tasks/{0}".format(TARGET_ID),
                    body,
                    request_headers,
                )
                self._fail_if_unsupported(status, payload, "DELETE {0}".format(label))
                if label == "stale revision":
                    self.assertEqual(status, 409)
                else:
                    self.assertNotEqual(status, 200)
                self.assertEqual(_json_files(self.app.root), before)

    def test_oracle_successful_delete_leaves_zero_task_owned_history(self) -> None:
        try:
            V3TaskRelationshipAdapter(self.app.stack).hard_delete_task(
                TARGET_ID, TARGET_REVISION
            )
        except TaskRelationshipError as error:
            self.fail(
                "hard_delete_task is {0}; unsupported is not success (plan §8.3)".format(
                    error.code
                )
            )
        ids = [task["id"] for task in json.loads((self.app.root / "backlog.json").read_text(encoding="utf-8"))["tasks"]]
        self.assertNotIn(TARGET_ID, ids)
        self.assertEqual(_task_owned_history_count(self.app.stack, TARGET_ID), 0)

    def test_oracle_idempotency_replay_returns_same_receipt_without_new_generation(self) -> None:
        token = self._preview("d0-oracle-idempotent-preview")
        headers = self.app.browser_headers()
        headers["If-Match"] = str(TARGET_REVISION)
        headers["Idempotency-Key"] = "d0-oracle-idempotent"
        body = {"preview_token": token, "confirm": TARGET_ID}
        first_status, first = self.app.json(
            "DELETE", "/api/v1/tasks/{0}".format(TARGET_ID), body, headers
        )
        self._fail_if_unsupported(first_status, first, "DELETE first idempotent commit")
        self.assertEqual(first_status, 200)
        generation = first["data"]["generation"]
        second_status, second = self.app.json(
            "DELETE", "/api/v1/tasks/{0}".format(TARGET_ID), body, headers
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second["data"], first["data"])
        self.assertEqual(second["data"]["generation"], generation)

    def test_oracle_deleted_display_id_is_not_reused(self) -> None:
        token = self._preview("d0-oracle-id-reuse-preview")
        headers = self.app.browser_headers()
        headers["If-Match"] = str(TARGET_REVISION)
        headers["Idempotency-Key"] = "d0-oracle-id-reuse"
        status, payload = self.app.json(
            "DELETE",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {"preview_token": token, "confirm": TARGET_ID},
            headers,
        )
        self._fail_if_unsupported(status, payload, "DELETE before id-reuse check")
        created = self.app.stack.add_task("Replacement after purge")
        self.assertNotEqual(created["id"], TARGET_ID)

    def test_oracle_deleted_task_is_absent_from_workspace_projection(self) -> None:
        token = self._preview("d0-oracle-views-preview")
        headers = self.app.browser_headers()
        headers["If-Match"] = str(TARGET_REVISION)
        headers["Idempotency-Key"] = "d0-oracle-views"
        status, payload = self.app.json(
            "DELETE",
            "/api/v1/tasks/{0}".format(TARGET_ID),
            {"preview_token": token, "confirm": TARGET_ID},
            headers,
        )
        self._fail_if_unsupported(status, payload, "DELETE before view check")
        workspace = self.app.stack.workspace_projection()
        task_ids = [task["id"] for task in workspace["tasks"]]
        self.assertNotIn(TARGET_ID, task_ids)
        blob = json.dumps(workspace)
        self.assertEqual(blob.count(TARGET_ID), 0)


def _task_owned_history_count(stack: WorkStack, task_id: str) -> int:
    activity = stack.store.load("activity.json")
    notes = 0
    try:
        task = stack.get_task(task_id)
        notes = len(task.get("notes") or [])
    except Exception:
        notes = 0
    owned_events = 0
    for bucket in ("activity", "planning_status"):
        for event in activity.get(bucket, []):
            if event.get("task_id") == task_id:
                owned_events += 1
    return notes + owned_events
