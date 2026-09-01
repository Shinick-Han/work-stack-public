"""Append-only Work Stack planning-status facts and deterministic projection."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any


MAX_REVISION = 9_007_199_254_740_991
TASK_STATUSES = ("open", "started", "done", "dropped")
FACT_TYPE = "task.planning_status"
FACT_FIELDS = {
    "id",
    "type",
    "task_id",
    "task_uid",
    "previous_fact_id",
    "prior_revision",
    "new_revision",
    "prior_status",
    "status",
    "created_at",
    "actor",
    "provenance",
}
ALLOWED_PROVENANCE = {
    "workstack.migration": {"store.v1", "store.v2"},
    "workstack.seed": {"demo.fixture"},
    "local.user": {"cli", "api.v1", "api.legacy"},
    "workstack.capture": {"api.v1.capture"},
}
FACT_ID_RE = re.compile(r"^PS-([0-9]{6,})$")
UTC_SECOND_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class PlanningStatusValidationError(ValueError):
    """Raised when the persisted fact graph is not a complete accepted history."""


def _revision(value: Any, label: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise PlanningStatusValidationError("{} is invalid".format(label))
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not UTC_SECOND_RE.fullmatch(value):
        raise PlanningStatusValidationError("planning status created_at is invalid")
    try:
        dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise PlanningStatusValidationError("planning status created_at is invalid") from error
    return value


def next_fact_id(facts: list[dict[str, Any]]) -> str:
    return "PS-{:06d}".format(len(facts) + 1)


def append_bootstrap(
    activity: dict[str, Any],
    task: dict[str, Any],
    *,
    created_at: str,
    actor: str,
    provenance: str,
) -> dict[str, Any]:
    facts = activity.setdefault("planning_status", [])
    fact = {
        "id": next_fact_id(facts),
        "type": FACT_TYPE,
        "task_id": task["id"],
        "task_uid": task["uid"],
        "previous_fact_id": None,
        "prior_revision": None,
        "new_revision": task["revision"],
        "prior_status": None,
        "status": task["status"],
        "created_at": created_at,
        "actor": actor,
        "provenance": provenance,
    }
    facts.append(fact)
    task["status_fact_id"] = fact["id"]
    return fact


def append_transition(
    activity: dict[str, Any],
    task: dict[str, Any],
    *,
    prior_status: str,
    status: str,
    prior_revision: int,
    new_revision: int,
    created_at: str,
    actor: str,
    provenance: str,
) -> dict[str, Any]:
    facts = activity.setdefault("planning_status", [])
    fact = {
        "id": next_fact_id(facts),
        "type": FACT_TYPE,
        "task_id": task["id"],
        "task_uid": task["uid"],
        "previous_fact_id": task["status_fact_id"],
        "prior_revision": prior_revision,
        "new_revision": new_revision,
        "prior_status": prior_status,
        "status": status,
        "created_at": created_at,
        "actor": actor,
        "provenance": provenance,
    }
    facts.append(fact)
    task["status_fact_id"] = fact["id"]
    return fact


def _task_index(backlog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tasks = backlog.get("tasks")
    if not isinstance(tasks, list):
        raise PlanningStatusValidationError("planning status store shape is invalid")
    indexed: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            raise PlanningStatusValidationError("planning status task reference is invalid")
        indexed[task["id"]] = task
    return indexed


def _validate_fact_envelope(fact: Any, index: int) -> dict[str, Any]:
    if not isinstance(fact, dict) or set(fact) != FACT_FIELDS:
        raise PlanningStatusValidationError("planning status fact schema is invalid")
    match = FACT_ID_RE.fullmatch(str(fact.get("id", "")))
    if match is None or int(match.group(1)) != index:
        raise PlanningStatusValidationError("planning status fact order is invalid")
    if fact.get("type") != FACT_TYPE:
        raise PlanningStatusValidationError("planning status fact type is invalid")
    return fact


def _fact_task_and_status(
    fact: dict[str, Any], task_by_id: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any], str]:
    task_id = fact.get("task_id")
    task = task_by_id.get(task_id) if isinstance(task_id, str) else None
    if task is None or fact.get("task_uid") != task.get("uid"):
        raise PlanningStatusValidationError("planning status task identity is invalid")
    status = fact.get("status")
    if status not in TASK_STATUSES:
        raise PlanningStatusValidationError("planning status value is invalid")
    return task_id, task, status


def _validate_fact_provenance(fact: dict[str, Any]) -> None:
    _timestamp(fact.get("created_at"))
    actor = fact.get("actor")
    provenance = fact.get("provenance")
    if not isinstance(actor, str) or provenance not in ALLOWED_PROVENANCE.get(actor, set()):
        raise PlanningStatusValidationError("planning status provenance is invalid")


def _validate_bootstrap(
    fact: dict[str, Any], task: dict[str, Any], status: str
) -> int:
    if (
        fact.get("previous_fact_id") is not None
        or fact.get("prior_revision") is not None
        or fact.get("prior_status") is not None
    ):
        raise PlanningStatusValidationError("planning status bootstrap is invalid")
    new_revision = _revision(fact.get("new_revision"), "bootstrap new_revision")
    if status != task.get("status"):
        raise PlanningStatusValidationError("planning status baseline does not match source")
    return new_revision


def _validate_transition(
    fact: dict[str, Any], previous: dict[str, Any], status: str
) -> int:
    if fact.get("previous_fact_id") != previous["id"]:
        raise PlanningStatusValidationError("planning status predecessor is invalid")
    prior_revision = _revision(fact.get("prior_revision"), "prior_revision")
    new_revision = _revision(fact.get("new_revision"), "new_revision")
    if (
        new_revision != prior_revision + 1
        or prior_revision < previous["new_revision"]
        or fact.get("prior_status") != previous["status"]
        or status == previous["status"]
    ):
        raise PlanningStatusValidationError("planning status transition is invalid")
    return new_revision


def _validate_fact_chain(
    fact: dict[str, Any],
    previous: dict[str, Any] | None,
    task: dict[str, Any],
    status: str,
) -> int:
    new_revision = (
        _validate_bootstrap(fact, task, status)
        if previous is None
        else _validate_transition(fact, previous, status)
    )
    if new_revision > task.get("revision", -1):
        raise PlanningStatusValidationError("planning status fact exceeds task revision")
    return new_revision


def _project_heads(
    task_by_id: dict[str, dict[str, Any]], heads: dict[str, dict[str, Any]]
) -> dict[str, str]:
    projection: dict[str, str] = {}
    for task_id, task in task_by_id.items():
        head = heads.get(task_id)
        if head is None or task.get("status_fact_id") != head["id"]:
            raise PlanningStatusValidationError("planning status head is missing or stale")
        projection[task_id] = head["status"]
    return projection


def validate_and_project(
    backlog: dict[str, Any], activity: dict[str, Any]
) -> dict[str, str]:
    facts = activity.get("planning_status")
    if not isinstance(facts, list):
        raise PlanningStatusValidationError("planning status store shape is invalid")
    task_by_id = _task_index(backlog)
    heads: dict[str, dict[str, Any]] = {}
    for index, candidate in enumerate(facts, start=1):
        fact = _validate_fact_envelope(candidate, index)
        task_id, task, status = _fact_task_and_status(fact, task_by_id)
        _validate_fact_provenance(fact)
        previous = heads.get(task_id)
        _validate_fact_chain(fact, previous, task, status)
        heads[task_id] = fact
    return _project_heads(task_by_id, heads)


def task_facts(activity: dict[str, Any], task_id: str) -> list[dict[str, Any]]:
    return [fact.copy() for fact in activity.get("planning_status", []) if fact.get("task_id") == task_id]
