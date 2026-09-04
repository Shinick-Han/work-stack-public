from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workstack.service import WorkStack
from workstack.storage.migration import StorageMigrationError, execute_v3_migration
from workstack.store import Store


class T5MigrationRefusalTest(unittest.TestCase):
    """The known-unsupported Task field must refuse before any Store side effect."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "workspace"
        self.source.mkdir()
        stack = WorkStack(Store(self.source))
        objective = stack.add_objective("Objective")
        key_result = stack.add_key_result(objective["id"], "Outcome")
        task = stack.add_task("Task")
        stack.patch_task(
            task["id"],
            {"objective_ids": [objective["id"]], "revision": task["revision"]},
        )
        self.objective_id = objective["id"]
        self.key_result_id = key_result["id"]
        self.task_id = task["id"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _tree(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def _write_refs(self, refs: list[dict[str, str]]) -> None:
        backlog_path = self.source / "backlog.json"
        backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
        for task in backlog["tasks"]:
            if task["id"] == self.task_id:
                task["key_result_refs"] = refs
        backlog_path.write_text(json.dumps(backlog, indent=2), encoding="utf-8")

    def _execute(self) -> None:
        execute_v3_migration(self.source, candidate_created_at="2026-09-02T00:00:00Z")

    def test_nonempty_refs_refuse_the_whole_workspace_without_side_effects(self) -> None:
        self._write_refs(
            [
                {
                    "objective_id": self.objective_id,
                    "key_result_id": self.key_result_id,
                }
            ]
        )
        before = self._tree()

        with self.assertRaises(StorageMigrationError) as raised:
            self._execute()

        self.assertEqual(raised.exception.code, "SEMANTIC_PARITY_MISMATCH")
        self.assertEqual(self._tree(), before)

    def test_explicit_empty_list_also_refuses_and_writes_nothing(self) -> None:
        self._write_refs([])
        before = self._tree()

        with self.assertRaises(StorageMigrationError) as raised:
            self._execute()

        self.assertEqual(raised.exception.code, "SEMANTIC_PARITY_MISMATCH")
        self.assertEqual(self._tree(), before)

    def test_refusal_creates_no_candidate_backup_runtime_or_lease_artifact(self) -> None:
        self._write_refs([])
        before = set(self._tree())
        siblings_before = {path.name for path in self.root.iterdir()}

        with self.assertRaises(StorageMigrationError):
            self._execute()

        self.assertEqual(set(self._tree()), before)
        self.assertEqual({path.name for path in self.root.iterdir()}, siblings_before)

    def test_legacy_workspace_without_the_field_is_not_refused_by_the_preflight(self) -> None:
        backlog = json.loads(
            (self.source / "backlog.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("key_result_refs", backlog["tasks"][0])

        try:
            self._execute()
        except StorageMigrationError as error:  # pragma: no cover - control path
            self.assertNotEqual(error.code, "SEMANTIC_PARITY_MISMATCH")


if __name__ == "__main__":  # pragma: no cover - module is run through unittest
    unittest.main()
