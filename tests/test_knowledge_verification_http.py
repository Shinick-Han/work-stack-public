"""The owner verification route, through a real local HTTP server and a real child.

Every server here binds ``127.0.0.1:0`` over a store this test created in its
own temporary directory, and every verifier is a synthetic local Python child
this test wrote. Nothing opens a real home directory, a live SSOT, a provider,
a credential, a NAS root or any network beyond that loopback socket, and no
dependency is installed. The issuing, import and verification clock is
injected, so the sixty-second observation window is a fact of the test rather
than of the second it runs in.

The Capture under check is a *real* one: the released policy, issue and manual
import routes build it over the actual store, so the evidence the owner derives
is the evidence that was really stored, and never something this test handed
the admission.

The child is driven entirely through the operator-configured environment: it is
told a state directory and reads its mode, its answer template and its release
barrier from files there. That is what lets one server, holding one
verification gate, act out a whole sequence -- a blocked child, a policy change
while it is blocked, a second concurrent check -- without ever being restarted.

The classes are the boundary's obligations in order: who may reach it and what
a refusal costs, what an operator must separately opt into, one whole
observation, what may run while the child does, and how untrusted the child's
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

from workstack.knowledge_execution_runtime import KnowledgeDriverBinding
from workstack.knowledge_ledger_document import KNOWLEDGE_DOCUMENT_NAME
from workstack.knowledge_verification_protocol import KnowledgeVerificationBinding
from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import Store

UPSTREAM_UID = "66666666-6666-4666-8666-666666666666"
OTHER_UPSTREAM_UID = "77777777-7777-4777-8777-777777777777"
INTENT_ID = "a1b2c3d4-1111-4111-8111-aaaaaaaaaaaa"
ITEM_ONE = "c3d4e5f6-3333-4333-8333-cccccccccccc"
ITEM_TWO = "e5f60718-5555-4555-8555-eeeeeeeeeeee"
ABSENT_CAPTURE = "C-9999"

# Strings that exist nowhere else. Each one is a probe for a different leak.
QUERY_CANARY = "canary 7f2b90 query probe phrase"
SUMMARY_CANARY = "canary 7f2b90 answer summary"
EVIDENCE_TITLE_CANARY = "canary 7f2b90 evidence title"
STDERR_CANARY = "canary 7f2b90 child stderr text"
AMBIENT_CANARY = "canary 7f2b90 ambient owner environment"
OPERATOR_CANARY = "canary 7f2b90 operator configured value"

NOW = "2026-09-09T09:00:00Z"

CONNECTIONS = "/api/v1/knowledge/connections"
REQUESTS = "/api/v1/knowledge/requests"
IMPORT = "/api/v1/knowledge/captures/import"
VERIFY = "/api/v1/knowledge/captures/verify"

DOCUMENT_ONE = "nas-doc-0001abcd"
DOCUMENT_TWO = "nas-doc-0002efgh"
VERSION_ONE = "nas-version-11"
VERSION_TWO = "nas-version-22"

# The synthetic verifier. It reads one verification request from stdin, records
# what it was given, optionally waits on a file barrier, and answers by
# reflecting the request's own evidence. It reaches no network, opens no source
# and knows nothing about the store.
VERIFIER_SOURCE = '''
import json
import os
import sys
import time

STATE = os.environ["WS_VERIFIER_STATE"]


def path(name):
    return os.path.join(STATE, name)


def read(name, default=""):
    try:
        with open(path(name), "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return default


def observed(entry, mode):
    if mode == "stale":
        return {"observed_source_version": "nas-version-99", "status": "stale", "code": "hash_differs"}
    if mode == "missing":
        return {"observed_source_version": None, "status": "missing", "code": "file_absent"}
    if mode == "impossible":
        return {"observed_source_version": None, "status": "current", "code": "hash_matched"}
    return {
        "observed_source_version": entry["expected_source_version"],
        "status": "current",
        "code": "hash_matched",
    }


def main():
    raw = sys.stdin.buffer.read()
    with open(path("calls"), "a", encoding="utf-8") as handle:
        handle.write("call\\n")
    with open(path("payload.json"), "w", encoding="utf-8") as handle:
        handle.write(raw.decode("utf-8"))
    with open(path("environment.json"), "w", encoding="utf-8") as handle:
        json.dump(dict(os.environ), handle, sort_keys=True)
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
    if mode == "silent":
        return 0
    if mode == "garbage":
        sys.stdout.buffer.write(b"{not json at all")
        return 0
    request = json.loads(raw)
    entries = []
    for entry in request["evidence"]:
        answer = {
            "document_ref": entry["document_ref"],
            "source_type": entry["source_type"],
            "expected_source_version": entry["expected_source_version"],
        }
        answer.update(observed(entry, mode))
        entries.append(answer)
    if mode == "reordered":
        entries.reverse()
    if mode == "truncated":
        entries = entries[:1]
    if mode == "forged_ref":
        entries[0]["document_ref"] = "nas-doc-0000zzzz"
    if mode == "forged_version":
        entries[0]["expected_source_version"] = "nas-version-77"
    result = {
        "schema": "workstack.knowledge-verification.v1",
        "verification_id": request["verification_id"],
        "checked_at": request["requested_at"],
        "evidence": entries,
    }
    if mode == "wrong_nonce":
        result["verification_id"] = "11111111-1111-4111-8111-111111111111"
    if mode == "late":
        result["checked_at"] = request["expires_at"]
    if mode == "stderr":
        sys.stderr.write(os.environ["WS_VERIFIER_STDERR"])
        sys.stderr.flush()
    sys.stdout.buffer.write(json.dumps(result).encode("utf-8"))
    return 0


sys.exit(main())
'''


class VerificationHttpCase(unittest.TestCase):
    """One loopback server, one synthetic store, one synthetic pinned verifier."""

    alias = "team-nas"
    pinned_upstream = UPSTREAM_UID
    #: Whether the operator separately opted this alias into verification.
    verifier_configured = True

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        # Registered first, so every server a test opens -- and this case's own
        # server -- is closed before the directory holding its lease is removed.
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.stop)
        self.root = Path(self.temporary.name)
        self.state = self.root / "verifier-state"
        self.state.mkdir()
        self.verifier = self.root / "verifier.py"
        self.verifier.write_text(VERIFIER_SOURCE, encoding="utf-8")
        self.now = NOW
        # An ambient value the operator did NOT configure. A child that can see
        # it is inheriting this owner's environment.
        self.addCleanup(os.environ.pop, "WS_AMBIENT_CANARY", None)
        os.environ["WS_AMBIENT_CANARY"] = AMBIENT_CANARY
        self.set_mode("answer")
        self.start()

    # -- the verifier ----------------------------------------------------

    def environment(self) -> dict[str, str]:
        environment = {
            "WS_VERIFIER_STATE": str(self.state),
            "WS_VERIFIER_STDERR": STDERR_CANARY,
            "WS_VERIFIER_OPERATOR": OPERATOR_CANARY,
        }
        # Windows launches a Python child through the system root; the operator
        # states it explicitly, which is the whole point -- nothing is
        # inherited that the operator did not name.
        if "SystemRoot" in os.environ:
            environment["SystemRoot"] = os.environ["SystemRoot"]
        return environment

    def drivers(self) -> dict[str, KnowledgeDriverBinding]:
        """The operator's registry: one search driver, optionally verifiable.

        The search command is a path that does not exist, deliberately. This
        route must never fall back to it, and a test that accidentally did
        would fail to start a child rather than quietly succeeding.
        """

        verification = (
            KnowledgeVerificationBinding(
                command=(sys.executable, str(self.verifier)),
                environment=self.environment(),
            )
            if self.verifier_configured
            else None
        )
        return {
            self.alias: KnowledgeDriverBinding(
                upstream_workspace_uid=self.pinned_upstream,
                command=(sys.executable, str(self.root / "search-driver-absent.py")),
                environment={},
                verification=verification,
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
        return len(
            [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        )

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

    def policy_body(self, *, upstream: str = UPSTREAM_UID) -> dict[str, Any]:
        return {
            "expected_policy_revision": self.document(KNOWLEDGE_DOCUMENT_NAME)[
                "policy_revision"
            ],
            "connections": [
                {
                    "alias": self.alias,
                    "upstream_workspace_uid": upstream,
                    "corpus_refs": ["nas-team-share"],
                }
            ],
        }

    def with_policy(self, **overrides: Any) -> None:
        status, payload = self.post(CONNECTIONS, self.policy_body(**overrides))
        self.assertEqual(status, 200, payload)

    def issue(self) -> dict[str, Any]:
        status, payload = self.post(
            REQUESTS,
            {
                "intent_id": INTENT_ID,
                "connection_alias": self.alias,
                "binding": {"workspace_uid": self.workspace_uid},
                "query": QUERY_CANARY,
                "corpus_refs": ["nas-team-share"],
                "purpose": "find_context",
                "result_limit": 3,
            },
        )
        self.assertEqual(status, 200, payload)
        return payload["data"]

    def evidence_entry(self, document_ref: str, version: str) -> dict[str, Any]:
        return {
            "source_type": "nas.file",
            "title": EVIDENCE_TITLE_CANARY,
            "document_ref": document_ref,
            "chunk_ref": "chunk-{}".format(document_ref[-8:]),
            "source_version": version,
            "indexed_digest": "sha256:" + "a" * 64,
            "web_url": None,
        }

    def answer_item(self, request_id: str) -> dict[str, Any]:
        """One imported answer whose evidence names two stored NAS documents."""

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
                "answer_scope": "synthesized",
                "confidence": {"level": "medium", "score": 0.62},
                "evidence": [
                    self.evidence_entry(DOCUMENT_ONE, VERSION_ONE),
                    self.evidence_entry(DOCUMENT_TWO, VERSION_TWO),
                ],
                "truncated": False,
            },
        }

    def stored_capture(self) -> dict[str, Any]:
        """The real Capture the released import route wrote, read from disk."""

        captures = self.document("captures.json")["captures"]
        self.assertEqual(len(captures), 1, captures)
        return captures[0]

    def ready(self) -> dict[str, Any]:
        """A configured policy, one completed request and one stored Capture."""

        self.with_policy()
        issued = self.issue()
        status, receipt = self.post(
            IMPORT,
            {
                "schema": "workstack.knowledge-import.v1",
                "request_id": issued["request_id"],
                "items": [self.answer_item(issued["request_id"])],
            },
        )
        self.assertEqual(status, 200, receipt)
        return self.stored_capture()

    def verify_body(self, capture: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        body = {
            "workspace_uid": self.workspace_uid,
            "capture_id": capture["id"],
            "capture_revision": capture["revision"],
        }
        body.update(overrides)
        return body

    def verify(
        self,
        capture: dict[str, Any],
        headers: dict[str, str] | None = None,
        **overrides: Any,
    ):
        return self.post(VERIFY, self.verify_body(capture, **overrides), headers)

    # -- assertions ------------------------------------------------------

    def assertRefused(self, payload: Any, code: str) -> None:
        self.assertEqual(payload["error"]["code"], code, payload)
        self.assertEqual(payload["error"]["details"].keys() - {"field"}, set())
        self.assertNoCanary(payload)

    def assertNoCanary(self, payload: Any) -> None:
        rendered = json.dumps(payload)
        for canary in (
            QUERY_CANARY,
            SUMMARY_CANARY,
            EVIDENCE_TITLE_CANARY,
            STDERR_CANARY,
            AMBIENT_CANARY,
            OPERATOR_CANARY,
            str(self.verifier),
            sys.executable,
        ):
            self.assertNotIn(canary, rendered)

    def assertNothingSaved(self, before: dict[str, Any]) -> None:
        """The stored documents this check must never touch, byte for byte.

        ``activity.json`` is in the set deliberately: it is where the released
        idempotent-POST mechanism stores a whole response body, and this route
        is absent from that set. A ``None`` snapshot means the file did not
        exist before and must still not exist.
        """

        for name, snapshot in before.items():
            if snapshot is None:
                self.assertFalse((self.root / name).exists(), name)
                continue
            self.assertEqual(self.document(name), snapshot, name)

    def snapshot(self) -> dict[str, Any]:
        names = ["captures.json", KNOWLEDGE_DOCUMENT_NAME, "backlog.json"]
        taken: dict[str, Any] = {name: self.document(name) for name in names}
        taken["activity.json"] = (
            self.document("activity.json")
            if (self.root / "activity.json").exists()
            else None
        )
        return taken


class VerificationAdmissionTest(VerificationHttpCase):
    """Who reaches the route, and what a refusal costs."""

    def test_a_request_without_csrf_reaches_no_child(self) -> None:
        capture = self.ready()
        headers = self.owner_headers()
        for name in ("X-WorkStack-CSRF", "Origin"):
            with self.subTest(missing=name):
                without = {
                    key: value for key, value in headers.items() if key != name
                }
                status, payload = self.verify(capture, without)
                self.assertEqual(status, 403, payload)
                self.assertEqual(self.child_calls(), 0)

    def test_a_capture_bearer_token_alone_reaches_no_child(self) -> None:
        """Capture ingestion authority is not owner verification authority."""

        capture = self.ready()
        for token in ("not-a-real-token", self.server.capture_token):
            with self.subTest(real=token != "not-a-real-token"):
                status, payload = self.verify(
                    capture,
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer {}".format(token),
                    },
                )
                self.assertIn(status, (401, 403), payload)
                self.assertEqual(self.child_calls(), 0)

    def test_a_non_canonical_target_is_the_unknown_route(self) -> None:
        """The route takes no parameter, so a target carrying one is not it."""

        capture = self.ready()
        body = json.dumps(self.verify_body(capture)).encode("utf-8")
        for target in (
            VERIFY + "?unexpected=1",
            VERIFY + "?",
            VERIFY + ";x=1",
            VERIFY + "/",
        ):
            with self.subTest(target=target):
                status, payload = self.call(
                    "POST", target, body, self.owner_headers()
                )
                self.assertEqual(status, 404, payload)
                self.assertEqual(payload["error"]["code"], "not_found")
                self.assertEqual(self.child_calls(), 0)

    def test_an_idempotency_key_is_refused_before_any_child(self) -> None:
        capture = self.ready()
        headers = self.owner_headers()
        headers["Idempotency-Key"] = "11111111-1111-4111-8111-111111111111"
        status, payload = self.verify(capture, headers)
        self.assertEqual(status, 400, payload)
        self.assertRefused(payload, "unsupported_idempotency_key")
        self.assertEqual(self.child_calls(), 0)

    def test_a_malformed_body_reaches_no_child(self) -> None:
        capture = self.ready()
        bodies = {
            "extra_field": self.verify_body(capture, corpus_refs=["nas-team-share"]),
            "document_ref": self.verify_body(capture, document_ref=DOCUMENT_ONE),
            "bad_workspace": self.verify_body(capture, workspace_uid="not-a-uuid"),
            "bad_capture": self.verify_body(capture, capture_id="nope"),
            "negative_revision": self.verify_body(capture, capture_revision=-1),
            "boolean_revision": self.verify_body(capture, capture_revision=True),
            "string_revision": self.verify_body(capture, capture_revision="0"),
        }
        for name, body in bodies.items():
            with self.subTest(body=name):
                status, payload = self.post(VERIFY, body)
                self.assertEqual(status, 400, payload)
                self.assertIn(
                    payload["error"]["code"], ("invalid_body", "invalid_request")
                )
                self.assertNoCanary(payload)
                self.assertEqual(self.child_calls(), 0)

    def test_a_missing_body_field_is_refused(self) -> None:
        capture = self.ready()
        for name in ("workspace_uid", "capture_id", "capture_revision"):
            with self.subTest(missing=name):
                body = self.verify_body(capture)
                del body[name]
                status, payload = self.post(VERIFY, body)
                self.assertEqual(status, 400, payload)
                self.assertEqual(self.child_calls(), 0)

    def test_an_unknown_capture_is_a_404_and_reaches_no_child(self) -> None:
        capture = self.ready()
        status, payload = self.verify(capture, capture_id=ABSENT_CAPTURE)
        self.assertEqual(status, 404, payload)
        self.assertRefused(payload, "unknown_capture")
        self.assertEqual(self.child_calls(), 0)

    def test_a_changed_revision_reaches_no_child(self) -> None:
        capture = self.ready()
        status, payload = self.verify(capture, capture_revision=capture["revision"] + 1)
        self.assertEqual(status, 409, payload)
        self.assertRefused(payload, "capture_revision_changed")
        self.assertEqual(self.child_calls(), 0)

    def test_a_foreign_workspace_uid_reaches_no_child(self) -> None:
        """The store's own binding is the one the browser said it saw."""

        capture = self.ready()
        status, payload = self.verify(capture, workspace_uid=OTHER_UPSTREAM_UID)
        self.assertEqual(status, 409, payload)
        self.assertRefused(payload, "verification_binding_mismatch")
        self.assertEqual(self.child_calls(), 0)


class VerificationConfigurationTest(VerificationHttpCase):
    """Verification is its own opt-in, and absence is a reportable state."""

    verifier_configured = False

    def test_a_search_driver_alone_is_not_a_verifier(self) -> None:
        """The search command is never borrowed, and no child is started."""

        capture = self.ready()
        status, payload = self.verify(capture)
        self.assertEqual(status, 503, payload)
        self.assertRefused(payload, "knowledge_verifier_unavailable")
        self.assertEqual(self.child_calls(), 0)

    def test_a_server_with_no_drivers_at_all_still_serves_the_surface(self) -> None:
        """No configuration is the empty registry, not a broken server."""

        self.restart(drivers={})
        capture = self.ready()
        status, payload = self.call("GET", CONNECTIONS)
        self.assertEqual(status, 200, payload)
        status, payload = self.verify(capture)
        self.assertEqual(status, 503, payload)
        self.assertRefused(payload, "knowledge_verifier_unavailable")
        self.assertEqual(self.child_calls(), 0)


class VerificationObservationTest(VerificationHttpCase):
    """One whole check: what the child is given and what comes back."""

    def test_a_valid_check_returns_a_bound_ordered_observation(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        status, payload = self.verify(capture)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"], {"outcome": "verification_ready"})
        self.assertEqual(
            payload["data"]["binding"],
            {
                "workspace_uid": self.workspace_uid,
                "capture_id": capture["id"],
                "capture_revision": capture["revision"],
            },
        )
        result = payload["data"]["result"]
        self.assertEqual(result["schema"], "workstack.knowledge-verification.v1")
        self.assertEqual(
            [entry["document_ref"] for entry in result["evidence"]],
            [DOCUMENT_ONE, DOCUMENT_TWO],
        )
        self.assertEqual(
            [entry["expected_source_version"] for entry in result["evidence"]],
            [VERSION_ONE, VERSION_TWO],
        )
        for entry in result["evidence"]:
            self.assertEqual(entry["source_type"], "nas.file")
            self.assertEqual(entry["status"], "current")
            self.assertEqual(entry["code"], "hash_matched")
            self.assertEqual(
                entry["observed_source_version"], entry["expected_source_version"]
            )
        self.assertEqual(self.child_calls(), 1)
        self.assertNoCanary(payload)
        # An observation, not a refresh: nothing on disk moved.
        self.assertNothingSaved(before)

    def test_the_child_is_given_the_request_and_an_immutable_sixty_second_window(
        self,
    ) -> None:
        capture = self.ready()
        status, payload = self.verify(capture)
        self.assertEqual(status, 200, payload)
        sent = self.child_payload()
        self.assertEqual(set(sent), {
            "schema",
            "verification_id",
            "binding",
            "connection",
            "corpus_refs",
            "evidence",
            "requested_at",
            "expires_at",
        })
        self.assertEqual(sent["schema"], "workstack.knowledge-verify.v1")
        self.assertEqual(sent["requested_at"], NOW)
        self.assertEqual(sent["expires_at"], "2026-09-09T09:01:00Z")
        self.assertEqual(sent["binding"]["capture_id"], capture["id"])
        self.assertEqual(sent["connection"]["alias"], self.alias)
        self.assertEqual(sent["connection"]["upstream_workspace_uid"], UPSTREAM_UID)
        self.assertEqual(sent["corpus_refs"], ["nas-team-share"])
        self.assertEqual(
            [entry["document_ref"] for entry in sent["evidence"]],
            [DOCUMENT_ONE, DOCUMENT_TWO],
        )
        # The child is told opaque identities and expected versions, and no
        # title, query, summary, digest, path or token at all.
        rendered = json.dumps(sent)
        for canary in (
            QUERY_CANARY,
            SUMMARY_CANARY,
            EVIDENCE_TITLE_CANARY,
            self.server.csrf_token,
            self.server.capture_token,
            str(self.root),
        ):
            self.assertNotIn(canary, rendered)
        self.assertNotIn("indexed_digest", rendered)

    def test_the_child_sees_only_the_operator_stated_environment(self) -> None:
        capture = self.ready()
        status, payload = self.verify(capture)
        self.assertEqual(status, 200, payload)
        seen = self.child_environment()
        self.assertNotIn("WS_AMBIENT_CANARY", seen)
        self.assertEqual(seen.get("WS_VERIFIER_OPERATOR"), OPERATOR_CANARY)
        rendered = json.dumps(seen)
        self.assertNotIn(self.server.csrf_token, rendered)
        self.assertNotIn(self.server.capture_token, rendered)

    def test_a_stale_observation_is_reported_as_stale(self) -> None:
        capture = self.ready()
        self.set_mode("stale")
        status, payload = self.verify(capture)
        self.assertEqual(status, 200, payload)
        for entry in payload["data"]["result"]["evidence"]:
            self.assertEqual(entry["status"], "stale")
            self.assertEqual(entry["code"], "hash_differs")
            self.assertEqual(entry["observed_source_version"], "nas-version-99")

    def test_an_explicit_second_check_may_run_after_a_settled_success(self) -> None:
        """Nothing here is one-shot: a user may ask again, deliberately."""

        capture = self.ready()
        for _ in range(2):
            status, payload = self.verify(capture)
            self.assertEqual(status, 200, payload)
        self.assertEqual(self.child_calls(), 2)


class VerificationOutcomeTest(VerificationHttpCase):
    """How untrusted the child's answer stays, and what a refusal releases."""

    def refuse(self, capture: dict[str, Any], mode: str, code: str, status: int) -> None:
        self.set_mode(mode)
        actual, payload = self.verify(capture)
        self.assertEqual(actual, status, payload)
        self.assertRefused(payload, code)

    def test_an_unusable_answer_is_a_bad_gateway_and_never_a_display(self) -> None:
        capture = self.ready()
        before = self.snapshot()
        for mode in (
            "garbage",
            "wrong_nonce",
            "reordered",
            "truncated",
            "forged_ref",
            "forged_version",
            "impossible",
            "late",
            "silent",
        ):
            with self.subTest(mode=mode):
                self.refuse(capture, mode, "verification_result_refused", 502)
        self.assertNothingSaved(before)

    def test_a_nonzero_exit_is_an_unknown_outcome_and_is_not_retried(self) -> None:
        capture = self.ready()
        self.refuse(capture, "exit", "driver_outcome_unknown", 502)
        self.assertEqual(self.child_calls(), 1)

    def test_a_settled_refusal_releases_the_gate_for_a_new_user_check(self) -> None:
        capture = self.ready()
        self.refuse(capture, "garbage", "verification_result_refused", 502)
        self.set_mode("answer")
        status, payload = self.verify(capture)
        self.assertEqual(status, 200, payload)

    def test_a_child_stderr_never_reaches_the_response(self) -> None:
        capture = self.ready()
        self.set_mode("stderr")
        status, payload = self.verify(capture)
        self.assertEqual(status, 200, payload)
        self.assertNoCanary(payload)


class VerificationConcurrencyTest(VerificationHttpCase):
    """What may run while the child does, and what may not."""

    def blocked_check(self, capture: dict[str, Any]) -> list[Any]:
        """Start one check that will block inside its child, and wait for it."""

        outcome: list[Any] = []
        self.arm_barrier()
        worker = threading.Thread(
            target=lambda: outcome.append(self.verify(capture)), daemon=True
        )
        worker.start()
        self.addCleanup(worker.join, 30)
        self.addCleanup(self.release_child)
        self.await_child()
        self.worker = worker
        return outcome

    def test_a_second_check_is_busy_and_starts_no_second_child(self) -> None:
        capture = self.ready()
        outcome = self.blocked_check(capture)
        status, payload = self.verify(capture)
        self.assertEqual(status, 409, payload)
        self.assertRefused(payload, "verification_busy")
        self.assertEqual(self.child_calls(), 1)
        self.release_child()
        self.worker.join(30)
        self.assertEqual(outcome[0][0], 200, outcome)

    def test_a_refused_check_does_not_release_the_running_one(self) -> None:
        """A busy answer must not hand the gate to the caller it refused.

        The gate carries no ownership token, so the discipline that keeps this
        true lives in the runtime: the acquire is outside the ``try`` whose
        ``finally`` settles, and a refused check therefore never reaches it.
        Two refused checks in a row are the counterexample -- if the first
        refusal released the gate, the second would start a child beside the
        one that is still blocked.
        """

        capture = self.ready()
        outcome = self.blocked_check(capture)
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                status, payload = self.verify(capture)
                self.assertEqual(status, 409, payload)
                self.assertRefused(payload, "verification_busy")
                self.assertEqual(self.child_calls(), 1)
                self.assertTrue(self.server.knowledge_verification_guard.active)
        self.release_child()
        self.worker.join(30)
        self.assertEqual(outcome[0][0], 200, outcome)
        self.assertEqual(self.child_calls(), 1)
        self.assertFalse(self.server.knowledge_verification_guard.active)

    def test_the_store_is_not_held_while_the_child_runs(self) -> None:
        """The read transaction is released before the child, not after it.

        The probe takes the server's *own* Store transaction from another
        thread. That lock is the one the admission held, so acquiring it while
        the child is provably still blocked is direct evidence that nothing
        this owner does while waiting on a child holds the store.
        """

        capture = self.ready()
        outcome = self.blocked_check(capture)
        acquired = threading.Event()

        def probe() -> None:
            with self.store.transaction():
                acquired.set()

        threading.Thread(target=probe, daemon=True).start()
        self.assertTrue(
            acquired.wait(15.0), "the store was held across the verifier child"
        )
        status, payload = self.call("GET", CONNECTIONS)
        self.assertEqual(status, 200, payload)
        self.release_child()
        self.worker.join(30)
        self.assertEqual(outcome[0][0], 200, outcome)

    def test_a_policy_change_while_the_child_runs_refuses_the_observation(self) -> None:
        capture = self.ready()
        outcome = self.blocked_check(capture)
        status, payload = self.post(CONNECTIONS, self.policy_body())
        self.assertEqual(status, 200, payload)
        self.release_child()
        self.worker.join(30)
        status, payload = outcome[0]
        self.assertEqual(status, 409, payload)
        self.assertRefused(payload, "verification_authority_changed")

    def test_the_gate_is_released_after_a_concurrent_refusal(self) -> None:
        capture = self.ready()
        outcome = self.blocked_check(capture)
        status, _ = self.verify(capture)
        self.assertEqual(status, 409)
        self.release_child()
        self.worker.join(30)
        self.assertEqual(outcome[0][0], 200, outcome)
        status, payload = self.verify(capture)
        self.assertEqual(status, 200, payload)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
