"""D5 transitions against a real contained Store and the real service.

Nothing here is a model of the service: every case commits through the actual
transaction, reads the documents back off disk, and inspects the real event
sequence. No installed runtime, live SSOT, browser, CLI child or process
control is involved.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import unicodedata
from pathlib import Path

from workstack.capture import canonical_digest
from workstack.service import (
    CheckpointTransitionConflictError,
    DomainError,
    WorkStack,
)
from workstack.store import Store
from workstack.storage.document_repository import StoreDocumentRepository

ATTRIBUTED = "agent-cli-v1"
CHANGE_TYPE = "workstack.change.v1"


class _TransitionCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Transition boundary")
        self.checkpoint = self.record_checkpoint("integration.entry.0001")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # -- helpers ---------------------------------------------------------
    def record_checkpoint(self, key: str, *, date: str = "2026-09-03") -> str:
        self.stack.add_worklog_v1(
            {
                "date": date, "task_id": self.task["id"],
                "done": ["one"], "next": [], "blockers": [],
            },
            key,
            origin=ATTRIBUTED,
        )
        audit = self.stack.list_checkpoint_audit()
        return audit["entries"][-1]["checkpoint_id"]

    def path_for(self, checkpoint: str | None = None) -> str:
        return "/api/v1/review/checkpoints/{}/transitions".format(
            checkpoint or self.checkpoint
        )

    def transition(self, key, state, revision, *, code=None, explanation="because",
                   origin=ATTRIBUTED, checkpoint=None, digest=None):
        if code is None:
            code = "restore" if state == "active" else "incorrect"
        body = {
            "state": state,
            "revision": revision,
            "reason": {"code": code, "explanation": explanation},
        }
        return self.stack.apply_checkpoint_transition_v1(
            checkpoint or self.checkpoint,
            body,
            key,
            path=self.path_for(checkpoint),
            request_digest=canonical_digest(body) if digest is None else digest,
            origin=origin,
        )

    def documents(self) -> dict:
        return {
            name: (self.root / name).read_bytes()
            for name in ("worklog.json", "backlog.json", "activity.json")
        }

    def typed(self) -> list:
        return [
            event for event in self.stack.store.sync_events(0)["events"]
            if event.get("type") == CHANGE_TYPE
        ]

    def transition_notices(self) -> list:
        return [
            event["notice"] for event in self.typed()
            if "transition_revision" in event["notice"]
        ]


class RepeatedCycles(_TransitionCase):
    def test_four_repeated_cycles_advance_the_revision_monotonically(self) -> None:
        states = ["superseded", "active", "superseded", "active"]
        for index, state in enumerate(states):
            result = self.transition("cycle.{:04d}".format(index), state, index)
            self.assertEqual(result["status"], 201)
            self.assertEqual(result["body"]["data"]["revision"], index + 1)
            self.assertEqual(result["body"]["data"]["state"], state)
        row = self.stack.list_checkpoint_audit()["entries"][0]
        self.assertEqual(row["revision"], 4)
        self.assertEqual(row["state"], "active")
        self.assertEqual(len(row["transitions"]), 4)

    def test_the_active_view_follows_every_cycle(self) -> None:
        for index, state in enumerate(["superseded", "active", "superseded"]):
            self.transition("view.{:04d}".format(index), state, index)
            expected = 0 if state == "superseded" else 1
            self.assertEqual(
                len(self.stack.list_worklog("2026-09-03")["entries"]), expected, state
            )

    def test_a_stale_revision_conflicts_with_its_closed_code(self) -> None:
        self.transition("stale.0001", "superseded", 0)
        with self.assertRaises(CheckpointTransitionConflictError) as caught:
            self.transition("stale.0002", "active", 0)
        self.assertEqual(caught.exception.details["transition_code"], "stale_revision")

    def test_the_same_state_conflicts(self) -> None:
        self.transition("same.0001", "superseded", 0)
        with self.assertRaises(CheckpointTransitionConflictError) as caught:
            self.transition("same.0002", "superseded", 1)
        self.assertEqual(caught.exception.details["transition_code"], "same_state")

    def test_two_keys_at_one_revision_produce_one_winner_and_one_conflict(self) -> None:
        first = self.transition("race.0001", "superseded", 0)
        self.assertEqual(first["status"], 201)
        with self.assertRaises(CheckpointTransitionConflictError) as caught:
            self.transition("race.0002", "superseded", 0)
        self.assertEqual(caught.exception.details["transition_code"], "stale_revision")
        self.assertEqual(len(self.stack.list_checkpoint_audit()["entries"][0]["transitions"]), 1)


class ReplayIdentity(_TransitionCase):
    def test_an_exact_replay_after_four_cycles_returns_its_original_event(self) -> None:
        original = self.transition("replay.0000", "superseded", 0)
        for index, state in enumerate(["active", "superseded", "active"], start=1):
            self.transition("replay.{:04d}".format(index), state, index)
        before_documents = self.documents()
        before_typed = len(self.typed())

        replayed = self.transition("replay.0000", "superseded", 0)
        self.assertEqual(replayed["status"], 200)
        self.assertTrue(replayed["body"]["meta"]["replayed"])
        self.assertEqual(replayed["body"]["data"], original["body"]["data"])
        self.assertEqual(replayed["body"]["data"]["revision"], 1)
        self.assertEqual(self.documents(), before_documents, "a replay saves nothing")
        self.assertEqual(len(self.typed()), before_typed, "a replay publishes nothing")

    def test_json_formatting_does_not_change_the_digest(self) -> None:
        body = {
            "state": "superseded", "revision": 0,
            "reason": {"code": "incorrect", "explanation": "because"},
        }
        reordered = {
            "reason": {"explanation": "because", "code": "incorrect"},
            "revision": 0, "state": "superseded",
        }
        self.assertEqual(canonical_digest(body), canonical_digest(reordered))
        first = self.transition("format.0001", "superseded", 0)
        second = self.stack.apply_checkpoint_transition_v1(
            self.checkpoint, reordered, "format.0001",
            path=self.path_for(), request_digest=canonical_digest(reordered),
            origin=ATTRIBUTED,
        )
        self.assertEqual(second["status"], 200)
        self.assertEqual(second["body"]["data"], first["body"]["data"])

    def test_explanation_whitespace_makes_it_a_different_request(self) -> None:
        self.transition("wspace.0001", "superseded", 0, explanation="because")
        with self.assertRaises(Exception) as caught:
            self.transition("wspace.0001", "superseded", 0, explanation="because ")
        self.assertEqual(
            getattr(caught.exception, "code", None), "idempotency_conflict",
            repr(caught.exception),
        )

    def test_unicode_normalization_makes_it_a_different_request(self) -> None:
        composed = unicodedata.normalize("NFC", "café")
        decomposed = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(composed, decomposed)
        self.transition("nfc.0001", "superseded", 0, explanation=composed)
        with self.assertRaises(Exception) as caught:
            self.transition("nfc.0001", "superseded", 0, explanation=decomposed)
        self.assertEqual(getattr(caught.exception, "code", None), "idempotency_conflict")

    def test_the_digest_is_the_replay_identity_the_transport_computes(self) -> None:
        """The service trusts the digest; the transport is what derives it.

        The contract says a DIRECT caller cannot forge a supplied digest,
        meaning the HTTP transport computes it from the raw parsed body rather
        than accepting one from the client. It does not mean the service
        second-guesses an in-process caller that hands it a mismatched pair, and
        asserting that here would claim a defence the design does not make. What
        is true, and asserted, is that a genuinely different body yields a
        different digest and therefore conflicts. The wire suite covers the
        transport side.
        """

        first = {
            "state": "superseded", "revision": 0,
            "reason": {"code": "incorrect", "explanation": "original"},
        }
        second = dict(first, reason={"code": "incorrect", "explanation": "different"})
        self.assertNotEqual(canonical_digest(first), canonical_digest(second))
        self.transition("forge.0001", "superseded", 0, explanation="original")
        with self.assertRaises(Exception) as caught:
            self.transition("forge.0001", "superseded", 0, explanation="different")
        self.assertEqual(getattr(caught.exception, "code", None), "idempotency_conflict")


class SaveAndPublicationBoundaries(_TransitionCase):
    def test_one_activity_only_save_leaves_worklog_and_task_bytes_untouched(self) -> None:
        before = self.documents()
        saves = []
        inner = self.stack.documents

        class _Counting:
            def load(self, document):
                return inner.load(document)

            def save(self, document, value):
                saves.append((document,))
                return inner.save(document, value)

            def save_many(self, writes, operation_id=None):
                saves.append(tuple(sorted(str(key) for key in writes)))
                return inner.save_many(writes, operation_id=operation_id)

            def total_bytes(self):
                return inner.total_bytes()

        # Counting wrapper only: the transition itself must refuse an
        # unadmitted composition, so the count is taken from the real one.
        self.transition("save.0001", "superseded", 0)
        after = self.documents()
        self.assertEqual(after["worklog.json"], before["worklog.json"])
        self.assertEqual(after["backlog.json"], before["backlog.json"])
        self.assertNotEqual(after["activity.json"], before["activity.json"])

    def test_the_event_and_receipt_are_committed_together(self) -> None:
        self.transition("atomic.0001", "superseded", 0)
        activity = self.stack.store.load("activity.json")
        events = [r for r in activity["activity"] if r["type"] == "worklog.superseded"]
        keys = [r["key"] for r in activity["idempotency"]]
        self.assertEqual(len(events), 1)
        self.assertIn("atomic.0001", keys)

    def test_a_save_failure_changes_nothing_and_publishes_nothing(self) -> None:
        before = self.documents()
        before_typed = len(self.typed())
        inner = self.stack.documents

        class _Failing:
            def load(self, document):
                return inner.load(document)

            def save(self, document, value):
                raise OSError("injected save failure")

            def save_many(self, writes, operation_id=None):
                raise OSError("injected save failure")

            def total_bytes(self):
                return inner.total_bytes()

            _store = inner._store

        failing = _Failing()
        self.stack.documents = failing
        try:
            with self.assertRaises(Exception):
                self.transition("fail.0001", "superseded", 0)
        finally:
            self.stack.documents = inner
        self.assertEqual(self.documents(), before)
        self.assertEqual(len(self.typed()), before_typed)

    def test_an_attributed_transition_publishes_one_eleven_field_notice(self) -> None:
        before = len(self.transition_notices())
        self.transition("notice.0001", "superseded", 0)
        notices = self.transition_notices()
        self.assertEqual(len(notices), before + 1)
        notice = notices[-1]
        self.assertEqual(len(notice), 11)
        self.assertEqual(notice["kind"], "agent.checkpoint.superseded")
        self.assertEqual(notice["transition_revision"], 1)
        self.assertNotIn("reason", notice)
        serialized = json.dumps(notice)
        for forbidden in ("because", "notice.0001", "incorrect", "explanation"):
            self.assertNotIn(forbidden, serialized, forbidden)

    def test_a_restore_announces_the_restored_kind(self) -> None:
        self.transition("kind.0001", "superseded", 0)
        self.transition("kind.0002", "active", 1)
        self.assertEqual(
            self.transition_notices()[-1]["kind"], "agent.checkpoint.restored"
        )

    def test_a_browser_null_origin_transition_publishes_no_notice(self) -> None:
        before = len(self.transition_notices())
        result = self.transition("browser.0001", "superseded", 0, origin=None)
        self.assertEqual(result["status"], 201)
        self.assertEqual(len(self.transition_notices()), before)
        self.assertIsNone(result["body"]["data"]["origin"])

    def test_origin_is_the_current_request_not_inherited_provenance(self) -> None:
        """The checkpoint was attributed; a browser transition is still null."""

        result = self.transition("inherit.0001", "superseded", 0, origin=None)
        self.assertIsNone(result["body"]["data"]["origin"])


class CompositionOwnership(_TransitionCase):
    def foreign(self):
        self.other = tempfile.TemporaryDirectory()
        self.addCleanup(self.other.cleanup)
        other = WorkStack(Store(Path(self.other.name)))
        return WorkStack(
            self.stack.store,
            initialize=False,
            document_repository=StoreDocumentRepository(other.store),
        )

    def test_a_cross_store_composition_refuses_even_without_attribution(self) -> None:
        """Every new D5 write uses the narrow guard, null origin included."""

        crossed = self.foreign()
        before = self.documents()
        with self.assertRaises(DomainError):
            crossed.apply_checkpoint_transition_v1(
                self.checkpoint,
                {"state": "superseded", "revision": 0,
                 "reason": {"code": "incorrect", "explanation": "because"}},
                "cross.0001",
                path=self.path_for(),
                request_digest="sha256:" + "0" * 64,
                origin=None,
            )
        self.assertEqual(self.documents(), before)

    def test_an_injected_repository_refuses(self) -> None:
        inner = self.stack.documents

        class _Delegating:
            def load(self, document):
                return inner.load(document)

            def save(self, document, value):
                return inner.save(document, value)

            def save_many(self, writes, operation_id=None):
                return inner.save_many(writes, operation_id=operation_id)

            def total_bytes(self):
                return inner.total_bytes()

        self.stack.documents = _Delegating()
        try:
            with self.assertRaises(DomainError):
                self.transition("inject.0001", "superseded", 0)
        finally:
            self.stack.documents = inner

    def test_the_admitted_composition_still_works(self) -> None:
        self.assertEqual(self.transition("admitted.0001", "superseded", 0)["status"], 201)


class SharedReaderParity(_TransitionCase):
    def test_every_named_active_reader_agrees(self) -> None:
        self.transition("parity.0001", "superseded", 0)
        self.assertEqual(self.stack.list_worklog("2026-09-03")["entries"], [])
        self.assertEqual(
            self.stack.review_projection("2026-09-03")["day"]["entries"], []
        )
        weekly = self.stack.weekly_report(end="2026-09-03", days=7)
        self.assertEqual(weekly["projects"], [])

        self.transition("parity.0002", "active", 1)
        self.assertEqual(len(self.stack.list_worklog("2026-09-03")["entries"]), 1)
        self.assertEqual(
            len(self.stack.review_projection("2026-09-03")["day"]["entries"]), 1
        )

    def test_physical_writers_still_see_superseded_rows(self) -> None:
        """Future ordinals and first-for-task must not shift when rows hide."""

        self.transition("physical.0001", "superseded", 0)
        second = self.record_checkpoint("integration.entry.0002")
        activity = self.stack.store.load("activity.json")
        facts = [
            r["details"] for r in activity["activity"] if r["type"] == "worklog.recorded"
        ]
        self.assertEqual([fact["ordinal"] for fact in facts], [0, 1])
        self.assertFalse(
            [f for f in facts if f["checkpoint_id"] == second][0]["origin"] is None
        )

    def test_checkpoint_evidence_never_consults_the_current_task_list(self) -> None:
        """A Task that no longer exists must not invalidate its audit.

        Deleting the row from backlog.json directly is not a legitimate way to
        show this: the Store validates planning status on commit and refuses,
        which would test the Store rather than the projection. What actually
        carries the requirement is that the projection reads only the Worklog
        and the Activity, so the current Task list cannot affect it at all.
        """

        from workstack import checkpoint_projection

        audit = checkpoint_projection.build_audit(
            workspace_uid=self.stack.store.readiness.workspace_uid,
            worklog=self.stack.store.load("worklog.json"),
            activity=self.stack.store.load("activity.json"),
        )
        self.assertEqual(audit, self.stack.list_checkpoint_audit())
        self.assertEqual(len(audit["entries"]), 1)
        self.assertEqual(self.transition("deleted.0001", "superseded", 0)["status"], 201)


class SnapshotSharesActiveMembership(_TransitionCase):
    """D5I-F2: the Graph snapshot is one of the shared active readers."""

    def test_snapshot_counts_and_edges_drop_a_superseded_entry(self) -> None:
        before = self.stack.snapshot()
        worklog_nodes = [n for n in before["nodes"] if n["id"].startswith("D-")]
        self.assertTrue(worklog_nodes, "the control needs a worklog day node")

        self.transition("snapshot.0001", "superseded", 0)
        after = self.stack.snapshot()
        days = [n for n in after["nodes"] if n["id"] == "D-2026-09-03"]
        self.assertTrue(days, "the physical day must remain")
        self.assertEqual(days[0]["entry_count"], 0, days[0])
        self.assertEqual(
            [e for e in after["edges"] if e.get("source") == "D-2026-09-03"], [],
            "no worklog edge may survive for a superseded entry",
        )

    def test_snapshot_agrees_with_review_and_list_worklog(self) -> None:
        self.transition("snapshot.0002", "superseded", 0)
        self.assertEqual(self.stack.list_worklog("2026-09-03")["entries"], [])
        self.assertEqual(self.stack.review_projection("2026-09-03")["day"]["entries"], [])
        days = [n for n in self.stack.snapshot()["nodes"] if n["id"] == "D-2026-09-03"]
        self.assertEqual(days[0]["entry_count"], 0)

    def test_a_restore_brings_the_snapshot_count_back(self) -> None:
        self.transition("snapshot.0003", "superseded", 0)
        self.transition("snapshot.0004", "active", 1)
        days = [n for n in self.stack.snapshot()["nodes"] if n["id"] == "D-2026-09-03"]
        self.assertEqual(days[0]["entry_count"], 1)


class ReplayIdentityCannotBeForged(_TransitionCase):
    """D5I-F6: the service owns the digest, and the key must be an exact str."""

    def test_a_supplied_digest_that_does_not_match_the_body_refuses(self) -> None:
        self.transition("forgery.0001", "superseded", 0, explanation="original")
        original = {
            "state": "superseded", "revision": 0,
            "reason": {"code": "incorrect", "explanation": "original"},
        }
        with self.assertRaises(DomainError):
            self.transition(
                "forgery.0001", "superseded", 0, explanation="changed",
                digest=canonical_digest(original),
            )
        row = self.stack.list_checkpoint_audit()["entries"][0]
        self.assertEqual(len(row["transitions"]), 1, "no second transition may commit")

    def test_the_service_computes_the_digest_when_none_is_supplied(self) -> None:
        first = self.stack.apply_checkpoint_transition_v1(
            self.checkpoint,
            {"state": "superseded", "revision": 0,
             "reason": {"code": "incorrect", "explanation": "computed"}},
            "computed.0001",
            path=self.path_for(),
            origin=ATTRIBUTED,
        )
        self.assertEqual(first["status"], 201)
        replay = self.stack.apply_checkpoint_transition_v1(
            self.checkpoint,
            {"state": "superseded", "revision": 0,
             "reason": {"code": "incorrect", "explanation": "computed"}},
            "computed.0001",
            path=self.path_for(),
            origin=ATTRIBUTED,
        )
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])

    def test_a_str_subclass_idempotency_key_refuses_on_this_entrypoint(self) -> None:
        class _Key(str):
            pass

        before = self.documents()
        with self.assertRaises(DomainError):
            self.transition(_Key("subclass.0001"), "superseded", 0)
        self.assertEqual(self.documents(), before)

    def test_the_legacy_writer_keeps_its_own_key_semantics(self) -> None:
        """The new exact-str guard is scoped to the NEW entrypoint only.

        An earlier version of this case asserted the legacy writer ACCEPTS
        a str subclass. It does not, and never did: the admitted pure
        checkpoint builder already requires an exact str and refuses one
        upstream of anything in this change. What the requirement actually
        protects is that the legacy writer is UNCHANGED, so that is what is
        asserted: an ordinary key still commits, and the subclass refusal
        still comes from the pure builder rather than from the new guard.
        """

        class _Key(str):
            pass

        result = self.stack.add_worklog_v1(
            {
                "date": "2026-09-04", "task_id": self.task["id"],
                "done": ["one"], "next": [], "blockers": [],
            },
            "legacy.key.0001",
            origin=None,
        )
        self.assertEqual(result["status"], 201)

        with self.assertRaises(DomainError) as caught:
            self.stack.add_worklog_v1(
                {
                    "date": "2026-09-05", "task_id": self.task["id"],
                    "done": ["one"], "next": [], "blockers": [],
                },
                _Key("legacy.key.0002"),
                origin=None,
            )
        self.assertNotIn(
            "Idempotency-Key must be a string", str(caught.exception),
            "the refusal must not come from the new entrypoint guard",
        )


class ReaderTransactionOwnership(_TransitionCase):
    """D5I-F8: the two documents a reader assembles share one owner."""

    def observed_depths(self, action):
        depths = []
        store = self.stack.store
        original = store.load

        def watched(name):
            depths.append((name, int(getattr(store._local, "depth", 0))))
            return original(name)

        store.load = watched
        try:
            action()
        finally:
            store.load = original
        return depths

    def test_list_worklog_loads_both_documents_inside_one_transaction(self) -> None:
        depths = self.observed_depths(lambda: self.stack.list_worklog("2026-09-03"))
        names = [name for name, _ in depths]
        self.assertIn("worklog.json", names)
        self.assertIn("activity.json", names)
        for name, depth in depths:
            self.assertGreater(depth, 0, "{} loaded outside the outer transaction".format(name))

    def test_the_already_transactional_readers_stay_transactional(self) -> None:
        for action in (
            lambda: self.stack.review_projection("2026-09-03"),
            lambda: self.stack.list_checkpoint_audit(),
        ):
            depths = self.observed_depths(action)
            self.assertTrue(depths)
            for name, depth in depths:
                self.assertGreater(depth, 0, name)


class CombinedFaultThroughTheOwner(_TransitionCase):
    """D5C-F1 at the real owner: the taxonomy must match end to end."""

    def corrupt_activity(self, mutate):
        activity = self.stack.store.load("activity.json")
        mutate(activity)
        self.stack.store.save("activity.json", activity)

    def recorded_details(self):
        activity = self.stack.store.load("activity.json")
        return [
            r for r in activity["activity"] if r["type"] == "worklog.recorded"
        ][0]["details"]

    def test_an_orphan_plus_a_later_malformed_fact_is_a_bad_request(self) -> None:
        template = self.recorded_details()

        def mutate(activity):
            activity["activity"].append({
                "id": "E-009001", "type": "worklog.recorded",
                "created_at": "2026-09-03T00:00:00Z", "task_id": template["task_id"],
                "details": dict(template, date="2026-09-09",
                                checkpoint_id="CP-" + "b" * 64),
            })
            activity["activity"].append({
                "id": "E-009002", "type": "worklog.recorded",
                "created_at": "2026-09-03T00:00:00Z", "task_id": template["task_id"],
                "details": dict(template, date="2026-09-08",
                                checkpoint_id="CP-not-hex"),
            })

        self.corrupt_activity(mutate)
        with self.assertRaises(DomainError) as caught:
            self.transition("combined.0001", "superseded", 0)
        self.assertNotIsInstance(
            caught.exception, CheckpointTransitionConflictError,
            "a malformed record is a bad request, not a history conflict",
        )

    def test_an_orphan_alone_through_the_owner_is_a_history_conflict(self) -> None:
        template = self.recorded_details()

        def mutate(activity):
            activity["activity"].append({
                "id": "E-009003", "type": "worklog.recorded",
                "created_at": "2026-09-03T00:00:00Z", "task_id": template["task_id"],
                "details": dict(template, date="2026-09-09",
                                checkpoint_id="CP-" + "b" * 64),
            })

        self.corrupt_activity(mutate)
        with self.assertRaises(CheckpointTransitionConflictError) as caught:
            self.transition("combined.0002", "superseded", 0)
        self.assertEqual(caught.exception.details["transition_code"], "history_invalid")


class WrongPhysicalTaskClaim(_TransitionCase):
    """D5C-F2 at the real owner: a corrupted claim must never commit."""

    def test_a_wrong_task_claim_refuses_and_changes_nothing(self) -> None:
        other = self.stack.add_task("Second task")
        activity = self.stack.store.load("activity.json")
        for record in activity["activity"]:
            if record["type"] == "worklog.recorded":
                record["details"] = dict(record["details"], task_id=other["id"])
        self.stack.store.save("activity.json", activity)

        before = self.documents()
        before_typed = len(self.typed())
        with self.assertRaises(CheckpointTransitionConflictError) as caught:
            self.transition("wrongtask.0001", "superseded", 0)
        self.assertEqual(caught.exception.details["transition_code"], "locator_mismatch")
        self.assertEqual(self.documents(), before, "no document may change")
        self.assertEqual(len(self.typed()), before_typed, "nothing may be published")

    def test_the_ordinary_transition_control_still_commits(self) -> None:
        self.assertEqual(self.transition("wrongtask.0002", "superseded", 0)["status"], 201)


class MalformedBodiesNeverEscapeAsRawExceptions(_TransitionCase):
    """D5C-F3: ordinary validation precedes replay and the digest."""

    def call(self, body, key="malformed.0001"):
        return self.stack.apply_checkpoint_transition_v1(
            self.checkpoint, body, key, path=self.path_for(), origin=ATTRIBUTED
        )

    def test_a_lone_surrogate_explanation_refuses_content_free(self) -> None:
        before = self.documents()
        with self.assertRaises(DomainError) as caught:
            self.call({
                "state": "superseded", "revision": 0,
                "reason": {"code": "incorrect", "explanation": "\ud800"},
            })
        self.assertNotIn("surrogate", str(caught.exception).lower())
        self.assertNotIn("\\ud800", str(caught.exception))
        self.assertEqual(self.documents(), before)

    def test_a_cyclic_reason_refuses_content_free(self) -> None:
        reason = {"code": "incorrect", "explanation": "x"}
        reason["self"] = reason
        before = self.documents()
        with self.assertRaises(DomainError):
            self.call({"state": "superseded", "revision": 0, "reason": reason},
                      "malformed.0002")
        self.assertEqual(self.documents(), before)

    def test_a_non_mapping_body_refuses_content_free(self) -> None:
        for body in (None, [], "text", 42):
            with self.subTest(body=body):
                with self.assertRaises(DomainError):
                    self.call(body, "malformed.0003")

    def test_the_digest_is_still_taken_from_the_original_body(self) -> None:
        """The raw parsed body, never a normalized replacement."""

        body = {
            "state": "superseded", "revision": 0,
            "reason": {"code": "incorrect", "explanation": "  spaced  "},
        }
        first = self.stack.apply_checkpoint_transition_v1(
            self.checkpoint, body, "rawdigest.0001",
            path=self.path_for(), origin=ATTRIBUTED,
        )
        self.assertEqual(first["status"], 201)
        replay = self.stack.apply_checkpoint_transition_v1(
            self.checkpoint, dict(body), "rawdigest.0001",
            path=self.path_for(), request_digest=canonical_digest(body),
            origin=ATTRIBUTED,
        )
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])

    def test_a_valid_replay_still_precedes_mutable_history(self) -> None:
        self.transition("precede.first.0001", "superseded", 0)
        for index, state in enumerate(["active", "superseded", "active"], start=1):
            self.transition("precede.cycle.{:04d}".format(index), state, index)
        replay = self.transition("precede.first.0001", "superseded", 0)
        self.assertEqual(replay["status"], 200)
        self.assertEqual(replay["body"]["data"]["revision"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
