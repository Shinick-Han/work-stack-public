from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from workstack import checkpoint_change
from workstack.checkpoint_change import (
    CheckpointChangeError,
    build_checkpoint_facts,
    build_committed_notice,
    derive_checkpoint_id,
)
from workstack.storage.canonical import canonical_json_bytes


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "checkpoint_change_v1.json"
VECTORS = json.loads(FIXTURE.read_text(encoding="utf-8"))
OPERATIONS = {
    "derive_checkpoint_id": derive_checkpoint_id,
    "build_checkpoint_facts": build_checkpoint_facts,
    "build_committed_notice": build_committed_notice,
}
CANONICAL_EVIDENCE = ("canonical_preimage", "canonical_entry", "canonical_notice")


def _vectors(name: str) -> list[dict]:
    return VECTORS[name]


def _by_id(name: str) -> dict[str, dict]:
    return {vector["id"]: vector for vector in VECTORS[name]}


def _resolve(container: dict, dotted: str):
    value = container
    for part in dotted.split("."):
        value = value[part]
    return value


class FrozenVectorConformanceTest(unittest.TestCase):
    """Every executable frozen vector, run against the public functions."""

    def _run(self, vector: dict):
        return OPERATIONS[vector["operation"]](**copy.deepcopy(vector["input"]))

    def _assert_vector(self, vector: dict) -> None:
        if "expected_error" in vector:
            expected = vector["expected_error"]
            with self.assertRaises(CheckpointChangeError) as raised:
                self._run(vector)
            self.assertEqual(type(raised.exception).__name__, expected["type"])
            self.assertEqual(raised.exception.code, expected["code"])
            self.assertEqual(str(raised.exception), expected["message"])
            return
        self.assertEqual(self._run(vector), vector["expected"])

    def test_identity_vectors(self) -> None:
        vectors = _vectors("identity_vectors")
        self.assertGreater(len(vectors), 0)
        for vector in vectors:
            with self.subTest(id=vector["id"]):
                self._assert_vector(vector)

    def test_facts_vectors(self) -> None:
        vectors = _vectors("facts_vectors")
        self.assertGreater(len(vectors), 0)
        for vector in vectors:
            with self.subTest(id=vector["id"]):
                self._assert_vector(vector)

    def test_notice_vectors(self) -> None:
        vectors = _vectors("notice_vectors")
        self.assertGreater(len(vectors), 0)
        for vector in vectors:
            with self.subTest(id=vector["id"]):
                self._assert_vector(vector)

    def test_canonical_evidence_matches_the_existing_canonical_serializer(self) -> None:
        """Recompute the frozen preimage/entry/notice bytes independently."""

        checked = 0
        for name in ("identity_vectors", "facts_vectors", "notice_vectors"):
            for vector in _vectors(name):
                evidence_key = next(
                    (key for key in CANONICAL_EVIDENCE if key in vector), None
                )
                if evidence_key is None:
                    continue
                evidence = vector[evidence_key]
                if evidence_key == "canonical_preimage":
                    value = [
                        "workstack.checkpoint.v1",
                        vector["input"]["workspace_uid"],
                        vector["input"]["idempotency_key"],
                    ]
                elif evidence_key == "canonical_entry":
                    value = vector["input"]["entry"]
                else:
                    value = vector["expected"]
                with self.subTest(id=vector["id"], evidence=evidence_key):
                    raw = canonical_json_bytes(value)
                    self.assertEqual(raw.decode("utf-8"), evidence["text"])
                    self.assertEqual(raw.hex(), evidence["utf8_hex"])
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), evidence["sha256"])
                    self.assertFalse(raw.endswith(b"\n"))
                checked += 1
        self.assertGreater(checked, 0)

    def test_declared_relations_hold_between_vectors(self) -> None:
        indexed = {}
        for name in ("identity_vectors", "facts_vectors", "notice_vectors"):
            indexed.update(_by_id(name))
        relations = VECTORS["relations"]
        self.assertGreater(len(relations), 0)
        for relation in relations:
            with self.subTest(id=relation["id"]):
                left = _resolve(indexed[relation["left"]], relation["field"])
                right = _resolve(indexed[relation["right"]], relation["field"])
                if relation["comparison"] == "equal":
                    self.assertEqual(left, right)
                else:
                    self.assertNotEqual(left, right)


class PublicSurfaceTest(unittest.TestCase):
    """Exports, error contract and the exact notice key set."""

    def test_exports_are_exactly_the_frozen_public_api(self) -> None:
        self.assertEqual(
            checkpoint_change.__all__,
            [
                "CheckpointChangeError",
                "derive_checkpoint_id",
                "build_checkpoint_facts",
                "build_committed_notice",
            ],
        )

    def test_error_is_a_value_error_with_a_content_free_message(self) -> None:
        error = CheckpointChangeError()

        self.assertIsInstance(error, ValueError)
        self.assertEqual(CheckpointChangeError.code, "invalid_checkpoint_input")
        self.assertEqual(str(error), "invalid checkpoint input")

    def test_a_refusal_never_interpolates_the_offending_input(self) -> None:
        diagnostic_canary = "Sensitive Title Text"

        with self.assertRaises(CheckpointChangeError) as raised:
            build_checkpoint_facts(
                workspace_uid="123e4567-e89b-42d3-a456-426614174000",
                idempotency_key="Agent-Intent-0001",
                date="2026-09-03",
                entry={
                    "task_id": "T-0001",
                    "task": diagnostic_canary,
                    "done": [],
                    "next": [],
                    "blockers": [],
                },
                ordinal=0,
                prior_entries=[],
                origin=None,
            )

        self.assertNotIn(diagnostic_canary, str(raised.exception))
        self.assertEqual(str(raised.exception), "invalid checkpoint input")

    def test_notice_key_order_and_set_match_the_frozen_list(self) -> None:
        base = _by_id("notice_vectors")["notice.base"]

        notice = build_committed_notice(**copy.deepcopy(base["input"]))

        self.assertEqual(list(notice), VECTORS["notice_keys"])
        self.assertEqual(len(VECTORS["notice_keys"]), 12)

    def test_module_imports_only_the_standard_library_and_pure_canonical(self) -> None:
        import ast

        tree = ast.parse(
            Path(checkpoint_change.__file__).read_text(encoding="utf-8")
        )
        absolute: set[str] = set()
        relative: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                absolute.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    relative.add(node.module or "")
                else:
                    absolute.add((node.module or "").split(".")[0])

        self.assertEqual(absolute, {"__future__", "hashlib", "re", "uuid", "datetime", "typing"})
        self.assertEqual(relative, {"storage.canonical"})


class StrictTypeAndPurityTest(unittest.TestCase):
    """The handwritten cases the JSON vectors cannot express."""

    BASE_UID = "123e4567-e89b-42d3-a456-426614174000"
    BASE_KEY = "Agent-Intent-0001"

    def _entry(self, **overrides) -> dict:
        entry = {
            "task_id": "T-0001",
            "task": "Then stored title",
            "done": ["Finished"],
            "next": ["Next"],
            "blockers": [],
        }
        entry.update(overrides)
        return entry

    def _facts(self, **overrides):
        arguments = {
            "workspace_uid": self.BASE_UID,
            "idempotency_key": self.BASE_KEY,
            "date": "2026-09-03",
            "entry": self._entry(),
            "ordinal": 0,
            "prior_entries": [],
            "origin": "agent-cli-v1",
        }
        arguments.update(overrides)
        return build_checkpoint_facts(**arguments)

    def test_healthy_control_builds_facts_and_a_notice(self) -> None:
        facts = self._facts()

        notice = build_committed_notice(facts=facts, event_id=1)

        self.assertEqual(facts["recorded"]["type"], "worklog.recorded")
        self.assertEqual(notice["kind"], "agent.checkpoint.committed")
        self.assertIs(notice["replayed"], False)

    def test_subclasses_of_builtin_types_are_refused(self) -> None:
        class Text(str):
            pass

        class Number(int):
            pass

        class Mapping(dict):
            pass

        class Sequence(list):
            pass

        with self.assertRaises(CheckpointChangeError):
            derive_checkpoint_id(
                workspace_uid=Text(self.BASE_UID), idempotency_key=self.BASE_KEY
            )
        with self.assertRaises(CheckpointChangeError):
            self._facts(ordinal=Number(0))
        with self.assertRaises(CheckpointChangeError):
            self._facts(entry=Mapping(self._entry()))
        with self.assertRaises(CheckpointChangeError):
            self._facts(prior_entries=Sequence())

    def test_tuple_and_set_inputs_are_refused(self) -> None:
        with self.assertRaises(CheckpointChangeError):
            self._facts(entry=self._entry(done=("Finished",)))
        with self.assertRaises(CheckpointChangeError):
            self._facts(entry=self._entry(done={"Finished"}))
        with self.assertRaises(CheckpointChangeError):
            self._facts(prior_entries=({"task_id": "T-0001"},))

    def test_booleans_are_not_integers_for_the_ordinal_or_the_event_id(self) -> None:
        with self.assertRaises(CheckpointChangeError):
            self._facts(ordinal=True)
        with self.assertRaises(CheckpointChangeError):
            build_committed_notice(facts=self._facts(), event_id=True)
        with self.assertRaises(CheckpointChangeError):
            build_committed_notice(facts=self._facts(), event_id=1.0)

    def test_unpaired_surrogate_strings_are_refused_content_free(self) -> None:
        surrogate = "bad \ud800 title"

        with self.assertRaises(CheckpointChangeError) as raised:
            self._facts(entry=self._entry(task=surrogate))

        self.assertEqual(str(raised.exception), "invalid checkpoint input")

    def test_cyclic_input_is_refused_content_free(self) -> None:
        cyclic_entry: dict = self._entry()
        cyclic_entry["self"] = cyclic_entry
        cyclic_facts: dict = self._facts()
        cyclic_facts["self"] = cyclic_facts

        with self.assertRaises(CheckpointChangeError) as entry_refusal:
            self._facts(entry=cyclic_entry)
        with self.assertRaises(CheckpointChangeError) as notice_refusal:
            build_committed_notice(facts=cyclic_facts, event_id=1)

        self.assertEqual(str(entry_refusal.exception), "invalid checkpoint input")
        self.assertEqual(str(notice_refusal.exception), "invalid checkpoint input")

    def test_ignored_legacy_prior_fields_are_never_serialized_or_echoed(self) -> None:
        """Contract: prior entries' other legacy fields are ignored, not validated."""

        cyclic_legacy: dict = {"task_id": "T-0002"}
        cyclic_legacy["self"] = cyclic_legacy

        facts = self._facts(prior_entries=[cyclic_legacy])

        self.assertTrue(facts["first_for_task"])
        self.assertNotIn("self", json.dumps(facts))

    def test_builders_do_not_mutate_inputs_or_return_aliases(self) -> None:
        entry = self._entry()
        prior = [{"task_id": "T-0002", "legacy_note": "browser"}]
        entry_snapshot = copy.deepcopy(entry)
        prior_snapshot = copy.deepcopy(prior)

        facts = build_checkpoint_facts(
            workspace_uid=self.BASE_UID,
            idempotency_key=self.BASE_KEY,
            date="2026-09-03",
            entry=entry,
            ordinal=0,
            prior_entries=prior,
            origin="agent-cli-v1",
        )
        facts_snapshot = copy.deepcopy(facts)
        notice = build_committed_notice(facts=facts, event_id=7)
        facts["recorded"]["task_id"] = "T-9999"

        self.assertEqual(entry, entry_snapshot)
        self.assertEqual(prior, prior_snapshot)
        self.assertIsNot(facts["recorded"], entry)
        self.assertEqual(notice["task_id"], facts_snapshot["recorded"]["task_id"])

    def test_repeated_calls_are_deterministic(self) -> None:
        first = self._facts()
        second = self._facts()

        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNot(first["recorded"], second["recorded"])

    def test_first_for_task_uses_all_prior_physical_entries(self) -> None:
        unrelated = self._facts(
            prior_entries=[{"task_id": "T-0002"}, {"task_id": None}, {}]
        )
        legacy_same_task = self._facts(
            prior_entries=[
                {"task_id": "T-0002"},
                {"task_id": "T-0001", "origin": "browser", "date": "2020-01-01"},
            ]
        )

        self.assertTrue(unrelated["first_for_task"])
        self.assertFalse(legacy_same_task["first_for_task"])

    def test_ordinary_origin_builds_facts_but_refuses_a_notice(self) -> None:
        facts = self._facts(origin=None)

        self.assertIsNone(facts["recorded"]["origin"])
        with self.assertRaises(CheckpointChangeError):
            build_committed_notice(facts=facts, event_id=1)

    def test_key_whitespace_and_case_policy(self) -> None:
        upper = derive_checkpoint_id(
            workspace_uid=self.BASE_UID, idempotency_key="AGENT-INTENT-0001"
        )
        lower = derive_checkpoint_id(
            workspace_uid=self.BASE_UID, idempotency_key="agent-intent-0001"
        )

        self.assertNotEqual(upper, lower)
        for key in (" Agent-Intent-0001", "Agent-Intent-0001 ", "Agent Intent 0001"):
            with self.subTest(key=key):
                with self.assertRaises(CheckpointChangeError):
                    derive_checkpoint_id(
                        workspace_uid=self.BASE_UID, idempotency_key=key
                    )


if __name__ == "__main__":  # pragma: no cover - module is run through unittest
    unittest.main()


class _Text(str):
    """An ordinary str subclass: equal by value, outside the built-in JSON domain."""


class StrictSchemaKeyAndValueTypeTest(unittest.TestCase):
    """Schema keys and the recorded type must be exact built-in strings."""

    def _facts(self) -> dict:
        return build_checkpoint_facts(
            workspace_uid="123e4567-e89b-42d3-a456-426614174000",
            idempotency_key="Agent-Intent-0001",
            date="2026-09-03",
            entry={
                "task_id": "T-0001",
                "task": "Then stored title",
                "done": ["Finished"],
                "next": ["Next"],
                "blockers": [],
            },
            ordinal=0,
            prior_entries=[],
            origin="agent-cli-v1",
        )

    def test_ordinary_string_facts_still_build_a_notice(self) -> None:
        """Positive control: the guards must not refuse plain built-in strings."""

        facts = self._facts()

        notice = build_committed_notice(facts=facts, event_id=1)

        self.assertEqual(notice["kind"], "agent.checkpoint.committed")
        self.assertEqual(list(notice), VECTORS["notice_keys"])

    def test_subclass_recorded_type_value_is_refused(self) -> None:
        facts = self._facts()
        facts["recorded"]["type"] = _Text("worklog.recorded")

        self.assertEqual(facts["recorded"]["type"], "worklog.recorded")
        with self.assertRaises(CheckpointChangeError) as raised:
            build_committed_notice(facts=facts, event_id=1)
        self.assertEqual(str(raised.exception), "invalid checkpoint input")

    def test_subclass_outer_facts_key_is_refused(self) -> None:
        facts = self._facts()
        facts[_Text("recorded")] = facts.pop("recorded")

        self.assertEqual(set(facts), {"recorded", "done_count", "next_count",
                                      "blocker_count", "first_for_task"})
        with self.assertRaises(CheckpointChangeError):
            build_committed_notice(facts=facts, event_id=1)

    def test_subclass_recorded_body_key_is_refused(self) -> None:
        facts = self._facts()
        facts["recorded"][_Text("type")] = facts["recorded"].pop("type")

        with self.assertRaises(CheckpointChangeError):
            build_committed_notice(facts=facts, event_id=1)

    def test_subclass_entry_key_is_refused_by_the_facts_builder(self) -> None:
        entry = {
            "task_id": "T-0001",
            "task": "Then stored title",
            "done": ["Finished"],
            "next": ["Next"],
            "blockers": [],
        }
        entry[_Text("task")] = entry.pop("task")

        with self.assertRaises(CheckpointChangeError):
            build_checkpoint_facts(
                workspace_uid="123e4567-e89b-42d3-a456-426614174000",
                idempotency_key="Agent-Intent-0001",
                date="2026-09-03",
                entry=entry,
                ordinal=0,
                prior_entries=[],
                origin="agent-cli-v1",
            )

    def test_ordinary_string_entry_keys_remain_accepted(self) -> None:
        """Positive control beside the entry-key negative."""

        facts = self._facts()

        self.assertEqual(facts["recorded"]["type"], "worklog.recorded")
        self.assertTrue(facts["first_for_task"])
