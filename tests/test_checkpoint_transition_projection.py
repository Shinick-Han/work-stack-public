"""The in-memory adapter between stored documents and the pure D5 contract.

Every assertion here runs the real adapter against real document shapes. The
adapter owns nothing durable, so these cases construct no Store, open no
transaction and touch no disk: that is exactly the property under test.

The load-bearing theme is that the adapter decides nothing. It rearranges;
the pure contract judges. So the cases below check that multiplicity, ordering
and malformed records all survive the trip intact, because silently collapsing
or dropping any of them would turn a refusal into an invisible omission.
"""

from __future__ import annotations

import copy
import hashlib
import unittest

from workstack.checkpoint_projection import (
    active_worklog_document,
    build_audit,
    build_projection_context,
    physical_locator_for,
)
from workstack.checkpoint_transition import CheckpointTransitionError
from workstack.storage.canonical import canonical_json_bytes

WORKSPACE = "11111111-2222-4333-8444-555555555555"
OTHER_WORKSPACE = "99999999-8888-4777-8666-555555555555"


def entry(task_id="T-0001", done=("one",)):
    return {
        "task_id": task_id,
        "task": "A title",
        "done": list(done),
        "next": [],
        "blockers": [],
    }


def digest(value):
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def recorded(row, *, date, ordinal, checkpoint="a", workspace=WORKSPACE, origin=None):
    return {
        "type": "worklog.recorded",
        "workspace_uid": workspace,
        "task_id": row["task_id"],
        "checkpoint_id": "CP-" + checkpoint * 64,
        "date": date,
        "ordinal": ordinal,
        "entry_digest": digest(row),
        "origin": origin,
    }


def activity(*records):
    return {"version": 2, "activity": list(records), "idempotency": [], "planning_status": []}


def feed(kind, details, identifier="E-000001"):
    return {
        "id": identifier,
        "type": kind,
        "created_at": "2026-09-03T00:00:00Z",
        "task_id": "T-0001",
        "details": details,
    }


def worklog(days):
    return {"days": days}


class ContextConstruction(unittest.TestCase):
    """One wrapper per physical entry, in physical order, nothing collapsed."""

    def test_every_physical_entry_becomes_exactly_one_wrapper(self) -> None:
        first, second = entry(done=("a",)), entry(done=("b",))
        context = build_projection_context(
            workspace_uid=WORKSPACE,
            worklog=worklog({"2026-09-03": {"entries": [first, second]}}),
            activity=activity(),
        )
        self.assertEqual(len(context["entries"]), 2)
        self.assertEqual(
            [wrapper["locator"]["ordinal"] for wrapper in context["entries"]], [0, 1]
        )
        self.assertIs(context["entries"][0]["entry"], first)
        self.assertIs(context["entries"][1]["entry"], second)

    def test_identical_entries_are_not_deduplicated(self) -> None:
        """Two byte-identical rows are two physical facts, not one."""

        same = entry()
        context = build_projection_context(
            workspace_uid=WORKSPACE,
            worklog=worklog({"2026-09-03": {"entries": [copy.deepcopy(same), copy.deepcopy(same)]}}),
            activity=activity(),
        )
        self.assertEqual(len(context["entries"]), 2)

    def test_days_are_visited_in_date_order_and_none_is_dropped(self) -> None:
        context = build_projection_context(
            workspace_uid=WORKSPACE,
            worklog=worklog({
                "2026-09-20": {"entries": [entry(done=("late",))]},
                "2026-09-01": {"entries": [entry(done=("early",))]},
            }),
            activity=activity(),
        )
        self.assertEqual(
            [wrapper["locator"]["date"] for wrapper in context["entries"]],
            ["2026-09-01", "2026-09-20"],
        )

    def test_a_legacy_row_carries_null_coordinates_and_is_not_invented(self) -> None:
        context = build_projection_context(
            workspace_uid=WORKSPACE,
            worklog=worklog({"2026-09-03": {"entries": [{"note": "opaque legacy row"}]}}),
            activity=activity(),
        )
        wrapper = context["entries"][0]
        self.assertIsNone(wrapper["recorded"], "a legacy row has no recorded fact")
        self.assertIsNone(wrapper["locator"]["task_id"])
        self.assertEqual(wrapper["entry"], {"note": "opaque legacy row"})

    def test_a_malformed_recognized_record_refuses_rather_than_vanishing(self) -> None:
        """Corrected: it is refused, not carried and not skipped.

        An earlier version of this case asserted the malformed details simply
        travelled onward. That let a recognized record reach the pure contract
        in a shape the contract could not judge, which is how several records
        disappeared instead of refusing.
        """

        with self.assertRaises(CheckpointTransitionError):
            build_projection_context(
                workspace_uid=WORKSPACE,
                worklog=worklog({"2026-09-03": {"entries": [entry()]}}),
                activity=activity(feed("worklog.superseded", {"not": "an event"})),
            )

    def test_transitions_keep_their_stored_order(self) -> None:
        row = entry()
        fact = recorded(row, date="2026-09-03", ordinal=0)

        def event(state, revision):
            return {
                "type": "worklog.superseded" if state == "superseded" else "worklog.restored",
                "workspace_uid": WORKSPACE,
                "task_id": row["task_id"],
                "checkpoint_id": fact["checkpoint_id"],
                "date": "2026-09-03",
                "ordinal": 0,
                "entry_digest": digest(row),
                "state": state,
                "revision": revision,
                "reason": {"code": "incorrect" if state == "superseded" else "restore",
                           "explanation": "n"},
                "origin": None,
            }

        ordered = [event("superseded", 1), event("active", 2), event("superseded", 3)]
        context = build_projection_context(
            workspace_uid=WORKSPACE,
            worklog=worklog({"2026-09-03": {"entries": [row]}}),
            activity=activity(
                feed("worklog.recorded", fact),
                *[feed(e["type"], e, "E-00000{}".format(i)) for i, e in enumerate(ordered, 2)],
            ),
        )
        self.assertEqual([t["revision"] for t in context["transitions"]], [1, 2, 3])

    def test_unrecognized_records_are_ignored_entirely(self) -> None:
        context = build_projection_context(
            workspace_uid=WORKSPACE,
            worklog=worklog({"2026-09-03": {"entries": [entry()]}}),
            activity=activity(feed("task.created", {"anything": True})),
        )
        self.assertEqual(context["transitions"], [])
        self.assertIsNone(context["entries"][0]["recorded"])


class LocatorDerivation(unittest.TestCase):
    """The locator is recomputed from the row, never copied from the fact."""

    def setUp(self) -> None:
        self.row = entry()
        self.fact = recorded(self.row, date="2026-09-03", ordinal=0)
        self.worklog = worklog({"2026-09-03": {"entries": [self.row]}})
        self.activity = activity(feed("worklog.recorded", self.fact))

    def test_a_matching_checkpoint_derives_the_actual_slot(self) -> None:
        locator, row = physical_locator_for(
            workspace_uid=WORKSPACE,
            checkpoint_id=self.fact["checkpoint_id"],
            worklog=self.worklog,
            activity=self.activity,
        )
        self.assertEqual(locator["date"], "2026-09-03")
        self.assertEqual(locator["ordinal"], 0)
        self.assertEqual(locator["entry_digest"], digest(self.row))
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["revision"], 0)

    def test_an_unknown_checkpoint_refuses(self) -> None:
        with self.assertRaises(CheckpointTransitionError) as caught:
            physical_locator_for(
                workspace_uid=WORKSPACE,
                checkpoint_id="CP-" + "b" * 64,
                worklog=self.worklog,
                activity=self.activity,
            )
        self.assertEqual(caught.exception.code, "locator_mismatch")

    def test_a_fact_whose_digest_no_longer_matches_the_row_refuses(self) -> None:
        """Recorded metadata is never accepted as evidence of the row."""

        drifted = worklog({"2026-09-03": {"entries": [entry(done=("changed",))]}})
        with self.assertRaises(CheckpointTransitionError):
            physical_locator_for(
                workspace_uid=WORKSPACE,
                checkpoint_id=self.fact["checkpoint_id"],
                worklog=drifted,
                activity=self.activity,
            )

    def test_a_foreign_workspace_fact_refuses(self) -> None:
        foreign = activity(
            feed("worklog.recorded", recorded(
                self.row, date="2026-09-03", ordinal=0, workspace=OTHER_WORKSPACE
            ))
        )
        with self.assertRaises(CheckpointTransitionError):
            physical_locator_for(
                workspace_uid=WORKSPACE,
                checkpoint_id=self.fact["checkpoint_id"],
                worklog=self.worklog,
                activity=foreign,
            )


class ActiveProjection(unittest.TestCase):
    """What the shared active readers see, and what they must keep seeing."""

    def setUp(self) -> None:
        self.row = entry()
        self.fact = recorded(self.row, date="2026-09-03", ordinal=0)
        self.worklog = {
            "days": {
                "2026-09-03": {"start_time": "09:00", "entries": [self.row]},
                "2026-09-04": {"entries": []},
            }
        }

    def event(self, state, revision):
        return {
            "type": "worklog.superseded" if state == "superseded" else "worklog.restored",
            "workspace_uid": WORKSPACE,
            "task_id": self.row["task_id"],
            "checkpoint_id": self.fact["checkpoint_id"],
            "date": "2026-09-03",
            "ordinal": 0,
            "entry_digest": digest(self.row),
            "state": state,
            "revision": revision,
            "reason": {"code": "incorrect", "explanation": "wrong"}
            if state == "superseded"
            else {"code": "restore", "explanation": "back"},
            "origin": None,
        }

    def project(self, *transitions):
        return active_worklog_document(
            workspace_uid=WORKSPACE,
            worklog=self.worklog,
            activity=activity(
                feed("worklog.recorded", self.fact),
                *[feed(e["type"], e) for e in transitions],
            ),
        )

    def test_an_untouched_entry_stays_active(self) -> None:
        projected = self.project()
        self.assertEqual(len(projected["days"]["2026-09-03"]["entries"]), 1)

    def test_a_superseded_entry_disappears_from_the_active_view(self) -> None:
        projected = self.project(self.event("superseded", 1))
        self.assertEqual(projected["days"]["2026-09-03"]["entries"], [])

    def test_a_restored_entry_returns_to_the_active_view(self) -> None:
        projected = self.project(self.event("superseded", 1), self.event("active", 2))
        self.assertEqual(len(projected["days"]["2026-09-03"]["entries"]), 1)

    def test_day_metadata_and_empty_days_are_preserved(self) -> None:
        projected = self.project(self.event("superseded", 1))
        self.assertEqual(projected["days"]["2026-09-03"]["start_time"], "09:00")
        self.assertIn(
            "2026-09-04", projected["days"],
            "an already empty day is a fact, not an absence",
        )

    def test_the_source_document_is_never_mutated(self) -> None:
        before = copy.deepcopy(self.worklog)
        self.project(self.event("superseded", 1))
        self.assertEqual(self.worklog, before, "physical writers must see the raw rows")

    def test_the_audit_keeps_the_complete_reason_history(self) -> None:
        audit = build_audit(
            workspace_uid=WORKSPACE,
            worklog=self.worklog,
            activity=activity(
                feed("worklog.recorded", self.fact),
                feed("worklog.superseded", self.event("superseded", 1)),
                feed("worklog.restored", self.event("active", 2)),
            ),
        )
        self.assertEqual(sorted(audit), ["entries", "workspace_uid"])
        row = audit["entries"][0]
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["revision"], 2)
        self.assertEqual(len(row["transitions"]), 2)
        self.assertEqual(
            [t["reason"]["code"] for t in row["transitions"]], ["incorrect", "restore"]
        )

    def test_a_malformed_history_refuses_rather_than_falling_back(self) -> None:
        broken = self.event("superseded", 1)
        broken["revision"] = 0
        with self.assertRaises(CheckpointTransitionError):
            self.project(broken)


class RecognizedEvidenceIsNeverLost(unittest.TestCase):
    """D5I-F1: every recognized record and known envelope is validated.

    The adapter may not quietly drop, collapse or null out evidence. A record
    the product recognizes by type is either validated or refused; it never
    disappears, and it never turns a known checkpoint row into a legacy one.
    """

    def setUp(self) -> None:
        self.row = entry()
        self.fact = recorded(self.row, date="2026-09-03", ordinal=0)
        self.worklog = worklog({"2026-09-03": {"entries": [self.row]}})

    def audit(self, *records):
        return build_audit(
            workspace_uid=WORKSPACE,
            worklog=self.worklog,
            activity=activity(feed("worklog.recorded", self.fact), *records),
        )

    def test_the_healthy_control_still_builds(self) -> None:
        audited = self.audit()
        self.assertEqual(len(audited["entries"]), 1)
        self.assertEqual(audited["entries"][0]["checkpoint_id"], self.fact["checkpoint_id"])

    def test_a_recognized_record_with_unusable_details_refuses(self) -> None:
        for details in (None, 42, {}, [], "text"):
            with self.subTest(details=details):
                with self.assertRaises(CheckpointTransitionError):
                    self.audit(feed("worklog.recorded", details, "E-000002"))

    def test_a_recognized_fact_with_a_non_string_date_refuses_content_free(self) -> None:
        broken = dict(self.fact, date=[], checkpoint_id="CP-" + "b" * 64)
        with self.assertRaises(CheckpointTransitionError):
            self.audit(feed("worklog.recorded", broken, "E-000002"))

    def test_an_orphan_fact_for_a_missing_row_refuses(self) -> None:
        orphan = dict(self.fact, date="2026-09-09", checkpoint_id="CP-" + "b" * 64)
        with self.assertRaises(CheckpointTransitionError):
            self.audit(feed("worklog.recorded", orphan, "E-000002"))

    def test_two_facts_claiming_one_slot_refuse_instead_of_nulling_the_row(self) -> None:
        second = dict(self.fact, checkpoint_id="CP-" + "b" * 64)
        with self.assertRaises(CheckpointTransitionError):
            self.audit(feed("worklog.recorded", second, "E-000002"))

    def test_outer_and_inner_record_types_must_agree(self) -> None:
        event = {
            "type": "worklog.superseded",
            "workspace_uid": WORKSPACE,
            "task_id": self.row["task_id"],
            "checkpoint_id": self.fact["checkpoint_id"],
            "date": "2026-09-03",
            "ordinal": 0,
            "entry_digest": digest(self.row),
            "state": "superseded",
            "revision": 1,
            "reason": {"code": "incorrect", "explanation": "x"},
            "origin": None,
        }
        self.assertEqual(self.audit(feed("worklog.superseded", event, "E-000002"))
                         ["entries"][0]["state"], "superseded")
        with self.assertRaises(CheckpointTransitionError):
            self.audit(feed("worklog.restored", event, "E-000003"))

    def test_a_malformed_later_record_outranks_an_earlier_binding_fault(self) -> None:
        foreign = dict(self.fact, workspace_uid=OTHER_WORKSPACE)
        with self.assertRaises(CheckpointTransitionError) as caught:
            build_audit(
                workspace_uid=WORKSPACE,
                worklog=self.worklog,
                activity=activity(
                    feed("worklog.recorded", foreign),
                    feed("worklog.recorded", 42, "E-000002"),
                ),
            )
        self.assertEqual(
            caught.exception.code, "malformed",
            "global syntax precedes binding, so the later malformed record wins",
        )

    def test_an_invalid_known_day_envelope_refuses(self) -> None:
        for days in (
            {"bad-date": {"entries": []}},
            {"2026-09-03": {"entries": None}},
            {"2026-09-03": None},
            {"2026-09-03": {"entries": [entry()]}, "also-bad": {"entries": []}},
        ):
            with self.subTest(days=days):
                with self.assertRaises(CheckpointTransitionError):
                    build_audit(
                        workspace_uid=WORKSPACE, worklog=worklog(days), activity=activity()
                    )

    def test_a_well_formed_empty_day_is_still_accepted(self) -> None:
        audited = build_audit(
            workspace_uid=WORKSPACE,
            worklog=worklog({"2026-09-03": {"entries": []}, "2026-09-04": {"entries": []}}),
            activity=activity(),
        )
        self.assertEqual(audited["entries"], [])


class OpaqueLegacyIdentityIsNotTightened(unittest.TestCase):
    """D5I-F7: an unrecorded opaque row may not be forced into known identity."""

    def audit_for(self, opaque):
        return build_audit(
            workspace_uid=WORKSPACE,
            worklog=worklog({"2026-09-03": {"entries": [opaque]}}),
            activity=activity(),
        )

    def test_unusable_legacy_task_identity_becomes_null(self) -> None:
        for task_id in (42, False, "legacy-task", "", None, ["T-0001"]):
            with self.subTest(task_id=task_id):
                audited = self.audit_for({"task_id": task_id, "note": "opaque"})
                row = audited["entries"][0]
                self.assertIsNone(row["locator"]["task_id"])
                self.assertIsNone(row["recorded"])

    def test_the_opaque_entry_object_is_preserved_verbatim(self) -> None:
        opaque = {"task_id": 42, "note": "opaque", "count": 3}
        audited = self.audit_for(opaque)
        self.assertEqual(audited["entries"][0]["entry"], opaque)

    def test_a_canonical_legacy_task_id_is_still_carried(self) -> None:
        audited = self.audit_for({"task_id": "T-0001", "note": "opaque"})
        self.assertEqual(audited["entries"][0]["locator"]["task_id"], "T-0001")

    def test_recorded_identity_stays_strict(self) -> None:
        """The positive control: a real recorded row is unaffected."""

        row = entry()
        audited = build_audit(
            workspace_uid=WORKSPACE,
            worklog=worklog({"2026-09-03": {"entries": [row]}}),
            activity=activity(
                feed("worklog.recorded", recorded(row, date="2026-09-03", ordinal=0))
            ),
        )
        self.assertEqual(audited["entries"][0]["locator"]["task_id"], "T-0001")


class CombinedFaultTaxonomy(unittest.TestCase):
    """D5C-F1: complete syntax, then all binding, then association.

    A container check is not syntax. When two faults are present the earlier
    PHASE must win regardless of which record carries it, so a malformed record
    anywhere outranks a binding fault anywhere, and both outrank association.
    """

    def setUp(self) -> None:
        self.row = entry()
        self.fact = recorded(self.row, date="2026-09-03", ordinal=0)
        self.worklog = worklog({"2026-09-03": {"entries": [self.row]}})

    def build(self, *extra):
        return build_audit(
            workspace_uid=WORKSPACE,
            worklog=self.worklog,
            activity=activity(feed("worklog.recorded", self.fact), *extra),
        )

    def orphan(self, **overrides):
        base = dict(
            self.fact, date="2026-09-09", checkpoint_id="CP-" + "b" * 64
        )
        base.update(overrides)
        return feed("worklog.recorded", base, "E-000002")

    def duplicate(self, **overrides):
        base = dict(self.fact, checkpoint_id="CP-" + "c" * 64)
        base.update(overrides)
        return feed("worklog.recorded", base, "E-000002")

    def code_for(self, *extra):
        with self.assertRaises(CheckpointTransitionError) as caught:
            self.build(*extra)
        return caught.exception.code

    # -- association alone stays a history verdict --------------------------
    def test_an_orphan_alone_is_history_invalid(self) -> None:
        self.assertEqual(self.code_for(self.orphan()), "history_invalid")

    def test_a_duplicate_alone_is_history_invalid(self) -> None:
        self.assertEqual(self.code_for(self.duplicate()), "history_invalid")

    # -- a later malformed record outranks an earlier association fault -----
    def test_an_orphan_plus_a_later_malformed_fact_is_malformed(self) -> None:
        for overrides in (
            {"checkpoint_id": "CP-not-hex"},
            {"task_id": 42},
            {"ordinal": -1},
            {"date": "2026-02-30"},
        ):
            with self.subTest(overrides=overrides):
                details = dict(self.fact, date="2026-09-08")
                details.update(overrides)
                later = feed("worklog.recorded", details, "E-000003")
                self.assertEqual(
                    self.code_for(self.orphan(), later), "malformed", overrides
                )

    def test_a_duplicate_plus_a_later_malformed_transition_is_malformed(self) -> None:
        broken = {
            "type": "worklog.superseded",
            "workspace_uid": WORKSPACE,
            "task_id": self.row["task_id"],
            "checkpoint_id": self.fact["checkpoint_id"],
            "date": "2026-09-03",
            "ordinal": 0,
            "entry_digest": digest(self.row),
            "state": "superseded",
            "revision": "one",
            "reason": {"code": "incorrect", "explanation": "x"},
            "origin": None,
        }
        self.assertEqual(
            self.code_for(self.duplicate(), feed("worklog.superseded", broken, "E-000003")),
            "malformed",
        )

    # -- binding outranks association ---------------------------------------
    def test_a_duplicate_plus_a_foreign_workspace_is_a_locator_mismatch(self) -> None:
        self.assertEqual(
            self.code_for(self.duplicate(workspace_uid=OTHER_WORKSPACE)),
            "locator_mismatch",
        )

    # -- required fields are read only after validation ---------------------
    def test_a_fact_missing_task_id_refuses_content_free(self) -> None:
        missing = {k: v for k, v in self.fact.items() if k != "task_id"}
        missing["checkpoint_id"] = "CP-" + "b" * 64
        with self.assertRaises(CheckpointTransitionError) as caught:
            self.build(feed("worklog.recorded", missing, "E-000002"))
        self.assertEqual(caught.exception.code, "malformed")

    def test_the_healthy_control_still_builds(self) -> None:
        audited = self.build()
        self.assertEqual(len(audited["entries"]), 1)
        self.assertEqual(audited["entries"][0]["state"], "active")


class PhysicalTaskIdentity(unittest.TestCase):
    """D5C-F2: the Task coordinate comes from the physical row, never a claim."""

    def test_a_recorded_claim_cannot_override_the_physical_task(self) -> None:
        row = entry(task_id="T-0001")
        claimed = dict(
            recorded(row, date="2026-09-03", ordinal=0), task_id="T-0002"
        )
        with self.assertRaises(CheckpointTransitionError) as caught:
            build_audit(
                workspace_uid=WORKSPACE,
                worklog=worklog({"2026-09-03": {"entries": [row]}}),
                activity=activity(feed("worklog.recorded", claimed)),
            )
        self.assertEqual(caught.exception.code, "locator_mismatch")

    def test_the_locator_reports_the_physical_task(self) -> None:
        row = entry(task_id="T-0007")
        fact = dict(recorded(row, date="2026-09-03", ordinal=0), task_id="T-0007")
        audited = build_audit(
            workspace_uid=WORKSPACE,
            worklog=worklog({"2026-09-03": {"entries": [row]}}),
            activity=activity(feed("worklog.recorded", fact)),
        )
        self.assertEqual(audited["entries"][0]["locator"]["task_id"], "T-0007")

    def test_opaque_unrecorded_identity_still_becomes_null(self) -> None:
        for task_id in (42, False, "legacy-task", None):
            with self.subTest(task_id=task_id):
                audited = build_audit(
                    workspace_uid=WORKSPACE,
                    worklog=worklog({"2026-09-03": {"entries": [{"task_id": task_id}]}}),
                    activity=activity(),
                )
                self.assertIsNone(audited["entries"][0]["locator"]["task_id"])
                self.assertEqual(audited["entries"][0]["entry"], {"task_id": task_id})


class BindingPrecedesDuplicateAssociation(unittest.TestCase):
    """D5F-F1: ALL binding reaches the frozen policy before history decides.

    A duplicate physical claim is an association verdict. It must not be
    reported while a binding fault is also present, because the frozen contract
    orders every workspace, recorded, locator and event comparison ahead of
    uniqueness and sequence. The oracle in each case is the frozen public
    ``build_audit_view`` run over the SAME duplicate wrappers: the adapter must
    agree with it, and neither invents a different physical coordinate to slip
    past uniqueness.
    """

    def setUp(self) -> None:
        self.row = entry(task_id="T-0001")
        self.fact = recorded(self.row, date="2026-09-03", ordinal=0)
        self.worklog = worklog({"2026-09-03": {"entries": [self.row]}})
        self.other_checkpoint = "CP-" + "c" * 64

    def duplicate(self, **overrides):
        """A second fact for the SAME physical slot, with its own CP."""

        details = dict(self.fact, checkpoint_id=self.other_checkpoint)
        details.update(overrides)
        return details

    def transition(self, **overrides):
        event = {
            "type": "worklog.superseded",
            "workspace_uid": WORKSPACE,
            "task_id": self.row["task_id"],
            "checkpoint_id": self.fact["checkpoint_id"],
            "date": "2026-09-03",
            "ordinal": 0,
            "entry_digest": digest(self.row),
            "state": "superseded",
            "revision": 1,
            "reason": {"code": "incorrect", "explanation": "x"},
            "origin": None,
        }
        event.update(overrides)
        return event

    def adapter_code(self, *records):
        with self.assertRaises(CheckpointTransitionError) as caught:
            build_audit(
                workspace_uid=WORKSPACE,
                worklog=self.worklog,
                activity=activity(feed("worklog.recorded", self.fact), *records),
            )
        return caught.exception.code

    def frozen_code(self, duplicate_recorded, transitions):
        """The frozen oracle over the same duplicate wrappers."""

        from workstack.checkpoint_transition import build_audit_view

        locator = {
            "workspace_uid": WORKSPACE,
            "task_id": self.row["task_id"],
            "date": "2026-09-03",
            "ordinal": 0,
            "entry_digest": digest(self.row),
        }
        context = {
            "workspace_uid": WORKSPACE,
            "entries": [
                {"locator": dict(locator), "recorded": dict(self.fact), "entry": self.row},
                {"locator": dict(locator), "recorded": dict(duplicate_recorded), "entry": self.row},
            ],
            "transitions": [dict(event) for event in transitions],
        }
        with self.assertRaises(CheckpointTransitionError) as caught:
            build_audit_view(context)
        return caught.exception.code

    def assert_agrees(self, duplicate_recorded, transitions):
        expected = self.frozen_code(duplicate_recorded, transitions)
        records = [feed("worklog.recorded", duplicate_recorded, "E-000002")]
        records += [
            feed(event["type"], event, "E-00001{}".format(index))
            for index, event in enumerate(transitions)
        ]
        self.assertEqual(self.adapter_code(*records), expected)
        return expected

    # -- the five demonstrated combinations --------------------------------
    def test_duplicate_plus_a_recorded_task_mismatch(self) -> None:
        code = self.assert_agrees(self.duplicate(task_id="T-0002"), [])
        self.assertEqual(code, "locator_mismatch")

    def test_duplicate_plus_a_recorded_digest_mismatch(self) -> None:
        code = self.assert_agrees(
            self.duplicate(entry_digest="sha256:" + "d" * 64), []
        )
        self.assertEqual(code, "locator_mismatch")

    def test_duplicate_plus_an_unknown_transition_checkpoint(self) -> None:
        code = self.assert_agrees(
            self.duplicate(), [self.transition(checkpoint_id="CP-" + "e" * 64)]
        )
        self.assertEqual(code, "locator_mismatch")

    def test_duplicate_plus_a_transition_task_mismatch(self) -> None:
        code = self.assert_agrees(
            self.duplicate(), [self.transition(task_id="T-0002")]
        )
        self.assertEqual(code, "locator_mismatch")

    def test_duplicate_plus_a_transition_digest_mismatch(self) -> None:
        code = self.assert_agrees(
            self.duplicate(), [self.transition(entry_digest="sha256:" + "f" * 64)]
        )
        self.assertEqual(code, "locator_mismatch")

    # -- association alone is still association ----------------------------
    def test_a_matching_duplicate_with_an_ordinary_transition_is_history(self) -> None:
        code = self.assert_agrees(self.duplicate(), [self.transition()])
        self.assertEqual(code, "history_invalid")

    def test_a_bare_duplicate_is_still_history_invalid(self) -> None:
        self.assertEqual(self.adapter_code(
            feed("worklog.recorded", self.duplicate(), "E-000002")
        ), "history_invalid")

    def test_a_healthy_nonduplicate_null_origin_history_is_accepted(self) -> None:
        audited = build_audit(
            workspace_uid=WORKSPACE,
            worklog=self.worklog,
            activity=activity(
                feed("worklog.recorded", self.fact),
                feed("worklog.superseded", self.transition(), "E-000002"),
            ),
        )
        self.assertEqual(audited["entries"][0]["state"], "superseded")
        self.assertEqual(audited["entries"][0]["revision"], 1)

    def test_an_orphan_alone_remains_history_invalid(self) -> None:
        orphan = dict(self.fact, date="2026-09-09", checkpoint_id=self.other_checkpoint)
        self.assertEqual(
            self.adapter_code(feed("worklog.recorded", orphan, "E-000002")),
            "history_invalid",
        )

    def test_an_orphan_plus_a_transition_binding_fault_reports_the_binding(self) -> None:
        """Binding must still outrank the orphan association verdict."""

        orphan = dict(self.fact, date="2026-09-09", checkpoint_id=self.other_checkpoint)
        self.assertEqual(
            self.adapter_code(
                feed("worklog.recorded", orphan, "E-000002"),
                feed("worklog.superseded", self.transition(task_id="T-0002"), "E-000003"),
            ),
            "locator_mismatch",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
