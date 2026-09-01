from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from typing import Any, Callable

from workstack.maintenance import BackupValidationError, backup_store, verify_backup
from workstack.service import WorkStack
from workstack.store import Store


class VerifiedArchiveCharacterizationTest(unittest.TestCase):
    """Freeze archive refusal order and messages before decomposition."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        stack = WorkStack(Store(self.source))
        stack.add_task("Archive contract")
        self.artifact = backup_store(self.source, self.root / "backups")
        with zipfile.ZipFile(self.artifact.path, "r") as archive:
            self.members = {
                info.filename: archive.read(info.filename)
                for info in archive.infolist()
            }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def digest(body: bytes) -> str:
        return "sha256:" + hashlib.sha256(body).hexdigest()

    def write_archive(
        self,
        name: str,
        *,
        manifest_change: Callable[[dict[str, Any]], None] | None = None,
        member_change: Callable[[dict[str, bytes]], None] | None = None,
        names: list[str] | None = None,
    ) -> Path:
        bodies = dict(self.members)
        if member_change is not None:
            member_change(bodies)
        manifest = json.loads(bodies["manifest.json"].decode("utf-8"))
        if member_change is not None:
            for record in manifest["files"]:
                body = bodies.get(record["name"])
                if body is not None:
                    record["size"] = len(body)
                    record["sha256"] = self.digest(body)
        if manifest_change is not None:
            manifest_change(manifest)
        bodies["manifest.json"] = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        target = self.root / name
        selected = names or list(bodies)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for member_name in selected:
                    archive.writestr(member_name, bodies.get(member_name, b"extra"))
        return target

    def assert_invalid(self, path: Path, message: str) -> None:
        with self.assertRaisesRegex(BackupValidationError, message):
            verify_backup(path)

    def test_file_and_member_envelope_refusal_table(self) -> None:
        missing = self.root / "missing.zip"
        invalid_zip = self.root / "invalid.zip"
        invalid_zip.write_bytes(b"not a zip")
        without_member = self.write_archive(
            "missing-member.zip",
            names=[name for name in self.members if name != "notes.json"],
        )
        with_extra = self.write_archive(
            "extra-member.zip", names=[*self.members, "extra.json"]
        )
        duplicate = self.write_archive(
            "duplicate-member.zip", names=[*self.members, "notes.json"]
        )
        directory = self.write_archive(
            "directory-member.zip",
            names=[name for name in self.members if name != "notes.json"] + ["notes.json/"],
        )
        for path, message in (
            (missing, "backup archive does not exist"),
            (invalid_zip, "backup archive is unreadable"),
            (without_member, "backup archive member set is invalid"),
            (with_extra, "backup archive member set is invalid"),
            (duplicate, "backup archive member set is invalid"),
            (directory, "backup archive member set is invalid"),
        ):
            with self.subTest(path=path.name):
                self.assert_invalid(path, message)

    def test_manifest_header_refusal_table(self) -> None:
        cases: tuple[tuple[str, Callable[[dict[str, Any]], None], str], ...] = (
            ("fields", lambda value: value.update({"extra": True}), "backup manifest fields are invalid"),
            ("schema", lambda value: value.update(schema_version=99), "backup schema version is unsupported"),
            ("product", lambda value: value.update(product_version=""), "backup product version is invalid"),
            ("time-type", lambda value: value.update(created_at=42), "backup creation time is invalid"),
            ("time-shape", lambda value: value.update(created_at="not-time"), "backup creation time is invalid"),
            ("time-zone", lambda value: value.update(created_at="2026-08-31T12:00:00"), "backup creation time must include a timezone"),
            ("workspace", lambda value: value.update(workspace_id=""), "backup workspace identity is invalid"),
            ("files", lambda value: value.update(files={}), "backup file manifest is invalid"),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label):
                self.assert_invalid(
                    self.write_archive(label + ".zip", manifest_change=mutate), message
                )

    def test_file_record_refusal_table(self) -> None:
        def mutate_first(mutator: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
            def apply(manifest: dict[str, Any]) -> None:
                mutator(manifest["files"][0])

            return apply

        cases = (
            ("record-fields", mutate_first(lambda record: record.update(extra=True)), "backup file record is invalid"),
            ("unknown", mutate_first(lambda record: record.update(name="unknown.json")), "backup file record is unknown or repeated"),
            ("size", mutate_first(lambda record: record.update(size=record["size"] + 1)), "backup member size mismatch"),
            ("digest", mutate_first(lambda record: record.update(sha256="sha256:" + "0" * 64)), "backup member digest mismatch"),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label):
                self.assert_invalid(
                    self.write_archive(label + ".zip", manifest_change=mutate), message
                )

    def test_semantic_identity_and_schema_refusal_table(self) -> None:
        def corrupt_backlog(bodies: dict[str, bytes]) -> None:
            bodies["backlog.json"] = b"{}"

        invalid_store = self.write_archive(
            "invalid-store.zip", member_change=corrupt_backlog
        )
        wrong_identity = self.write_archive(
            "wrong-identity.zip",
            manifest_change=lambda value: value.update(
                workspace_id="00000000-0000-4000-8000-000000000001"
            ),
        )
        wrong_schema = self.write_archive(
            "wrong-schema.zip",
            manifest_change=lambda value: value.update(store_schema_version=2),
        )
        for path, message in (
            (invalid_store, "backup store failed semantic validation"),
            (wrong_identity, "backup workspace identity mismatch"),
            (wrong_schema, "backup store schema mismatch"),
        ):
            with self.subTest(path=path.name):
                self.assert_invalid(path, message)

    def test_success_receipt_is_derived_from_exact_archive_bytes(self) -> None:
        verified = verify_backup(self.artifact.path)
        self.assertEqual(verified.path, self.artifact.path.resolve())
        self.assertEqual(verified.workspace_id, self.artifact.workspace_id)
        self.assertEqual(verified.digest, self.digest(self.artifact.path.read_bytes()))
        self.assertEqual(verified.file_count, 9)


if __name__ == "__main__":
    unittest.main()
