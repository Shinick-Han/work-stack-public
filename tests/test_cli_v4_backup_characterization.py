from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack import cli
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.migration_conversion import convert_v3_documents


FIXTURE = Path(__file__).parent / "fixtures" / "store-v3" / "populated"


def _write_v4(root: Path) -> None:
    documents = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in FIXTURE.glob("*.json")
    }
    conversion = convert_v3_documents(
        documents, candidate_created_at="2026-09-01T00:00:00Z"
    )

    def write(path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    write(root / "store.json", canonical_json_bytes(dict(conversion.store)))
    write(root / "workspace.json", canonical_json_bytes(dict(conversion.workspace)))
    for kind, records in conversion.records.items():
        for record in records:
            uid = str(record["uid"])
            write(root / "records" / kind / uid[:2] / f"{uid}.json", canonical_json_bytes(dict(record)))
    grouped = {}
    for kind, events in conversion.streams.items():
        for event in events:
            grouped.setdefault((kind, str(event["created_at"])[:7]), []).append(event)
    for (kind, segment), events in grouped.items():
        write(root / "streams" / kind / f"{segment}.ndjson", b"".join(
            canonical_json_bytes(dict(event)) + b"\n"
            for event in sorted(events, key=lambda item: item["sequence"])
        ))


class CliV4BackupCharacterizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.authority = self.root / "authority-v4"
        _write_v4(self.authority)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, arguments: list[str]) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = cli.main(arguments)
        return status, json.loads(output.getvalue())

    def storage_guards(self):
        refused = AssertionError("storage command touched released v3 application path")
        return (
            mock.patch.object(cli, "Store", side_effect=refused),
            mock.patch.object(cli, "WorkStack", side_effect=refused),
            mock.patch.object(cli, "execute_v3_migration", side_effect=refused),
            mock.patch.object(cli, "resume_v3_migration", side_effect=refused),
        )

    def test_create_verify_restore_emit_content_free_machine_receipts(self) -> None:
        archive = self.root / "backup.zip"
        destination = self.root / "restored"
        guards = self.storage_guards()
        with guards[0], guards[1], guards[2], guards[3]:
            create_status, created = self.run_cli([
                "--data-dir", str(self.root / "must-not-exist"),
                "storage", "v4-backup", "create", str(self.authority),
                "--out", str(archive),
            ])
            verify_status, verified = self.run_cli([
                "storage", "v4-backup", "verify", str(archive),
            ])
            restore_status, restored = self.run_cli([
                "storage", "v4-backup", "restore", str(archive),
                "--to", str(destination),
            ])

        self.assertEqual((create_status, verify_status, restore_status), (0, 0, 0))
        self.assertEqual(created["status"], "backed_up")
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(created["backup_digest"], verified["backup_digest"])
        self.assertEqual(verified["backup_digest"], restored["backup_digest"])
        self.assertEqual(created["workspace_uid"], restored["workspace_uid"])
        self.assertFalse(created["activated"])
        self.assertFalse(verified["activated"])
        self.assertFalse(restored["activated"])
        self.assertEqual(set(created), {
            "status", "archive_path", "backup_digest", "authority_digest",
            "workspace_uid", "file_count", "activated",
        })
        self.assertEqual(set(restored), {
            "status", "destination", "backup_digest", "authority_digest",
            "workspace_uid", "file_count", "activated",
        })
        self.assertTrue((destination / "store.json").is_file())
        self.assertFalse((self.root / "must-not-exist").exists())

    def test_restore_refuses_nonempty_destination_without_replace_surface(self) -> None:
        archive = self.root / "backup.zip"
        self.run_cli([
            "storage", "v4-backup", "create", str(self.authority),
            "--out", str(archive),
        ])
        destination = self.root / "occupied"
        destination.mkdir()
        marker = destination / "preserved.txt"
        marker.write_text("preserve", encoding="utf-8")

        status, receipt = self.run_cli([
            "storage", "v4-backup", "restore", str(archive),
            "--to", str(destination),
        ])

        self.assertEqual(status, 2)
        self.assertEqual(receipt, {
            "status": "refused",
            "code": "RESTORE_DESTINATION_NOT_EMPTY",
            "activated": False,
        })
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
        with self.assertRaises(SystemExit):
            cli.parser().parse_args([
                "storage", "v4-backup", "restore", str(archive),
                "--to", str(destination), "--replace",
            ])

    def test_released_nonstorage_command_still_constructs_v3_store_and_workstack(self) -> None:
        store = mock.Mock()
        stack = mock.Mock()
        stack.list_tasks.return_value = []
        with mock.patch.object(cli, "Store", return_value=store) as store_type:
            with mock.patch.object(cli, "WorkStack", return_value=stack) as stack_type:
                status, payload = self.run_cli([
                    "--data-dir", str(self.root / "released-v3"),
                    "backlog", "list",
                ])

        self.assertEqual(status, 0)
        self.assertEqual(payload, [])
        store_type.assert_called_once_with(str(self.root / "released-v3"))
        stack_type.assert_called_once_with(store, initialize=True)
        stack.list_tasks.assert_called_once_with("active")


if __name__ == "__main__":
    unittest.main()
