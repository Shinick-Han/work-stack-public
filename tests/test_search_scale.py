from __future__ import annotations

import tempfile
import time
import unittest
import uuid
from pathlib import Path

from workstack.planning_status import append_bootstrap
from workstack.service import WorkStack
from workstack.store import Store


class SearchScaleTest(unittest.TestCase):
    def build_stack(self, count: int) -> WorkStack:
        root = Path(self.temporary.name)
        stack = WorkStack(Store(root))
        workspace_id = stack.store.load("workspace.json")["id"]
        backlog = stack.store.load("backlog.json")
        activity = stack.store.load("activity.json")
        for index in range(1, count + 1):
            task_id = "T-{:04d}".format(index)
            task = {
                "id": task_id,
                "uid": str(uuid.uuid5(uuid.UUID(workspace_id), task_id)),
                "title": "Synthetic scale task {}".format(index),
                "detail": "Search marker group {}".format(index % 97),
                "status": "open",
                "priority": "P2",
                "due": None,
                "tags": ["group-{}".format(index % 31)],
                "objective_ids": [],
                "parent_id": None,
                "dependencies": [],
                "subtasks": [],
                "notes": [],
                "created": "2026-08-30",
                "updated_at": "2026-08-30",
                "revision": 0,
            }
            append_bootstrap(
                activity,
                task,
                created_at="2026-08-30T00:00:00Z",
                actor="workstack.seed",
                provenance="demo.fixture",
            )
            backlog["tasks"].append(task)
        stack.store.save_many(
            {"backlog.json": backlog, "activity.json": activity},
            operation_id="search-scale-{}".format(count),
        )
        return stack

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_warm_10000_task_search_is_bounded_and_mutation_invalidates_index(self) -> None:
        stack = self.build_stack(10_000)
        first = stack.search_projection("Synthetic scale task 10000", 30)
        self.assertEqual(first["items"][0]["id"], "T-10000")

        started = time.perf_counter()
        warm = stack.search_projection("Search marker group 96", 30)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertTrue(warm["items"])
        self.assertLess(elapsed_ms, 1_000)

        created = stack.add_task("Fresh cache invalidation marker")
        refreshed = stack.search_projection("Fresh cache invalidation marker", 30)
        self.assertEqual(refreshed["items"][0]["id"], created["id"])


if __name__ == "__main__":
    unittest.main()
