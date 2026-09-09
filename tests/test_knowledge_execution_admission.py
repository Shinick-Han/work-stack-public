"""Admission of an already-issued KnowledgeRequest against a live schema-6 store.

Every store is a temporary directory this module created. Nothing opens a live
SSOT, reaches a network, or reads a wall clock. Policy and requests are issued
through the production issuer. Admission is the unit under test; there is no
executor, HTTP route, instance guard or child process here.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from workstack import knowledge_request_issuer as issuer
from workstack.knowledge_execution_admission import (
    AdmittedExecution,
    admit_execution_request,
)
from workstack.knowledge_ledger_document import (
    KNOWLEDGE_DOCUMENT_NAME,
    KnowledgeLedgerError,
)
from workstack.knowledge_owner_requests import (
    ConnectionPolicy,
    plan_ledger_stage_completion,
    request_digest,
)
from workstack.knowledge_request import KnowledgeRequestError
from workstack.service import WorkStack
from workstack.store import Store

UPSTREAM_UID = "66666666-6666-4666-8666-666666666666"
OTHER_UPSTREAM = "77777777-7777-4777-8777-777777777777"
INTENT_ID = "a1b2c3d4-1111-4111-8111-aaaaaaaaaaaa"
OTHER_INTENT = "b2c3d4e5-2222-4222-8222-bbbbbbbbbbbb"
QUERY = "rollback verification owner"
NOW = "2026-09-08T09:00:00Z"
INSIDE = "2026-09-08T09:02:00Z"
AFTER_EXPIRY = "2026-09-08T09:05:01Z"
POLICY = (
    ConnectionPolicy(
        alias="team-nas",
        upstream_workspace_uid=UPSTREAM_UID,
        corpus_refs=("nas-team-share", "notion-product"),
    ),
)


def _clock(value: str):
    return lambda: value


class AdmissionCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.workspace_uid = self.store.initialize().workspace_uid

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def tree(self) -> dict[str, bytes]:
        payload: dict[str, bytes] = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.suffix != ".lock":
                payload[path.relative_to(self.root).as_posix()] = path.read_bytes()
        return payload

    def with_policy(self) -> None:
        issuer.replace_connection_policy(
            self.store, expected_policy_revision=0, connections=POLICY
        )

    def body(self, **overrides: Any) -> dict[str, Any]:
        document = {
            "intent_id": INTENT_ID,
            "connection_alias": "team-nas",
            "binding": {"workspace_uid": self.workspace_uid},
            "query": QUERY,
            "corpus_refs": ["nas-team-share"],
            "purpose": "find_context",
            "result_limit": 3,
        }
        document.update(overrides)
        return document

    def issue(self, *, now: str = NOW, **overrides: Any):
        return issuer.issue_knowledge_request(
            self.store,
            issuer.parse_issue_body(self.body(**overrides)),
            clock=_clock(now),
        )

    def open_task(self) -> dict[str, Any]:
        WorkStack(self.store).add_task("Held task")
        return json.loads((self.root / "backlog.json").read_text(encoding="utf-8"))[
            "tasks"
        ][0]

    def refuse(
        self, document: Any, code: str, *, now: str = INSIDE
    ) -> KnowledgeLedgerError | KnowledgeRequestError:
        before = self.tree()
        with self.assertRaises((KnowledgeLedgerError, KnowledgeRequestError)) as raised:
            admit_execution_request(self.store, document, now=now)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.tree(), before)
        text = str(raised.exception)
        details = json.dumps(raised.exception.details)
        self.assertNotIn(QUERY, text)
        self.assertNotIn(QUERY, details)
        self.assertNotIn("team-nas", text)
        return raised.exception

    def complete(self, issued) -> None:
        with self.store.transaction():
            planned = plan_ledger_stage_completion(
                self.store.load(KNOWLEDGE_DOCUMENT_NAME),
                request_id=issued.document["request_id"],
                completion_digest="sha256:" + "ab" * 32,
                capture_ids=["C-0001"],
                workspace_uid=self.workspace_uid,
                now=INSIDE,
            )
            self.store.save_many({KNOWLEDGE_DOCUMENT_NAME: planned.document})


class HappyPathTest(AdmissionCase):
    def test_an_issued_pending_request_is_admitted_without_writing(self) -> None:
        self.with_policy()
        issued = self.issue()
        before = self.tree()

        admitted = admit_execution_request(self.store, issued.document, now=INSIDE)

        self.assertIsInstance(admitted, AdmittedExecution)
        self.assertEqual(admitted.document, issued.document)
        self.assertEqual(admitted.request_digest, request_digest(issued.document))
        self.assertEqual(admitted.connection_alias, "team-nas")
        self.assertEqual(admitted.policy_revision, 1)
        self.assertEqual(admitted.upstream_workspace_uid, UPSTREAM_UID)
        self.assertEqual(self.tree(), before)
        self.assertIsNot(admitted.document, issued.document)

    def test_a_task_bound_request_is_admitted_from_the_stored_binding(self) -> None:
        self.with_policy()
        task = self.open_task()
        issued = self.issue(
            binding={
                "workspace_uid": self.workspace_uid,
                "task_uid": task["uid"],
                "task_id": task["id"],
                "task_revision": task["revision"],
            }
        )
        hostile = copy.deepcopy(issued.document)
        hostile["binding"] = {"workspace_uid": self.workspace_uid}

        admitted = admit_execution_request(self.store, issued.document, now=INSIDE)

        self.assertEqual(admitted.document["binding"]["task_id"], task["id"].upper())
        self.refuse(hostile, "task_binding_required")

    def test_canonical_projection_not_raw_json_spelling(self) -> None:
        self.with_policy()
        issued = self.issue()
        scrambled = {
            key: issued.document[key] for key in reversed(list(issued.document))
        }
        scrambled["query"] = "  " + QUERY + "  "

        admitted = admit_execution_request(self.store, scrambled, now=INSIDE)

        self.assertEqual(admitted.document["query"], QUERY)
        self.assertEqual(admitted.document, issued.document)
        self.assertEqual(admitted.request_digest, request_digest(issued.document))

    def test_the_result_is_detached_from_the_caller_and_the_store(self) -> None:
        self.with_policy()
        issued = self.issue()
        caller = copy.deepcopy(issued.document)
        admitted = admit_execution_request(self.store, caller, now=INSIDE)
        caller["query"] = "mutated by caller"
        admitted.document["query"] = "mutated result"

        again = admit_execution_request(self.store, issued.document, now=INSIDE)
        self.assertEqual(again.document["query"], QUERY)
        raw = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_text(encoding="utf-8")
        self.assertNotIn("mutated", raw)
        self.assertNotIn("query", raw)

    def test_a_nested_transaction_composes_without_deadlock(self) -> None:
        self.with_policy()
        issued = self.issue()
        with self.store.transaction():
            first = admit_execution_request(self.store, issued.document, now=INSIDE)
            second = admit_execution_request(self.store, issued.document, now=INSIDE)
            self.store.load(KNOWLEDGE_DOCUMENT_NAME)
        third = admit_execution_request(self.store, issued.document, now=INSIDE)
        self.assertEqual(first.document, second.document)
        self.assertEqual(second.document, third.document)


class RefusalTest(AdmissionCase):
    def test_an_unknown_request_is_refused_and_not_issued(self) -> None:
        self.with_policy()
        issued = self.issue()
        missing = copy.deepcopy(issued.document)
        missing["request_id"] = OTHER_INTENT

        self.refuse(missing, "unknown_request")
        self.assertEqual(
            json.loads((self.root / KNOWLEDGE_DOCUMENT_NAME).read_text(encoding="utf-8"))[
                "requests"
            ][0]["request_id"],
            issued.document["request_id"],
        )

    def test_a_foreign_workspace_binding_is_refused(self) -> None:
        self.with_policy()
        issued = self.issue()
        foreign = copy.deepcopy(issued.document)
        foreign["binding"] = {"workspace_uid": UPSTREAM_UID}
        self.refuse(foreign, "workspace_mismatch")

    def test_a_changed_query_is_a_digest_mismatch(self) -> None:
        self.with_policy()
        issued = self.issue()
        changed = copy.deepcopy(issued.document)
        changed["query"] = "a different question entirely"
        error = self.refuse(changed, "request_digest_mismatch")
        self.assertEqual(error.details, {"field": "request_id"})

    def test_a_policy_change_refuses_before_any_executor(self) -> None:
        self.with_policy()
        issued = self.issue()
        issuer.replace_connection_policy(
            self.store,
            expected_policy_revision=1,
            connections=(
                ConnectionPolicy(
                    alias="team-nas",
                    upstream_workspace_uid=UPSTREAM_UID,
                    corpus_refs=("notion-product",),
                ),
            ),
        )
        self.refuse(issued.document, "policy_revision_changed")

    def test_a_revoked_connection_is_policy_revision_changed(self) -> None:
        self.with_policy()
        issued = self.issue()
        issuer.replace_connection_policy(
            self.store,
            expected_policy_revision=1,
            connections=(
                ConnectionPolicy(
                    alias="other-nas",
                    upstream_workspace_uid=OTHER_UPSTREAM,
                    corpus_refs=("nas-team-share",),
                ),
            ),
        )
        self.refuse(issued.document, "policy_revision_changed")

    def test_a_completed_request_is_not_pending(self) -> None:
        self.with_policy()
        issued = self.issue()
        self.complete(issued)
        self.refuse(issued.document, "request_not_pending")

    def test_an_expired_window_is_refused_by_the_wire_validator(self) -> None:
        self.with_policy()
        issued = self.issue()
        self.refuse(issued.document, "request_expired", now=AFTER_EXPIRY)

    def test_a_stale_task_revision_is_refused(self) -> None:
        self.with_policy()
        task = self.open_task()
        issued = self.issue(
            binding={
                "workspace_uid": self.workspace_uid,
                "task_uid": task["uid"],
                "task_id": task["id"],
                "task_revision": task["revision"],
            }
        )
        with self.store.transaction():
            backlog = self.store.load("backlog.json")
            for entry in backlog["tasks"]:
                if entry["id"] == task["id"]:
                    entry["revision"] = int(entry["revision"]) + 1
            self.store.save_many({"backlog.json": backlog})
        self.refuse(issued.document, "task_binding_mismatch")

    def test_unknown_keys_are_refused_by_the_wire_validator(self) -> None:
        self.with_policy()
        issued = self.issue()
        extra = copy.deepcopy(issued.document)
        extra["provider"] = "not-a-grant"
        error = self.refuse(extra, "unknown_field")
        self.assertIsInstance(error, KnowledgeRequestError)
        self.assertEqual(error.details, {"field": "request"})
