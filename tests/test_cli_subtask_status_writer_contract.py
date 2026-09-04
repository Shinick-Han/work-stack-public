"""Public wire contract for ``work-stack backlog subtask start|done|drop|reopen``.

Everything here drives the public ``cli.main`` entry point against a real
ephemeral loopback owner built by ``workstack.server.create_server``, behind an
owned relay that records each request exactly as the CLI sent it and can drop a
response after the owner has already committed. No CLI child is spawned.

The route is PATCH ``/api/v1/tasks/{parent}/subtasks/{lookup}`` with a frozen
``{status, revision}`` body. There is no idempotency key and no replay: the
owner's setter takes the next revision BEFORE it looks the subtask up, so the
same status is still a real revision+1 write and an exhausted revision refuses
before the lookup.
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import re
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
SUBTASK_KEYS = ("id", "title", "priority", "status")
MAX_REVISION = 2**53 - 1


def _result_root() -> Path | None:
    configured = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
    if configured:
        root = Path(configured) / "subtask-status-fixtures"
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


class _IdleEndpoint:
    """A second bound endpoint that records every contact it receives."""

    def __init__(self) -> None:
        self.contacts: list[str] = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                return

            def _record(self, method: str) -> None:
                endpoint.contacts.append(f"{method} {self.path}")
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                self._record("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._record("POST")

            def do_PATCH(self) -> None:  # noqa: N802
                # This route mutates by PATCH, so the zero-contact assertions
                # must observe PATCH as well as GET and POST.
                self._record("PATCH")

        self.server = _RecordingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


class _OwnerRelay:
    """Relay to a real owner, freezing each request before any Origin rewrite."""

    def __init__(self, backend_port: int) -> None:
        self.backend_port = backend_port
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.drop_successful_posts = 0
        self.on_task_get = None
        self.before_post = None
        self.mutate_success = None
        self.mutate_task_get = None
        # path -> callable(decoded) for any genuine GET envelope.
        self.mutate_get = {}
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
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
                # Frozen before the backend Origin rewrite below.
                relay.requests.append({
                    "method": method,
                    "route": self.path,
                    "raw": payload,
                    "headers": dict(original),
                    "key": original.get("Idempotency-Key"),
                    "body": parsed,
                })
                headers = dict(original)
                headers["Host"] = f"127.0.0.1:{relay.backend_port}"
                if "Origin" in headers:
                    headers["Origin"] = f"http://127.0.0.1:{relay.backend_port}"
                if method != "GET" and relay.before_post is not None:
                    hook = relay.before_post
                    relay.before_post = None
                    hook()
                connection = http.client.HTTPConnection(
                    "127.0.0.1", relay.backend_port, timeout=15
                )
                connection.request(method, self.path, body=payload, headers=headers)
                response = connection.getresponse()
                body = response.read()
                status = response.status
                connection.close()

                decoded = None
                with contextlib.suppress(Exception):
                    decoded = json.loads(body.decode("utf-8"))
                if method != "GET":
                    # The genuine backend answer, recorded before any change.
                    meta = decoded.get("meta") if isinstance(decoded, dict) else None
                    relay.responses.append({
                        "status": status,
                        "body": decoded,
                        "replayed": (meta or {}).get("replayed")
                        if isinstance(meta, dict)
                        else None,
                    })
                    if 200 <= status < 300 and relay.mutate_success is not None:
                        relay.mutate_success(decoded)
                        body = json.dumps(decoded).encode("utf-8")
                    if 200 <= status < 300 and relay.drop_successful_posts > 0:
                        relay.drop_successful_posts -= 1
                        self.close_connection = True
                        return
                if (
                    method == "GET"
                    and isinstance(decoded, dict)
                    and self.path in relay.mutate_get
                ):
                    relay.mutate_get[self.path](decoded)
                    body = json.dumps(decoded).encode("utf-8")
                if (
                    method == "GET"
                    and self.path.startswith(TASKS_PATH)
                    and relay.mutate_task_get is not None
                    and isinstance(decoded, dict)
                ):
                    relay.mutate_task_get(decoded)
                    body = json.dumps(decoded).encode("utf-8")
                if method == "GET" and self.path.startswith(TASKS_PATH):
                    hook = relay.on_task_get
                    relay.on_task_get = None
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
    def posts(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]

    @property
    def patches(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "PATCH"]

    @property
    def mutations(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] != "GET"]

    @property
    def task_gets(self) -> list[dict[str, Any]]:
        return [
            r for r in self.requests
            if r["method"] == "GET" and r["route"].startswith(TASKS_PATH)
        ]


class _SubtaskStatusCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
        # Registered first so LIFO runs every endpoint cleanup before removal.
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
            n: os.environ.get(n)
            for n in ("WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR")
        }
        os.environ["WORK_STACK_RUNTIME"] = str(self.runtime)
        for n in ("TEMP", "TMP", "TMPDIR"):
            os.environ[n] = str(self.scratch)
        self.addCleanup(self._restore_environment)

        from workstack.service import WorkStack
        from workstack.store import Store

        self.store = Store(self.root)
        self.stack = WorkStack(self.store)

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

    def start_owner(self) -> _OwnerRelay:
        from workstack.server import create_server

        server = create_server(self.stack, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._owned_threads.append(thread)
        self.addCleanup(thread.join, 10)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        relay = _OwnerRelay(server.server_address[1])
        self._error_sinks.append(("the owner relay", relay.server))
        self._owned_threads.append(relay.thread)
        self.addCleanup(relay.thread.join, 10)
        self.addCleanup(relay.close)
        self.write_advertisement(relay.port)
        return relay

    def start_idle_endpoint(self) -> _IdleEndpoint:
        endpoint = _IdleEndpoint()
        self._error_sinks.append(("the idle endpoint", endpoint.server))
        self._owned_threads.append(endpoint.thread)
        self.addCleanup(endpoint.thread.join, 10)
        self.addCleanup(endpoint.close)
        endpoint.thread.start()
        return endpoint

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

    def act(self, action: str, task_id: str, lookup: str, *extra: str) -> tuple[int, str, str]:
        # backlog subtask <start|done|drop|reopen> <task> <subtask> [--priority P]
        return self.run_cli("subtask", action, task_id, lookup, *extra)

    def seed_parent(self, title: str, *, subtasks: tuple[str, ...] = ()) -> str:
        task_id = self.stack.add_task(title)["id"]
        for entry in subtasks:
            self.stack.add_subtask(task_id, entry)
        if subtasks:
            stored = self.task_on_disk(task_id)["subtasks"]
            self.assertEqual([s["title"] for s in stored], list(subtasks))
        return task_id

    def task_on_disk(self, identifier: str) -> dict[str, Any]:
        document = json.loads(
            self.store.path("backlog.json").read_text(encoding="utf-8")
        )
        matches = [t for t in document["tasks"] if t["id"] == identifier]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def subtasks_on_disk(self, identifier: str) -> list[dict[str, Any]]:
        return self.task_on_disk(identifier).get("subtasks", [])


class SubtaskStatusOwnerContract(_SubtaskStatusCase):
    def seed_legacy(self, title: str, records: list[dict[str, Any]]) -> str:
        """A parent carrying exactly the given legacy subtask records."""

        from workstack.storage.document_repository import WorkspaceDocument

        parent = self.stack.add_task(title)["id"]
        document = self.stack.documents.load(WorkspaceDocument.TASKS)
        for entry in document["tasks"]:
            if entry["id"] == parent:
                entry["subtasks"] = json.loads(json.dumps(records))
        self.stack.documents.save(WorkspaceDocument.TASKS, document)
        self.assertEqual(self.subtasks_on_disk(parent), records)
        return parent

    def test_each_action_patches_once_and_prints_the_whole_record(self) -> None:
        relay = self.start_owner()
        for action, status in (
            ("start", "started"), ("done", "done"),
            ("drop", "dropped"), ("reopen", "open"),
        ):
            with self.subTest(action=action):
                record = {
                    "id": "S-1", "title": "주간 정리 — café",
                    "priority": "P1", "status": "open",
                    "legacy": {"reviewed": True}, "note": "kept",
                }
                parent = self.seed_legacy("Parent " + action, [record])
                before = self.task_on_disk(parent)["revision"]
                sent = len(relay.mutations)
                gets = len(relay.task_gets)

                code, out, err = self.act(action, parent, "s-1")

                self.assertEqual(code, 0, err)
                printed = json.loads(out)
                expected = dict(record)
                expected["status"] = status
                self.assertEqual(printed, expected, action)
                # The order is the one the owner actually holds. The Store sorts
                # keys when it saves, so a reloaded legacy record's order is the
                # sorted one; status keeps its own position because it existed.
                stored_order = list(self.subtasks_on_disk(parent)[0])
                self.assertEqual(list(printed), stored_order, "key order survives")
                self.assertEqual(len(relay.mutations), sent + 1)
                self.assertEqual(len(relay.task_gets), gets + 1)
                mutation = relay.mutations[-1]
                self.assertEqual(mutation["method"], "PATCH")
                self.assertEqual(
                    mutation["route"], "{}/{}/subtasks/S-1".format(TASKS_PATH, parent)
                )
                self.assertEqual(mutation["body"], {"status": status, "revision": before})
                self.assertNotIn("Idempotency-Key", mutation["headers"])
                self.assertEqual(self.subtasks_on_disk(parent)[0]["status"], status)
                self.assertEqual(self.task_on_disk(parent)["revision"], before + 1)

    def test_priority_is_accepted_and_never_transmitted(self) -> None:
        parent = self.seed_legacy(
            "Priority ignored",
            [{"id": "S-1", "title": "t", "priority": "P2", "status": "open"}],
        )
        relay = self.start_owner()

        code, out, err = self.act("start", parent, "S-1", "--priority", "P0")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["priority"], "P2", "the setter ignores it")
        self.assertEqual(set(relay.mutations[0]["body"]), {"status", "revision"})

    def test_the_same_status_is_still_a_real_write(self) -> None:
        parent = self.seed_legacy(
            "Same status",
            [{"id": "S-1", "title": "t", "priority": "P2", "status": "open"}],
        )
        before = self.task_on_disk(parent)["revision"]
        relay = self.start_owner()

        code, out, err = self.act("reopen", parent, "S-1")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "open")
        self.assertEqual(len(relay.mutations), 1, "no no-op shortcut")
        self.assertEqual(self.task_on_disk(parent)["revision"], before + 1)

    def test_an_exhausted_revision_refuses_before_the_subtask_lookup(self) -> None:
        from workstack.storage.document_repository import WorkspaceDocument

        parent = self.seed_legacy(
            "Exhausted",
            [{"id": "S-1", "title": "t", "priority": "P2", "status": "open"}],
        )
        document = self.stack.documents.load(WorkspaceDocument.TASKS)
        for entry in document["tasks"]:
            if entry["id"] == parent:
                entry["revision"] = MAX_REVISION
        self.stack.documents.save(WorkspaceDocument.TASKS, document)
        relay = self.start_owner()

        for action in ("reopen", "start"):
            with self.subTest(action=action):
                code, out, err = self.act(action, parent, "UNKNOWN-ID")
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("safe integer limit", err, "revision precedes the lookup")
                self.assertEqual(relay.mutations, [])
        self.assertEqual(self.subtasks_on_disk(parent)[0]["status"], "open")

    def test_an_absent_status_is_appended_last(self) -> None:
        parent = self.seed_legacy(
            "Absent status",
            [{"id": "S-1", "title": "legacy", "priority": "P3", "extra": {"deep": True}}],
        )
        self.start_owner()

        code, out, err = self.act("done", parent, "S-1")

        self.assertEqual(code, 0, err)
        printed = json.loads(out)
        # Sorted by the Store on save, with status appended LAST because the
        # legacy record had none.
        self.assertEqual(list(printed), ["extra", "id", "priority", "title", "status"])
        self.assertEqual(list(printed)[-1], "status")
        self.assertEqual(printed["status"], "done")
        self.assertEqual(printed["extra"], {"deep": True})

    def test_an_unexpected_legacy_status_is_overwritten(self) -> None:
        parent = self.seed_legacy(
            "Odd status",
            [{"id": "S-1", "title": "t", "priority": "P2", "status": "archived"}],
        )
        self.start_owner()

        code, out, err = self.act("start", parent, "S-1")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "started")

    def test_legacy_identifiers_route_through_the_setter_rule(self) -> None:
        relay = self.start_owner()
        del relay
        for label, stored, typed in (
            ("zero", "S-0", "s-0"),
            ("leading zero", "S-03", "S-03"),
            ("unicode", "S-٣", "S-٣"),
            ("numeric", 7, "7"),
        ):
            with self.subTest(identifier=label):
                parent = self.seed_legacy(
                    "Legacy " + label,
                    [{"id": stored, "title": "t", "priority": "P2", "status": "open"}],
                )
                code, out, err = self.act("start", parent, typed)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["id"], stored, label)
                self.assertEqual(
                    self.subtasks_on_disk(parent)[0]["status"], "started", label
                )

    def test_a_duplicate_identifier_keeps_first_match(self) -> None:
        parent = self.seed_legacy("Duplicates", [
            {"id": "S-1", "title": "first", "priority": "P2", "status": "open"},
            {"id": "S-1", "title": "second", "priority": "P2", "status": "open"},
        ])
        self.start_owner()

        code, out, err = self.act("done", parent, "S-1")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["title"], "first")
        stored = self.subtasks_on_disk(parent)
        self.assertEqual(stored[0]["status"], "done")
        self.assertEqual(stored[1]["status"], "open", "the later record is untouched")

    def test_an_empty_identifier_refuses_before_the_patch(self) -> None:
        """The one documented compatibility difference from the local setter."""

        parent = self.seed_legacy("Empty identity", [
            {"id": "", "title": "missing id", "priority": "P2", "status": "open"},
            {"id": "S-1", "title": "normal", "priority": "P2", "status": "open"},
        ])
        relay = self.start_owner()

        code, out, err = self.act("start", parent, "   ")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertEqual(relay.mutations, [], "nothing may be sent")
        self.assertEqual(
            [s["status"] for s in self.subtasks_on_disk(parent)], ["open", "open"]
        )

        # Healthy nonempty legacy control on the same parent and owner.
        code, out, err = self.act("start", parent, "S-1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.subtasks_on_disk(parent)[1]["status"], "started")

    def test_a_lost_response_is_unknown_with_one_patch(self) -> None:
        parent = self.seed_legacy(
            "Ambiguous",
            [{"id": "S-1", "title": "t", "priority": "P2", "status": "open"}],
        )
        before = self.task_on_disk(parent)["revision"]
        relay = self.start_owner()
        relay.drop_successful_posts = 1

        code, out, err = self.act("done", parent, "S-1")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("subtask status commit is unknown", err)
        self.assertEqual(len(relay.mutations), 1, "one attempt only")
        self.assertNotIn("Idempotency-Key", relay.mutations[0]["headers"])
        self.assertEqual(self.subtasks_on_disk(parent)[0]["status"], "done")
        self.assertEqual(self.task_on_disk(parent)["revision"], before + 1)

    def test_contradictory_successes_are_refused(self) -> None:
        variants: dict[str, Any] = {
            "other sibling changed": lambda d: d["data"]["subtasks"][1].__setitem__(
                "status", "done"
            ),
            "array shortened": lambda d: d["data"]["subtasks"].pop(),
            "array appended": lambda d: d["data"]["subtasks"].append({"id": "S-9"}),
            "nested true to 1": lambda d: d["data"]["subtasks"][0]["legacy"].__setitem__(
                "ok", 1
            ),
            "wrong revision": lambda d: d["data"].__setitem__("revision", 99),
            "parent title changed": lambda d: d["data"].__setitem__("title", "moved"),
            "missing data": lambda d: d.pop("data"),
        }
        relay = self.start_owner()
        for label, mutate in variants.items():
            with self.subTest(response=label):
                parent = self.seed_legacy("Contradiction " + label, [
                    {"id": "S-1", "title": "t", "priority": "P2", "status": "open",
                     "legacy": {"ok": True}},
                    {"id": "S-2", "title": "u", "priority": "P2", "status": "open"},
                ])
                sent = len(relay.mutations)
                relay.mutate_success = mutate

                code, out, err = self.act("start", parent, "S-1")

                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertEqual(len(relay.mutations), sent + 1, label)
                self.assertEqual(relay.responses[-1]["status"], 200, label)
                # The owner really committed; nothing is rolled back.
                self.assertEqual(
                    self.subtasks_on_disk(parent)[0]["status"], "started", label
                )
        relay.mutate_success = None


class SubtaskStatusOwnerBoundaryContract(_SubtaskStatusCase):
    """Route-specific preflight, binding and conflict cells for this selector."""

    def seed_one(self, title: str) -> str:
        from workstack.storage.document_repository import WorkspaceDocument

        parent = self.stack.add_task(title)["id"]
        document = self.stack.documents.load(WorkspaceDocument.TASKS)
        for entry in document["tasks"]:
            if entry["id"] == parent:
                entry["subtasks"] = [
                    {"id": "S-1", "title": "t", "priority": "P2", "status": "open"}
                ]
        self.stack.documents.save(WorkspaceDocument.TASKS, document)
        return parent

    def test_unusable_advertisements_refuse_without_local_fallback(self) -> None:
        parent = self.seed_one("Guarded metadata")
        idle = self.start_idle_endpoint()
        path = self.store.server_info_path
        path.parent.mkdir(parents=True, exist_ok=True)
        cases = {
            "malformed": b"{not json",
            "structurally invalid": json.dumps({"version": 1, "host": []}).encode(),
            "oversized": json.dumps({
                "version": 1, "host": "127.0.0.1", "port": idle.port, "pad": "x" * 70000
            }).encode(),
        }
        for label, payload in cases.items():
            with self.subTest(advertisement=label):
                path.write_bytes(payload)
                code, out, err = self.act("start", parent, "S-1")
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertNotEqual(err, "", label)
                # No local fallback and no metadata cleanup.
                self.assertEqual(
                    self.subtasks_on_disk(parent)[0]["status"], "open", label
                )
                self.assertTrue(path.exists(), label)
                self.assertEqual(idle.contacts, [], label)

    def test_a_directory_advertisement_refuses_and_is_not_removed(self) -> None:
        parent = self.seed_one("Directory metadata")
        path = self.store.server_info_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()

        code, out, err = self.act("done", parent, "S-1")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(path.is_dir(), "the non-regular entry is left alone")
        self.assertEqual(self.subtasks_on_disk(parent)[0]["status"], "open")

    def test_an_empty_advertisement_refuses_without_local_fallback(self) -> None:
        parent = self.seed_one("Empty metadata")
        self.store.server_info_path.write_bytes(b"")

        code, out, err = self.act("start", parent, "S-1")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertEqual(self.subtasks_on_disk(parent)[0]["status"], "open")
        self.assertTrue(self.store.server_info_path.exists())

    def test_a_foreign_workspace_or_out_of_sync_owner_refuses(self) -> None:
        parent = self.seed_one("Identity guarded")
        relay = self.start_owner()
        variants = {
            "wrong workspace": (
                STORAGE_PATH,
                lambda d: d["data"].__setitem__(
                    "workspace_id", "11111111-1111-4111-8111-111111111111"
                ),
            ),
            "not in sync": (
                SYNC_PATH,
                lambda d: d["data"].__setitem__("state", "external-change-detected"),
            ),
        }
        for label, (route, mutate) in variants.items():
            with self.subTest(preflight=label):
                relay.mutate_get = {route: mutate}
                code, out, err = self.act("start", parent, "S-1")
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertNotEqual(err, "", label)
                self.assertEqual(relay.mutations, [], label)
                self.assertEqual(
                    self.subtasks_on_disk(parent)[0]["status"], "open", label
                )
        relay.mutate_get = {}
        code, out, err = self.act("start", parent, "S-1")
        self.assertEqual(code, 0, err)

    def test_a_different_parent_uid_refuses(self) -> None:
        parent = self.seed_one("Wrong uid")
        relay = self.start_owner()
        relay.mutate_success = lambda d: d["data"].__setitem__(
            "uid", "11111111-1111-4111-8111-111111111111"
        )

        code, out, err = self.act("start", parent, "S-1")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertEqual(len(relay.mutations), 1)
        self.assertEqual(relay.responses[-1]["status"], 200)
        # The owner really applied it; the refusal makes no rollback claim.
        self.assertEqual(self.subtasks_on_disk(parent)[0]["status"], "started")

    def test_a_concurrent_advance_produces_one_real_conflict(self) -> None:
        parent = self.seed_one("Conflicting")
        relay = self.start_owner()
        # A genuine competing write through the same owner, after the frozen
        # revision was read and before the PATCH is forwarded.
        relay.before_post = lambda: self.stack.add_task_note(parent, "competing")

        code, out, err = self.act("done", parent, "S-1")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual([r["status"] for r in relay.responses], [409])
        self.assertEqual(len(relay.task_gets), 1, "no refetch")
        self.assertEqual(len(relay.mutations), 1, "no retry")
        self.assertEqual(self.subtasks_on_disk(parent)[0]["status"], "open")
        self.assertEqual(len(self.task_on_disk(parent)["notes"]), 1)

    def test_binding_changes_after_the_genuine_get_refuse_before_the_patch(self) -> None:
        parent = self.seed_one("Rebound")
        idle = self.start_idle_endpoint()
        relay = self.start_owner()

        def replaced() -> None:
            self.write_advertisement(idle.port)

        def removed() -> None:
            self.store.server_info_path.unlink()

        def incompatible() -> None:
            self.write_advertisement(idle.port, host="127.0.0.2")

        for label, mutate in (
            ("replaced", replaced), ("removed", removed), ("incompatible", incompatible)
        ):
            with self.subTest(binding=label):
                sent = len(relay.mutations)
                relay.on_task_get = mutate
                code, out, err = self.act("start", parent, "S-1")
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertNotEqual(err, "", label)
                self.assertEqual(len(relay.mutations), sent, "zero PATCH")
                self.assertEqual(idle.contacts, [], "the replacement is never contacted")
                self.assertEqual(
                    self.subtasks_on_disk(parent)[0]["status"], "open", label
                )
                if label == "removed":
                    self.write_advertisement(relay.port)
                else:
                    self.write_advertisement(relay.port)
        self.assertEqual(idle.contacts, [])

    def test_a_genuinely_absent_owner_still_writes_locally(self) -> None:
        parent = self.seed_one("Absent owner")

        code, out, err = self.act("start", parent, "S-1")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "started")
        self.assertEqual(self.subtasks_on_disk(parent)[0]["status"], "started")


class _AdvertisementReadFault:
    """An authorized FIXTURE fault at the advertisement binary-read boundary.

    It replaces the name ``open`` inside workstack.cli_writer only, raises
    PermissionError(EACCES) for exactly that one path opened for binary
    reading, and delegates every other open to the genuine builtin. No product
    file is edited and no OS permission is changed, so this asserts the
    writer's behaviour on an unreadable advertisement without claiming
    anything about real filesystem ACLs.
    """

    def __init__(self, target: Path) -> None:
        self.target = target.resolve()
        self.matches = 0
        self.delegated = 0

    def __call__(self, file: Any, mode: str = "r", *arguments: Any, **keywords: Any) -> Any:
        try:
            same = Path(file).resolve() == self.target
        except (TypeError, ValueError, OSError):
            same = False
        if same and "b" in mode and "r" in mode:
            self.matches += 1
            raise PermissionError(errno.EACCES, "synthetic unreadable advertisement")
        self.delegated += 1
        return open(file, mode, *arguments, **keywords)


class SubtaskStatusUnreadableAdvertisementContract(_SubtaskStatusCase):
    """The EACCES boundary, with its own healthy control on the same owner."""

    def seed_one(self, title: str) -> str:
        from workstack.storage.document_repository import WorkspaceDocument

        parent = self.stack.add_task(title)["id"]
        document = self.stack.documents.load(WorkspaceDocument.TASKS)
        for entry in document["tasks"]:
            if entry["id"] == parent:
                entry["subtasks"] = [
                    {"id": "S-1", "title": "t", "priority": "P2", "status": "open"}
                ]
        self.stack.documents.save(WorkspaceDocument.TASKS, document)
        return parent

    def test_an_unreadable_advertisement_refuses_and_the_same_run_then_succeeds(self) -> None:
        from workstack import cli_writer

        parent = self.seed_one("Unreadable")
        idle = self.start_idle_endpoint()
        relay = self.start_owner()
        advertisement = self.store.server_info_path
        original_bytes = advertisement.read_bytes()
        planning_before = self.store.path("backlog.json").read_bytes()

        fault = _AdvertisementReadFault(advertisement)
        def remove_fault() -> None:
            # Tolerant so the explicit removal below and this cleanup cannot
            # collide; the injection must never outlive the case either way.
            if hasattr(cli_writer, "open"):
                delattr(cli_writer, "open")

        cli_writer.open = fault  # type: ignore[attr-defined]
        self.addCleanup(remove_fault)

        code, out, err = self.act("start", parent, "S-1")

        # Exactly one matching injection, and every other open delegated.
        self.assertEqual(fault.matches, 1, "one advertisement read was faulted")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("unreadable", err)
        self.assertNotIn(str(advertisement), err, "the path is not quoted")
        self.assertNotIn(str(self.root), err)
        # The owner was never reached and nothing was written anywhere.
        self.assertEqual(relay.requests, [], "zero owner contacts")
        self.assertEqual(relay.mutations, [], "zero PATCH")
        self.assertEqual(idle.contacts, [], "zero wrong-endpoint contacts")
        self.assertEqual(advertisement.read_bytes(), original_bytes)
        self.assertEqual(self.store.path("backlog.json").read_bytes(), planning_before)
        self.assertEqual(self.subtasks_on_disk(parent)[0]["status"], "open")

        # Remove the injection and run the IDENTICAL argv against the SAME
        # real owner: the refusal was the unreadable read, nothing else.
        remove_fault()
        self.assertFalse(hasattr(cli_writer, "open"), "the injection is gone")
        before = self.task_on_disk(parent)["revision"]

        code, out, err = self.act("start", parent, "S-1")

        self.assertEqual(code, 0, err)
        printed = json.loads(out)
        stored = self.subtasks_on_disk(parent)[0]
        self.assertEqual(printed, stored)
        self.assertEqual(list(printed), list(stored), "the owner's own key order")
        self.assertEqual(len(relay.task_gets), 1, "one Task GET, no refetch")
        self.assertEqual(len(relay.mutations), 1, "one PATCH, no retry")
        self.assertEqual(self.task_on_disk(parent)["revision"], before + 1)
        self.assertEqual(idle.contacts, [])

    def test_the_idle_endpoint_records_a_patch(self) -> None:
        """The zero-contact assertions rest on a recorder that really records."""

        import http.client

        idle = self.start_idle_endpoint()
        self.assertEqual(idle.contacts, [])
        connection = http.client.HTTPConnection("127.0.0.1", idle.port, timeout=10)
        # A probe against the inert endpoint only: it owns no workspace and
        # performs no domain write.
        connection.request("PATCH", "/probe", body=b"{}", headers={"Content-Length": "2"})
        connection.getresponse().read()
        connection.close()

        self.assertEqual(idle.contacts, ["PATCH /probe"])
