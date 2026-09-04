"""Public wire contract for ``work-stack backlog subtask add``.

Everything here drives the public ``cli.main`` entry point against a real
ephemeral loopback owner built by ``workstack.server.create_server``, behind an
owned relay that records each request exactly as the CLI sent it and can drop a
response after the owner has already committed. No CLI child is spawned.

The route is the admitted POST mechanism: ``/api/v1/tasks/{id}/subtasks`` with a
frozen ``{title, priority, revision}`` body, one identical replay under the same
key after a lost answer, and the owner's own idempotency ledger deciding.
"""

from __future__ import annotations

import contextlib
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
        root = Path(configured) / "subtask-fixtures"
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
                if method == "POST" and relay.before_post is not None:
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
                if method == "POST":
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
    def task_gets(self) -> list[dict[str, Any]]:
        return [
            r for r in self.requests
            if r["method"] == "GET" and r["route"].startswith(TASKS_PATH)
        ]


class _SubtaskCase(unittest.TestCase):
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

    def add(self, task_id: str, title: str, *extra: str) -> tuple[int, str, str]:
        # The frozen syntax: backlog subtask add <task> <title> [--priority P]
        return self.run_cli("subtask", "add", task_id, title, *extra)

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


class SubtaskOwnerContract(_SubtaskCase):
    def test_the_wire_carries_the_frozen_body_and_prints_only_the_new_subtask(self) -> None:
        parent = self.seed_parent("Parent", subtasks=("first seeded", "second seeded"))
        sibling = self.seed_parent("Sibling")
        before = self.task_on_disk(parent)["revision"]
        baseline = [dict(s) for s in self.subtasks_on_disk(parent)]
        self.assertEqual(len(baseline), 2)
        relay = self.start_owner()

        code, out, err = self.add("  " + parent.lower() + "  ", "  주간 정리 — café  ")

        self.assertEqual(code, 0, err)
        record = json.loads(out)
        # Exactly the appended subtask, in the frozen key order.
        self.assertEqual(tuple(record), SUBTASK_KEYS)
        self.assertEqual(record["title"], "주간 정리 — café")
        self.assertEqual(record["priority"], "P2")
        self.assertEqual(record["status"], "open")
        self.assertRegex(record["id"], r"^S-\d+$")

        self.assertEqual(len(relay.posts), 1)
        sent = relay.posts[0]
        self.assertEqual(sent["route"], f"{TASKS_PATH}/{parent}/subtasks")
        self.assertEqual(
            sent["body"],
            {"title": "주간 정리 — café", "priority": "P2", "revision": before},
        )
        self.assertTrue(sent["key"])
        self.assertEqual(sent["headers"]["Origin"], f"http://127.0.0.1:{relay.port}")
        self.assertTrue(sent["headers"]["X-WorkStack-CSRF"])
        self.assertEqual(len(relay.task_gets), 1)
        self.assertEqual(relay.responses[0]["status"], 200)
        self.assertIs(relay.responses[0]["replayed"], False)

        stored = self.subtasks_on_disk(parent)
        self.assertEqual(stored[: len(baseline)], baseline, "the prefix is preserved")
        self.assertEqual(len(stored), len(baseline) + 1)
        self.assertEqual(stored[-1], record)
        self.assertEqual(self.task_on_disk(parent)["revision"], before + 1)
        self.assertEqual(self.subtasks_on_disk(sibling), [])

    def test_an_explicit_priority_is_frozen_and_duplicates_are_separate_intents(self) -> None:
        parent = self.seed_parent("Priorities")
        relay = self.start_owner()

        code, out, err = self.add(parent, "same words", "--priority", "P0")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["priority"], "P0")
        self.assertEqual(relay.posts[0]["body"]["priority"], "P0")

        code, second, err = self.add(parent, "same words", "--priority", "P0")
        self.assertEqual(code, 0, err)
        self.assertNotEqual(json.loads(second)["id"], json.loads(out)["id"])
        self.assertEqual(len(self.subtasks_on_disk(parent)), 2)
        self.assertNotEqual(relay.posts[0]["key"], relay.posts[1]["key"])

    def test_local_and_owner_output_agree(self) -> None:
        local_parent = self.seed_parent("Local parity")
        code, local_out, err = self.add(local_parent, "parity text")
        self.assertEqual(code, 0, err)

        owner_parent = self.seed_parent("Owner parity")
        self.start_owner()
        code, owner_out, err = self.add(owner_parent, "parity text")

        self.assertEqual(code, 0, err)
        local, owner = json.loads(local_out), json.loads(owner_out)
        self.assertEqual(list(local), list(owner))
        self.assertEqual(
            {k: v for k, v in local.items() if k != "id"},
            {k: v for k, v in owner.items() if k != "id"},
        )

    def test_refusals_happen_before_any_post(self) -> None:
        parent = self.seed_parent("Refusals")
        relay = self.start_owner()

        code, out, err = self.add("T-9999", "unknown parent")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(relay.posts, [], "an unknown Task must not post")

        code, out, err = self.add(parent, "   ")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(relay.posts, [], "a blank title must not post")

        code, out, err = self.add(parent, "healthy control")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(relay.posts), 1)

    def test_an_exhausted_parent_revision_refuses_before_the_post(self) -> None:
        from workstack.storage.document_repository import WorkspaceDocument

        parent = self.seed_parent("Exhausted")
        document = self.stack.documents.load(WorkspaceDocument.TASKS)
        for entry in document["tasks"]:
            if entry["id"] == parent:
                entry["revision"] = MAX_REVISION
        self.stack.documents.save(WorkspaceDocument.TASKS, document)
        relay = self.start_owner()

        code, out, err = self.add(parent, "must not post")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("safe integer limit", err)
        self.assertEqual(relay.posts, [])
        self.assertEqual(self.subtasks_on_disk(parent), [])

    def test_an_explicit_null_or_nonlist_baseline_refuses_before_the_post(self) -> None:
        parent = self.seed_parent("Null baseline")
        relay = self.start_owner()
        for label, value in (("null", None), ("non-list", {"S-1": "x"})):
            with self.subTest(baseline=label):
                relay.mutate_task_get = (
                    lambda d, v=value: d["data"]["task"].__setitem__("subtasks", v)
                )
                code, out, err = self.add(parent, "must not post")
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertNotEqual(err, "", label)
                self.assertEqual(relay.posts, [], label)
                self.assertEqual(self.subtasks_on_disk(parent), [], label)
        # Absence stays the legacy empty default: a healthy control on the same
        # owner still appends.
        relay.mutate_task_get = None
        code, out, err = self.add(parent, "healthy control")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.subtasks_on_disk(parent)), 1)

    def test_a_replaced_advertisement_after_the_task_read_refuses(self) -> None:
        parent = self.seed_parent("Guarded")
        idle = self.start_idle_endpoint()
        relay = self.start_owner()
        relay.on_task_get = lambda: self.write_advertisement(idle.port)

        code, out, err = self.add(parent, "must not post")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertEqual(relay.posts, [])
        self.assertEqual(idle.contacts, [], "the replacement is never contacted")
        self.assertEqual(self.subtasks_on_disk(parent), [])

    def test_a_competing_revision_advance_is_a_determinate_conflict(self) -> None:
        parent = self.seed_parent("Conflicting")
        relay = self.start_owner()
        relay.before_post = lambda: self.stack.add_subtask(parent, "competing")

        code, out, err = self.add(parent, "loser")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual([r["status"] for r in relay.responses], [409])
        self.assertEqual(len(relay.task_gets), 1, "no refetch")
        self.assertEqual(len(relay.posts), 1, "no retry")
        stored = self.subtasks_on_disk(parent)
        self.assertEqual([s["title"] for s in stored], ["competing"])

    def test_one_lost_response_replays_once_and_appends_exactly_one(self) -> None:
        parent = self.seed_parent("Ambiguous", subtasks=("seeded",))
        before = self.task_on_disk(parent)["revision"]
        relay = self.start_owner()
        relay.drop_successful_posts = 1

        code, out, err = self.add(parent, "committed once")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["title"], "committed once")
        self.assertEqual(len(relay.posts), 2, "exactly one replay")
        first, second = relay.posts
        self.assertEqual(second["raw"], first["raw"])
        self.assertEqual(second["key"], first["key"])
        self.assertEqual(second["route"], first["route"])
        self.assertEqual([r["status"] for r in relay.responses], [200, 200])
        self.assertEqual([r["replayed"] for r in relay.responses], [False, True])
        stored = self.subtasks_on_disk(parent)
        self.assertEqual(len(stored), 2, "the replay must not duplicate")
        self.assertEqual(self.task_on_disk(parent)["revision"], before + 1)

    def test_a_second_loss_reports_commit_unknown(self) -> None:
        parent = self.seed_parent("Twice lost", subtasks=("seeded",))
        relay = self.start_owner()
        relay.drop_successful_posts = 2

        code, out, err = self.add(parent, "unknown outcome")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("subtask commit is unknown", err)
        self.assertEqual(len(relay.posts), 2, "no third send")
        self.assertEqual(len(self.subtasks_on_disk(parent)), 2)

    def test_contradictory_success_responses_are_refused(self) -> None:
        cases: dict[str, Any] = {
            "wrong parent id": lambda d: d["data"].__setitem__("id", "T-9999"),
            "wrong uid": lambda d: d["data"].__setitem__(
                "uid", "11111111-1111-4111-8111-111111111111"
            ),
            "wrong revision": lambda d: d["data"].__setitem__("revision", 99),
            "no append": lambda d: d["data"].__setitem__("subtasks", []),
            "two appends": lambda d: d["data"]["subtasks"].append(
                dict(d["data"]["subtasks"][-1])
            ),
            "rewritten history": lambda d: d["data"]["subtasks"][0].__setitem__(
                "title", "rewritten"
            ),
            "wrong new title": lambda d: d["data"]["subtasks"][-1].__setitem__(
                "title", "not what was sent"
            ),
            "wrong new status": lambda d: d["data"]["subtasks"][-1].__setitem__(
                "status", "done"
            ),
            "extra key on the new record": lambda d: d["data"]["subtasks"][-1].__setitem__(
                "extra", 1
            ),
            "missing data": lambda d: d.pop("data"),
        }
        relay = self.start_owner()
        for label, mutate in cases.items():
            with self.subTest(response=label):
                parent = self.seed_parent(f"Contradiction {label}", subtasks=("seeded",))
                sent_before = len(relay.posts)
                relay.mutate_success = mutate

                code, out, err = self.add(parent, "target text")

                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertNotEqual(err, "", label)
                self.assertEqual(len(relay.posts), sent_before + 1, label)
                self.assertEqual(relay.responses[-1]["status"], 200, label)
                # The owner really committed once; the refusal must not claim
                # otherwise and must not roll anything back.
                self.assertEqual(len(self.subtasks_on_disk(parent)), 2, label)


    def seed_rich_parent(self, title: str) -> str:
        """A parent with real history, a note and a nested legacy extra."""

        from workstack.storage.document_repository import WorkspaceDocument

        parent = self.seed_parent(title, subtasks=("first seeded", "second seeded"))
        self.stack.add_task_note(parent, "a real parent note")
        document = self.stack.documents.load(WorkspaceDocument.TASKS)
        for entry in document["tasks"]:
            if entry["id"] == parent:
                entry["legacy_parent"] = {"reviewed": True, "source": "imported"}
        self.stack.documents.save(WorkspaceDocument.TASKS, document)
        stored = self.task_on_disk(parent)
        self.assertIs(stored["legacy_parent"]["reviewed"], True)
        self.assertEqual(len(stored["notes"]), 1)
        self.assertEqual(len(stored["subtasks"]), 2)
        return parent

    def assert_committed_then_refused(
        self, relay: Any, parent: str, before: dict[str, Any], sent_before: int
    ) -> None:
        """The owner really appended once; only the delivered answer was wrong."""

        self.assertEqual(len(relay.posts), sent_before + 1)
        self.assertEqual(relay.responses[-1]["status"], 200)
        stored = self.task_on_disk(parent)
        self.assertEqual(len(stored["subtasks"]), len(before["subtasks"]) + 1)
        self.assertEqual(stored["revision"], before["revision"] + 1)
        # Nothing was rolled back and nothing else moved.
        self.assertEqual(stored["notes"], before["notes"])
        self.assertEqual(stored["title"], before["title"])
        self.assertEqual(stored["subtasks"][: len(before["subtasks"])], before["subtasks"])
        self.assertIs(stored["legacy_parent"]["reviewed"], True)

    def test_parent_values_cannot_change_in_a_committed_response(self) -> None:
        variants: dict[str, Any] = {
            "title": lambda d: d["data"].__setitem__("title", "a different title"),
            "detail": lambda d: d["data"].__setitem__("detail", "a different detail"),
            "priority": lambda d: d["data"].__setitem__("priority", "P3"),
            "notes emptied": lambda d: d["data"].__setitem__("notes", []),
            "nested true to 1": lambda d: d["data"]["legacy_parent"].__setitem__(
                "reviewed", 1
            ),
        }
        relay = self.start_owner()
        for label, mutate in variants.items():
            with self.subTest(parent_value=label):
                parent = self.seed_rich_parent(f"Preserved {label}")
                before = json.loads(json.dumps(self.task_on_disk(parent)))
                sent_before = len(relay.posts)
                relay.mutate_success = mutate

                code, out, err = self.add(parent, f"append for {label}")

                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertNotEqual(err, "", label)
                self.assert_committed_then_refused(relay, parent, before, sent_before)
                self.assertEqual(len(relay.task_gets), sent_before + 1, label)
        relay.mutate_success = None

    def test_a_genuine_response_and_its_legitimate_effects_are_accepted(self) -> None:
        """Healthy control for the value comparison, on the same fixture shape."""

        parent = self.seed_rich_parent("Genuine")
        before = json.loads(json.dumps(self.task_on_disk(parent)))
        self.start_owner()

        code, out, err = self.add(parent, "legitimate append")

        self.assertEqual(code, 0, err)
        record = json.loads(out)
        self.assertEqual(tuple(record), SUBTASK_KEYS)
        stored = self.task_on_disk(parent)
        # The sanctioned effects moved and nothing else did.
        self.assertEqual(stored["revision"], before["revision"] + 1)
        self.assertEqual(len(stored["subtasks"]), len(before["subtasks"]) + 1)
        self.assertEqual(stored["notes"], before["notes"])
        self.assertEqual(stored["legacy_parent"], before["legacy_parent"])

    def test_a_new_subtask_id_must_use_the_allocator_spelling(self) -> None:
        # Verified directly against workstack.service._next_id: it emits an
        # ASCII "S-<n>" with no leading zero and never S-0.
        variants = {
            "trailing newline": "S-3\n",
            "arabic-indic digit": "S-٣",
            "fullwidth digit": "S-３",
            "leading zero": "S-03",
            "zero": "S-0",
        }
        relay = self.start_owner()
        for label, spelling in variants.items():
            with self.subTest(identifier=label):
                parent = self.seed_parent(f"Spelling {label}", subtasks=("a", "b"))
                before = json.loads(json.dumps(self.task_on_disk(parent)))
                sent_before = len(relay.posts)
                relay.mutate_success = (
                    lambda d, s=spelling: d["data"]["subtasks"][-1].__setitem__("id", s)
                )

                code, out, err = self.add(parent, "target")

                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertNotEqual(err, "", label)
                self.assertEqual(len(relay.posts), sent_before + 1, label)
                self.assertEqual(relay.responses[-1]["status"], 200, label)
                stored = self.task_on_disk(parent)
                self.assertEqual(len(stored["subtasks"]), 3, label)
                self.assertEqual(stored["revision"], before["revision"] + 1, label)
                self.assertRegex(stored["subtasks"][-1]["id"], r"\AS-[1-9][0-9]*\Z")
        relay.mutate_success = None

        # Healthy control: the genuine allocator spelling still succeeds.
        parent = self.seed_parent("Spelling healthy", subtasks=("a", "b"))
        code, out, err = self.add(parent, "genuine")
        self.assertEqual(code, 0, err)
        self.assertRegex(json.loads(out)["id"], r"\AS-[1-9][0-9]*\Z")


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()
