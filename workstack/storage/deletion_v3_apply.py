"""In-memory application of a v3 Task deletion plan.

Every phase rewrites the deep-copied document set only; nothing here touches
the filesystem, the lock, the journal or a clock. The phase tuple is the
single ordering authority, and a plan whose operations do not match the
frozen operation shapes is refused before any phase runs.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Sequence

from ..planning_status import PlanningStatusValidationError, validate_and_project
from .deletion_v3_documents import (
    TaskDeletionTransactionError,
    filter_display,
    task_by_display,
)
from .task_deletion_plan import TaskDeletionPlan


_OPERATION_STRING_FIELDS: dict[str, tuple[str, ...]] = {
    "content_free_idempotency": ("key",),
    "remove_activity_event": ("event_id",),
    "remove_planning_event": ("fact_id",),
    "remove_reply": ("reply_id",),
    "remove_task_owned_note": ("source",),
    "remove_task_record": ("display_id", "uid"),
    "remove_work_session": ("work_date", "session_id"),
    "remove_worklog_entry": ("work_date",),
    "rewrite_note_links": ("note_id",),
    "rewrite_task_field": ("field", "subject_display_id"),
    "unlink_capture_action": ("action_id", "capture_id"),
    "unlink_capture_field": ("capture_id", "field"),
}
_OPERATION_INT_FIELDS: dict[str, tuple[str, ...]] = {
    "remove_task_owned_note": ("index",),
    "remove_worklog_entry": ("index",),
}


def _compact_planning_status(documents: dict[str, Any]) -> None:
    activity = documents["activity.json"]
    facts = list(activity.get("planning_status") or [])
    id_map: dict[str, str] = {}
    compacted: list[dict[str, Any]] = []
    for index, fact in enumerate(facts, start=1):
        if not isinstance(fact, dict):
            continue
        rewritten = copy.deepcopy(fact)
        old_id = str(rewritten.get("id", ""))
        new_id = "PS-{:06d}".format(index)
        id_map[old_id] = new_id
        rewritten["id"] = new_id
        previous = rewritten.get("previous_fact_id")
        if isinstance(previous, str) and previous in id_map:
            rewritten["previous_fact_id"] = id_map[previous]
        compacted.append(rewritten)
    activity["planning_status"] = compacted
    for task in documents["backlog.json"].get("tasks", []):
        if not isinstance(task, dict):
            continue
        fact_id = task.get("status_fact_id")
        if isinstance(fact_id, str) and fact_id in id_map:
            task["status_fact_id"] = id_map[fact_id]


def _apply_rewrite_task_fields(
    applied: dict[str, Any], ops: list[dict[str, Any]], plan: TaskDeletionPlan
) -> None:
    remove = plan.target_display_id
    for item in ops:
        if item.get("op") != "rewrite_task_field":
            continue
        task = task_by_display(
            applied["backlog.json"].get("tasks", []), str(item["subject_display_id"])
        )
        if task is None:
            raise TaskDeletionTransactionError("not_found", "not_found", 404)
        field = str(item["field"])
        if field == "parent_id":
            parent = task.get("parent_id")
            if isinstance(parent, str) and parent.strip().upper() == remove:
                task["parent_id"] = None
            continue
        current = task.get(field)
        if isinstance(current, list):
            task[field] = filter_display(current, str(item.get("remove", remove)))


def _apply_rewrite_note_links(
    applied: dict[str, Any], ops: list[dict[str, Any]], plan: TaskDeletionPlan
) -> None:
    remove = plan.target_display_id
    for item in ops:
        if item.get("op") != "rewrite_note_links":
            continue
        note_id = str(item["note_id"])
        for note in applied["notes.json"].get("notes", []):
            if not isinstance(note, dict) or str(note.get("id")) != note_id:
                continue
            links = note.get("links")
            if isinstance(links, list):
                note["links"] = filter_display(links, str(item.get("remove", remove)))


def _apply_unlink_capture_fields(
    applied: dict[str, Any], ops: list[dict[str, Any]], plan: TaskDeletionPlan
) -> None:
    remove = plan.target_display_id
    for item in ops:
        if item.get("op") != "unlink_capture_field":
            continue
        capture_id = str(item["capture_id"])
        field = str(item["field"])
        token = str(item.get("remove", remove))
        for capture in applied["captures.json"].get("captures", []):
            if not isinstance(capture, dict) or str(capture.get("id")) != capture_id:
                continue
            current = capture.get(field)
            if isinstance(current, list):
                capture[field] = filter_display(current, token)


def _apply_unlink_capture_actions(
    applied: dict[str, Any], ops: list[dict[str, Any]], plan: TaskDeletionPlan
) -> None:
    del plan
    for item in ops:
        if item.get("op") != "unlink_capture_action":
            continue
        capture_id = str(item["capture_id"])
        action_id = str(item["action_id"])
        for capture in applied["captures.json"].get("captures", []):
            if not isinstance(capture, dict) or str(capture.get("id")) != capture_id:
                continue
            normalized = capture.get("normalized")
            if not isinstance(normalized, dict):
                continue
            for action in normalized.get("action_items", []) or []:
                if isinstance(action, dict) and str(action.get("id")) == action_id:
                    action["task_id"] = None


def _pop_indexed_items(values: list[Any], indexes: list[int]) -> list[Any]:
    remaining = list(values)
    for index in sorted(set(indexes), reverse=True):
        if 0 <= index < len(remaining):
            remaining.pop(index)
    return remaining


def _apply_remove_worklog_entries(
    applied: dict[str, Any], ops: list[dict[str, Any]], plan: TaskDeletionPlan
) -> None:
    del plan
    worklog_indexes: dict[str, list[int]] = {}
    for item in ops:
        if item.get("op") != "remove_worklog_entry":
            continue
        worklog_indexes.setdefault(str(item["work_date"]), []).append(int(item["index"]))
    days = applied["worklog.json"].setdefault("days", {})
    for work_date, indexes in worklog_indexes.items():
        day = days.get(work_date)
        if not isinstance(day, dict):
            continue
        day["entries"] = _pop_indexed_items(list(day.get("entries") or []), indexes)


def _apply_remove_work_sessions(
    applied: dict[str, Any], ops: list[dict[str, Any]], plan: TaskDeletionPlan
) -> None:
    del plan
    days = applied["worklog.json"].setdefault("days", {})
    for item in ops:
        if item.get("op") != "remove_work_session":
            continue
        day = days.get(str(item["work_date"]))
        if not isinstance(day, dict):
            continue
        session_id = str(item["session_id"])
        day["sessions"] = _drop_records_by_id(day.get("sessions") or [], {session_id})


def _ids_for_op(ops: list[dict[str, Any]], op_name: str, key: str) -> set[str]:
    return {str(item[key]) for item in ops if item.get("op") == op_name}


def _drop_records_by_id(records: list[Any], ids: set[str]) -> list[Any]:
    return [
        record
        for record in records
        if not (isinstance(record, dict) and str(record.get("id")) in ids)
    ]


def _apply_remove_replies(
    applied: dict[str, Any], ops: list[dict[str, Any]], plan: TaskDeletionPlan
) -> None:
    del plan
    reply_ids = _ids_for_op(ops, "remove_reply", "reply_id")
    if reply_ids:
        applied["replies.json"]["replies"] = _drop_records_by_id(
            applied["replies.json"].get("replies", []), reply_ids
        )


def _apply_remove_activity_events(
    applied: dict[str, Any], ops: list[dict[str, Any]], plan: TaskDeletionPlan
) -> None:
    del plan
    event_ids = _ids_for_op(ops, "remove_activity_event", "event_id")
    if event_ids:
        applied["activity.json"]["activity"] = _drop_records_by_id(
            applied["activity.json"].get("activity", []), event_ids
        )


def _apply_remove_planning_events(
    applied: dict[str, Any], ops: list[dict[str, Any]], plan: TaskDeletionPlan
) -> None:
    del plan
    fact_ids = _ids_for_op(ops, "remove_planning_event", "fact_id")
    if fact_ids:
        applied["activity.json"]["planning_status"] = _drop_records_by_id(
            applied["activity.json"].get("planning_status", []), fact_ids
        )


def _apply_content_free_idempotency(
    applied: dict[str, Any], ops: list[dict[str, Any]], plan: TaskDeletionPlan
) -> None:
    del plan
    for item in ops:
        if item.get("op") != "content_free_idempotency":
            continue
        key = str(item["key"])
        ledger = applied["activity.json"].setdefault("idempotency", [])
        for index, entry in enumerate(ledger):
            if not isinstance(entry, dict) or str(entry.get("key")) != key:
                continue
            ledger[index] = {
                "created_at": entry.get("created_at"),
                "key": key,
                "method": entry.get("method"),
                "path": entry.get("path"),
                "request_digest": entry.get("request_digest"),
                "response_body": {"data": {"purged": True}},
                "response_status": entry.get("response_status"),
            }


def _apply_remove_task_owned_notes(
    applied: dict[str, Any], ops: list[dict[str, Any]], plan: TaskDeletionPlan
) -> None:
    note_indexes: list[int] = []
    for item in ops:
        if item.get("op") != "remove_task_owned_note":
            continue
        if item.get("source") != "task.notes":
            continue
        note_indexes.append(int(item["index"]))
    target_task = task_by_display(
        applied["backlog.json"].get("tasks", []), plan.target_display_id
    )
    if target_task is None or not note_indexes:
        return
    target_task["notes"] = _pop_indexed_items(
        list(target_task.get("notes") or []), note_indexes
    )


def _apply_remove_task_record(
    applied: dict[str, Any], ops: list[dict[str, Any]], plan: TaskDeletionPlan
) -> None:
    if not any(item.get("op") == "remove_task_record" for item in ops):
        return
    wanted = plan.target_display_id
    applied["backlog.json"]["tasks"] = [
        task
        for task in applied["backlog.json"].get("tasks", [])
        if not (
            isinstance(task, dict)
            and str(task.get("id", "")).strip().upper() == wanted
        )
    ]


def _require_valid_projection(applied: dict[str, Any]) -> None:
    try:
        validate_and_project(applied["backlog.json"], applied["activity.json"])
    except PlanningStatusValidationError as error:
        raise TaskDeletionTransactionError(
            "validation_failed", "validation_failed", 409
        ) from error


_APPLY_PHASES: tuple[
    Callable[[dict[str, Any], list[dict[str, Any]], TaskDeletionPlan], None],
    ...,
] = (
    _apply_rewrite_task_fields,
    _apply_rewrite_note_links,
    _apply_unlink_capture_fields,
    _apply_unlink_capture_actions,
    _apply_remove_worklog_entries,
    _apply_remove_work_sessions,
    _apply_remove_replies,
    _apply_remove_activity_events,
    _apply_remove_planning_events,
    _apply_content_free_idempotency,
    _apply_remove_task_owned_notes,
    _apply_remove_task_record,
)


def _invalid_plan() -> None:
    raise TaskDeletionTransactionError("invalid_plan", "invalid_plan", 400)


def _require_string_fields(item: Mapping[str, Any], keys: Sequence[str]) -> None:
    for key in keys:
        value = item.get(key)
        if not isinstance(value, str) or not value:
            _invalid_plan()


def _require_int_fields(item: Mapping[str, Any], keys: Sequence[str]) -> None:
    for key in keys:
        if type(item.get(key)) is not int:
            _invalid_plan()


def _require_one_operation(item: object) -> None:
    if not isinstance(item, Mapping):
        _invalid_plan()
        return
    op = item.get("op")
    if not isinstance(op, str) or not op.strip():
        _invalid_plan()
        return
    strings = _OPERATION_STRING_FIELDS.get(op)
    if strings is None:
        _invalid_plan()
        return
    _require_string_fields(item, strings)
    _require_int_fields(item, _OPERATION_INT_FIELDS.get(op, ()))
    if op == "remove_task_owned_note" and item.get("source") != "task.notes":
        _invalid_plan()


def _require_plan_operations(operations: Sequence[object]) -> None:
    for item in operations:
        _require_one_operation(item)


def apply_v3_task_deletion_plan(
    documents: Mapping[str, Any],
    plan: TaskDeletionPlan,
) -> dict[str, Any]:
    """Apply a v3 plan in memory. Does not touch the filesystem."""

    if plan.layout != "v3":
        raise TaskDeletionTransactionError("invalid_layout", "invalid_layout", 400)
    _require_plan_operations(plan.operations)
    applied = copy.deepcopy(dict(documents))
    ops = [dict(item) for item in plan.operations]
    applied["worklog.json"].setdefault("days", {})
    try:
        for apply_phase in _APPLY_PHASES:
            apply_phase(applied, ops, plan)
        _compact_planning_status(applied)
        _require_valid_projection(applied)
    except (KeyError, TypeError, ValueError) as error:
        raise TaskDeletionTransactionError("invalid_plan", "invalid_plan", 400) from error
    return applied
