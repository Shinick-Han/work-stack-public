"""The manual knowledge Capture import route, through a real local server.

Every server here binds ``127.0.0.1:0`` over a store this test created in its
own temporary directory, and every fact the import stands on is established
through the *released* owner routes: the connection policy is written through
``POST /api/v1/knowledge/connections`` and the request is issued through
``POST /api/v1/knowledge/requests``. Nothing opens a real home directory, a
live SSOT, a model, a credential or a network beyond that loopback socket, and
the clock is injected so expiry is a fact of the test.

The suite is organised by what the boundary has to get right:

* who may reach it at all, and with what body (``ImportAdmissionTest``),
* the whole user path end to end, across a restart (``ImportFlowTest``),
* what a moved world and a malformed batch refuse (``ImportRefusalTest``),
* what a retry means on the wire (``ImportRetryTest``),
* and that no envelope content becomes durable state (``ImportConfinementTest``).
"""

from __future__ import annotations

import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from workstack.knowledge_captures_http import (
    IMPORT_BODY_LIMIT,
    IMPORT_PATH,
    KnowledgeCapturesHttpMixin,
)
from workstack.knowledge_ledger_document import KNOWLEDGE_DOCUMENT_NAME
from workstack.knowledge_requests_http import KNOWLEDGE_BODY_LIMIT
from workstack.server import CAPTURE_BODY_LIMIT, DEFAULT_BODY_LIMIT, create_server
from workstack.server_errors import RequestError
from workstack.service import WorkStack
from workstack.store import Store, StoreCorruptError

UPSTREAM_UID = "66666666-6666-4666-8666-666666666666"
INTENT_ID = "a1b2c3d4-1111-4111-8111-aaaaaaaaaaaa"
ITEM_ONE = "11111111-1111-4111-8111-111111111111"
ITEM_TWO = "22222222-2222-4222-8222-222222222222"

ISSUED_AT = "2026-09-08T09:00:00Z"
INSIDE = "2026-09-08T09:02:00Z"
LATER_INSIDE = "2026-09-08T09:03:00Z"
AFTER_EXPIRY = "2026-09-08T09:05:01Z"

CONNECTIONS = "/api/v1/knowledge/connections"
REQUESTS = "/api/v1/knowledge/requests"
IMPORT = "/api/v1/knowledge/captures/import"

# Strings that exist nowhere else in the repository or the store.
QUERY_CANARY = "canary 9f2c1d7a unique retrieval probe phrase"
SUMMARY_CANARY = "canary 4b81ff02 imported answer summary phrase"
EVIDENCE_TITLE_CANARY = "canary 71ac9de4 imported evidence display title"


class ImportHttpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = ISSUED_AT
        self.start()

    def tearDown(self) -> None:
        self.stop()
        self.temporary.cleanup()

    # -- the server ------------------------------------------------------

    def start(self) -> None:
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.server.knowledge_clock = lambda: self.now
        self.port = self.server.actual_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.running = True
        self.workspace_uid = self.document("workspace.json")["id"]

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
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
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

    def document(self, name: str) -> dict[str, Any]:
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    # -- the world, built through the released owner routes ---------------

    def with_policy(self, *corpus_refs: str) -> None:
        status, payload = self.post(
            CONNECTIONS,
            {
                "expected_policy_revision": self.document(KNOWLEDGE_DOCUMENT_NAME)[
                    "policy_revision"
                ],
                "connections": [
                    {
                        "alias": "team-nas",
                        "upstream_workspace_uid": UPSTREAM_UID,
                        "corpus_refs": list(corpus_refs) or ["nas-team-share"],
                    }
                ],
            },
        )
        self.assertEqual(status, 200, payload)

    def issue(self, *, result_limit: int = 3, task: dict[str, Any] | None = None) -> str:
        binding: dict[str, Any] = {"workspace_uid": self.workspace_uid}
        if task is not None:
            binding = {
                "workspace_uid": self.workspace_uid,
                "task_uid": task["uid"],
                "task_id": task["id"],
                "task_revision": task["revision"],
            }
        status, payload = self.post(
            REQUESTS,
            {
                "intent_id": INTENT_ID,
                "connection_alias": "team-nas",
                "binding": binding,
                "query": QUERY_CANARY,
                "corpus_refs": ["nas-team-share"],
                "purpose": "find_context",
                "result_limit": result_limit,
            },
        )
        self.assertEqual(status, 200, payload)
        return payload["data"]["request_id"]

    def ready(self, **issue_kwargs: Any) -> str:
        self.with_policy()
        request_id = self.issue(**issue_kwargs)
        self.now = INSIDE
        return request_id

    def open_task(self) -> dict[str, Any]:
        self.stack.add_task("Held task")
        return self.document("backlog.json")["tasks"][0]

    # -- the envelope ----------------------------------------------------

    def item(
        self, request_id: str, *, item_id: str = ITEM_ONE, index: int = 1
    ) -> dict[str, Any]:
        return {
            "item_id": item_id,
            "title": "Rollback verification owner",
            "normalized": {
                "summary": SUMMARY_CANARY,
                "context": "Carried out of band by the owner.",
                "action_items": [{"title": "Confirm the rollback owner"}],
                "tags": ["rollback"],
            },
            "retrieval": {
                "schema": "workstack.capture-retrieval.v1.1",
                "capture_schema_version": "1.1",
                "request_id": request_id,
                "query_id": "engine-q-000{}".format(index),
                "answer_scope": "single_source",
                "confidence": {"level": "medium", "score": 0.62},
                "evidence": [
                    {
                        "source_type": "nas.file",
                        "title": EVIDENCE_TITLE_CANARY,
                        "document_ref": "nas-doc-000{}abcd".format(index),
                        "chunk_ref": "chunk-000{}abcd".format(index),
                        "source_version": "nas-version-1{}".format(index),
                        "indexed_digest": "sha256:" + "a" * 64,
                        "web_url": None,
                    }
                ],
                "truncated": False,
            },
        }

    def envelope(self, request_id: str, items: list[Any] | None = None) -> dict[str, Any]:
        return {
            "schema": "workstack.knowledge-import.v1",
            "request_id": request_id,
            "items": [self.item(request_id)] if items is None else items,
        }

    def two_items(self, request_id: str) -> list[dict[str, Any]]:
        return [
            self.item(request_id, item_id=ITEM_ONE, index=1),
            self.item(request_id, item_id=ITEM_TWO, index=2),
        ]

    def assertRefused(self, payload: Any, code: str) -> None:
        self.assertEqual(payload["error"]["code"], code, payload)
        self.assertEqual(payload["error"]["details"].keys() - {"field"}, set())
        for canary in (QUERY_CANARY, SUMMARY_CANARY, EVIDENCE_TITLE_CANARY):
            self.assertNotIn(canary, json.dumps(payload))


class ImportAdmissionTest(ImportHttpCase):
    def test_a_bearer_token_alone_reaches_nothing(self) -> None:
        """Capture ingestion authority is not owner import authority."""

        request_id = self.ready()
        before = self.document("captures.json")
        for token in ("not-a-real-token", self.server.capture_token):
            with self.subTest(token="real" if token != "not-a-real-token" else "fake"):
                status, payload = self.call(
                    "POST",
                    IMPORT,
                    json.dumps(self.envelope(request_id)).encode("utf-8"),
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer {}".format(token),
                    },
                )
                self.assertIn(status, (401, 403))
        self.assertEqual(self.document("captures.json"), before)
        self.assertEqual(
            self.document(KNOWLEDGE_DOCUMENT_NAME)["requests"][0]["state"], "pending"
        )

    def test_a_cross_origin_page_and_a_bad_csrf_token_are_refused(self) -> None:
        request_id = self.ready()
        body = json.dumps(self.envelope(request_id)).encode("utf-8")
        headers = self.owner_headers()
        cases = (
            ({k: v for k, v in headers.items() if k != "Origin"}, 403, "origin_required"),
            (dict(headers, Origin="http://evil.invalid"), 403, "invalid_origin"),
            (dict(headers, **{"X-WorkStack-CSRF": "wrong"}), 403, "invalid_csrf"),
        )
        for candidate, status, code in cases:
            with self.subTest(code=code):
                answered, payload = self.call("POST", IMPORT, body, candidate)
                self.assertEqual(answered, status, payload)
                self.assertRefused(payload, code)
        self.assertEqual(
            self.document(KNOWLEDGE_DOCUMENT_NAME)["requests"][0]["state"], "pending"
        )

    def test_an_idempotency_key_is_refused_as_the_issuer_refuses_one(self) -> None:
        request_id = self.ready()
        status, payload = self.call(
            "POST",
            IMPORT,
            json.dumps(self.envelope(request_id)).encode("utf-8"),
            dict(self.owner_headers(), **{"Idempotency-Key": "import-key-0001"}),
        )
        self.assertEqual(status, 400)
        self.assertRefused(payload, "unsupported_idempotency_key")
        self.assertEqual(self.document("activity.json")["idempotency"], [])

    def test_the_import_bound_is_measured_on_the_whole_raw_request(self) -> None:
        """64 KiB for the whole request, not per item, and only on this path."""

        self.assertEqual(IMPORT_BODY_LIMIT, CAPTURE_BODY_LIMIT)
        self.assertLess(KNOWLEDGE_BODY_LIMIT, IMPORT_BODY_LIMIT)
        self.assertLess(IMPORT_BODY_LIMIT, DEFAULT_BODY_LIMIT)
        self.ready()

        # Raw wire bytes: one octet over the bound is refused before any
        # handler, and the same bytes are inside the bound one octet under.
        oversized = b'{"schema":"' + b"a" * (IMPORT_BODY_LIMIT + 1) + b'"}'
        status, payload = self.call("POST", IMPORT, oversized, self.owner_headers())
        self.assertEqual(status, 413)
        self.assertRefused(payload, "body_too_large")

        inside = b'{"schema":"' + b"a" * (KNOWLEDGE_BODY_LIMIT + 1) + b'"}'
        status, payload = self.call("POST", IMPORT, inside, self.owner_headers())
        self.assertEqual(status, 400)
        self.assertNotEqual(payload["error"]["code"], "body_too_large")

        # The two released knowledge routes keep the tighter bound. The import
        # budget is not borrowed by them and does not widen them.
        status, payload = self.call("POST", REQUESTS, inside, self.owner_headers())
        self.assertEqual(status, 413)
        self.assertRefused(payload, "body_too_large")

    def test_a_path_that_only_resolves_to_the_route_is_not_the_route(self) -> None:
        request_id = self.ready()
        status, payload = self.call(
            "POST",
            IMPORT + ";x",
            json.dumps(self.envelope(request_id)).encode("utf-8"),
            self.owner_headers(),
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_the_v4_backend_is_refused_by_code(self) -> None:
        surface = KnowledgeCapturesHttpMixin()
        # Stands in for the experimental v4 store adapter, which has no
        # knowledge document.
        surface.stack = SimpleNamespace(store=object())
        with self.assertRaises(RequestError) as raised:
            surface._knowledge_store()
        self.assertEqual(raised.exception.code, "knowledge_backend_unsupported")
        self.assertEqual(raised.exception.status, 409)


class ImportFlowTest(ImportHttpCase):
    def test_the_whole_owner_path_end_to_end(self) -> None:
        """Policy, issue, import, restart, read, and an explicit conversion."""

        request_id = self.ready()
        status, payload = self.post(IMPORT, self.envelope(request_id, self.two_items(request_id)))

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["request_id"], request_id)
        self.assertEqual(payload["data"]["capture_ids"], ["C-0001", "C-0002"])
        self.assertEqual(payload["data"]["completed_at"], INSIDE)
        self.assertEqual(payload["meta"], {"replayed": False, "imported_count": 2})

        # The document on disk is the only state.
        self.restart()

        status, listed = self.call("GET", "/api/v1/captures")
        self.assertEqual(status, 200, listed)
        captures = listed["data"]
        self.assertEqual([capture["id"] for capture in captures], ["C-0001", "C-0002"])
        for capture in captures:
            self.assertEqual(capture["schema_version"], "1.1")
            self.assertEqual(capture["status"], "inbox")
            self.assertEqual(capture["linked_task_ids"], [])
            self.assertEqual(capture["task_hints"], [])
            retrieval = capture["retrieval"]
            # The evidence is readable; the origin is not attested, and says so.
            self.assertEqual(len(retrieval["evidence"]), 1)
            self.assertIsNone(retrieval["origin"])
            self.assertEqual(retrieval["origin_state"], "reported_unverified")
            self.assertEqual(retrieval["capture_source_type"], "knowledge.answer")
            self.assertEqual(
                retrieval["evidence"][0]["version_state"], "reported_unverified"
            )
            self.assertIsNone(retrieval["evidence"][0]["web_url"])

        # No Task was created or touched by the import itself.
        self.assertEqual(self.document("backlog.json")["tasks"], [])

        # An explicit user action is what turns evidence into work.
        action_id = captures[0]["normalized"]["action_items"][0]["id"]
        status, created = self.call(
            "POST",
            "/api/v1/captures/C-0001/actions/{}/task".format(action_id),
            b"{}",
            dict(self.owner_headers(), **{"Idempotency-Key": "convert-key-0001"}),
        )
        self.assertEqual(status, 201, created)
        self.assertEqual(len(self.document("backlog.json")["tasks"]), 1)
        status, after = self.call("GET", "/api/v1/captures?status=converted")
        self.assertEqual([capture["id"] for capture in after["data"]], ["C-0001"])


class ImportRefusalTest(ImportHttpCase):
    def refuse(self, envelope: Any, status: int, code: str) -> None:
        before = (
            self.document(KNOWLEDGE_DOCUMENT_NAME),
            self.document("captures.json"),
            self.document("activity.json"),
        )
        answered, payload = self.post(IMPORT, envelope)
        self.assertEqual(answered, status, payload)
        self.assertRefused(payload, code)
        self.assertEqual(
            (
                self.document(KNOWLEDGE_DOCUMENT_NAME),
                self.document("captures.json"),
                self.document("activity.json"),
            ),
            before,
        )

    def test_a_malformed_retrieval_extension_refuses_the_whole_batch(self) -> None:
        request_id = self.ready()
        items = self.two_items(request_id)
        items[1]["retrieval"]["evidence"][0]["web_url"] = "https://example.invalid/x"
        self.refuse(self.envelope(request_id, items), 400, "web_url_not_allowed")

    def test_an_envelope_that_claims_a_provider_is_refused(self) -> None:
        request_id = self.ready()
        envelope = self.envelope(request_id)
        envelope["items"][0]["provider"] = "opendocuments"
        self.refuse(envelope, 400, "unknown_field")

    def test_an_unissued_request_refuses(self) -> None:
        self.ready()
        self.refuse(
            self.envelope("99999999-9999-4999-8999-999999999999"), 409, "unknown_request"
        )

    def test_a_changed_policy_refuses(self) -> None:
        request_id = self.ready()
        self.with_policy("nas-team-share", "notion-product")
        self.refuse(self.envelope(request_id), 409, "policy_revision_changed")

    def test_a_moved_task_refuses(self) -> None:
        task = self.open_task()
        self.with_policy()
        request_id = self.issue(task=task)
        self.now = INSIDE
        self.stack.patch_task(task["id"], {"revision": task["revision"], "title": "Moved"})
        self.refuse(self.envelope(request_id), 409, "task_binding_mismatch")

    def test_an_expired_request_refuses(self) -> None:
        request_id = self.ready()
        self.now = AFTER_EXPIRY
        self.refuse(self.envelope(request_id), 409, "request_expired")

    def test_a_batch_wider_than_the_issued_limit_refuses(self) -> None:
        self.with_policy()
        request_id = self.issue(result_limit=1)
        self.now = INSIDE
        self.refuse(
            self.envelope(request_id, self.two_items(request_id)),
            409,
            "result_limit_exceeded",
        )

    def test_a_malformed_stored_projection_refuses_the_read(self) -> None:
        """Corrupt evidence is never read back to a client as metadata."""

        request_id = self.ready()
        status, _ = self.post(IMPORT, self.envelope(request_id))
        self.assertEqual(status, 200)
        self.stop()
        captures = self.document("captures.json")
        captures["captures"][0]["retrieval"]["evidence"][0]["source_type"] = "unknown"
        (self.root / "captures.json").write_text(
            json.dumps(captures), encoding="utf-8"
        )
        # A restart meets the corruption where the store is opened, and the
        # record is never projected to a client as trusted metadata.
        with self.assertRaises(StoreCorruptError):
            Store(self.root).initialize()
        with self.assertRaises(StoreCorruptError):
            WorkStack(Store(self.root), initialize=False).list_captures()


class ImportRetryTest(ImportHttpCase):
    def test_the_same_envelope_replays_the_original_capture_identifiers(self) -> None:
        """A lost response is retried with the SAME envelope, not a new one."""

        request_id = self.ready()
        envelope = self.envelope(request_id, self.two_items(request_id))
        status, first = self.post(IMPORT, envelope)
        self.assertEqual(status, 200, first)
        before = self.document("activity.json")

        self.now = LATER_INSIDE
        status, second = self.post(IMPORT, copy.deepcopy(envelope))

        self.assertEqual(status, 200, second)
        self.assertEqual(second["data"], first["data"])
        self.assertEqual(second["meta"], {"replayed": True, "imported_count": 2})
        self.assertEqual(self.document("activity.json"), before)
        self.assertEqual(len(self.document("captures.json")["captures"]), 2)

    def test_a_retry_after_expiry_still_replays(self) -> None:
        request_id = self.ready()
        envelope = self.envelope(request_id)
        status, first = self.post(IMPORT, envelope)
        self.assertEqual(status, 200, first)
        self.now = AFTER_EXPIRY
        status, replayed = self.post(IMPORT, copy.deepcopy(envelope))
        self.assertEqual(status, 200, replayed)
        self.assertTrue(replayed["meta"]["replayed"])
        self.assertEqual(replayed["data"], first["data"])

    def test_a_reordered_retry_is_a_conflict_not_an_equivalence(self) -> None:
        request_id = self.ready()
        items = self.two_items(request_id)
        status, _ = self.post(IMPORT, self.envelope(request_id, items))
        self.assertEqual(status, 200)
        self.now = LATER_INSIDE
        status, payload = self.post(
            IMPORT, self.envelope(request_id, list(reversed(copy.deepcopy(items))))
        )
        self.assertEqual(status, 409)
        self.assertRefused(payload, "completion_digest_mismatch")
        self.assertEqual(len(self.document("captures.json")["captures"]), 2)

    def test_two_concurrent_imports_of_one_request_settle_once(self) -> None:
        request_id = self.ready()
        envelope = self.envelope(request_id, self.two_items(request_id))
        headers = self.owner_headers()
        body = json.dumps(envelope).encode("utf-8")
        answers: list[tuple[int, Any]] = []
        barrier = threading.Barrier(2)

        def attempt() -> None:
            barrier.wait()
            answers.append(self.call("POST", IMPORT, body, dict(headers)))

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual([status for status, _ in answers], [200, 200])
        self.assertEqual(
            {payload["meta"]["replayed"] for _, payload in answers}, {False, True}
        )
        self.assertEqual(
            {tuple(payload["data"]["capture_ids"]) for _, payload in answers},
            {("C-0001", "C-0002")},
        )
        self.assertEqual(len(self.document("captures.json")["captures"]), 2)


class ImportConfinementTest(ImportHttpCase):
    def test_no_envelope_content_becomes_durable_state_outside_the_capture(
        self,
    ) -> None:
        request_id = self.ready()
        status, payload = self.post(IMPORT, self.envelope(request_id))
        self.assertEqual(status, 200, payload)

        for name in (KNOWLEDGE_DOCUMENT_NAME, "activity.json", "backlog.json"):
            body = (self.root / name).read_text(encoding="utf-8")
            with self.subTest(document=name):
                self.assertNotIn(QUERY_CANARY, body)
                self.assertNotIn(SUMMARY_CANARY, body)
                self.assertNotIn(EVIDENCE_TITLE_CANARY, body)

        # The response names identities and counts, and no content at all.
        rendered = json.dumps(payload)
        self.assertNotIn(SUMMARY_CANARY, rendered)
        self.assertNotIn(EVIDENCE_TITLE_CANARY, rendered)

        # The allowed sanitized summary is still readable where it belongs.
        status, listed = self.call("GET", "/api/v1/captures")
        self.assertEqual(status, 200)
        capture = listed["data"][0]
        self.assertEqual(capture["normalized"]["summary"], SUMMARY_CANARY)
        self.assertEqual(
            capture["retrieval"]["evidence"][0]["title"], EVIDENCE_TITLE_CANARY
        )

    def test_the_route_writes_no_extra_durable_document(self) -> None:
        request_id = self.ready()
        before = sorted(path.name for path in self.root.iterdir())
        status, _ = self.post(IMPORT, self.envelope(request_id))
        self.assertEqual(status, 200)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)


if __name__ == "__main__":  # pragma: no cover - runner convenience
    unittest.main()
