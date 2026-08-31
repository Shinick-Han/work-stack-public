from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from workstack.maintenance import (
    BackupValidationError,
    backup_store,
    create_backup_download,
    relocate_store,
    restore_store,
    verify_backup,
)
from workstack.service import WorkStack
from workstack.store import Store, StoreLockedError


class MaintenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.backups = self.root / "backups"
        self.stack = WorkStack(Store(self.source))
        self.task = self.stack.add_task("Back up this planning fact", priority="P1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_backup_verify_and_restore_round_trip(self) -> None:
        artifact = backup_store(self.source, self.backups)

        self.assertTrue(artifact.path.is_file())
        verified = verify_backup(artifact.path)
        self.assertEqual(verified.workspace_id, artifact.workspace_id)
        self.assertEqual(verified.digest, artifact.digest)
        self.assertEqual(verified.file_count, 9)

        destination = self.root / "restored"
        receipt = restore_store(artifact.path, destination)
        restored = WorkStack(Store(destination))
        self.assertEqual(restored.get_task(self.task["id"])["title"], self.task["title"])
        self.assertEqual(receipt.workspace_id, artifact.workspace_id)
        self.assertIsNone(receipt.safety_backup)

    def test_restore_existing_store_creates_safety_backup(self) -> None:
        artifact = backup_store(self.source, self.backups)
        destination = self.root / "existing"
        existing = WorkStack(Store(destination))
        existing.add_task("Existing fact")

        receipt = restore_store(
            artifact.path,
            destination,
            replace=True,
            safety_backup_dir=self.backups / "pre-restore",
        )

        self.assertIsNotNone(receipt.safety_backup)
        self.assertTrue(receipt.safety_backup.is_file())
        restored = WorkStack(Store(destination))
        self.assertEqual([item["title"] for item in restored.list_tasks(status="all")], [self.task["title"]])

    def test_tampered_backup_is_rejected_without_touching_destination(self) -> None:
        artifact = backup_store(self.source, self.backups)
        tampered = self.root / "tampered.zip"
        with zipfile.ZipFile(artifact.path, "r") as source, zipfile.ZipFile(tampered, "w") as target:
            for item in source.infolist():
                body = source.read(item.filename)
                if item.filename == "backlog.json":
                    body = body.replace(b"Back up this planning fact", b"Changed planning evidence")
                target.writestr(item, body)

        destination = self.root / "untouched"
        with self.assertRaises(BackupValidationError):
            restore_store(tampered, destination)
        self.assertFalse(destination.exists())

    def test_backup_refuses_a_store_owned_by_the_server(self) -> None:
        store = Store(self.source)
        with store.server_lease():
            with self.assertRaises(StoreLockedError):
                backup_store(self.source, self.backups)

    def test_running_server_owner_can_create_a_read_only_verified_download(self) -> None:
        before = {
            path.name: path.read_bytes()
            for path in self.source.glob("*.json")
        }
        with self.stack.store.server_lease():
            download = create_backup_download(self.stack.store)

        archive = self.root / download.filename
        archive.write_bytes(download.body)
        verified = verify_backup(archive)
        self.assertEqual(verified.workspace_id, download.workspace_id)
        self.assertEqual(verified.digest, download.digest)
        self.assertEqual(verified.file_count, download.file_count)
        self.assertEqual(
            before,
            {path.name: path.read_bytes() for path in self.source.glob("*.json")},
        )

    def test_relocation_copies_and_verifies_without_deleting_source(self) -> None:
        destination = self.root / "relocated"
        receipt = relocate_store(self.source, destination)

        self.assertTrue((self.source / "backlog.json").is_file())
        self.assertEqual(receipt.workspace_id, self.stack.store.load("workspace.json")["id"])
        relocated = json.loads((destination / "backlog.json").read_text(encoding="utf-8"))
        self.assertEqual(relocated["tasks"][0]["title"], self.task["title"])


if __name__ == "__main__":
    unittest.main()
