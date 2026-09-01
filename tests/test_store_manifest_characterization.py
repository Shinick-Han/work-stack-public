from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from workstack.service import WorkStack
from workstack.store import Store, StoreCorruptError


class StoreManifestCharacterizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        WorkStack(self.store)
        self.manifest = json.loads(
            self.store.store_manifest_path.read_text(encoding="utf-8")
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, manifest: object) -> None:
        self.store.store_manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )

    def assert_invalid(self, manifest: object, message: str) -> None:
        self.write(manifest)
        with self.assertRaisesRegex(StoreCorruptError, "^" + re.escape(message) + "$"):
            self.store._read_manifest_locked()

    def test_missing_manifest_returns_none_and_valid_manifest_round_trips(self) -> None:
        expected = copy.deepcopy(self.manifest)
        self.assertEqual(self.store._read_manifest_locked(), expected)
        self.store.store_manifest_path.unlink()
        self.assertIsNone(self.store._read_manifest_locked())

    def test_header_failures_are_stable(self) -> None:
        cases = (
            ("fields", lambda value: value.update(extra=True), "store manifest schema is invalid"),
            ("version", lambda value: value.update(version=999), "store manifest schema is invalid"),
            ("generation bool", lambda value: value.update(generation=True), "store manifest generation is invalid"),
            ("generation negative", lambda value: value.update(generation=-1), "store manifest generation is invalid"),
            ("schema version", lambda value: value.update(store_schema_version=999), "store manifest schema version is invalid"),
            ("workspace", lambda value: value.update(workspace_id="not-a-uuid"), "store_manifest.workspace_id must be a canonical UUID string"),
        )
        for label, mutate, message in cases:
            value = copy.deepcopy(self.manifest)
            mutate(value)
            with self.subTest(label=label):
                self.assert_invalid(value, message)

    def test_file_roster_and_digest_failures_are_stable(self) -> None:
        value = copy.deepcopy(self.manifest)
        value["files"].pop(next(iter(value["files"])))
        self.assert_invalid(value, "store manifest file roster is invalid")

        value = copy.deepcopy(self.manifest)
        value["files"][next(iter(value["files"]))] = "sha256:INVALID"
        self.assert_invalid(value, "store manifest file digest is invalid")

    def test_task_baseline_envelope_failures_are_stable(self) -> None:
        task = self.store.load("backlog.json").get("tasks", [])
        self.assertEqual(task, [])
        for value in ([], None, "tasks"):
            manifest = copy.deepcopy(self.manifest)
            manifest["tasks"] = value
            with self.subTest(value=value):
                self.assert_invalid(manifest, "store manifest task baseline is invalid")

    def test_each_task_baseline_field_is_validated(self) -> None:
        valid = {"revision": 0, "digest": "sha256:" + "0" * 64}
        cases = (
            ("id", "task", valid),
            ("record", "T-0001", []),
            ("fields", "T-0001", {**valid, "extra": True}),
            ("revision bool", "T-0001", {**valid, "revision": True}),
            ("revision negative", "T-0001", {**valid, "revision": -1}),
            ("digest", "T-0001", {**valid, "digest": "sha256:INVALID"}),
        )
        for label, task_id, task in cases:
            manifest = copy.deepcopy(self.manifest)
            manifest["tasks"] = {task_id: task}
            with self.subTest(label=label):
                self.assert_invalid(manifest, "store manifest task baseline is invalid")


if __name__ == "__main__":
    unittest.main()
