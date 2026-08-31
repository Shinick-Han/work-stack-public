from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from workstack.service import IdempotencyConflictError, WorkStack
from workstack.store import Store


class IntentMutationsV1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Intent boundary")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_all_creation_intents_replay_after_restart_without_duplicates(self) -> None:
        objective_body = {"objective": "One outcome", "quarter": "2026-Q3"}
        note_body = {"text": "One decision", "links": [self.task["id"]]}
        task_note_body = {"text": "One Task note", "revision": 0}
        subtask_body = {"title": "One next step", "priority": "P1", "revision": 1}

        objective = self.stack.create_objective_v1(
            objective_body, "objective.restart.0001"
        )
        key_result_body = {"text": "One measure", "target": "100%", "revision": 0}
        key_result_path = "/api/v1/objectives/O-1/key-results"
        key_result = self.stack.add_key_result_v1(
            "O-1",
            key_result_body,
            "key.result.restart.0001",
            path=key_result_path,
        )
        note = self.stack.create_note_v1(note_body, "note.restart.0001")
        task_note = self.stack.add_task_note_v1(
            self.task["id"],
            task_note_body,
            "task.note.restart.0001",
            path="/api/v1/tasks/{}/notes".format(self.task["id"]),
        )
        subtask = self.stack.add_subtask_v1(
            self.task["id"],
            subtask_body,
            "task.subtask.restart.0001",
            path="/api/v1/tasks/{}/subtasks".format(self.task["id"]),
        )

        restarted = WorkStack(Store(self.root))
        replays = (
            restarted.create_objective_v1(objective_body, "objective.restart.0001"),
            restarted.add_key_result_v1(
                "O-1",
                key_result_body,
                "key.result.restart.0001",
                path=key_result_path,
            ),
            restarted.create_note_v1(note_body, "note.restart.0001"),
            restarted.add_task_note_v1(
                self.task["id"],
                task_note_body,
                "task.note.restart.0001",
                path="/api/v1/tasks/{}/notes".format(self.task["id"]),
            ),
            restarted.add_subtask_v1(
                self.task["id"],
                subtask_body,
                "task.subtask.restart.0001",
                path="/api/v1/tasks/{}/subtasks".format(self.task["id"]),
            ),
        )

        for first, replay in zip((objective, key_result, note, task_note, subtask), replays):
            self.assertEqual(replay["status"], 200)
            self.assertTrue(replay["body"]["meta"]["replayed"])
            self.assertEqual(replay["body"]["data"], first["body"]["data"])
        self.assertEqual(len(restarted.list_objectives(status="all")), 1)
        self.assertEqual(len(restarted.store.load("notes.json")["notes"]), 1)
        persisted_task = restarted.get_task(self.task["id"])
        self.assertEqual(len(persisted_task["notes"]), 1)
        self.assertEqual(len(persisted_task["subtasks"]), 1)
        self.assertEqual(len(restarted.store.load("activity.json")["idempotency"]), 5)

    def test_concurrent_objective_requests_create_one_record(self) -> None:
        def create() -> dict:
            return self.stack.create_objective_v1(
                {"objective": "One concurrent outcome", "quarter": "2026-Q3"},
                "objective.concurrent.0001",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))

        self.assertEqual(sorted(item["status"] for item in results), [200, 201])
        self.assertEqual(len(self.stack.list_objectives(status="all")), 1)

    def test_reused_key_with_different_body_fails_closed(self) -> None:
        self.stack.create_note_v1(
            {"text": "Original", "links": []}, "note.conflict.0001"
        )
        with self.assertRaises(IdempotencyConflictError):
            self.stack.create_note_v1(
                {"text": "Different", "links": []}, "note.conflict.0001"
            )
        self.assertEqual(len(self.stack.store.load("notes.json")["notes"]), 1)

    def test_review_intents_replay_after_restart_without_duplicate_evidence(self) -> None:
        checkin_body = {"date": "2026-08-30", "time": "09:20"}
        entry_body = {
            "date": "2026-08-30",
            "task_id": self.task["id"],
            "done": ["Closed one gate"],
            "next": ["Open the next gate"],
            "blockers": [],
        }
        checkin = self.stack.checkin_v1(checkin_body, "review.checkin.restart.0001")
        entry = self.stack.add_worklog_v1(entry_body, "review.entry.restart.0001")

        restarted = WorkStack(Store(self.root))
        checkin_replay = restarted.checkin_v1(
            checkin_body, "review.checkin.restart.0001"
        )
        entry_replay = restarted.add_worklog_v1(
            entry_body, "review.entry.restart.0001"
        )

        self.assertEqual(checkin_replay["body"]["data"], checkin["body"]["data"])
        self.assertEqual(entry_replay["body"]["data"], entry["body"]["data"])
        self.assertTrue(checkin_replay["body"]["meta"]["replayed"])
        self.assertTrue(entry_replay["body"]["meta"]["replayed"])
        projection = restarted.review_projection("2026-08-30")
        self.assertEqual(len(projection["day"]["entries"]), 1)


if __name__ == "__main__":
    unittest.main()
