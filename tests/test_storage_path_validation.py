from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workstack.storage import validation
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.validation import validate_storage_path


ROOT = Path(__file__).resolve().parents[1]
V3_FIXTURE = ROOT / "tests" / "fixtures" / "store-v3" / "populated"
VALID_CASES = json.loads(
    (ROOT / "contracts" / "workstack-ssot-v4" / "examples" / "valid" / "cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]


def _case(name: str) -> dict[str, object]:
    return next(dict(item["instance"]) for item in VALID_CASES if item["name"] == name)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _write_record(root: Path, kind: str, value: dict[str, object]) -> Path:
    uid = str(value["uid"])
    path = root / "records" / kind / uid[:2] / (uid + ".json")
    _write_json(path, value)
    return path


def _make_v4(root: Path, *, include_task: bool = True) -> None:
    _write_json(root / "store.json", _case("store-metadata"))
    _write_json(root / "workspace.json", _case("workspace"))
    if include_task:
        task = _case("task")
        for subtask in task["subtasks"]:
            subtask.setdefault("status", "open")
        _write_record(root, "objectives", _case("objective"))
        _write_record(root, "tasks", task)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class StoragePathValidationTest(unittest.TestCase):
    def test_valid_v3_is_checked_on_a_copy_without_mutating_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "authority"
            shutil.copytree(V3_FIXTURE, root)
            before = _tree_bytes(root)
            report = validate_storage_path(root)
            self.assertTrue(report.valid, report.issues)
            self.assertEqual(report.format_version, 3)
            self.assertEqual(report.record_count, 2)
            self.assertEqual(_tree_bytes(root), before)
            self.assertEqual(sorted(path.name for path in root.iterdir()), sorted(before))

    def test_invalid_v3_is_content_free_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "authority"
            shutil.copytree(V3_FIXTURE, root)
            sensitive_text = "sensitive task material"
            (root / "backlog.json").write_text(sensitive_text, encoding="utf-8")
            before = _tree_bytes(root)
            report = validate_storage_path(root)
            self.assertFalse(report.valid)
            self.assertEqual([issue.code for issue in report.issues], ["V3_INVALID"])
            self.assertNotIn(sensitive_text, repr(report))
            self.assertEqual(_tree_bytes(root), before)

    def test_v3_change_during_validation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "authority"
            shutil.copytree(V3_FIXTURE, root)
            digests = validation._v3_source_digests(root)
            with patch.object(
                validation,
                "_v3_source_digests",
                side_effect=(digests, digests, {}),
            ):
                report = validate_storage_path(root)
            self.assertFalse(report.valid)
            self.assertEqual(report.issues[0].code, "V3_SOURCE_CHANGED")

    def test_valid_v4_schema_layout_and_identity_are_accepted_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root)
            before = _tree_bytes(root)
            report = validate_storage_path(root)
            self.assertTrue(report.valid, report.issues)
            self.assertEqual(report.format_version, 4)
            self.assertEqual(report.workspace_uid, "11111111-1111-1111-1111-111111111111")
            self.assertEqual(report.record_count, 2)
            self.assertEqual(_tree_bytes(root), before)

    def test_v4_schema_and_workspace_mismatch_are_reported_without_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, include_task=False)
            task = _case("task")
            task["workspace_uid"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            task.pop("title")
            _write_record(root, "tasks", task)
            report = validate_storage_path(root)
            codes = {issue.code for issue in report.issues}
            self.assertIn("SCHEMA_VIOLATION", codes)
            self.assertIn("WORKSPACE_UID_MISMATCH", codes)
            self.assertNotIn("Normalize the SSOT contract", repr(report))

    def test_v4_uid_must_match_bucket_and_be_unique_across_record_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, include_task=False)
            task = _case("task")
            task_path = root / "records" / "tasks" / "ff" / (str(task["uid"]) + ".json")
            _write_json(task_path, task)
            note = _case("note")
            note["uid"] = task["uid"]
            _write_record(root, "notes", note)
            report = validate_storage_path(root)
            codes = [issue.code for issue in report.issues]
            self.assertIn("UID_PATH_MISMATCH", codes)
            self.assertIn("DUPLICATE_UID", codes)

    def test_v4_unknown_record_kind_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, include_task=False)
            _write_json(root / "records" / "widgets" / "aa" / "record.json", {})
            report = validate_storage_path(root)
            self.assertEqual(
                [issue.code for issue in report.issues],
                ["UNKNOWN_RECORD_KIND"],
            )

    def test_symlink_is_rejected_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "authority"
            target = Path(temporary) / "outside.json"
            root.mkdir()
            _make_v4(root, include_task=False)
            _write_json(target, {"sample": "outside"})
            link = root / "records" / "outside.json"
            link.parent.mkdir()
            try:
                os.symlink(target, link)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            report = validate_storage_path(root)
            self.assertFalse(report.valid)
            self.assertEqual(report.issues[0].code, "SYMLINK_REJECTED")

    def test_detected_link_is_rejected_on_hosts_without_symlink_privilege(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, include_task=False)
            candidate = root / "records" / "linked"
            candidate.parent.mkdir()
            candidate.write_text("not followed", encoding="utf-8")
            real_is_link = validation._is_link

            def detected_link(path: Path) -> bool:
                return path == candidate or real_is_link(path)

            with patch.object(validation, "_is_link", side_effect=detected_link):
                report = validate_storage_path(root)
            self.assertFalse(report.valid)
            self.assertEqual(report.issues[0].code, "SYMLINK_REJECTED")

    def test_missing_unknown_and_ambiguous_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            missing = validate_storage_path(base / "missing")
            self.assertEqual(missing.issues[0].code, "ROOT_NOT_FOUND")
            unknown = validate_storage_path(base)
            self.assertEqual(unknown.issues[0].code, "FORMAT_NOT_DETECTED")
            _make_v4(base, include_task=False)
            _write_json(base / "backlog.json", {"version": 3, "tasks": []})
            ambiguous = validate_storage_path(base)
            self.assertEqual(ambiguous.issues[0].code, "AMBIGUOUS_FORMAT")

    def test_file_root_root_link_and_incomplete_v4_layout_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            plain_file = base / "not-a-directory"
            plain_file.write_text("not a store", encoding="utf-8")
            self.assertEqual(
                validate_storage_path(plain_file).issues[0].code,
                "ROOT_NOT_DIRECTORY",
            )

            linked_root = base / "linked-root"
            linked_root.mkdir()
            with patch.object(validation, "_is_link", side_effect=lambda path: path == linked_root):
                self.assertEqual(
                    validate_storage_path(linked_root).issues[0].code,
                    "SYMLINK_REJECTED",
                )

            incomplete = base / "incomplete-v4"
            incomplete.mkdir()
            _write_json(incomplete / "store.json", _case("store-metadata"))
            (incomplete / "records").write_text("not a directory", encoding="utf-8")
            report = validate_storage_path(incomplete)
            codes = {issue.code for issue in report.issues}
            self.assertIn("REQUIRED_FILE_MISSING", codes)
            self.assertIn("RECORD_LAYOUT_INVALID", codes)

    def test_v4_json_reads_are_bounded_without_disclosing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, include_task=False)
            with patch.object(validation, "MAX_V4_JSON_BYTES", 8):
                report = validate_storage_path(root)
            self.assertFalse(report.valid)
            self.assertIn("JSON_TOO_LARGE", {issue.code for issue in report.issues})
            self.assertNotIn("11111111-1111-1111-1111-111111111111", repr(report))

    def test_v4_stream_reads_are_bounded_without_parsing_partial_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, include_task=False)
            stream = root / "streams" / "activity" / "2026-09.ndjson"
            stream.parent.mkdir(parents=True)
            stream.write_text('{"sensitive":"never parsed"}\n', encoding="utf-8")
            with patch.object(validation, "MAX_V4_STREAM_SEGMENT_BYTES", 8):
                report = validate_storage_path(root)
            self.assertFalse(report.valid)
            self.assertIn(
                "STREAM_SEGMENT_TOO_LARGE",
                {issue.code for issue in report.issues},
            )
            self.assertNotIn("never parsed", repr(report))


if __name__ == "__main__":
    unittest.main()
