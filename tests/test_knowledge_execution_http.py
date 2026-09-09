"""The owner execution route, through a real local HTTP server and a real child.

Every server here binds ``127.0.0.1:0`` over a store this test created in its
own temporary directory, and every driver is a synthetic local Python child
this test wrote. Nothing opens a real home directory, a live SSOT, a provider,
a credential or any network beyond that loopback socket, and no dependency is
installed. The issuing and execution clock is injected, so expiry is a fact of
the test rather than of the day it runs on.

The child is driven entirely through the operator-configured environment: it is
told a state directory, and reads its mode, its answer template and its release
barrier from files there. That is what lets one server, holding one attempt
guard, act out a whole sequence -- a blocked child, a policy change while it is
blocked, a second concurrent request -- without ever being restarted, and it is
also the proof that the environment a child sees is the one the operator stated
and nothing else.

The classes are the boundary's obligations in order: who may reach it and what
a refusal costs, one whole execution and the manual import after it, whose
guard this is, what may run while the child does, and how untrusted the child's
output stays.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from workstack import knowledge_execution_runtime
from workstack.knowledge_execution_http import EXECUTE_PATH
from workstack.knowledge_execution_runtime import KnowledgeDriverBinding
from workstack.knowledge_ledger_document import KNOWLEDGE_DOCUMENT_NAME
from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import Store

UPSTREAM_UID = "66666666-6666-4666-8666-666666666666"
OTHER_UPSTREAM_UID = "77777777-7777-4777-8777-777777777777"
INTENT_ID = "a1b2c3d4-1111-4111-8111-aaaaaaaaaaaa"
OTHER_INTENT_ID = "b2c3d4e5-2222-4222-8222-bbbbbbbbbbbb"
ITEM_ONE = "c3d4e5f6-3333-4333-8333-cccccccccccc"
FOREIGN_REQUEST_ID = "d4e5f607-4444-4444-8444-dddddddddddd"

# Strings that exist nowhere else. Each one is a probe for a different leak.
QUERY_CANARY = "canary 51ac7e query probe phrase"
SUMMARY_CANARY = "canary 51ac7e answer summary"
EVIDENCE_TITLE_CANARY = "canary 51ac7e evidence title"
STDERR_CANARY = "canary 51ac7e child stderr text"
AMBIENT_CANARY = "canary 51ac7e ambient owner environment"
OPERATOR_CANARY = "canary 51ac7e operator configured value"

NOW = "2026-09-08T09:00:00Z"
AFTER_EXPIRY = "2026-09-08T09:05:01Z"

CONNECTIONS = "/api/v1/knowledge/connections"
REQUESTS = "/api/v1/knowledge/requests"
IMPORT = "/api/v1/knowledge/captures/import"

# The synthetic driver. It reads one request from stdin, records what it was
# given, optionally waits on a file barrier, and answers from a template the
# test wrote. It reaches no network and knows nothing about the store.
DRIVER_SOURCE = '''
import json
import os
import sys
import time

STATE = os.environ["WS_DRIVER_STATE"]


def path(name):
    return os.path.join(STATE, name)


def read(name, default=""):
    try:
        with open(path(name), "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return default


def main():
    raw = sys.stdin.buffer.read()
    with open(path("calls"), "a", encoding="utf-8") as handle:
        handle.write("call\\n")
    with open(path("payload.json"), "w", encoding="utf-8") as handle:
        handle.write(raw.decode("utf-8"))
    with open(path("environment.json"), "w", encoding="utf-8") as handle:
        json.dump(dict(os.environ), handle, sort_keys=True)
    with open(path("argv.json"), "w", encoding="utf-8") as handle:
        json.dump(list(sys.argv), handle)
    if os.path.exists(path("barrier")):
        with open(path("started"), "w", encoding="utf-8") as handle:
            handle.write("started")
        deadline = time.monotonic() + 30.0
        while not os.path.exists(path("release")) and time.monotonic() < deadline:
            time.sleep(0.01)
    mode = read("mode", "answer").strip() or "answer"
    if mode == "sleep":
        time.sleep(30.0)
        return 0
    if mode == "exit":
        return 3
    if mode == "garbage":
        sys.stdout.buffer.write(b"{not json at all")
        return 0
    envelope = json.loads(read("envelope.json"))
    if mode != "wrong_request":
        envelope["request_id"] = json.loads(raw)["request"]["request_id"]
        for item in envelope["items"]:
            item["retrieval"]["request_id"] = envelope["request_id"]
    if mode == "stderr":
        sys.stderr.write(os.environ["WS_DRIVER_STDERR"])
        sys.stderr.flush()
    if mode == "injection":
        envelope["items"][0]["normalized"]["raw_answer"] = os.environ["WS_DRIVER_STDERR"]
    sys.stdout.buffer.write(json.dumps(envelope).encode("utf-8"))
    return 0


sys.exit(main())
'''


class ExecutionHttpCase(unittest.TestCase):
    """One loopback server, one synthetic store, one synthetic pinned driver."""

    alias = "team-nas"
    pinned_upstream = UPSTREAM_UID

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        # Registered first, so every server a test opens -- and this case's own
        # server -- is closed before the directory holding its lease is removed.
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.stop)
        self.root = Path(self.temporary.name)
        self.state = self.root / "driver-state"
        self.state.mkdir()
        self.driver = self.root / "driver.py"
        self.driver.write_text(DRIVER_SOURCE, encoding="utf-8")
        self.now = NOW
        # An ambient value the operator did NOT configure. A child that can see
        # it is inheriting this owner's environment.
        self.addCleanup(os.environ.pop, "WS_AMBIENT_CANARY", None)
        os.environ["WS_AMBIENT_CANARY"] = AMBIENT_CANARY
        self.set_mode("answer")
        self.start()

    # -- the driver ------------------------------------------------------

    def drivers(self) -> dict[str, KnowledgeDriverBinding]:
        environment = {
            "WS_DRIVER_STATE": str(self.state),
            "WS_DRIVER_STDERR": STDERR_CANARY,
            "WS_DRIVER_OPERATOR": OPERATOR_CANARY,
        }
        # Windows launches a Python child through the system root; the operator
        # states it explicitly, which is the whole point -- nothing is
        # inherited that the operator did not name.
        if "SystemRoot" in os.environ:
            environment["SystemRoot"] = os.environ["SystemRoot"]
        return {
            self.alias: KnowledgeDriverBinding(
                upstream_workspace_uid=self.pinned_upstream,
                command=(sys.executable, str(self.driver)),
                environment=environment,
            )
        }

    def set_mode(self, mode: str) -> None:
        (self.state / "mode").write_text(mode, encoding="utf-8")

    def arm_barrier(self) -> None:
        (self.state / "barrier").write_text("armed", encoding="utf-8")

    def await_child(self) -> None:
        deadline = time.monotonic() + 20.0
        while not (self.state / "started").exists():
            self.assertLess(time.monotonic(), deadline, "the child never started")
            time.sleep(0.01)

    def release_child(self) -> None:
        (self.state / "release").write_text("go", encoding="utf-8")

    def child_calls(self) -> int:
        path = self.state / "calls"
        if not path.exists():
            return 0
        return len([line for line in path.read_text(encoding="utf-8").splitlines() if line])

    def child_payload(self) -> dict[str, Any]:
        return json.loads((self.state / "payload.json").read_text(encoding="utf-8"))

    def child_environment(self) -> dict[str, str]:
        return json.loads((self.state / "environment.json").read_text(encoding="utf-8"))

    # -- the server ------------------------------------------------------

    def start(self, *, drivers: Any = None) -> None:
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.server = create_server(
            self.stack,
            "127.0.0.1",
            0,
            knowledge_drivers=self.drivers() if drivers is None else drivers,
        )
        self.server.knowledge_clock = lambda: self.now
        self.port = self.server.actual_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.running = True
        self.workspace_uid = json.loads(
            (self.root / "workspace.json").read_text(encoding="utf-8")
        )["id"]

    def stop(self) -> None:
        if not getattr(self, "running", False):
            return
        self.running = False
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)

    def restart(self, *, drivers: Any = None) -> None:
        """A second owner incarnation: the document on disk is the only state."""

        self.stop()
        self.start(drivers=drivers)

    # -- the wire --------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        try:
            return status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return status, raw

    def owner_headers(self) -> dict[str, str]:
        status, payload = self.call("GET", "/api/v1/session")
        self.assertEqual(status, 200)
        return {
            "Origin": "http://127.0.0.1:{}".format(self.port),
            "X-WorkStack-CSRF": payload["data"]["csrf_token"],
            "Content-Type": "application/json",
        }

    def post(self, path: str, body: Any, headers: dict[str, str] | None = None):
        return self.call(
            "POST",
            path,
            json.dumps(body, separators=(",", ":")).encode("utf-8"),
            self.owner_headers() if headers is None else headers,
        )

    # -- fixtures --------------------------------------------------------

    def document(self, name: str) -> Any:
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def policy_body(self, *, alias: str = "team-nas", upstream: str = UPSTREAM_UID) -> dict[str, Any]:
        return {
            "expected_policy_revision": self.document(KNOWLEDGE_DOCUMENT_NAME)[
                "policy_revision"
            ],
            "connections": [
                {
                    "alias": alias,
                    "upstream_workspace_uid": upstream,
                    "corpus_refs": ["nas-team-share"],
                }
            ],
        }

    def with_policy(self, **overrides: Any) -> None:
        status, payload = self.post(CONNECTIONS, self.policy_body(**overrides))
        self.assertEqual(status, 200, payload)

    def issue_body(self, **overrides: Any) -> dict[str, Any]:
        body = {
            "intent_id": INTENT_ID,
            "connection_alias": "team-nas",
            "binding": {"workspace_uid": self.workspace_uid},
            "query": QUERY_CANARY,
            "corpus_refs": ["nas-team-share"],
            "purpose": "find_context",
            "result_limit": 3,
        }
        body.update(overrides)
        return body

    def issue(self, **overrides: Any) -> dict[str, Any]:
        status, payload = self.post(REQUESTS, self.issue_body(**overrides))
        self.assertEqual(status, 200, payload)
        return payload["data"]

    def ready(self, **overrides: Any) -> dict[str, Any]:
        """A configured policy and one freshly issued request, ready to run."""

        self.with_policy()
        return self.issue(**overrides)

    def answer_item(self, request_id: str) -> dict[str, Any]:
        return {
            "item_id": ITEM_ONE,
            "title": "Rollback verification owner",
            "normalized": {
                "summary": SUMMARY_CANARY,
                "context": "Answered by the pinned driver for review.",
                "action_items": [{"title": "Confirm the rollback owner"}],
                "tags": ["rollback"],
            },
            "retrieval": {
                "schema": "workstack.capture-retrieval.v1.1",
                "capture_schema_version": "1.1",
                "request_id": request_id,
                "query_id": "engine-q-0001",
                "answer_scope": "single_source",
                "confidence": {"level": "medium", "score": 0.62},
                "evidence": [
                    {
                        "source_type": "nas.file",
                        "title": EVIDENCE_TITLE_CANARY,
                        "document_ref": "nas-doc-0001abcd",
                        "chunk_ref": "chunk-0001abcd",
                        "source_version": "nas-version-11",
                        "indexed_digest": "sha256:" + "a" * 64,
                        "web_url": None,
                    }
                ],
                "truncated": False,
            },
        }

    def arm_answer(self, request_id: str = FOREIGN_REQUEST_ID) -> None:
        """The template the child answers with; its identity is overwritten."""

        (self.state / "envelope.json").write_text(
            json.dumps(
                {
                    "schema": "workstack.knowledge-import.v1",
                    "request_id": request_id,
                    "items": [self.answer_item(request_id)],
                }
            ),
            encoding="utf-8",
        )

    def execute(self, document: Any, headers: dict[str, str] | None = None):
        return self.post(EXECUTE_PATH, document, headers)

    # -- assertions ------------------------------------------------------

    def assertRefused(self, payload: Any, code: str) -> None:
        self.assertEqual(payload["error"]["code"], code, payload)
        self.assertEqual(payload["error"]["details"].keys() - {"field"}, set())
        self.assertNoCanary(payload)

    def assertNoCanary(self, payload: Any) -> None:
        rendered = json.dumps(payload)
        for canary in (
            QUERY_CANARY,
            STDERR_CANARY,
            AMBIENT_CANARY,
            OPERATOR_CANARY,
            str(self.driver),
            sys.executable,
        ):
            self.assertNotIn(canary, rendered)

    def assertNoAttemptSpent(self, document: Any) -> None:
        """The attempt is still there: a valid execution now still succeeds."""

        self.arm_answer()
        self.set_mode("answer")
        status, payload = self.execute(document)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"], {"outcome": "proposal_ready"})


class ExecutionAdmissionTest(ExecutionHttpCase):
    """Who reaches the route, and what a refusal costs."""

    def test_a_request_without_csrf_reaches_no_child_and_spends_nothing(self) -> None:
        document = self.ready()
        headers = self.owner_headers()
        for name in ("X-WorkStack-CSRF", "Origin"):
            with self.subTest(missing=name):
                without = {key: value for key, value in headers.items() if key != name}
                status, payload = self.execute(document, without)
                self.assertEqual(status, 403, payload)
                self.assertEqual(self.child_calls(), 0)
        self.assertNoAttemptSpent(document)

    def test_a_bearer_token_alone_reaches_no_child_and_spends_nothing(self) -> None:
        """Capture ingestion authority is not owner execution authority."""

        document = self.ready()
        for token in ("not-a-real-token", self.server.capture_token):
            with self.subTest(real=token != "not-a-real-token"):
                status, payload = self.execute(
                    document,
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer {}".format(token),
                    },
                )
                self.assertIn(status, (401, 403), payload)
                self.assertEqual(self.child_calls(), 0)
        self.assertNoAttemptSpent(document)

    def test_an_unconfigured_driver_is_unavailable_and_spends_nothing(self) -> None:
        """No driver is an explicit state, and it costs the request nothing."""

        self.stop()
        self.start(drivers={})
        document = self.ready()
        status, payload = self.execute(document)
        self.assertEqual(status, 503, payload)
        self.assertRefused(payload, "knowledge_driver_unavailable")
        self.assertEqual(self.child_calls(), 0)

        # The same request, on a server that does have the driver, still runs:
        # nothing above spent the attempt. The guard is per incarnation, so the
        # request is re-issued (a replay would not rearm) on the new server.
        self.restart()
        document = self.ready(intent_id=OTHER_INTENT_ID)
        self.assertNoAttemptSpent(document)

    def test_a_forged_request_reaches_no_child_and_spends_nothing(self) -> None:
        """A document the ledger did not authorise is refused, not executed."""

        document = self.ready()
        altered = dict(document, query="a different question entirely")
        unknown = dict(document, request_id=FOREIGN_REQUEST_ID)
        for name, forged, code in (
            ("altered query", altered, "request_digest_mismatch"),
            ("unknown identity", unknown, "unknown_request"),
        ):
            with self.subTest(case=name):
                status, payload = self.execute(forged)
                self.assertEqual(status, 409, payload)
                self.assertRefused(payload, code)
                self.assertEqual(self.child_calls(), 0)
        self.assertNoAttemptSpent(document)

    def test_the_route_caches_no_receipt_and_answers_only_its_own_spelling(self) -> None:
        document = self.ready()
        headers = dict(self.owner_headers(), **{"Idempotency-Key": "a" * 16})
        status, payload = self.execute(document, headers)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "unsupported_idempotency_key")

        status, payload = self.post(EXECUTE_PATH + ";x", document)
        self.assertEqual(status, 404, payload)
        self.assertEqual(self.child_calls(), 0)
        self.assertNotIn("knowledge", json.dumps(self.document("activity.json")))
        self.assertNoAttemptSpent(document)

    def test_a_query_or_a_fragment_is_not_this_route(self) -> None:
        """An authorized owner asking for a parameter gets the unknown route.

        The v1 POST table routes on the *stripped* target, so each of these
        reaches this handler as the canonical path with its query or fragment
        discarded. Only the raw-target comparison refuses them, and it refuses
        them before any Store transaction, guard consume or child: the request
        stays pending, its one attempt stays unspent, and the driver is never
        started.
        """

        document = self.ready()
        for suffix in ("?unexpected=1", "?", "?#fragment", "#fragment"):
            with self.subTest(target=EXECUTE_PATH + suffix):
                status, payload = self.post(EXECUTE_PATH + suffix, document)
                self.assertEqual(status, 404, payload)
                self.assertEqual(payload["error"]["code"], "not_found", payload)
                self.assertNoCanary(payload)
                self.assertEqual(self.child_calls(), 0)
                self.assertEqual(
                    self.document(KNOWLEDGE_DOCUMENT_NAME)["requests"][0]["state"],
                    "pending",
                )
        # Nothing above was spent: the exact target still executes, once.
        self.assertNoAttemptSpent(document)
        self.assertEqual(self.child_calls(), 1)

    def test_a_binding_mismatch_spawns_nothing(self) -> None:
        """The operator's pin and the owner's roster must name one upstream."""

        self.pinned_upstream = OTHER_UPSTREAM_UID
        self.restart()
        document = self.ready()
        status, payload = self.execute(document)
        self.assertEqual(status, 409, payload)
        self.assertRefused(payload, "knowledge_driver_binding_mismatch")
        self.assertEqual(self.child_calls(), 0)


class ExecutionFlowTest(ExecutionHttpCase):
    """One execution, end to end, and what the child was and was not given."""

    def test_issue_execute_and_manually_import_one_proposal(self) -> None:
        document = self.ready()
        self.arm_answer()

        status, payload = self.execute(document)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"], {"outcome": "proposal_ready"})
        self.assertEqual(self.child_calls(), 1)

        # The projection, and nothing beside it: no raw stdout, no stderr, no
        # command, no environment, no token and no driver path.
        proposal = payload["data"]
        self.assertEqual(proposal["schema"], "workstack.knowledge-import.v1")
        self.assertEqual(proposal["request_id"], document["request_id"])
        self.assertEqual(len(proposal["items"]), 1)
        self.assertEqual(set(payload), {"data", "meta"})
        self.assertEqual(set(proposal), {"schema", "request_id", "items"})
        self.assertEqual(
            set(proposal["items"][0]), {"item_id", "title", "normalized", "retrieval"}
        )
        rendered = json.dumps(payload)
        for absent in (STDERR_CANARY, AMBIENT_CANARY, OPERATOR_CANARY, sys.executable):
            self.assertNotIn(absent, rendered)

        # The child got the request on stdin, and only there.
        stdin = self.child_payload()
        self.assertEqual(stdin["schema"], "workstack.knowledge-execute.v1")
        self.assertEqual(stdin["request"], document)
        self.assertEqual(
            stdin["connection"],
            {
                "alias": "team-nas",
                "upstream_workspace_uid": UPSTREAM_UID,
                "policy_revision": self.document(KNOWLEDGE_DOCUMENT_NAME)[
                    "policy_revision"
                ],
            },
        )
        self.assertEqual(set(stdin), {"schema", "request", "connection"})
        argv = json.loads((self.state / "argv.json").read_text(encoding="utf-8"))
        self.assertNotIn(QUERY_CANARY, json.dumps(argv))

        # Its environment is exactly the operator's, with nothing inherited and
        # no owner CSRF or capture token anywhere in it.
        environment = self.child_environment()
        self.assertEqual(environment["WS_DRIVER_OPERATOR"], OPERATOR_CANARY)
        self.assertNotIn("WS_AMBIENT_CANARY", environment)
        rendered_env = json.dumps(environment)
        for secret in (
            AMBIENT_CANARY,
            QUERY_CANARY,
            self.server.csrf_token,
            self.server.capture_token,
        ):
            self.assertNotIn(secret, rendered_env)

        # Nothing was saved. The proposal is a proposal.
        self.assertEqual(self.document("captures.json")["captures"], [])
        self.assertEqual(self.document("backlog.json")["tasks"], [])
        self.assertEqual(
            self.document(KNOWLEDGE_DOCUMENT_NAME)["requests"][0]["state"], "pending"
        )

        # The user confirms it through the released manual import route.
        status, receipt = self.post(IMPORT, proposal)
        self.assertEqual(status, 200, receipt)
        self.assertEqual(receipt["meta"]["imported_count"], 1)
        captures = self.document("captures.json")["captures"]
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0]["schema_version"], "1.1")
        self.assertEqual(captures[0]["provenance"]["capture_mode"], "manual")
        self.assertEqual(captures[0]["source"]["resource_type"], "knowledge.answer")
        self.assertEqual(self.document("backlog.json")["tasks"], [])
        self.assertEqual(
            self.document(KNOWLEDGE_DOCUMENT_NAME)["requests"][0]["state"], "completed"
        )
        self.assertNotIn(QUERY_CANARY, json.dumps(self.document("captures.json")))

    def test_a_second_execution_starts_no_second_child(self) -> None:
        """The attempt is one-way, and a repeat never reaches the driver."""

        document = self.ready()
        self.arm_answer()
        self.assertEqual(self.execute(document)[0], 200)
        self.assertEqual(self.child_calls(), 1)

        status, payload = self.execute(document)
        self.assertEqual(status, 409, payload)
        self.assertRefused(payload, "request_already_attempted")
        self.assertEqual(self.child_calls(), 1)

    def test_two_concurrent_executions_start_one_child(self) -> None:
        """The consume is inside the transaction that proved the authority."""

        document = self.ready()
        self.arm_answer()
        self.arm_barrier()
        results: list[tuple[int, Any]] = []
        lock = threading.Lock()

        def run() -> None:
            outcome = self.execute(document)
            with lock:
                results.append(outcome)

        first = threading.Thread(target=run)
        first.start()
        self.await_child()
        second = threading.Thread(target=run)
        second.start()
        second.join(timeout=30)
        self.release_child()
        first.join(timeout=30)

        self.assertEqual(len(results), 2, results)
        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses, [200, 409])
        refused = [payload for status, payload in results if status == 409][0]
        self.assertRefused(refused, "request_already_attempted")
        self.assertEqual(self.child_calls(), 1)


class ExecutionOutputTest(ExecutionHttpCase):
    """The child's stdout is projected by the released validators, not trusted."""

    def refuse(self, mode: str, status_code: int, code: str) -> Any:
        document = self.ready()
        self.arm_answer()
        self.set_mode(mode)
        status, payload = self.execute(document)
        self.assertEqual(status, status_code, payload)
        self.assertRefused(payload, code)
        self.assertEqual(self.child_calls(), 1)
        # Nothing was saved, and the attempt is not given back.
        self.assertEqual(self.document("captures.json")["captures"], [])
        self.assertEqual(self.execute(document)[1]["error"]["code"], "request_already_attempted")
        self.assertEqual(self.child_calls(), 1)
        return payload

    def test_a_wrong_stdout_request_id_is_refused(self) -> None:
        self.refuse("wrong_request", 502, "request_id_mismatch")

    def test_a_child_that_answers_nothing_parseable_is_refused(self) -> None:
        payload = self.refuse("garbage", 502, "invalid_json")
        self.assertNotIn("not json at all", json.dumps(payload))

    def test_a_nonzero_exit_is_an_unknown_outcome(self) -> None:
        self.refuse("exit", 502, "driver_outcome_unknown")

    def test_child_stderr_never_reaches_the_response(self) -> None:
        """A child may write to stderr; it is discarded, not returned."""

        document = self.ready()
        self.arm_answer()
        self.set_mode("stderr")
        status, payload = self.execute(document)
        self.assertEqual(status, 200, payload)
        self.assertNotIn(STDERR_CANARY, json.dumps(payload))

    def test_an_injected_field_is_refused_by_the_projection(self) -> None:
        payload = self.refuse("injection", 502, "unknown_field")
        self.assertNotIn(STDERR_CANARY, json.dumps(payload))

    def test_a_child_that_exceeds_its_budget_is_an_unknown_outcome(self) -> None:
        """The one budget is the caller's, and an expired one is not a retry."""

        original = knowledge_execution_runtime.DRIVER_TIMEOUT_SECONDS
        knowledge_execution_runtime.DRIVER_TIMEOUT_SECONDS = 1.0
        self.addCleanup(
            setattr, knowledge_execution_runtime, "DRIVER_TIMEOUT_SECONDS", original
        )
        self.refuse("sleep", 502, "driver_outcome_unknown")


class ExistingSurfaceRegressionTest(ExecutionHttpCase):
    """The routes this caller changed still behave exactly as they did."""

    def test_issuing_is_unchanged_by_the_registration_callback(self) -> None:
        """Registration is a callback into this process, not a second answer."""

        self.with_policy()
        status, issued = self.post(REQUESTS, self.issue_body())
        self.assertEqual(status, 200, issued)
        self.assertEqual(
            set(issued["meta"]),
            {"replayed", "state", "connection_alias", "policy_revision"},
        )
        self.assertEqual(issued["meta"]["replayed"], False)
        self.assertEqual(issued["meta"]["state"], "pending")
        self.assertEqual(issued["data"]["requested_at"], NOW)
        self.assertEqual(issued["data"]["expires_at"], "2026-09-08T09:05:00Z")

        status, replayed = self.post(REQUESTS, self.issue_body())
        self.assertEqual(status, 200, replayed)
        self.assertEqual(replayed["data"], issued["data"])
        self.assertEqual(replayed["meta"]["replayed"], True)

        # A refused issue still refuses, and still registers nothing.
        status, refused = self.post(REQUESTS, self.issue_body(connection_alias="absent"))
        self.assertEqual(status, 400, refused)
        self.assertNoCanary(refused)

    def test_the_manual_import_route_is_unchanged(self) -> None:
        """The released import path still works without any execution at all."""

        document = self.ready(intent_id=OTHER_INTENT_ID)
        envelope = {
            "schema": "workstack.knowledge-import.v1",
            "request_id": document["request_id"],
            "items": [self.answer_item(document["request_id"])],
        }
        status, receipt = self.post(IMPORT, envelope)
        self.assertEqual(status, 200, receipt)
        self.assertEqual(receipt["meta"]["imported_count"], 1)
        self.assertEqual(len(self.document("captures.json")["captures"]), 1)
        self.assertEqual(self.document("backlog.json")["tasks"], [])
        self.assertEqual(self.child_calls(), 0)

    def test_the_released_route_table_still_answers_its_own_paths(self) -> None:
        """Adding one POST route did not shadow the two beside it."""

        self.with_policy()
        self.assertEqual(self.call("GET", CONNECTIONS)[0], 200)
        self.assertEqual(self.post(REQUESTS, self.issue_body())[0], 200)
        self.assertEqual(self.call("GET", "/api/v1/knowledge/requests")[0], 404)
        self.assertEqual(self.post("/api/v1/knowledge/requests/other", {})[0], 404)


if __name__ == "__main__":  # pragma: no cover - parity with the released suites
    unittest.main()
