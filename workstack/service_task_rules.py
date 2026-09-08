"""Task creation and patch rules, decided before any document is written.

The patch path normalizes then validates: scalars, collections and the two
relationship fields each have one owner, and cycle detection runs over the
supplied Task index rather than a live read. The create path validates the
strict v1 body and builds the canonical new record. Nothing here touches a
Store, so the same rules serve the CLI, the browser API and the capture
conversions.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Iterable

from .outcome_write_invariant import (
    OutcomeWriteInvariantError,
    apply_task_outcome_write_invariant,
    canonicalize_key_result_refs,
)
from .service_domain import (
    KEY_RESULT_REF_FIELDS,
    PRIORITIES,
    TASK_CREATE_FIELDS,
    TASK_STATUSES,
    _required_text,
    _task_uid,
)
from .service_errors import DomainError


def _relationship_reaches(
    tasks_by_id: dict[str, dict[str, Any]],
    start_ids: Iterable[str],
    target_id: str,
    field: str,
) -> bool:
    """Return whether following one relationship kind reaches ``target_id``."""

    pending = list(start_ids)
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        task = tasks_by_id.get(current)
        if task is None:
            continue
        if field == "parent_id":
            parent = task.get("parent_id")
            if isinstance(parent, str) and parent:
                pending.append(parent)
        else:
            pending.extend(
                dependency
                for dependency in task.get("dependencies", [])
                if isinstance(dependency, str) and dependency
            )
    return False


def _validate_patch_local_date(value: Any, field: str) -> None:
    if value is None:
        return
    message = "{} must be an ISO date or null".format(field)
    if not isinstance(value, str):
        raise DomainError(message)
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise DomainError(message) from error
    if parsed.isoformat() != value:
        raise DomainError(message)


def _normalize_patch_title(changes: dict[str, Any]) -> None:
    if "title" not in changes:
        return
    if not isinstance(changes["title"], str):
        raise DomainError("title must be a string")
    changes["title"] = _required_text(changes["title"], "title")


def _normalize_patch_detail(changes: dict[str, Any]) -> None:
    if "detail" not in changes:
        return
    if not isinstance(changes["detail"], str):
        raise DomainError("detail must be a string")
    changes["detail"] = changes["detail"].strip()


def _validate_patch_enums(changes: dict[str, Any]) -> None:
    if "status" in changes and changes["status"] not in TASK_STATUSES:
        raise DomainError("invalid task status")
    if "priority" in changes and changes["priority"] not in PRIORITIES:
        raise DomainError("invalid task priority")


def _validate_patch_estimate(changes: dict[str, Any]) -> None:
    if "estimate_minutes" not in changes or changes["estimate_minutes"] is None:
        return
    estimate = changes["estimate_minutes"]
    if (
        not isinstance(estimate, int)
        or isinstance(estimate, bool)
        or not 1 <= estimate <= 1440
    ):
        raise DomainError(
            "estimate_minutes must be null or an integer from 1 to 1440"
        )


def _normalize_patch_scalar_fields(changes: dict[str, Any]) -> None:
    _normalize_patch_title(changes)
    _normalize_patch_detail(changes)
    _validate_patch_enums(changes)
    for field in ("due", "scheduled"):
        if field in changes:
            _validate_patch_local_date(changes[field], field)
    _validate_patch_estimate(changes)


def _require_patch_array(changes: dict[str, Any], field: str) -> None:
    if field in changes and not isinstance(changes[field], list):
        raise DomainError("{} must be an array".format(field))


def _normalize_patch_tags(changes: dict[str, Any]) -> None:
    if "tags" not in changes:
        return
    if any(not isinstance(item, str) for item in changes["tags"]):
        raise DomainError("tags entries must be strings")
    changes["tags"] = sorted(
        {item.strip() for item in changes["tags"] if item.strip()}
    )


def _normalize_patch_objectives(
    changes: dict[str, Any], objectives: set[str]
) -> None:
    if "objective_ids" not in changes:
        return
    if any(not isinstance(item, str) for item in changes["objective_ids"]):
        raise DomainError("objective_ids entries must be strings")
    changes["objective_ids"] = sorted(
        {item.strip().upper() for item in changes["objective_ids"] if item.strip()}
    )
    missing = sorted(set(changes["objective_ids"]) - objectives)
    if missing:
        raise DomainError("unknown objective ids", {"ids": missing})


def _normalized_ref_id(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise DomainError("key_result_refs {} must be a string".format(field))
    normalized = value.strip().upper()
    if not normalized:
        raise DomainError("key_result_refs {} must not be blank".format(field))
    return normalized


def _normalized_key_result_ref(item: Any) -> dict[str, str]:
    if not isinstance(item, dict) or set(item) != KEY_RESULT_REF_FIELDS:
        raise DomainError("key_result_refs entries must be exact scoped pairs")
    return {
        "objective_id": _normalized_ref_id(item["objective_id"], "objective_id"),
        "key_result_id": _normalized_ref_id(item["key_result_id"], "key_result_id"),
    }


def _normalize_patch_key_result_refs(changes: dict[str, Any]) -> None:
    if "key_result_refs" not in changes:
        return
    changes["key_result_refs"] = canonicalize_key_result_refs(
        [_normalized_key_result_ref(item) for item in changes["key_result_refs"]]
    )


def _apply_task_outcome_write_invariant(
    changes: dict[str, Any],
    task: dict[str, Any],
    objectives_by_id: dict[str, list[dict[str, Any]]],
) -> None:
    """KR parent auto-alignment and fail-closed roster checks before any write."""

    try:
        apply_task_outcome_write_invariant(changes, task, objectives_by_id)
    except OutcomeWriteInvariantError as exc:
        raise DomainError(str(exc), exc.details) from exc


def _objective_records_by_id(
    records: Iterable[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Group Objective records by ID, preserving duplicate multiplicity."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["id"], []).append(record)
    return grouped


def _normalize_patch_dependencies(changes: dict[str, Any]) -> None:
    if "dependencies" not in changes:
        return
    if any(not isinstance(item, str) for item in changes["dependencies"]):
        raise DomainError("dependencies entries must be strings")
    changes["dependencies"] = sorted(
        {item.strip().upper() for item in changes["dependencies"] if item.strip()}
    )


def _normalize_patch_collection_fields(
    changes: dict[str, Any], objectives: set[str]
) -> None:
    for field in ("tags", "objective_ids", "dependencies", "key_result_refs"):
        _require_patch_array(changes, field)
    _normalize_patch_tags(changes)
    _normalize_patch_objectives(changes, objectives)
    _normalize_patch_dependencies(changes)
    _normalize_patch_key_result_refs(changes)


def _validate_patch_dependencies(
    changes: dict[str, Any],
    task: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
) -> None:
    if "dependencies" not in changes:
        return
    missing = sorted(set(changes["dependencies"]) - set(tasks_by_id))
    if missing or task["id"] in changes["dependencies"]:
        raise DomainError("invalid dependency ids", {"ids": missing})
    cyclic_dependencies = [
        dependency
        for dependency in changes["dependencies"]
        if _relationship_reaches(
            tasks_by_id, [dependency], task["id"], "dependencies"
        )
    ]
    if cyclic_dependencies:
        raise DomainError(
            "dependency relationship would create a cycle",
            {"ids": cyclic_dependencies},
        )


def _normalize_and_validate_patch_parent(
    changes: dict[str, Any],
    task: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
) -> None:
    if "parent_id" not in changes:
        return
    parent = changes["parent_id"]
    if parent is not None and not isinstance(parent, str):
        raise DomainError("parent_id must be a task ID or null")
    parent = parent.strip().upper() if parent else None
    if parent == task["id"] or (parent and parent not in tasks_by_id):
        raise DomainError("invalid parent task")
    if parent and _relationship_reaches(
        tasks_by_id, [parent], task["id"], "parent_id"
    ):
        raise DomainError(
            "parent relationship would create a cycle",
            {"id": parent},
        )
    changes["parent_id"] = parent


def _normalize_and_validate_patch_relationships(
    changes: dict[str, Any],
    task: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
) -> None:
    _validate_patch_dependencies(changes, task, tasks_by_id)
    _normalize_and_validate_patch_parent(changes, task, tasks_by_id)


def _patch_change_set(
    patch: dict[str, Any],
    task: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
    objectives: set[str],
) -> tuple[dict[str, Any], Any]:
    changes = {key: value for key, value in patch.items() if key != "revision"}
    _normalize_patch_scalar_fields(changes)
    _normalize_patch_collection_fields(changes, objectives)
    _normalize_and_validate_patch_relationships(changes, task, tasks_by_id)
    return changes, changes.pop("status", None)


def _patch_changed_fields(
    changes: dict[str, Any], requested_status: Any, current_status: str
) -> list[str]:
    fields = sorted(changes)
    if requested_status is not None and requested_status != current_status:
        fields.append("status")
    return fields


def _validate_new_task_schedule(
    priority: str,
    due: str | None,
    scheduled: str | None,
    estimate_minutes: int | None,
) -> None:
    if priority not in PRIORITIES:
        raise ValueError("priority must be one of {}".format(", ".join(PRIORITIES)))
    if due:
        dt.date.fromisoformat(due)
    if scheduled:
        dt.date.fromisoformat(scheduled)
    if estimate_minutes is not None and (
        not isinstance(estimate_minutes, int)
        or isinstance(estimate_minutes, bool)
        or not 1 <= estimate_minutes <= 1440
    ):
        raise ValueError("estimate_minutes must be null or an integer from 1 to 1440")


def _normalize_new_task_relationships(
    tasks: list[dict[str, Any]],
    parent_id: str | None,
    dependencies: Iterable[str],
) -> tuple[str | None, list[str]]:
    known_tasks = {item["id"] for item in tasks}
    normalized_parent = parent_id.strip().upper() if parent_id else None
    normalized_dependencies = sorted(
        set(str(item).strip().upper() for item in dependencies if str(item).strip())
    )
    referenced = ({normalized_parent} if normalized_parent else set()) | set(
        normalized_dependencies
    )
    unknown_tasks = sorted(item for item in referenced if item not in known_tasks)
    if unknown_tasks:
        raise ValueError("unknown task ids: {}".format(", ".join(unknown_tasks)))
    return normalized_parent, normalized_dependencies


def _new_task_record(
    *,
    day: Callable[[], str],
    task_id: str,
    workspace_id: str,
    title: str,
    detail: str,
    priority: str,
    due: str | None,
    scheduled: str | None,
    estimate_minutes: int | None,
    tags: Iterable[str],
    objective_ids: Iterable[str],
    parent_id: str | None,
    dependencies: list[str],
) -> dict[str, Any]:
    """The canonical new Task record.

    ``day`` is the caller's local-day reader, not a value: ``created`` and
    ``updated_at`` have always been two INDEPENDENT clock reads, and an owner
    contract proves a create spanning midnight keeps both.
    """

    return {
        "id": task_id,
        "uid": _task_uid(workspace_id, task_id),
        "title": _required_text(title, "title"),
        "detail": str(detail or "").strip(),
        "status": "open",
        "priority": priority,
        "due": due or None,
        "scheduled": scheduled or None,
        "estimate_minutes": estimate_minutes,
        "tags": sorted(set(str(tag).strip() for tag in tags if str(tag).strip())),
        "objective_ids": sorted(
            set(str(oid).strip().upper() for oid in objective_ids if str(oid).strip())
        ),
        "parent_id": parent_id,
        "dependencies": dependencies,
        "subtasks": [],
        "notes": [],
        "created": day(),
        "updated_at": day(),
        "revision": 0,
    }


def _validate_task_create_shape(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise DomainError("request body must be a JSON object")
    unknown = sorted(set(body) - TASK_CREATE_FIELDS)
    if unknown:
        raise DomainError("task create has unknown fields", {"fields": unknown})
    return body


def _task_create_text_fields(body: dict[str, Any]) -> tuple[str, str, str]:
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise DomainError("title must be a non-empty string", {"field": "title"})
    detail = body.get("detail", "")
    if not isinstance(detail, str):
        raise DomainError("detail must be a string", {"field": "detail"})
    priority = body.get("priority", "P2")
    if not isinstance(priority, str) or priority not in PRIORITIES:
        raise DomainError(
            "priority must be one of {}".format(", ".join(PRIORITIES)),
            {"field": "priority"},
        )
    return title.strip(), detail.strip(), priority


def _task_create_date(body: dict[str, Any], field: str) -> str | None:
    value = body.get(field)
    if value is None:
        return None
    message = "{} must be null or YYYY-MM-DD".format(field)
    if not isinstance(value, str):
        raise DomainError(message, {"field": field})
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise DomainError(message, {"field": field}) from error
    if parsed.isoformat() != value:
        raise DomainError(message, {"field": field})
    return value


def _task_create_estimate(body: dict[str, Any]) -> int | None:
    value = body.get("estimate_minutes")
    if value is not None and (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 1440
    ):
        raise DomainError(
            "estimate_minutes must be null or an integer from 1 to 1440",
            {"field": "estimate_minutes"},
        )
    return value


def _task_create_string_list(
    body: dict[str, Any], field: str, *, uppercase: bool = False
) -> list[str]:
    values = body.get(field, [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise DomainError("{} must be an array of strings".format(field), {"field": field})
    normalized = (value.strip() for value in values)
    if uppercase:
        normalized = (value.upper() for value in normalized)
    return sorted(set(value for value in normalized if value))
