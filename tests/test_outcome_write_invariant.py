"""K2 write invariant: parent auto-align, fail-closed roster, omission/clear, stale revision."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workstack.outcome_write_invariant import (
    OutcomeWriteInvariantError,
    apply_task_outcome_write_invariant,
    canonicalize_key_result_refs,
    ensure_parent_objective_ids,
    serialize_task_patch,
    validate_key_result_ref_roster,
)
from workstack.service import DomainError, RevisionConflictError, WorkStack
from workstack.store import Store


def _roster(*objectives: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for record in objectives:
        grouped.setdefault(record["id"], []).append(record)
    return grouped


def _objective(oid: str, *key_result_ids: str) -> dict:
    return {
        "id": oid,
        "key_results": [{"id": krid, "text": krid} for krid in key_result_ids],
    }


class PureOutcomeWriteInvariantTest(unittest.TestCase):
    def test_canonicalize_drops_duplicates_then_sorts(self) -> None:
        refs = canonicalize_key_result_refs(
            [
                {"objective_id": "O-2", "key_result_id": "KR-1"},
                {"objective_id": "O-1", "key_result_id": "KR-1"},
                {"objective_id": "O-2", "key_result_id": "KR-1"},
            ]
        )
        self.assertEqual(
            refs,
            [
                {"objective_id": "O-1", "key_result_id": "KR-1"},
                {"objective_id": "O-2", "key_result_id": "KR-1"},
            ],
        )

    def test_ensure_parent_appends_once_without_reordering_existing(self) -> None:
        aligned = ensure_parent_objective_ids(
            ["O-2", "O-2"],
            [{"objective_id": "O-1", "key_result_id": "KR-1"}],
        )
        self.assertEqual(aligned, ["O-2", "O-1"])

    def test_serialize_omits_none_and_keeps_empty_list(self) -> None:
        self.assertNotIn("key_result_refs", serialize_task_patch({"revision": 1}))
        self.assertNotIn(
            "key_result_refs",
            serialize_task_patch({"revision": 1, "key_result_refs": None}),
        )
        self.assertEqual(
            serialize_task_patch({"revision": 1, "key_result_refs": []})["key_result_refs"],
            [],
        )

    def test_wrong_parent_fails_closed(self) -> None:
        roster = _roster(_objective("O-1", "KR-1"), _objective("O-2", "KR-1", "KR-2"))
        with self.assertRaises(OutcomeWriteInvariantError) as caught:
            validate_key_result_ref_roster(
                {"objective_id": "O-1", "key_result_id": "KR-2"},
                roster,
            )
        self.assertEqual(
            str(caught.exception),
            "key result is not owned by the referenced objective",
        )

    def test_apply_auto_aligns_missing_parent_and_skips_omitted_refs(self) -> None:
        roster = _roster(_objective("O-1", "KR-1"))
        changes = {
            "key_result_refs": [{"objective_id": "O-1", "key_result_id": "KR-1"}],
        }
        apply_task_outcome_write_invariant(changes, {"objective_ids": ["O-2"]}, roster)
        self.assertEqual(changes["objective_ids"], ["O-2", "O-1"])

        omitted = {"title": "x"}
        apply_task_outcome_write_invariant(
            omitted,
            {
                "objective_ids": ["O-1"],
                "key_result_refs": [{"objective_id": "O-1", "key_result_id": "KR-1"}],
            },
            roster,
        )
        self.assertNotIn("key_result_refs", omitted)
        self.assertNotIn("objective_ids", omitted)


class PatchTaskOutcomeWriteInvariantTest(unittest.TestCase):
    """Authoritative mutation boundary: WorkStack.patch_task."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.first = self.stack.add_objective("First objective")
        self.second = self.stack.add_objective("Second objective")
        self.first_kr = self.stack.add_key_result(self.first["id"], "First outcome")
        self.second_kr = self.stack.add_key_result(self.second["id"], "Second outcome")
        self.second_only_kr = self.stack.add_key_result(self.second["id"], "Second-only")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store_bytes(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.root.glob("*.json")}

    def _persisted(self, task_id: str) -> dict[str, object]:
        import json

        backlog = json.loads((self.root / "backlog.json").read_text(encoding="utf-8"))
        return next(item for item in backlog["tasks"] if item["id"] == task_id)

    def test_adding_valid_ref_auto_aligns_parent_exactly_once(self) -> None:
        task = self.stack.add_task("Needs parent")
        task = self.stack.patch_task(
            task["id"],
            {"objective_ids": [self.second["id"]], "revision": task["revision"]},
        )

        updated = self.stack.patch_task(
            task["id"],
            {
                "key_result_refs": [
                    {
                        "objective_id": self.first["id"],
                        "key_result_id": self.first_kr["id"],
                    }
                ],
                "revision": task["revision"],
            },
        )

        self.assertEqual(updated["objective_ids"], [self.second["id"], self.first["id"]])
        self.assertEqual(updated["objective_ids"].count(self.first["id"]), 1)
        self.assertEqual(
            self._persisted(task["id"])["objective_ids"],
            [self.second["id"], self.first["id"]],
        )

    def test_wrong_parent_writes_nothing(self) -> None:
        task = self.stack.add_task("Wrong parent")
        before = self._store_bytes()

        with self.assertRaises(DomainError) as caught:
            self.stack.patch_task(
                task["id"],
                {
                    "key_result_refs": [
                        {
                            "objective_id": self.first["id"],
                            "key_result_id": self.second_only_kr["id"],
                        }
                    ],
                    "revision": task["revision"],
                },
            )

        self.assertEqual(
            str(caught.exception),
            "key result is not owned by the referenced objective",
        )
        self.assertEqual(self._store_bytes(), before)

    def test_duplicate_refs_canonicalize_without_refusing(self) -> None:
        task = self.stack.add_task("Dup")
        updated = self.stack.patch_task(
            task["id"],
            {
                "key_result_refs": [
                    {
                        "objective_id": self.first["id"],
                        "key_result_id": self.first_kr["id"],
                    },
                    {
                        "objective_id": self.first["id"].lower(),
                        "key_result_id": self.first_kr["id"].lower(),
                    },
                ],
                "revision": task["revision"],
            },
        )
        self.assertEqual(len(updated["key_result_refs"]), 1)
        self.assertEqual(updated["objective_ids"], [self.first["id"]])

    def test_clearing_refs_keeps_explicit_objective_alignment(self) -> None:
        task = self.stack.add_task("Linked")
        linked = self.stack.patch_task(
            task["id"],
            {
                "key_result_refs": [
                    {
                        "objective_id": self.first["id"],
                        "key_result_id": self.first_kr["id"],
                    }
                ],
                "revision": task["revision"],
            },
        )
        cleared = self.stack.patch_task(
            linked["id"],
            {"key_result_refs": [], "revision": linked["revision"]},
        )
        self.assertEqual(cleared["key_result_refs"], [])
        self.assertEqual(cleared["objective_ids"], [self.first["id"]])

    def test_omitted_refs_survive_unrelated_patch(self) -> None:
        task = self.stack.add_task("Legacy")
        self.assertNotIn("key_result_refs", self._persisted(task["id"]))
        self.stack.patch_task(
            task["id"], {"title": "Renamed", "revision": task["revision"]}
        )
        self.assertNotIn("key_result_refs", self._persisted(task["id"]))

    def test_stale_revision_does_not_partially_align(self) -> None:
        task = self.stack.add_task("Stale")
        task = self.stack.patch_task(
            task["id"], {"title": "Named", "revision": task["revision"]}
        )
        before = self._store_bytes()
        with self.assertRaises(RevisionConflictError):
            self.stack.patch_task(
                task["id"],
                {
                    "key_result_refs": [
                        {
                            "objective_id": self.first["id"],
                            "key_result_id": self.first_kr["id"],
                        }
                    ],
                    "revision": task["revision"] - 1,
                },
            )
        self.assertEqual(self._store_bytes(), before)
        persisted = self._persisted(task["id"])
        self.assertEqual(persisted.get("objective_ids", []), [])
        self.assertNotIn("key_result_refs", persisted)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
