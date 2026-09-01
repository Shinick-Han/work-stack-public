from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from workstack.service import DomainError, IdempotencyConflictError, WorkStack
from workstack.store import Store


class TaskCreationV1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_strict_validation_refuses_without_persistence(self) -> None:
        invalid = (
            {},
            {"title": "Valid", "unknown": True},
            {"title": 42},
            {"title": "Valid", "detail": 42},
            {"title": "Valid", "priority": "urgent"},
            {"title": "Valid", "due": "tomorrow"},
            {"title": "Valid", "tags": "one"},
            {"title": "Valid", "tags": ["one", 2]},
            {"title": "Valid", "objective_ids": "O-1"},
        )
        before = {path.name: path.read_bytes() for path in self.root.glob("*.json")}
        for index, body in enumerate(invalid):
            with self.subTest(index=index):
                with self.assertRaises(DomainError):
                    self.stack.create_task_v1(body, "create.invalid.{:02d}".format(index))
                self.assertEqual(self.stack.list_tasks(status="all"), [])
                self.assertEqual(self.store_idempotency(), [])
        self.assertEqual(
            before,
            {name: self.root.joinpath(name).read_bytes() for name in before},
        )
        with self.assertRaises(DomainError):
            self.stack.create_task_v1({"title": "Valid"}, "short")

    def test_strict_validation_precedence_and_schedule_normalization_are_frozen(self) -> None:
        invalid = (
            (
                {"title": "Valid", "unknown": True, "detail": 42},
                "task create has unknown fields",
                {"fields": ["unknown"]},
            ),
            ({"title": " ", "detail": 42}, "title must be a non-empty string", {"field": "title"}),
            ({"title": "Valid", "detail": 42, "priority": "urgent"}, "detail must be a string", {"field": "detail"}),
            ({"title": "Valid", "priority": "urgent", "due": "tomorrow"}, "priority must be one of P0, P1, P2, P3", {"field": "priority"}),
            ({"title": "Valid", "due": "tomorrow", "scheduled": "later"}, "due must be null or YYYY-MM-DD", {"field": "due"}),
            ({"title": "Valid", "scheduled": "later", "estimate_minutes": True}, "scheduled must be null or YYYY-MM-DD", {"field": "scheduled"}),
            ({"title": "Valid", "estimate_minutes": True, "tags": "bad"}, "estimate_minutes must be null or an integer from 1 to 1440", {"field": "estimate_minutes"}),
            ({"title": "Valid", "tags": "bad", "objective_ids": "bad"}, "tags must be an array of strings", {"field": "tags"}),
        )
        before = {path.name: path.read_bytes() for path in self.root.glob("*.json")}
        for index, (body, message, details) in enumerate(invalid):
            with self.subTest(index=index):
                with self.assertRaises(DomainError) as raised:
                    self.stack.create_task_v1(body, "create.precedence.{:02d}".format(index))
                self.assertEqual(str(raised.exception), message)
                self.assertEqual(raised.exception.details, details)
        self.assertEqual(
            before,
            {name: self.root.joinpath(name).read_bytes() for name in before},
        )

        objective = self.stack.add_objective("Execution quality")
        created = self.stack.create_task_v1(
            {
                "title": "  Scheduled task  ",
                "scheduled": "2030-02-03",
                "estimate_minutes": 45,
                "tags": [" beta ", "alpha", "beta"],
                "objective_ids": [objective["id"].lower(), objective["id"]],
            },
            "create.schedule.0001",
        )["body"]["data"]
        self.assertEqual(created["title"], "Scheduled task")
        self.assertEqual(created["scheduled"], "2030-02-03")
        self.assertEqual(created["estimate_minutes"], 45)
        self.assertEqual(created["tags"], ["alpha", "beta"])
        self.assertEqual(created["objective_ids"], [objective["id"]])

    def test_append_task_relationship_normalization_and_error_precedence_are_frozen(self) -> None:
        parent = self.stack.add_task("Parent")
        child = self.stack.add_task(
            "Child",
            parent_id="  {}  ".format(parent["id"].lower()),
            dependencies=[parent["id"].lower(), parent["id"], " "],
        )
        self.assertEqual(child["parent_id"], parent["id"])
        self.assertEqual(child["dependencies"], [parent["id"]])

        with self.assertRaisesRegex(ValueError, "priority must be one of"):
            self.stack.add_task("Valid", priority="urgent", due="tomorrow")
        with self.assertRaisesRegex(ValueError, "unknown task ids: T-9999"):
            self.stack.add_task(" ", parent_id="t-9999")
        with self.assertRaisesRegex(ValueError, "title is required"):
            self.stack.add_task(" ", objective_ids=["O-9999"])
        self.assertEqual([task["id"] for task in self.stack.list_tasks(status="all")], [parent["id"], child["id"]])

    def test_first_creation_exact_replay_and_conflict(self) -> None:
        body = {
            "title": "  One task  ",
            "detail": "  Frozen response  ",
            "priority": "P1",
            "due": "2030-01-02",
            "tags": ["beta", "alpha", "beta"],
            "objective_ids": [],
        }
        first = self.stack.create_task_v1(body, "create.exact.0001")
        self.assertEqual(first["status"], 201)
        self.assertFalse(first["body"]["meta"]["replayed"])
        created = first["body"]["data"]
        self.assertEqual(created["title"], "One task")
        self.assertEqual(created["detail"], "Frozen response")
        self.assertEqual(created["tags"], ["alpha", "beta"])
        self.assertEqual(created["revision"], 0)

        self.stack.patch_task(
            created["id"], {"title": "Later edit", "revision": created["revision"]}
        )
        replay = self.stack.create_task_v1(
            {
                "title": "One task",
                "detail": "Frozen response",
                "priority": "P1",
                "due": "2030-01-02",
                "tags": ["alpha", "beta"],
                "objective_ids": [],
            },
            "create.exact.0001",
        )
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(replay["body"]["data"], created)
        self.assertEqual(len(self.stack.list_tasks(status="all")), 1)

        with self.assertRaises(IdempotencyConflictError):
            self.stack.create_task_v1(
                {"title": "Different task"}, "create.exact.0001"
            )
        self.assertEqual(len(self.stack.list_tasks(status="all")), 1)

    def test_response_loss_and_restart_replay_same_frozen_response(self) -> None:
        committed = self.stack.create_task_v1(
            {"title": "Committed before response loss"}, "create.restart.0001"
        )
        committed_task = committed["body"]["data"]

        restarted = WorkStack(Store(self.root))
        replay = restarted.create_task_v1(
            {"title": "Committed before response loss"}, "create.restart.0001"
        )
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(replay["body"]["data"], committed_task)
        self.assertEqual(len(restarted.list_tasks(status="all")), 1)
        self.assertEqual(len(self.store_idempotency(restarted)), 1)

    def test_two_concurrent_requests_create_one_task(self) -> None:
        def create() -> dict:
            return self.stack.create_task_v1(
                {"title": "Concurrent intent"}, "create.concurrent.0001"
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))

        self.assertEqual(sorted(result["status"] for result in results), [200, 201])
        self.assertEqual(len(self.stack.list_tasks(status="all")), 1)
        self.assertEqual(len(self.store_idempotency()), 1)
        self.assertEqual(
            results[0]["body"]["data"]["uid"], results[1]["body"]["data"]["uid"]
        )

    def store_idempotency(self, stack: WorkStack | None = None) -> list[dict]:
        target = stack or self.stack
        return target.store.load("activity.json").get("idempotency", [])


if __name__ == "__main__":
    unittest.main()
