"""Public wire contract for ``work-stack worklog checkpoint-state``.

Everything here drives the public ``cli.main`` entry point against a real
ephemeral loopback owner built by ``workstack.server.create_server``, behind an
owned relay that records each request exactly as the CLI sent it. No CLI child
is spawned and no timeout machinery is introduced.
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TRANSITIONS = "/api/v1/review/checkpoints/{}/transitions"
CHECKPOINT = "CP-" + "a" * 64
AGENT_CLIENT_HEADER = "X-WorkStack-Client"
AGENT_CLIENT_VALUE = "agent-cli-v1"


def _result_root() -> Path | None:
    configured = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
    if configured:
        root = Path(configured) / "checkpoint-state-fixtures"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None


class _RecordingHTTPServer(ThreadingHTTPServer):
    def __init__(self, *arguments: Any, **keywords: Any) -> None:
        self.handler_errors: list[str] = []
        super().__init__(*arguments, **keywords)

    def handle_error(self, request: Any, client_address: Any) -> None:
        import traceback

        self.handler_errors.append(f"{client_address}: {traceback.format_exc()}")


class _IdleEndpoint:
    """A second bound endpoint recording every contact, GET and POST alike."""

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
        self.mutate_success = None
        self.mutate_get = {}
        self.before_post = None
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                return

            def _relay(self, method: str) -> None:
                import http.client

                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else None
                original = {k: v for k, v in self.headers.items()}
                relay.requests.append({
                    "method": method,
                    "route": self.path,
                    "raw": payload,
                    "headers": dict(original),
                    "key": original.get("Idempotency-Key"),
                    "client": original.get(AGENT_CLIENT_HEADER),
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
                    relay.responses.append({"status": status, "body": decoded})
                    if 200 <= status < 300 and relay.mutate_success is not None:
                        relay.mutate_success(decoded)
                        body = json.dumps(decoded).encode("utf-8")
                    if 200 <= status < 300 and relay.drop_successful_posts > 0:
                        relay.drop_successful_posts -= 1
                        # The owner committed; the answer never arrives.
                        self.close_connection = True
                        return
                if method == "GET" and isinstance(decoded, dict) and self.path in relay.mutate_get:
                    relay.mutate_get[self.path](decoded)
                    body = json.dumps(decoded).encode("utf-8")
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


class _CheckpointStateCase(unittest.TestCase):
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

    def run_cli(self, raw: bytes, *arguments: str) -> tuple[int, str, str]:
        from workstack import cli

        out, err = io.StringIO(), io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
        saved = __import__("sys").stdin
        __import__("sys").stdin = stdin
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--data-dir", str(self.root), "worklog", *arguments])
        finally:
            __import__("sys").stdin = saved
        return code, out.getvalue(), err.getvalue()

    def send(self, raw: bytes, key: str = "cli-cp-1", checkpoint: str = CHECKPOINT):
        return self.run_cli(
            raw, "checkpoint-state", checkpoint, "--stdin", "--idempotency-key", key
        )

    def body(self, **overrides: Any) -> bytes:
        document = {
            "state": "superseded",
            "revision": 0,
            "reason": {"code": "obsolete", "explanation": "a newer checkpoint"},
        }
        document.update(overrides)
        return json.dumps(document, ensure_ascii=False).encode("utf-8")


class CheckpointStateWireContract(_CheckpointStateCase):
    def test_the_wire_carries_the_frozen_route_body_key_and_attribution(self) -> None:
        relay = self.start_owner()

        code, out, err = self.send(self.body(), key="cli-cp-frozen")

        # The owner decides the outcome for an unknown checkpoint; what this
        # case pins is exactly what the CLI put on the wire.
        self.assertEqual(len(relay.posts), 1, "one POST per explicit invocation")
        sent = relay.posts[0]
        self.assertEqual(sent["route"], TRANSITIONS.format(CHECKPOINT))
        self.assertEqual(json.loads(sent["raw"].decode("utf-8")), json.loads(self.body()))
        self.assertEqual(sent["key"], "cli-cp-frozen", "the caller's own key")
        self.assertEqual(sent["client"], AGENT_CLIENT_VALUE, "agent-cli-v1 attribution")
        self.assertTrue(sent["headers"]["X-WorkStack-CSRF"])
        self.assertEqual(sent["headers"]["Origin"], f"http://127.0.0.1:{relay.port}")
        # No automatic audit or Task read accompanies the write.
        self.assertEqual(
            [r["route"] for r in relay.requests if r["method"] == "GET"],
            ["/api/v1/session", "/api/v1/storage", "/api/v1/sync/status"],
        )
        if code != 0:
            self.assertEqual(out, "", "a refusal prints nothing")
            self.assertNotEqual(err, "")

    def test_a_determinate_refusal_is_not_retried(self) -> None:
        relay = self.start_owner()

        code, out, err = self.send(self.body(), key="cli-cp-determinate")

        self.assertEqual(len(relay.posts), 1, "one attempt, no replay")
        self.assertTrue(relay.responses, "the owner really answered")
        if code == 2:
            self.assertEqual(out, "")
            self.assertNotEqual(err, "")
            self.assertNotIn(str(self.root), err, "the error stays sanitized")

    def test_an_absent_owner_refuses_without_a_local_write(self) -> None:
        code, out, err = self.send(self.body())

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertFalse(self.store.server_info_path.exists())

    def test_an_unusable_advertisement_refuses_before_any_request(self) -> None:
        relay = self.start_owner()
        path = self.store.server_info_path
        for label, payload in (
            ("malformed", b"{not json"),
            ("structurally invalid", json.dumps({"version": 1, "host": []}).encode()),
            ("oversized", json.dumps({
                "version": 1, "host": "127.0.0.1", "port": relay.port, "pad": "x" * 70000
            }).encode()),
        ):
            with self.subTest(advertisement=label):
                path.write_bytes(payload)
                code, out, err = self.send(self.body())
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertEqual(relay.requests, [], "nothing reaches the owner")
                self.assertTrue(path.exists(), "the metadata is not cleaned up")

    def test_a_directory_advertisement_refuses_and_is_not_removed(self) -> None:
        path = self.store.server_info_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()

        code, out, err = self.send(self.body())

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(path.is_dir())

    def test_malformed_request_bodies_refuse_before_any_request(self) -> None:
        relay = self.start_owner()
        cases = {
            "not json": b"{not json",
            "not an object": b'"a string"',
            "missing reason": json.dumps({"state": "superseded", "revision": 0}).encode(),
            "extra field": self.body(extra=1),
            "bad revision type": self.body(revision="0"),
            "negative revision": self.body(revision=-1),
            "reason not an object": self.body(reason="why"),
            "reason extra field": self.body(
                reason={"code": "c", "explanation": "e", "extra": 1}
            ),
            "oversized": b'{"state":"superseded","revision":0,"reason":{"code":"c",'
            + b'"explanation":"' + b"x" * (32 * 1024) + b'"}}',
        }
        for label, raw in cases.items():
            with self.subTest(body=label):
                code, out, err = self.send(raw)
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertNotEqual(err, "", label)
                self.assertEqual(relay.requests, [], "nothing reaches the owner")

    def test_the_body_is_sent_exactly_as_parsed(self) -> None:
        """No trim, reorder or NFC rewrite before the send."""

        relay = self.start_owner()
        raw = json.dumps(
            {
                "state": "superseded",
                "revision": 0,
                # Padding and a decomposed accent in the one free-text field:
                # the canonical copy normalizes it, the wire must not.
                "reason": {"code": "obsolete", "explanation": "  café  "},
            },
            ensure_ascii=False,
        ).encode("utf-8")

        self.send(raw, key="cli-cp-exact")

        self.assertEqual(len(relay.posts), 1)
        self.assertEqual(
            json.loads(relay.posts[0]["raw"].decode("utf-8")), json.loads(raw)
        )

    def test_an_identical_reinvocation_repeats_the_same_body_and_key(self) -> None:
        relay = self.start_owner()
        raw = self.body()

        self.send(raw, key="cli-cp-repeat")
        self.send(raw, key="cli-cp-repeat")

        self.assertEqual(len(relay.posts), 2, "one POST per explicit invocation")
        first, second = relay.posts
        self.assertEqual(second["raw"], first["raw"])
        self.assertEqual(second["key"], first["key"])
        self.assertEqual(second["route"], first["route"])



class CheckpointStateRealOwnerContract(_CheckpointStateCase):
    """Genuine transitions against a real owner holding a real checkpoint."""

    def seed_checkpoint(self) -> str:
        """The admitted public seam: a worklog entry produces a checkpoint.

        The fixture may read the audit; the COMMAND never does.
        """

        self._seeded = getattr(self, "_seeded", 0) + 1
        task = self.stack.add_task("Checkpoint parent %d" % self._seeded)
        self.stack.add_worklog_v1(
            {
                "date": "2026-09-03",
                "task_id": task["id"],
                "done": ["one"],
                "next": [],
                "blockers": [],
            },
            # A distinct key per seeded entry: one key may not be reused for a
            # different request.
            "cli.seed.entry.%04d" % self._seeded,
            origin="agent-cli-v1",
        )
        entries = self.stack.list_checkpoint_audit()["entries"]
        self.assertTrue(entries, "the seam really produced a checkpoint")
        # The audit entry carries the task id inside its locator.
        mine = [
            e for e in entries
            if isinstance(e.get("locator"), dict)
            and e["locator"].get("task_id") == task["id"]
        ]
        return (mine or entries)[0]["checkpoint_id"]

    def worklog_bytes(self) -> bytes:
        return self.store.path("worklog.json").read_bytes()

    def test_a_fresh_transition_then_its_exact_replay(self) -> None:
        checkpoint = self.seed_checkpoint()
        relay = self.start_owner()
        raw = self.body()
        worklog_before = self.worklog_bytes()

        code, out, err = self.send(raw, key="cli.cp.cycle.0001", checkpoint=checkpoint)

        self.assertEqual(code, 0, err)
        first = json.loads(out)
        self.assertEqual(set(first), {"data", "meta"})
        self.assertEqual(first["meta"], {"replayed": False})
        event = first["data"]
        self.assertEqual(len(event), 11, "the durable eleven-field event")
        self.assertEqual(event["checkpoint_id"], checkpoint)
        self.assertEqual(event["state"], "superseded")
        self.assertEqual(event["origin"], "agent-cli-v1")
        self.assertEqual(event["revision"], 1)
        self.assertEqual(relay.responses[0]["status"], 201)
        self.assertEqual(len(relay.posts), 1)

        # The exact same body under the exact same key is the historical
        # receipt: 200 with replayed true, and no second transition.
        code, out, err = self.send(raw, key="cli.cp.cycle.0001", checkpoint=checkpoint)

        self.assertEqual(code, 0, err)
        second = json.loads(out)
        self.assertEqual(second["meta"], {"replayed": True})
        self.assertEqual(second["data"], event, "the original event comes back")
        self.assertEqual(relay.responses[1]["status"], 200)
        self.assertEqual(len(relay.posts), 2, "one POST per explicit invocation")
        self.assertEqual(relay.posts[1]["raw"], relay.posts[0]["raw"])
        self.assertEqual(relay.posts[1]["key"], relay.posts[0]["key"])
        # The worklog document itself is not rewritten by a transition.
        self.assertNotEqual(self.worklog_bytes(), b"")

    def test_a_lost_response_then_an_explicit_identical_invocation(self) -> None:
        checkpoint = self.seed_checkpoint()
        relay = self.start_owner()
        relay.drop_successful_posts = 1
        raw = self.body()

        code, out, err = self.send(raw, key="cli.cp.lost.0001", checkpoint=checkpoint)

        self.assertEqual(code, 2, "an ambiguous outcome is not a success")
        self.assertEqual(out, "")
        self.assertIn("commit is unknown", err)
        self.assertEqual(len(relay.posts), 1, "one POST, no automatic replay")
        self.assertEqual(relay.responses[0]["status"], 201, "the owner did commit")

        # The caller decides to retry, explicitly and identically.
        code, out, err = self.send(raw, key="cli.cp.lost.0001", checkpoint=checkpoint)

        self.assertEqual(code, 0, err)
        answer = json.loads(out)
        self.assertEqual(answer["meta"], {"replayed": True})
        self.assertEqual(answer["data"]["revision"], 1, "exactly one transition")
        self.assertEqual(len(relay.posts), 2)
        self.assertEqual(relay.posts[1]["raw"], relay.posts[0]["raw"])

    def test_contradictory_successes_are_commit_unknown(self) -> None:
        variants: dict[str, Any] = {
            "integer replayed": lambda d: d["meta"].__setitem__("replayed", 0),
            "meta extra": lambda d: d["meta"].__setitem__("extra", 1),
            "empty data": lambda d: d.__setitem__("data", {}),
            "missing field": lambda d: d["data"].pop("origin"),
            "wrong checkpoint": lambda d: d["data"].__setitem__(
                "checkpoint_id", "CP-" + "b" * 64
            ),
            "wrong workspace": lambda d: d["data"].__setitem__(
                "workspace_uid", "11111111-1111-4111-8111-111111111111"
            ),
            "wrong state": lambda d: d["data"].__setitem__("state", "active"),
            "wrong revision": lambda d: d["data"].__setitem__("revision", 4),
            "wrong origin": lambda d: d["data"].__setitem__("origin", "browser"),
        }
        for index, (label, mutate) in enumerate(variants.items()):
            with self.subTest(response=label):
                checkpoint = self.seed_checkpoint()
                relay = self.start_owner()
                relay.mutate_success = mutate

                code, out, err = self.send(
                    self.body(), key="cli.cp.bad.%04d" % index, checkpoint=checkpoint
                )

                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertIn("commit is unknown", err, label)
                self.assertNotIn("refused", err, label)
                self.assertEqual(len(relay.posts), 1, label)
                self.assertEqual(relay.responses[-1]["status"], 201, label)
                self.doCleanups()
                self.setUp()

    def test_a_malformed_identifier_or_key_refuses_before_any_request(self) -> None:
        relay = self.start_owner()
        cases = {
            "short checkpoint": ("CP-abc", "cli.cp.key.0001"),
            "uppercase checkpoint": ("CP-" + "A" * 64, "cli.cp.key.0001"),
            "empty key": (CHECKPOINT, ""),
            "short key": (CHECKPOINT, "abc"),
            "bad key charset": (CHECKPOINT, "cli cp key 0001"),
        }
        for label, (checkpoint, key) in cases.items():
            with self.subTest(target=label):
                code, out, err = self.send(
                    self.body(), key=key, checkpoint=checkpoint
                )
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertNotEqual(err, "", label)
                self.assertEqual(relay.requests, [], "nothing reaches the owner")

    def test_a_reason_outside_the_admitted_domain_refuses_before_any_request(self) -> None:
        relay = self.start_owner()
        cases = {
            "unknown code": self.body(reason={"code": "replaced", "explanation": "x"}),
            "restore for superseded": self.body(
                reason={"code": "restore", "explanation": "x"}
            ),
            "unknown state": self.body(state="archived"),
        }
        for label, raw in cases.items():
            with self.subTest(body=label):
                code, out, err = self.send(raw, key="cli.cp.domain.001")
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertEqual(relay.requests, [], "nothing reaches the owner")


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()


class CheckpointStateEventDomainContract(CheckpointStateRealOwnerContract):
    """Every durable field's own domain, not merely the field set."""

    def drive_contradiction(self, relay: Any, label: str, mutate: Any, index: int) -> None:
        checkpoint = self.seed_checkpoint()
        relay.mutate_success = mutate
        posts_before = len(relay.posts)
        before = self.store.path("backlog.json").read_bytes()

        code, out, err = self.send(
            self.body(), key="cli.cp.dom.%04d" % index, checkpoint=checkpoint
        )

        self.assertEqual(code, 2, label)
        self.assertEqual(out, "", label)
        self.assertIn("commit is unknown", err, label)
        self.assertNotIn("refused", err, label)
        self.assertEqual(len(relay.posts), posts_before + 1, label)
        self.assertEqual(relay.responses[-1]["status"], 201, label)
        # The owner really committed; the refusal claims no rollback.
        self.assertEqual(self.store.path("backlog.json").read_bytes(), before, label)

    def test_each_durable_field_domain_is_validated(self) -> None:
        variants: dict[str, Any] = {
            "type contradicts state": lambda d: d["data"].__setitem__(
                "type", "worklog.restored"
            ),
            "unknown type": lambda d: d["data"].__setitem__("type", "worklog.other"),
            "numeric task id": lambda d: d["data"].__setitem__("task_id", 1),
            "malformed task id": lambda d: d["data"].__setitem__("task_id", "TASK-1"),
            "impossible date": lambda d: d["data"].__setitem__("date", "2026-02-30"),
            "malformed date": lambda d: d["data"].__setitem__("date", "03-09-2026"),
            "boolean ordinal": lambda d: d["data"].__setitem__("ordinal", True),
            "negative ordinal": lambda d: d["data"].__setitem__("ordinal", -1),
            "malformed digest": lambda d: d["data"].__setitem__(
                "entry_digest", "sha256:zz"
            ),
            "digest without prefix": lambda d: d["data"].__setitem__(
                "entry_digest", "a" * 64
            ),
            "reason code outside domain": lambda d: d["data"]["reason"].__setitem__(
                "code", "restore"
            ),
            "reason extra field": lambda d: d["data"]["reason"].__setitem__("extra", 1),
            "malformed workspace uid": lambda d: d["data"].__setitem__(
                "workspace_uid", "not-a-uuid"
            ),
        }
        relay = self.start_owner()
        for index, (label, mutate) in enumerate(variants.items()):
            with self.subTest(field=label):
                self.drive_contradiction(relay, label, mutate, index)
        relay.mutate_success = None

    def test_a_genuine_success_and_replay_still_pass(self) -> None:
        """Healthy control for the domain validation, same fixture shape."""

        checkpoint = self.seed_checkpoint()
        relay = self.start_owner()
        raw = self.body()

        code, out, err = self.send(raw, key="cli.cp.dom.ok.01", checkpoint=checkpoint)
        self.assertEqual(code, 0, err)
        first = json.loads(out)

        code, out, err = self.send(raw, key="cli.cp.dom.ok.01", checkpoint=checkpoint)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["data"], first["data"])
        self.assertEqual(len(relay.posts), 2)


class CheckpointStateRemainingVectorsContract(CheckpointStateRealOwnerContract):
    """The originally named vectors that were still outstanding."""

    def cycle(self, relay: Any, checkpoint: str, state: str, code_word: str, revision: int, key: str):
        raw = self.body(
            state=state,
            revision=revision,
            reason={"code": code_word, "explanation": "cycle"},
        )
        return raw, self.send(raw, key=key, checkpoint=checkpoint)

    def test_four_cycles_then_the_original_receipt(self) -> None:
        checkpoint = self.seed_checkpoint()
        relay = self.start_owner()
        first_raw, (code, out, err) = self.cycle(
            relay, checkpoint, "superseded", "obsolete", 0, "cli.cp.cycle.a001"
        )
        self.assertEqual(code, 0, err)
        original = json.loads(out)

        for index, (state, word, revision, key) in enumerate((
            ("active", "restore", 1, "cli.cp.cycle.a002"),
            ("superseded", "duplicate", 2, "cli.cp.cycle.a003"),
            ("active", "restore", 3, "cli.cp.cycle.a004"),
        )):
            with self.subTest(cycle=index):
                _, (code, out, err) = self.cycle(
                    relay, checkpoint, state, word, revision, key
                )
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["data"]["revision"], revision + 1)

        posts_before = len(relay.posts)
        code, out, err = self.send(
            first_raw, key="cli.cp.cycle.a001", checkpoint=checkpoint
        )

        self.assertEqual(code, 0, err)
        answer = json.loads(out)
        self.assertEqual(answer["meta"], {"replayed": True})
        self.assertEqual(answer["data"], original["data"], "the ORIGINAL event")
        self.assertEqual(len(relay.posts), posts_before + 1, "one POST")
        # The command reads only the existing preflight; no audit or Task GET.
        self.assertEqual(
            sorted({r["route"] for r in relay.requests if r["method"] == "GET"}),
            ["/api/v1/session", "/api/v1/storage", "/api/v1/sync/status"],
        )

    def test_a_stale_revision_is_determinate_and_changes_nothing(self) -> None:
        checkpoint = self.seed_checkpoint()
        relay = self.start_owner()
        raw = self.body()
        code, out, err = self.send(raw, key="cli.cp.stale.0001", checkpoint=checkpoint)
        self.assertEqual(code, 0, err)
        before = self.store.path("backlog.json").read_bytes()
        posts = len(relay.posts)

        # A fresh key at the now-stale revision 0.
        code, out, err = self.send(raw, key="cli.cp.stale.0002", checkpoint=checkpoint)

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn("commit is unknown", err, "a determinate refusal, not unknown")
        self.assertEqual(len(relay.posts), posts + 1, "one POST, no retry or refetch")
        self.assertEqual(self.store.path("backlog.json").read_bytes(), before)

    def test_a_changed_body_under_the_same_key_conflicts(self) -> None:
        checkpoint = self.seed_checkpoint()
        relay = self.start_owner()
        self.send(self.body(), key="cli.cp.same.0001", checkpoint=checkpoint)
        posts = len(relay.posts)

        code, out, err = self.send(
            self.body(reason={"code": "duplicate", "explanation": "changed"}),
            key="cli.cp.same.0001",
            checkpoint=checkpoint,
        )

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertEqual(len(relay.posts), posts + 1)

    def test_documents_are_byte_identical_across_success_and_replay(self) -> None:
        checkpoint = self.seed_checkpoint()
        self.start_owner()
        raw = self.body()
        backlog_before = self.store.path("backlog.json").read_bytes()
        worklog_before = self.store.path("worklog.json").read_bytes()

        self.send(raw, key="cli.cp.bytes.0001", checkpoint=checkpoint)
        backlog_after = self.store.path("backlog.json").read_bytes()
        worklog_after = self.store.path("worklog.json").read_bytes()
        self.assertEqual(backlog_after, backlog_before, "a transition rewrites no Task")
        self.assertEqual(worklog_after, worklog_before, "and no worklog entry")

        self.send(raw, key="cli.cp.bytes.0001", checkpoint=checkpoint)
        self.assertEqual(self.store.path("backlog.json").read_bytes(), backlog_after)
        self.assertEqual(self.store.path("worklog.json").read_bytes(), worklog_after)

    def test_a_foreign_workspace_or_out_of_sync_owner_refuses(self) -> None:
        checkpoint = self.seed_checkpoint()
        relay = self.start_owner()
        idle = self.start_idle_endpoint()
        variants = {
            "foreign workspace": (
                "/api/v1/storage",
                lambda d: d["data"].__setitem__(
                    "workspace_id", "11111111-1111-4111-8111-111111111111"
                ),
            ),
            "not in sync": (
                "/api/v1/sync/status",
                lambda d: d["data"].__setitem__("state", "external-change-detected"),
            ),
        }
        advertisement = self.store.server_info_path.read_bytes()
        for label, (route, mutate) in variants.items():
            with self.subTest(preflight=label):
                relay.mutate_get = {route: mutate}
                code, out, err = self.send(
                    self.body(), key="cli.cp.pre.00001", checkpoint=checkpoint
                )
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertEqual(relay.posts, [], "zero POST")
                self.assertEqual(idle.contacts, [], "zero wrong-endpoint contacts")
                self.assertEqual(
                    self.store.server_info_path.read_bytes(), advertisement, label
                )
        relay.mutate_get = {}
        code, out, err = self.send(
            self.body(), key="cli.cp.pre.healthy", checkpoint=checkpoint
        )
        self.assertEqual(code, 0, err)

    def test_the_binding_changing_after_preflight_refuses(self) -> None:
        checkpoint = self.seed_checkpoint()
        idle = self.start_idle_endpoint()
        relay = self.start_owner()

        # First prove the recorder observes a POST at all.
        import http.client

        probe = http.client.HTTPConnection("127.0.0.1", idle.port, timeout=10)
        probe.request("POST", "/probe", body=b"{}", headers={"Content-Length": "2"})
        probe.getresponse().read()
        probe.close()
        self.assertEqual(idle.contacts, ["POST /probe"])
        idle.contacts.clear()

        for label, mutate in (
            ("replaced", lambda: self.write_advertisement(idle.port)),
            ("removed", lambda: self.store.server_info_path.unlink()),
        ):
            with self.subTest(binding=label):
                relay.mutate_get = {
                    "/api/v1/sync/status": lambda _d, m=mutate: m()
                }
                code, out, err = self.send(
                    self.body(), key="cli.cp.bind.0001", checkpoint=checkpoint
                )
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertEqual(relay.posts, [], "zero mutations")
                self.assertEqual(idle.contacts, [], "no redirected request")
                self.write_advertisement(relay.port)
        relay.mutate_get = {}


class _AdvertisementReadFault:
    """An authorized FIXTURE fault at the advertisement binary-read boundary.

    It shadows the name ``open`` inside workstack.cli_writer only, raises
    PermissionError(EACCES) for exactly that one path opened for binary
    reading, and delegates every other open to the genuine builtin. No product
    file is edited and no OS permission is changed, so nothing here claims
    anything about real filesystem ACL behaviour.
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


class CheckpointStateFinalProofContract(_CheckpointStateCase):
    """The two remaining named cells, as this route's own cases."""

    seed_checkpoint = CheckpointStateRealOwnerContract.seed_checkpoint

    def documents(self) -> dict[str, bytes]:
        return {
            name: self.store.path(name).read_bytes()
            for name in ("backlog.json", "worklog.json", "activity.json")
        }

    def transitions_recorded(self, checkpoint: str) -> list[Any]:
        entries = self.stack.list_checkpoint_audit()["entries"]
        for entry in entries:
            if entry["checkpoint_id"] == checkpoint:
                return list(entry.get("transitions", []))
        self.fail("the checkpoint disappeared from the audit")

    def test_a_checkpoint_whose_task_is_dropped_still_transitions(self) -> None:
        from workstack.storage.document_repository import WorkspaceDocument

        checkpoint = self.seed_checkpoint()
        # Remove the current Task through the supported document repository,
        # leaving the physical worklog row and the recorded activity fact
        # exactly as they are. The locator keeps binding to the historical Task
        # identity; no replacement Task is synthesized.
        tasks = self.stack.documents.load(WorkspaceDocument.TASKS)
        self.assertTrue(tasks["tasks"], "the fixture really had a Task")
        task_id = tasks["tasks"][0]["id"]
        worklog_before = self.store.path("worklog.json").read_bytes()
        # The supported removal is the dropped state: the repository declares
        # hard_delete_task unsupported, and deleting the row directly leaves a
        # planning status fact pointing at a task that no longer exists, which
        # validate_and_project rejects. So the Task is retired the way the
        # product actually retires one, and the physical worklog row and the
        # recorded checkpoint fact are left exactly as they were.
        self.stack.patch_task(
            task_id,
            {"status": "dropped", "revision": self.stack.get_task(task_id)["revision"]},
        )
        self.assertEqual(self.stack.get_task(task_id)["status"], "dropped")
        self.assertEqual(self.store.path("worklog.json").read_bytes(), worklog_before)
        self.assertEqual(len(self.transitions_recorded(checkpoint)), 0)

        relay = self.start_owner()
        raw = self.body()
        backlog_before = self.store.path("backlog.json").read_bytes()

        code, out, err = self.send(raw, key="cli.cp.notask.001", checkpoint=checkpoint)

        self.assertEqual(code, 0, err)
        answer = json.loads(out)
        self.assertEqual(answer["meta"], {"replayed": False})
        self.assertEqual(answer["data"]["checkpoint_id"], checkpoint)
        self.assertEqual(answer["data"]["revision"], 1)
        self.assertEqual(relay.responses[-1]["status"], 201)
        self.assertEqual(len(relay.posts), 1, "one POST")
        self.assertEqual(
            sorted({r["route"] for r in relay.requests if r["method"] == "GET"}),
            ["/api/v1/session", "/api/v1/storage", "/api/v1/sync/status"],
            "no Task or audit read by the command",
        )
        self.assertEqual(len(self.transitions_recorded(checkpoint)), 1)
        # Neither the planning document nor the worklog row is rewritten.
        self.assertEqual(self.store.path("backlog.json").read_bytes(), backlog_before)
        self.assertEqual(self.store.path("worklog.json").read_bytes(), worklog_before)

        # The explicit identical invocation returns the original receipt.
        code, out, err = self.send(raw, key="cli.cp.notask.001", checkpoint=checkpoint)

        self.assertEqual(code, 0, err)
        replay = json.loads(out)
        self.assertEqual(replay["meta"], {"replayed": True})
        self.assertEqual(replay["data"], answer["data"], "the original event")
        self.assertEqual(len(relay.posts), 2)
        self.assertEqual(len(self.transitions_recorded(checkpoint)), 1, "no extra")
        self.assertEqual(self.store.path("backlog.json").read_bytes(), backlog_before)
        self.assertEqual(self.store.path("worklog.json").read_bytes(), worklog_before)


    def clear_tasks_and_planning_ledger(self) -> dict[str, Any]:
        """Remove the current Task AND its planning ledger, atomically.

        Dropped is not absent. Clearing only TASKS would leave a planning
        status fact pointing at a task that no longer exists, which
        validate_and_project rejects - that is a dangling ledger, not an absent
        Task. Both released documents are therefore emptied together inside one
        real Store.transaction and saved in a single save_many, which is an
        internally consistent fixture and needs no hard-delete API. The physical
        checkpoint facts, the idempotency entries and the whole worklog are left
        exactly as they are.
        """

        from workstack.storage.document_repository import WorkspaceDocument

        with self.store.transaction():
            tasks = self.stack.documents.load(WorkspaceDocument.TASKS)
            activity = self.stack.documents.load(WorkspaceDocument.ACTIVITY)
            self.assertEqual(len(tasks["tasks"]), 1, "the fixture owns one Task")
            self.assertEqual(
                len(activity["planning_status"]), 1, "and one planning ledger entry"
            )
            preserved = {
                "activity": json.loads(json.dumps(activity["activity"])),
                "idempotency": json.loads(json.dumps(activity["idempotency"])),
            }
            tasks["tasks"] = []
            activity["planning_status"] = []
            self.stack.documents.save_many({
                WorkspaceDocument.TASKS: tasks,
                WorkspaceDocument.ACTIVITY: activity,
            })
        return preserved

    def activity_now(self) -> dict[str, Any]:
        from workstack.storage.document_repository import WorkspaceDocument

        document = self.stack.documents.load(WorkspaceDocument.ACTIVITY)
        return {
            "activity": document["activity"],
            "idempotency": document["idempotency"],
        }

    def test_a_checkpoint_with_an_absent_current_task_still_transitions(self) -> None:
        from workstack.storage.document_repository import WorkspaceDocument

        checkpoint = self.seed_checkpoint()
        worklog_before = self.store.path("worklog.json").read_bytes()
        preserved = self.clear_tasks_and_planning_ledger()

        # The absence is real and the physical checkpoint evidence is intact,
        # asserted BEFORE the command runs.
        self.assertEqual(
            self.stack.documents.load(WorkspaceDocument.TASKS)["tasks"], []
        )
        self.assertEqual(self.activity_now(), preserved, "checkpoint facts untouched")
        self.assertEqual(self.store.path("worklog.json").read_bytes(), worklog_before)
        self.assertEqual(len(self.transitions_recorded(checkpoint)), 0)

        relay = self.start_owner()
        raw = self.body()
        backlog_before = self.store.path("backlog.json").read_bytes()

        code, out, err = self.send(raw, key="cli.cp.absent.001", checkpoint=checkpoint)

        self.assertEqual(code, 0, err)
        answer = json.loads(out)
        self.assertEqual(answer["meta"], {"replayed": False})
        self.assertEqual(answer["data"]["checkpoint_id"], checkpoint)
        self.assertEqual(answer["data"]["revision"], 1)
        self.assertEqual(relay.responses[-1]["status"], 201)
        self.assertEqual(len(relay.posts), 1, "exactly one POST")
        self.assertEqual(
            sorted({r["route"] for r in relay.requests if r["method"] == "GET"}),
            ["/api/v1/session", "/api/v1/storage", "/api/v1/sync/status"],
            "no Task or audit read by the command",
        )
        self.assertEqual(len(self.transitions_recorded(checkpoint)), 1)
        self.assertEqual(self.store.path("backlog.json").read_bytes(), backlog_before)
        self.assertEqual(self.store.path("worklog.json").read_bytes(), worklog_before)
        self.assertEqual(
            self.stack.documents.load(WorkspaceDocument.TASKS)["tasks"],
            [],
            "the absence persists",
        )

        # The identical argv, body and key return the ORIGINAL receipt.
        code, out, err = self.send(raw, key="cli.cp.absent.001", checkpoint=checkpoint)

        self.assertEqual(code, 0, err)
        replay = json.loads(out)
        self.assertEqual(replay["meta"], {"replayed": True})
        self.assertEqual(replay["data"], answer["data"], "the original event")
        self.assertEqual(len(relay.posts), 2)
        self.assertEqual(len(self.transitions_recorded(checkpoint)), 1, "no extra")
        self.assertEqual(self.store.path("backlog.json").read_bytes(), backlog_before)
        self.assertEqual(self.store.path("worklog.json").read_bytes(), worklog_before)
        self.assertEqual(
            self.stack.documents.load(WorkspaceDocument.TASKS)["tasks"], []
        )

    def test_an_unreadable_advertisement_refuses_then_the_same_argv_succeeds(self) -> None:
        from workstack import cli_writer

        checkpoint = self.seed_checkpoint()
        idle = self.start_idle_endpoint()
        relay = self.start_owner()

        # Prove the second endpoint's recorder observes a POST before relying
        # on its silence. The endpoint is inert and owns no workspace.
        import http.client

        probe = http.client.HTTPConnection("127.0.0.1", idle.port, timeout=10)
        probe.request("POST", "/probe", body=b"{}", headers={"Content-Length": "2"})
        probe.getresponse().read()
        probe.close()
        self.assertEqual(idle.contacts, ["POST /probe"])
        idle.contacts.clear()

        advertisement = self.store.server_info_path
        advertisement_before = advertisement.read_bytes()
        documents_before = self.documents()
        raw = self.body()

        fault = _AdvertisementReadFault(advertisement)

        def remove_fault() -> None:
            if hasattr(cli_writer, "open"):
                delattr(cli_writer, "open")

        cli_writer.open = fault  # type: ignore[attr-defined]
        self.addCleanup(remove_fault)

        code, out, err = self.send(raw, key="cli.cp.eacces.01", checkpoint=checkpoint)

        self.assertEqual(fault.matches, 1, "exactly one matching injection")
        self.assertGreater(fault.delegated, 0, "every other open delegated")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("unreadable", err)
        self.assertNotIn(str(advertisement), err, "no path in the diagnostic")
        self.assertNotIn(str(self.root), err)
        self.assertEqual(relay.requests, [], "zero genuine owner contacts")
        self.assertEqual(relay.posts, [], "zero POST")
        self.assertEqual(idle.contacts, [], "zero second-endpoint contacts")
        self.assertEqual(advertisement.read_bytes(), advertisement_before)
        self.assertEqual(self.documents(), documents_before)

        # Identical argv, body and key against the SAME owner once the
        # injection is gone: the refusal was the unreadable read, nothing else.
        remove_fault()
        self.assertFalse(hasattr(cli_writer, "open"))

        code, out, err = self.send(raw, key="cli.cp.eacces.01", checkpoint=checkpoint)

        self.assertEqual(code, 0, err)
        answer = json.loads(out)
        self.assertEqual(answer["meta"], {"replayed": False})
        self.assertEqual(relay.responses[-1]["status"], 201)
        self.assertEqual(len(relay.posts), 1, "one POST, no retry or refetch")
        self.assertEqual(
            sorted({r["route"] for r in relay.requests if r["method"] == "GET"}),
            ["/api/v1/session", "/api/v1/storage", "/api/v1/sync/status"],
        )
        self.assertEqual(idle.contacts, [])
