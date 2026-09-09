"""The manual knowledge Capture importer, over a real synthetic Store.

Every case here builds its own store in a temporary directory, configures the
owner connection policy and issues a KnowledgeRequest through the *released*
owner operations, and then imports against that real ledger record. Nothing
reaches a network, a home directory, a live SSOT, a model or a credential, and
the clock is stated rather than read.

The suite is organised by the obligation it holds the importer to:

* one transaction, one ``save_many``, no per-item write (``TransactionTest``),
* what the closed envelope refuses (``EnvelopeRefusalTest``),
* what a retry means (``ReplayTest``),
* what a moved world means (``AuthorityTest``),
* what the audit trail may say and what a failed commit leaves behind
  (``AuditAndRecoveryTest``),
* and how a stored 1.1 record reads back (``StoredRecordTest``).
"""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from workstack.capture import fingerprint_for, source_key_for
from workstack.knowledge_capture_import import (
    IMPORT_EVENT_TYPE,
    import_knowledge_captures,
)
from workstack.knowledge_capture_packets import KnowledgeImportError
from workstack.knowledge_ledger_document import (
    KNOWLEDGE_DOCUMENT_NAME,
    KnowledgeLedgerError,
)
from workstack.knowledge_owner_requests import (
    ConnectionPolicy,
    set_owner_connection_policy,
)
from workstack.knowledge_request_issuer import IssueIntent, issue_knowledge_request
from workstack.service import WorkStack
from workstack.service_errors import SourceRevisionConflictError
from workstack.store import Store, StoreCorruptError
from workstack.store_document_validation import validate_document_values
from workstack.store_rosters import V6_DOCUMENT_NAMES

UPSTREAM_UID = "66666666-6666-4666-8666-666666666666"
INTENT_ID = "a1b2c3d4-1111-4111-8111-aaaaaaaaaaaa"
ITEM_ONE = "11111111-1111-4111-8111-111111111111"
ITEM_TWO = "22222222-2222-4222-8222-222222222222"

ISSUED_AT = "2026-09-08T09:00:00Z"
INSIDE = "2026-09-08T09:02:00Z"
LATER_INSIDE = "2026-09-08T09:03:00Z"
AFTER_EXPIRY = "2026-09-08T09:05:01Z"

# Strings that exist nowhere else in the repository or the store. If one turns
# up in the activity log, the ledger or a diagnostic, content leaked out of the
# envelope and into durable state.
SUMMARY_CANARY = "canary 4b81ff02 imported answer summary phrase"
TITLE_CANARY = "canary 71ac9de4 imported evidence display title"


class ImportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.workspace_uid = self.document("workspace.json")["id"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # -- the world the importer runs against -----------------------------

    def document(self, name: str) -> dict[str, Any]:
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def with_policy(self, *corpus_refs: str) -> None:
        set_owner_connection_policy(
            self.store,
            (
                ConnectionPolicy(
                    alias="team-nas",
                    upstream_workspace_uid=UPSTREAM_UID,
                    corpus_refs=tuple(corpus_refs) or ("nas-team-share",),
                ),
            ),
        )

    def open_task(self) -> dict[str, Any]:
        self.stack.add_task("Held task")
        return self.document("backlog.json")["tasks"][0]

    def issue(self, *, result_limit: int = 3, task: dict[str, Any] | None = None) -> str:
        binding: dict[str, Any] = {"workspace_uid": self.workspace_uid}
        task_id = None
        if task is not None:
            binding = {
                "workspace_uid": self.workspace_uid,
                "task_uid": task["uid"],
                "task_id": task["id"],
                "task_revision": task["revision"],
            }
            task_id = task["id"]
        intent = IssueIntent(
            intent_id=INTENT_ID,
            connection_alias="team-nas",
            binding=binding,
            query="rollback verification owner",
            corpus_refs=["nas-team-share"],
            purpose="find_context",
            result_limit=result_limit,
        )
        del task_id
        issued = issue_knowledge_request(self.store, intent, clock=lambda: ISSUED_AT)
        return issued.document["request_id"]

    def ready(self, **issue_kwargs: Any) -> str:
        self.with_policy()
        return self.issue(**issue_kwargs)

    # -- the envelope ----------------------------------------------------

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

    def item(self, request_id: str, *, item_id: str = ITEM_ONE, index: int = 1, **overrides: Any):
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

    # -- running it ------------------------------------------------------

    def run_import(self, envelope: dict[str, Any], *, now: str = INSIDE):
        return import_knowledge_captures(self.store, envelope, now=now)

    def triple(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        return (
            self.document(KNOWLEDGE_DOCUMENT_NAME),
            self.document("captures.json"),
            self.document("activity.json"),
        )

    def assertRefused(self, code: str, envelope: dict[str, Any], *, now: str = INSIDE) -> None:
        """The refusal is the closed code, and nothing at all was written."""

        before = self.triple()
        with self.assertRaises((KnowledgeImportError, KnowledgeLedgerError)) as raised:
            self.run_import(envelope, now=now)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(set(raised.exception.details) - {"field"}, set())
        self.assertEqual(self.triple(), before)


class TransactionTest(ImportCase):
    def test_one_batch_commits_once_and_retires_the_request(self) -> None:
        request_id = self.ready()
        writes: list[dict[str, Any]] = []
        original = self.store.save_many

        def counted(mapping, operation_id=None):
            writes.append(dict(mapping))
            return original(mapping, operation_id=operation_id)

        with mock.patch.object(self.store, "save_many", side_effect=counted):
            receipt = self.run_import(self.envelope(request_id, self.two_items(request_id)))

        # One save_many, over exactly the three documents an import may touch.
        self.assertEqual(len(writes), 1)
        self.assertEqual(
            set(writes[0]),
            {KNOWLEDGE_DOCUMENT_NAME, "captures.json", "activity.json"},
        )
        self.assertEqual(receipt.capture_ids, ("C-0001", "C-0002"))
        self.assertFalse(receipt.replayed)

        record = self.document(KNOWLEDGE_DOCUMENT_NAME)["requests"][0]
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["capture_ids"], ["C-0001", "C-0002"])
        self.assertEqual(record["completion_digest"], receipt.completion_digest)
        self.assertEqual(record["completed_at"], INSIDE)

    def test_the_imported_record_is_an_unattested_manual_answer(self) -> None:
        request_id = self.ready()
        self.run_import(self.envelope(request_id))
        capture = self.stack.list_captures()[0]

        self.assertEqual(capture["schema_version"], "1.1")
        self.assertEqual(capture["source"]["provider"], "manual")
        self.assertEqual(capture["source"]["resource_type"], "knowledge.answer")
        self.assertEqual(capture["provenance"]["capture_mode"], "manual")
        self.assertIsNone(capture["source"]["web_url"])
        # The locator is the server's: the recorded alias, the request identity
        # and the item identity. None of it came from the envelope.
        self.assertEqual(capture["source"]["connection_ref"], "team-nas")
        self.assertEqual(capture["source"]["container_ref"], request_id)
        self.assertEqual(capture["source"]["object_ref"], ITEM_ONE)

        retrieval = capture["retrieval"]
        self.assertIsNone(retrieval["origin"])
        self.assertEqual(retrieval["origin_state"], "reported_unverified")
        self.assertEqual(retrieval["capture_source_type"], "knowledge.answer")
        self.assertEqual(
            [item["version_state"] for item in retrieval["evidence"]],
            ["reported_unverified"],
        )

    def test_an_import_touches_no_task_and_links_nothing(self) -> None:
        task = self.open_task()
        request_id = self.ready(task=task)
        backlog_before = self.document("backlog.json")
        workspace_before = self.document("workspace.json")

        self.run_import(self.envelope(request_id))

        self.assertEqual(self.document("backlog.json"), backlog_before)
        self.assertEqual(self.document("workspace.json"), workspace_before)
        capture = self.stack.list_captures()[0]
        self.assertEqual(capture["status"], "inbox")
        self.assertEqual(capture["task_hints"], [])
        self.assertEqual(capture["linked_task_ids"], [])
        self.assertEqual(capture["converted_task_ids"], [])

    def test_a_partial_batch_never_commits(self) -> None:
        request_id = self.ready()
        items = self.two_items(request_id)
        items[1]["retrieval"]["evidence"][0]["document_ref"] = "not a ref"
        self.assertRefused("invalid_ref", self.envelope(request_id, items))
        self.assertEqual(self.document("captures.json")["captures"], [])

    def test_a_batch_wider_than_the_issued_limit_refuses(self) -> None:
        request_id = self.ready(result_limit=1)
        self.assertRefused(
            "result_limit_exceeded", self.envelope(request_id, self.two_items(request_id))
        )

    def test_an_import_never_lands_on_an_existing_capture(self) -> None:
        """A pre-existing record holding this source key is never overwritten."""

        request_id = self.ready()
        source = {
            "provider": "manual",
            "resource_type": "knowledge.answer",
            "connection_ref": "team-nas",
            "container_ref": request_id,
            "object_ref": ITEM_ONE,
            "version_ref": "sha256:" + "b" * 64,
        }
        packet = {
            "schema_version": "1.0",
            "source_key": source_key_for(source),
            "source": {
                **source,
                "display_title": "An unrelated manual note",
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
        self.stack.ingest_capture(packet, "collision-key-00001")
        before = self.document("captures.json")

        self.assertRefused("source_key_conflict", self.envelope(request_id))
        self.assertEqual(self.document("captures.json"), before)


class EnvelopeRefusalTest(ImportCase):
    def test_the_envelope_is_closed(self) -> None:
        request_id = self.ready()
        envelope = self.envelope(request_id)
        envelope["provider"] = "opendocuments"
        self.assertRefused("unknown_field", envelope)

    def test_an_item_may_not_assert_a_source_or_a_verification(self) -> None:
        request_id = self.ready()
        for extra in ("source", "verification", "provider", "tools", "web_url"):
            with self.subTest(field=extra):
                items = [self.item(request_id)]
                items[0][extra] = "opendocuments"
                self.assertRefused("unknown_field", self.envelope(request_id, items))

    def test_a_foreign_schema_is_refused(self) -> None:
        request_id = self.ready()
        envelope = self.envelope(request_id)
        envelope["schema"] = "workstack.knowledge-import.v2"
        self.assertRefused("unsupported_schema", envelope)

    def test_duplicate_item_identifiers_refuse(self) -> None:
        request_id = self.ready()
        items = [self.item(request_id, index=1), self.item(request_id, index=2)]
        self.assertRefused("duplicate_item_id", self.envelope(request_id, items))

    def test_the_batch_is_bounded(self) -> None:
        request_id = self.ready()
        self.assertRefused("invalid_items", self.envelope(request_id, []))

    def test_an_unissued_request_cannot_be_imported_against(self) -> None:
        self.ready()
        unknown = "99999999-9999-4999-8999-999999999999"
        self.assertRefused("unknown_request", self.envelope(unknown))

    def test_a_resolved_source_location_in_a_title_refuses(self) -> None:
        request_id = self.ready()
        items = [self.item(request_id)]
        items[0]["retrieval"]["evidence"][0]["title"] = "C:/secret/payroll.xlsx"
        self.assertRefused("source_location_suspected", self.envelope(request_id, items))

    def test_credential_material_in_a_correlation_id_refuses(self) -> None:
        request_id = self.ready()
        items = [self.item(request_id)]
        items[0]["retrieval"]["query_id"] = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
        )
        self.assertRefused(
            "credential_material_suspected", self.envelope(request_id, items)
        )

    def test_raw_content_keys_are_refused_at_any_depth(self) -> None:
        request_id = self.ready()
        items = [self.item(request_id)]
        items[0]["retrieval"]["evidence"][0]["snippet"] = "the answer body"
        self.assertRefused("forbidden_field", self.envelope(request_id, items))

    def test_a_reflected_secret_in_the_summary_refuses(self) -> None:
        request_id = self.ready()
        items = [self.item(request_id)]
        items[0]["normalized"]["summary"] = (
            "api_key: AKIA1234567890ABCDEF client_secret: s3cr3tv4lue0000"
        )
        self.assertRefused(
            "credential_material_suspected", self.envelope(request_id, items)
        )

    def test_a_retrieval_naming_another_request_refuses(self) -> None:
        request_id = self.ready()
        items = [self.item(request_id)]
        items[0]["retrieval"]["request_id"] = "99999999-9999-4999-8999-999999999999"
        self.assertRefused("request_id_mismatch", self.envelope(request_id, items))

    def test_an_oversized_retrieval_wire_refuses(self) -> None:
        """The released 16 KiB retrieval bound, on this item's real octets.

        The refusal lands in the strict decoder the released payload boundary
        shares, which is where that bound lives, so the code is the decoder's
        ``request_too_large`` rather than the post-decode arithmetic one.
        """

        request_id = self.ready()
        items = [self.item(request_id)]
        evidence = items[0]["retrieval"]["evidence"][0]
        items[0]["retrieval"]["answer_scope"] = "synthesized"
        # Ten items, each with a title at the 500-character bound written in
        # three-octet characters. Every value is individually legal; only the
        # measured UTF-8 size of the whole wire is not.
        items[0]["retrieval"]["evidence"] = [
            {**evidence, "document_ref": "nas-doc-{:06d}".format(number),
             "chunk_ref": "chunk-{:06d}".format(number),
             "title": "가" * 500}
            for number in range(10)
        ]
        self.assertRefused("request_too_large", self.envelope(request_id, items))


class ReplayTest(ImportCase):
    def test_the_same_envelope_returns_the_original_captures(self) -> None:
        request_id = self.ready()
        envelope = self.envelope(request_id, self.two_items(request_id))
        first = self.run_import(envelope)
        after_first = self.triple()

        second = self.run_import(copy.deepcopy(envelope), now=LATER_INSIDE)

        self.assertTrue(second.replayed)
        self.assertEqual(second.capture_ids, first.capture_ids)
        self.assertEqual(second.completion_digest, first.completion_digest)
        self.assertEqual(second.completed_at, first.completed_at)
        self.assertEqual(second.captures, first.captures)
        # No new record, no new audit event, no new document generation.
        self.assertEqual(self.triple(), after_first)

    def test_a_replay_after_expiry_still_recognises_its_own_work(self) -> None:
        request_id = self.ready()
        envelope = self.envelope(request_id)
        first = self.run_import(envelope)
        after_first = self.triple()

        replayed = self.run_import(copy.deepcopy(envelope), now=AFTER_EXPIRY)

        self.assertTrue(replayed.replayed)
        self.assertEqual(replayed.capture_ids, first.capture_ids)
        self.assertEqual(self.triple(), after_first)

    def test_a_replay_after_the_policy_is_retired_still_replays(self) -> None:
        request_id = self.ready()
        envelope = self.envelope(request_id)
        first = self.run_import(envelope)
        set_owner_connection_policy(self.store, ())
        after_retirement = self.triple()

        replayed = self.run_import(copy.deepcopy(envelope), now=LATER_INSIDE)

        self.assertTrue(replayed.replayed)
        self.assertEqual(replayed.capture_ids, first.capture_ids)
        self.assertEqual(self.triple(), after_retirement)

    def test_a_changed_batch_under_the_same_request_is_a_digest_conflict(self) -> None:
        request_id = self.ready()
        self.run_import(self.envelope(request_id))
        changed = self.envelope(request_id)
        changed["items"][0]["normalized"]["context"] = "Something else entirely."
        self.assertRefused("completion_digest_mismatch", changed, now=LATER_INSIDE)

    def test_a_reordered_batch_is_a_digest_conflict_not_an_equivalence(self) -> None:
        request_id = self.ready()
        items = self.two_items(request_id)
        self.run_import(self.envelope(request_id, items))
        reordered = self.envelope(request_id, list(reversed(copy.deepcopy(items))))
        self.assertRefused("completion_digest_mismatch", reordered, now=LATER_INSIDE)

    def test_a_replay_whose_capture_was_removed_refuses(self) -> None:
        request_id = self.ready()
        envelope = self.envelope(request_id)
        self.run_import(envelope)
        captures = self.document("captures.json")
        captures["captures"] = []
        with self.store.transaction():
            self.store.save_many({"captures.json": captures})
        self.assertRefused(
            "capture_record_missing", copy.deepcopy(envelope), now=LATER_INSIDE
        )

    def test_two_competing_imports_of_one_envelope_settle_once(self) -> None:
        request_id = self.ready()
        envelope = self.envelope(request_id, self.two_items(request_id))
        receipts: list[Any] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def attempt() -> None:
            barrier.wait()
            try:
                receipts.append(self.run_import(copy.deepcopy(envelope)))
            except BaseException as error:  # noqa: BLE001 - recorded, then asserted
                errors.append(error)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(receipts), 2)
        self.assertEqual({receipt.replayed for receipt in receipts}, {False, True})
        self.assertEqual(
            {receipt.capture_ids for receipt in receipts}, {("C-0001", "C-0002")}
        )
        self.assertEqual(len(self.document("captures.json")["captures"]), 2)
        events = [
            event
            for event in self.document("activity.json")["activity"]
            if event["type"] == IMPORT_EVENT_TYPE
        ]
        self.assertEqual(len(events), 1)


class AuthorityTest(ImportCase):
    def test_a_changed_policy_refuses_a_new_completion(self) -> None:
        request_id = self.ready()
        self.with_policy("nas-team-share", "notion-product")
        self.assertRefused("policy_revision_changed", self.envelope(request_id))

    def test_an_expired_pending_request_refuses(self) -> None:
        request_id = self.ready()
        self.assertRefused("request_expired", self.envelope(request_id), now=AFTER_EXPIRY)

    def test_a_moved_task_refuses(self) -> None:
        task = self.open_task()
        request_id = self.ready(task=task)
        self.stack.patch_task(task["id"], {"revision": task["revision"], "title": "Moved"})
        self.assertRefused("task_binding_mismatch", self.envelope(request_id))

    def test_a_task_bound_request_whose_task_is_gone_refuses(self) -> None:
        task = self.open_task()
        request_id = self.ready(task=task)
        ledger = self.document(KNOWLEDGE_DOCUMENT_NAME)
        ledger["requests"][0]["binding"]["task_id"] = "T-0099"
        with self.store.transaction():
            self.store.save_many({KNOWLEDGE_DOCUMENT_NAME: ledger})
        self.assertRefused("unknown_task", self.envelope(request_id))

    def test_a_task_bound_import_still_writes_no_task(self) -> None:
        task = self.open_task()
        request_id = self.ready(task=task)
        before = self.document("backlog.json")
        self.run_import(self.envelope(request_id))
        self.assertEqual(self.document("backlog.json"), before)


class AuditAndRecoveryTest(ImportCase):
    def test_the_audit_event_is_content_free(self) -> None:
        request_id = self.ready()
        receipt = self.run_import(self.envelope(request_id, self.two_items(request_id)))
        activity = self.document("activity.json")
        events = [
            event for event in activity["activity"] if event["type"] == IMPORT_EVENT_TYPE
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0]["details"],
            {
                "request_id": request_id,
                "capture_ids": list(receipt.capture_ids),
                "imported_count": 2,
            },
        )
        # No idempotency receipt is stored for this route at all.
        self.assertEqual(activity["idempotency"], [])

    def test_no_forbidden_content_reaches_the_ledger_or_the_activity_log(self) -> None:
        request_id = self.ready()
        self.run_import(self.envelope(request_id))
        for name in (KNOWLEDGE_DOCUMENT_NAME, "activity.json"):
            body = (self.root / name).read_text(encoding="utf-8")
            with self.subTest(document=name):
                self.assertNotIn(SUMMARY_CANARY, body)
                self.assertNotIn(TITLE_CANARY, body)
                self.assertNotIn("rollback verification owner", body)
        # The allowed sanitized summary is still readable where it belongs.
        captures = (self.root / "captures.json").read_text(encoding="utf-8")
        self.assertIn(SUMMARY_CANARY, captures)
        self.assertEqual(
            self.stack.list_captures()[0]["normalized"]["summary"], SUMMARY_CANARY
        )

    def test_no_extra_durable_document_is_written(self) -> None:
        request_id = self.ready()
        before = sorted(path.name for path in self.root.iterdir())
        self.run_import(self.envelope(request_id))
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)

    def test_a_crash_mid_commit_recovers_to_one_coherent_triple(self) -> None:
        """A real journalled commit, interrupted after the first replacement."""

        request_id = self.ready()
        original_write = self.store._atomic_write_locked

        class SimulatedProcessCrash(BaseException):
            pass

        def crash_after_captures(path: Path, value: object) -> None:
            original_write(path, value)
            if path == self.store.path("captures.json"):
                raise SimulatedProcessCrash()

        with mock.patch.object(
            self.store, "_atomic_write_locked", side_effect=crash_after_captures
        ):
            with self.assertRaises(SimulatedProcessCrash):
                self.run_import(self.envelope(request_id))

        self.assertTrue(self.store.journal_path.exists())
        # A partial generation is on disk: the crash landed between the
        # replacements, so at least one document of the triple is still the old
        # one. Recovery must not leave that mix readable.
        self.assertEqual(
            [
                event
                for event in self.document("activity.json")["activity"]
                if event["type"] == IMPORT_EVENT_TYPE
            ],
            [],
        )

        recovered = Store(self.root)
        stack = WorkStack(recovered)
        self.assertFalse(recovered.journal_path.exists())
        record = self.document(KNOWLEDGE_DOCUMENT_NAME)["requests"][0]
        captures = stack.list_captures()
        events = [
            event
            for event in self.document("activity.json")["activity"]
            if event["type"] == IMPORT_EVENT_TYPE
        ]
        # One coherent generation: either the whole import, or none of it.
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["capture_ids"], [capture["id"] for capture in captures])
        self.assertEqual(len(events), 1)

    def test_a_refused_commit_leaves_all_three_documents_unchanged(self) -> None:
        request_id = self.ready()
        before = self.triple()

        class SimulatedWriteFailure(BaseException):
            pass

        with mock.patch.object(
            self.store, "save_many", side_effect=SimulatedWriteFailure()
        ):
            with self.assertRaises(SimulatedWriteFailure):
                self.run_import(self.envelope(request_id))

        self.assertEqual(self.triple(), before)


class StoredRecordTest(ImportCase):
    def imported(self) -> dict[str, Any]:
        request_id = self.ready()
        self.run_import(self.envelope(request_id))
        return self.stack.list_captures()[0]

    def test_a_stored_record_reads_back_the_same_way_after_a_restart(self) -> None:
        first = self.imported()
        restarted = WorkStack(Store(self.root))
        self.assertEqual(restarted.list_captures(), [first])

    def test_generic_ingestion_cannot_overwrite_an_imported_record(self) -> None:
        """Source-key and fingerprint spoofing both refuse, and nothing moves."""

        capture = self.imported()
        source = {
            field: capture["source"][field]
            for field in (
                "provider", "resource_type", "connection_ref",
                "container_ref", "object_ref", "version_ref",
            )
        }
        packet = {
            "schema_version": "1.0",
            "source_key": source_key_for(source),
            "source": {
                **source,
                "display_title": "A spoofed replacement",
                "web_url": None,
                "retrieved_at": "2026-09-08T10:00:00Z",
                "fingerprint": fingerprint_for(source),
            },
            "normalized": {
                "summary": "spoofed", "context": "spoofed context",
                "action_items": [], "tags": [],
            },
            "task_hints": [],
            "provenance": {
                "capture_mode": "manual", "adapter": "manual", "adapter_version": "1",
                "redaction_policy_version": "1", "raw_retained": False,
                "created_at": "2026-09-08T10:00:00Z",
            },
        }
        # Same fingerprint (a claimed duplicate) and a different one (a claimed
        # new revision) are both refused rather than replacing the evidence.
        with self.assertRaises(SourceRevisionConflictError):
            self.stack.ingest_capture(packet, "spoof-key-000001")
        spoofed_version = dict(source, version_ref="sha256:" + "c" * 64)
        packet["source"].update(
            version_ref=spoofed_version["version_ref"],
            fingerprint=fingerprint_for(spoofed_version),
        )
        with self.assertRaises(SourceRevisionConflictError):
            self.stack.ingest_capture(packet, "spoof-key-000002")
        self.assertEqual(self.stack.list_captures(), [capture])

    def test_a_malformed_stored_retrieval_is_refused_on_read_and_on_load(self) -> None:
        self.imported()
        captures = self.document("captures.json")
        captures["captures"][0]["retrieval"]["evidence"][0]["web_url"] = (
            "https://example.invalid/leak"
        )
        (self.root / "captures.json").write_text(
            json.dumps(captures), encoding="utf-8"
        )

        with self.assertRaises(StoreCorruptError):
            WorkStack(Store(self.root), initialize=False).list_captures()

        values = {
            name: json.loads((self.root / name).read_text(encoding="utf-8"))
            for name in sorted(V6_DOCUMENT_NAMES)
        }
        with self.assertRaises(StoreCorruptError):
            validate_document_values(values, schema_version=6)

    def test_a_historical_10_record_keeps_its_released_behaviour(self) -> None:
        """1.0 ingestion, re-ingestion, linking and conversion are untouched."""

        packet = _manual_v1_packet("2026-09-08T08:00:00Z", "First review")
        first = self.stack.ingest_capture(packet, "legacy-key-00001")
        self.assertEqual(first["status"], 201)
        self.assertNotIn("retrieval", first["body"]["data"])

        revised = _manual_v1_packet("2026-09-08T08:30:00Z", "Second review")
        second = self.stack.ingest_capture(revised, "legacy-key-00002")
        self.assertEqual(second["status"], 200)
        self.assertEqual(second["body"]["data"]["revision"], 1)

        capture_id = second["body"]["data"]["id"]
        self.stack.add_task("Legacy target")
        task_id = self.document("backlog.json")["tasks"][0]["id"]
        linked = self.stack.link_capture(capture_id, task_id, "legacy-key-00003")
        self.assertEqual(linked["body"]["data"]["linked_task_ids"], [task_id])
        dismissed = self.stack.dismiss_capture(capture_id, "legacy-key-00004")
        self.assertEqual(dismissed["body"]["data"]["status"], "dismissed")

    def test_an_imported_record_converts_only_on_an_explicit_user_action(self) -> None:
        capture = self.imported()
        self.stack.add_task("Existing")
        task_id = self.document("backlog.json")["tasks"][0]["id"]

        linked = self.stack.link_capture(capture["id"], task_id, "user-key-000001")
        self.assertEqual(linked["body"]["data"]["linked_task_ids"], [task_id])
        self.assertEqual(linked["body"]["data"]["status"], "linked")
        self.assertIn("retrieval", linked["body"]["data"])

        action_id = capture["normalized"]["action_items"][0]["id"]
        converted = self.stack.convert_capture_action(
            capture["id"], action_id, [], "user-key-000002"
        )
        self.assertEqual(converted["status"], 201)
        self.assertEqual(
            self.stack.list_captures(status="converted")[0]["id"], capture["id"]
        )


def _manual_v1_packet(retrieved_at: str, summary: str) -> dict[str, Any]:
    source = {
        "provider": "manual",
        "resource_type": "note",
        "connection_ref": "local",
        "container_ref": "notebook",
        "object_ref": "entry-1",
        "version_ref": "v-" + retrieved_at,
    }
    return {
        "schema_version": "1.0",
        "source_key": source_key_for(source),
        "source": {
            **source,
            "display_title": "A hand written note",
            "web_url": None,
            "retrieved_at": retrieved_at,
            "fingerprint": fingerprint_for(source),
        },
        "normalized": {
            "summary": summary,
            "context": "A hand written context line.",
            "action_items": [{"title": "Follow up"}],
            "tags": [],
        },
        "task_hints": [],
        "provenance": {
            "capture_mode": "manual",
            "adapter": "manual",
            "adapter_version": "1",
            "redaction_policy_version": "1",
            "raw_retained": False,
            "created_at": retrieved_at,
        },
    }


if __name__ == "__main__":  # pragma: no cover - runner convenience
    unittest.main()
