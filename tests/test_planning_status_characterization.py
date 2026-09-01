from __future__ import annotations

import copy
import unittest

from workstack.planning_status import PlanningStatusValidationError, validate_and_project


def _task(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "T-0001",
        "uid": "11111111-1111-5111-8111-111111111111",
        "status": "open",
        "revision": 0,
        "status_fact_id": "PS-000001",
    }
    value.update(changes)
    return value


def _fact(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "PS-000001",
        "type": "task.planning_status",
        "task_id": "T-0001",
        "task_uid": "11111111-1111-5111-8111-111111111111",
        "previous_fact_id": None,
        "prior_revision": None,
        "new_revision": 0,
        "prior_status": None,
        "status": "open",
        "created_at": "2026-08-31T01:02:03Z",
        "actor": "local.user",
        "provenance": "api.v1",
    }
    value.update(changes)
    return value


def _valid_chain() -> tuple[dict[str, object], dict[str, object]]:
    backlog: dict[str, object] = {
        "tasks": [_task(revision=1, status_fact_id="PS-000002")]
    }
    activity: dict[str, object] = {
        "planning_status": [
            _fact(),
            _fact(
                id="PS-000002",
                previous_fact_id="PS-000001",
                prior_revision=0,
                new_revision=1,
                prior_status="open",
                status="started",
                created_at="2026-08-31T01:03:03Z",
            ),
        ]
    }
    return backlog, activity


class PlanningStatusCharacterizationTests(unittest.TestCase):
    def assert_invalid(
        self,
        backlog: dict[str, object],
        activity: dict[str, object],
        message: str,
    ) -> None:
        with self.assertRaisesRegex(PlanningStatusValidationError, "^" + message + "$"):
            validate_and_project(backlog, activity)

    def test_valid_bootstrap_and_transition_project_the_fact_head(self) -> None:
        backlog, activity = _valid_chain()
        self.assertEqual(validate_and_project(backlog, activity), {"T-0001": "started"})

    def test_store_and_task_envelope_failures_are_stable(self) -> None:
        self.assert_invalid({"tasks": {}}, {"planning_status": []}, "planning status store shape is invalid")
        self.assert_invalid(
            {"tasks": [None]}, {"planning_status": []},
            "planning status task reference is invalid",
        )

    def test_fact_field_identity_value_and_provenance_failures_are_stable(self) -> None:
        cases: list[tuple[str, object, str]] = [
            ("schema", lambda fact: fact.pop("actor"), "planning status fact schema is invalid"),
            ("order", lambda fact: fact.update(id="PS-000002"), "planning status fact order is invalid"),
            ("type", lambda fact: fact.update(type="other"), "planning status fact type is invalid"),
            ("identity", lambda fact: fact.update(task_uid="wrong"), "planning status task identity is invalid"),
            ("status", lambda fact: fact.update(status="invalid"), "planning status value is invalid"),
            ("timestamp", lambda fact: fact.update(created_at="2026-08-31"), "planning status created_at is invalid"),
            ("provenance", lambda fact: fact.update(provenance="untrusted"), "planning status provenance is invalid"),
        ]
        for label, mutate, message in cases:
            backlog = {"tasks": [_task()]}
            fact = _fact()
            mutate(fact)
            with self.subTest(label=label):
                self.assert_invalid(backlog, {"planning_status": [fact]}, message)

    def test_bootstrap_chain_revision_and_head_failures_are_stable(self) -> None:
        bootstrap_cases = (
            (
                _fact(previous_fact_id="PS-000000"),
                _task(),
                "planning status bootstrap is invalid",
            ),
            (
                _fact(status="started"),
                _task(),
                "planning status baseline does not match source",
            ),
            (
                _fact(new_revision=1),
                _task(),
                "planning status fact exceeds task revision",
            ),
            (
                _fact(),
                _task(status_fact_id="PS-999999"),
                "planning status head is missing or stale",
            ),
        )
        for fact, task, message in bootstrap_cases:
            with self.subTest(message=message):
                self.assert_invalid({"tasks": [task]}, {"planning_status": [fact]}, message)

        backlog, activity = _valid_chain()
        predecessor = copy.deepcopy(activity)
        predecessor["planning_status"][1]["previous_fact_id"] = "PS-999999"
        self.assert_invalid(backlog, predecessor, "planning status predecessor is invalid")

        for label, field, value in (
            ("revision gap", "new_revision", 2),
            ("prior regression", "prior_revision", -1),
            ("prior status", "prior_status", "done"),
            ("same status", "status", "open"),
        ):
            corrupt = copy.deepcopy(activity)
            corrupt["planning_status"][1][field] = value
            expected = "prior_revision is invalid" if label == "prior regression" else "planning status transition is invalid"
            with self.subTest(label=label):
                self.assert_invalid(backlog, corrupt, expected)

    def test_missing_fact_for_a_task_is_rejected(self) -> None:
        self.assert_invalid(
            {"tasks": [_task()]},
            {"planning_status": []},
            "planning status head is missing or stale",
        )


if __name__ == "__main__":
    unittest.main()
