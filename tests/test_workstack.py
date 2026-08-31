from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from workstack.server import serve
from workstack.service import WorkStack, current_quarter
from workstack.store import Store, StoreCorruptError


class WorkStackTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.stack = WorkStack(Store(Path(self.temporary.name)))

    def tearDown(self):
        self.temporary.cleanup()

    def test_aligned_flow(self):
        objective = self.stack.add_objective("Make delivery repeatable", "2030-Q2")
        key_result = self.stack.add_key_result(objective["id"], "Use one checklist", "100%")
        task = self.stack.add_task("Draft checklist", priority="P1", objective_ids=[objective["id"]])
        self.stack.set_task_status(task["id"], "started")
        self.stack.add_worklog(task["id"], done=["Drafted sections"], next_items=["Review"])
        self.stack.set_key_result_progress(objective["id"], key_result["id"], 50)
        report = self.stack.weekly_report(days=7)
        self.assertEqual(report["projects"][0]["task_id"], task["id"])
        self.assertEqual(report["projects"][0]["objective_ids"], [objective["id"]])
        self.assertEqual(report["projects"][0]["done"], ["Drafted sections"])

    def test_snapshot_drops_dangling_note_link(self):
        note = self.stack.add_note("Portable design", links=["T-9999"])
        snapshot = self.stack.snapshot()
        self.assertIn(note["id"], {node["id"] for node in snapshot["nodes"]})
        self.assertEqual(snapshot["edges"], [])

    def test_identity_store_loss_fails_closed(self):
        path = Path(self.temporary.name) / "backlog.json"
        path.unlink()
        with self.assertRaises(StoreCorruptError):
            self.stack.list_tasks()

    def test_invalid_due_date_is_rejected(self):
        with self.assertRaises(ValueError):
            self.stack.add_task("Invalid date", due="tomorrow")

    def test_quarter_math(self):
        self.assertEqual(current_quarter(dt.date(2030, 1, 1)), "2030-Q1")
        self.assertEqual(current_quarter(dt.date(2030, 12, 31)), "2030-Q4")

    def test_non_loopback_server_is_rejected(self):
        with self.assertRaises(ValueError):
            serve(self.stack, "0.0.0.0", 0)


if __name__ == "__main__":
    unittest.main()
