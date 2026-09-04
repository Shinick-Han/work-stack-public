"""Public wire contract for ``work-stack okr add-key-result`` owner forwarding.

Everything here drives the public ``cli.main`` entry point and a real ephemeral
loopback wire. No private helper is called and no helper call sequence is
asserted: the subject is the observable CLI result and the bytes that reach the
server.

Two owners appear. ``create_server`` from :mod:`workstack.server` is the real
product owner, used for the success, parity and mutation-scope cases so the
response shapes are genuine rather than hand-invented. The real-owner proxy
relays complete responses and can drop them after the owner commits. A separate
scripted owner uses partial, synthetic preflight payloads for focused protocol
cases, including stale-revision 409 and malformed bodies; these are not captures.

Owner-aware routing is expected to be RED against the pre-implementation
baseline: ``okr add-key-result`` still takes the exclusive-local Store path, so
it fails closed on the writer lease while an owner holds it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SESSION_PATH = "/api/v1/session"
STORAGE_PATH = "/api/v1/storage"
SYNC_PATH = "/api/v1/sync/status"
OBJECTIVES_PATH = "/api/v1/objectives"
KEY_RESULT_KEYS = ("id", "text", "target", "progress", "status")
LEAKY_DIAGNOSTIC_CANARY = "scripted-key-result-secret-must-not-be-printed"
LEAKY_PATH = r"C:\scripted-owner\internal\path\must-not-be-printed"


def _result_root() -> Path | None:
    """Fixture root; confined to the assigned results directory when provided."""

    configured = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
    if configured:
        root = Path(configured) / "key-result-fixtures"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None


class _IsolatedRuntimeCase(unittest.TestCase):
    """Redirect runtime and temporary storage BEFORE constructing any Store."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
        self.home = Path(self.temporary.name)
        self.root = self.home / "data"
        self.runtime = self.home / "runtime"
        self.scratch = self.home / "tmp"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._saved_environment = {
            name: os.environ.get(name)
            for name in ("WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR")
        }
        os.environ["WORK_STACK_RUNTIME"] = str(self.runtime)
        for name in ("TEMP", "TMP", "TMPDIR"):
            os.environ[name] = str(self.scratch)
        self.addCleanup(self._restore_environment)

        # Imported after the redirection so no Store can resolve a real runtime path.
        from workstack.service import WorkStack
        from workstack.store import Store

        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.workspace_uid = self.store.load("workspace.json")["id"]

    def _restore_environment(self) -> None:
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        from workstack import cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                ["--data-dir", str(self.root), "okr", "add-key-result", *arguments]
            )
        return code, out.getvalue(), err.getvalue()

    def objectives_on_disk(self) -> list[dict[str, Any]]:
        return json.loads(self.store.path("okr.json").read_text(encoding="utf-8"))[
            "objectives"
        ]

    def objective_on_disk(self, identifier: str) -> dict[str, Any]:
        matches = [o for o in self.objectives_on_disk() if o["id"] == identifier]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def seed_objective(self, text: str, *, key_results: tuple[str, ...] = ()) -> str:
        objective = self.stack.add_objective(text, "2026-Q3")
        for entry in key_results:
            self.stack.add_key_result(objective["id"], entry)
        return objective["id"]


# ---------------------------------------------------------------------------
# The real product owner holds the writer lease.
# ---------------------------------------------------------------------------


class KeyResultRealOwnerContract(_IsolatedRuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        from workstack.server import create_server

        self.objective_id = self.seed_objective("Owner objective", key_results=("Existing KR",))
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        # Release the writer lease before the temporary tree is removed; Windows
        # refuses to unlink .workstack.lock while it is held.
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        super().tearDown()

    def test_real_owner_prints_exactly_the_five_legacy_fields_in_order(self) -> None:
        code, out, err = self.run_cli(self.objective_id, "Routed key result")

        self.assertEqual(code, 0, msg=err)
        payload = json.loads(out)
        self.assertEqual(list(payload), list(KEY_RESULT_KEYS))
        for absent in ("objective", "revision", "meta", "data", "key_results"):
            self.assertNotIn(absent, payload)
        self.assertEqual(payload["text"], "Routed key result")
        self.assertEqual(payload["progress"], 0)
        self.assertEqual(payload["status"], "active")

    def test_real_owner_appends_to_the_existing_roster_of_the_parent_only(self) -> None:
        other = self.seed_objective("Untouched objective", key_results=("Other KR",))
        before_other = self.objective_on_disk(other)

        code, out, err = self.run_cli(self.objective_id, "Second KR")
        self.assertEqual(code, 0, msg=err)

        parent = self.objective_on_disk(self.objective_id)
        self.assertEqual(
            [entry["text"] for entry in parent["key_results"]],
            ["Existing KR", "Second KR"],
        )
        self.assertEqual(json.loads(out)["id"], parent["key_results"][-1]["id"])
        self.assertEqual(self.objective_on_disk(other), before_other)

    def test_key_result_ids_are_scoped_per_objective(self) -> None:
        second = self.seed_objective("Second objective")

        first_code, first_out, _ = self.run_cli(self.objective_id, "Alpha")
        second_code, second_out, _ = self.run_cli(second, "Beta")

        self.assertEqual((first_code, second_code), (0, 0))
        # The seeded objective already owns KR-1, the fresh one does not.
        self.assertEqual(json.loads(first_out)["id"], "KR-2")
        self.assertEqual(json.loads(second_out)["id"], "KR-1")

    def test_objective_id_is_case_and_space_insensitive(self) -> None:
        code, out, err = self.run_cli(
            "  {}  ".format(self.objective_id.lower()), "Normalized lookup"
        )
        self.assertEqual(code, 0, msg=err)
        parent = self.objective_on_disk(self.objective_id)
        self.assertEqual(parent["key_results"][-1]["text"], "Normalized lookup")
        self.assertEqual(json.loads(out)["text"], "Normalized lookup")

    def test_text_and_target_are_trimmed_and_target_defaults_to_empty(self) -> None:
        code, out, err = self.run_cli(self.objective_id, "  Padded text  ", "--target", "  90%  ")
        self.assertEqual(code, 0, msg=err)
        payload = json.loads(out)
        self.assertEqual(payload["text"], "Padded text")
        self.assertEqual(payload["target"], "90%")

        code, out, err = self.run_cli(self.objective_id, "No target given")
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(json.loads(out)["target"], "")

    def test_arbitrary_target_is_preserved_after_trimming(self) -> None:
        for target in ("120 signups/week", "?", "Q3 · 목표"):
            with self.subTest(target=target):
                code, out, err = self.run_cli(self.objective_id, "Target case", "--target", target)
                self.assertEqual(code, 0, msg=err)
                self.assertEqual(json.loads(out)["target"], target)

    def test_repeated_identical_invocations_create_distinct_key_results(self) -> None:
        first_code, first_out, _ = self.run_cli(self.objective_id, "Duplicate intent")
        second_code, second_out, _ = self.run_cli(self.objective_id, "Duplicate intent")

        self.assertEqual((first_code, second_code), (0, 0))
        first, second = json.loads(first_out), json.loads(second_out)
        self.assertNotEqual(first["id"], second["id"])
        parent = self.objective_on_disk(self.objective_id)
        self.assertEqual(
            [entry["text"] for entry in parent["key_results"]].count("Duplicate intent"), 2
        )

    def test_empty_text_is_refused_with_the_local_message_and_no_write(self) -> None:
        before = self.objective_on_disk(self.objective_id)
        code, out, err = self.run_cli(self.objective_id, "   ")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("text is required", err)
        self.assertEqual(self.objective_on_disk(self.objective_id), before)

    def test_unknown_objective_is_refused_without_creating_anything(self) -> None:
        before = self.objectives_on_disk()
        code, out, err = self.run_cli("O-9999", "Orphan key result")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(self.objectives_on_disk(), before)

    def test_other_okr_actions_are_not_forwarded(self) -> None:
        from workstack import cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--data-dir", str(self.root), "okr", "rollup"])
        # The owner holds the lease, so the local path fails closed rather than
        # being silently rerouted through the key-result writer.
        self.assertIn(code, (0, 2))
        self.assertNotIn("key result", err.getvalue().lower())


# ---------------------------------------------------------------------------
# Absent owner: the exclusive-local path is unchanged.
# ---------------------------------------------------------------------------


class KeyResultAbsentOwnerParity(_IsolatedRuntimeCase):
    def test_absent_owner_writes_locally_with_the_same_record_shape(self) -> None:
        objective_id = self.seed_objective("Local objective", key_results=("Existing KR",))
        self.assertFalse(self.store.server_info_path.exists())

        code, out, err = self.run_cli(objective_id, "  Local KR  ", "--target", " 42 ")

        self.assertEqual(code, 0, msg=err)
        payload = json.loads(out)
        self.assertEqual(list(payload), list(KEY_RESULT_KEYS))
        self.assertEqual(payload["text"], "Local KR")
        self.assertEqual(payload["target"], "42")
        self.assertEqual(payload["id"], "KR-2")
        parent = self.objective_on_disk(objective_id)
        self.assertEqual([entry["id"] for entry in parent["key_results"]], ["KR-1", "KR-2"])

    def test_absent_owner_empty_text_is_refused(self) -> None:
        objective_id = self.seed_objective("Local objective")
        before = self.objective_on_disk(objective_id)
        code, out, err = self.run_cli(objective_id, "  ")
        self.assertEqual(code, 2)
        self.assertIn("text is required", err)
        self.assertEqual(self.objective_on_disk(objective_id), before)


# ---------------------------------------------------------------------------
# A scripted owner supplies partial synthetic payloads and injects faults.
# ---------------------------------------------------------------------------


class _ScriptedOwner:
    """Serve partial synthetic preflight payloads and a scripted KR outcome."""

    def __init__(self, workspace_uid: str, objective: dict[str, Any]) -> None:
        self.workspace_uid = workspace_uid
        self.objective = objective
        self.csrf_token = "scripted-csrf-token"
        self.posts: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.post_script: list[Any] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                pass

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                owner.gets.append(self.path)
                if self.path == SESSION_PATH:
                    self._send(200, {"data": {"csrf_token": owner.csrf_token}})
                elif self.path == STORAGE_PATH:
                    self._send(200, {"data": {
                        "workspace_id": owner.workspace_uid, "store_schema_version": 3,
                    }})
                elif self.path == SYNC_PATH:
                    self._send(200, {"data": {"state": "in-sync"}})
                elif self.path.startswith(OBJECTIVES_PATH + "/"):
                    self._send(200, {"data": {
                        "objective": owner.objective, "tasks": [], "activity": [],
                    }})
                else:
                    self._send(404, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                owner.posts.append({
                    "path": self.path,
                    "raw": raw,
                    "body": json.loads(raw.decode("utf-8")),
                    "key": self.headers.get("Idempotency-Key"),
                    "csrf": self.headers.get("X-WorkStack-CSRF"),
                    "origin": self.headers.get("Origin"),
                })
                action = owner.post_script.pop(0) if owner.post_script else ("created", None)
                kind, detail = action
                if kind == "drop":
                    # Close without answering: the request went out and the
                    # outcome is unknown to the caller.
                    self.close_connection = True
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                    return
                if kind == "status":
                    self._send(detail, {
                        "error": "conflict",
                        "detail": LEAKY_DIAGNOSTIC_CANARY,
                        "path": LEAKY_PATH,
                    })
                    return
                if kind == "malformed":
                    self._send(201, detail)
                    return
                self._send(201, {"data": owner.updated_objective(), "meta": {"replayed": False}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def updated_objective(self) -> dict[str, Any]:
        """The whole Objective as the real server returns it after a create."""

        updated = json.loads(json.dumps(self.objective))
        existing = len(updated["key_results"])
        updated["key_results"].append({
            "id": "KR-{}".format(existing + 1),
            "text": self.posts[-1]["body"]["text"].strip(),
            "target": self.posts[-1]["body"]["target"],
            "progress": 0,
            "status": "active",
        })
        updated["revision"] = updated["revision"] + 1
        return updated

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


class KeyResultScriptedOwnerContract(_IsolatedRuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        self.objective_id = self.seed_objective("Scripted objective", key_results=("Existing KR",))
        captured = self.objective_on_disk(self.objective_id)
        self.objective_payload = {
            "id": captured["id"],
            "quarter": captured["quarter"],
            "objective": captured["objective"],
            "status": captured["status"],
            "key_results": json.loads(json.dumps(captured["key_results"])),
            "created": captured["created"],
            "updated_at": captured["updated_at"],
            "revision": captured.get("revision", 0),
        }
        self.owner = _ScriptedOwner(self.workspace_uid, self.objective_payload)
        self.addCleanup(self.owner.close)
        self.store.write_server_info("127.0.0.1", self.owner.port)
        self.sentinel = json.loads(
            self.store.path("okr.json").read_text(encoding="utf-8")
        )

    def assertNoLocalWrite(self) -> None:
        self.assertEqual(
            json.loads(self.store.path("okr.json").read_text(encoding="utf-8")),
            self.sentinel,
            "the local Store must not be written on the owner route",
        )
        self.assertTrue(
            self.store.server_info_path.is_file(),
            "owner metadata must never be cleaned up",
        )

    def test_route_body_revision_and_headers_are_exact(self) -> None:
        code, out, err = self.run_cli(self.objective_id, " Wire KR ", "--target", " 75% ")

        self.assertEqual(code, 0, msg=err)
        self.assertEqual(len(self.owner.posts), 1)
        post = self.owner.posts[0]
        self.assertEqual(
            post["path"], "{}/{}/key-results".format(OBJECTIVES_PATH, self.objective_id)
        )
        self.assertEqual(set(post["body"]), {"text", "target", "revision"})
        self.assertEqual(post["body"]["target"], "75%")
        self.assertIs(type(post["body"]["revision"]), int)
        self.assertEqual(post["body"]["revision"], self.objective_payload["revision"])
        self.assertEqual(post["csrf"], self.owner.csrf_token)
        self.assertEqual(post["origin"], "http://127.0.0.1:{}".format(self.owner.port))
        self.assertTrue(post["key"])
        self.assertEqual(json.loads(out)["text"], "Wire KR")

    def test_objective_is_fetched_from_the_same_owner_before_the_post(self) -> None:
        code, _, err = self.run_cli(self.objective_id, "Fetch first")
        self.assertEqual(code, 0, msg=err)
        detail_path = "{}/{}".format(OBJECTIVES_PATH, self.objective_id)
        self.assertIn(detail_path, self.owner.gets)
        self.assertIn(SESSION_PATH, self.owner.gets)
        self.assertIn(STORAGE_PATH, self.owner.gets)
        self.assertIn(SYNC_PATH, self.owner.gets)

    def test_stale_revision_conflict_is_not_refreshed_or_retried(self) -> None:
        self.owner.post_script = [("status", 409)]
        code, out, err = self.run_cli(self.objective_id, "Stale revision")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(self.owner.posts), 1, "a determinate 409 must not be retried")
        detail_path = "{}/{}".format(OBJECTIVES_PATH, self.objective_id)
        self.assertEqual(
            self.owner.gets.count(detail_path), 1, "no refresh fetch is permitted"
        )
        self.assertIn("409", err)
        self.assertNotIn(LEAKY_DIAGNOSTIC_CANARY, err)
        self.assertNotIn(LEAKY_PATH, err)
        self.assertNoLocalWrite()

    def test_malformed_created_response_is_refused_safely(self) -> None:
        for payload in (
            {"data": {"id": "O-1"}},
            {"data": {**{k: v for k, v in {"id": "O-1"}.items()}, "key_results": "not-a-list"}},
            {"meta": {"replayed": False}},
        ):
            with self.subTest(payload=sorted(payload)):
                self.owner.posts.clear()
                self.owner.post_script = [("malformed", payload)]
                code, out, err = self.run_cli(self.objective_id, "Malformed response")
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertNotIn(LEAKY_DIAGNOSTIC_CANARY, err)
                self.assertNoLocalWrite()

    def test_invalid_owner_metadata_refuses_before_any_request(self) -> None:
        for info in (
            "{not json",
            json.dumps({"version": True, "host": "127.0.0.1", "port": 8765}),
            json.dumps({"version": 1, "host": "example.com", "port": 8765}),
            json.dumps({"version": 1, "host": "127.0.0.1", "port": True}),
        ):
            with self.subTest(info=info[:32]):
                self.owner.posts.clear()
                self.store.server_info_path.write_text(info, encoding="utf-8")
                code, out, err = self.run_cli(self.objective_id, "Invalid owner")
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(self.owner.posts, [])
                self.assertNoLocalWrite()


# ---------------------------------------------------------------------------
# Faults in front of a REAL owner, so a lost response follows a real commit.
# ---------------------------------------------------------------------------


class _RealOwnerProxy:
    """Relay to a real ``create_server`` owner and inject faults after it answers.

    The backend is the product server, so a dropped response is a response the
    owner really produced and really committed. That is what makes "socket loss
    after commit" mean what it says, and it lets the replay be answered by the
    owner's own idempotency ledger rather than by a scripted stand-in.

    The CLI addresses this proxy, so its ``Origin`` names the proxy port; it is
    rewritten to the backend before relaying, otherwise the real owner refuses
    the POST as cross-origin.
    """

    def __init__(self, backend_port: int) -> None:
        self.backend_port = backend_port
        self.gets: list[str] = []
        self.posts: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.drop_successful_posts = 0
        self.after_objective_get = None
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                pass

            def _relay(self, method: str) -> None:
                import http.client

                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else None
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in ("host", "content-length")
                }
                if "Origin" in headers:
                    headers["Origin"] = "http://127.0.0.1:{}".format(proxy.backend_port)
                connection = http.client.HTTPConnection(
                    "127.0.0.1", proxy.backend_port, timeout=10
                )
                try:
                    connection.request(method, self.path, body=body, headers=headers)
                    response = connection.getresponse()
                    status, raw = response.status, response.read()
                finally:
                    connection.close()

                if method == "GET":
                    proxy.gets.append(self.path)
                else:
                    document = json.loads(raw.decode("utf-8")) if raw else {}
                    proxy.posts.append({
                        "path": self.path,
                        "raw": body,
                        "key": self.headers.get("Idempotency-Key"),
                        "csrf": self.headers.get("X-WorkStack-CSRF"),
                        "origin": self.headers.get("Origin"),
                    })
                    proxy.responses.append({
                        "status": status,
                        "replayed": document.get("meta", {}).get("replayed"),
                    })
                    if 200 <= status < 300 and proxy.drop_successful_posts > 0:
                        # The owner has already committed. Losing the answer is
                        # exactly the ambiguity the contract must survive.
                        proxy.drop_successful_posts -= 1
                        self.close_connection = True
                        try:
                            self.connection.close()
                        except OSError:
                            pass
                        return

                if method == "GET" and self.path.startswith(OBJECTIVES_PATH + "/"):
                    hook = proxy.after_objective_get
                    if hook is not None:
                        proxy.after_objective_get = None
                        # Run before the response is handed back, so the change
                        # is durably in place before the caller can proceed.
                        # Firing afterwards would race the caller's next read.
                        hook()

                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                self._relay("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._relay("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


class _OwnedIdleEndpoint:
    """A second endpoint this fixture owns, for redirect-refusal cases.

    Pointing the advertisement at an arithmetically chosen port would probe
    whatever happens to be listening there. This is bound by the fixture, so the
    replacement advertisement always names an endpoint the test owns.
    """

    def __init__(self) -> None:
        self.requests: list[str] = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                endpoint.requests.append(self.path)
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_GET  # noqa: N815

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


class KeyResultRealOwnerFaultContract(_IsolatedRuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        from workstack.server import create_server

        self.objective_id = self.seed_objective("Fault objective", key_results=("Existing KR",))
        self.backend = create_server(self.stack, "127.0.0.1", 0)
        self.backend_thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.backend_thread.start()
        self.proxy = _RealOwnerProxy(int(self.backend.server_address[1]))
        self.store.write_server_info("127.0.0.1", self.proxy.port)

    def tearDown(self) -> None:
        self.proxy.close()
        self.backend.shutdown()
        self.backend.server_close()
        self.backend_thread.join(timeout=10)
        super().tearDown()

    def key_results(self) -> list[dict[str, Any]]:
        return self.objective_on_disk(self.objective_id)["key_results"]

    def test_loss_after_a_real_commit_replays_and_leaves_exactly_one(self) -> None:
        self.proxy.drop_successful_posts = 1

        code, out, err = self.run_cli(self.objective_id, "Committed then lost")

        self.assertEqual(code, 0, msg=err)
        self.assertEqual(len(self.proxy.posts), 2, "exactly one replay is permitted")
        first, second = self.proxy.posts
        self.assertEqual(first["raw"], second["raw"], "identical bytes")
        self.assertEqual(first["key"], second["key"], "identical idempotency key")
        # The first POST really committed; the second is the owner's own replay.
        self.assertEqual(self.proxy.responses[0]["status"], 201)
        self.assertEqual(self.proxy.responses[0]["replayed"], False)
        self.assertEqual(self.proxy.responses[1]["status"], 200)
        self.assertEqual(self.proxy.responses[1]["replayed"], True)
        # Durably exactly one new Key Result, not two.
        self.assertEqual([entry["id"] for entry in self.key_results()], ["KR-1", "KR-2"])
        self.assertEqual(json.loads(out)["id"], "KR-2")
        self.assertEqual(json.loads(out)["text"], "Committed then lost")

    def test_two_lost_successes_are_unknown_with_no_third_attempt(self) -> None:
        self.proxy.drop_successful_posts = 2

        code, out, err = self.run_cli(self.objective_id, "Lost twice")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(self.proxy.posts), 2, "no third attempt")
        self.assertIn("key result commit is unknown", err)
        # The owner did commit once; the CLI must not claim otherwise, and it
        # must not write locally or attempt a rollback.
        self.assertEqual([entry["id"] for entry in self.key_results()], ["KR-1", "KR-2"])
        self.assertTrue(self.store.server_info_path.is_file())

    def test_fresh_identical_invocations_use_distinct_keys_and_create_distinct_records(self) -> None:
        first_code, first_out, _ = self.run_cli(self.objective_id, "Same words")
        second_code, second_out, _ = self.run_cli(self.objective_id, "Same words")

        self.assertEqual((first_code, second_code), (0, 0))
        keys = {post["key"] for post in self.proxy.posts}
        self.assertEqual(len(keys), 2, "each invocation needs a fresh key")
        self.assertNotEqual(json.loads(first_out)["id"], json.loads(second_out)["id"])
        self.assertEqual(
            [entry["text"] for entry in self.key_results()],
            ["Existing KR", "Same words", "Same words"],
        )

    def test_advertisement_replaced_after_the_objective_get_refuses_before_post(self) -> None:
        replacement = _OwnedIdleEndpoint()
        self.addCleanup(replacement.close)
        before = list(self.key_results())

        def replace_advertisement() -> None:
            self.store.write_server_info("127.0.0.1", replacement.port)

        self.proxy.after_objective_get = replace_advertisement

        code, out, err = self.run_cli(self.objective_id, "Must not post")

        self.assertIsNone(self.proxy.after_objective_get, "the hook must have fired")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(self.proxy.posts, [], "no POST may be attempted")
        self.assertEqual(
            self.proxy.gets,
            [SESSION_PATH, STORAGE_PATH, SYNC_PATH,
             "{}/{}".format(OBJECTIVES_PATH, self.objective_id)],
        )
        self.assertEqual(replacement.requests, [], "the replacement must not be contacted")
        self.assertEqual(self.key_results(), before)
        self.assertTrue(self.store.server_info_path.is_file(), "no metadata cleanup")

    def test_advertisement_deleted_after_the_objective_get_refuses_before_post(self) -> None:
        from workstack.store import DEFAULTS

        before = {name: self.store.path(name).read_bytes() for name in DEFAULTS}
        hook_fired = []

        def delete_advertisement() -> None:
            self.store.server_info_path.unlink()
            hook_fired.append(True)

        self.proxy.after_objective_get = delete_advertisement
        code, out, err = self.run_cli(self.objective_id, "Must not write locally")

        self.assertEqual(hook_fired, [True])
        self.assertIsNone(self.proxy.after_objective_get)
        self.assertEqual(code, 2, msg=err)
        self.assertIn("metadata became unavailable; refusing to write locally", err)
        self.assertEqual(out, "")
        self.assertEqual(self.proxy.posts, [])
        self.assertEqual(
            self.proxy.gets,
            [SESSION_PATH, STORAGE_PATH, SYNC_PATH,
             "{}/{}".format(OBJECTIVES_PATH, self.objective_id)],
        )
        self.assertEqual(
            {name: self.store.path(name).read_bytes() for name in DEFAULTS}, before,
            "refusal must not fall back to a local planning write",
        )
        self.assertFalse(self.store.server_info_path.exists(), "no metadata recreation")

    def test_public_subprocess_invocation_matches_the_in_process_contract(self) -> None:
        import subprocess

        repository_root = Path(__file__).resolve().parent.parent
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(repository_root)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [
                # run_work_stack.py is the product's own console entry point.
                sys.executable, "-X", "utf8", "-B", str(repository_root / "run_work_stack.py"),
                "--data-dir", str(self.root),
                "okr", "add-key-result", self.objective_id.lower(), "  Subprocess KR  ",
                "--target", "  60%  ",
            ],
            capture_output=True, text=True, timeout=120,
            cwd=str(repository_root), env=environment,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(list(payload), list(KEY_RESULT_KEYS))
        self.assertEqual(payload["text"], "Subprocess KR")
        self.assertEqual(payload["target"], "60%")
        self.assertEqual([entry["id"] for entry in self.key_results()], ["KR-1", "KR-2"])


class KeyResultBaselineValidationContract(_IsolatedRuntimeCase):
    """Contradictory owner evidence must refuse, before or after the POST."""

    def setUp(self) -> None:
        super().setUp()
        from workstack.server import create_server

        self.objective_id = self.seed_objective("Baseline objective", key_results=("Existing KR",))
        self.backend = create_server(self.stack, "127.0.0.1", 0)
        self.backend_thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.backend_thread.start()
        self.mutate_get = None
        self.mutate_post = None
        self.posts = 0
        self.gets = []
        self._start_proxy()
        self.store.write_server_info("127.0.0.1", self.proxy_port)
        self.before = list(self.objective_on_disk(self.objective_id)["key_results"])

    def tearDown(self) -> None:
        # Both servers must release the writer lease before the temporary tree
        # is removed; Windows refuses to unlink .workstack.lock while it is held.
        self._stop_proxy()
        self._stop_backend()
        super().tearDown()

    def _stop_backend(self) -> None:
        self.backend.shutdown()
        self.backend.server_close()
        self.backend_thread.join(timeout=10)

    def _start_proxy(self) -> None:
        import http.client

        case = self
        backend_port = int(self.backend.server_address[1])

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                pass

            def _relay(self, method: str) -> None:
                if method == "GET":
                    case.gets.append(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else None
                headers = {
                    key: value for key, value in self.headers.items()
                    if key.lower() not in ("host", "content-length")
                }
                if "Origin" in headers:
                    headers["Origin"] = "http://127.0.0.1:{}".format(backend_port)
                connection = http.client.HTTPConnection("127.0.0.1", backend_port, timeout=10)
                try:
                    connection.request(method, self.path, body=body, headers=headers)
                    response = connection.getresponse()
                    status, raw = response.status, response.read()
                finally:
                    connection.close()
                if method == "POST":
                    case.posts += 1
                    if 200 <= status < 300 and case.mutate_post is not None:
                        raw = json.dumps(
                            case.mutate_post(json.loads(raw.decode("utf-8")))
                        ).encode("utf-8")
                elif (
                    case.mutate_get is not None
                    and self.path.startswith(OBJECTIVES_PATH + "/")
                    and 200 <= status < 300
                ):
                    raw = json.dumps(
                        case.mutate_get(json.loads(raw.decode("utf-8")))
                    ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                self._relay("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._relay("POST")

        self._proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._proxy_thread = threading.Thread(target=self._proxy.serve_forever, daemon=True)
        self._proxy_thread.start()
        self.proxy_port = int(self._proxy.server_address[1])

    def _stop_proxy(self) -> None:
        self._proxy.shutdown()
        self._proxy.server_close()
        self._proxy_thread.join(timeout=10)

    def assertRefused(self, *, expect_posts: int) -> None:
        code, out, err = self.run_cli(self.objective_id, "Contradictory")
        self.assertEqual(code, 2, msg=out)
        self.assertEqual(out, "")
        self.assertEqual(self.posts, expect_posts)
        self.assertNotIn(LEAKY_DIAGNOSTIC_CANARY, err)
        return err

    # -- baseline, before any POST ----------------------------------------
    def test_baseline_revision_above_the_supported_maximum_refuses_before_post(self) -> None:
        maximum = 9007199254740991

        def mutate(document):
            document["data"]["objective"]["revision"] = maximum + 1
            return document

        self.mutate_get = mutate
        self.assertRefused(expect_posts=0)

    def test_baseline_revision_at_the_maximum_refuses_before_post(self) -> None:
        maximum = 9007199254740991

        def mutate(document):
            document["data"]["objective"]["revision"] = maximum
            return document

        self.mutate_get = mutate
        err = self.assertRefused(expect_posts=0)
        self.assertIn("cannot advance", err)

    def test_baseline_duplicate_identities_refuse_before_post(self) -> None:
        def mutate(document):
            roster = document["data"]["objective"]["key_results"]
            roster.append(dict(roster[0]))
            return document

        self.mutate_get = mutate
        self.assertRefused(expect_posts=0)

    def test_baseline_malformed_roster_entry_refuses_before_post(self) -> None:
        def mutate(document):
            document["data"]["objective"]["key_results"].append(None)
            return document

        self.mutate_get = mutate
        self.assertRefused(expect_posts=0)

    def test_baseline_bool_revision_refuses_before_post(self) -> None:
        def mutate(document):
            document["data"]["objective"]["revision"] = True
            return document

        self.mutate_get = mutate
        self.assertRefused(expect_posts=0)

    # -- created response, after the POST ---------------------------------
    def test_created_response_without_a_revision_refuses(self) -> None:
        self.mutate_post = lambda d: (d["data"].pop("revision", None), d)[1]
        err = self.assertRefused(expect_posts=1)
        self.assertIn("impossible objective revision", err)

    def test_created_response_with_an_unchanged_revision_refuses(self) -> None:
        def mutate(document):
            document["data"]["revision"] = 1
            return document

        self.mutate_post = mutate
        self.assertRefused(expect_posts=1)

    def test_created_response_with_a_malformed_roster_entry_refuses(self) -> None:
        def mutate(document):
            document["data"]["key_results"].append(None)
            return document

        self.mutate_post = mutate
        self.assertRefused(expect_posts=1)

    def test_created_response_dropping_a_baseline_record_refuses(self) -> None:
        def mutate(document):
            document["data"]["key_results"] = [
                entry for entry in document["data"]["key_results"]
                if entry.get("id") != "KR-1"
            ]
            return document

        self.mutate_post = mutate
        err = self.assertRefused(expect_posts=1)
        self.assertIn("dropped an existing key result", err)

    def test_an_impossible_revision_outranks_a_dropped_baseline_record(self) -> None:
        """Order, not just outcome: the revision check precedes the roster one.

        Every other case here presents a single fault, so none of them pins
        WHICH refusal wins when two are present. Extracting these checks into
        separate helpers could silently reorder them, and this is the control
        that would notice.
        """

        def mutate(document):
            document["data"]["revision"] = 99
            document["data"]["key_results"] = [
                entry for entry in document["data"]["key_results"]
                if entry.get("id") != "KR-1"
            ]
            return document

        self.mutate_post = mutate
        err = self.assertRefused(expect_posts=1)
        self.assertIn("impossible objective revision", err)
        self.assertNotIn("dropped an existing key result", err)

    def test_a_dropped_baseline_record_outranks_an_unidentifiable_new_record(self) -> None:
        """The roster check precedes the single-new-identity check."""

        def mutate(document):
            document["data"]["key_results"] = [
                entry for entry in document["data"]["key_results"]
                if entry.get("id") != "KR-1"
            ] + [
                {"id": "KR-9", "text": "extra", "target": "", "progress": 0,
                 "status": "active"}
            ]
            return document

        self.mutate_post = mutate
        err = self.assertRefused(expect_posts=1)
        self.assertIn("dropped an existing key result", err)

    def test_created_response_with_wrong_field_types_refuses(self) -> None:
        def mutate(document):
            for entry in document["data"]["key_results"]:
                if entry.get("id") == "KR-2":
                    entry["text"] = 17
                    entry["target"] = None
                    entry["progress"] = "zero"
                    entry["status"] = False
            return document

        self.mutate_post = mutate
        self.assertRefused(expect_posts=1)

    def test_created_response_with_duplicated_identities_refuses(self) -> None:
        def mutate(document):
            roster = document["data"]["key_results"]
            roster.append(dict(roster[-1]))
            return document

        self.mutate_post = mutate
        self.assertRefused(expect_posts=1)

    def assertCreatedValueRefused(self, field: str, value: object) -> None:
        """Change only a genuine successful response, never the durable record."""

        before = self.objective_on_disk(self.objective_id)["key_results"]
        baseline_ids = {entry["id"] for entry in before}
        metadata = self.store.server_info_path.read_bytes()
        previous_posts, previous_gets = self.posts, len(self.gets)
        mutations = []

        def mutate(document):
            created = [entry for entry in document["data"]["key_results"]
                       if entry["id"] not in baseline_ids]
            self.assertEqual(len(created), 1, "genuine owner must create exactly one KR")
            mutations.append(created[0][field])
            created[0][field] = value
            return document

        self.mutate_post = mutate
        code, out, err = self.run_cli(
            self.objective_id, "  Expected created text  ", "--target", "  arbitrary target  "
        )
        self.assertEqual(len(mutations), 1, "the successful-response fault must fire once")
        self.assertEqual(self.posts - previous_posts, 1, "no retry after contradictory success")
        self.assertEqual(
            self.gets[previous_gets:],
            [SESSION_PATH, STORAGE_PATH, SYNC_PATH,
             "{}/{}".format(OBJECTIVES_PATH, self.objective_id)],
            "no extra GET or revision refetch after POST",
        )
        durable = self.objective_on_disk(self.objective_id)["key_results"]
        self.assertEqual(durable[:-1], before)
        self.assertEqual(len(durable), len(before) + 1, "the owner committed exactly once")
        self.assertRegex(durable[-1]["id"], r"^KR-[1-9][0-9]*$")
        self.assertEqual(
            {key: durable[-1][key] for key in ("text", "target", "progress", "status")},
            {"text": "Expected created text", "target": "arbitrary target",
             "progress": 0, "status": "active"},
        )
        self.assertEqual(self.store.server_info_path.read_bytes(), metadata)
        self.assertEqual(code, 2, msg=out)
        self.assertEqual(out, "", "a contradictory success must not print a raw KR")
        self.assertIn("invalid key result response", err)

    def test_created_progress_must_be_strict_integer_zero(self) -> None:
        for value in (101, 1, 40, -1, False, 0.0):
            with self.subTest(progress=value):
                self.assertCreatedValueRefused("progress", value)

    def test_created_status_must_be_active(self) -> None:
        for value in ("invalid-status", "done", "dropped", " active "):
            with self.subTest(status=value):
                self.assertCreatedValueRefused("status", value)

    def test_created_identifier_must_be_canonical_generated_kr_identity(self) -> None:
        for value in ("unrelated-id", "kr-99", "KR-0", "KR-099", "KR-99\n", "KR-\u0669"):
            with self.subTest(identifier=value):
                self.assertCreatedValueRefused("id", value)

    def test_created_text_must_match_the_normalized_request(self) -> None:
        for value in ("", "  ", " Expected created text ", "Another valid text"):
            with self.subTest(text=value):
                self.assertCreatedValueRefused("text", value)

    def test_created_target_must_match_the_normalized_request(self) -> None:
        for value in (" arbitrary target ", "Another valid target"):
            with self.subTest(target=value):
                self.assertCreatedValueRefused("target", value)

    def test_healthy_response_through_the_same_proxy_still_succeeds(self) -> None:
        """The control: nothing mutated, so the whole path must work."""

        code, out, err = self.run_cli(self.objective_id, "Healthy through proxy")
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(self.posts, 1)
        payload = json.loads(out)
        self.assertEqual(list(payload), list(KEY_RESULT_KEYS))
        self.assertEqual(payload["id"], "KR-2")
        self.assertEqual(
            [entry["id"] for entry in self.objective_on_disk(self.objective_id)["key_results"]],
            ["KR-1", "KR-2"],
        )

    def test_existing_records_keep_their_own_progress_and_duplicate_text(self) -> None:
        """Valid old records must not be constrained by the new validation."""

        self.stack.set_key_result_progress(self.objective_id, "KR-1", 40)
        self.stack.add_key_result(self.objective_id, "Existing KR")

        code, out, err = self.run_cli(self.objective_id, "Third record")

        self.assertEqual(code, 0, msg=err)
        roster = self.objective_on_disk(self.objective_id)["key_results"]
        self.assertEqual([entry["id"] for entry in roster], ["KR-1", "KR-2", "KR-3"])
        self.assertEqual(roster[0]["progress"], 40)
        self.assertEqual([entry["text"] for entry in roster[:2]], ["Existing KR", "Existing KR"])
        self.assertEqual(json.loads(out)["id"], "KR-3")

    def test_existing_done_and_dropped_records_do_not_inherit_create_constraints(self) -> None:
        self.stack.add_key_result(self.objective_id, "Existing KR")
        for identifier, progress, status in (("KR-1", 40, "done"), ("KR-2", 75, "dropped")):
            revision = self.stack.objective_detail(self.objective_id)["objective"]["revision"]
            self.stack.patch_key_result_v1(
                self.objective_id, identifier,
                {"revision": revision, "progress": progress, "status": status},
            )
        before = self.objective_on_disk(self.objective_id)["key_results"]

        code, out, err = self.run_cli(self.objective_id, "  Existing KR  ", "--target", "   ")

        self.assertEqual(code, 0, msg=err)
        self.assertEqual(self.objective_on_disk(self.objective_id)["key_results"][:-1], before)
        self.assertEqual(json.loads(out), {
            "id": "KR-3", "text": "Existing KR", "target": "", "progress": 0, "status": "active",
        })


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()
