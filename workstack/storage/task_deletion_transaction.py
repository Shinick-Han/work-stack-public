"""v3 permanent-delete preview and guarded Store transaction.

Preview is read-only. Commit takes one journaled ``save_many`` generation
after an exact backup under the existing ``.workstack.lock``. The pure planner
in ``task_deletion_plan`` is never given a filesystem or clock.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import io
import secrets
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..planning_status import PlanningStatusValidationError, validate_and_project
from ..task_display_id import (
    TaskDisplayIdError,
    admitted_high_water,
    persist_field,
    read_optional_high_water,
    task_ids_from_records,
)
from .canonical import canonical_json_bytes, canonical_sha256
from .task_deletion_plan import (
    TaskDeletionPlan,
    TaskDeletionPlanError,
    plan_v3_task_deletion,
)

V3_DOCUMENT_NAMES = (
    "workspace.json",
    "backlog.json",
    "store-meta.json",
    "okr.json",
    "worklog.json",
    "notes.json",
    "captures.json",
    "replies.json",
    "activity.json",
)
PREVIEW_TTL_SECONDS = 900
_PREVIEW_ATTR = "_v3_deletion_previews"
_FAULT_ATTR = "_v3_deletion_before_commit"


class TaskDeletionTransactionError(Exception):
    """Content-free HTTP/repository refusal at the v3 deletion boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


def _previews(store: Any) -> dict[str, dict[str, Any]]:
    bucket = getattr(store, _PREVIEW_ATTR, None)
    if bucket is None:
        bucket = {}
        setattr(store, _PREVIEW_ATTR, bucket)
    return bucket


def _load_documents(store: Any) -> dict[str, dict[str, Any]]:
    return {name: store.load(name) for name in V3_DOCUMENT_NAMES}


def _preserve_high_water(
    original: Mapping[str, Any], applied: dict[str, Any]
) -> int:
    try:
        water = admitted_high_water(
            read_optional_high_water(original["workspace.json"]),
            task_ids_from_records(original["backlog.json"].get("tasks", [])),
        )
    except TaskDisplayIdError as error:
        raise TaskDeletionTransactionError(error.code, error.code, 400) from error
    persist_field(applied["workspace.json"], water)
    return water


def store_digest_from_documents(documents: Mapping[str, Any]) -> str:
    payload = {
        name: canonical_sha256(documents[name])
        for name in V3_DOCUMENT_NAMES
        if name in documents
    }
    return canonical_sha256(payload)


def _raise_plan(error: TaskDeletionPlanError) -> None:
    mapping = {
        "not_found": ("not_found", "not_found", 404),
        "revision_mismatch": ("revision_conflict", "revision_conflict", 409),
        "unknown_unsafe_reference": (
            "unknown_unsafe_reference",
            "unknown_unsafe_reference",
            409,
        ),
        "malformed_reference_container": (
            "malformed_reference_container",
            "malformed_reference_container",
            400,
        ),
        "duplicate_identity": ("duplicate_identity", "duplicate_identity", 409),
    }
    code, message, status = mapping.get(error.code, (error.code, error.code, 400))
    raise TaskDeletionTransactionError(code, message, status) from error


def _plan_v3(documents: Mapping[str, Any], task_id: str, expected_revision: int) -> TaskDeletionPlan:
    try:
        return plan_v3_task_deletion(
            documents, task_id=task_id, expected_revision=expected_revision
        )
    except TaskDeletionPlanError as error:
        _raise_plan(error)
        raise


def _filter_display(values: list[Any], remove: str) -> list[Any]:
    target = remove.strip().upper()
    return [
        item
        for item in values
        if not (isinstance(item, str) and item.strip().upper() == target)
    ]


def _task_by_display(tasks: list[Any], display_id: str) -> dict[str, Any] | None:
    wanted = display_id.strip().upper()
    for task in tasks:
        if isinstance(task, dict) and str(task.get("id", "")).strip().upper() == wanted:
            return task
    return None


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
        task = _task_by_display(
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
            task[field] = _filter_display(current, str(item.get("remove", remove)))


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
                note["links"] = _filter_display(links, str(item.get("remove", remove)))


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
                capture[field] = _filter_display(current, token)


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
    target_task = _task_by_display(
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


def _preview_counts(plan: TaskDeletionPlan) -> dict[str, Any]:
    owned = {
        "activity_events": 0,
        "idempotency_keys": 0,
        "notes": 0,
        "planning_events": 0,
        "replies": 0,
        "work_sessions": 0,
        "worklog_entries": 0,
    }
    notes: list[str] = []
    captures: list[str] = []
    actions = 0
    for item in plan.operations:
        op = str(item["op"])
        if op == "remove_task_owned_note":
            owned["notes"] += 1
        elif op == "remove_reply":
            owned["replies"] += 1
        elif op == "remove_worklog_entry":
            owned["worklog_entries"] += 1
        elif op == "remove_work_session":
            owned["work_sessions"] += 1
        elif op == "remove_activity_event":
            owned["activity_events"] += 1
        elif op == "remove_planning_event":
            owned["planning_events"] += 1
        elif op == "content_free_idempotency":
            owned["idempotency_keys"] += 1
        elif op == "rewrite_note_links":
            notes.append(str(item["note_id"]))
        elif op == "unlink_capture_field":
            captures.append(str(item["capture_id"]))
        elif op == "unlink_capture_action":
            captures.append(str(item["capture_id"]))
            actions += 1
    preview = plan.preview()
    return {
        "modified_references": {
            "notes": sorted(set(notes)),
            "tasks": list(preview["rewritten_task_display_ids"]),
        },
        "removed_task_owned_records": owned,
        "unlinked_captures": {
            "actions": actions,
            "captures": sorted(set(captures)),
        },
    }


def _backup_dir(store: Any) -> Path:
    return Path(store.runtime_root) / "task-deletion-backups"


def _write_exact_backup(store: Any, task_id: str, digest: str) -> dict[str, str]:
    directory = _backup_dir(store)
    directory.mkdir(parents=True, exist_ok=True)
    filename = "task-deletion-{}-{}.zip".format(task_id, digest[-16:])
    destination = directory / filename
    buffer = io.BytesIO()
    bodies: dict[str, bytes] = {}
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in V3_DOCUMENT_NAMES:
            body = store.path(name).read_bytes()
            bodies[name] = body
            archive.writestr(name, body)
    payload = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        for name, body in bodies.items():
            if archive.read(name) != body:
                raise TaskDeletionTransactionError("backup_failed", "backup_failed", 409)
    destination.write_bytes(payload)
    return {
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "location": str(destination),
    }


def _task_title(documents: Mapping[str, Any], task_id: str) -> str:
    task = _task_by_display(documents["backlog.json"].get("tasks", []), task_id)
    if task is None:
        return ""
    title = task.get("title")
    return title if isinstance(title, str) else ""


def preview_v3_task_deletion(store: Any, task_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    if set(body) != {"revision", "workspace_uid", "client_request_id"}:
        raise TaskDeletionTransactionError("invalid_body", "invalid_body", 400)
    revision = body.get("revision")
    workspace_uid = body.get("workspace_uid")
    client_request_id = body.get("client_request_id")
    if type(revision) is not int:
        raise TaskDeletionTransactionError("invalid_body", "invalid_body", 400)
    if not isinstance(workspace_uid, str) or not workspace_uid:
        raise TaskDeletionTransactionError("invalid_body", "invalid_body", 400)
    if not isinstance(client_request_id, str) or not client_request_id:
        raise TaskDeletionTransactionError("invalid_body", "invalid_body", 400)
    with store.transaction():
        documents = _load_documents(store)
        actual_workspace = documents["workspace.json"].get("id")
        if actual_workspace != workspace_uid:
            raise TaskDeletionTransactionError(
                "workspace_mismatch", "workspace_mismatch", 409
            )
        plan = _plan_v3(documents, task_id, revision)
        digest = store_digest_from_documents(documents)
        token = secrets.token_urlsafe(32)
        binding = {
            "client_request_id": client_request_id,
            "expires_at": time.time() + PREVIEW_TTL_SECONDS,
            "plan_digest": canonical_sha256(plan.to_mapping()),
            "revision": revision,
            "store_digest": digest,
            "task_id": plan.target_display_id,
            "task_uid": plan.target_uid,
            "workspace_uid": workspace_uid,
        }
        _previews(store)[token] = binding
        counts = _preview_counts(plan)
        backup_location = str(_backup_dir(store) / "pending")
        return {
            "backup": {
                "created": False,
                "location": backup_location,
                "retention": "retain-until-operator-purge",
            },
            "modified_references": counts["modified_references"],
            "preview_token": token,
            "removed_task_owned_records": counts["removed_task_owned_records"],
            "store_digest": digest,
            "task": {
                "id": plan.target_display_id,
                "revision": plan.expected_revision,
                "title": _task_title(documents, plan.target_display_id),
                "uid": plan.target_uid,
            },
            "unlinked_captures": counts["unlinked_captures"],
        }


def _idempotency_replay(
    documents: Mapping[str, Any],
    *,
    key: str,
    method: str,
    path: str,
    request_digest: str,
) -> dict[str, Any] | None:
    for record in documents["activity.json"].get("idempotency", []) or []:
        if not isinstance(record, dict) or record.get("key") != key:
            continue
        if (
            record.get("method") != method
            or record.get("path") != path
            or record.get("request_digest") != request_digest
        ):
            raise TaskDeletionTransactionError(
                "idempotency_conflict", "idempotency_conflict", 409, {"key": key}
            )
        body = record.get("response_body")
        if not isinstance(body, dict) or "data" not in body:
            raise TaskDeletionTransactionError(
                "idempotency_conflict", "idempotency_conflict", 409, {"key": key}
            )
        payload = body["data"]
        if not isinstance(payload, dict):
            raise TaskDeletionTransactionError(
                "idempotency_conflict", "idempotency_conflict", 409, {"key": key}
            )
        return copy.deepcopy(payload)
    return None


def _record_idempotency(
    documents: dict[str, Any],
    *,
    key: str,
    method: str,
    path: str,
    request_digest: str,
    receipt: Mapping[str, Any],
) -> None:
    ledger = documents["activity.json"].setdefault("idempotency", [])
    ledger.append(
        {
            "created_at": "1970-01-01T00:00:00Z",
            "key": key,
            "method": method,
            "path": path,
            "request_digest": request_digest,
            "response_body": {"data": copy.deepcopy(dict(receipt))},
            "response_status": 200,
        }
    )


def _parse_if_match(if_match: str | None) -> int:
    if if_match is None or not str(if_match).strip():
        raise TaskDeletionTransactionError("if_match_required", "if_match_required", 400)
    raw = str(if_match).strip()
    if not raw.isdigit() and not (raw.startswith("-") and raw[1:].isdigit()):
        raise TaskDeletionTransactionError("invalid_if_match", "invalid_if_match", 400)
    try:
        return int(raw)
    except ValueError as error:
        raise TaskDeletionTransactionError("invalid_if_match", "invalid_if_match", 400) from error


def _changed_writes(
    original: Mapping[str, Any], applied: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    writes: dict[str, dict[str, Any]] = {}
    for name in V3_DOCUMENT_NAMES:
        if canonical_json_bytes(applied[name]) != canonical_json_bytes(original[name]):
            writes[name] = applied[name]
    return writes


def _validate_token(
    store: Any,
    token: object,
    *,
    task_id: str,
    task_uid: str,
    revision: int,
    workspace_uid: str,
    store_digest: str,
    plan_digest: str,
) -> str:
    if not isinstance(token, str) or not token:
        raise TaskDeletionTransactionError(
            "invalid_preview_token",
            "invalid_preview_token",
            409,
            {"preview_token": True},
        )
    binding = _previews(store).get(token)
    if binding is None:
        raise TaskDeletionTransactionError(
            "invalid_preview_token",
            "invalid_preview_token",
            409,
            {"preview_token": True},
        )
    if float(binding.get("expires_at", 0)) < time.time():
        raise TaskDeletionTransactionError("preview_expired", "preview_expired", 409)
    expected = {
        "task_id": task_id,
        "task_uid": task_uid,
        "revision": revision,
        "workspace_uid": workspace_uid,
        "store_digest": store_digest,
        "plan_digest": plan_digest,
    }
    for field, value in expected.items():
        actual = binding.get(field)
        if isinstance(actual, str) and isinstance(value, str):
            if not hmac.compare_digest(actual, value):
                raise TaskDeletionTransactionError("preview_stale", "preview_stale", 409)
        elif actual != value:
            raise TaskDeletionTransactionError("preview_stale", "preview_stale", 409)
    return token


def _run_backup_then_commit(
    store: Any,
    *,
    original: Mapping[str, Any],
    applied: dict[str, Any],
    task_id: str,
    digest: str,
    force_backup_failure: bool,
    operation_id: str,
) -> dict[str, str]:
    _preserve_high_water(original, applied)
    if force_backup_failure:
        raise TaskDeletionTransactionError("backup_failed", "backup_failed", 409)
    backup = _write_exact_backup(store, task_id, digest)
    fault: Callable[[], None] | None = getattr(store, _FAULT_ATTR, None)
    if callable(fault):
        fault()
    writes = _changed_writes(original, applied)
    if writes:
        store.save_many(writes, operation_id=operation_id)
    return backup


def commit_v3_task_deletion(
    store: Any,
    *,
    task_id: str,
    body: Mapping[str, Any],
    request_digest: str,
    path: str,
    idempotency_key: str,
    if_match: str | None,
    force_backup_failure: bool = False,
) -> dict[str, Any]:
    if set(body) != {"preview_token", "confirm"}:
        raise TaskDeletionTransactionError("invalid_body", "invalid_body", 400)
    confirm = body.get("confirm")
    if not isinstance(confirm, str):
        raise TaskDeletionTransactionError("invalid_body", "invalid_body", 400)
    wanted = task_id.strip().upper()
    if confirm.strip().upper() != wanted:
        raise TaskDeletionTransactionError("confirm_mismatch", "confirm_mismatch", 400)
    matched_revision = _parse_if_match(if_match)
    with store.transaction():
        documents = _load_documents(store)
        replay = _idempotency_replay(
            documents,
            key=idempotency_key,
            method="DELETE",
            path=path,
            request_digest=request_digest,
        )
        if replay is not None:
            return replay
        plan = _plan_v3(documents, wanted, matched_revision)
        digest = store_digest_from_documents(documents)
        plan_digest = canonical_sha256(plan.to_mapping())
        workspace_uid = str(documents["workspace.json"].get("id", ""))
        token = _validate_token(
            store,
            body.get("preview_token"),
            task_id=plan.target_display_id,
            task_uid=plan.target_uid,
            revision=plan.expected_revision,
            workspace_uid=workspace_uid,
            store_digest=digest,
            plan_digest=plan_digest,
        )
        applied = apply_v3_task_deletion_plan(documents, plan)
        high_water = _preserve_high_water(documents, applied)
        if force_backup_failure:
            raise TaskDeletionTransactionError("backup_failed", "backup_failed", 409)
        backup = _write_exact_backup(store, plan.target_display_id, digest)
        fault: Callable[[], None] | None = getattr(store, _FAULT_ATTR, None)
        if callable(fault):
            fault()
        receipt = {
            "backup": backup,
            "deleted": True,
            "display_id_high_water": high_water,
            "generation": int(store.generation) + 1,
            "revision": plan.expected_revision,
            "task_id": plan.target_display_id,
            "task_uid": plan.target_uid,
        }
        _record_idempotency(
            applied,
            key=idempotency_key,
            method="DELETE",
            path=path,
            request_digest=request_digest,
            receipt=receipt,
        )
        writes = _changed_writes(documents, applied)
        if writes:
            store.save_many(
                writes,
                operation_id="v3-task-deletion-{}".format(digest[-16:]),
            )
        _previews(store).pop(token, None)
        return receipt


def commit_v3_hard_delete(store: Any, task_id: str, expected_revision: int) -> None:
    wanted = task_id.strip().upper()
    with store.transaction():
        documents = _load_documents(store)
        plan = _plan_v3(documents, wanted, expected_revision)
        digest = store_digest_from_documents(documents)
        applied = apply_v3_task_deletion_plan(documents, plan)
        _run_backup_then_commit(
            store,
            original=documents,
            applied=applied,
            task_id=plan.target_display_id,
            digest=digest,
            force_backup_failure=False,
            operation_id="v3-task-hard-delete-{}".format(digest[-16:]),
        )
