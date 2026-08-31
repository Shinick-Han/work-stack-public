from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from workstack.service import DomainError, RevisionConflictError, WorkStack
from workstack.store import MAX_REVISION, Store, StoreCorruptError


WORKSPACE_UID = "0f50a123-3da8-4c82-8f16-8ee1a57260c4"


class StoreIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _write_legacy_store(self, tasks: list[dict] | None = None) -> None:
        values = {
            "workspace.json": {"version": 1, "id": WORKSPACE_UID, "name": "Legacy"},
            "backlog.json": {
                "version": 1,
                "tasks": tasks
                if tasks is not None
                else [{"id": "T-0001", "title": "Migrated task", "status": "open"}],
            },
            "okr.json": {"version": 1, "objectives": []},
            "worklog.json": {"version": 1, "days": {}},
            "notes.json": {"version": 1, "notes": []},
            "captures.json": {"version": 1, "captures": []},
            "replies.json": {"version": 1, "replies": []},
            "activity.json": {"version": 1, "activity": [], "idempotency": []},
        }
        for name, value in values.items():
            self._write(self.root / name, value)

    def test_fresh_identity_and_revision_survive_restart(self) -> None:
        first = WorkStack(Store(self.root))
        task = first.add_task("Stable identity")
        workspace_uid = first.workspace_projection()["workspace"]["id"]

        self.assertEqual(str(uuid.UUID(workspace_uid)), workspace_uid)
        self.assertEqual(str(uuid.UUID(task["uid"])), task["uid"])
        self.assertEqual(task["revision"], 0)
        before_read = self.root.joinpath("backlog.json").read_bytes()

        first.get_task(task["id"])
        first.workspace_projection()
        self.assertEqual(self.root.joinpath("backlog.json").read_bytes(), before_read)

        restarted = WorkStack(Store(self.root))
        restarted_task = restarted.get_task(task["id"])
        self.assertEqual(restarted.workspace_projection()["workspace"]["id"], workspace_uid)
        self.assertEqual(restarted_task["uid"], task["uid"])
        self.assertEqual(restarted_task["revision"], 0)

    def test_mutation_advances_once_and_stale_writer_fails_closed(self) -> None:
        first = WorkStack(Store(self.root))
        task = first.add_task("Concurrent edit")
        second = WorkStack(Store(self.root))

        changed = first.patch_task(task["id"], {"title": "First wins", "revision": 0})
        self.assertEqual(changed["revision"], 1)
        with self.assertRaises(RevisionConflictError):
            second.patch_task(task["id"], {"title": "Stale loses", "revision": 0})
        self.assertEqual(first.get_task(task["id"])["revision"], 1)

    def test_legacy_migration_is_deterministic_idempotent_and_recorded(self) -> None:
        self._write_legacy_store()
        first = Store(self.root)
        readiness = first.initialize()

        migrated = first.load("backlog.json")
        task = migrated["tasks"][0]
        expected_uid = str(uuid.uuid5(uuid.UUID(WORKSPACE_UID), "T-0001"))
        self.assertEqual(migrated["version"], 3)
        self.assertEqual(task["uid"], expected_uid)
        self.assertEqual(task["revision"], 0)
        self.assertRegex(task["status_fact_id"], r"^PS-[0-9]{6,}$")
        self.assertEqual(readiness.schema_version, 3)
        self.assertEqual(readiness.workspace_uid, WORKSPACE_UID)
        self.assertEqual(readiness.migration_origin, "migrated_v1")

        metadata = first.load("store-meta.json")
        self.assertEqual(metadata["store_schema_version"], 3)
        self.assertEqual(metadata["migrations"]["identity"]["id"], "workstack.store.v1-to-v2")
        self.assertRegex(
            metadata["migrations"]["identity"]["source_sha256"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(
            metadata["migrations"]["planning_status"]["id"],
            "workstack.planning-status.v1",
        )
        frozen = {
            name: self.root.joinpath(name).read_bytes()
            for name in ("workspace.json", "backlog.json", "store-meta.json")
        }

        second = Store(self.root)
        second_readiness = second.initialize()
        self.assertEqual(second_readiness, readiness)
        self.assertEqual(
            frozen,
            {name: self.root.joinpath(name).read_bytes() for name in frozen},
        )

    def test_partial_migration_fails_closed_without_rewriting_bytes(self) -> None:
        self._write_legacy_store()
        workspace_path = self.root / "workspace.json"
        workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
        workspace["version"] = 2
        self._write(workspace_path, workspace)
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}

        with self.assertRaises(StoreCorruptError):
            Store(self.root).initialize()
        self.assertEqual(
            before,
            {name: self.root.joinpath(name).read_bytes() for name in before},
        )

    def test_interrupted_migration_replays_the_complete_roster_on_restart(self) -> None:
        self._write_legacy_store()
        interrupted = Store(self.root)
        original_write = interrupted._atomic_write_locked
        failed = False

        def fail_after_workspace(path: Path, value: object) -> None:
            nonlocal failed
            original_write(path, value)
            if path.name == "workspace.json" and not failed:
                failed = True
                raise OSError("injected migration interruption")

        interrupted._atomic_write_locked = fail_after_workspace  # type: ignore[method-assign]
        with self.assertRaisesRegex(OSError, "injected migration interruption"):
            interrupted.initialize()
        self.assertTrue((self.root / ".workstack-journal.json").exists())

        recovered = Store(self.root)
        readiness = recovered.initialize()
        self.assertEqual(readiness.schema_version, 3)
        self.assertEqual(readiness.migration_origin, "migrated_v1")
        self.assertFalse((self.root / ".workstack-journal.json").exists())
        self.assertEqual(
            set(path.name for path in self.root.iterdir() if path.name.endswith(".json")),
            set({
                "workspace.json",
                "backlog.json",
                "store-meta.json",
                "okr.json",
                "worklog.json",
                "notes.json",
                "captures.json",
                "replies.json",
                "activity.json",
            }),
        )

    def test_corrupt_existing_identity_is_never_replaced(self) -> None:
        cases = {
            "malformed": [
                {"id": "T-0001", "uid": "not-a-uuid", "revision": 0}
            ],
            "reserved_variant": [
                {"id": "T-0001", "uid": "ffffffff-ffff-ffff-ffff-ffffffffffff", "revision": 0}
            ],
            "duplicate": [
                {
                    "id": "T-0001",
                    "uid": "2e82845c-bccb-5aa6-9b6d-8ec65170c00a",
                    "revision": 0,
                },
                {
                    "id": "T-0002",
                    "uid": "2e82845c-bccb-5aa6-9b6d-8ec65170c00a",
                    "revision": 0,
                },
            ],
        }
        for label, tasks in cases.items():
            with self.subTest(label=label):
                child = self.root / label
                child.mkdir()
                original_root = self.root
                self.root = child
                try:
                    self._write_legacy_store(tasks)
                    before = (child / "backlog.json").read_bytes()
                    with self.assertRaises(StoreCorruptError):
                        Store(child).initialize()
                    self.assertEqual((child / "backlog.json").read_bytes(), before)
                finally:
                    self.root = original_root

    def test_non_rfc_workspace_uuid_variant_fails_closed(self) -> None:
        self._write_legacy_store()
        workspace_path = self.root / "workspace.json"
        workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
        workspace["id"] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        self._write(workspace_path, workspace)
        before = {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}

        with self.assertRaises(StoreCorruptError):
            Store(self.root).initialize()

        self.assertEqual(
            before,
            {name: self.root.joinpath(name).read_bytes() for name in before},
        )

    def test_ready_store_rejects_missing_revision_and_future_schema(self) -> None:
        stack = WorkStack(Store(self.root))
        task = stack.add_task("Persisted")
        backlog_path = self.root / "backlog.json"
        backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
        del backlog["tasks"][0]["revision"]
        self._write(backlog_path, backlog)
        with self.assertRaises(StoreCorruptError):
            Store(self.root).initialize()

        backlog["tasks"][0]["revision"] = task["revision"]
        self._write(backlog_path, backlog)
        metadata_path = self.root / "store-meta.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["store_schema_version"] = 999
        self._write(metadata_path, metadata)
        with self.assertRaises(StoreCorruptError):
            Store(self.root).initialize()

    def test_ready_store_refuses_missing_or_corrupt_auxiliary_store_without_repair(self) -> None:
        WorkStack(Store(self.root)).add_task("Persisted")

        activity_path = self.root / "activity.json"
        activity_path.unlink()
        before_missing = {
            path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()
        }
        with self.assertRaises(StoreCorruptError):
            Store(self.root).initialize()
        self.assertFalse(activity_path.exists())
        self.assertEqual(
            before_missing,
            {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()},
        )

        activity = {"version": 1, "activity": [], "idempotency": []}
        self._write(activity_path, activity)
        notes_path = self.root / "notes.json"
        notes_path.write_bytes(b"{broken")
        before_corrupt = {
            path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()
        }
        with self.assertRaises(StoreCorruptError):
            Store(self.root).initialize()
        self.assertEqual(
            before_corrupt,
            {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()},
        )

    def test_revision_overflow_is_refused_before_every_task_mutation(self) -> None:
        for case in (
            "add_subtask",
            "set_subtask_status",
            "set_task_status",
            "add_task_note",
            "link_task",
            "patch_task",
        ):
            with self.subTest(case=case):
                child = self.root / case
                child.mkdir()
                stack = WorkStack(Store(child))
                task = stack.add_task("Revision ceiling")
                subtask = stack.add_subtask(task["id"], "Existing") if case == "set_subtask_status" else None
                objective = stack.add_objective("Existing") if case == "link_task" else None
                backlog_path = child / "backlog.json"
                backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
                backlog["tasks"][0]["revision"] = MAX_REVISION
                self._write(backlog_path, backlog)
                before = {
                    path.name: path.read_bytes() for path in child.iterdir() if path.is_file()
                }
                actions = {
                    "add_subtask": lambda: stack.add_subtask(task["id"], "Must not persist"),
                    "set_subtask_status": lambda: stack.set_subtask_status(task["id"], subtask["id"], "done"),
                    "set_task_status": lambda: stack.set_task_status(task["id"], "done"),
                    "add_task_note": lambda: stack.add_task_note(task["id"], "Must not persist"),
                    "link_task": lambda: stack.link_task(objective["id"], task["id"]),
                    "patch_task": lambda: stack.patch_task(
                        task["id"], {"title": "Must not persist", "revision": MAX_REVISION}
                    ),
                }

                with self.assertRaises(DomainError) as raised:
                    actions[case]()

                self.assertEqual(raised.exception.code, "revision_exhausted")
                self.assertEqual(
                    before,
                    {path.name: path.read_bytes() for path in child.iterdir() if path.is_file()},
                )

if __name__ == "__main__":
    unittest.main()
