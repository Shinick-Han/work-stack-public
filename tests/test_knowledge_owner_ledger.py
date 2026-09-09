"""The owner-held knowledge ledger: policy, issue and stage completion.

Every store here is a synthetic directory this test created. Nothing opens a
real home directory or a live SSOT, nothing reaches a network, and every clock
is supplied explicitly so expiry is a fact of the test rather than of the day
it runs on.

The suite is organised by the four things the ledger has to get right:

* what may be written down at all (``PolicyDocumentTest``),
* what issuing and reissuing a request means (``IssueTest``),
* what completing an imported batch means, including replay after the window
  has closed (``StageCompletionTest``),
* and that a completion is committed with the evidence it completes, in one
  Store transaction, or not at all (``AtomicCompositionTest``).
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from workstack import knowledge_owner_requests as owner
from workstack.knowledge_ledger_document import (
    KNOWLEDGE_DEFAULT,
    KNOWLEDGE_DOCUMENT_NAME,
    MAX_REQUESTS,
    KnowledgeLedgerError,
    validate_knowledge_document,
)
from workstack.knowledge_request import (
    ActiveTask,
    KnowledgeRequestError,
    RequestAuthority,
    SCHEMA,
)
from workstack.service import WorkStack
from workstack.store import JOURNAL_NAME, Store, StoreCorruptError

QUERY = "rollback verification owner"
REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_REQUEST_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
UPSTREAM_UID = "66666666-6666-4666-8666-666666666666"
NOW = "2026-09-08T09:00:30Z"
LATER = "2026-09-08T09:02:00Z"
AFTER_EXPIRY = "2026-09-08T09:06:00Z"

POLICY = (
    owner.ConnectionPolicy(
        alias="team-nas",
        upstream_workspace_uid=UPSTREAM_UID,
        corpus_refs=("nas-team-share", "notion-product"),
    ),
)


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.readiness = self.store.initialize()
        self.workspace_uid = self.readiness.workspace_uid

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def ledger(self) -> dict[str, Any]:
        return json.loads(
            (self.root / KNOWLEDGE_DOCUMENT_NAME).read_text(encoding="utf-8")
        )

    def with_policy(self) -> dict[str, Any]:
        return owner.set_owner_connection_policy(self.store, POLICY)

    def request(self, **overrides: Any) -> dict[str, Any]:
        document = {
            "schema": SCHEMA,
            "request_id": REQUEST_ID,
            "binding": {"workspace_uid": self.workspace_uid},
            "purpose": "find_context",
            "query": QUERY,
            "corpus_refs": ["nas-team-share"],
            "result_limit": 3,
            "requested_at": "2026-09-08T09:00:00Z",
            "expires_at": "2026-09-08T09:05:00Z",
        }
        document.update(overrides)
        return document

    def issue(self, **overrides: Any) -> owner.IssuedRequest:
        return owner.issue_owner_knowledge_request(
            self.store,
            self.request(**overrides.pop("request", {})),
            connection_alias=overrides.pop("connection_alias", "team-nas"),
            now=overrides.pop("now", NOW),
            task_id=overrides.pop("task_id", None),
        )

    def open_task(self) -> ActiveTask:
        """A Task the Store really holds, at the revision it really carries."""

        WorkStack(self.store).add_task("Held task")
        task = json.loads(
            (self.root / "backlog.json").read_text(encoding="utf-8")
        )["tasks"][0]
        return ActiveTask(
            task_uid=task["uid"],
            task_id=task["id"],
            task_revision=task["revision"],
        )


class PolicyDocumentTest(LedgerCase):
    def test_a_fresh_ledger_is_the_closed_empty_shape(self) -> None:
        self.assertEqual(self.ledger(), KNOWLEDGE_DEFAULT)
        self.assertEqual(
            set(self.ledger()),
            {"version", "policy_revision", "connections", "requests"},
        )

    def test_a_connection_names_an_alias_an_upstream_and_corpora_only(self) -> None:
        document = self.with_policy()

        self.assertEqual(
            document["connections"],
            [
                {
                    "alias": "team-nas",
                    "upstream_workspace_uid": UPSTREAM_UID,
                    "corpus_refs": ["nas-team-share", "notion-product"],
                    "scope": "workspace",
                }
            ],
        )

    def test_the_stored_policy_has_nowhere_to_put_a_location_or_a_secret(
        self,
    ) -> None:
        document = self.with_policy()
        for extra in ("endpoint", "url", "token", "password", "path", "collection"):
            with self.subTest(extra=extra):
                broken = copy.deepcopy(document)
                broken["connections"][0][extra] = "https://host/secret"
                with self.assertRaises(KnowledgeLedgerError) as caught:
                    validate_knowledge_document(
                        broken, workspace_uid=self.workspace_uid
                    )
                self.assertEqual(caught.exception.code, "unknown_field")

    def test_an_alias_cannot_express_a_scheme_a_host_or_a_path(self) -> None:
        for alias in (
            "https://nas.example",
            "//server/share",
            "C:/data",
            "team nas",
            "TEAM-NAS",
            "user:secret@host",
        ):
            with self.subTest(alias=alias):
                with self.assertRaises(KnowledgeLedgerError) as caught:
                    owner.plan_policy_revision(
                        self.ledger(),
                        (
                            owner.ConnectionPolicy(
                                alias=alias,
                                upstream_workspace_uid=UPSTREAM_UID,
                                corpus_refs=("nas-team-share",),
                            ),
                        ),
                        workspace_uid=self.workspace_uid,
                    )
                self.assertEqual(caught.exception.code, "invalid_alias")

    def test_only_workspace_wide_scope_is_expressible(self) -> None:
        with self.assertRaises(KnowledgeLedgerError) as caught:
            owner.plan_policy_revision(
                self.ledger(),
                (
                    owner.ConnectionPolicy(
                        alias="team-nas",
                        upstream_workspace_uid=UPSTREAM_UID,
                        corpus_refs=("nas-team-share",),
                        scope="project:alpha",
                    ),
                ),
                workspace_uid=self.workspace_uid,
            )

        self.assertEqual(caught.exception.code, "invalid_scope")

    def test_each_policy_change_increments_the_revision(self) -> None:
        self.assertEqual(self.ledger()["policy_revision"], 0)

        self.with_policy()
        self.assertEqual(self.ledger()["policy_revision"], 1)

        owner.set_owner_connection_policy(self.store, ())
        self.assertEqual(self.ledger()["policy_revision"], 2)

    def test_the_authority_a_request_is_judged_against_comes_from_the_policy(
        self,
    ) -> None:
        document = self.with_policy()

        authority = owner.owner_request_authority(
            document,
            workspace_uid=self.workspace_uid,
            connection_alias="team-nas",
            now=NOW,
        )

        self.assertIsInstance(authority, RequestAuthority)
        self.assertEqual(
            authority.granted_corpus_refs, ("nas-team-share", "notion-product")
        )
        self.assertEqual(authority.workspace_uid, self.workspace_uid)
        self.assertIsNone(authority.active_task)

    def test_an_unknown_connection_grants_nothing(self) -> None:
        with self.assertRaises(KnowledgeLedgerError) as caught:
            owner.owner_request_authority(
                self.with_policy(),
                workspace_uid=self.workspace_uid,
                connection_alias="not-approved",
                now=NOW,
            )

        self.assertEqual(caught.exception.code, "connection_not_found")


class IssueTest(LedgerCase):
    def test_issuing_records_a_digest_and_never_the_query(self) -> None:
        self.with_policy()

        issued = self.issue()

        self.assertFalse(issued.replayed)
        record = self.ledger()["requests"][0]
        self.assertEqual(record["request_id"], REQUEST_ID)
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["request_digest"], issued.request_digest)
        self.assertEqual(record["policy_revision"], 1)
        self.assertEqual(record["result_limit"], 3)
        self.assertEqual(record["corpus_refs"], ["nas-team-share"])
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
        raw = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_text(encoding="utf-8")
        self.assertNotIn(QUERY, raw)
        self.assertNotIn("query", raw)

    def test_the_released_wire_validator_decides_scope(self) -> None:
        self.with_policy()

        with self.assertRaises(KnowledgeRequestError) as caught:
            self.issue(request={"corpus_refs": ["nas-team-share", "finance-share"]})

        # Refused by workstack.knowledge_request, unedited, with its own code.
        self.assertEqual(caught.exception.code, "corpus_not_granted")
        self.assertEqual(self.ledger()["requests"], [])

    def test_a_request_naming_another_workspace_is_refused(self) -> None:
        self.with_policy()

        with self.assertRaises(KnowledgeRequestError) as caught:
            self.issue(request={"binding": {"workspace_uid": UPSTREAM_UID}})

        self.assertEqual(caught.exception.code, "workspace_mismatch")

    def test_a_window_longer_than_five_minutes_is_refused(self) -> None:
        self.with_policy()

        with self.assertRaises(KnowledgeRequestError) as caught:
            self.issue(request={"expires_at": "2026-09-08T09:05:01Z"})

        self.assertEqual(caught.exception.code, "invalid_request_window")

    def test_an_already_expired_request_is_refused_at_the_supplied_clock(
        self,
    ) -> None:
        self.with_policy()

        with self.assertRaises(KnowledgeRequestError) as caught:
            self.issue(now=AFTER_EXPIRY)

        self.assertEqual(caught.exception.code, "request_expired")

    def test_the_task_binding_is_checked_against_the_store_in_the_transaction(
        self,
    ) -> None:
        self.with_policy()
        task = self.open_task()

        issued = self.issue(
            request={
                "binding": {
                    "workspace_uid": self.workspace_uid,
                    "task_uid": task.task_uid,
                    "task_id": task.task_id,
                    "task_revision": task.task_revision,
                }
            },
            task_id=task.task_id,
        )

        self.assertFalse(issued.replayed)
        self.assertEqual(
            self.ledger()["requests"][0]["binding"],
            {
                "workspace_uid": self.workspace_uid,
                "task_uid": task.task_uid,
                "task_id": task.task_id,
                "task_revision": task.task_revision,
            },
        )

    def test_a_stale_task_revision_is_refused_against_the_stored_task(self) -> None:
        self.with_policy()
        task = self.open_task()

        with self.assertRaises(KnowledgeRequestError) as caught:
            self.issue(
                request={
                    "binding": {
                        "workspace_uid": self.workspace_uid,
                        "task_uid": task.task_uid,
                        "task_id": task.task_id,
                        "task_revision": task.task_revision + 1,
                    }
                },
                task_id=task.task_id,
            )

        self.assertEqual(caught.exception.code, "task_binding_mismatch")

    def test_reissuing_an_identical_request_returns_the_original_result(self) -> None:
        self.with_policy()
        first = self.issue()
        before = self.ledger()

        second = self.issue(now=LATER)

        self.assertTrue(second.replayed)
        self.assertEqual(second.request_digest, first.request_digest)
        self.assertEqual(second.policy_revision, first.policy_revision)
        self.assertEqual(second.expires_at, first.expires_at)
        self.assertEqual(self.ledger(), before)
        self.assertEqual(len(self.ledger()["requests"]), 1)

    def test_reissuing_the_same_identity_with_a_different_query_refuses(self) -> None:
        self.with_policy()
        self.issue()

        with self.assertRaises(KnowledgeLedgerError) as caught:
            self.issue(request={"query": "a different question entirely"})

        self.assertEqual(caught.exception.code, "request_digest_mismatch")
        self.assertEqual(len(self.ledger()["requests"]), 1)

    def test_a_policy_change_invalidates_an_outstanding_request(self) -> None:
        self.with_policy()
        self.issue()

        owner.set_owner_connection_policy(self.store, POLICY)

        pending = owner.owner_pending_requests(self.store)
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0].authority_current)
        self.assertEqual(pending[0].policy_revision, 1)
        self.assertEqual(self.ledger()["policy_revision"], 2)

    def test_a_restart_reconstructs_pending_state_from_the_document(self) -> None:
        self.with_policy()
        issued = self.issue()

        # A different Store object over the same directory: nothing survives in
        # memory, so whatever comes back was reconstructed from disk.
        restarted = Store(self.root)
        restarted.initialize()
        pending = owner.owner_pending_requests(restarted, now=LATER)

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].request_id, issued.request_id)
        self.assertEqual(pending[0].request_digest, issued.request_digest)
        self.assertEqual(pending[0].result_limit, 3)
        self.assertTrue(pending[0].authority_current)
        self.assertFalse(pending[0].expired)
        self.assertTrue(
            owner.owner_pending_requests(restarted, now=AFTER_EXPIRY)[0].expired
        )

    def test_the_ledger_refuses_to_grow_past_its_bound(self) -> None:
        document = self.with_policy()
        record = {
            "request_id": REQUEST_ID,
            "request_digest": "sha256:" + "2" * 64,
            "connection_alias": "team-nas",
            "binding": {"workspace_uid": self.workspace_uid},
            "corpus_refs": ["nas-team-share"],
            "policy_revision": 1,
            "result_limit": 1,
            "requested_at": "2026-09-08T09:00:00Z",
            "expires_at": "2026-09-08T09:05:00Z",
            "state": "pending",
            "capture_ids": [],
            "completion_digest": None,
            "completed_at": None,
        }
        for index in range(MAX_REQUESTS):
            entry = copy.deepcopy(record)
            entry["request_id"] = "{:08x}-0000-4000-8000-000000000000".format(index)
            document["requests"].append(entry)

        with self.assertRaises(KnowledgeLedgerError) as caught:
            owner.plan_request_issue(
                document,
                self.request(),
                workspace_uid=self.workspace_uid,
                connection_alias="team-nas",
                now=NOW,
            )

        self.assertEqual(caught.exception.code, "ledger_full")


class StageCompletionTest(LedgerCase):
    def setUp(self) -> None:
        super().setUp()
        self.with_policy()
        self.issued = self.issue()
        self.document = self.ledger()
        self.completion_digest = "sha256:" + "3" * 64

    def complete(self, **overrides: Any) -> owner.StageCompletion:
        return owner.plan_ledger_stage_completion(
            overrides.pop("document", self.document),
            request_id=overrides.pop("request_id", REQUEST_ID),
            completion_digest=overrides.pop(
                "completion_digest", self.completion_digest
            ),
            capture_ids=overrides.pop("capture_ids", ["C-0001", "C-0002"]),
            workspace_uid=overrides.pop("workspace_uid", self.workspace_uid),
            now=overrides.pop("now", LATER),
            active_task=overrides.pop("active_task", None),
        )

    def test_pending_becomes_completed_with_bounded_ids_and_a_digest(self) -> None:
        completion = self.complete()

        self.assertFalse(completion.replayed)
        record = completion.document["requests"][0]
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["capture_ids"], ["C-0001", "C-0002"])
        self.assertEqual(record["completion_digest"], self.completion_digest)
        self.assertEqual(record["completed_at"], LATER)
        validate_knowledge_document(
            completion.document, workspace_uid=self.workspace_uid
        )

    def test_the_batch_may_not_exceed_the_issued_result_limit(self) -> None:
        with self.assertRaises(KnowledgeLedgerError) as caught:
            self.complete(capture_ids=["C-0001", "C-0002", "C-0003", "C-0004"])

        self.assertEqual(caught.exception.code, "result_limit_exceeded")

    def test_an_identical_replay_returns_the_prior_result_and_writes_nothing(
        self,
    ) -> None:
        first = self.complete()

        second = owner.plan_ledger_stage_completion(
            first.document,
            request_id=REQUEST_ID,
            completion_digest=self.completion_digest,
            capture_ids=["C-0001", "C-0002"],
            workspace_uid=self.workspace_uid,
            now="2026-09-08T09:03:00Z",
        )

        self.assertTrue(second.replayed)
        self.assertEqual(second.completed_at, first.completed_at)
        self.assertEqual(second.capture_ids, first.capture_ids)
        self.assertEqual(second.document, first.document)

    def test_an_identical_replay_is_still_recognised_after_expiry(self) -> None:
        first = self.complete()

        second = owner.plan_ledger_stage_completion(
            first.document,
            request_id=REQUEST_ID,
            completion_digest=self.completion_digest,
            capture_ids=["C-0001", "C-0002"],
            workspace_uid=self.workspace_uid,
            now=AFTER_EXPIRY,
        )

        self.assertTrue(second.replayed)
        self.assertEqual(second.completed_at, LATER)

    def test_a_different_completion_digest_for_the_same_request_refuses(self) -> None:
        first = self.complete()

        with self.assertRaises(KnowledgeLedgerError) as caught:
            owner.plan_ledger_stage_completion(
                first.document,
                request_id=REQUEST_ID,
                completion_digest="sha256:" + "4" * 64,
                capture_ids=["C-0001", "C-0002"],
                workspace_uid=self.workspace_uid,
                now=LATER,
            )

        self.assertEqual(caught.exception.code, "completion_digest_mismatch")

    def test_the_same_digest_with_different_captures_refuses(self) -> None:
        first = self.complete()

        with self.assertRaises(KnowledgeLedgerError) as caught:
            owner.plan_ledger_stage_completion(
                first.document,
                request_id=REQUEST_ID,
                completion_digest=self.completion_digest,
                capture_ids=["C-0001", "C-0009"],
                workspace_uid=self.workspace_uid,
                now=LATER,
            )

        self.assertEqual(caught.exception.code, "completion_replay_mismatch")

    def test_a_new_completion_after_the_window_closed_refuses(self) -> None:
        with self.assertRaises(KnowledgeLedgerError) as caught:
            self.complete(now=AFTER_EXPIRY)

        self.assertEqual(caught.exception.code, "request_expired")

    def test_a_new_completion_under_a_changed_policy_refuses(self) -> None:
        owner.set_owner_connection_policy(self.store, POLICY)

        with self.assertRaises(KnowledgeLedgerError) as caught:
            self.complete(document=self.ledger())

        self.assertEqual(caught.exception.code, "policy_revision_changed")

    def test_a_completion_against_another_workspace_refuses(self) -> None:
        with self.assertRaises(KnowledgeLedgerError) as caught:
            self.complete(workspace_uid=UPSTREAM_UID)

        # The ledger is bound to the store that holds it, so the mismatch is
        # caught while the document is being admitted at all.
        self.assertEqual(caught.exception.code, "workspace_mismatch")

    def test_a_task_bound_request_may_not_be_completed_with_none_open(self) -> None:
        task = self.open_task()
        issued = owner.issue_owner_knowledge_request(
            self.store,
            self.request(
                request_id=OTHER_REQUEST_ID,
                binding={
                    "workspace_uid": self.workspace_uid,
                    "task_uid": task.task_uid,
                    "task_id": task.task_id,
                    "task_revision": task.task_revision,
                },
            ),
            connection_alias="team-nas",
            now=NOW,
            task_id=task.task_id,
        )

        with self.assertRaises(KnowledgeLedgerError) as caught:
            self.complete(
                document=issued.document,
                request_id=OTHER_REQUEST_ID,
                active_task=None,
            )

        self.assertEqual(caught.exception.code, "task_binding_mismatch")

    def test_a_moved_task_revision_refuses_the_completion(self) -> None:
        task = self.open_task()
        issued = owner.issue_owner_knowledge_request(
            self.store,
            self.request(
                request_id=OTHER_REQUEST_ID,
                binding={
                    "workspace_uid": self.workspace_uid,
                    "task_uid": task.task_uid,
                    "task_id": task.task_id,
                    "task_revision": task.task_revision,
                },
            ),
            connection_alias="team-nas",
            now=NOW,
            task_id=task.task_id,
        )
        moved = ActiveTask(
            task_uid=task.task_uid,
            task_id=task.task_id,
            task_revision=task.task_revision + 1,
        )

        with self.assertRaises(KnowledgeLedgerError) as caught:
            self.complete(
                document=issued.document,
                request_id=OTHER_REQUEST_ID,
                active_task=moved,
            )

        self.assertEqual(caught.exception.code, "task_binding_mismatch")

    def test_an_unknown_request_cannot_be_completed(self) -> None:
        with self.assertRaises(KnowledgeLedgerError) as caught:
            self.complete(request_id=OTHER_REQUEST_ID)

        self.assertEqual(caught.exception.code, "unknown_request")

    def test_a_capture_identifier_outside_the_stored_grammar_refuses(self) -> None:
        for capture_ids in (
            ["C:/data/leak.json"],
            ["../../etc/passwd"],
            ["C-0001", "C-0001"],
            ["not-a-capture"],
        ):
            with self.subTest(capture_ids=capture_ids):
                with self.assertRaises(KnowledgeLedgerError):
                    self.complete(capture_ids=capture_ids)

    def test_planning_never_writes_to_the_store(self) -> None:
        before = (self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes()

        self.complete()

        self.assertEqual(
            (self.root / KNOWLEDGE_DOCUMENT_NAME).read_bytes(), before
        )

    def test_there_is_no_standalone_consume_entry_point(self) -> None:
        exported = set(owner.__all__)

        self.assertFalse({name for name in exported if "consume" in name})
        self.assertFalse(
            {name for name in dir(owner) if not name.startswith("_") and "consume" in name}
        )
        self.assertIn("plan_ledger_stage_completion", exported)


class DiagnosticContentTest(LedgerCase):
    def test_no_refusal_carries_a_query_a_body_a_path_or_a_credential(self) -> None:
        self.with_policy()
        secret = "password=hunter2"
        cases = [
            lambda: self.issue(
                request={"query": "https://nas.example/share?token=abcdef123456"}
            ),
            lambda: self.issue(request={"corpus_refs": ["finance-share"]}),
            lambda: owner.plan_policy_revision(
                self.ledger(),
                (
                    owner.ConnectionPolicy(
                        alias="//server/share$",
                        upstream_workspace_uid=UPSTREAM_UID,
                        corpus_refs=("nas-team-share",),
                    ),
                ),
                workspace_uid=self.workspace_uid,
            ),
            lambda: owner.plan_ledger_stage_completion(
                self.ledger(),
                request_id=REQUEST_ID,
                completion_digest="sha256:" + "5" * 64,
                capture_ids=["C:/secret/" + secret],
                workspace_uid=self.workspace_uid,
                now=NOW,
            ),
        ]
        for index, case in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises((KnowledgeLedgerError, KnowledgeRequestError)) as caught:
                    case()
                text = str(caught.exception) + json.dumps(
                    getattr(caught.exception, "details", {})
                )
                for leak in (
                    QUERY,
                    secret,
                    "token=",
                    "https://",
                    "//server",
                    "C:/",
                    "finance-share",
                ):
                    self.assertNotIn(leak, text)

    def test_a_store_refusal_names_the_rule_and_not_the_content(self) -> None:
        document = self.with_policy()
        self.issue()
        broken = self.ledger()
        broken["requests"][0]["query"] = QUERY
        (self.root / KNOWLEDGE_DOCUMENT_NAME).write_text(
            json.dumps(broken, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        self.assertIn("knowledge.json schema is invalid: unknown_field", str(caught.exception))
        self.assertNotIn(QUERY, str(caught.exception))
        self.assertIsNotNone(document)


class CurrentPolicyIntegrityTest(LedgerCase):
    """A record claiming the policy in force must be covered by it.

    The whole point of a durable ledger is that the document on disk is the
    only state, so the question this class asks is the one a restart asks: does
    the store still stand behind every record that says it is current? A record
    at the current ``policy_revision`` naming a connection the owner never
    approved -- or reaching past the corpora that connection grants -- is not a
    stale record. It is a record no policy ever authorised, and admitting it
    would make forged authority indistinguishable from issued authority.

    Records issued under an *older* revision are the opposite case and are
    deliberately left alone: they are history, they stay readable, and they can
    never newly complete.
    """

    def setUp(self) -> None:
        super().setUp()
        self.with_policy()
        self.issued = self.issue()
        self.completion_digest = "sha256:" + "8" * 64

    def write_ledger(self, document: dict[str, Any]) -> None:
        """Put a document on disk *without* going through the owner path.

        The planners refuse to produce these documents at all, so the only way
        one reaches a real store is the way corruption or a foreign writer
        would put it there: straight into the file the store reads on restart.
        """

        (self.root / KNOWLEDGE_DOCUMENT_NAME).write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def forged(self, mutate) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        """The real issued ledger, with its *current* policy tampered with."""

        document = self.ledger()
        self.assertEqual(
            document["requests"][0]["policy_revision"], document["policy_revision"]
        )
        mutate(document)
        return document

    # -- a same-revision record outside the current policy is not admitted ---

    def test_a_same_revision_record_with_no_connection_refuses_the_restart(
        self,
    ) -> None:
        def retire_every_connection(document: dict[str, Any]) -> None:
            document["connections"] = []

        forged = self.forged(retire_every_connection)
        self.write_ledger(forged)

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        self.assertIn(
            "knowledge.json schema is invalid: connection_not_found",
            str(caught.exception),
        )
        with self.assertRaises(KnowledgeLedgerError) as direct:
            validate_knowledge_document(forged, workspace_uid=self.workspace_uid)
        self.assertEqual(direct.exception.code, "connection_not_found")
        self.assertEqual(direct.exception.field, "requests[0].connection_alias")

    def test_a_same_revision_record_naming_another_connection_refuses(self) -> None:
        def rename_the_connection(document: dict[str, Any]) -> None:
            document["connections"][0]["alias"] = "other-nas"

        self.write_ledger(self.forged(rename_the_connection))

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        self.assertIn("connection_not_found", str(caught.exception))

    def test_a_same_revision_record_outside_the_granted_corpora_refuses(self) -> None:
        def narrow_the_grant(document: dict[str, Any]) -> None:
            # The connection survives; what it grants no longer covers the
            # corpus the record names. That is scope expansion, not history.
            document["connections"][0]["corpus_refs"] = ["notion-product"]

        forged = self.forged(narrow_the_grant)
        self.assertEqual(forged["requests"][0]["corpus_refs"], ["nas-team-share"])
        self.write_ledger(forged)

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        self.assertIn(
            "knowledge.json schema is invalid: corpus_not_granted",
            str(caught.exception),
        )
        with self.assertRaises(KnowledgeLedgerError) as direct:
            validate_knowledge_document(forged, workspace_uid=self.workspace_uid)
        self.assertEqual(direct.exception.field, "requests[0].corpus_refs")

    def test_the_refusal_names_the_rule_and_not_the_content(self) -> None:
        def retire_every_connection(document: dict[str, Any]) -> None:
            document["connections"] = []

        self.write_ledger(self.forged(retire_every_connection))

        with self.assertRaises(StoreCorruptError) as caught:
            Store(self.root).initialize()

        text = str(caught.exception)
        for leak in (QUERY, "team-nas", "nas-team-share", REQUEST_ID):
            self.assertNotIn(leak, text)

    def test_a_forged_current_record_is_neither_pending_nor_completable(self) -> None:
        """The reviewer's counterexample, end to end, against a real store."""

        def retire_every_connection(document: dict[str, Any]) -> None:
            document["connections"] = []

        forged = self.forged(retire_every_connection)

        for call in (
            lambda: owner.pending_requests(
                forged, workspace_uid=self.workspace_uid, now=LATER
            ),
            lambda: owner.plan_ledger_stage_completion(
                forged,
                request_id=REQUEST_ID,
                completion_digest=self.completion_digest,
                capture_ids=["C-0001"],
                workspace_uid=self.workspace_uid,
                now=LATER,
            ),
        ):
            with self.assertRaises(KnowledgeLedgerError) as caught:
                call()
            self.assertEqual(caught.exception.code, "connection_not_found")

    # -- the legitimate path is untouched -----------------------------------

    def test_a_legitimately_issued_current_request_still_completes(self) -> None:
        pending = owner.owner_pending_requests(self.store, now=NOW)

        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].authority_current)

        completion = owner.plan_ledger_stage_completion(
            self.ledger(),
            request_id=REQUEST_ID,
            completion_digest=self.completion_digest,
            capture_ids=["C-0001", "C-0002"],
            workspace_uid=self.workspace_uid,
            now=LATER,
        )

        self.assertFalse(completion.replayed)
        self.assertEqual(completion.document["requests"][0]["state"], "completed")
        validate_knowledge_document(
            completion.document, workspace_uid=self.workspace_uid
        )

    # -- an older-revision record stays history -----------------------------

    def test_a_retired_connection_leaves_the_old_record_readable(self) -> None:
        owner.set_owner_connection_policy(self.store, ())

        restarted = Store(self.root)
        restarted.initialize()
        pending = owner.owner_pending_requests(restarted, now=LATER)

        self.assertEqual(restarted.load(KNOWLEDGE_DOCUMENT_NAME)["connections"], [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].request_id, REQUEST_ID)
        self.assertEqual(pending[0].policy_revision, 1)
        self.assertFalse(pending[0].authority_current)

        with self.assertRaises(KnowledgeLedgerError) as caught:
            owner.plan_ledger_stage_completion(
                self.ledger(),
                request_id=REQUEST_ID,
                completion_digest=self.completion_digest,
                capture_ids=["C-0001"],
                workspace_uid=self.workspace_uid,
                now=LATER,
            )

        self.assertEqual(caught.exception.code, "policy_revision_changed")

    def test_a_narrowed_grant_leaves_the_old_record_readable(self) -> None:
        owner.set_owner_connection_policy(
            self.store,
            (
                owner.ConnectionPolicy(
                    alias="team-nas",
                    upstream_workspace_uid=UPSTREAM_UID,
                    corpus_refs=("notion-product",),
                ),
            ),
        )

        restarted = Store(self.root)
        restarted.initialize()
        pending = owner.owner_pending_requests(restarted, now=LATER)

        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0].authority_current)

        with self.assertRaises(KnowledgeLedgerError) as caught:
            owner.plan_ledger_stage_completion(
                self.ledger(),
                request_id=REQUEST_ID,
                completion_digest=self.completion_digest,
                capture_ids=["C-0001"],
                workspace_uid=self.workspace_uid,
                now=LATER,
            )

        self.assertEqual(caught.exception.code, "policy_revision_changed")

    def test_a_completed_record_still_replays_after_its_policy_is_retired(self) -> None:
        completion = owner.plan_ledger_stage_completion(
            self.ledger(),
            request_id=REQUEST_ID,
            completion_digest=self.completion_digest,
            capture_ids=["C-0001"],
            workspace_uid=self.workspace_uid,
            now=LATER,
        )
        self.store.save_many({KNOWLEDGE_DOCUMENT_NAME: completion.document})
        owner.set_owner_connection_policy(self.store, ())

        restarted = Store(self.root)
        restarted.initialize()
        replay = owner.plan_ledger_stage_completion(
            restarted.load(KNOWLEDGE_DOCUMENT_NAME),
            request_id=REQUEST_ID,
            completion_digest=self.completion_digest,
            capture_ids=["C-0001"],
            workspace_uid=self.workspace_uid,
            # After the window closed, and after the policy moved: identical
            # completed work is still the importer's own recognisable result.
            now=AFTER_EXPIRY,
        )

        self.assertTrue(replay.replayed)
        self.assertEqual(replay.completed_at, LATER)
        self.assertEqual(replay.capture_ids, ("C-0001",))

    def test_expiry_is_still_what_stops_a_current_request(self) -> None:
        with self.assertRaises(KnowledgeLedgerError) as caught:
            owner.plan_ledger_stage_completion(
                self.ledger(),
                request_id=REQUEST_ID,
                completion_digest=self.completion_digest,
                capture_ids=["C-0001"],
                workspace_uid=self.workspace_uid,
                now=AFTER_EXPIRY,
            )

        self.assertEqual(caught.exception.code, "request_expired")


class AtomicCompositionTest(LedgerCase):
    """One completion, one transaction, together with its captures and activity."""

    def setUp(self) -> None:
        super().setUp()
        self.with_policy()
        self.issued = self.issue()
        self.completion_digest = "sha256:" + "7" * 64

    def import_batch(
        self,
        store: Store,
        *,
        capture_ids: list[str],
        completion_digest: str | None = None,
        now: str = LATER,
    ) -> owner.StageCompletion:
        """What the following Capture importer does, in one Store transaction.

        Every Capture in the batch is staged first; the ledger is consumed once,
        after the whole batch, and the three documents are committed through a
        single journalled ``save_many``. There is no earlier durable write, so a
        failure anywhere in here leaves the request pending.
        """

        with store.transaction():
            captures = store.load("captures.json")
            activity = store.load("activity.json")
            ledger = store.load(KNOWLEDGE_DOCUMENT_NAME)
            for capture_id in capture_ids:
                captures["captures"].append(
                    {"id": capture_id, "title": "Imported evidence"}
                )
            completion = owner.plan_ledger_stage_completion(
                ledger,
                request_id=REQUEST_ID,
                completion_digest=completion_digest or self.completion_digest,
                capture_ids=capture_ids,
                workspace_uid=self.workspace_uid,
                now=now,
            )
            activity["activity"].append(
                {
                    "type": "knowledge.import",
                    "request_id": REQUEST_ID,
                    "capture_ids": list(capture_ids),
                }
            )
            store.save_many(
                {
                    "captures.json": captures,
                    "activity.json": activity,
                    KNOWLEDGE_DOCUMENT_NAME: completion.document,
                },
                operation_id="knowledge-import-" + REQUEST_ID,
            )
            return completion

    def test_the_batch_and_the_completion_commit_together(self) -> None:
        completion = self.import_batch(
            self.store, capture_ids=["C-0001", "C-0002", "C-0003"]
        )

        self.assertFalse(completion.replayed)
        captures = json.loads(
            (self.root / "captures.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [entry["id"] for entry in captures["captures"]],
            ["C-0001", "C-0002", "C-0003"],
        )
        record = self.ledger()["requests"][0]
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["capture_ids"], ["C-0001", "C-0002", "C-0003"])

    def test_the_authoritative_state_is_the_ledger_not_the_activity_detail(
        self,
    ) -> None:
        self.import_batch(self.store, capture_ids=["C-0001"])

        activity = json.loads(
            (self.root / "activity.json").read_text(encoding="utf-8")
        )
        entry = activity["activity"][-1]
        self.assertEqual(entry["request_id"], REQUEST_ID)
        # The activity entry references the request; it does not carry the
        # policy, the state or the authority the ledger owns.
        self.assertNotIn("policy_revision", entry)
        self.assertNotIn("state", entry)
        self.assertEqual(self.ledger()["requests"][0]["state"], "completed")

    def test_a_failure_mid_commit_rolls_back_to_a_still_pending_request(
        self,
    ) -> None:
        store = Store(self.root)
        store.initialize()
        original = store._atomic_write_locked
        seen: list[str] = []

        def failing(path: Path, value: Any) -> None:
            if len(seen) >= 2:
                raise OSError("injected import interruption")
            seen.append(path.name)
            original(path, value)

        with mock.patch.object(store, "_atomic_write_locked", failing):
            with self.assertRaises(OSError):
                self.import_batch(store, capture_ids=["C-0001", "C-0002"])

        self.assertTrue((self.root / JOURNAL_NAME).exists())

        # Restart: recovery replays the one journalled generation, so the
        # captures and the completion are either both there or neither is.
        restarted = Store(self.root)
        restarted.initialize()
        self.assertFalse((self.root / JOURNAL_NAME).exists())
        captures = json.loads(
            (self.root / "captures.json").read_text(encoding="utf-8")
        )
        record = self.ledger()["requests"][0]
        self.assertEqual(
            bool(captures["captures"]), record["state"] == "completed"
        )
        pending = owner.owner_pending_requests(restarted, now=LATER)
        self.assertEqual(bool(pending), record["state"] == "pending")

    def test_an_interrupted_import_can_be_retried_to_completion(self) -> None:
        store = Store(self.root)
        store.initialize()
        original = store._atomic_write_locked
        seen: list[str] = []

        def failing(path: Path, value: Any) -> None:
            if len(seen) >= 1:
                raise OSError("injected import interruption")
            seen.append(path.name)
            original(path, value)

        with mock.patch.object(store, "_atomic_write_locked", failing):
            with self.assertRaises(OSError):
                self.import_batch(store, capture_ids=["C-0001"])

        retried = Store(self.root)
        retried.initialize()
        completion = self.import_batch(retried, capture_ids=["C-0001"])

        self.assertEqual(self.ledger()["requests"][0]["state"], "completed")
        self.assertEqual(completion.capture_ids, ("C-0001",))

    def test_two_competing_completions_settle_on_exactly_one(self) -> None:
        self.import_batch(self.store, capture_ids=["C-0001", "C-0002"])

        # A second importer that re-reads inside its own transaction sees the
        # completed record. Its own identical work replays; different work is
        # refused rather than spending the request twice.
        replay = self.import_batch(self.store, capture_ids=["C-0001", "C-0002"])
        self.assertTrue(replay.replayed)

        with self.assertRaises(KnowledgeLedgerError) as caught:
            self.import_batch(
                self.store,
                capture_ids=["C-0007"],
                completion_digest="sha256:" + "8" * 64,
            )

        self.assertEqual(caught.exception.code, "completion_digest_mismatch")
        self.assertEqual(
            self.ledger()["requests"][0]["capture_ids"], ["C-0001", "C-0002"]
        )

    def test_a_completion_planned_from_a_stale_snapshot_is_caught_on_re_read(
        self,
    ) -> None:
        stale = self.store.load(KNOWLEDGE_DOCUMENT_NAME)
        self.import_batch(self.store, capture_ids=["C-0001"])

        # The stale snapshot still says pending, so planning against it would
        # succeed; planning against what the transaction actually reads does
        # not. This is why the importer plans from the document it just read.
        planned_from_stale = owner.plan_ledger_stage_completion(
            stale,
            request_id=REQUEST_ID,
            completion_digest="sha256:" + "9" * 64,
            capture_ids=["C-0009"],
            workspace_uid=self.workspace_uid,
            now=LATER,
        )
        self.assertFalse(planned_from_stale.replayed)

        with self.assertRaises(KnowledgeLedgerError):
            owner.plan_ledger_stage_completion(
                self.store.load(KNOWLEDGE_DOCUMENT_NAME),
                request_id=REQUEST_ID,
                completion_digest="sha256:" + "9" * 64,
                capture_ids=["C-0009"],
                workspace_uid=self.workspace_uid,
                now=LATER,
            )
