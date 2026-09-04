"""Pure, workspace-local read projection for shared notes and reviewed captures."""

from __future__ import annotations

import copy
import datetime as dt
import re
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from .capture import parse_rfc3339


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z", re.ASCII)
_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z",
    re.ASCII,
)


def _creation_order(value: Any, key: str) -> tuple[str, tuple[Any, ...]]:
    """Use UTC civil days for instants; never assign an instant to a date-only value."""
    if isinstance(value, str):
        try:
            if _DATE.fullmatch(value):
                day = dt.date.fromisoformat(value)
                return "date", (-day.toordinal(), 1, 0, 0, key)
            if _INSTANT.fullmatch(value):
                parsed = parse_rfc3339(value, "context creation time")
                instant = parsed.utc_second
                seconds = instant.hour * 3600 + instant.minute * 60 + instant.second
                # Exact construction and copy_negate avoid both microsecond loss
                # and rounding under the caller's decimal arithmetic context.
                fraction = Decimal("0." + parsed.fraction).copy_negate()
                return "instant", (
                    -instant.date().toordinal(), 0, -seconds, fraction, key,
                )
        except (ValueError, OverflowError):
            pass
    return "unknown", (0, 2, 0, 0, key)


def _connections(
    record: Mapping[str, Any], kind: str, task_ids: set[str], objective_ids: set[str],
) -> list[dict[str, Any]]:
    targets: dict[tuple[str, str], list[str]] = {}
    if kind == "note":
        for target_id in set(record.get("links", [])):
            for target_kind, known in (("task", task_ids), ("objective", objective_ids)):
                if target_id in known:
                    targets[(target_kind, target_id)] = ["note-link"]
    else:
        for field, reason in (
            ("linked_task_ids", "capture-link"),
            ("converted_task_ids", "capture-conversion"),
        ):
            for task_id in set(record.get(field, [])) & task_ids:
                targets.setdefault(("task", task_id), []).append(reason)
    return [
        {"target": {"kind": target_kind, "id": target_id}, "reasons": reasons}
        for (target_kind, target_id), reasons in sorted(targets.items())
    ]


def project_context_items(
    notes: Iterable[Mapping[str, Any]],
    captures: Iterable[Mapping[str, Any]],
    task_ids: Iterable[str],
    objective_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Add read metadata to already-allowlisted records without changing their fields.

    Identity is (kind, existing id), never content or URL. Dismissed captures retain
    their links, and orphan/Objective-only notes remain in the complete projection.
    """
    known_tasks, known_objectives = set(task_ids), set(objective_ids)
    records: dict[tuple[str, str], Mapping[str, Any]] = {}
    projected: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for kind, values in (("note", notes), ("capture", captures)):
        for record in values:
            identity = (kind, record["id"])
            if identity in records:
                if records[identity] != record:
                    raise ValueError("conflicting shared context identity")
                continue
            records[identity] = record
            item = copy.deepcopy(dict(record))
            item["ref"] = {"kind": kind, "id": record["id"]}
            item["connections"] = _connections(record, kind, known_tasks, known_objectives)
            created = record.get("created" if kind == "note" else "created_at")
            precision, order = _creation_order(created, "{}:{}".format(*identity))
            item["date_precision"] = precision
            projected.append((order, item))
    return [item for _, item in sorted(projected, key=lambda entry: entry[0])]


def group_context_by_task(
    notes: Iterable[Mapping[str, Any]],
    captures: Iterable[Mapping[str, Any]],
    task_ids: Iterable[str],
    objective_ids: Iterable[str],
) -> dict[str, list[dict[str, Any]]]:
    """Return the same ordered, distinct card set used by counts and Task detail."""
    known_tasks = set(task_ids)
    grouped: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in known_tasks}
    for item in project_context_items(notes, captures, known_tasks, objective_ids):
        for connection in item["connections"]:
            target = connection["target"]
            if target["kind"] == "task":
                grouped[target["id"]].append(item)
    return grouped
