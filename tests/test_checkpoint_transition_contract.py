"""Contract tests for the pure checkpoint-transition module.

The frozen supplemental fixture is executed entry by entry with explicit
per-ID accounting. Entries whose proof level is integration-only are recorded
as unexecuted obligations and are never counted as passing here.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from workstack.checkpoint_transition import (
    MAX,
    CheckpointTransitionError,
    build_audit_view,
    build_transition_event,
    build_transition_notice,
    next_transition,
    normalize_transition_request,
    project_active_entries,
    verify_locator,
)
from workstack.storage.canonical import canonical_json_bytes

FIXTURE = Path(__file__).parent / "fixtures" / "checkpoint_transition_v1.json"

FUNCTIONS = {
    "normalize_transition_request": normalize_transition_request,
    "next_transition": next_transition,
    "verify_locator": verify_locator,
    "build_transition_event": build_transition_event,
    "build_transition_notice": build_transition_notice,
    "project_active_entries": project_active_entries,
    "build_audit_view": build_audit_view,
}

WORKSPACE = "f67e2aad-9ed9-4fc7-b1ae-63b240269855"
CHECKPOINT = "CP-" + "a" * 64
DIGEST = "sha256:" + "b" * 64


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def locator(**overrides: object) -> dict:
    value = {
        "workspace_uid": WORKSPACE,
        "task_id": "T-0001",
        "date": "2026-09-03",
        "ordinal": 0,
        "entry_digest": DIGEST,
    }
    value.update(overrides)
    return value


def recorded(**overrides: object) -> dict:
    value = {
        "type": "worklog.recorded",
        "workspace_uid": WORKSPACE,
        "task_id": "T-0001",
        "checkpoint_id": CHECKPOINT,
        "date": "2026-09-03",
        "ordinal": 0,
        "entry_digest": DIGEST,
        "origin": None,
    }
    value.update(overrides)
    return value


def event(**overrides: object) -> dict:
    value = {
        "type": "worklog.superseded",
        "workspace_uid": WORKSPACE,
        "task_id": "T-0001",
        "checkpoint_id": CHECKPOINT,
        "date": "2026-09-03",
        "ordinal": 0,
        "entry_digest": DIGEST,
        "state": "superseded",
        "revision": 1,
        "reason": {"code": "incorrect", "explanation": "Wrong day"},
        "origin": "agent-cli-v1",
    }
    value.update(overrides)
    return value


class FrozenSupplementTests(unittest.TestCase):
    """Executes every applicable frozen entry with per-ID accounting."""

    def test_fixture_matches_the_frozen_digest(self) -> None:
        digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        self.assertEqual(
            digest,
            "a64351ccb66784e8f1d5c96501598f340bfd0f2d435a14c776d6d55a60e49dd8",
        )

    def test_every_frozen_entry_is_accounted_for(self) -> None:
        fixture = load_fixture()
        vectors = fixture["vectors"]
        self.assertEqual(len(vectors), 69)

        executed: list[str] = []
        integration_only: list[str] = []
        recipes: list[str] = []
        arithmetic: list[str] = []

        for vector in vectors:
            identifier = vector["id"]
            if vector.get("proof_level") == "integration-only":
                integration_only.append(identifier)
                continue
            if "recipe" in vector and "function" not in vector:
                recipes.append(identifier)
                continue
            if vector["id"] == "S-opaque-legacy-cycle":
                # Carries both function and recipe: executed explicitly
                # below with a real self-referencing entry.
                recipes.append(identifier)
                self._run_opaque_legacy_cycle(vector)
                continue
            if "function" not in vector:
                arithmetic.append(identifier)
                continue
            self._run_vector(vector)
            executed.append(identifier)

        # Integration-only scenarios are unexecuted obligations, never passes.
        self.assertEqual(len(integration_only), 2)
        self.assertEqual(len(recipes), 9)
        self.assertEqual(len(arithmetic), 1)
        self.assertEqual(
            len(executed) + len(integration_only) + len(recipes) + len(arithmetic),
            69,
        )
        self.assertEqual(len(executed), 57)
        # 57 pure calls + 9 recipes + 1 arithmetic + 2 integration-only.
        self.assertEqual(
            sorted(integration_only),
            ["S-key-conflict-after-normalization", "S-old-exact-replay-after-four-cycles"],
        )

    def _run_vector(self, vector: dict) -> None:
        function = FUNCTIONS[vector["function"]]
        arguments = (
            vector["input_args"] if "input_args" in vector else [vector["input"]]
        )
        with self.subTest(vector=vector["id"]):
            if vector.get("error_code") is not None:
                with self.assertRaises(CheckpointTransitionError) as caught:
                    function(*arguments)
                self.assertEqual(caught.exception.code, vector["error_code"])
                self.assertEqual(
                    str(caught.exception), "invalid checkpoint transition input"
                )
                return
            result = function(*arguments)
            if "active_entry_indexes" in vector:
                entries = vector["input"]["entries"]
                expected = [
                    entries[index]["entry"] for index in vector["active_entry_indexes"]
                ]
                self.assertEqual(len(result), len(expected))
                for actual, wanted in zip(result, expected):
                    # Opaque entries keep their exact identity.
                    self.assertIs(actual, wanted)
                return
            if "output" in vector:
                self.assertEqual(result, vector["output"])
            if "field_order" in vector:
                self.assertEqual(list(result), vector["field_order"])
            if "outer_field_order" in vector:
                self.assertEqual(list(result), vector["outer_field_order"])

    def _run_opaque_legacy_cycle(self, vector: dict) -> None:
        """S-opaque-legacy-cycle: a self-referencing opaque payload is never read."""
        cyclic: dict = {"self": None}
        cyclic["self"] = cyclic
        context = {
            "workspace_uid": WORKSPACE,
            "entries": [
                {
                    "locator": locator(task_id=None, entry_digest=None),
                    "recorded": None,
                    "entry": cyclic,
                }
            ],
            "transitions": [],
        }
        with self.subTest(vector=vector["id"]):
            active = project_active_entries(context)
            self.assertEqual(len(active), 1)
            # Identity, not equality: the cycle is neither traversed nor copied.
            self.assertIs(active[0], cyclic)
            audited = build_audit_view(context)["entries"][0]
            self.assertIs(audited["entry"], cyclic)
            self.assertIsNone(audited["recorded"])
            self.assertEqual(audited["state"], "active")

    def test_canonical_arithmetic_entry_is_reproduced(self) -> None:
        fixture = load_fixture()
        arithmetic = [
            vector
            for vector in fixture["vectors"]
            if "checkpoint_preimage" in vector
        ]
        self.assertEqual(len(arithmetic), 1)
        vector = arithmetic[0]
        encoded = canonical_json_bytes(vector["entry"])
        self.assertEqual(encoded.decode("utf-8"), vector["canonical_text"])
        self.assertEqual(encoded.hex(), vector["utf8_hex"])
        self.assertFalse(encoded.endswith(b"\n"))
        self.assertEqual(
            "sha256:" + hashlib.sha256(encoded).hexdigest(), vector["entry_digest"]
        )
        # The checkpoint identity is recomputed from its frozen preimage using
        # the same admitted canonical serializer.
        preimage = canonical_json_bytes(vector["checkpoint_preimage"])
        self.assertEqual(
            "CP-" + hashlib.sha256(preimage).hexdigest(), vector["checkpoint_id"]
        )

    def test_python_type_recipes_refuse(self) -> None:
        """The eight builtin-subclass recipes need real Python instances.

        Each case is mapped to its frozen recipe ID so the accounting is a real
        assertion rather than a skipped vector that was still counted.
        """
        recipe_ids = [
            "S-python-exact-type-outer-key",
            "S-python-exact-type-reason-key",
            "S-python-exact-type-code-value",
            "S-python-exact-type-state-value",
            "S-python-exact-type-revision-value",
            "S-python-exact-type-outer-dict",
            "S-python-exact-type-reason-dict",
        ]

        class Text(str):
            pass

        class Number(int):
            pass

        class Mapping(dict):
            pass

        class Sequence(list):
            pass

        request = {
            "state": "superseded",
            "revision": 0,
            "reason": {"code": "incorrect", "explanation": "Wrong day"},
        }
        # A dict literal would absorb a str-subclass key equal to an existing
        # one, so the subclass keys are inserted explicitly.
        outer_key = {"revision": 0, "reason": dict(request["reason"])}
        outer_key[Text("state")] = "superseded"
        reason_key = {"explanation": "x"}
        reason_key[Text("code")] = "incorrect"
        cases = [
            outer_key,
            {**request, "reason": reason_key},
            {**request, "reason": {"code": Text("incorrect"), "explanation": "x"}},
            {**request, "state": Text("superseded")},
            {**request, "revision": Number(0)},
            Mapping(request),
            {**request, "reason": Mapping(request["reason"])},
        ]
        self.assertEqual(len(cases), len(recipe_ids))
        for index, case in enumerate(cases):
            with self.subTest(recipe=recipe_ids[index]):
                with self.assertRaises(CheckpointTransitionError) as caught:
                    normalize_transition_request(case)
                self.assertEqual(caught.exception.code, "malformed")

        with self.subTest(recipe="S-python-exact-type-transitions-list"):
            with self.assertRaises(CheckpointTransitionError) as caught:
                project_active_entries(
                    {
                        "workspace_uid": WORKSPACE,
                        "entries": [],
                        "transitions": Sequence(),
                    }
                )
            self.assertEqual(caught.exception.code, "malformed")


class NormalizeTests(unittest.TestCase):
    def test_trims_only_the_outer_explanation_and_orders_keys(self) -> None:
        result = normalize_transition_request(
            {
                "state": "superseded",
                "revision": 2,
                "reason": {"code": "incorrect", "explanation": "  a\nb  "},
            }
        )
        self.assertEqual(list(result), ["state", "revision", "reason"])
        self.assertEqual(list(result["reason"]), ["code", "explanation"])
        self.assertEqual(result["reason"]["explanation"], "a\nb")

    def test_accepts_max_as_a_syntactic_request_revision(self) -> None:
        result = normalize_transition_request(
            {
                "state": "active",
                "revision": MAX,
                "reason": {"code": "restore", "explanation": "back"},
            }
        )
        self.assertEqual(result["revision"], MAX)

    def test_returned_metadata_is_detached(self) -> None:
        reason = {"code": "incorrect", "explanation": "Wrong day"}
        request = {"state": "superseded", "revision": 0, "reason": reason}
        result = normalize_transition_request(request)
        self.assertIsNot(result["reason"], reason)

    def test_refuses_bool_revision_and_wrong_code_for_state(self) -> None:
        for bad in (
            {"state": "superseded", "revision": True, "reason": {"code": "incorrect", "explanation": "x"}},
            {"state": "active", "revision": 0, "reason": {"code": "incorrect", "explanation": "x"}},
            {"state": "superseded", "revision": 0, "reason": {"code": "restore", "explanation": "x"}},
            {"state": "superseded", "revision": 0, "reason": {"code": "incorrect", "explanation": "   "}},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(CheckpointTransitionError) as caught:
                    normalize_transition_request(bad)
                self.assertEqual(caught.exception.code, "malformed")


class NextTransitionTests(unittest.TestCase):
    def test_four_successive_cycles(self) -> None:
        state, revision = "active", 0
        codes = ["incorrect", "restore", "duplicate", "restore"]
        seen = []
        for code in codes:
            requested = "superseded" if state == "active" else "active"
            result = next_transition(
                {
                    "current": {"state": state, "revision": revision},
                    "request": {
                        "state": requested,
                        "revision": revision,
                        "reason": {"code": code, "explanation": "cycle"},
                    },
                }
            )
            seen.append((result["state"], result["revision"]))
            state, revision = result["state"], result["revision"]
        self.assertEqual(
            seen,
            [("superseded", 1), ("active", 2), ("superseded", 3), ("active", 4)],
        )

    def test_max_current_is_exhausted_for_the_opposite_request(self) -> None:
        with self.assertRaises(CheckpointTransitionError) as caught:
            next_transition(
                {
                    "current": {"state": "superseded", "revision": MAX},
                    "request": {
                        "state": "active",
                        "revision": MAX,
                        "reason": {"code": "restore", "explanation": "back"},
                    },
                }
            )
        self.assertEqual(caught.exception.code, "exhausted")

    def test_impossible_parity_is_history_invalid(self) -> None:
        with self.assertRaises(CheckpointTransitionError) as caught:
            next_transition(
                {
                    "current": {"state": "superseded", "revision": 0},
                    "request": {
                        "state": "active",
                        "revision": 0,
                        "reason": {"code": "restore", "explanation": "back"},
                    },
                }
            )
        self.assertEqual(caught.exception.code, "history_invalid")


class VerifyLocatorTests(unittest.TestCase):
    def test_success_returns_a_detached_five_key_locator(self) -> None:
        fact = recorded()
        result = verify_locator(
            {
                "workspace_uid": WORKSPACE,
                "checkpoint_id": CHECKPOINT,
                "recorded": fact,
                "actual_locator": locator(),
            }
        )
        self.assertEqual(
            list(result),
            ["workspace_uid", "task_id", "date", "ordinal", "entry_digest"],
        )
        self.assertIsNot(result, fact)

    def test_value_mismatch_is_locator_mismatch(self) -> None:
        with self.assertRaises(CheckpointTransitionError) as caught:
            verify_locator(
                {
                    "workspace_uid": WORKSPACE,
                    "checkpoint_id": CHECKPOINT,
                    "recorded": recorded(),
                    "actual_locator": locator(ordinal=1),
                }
            )
        self.assertEqual(caught.exception.code, "locator_mismatch")


class EventAndNoticeTests(unittest.TestCase):
    def test_event_field_order_and_type(self) -> None:
        result = build_transition_event(
            {
                "workspace_uid": WORKSPACE,
                "checkpoint_id": CHECKPOINT,
                "locator": locator(),
                "transition": {
                    "state": "superseded",
                    "revision": 1,
                    "reason": {"code": "incorrect", "explanation": "Wrong day"},
                },
                "origin": None,
            }
        )
        self.assertEqual(
            list(result),
            [
                "type",
                "workspace_uid",
                "task_id",
                "checkpoint_id",
                "date",
                "ordinal",
                "entry_digest",
                "state",
                "revision",
                "reason",
                "origin",
            ],
        )
        self.assertEqual(result["type"], "worklog.superseded")
        self.assertIsNone(result["origin"])

    def test_restored_type_and_workspace_binding(self) -> None:
        result = build_transition_event(
            {
                "workspace_uid": WORKSPACE,
                "checkpoint_id": CHECKPOINT,
                "locator": locator(),
                "transition": {
                    "state": "active",
                    "revision": 2,
                    "reason": {"code": "restore", "explanation": "back"},
                },
                "origin": "agent-cli-v1",
            }
        )
        self.assertEqual(result["type"], "worklog.restored")

        with self.assertRaises(CheckpointTransitionError) as caught:
            build_transition_event(
                {
                    "workspace_uid": "0f8fad5b-d9cb-469f-a165-70867728950e",
                    "checkpoint_id": CHECKPOINT,
                    "locator": locator(),
                    "transition": {
                        "state": "superseded",
                        "revision": 1,
                        "reason": {"code": "incorrect", "explanation": "x"},
                    },
                    "origin": None,
                }
            )
        self.assertEqual(caught.exception.code, "locator_mismatch")

    def test_notice_order_and_attribution(self) -> None:
        notice = build_transition_notice(event(), 7)
        self.assertEqual(
            list(notice),
            [
                "event_id",
                "kind",
                "workspace_uid",
                "task_id",
                "date",
                "checkpoint_id",
                "ordinal",
                "entry_digest",
                "state",
                "transition_revision",
                "origin",
            ],
        )
        self.assertEqual(notice["kind"], "agent.checkpoint.superseded")
        self.assertEqual(notice["transition_revision"], 1)
        self.assertNotIn("reason", notice)

    def test_ordinary_null_origin_event_has_no_notice(self) -> None:
        with self.assertRaises(CheckpointTransitionError) as caught:
            build_transition_notice(event(origin=None), 1)
        self.assertEqual(caught.exception.code, "malformed")


class ProjectionTests(unittest.TestCase):
    def _context(self, **overrides: object) -> dict:
        payload = {"opaque": ["value"]}
        value = {
            "workspace_uid": WORKSPACE,
            "entries": [
                {"locator": locator(), "recorded": recorded(), "entry": payload},
            ],
            "transitions": [],
        }
        value.update(overrides)
        return value

    def test_empty_history_folds_to_active(self) -> None:
        context = self._context()
        active = project_active_entries(context)
        self.assertEqual(len(active), 1)
        self.assertIs(active[0], context["entries"][0]["entry"])

    def test_legacy_row_is_always_active_with_null_metadata(self) -> None:
        payload = object()
        context = {
            "workspace_uid": WORKSPACE,
            "entries": [
                {
                    "locator": locator(task_id=None, entry_digest=None),
                    "recorded": None,
                    "entry": payload,
                }
            ],
            "transitions": [],
        }
        audit = build_audit_view(context)
        row = audit["entries"][0]
        self.assertIsNone(row["checkpoint_id"])
        self.assertIsNone(row["recorded"])
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["revision"], 0)
        self.assertEqual(row["transitions"], [])
        self.assertIs(row["entry"], payload)

    def test_duplicate_physical_slot_is_history_invalid(self) -> None:
        context = self._context(
            entries=[
                {"locator": locator(), "recorded": recorded(), "entry": 1},
                {
                    "locator": locator(),
                    "recorded": recorded(checkpoint_id="CP-" + "c" * 64),
                    "entry": 2,
                },
            ]
        )
        with self.assertRaises(CheckpointTransitionError) as caught:
            project_active_entries(context)
        self.assertEqual(caught.exception.code, "history_invalid")

    def test_superseded_row_is_filtered_but_audited(self) -> None:
        context = self._context(transitions=[event()])
        self.assertEqual(project_active_entries(context), [])
        audit = build_audit_view(context)
        row = audit["entries"][0]
        self.assertEqual(row["state"], "superseded")
        self.assertEqual(row["revision"], 1)
        self.assertEqual(len(row["transitions"]), 1)

    def test_audit_schema_order_and_detachment(self) -> None:
        source = recorded()
        context = self._context(
            entries=[{"locator": locator(), "recorded": source, "entry": 1}],
            transitions=[event()],
        )
        audit = build_audit_view(context)
        self.assertEqual(list(audit), ["workspace_uid", "entries"])
        row = audit["entries"][0]
        self.assertEqual(
            list(row),
            [
                "locator",
                "checkpoint_id",
                "entry",
                "recorded",
                "state",
                "revision",
                "transitions",
            ],
        )
        self.assertIsNot(row["recorded"], source)
        self.assertIsNot(row["transitions"][0], context["transitions"][0])
        self.assertIsNot(
            row["transitions"][0]["reason"], context["transitions"][0]["reason"]
        )

    def test_transition_targeting_a_legacy_row_is_locator_mismatch(self) -> None:
        context = {
            "workspace_uid": WORKSPACE,
            "entries": [
                {
                    "locator": locator(),
                    "recorded": None,
                    "entry": 1,
                }
            ],
            "transitions": [event()],
        }
        with self.assertRaises(CheckpointTransitionError) as caught:
            build_audit_view(context)
        self.assertEqual(caught.exception.code, "locator_mismatch")

    def test_history_gap_is_history_invalid(self) -> None:
        context = self._context(
            transitions=[event(), event(state="superseded", revision=3)]
        )
        with self.assertRaises(CheckpointTransitionError) as caught:
            build_audit_view(context)
        self.assertEqual(caught.exception.code, "history_invalid")

    def test_empty_context_validates_its_workspace(self) -> None:
        audit = build_audit_view(
            {"workspace_uid": WORKSPACE, "entries": [], "transitions": []}
        )
        self.assertEqual(audit, {"workspace_uid": WORKSPACE, "entries": []})


class ApiSurfaceTests(unittest.TestCase):
    def test_extra_context_key_is_a_domain_refusal(self) -> None:
        with self.assertRaises(CheckpointTransitionError) as caught:
            next_transition(
                {
                    "current": {"state": "active", "revision": 0},
                    "request": {
                        "state": "superseded",
                        "revision": 0,
                        "reason": {"code": "incorrect", "explanation": "x"},
                    },
                    "extra": 1,
                }
            )
        self.assertEqual(caught.exception.code, "malformed")

    def test_wrong_arity_remains_an_ordinary_type_error(self) -> None:
        with self.assertRaises(TypeError):
            next_transition()  # type: ignore[call-arg]

    def test_error_message_never_carries_values(self) -> None:
        with self.assertRaises(CheckpointTransitionError) as caught:
            normalize_transition_request({"state": "nope", "revision": 0, "reason": {}})
        self.assertEqual(str(caught.exception), "invalid checkpoint transition input")
        self.assertIsInstance(caught.exception, ValueError)

    def test_canonical_uuid_version_seven_is_accepted(self) -> None:
        uuid7 = "018f6a4b-7c2d-7def-8123-456789abcdef"
        result = verify_locator(
            {
                "workspace_uid": uuid7,
                "checkpoint_id": CHECKPOINT,
                "recorded": recorded(workspace_uid=uuid7),
                "actual_locator": locator(workspace_uid=uuid7),
            }
        )
        self.assertEqual(result["workspace_uid"], uuid7)


if __name__ == "__main__":
    unittest.main()


FOREIGN = "0f8fad5b-d9cb-469f-a165-70867728950e"


class WorkspaceBindingTests(unittest.TestCase):
    """TPR-F1: every known row binds its locator workspace to the context."""

    def _foreign_recorded_context(self) -> dict:
        return {
            "workspace_uid": WORKSPACE,
            "entries": [
                {
                    "locator": locator(workspace_uid=FOREIGN),
                    "recorded": recorded(workspace_uid=FOREIGN),
                    "entry": {"payload": 1},
                }
            ],
            "transitions": [],
        }

    def _foreign_legacy_context(self) -> dict:
        return {
            "workspace_uid": WORKSPACE,
            "entries": [
                {
                    "locator": locator(
                        workspace_uid=FOREIGN, task_id=None, entry_digest=None
                    ),
                    "recorded": None,
                    "entry": {"payload": 2},
                }
            ],
            "transitions": [],
        }

    def test_foreign_recorded_row_refuses_in_both_apis(self) -> None:
        for function in (project_active_entries, build_audit_view):
            with self.subTest(function=function.__name__):
                with self.assertRaises(CheckpointTransitionError) as caught:
                    function(self._foreign_recorded_context())
                self.assertEqual(caught.exception.code, "locator_mismatch")

    def test_foreign_legacy_row_refuses_in_both_apis(self) -> None:
        for function in (project_active_entries, build_audit_view):
            with self.subTest(function=function.__name__):
                with self.assertRaises(CheckpointTransitionError) as caught:
                    function(self._foreign_legacy_context())
                self.assertEqual(caught.exception.code, "locator_mismatch")

    def test_same_workspace_rows_remain_positive_controls(self) -> None:
        context = {
            "workspace_uid": WORKSPACE,
            "entries": [
                {"locator": locator(), "recorded": recorded(), "entry": {"a": 1}},
                {
                    "locator": locator(ordinal=1, task_id=None, entry_digest=None),
                    "recorded": None,
                    "entry": {"b": 2},
                },
            ],
            "transitions": [],
        }
        self.assertEqual(len(project_active_entries(context)), 2)
        audit = build_audit_view(context)
        self.assertEqual(audit["workspace_uid"], WORKSPACE)
        self.assertEqual(len(audit["entries"]), 2)

    def test_empty_context_remains_a_positive_control(self) -> None:
        context = {"workspace_uid": WORKSPACE, "entries": [], "transitions": []}
        self.assertEqual(project_active_entries(context), [])
        self.assertEqual(build_audit_view(context)["entries"], [])


class GlobalPrecedenceTests(unittest.TestCase):
    """TPR-F2: all known syntax first, then binding, then history."""

    def _context(self, entries: list, transitions: list) -> dict:
        return {
            "workspace_uid": WORKSPACE,
            "entries": entries,
            "transitions": transitions,
        }

    def _assert_code(self, function, argument, code: str) -> None:
        with self.assertRaises(CheckpointTransitionError) as caught:
            function(argument)
        self.assertEqual(caught.exception.code, code)

    def test_duplicate_slot_with_a_later_malformed_transition(self) -> None:
        context = self._context(
            [
                {"locator": locator(), "recorded": recorded(), "entry": 1},
                {
                    "locator": locator(),
                    "recorded": recorded(checkpoint_id="CP-" + "c" * 64),
                    "entry": 2,
                },
            ],
            [event(state="nonsense")],
        )
        self._assert_code(build_audit_view, context, "malformed")

    def test_first_binding_mismatch_with_a_later_malformed_row(self) -> None:
        context = self._context(
            [
                {
                    "locator": locator(ordinal=5),
                    "recorded": recorded(ordinal=6),
                    "entry": 1,
                },
                {
                    "locator": locator(date="2026-02-30"),
                    "recorded": recorded(),
                    "entry": 2,
                },
            ],
            [],
        )
        self._assert_code(build_audit_view, context, "malformed")

    def test_parity_error_with_a_malformed_uuid_elsewhere(self) -> None:
        context = self._context(
            [{"locator": locator(), "recorded": recorded(), "entry": 1}],
            [event(state="active", revision=1), event(workspace_uid="not-a-uuid")],
        )
        self._assert_code(build_audit_view, context, "malformed")

    def test_parity_error_with_a_boolean_caller_event_id(self) -> None:
        self._assert_code(
            lambda argument: build_transition_notice(argument, True),
            event(state="active", revision=1),
            "malformed",
        )

    def test_early_parity_with_a_later_malformed_event(self) -> None:
        context = self._context(
            [{"locator": locator(), "recorded": recorded(), "entry": 1}],
            [event(state="active", revision=1), event(revision=0)],
        )
        self._assert_code(build_audit_view, context, "malformed")

    def test_duplicate_checkpoint_with_a_foreign_event(self) -> None:
        context = self._context(
            [
                {"locator": locator(), "recorded": recorded(), "entry": 1},
                {
                    "locator": locator(ordinal=1),
                    "recorded": recorded(ordinal=1),
                    "entry": 2,
                },
            ],
            [event(workspace_uid=FOREIGN)],
        )
        # Binding precedes the duplicate-checkpoint history refusal.
        self._assert_code(build_audit_view, context, "locator_mismatch")

    def test_event_builder_parity_with_outer_workspace_mismatch(self) -> None:
        context = {
            "workspace_uid": FOREIGN,
            "checkpoint_id": CHECKPOINT,
            "locator": locator(),
            "transition": {
                "state": "active",
                "revision": 1,
                "reason": {"code": "restore", "explanation": "back"},
            },
            "origin": None,
        }
        # Parity is a history rule, so the binding mismatch is reported first.
        self._assert_code(build_transition_event, context, "locator_mismatch")

    def test_isolated_error_controls_keep_their_own_codes(self) -> None:
        duplicate = self._context(
            [
                {"locator": locator(), "recorded": recorded(), "entry": 1},
                {
                    "locator": locator(),
                    "recorded": recorded(checkpoint_id="CP-" + "c" * 64),
                    "entry": 2,
                },
            ],
            [],
        )
        self._assert_code(build_audit_view, duplicate, "history_invalid")

        mismatch = self._context(
            [
                {
                    "locator": locator(ordinal=5),
                    "recorded": recorded(ordinal=6),
                    "entry": 1,
                }
            ],
            [],
        )
        self._assert_code(build_audit_view, mismatch, "locator_mismatch")

        malformed = self._context(
            [
                {
                    "locator": locator(date="2026-02-30"),
                    "recorded": recorded(),
                    "entry": 1,
                }
            ],
            [],
        )
        self._assert_code(build_audit_view, malformed, "malformed")
