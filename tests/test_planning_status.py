from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from workstack.service import RevisionConflictError, WorkStack
from workstack.store import Store, StoreCorruptError


FACT_TYPE = "task.planning_status"
STATUSES = ("open", "started", "done", "dropped")
CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
DEMO_DATA = Path(__file__).resolve().parents[1] / "data"


class PlanningStatusFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def stack(self) -> WorkStack:
        return WorkStack(Store(self.root))

    @staticmethod
    def facts(stack: WorkStack) -> list[dict]:
        return stack.store.load("activity.json")["planning_status"]

    def write_v2_store(self, *, status: str = "started", revision: int = 4) -> None:
        workspace_uid = str(uuid.uuid4())
        task_id = "T-0001"
        task = {
            "id": task_id,
            "uid": str(uuid.uuid5(uuid.UUID(workspace_uid), task_id)),
            "title": "Existing planning task",
            "detail": "Preserve current planning evidence.",
            "status": status,
            "priority": "P1",
            "due": None,
            "tags": [],
            "objective_ids": [],
            "parent_id": None,
            "dependencies": [],
            "subtasks": [],
            "notes": [],
            "created": "2026-08-01",
            "updated_at": "2026-08-28",
            "revision": revision,
        }
        values = {
            "workspace.json": {"version": 2, "id": workspace_uid, "name": "Work Stack"},
            "backlog.json": {"version": 2, "tasks": [task]},
            "store-meta.json": {
                "version": 1,
                "store_schema_version": 2,
                "migration": {
                    "id": "workstack.store.v2",
                    "origin": "fresh",
                    "source_sha256": None,
                },
            },
            "okr.json": {"version": 1, "objectives": []},
            "worklog.json": {"version": 1, "days": {}},
            "notes.json": {"version": 1, "notes": []},
            "captures.json": {"version": 1, "captures": []},
            "replies.json": {"version": 1, "replies": []},
            "activity.json": {"version": 1, "activity": [], "idempotency": []},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        for name, value in values.items():
            self.root.joinpath(name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    def test_v2_status_migrates_once_with_head_and_source_evidence(self) -> None:
        self.write_v2_store()
        stack = self.stack()
        facts = self.facts(stack)
        raw_task = stack.store.load("backlog.json")["tasks"][0]

        self.assertEqual(stack.store.readiness.schema_version, 3)
        self.assertEqual(stack.store.load("backlog.json")["version"], 3)
        self.assertEqual(stack.store.load("activity.json")["version"], 2)
        self.assertEqual(len(facts), 1)
        self.assertEqual(raw_task["status"], "started")
        self.assertEqual(raw_task["status_fact_id"], facts[0]["id"])
        self.assertEqual(
            {
                key: facts[0][key]
                for key in (
                    "type",
                    "task_id",
                    "task_uid",
                    "previous_fact_id",
                    "prior_revision",
                    "new_revision",
                    "prior_status",
                    "status",
                    "actor",
                    "provenance",
                )
            },
            {
                "type": FACT_TYPE,
                "task_id": "T-0001",
                "task_uid": raw_task["uid"],
                "previous_fact_id": None,
                "prior_revision": None,
                "new_revision": 4,
                "prior_status": None,
                "status": "started",
                "actor": "workstack.migration",
                "provenance": "store.v2",
            },
        )
        metadata = stack.store.load("store-meta.json")
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["store_schema_version"], 3)
        self.assertEqual(metadata["migrations"]["identity"]["origin"], "fresh")
        self.assertEqual(metadata["migrations"]["planning_status"]["origin"], "migrated_v2")
        migrated_bytes = {
            name: stack.store.path(name).read_bytes()
            for name in ("backlog.json", "activity.json", "store-meta.json")
        }

        restarted = self.stack()
        self.assertEqual(
            migrated_bytes,
            {name: restarted.store.path(name).read_bytes() for name in migrated_bytes},
        )

    def test_all_distinct_edges_work_and_same_status_is_stale_checked_noop(self) -> None:
        for source in STATUSES:
            for target in STATUSES:
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    temporary = tempfile.TemporaryDirectory()
                    try:
                        stack = WorkStack(Store(Path(temporary.name)))
                        task = stack.add_task("{} to {}".format(source, target))
                        if source != "open":
                            task = stack.patch_task(
                                task["id"], {"status": source, "revision": task["revision"]}
                            )
                        changed = stack.patch_task(
                            task["id"], {"status": target, "revision": task["revision"]}
                        )
                        self.assertEqual(changed["status"], target)
                    finally:
                        temporary.cleanup()

        stack = self.stack()
        task = stack.add_task("No-op")
        before = {
            name: stack.store.path(name).read_bytes()
            for name in ("backlog.json", "activity.json")
        }
        unchanged = stack.patch_task(task["id"], {"status": "open", "revision": 0})
        self.assertEqual(unchanged["revision"], 0)
        self.assertEqual(
            before,
            {name: stack.store.path(name).read_bytes() for name in before},
        )
        with self.assertRaises(RevisionConflictError):
            stack.patch_task(task["id"], {"status": "open", "revision": 99})

    def test_chain_spans_non_status_revisions_and_keeps_raw_baseline(self) -> None:
        stack = self.stack()
        task = stack.add_task("Fact chain")
        started = stack.patch_task(task["id"], {"status": "started", "revision": 0})
        renamed = stack.patch_task(task["id"], {"title": "Renamed", "revision": 1})
        done = stack.patch_task(task["id"], {"status": "done", "revision": 2})

        facts = self.facts(stack)
        self.assertEqual(len(facts), 3)
        self.assertEqual(
            (facts[1]["previous_fact_id"], facts[1]["prior_revision"], facts[1]["new_revision"], facts[1]["prior_status"], facts[1]["status"]),
            (facts[0]["id"], 0, 1, "open", "started"),
        )
        self.assertEqual(
            (facts[2]["previous_fact_id"], facts[2]["prior_revision"], facts[2]["new_revision"], facts[2]["prior_status"], facts[2]["status"]),
            (facts[1]["id"], 2, 3, "started", "done"),
        )
        self.assertEqual(started["status"], "started")
        self.assertEqual(renamed["status"], "started")
        self.assertEqual(done["status"], "done")
        raw = stack.store.load("backlog.json")["tasks"][0]
        self.assertEqual(raw["status"], "open")
        self.assertEqual(raw["status_fact_id"], facts[-1]["id"])

    def test_tail_truncation_and_reordering_fail_closed_without_repair(self) -> None:
        stack = self.stack()
        task = stack.add_task("Corrupt tail")
        stack.patch_task(task["id"], {"status": "started", "revision": 0})
        stack.patch_task(task["id"], {"status": "done", "revision": 1})
        valid_activity = stack.store.load("activity.json")

        for label, corrupt in (
            ("truncated", {**valid_activity, "planning_status": valid_activity["planning_status"][:-1]}),
            (
                "reordered",
                {
                    **valid_activity,
                    "planning_status": [
                        valid_activity["planning_status"][0],
                        valid_activity["planning_status"][2],
                        valid_activity["planning_status"][1],
                    ],
                },
            ),
        ):
            with self.subTest(label=label):
                stack.store.path("activity.json").write_text(
                    json.dumps(corrupt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                before = stack.store.path("activity.json").read_bytes()
                with self.assertRaises(StoreCorruptError):
                    WorkStack(Store(self.root))
                self.assertEqual(stack.store.path("activity.json").read_bytes(), before)
                stack.store.path("activity.json").write_text(
                    json.dumps(valid_activity, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

    def test_interrupted_v2_migration_replays_once_on_restart(self) -> None:
        self.write_v2_store(status="open", revision=2)
        store = Store(self.root)
        original = store._atomic_write_locked
        injected = False

        def interrupt_after_backlog(path: Path, value: object) -> None:
            nonlocal injected
            original(path, value)
            if path.name == "backlog.json" and not injected:
                injected = True
                raise OSError("injected planning migration interruption")

        store._atomic_write_locked = interrupt_after_backlog  # type: ignore[method-assign]
        with self.assertRaisesRegex(OSError, "planning migration interruption"):
            store.initialize()
        self.assertTrue(store.journal_path.exists())

        restarted = self.stack()
        self.assertFalse(restarted.store.journal_path.exists())
        self.assertEqual(len(self.facts(restarted)), 1)
        self.assertEqual(restarted.get_task("T-0001")["status"], "open")

    def test_new_task_writers_append_bootstrap_facts_atomically(self) -> None:
        stack = self.stack()
        cli_task = stack.add_task("CLI task")
        api_result = stack.create_task_v1({"title": "Browser task"}, "status.create.0001")
        api_task = api_result["body"]["data"]

        by_task = {fact["task_id"]: fact for fact in self.facts(stack)}
        self.assertEqual(by_task[cli_task["id"]]["actor"], "local.user")
        self.assertEqual(by_task[cli_task["id"]]["provenance"], "cli")
        self.assertEqual(by_task[api_task["id"]]["actor"], "local.user")
        self.assertEqual(by_task[api_task["id"]]["provenance"], "api.v1")
        self.assertEqual({fact["status"] for fact in by_task.values()}, {"open"})
        self.assertTrue(all(fact["previous_fact_id"] is None for fact in by_task.values()))
        self.assertTrue(all(fact["prior_revision"] is None for fact in by_task.values()))
        self.assertTrue(all(fact["new_revision"] == 0 for fact in by_task.values()))

        replay = stack.create_task_v1({"title": "Browser task"}, "status.create.0001")
        self.assertEqual(replay["status"], 200)
        self.assertEqual(len(self.facts(stack)), 2)

    def test_capture_task_writers_append_one_bootstrap_fact_each(self) -> None:
        stack = self.stack()
        packet = json.loads(
            CONTRACTS.joinpath("capture-packet-v1.fixture.json").read_text(
                encoding="utf-8"
            )
        )
        capture = stack.ingest_capture(packet, "status.capture.ingest")["body"]["data"]
        generic = stack.create_task_from_capture(
            capture["id"], {"title": "Generic capture task"}, "status.capture.generic"
        )
        action_id = capture["normalized"]["action_items"][0]["id"]
        action = stack.convert_capture_action(
            capture["id"], action_id, [], "status.capture.action"
        )

        facts = self.facts(stack)
        self.assertEqual(len(facts), 2)
        self.assertEqual(
            {fact["task_id"] for fact in facts},
            {generic["body"]["data"]["id"], action["body"]["data"]["id"]},
        )
        self.assertTrue(
            all(
                fact["actor"] == "workstack.capture"
                and fact["provenance"] == "api.v1.capture"
                for fact in facts
            )
        )

        stack.create_task_from_capture(
            capture["id"], {"title": "Generic capture task"}, "status.capture.generic"
        )
        stack.convert_capture_action(
            capture["id"], action_id, [], "status.capture.action.retry"
        )
        self.assertEqual(len(self.facts(stack)), 2)

    def test_demo_seed_bootstraps_every_task_and_survives_restart(self) -> None:
        stack = self.stack()
        self.assertTrue(stack.store.seed_demo(DEMO_DATA))
        facts = self.facts(stack)
        self.assertEqual(len(facts), 30)
        self.assertTrue(
            all(
                fact["actor"] == "workstack.seed"
                and fact["provenance"] == "demo.fixture"
                for fact in facts
            )
        )
        frozen = {
            name: stack.store.path(name).read_bytes()
            for name in ("backlog.json", "activity.json")
        }
        restarted = self.stack()
        self.assertEqual(len(restarted.list_tasks(status="all")), 30)
        self.assertEqual(
            frozen,
            {name: restarted.store.path(name).read_bytes() for name in frozen},
        )


if __name__ == "__main__":
    unittest.main()
