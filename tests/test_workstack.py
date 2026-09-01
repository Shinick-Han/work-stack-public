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

    def test_weekly_report_merges_entries_in_date_order_without_duplicates(self):
        objective = self.stack.add_objective("Ship the quality gate", "2030-Q2")
        task = self.stack.add_task(
            "Harden release checks", objective_ids=[objective["id"]]
        )
        worklog = self.stack.store.load("worklog.json")
        worklog["days"] = {
            "2030-04-03": {
                "entries": [{
                    "task_id": task["id"],
                    "task": task["title"],
                    "done": ["Validated", "Documented"],
                    "next": ["Release"],
                    "blockers": [],
                    "duration_seconds": 120,
                }]
            },
            "2030-04-02": {
                "entries": [{
                    "task_id": task["id"],
                    "task": task["title"],
                    "done": ["Validated"],
                    "next": ["Document"],
                    "blockers": ["Approval"],
                    "duration_seconds": 60,
                }]
            },
        }
        self.stack.store.save("worklog.json", worklog)

        report = self.stack.weekly_report(end="2030-04-03", days=2)

        self.assertEqual(
            report,
            {
                "range": {"start": "2030-04-02", "end": "2030-04-03", "days": 2},
                "objectives": [
                    {"id": objective["id"], "objective": "Ship the quality gate"}
                ],
                "projects": [{
                    "task_id": task["id"],
                    "task": task["title"],
                    "objective_ids": [objective["id"]],
                    "done": ["Validated", "Documented"],
                    "next": ["Document", "Release"],
                    "blockers": ["Approval"],
                    "dates": ["2030-04-02", "2030-04-03"],
                    "duration_seconds": 180,
                }],
            },
        )

    def test_snapshot_projects_all_node_and_edge_kinds_in_stable_order(self):
        objective = self.stack.add_objective("Map execution", "2030-Q2")
        parent = self.stack.add_task(
            "Parent task", objective_ids=[objective["id"]]
        )
        child = self.stack.add_task(
            "Child task", parent_id=parent["id"], dependencies=[parent["id"]]
        )
        subtask = self.stack.add_subtask(child["id"], "Child checklist")
        self.stack.add_worklog(
            child["id"], done=["Mapped"], date="2030-04-02"
        )
        note = self.stack.add_note(
            "Execution note", links=[child["id"], "T-9999"]
        )

        snapshot = self.stack.snapshot()

        self.assertEqual(
            [node["kind"] for node in snapshot["nodes"]],
            ["objective", "task", "task", "subtask", "day", "note"],
        )
        self.assertEqual(
            snapshot["edges"],
            [
                {"source": parent["id"], "target": objective["id"], "kind": "objective"},
                {"source": child["id"], "target": parent["id"], "kind": "parent"},
                {"source": child["id"], "target": parent["id"], "kind": "dependency"},
                {
                    "source": "{}-{}".format(child["id"], subtask["id"]),
                    "target": child["id"],
                    "kind": "parent",
                },
                {"source": "D-2030-04-02", "target": child["id"], "kind": "worklog"},
                {"source": note["id"], "target": child["id"], "kind": "note"},
            ],
        )
        self.assertEqual(
            snapshot["summary"],
            {"objectives": 1, "tasks": 2, "days": 1, "notes": 1},
        )

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
