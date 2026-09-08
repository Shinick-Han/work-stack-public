"""v3 permanent-delete preview and guarded Store transaction.

Preview is read-only. Commit takes one journaled ``save_many`` generation
after an exact backup under the existing ``.workstack.lock``. The pure planner
in ``task_deletion_plan`` is never given a filesystem or clock.

The document roster and refusal identity live in ``deletion_v3_documents``,
the in-memory phases in ``deletion_v3_apply``, and the backup plus preview
binding in ``deletion_v3_backup``. This module owns the transaction
boundaries themselves: what happens inside one ``store.transaction()``, and
in what order.
"""

from __future__ import annotations

import copy
import secrets
from typing import Any, Mapping

from .canonical import canonical_sha256
from .deletion_v3_apply import apply_v3_task_deletion_plan
from .deletion_v3_backup import (
    PREVIEW_TTL_SECONDS,
    backup_dir,
    bind_preview,
    previews,
    run_before_commit_fault,
    validate_token,
    write_exact_backup,
)
from .deletion_v3_documents import (
    V3_DOCUMENT_NAMES,
    TaskDeletionTransactionError,
    changed_writes,
    load_documents,
    preserve_high_water,
    raise_plan_refusal,
    store_digest_from_documents,
    task_by_display,
)
from .task_deletion_plan import (
    TaskDeletionPlan,
    TaskDeletionPlanError,
    plan_v3_task_deletion,
)

__all__ = [
    "PREVIEW_TTL_SECONDS",
    "V3_DOCUMENT_NAMES",
    "TaskDeletionTransactionError",
    "apply_v3_task_deletion_plan",
    "commit_v3_hard_delete",
    "commit_v3_task_deletion",
    "preview_v3_task_deletion",
    "store_digest_from_documents",
]


def _plan_v3(documents: Mapping[str, Any], task_id: str, expected_revision: int) -> TaskDeletionPlan:
    try:
        return plan_v3_task_deletion(
            documents, task_id=task_id, expected_revision=expected_revision
        )
    except TaskDeletionPlanError as error:
        raise_plan_refusal(error)
        raise


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


def _task_title(documents: Mapping[str, Any], task_id: str) -> str:
    task = task_by_display(documents["backlog.json"].get("tasks", []), task_id)
    if task is None:
        return ""
    title = task.get("title")
    return title if isinstance(title, str) else ""


def _require_preview_body(body: Mapping[str, Any]) -> tuple[int, str, str]:
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
    return revision, workspace_uid, client_request_id


def preview_v3_task_deletion(store: Any, task_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    revision, workspace_uid, client_request_id = _require_preview_body(body)
    with store.transaction():
        documents = load_documents(store)
        actual_workspace = documents["workspace.json"].get("id")
        if actual_workspace != workspace_uid:
            raise TaskDeletionTransactionError(
                "workspace_mismatch", "workspace_mismatch", 409
            )
        plan = _plan_v3(documents, task_id, revision)
        digest = store_digest_from_documents(documents)
        token = secrets.token_urlsafe(32)
        bind_preview(
            store,
            token,
            client_request_id=client_request_id,
            plan_digest=canonical_sha256(plan.to_mapping()),
            revision=revision,
            store_digest=digest,
            task_id=plan.target_display_id,
            task_uid=plan.target_uid,
            workspace_uid=workspace_uid,
        )
        counts = _preview_counts(plan)
        backup_location = str(backup_dir(store) / "pending")
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
    preserve_high_water(original, applied)
    if force_backup_failure:
        raise TaskDeletionTransactionError("backup_failed", "backup_failed", 409)
    backup = write_exact_backup(store, task_id, digest)
    run_before_commit_fault(store)
    writes = changed_writes(original, applied)
    if writes:
        store.save_many(writes, operation_id=operation_id)
    return backup


def _require_commit_body(body: Mapping[str, Any], task_id: str) -> str:
    if set(body) != {"preview_token", "confirm"}:
        raise TaskDeletionTransactionError("invalid_body", "invalid_body", 400)
    confirm = body.get("confirm")
    if not isinstance(confirm, str):
        raise TaskDeletionTransactionError("invalid_body", "invalid_body", 400)
    wanted = task_id.strip().upper()
    if confirm.strip().upper() != wanted:
        raise TaskDeletionTransactionError("confirm_mismatch", "confirm_mismatch", 400)
    return wanted


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
    wanted = _require_commit_body(body, task_id)
    matched_revision = _parse_if_match(if_match)
    with store.transaction():
        documents = load_documents(store)
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
        token = validate_token(
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
        high_water = preserve_high_water(documents, applied)
        if force_backup_failure:
            raise TaskDeletionTransactionError("backup_failed", "backup_failed", 409)
        backup = write_exact_backup(store, plan.target_display_id, digest)
        run_before_commit_fault(store)
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
        writes = changed_writes(documents, applied)
        if writes:
            store.save_many(
                writes,
                operation_id="v3-task-deletion-{}".format(digest[-16:]),
            )
        previews(store).pop(token, None)
        return receipt


def commit_v3_hard_delete(store: Any, task_id: str, expected_revision: int) -> None:
    wanted = task_id.strip().upper()
    with store.transaction():
        documents = load_documents(store)
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
