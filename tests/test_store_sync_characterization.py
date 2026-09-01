from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workstack.service import WorkStack
from workstack.store import Store


class StoreSyncCharacterizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def inspect(self) -> dict[str, object]:
        with self.store._process_lock:
            return self.store._inspect_sync_locked()

    def changed_hashes(self, manifest: dict[str, object]) -> dict[str, str]:
        hashes = copy.deepcopy(manifest["files"])
        hashes["notes.json"] = "sha256:" + "1" * 64
        return hashes

    def test_repeated_valid_candidate_has_stable_digest_and_one_detection_event(self) -> None:
        notes_path = self.root / "notes.json"
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
        notes["notes"].append({"id": "N-external", "title": "Agent", "body": "Changed"})
        notes_path.write_text(json.dumps(notes), encoding="utf-8")

        first = self.inspect()
        second = self.inspect()
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "external-change-detected")
        self.assertEqual(first["changed_files"], ["notes.json"])
        self.assertRegex(str(first["candidate_digest"]), r"^sha256:[0-9a-f]{64}$")
        events = [
            event
            for event in self.store.sync_events()["events"]
            if event["type"] == "store.external-change-detected"
        ]
        self.assertEqual(len(events), 1)

    def test_missing_manifest_is_recreated_as_an_in_sync_baseline(self) -> None:
        generation = self.store.generation
        self.store.store_manifest_path.unlink()
        status = self.inspect()
        self.assertEqual(status["status"], "in-sync")
        self.assertEqual(status["generation"], generation)
        self.assertEqual(status["changed_files"], [])
        self.assertIsNone(status["candidate_digest"])
        self.assertTrue(self.store.store_manifest_path.is_file())

    def test_new_task_revision_and_removed_task_errors_are_exact(self) -> None:
        manifest = self.store._read_manifest_locked()
        self.assertIsNotNone(manifest)
        hashes = self.changed_hashes(manifest)
        new_task = {
            "T-0001": {"revision": 1, "digest": "sha256:" + "2" * 64}
        }
        with (
            patch.object(self.store, "_authoritative_hashes_locked", return_value=hashes),
            patch.object(self.store, "_task_semantics_locked", return_value=new_task),
        ):
            status = self.inspect()
        self.assertEqual(status["status"], "external-change-invalid")
        self.assertEqual(status["validation_error"], "external candidate new Task revision is invalid")

        task = self.stack.add_task("Tracked task")
        manifest = self.store._read_manifest_locked()
        self.assertIn(task["id"], manifest["tasks"])
        hashes = self.changed_hashes(manifest)
        with (
            patch.object(self.store, "_authoritative_hashes_locked", return_value=hashes),
            patch.object(self.store, "_task_semantics_locked", return_value={}),
        ):
            status = self.inspect()
        self.assertEqual(status["status"], "external-change-invalid")
        self.assertEqual(status["validation_error"], "external candidate removes existing Tasks")

    def test_changed_task_without_revision_advance_error_is_exact(self) -> None:
        task = self.stack.add_task("Tracked task")
        manifest = self.store._read_manifest_locked()
        hashes = self.changed_hashes(manifest)
        baseline = manifest["tasks"][task["id"]]
        candidate = {
            task["id"]: {
                "revision": baseline["revision"],
                "digest": "sha256:" + "3" * 64,
            }
        }
        with (
            patch.object(self.store, "_authoritative_hashes_locked", return_value=hashes),
            patch.object(self.store, "_task_semantics_locked", return_value=candidate),
        ):
            status = self.inspect()
        self.assertEqual(status["status"], "external-change-invalid")
        self.assertEqual(
            status["validation_error"],
            "external candidate Task revision did not advance",
        )

    def test_candidate_changing_during_validation_fails_closed(self) -> None:
        manifest = self.store._read_manifest_locked()
        first = self.changed_hashes(manifest)
        second = copy.deepcopy(first)
        second["notes.json"] = "sha256:" + hashlib.sha256(b"changed again").hexdigest()
        with patch.object(
            self.store,
            "_authoritative_hashes_locked",
            side_effect=[first, second],
        ):
            status = self.inspect()
        self.assertEqual(status["status"], "external-change-invalid")
        self.assertEqual(
            status["validation_error"],
            "external candidate changed during validation",
        )


if __name__ == "__main__":
    unittest.main()
