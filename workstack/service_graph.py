"""Weekly rollup and graph snapshot projections over supplied documents.

These builders never read a Store: the caller hands them the already-loaded
Worklog, Tasks, Objectives and Notes, so the ACTIVE (superseded-row-free) view
and the physical view can both be projected by the same code.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .store import StoreCorruptError


def _weekly_range(end: str | None, days: int, local_day: dt.date) -> tuple[dt.date, dt.date]:
    """The inclusive range a weekly report covers. ``local_day`` is today."""

    if days < 1 or days > 366:
        raise ValueError("days must be between 1 and 366")
    end_day = dt.date.fromisoformat(end) if end else local_day
    return end_day - dt.timedelta(days=days - 1), end_day


def _weekly_project_slot(
    task_id: Any, entry: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task": entry.get("task") or task.get("title", task_id),
        "objective_ids": task.get("objective_ids", []),
        "done": [],
        "next": [],
        "blockers": [],
        "dates": [],
        "duration_seconds": 0,
    }


def _merge_weekly_entry(
    slot: dict[str, Any], entry: dict[str, Any], date: str
) -> None:
    duration_seconds = entry.get("duration_seconds", 0)
    if type(duration_seconds) is not int or duration_seconds < 0:
        raise StoreCorruptError("persisted worklog duration is invalid")
    slot["duration_seconds"] += duration_seconds
    for field in ("done", "next", "blockers"):
        for value in entry.get(field, []):
            if value not in slot[field]:
                slot[field].append(value)
    if date not in slot["dates"]:
        slot["dates"].append(date)


def _weekly_projects(
    worklog: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    start_day: dt.date,
    end_day: dt.date,
) -> dict[str, dict[str, Any]]:
    projects: dict[str, dict[str, Any]] = {}
    for date in sorted(worklog):
        parsed = dt.date.fromisoformat(date)
        if not start_day <= parsed <= end_day:
            continue
        for entry in worklog[date].get("entries", []):
            task_id = entry.get("task_id")
            task = tasks.get(task_id, {})
            slot = projects.setdefault(
                task_id, _weekly_project_slot(task_id, entry, task)
            )
            _merge_weekly_entry(slot, entry, date)
    return projects


def _weekly_objectives(
    projects: dict[str, dict[str, Any]],
    objectives: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    used = {
        objective_id
        for project in projects.values()
        for objective_id in project["objective_ids"]
    }
    return [
        {"id": objective_id, "objective": objectives[objective_id].get("objective", "")}
        for objective_id in sorted(used)
        if objective_id in objectives
    ]


def _snapshot_objective_node(objective: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": objective["id"],
        "kind": "objective",
        "title": objective["objective"],
        "status": objective.get("status", "active"),
        "meta": objective.get("quarter", ""),
        "quarter": objective.get("quarter", ""),
        "key_results": objective.get("key_results", []),
    }


def _snapshot_task_node(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task["id"],
        "uid": task["uid"],
        "kind": "task",
        "title": task["title"],
        "status": task.get("status", "open"),
        "revision": task["revision"],
        "meta": "{} · {}".format(
            task.get("priority", "P2"), task.get("due") or "no due date"
        ),
        "detail": task.get("detail", ""),
        "tags": task.get("tags", []),
        "priority": task.get("priority", "P2"),
        "due": task.get("due"),
        "objective_ids": task.get("objective_ids", []),
        "parent_id": task.get("parent_id"),
        "dependencies": task.get("dependencies", []),
        "subtasks": task.get("subtasks", []),
    }


def _append_snapshot_task(
    task: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    known: set[str],
) -> None:
    node = _snapshot_task_node(task)
    nodes.append(node)
    known.add(node["id"])
    if task.get("parent_id"):
        edges.append(
            {"source": task["id"], "target": task["parent_id"], "kind": "parent"}
        )
    for dependency in task.get("dependencies", []):
        edges.append(
            {"source": task["id"], "target": dependency, "kind": "dependency"}
        )
    for objective_id in task.get("objective_ids", []):
        edges.append(
            {"source": task["id"], "target": objective_id, "kind": "objective"}
        )
    for subtask in task.get("subtasks", []):
        subtask_id = "{}-{}".format(task["id"], subtask["id"])
        nodes.append({
            "id": subtask_id,
            "kind": "subtask",
            "title": subtask["title"],
            "status": subtask.get("status", "open"),
            "meta": subtask.get("priority", "P2"),
        })
        known.add(subtask_id)
        edges.append(
            {"source": subtask_id, "target": task["id"], "kind": "parent"}
        )


def _append_snapshot_worklog(
    worklog: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    known: set[str],
) -> None:
    for date, day in sorted(worklog.items()):
        day_id = "D-" + date
        nodes.append({
            "id": day_id,
            "kind": "day",
            "title": date,
            "status": "recorded",
            "meta": "{} entries".format(len(day.get("entries", []))),
            "entry_count": len(day.get("entries", [])),
        })
        known.add(day_id)
        for entry in day.get("entries", []):
            if entry.get("task_id"):
                edges.append(
                    {"source": day_id, "target": entry["task_id"], "kind": "worklog"}
                )


def _append_snapshot_notes(
    notes: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    known: set[str],
) -> None:
    for note in notes:
        nodes.append({
            "id": note["id"],
            "kind": "note",
            "title": note["text"],
            "status": "recorded",
            "meta": note.get("created", ""),
            "links": note.get("links", []),
        })
        known.add(note["id"])
        for link in note.get("links", []):
            edges.append({"source": note["id"], "target": link, "kind": "note"})
