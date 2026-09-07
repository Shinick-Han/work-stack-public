from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from workstack.storage.migration import StorageMigrationError, execute_v3_migration
from workstack.store_rosters import V3_DOCUMENT_NAMES


# The checked-in nine-document schema-3 authority. The v3 migration only ever
# accepts that source, and the released Store this build ships writes the
# current schema instead, so building the workspace through the product here
# would make every case below refuse for the wrong reason.
FIXTURE = Path(__file__).parent / "fixtures" / "store-v3" / "populated"


class T5MigrationRefusalTest(unittest.TestCase):
    """The known-unsupported Task field must refuse before any Store side effect."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "workspace"
        shutil.copytree(FIXTURE, self.source)
        self.assertEqual(
            {path.name for path in self.source.iterdir()}, set(V3_DOCUMENT_NAMES)
        )
        self.assertEqual(
            json.loads(
                (self.source / "store-meta.json").read_text(encoding="utf-8")
            )["store_schema_version"],
            3,
        )
        backlog = json.loads(
            (self.source / "backlog.json").read_text(encoding="utf-8")
        )
        objective = json.loads(
            (self.source / "okr.json").read_text(encoding="utf-8")
        )["objectives"][0]
        self.objective_id = objective["id"]
        self.key_result_id = objective["key_results"][0]["id"]
        self.task_id = next(
            task["id"]
            for task in backlog["tasks"]
            if self.objective_id in task["objective_ids"]
        )

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
        """A historical v3 workspace lacking the field must reach the migration.

        This is the one case that runs past the read-only preflight and into
        the under-lease route, so it is also the case that depends on that
        route still admitting a historical v3 source. The assertion is left
        exactly as narrow as the contract it guards -- the preflight must not
        refuse -- and any other failure is reported rather than absorbed.
        """

        backlog = json.loads(
            (self.source / "backlog.json").read_text(encoding="utf-8")
        )
        for task in backlog["tasks"]:
            self.assertNotIn("key_result_refs", task)

        try:
            self._execute()
        except StorageMigrationError as error:  # pragma: no cover - control path
            self.assertNotEqual(error.code, "SEMANTIC_PARITY_MISMATCH")


if __name__ == "__main__":  # pragma: no cover - module is run through unittest
    unittest.main()
