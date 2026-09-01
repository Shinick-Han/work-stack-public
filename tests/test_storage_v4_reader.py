from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.layout import (
    StorageLayoutError,
    V4Layout,
    _reject_case_collisions,
)
from workstack.storage.reader import StorageReadError, V4ReadLimits, read_v4
from workstack.storage import reader


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads(
    (
        ROOT
        / "contracts"
        / "workstack-ssot-v4"
        / "examples"
        / "valid"
        / "cases.json"
    ).read_text(encoding="utf-8")
)["cases"]


def _case(name: str) -> dict[str, object]:
    return copy.deepcopy(next(item["instance"] for item in CASES if item["name"] == name))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _write_record(root: Path, kind: str, value: dict[str, object]) -> Path:
    uid = str(value["uid"])
    path = root / "records" / kind / uid[:2] / f"{uid}.json"
    _write_json(path, value)
    return path


def _write_event(root: Path, kind: str, value: dict[str, object]) -> Path:
    path = root / "streams" / kind / "2026-09.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as output:
        output.write(canonical_json_bytes(value) + b"\n")
    return path


def _make_v4(root: Path, *, records: bool = True, streams: bool = True) -> None:
    _write_json(root / "store.json", _case("store-metadata"))
    _write_json(root / "workspace.json", _case("workspace"))
    if records:
        _write_record(root, "objectives", _case("objective"))
        _write_record(root, "tasks", _case("task"))
    if streams:
        _write_event(root, "planning-status", _case("planning-status-event"))
        _write_event(root, "activity", _case("activity-event"))
        _write_event(root, "worklog", _case("worklog-check-in-event"))


class V4LayoutTest(unittest.TestCase):
    def test_exact_record_path_and_deterministic_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, streams=False)
            layout = V4Layout.open(root)
            files = layout.record_files()
            self.assertEqual(
                [item.kind for item in files],
                ["objectives", "tasks"],
            )
            for item in files:
                self.assertEqual(item.path, layout.record_path(item.kind, item.uid))
                self.assertEqual(item.bucket, item.uid[:2])

    def test_shuffled_filesystem_enumeration_produces_the_same_roster(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root)
            expected = [artifact.artifact for artifact in read_v4(root).artifacts]
            original_scandir = os.scandir

            def reversed_scandir(path: object) -> list[os.DirEntry[str]]:
                return list(original_scandir(path))[::-1]

            with patch.object(os, "scandir", side_effect=reversed_scandir):
                actual = [artifact.artifact for artifact in read_v4(root).artifacts]
            self.assertEqual(actual, expected)

    def test_rejects_traversal_device_unc_and_invalid_uid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, records=False, streams=False)
            with self.assertRaisesRegex(StorageLayoutError, "PATH_TRAVERSAL_REJECTED"):
                V4Layout.open(root / ".." / root.name)
            for unsafe in (r"\\?\C:\authority", r"\\.\C:\authority", r"\\server\share"):
                with self.assertRaisesRegex(StorageLayoutError, "ROOT_PATH_REJECTED"):
                    V4Layout.open(unsafe)
            with self.assertRaisesRegex(StorageLayoutError, "INVALID_RECORD_UID"):
                V4Layout.open(root).record_path("tasks", "../workspace.json")

    def test_case_collision_detection_is_platform_independent(self) -> None:
        with self.assertRaisesRegex(StorageLayoutError, "CASE_COLLISION"):
            _reject_case_collisions(("tasks", "TASKS"), "records")

    def test_rejects_unknown_kinds_and_wrong_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, records=False, streams=False)
            (root / "records" / "mystery").mkdir(parents=True)
            with self.assertRaisesRegex(StorageLayoutError, "UNKNOWN_RECORD_KIND"):
                V4Layout.open(root).record_files()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, records=False, streams=False)
            task = _case("task")
            _write_json(root / "records" / "tasks" / "ff" / f"{task['uid']}.json", task)
            with self.assertRaisesRegex(StorageLayoutError, "RECORD_PATH_MISMATCH"):
                V4Layout.open(root).record_files()

    def test_rejects_unknown_stream_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, records=False, streams=False)
            (root / "streams" / "mystery").mkdir(parents=True)
            with self.assertRaisesRegex(StorageLayoutError, "UNKNOWN_STREAM_KIND"):
                V4Layout.open(root).stream_files()

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_rejects_symlink_when_platform_allows_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, records=False, streams=False)
            target = root / "target"
            target.mkdir()
            link = root / "records"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation requires additional Windows privilege")
            with self.assertRaisesRegex(StorageLayoutError, "LINK_REJECTED"):
                V4Layout.open(root).record_files()


class V4ReaderTest(unittest.TestCase):
    def test_reads_hand_built_v4_with_stable_artifact_roster(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root)
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            result = read_v4(root)
            self.assertEqual(result.workspace_uid, "11111111-1111-1111-1111-111111111111")
            self.assertEqual(result.record_count, 2)
            self.assertEqual(result.event_count, 3)
            self.assertEqual(
                [artifact.artifact for artifact in result.artifacts],
                [
                    "store.json",
                    "workspace.json",
                    "records/objectives/33/33333333-3333-3333-3333-333333333333.json",
                    "records/tasks/22/22222222-2222-2222-2222-222222222222.json",
                    "streams/activity/2026-09.ndjson",
                    "streams/planning-status/2026-09.ndjson",
                    "streams/worklog/2026-09.ndjson",
                ],
            )
            after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_duplicate_record_uid_across_kinds_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, records=False, streams=False)
            task = _case("task")
            _write_record(root, "tasks", task)
            note = _case("note")
            note["uid"] = task["uid"]
            _write_record(root, "notes", note)
            with self.assertRaisesRegex(StorageReadError, "DUPLICATE_RECORD_UID"):
                read_v4(root)

    def test_malformed_and_truncated_ndjson_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, records=False, streams=False)
            path = root / "streams" / "activity" / "2026-09.ndjson"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"{bad}\n")
            with self.assertRaisesRegex(StorageReadError, "INVALID_JSON"):
                read_v4(root)
            path.write_bytes(canonical_json_bytes(_case("activity-event")))
            with self.assertRaisesRegex(StorageReadError, "TRUNCATED_FINAL_LINE"):
                read_v4(root)

    def test_oversized_records_lines_segments_and_counts_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, records=False, streams=False)
            _write_record(root, "tasks", _case("task"))
            _write_record(root, "objectives", _case("objective"))
            with self.assertRaisesRegex(StorageReadError, "ARTIFACT_BYTE_LIMIT_EXCEEDED"):
                read_v4(root, limits=V4ReadLimits(max_record_bytes=100))
            with self.assertRaisesRegex(StorageReadError, "RECORD_COUNT_LIMIT_EXCEEDED"):
                read_v4(root, limits=V4ReadLimits(max_records=1))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, records=False, streams=True)
            with self.assertRaisesRegex(StorageReadError, "STREAM_LINE_BYTE_LIMIT_EXCEEDED"):
                read_v4(root, limits=V4ReadLimits(max_stream_line_bytes=20))
            with self.assertRaisesRegex(StorageReadError, "STREAM_EVENT_COUNT_LIMIT_EXCEEDED"):
                read_v4(root, limits=V4ReadLimits(max_stream_events=2))

    def test_changed_file_during_read_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_v4(root, records=False, streams=False)
            stable = (1, 2, 3, 4)
            changed = (1, 2, 3, 5)
            with patch.object(reader, "_signature", side_effect=(stable, stable, changed)):
                with self.assertRaisesRegex(StorageReadError, "ARTIFACT_CHANGED_DURING_READ"):
                    read_v4(root)


if __name__ == "__main__":
    unittest.main()
