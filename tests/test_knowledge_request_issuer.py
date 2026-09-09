"""The composition layer between the HTTP surface and the owner ledger.

Every store here is a synthetic directory this test created. Nothing opens a
real home directory or a live SSOT, nothing reaches a network, and every clock
is injected explicitly so expiry is a fact of the test rather than of the day it
runs on.

These tests cover the part that is *not* visible from the wire: that the
document the caller gets back is the exact document the released validator
projects and the released ledger digested, that identity is derived from the
actual workspace rather than chosen, and that the closed bodies are closed.
The boundary itself — admission, CSRF, refusal envelopes, persistence scans —
is proven through a real local server in ``test_knowledge_request_http``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from workstack import knowledge_request_issuer as issuer
from workstack.knowledge_ledger_document import (
    KNOWLEDGE_DOCUMENT_NAME,
    MAX_KNOWLEDGE_BYTES,
    MAX_REQUESTS,
    KnowledgeLedgerError,
    compact_bytes,
)
from workstack.knowledge_owner_requests import (
    ConnectionPolicy,
    owner_request_authority,
    request_digest,
)
from workstack.knowledge_request import (
    MAX_ACTIVE_SECONDS,
    SCHEMA,
    KnowledgeRequestError,
    validate_knowledge_request,
)
from workstack.service import WorkStack
from workstack.store import Store

UPSTREAM_UID = "66666666-6666-4666-8666-666666666666"
INTENT_ID = "a1b2c3d4-1111-4111-8111-aaaaaaaaaaaa"
OTHER_INTENT_ID = "b2c3d4e5-2222-4222-8222-bbbbbbbbbbbb"
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


class IssuerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.workspace_uid = self.store.initialize().workspace_uid

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def ledger(self) -> dict[str, Any]:
        return json.loads(
            (self.root / KNOWLEDGE_DOCUMENT_NAME).read_text(encoding="utf-8")
        )

    def with_policy(self) -> dict[str, Any]:
        return issuer.replace_connection_policy(
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
        return json.loads(
            (self.root / "backlog.json").read_text(encoding="utf-8")
        )["tasks"][0]


class IdentityTest(IssuerCase):
    def test_the_namespace_is_the_documented_application_constant(self) -> None:
        """The published constant, recomputed rather than restated."""

        self.assertEqual(
            issuer.REQUEST_INTENT_NAMESPACE,
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "https://work-stack.invalid/knowledge-request/v1/intent",
            ),
        )
        self.assertEqual(
            str(issuer.REQUEST_INTENT_NAMESPACE),
            "92a31ef4-d870-5657-a390-cbd452f0205b",
        )

    def test_identity_is_derived_from_the_actual_workspace_and_the_intent(self) -> None:
        derived = issuer.derive_request_id(self.workspace_uid, INTENT_ID)

        self.assertEqual(
            derived,
            str(
                uuid.uuid5(
                    issuer.REQUEST_INTENT_NAMESPACE,
                    "{}:{}".format(self.workspace_uid, INTENT_ID),
                )
            ),
        )
        self.assertEqual(derived, issuer.derive_request_id(self.workspace_uid, INTENT_ID))

    def test_the_same_intent_in_another_workspace_is_another_identity(self) -> None:
        """A workspace cannot mint an identity inside another one's namespace."""

        self.assertNotEqual(
            issuer.derive_request_id(self.workspace_uid, INTENT_ID),
            issuer.derive_request_id(UPSTREAM_UID, INTENT_ID),
        )

    def test_a_non_canonical_intent_is_refused_before_anything_is_derived(self) -> None:
        for candidate in (
            INTENT_ID.upper(),
            "{" + INTENT_ID + "}",
            "urn:uuid:" + INTENT_ID,
            INTENT_ID.replace("-", ""),
            "00000000-0000-0000-0000-000000000000",
            17,
        ):
            with self.subTest(intent=repr(candidate)):
                with self.assertRaises(KnowledgeRequestError) as raised:
                    issuer.parse_issue_body(self.body(intent_id=candidate))
                self.assertEqual(raised.exception.code, "invalid_uuid")


class ClosedBodyTest(IssuerCase):
    def test_the_issue_body_has_nowhere_to_assert_identity_or_authority(self) -> None:
        for field in (
            "request_id",
            "requested_at",
            "expires_at",
            "schema",
            "provider",
            "tools",
            "connection",
            "authority",
            "task_title",
        ):
            with self.subTest(field=field):
                with self.assertRaises(KnowledgeRequestError) as raised:
                    issuer.parse_issue_body(self.body(**{field: "x"}))
                self.assertEqual(raised.exception.code, "unknown_field")

    def test_a_missing_issue_field_is_refused_rather_than_defaulted(self) -> None:
        for field in sorted(issuer.ISSUE_FIELDS):
            with self.subTest(field=field):
                body = self.body()
                del body[field]
                with self.assertRaises(KnowledgeRequestError) as raised:
                    issuer.parse_issue_body(body)
                self.assertEqual(raised.exception.code, "missing_field")

    def test_the_policy_body_is_closed_and_scope_is_not_a_caller_field(self) -> None:
        with self.assertRaises(KnowledgeRequestError) as unknown:
            issuer.parse_policy_body(
                {
                    "expected_policy_revision": 0,
                    "connections": [],
                    "policy_revision": 9,
                }
            )
        self.assertEqual(unknown.exception.code, "unknown_field")

        with self.assertRaises(KnowledgeRequestError) as scope:
            issuer.parse_policy_body(
                {
                    "expected_policy_revision": 0,
                    "connections": [
                        {
                            "alias": "team-nas",
                            "upstream_workspace_uid": UPSTREAM_UID,
                            "corpus_refs": ["nas-team-share"],
                            "scope": "project:alpha",
                        }
                    ],
                }
            )
        self.assertEqual(scope.exception.code, "unknown_field")

    def test_a_connection_alias_cannot_smuggle_a_location_or_a_secret(self) -> None:
        for alias in (
            "https://evil.example",
            "//server/share",
            "C:/data",
            "user:secret@host",
            "team nas",
            "TEAM-NAS",
            "../../etc",
        ):
            with self.subTest(alias=alias):
                with self.assertRaises(KnowledgeLedgerError) as raised:
                    issuer.connection_policies(
                        [
                            {
                                "alias": alias,
                                "upstream_workspace_uid": UPSTREAM_UID,
                                "corpus_refs": ["nas-team-share"],
                            }
                        ]
                    )
                self.assertEqual(raised.exception.code, "invalid_alias")
                self.assertNotIn(alias, str(raised.exception))

    def test_an_issue_alias_is_held_to_the_same_grammar(self) -> None:
        with self.assertRaises(KnowledgeLedgerError) as raised:
            issuer.parse_issue_body(self.body(connection_alias="https://evil.example"))
        self.assertEqual(raised.exception.code, "invalid_alias")

    def test_a_boolean_expected_revision_is_not_a_revision(self) -> None:
        with self.assertRaises(KnowledgeLedgerError) as raised:
            issuer.parse_policy_body(
                {"expected_policy_revision": True, "connections": []}
            )
        self.assertEqual(raised.exception.code, "invalid_number")


class IssuedDocumentTest(IssuerCase):
    def test_the_returned_document_is_the_validator_projection(self) -> None:
        """The receipt is the document the ledger digested, not a re-shape."""

        self.with_policy()
        issued = self.issue()

        document = self.store.load(KNOWLEDGE_DOCUMENT_NAME)
        authority = owner_request_authority(
            document,
            workspace_uid=self.workspace_uid,
            connection_alias="team-nas",
            now=NOW,
        )
        projection = validate_knowledge_request(issued.document, authority)

        self.assertEqual(issued.document, projection)
        self.assertEqual(
            request_digest(projection), self.ledger()["requests"][0]["request_digest"]
        )
        self.assertEqual(issued.document["schema"], SCHEMA)

    def test_the_server_owns_the_window_and_bounds_it_at_five_minutes(self) -> None:
        self.with_policy()
        issued = self.issue()

        self.assertEqual(issued.document["requested_at"], NOW)
        self.assertEqual(issued.document["expires_at"], "2026-09-08T09:05:00Z")
        self.assertEqual(issuer.REQUEST_LIFETIME_SECONDS, MAX_ACTIVE_SECONDS)

    def test_a_sub_second_clock_still_produces_an_exact_window(self) -> None:
        """A truncated start keeps `now >= requested_at` and the window exact."""

        self.with_policy()
        issued = self.issue(now="2026-09-08T09:00:00.750000+00:00")

        self.assertEqual(issued.document["requested_at"], NOW)
        self.assertEqual(issued.document["expires_at"], "2026-09-08T09:05:00Z")

    def test_a_query_is_trimmed_exactly_as_the_validator_projects_it(self) -> None:
        self.with_policy()
        issued = self.issue(query="  " + QUERY + "  ")

        self.assertEqual(issued.document["query"], QUERY)

    def test_the_task_binding_is_read_from_the_store_not_from_the_body(self) -> None:
        self.with_policy()
        task = self.open_task()
        issued = self.issue(
            binding={
                "workspace_uid": self.workspace_uid,
                "task_uid": task["uid"],
                "task_id": str(task["id"]).lower(),
                "task_revision": task["revision"],
            }
        )

        self.assertEqual(issued.document["binding"]["task_id"], str(task["id"]).upper())
        self.assertEqual(issued.document["binding"]["task_uid"], task["uid"])
        self.assertEqual(
            issued.document["binding"]["task_revision"], task["revision"]
        )

    def test_no_task_text_reaches_the_issued_request(self) -> None:
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

        self.assertNotIn("Held task", json.dumps(issued.document))
        self.assertEqual(
            set(issued.document["binding"]),
            {"workspace_uid", "task_uid", "task_id", "task_revision"},
        )


class IdempotenceTest(IssuerCase):
    def test_an_unchanged_retry_replays_the_original_receipt(self) -> None:
        self.with_policy()
        first = self.issue()
        second = self.issue(now=INSIDE)

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.document, second.document)
        self.assertEqual(len(self.ledger()["requests"]), 1)

    def test_a_changed_body_under_the_same_intent_refuses(self) -> None:
        self.with_policy()
        self.issue()

        for change in (
            {"query": "a different question entirely"},
            {"result_limit": 4},
            {"corpus_refs": ["notion-product"]},
            {"purpose": "extract_actions"},
        ):
            with self.subTest(change=sorted(change)):
                with self.assertRaises(KnowledgeLedgerError) as raised:
                    self.issue(now=INSIDE, **change)
                self.assertEqual(raised.exception.code, "request_digest_mismatch")
                self.assertEqual(len(self.ledger()["requests"]), 1)

    def test_an_expired_retry_refuses_without_minting_a_new_identity(self) -> None:
        self.with_policy()
        first = self.issue()

        with self.assertRaises(KnowledgeRequestError) as raised:
            self.issue(now=AFTER_EXPIRY)

        self.assertEqual(raised.exception.code, "request_expired")
        records = self.ledger()["requests"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["request_id"], first.document["request_id"])
        self.assertEqual(records[0]["expires_at"], first.document["expires_at"])

    def test_a_fresh_intent_after_expiry_is_a_new_request_the_user_reviewed(self) -> None:
        self.with_policy()
        self.issue()
        later = self.issue(now=AFTER_EXPIRY, intent_id=OTHER_INTENT_ID)

        self.assertFalse(later.replayed)
        self.assertEqual(later.document["requested_at"], AFTER_EXPIRY)
        self.assertEqual(len(self.ledger()["requests"]), 2)

    def test_a_restart_reconstructs_the_original_receipt(self) -> None:
        self.with_policy()
        first = self.issue()

        restarted = Store(self.root)
        restarted.initialize()
        replayed = issuer.issue_knowledge_request(
            restarted,
            issuer.parse_issue_body(self.body()),
            clock=_clock(INSIDE),
        )

        self.assertTrue(replayed.replayed)
        self.assertEqual(replayed.document, first.document)


class ScopeTest(IssuerCase):
    def test_a_corpus_the_policy_does_not_grant_is_refused(self) -> None:
        self.with_policy()

        with self.assertRaises(KnowledgeRequestError) as raised:
            self.issue(corpus_refs=["nas-team-share", "finance-drive"])

        self.assertEqual(raised.exception.code, "corpus_not_granted")
        self.assertEqual(self.ledger()["requests"], [])

    def test_a_foreign_workspace_guard_is_refused_rather_than_obeyed(self) -> None:
        self.with_policy()

        with self.assertRaises(KnowledgeRequestError) as raised:
            self.issue(binding={"workspace_uid": UPSTREAM_UID})

        self.assertEqual(raised.exception.code, "workspace_mismatch")
        self.assertEqual(self.ledger()["requests"], [])

    def test_a_stale_task_revision_guard_is_refused(self) -> None:
        self.with_policy()
        task = self.open_task()

        with self.assertRaises(KnowledgeRequestError) as raised:
            self.issue(
                binding={
                    "workspace_uid": self.workspace_uid,
                    "task_uid": task["uid"],
                    "task_id": task["id"],
                    "task_revision": task["revision"] + 3,
                }
            )

        self.assertEqual(raised.exception.code, "task_binding_mismatch")
        self.assertEqual(self.ledger()["requests"], [])

    def test_a_task_the_store_does_not_hold_is_refused(self) -> None:
        self.with_policy()

        with self.assertRaises(KnowledgeLedgerError) as raised:
            self.issue(
                binding={
                    "workspace_uid": self.workspace_uid,
                    "task_uid": UPSTREAM_UID,
                    "task_id": "T-9999",
                    "task_revision": 0,
                }
            )

        self.assertEqual(raised.exception.code, "unknown_task")

    def test_an_unknown_connection_is_refused_and_writes_nothing(self) -> None:
        self.with_policy()

        with self.assertRaises(KnowledgeLedgerError) as raised:
            self.issue(connection_alias="not-approved")

        self.assertEqual(raised.exception.code, "connection_not_found")
        self.assertEqual(self.ledger()["requests"], [])

    def test_a_policy_change_invalidates_an_outstanding_request(self) -> None:
        """A reissue after a re-scope is judged against the new policy."""

        self.with_policy()
        self.issue()
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

        with self.assertRaises(KnowledgeRequestError) as raised:
            self.issue(now=INSIDE)

        self.assertEqual(raised.exception.code, "corpus_not_granted")
        self.assertEqual(self.ledger()["requests"][0]["policy_revision"], 1)


class PolicyTest(IssuerCase):
    def test_the_projection_names_the_four_contract_fields_only(self) -> None:
        policy = self.with_policy()

        self.assertEqual(set(policy), {"policy_revision", "connections"})
        self.assertEqual(
            policy["connections"],
            [
                {
                    "alias": "team-nas",
                    "upstream_workspace_uid": UPSTREAM_UID,
                    "corpus_refs": ["nas-team-share", "notion-product"],
                    "scope": "workspace",
                }
            ],
        )
        self.assertEqual(policy, issuer.read_connection_policy(self.store))

    def test_a_stale_expected_revision_refuses_and_writes_nothing(self) -> None:
        self.with_policy()
        before = self.ledger()

        with self.assertRaises(KnowledgeLedgerError) as raised:
            issuer.replace_connection_policy(
                self.store, expected_policy_revision=0, connections=()
            )

        self.assertEqual(raised.exception.code, "policy_revision_changed")
        self.assertEqual(self.ledger(), before)

    def test_each_accepted_replacement_advances_the_revision_by_one(self) -> None:
        self.assertEqual(self.with_policy()["policy_revision"], 1)
        self.assertEqual(
            issuer.replace_connection_policy(
                self.store, expected_policy_revision=1, connections=()
            )["policy_revision"],
            2,
        )


class OccupancyProjectionTest(IssuerCase):
    """Occupancy is a same-snapshot projection; policy returns stay two-field."""

    def test_policy_helpers_stay_policy_only_and_receipt_uses_one_document(self) -> None:
        policy = self.with_policy()
        self.assertEqual(set(policy), {"policy_revision", "connections"})
        self.assertEqual(policy, issuer.read_connection_policy(self.store))

        receipt = issuer.read_connection_policy_receipt(self.store)
        self.assertEqual(receipt.policy, policy)
        self.assertEqual(set(receipt.occupancy), issuer.OCCUPANCY_FIELDS)
        self.assertEqual(
            receipt.occupancy,
            issuer.project_knowledge_occupancy(self.ledger()),
        )
        self.assertIs(type(receipt.occupancy["request_count"]), int)
        self.assertEqual(receipt.occupancy["request_count"], 0)
        self.assertEqual(receipt.occupancy["request_bound"], MAX_REQUESTS)
        self.assertEqual(receipt.occupancy["byte_bound"], MAX_KNOWLEDGE_BYTES)
        self.assertEqual(
            receipt.occupancy["encoded_bytes"],
            len(compact_bytes(self.ledger())),
        )
        self.assertLess(
            receipt.occupancy["encoded_bytes"],
            (self.root / KNOWLEDGE_DOCUMENT_NAME).stat().st_size,
        )

    def test_a_replacement_receipt_projects_the_planned_document_not_a_reload(self) -> None:
        self.with_policy()
        loads: list[str] = []
        inner_load = self.store.load

        def counted_load(name: str) -> Any:
            loads.append(name)
            return inner_load(name)

        self.store.load = counted_load  # type: ignore[method-assign]
        try:
            receipt = issuer.replace_connection_policy_receipt(
                self.store, expected_policy_revision=1, connections=()
            )
        finally:
            self.store.load = inner_load  # type: ignore[method-assign]

        planned = self.ledger()
        self.assertEqual(set(receipt.policy), {"policy_revision", "connections"})
        self.assertEqual(receipt.policy["policy_revision"], 2)
        self.assertEqual(receipt.policy["connections"], [])
        self.assertEqual(
            receipt.occupancy, issuer.project_knowledge_occupancy(planned)
        )
        self.assertEqual(
            issuer.read_connection_policy(self.store),
            receipt.policy,
        )
        # The existing mutation already reads the ledger twice (outer CAS +
        # nested owner write). Occupancy must use the planned document those
        # writes returned, so it does not add a third knowledge load.
        self.assertEqual(
            [name for name in loads if name == KNOWLEDGE_DOCUMENT_NAME],
            [KNOWLEDGE_DOCUMENT_NAME, KNOWLEDGE_DOCUMENT_NAME],
        )

    def test_occupancy_counts_held_records_without_copying_them(self) -> None:
        self.with_policy()
        occupancy = issuer.project_knowledge_occupancy(
            {
                "version": 1,
                "policy_revision": 1,
                "connections": [],
                "requests": [{}, {}, {}],
            }
        )
        self.assertEqual(occupancy["request_count"], 3)
        self.assertEqual(occupancy["request_bound"], MAX_REQUESTS)
        self.assertEqual(occupancy["byte_bound"], MAX_KNOWLEDGE_BYTES)
        dumped = json.dumps(occupancy)
        self.assertNotIn("sha256:", dumped)
        self.assertNotIn("query", dumped)
        self.assertNotIn("request_id", dumped)
        self.assertNotIn("requests", dumped)


if __name__ == "__main__":
    unittest.main()
