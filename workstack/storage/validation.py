"""Read-only validation for legacy and normalized SSOT directories."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..store import DEFAULTS, Store
from .canonical import CanonicalJsonError, canonical_json_bytes, canonical_sha256
from .contracts import StorageContractError, validate_instance


_RECORD_KINDS = {
    "captures": ("capture.schema.json", "workstack.capture"),
    "notes": ("note.schema.json", "workstack.note"),
    "objectives": ("objective.schema.json", "workstack.objective"),
    "replies": ("reply.schema.json", "workstack.reply"),
    "tasks": ("task.schema.json", "workstack.task"),
}
_UID_BUCKET = re.compile(r"^[0-9a-f]{2}$")
_STREAM_SEGMENT = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])\.ndjson$")
_STREAM_KINDS = {
    "activity": ("activity-event.schema.json", "workstack.activity-event"),
    "planning-status": ("planning-status-event.schema.json", "workstack.planning-status-event"),
    "worklog": ("worklog-event.schema.json", "workstack.worklog-event"),
}
MAX_V4_JSON_BYTES = 4 * 1024 * 1024
MAX_V4_STREAM_SEGMENT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class StorageValidationIssue:
    """One content-free validation diagnostic."""

    code: str
    artifact: str = ""
    instance_path: str = ""
    keyword: str = ""


@dataclass(frozen=True)
class StoragePathValidationReport:
    """Typed result of inspecting one candidate authority without mutating it."""

    format_version: int | None
    workspace_uid: str | None
    record_count: int
    issues: tuple[StorageValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues


class _V3SourceChangedError(RuntimeError):
    pass


@dataclass(frozen=True)
class _StreamEvent:
    kind: str
    artifact: str
    value: dict[str, Any]


def _issue_key(issue: StorageValidationIssue) -> tuple[str, str, str, str]:
    return (issue.artifact, issue.instance_path, issue.keyword, issue.code)


def _report(
    format_version: int | None,
    workspace_uid: str | None,
    record_count: int,
    issues: list[StorageValidationIssue],
) -> StoragePathValidationReport:
    return StoragePathValidationReport(
        format_version=format_version,
        workspace_uid=workspace_uid,
        record_count=record_count,
        issues=tuple(sorted(set(issues), key=_issue_key)),
    )


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _relative(path: Path, root: Path) -> str:
    if path == root:
        return "."
    return path.relative_to(root).as_posix()


def _symlink_issues(root: Path) -> list[StorageValidationIssue]:
    issues: list[StorageValidationIssue] = []
    if _is_link(root):
        return [StorageValidationIssue("SYMLINK_REJECTED", ".")]
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories + files):
            candidate = current_path / name
            if _is_link(candidate):
                issues.append(StorageValidationIssue("SYMLINK_REJECTED", _relative(candidate, root)))
    return issues


def _detect_format(root: Path) -> tuple[int | None, list[StorageValidationIssue]]:
    has_v4 = (root / "store.json").exists()
    legacy_markers = set(DEFAULTS) - {"workspace.json"}
    has_v3 = any((root / name).exists() for name in legacy_markers)
    if has_v4 and has_v3:
        return None, [StorageValidationIssue("AMBIGUOUS_FORMAT")]
    if has_v4:
        return 4, []
    if has_v3 or (root / "workspace.json").exists():
        return 3, []
    return None, [StorageValidationIssue("FORMAT_NOT_DETECTED")]


def _v3_source_digests(root: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in sorted(DEFAULTS)
        if (root / name).exists()
    }


def _copy_v3_files(root: Path, candidate_root: Path) -> dict[str, str]:
    digests = _v3_source_digests(root)
    for name in digests:
        body = (root / name).read_bytes()
        if hashlib.sha256(body).hexdigest() != digests[name]:
            raise _V3SourceChangedError
        (candidate_root / name).write_bytes(body)
    if _v3_source_digests(root) != digests:
        raise _V3SourceChangedError
    return digests


def _validate_v3(root: Path) -> StoragePathValidationReport:
    with tempfile.TemporaryDirectory(prefix="workstack-v3-validate-") as temporary:
        candidate_root = Path(temporary) / "authority"
        candidate_root.mkdir()
        try:
            source_digests = _copy_v3_files(root, candidate_root)
            store = Store(candidate_root)
            runtime_root = store.runtime_root
            try:
                readiness = store.initialize()
            finally:
                shutil.rmtree(runtime_root, ignore_errors=True)
            if _v3_source_digests(root) != source_digests:
                raise _V3SourceChangedError
        except _V3SourceChangedError:
            return _report(3, None, 0, [StorageValidationIssue("V3_SOURCE_CHANGED")])
        except (OSError, ValueError):
            return _report(3, None, 0, [StorageValidationIssue("V3_INVALID")])
    return _report(3, readiness.workspace_uid, readiness.task_count, [])


class _DuplicateKeyError(ValueError):
    pass


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _read_json(path: Path, root: Path) -> tuple[dict[str, Any] | None, list[StorageValidationIssue]]:
    artifact = _relative(path, root)
    if not path.is_file():
        return None, [StorageValidationIssue("REQUIRED_FILE_MISSING", artifact)]
    try:
        with path.open("rb") as stream:
            body = stream.read(MAX_V4_JSON_BYTES + 1)
        if len(body) > MAX_V4_JSON_BYTES:
            return None, [StorageValidationIssue("JSON_TOO_LARGE", artifact)]
        value = json.loads(body.decode("utf-8", errors="strict"), object_pairs_hook=_without_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, _DuplicateKeyError):
        return None, [StorageValidationIssue("INVALID_JSON", artifact)]
    if not isinstance(value, dict):
        return None, [StorageValidationIssue("JSON_OBJECT_REQUIRED", artifact)]
    try:
        canonical = canonical_json_bytes(value)
    except CanonicalJsonError:
        return value, [StorageValidationIssue("CANONICAL_JSON_VIOLATION", artifact)]
    issues = [] if canonical == body else [
        StorageValidationIssue("CANONICAL_JSON_BYTES_MISMATCH", artifact)
    ]
    return value, issues


def _schema_issues(
    schema_name: str,
    value: dict[str, Any],
    artifact: str,
) -> list[StorageValidationIssue]:
    try:
        violations = validate_instance(schema_name, value)
    except StorageContractError:
        return [StorageValidationIssue("CONTRACT_UNAVAILABLE", artifact)]
    return [
        StorageValidationIssue(
            "SCHEMA_VIOLATION",
            artifact,
            _content_free_instance_path(violation.instance_path),
            violation.code,
        )
        for violation in violations
    ]


def _content_free_instance_path(path: str) -> str:
    """Redact keys below schema-defined opaque maps from diagnostics."""

    if path.startswith("/details/"):
        return "/details/*"
    return path


def _canonical_issues(value: dict[str, Any], artifact: str) -> list[StorageValidationIssue]:
    try:
        canonical_json_bytes(value)
    except CanonicalJsonError:
        return [StorageValidationIssue("CANONICAL_JSON_VIOLATION", artifact)]
    return []


def _record_files(
    root: Path,
) -> tuple[list[tuple[str, Path, str]], list[StorageValidationIssue]]:
    records_root = root / "records"
    if not records_root.exists():
        return [], []
    if not records_root.is_dir():
        return [], [StorageValidationIssue("RECORD_LAYOUT_INVALID", "records")]
    records: list[tuple[str, Path, str]] = []
    issues: list[StorageValidationIssue] = []
    for kind_path in sorted(records_root.iterdir(), key=lambda item: item.name):
        if kind_path.name not in _RECORD_KINDS:
            issues.append(StorageValidationIssue("UNKNOWN_RECORD_KIND", _relative(kind_path, root)))
            continue
        if not kind_path.is_dir():
            issues.append(StorageValidationIssue("RECORD_LAYOUT_INVALID", _relative(kind_path, root)))
            continue
        _collect_kind_records(root, kind_path, records, issues)
    return records, issues


def _collect_kind_records(
    root: Path,
    kind_path: Path,
    records: list[tuple[str, Path, str]],
    issues: list[StorageValidationIssue],
) -> None:
    for bucket_path in sorted(kind_path.iterdir(), key=lambda item: item.name):
        if not bucket_path.is_dir() or not _UID_BUCKET.fullmatch(bucket_path.name):
            issues.append(StorageValidationIssue("RECORD_BUCKET_INVALID", _relative(bucket_path, root)))
            continue
        for record_path in sorted(bucket_path.iterdir(), key=lambda item: item.name):
            if not record_path.is_file() or record_path.suffix != ".json":
                issues.append(StorageValidationIssue("RECORD_LAYOUT_INVALID", _relative(record_path, root)))
                continue
            records.append((kind_path.name, record_path, bucket_path.name))


def _validate_record(
    root: Path,
    kind: str,
    path: Path,
    bucket: str,
    workspace_uid: str | None,
    seen_uids: set[str],
) -> tuple[dict[str, Any] | None, list[StorageValidationIssue]]:
    artifact = _relative(path, root)
    value, issues = _read_json(path, root)
    if value is None:
        return None, issues
    schema_name, expected_format = _RECORD_KINDS[kind]
    issues.extend(_schema_issues(schema_name, value, artifact))
    issues.extend(_canonical_issues(value, artifact))
    uid = value.get("uid")
    if value.get("format") != expected_format:
        issues.append(StorageValidationIssue("RECORD_KIND_MISMATCH", artifact))
    issues.extend(_record_uid_issues(uid, path, bucket, artifact, seen_uids))
    if workspace_uid is not None and value.get("workspace_uid") != workspace_uid:
        issues.append(StorageValidationIssue("WORKSPACE_UID_MISMATCH", artifact))
    return value, issues


def _record_uid_issues(
    uid: Any,
    path: Path,
    bucket: str,
    artifact: str,
    seen_uids: set[str],
) -> list[StorageValidationIssue]:
    if not isinstance(uid, str):
        return []
    issues: list[StorageValidationIssue] = []
    if path.name != uid + ".json" or bucket != uid[:2]:
        issues.append(StorageValidationIssue("UID_PATH_MISMATCH", artifact))
    if uid in seen_uids:
        issues.append(StorageValidationIssue("DUPLICATE_UID", artifact))
    seen_uids.add(uid)
    return issues


def _reference_issue(
    artifact: str,
    instance_path: str,
    uid: Any,
    targets: dict[str, dict[str, Any]],
) -> list[StorageValidationIssue]:
    if isinstance(uid, str) and uid not in targets:
        return [StorageValidationIssue("DANGLING_REFERENCE", artifact, instance_path)]
    return []


def _list_reference_issues(
    artifact: str,
    instance_path: str,
    values: Any,
    targets: dict[str, dict[str, Any]],
) -> list[StorageValidationIssue]:
    if not isinstance(values, list):
        return []
    issues: list[StorageValidationIssue] = []
    for index, uid in enumerate(values):
        issues.extend(_reference_issue(artifact, f"{instance_path}/{index}", uid, targets))
    return issues


def _embedded_uid_issues(
    records: dict[str, dict[str, dict[str, Any]]],
    seen_uids: set[str],
) -> list[StorageValidationIssue]:
    issues: list[StorageValidationIssue] = []
    for kind, field in (("tasks", "subtasks"), ("objectives", "key_results")):
        for record in records[kind].values():
            artifact = str(record["_artifact"])
            for index, child in enumerate(record.get(field, [])):
                uid = child.get("uid") if isinstance(child, dict) else None
                if isinstance(uid, str) and uid in seen_uids:
                    issues.append(StorageValidationIssue("DUPLICATE_UID", artifact, f"/{field}/{index}/uid"))
                if isinstance(uid, str):
                    seen_uids.add(uid)
    return issues


def _task_reference_issues(
    task: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    objectives: dict[str, dict[str, Any]],
) -> list[StorageValidationIssue]:
    artifact = str(task["_artifact"])
    issues = _reference_issue(artifact, "/parent_uid", task.get("parent_uid"), tasks)
    issues.extend(_list_reference_issues(artifact, "/dependency_uids", task.get("dependency_uids"), tasks))
    issues.extend(_list_reference_issues(artifact, "/reference_uids", task.get("reference_uids"), tasks))
    issues.extend(_list_reference_issues(artifact, "/objective_uids", task.get("objective_uids"), objectives))
    return issues


def _capture_reference_issues(
    capture: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> list[StorageValidationIssue]:
    artifact = str(capture["_artifact"])
    issues = _list_reference_issues(artifact, "/linked_task_uids", capture.get("linked_task_uids"), tasks)
    issues.extend(
        _list_reference_issues(artifact, "/converted_task_uids", capture.get("converted_task_uids"), tasks)
    )
    action_items = capture.get("normalized", {}).get("action_items", [])
    for index, item in enumerate(action_items):
        uid = item.get("task_uid") if isinstance(item, dict) else None
        issues.extend(_reference_issue(artifact, f"/normalized/action_items/{index}/task_uid", uid, tasks))
    return issues


def _reply_semantic_issues(
    reply: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    captures: dict[str, dict[str, Any]],
) -> list[StorageValidationIssue]:
    artifact = str(reply["_artifact"])
    issues = _reference_issue(artifact, "/task_uid", reply.get("task_uid"), tasks)
    issues.extend(_reference_issue(artifact, "/capture_uid", reply.get("capture_uid"), captures))
    capture = captures.get(reply.get("capture_uid"))
    if capture is not None and isinstance(reply.get("capture_revision"), int):
        if reply["capture_revision"] > capture.get("revision", -1):
            issues.append(StorageValidationIssue("REFERENCED_REVISION_MISSING", artifact, "/capture_revision"))
    expected_capability = {
        "microsoft-outlook": "outlook.reply",
        "microsoft-teams": "teams.reply",
    }.get(reply.get("provider"))
    if expected_capability is not None and reply.get("capability") != expected_capability:
        issues.append(StorageValidationIssue("REPLY_CAPABILITY_MISMATCH", artifact, "/capability"))
    issues.extend(_receipt_issues(reply, artifact))
    return issues


def _receipt_issues(reply: dict[str, Any], artifact: str) -> list[StorageValidationIssue]:
    receipt = reply.get("receipt")
    state = reply.get("state")
    if state == "approved" and receipt is not None:
        return [StorageValidationIssue("REPLY_RECEIPT_STATE_MISMATCH", artifact, "/receipt")]
    if state in {"sent", "failed", "unknown"} and not isinstance(receipt, dict):
        return [StorageValidationIssue("REPLY_RECEIPT_MISSING", artifact, "/receipt")]
    if not isinstance(receipt, dict):
        return []
    issues: list[StorageValidationIssue] = []
    compared = {
        "reply_display_id": "display_id",
        "provider": "provider",
        "outcome": "state",
        "body_digest": "body_digest",
        "target_digest": "target_digest",
    }
    for receipt_field, reply_field in compared.items():
        if receipt.get(receipt_field) != reply.get(reply_field):
            issues.append(StorageValidationIssue("REPLY_RECEIPT_MISMATCH", artifact, f"/receipt/{receipt_field}"))
    return issues


def _note_semantic_issues(
    note: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> list[StorageValidationIssue]:
    artifact = str(note["_artifact"])
    task_uid = note.get("task_uid")
    issues = _reference_issue(artifact, "/task_uid", task_uid, tasks)
    if (note.get("note_kind") == "standalone") != (task_uid is None):
        issues.append(StorageValidationIssue("NOTE_KIND_MISMATCH", artifact, "/task_uid"))
    return issues


def _cycle_nodes(tasks: dict[str, dict[str, Any]], field: str) -> set[str]:
    graph: dict[str, list[str]] = {}
    for uid, task in tasks.items():
        raw = task.get(field)
        values = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
        graph[uid] = [value for value in values if value in tasks]
    stack: list[str] = []
    positions: dict[str, int] = {}
    visited: set[str] = set()
    cyclic: set[str] = set()

    def visit(uid: str) -> None:
        if uid in positions:
            cyclic.update(stack[positions[uid] :])
            return
        if uid in visited:
            return
        positions[uid] = len(stack)
        stack.append(uid)
        for target in graph[uid]:
            visit(target)
        stack.pop()
        positions.pop(uid)
        visited.add(uid)

    for uid in graph:
        visit(uid)
    return cyclic


def _record_semantic_issues(
    records: dict[str, dict[str, dict[str, Any]]], seen_uids: set[str]
) -> list[StorageValidationIssue]:
    issues = _embedded_uid_issues(records, seen_uids)
    tasks = records["tasks"]
    objectives = records["objectives"]
    captures = records["captures"]
    for task in tasks.values():
        issues.extend(_task_reference_issues(task, tasks, objectives))
    for capture in captures.values():
        issues.extend(_capture_reference_issues(capture, tasks))
    for reply in records["replies"].values():
        issues.extend(_reply_semantic_issues(reply, tasks, captures))
    for note in records["notes"].values():
        issues.extend(_note_semantic_issues(note, tasks))
    for field in ("parent_uid", "dependency_uids"):
        for uid in _cycle_nodes(tasks, field):
            issues.append(StorageValidationIssue("TASK_RELATIONSHIP_CYCLE", str(tasks[uid]["_artifact"]), f"/{field}"))
    return issues


def _stream_files(root: Path) -> tuple[list[tuple[str, Path]], list[StorageValidationIssue]]:
    streams_root = root / "streams"
    if not streams_root.exists():
        return [], []
    if not streams_root.is_dir():
        return [], [StorageValidationIssue("STREAM_LAYOUT_INVALID", "streams")]
    files: list[tuple[str, Path]] = []
    issues: list[StorageValidationIssue] = []
    for kind_path in sorted(streams_root.iterdir(), key=lambda item: item.name):
        if kind_path.name not in _STREAM_KINDS:
            issues.append(StorageValidationIssue("UNKNOWN_STREAM_KIND", _relative(kind_path, root)))
        elif not kind_path.is_dir():
            issues.append(StorageValidationIssue("STREAM_LAYOUT_INVALID", _relative(kind_path, root)))
        else:
            _collect_stream_files(root, kind_path, files, issues)
    return files, issues


def _collect_stream_files(
    root: Path,
    kind_path: Path,
    files: list[tuple[str, Path]],
    issues: list[StorageValidationIssue],
) -> None:
    for path in sorted(kind_path.iterdir(), key=lambda item: item.name):
        if not path.is_file() or not _STREAM_SEGMENT.fullmatch(path.name):
            issues.append(StorageValidationIssue("STREAM_SEGMENT_INVALID", _relative(path, root)))
        else:
            files.append((kind_path.name, path))


def _read_stream_file(
    root: Path, kind: str, path: Path
) -> tuple[list[_StreamEvent], list[StorageValidationIssue]]:
    artifact = _relative(path, root)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_V4_STREAM_SEGMENT_BYTES + 1)
        if len(raw) > MAX_V4_STREAM_SEGMENT_BYTES:
            return [], [StorageValidationIssue("STREAM_SEGMENT_TOO_LARGE", artifact)]
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return [], [StorageValidationIssue("INVALID_NDJSON", artifact)]
    issues: list[StorageValidationIssue] = []
    if raw and not raw.endswith(b"\n"):
        issues.append(StorageValidationIssue("TRUNCATED_FINAL_LINE", artifact))
    events: list[_StreamEvent] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line_artifact = f"{artifact}:{line_number}"
        event, line_issues = _parse_stream_line(kind, line, line_artifact)
        issues.extend(line_issues)
        if event is not None:
            events.append(event)
    issues.extend(_segment_order_issues(path, events))
    return events, issues


def _segment_order_issues(path: Path, events: list[_StreamEvent]) -> list[StorageValidationIssue]:
    issues: list[StorageValidationIssue] = []
    sequences = [event.value.get("sequence") for event in events]
    integer_sequences = [value for value in sequences if type(value) is int]
    if integer_sequences != sorted(integer_sequences) or len(integer_sequences) != len(set(integer_sequences)):
        issues.append(StorageValidationIssue("STREAM_SEGMENT_ORDER_INVALID", events[0].artifact if events else path.name))
    for event in events:
        created_at = event.value.get("created_at")
        if isinstance(created_at, str) and not created_at.startswith(path.stem):
            issues.append(StorageValidationIssue("STREAM_SEGMENT_MONTH_MISMATCH", event.artifact, "/created_at"))
    return issues


def _parse_stream_line(
    kind: str, line: str, artifact: str
) -> tuple[_StreamEvent | None, list[StorageValidationIssue]]:
    if not line:
        return None, [StorageValidationIssue("MALFORMED_NDJSON_LINE", artifact)]
    try:
        value = json.loads(line, object_pairs_hook=_without_duplicate_keys)
    except (json.JSONDecodeError, _DuplicateKeyError):
        return None, [StorageValidationIssue("MALFORMED_NDJSON_LINE", artifact)]
    if not isinstance(value, dict):
        return None, [StorageValidationIssue("NDJSON_OBJECT_REQUIRED", artifact)]
    schema_name, expected_format = _STREAM_KINDS[kind]
    issues = _schema_issues(schema_name, value, artifact)
    issues.extend(_canonical_issues(value, artifact))
    try:
        if canonical_json_bytes(value).decode("utf-8") != line:
            issues.append(StorageValidationIssue("CANONICAL_JSON_BYTES_MISMATCH", artifact))
    except CanonicalJsonError:
        pass
    if value.get("format") != expected_format:
        issues.append(StorageValidationIssue("STREAM_KIND_MISMATCH", artifact))
    return _StreamEvent(kind, artifact, value), issues


def _event_envelope_issues(
    events: list[_StreamEvent], workspace_uid: str | None, authority_uids: set[str]
) -> list[StorageValidationIssue]:
    issues: list[StorageValidationIssue] = []
    seen_uids: set[str] = set()
    sequences: dict[int, _StreamEvent] = {}
    for event in events:
        value = event.value
        if workspace_uid is not None and value.get("workspace_uid") != workspace_uid:
            issues.append(StorageValidationIssue("WORKSPACE_UID_MISMATCH", event.artifact))
        uid = value.get("event_uid")
        if isinstance(uid, str) and uid in authority_uids:
            issues.append(StorageValidationIssue("DUPLICATE_UID", event.artifact, "/event_uid"))
        if isinstance(uid, str) and uid in seen_uids:
            issues.append(StorageValidationIssue("DUPLICATE_EVENT_UID", event.artifact, "/event_uid"))
        if isinstance(uid, str):
            seen_uids.add(uid)
        sequence = value.get("sequence")
        if type(sequence) is int and sequence in sequences:
            issues.append(StorageValidationIssue("DUPLICATE_STREAM_SEQUENCE", event.artifact, "/sequence"))
        if type(sequence) is int:
            sequences[sequence] = event
    expected = list(range(1, len(events) + 1))
    if sorted(sequences) != expected:
        issues.append(StorageValidationIssue("STREAM_SEQUENCE_GAP", "streams"))
    return issues


def _digest_chain_issues(events: list[_StreamEvent]) -> list[StorageValidationIssue]:
    ordered = sorted(events, key=lambda event: event.value.get("sequence", 0))
    chain_enabled = any("event_digest" in event.value or "previous_event_digest" in event.value for event in ordered)
    if not chain_enabled:
        return []
    issues: list[StorageValidationIssue] = []
    previous_digest: str | None = None
    for index, event in enumerate(ordered):
        value = event.value
        if value.get("previous_event_digest") != previous_digest:
            issues.append(StorageValidationIssue("STREAM_CHAIN_BROKEN", event.artifact, "/previous_event_digest"))
        candidate = dict(value)
        claimed = candidate.pop("event_digest", None)
        try:
            actual = canonical_sha256(candidate)
        except CanonicalJsonError:
            actual = None
        if claimed != actual:
            issues.append(StorageValidationIssue("EVENT_DIGEST_MISMATCH", event.artifact, "/event_digest"))
        previous_digest = claimed if isinstance(claimed, str) else None
        if index and previous_digest is None:
            issues.append(StorageValidationIssue("STREAM_CHAIN_INCOMPLETE", event.artifact))
    return issues


def _event_record_reference_issues(
    event: _StreamEvent,
    records: dict[str, dict[str, dict[str, Any]]],
) -> list[StorageValidationIssue]:
    value = event.value
    all_records = {uid for by_uid in records.values() for uid in by_uid}
    issues: list[StorageValidationIssue] = []
    record_uid = value.get("record_uid")
    if isinstance(record_uid, str) and record_uid not in all_records:
        issues.append(StorageValidationIssue("DANGLING_REFERENCE", event.artifact, "/record_uid"))
    expected: list[tuple[str, str]] = []
    if event.kind == "planning-status":
        expected = [("task_uid", "tasks")]
    elif event.kind == "activity":
        expected = [("task_uid", "tasks"), ("capture_uid", "captures"), ("reply_uid", "replies")]
    elif value.get("kind") in {"entry", "session"}:
        expected = [("task_uid", "tasks")]
    for field, kind in expected:
        issues.extend(_reference_issue(event.artifact, f"/{field}", value.get(field), records[kind]))
    issues.extend(_event_record_uid_issues(event))
    return issues


def _event_record_uid_issues(event: _StreamEvent) -> list[StorageValidationIssue]:
    value = event.value
    if event.kind == "planning-status" and value.get("record_uid") != value.get("task_uid"):
        return [StorageValidationIssue("EVENT_RECORD_UID_MISMATCH", event.artifact, "/record_uid")]
    if event.kind == "worklog":
        expected = value.get("task_uid") if value.get("kind") in {"entry", "session"} else None
        if value.get("record_uid") != expected:
            return [StorageValidationIssue("EVENT_RECORD_UID_MISMATCH", event.artifact, "/record_uid")]
    if event.kind == "activity":
        references = [value.get(name) for name in ("task_uid", "capture_uid", "reply_uid")]
        non_null = [uid for uid in references if isinstance(uid, str)]
        if len(non_null) == 1 and value.get("record_uid") != non_null[0]:
            return [StorageValidationIssue("EVENT_RECORD_UID_MISMATCH", event.artifact, "/record_uid")]
    return []


def _planning_bootstrap_issues(event: _StreamEvent) -> list[StorageValidationIssue]:
    value = event.value
    fields = ("previous_event_uid", "previous_legacy_fact_id", "prior_revision", "prior_status")
    if any(value.get(field) is not None for field in fields):
        return [StorageValidationIssue("PLANNING_BOOTSTRAP_INVALID", event.artifact)]
    return []


def _planning_transition_issues(
    event: _StreamEvent, previous: _StreamEvent
) -> list[StorageValidationIssue]:
    value = event.value
    prior = previous.value
    issues: list[StorageValidationIssue] = []
    comparisons = {
        "previous_event_uid": prior.get("event_uid"),
        "previous_legacy_fact_id": prior.get("legacy_fact_id"),
        "prior_status": prior.get("status"),
    }
    for field, expected in comparisons.items():
        if value.get(field) != expected:
            issues.append(StorageValidationIssue("PLANNING_CHAIN_INVALID", event.artifact, f"/{field}"))
    prior_revision = value.get("prior_revision")
    new_revision = value.get("new_revision")
    previous_revision = prior.get("new_revision")
    if not (
        isinstance(prior_revision, int)
        and isinstance(new_revision, int)
        and isinstance(previous_revision, int)
        and new_revision == prior_revision + 1
        and prior_revision >= previous_revision
    ):
        issues.append(StorageValidationIssue("PLANNING_REVISION_INVALID", event.artifact, "/new_revision"))
    if value.get("status") == prior.get("status"):
        issues.append(StorageValidationIssue("PLANNING_STATUS_UNCHANGED", event.artifact, "/status"))
    return issues


def _planning_status_issues(
    events: list[_StreamEvent], tasks: dict[str, dict[str, Any]]
) -> list[StorageValidationIssue]:
    planning = sorted(
        (event for event in events if event.kind == "planning-status"),
        key=lambda event: event.value.get("sequence", 0),
    )
    issues: list[StorageValidationIssue] = []
    heads: dict[str, _StreamEvent] = {}
    seen_legacy: set[str] = set()
    for index, event in enumerate(planning, start=1):
        value = event.value
        legacy_id = value.get("legacy_fact_id")
        if legacy_id in seen_legacy:
            issues.append(StorageValidationIssue("DUPLICATE_PLANNING_FACT_ID", event.artifact, "/legacy_fact_id"))
        if isinstance(legacy_id, str):
            seen_legacy.add(legacy_id)
        if legacy_id != f"PS-{index:06d}":
            issues.append(StorageValidationIssue("PLANNING_FACT_ORDER_INVALID", event.artifact, "/legacy_fact_id"))
        task_uid = value.get("task_uid")
        task = tasks.get(task_uid)
        if task is not None:
            issues.extend(_planning_task_issues(event, task))
        previous = heads.get(task_uid) if isinstance(task_uid, str) else None
        issues.extend(_planning_bootstrap_issues(event) if previous is None else _planning_transition_issues(event, previous))
        if isinstance(task_uid, str):
            heads[task_uid] = event
    return issues


def _planning_task_issues(
    event: _StreamEvent, task: dict[str, Any]
) -> list[StorageValidationIssue]:
    value = event.value
    issues: list[StorageValidationIssue] = []
    if value.get("task_display_id") != task.get("display_id"):
        issues.append(StorageValidationIssue("PLANNING_TASK_ID_MISMATCH", event.artifact, "/task_display_id"))
    new_revision = value.get("new_revision")
    if isinstance(new_revision, int) and new_revision > task.get("revision", -1):
        issues.append(StorageValidationIssue("PLANNING_REVISION_EXCEEDS_TASK", event.artifact, "/new_revision"))
    return issues


def _stream_semantic_issues(
    events: list[_StreamEvent],
    records: dict[str, dict[str, dict[str, Any]]],
    workspace_uid: str | None,
    authority_uids: set[str],
) -> list[StorageValidationIssue]:
    issues = _event_envelope_issues(events, workspace_uid, authority_uids)
    issues.extend(_digest_chain_issues(events))
    for event in events:
        issues.extend(_event_record_reference_issues(event, records))
    issues.extend(_planning_status_issues(events, records["tasks"]))
    return issues


def _load_v4_records(
    root: Path,
    workspace_uid: str | None,
    seen_uids: set[str],
) -> tuple[dict[str, dict[str, dict[str, Any]]], int, list[StorageValidationIssue]]:
    records = {kind: {} for kind in _RECORD_KINDS}
    paths, issues = _record_files(root)
    for kind, path, bucket in paths:
        value, record_issues = _validate_record(root, kind, path, bucket, workspace_uid, seen_uids)
        issues.extend(record_issues)
        if value is not None and isinstance(value.get("uid"), str):
            value["_artifact"] = _relative(path, root)
            records[kind][value["uid"]] = value
    return records, len(paths), issues


def _load_v4_streams(
    root: Path,
) -> tuple[list[_StreamEvent], list[StorageValidationIssue]]:
    paths, issues = _stream_files(root)
    events: list[_StreamEvent] = []
    for kind, path in paths:
        parsed, parse_issues = _read_stream_file(root, kind, path)
        events.extend(parsed)
        issues.extend(parse_issues)
    return events, issues


def _validate_v4(root: Path) -> StoragePathValidationReport:
    store_value, store_issues = _read_json(root / "store.json", root)
    workspace_value, workspace_issues = _read_json(root / "workspace.json", root)
    issues = store_issues + workspace_issues
    if store_value is not None:
        issues.extend(_schema_issues("store.schema.json", store_value, "store.json"))
        issues.extend(_canonical_issues(store_value, "store.json"))
    if workspace_value is not None:
        issues.extend(_schema_issues("workspace.schema.json", workspace_value, "workspace.json"))
        issues.extend(_canonical_issues(workspace_value, "workspace.json"))
    workspace_uid = store_value.get("workspace_uid") if store_value is not None else None
    if not isinstance(workspace_uid, str):
        workspace_uid = None
    seen_uids = {workspace_uid} if workspace_uid is not None else set()
    if workspace_value is not None:
        if workspace_value.get("workspace_uid") != workspace_uid or workspace_value.get("uid") != workspace_uid:
            issues.append(StorageValidationIssue("WORKSPACE_UID_MISMATCH", "workspace.json"))
    records, record_count, record_issues = _load_v4_records(root, workspace_uid, seen_uids)
    issues.extend(record_issues)
    issues.extend(_record_semantic_issues(records, seen_uids))
    events, stream_issues = _load_v4_streams(root)
    issues.extend(stream_issues)
    issues.extend(_stream_semantic_issues(events, records, workspace_uid, seen_uids))
    return _report(4, workspace_uid, record_count, issues)


def validate_storage_path(path: Path | str) -> StoragePathValidationReport:
    """Inspect one candidate SSOT path without writing to that path."""

    root = Path(path).expanduser()
    try:
        if not root.exists():
            return _report(None, None, 0, [StorageValidationIssue("ROOT_NOT_FOUND")])
        if not root.is_dir():
            return _report(None, None, 0, [StorageValidationIssue("ROOT_NOT_DIRECTORY")])
        link_issues = _symlink_issues(root)
        if link_issues:
            return _report(None, None, 0, link_issues)
        format_version, format_issues = _detect_format(root)
        if format_issues:
            return _report(format_version, None, 0, format_issues)
        if format_version == 3:
            return _validate_v3(root)
        return _validate_v4(root)
    except OSError:
        return _report(None, None, 0, [StorageValidationIssue("IO_ERROR")])
