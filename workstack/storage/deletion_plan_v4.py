"""Normalized (v4) record and stream collectors for the deletion planner.

Deterministic. Each collector reads one v4 record or stream family from an
already deep-copied physical snapshot and returns the purge operations that
family owes. v4 references the target by UID; display ids survive only where
the v3 shape kept them. No filesystem, lock, journal, backup, clock,
randomness, or network.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .deletion_plan_primitives import (
    V4_CAPTURE_UID_FIELDS,
    V4_TASK_LIST_FIELDS,
    TaskDeletionPlanError,
    any_display,
    as_mapping,
    as_sequence,
    normalize_display,
    require_id_list,
    rewrite_task_field_op,
    string_field,
)


def _require_target_lists(record: Mapping[str, Any]) -> None:
    for field in V4_TASK_LIST_FIELDS:
        if field in record:
            require_id_list(record.get(field))


def _inbound_uid_list_ops(
    record: Mapping[str, Any],
    display: str,
    uid: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for field in V4_TASK_LIST_FIELDS:
        if field not in record:
            continue
        values = require_id_list(record.get(field))
        if uid in values:
            operations.append(
                rewrite_task_field_op(
                    field=field,
                    remove=uid,
                    subject_display_id=display,
                )
            )
    return operations


def task_reference_ops(
    tasks: Sequence[Any],
    wanted: str,
    uid: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for task in tasks:
        record = as_mapping(task)
        display = normalize_display(str(record["display_id"]))
        if display == wanted:
            _require_target_lists(record)
            continue
        parent = record.get("parent_uid")
        if parent is not None and not isinstance(parent, str):
            raise TaskDeletionPlanError("malformed_reference_container")
        if parent == uid:
            operations.append(
                rewrite_task_field_op(
                    field="parent_uid",
                    remove=uid,
                    subject_display_id=display,
                )
            )
        operations.extend(_inbound_uid_list_ops(record, display, uid))
    return operations


def note_ops(
    records: Mapping[str, Any],
    wanted: str,
    uid: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for note in as_sequence(records.get("notes", [])):
        record = as_mapping(note)
        if record.get("note_kind") == "task_annotation" and record.get("task_uid") == uid:
            operations.append(
                {
                    "note_uid": string_field(record, "uid"),
                    "op": "remove_task_owned_note",
                    "source": "records.notes",
                }
            )
        if "links" not in record:
            continue
        links = require_id_list(record.get("links"))
        if any_display(links, wanted):
            operations.append(
                {
                    "note_id": string_field(record, "display_id"),
                    "op": "rewrite_note_links",
                    "remove": wanted,
                }
            )
    return operations


def reply_ops(
    records: Mapping[str, Any],
    uid: str,
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    reply_ids: list[str] = []
    reply_uids: list[str] = []
    operations: list[dict[str, Any]] = []
    for reply in as_sequence(records.get("replies", [])):
        record = as_mapping(reply)
        if record.get("task_uid") == uid:
            reply_id = string_field(record, "display_id")
            reply_uid = string_field(record, "uid")
            reply_ids.append(reply_id)
            reply_uids.append(reply_uid)
            operations.append(
                {
                    "op": "remove_reply",
                    "reply_id": reply_id,
                    "reply_uid": reply_uid,
                }
            )
    return reply_ids, reply_uids, operations


def _capture_uid_field_ops(
    record: Mapping[str, Any],
    capture_id: str,
    uid: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for field in V4_CAPTURE_UID_FIELDS:
        if field not in record:
            continue
        values = require_id_list(record.get(field))
        if uid in values:
            operations.append(
                {
                    "capture_id": capture_id,
                    "field": field,
                    "op": "unlink_capture_field",
                    "remove": uid,
                }
            )
    return operations


def _capture_hint_ops(
    record: Mapping[str, Any],
    capture_id: str,
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    if "task_hints" not in record:
        return operations
    hints = require_id_list(record.get("task_hints"))
    if any_display(hints, wanted):
        operations.append(
            {
                "capture_id": capture_id,
                "field": "task_hints",
                "op": "unlink_capture_field",
                "remove": wanted,
            }
        )
    return operations


def _capture_action_ops(
    record: Mapping[str, Any],
    capture_id: str,
    uid: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    normalized = record.get("normalized")
    if not isinstance(normalized, Mapping):
        return operations
    for action in as_sequence(normalized.get("action_items", [])):
        item = as_mapping(action)
        if item.get("task_uid") == uid:
            operations.append(
                {
                    "action_id": string_field(item, "id"),
                    "capture_id": capture_id,
                    "op": "unlink_capture_action",
                }
            )
    return operations


def capture_ops(
    records: Mapping[str, Any],
    wanted: str,
    uid: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for capture in as_sequence(records.get("captures", [])):
        record = as_mapping(capture)
        capture_id = string_field(record, "display_id")
        operations.extend(_capture_uid_field_ops(record, capture_id, uid))
        operations.extend(_capture_hint_ops(record, capture_id, wanted))
        operations.extend(_capture_action_ops(record, capture_id, uid))
    return operations


def activity_ops(streams: Mapping[str, Any], uid: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for event in as_sequence(streams.get("activity", [])):
        record = as_mapping(event)
        if record.get("task_uid") == uid:
            event_key = "legacy_event_id" if record.get("legacy_event_id") else "event_uid"
            operations.append(
                {
                    "event_id": string_field(record, event_key),
                    "op": "remove_activity_event",
                }
            )
    return operations


def planning_ops(streams: Mapping[str, Any], uid: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for event in as_sequence(streams.get("planning-status", [])):
        record = as_mapping(event)
        if record.get("task_uid") == uid:
            fact_key = "legacy_fact_id" if record.get("legacy_fact_id") else "event_uid"
            operations.append(
                {"fact_id": string_field(record, fact_key), "op": "remove_planning_event"}
            )
    return operations


def _worklog_session_op(record: Mapping[str, Any], event_uid: str) -> dict[str, Any]:
    session_id = record.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise TaskDeletionPlanError("malformed_reference_container")
    return {
        "event_uid": event_uid,
        "op": "remove_work_session",
        "session_id": session_id,
        "work_date": record.get("work_date"),
    }


def worklog_ops(streams: Mapping[str, Any], uid: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for event in as_sequence(streams.get("worklog", [])):
        record = as_mapping(event)
        if record.get("task_uid") != uid:
            continue
        kind = record.get("kind")
        event_uid = string_field(record, "event_uid")
        if kind == "session":
            operations.append(_worklog_session_op(record, event_uid))
        elif kind == "entry":
            operations.append(
                {
                    "event_uid": event_uid,
                    "op": "remove_worklog_entry",
                    "work_date": record.get("work_date"),
                }
            )
    return operations


def ledger_records(snapshot: Mapping[str, Any]) -> list[Any]:
    ledger = snapshot.get("idempotency_ledger", {})
    if isinstance(ledger, Mapping) and "records" in ledger:
        return as_sequence(ledger.get("records", []))
    if isinstance(ledger, Sequence) and not isinstance(ledger, (str, bytes, bytearray)):
        return list(ledger)
    return []
