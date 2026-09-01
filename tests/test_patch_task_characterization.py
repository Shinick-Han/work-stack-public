from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from workstack.service import DomainError, RevisionConflictError, WorkStack
from workstack.store import Store


class PatchTaskCharacterizationTest(unittest.TestCase):
    """Freeze the externally observable patch contract before decomposition."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.objective = self.stack.add_objective("Characterize patching", "2026-Q3")
        self.parent = self.stack.add_task("Parent")
        self.task = self.stack.add_task("Target")
        self.dependency = self.stack.add_task("Dependency")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def store_bytes(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.root.glob("*.json")}

    def assert_rejected_without_persistence(
        self,
        patch: Any,
        message: str,
        *,
        exception: type[Exception] = DomainError,
    ) -> None:
        before = self.store_bytes()
        with self.assertRaisesRegex(exception, message):
            self.stack.patch_task(self.task["id"], patch)
        self.assertEqual(self.store_bytes(), before)

    def test_invalid_patch_table_is_fail_closed(self) -> None:
        cases = (
            ([], "task patch must be an object", DomainError),
            ({"revision": 0, "extra": True}, "unknown task fields", DomainError),
            ({"title": "Changed"}, "revision is required", DomainError),
            ({"revision": True}, "revision is required", DomainError),
            ({"revision": -1}, "revision is required", DomainError),
            ({"revision": 0, "title": 42}, "title must be a string", DomainError),
            ({"revision": 0, "title": "  "}, "title is required", ValueError),
            ({"revision": 0, "detail": 42}, "detail must be a string", DomainError),
            ({"revision": 0, "status": "paused"}, "invalid task status", DomainError),
            ({"revision": 0, "priority": "urgent"}, "invalid task priority", DomainError),
            ({"revision": 0, "due": 42}, "due must be an ISO date or null", DomainError),
            ({"revision": 0, "due": "2026-8-1"}, "due must be an ISO date or null", DomainError),
            ({"revision": 0, "scheduled": "2026-02-30"}, "scheduled must be an ISO date or null", DomainError),
            ({"revision": 0, "estimate_minutes": True}, "estimate_minutes must be null", DomainError),
            ({"revision": 0, "estimate_minutes": 0}, "estimate_minutes must be null", DomainError),
            ({"revision": 0, "tags": "one"}, "tags must be an array", DomainError),
            ({"revision": 0, "tags": [1]}, "tags entries must be strings", DomainError),
            ({"revision": 0, "objective_ids": [1]}, "objective_ids entries must be strings", DomainError),
            ({"revision": 0, "objective_ids": ["O-999"]}, "unknown objective ids", DomainError),
            ({"revision": 0, "dependencies": [1]}, "dependencies entries must be strings", DomainError),
            ({"revision": 0, "dependencies": ["T-9999"]}, "invalid dependency ids", DomainError),
            ({"revision": 0, "dependencies": [self.task["id"]]}, "invalid dependency ids", DomainError),
            ({"revision": 0, "parent_id": 42}, "parent_id must be a task ID or null", DomainError),
            ({"revision": 0, "parent_id": "T-9999"}, "invalid parent task", DomainError),
            ({"revision": 0, "parent_id": self.task["id"]}, "invalid parent task", DomainError),
        )
        for patch, message, exception in cases:
            with self.subTest(patch=patch):
                self.assert_rejected_without_persistence(
                    patch, message, exception=exception
                )

    def test_stale_revision_reports_expected_and_received_without_persistence(self) -> None:
        before = self.store_bytes()
        with self.assertRaises(RevisionConflictError) as raised:
            self.stack.patch_task(self.task["id"], {"revision": 7, "title": "No"})
        self.assertEqual(
            raised.exception.details,
            {"expected": 0, "received": 7},
        )
        self.assertEqual(self.store_bytes(), before)

    def test_all_patch_fields_normalize_and_emit_field_names_only(self) -> None:
        changed = self.stack.patch_task(
            self.task["id"],
            {
                "revision": 0,
                "title": "  Normalized title  ",
                "detail": "  Kept detail  ",
                "status": "started",
                "priority": "P1",
                "due": "2026-09-10",
                "scheduled": "2026-09-08",
                "estimate_minutes": 45,
                "tags": [" beta ", "alpha", "alpha", ""],
                "objective_ids": [self.objective["id"].lower(), self.objective["id"]],
                "parent_id": self.parent["id"].lower(),
                "dependencies": [self.dependency["id"].lower(), self.dependency["id"]],
            },
        )

        self.assertEqual(changed["revision"], 1)
        self.assertEqual(changed["title"], "Normalized title")
        self.assertEqual(changed["detail"], "Kept detail")
        self.assertEqual(changed["status"], "started")
        self.assertEqual(changed["priority"], "P1")
        self.assertEqual(changed["due"], "2026-09-10")
        self.assertEqual(changed["scheduled"], "2026-09-08")
        self.assertEqual(changed["estimate_minutes"], 45)
        self.assertEqual(changed["tags"], ["alpha", "beta"])
        self.assertEqual(changed["objective_ids"], [self.objective["id"]])
        self.assertEqual(changed["parent_id"], self.parent["id"])
        self.assertEqual(changed["dependencies"], [self.dependency["id"]])

        activity = self.stack.store.load("activity.json")
        update = activity["activity"][-1]
        self.assertEqual(update["type"], "task.updated")
        self.assertEqual(
            update["details"]["fields"],
            [
                "dependencies",
                "detail",
                "due",
                "estimate_minutes",
                "objective_ids",
                "parent_id",
                "priority",
                "scheduled",
                "tags",
                "title",
                "status",
            ],
        )
        self.assertNotIn("Normalized title", str(update))
        facts = activity["planning_status"]
        self.assertEqual(facts[-1]["prior_status"], "open")
        self.assertEqual(facts[-1]["status"], "started")
        self.assertEqual(facts[-1]["prior_revision"], 0)
        self.assertEqual(facts[-1]["new_revision"], 1)

    def test_revision_only_and_same_status_are_byte_identical_noops(self) -> None:
        for patch in ({"revision": 0}, {"revision": 0, "status": "open"}):
            with self.subTest(patch=patch):
                before = self.store_bytes()
                unchanged = self.stack.patch_task(self.task["id"], patch)
                self.assertEqual(unchanged["revision"], 0)
                self.assertEqual(unchanged["status"], "open")
                self.assertEqual(self.store_bytes(), before)

    def test_same_non_status_value_is_currently_a_real_revision(self) -> None:
        changed = self.stack.patch_task(
            self.task["id"], {"revision": 0, "title": self.task["title"]}
        )
        self.assertEqual(changed["revision"], 1)
        self.assertEqual(
            self.stack.store.load("activity.json")["activity"][-1]["details"],
            {"fields": ["title"]},
        )

    def test_relationship_cycles_are_rejected_without_persistence(self) -> None:
        child = self.stack.add_task("Child", parent_id=self.task["id"])
        dependent = self.stack.add_task("Dependent", dependencies=[self.task["id"]])
        before = self.store_bytes()
        with self.assertRaisesRegex(DomainError, "parent relationship would create a cycle"):
            self.stack.patch_task(
                self.task["id"], {"revision": 0, "parent_id": child["id"]}
            )
        self.assertEqual(self.store_bytes(), before)
        with self.assertRaisesRegex(DomainError, "dependency relationship would create a cycle"):
            self.stack.patch_task(
                self.task["id"], {"revision": 0, "dependencies": [dependent["id"]]}
            )
        self.assertEqual(self.store_bytes(), before)

    def test_persistence_failure_does_not_change_authoritative_files(self) -> None:
        before = self.store_bytes()
        original = self.stack.store.save_many

        def fail_before_commit(*args: Any, **kwargs: Any) -> None:
            raise OSError("injected save failure")

        self.stack.store.save_many = fail_before_commit  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(OSError, "injected save failure"):
                self.stack.patch_task(
                    self.task["id"], {"revision": 0, "title": "Never persisted"}
                )
        finally:
            self.stack.store.save_many = original  # type: ignore[method-assign]
        self.assertEqual(self.store_bytes(), before)


if __name__ == "__main__":
    unittest.main()
