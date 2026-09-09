"""Exercise the exact new-runtime CLI used by the Windows installer's safety backup."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from workstack.maintenance import backup_store
from workstack.store import Store
from workstack.store_report_migration import verify_archive_file

ROOT = Path(__file__).resolve().parents[1]


class PreUpgradeBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "historical"
        shutil.copytree(ROOT / "tests/fixtures/store-v3/populated", self.source)
        self.out = self.base / "backups"
        self.before = self.documents()

    def documents(self):
        return {p.name: p.read_bytes() for p in self.source.glob("*.json")}

    def cli(self, action, *extra):
        return subprocess.run(
            [sys.executable, "-B", str(ROOT / "run_work_stack.py"),
             "--data-dir", str(self.source), "maintenance", action, *map(str, extra)],
            cwd=ROOT, capture_output=True, timeout=30,
        )

    def test_staged_cli_backs_up_real_v3_and_verifies_without_migration(self):
        result = self.cli("backup", "--out", self.out)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        response = json.loads(result.stdout)
        archive = verify_archive_file(response["path"])
        self.assertEqual(archive.store_schema_version, 3)
        self.assertEqual(archive.file_count, 9)
        self.assertEqual(archive.bodies, self.before)
        self.assertEqual(self.cli("verify", response["path"]).returncode, 0)
        self.assertEqual(self.documents(), self.before)
        self.assertFalse((self.source / "reports.json").exists())
        self.assertFalse((self.source / ".workstack-migration-backups").exists())

    def test_current_cli_keeps_current_backup_path(self):
        Store(self.source).initialize()
        before = self.documents()
        result = self.cli("backup", "--out", self.out)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        archive = verify_archive_file(json.loads(result.stdout)["path"])
        self.assertEqual(archive.store_schema_version, 6)
        self.assertEqual(archive.file_count, 11)
        self.assertEqual(archive.bodies, before)
        self.assertEqual(self.documents(), before)

    def test_legacy_backup_never_initializes_or_recovers(self):
        with mock.patch.object(Store, "initialize", side_effect=AssertionError("initialize")), mock.patch.object(
            Store, "_recover_locked", side_effect=AssertionError("recover")
        ):
            archive = backup_store(self.source, self.out)
        self.assertEqual(verify_archive_file(archive.path).bodies, self.before)

    def test_mixed_report_roster_refuses_before_output(self):
        (self.source / "reports.json").write_text('{"version":1,"reports":[],"idempotency":[]}', encoding="utf-8")
        before = self.documents()
        self.assertNotEqual(self.cli("backup", "--out", self.out).returncode, 0)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.documents(), before)

    def test_pending_journal_refuses_without_recovery(self):
        journal = Store(self.source).journal_path
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_bytes(b"pending")
        before = self.documents()
        self.assertNotEqual(self.cli("backup", "--out", self.out).returncode, 0)
        self.assertEqual(journal.read_bytes(), b"pending")
        self.assertEqual(self.documents(), before)
        self.assertFalse(self.out.exists())

    def test_mixed_v4_marker_refuses_before_output(self):
        (self.source / "store.json").write_text("{}", encoding="utf-8")
        before = self.documents()
        self.assertNotEqual(self.cli("backup", "--out", self.out).returncode, 0)
        self.assertEqual(self.documents(), before)
        self.assertFalse(self.out.exists())

    def test_busy_writer_refuses_then_releases_for_retry(self):
        owner = Store(self.source)
        lease = owner.try_acquire_writer_lease()
        self.assertIsNotNone(lease)
        try:
            self.assertNotEqual(self.cli("backup", "--out", self.out).returncode, 0)
            self.assertFalse(self.out.exists())
        finally:
            owner.release_writer_lease(lease)
        self.assertEqual(self.cli("backup", "--out", self.out).returncode, 0)

    def test_unlocked_format_selection_cannot_admit_changed_metadata(self):
        from workstack import maintenance
        original = maintenance._historical_backup_download
        def change_before_held_read(store):
            meta = json.loads(store.path("store-meta.json").read_bytes())
            meta["store_schema_version"] = 5
            store.path("store-meta.json").write_text(json.dumps(meta), encoding="utf-8")
            return original(store)
        with mock.patch.object(maintenance, "_historical_backup_download", change_before_held_read):
            with self.assertRaises(ValueError):
                backup_store(self.source, self.out)
        self.assertFalse(self.out.exists())
