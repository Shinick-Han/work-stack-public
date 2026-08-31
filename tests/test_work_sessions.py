from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workstack.service import WorkSessionConflictError, WorkStack
from workstack.store import Store


class WorkSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.stack = WorkStack(self.store)
        self.task = self.stack.add_task("Prepare the weekly decision brief", priority="P1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_start_is_idempotent_single_active_and_planning_state_neutral(self) -> None:
        backlog_before = self.store.path("backlog.json").read_bytes()
        snapshot_before = self.stack.planning_snapshot(self.task["id"])

        with (
            patch("workstack.service.utc_now", return_value="2026-08-30T09:00:00Z"),
            patch("workstack.service.today", return_value="2026-08-30"),
        ):
            started = self.stack.start_work_session_v1(
                {"task_id": self.task["id"]}, "work-session.start.0001"
            )

        self.assertEqual(started["status"], 201)
        self.assertEqual(
            started["body"]["data"],
            {
                "id": "WS-000001",
                "task_id": self.task["id"],
                "task": self.task["title"],
                "date": "2026-08-30",
                "state": "running",
                "started_at": "2026-08-30T09:00:00Z",
                "updated_at": "2026-08-30T09:00:00Z",
                "elapsed_seconds": 0,
                "worklog_state": "not_ready",
            },
        )
        self.assertEqual(self.store.path("backlog.json").read_bytes(), backlog_before)
        snapshot_after = self.stack.planning_snapshot(self.task["id"])
        self.assertEqual(snapshot_after.digest, snapshot_before.digest)
        self.assertEqual(snapshot_after.canonical_bytes, snapshot_before.canonical_bytes)

        restarted = WorkStack(Store(self.store.root))
        replayed = restarted.start_work_session_v1(
            {"task_id": self.task["id"]}, "work-session.start.0001"
        )
        self.assertEqual(replayed["status"], 200)
        self.assertTrue(replayed["body"]["meta"]["replayed"])
        self.assertEqual(replayed["body"]["data"], started["body"]["data"])

        other = restarted.add_task("A second task")
        with self.assertRaises(WorkSessionConflictError):
            restarted.start_work_session_v1(
                {"task_id": other["id"]}, "work-session.start.0002"
            )

    def test_pause_resume_stop_and_confirmed_worklog_survive_restart(self) -> None:
        with (
            patch("workstack.service.utc_now", return_value="2026-08-30T09:00:00Z"),
            patch("workstack.service.today", return_value="2026-08-30"),
        ):
            started = self.stack.start_work_session_v1(
                {"task_id": self.task["id"]}, "work-session.flow.start"
            )["body"]["data"]

        with patch("workstack.service.utc_now", return_value="2026-08-30T09:10:00Z"):
            paused = self.stack.transition_work_session_v1(
                started["id"], "pause", {}, "work-session.flow.pause"
            )["body"]["data"]
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(paused["elapsed_seconds"], 600)

        with self.assertRaises(WorkSessionConflictError):
            self.stack.transition_work_session_v1(
                started["id"], "pause", {}, "work-session.flow.pause-again"
            )

        with patch("workstack.service.utc_now", return_value="2026-08-30T09:15:00Z"):
            resumed = self.stack.transition_work_session_v1(
                started["id"], "resume", {}, "work-session.flow.resume"
            )["body"]["data"]
        self.assertEqual(resumed["state"], "running")
        self.assertEqual(resumed["elapsed_seconds"], 600)

        with patch("workstack.service.utc_now", return_value="2026-08-30T09:35:00Z"):
            stopped = self.stack.transition_work_session_v1(
                started["id"], "stop", {}, "work-session.flow.stop"
            )["body"]["data"]
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["elapsed_seconds"], 1800)
        self.assertEqual(stopped["worklog_state"], "pending")

        restarted = WorkStack(Store(self.store.root))
        projection = restarted.work_sessions_projection()
        self.assertIsNone(projection["current"])
        self.assertEqual([item["id"] for item in projection["pending"]], [started["id"]])

        body = {
            "done": ["Prepared the decision brief outline"],
            "next": ["Verify the supporting evidence"],
            "blockers": [],
        }
        recorded = restarted.record_work_session_v1(
            started["id"], body, "work-session.flow.worklog"
        )
        self.assertEqual(recorded["status"], 201)
        self.assertEqual(recorded["body"]["data"]["session_id"], started["id"])
        self.assertEqual(recorded["body"]["data"]["duration_seconds"], 1800)

        replayed = WorkStack(Store(self.store.root)).record_work_session_v1(
            started["id"], body, "work-session.flow.worklog"
        )
        self.assertEqual(replayed["status"], 200)
        self.assertTrue(replayed["body"]["meta"]["replayed"])
        self.assertEqual(
            len(self.store.load("worklog.json")["days"]["2026-08-30"]["entries"]),
            1,
        )
        report = restarted.weekly_report(end="2026-08-30", days=7)
        self.assertEqual(report["projects"][0]["duration_seconds"], 1800)
        self.assertEqual(restarted.get_task(self.task["id"])["revision"], 0)


if __name__ == "__main__":
    unittest.main()
