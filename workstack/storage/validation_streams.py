"""Normalized (v4) stream-segment validation.

Read-only. Reads ``streams/<kind>/<YYYY-MM>.ndjson`` under a caller-supplied
byte bound, checks each line against its schema and canonical bytes, then
checks what only the whole stream can answer: envelope identity, sequence
continuity, the event digest chain, record back-references and the
planning-status transition chain.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import CanonicalJsonError, canonical_json_bytes, canonical_sha256
from .validation_primitives import (
    DuplicateKeyError,
    StorageValidationIssue,
    canonical_issues,
    reference_issue,
    relative,
    schema_issues,
    without_duplicate_keys,
)


STREAM_KINDS = {
    "activity": ("activity-event.schema.json", "workstack.activity-event"),
    "planning-status": ("planning-status-event.schema.json", "workstack.planning-status-event"),
    "worklog": ("worklog-event.schema.json", "workstack.worklog-event"),
}
_STREAM_SEGMENT = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])\.ndjson$")


@dataclass(frozen=True)
class StreamEvent:
    kind: str
    artifact: str
    value: dict[str, Any]


def _stream_files(root: Path) -> tuple[list[tuple[str, Path]], list[StorageValidationIssue]]:
    streams_root = root / "streams"
    if not streams_root.exists():
        return [], []
    if not streams_root.is_dir():
        return [], [StorageValidationIssue("STREAM_LAYOUT_INVALID", "streams")]
    files: list[tuple[str, Path]] = []
    issues: list[StorageValidationIssue] = []
    for kind_path in sorted(streams_root.iterdir(), key=lambda item: item.name):
        if kind_path.name not in STREAM_KINDS:
            issues.append(StorageValidationIssue("UNKNOWN_STREAM_KIND", relative(kind_path, root)))
        elif not kind_path.is_dir():
            issues.append(StorageValidationIssue("STREAM_LAYOUT_INVALID", relative(kind_path, root)))
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
            issues.append(StorageValidationIssue("STREAM_SEGMENT_INVALID", relative(path, root)))
        else:
            files.append((kind_path.name, path))


def _read_stream_file(
    root: Path, kind: str, path: Path, max_segment_bytes: int
) -> tuple[list[StreamEvent], list[StorageValidationIssue]]:
    artifact = relative(path, root)
    try:
        with path.open("rb") as stream:
            raw = stream.read(max_segment_bytes + 1)
        if len(raw) > max_segment_bytes:
            return [], [StorageValidationIssue("STREAM_SEGMENT_TOO_LARGE", artifact)]
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return [], [StorageValidationIssue("INVALID_NDJSON", artifact)]
    issues: list[StorageValidationIssue] = []
    if raw and not raw.endswith(b"\n"):
        issues.append(StorageValidationIssue("TRUNCATED_FINAL_LINE", artifact))
    events: list[StreamEvent] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line_artifact = f"{artifact}:{line_number}"
        event, line_issues = _parse_stream_line(kind, line, line_artifact)
        issues.extend(line_issues)
        if event is not None:
            events.append(event)
    issues.extend(_segment_order_issues(path, events))
    return events, issues


def _segment_order_issues(path: Path, events: list[StreamEvent]) -> list[StorageValidationIssue]:
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
) -> tuple[StreamEvent | None, list[StorageValidationIssue]]:
    if not line:
        return None, [StorageValidationIssue("MALFORMED_NDJSON_LINE", artifact)]
    try:
        value = json.loads(line, object_pairs_hook=without_duplicate_keys)
    except (json.JSONDecodeError, DuplicateKeyError):
        return None, [StorageValidationIssue("MALFORMED_NDJSON_LINE", artifact)]
    if not isinstance(value, dict):
        return None, [StorageValidationIssue("NDJSON_OBJECT_REQUIRED", artifact)]
    schema_name, expected_format = STREAM_KINDS[kind]
    issues = schema_issues(schema_name, value, artifact)
    issues.extend(canonical_issues(value, artifact))
    try:
        if canonical_json_bytes(value).decode("utf-8") != line:
            issues.append(StorageValidationIssue("CANONICAL_JSON_BYTES_MISMATCH", artifact))
    except CanonicalJsonError:
        pass
    if value.get("format") != expected_format:
        issues.append(StorageValidationIssue("STREAM_KIND_MISMATCH", artifact))
    return StreamEvent(kind, artifact, value), issues


def _event_envelope_issues(
    events: list[StreamEvent], workspace_uid: str | None, authority_uids: set[str]
) -> list[StorageValidationIssue]:
    issues: list[StorageValidationIssue] = []
    seen_uids: set[str] = set()
    sequences: dict[int, StreamEvent] = {}
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


def _digest_chain_issues(events: list[StreamEvent]) -> list[StorageValidationIssue]:
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
    event: StreamEvent,
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
        issues.extend(reference_issue(event.artifact, f"/{field}", value.get(field), records[kind]))
    issues.extend(_event_record_uid_issues(event))
    return issues


def _event_record_uid_issues(event: StreamEvent) -> list[StorageValidationIssue]:
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


def _planning_bootstrap_issues(event: StreamEvent) -> list[StorageValidationIssue]:
    value = event.value
    fields = ("previous_event_uid", "previous_legacy_fact_id", "prior_revision", "prior_status")
    if any(value.get(field) is not None for field in fields):
        return [StorageValidationIssue("PLANNING_BOOTSTRAP_INVALID", event.artifact)]
    return []


def _planning_transition_issues(
    event: StreamEvent, previous: StreamEvent
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
    events: list[StreamEvent], tasks: dict[str, dict[str, Any]]
) -> list[StorageValidationIssue]:
    planning = sorted(
        (event for event in events if event.kind == "planning-status"),
        key=lambda event: event.value.get("sequence", 0),
    )
    issues: list[StorageValidationIssue] = []
    heads: dict[str, StreamEvent] = {}
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
    event: StreamEvent, task: dict[str, Any]
) -> list[StorageValidationIssue]:
    value = event.value
    issues: list[StorageValidationIssue] = []
    if value.get("task_display_id") != task.get("display_id"):
        issues.append(StorageValidationIssue("PLANNING_TASK_ID_MISMATCH", event.artifact, "/task_display_id"))
    new_revision = value.get("new_revision")
    if isinstance(new_revision, int) and new_revision > task.get("revision", -1):
        issues.append(StorageValidationIssue("PLANNING_REVISION_EXCEEDS_TASK", event.artifact, "/new_revision"))
    return issues


def stream_semantic_issues(
    events: list[StreamEvent],
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


def load_v4_streams(
    root: Path, max_segment_bytes: int
) -> tuple[list[StreamEvent], list[StorageValidationIssue]]:
    paths, issues = _stream_files(root)
    events: list[StreamEvent] = []
    for kind, path in paths:
        parsed, parse_issues = _read_stream_file(root, kind, path, max_segment_bytes)
        events.extend(parsed)
        issues.extend(parse_issues)
    return events, issues
