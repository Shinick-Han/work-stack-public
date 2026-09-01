from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from workstack.service import WorkStack
from workstack.store import Store, StoreCorruptError


class StoreReadinessCharacterizationTest(unittest.TestCase):
    """Freeze ready-store evidence refusal before validator decomposition."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.valid = self.root / "valid"
        stack = WorkStack(Store(self.valid))
        stack.add_task("Readiness contract")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def write(path: Path, value: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def corrupt(
        self,
        label: str,
        filename: str,
        mutate: Callable[[dict[str, Any]], None],
    ) -> Path:
        target = self.root / label
        shutil.copytree(self.valid, target)
        path = target / filename
        value = json.loads(path.read_text(encoding="utf-8"))
        mutate(value)
        self.write(path, value)
        return target

    def assert_invalid(self, root: Path, message: str) -> None:
        before = {
            path.name: path.read_bytes()
            for path in root.iterdir()
            if path.is_file()
        }
        with self.assertRaisesRegex(StoreCorruptError, message):
            Store(root).initialize()
        self.assertEqual(
            before,
            {
                name: root.joinpath(name).read_bytes()
                for name in before
            },
        )

    def test_metadata_envelope_refusal_table(self) -> None:
        cases = (
            ("fields", lambda value: value.update(extra=True), "store metadata has unknown or missing fields"),
            ("version", lambda value: value.update(version=1), "store metadata version is unsupported"),
            ("future", lambda value: value.update(store_schema_version=99), "store schema is newer than this Work Stack build"),
            ("schema", lambda value: value.update(store_schema_version="3"), "store schema version is invalid"),
            ("migrations", lambda value: value.update(migrations=[]), "store migration evidence is invalid"),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label):
                self.assert_invalid(
                    self.corrupt(label, "store-meta.json", mutate), message
                )

    def test_identity_evidence_refusal_table(self) -> None:
        def identity(value: dict[str, Any]) -> dict[str, Any]:
            return value["migrations"]["identity"]

        cases = (
            ("identity-fields", lambda value: identity(value).update(extra=True), "store migration evidence is invalid"),
            ("fresh-id", lambda value: identity(value).update(id="wrong"), "fresh store migration evidence is invalid"),
            ("fresh-digest", lambda value: identity(value).update(source_sha256="sha256:" + "0" * 64), "fresh store migration evidence is invalid"),
            ("migrated-digest", lambda value: identity(value).update(origin="migrated_v1", id="workstack.store.v1-to-v2", source_sha256="bad"), "v1 migration evidence is invalid"),
            ("identity-origin", lambda value: identity(value).update(origin="unknown"), "store migration origin is invalid"),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label):
                self.assert_invalid(
                    self.corrupt(label, "store-meta.json", mutate), message
                )

    def test_planning_evidence_refusal_table(self) -> None:
        def planning(value: dict[str, Any]) -> dict[str, Any]:
            return value["migrations"]["planning_status"]

        cases = (
            ("planning-fields", lambda value: planning(value).update(extra=True), "store migration evidence is invalid"),
            ("planning-id", lambda value: planning(value).update(id="wrong"), "planning-status migration evidence is invalid"),
            ("planning-fresh-digest", lambda value: planning(value).update(source_sha256="sha256:" + "0" * 64), "fresh planning-status evidence is invalid"),
            ("planning-migrated-digest", lambda value: planning(value).update(origin="migrated_v2", source_sha256="bad"), "planning-status migration evidence is invalid"),
            ("planning-origin", lambda value: planning(value).update(origin="unknown"), "planning-status migration origin is invalid"),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label):
                self.assert_invalid(
                    self.corrupt(label, "store-meta.json", mutate), message
                )

    def test_auxiliary_activity_and_projection_refusal_table(self) -> None:
        cases = (
            (
                "auxiliary",
                "notes.json",
                lambda value: value.update(version=99),
                "notes.json schema is invalid",
            ),
            (
                "activity-fields",
                "activity.json",
                lambda value: value.update(extra=True),
                "activity.json schema is invalid",
            ),
            (
                "activity-type",
                "activity.json",
                lambda value: value.update(activity={}),
                "activity.json schema is invalid",
            ),
            (
                "planning-chain",
                "activity.json",
                lambda value: value["planning_status"].clear(),
                "planning status head is missing or stale",
            ),
        )
        for label, filename, mutate, message in cases:
            with self.subTest(label=label):
                self.assert_invalid(
                    self.corrupt(label, filename, mutate), message
                )

    def test_success_readiness_shape_and_restart_are_stable(self) -> None:
        first = Store(self.valid).initialize()
        second = Store(self.valid).initialize()
        self.assertEqual(first, second)
        self.assertEqual(first.schema_version, 3)
        self.assertEqual(first.task_count, 1)
        self.assertEqual(first.migration_origin, "fresh")


if __name__ == "__main__":
    unittest.main()
