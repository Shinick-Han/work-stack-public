from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from workstack.storage.canonical import CanonicalJsonError, canonical_json_bytes, canonical_sha256
from workstack.storage.validation import validate_storage_path


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads(
    (ROOT / "contracts" / "workstack-ssot-v4" / "examples" / "valid" / "cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]


def _case(name: str) -> dict[str, object]:
    return copy.deepcopy(next(item["instance"] for item in CASES if item["name"] == name))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _write_record(root: Path, kind: str, value: dict[str, object]) -> None:
    uid = str(value["uid"])
    _write_json(root / "records" / kind / uid[:2] / f"{uid}.json", value)


def _write_stream(root: Path, kind: str, events: list[dict[str, object]], *, final_newline: bool = True) -> Path:
    path = root / "streams" / kind / "2026-09.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    def encoded(event: dict[str, object]) -> bytes:
        try:
            return canonical_json_bytes(event)
        except CanonicalJsonError:
            return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    body = b"\n".join(encoded(event) for event in events)
    path.write_bytes(body + (b"\n" if final_newline else b""))
    return path


def _make_store(root: Path) -> dict[str, dict[str, object]]:
    records = {
        "task": _case("task"),
        "objective": _case("objective"),
        "capture": _case("capture"),
        "reply": _case("reply"),
        "note": _case("note"),
    }
    task_uid = records["task"]["uid"]
    objective_uid = records["objective"]["uid"]
    capture_uid = records["capture"]["uid"]
    records["task"]["objective_uids"] = [objective_uid]
    records["capture"]["linked_task_uids"] = [task_uid]
    records["capture"]["converted_task_uids"] = []
    records["reply"]["task_uid"] = task_uid
    records["reply"]["capture_uid"] = capture_uid
    records["note"]["note_kind"] = "task_annotation"
    records["note"]["task_uid"] = task_uid
    _write_json(root / "store.json", _case("store-metadata"))
    _write_json(root / "workspace.json", _case("workspace"))
    for singular, plural in (
        ("task", "tasks"),
        ("objective", "objectives"),
        ("capture", "captures"),
        ("reply", "replies"),
        ("note", "notes"),
    ):
        _write_record(root, plural, records[singular])
    return records


def _planning_event(records: dict[str, dict[str, object]]) -> dict[str, object]:
    event = _case("planning-status-event")
    event["task_uid"] = records["task"]["uid"]
    event["record_uid"] = records["task"]["uid"]
    event["task_display_id"] = records["task"]["display_id"]
    return event


class StorageCrossInvariantTests(unittest.TestCase):
    def test_valid_references_and_planning_bootstrap_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = _make_store(root)
            _write_stream(root, "planning-status", [_planning_event(records)])
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

            report = validate_storage_path(root)

            self.assertTrue(report.valid, report.issues)
            after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_dangling_references_cycles_and_reply_semantics_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = _make_store(root)
            task = records["task"]
            task["parent_uid"] = task["uid"]
            task["objective_uids"] = ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
            _write_record(root, "tasks", task)
            reply = records["reply"]
            reply["capability"] = "teams.reply"
            reply["capture_revision"] = 900
            _write_record(root, "replies", reply)

            report = validate_storage_path(root)

            codes = {issue.code for issue in report.issues}
            self.assertIn("DANGLING_REFERENCE", codes)
            self.assertIn("TASK_RELATIONSHIP_CYCLE", codes)
            self.assertIn("REPLY_CAPABILITY_MISMATCH", codes)
            self.assertIn("REFERENCED_REVISION_MISSING", codes)

    def test_stream_layout_malformed_tail_duplicates_and_unknown_kind_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = _make_store(root)
            first = _planning_event(records)
            second = copy.deepcopy(first)
            second["sequence"] = 1
            _write_stream(root, "planning-status", [first, second], final_newline=False)
            malformed = root / "streams" / "activity" / "2026-09.ndjson"
            malformed.parent.mkdir(parents=True)
            malformed.write_text("{not-json}\n", encoding="utf-8")
            (root / "streams" / "future").mkdir(parents=True)

            report = validate_storage_path(root)

            codes = {issue.code for issue in report.issues}
            self.assertIn("TRUNCATED_FINAL_LINE", codes)
            self.assertIn("MALFORMED_NDJSON_LINE", codes)
            self.assertIn("DUPLICATE_EVENT_UID", codes)
            self.assertIn("DUPLICATE_STREAM_SEQUENCE", codes)
            self.assertIn("STREAM_SEQUENCE_GAP", codes)
            self.assertIn("UNKNOWN_STREAM_KIND", codes)

    def test_event_uid_cannot_collide_with_authority_or_embedded_uids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = _make_store(root)
            event = _planning_event(records)
            event["event_uid"] = records["task"]["subtasks"][0]["uid"]
            _write_stream(root, "planning-status", [event])

            report = validate_storage_path(root)

            self.assertIn("DUPLICATE_UID", {issue.code for issue in report.issues})

    def test_segment_month_and_physical_sequence_order_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = _make_store(root)
            first = _planning_event(records)
            first["sequence"] = 2
            second = copy.deepcopy(first)
            second["event_uid"] = "88888888-8888-8888-8888-888888888888"
            second["sequence"] = 1
            second["legacy_fact_id"] = "PS-000002"
            second["created_at"] = "2026-08-31T23:59:59Z"
            _write_stream(root, "planning-status", [first, second])

            codes = {issue.code for issue in validate_storage_path(root).issues}

            self.assertIn("STREAM_SEGMENT_ORDER_INVALID", codes)
            self.assertIn("STREAM_SEGMENT_MONTH_MISMATCH", codes)

    def test_global_stream_digest_chain_is_verified_across_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = _make_store(root)
            first = _planning_event(records)
            first["event_digest"] = canonical_sha256(first)
            second = _case("activity-event")
            second["sequence"] = 2
            second["task_uid"] = records["task"]["uid"]
            second["record_uid"] = records["task"]["uid"]
            second["previous_event_digest"] = first["event_digest"]
            second["event_digest"] = canonical_sha256(second)
            _write_stream(root, "planning-status", [first])
            _write_stream(root, "activity", [second])

            self.assertTrue(validate_storage_path(root).valid)
            second["previous_event_digest"] = "sha256:" + "0" * 64
            _write_stream(root, "activity", [second])

            codes = {issue.code for issue in validate_storage_path(root).issues}
            self.assertIn("STREAM_CHAIN_BROKEN", codes)
            self.assertIn("EVENT_DIGEST_MISMATCH", codes)

    def test_planning_transition_checks_predecessor_revision_status_and_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = _make_store(root)
            first = _planning_event(records)
            second = copy.deepcopy(first)
            second.update(
                event_uid="88888888-8888-8888-8888-888888888888",
                sequence=2,
                legacy_fact_id="PS-000002",
                previous_event_uid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                previous_legacy_fact_id="PS-000001",
                prior_revision=0,
                new_revision=5,
                prior_status="open",
                status="open",
                task_display_id="T-9999",
            )
            _write_stream(root, "planning-status", [first, second])

            report = validate_storage_path(root)

            codes = {issue.code for issue in report.issues}
            self.assertIn("PLANNING_CHAIN_INVALID", codes)
            self.assertIn("PLANNING_REVISION_INVALID", codes)
            self.assertIn("PLANNING_STATUS_UNCHANGED", codes)
            self.assertIn("PLANNING_TASK_ID_MISMATCH", codes)
            self.assertIn("PLANNING_REVISION_EXCEEDS_TASK", codes)

    def test_diagnostics_do_not_echo_event_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_store(root)
            sensitive = "PRIVATE_EVENT_BODY_MUST_NOT_ESCAPE"
            path = root / "streams" / "activity" / "2026-09.ndjson"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"details": sensitive}) + "\n", encoding="utf-8")

            report = validate_storage_path(root)

            self.assertFalse(report.valid)
            self.assertNotIn(sensitive, repr(report))

    def test_unchained_records_and_events_must_use_canonical_value_space(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = _make_store(root)
            task_path = root / "records" / "tasks" / str(records["task"]["uid"])[:2] / (
                str(records["task"]["uid"]) + ".json"
            )
            task = records["task"]
            task["detail"] = "\ud800"
            task_path.write_text(json.dumps(task, ensure_ascii=True) + "\n", encoding="utf-8")
            event = _case("activity-event")
            event["details"] = {"unsupported_measurement": 1.5}
            _write_stream(root, "activity", [event])

            report = validate_storage_path(root)

            canonical_issues = [
                issue for issue in report.issues if issue.code == "CANONICAL_JSON_VIOLATION"
            ]
            self.assertEqual(len(canonical_issues), 2)
            self.assertNotIn("unsupported_measurement", repr(report))


if __name__ == "__main__":
    unittest.main()
