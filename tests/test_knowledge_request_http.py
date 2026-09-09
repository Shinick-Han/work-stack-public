"""The owner knowledge surface, through a real local HTTP server.

Every server here binds ``127.0.0.1:0`` over a store this test created in its
own temporary directory. Nothing opens a real home directory or a live SSOT,
nothing reaches a network beyond that loopback socket, no dependency is
installed and the issuing clock is injected, so expiry is a fact of the test
rather than of the day it runs on.

The suite is organised by what the boundary has to get right:

* who may reach it at all (``AdmissionTest``),
* what the policy surface publishes and accepts (``PolicyRouteTest``),
* content-free occupancy on that same policy snapshot (``OccupancyRouteTest``),
* what issuing means, including retries, expiry, restart and
  capacity (``IssueRouteTest``, ``CapacityIssueRouteTest``),
* that the query never becomes durable state (``QueryConfinementTest``),
* and that the released route admission order is unchanged (``RouteAdmissionTest``).
"""

from __future__ import annotations

import copy
import http.client
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from workstack.knowledge_ledger_document import (
    KNOWLEDGE_DOCUMENT_NAME,
    MAX_KNOWLEDGE_BYTES,
    MAX_REQUESTS,
    KnowledgeLedgerError,
    compact_bytes,
    validate_knowledge_document,
)
from workstack.knowledge_requests_http import (
    KNOWLEDGE_BODY_LIMIT,
    KnowledgeRequestsHttpMixin,
)
from workstack.server_errors import RequestError
from workstack.server import CAPTURE_BODY_LIMIT, DEFAULT_BODY_LIMIT, create_server
from workstack.service import WorkStack
from workstack.store import Store

UPSTREAM_UID = "66666666-6666-4666-8666-666666666666"
INTENT_ID = "a1b2c3d4-1111-4111-8111-aaaaaaaaaaaa"
OTHER_INTENT_ID = "b2c3d4e5-2222-4222-8222-bbbbbbbbbbbb"
# One string that exists nowhere else in the repository or the store. If it ever
# turns up in a persisted document, an activity record or a receipt, the query
# became durable state.
QUERY_CANARY = "canary 9f2c1d7a unique retrieval probe phrase"
NOW = "2026-09-08T09:00:00Z"
INSIDE = "2026-09-08T09:02:00Z"
AFTER_EXPIRY = "2026-09-08T09:05:01Z"

CONNECTIONS = "/api/v1/knowledge/connections"
REQUESTS = "/api/v1/knowledge/requests"


class KnowledgeHttpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = NOW
        self.start()

    def tearDown(self) -> None:
        self.stop()
        self.temporary.cleanup()

    # -- the server ------------------------------------------------------

    def start(self) -> None:
        """One loopback server over this test's own synthetic store."""

        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.server = create_server(self.stack, "127.0.0.1", 0)
        # The explicit clock seam: production reads the UTC wall clock, and a
        # test states the instant instead.
        self.server.knowledge_clock = lambda: self.now
        self.port = self.server.actual_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.running = True
        self.workspace_uid = json.loads(
            (self.root / "workspace.json").read_text(encoding="utf-8")
        )["id"]

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def restart(self) -> None:
        """A second process's view: the document on disk is the only state."""

        self.stop()
        self.start()

    # -- the wire --------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
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

    def ledger(self) -> dict[str, Any]:
        return json.loads(
            (self.root / KNOWLEDGE_DOCUMENT_NAME).read_text(encoding="utf-8")
        )

    def policy_body(self, *corpus_refs: str) -> dict[str, Any]:
        return {
            "expected_policy_revision": self.ledger()["policy_revision"],
            "connections": [
                {
                    "alias": "team-nas",
                    "upstream_workspace_uid": UPSTREAM_UID,
                    "corpus_refs": list(corpus_refs)
                    or ["nas-team-share", "notion-product"],
                }
            ],
        }

    def with_policy(self, *corpus_refs: str) -> None:
        status, payload = self.post(CONNECTIONS, self.policy_body(*corpus_refs))
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

    def open_task(self) -> dict[str, Any]:
        self.stack.add_task("Held task")
        return json.loads(
            (self.root / "backlog.json").read_text(encoding="utf-8")
        )["tasks"][0]

    def task_binding(self, task: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        binding = {
            "workspace_uid": self.workspace_uid,
            "task_uid": task["uid"],
            "task_id": task["id"],
            "task_revision": task["revision"],
        }
        binding.update(overrides)
        return binding

    def assertRefused(self, payload: Any, code: str) -> None:
        self.assertEqual(payload["error"]["code"], code, payload)
        self.assertEqual(payload["error"]["details"].keys() - {"field"}, set())

    def assertClosedOccupancy(self, payload: Any, document: dict[str, Any]) -> None:
        """Closed content-free occupancy of this exact admitted snapshot."""

        self.assertEqual(set(payload), {"data", "meta"}, payload)
        self.assertEqual(set(payload["data"]), {"policy_revision", "connections"})
        self.assertEqual(set(payload["meta"]), {"occupancy"})
        occupancy = payload["meta"]["occupancy"]
        self.assertEqual(
            set(occupancy),
            {"request_count", "request_bound", "encoded_bytes", "byte_bound"},
        )
        for value in occupancy.values():
            self.assertIs(type(value), int)
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 2**53 - 1)
        self.assertEqual(occupancy["request_bound"], MAX_REQUESTS)
        self.assertEqual(occupancy["byte_bound"], MAX_KNOWLEDGE_BYTES)
        self.assertGreater(occupancy["request_bound"], 0)
        self.assertGreater(occupancy["byte_bound"], 0)
        self.assertEqual(occupancy["request_count"], len(document["requests"]))
        self.assertEqual(occupancy["encoded_bytes"], len(compact_bytes(document)))
        self.assertLessEqual(occupancy["request_count"], occupancy["request_bound"])
        self.assertLessEqual(occupancy["encoded_bytes"], occupancy["byte_bound"])
        disk = (self.root / KNOWLEDGE_DOCUMENT_NAME).stat().st_size
        self.assertLess(occupancy["encoded_bytes"], disk)

    def typical_occupancy_record(
        self, index: int, policy_revision: int
    ) -> dict[str, Any]:
        """The same synthetic owner row the ledger tests fill to the row cap."""

        return {
            "request_id": "{:08x}-0000-4000-8000-000000000000".format(index),
            "request_digest": "sha256:" + "2" * 64,
            "connection_alias": "team-nas",
            "binding": {"workspace_uid": self.workspace_uid},
            "corpus_refs": ["nas-team-share"],
            "policy_revision": policy_revision,
            "result_limit": 1,
            "requested_at": "2026-09-08T09:00:00Z",
            "expires_at": "2026-09-08T09:05:00Z",
            "state": "pending",
            "capture_ids": [],
            "completion_digest": None,
            "completed_at": None,
        }

    def fat_occupancy_record(
        self,
        index: int,
        policy_revision: int,
        corpus_refs: list[str],
    ) -> dict[str, Any]:
        """A max-fat completed row: byte cap is reachable before 200."""

        return {
            "request_id": "{:08x}-0000-4000-8000-000000000000".format(index),
            "request_digest": "sha256:" + "2" * 64,
            "connection_alias": "team-nas",
            "binding": {
                "workspace_uid": self.workspace_uid,
                "task_uid": "77777777-7777-4777-8777-777777777777",
                "task_id": "T-0001",
                "task_revision": 1,
            },
            "corpus_refs": list(corpus_refs),
            "policy_revision": policy_revision,
            "result_limit": 10,
            "requested_at": "2026-09-08T09:00:00Z",
            "expires_at": "2026-09-08T09:05:00Z",
            "state": "completed",
            "capture_ids": ["C-{}".format(str(n).zfill(30)) for n in range(10)],
            "completion_digest": "sha256:" + "3" * 64,
            "completed_at": "2026-09-08T09:02:00Z",
        }

    def persist_owner_ledger(self, document: dict[str, Any]) -> None:
        """Commit a validated ledger through the same Store the server holds."""

        validate_knowledge_document(document, workspace_uid=self.workspace_uid)
        with self.store.transaction():
            self.store.save_many({KNOWLEDGE_DOCUMENT_NAME: document})

    def fill_row_cap(self) -> None:
        document = copy.deepcopy(self.ledger())
        document["requests"] = [
            self.typical_occupancy_record(index, document["policy_revision"])
            for index in range(MAX_REQUESTS)
        ]
        self.persist_owner_ledger(document)

    def fill_until_typical_issue_exceeds_bytes(self) -> int:
        """Leave a valid ledger where the next typical issue is document_too_large."""

        fat_refs = ["nas-team-share"] + [
            "r{}{}".format(index, "y" * 61) for index in range(7)
        ]
        document = copy.deepcopy(self.ledger())
        document["connections"][0]["corpus_refs"] = fat_refs
        policy_revision = document["policy_revision"]
        records: list[dict[str, Any]] = []

        def admits(candidate: list[dict[str, Any]]) -> bool:
            trial = copy.deepcopy(document)
            trial["requests"] = candidate
            try:
                validate_knowledge_document(trial, workspace_uid=self.workspace_uid)
            except KnowledgeLedgerError as error:
                if error.code != "document_too_large":
                    raise
                return False
            return True

        for index in range(MAX_REQUESTS):
            candidate = records + [
                self.fat_occupancy_record(index, policy_revision, fat_refs)
            ]
            if not admits(candidate):
                break
            records = candidate
        else:
            self.fail("fat occupancy never reached the knowledge byte cap")

        while len(records) < MAX_REQUESTS:
            candidate = records + [
                self.typical_occupancy_record(len(records), policy_revision)
            ]
            if not admits(candidate):
                break
            records = candidate
        else:
            self.fail("byte occupancy still has a free row; issue would be ledger_full")

        self.assertTrue(records)
        self.assertLess(len(records), MAX_REQUESTS)
        document["requests"] = records
        self.persist_owner_ledger(document)
        return len(records)

    def assertClosedCapacityRefusal(
        self, status: int, payload: Any, code: str, before: bytes
    ) -> None:
        self.assertEqual(status, 409, payload)
        self.assertRefused(payload, code)
        self.assertEqual(
            payload["error"]["message"], "the knowledge request was refused"
        )
        body = json.dumps(payload)
        self.assertNotIn(QUERY_CANARY, body)
        self.assertNotIn(str(self.root), body)
        self.assertNotIn(self.root.as_posix(), body)
        self.assertEqual((self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes(), before)


class AdmissionTest(KnowledgeHttpCase):
    def test_the_owner_session_reads_writes_and_issues(self) -> None:
        """The whole positive path, in the order a client performs it."""

        status, empty = self.call("GET", CONNECTIONS)
        self.assertEqual(status, 200)
        self.assertEqual(empty["data"], {"policy_revision": 0, "connections": []})

        status, written = self.post(CONNECTIONS, self.policy_body())
        self.assertEqual(status, 200)
        self.assertEqual(
            written["data"],
            {
                "policy_revision": 1,
                "connections": [
                    {
                        "alias": "team-nas",
                        "upstream_workspace_uid": UPSTREAM_UID,
                        "corpus_refs": ["nas-team-share", "notion-product"],
                        "scope": "workspace",
                    }
                ],
            },
        )
        self.assertEqual(self.call("GET", CONNECTIONS)[1], written)

        status, issued = self.post(REQUESTS, self.issue_body())
        self.assertEqual(status, 200)
        self.assertEqual(
            set(issued["data"]),
            {
                "schema",
                "request_id",
                "binding",
                "purpose",
                "query",
                "corpus_refs",
                "result_limit",
                "requested_at",
                "expires_at",
            },
        )
        self.assertEqual(issued["data"]["schema"], "workstack.knowledge-request.v1")
        self.assertEqual(issued["data"]["requested_at"], NOW)
        self.assertEqual(issued["data"]["expires_at"], "2026-09-08T09:05:00Z")
        self.assertEqual(
            issued["meta"],
            {
                "replayed": False,
                "state": "pending",
                "connection_alias": "team-nas",
                "policy_revision": 1,
            },
        )

    def test_a_cross_origin_page_writes_no_policy_and_no_ledger(self) -> None:
        self.with_policy()
        before = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes()
        hostile = dict(self.owner_headers(), Origin="http://evil.example")

        for path, body in (
            (CONNECTIONS, self.policy_body("notion-product")),
            (REQUESTS, self.issue_body()),
        ):
            with self.subTest(path=path):
                status, payload = self.post(path, body, hostile)
                self.assertEqual(status, 403)
                self.assertRefused(payload, "invalid_origin")

        self.assertEqual((self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes(), before)

    def test_a_missing_or_wrong_csrf_token_writes_nothing(self) -> None:
        self.with_policy()
        before = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes()
        headers = self.owner_headers()

        cases = (
            ({key: value for key, value in headers.items() if key != "X-WorkStack-CSRF"}, "invalid_csrf"),
            (dict(headers, **{"X-WorkStack-CSRF": "not-the-token"}), "invalid_csrf"),
        )
        for candidate, code in cases:
            with self.subTest(code=code):
                status, payload = self.post(REQUESTS, self.issue_body(), candidate)
                self.assertEqual(status, 403)
                self.assertRefused(payload, code)

        self.assertEqual((self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes(), before)

    def test_a_capture_bearer_alone_configures_nothing_and_issues_nothing(self) -> None:
        """Capture ingestion authority is not owner configuration authority."""

        self.with_policy()
        before = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes()
        # Both the forged token and the *real* Capture token: neither is an
        # owner session, so neither reaches policy configuration or issuing.
        for token in ("not-a-real-token", self.server.capture_token):
            with self.subTest(
                token="real" if token == self.server.capture_token else "invalid"
            ):
                status, payload = self.post(
                    REQUESTS,
                    self.issue_body(),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer {}".format(token),
                    },
                )
                self.assertEqual(status, 403)
                self.assertRefused(payload, "origin_required")

        self.assertEqual((self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes(), before)

    def test_neither_route_accepts_an_idempotency_key(self) -> None:
        """That mechanism caches response bodies; an issued one holds the query."""

        self.with_policy()
        headers = dict(self.owner_headers(), **{"Idempotency-Key": "knowledge-key-0001"})

        for path, body in (
            (CONNECTIONS, self.policy_body("notion-product")),
            (REQUESTS, self.issue_body()),
        ):
            with self.subTest(path=path):
                status, payload = self.post(path, body, headers)
                self.assertEqual(status, 400)
                self.assertRefused(payload, "unsupported_idempotency_key")


class PolicyRouteTest(KnowledgeHttpCase):
    def test_the_read_publishes_no_secret_and_no_request_record(self) -> None:
        self.with_policy()
        self.post(REQUESTS, self.issue_body())

        status, payload = self.call("GET", CONNECTIONS)
        body = json.dumps(payload)

        self.assertEqual(status, 200)
        self.assertEqual(set(payload["data"]), {"policy_revision", "connections"})
        self.assertNotIn(self.server.csrf_token, body)
        self.assertNotIn(self.server.capture_token, body)
        self.assertNotIn(QUERY_CANARY, body)
        self.assertNotIn("requests", body)
        self.assertNotIn("sha256:", body)

    def test_the_read_takes_no_query_string(self) -> None:
        status, payload = self.call("GET", CONNECTIONS + "?alias=team-nas")

        self.assertEqual(status, 400)
        self.assertRefused(payload, "invalid_query")

    def test_a_stale_expected_revision_refuses_and_changes_nothing(self) -> None:
        self.with_policy()
        before = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes()

        status, payload = self.post(
            CONNECTIONS,
            {"expected_policy_revision": 0, "connections": []},
        )

        self.assertEqual(status, 409)
        self.assertRefused(payload, "policy_revision_changed")
        self.assertEqual((self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes(), before)

    def test_a_policy_body_cannot_carry_a_location_a_secret_or_a_scope(self) -> None:
        cases = (
            ({"endpoint": "https://evil.example"}, "unknown_field"),
            ({"credential": "hunter2"}, "unknown_field"),
            ({"scope": "project:alpha"}, "unknown_field"),
            ({"alias": "https://evil.example"}, "invalid_alias"),
            ({"alias": "//server/share"}, "invalid_alias"),
            ({"alias": "C:/data"}, "invalid_alias"),
            ({"alias": "user:secret@host"}, "invalid_alias"),
            ({"upstream_workspace_uid": "not-a-uuid"}, "invalid_uuid"),
            ({"corpus_refs": "nas-team-share"}, "invalid_corpus_refs"),
        )
        for overrides, code in cases:
            with self.subTest(code=code, field=sorted(overrides)):
                body = self.policy_body()
                body["connections"][0].update(overrides)
                status, payload = self.post(CONNECTIONS, body)

                self.assertEqual(status, 400)
                self.assertRefused(payload, code)
                for value in overrides.values():
                    self.assertNotIn(str(value), json.dumps(payload))
        self.assertEqual(self.ledger()["connections"], [])


class OccupancyRouteTest(KnowledgeHttpCase):
    """Occupancy rides the existing connections envelope; data stays closed."""

    def test_empty_and_saved_policy_occupancy_match_the_admitted_document(self) -> None:
        status, empty = self.call("GET", CONNECTIONS)
        self.assertEqual(status, 200)
        self.assertClosedOccupancy(empty, self.ledger())
        self.assertEqual(empty["data"], {"policy_revision": 0, "connections": []})
        self.assertEqual(empty["meta"]["occupancy"]["request_count"], 0)

        status, written = self.post(CONNECTIONS, self.policy_body())
        self.assertEqual(status, 200, written)
        self.assertClosedOccupancy(written, self.ledger())
        self.assertGreater(
            written["meta"]["occupancy"]["encoded_bytes"],
            empty["meta"]["occupancy"]["encoded_bytes"],
        )
        status, reread = self.call("GET", CONNECTIONS)
        self.assertEqual(status, 200)
        self.assertEqual(reread, written)

    def test_issued_replayed_and_held_records_share_one_content_free_count(self) -> None:
        self.with_policy()
        before = self.call("GET", CONNECTIONS)[1]["meta"]["occupancy"]

        status, issued = self.post(REQUESTS, self.issue_body())
        self.assertEqual(status, 200, issued)
        request_id = issued["data"]["request_id"]

        status, after_issue = self.call("GET", CONNECTIONS)
        self.assertEqual(status, 200)
        self.assertClosedOccupancy(after_issue, self.ledger())
        occupancy = after_issue["meta"]["occupancy"]
        self.assertEqual(occupancy["request_count"], before["request_count"] + 1)
        self.assertGreater(occupancy["encoded_bytes"], before["encoded_bytes"])

        body = json.dumps(after_issue)
        self.assertNotIn(request_id, body)
        self.assertNotIn(QUERY_CANARY, body)
        self.assertNotIn("sha256:", body)
        self.assertNotIn(self.server.csrf_token, body)
        self.assertNotIn(self.server.capture_token, body)
        self.assertNotIn('"requests"', body)

        self.now = INSIDE
        status, replay = self.post(REQUESTS, self.issue_body())
        self.assertEqual(status, 200, replay)
        self.assertTrue(replay["meta"]["replayed"])
        status, after_replay = self.call("GET", CONNECTIONS)
        self.assertEqual(status, 200)
        self.assertEqual(after_replay, after_issue)

        document = copy.deepcopy(self.ledger())
        policy_revision = document["policy_revision"]
        document["requests"].extend(
            [
                self.typical_occupancy_record(1, policy_revision),
                self.fat_occupancy_record(
                    2, policy_revision, list(document["connections"][0]["corpus_refs"])
                ),
            ]
        )
        expired = self.typical_occupancy_record(3, policy_revision)
        expired["requested_at"] = "2026-09-08T08:00:00Z"
        expired["expires_at"] = "2026-09-08T08:05:00Z"
        document["requests"].append(expired)
        self.persist_owner_ledger(document)

        status, mixed = self.call("GET", CONNECTIONS)
        self.assertEqual(status, 200)
        self.assertClosedOccupancy(mixed, self.ledger())
        self.assertEqual(mixed["meta"]["occupancy"]["request_count"], 4)
        mixed_body = json.dumps(mixed)
        self.assertNotIn(request_id, mixed_body)
        self.assertNotIn("sha256:", mixed_body)
        self.assertNotIn('"requests"', mixed_body)

    def test_a_full_row_cap_is_visible_and_a_new_intent_still_conflicts(self) -> None:
        self.with_policy()
        self.fill_row_cap()
        status, payload = self.call("GET", CONNECTIONS)
        self.assertEqual(status, 200)
        self.assertClosedOccupancy(payload, self.ledger())
        self.assertEqual(payload["meta"]["occupancy"]["request_count"], MAX_REQUESTS)
        self.assertLess(
            payload["meta"]["occupancy"]["encoded_bytes"],
            payload["meta"]["occupancy"]["byte_bound"],
        )

        before = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes()
        status, refused = self.post(REQUESTS, self.issue_body())
        self.assertClosedCapacityRefusal(status, refused, "ledger_full", before)
        self.assertNotIn("occupancy", json.dumps(refused))

    def test_byte_bound_occupancy_stays_below_the_row_cap_and_still_conflicts(self) -> None:
        self.with_policy()
        occupied = self.fill_until_typical_issue_exceeds_bytes()
        status, payload = self.call("GET", CONNECTIONS)
        self.assertEqual(status, 200)
        self.assertClosedOccupancy(payload, self.ledger())
        occupancy = payload["meta"]["occupancy"]
        self.assertEqual(occupancy["request_count"], occupied)
        self.assertLess(occupancy["request_count"], MAX_REQUESTS)

        before = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes()
        status, refused = self.post(REQUESTS, self.issue_body())
        self.assertClosedCapacityRefusal(
            status, refused, "document_too_large", before
        )
        self.assertNotIn("occupancy", json.dumps(refused))

    def test_query_and_host_refusals_invent_no_occupancy(self) -> None:
        status, query = self.call("GET", CONNECTIONS + "?alias=team-nas")
        self.assertEqual(status, 400)
        self.assertRefused(query, "invalid_query")
        self.assertNotIn("occupancy", json.dumps(query))

        status, host_get = self.call("GET", CONNECTIONS, headers={"Host": "example.com"})
        self.assertEqual(status, 400)
        self.assertRefused(host_get, "invalid_host")
        self.assertNotIn("occupancy", json.dumps(host_get))

        headers = dict(self.owner_headers(), Host="example.com")
        status, host = self.call("POST", CONNECTIONS, b"", headers)
        self.assertEqual(status, 400)
        self.assertRefused(host, "invalid_host")
        self.assertNotIn("occupancy", json.dumps(host))


class IssueRouteTest(KnowledgeHttpCase):
    def test_a_hostile_closed_body_is_refused_without_echoing_it(self) -> None:
        self.with_policy()
        cases = (
            ({"request_id": str(uuid.uuid4())}, 400, "unknown_field"),
            ({"requested_at": "2026-01-01T00:00:00Z"}, 400, "unknown_field"),
            ({"expires_at": "2099-01-01T00:00:00Z"}, 400, "unknown_field"),
            ({"provider": "opendocuments.ask"}, 400, "unknown_field"),
            ({"authority": {"granted_corpus_refs": ["finance-drive"]}}, 400, "unknown_field"),
            ({"query": ""}, 400, "invalid_query"),
            ({"query": "a" * 1001}, 400, "invalid_query"),
            ({"query": "line\nbreak"}, 400, "invalid_query"),
            (
                {"query": "password=hunter2-and-more-secret-material"},
                400,
                "credential_material_suspected",
            ),
            ({"result_limit": 0}, 400, "out_of_range"),
            ({"result_limit": 11}, 400, "out_of_range"),
            ({"result_limit": True}, 400, "invalid_number"),
            ({"purpose": "exfiltrate"}, 400, "invalid_purpose"),
            ({"corpus_refs": []}, 400, "invalid_corpus_refs"),
            ({"corpus_refs": ["nas-team-share", "nas-team-share"]}, 400, "duplicate_corpus_ref"),
            ({"binding": {}}, 400, "invalid_task_binding"),
            ({"binding": {"workspace_uid": self.workspace_uid, "task_uid": UPSTREAM_UID}}, 400, "invalid_task_binding"),
            ({"connection_alias": "not-approved"}, 400, "connection_not_found"),
        )
        for overrides, expected_status, code in cases:
            with self.subTest(code=code, field=sorted(overrides)):
                status, payload = self.post(REQUESTS, self.issue_body(**overrides))

                self.assertEqual(status, expected_status, payload)
                self.assertRefused(payload, code)
                self.assertEqual(
                    payload["error"]["message"], "the knowledge request was refused"
                )
                self.assertNotIn("hunter2", json.dumps(payload))
                self.assertNotIn(QUERY_CANARY, json.dumps(payload))
        self.assertEqual(self.ledger()["requests"], [])

    def test_a_wrong_workspace_or_an_ungranted_corpus_is_refused(self) -> None:
        self.with_policy()
        cases = (
            ({"binding": {"workspace_uid": UPSTREAM_UID}}, "workspace_mismatch", 409),
            ({"corpus_refs": ["finance-drive"]}, "corpus_not_granted", 400),
        )
        for overrides, code, expected_status in cases:
            with self.subTest(code=code):
                status, payload = self.post(REQUESTS, self.issue_body(**overrides))

                self.assertEqual(status, expected_status, payload)
                self.assertRefused(payload, code)
        self.assertEqual(self.ledger()["requests"], [])

    def test_a_task_that_moved_since_the_caller_read_it_refuses(self) -> None:
        self.with_policy()
        task = self.open_task()

        status, issued = self.post(
            REQUESTS, self.issue_body(binding=self.task_binding(task))
        )
        self.assertEqual(status, 200, issued)

        status, stale = self.post(
            REQUESTS,
            self.issue_body(
                intent_id=OTHER_INTENT_ID,
                binding=self.task_binding(task, task_revision=task["revision"] + 3),
            ),
        )

        self.assertEqual(status, 409)
        self.assertRefused(stale, "task_binding_mismatch")
        self.assertEqual(len(self.ledger()["requests"]), 1)

    def test_a_task_the_store_does_not_hold_refuses(self) -> None:
        self.with_policy()

        status, payload = self.post(
            REQUESTS,
            self.issue_body(
                binding={
                    "workspace_uid": self.workspace_uid,
                    "task_uid": UPSTREAM_UID,
                    "task_id": "T-9999",
                    "task_revision": 0,
                }
            ),
        )

        self.assertEqual(status, 409)
        self.assertRefused(payload, "unknown_task")
        self.assertEqual(self.ledger()["requests"], [])

    def test_a_policy_change_between_issues_refuses_the_reissue(self) -> None:
        self.with_policy()
        status, first = self.post(REQUESTS, self.issue_body())
        self.assertEqual(status, 200, first)

        self.with_policy("notion-product")
        self.now = INSIDE
        status, payload = self.post(REQUESTS, self.issue_body())

        self.assertEqual(status, 400)
        self.assertRefused(payload, "corpus_not_granted")
        self.assertEqual(len(self.ledger()["requests"]), 1)

    def test_an_identical_retry_replays_and_a_changed_one_refuses(self) -> None:
        self.with_policy()
        status, first = self.post(REQUESTS, self.issue_body())
        self.assertEqual(status, 200, first)

        self.now = INSIDE
        status, replay = self.post(REQUESTS, self.issue_body())

        self.assertEqual(status, 200)
        self.assertEqual(replay["data"], first["data"])
        self.assertTrue(replay["meta"]["replayed"])

        status, changed = self.post(
            REQUESTS, self.issue_body(query="an entirely different question")
        )

        self.assertEqual(status, 409)
        self.assertRefused(changed, "request_digest_mismatch")
        records = self.ledger()["requests"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["request_id"], first["data"]["request_id"])

    def test_an_expired_retry_refuses_and_mints_no_second_identity(self) -> None:
        self.with_policy()
        status, first = self.post(REQUESTS, self.issue_body())
        self.assertEqual(status, 200, first)

        self.now = AFTER_EXPIRY
        status, expired = self.post(REQUESTS, self.issue_body())

        self.assertEqual(status, 409)
        self.assertRefused(expired, "request_expired")
        records = self.ledger()["requests"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["request_id"], first["data"]["request_id"])
        self.assertEqual(records[0]["expires_at"], first["data"]["expires_at"])

        # A lapsed window becomes live again only under a fresh intent the user
        # explicitly reviewed, never by renewing the old one.
        status, fresh = self.post(REQUESTS, self.issue_body(intent_id=OTHER_INTENT_ID))
        self.assertEqual(status, 200, fresh)
        self.assertNotEqual(fresh["data"]["request_id"], first["data"]["request_id"])
        self.assertEqual(fresh["data"]["requested_at"], AFTER_EXPIRY)

    def test_a_restart_reconstructs_the_original_receipt(self) -> None:
        self.with_policy()
        status, first = self.post(REQUESTS, self.issue_body())
        self.assertEqual(status, 200, first)

        self.restart()
        self.now = INSIDE
        status, replay = self.post(REQUESTS, self.issue_body())

        self.assertEqual(status, 200)
        self.assertEqual(replay["data"], first["data"])
        self.assertTrue(replay["meta"]["replayed"])
        self.assertEqual(len(self.ledger()["requests"]), 1)

    def test_two_concurrent_issues_of_one_intent_settle_on_one_authorization(self) -> None:
        self.with_policy()
        headers = self.owner_headers()
        started = threading.Barrier(2)
        answers: list[tuple[int, Any]] = []
        lock = threading.Lock()

        def issue() -> None:
            started.wait(timeout=5)
            answer = self.post(REQUESTS, self.issue_body(), headers)
            with lock:
                answers.append(answer)

        workers = [threading.Thread(target=issue) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertEqual(len(answers), 2)
        self.assertEqual([status for status, _ in answers], [200, 200])
        first, second = (payload for _, payload in answers)
        self.assertEqual(first["data"], second["data"])
        self.assertEqual(
            sorted(payload["meta"]["replayed"] for _, payload in answers),
            [False, True],
        )
        self.assertEqual(len(self.ledger()["requests"]), 1)


class CapacityIssueRouteTest(KnowledgeHttpCase):
    """Row and byte caps refuse through the existing closed 409 envelope."""

    def test_a_row_cap_issue_is_409_and_writes_nothing(self) -> None:
        self.with_policy()
        self.fill_row_cap()
        before = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes()

        status, payload = self.post(REQUESTS, self.issue_body())

        self.assertClosedCapacityRefusal(status, payload, "ledger_full", before)
        self.assertEqual(len(self.ledger()["requests"]), MAX_REQUESTS)
        self.assertEqual(payload["error"]["details"].get("field"), "requests")

    def test_a_byte_cap_issue_is_409_and_writes_nothing(self) -> None:
        self.with_policy()
        occupied = self.fill_until_typical_issue_exceeds_bytes()
        before = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes()

        status, payload = self.post(REQUESTS, self.issue_body())

        self.assertClosedCapacityRefusal(
            status, payload, "document_too_large", before
        )
        self.assertEqual(len(self.ledger()["requests"]), occupied)
        self.assertEqual(payload["error"]["details"].get("field"), "knowledge")


class QueryConfinementTest(KnowledgeHttpCase):
    def _persisted(self) -> dict[str, str]:
        """Every byte this store wrote, by path, including runtime receipts.

        Read after the server is closed, so the writer lease is released and no
        file is skipped: a scan that silently skipped a locked file would prove
        nothing about what is on disk.
        """

        self.stop()
        return {
            str(path.relative_to(self.root)): path.read_text(
                encoding="utf-8", errors="replace"
            )
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def test_the_query_reaches_the_response_and_never_the_disk(self) -> None:
        self.with_policy()
        task = self.open_task()

        status, issued = self.post(
            REQUESTS, self.issue_body(binding=self.task_binding(task))
        )
        self.assertEqual(status, 200, issued)
        # The response the UI copies really does carry the query it asked for.
        self.assertEqual(issued["data"]["query"], QUERY_CANARY)

        self.now = INSIDE
        status, replay = self.post(
            REQUESTS, self.issue_body(binding=self.task_binding(task))
        )
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay["data"]["query"], QUERY_CANARY)

        # A refusal under the same session must not leave it behind either.
        self.post(REQUESTS, self.issue_body(corpus_refs=["finance-drive"]))

        persisted = self._persisted()
        self.assertGreaterEqual(len(persisted), 11)
        self.assertIn(KNOWLEDGE_DOCUMENT_NAME, persisted)
        self.assertIn("activity.json", persisted)
        for name, body in persisted.items():
            with self.subTest(document=name):
                self.assertNotIn(QUERY_CANARY, body)
                self.assertNotIn("canary", body)
                self.assertNotIn('"query"', body)

    def test_the_ledger_record_is_a_digest_and_the_closed_key_set(self) -> None:
        self.with_policy()
        self.post(REQUESTS, self.issue_body())

        record = self.ledger()["requests"][0]

        self.assertEqual(
            set(record),
            {
                "request_id",
                "request_digest",
                "connection_alias",
                "binding",
                "corpus_refs",
                "policy_revision",
                "result_limit",
                "requested_at",
                "expires_at",
                "state",
                "capture_ids",
                "completion_digest",
                "completed_at",
            },
        )
        self.assertTrue(record["request_digest"].startswith("sha256:"))

    def test_no_issued_receipt_is_cached_in_the_activity_idempotency_log(self) -> None:
        self.with_policy()
        self.post(REQUESTS, self.issue_body())

        activity = json.loads(
            (self.root / "activity.json").read_text(encoding="utf-8")
        )

        self.assertEqual(activity["idempotency"], [])


class RouteAdmissionTest(KnowledgeHttpCase):
    def test_only_the_canonical_spelling_reaches_a_handler(self) -> None:
        """`urlparse` strips a trailing `;params` run; the route does not."""

        self.with_policy()
        before = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes()

        cases = (
            ("GET", CONNECTIONS + ";x", None),
            ("POST", CONNECTIONS + ";x", self.policy_body("notion-product")),
            ("POST", REQUESTS + ";x", self.issue_body()),
            ("POST", "/api/v1/knowledge/requests/extra", self.issue_body()),
            ("POST", "/api/v1/knowledge", self.issue_body()),
        )
        for method, path, body in cases:
            with self.subTest(path=path):
                if body is None:
                    status, payload = self.call("GET", path)
                else:
                    status, payload = self.post(path, body)
                self.assertEqual(status, 404, payload)
                self.assertRefused(payload, "not_found")

        self.assertEqual((self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes(), before)

    def test_the_released_admission_order_is_unchanged_on_a_knowledge_path(self) -> None:
        headers = self.owner_headers()
        body = json.dumps(self.issue_body()).encode("utf-8")
        cases = (
            # Host precedes everything, including the content type. It is sent
            # with an empty body on purpose: this refusal lands *before* the
            # body is drained, and Windows resets a connection closed with
            # unread request bytes still buffered.
            (dict(headers, Host="example.com"), b"", 400, "invalid_host"),
            (
                {key: value for key, value in headers.items() if key != "Content-Type"},
                body,
                415,
                "unsupported_media_type",
            ),
            (
                {key: value for key, value in headers.items() if key != "Origin"},
                body,
                403,
                "origin_required",
            ),
        )
        for candidate, payload_bytes, status, code in cases:
            with self.subTest(code=code):
                answered, payload = self.call("POST", REQUESTS, payload_bytes, candidate)
                self.assertEqual(answered, status, payload)
                self.assertRefused(payload, code)

    def test_the_knowledge_body_bound_is_tighter_and_borrows_no_other_budget(self) -> None:
        self.assertLess(KNOWLEDGE_BODY_LIMIT, CAPTURE_BODY_LIMIT)
        self.assertLess(CAPTURE_BODY_LIMIT, DEFAULT_BODY_LIMIT)

        oversized = b'{"query":"' + b"a" * (KNOWLEDGE_BODY_LIMIT + 1) + b'"}'
        status, payload = self.call("POST", REQUESTS, oversized, self.owner_headers())

        self.assertEqual(status, 413)
        self.assertRefused(payload, "body_too_large")

        # The same body on a default-limit route is not refused for its size,
        # so the tighter bound belongs to this surface and to nothing else.
        status, payload = self.call(
            "POST",
            "/api/v1/tasks",
            oversized,
            dict(self.owner_headers(), **{"Idempotency-Key": "task-key-00000001"}),
        )
        self.assertNotEqual(status, 413)


class BackendGuardTest(KnowledgeHttpCase):
    """Schema 4 stays explicitly unsupported rather than quietly improvised."""

    def _surface(self, store: Any) -> KnowledgeRequestsHttpMixin:
        surface = KnowledgeRequestsHttpMixin()
        surface.stack = SimpleNamespace(store=store)
        return surface

    def test_a_store_without_the_knowledge_document_is_refused_by_code(self) -> None:
        class NotTheCollectionStore:
            """Stands in for the experimental v4 store adapter."""

        with self.assertRaises(RequestError) as raised:
            self._surface(NotTheCollectionStore())._knowledge_store()

        self.assertEqual(raised.exception.code, "knowledge_backend_unsupported")
        self.assertEqual(raised.exception.status, 409)

    def test_the_collection_store_is_admitted(self) -> None:
        self.assertIs(self._surface(self.store)._knowledge_store(), self.store)


if __name__ == "__main__":
    unittest.main()
