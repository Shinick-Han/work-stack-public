"""Admission of a stored Capture for a transient source verification.

Every store is a temporary directory this module created. Policy, issue and
import go through the released owner operations; admission is the unit under
test. Nothing opens a live SSOT, reaches a network, or supplies a
caller-invented Capture or ledger record.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from workstack.capture import SHA256_RE, canonical_digest, fingerprint_for, source_key_for
from workstack.knowledge_capture_import import import_knowledge_captures
from workstack.knowledge_capture_packets import KnowledgeImportError
from workstack.knowledge_ledger_document import (
    KNOWLEDGE_DOCUMENT_NAME,
    KnowledgeLedgerError,
)
from workstack.knowledge_owner_requests import ConnectionPolicy, set_owner_connection_policy
from workstack.knowledge_request import KnowledgeRequestError
from workstack.knowledge_request_issuer import (
    IssueIntent,
    issue_knowledge_request,
    replace_connection_policy,
)
from workstack.knowledge_verification_admission import (
    AdmittedVerification,
    KnowledgeVerificationAdmissionError,
    admit_capture_verification,
)
from workstack.knowledge_verification_protocol import VerificationError
from workstack.service import WorkStack
from workstack.store import Store

UPSTREAM_UID = "66666666-6666-4666-8666-666666666666"
OTHER_UPSTREAM = "77777777-7777-4777-8777-777777777777"
INTENT_ID = "a1b2c3d4-1111-4111-8111-aaaaaaaaaaaa"
ITEM_ONE = "11111111-1111-4111-8111-111111111111"
ITEM_TWO = "22222222-2222-4222-8222-222222222222"
VERIFICATION_ID = "c0ffeeee-1111-4111-8111-aaaaaaaaaaaa"
OTHER_VERIFICATION = "d1ffeeee-2222-4222-8222-bbbbbbbbbbbb"
MISSING_REQUEST = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

QUERY = "rollback verification owner"
SUMMARY_CANARY = "canary 4b81ff02 imported answer summary phrase"
TITLE_CANARY = "canary 71ac9de4 imported evidence display title"
PATH_CANARY = r"Z:\PilotShare\sample-handbook.md"
SECRET_CANARY = "api_key: AKIA1234567890ABCDEF client_secret: s3cr3tv4lue0000"

ISSUED_AT = "2026-09-08T09:00:00Z"
INSIDE = "2026-09-08T09:02:00Z"
AFTER_EXPIRY = "2026-09-08T09:10:00Z"
OBSERVATION_END = "2026-09-08T09:11:00Z"

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "workstack"
    / "knowledge_verification_admission.py"
)
MAX_FUNCTION_LINES = 100
MAX_FILE_LINES = 800
MAX_CCN = 15

CLOSED = (
    KnowledgeVerificationAdmissionError,
    KnowledgeLedgerError,
    KnowledgeImportError,
    KnowledgeRequestError,
    VerificationError,
)
DOCUMENT_KEYS = frozenset(
    {
        "schema",
        "verification_id",
        "binding",
        "connection",
        "corpus_refs",
        "evidence",
        "requested_at",
        "expires_at",
    }
)
EVIDENCE_KEYS = frozenset({"document_ref", "source_type", "expected_source_version"})


def complexity(function: ast.AST) -> int:
    score = 1
    for node in ast.walk(function):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.Assert)):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += max(0, len(node.values) - 1)
        elif isinstance(node, ast.Try):
            score += len(node.handlers) + int(bool(node.orelse))
        elif isinstance(node, ast.Match):
            score += max(0, len(node.cases) - 1)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            score += sum(1 + len(generator.ifs) for generator in node.generators)
    return score


def leak_surface(error: BaseException) -> str:
    details = getattr(error, "details", {})
    return "".join(
        (getattr(error, "code", ""), str(error), repr(error), json.dumps(details))
    )


class AdmissionCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.workspace_uid = self.document("workspace.json")["id"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def document(self, name: str) -> dict[str, Any]:
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def tree(self) -> dict[str, bytes]:
        payload: dict[str, bytes] = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.suffix != ".lock":
                payload[path.relative_to(self.root).as_posix()] = path.read_bytes()
        return payload

    def with_policy(self, *corpus_refs: str, upstream: str = UPSTREAM_UID) -> None:
        set_owner_connection_policy(
            self.store,
            (
                ConnectionPolicy(
                    alias="team-nas",
                    upstream_workspace_uid=upstream,
                    corpus_refs=tuple(corpus_refs) or ("nas-team-share", "notion-product"),
                ),
            ),
        )

    def open_task(self) -> dict[str, Any]:
        self.stack.add_task("Held task")
        return self.document("backlog.json")["tasks"][0]

    def issue(
        self,
        *,
        result_limit: int = 3,
        task: dict[str, Any] | None = None,
        corpus_refs: list[str] | None = None,
        intent_id: str = INTENT_ID,
    ) -> str:
        binding: dict[str, Any] = {"workspace_uid": self.workspace_uid}
        if task is not None:
            binding = {
                "workspace_uid": self.workspace_uid,
                "task_uid": task["uid"],
                "task_id": task["id"],
                "task_revision": task["revision"],
            }
        intent = IssueIntent(
            intent_id=intent_id,
            connection_alias="team-nas",
            binding=binding,
            query=QUERY,
            corpus_refs=list(corpus_refs or ["nas-team-share"]),
            purpose="find_context",
            result_limit=result_limit,
        )
        issued = issue_knowledge_request(self.store, intent, clock=lambda: ISSUED_AT)
        return issued.document["request_id"]

    def retrieval(self, request_id: str, *, index: int = 1, **overrides: Any) -> dict[str, Any]:
        wire = {
            "schema": "workstack.capture-retrieval.v1.1",
            "capture_schema_version": "1.1",
            "request_id": request_id,
            "query_id": "engine-q-000{}".format(index),
            "answer_scope": "single_source",
            "confidence": {"level": "medium", "score": 0.62},
            "evidence": [
                {
                    "source_type": "nas.file",
                    "title": TITLE_CANARY,
                    "document_ref": "nas-doc-000{}abcd".format(index),
                    "chunk_ref": "chunk-000{}abcd".format(index),
                    "source_version": "nas-version-1{}".format(index),
                    "indexed_digest": "sha256:" + "a" * 64,
                    "web_url": None,
                }
            ],
            "truncated": False,
        }
        wire.update(overrides)
        return wire

    def item(
        self, request_id: str, *, item_id: str = ITEM_ONE, index: int = 1, **overrides: Any
    ) -> dict[str, Any]:
        entry = {
            "item_id": item_id,
            "title": "Rollback verification owner",
            "normalized": {
                "summary": SUMMARY_CANARY,
                "context": "Carried out of band by the owner.",
                "action_items": [{"title": "Confirm the rollback owner"}],
                "tags": ["rollback"],
            },
            "retrieval": self.retrieval(request_id, index=index),
        }
        entry.update(overrides)
        return entry

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

    def imported(self, items: list[Any] | None = None, **issue_kwargs: Any) -> dict[str, Any]:
        self.with_policy()
        request_id = self.issue(**issue_kwargs)
        import_knowledge_captures(
            self.store, self.envelope(request_id, items), now=INSIDE
        )
        return self.stored_capture("C-0001")

    def stored_capture(self, capture_id: str) -> dict[str, Any]:
        for entry in self.document("captures.json")["captures"]:
            if entry["id"] == capture_id:
                return entry
        raise AssertionError("missing capture {}".format(capture_id))

    def admit(
        self,
        capture: dict[str, Any],
        *,
        revision: int | None = None,
        verification_id: str = VERIFICATION_ID,
        now: str = AFTER_EXPIRY,
        capture_id: str | None = None,
    ) -> AdmittedVerification:
        return admit_capture_verification(
            self.store,
            capture["id"] if capture_id is None else capture_id,
            capture["revision"] if revision is None else revision,
            verification_id=verification_id,
            now=now,
        )

    def refuse(
        self,
        capture: dict[str, Any],
        code: str,
        *,
        revision: Any = None,
        capture_id: Any = None,
        verification_id: str = VERIFICATION_ID,
        now: str = AFTER_EXPIRY,
    ) -> BaseException:
        before = self.tree()
        with self.assertRaises(CLOSED) as raised:
            admit_capture_verification(
                self.store,
                capture["id"] if capture_id is None else capture_id,
                capture["revision"] if revision is None else revision,
                verification_id=verification_id,
                now=now,
            )
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.tree(), before)
        surface = leak_surface(raised.exception)
        for canary in (QUERY, SUMMARY_CANARY, TITLE_CANARY, PATH_CANARY, SECRET_CANARY):
            self.assertNotIn(canary, surface)
        return raised.exception

    def rewrite_request(self, **fields: Any) -> None:
        with self.store.transaction():
            ledger = self.store.load(KNOWLEDGE_DOCUMENT_NAME)
            ledger["requests"][0].update(fields)
            self.store.save_many({KNOWLEDGE_DOCUMENT_NAME: ledger})

    def rewrite_capture(self, capture_id: str, mutate) -> None:
        with self.store.transaction():
            captures = self.store.load("captures.json")
            for entry in captures["captures"]:
                if entry["id"] == capture_id:
                    mutate(entry)
                    break
            else:
                raise AssertionError("missing capture {}".format(capture_id))
            self.store.save_many({"captures.json": captures})

    def bound_digest(self, capture: dict[str, Any]) -> str:
        ledger = self.document(KNOWLEDGE_DOCUMENT_NAME)
        record = ledger["requests"][0]
        connection = ledger["connections"][0]
        evidence = [
            {
                "document_ref": item["document_ref"],
                "source_type": item["source_type"],
                "expected_source_version": item["source_version"],
            }
            for item in capture["retrieval"]["evidence"]
        ]
        return canonical_digest(
            {
                "workspace_uid": self.workspace_uid,
                "capture_id": capture["id"],
                "capture_revision": capture["revision"],
                "request_id": record["request_id"],
                "request_digest": record["request_digest"],
                "completion_digest": record["completion_digest"],
                "completed_at": record["completed_at"],
                "capture_ids": list(record["capture_ids"]),
                "connection_alias": record["connection_alias"],
                "policy_revision": record["policy_revision"],
                "upstream_workspace_uid": connection["upstream_workspace_uid"],
                "corpus_refs": list(record["corpus_refs"]),
                "evidence": evidence,
            }
        )


class HappyPathTest(AdmissionCase):
    def test_an_imported_capture_is_admitted_after_the_original_request_expired(self) -> None:
        capture = self.imported()
        before = self.tree()

        admitted = self.admit(capture)

        self.assertIsInstance(admitted, AdmittedVerification)
        self.assertEqual(set(admitted.document), DOCUMENT_KEYS)
        self.assertEqual(admitted.document["schema"], "workstack.knowledge-verify.v1")
        self.assertEqual(admitted.document["verification_id"], VERIFICATION_ID)
        self.assertEqual(admitted.document["requested_at"], AFTER_EXPIRY)
        self.assertEqual(admitted.document["expires_at"], OBSERVATION_END)
        self.assertEqual(
            admitted.document["binding"],
            {
                "workspace_uid": self.workspace_uid,
                "capture_id": "C-0001",
                "capture_revision": 0,
            },
        )
        self.assertEqual(admitted.document["connection"]["alias"], "team-nas")
        self.assertEqual(admitted.document["connection"]["upstream_workspace_uid"], UPSTREAM_UID)
        self.assertEqual(admitted.document["corpus_refs"], ["nas-team-share"])
        self.assertEqual(
            [set(item) for item in admitted.document["evidence"]],
            [EVIDENCE_KEYS],
        )
        self.assertEqual(
            admitted.document["evidence"],
            [
                {
                    "document_ref": "nas-doc-0001abcd",
                    "source_type": "nas.file",
                    "expected_source_version": "nas-version-11",
                }
            ],
        )
        self.assertTrue(SHA256_RE.fullmatch(admitted.capture_digest))
        self.assertEqual(admitted.capture_digest, self.bound_digest(capture))
        self.assertEqual(self.tree(), before)

    def test_a_dismissed_capture_is_still_admitted_at_its_current_revision(self) -> None:
        capture = self.imported()
        self.stack.dismiss_capture(capture["id"], "dismiss-key-00001")
        dismissed = self.stored_capture("C-0001")
        self.assertEqual(dismissed["status"], "dismissed")
        self.assertNotEqual(dismissed["revision"], 0)

        admitted = self.admit(dismissed)
        self.assertEqual(admitted.document["binding"]["capture_revision"], dismissed["revision"])
        self.refuse(dismissed, "capture_revision_mismatch", revision=0)

    def test_a_task_bound_request_does_not_put_task_query_or_secrets_on_the_wire(self) -> None:
        task = self.open_task()
        capture = self.imported(task=task)
        admitted = self.admit(capture)
        payload = json.dumps(admitted.document)
        for canary in (
            QUERY,
            SUMMARY_CANARY,
            TITLE_CANARY,
            PATH_CANARY,
            SECRET_CANARY,
            task["id"],
            task["uid"],
            "query_id",
            "indexed_digest",
            "version_ref",
            capture["retrieval"]["query_id"],
        ):
            self.assertNotIn(canary, payload)
        self.assertNotIn("task_id", admitted.document.get("binding", {}))
        self.assertNotIn("query", admitted.document)


class DigestTest(AdmissionCase):
    def test_the_digest_is_deterministic_and_ignores_caller_nonce_and_clock(self) -> None:
        capture = self.imported()
        first = self.admit(capture)
        second = self.admit(capture, verification_id=OTHER_VERIFICATION, now="2026-09-08T09:20:00Z")
        self.assertEqual(first.capture_digest, second.capture_digest)
        self.assertEqual(first.capture_digest, self.admit(capture).capture_digest)
        self.assertNotEqual(first.document["verification_id"], second.document["verification_id"])
        self.assertNotEqual(first.document["requested_at"], second.document["requested_at"])

    def test_the_digest_changes_when_revision_evidence_or_authorization_changes(self) -> None:
        self.with_policy()
        request_id = self.issue()
        import_knowledge_captures(
            self.store, self.envelope(request_id, self.two_items(request_id)), now=INSIDE
        )
        first = self.stored_capture("C-0001")
        second = self.stored_capture("C-0002")
        digest_one = self.admit(first).capture_digest
        digest_two = self.admit(second).capture_digest
        self.assertNotEqual(digest_one, digest_two)

        self.rewrite_capture(
            "C-0001",
            lambda entry: entry.__setitem__("revision", 1),
        )
        self.assertNotEqual(self.admit(self.stored_capture("C-0001")).capture_digest, digest_one)

        self.rewrite_capture(
            "C-0002",
            lambda entry: entry["retrieval"]["evidence"][0].__setitem__(
                "source_version", "nas-version-99"
            ),
        )
        self.assertNotEqual(self.admit(self.stored_capture("C-0002")).capture_digest, digest_two)

        before_upstream = self.admit(self.stored_capture("C-0002")).capture_digest
        with self.store.transaction():
            ledger = self.store.load(KNOWLEDGE_DOCUMENT_NAME)
            ledger["connections"][0]["upstream_workspace_uid"] = OTHER_UPSTREAM
            self.store.save_many({KNOWLEDGE_DOCUMENT_NAME: ledger})
        self.assertNotEqual(
            self.admit(self.stored_capture("C-0002")).capture_digest, before_upstream
        )

        before_corpus = self.admit(self.stored_capture("C-0002")).capture_digest
        self.rewrite_request(corpus_refs=["notion-product"])
        self.assertNotEqual(
            self.admit(self.stored_capture("C-0002")).capture_digest, before_corpus
        )

    def test_indexed_digest_and_version_ref_are_not_authorization_bindings(self) -> None:
        capture = self.imported()
        original = self.admit(capture).capture_digest
        self.rewrite_capture(
            "C-0001",
            lambda entry: entry["retrieval"]["evidence"][0].__setitem__(
                "indexed_digest", "sha256:" + "b" * 64
            ),
        )
        self.rewrite_capture(
            "C-0001",
            lambda entry: entry["source"].__setitem__("version_ref", "sha256:" + "c" * 64),
        )
        self.assertEqual(self.admit(self.stored_capture("C-0001")).capture_digest, original)


class RefusalTest(AdmissionCase):
    def test_exact_id_and_revision_are_required(self) -> None:
        capture = self.imported()
        self.refuse(capture, "unknown_capture", capture_id="C-9999")
        self.refuse(capture, "capture_revision_mismatch", revision=1)
        self.refuse(capture, "invalid_capture_id", capture_id="not-a-capture")
        self.refuse(capture, "invalid_number", revision=True)

    def test_a_legacy_capture_is_unsupported(self) -> None:
        packet = _manual_v1_packet()
        ingested = self.stack.ingest_capture(packet, "legacy-key-00001")["body"]["data"]
        self.refuse(ingested, "unsupported_schema")

    def test_a_missing_historical_record_is_refused(self) -> None:
        capture = self.imported()
        self.rewrite_request(request_id=MISSING_REQUEST)
        self.refuse(capture, "unknown_request")

    def test_capture_membership_on_the_completed_record_is_required(self) -> None:
        capture = self.imported()
        self.rewrite_request(capture_ids=["C-9999"])
        self.refuse(capture, "capture_not_bound")

    def test_a_pending_record_is_not_a_completed_binding(self) -> None:
        capture = self.imported()
        self.rewrite_request(
            state="pending",
            capture_ids=[],
            completion_digest=None,
            completed_at=None,
        )
        self.refuse(capture, "request_not_completed")

    def test_current_policy_is_required(self) -> None:
        capture = self.imported()
        replace_connection_policy(
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
        self.refuse(capture, "policy_revision_changed")

    def test_connection_ref_tampering_is_refused(self) -> None:
        self.imported()
        self.rewrite_capture(
            "C-0001",
            lambda entry: entry["source"].__setitem__("connection_ref", "other-nas"),
        )
        self.refuse(self.stored_capture("C-0001"), "capture_binding_mismatch")

    def test_container_ref_tampering_is_refused(self) -> None:
        self.imported()
        self.rewrite_capture(
            "C-0001",
            lambda entry: entry["source"].__setitem__("container_ref", MISSING_REQUEST),
        )
        self.refuse(self.stored_capture("C-0001"), "capture_binding_mismatch")

    def test_retrieval_request_identity_tampering_is_refused(self) -> None:
        self.imported()
        self.rewrite_capture(
            "C-0001",
            lambda entry: entry["retrieval"].__setitem__("request_id", MISSING_REQUEST),
        )
        self.refuse(self.stored_capture("C-0001"), "unknown_request")


class IsolationTest(AdmissionCase):
    def test_admission_mutates_neither_the_store_nor_the_caller_snapshot(self) -> None:
        capture = self.imported()
        before = self.tree()
        admitted = self.admit(capture)
        admitted.document["evidence"][0]["document_ref"] = PATH_CANARY
        admitted.document["corpus_refs"].append("forged-corpus")
        again = self.admit(capture)
        self.assertEqual(again.document["evidence"][0]["document_ref"], "nas-doc-0001abcd")
        self.assertEqual(again.document["corpus_refs"], ["nas-team-share"])
        self.assertEqual(self.tree(), before)
        raw = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_text(encoding="utf-8")
        self.assertNotIn(QUERY, raw)
        self.assertNotIn(PATH_CANARY, raw)

    def test_a_nested_transaction_composes_without_deadlock(self) -> None:
        capture = self.imported()
        with self.store.transaction():
            first = self.admit(capture)
            second = self.admit(capture)
            self.store.load(KNOWLEDGE_DOCUMENT_NAME)
        third = self.admit(capture)
        self.assertEqual(first.document, second.document)
        self.assertEqual(second.document, third.document)


class BudgetTest(unittest.TestCase):
    def test_the_admission_module_stays_inside_declared_budgets(self) -> None:
        text = MODULE_PATH.read_text(encoding="utf-8")
        self.assertLessEqual(text.count("\n") + 1, MAX_FILE_LINES)
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            self.assertLessEqual(length, MAX_FUNCTION_LINES, node.name)
            self.assertLessEqual(complexity(node), MAX_CCN, node.name)


def _manual_v1_packet() -> dict[str, Any]:
    source = {
        "provider": "manual",
        "resource_type": "note",
        "connection_ref": "local",
        "container_ref": "notebook",
        "object_ref": "entry-1",
        "version_ref": "v-legacy-note-1",
    }
    return {
        "schema_version": "1.0",
        "source_key": source_key_for(source),
        "source": {
            **source,
            "display_title": "A hand written note",
            "web_url": None,
            "retrieved_at": ISSUED_AT,
            "fingerprint": fingerprint_for(source),
        },
        "normalized": {
            "summary": "Unrelated reviewed content",
            "context": "A note the owner wrote by hand.",
            "action_items": [],
            "tags": [],
        },
        "task_hints": [],
        "provenance": {
            "capture_mode": "manual",
            "adapter": "manual",
            "adapter_version": "1",
            "redaction_policy_version": "1",
            "raw_retained": False,
            "created_at": ISSUED_AT,
        },
    }


if __name__ == "__main__":
    unittest.main()
