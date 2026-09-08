"""Legacy (v3) document collectors for the pure Task deletion planner.

Deterministic. Each collector reads one v3 document family from an already
deep-copied snapshot and returns the purge operations that family owes. No
filesystem, lock, journal, backup, clock, randomness, or network.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .deletion_plan_primitives import (
    V3_CAPTURE_LIST_FIELDS,
    V3_TASK_LIST_FIELDS,
    TaskDeletionPlanError,
    any_display,
    as_mapping,
    as_sequence,
    display_matches,
    normalize_display,
    require_id_list,
    rewrite_task_field_op,
    string_field,
)


def owned_note_ops(target: Mapping[str, Any], wanted: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for index, _note in enumerate(as_sequence(target.get("notes", []))):
        operations.append(
            {
                "index": index,
                "op": "remove_task_owned_note",
                "source": "task.notes",
                "task_display_id": wanted,
            }
        )
    return operations


def _require_target_lists(record: Mapping[str, Any]) -> None:
    for field in V3_TASK_LIST_FIELDS:
        if field in record:
            require_id_list(record.get(field))


def task_reference_ops(
    tasks: Sequence[Any],
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for task in tasks:
        record = as_mapping(task)
        display = normalize_display(str(record["id"]))
        if display == wanted:
            _require_target_lists(record)
            continue
        parent = record.get("parent_id")
        if parent is not None and not isinstance(parent, str):
            raise TaskDeletionPlanError("malformed_reference_container")
        if display_matches(parent, wanted):
            operations.append(
                rewrite_task_field_op(
                    field="parent_id",
                    remove=wanted,
                    subject_display_id=display,
                )
            )
        operations.extend(_inbound_list_ops(record, display, wanted))
    return operations


def _inbound_list_ops(
    record: Mapping[str, Any],
    display: str,
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for field in V3_TASK_LIST_FIELDS:
        if field not in record:
            continue
        values = require_id_list(record.get(field))
        if any_display(values, wanted):
            operations.append(
                rewrite_task_field_op(
                    field=field,
                    remove=wanted,
                    subject_display_id=display,
                )
            )
    return operations


def note_link_ops(snapshot: Mapping[str, Any], wanted: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    notes_doc = as_mapping(snapshot.get("notes.json", {}))
    for note in as_sequence(notes_doc.get("notes", [])):
        record = as_mapping(note)
        note_id = string_field(record, "id")
        if "links" not in record:
            continue
        links = require_id_list(record.get("links"))
        if any_display(links, wanted):
            operations.append(
                {"note_id": note_id, "op": "rewrite_note_links", "remove": wanted}
            )
    return operations


def reply_ops(
    snapshot: Mapping[str, Any],
    wanted: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    reply_ids: list[str] = []
    operations: list[dict[str, Any]] = []
    replies_doc = as_mapping(snapshot.get("replies.json", {}))
    for reply in as_sequence(replies_doc.get("replies", [])):
        record = as_mapping(reply)
        reply_id = string_field(record, "id")
        if display_matches(record.get("task_id"), wanted):
            reply_ids.append(reply_id)
            operations.append({"op": "remove_reply", "reply_id": reply_id})
    return reply_ids, operations


def _worklog_day_ops(
    day: Mapping[str, Any],
    work_date: str,
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for index, entry in enumerate(as_sequence(day.get("entries", []))):
        record = as_mapping(entry)
        if display_matches(record.get("task_id"), wanted):
            operations.append(
                {
                    "index": index,
                    "op": "remove_worklog_entry",
                    "work_date": work_date,
                }
            )
    for session in as_sequence(day.get("sessions", [])):
        record = as_mapping(session)
        if display_matches(record.get("task_id"), wanted):
            operations.append(
                {
                    "op": "remove_work_session",
                    "session_id": string_field(record, "id"),
                    "work_date": work_date,
                }
            )
    return operations


def worklog_ops(snapshot: Mapping[str, Any], wanted: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    worklog_doc = as_mapping(snapshot.get("worklog.json", {}))
    days = worklog_doc.get("days", {})
    if days is None:
        days = {}
    days_map = as_mapping(days)
    for work_date in sorted(days_map):
        day = as_mapping(days_map[work_date])
        operations.extend(_worklog_day_ops(day, work_date, wanted))
    return operations


def _capture_field_ops(
    record: Mapping[str, Any],
    capture_id: str,
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for field in V3_CAPTURE_LIST_FIELDS:
        if field not in record:
            continue
        values = require_id_list(record.get(field))
        if any_display(values, wanted):
            operations.append(
                {
                    "capture_id": capture_id,
                    "field": field,
                    "op": "unlink_capture_field",
                    "remove": wanted,
                }
            )
    return operations


def _capture_action_ops(
    record: Mapping[str, Any],
    capture_id: str,
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    normalized = record.get("normalized")
    if not isinstance(normalized, Mapping):
        return operations
    for action in as_sequence(normalized.get("action_items", [])):
        item = as_mapping(action)
        if display_matches(item.get("task_id"), wanted):
            operations.append(
                {
                    "action_id": string_field(item, "id"),
                    "capture_id": capture_id,
                    "op": "unlink_capture_action",
                }
            )
    return operations


def capture_ops(snapshot: Mapping[str, Any], wanted: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    captures_doc = as_mapping(snapshot.get("captures.json", {}))
    for capture in as_sequence(captures_doc.get("captures", [])):
        record = as_mapping(capture)
        capture_id = string_field(record, "id")
        operations.extend(_capture_field_ops(record, capture_id, wanted))
        operations.extend(_capture_action_ops(record, capture_id, wanted))
    return operations


def activity_ops(
    activity_doc: Mapping[str, Any],
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for event in as_sequence(activity_doc.get("activity", [])):
        record = as_mapping(event)
        if display_matches(record.get("task_id"), wanted):
            operations.append(
                {
                    "event_id": string_field(record, "id"),
                    "op": "remove_activity_event",
                }
            )
    return operations


def planning_ops(
    activity_doc: Mapping[str, Any],
    wanted: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for fact in as_sequence(activity_doc.get("planning_status", [])):
        record = as_mapping(fact)
        if display_matches(record.get("task_id"), wanted):
            operations.append(
                {
                    "fact_id": string_field(record, "id"),
                    "op": "remove_planning_event",
                }
            )
    return operations
