"""Public wire contract for ``work-stack backlog start|done|drop|reopen``.

Everything here drives the public ``cli.main`` entry point against a real
ephemeral loopback owner built by ``workstack.server.create_server``, so the
request that leaves the CLI and the response shapes it must accept are the
product's own rather than hand-invented.

This route is a PATCH. The server reads ``Idempotency-Key`` only on POST routes
and keeps no ledger for PATCH, so there is exactly ONE attempt: no key, no
replay, and an ambiguous outcome is reported as unknown instead of guessed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SESSION_PATH = "/api/v1/session"
STORAGE_PATH = "/api/v1/storage"
SYNC_PATH = "/api/v1/sync/status"
TASKS_PATH = "/api/v1/tasks"
# The product's own supported revision ceiling.
MAX_REVISION = 2**53 - 1


def _result_root() -> Path | None:
    configured = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
    if configured:
        root = Path(configured) / "task-status-fixtures"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None


class _RecordingHTTPServer(ThreadingHTTPServer):
    """Retains handler failures instead of printing them into redirected stderr."""

    def __init__(self, *arguments: Any, **keywords: Any) -> None:
        self.handler_errors: list[str] = []
        super().__init__(*arguments, **keywords)

    def handle_error(self, request: Any, client_address: Any) -> None:
        import traceback

        self.handler_errors.append(f"{client_address}: {traceback.format_exc()}")


class _OwnerProxy:
    """Relay to a real owner, recording each request exactly as the CLI sent it.

    It can drop the response to one successful mutation after the owner has
    already applied it, which is the only way to reach the ambiguous outcome
    without inventing a transport.
    """

    def __init__(self, backend_port: int) -> None:
        self.backend_port = backend_port
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.drop_successful_mutations = 0
        # Fired after the genuine backend Task response has been read and
        # BEFORE it is released, so a change cannot race the client.
        self.on_task_get = None
        # Fired before the frozen mutation is forwarded to the owner, so a
        # competing writer lands first and the owner itself answers 409.
        self.before_mutation = None
        # Truncates a genuine successful mutation response to these keys only.
        self.truncate_success_to = None
        # Removes exactly these keys from a genuine successful response.
        self.drop_success_fields = None
        # Replaces one field of a genuine successful response with one value.
        self.replace_success_field = None
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_arguments: Any) -> None:
                return

            def _relay(self, method: str) -> None:
                import http.client

                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else None
                original = {k: v for k, v in self.headers.items()}
                parsed = None
                if payload:
                    with contextlib.suppress(Exception):
                        parsed = json.loads(payload.decode("utf-8"))
                proxy.requests.append({
                    "method": method,
                    "route": self.path,
                    "raw": payload,
                    "headers": dict(original),
                    "body": parsed,
                })
                headers = dict(original)
                headers["Host"] = f"127.0.0.1:{proxy.backend_port}"
                if "Origin" in headers:
                    headers["Origin"] = f"http://127.0.0.1:{proxy.backend_port}"
                if method != "GET" and proxy.before_mutation is not None:
                    hook = proxy.before_mutation
                    proxy.before_mutation = None
                    hook()
                connection = http.client.HTTPConnection(
                    "127.0.0.1", proxy.backend_port, timeout=15
                )
                connection.request(method, self.path, body=payload, headers=headers)
                response = connection.getresponse()
                body = response.read()
                status = response.status
                connection.close()

                if method != "GET":
                    decoded = None
                    with contextlib.suppress(Exception):
                        decoded = json.loads(body.decode("utf-8"))
                    proxy.responses.append({"status": status, "body": decoded})
                    if (
                        200 <= status < 300
                        and proxy.truncate_success_to is not None
                        and isinstance(decoded, dict)
                        and isinstance(decoded.get("data"), dict)
                    ):
                        # A genuine committed answer, reduced to a subset of its
                        # own real keys. Nothing is invented.
                        kept = {
                            key: value
                            for key, value in decoded["data"].items()
                            if key in proxy.truncate_success_to
                        }
                        body = json.dumps({"data": kept}).encode("utf-8")
                    if (
                        200 <= status < 300
                        and isinstance(decoded, dict)
                        and isinstance(decoded.get("data"), dict)
                        and (proxy.drop_success_fields or proxy.replace_success_field)
                    ):
                        kept = dict(decoded["data"])
                        for field in proxy.drop_success_fields or ():
                            kept.pop(field, None)
                        if proxy.replace_success_field:
                            name, value = proxy.replace_success_field
                            kept[name] = value
                        body = json.dumps({"data": kept}).encode("utf-8")
                    if 200 <= status < 300 and proxy.drop_successful_mutations > 0:
                        proxy.drop_successful_mutations -= 1
                        # The owner applied it; the answer never arrives. The
                        # connection closes the ordinary way.
                        self.close_connection = True
                        return
                if method == "GET" and self.path.startswith(TASKS_PATH):
                    # After the genuine Task answer has been read from the
                    # owner and before it is released to the client.
                    hook = proxy.on_task_get
                    proxy.on_task_get = None
                    if hook is not None:
                        hook()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                self._relay("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._relay("POST")

            def do_PATCH(self) -> None:  # noqa: N802
                self._relay("PATCH")

        self.server = _RecordingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)

    @property
    def mutations(self) -> list[dict[str, Any]]:
        return [record for record in self.requests if record["method"] != "GET"]


class _StatusCase(unittest.TestCase):
    """Containment is established before any product import or Store."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
        self.addCleanup(self._remove_fixture_root)
        self._owned_threads: list[threading.Thread] = []
        self._error_sinks: list[tuple[str, _RecordingHTTPServer]] = []
        self.home = Path(self.temporary.name)
        self.root = self.home / "data"
        self.runtime = self.home / "runtime"
        self.scratch = self.home / "tmp"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._saved = {
            name: os.environ.get(name)
            for name in ("WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR")
        }
        os.environ["WORK_STACK_RUNTIME"] = str(self.runtime)
        for name in ("TEMP", "TMP", "TMPDIR"):
            os.environ[name] = str(self.scratch)
        self.addCleanup(self._restore_environment)

        from workstack.service import WorkStack
        from workstack.store import Store

        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.workspace_uid = self.store.load("workspace.json")["id"]

    def _restore_environment(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _remove_fixture_root(self) -> None:
        for thread in self._owned_threads:
            self.assertFalse(thread.is_alive(), "an owned thread outlived its join")
        for label, server in self._error_sinks:
            self.assertEqual(server.handler_errors, [], f"{label} handler error")
        self.temporary.cleanup()
        self.assertFalse(Path(self.temporary.name).exists())

    # -- fixtures ---------------------------------------------------------
    def start_owner(self) -> _OwnerProxy:
        from workstack.server import create_server

        server = create_server(self.stack, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._owned_threads.append(thread)
        self.addCleanup(thread.join, 10)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        proxy = _OwnerProxy(server.server_address[1])
        self._error_sinks.append(("the owner proxy", proxy.server))
        self._owned_threads.append(proxy.thread)
        self.addCleanup(proxy.thread.join, 10)
        self.addCleanup(proxy.close)
        self.write_advertisement(proxy.port)
        return proxy

    def write_advertisement(self, port: int, **overrides: Any) -> Path:
        document = {"version": 1, "host": "127.0.0.1", "port": port}
        document.update(overrides)
        path = self.store.server_info_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        from workstack import cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--data-dir", str(self.root), "backlog", *arguments])
        return code, out.getvalue(), err.getvalue()

    def seed_task(self, title: str) -> str:
        return self.stack.add_task(title)["id"]

    def task_on_disk(self, identifier: str) -> dict[str, Any]:
        document = json.loads(
            self.store.path("backlog.json").read_text(encoding="utf-8")
        )
        matches = [t for t in document["tasks"] if t["id"] == identifier]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def transitions(self, identifier: str) -> list[dict[str, Any]]:
        document = json.loads(
            self.store.path("activity.json").read_text(encoding="utf-8")
        )
        records = document.get("transitions", document.get("events", []))
        return [r for r in records if r.get("task_id") == identifier]


class TaskStatusOwnerContract(_StatusCase):
    def test_a_transition_goes_out_as_one_patch_and_prints_the_owner_record(self) -> None:
        task_id = self.seed_task("Owner routed status")
        before = self.task_on_disk(task_id)["revision"]
        proxy = self.start_owner()

        code, out, err = self.run_cli("start", task_id.lower())

        self.assertEqual(code, 0, err)
        record = json.loads(out)
        self.assertEqual(record["id"], task_id)
        self.assertEqual(record["status"], "started")
        self.assertEqual(record["revision"], before + 1)

        # Exactly one mutation, and it is a PATCH with no idempotency key.
        self.assertEqual(len(proxy.mutations), 1, "one attempt only")
        sent = proxy.mutations[0]
        self.assertEqual(sent["method"], "PATCH")
        self.assertEqual(sent["route"], f"{TASKS_PATH}/{task_id}")
        self.assertEqual(sent["body"], {"status": "started", "revision": before})
        self.assertNotIn("Idempotency-Key", sent["headers"])
        self.assertEqual(sent["headers"]["Origin"], f"http://127.0.0.1:{proxy.port}")
        self.assertTrue(sent["headers"]["X-WorkStack-CSRF"])
        # The Task was read once from the same owner before the write.
        self.assertEqual(
            [r["route"] for r in proxy.requests if r["method"] == "GET"],
            [SESSION_PATH, STORAGE_PATH, SYNC_PATH, f"{TASKS_PATH}/{task_id}"],
        )
        # One durable transition, and the printed record is the owner's.
        self.assertEqual(self.task_on_disk(task_id)["revision"], before + 1)
        self.assertEqual(record, proxy.responses[0]["body"]["data"])

    def test_every_action_maps_to_its_planning_status(self) -> None:
        proxy = self.start_owner()
        for action, status in (
            ("start", "started"),
            ("done", "done"),
            ("drop", "dropped"),
            ("reopen", "open"),
        ):
            with self.subTest(action=action):
                task_id = self.seed_task(f"Mapped {action}")
                code, out, err = self.run_cli(action, task_id)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["status"], status)
                self.assertEqual(proxy.mutations[-1]["body"]["status"], status)

    def test_the_same_status_is_a_no_op_that_does_not_advance_the_revision(self) -> None:
        task_id = self.seed_task("Already open")
        before = self.task_on_disk(task_id)["revision"]
        proxy = self.start_owner()

        code, out, err = self.run_cli("reopen", task_id)

        self.assertEqual(code, 0, err)
        record = json.loads(out)
        self.assertEqual(record["status"], "open")
        # The supported service returns the projected task without advancing
        # the revision for a no-op, and the writer accepts exactly that.
        self.assertEqual(record["revision"], before)
        self.assertEqual(self.task_on_disk(task_id)["revision"], before)
        self.assertEqual(len(proxy.mutations), 1)

    def test_a_lost_response_is_unknown_with_no_second_attempt(self) -> None:
        task_id = self.seed_task("Ambiguous status")
        before = self.task_on_disk(task_id)["revision"]
        proxy = self.start_owner()
        proxy.drop_successful_mutations = 1

        code, out, err = self.run_cli("done", task_id)

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("task status commit is unknown", err)
        # ONE PATCH: no replay, and no POST anywhere on this route.
        self.assertEqual(len(proxy.mutations), 1, "no second attempt")
        self.assertEqual(proxy.mutations[0]["method"], "PATCH")
        self.assertEqual([r["method"] for r in proxy.requests].count("POST"), 0)
        # The owner did apply it exactly once; the CLI must not claim otherwise
        # and must not write locally on top of it.
        self.assertEqual(self.task_on_disk(task_id)["revision"], before + 1)
        self.assertEqual(proxy.responses[0]["status"], 200)

    def test_a_competing_revision_advance_is_a_determinate_conflict(self) -> None:
        task_id = self.seed_task("Conflicting")
        proxy = self.start_owner()

        def advance() -> None:
            # A competing writer lands between the Task read and the frozen
            # PATCH, through the same real owner, so the owner itself answers.
            self.stack.patch_task(
                task_id,
                {"title": "Renamed by a competing writer", "revision": 0},
            )

        proxy.before_mutation = advance

        code, out, err = self.run_cli("start", task_id)

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        # The owner really refused with a determinate conflict.
        self.assertEqual([r["status"] for r in proxy.responses], [409])
        # Exactly one Task read and one PATCH: no refetch, no retry.
        self.assertEqual(
            len([r for r in proxy.requests
                 if r["method"] == "GET" and r["route"].startswith(TASKS_PATH)]),
            1,
        )
        self.assertEqual(len(proxy.mutations), 1)
        # The competing update is durable and the intended status never applied.
        stored = self.task_on_disk(task_id)
        self.assertEqual(stored["revision"], 1)
        self.assertEqual(stored["title"], "Renamed by a competing writer")
        self.assertEqual(self.stack.get_task(task_id)["status"], "open")

    def test_a_changed_advertisement_refuses_before_any_mutation(self) -> None:
        task_id = self.seed_task("Guarded status")
        proxy = self.start_owner()
        # The advertisement is repointed at the real backend endpoint - a
        # different, genuinely bound owner this fixture owns - after the Task
        # read but before its response is released.
        proxy.on_task_get = lambda: self.write_advertisement(proxy.backend_port)

        code, out, err = self.run_cli("start", task_id)

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertEqual(proxy.mutations, [], "nothing may be sent after the change")
        self.assertEqual(self.task_on_disk(task_id)["status"], "open")

    def exhaust_revision(self, task_id: str) -> None:
        """Seed the supported ceiling through the released document repository."""

        from workstack.storage.document_repository import WorkspaceDocument

        document = self.stack.documents.load(WorkspaceDocument.TASKS)
        for entry in document["tasks"]:
            if entry["id"] == task_id:
                entry["revision"] = MAX_REVISION
        self.stack.documents.save(WorkspaceDocument.TASKS, document)
        self.assertEqual(self.task_on_disk(task_id)["revision"], MAX_REVISION)

    def test_a_same_status_no_op_is_allowed_at_the_maximum_revision(self) -> None:
        task_id = self.seed_task("Exhausted no-op")
        self.exhaust_revision(task_id)
        proxy = self.start_owner()

        code, out, err = self.run_cli("reopen", task_id)

        # A no-op never asks for the next revision, so exhaustion is irrelevant
        # to it: the owner returns the existing projected Task unchanged.
        self.assertEqual(code, 0, err)
        record = json.loads(out)
        self.assertEqual(record["status"], "open")
        self.assertEqual(record["revision"], MAX_REVISION)
        self.assertEqual(len(proxy.mutations), 1)
        self.assertEqual(
            proxy.mutations[0]["body"], {"status": "open", "revision": MAX_REVISION}
        )
        self.assertEqual(self.task_on_disk(task_id)["revision"], MAX_REVISION)

    def test_a_transition_at_the_maximum_revision_refuses_before_any_patch(self) -> None:
        task_id = self.seed_task("Exhausted transition")
        self.exhaust_revision(task_id)
        proxy = self.start_owner()

        code, out, err = self.run_cli("start", task_id)

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("safe integer limit", err)
        self.assertEqual(proxy.mutations, [], "nothing may be sent")
        self.assertEqual(self.task_on_disk(task_id)["revision"], MAX_REVISION)
        self.assertEqual(self.stack.get_task(task_id)["status"], "open")

    def test_a_truncated_success_is_refused_without_claiming_noncommit(self) -> None:
        task_id = self.seed_task("Truncated success")
        before = self.task_on_disk(task_id)["revision"]
        proxy = self.start_owner()
        # A genuine committed answer, cut down to only the four fields the
        # writer used to check. Every kept value is the owner's own.
        proxy.truncate_success_to = {"id", "uid", "revision", "status"}

        code, out, err = self.run_cli("start", task_id)

        self.assertEqual(code, 2)
        self.assertEqual(out, "", "a partial Task must never be printed as the result")
        self.assertNotEqual(err, "")
        self.assertEqual(len(proxy.mutations), 1, "one attempt, no retry")
        self.assertEqual(proxy.responses[0]["status"], 200)
        # The owner did commit exactly once; the refusal must not undo or
        # re-send anything, and must not claim the write did not happen.
        self.assertEqual(self.task_on_disk(task_id)["revision"], before + 1)
        self.assertEqual(self.stack.get_task(task_id)["status"], "started")
        self.assertNotIn("did not", err)

    def test_a_healthy_success_carries_the_full_projected_task(self) -> None:
        """Healthy control for the truncation case, on the same fixture."""

        task_id = self.seed_task("Full projection")
        proxy = self.start_owner()

        code, out, err = self.run_cli("start", task_id)

        self.assertEqual(code, 0, err)
        record = json.loads(out)
        for field in ("id", "uid", "revision", "status", "scheduled",
                      "estimate_minutes", "context_count"):
            self.assertIn(field, record)
        self.assertNotIn("status_fact_id", record)
        # Exactly the owner's own document, in its order and with its values.
        self.assertEqual(record, proxy.responses[0]["body"]["data"])
        self.assertEqual(list(record), list(proxy.responses[0]["body"]["data"]))

    def genuine_task_fields(self, task_id: str) -> set[str]:
        """The field names the owner's own Task detail actually carries."""

        return set(self.stack.task_detail(task_id)["task"])

    def test_a_success_reduced_to_the_validated_fields_is_still_refused(self) -> None:
        task_id = self.seed_task("Checklist truncation")
        before = self.task_on_disk(task_id)["revision"]
        proxy = self.start_owner()
        # Exactly the fields the writer validates, every retained value the
        # owner's own. Matching the checklist must not be enough.
        proxy.truncate_success_to = {
            "id", "uid", "revision", "status",
            "scheduled", "estimate_minutes", "context_count",
        }

        code, out, err = self.run_cli("start", task_id)

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertEqual(len(proxy.mutations), 1, "one attempt, no retry")
        self.assertEqual(proxy.responses[0]["status"], 200)
        # The owner committed once; the refusal must not undo or re-send it.
        self.assertEqual(self.task_on_disk(task_id)["revision"], before + 1)
        self.assertEqual(self.stack.get_task(task_id)["status"], "started")

    def test_dropping_one_known_baseline_field_is_refused(self) -> None:
        # One owner holds the Store lease for the whole case; each subcase gets
        # its own Task and its own single dropped field.
        proxy = self.start_owner()
        for field in ("title", "priority", "created", "tags"):
            with self.subTest(dropped=field):
                task_id = self.seed_task(f"Dropped {field}")
                self.assertIn(
                    field,
                    self.genuine_task_fields(task_id),
                    "the field must really be in this Task's baseline",
                )
                before = self.task_on_disk(task_id)["revision"]
                proxy.drop_success_fields = (field,)

                code, out, err = self.run_cli("start", task_id)

                self.assertEqual(code, 2, field)
                self.assertEqual(out, "")
                self.assertNotEqual(err, "")
                self.assertEqual(self.task_on_disk(task_id)["revision"], before + 1)

    def test_a_negative_context_count_is_refused(self) -> None:
        task_id = self.seed_task("Negative count")
        before = self.task_on_disk(task_id)["revision"]
        proxy = self.start_owner()
        proxy.replace_success_field = ("context_count", -1)

        code, out, err = self.run_cli("done", task_id)

        self.assertEqual(code, 2)
        self.assertEqual(out, "", "a negative count must never be printed")
        self.assertNotEqual(err, "")
        self.assertEqual(len(proxy.mutations), 1)
        self.assertEqual(self.task_on_disk(task_id)["revision"], before + 1)

    def test_a_zero_context_count_is_accepted(self) -> None:
        """Positive control: the ordinary value must keep working."""

        task_id = self.seed_task("Zero count")
        proxy = self.start_owner()
        proxy.replace_success_field = ("context_count", 0)

        code, out, err = self.run_cli("done", task_id)

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["context_count"], 0)
        self.assertEqual(len(proxy.mutations), 1)

    def test_a_legacy_task_missing_an_optional_field_still_succeeds(self) -> None:
        """A baseline that never carried a field must not be forced to have one."""

        from workstack.storage.document_repository import WorkspaceDocument

        task_id = self.seed_task("Legacy without tags")
        document = self.stack.documents.load(WorkspaceDocument.TASKS)
        for entry in document["tasks"]:
            if entry["id"] == task_id:
                entry.pop("tags", None)
                entry["legacy_extra"] = {"imported": True}
        self.stack.documents.save(WorkspaceDocument.TASKS, document)
        self.assertNotIn("tags", self.genuine_task_fields(task_id))
        proxy = self.start_owner()

        code, out, err = self.run_cli("start", task_id)

        self.assertEqual(code, 0, err)
        record = json.loads(out)
        self.assertEqual(record["status"], "started")
        self.assertNotIn("tags", record, "no absent legacy field may be synthesized")
        self.assertEqual(record["legacy_extra"], {"imported": True})
        self.assertEqual(record, proxy.responses[0]["body"]["data"])

    def test_an_absent_owner_still_takes_the_local_path(self) -> None:
        task_id = self.seed_task("Local status")

        code, out, err = self.run_cli("start", task_id)

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "started")
        self.assertEqual(self.task_on_disk(task_id)["revision"], 1)


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()
