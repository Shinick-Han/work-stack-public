"""v3 deletion refusal identity and whole-document arithmetic.

Holds the roster of legacy documents one deletion generation touches, the
content-free transaction refusal, the planner-to-transaction refusal mapping,
and the pure document helpers (digest, changed-write diff, high-water
preservation, display-id lookup) shared by the apply phases and the guarded
transaction. Loading and saving stay with the caller's ``Store``.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..task_display_id import (
    TaskDisplayIdError,
    admitted_high_water,
    persist_field,
    read_optional_high_water,
    task_ids_from_records,
)
from .canonical import canonical_json_bytes, canonical_sha256
from .deletion_plan_primitives import TaskDeletionPlanError


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

_PLAN_REFUSALS: dict[str, tuple[str, str, int]] = {
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


def raise_plan_refusal(error: TaskDeletionPlanError) -> None:
    code, message, status = _PLAN_REFUSALS.get(
        error.code, (error.code, error.code, 400)
    )
    raise TaskDeletionTransactionError(code, message, status) from error


def load_documents(store: Any) -> dict[str, dict[str, Any]]:
    return {name: store.load(name) for name in V3_DOCUMENT_NAMES}


def store_digest_from_documents(documents: Mapping[str, Any]) -> str:
    payload = {
        name: canonical_sha256(documents[name])
        for name in V3_DOCUMENT_NAMES
        if name in documents
    }
    return canonical_sha256(payload)


def changed_writes(
    original: Mapping[str, Any], applied: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    writes: dict[str, dict[str, Any]] = {}
    for name in V3_DOCUMENT_NAMES:
        if canonical_json_bytes(applied[name]) != canonical_json_bytes(original[name]):
            writes[name] = applied[name]
    return writes


def preserve_high_water(original: Mapping[str, Any], applied: dict[str, Any]) -> int:
    try:
        water = admitted_high_water(
            read_optional_high_water(original["workspace.json"]),
            task_ids_from_records(original["backlog.json"].get("tasks", [])),
        )
    except TaskDisplayIdError as error:
        raise TaskDeletionTransactionError(error.code, error.code, 400) from error
    persist_field(applied["workspace.json"], water)
    return water


def filter_display(values: list[Any], remove: str) -> list[Any]:
    target = remove.strip().upper()
    return [
        item
        for item in values
        if not (isinstance(item, str) and item.strip().upper() == target)
    ]


def task_by_display(tasks: list[Any], display_id: str) -> dict[str, Any] | None:
    wanted = display_id.strip().upper()
    for task in tasks:
        if isinstance(task, dict) and str(task.get("id", "")).strip().upper() == wanted:
            return task
    return None
