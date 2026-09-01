"""Format-neutral semantic snapshots for v3-to-v4 migration parity.

This module deliberately projects physical v3 documents and normalized v4
records into the frozen v3 semantic boundary.  It performs no writes and does
not make a v4 candidate authoritative.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from workstack.planning_status import validate_and_project

from .canonical import canonical_sha256
from .reader import V4ReadResult


class SemanticProjectionError(ValueError):
    """A content-free refusal to invent missing migration semantics."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class V4SemanticSource:
    """Validated v4 records plus deferred migration/runtime evidence.

    ``task_note_source_indexes`` comes from the migration receipt roster.  A
    Task annotation has no source ID in v3, so its original array position
    cannot be reconstructed from the generated UUID alone.
    """

    workspace: Mapping[str, Any]
    tasks: Sequence[Mapping[str, Any]] = ()
    objectives: Sequence[Mapping[str, Any]] = ()
    captures: Sequence[Mapping[str, Any]] = ()
    replies: Sequence[Mapping[str, Any]] = ()
    notes: Sequence[Mapping[str, Any]] = ()
    planning_events: Sequence[Mapping[str, Any]] = ()
    activity_events: Sequence[Mapping[str, Any]] = ()
    worklog_events: Sequence[Mapping[str, Any]] = ()
    idempotency_records: Sequence[Mapping[str, Any]] = ()
    task_note_source_indexes: Mapping[str, int] | None = None


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Immutable-by-convention logical workspace projection."""

    _value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._value))

    def to_v3_documents(self) -> dict[str, dict[str, Any]]:
        """Materialize the frozen legacy read model without touching authority."""

        value = self.to_dict()
        return {
            "workspace.json": value["workspace"],
            "backlog.json": {"version": 3, "tasks": value["tasks"]},
            "store-meta.json": {
                "version": 2,
                "store_schema_version": 3,
                "migrations": {
                    "identity": {
                        "id": "workstack.store.v2",
                        "origin": "fresh",
                        "source_sha256": None,
                    },
                    "planning_status": {
                        "id": "workstack.planning-status.v1",
                        "origin": "fresh",
                        "source_sha256": None,
                    },
                },
            },
            "okr.json": {"version": 1, "objectives": value["objectives"]},
            "worklog.json": {"version": 1, "days": value["worklog_days"]},
            "notes.json": {"version": 1, "notes": value["notes"]},
            "captures.json": {"version": 1, "captures": value["captures"]},
            "replies.json": {"version": 1, "replies": value["replies"]},
            "activity.json": {
                "version": 2,
                "activity": value["activity"],
                "idempotency": value["idempotency"],
                "planning_status": value["planning_facts"],
            },
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self._value)

    def relation_edges(self) -> tuple[tuple[str, str, str], ...]:
        """Return deterministic source relationships used by graph views."""

        edges: set[tuple[str, str, str]] = set()
        for task in self._value["tasks"]:
            task_id = task["id"]
            edges.update(("objective", task_id, item) for item in task["objective_ids"])
            if task["parent_id"] is not None:
                edges.add(("parent", task_id, task["parent_id"]))
            edges.update(("dependency", task_id, item) for item in task["dependencies"])
        for note in self._value["notes"]:
            edges.update(("note-link", note["id"], item) for item in note["links"])
        return tuple(sorted(edges))

    def search_documents(self) -> tuple[tuple[str, str], ...]:
        """Return stable searchable source text without ranking policy."""

        documents: list[tuple[str, str]] = []
        for task in self._value["tasks"]:
            parts = [task["title"], task["detail"], *task["tags"], *task["objective_ids"]]
            parts.extend(note["text"] for note in task["notes"])
            parts.extend(subtask["title"] for subtask in task["subtasks"])
            documents.append((task["id"], "\n".join(parts)))
        for objective in self._value["objectives"]:
            parts = [objective["objective"]]
            parts.extend(item["text"] for item in objective["key_results"])
            parts.extend(item["target"] for item in objective["key_results"])
            documents.append((objective["id"], "\n".join(parts)))
        for note in self._value["notes"]:
            documents.append((note["id"], "\n".join((note["text"], *note["links"]))))
        for capture in self._value["captures"]:
            normalized = capture["normalized"]
            parts = [capture["source"]["display_title"], normalized["summary"], normalized["context"]]
            parts.extend(item["title"] for item in normalized["action_items"])
            documents.append((capture["id"], "\n".join(parts)))
        for event in self._value["activity"]:
            details = event["details"] if isinstance(event["details"], dict) else {}
            documents.append((event["id"], "\n".join((
                event["type"], str(details.get("provider", "")), str(details.get("state", "")),
            ))))
        return tuple(sorted(documents))


def semantic_source_from_v4_read(
    result: V4ReadResult,
    *,
    idempotency_records: Sequence[Mapping[str, Any]] = (),
    task_note_source_indexes: Mapping[str, int] | None = None,
) -> V4SemanticSource:
    """Adapt one validated physical read into the format-neutral projection input.

    Deferred migration evidence is explicit: the reader never guesses the source
    order of embedded v3 Task annotations or a future idempotency-ledger shape.
    """

    return V4SemanticSource(
        workspace=result.workspace,
        tasks=result.records.get("tasks", ()),
        objectives=result.records.get("objectives", ()),
        captures=result.records.get("captures", ()),
        replies=result.records.get("replies", ()),
        notes=result.records.get("notes", ()),
        planning_events=result.streams.get("planning-status", ()),
        activity_events=result.streams.get("activity", ()),
        worklog_events=result.streams.get("worklog", ()),
        idempotency_records=idempotency_records,
        task_note_source_indexes=task_note_source_indexes,
    )


def snapshot_from_v3_documents(documents: Mapping[str, Mapping[str, Any]]) -> WorkspaceSnapshot:
    """Project the nine authoritative v3 documents without physical wrappers."""

    backlog = documents["backlog.json"]
    activity = documents["activity.json"]
    value = {
        "format": "workstack.v3.semantic-snapshot",
        "workspace": copy.deepcopy(documents["workspace.json"]),
        "tasks": copy.deepcopy(backlog["tasks"]),
        "planning_status": validate_and_project(dict(backlog), dict(activity)),
        "objectives": copy.deepcopy(documents["okr.json"]["objectives"]),
        "worklog_days": copy.deepcopy(documents["worklog.json"]["days"]),
        "notes": copy.deepcopy(documents["notes.json"]["notes"]),
        "captures": copy.deepcopy(documents["captures.json"]["captures"]),
        "replies": copy.deepcopy(documents["replies.json"]["replies"]),
        "activity": copy.deepcopy(activity["activity"]),
        "idempotency": copy.deepcopy(activity["idempotency"]),
        "planning_facts": copy.deepcopy(activity["planning_status"]),
    }
    return WorkspaceSnapshot(value)


def _by_uid(records: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {record["uid"]: record for record in records}


def _display_id_by_uid(records: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    return {record["uid"]: record["display_id"] for record in records}


def _optional_copy(target: dict[str, Any], source: Mapping[str, Any], *fields: str) -> None:
    for field in fields:
        if field in source:
            target[field] = copy.deepcopy(source[field])


def _planning_indexes(events: Sequence[Mapping[str, Any]]) -> tuple[dict[str, list[Mapping[str, Any]]], dict[str, str]]:
    by_task: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        by_task.setdefault(event["task_uid"], []).append(event)
    current: dict[str, str] = {}
    for task_events in by_task.values():
        task_events.sort(key=lambda item: (item["sequence"], item["legacy_fact_id"]))
        current[task_events[-1]["task_display_id"]] = task_events[-1]["status"]
    return by_task, current


def _task_notes(source: V4SemanticSource) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    indexes = source.task_note_source_indexes or {}
    for note in source.notes:
        if note["note_kind"] != "task_annotation":
            continue
        if note["uid"] not in indexes:
            raise SemanticProjectionError("TASK_NOTE_SOURCE_INDEX_REQUIRED")
        result.setdefault(note["task_uid"], []).append(note)
    for notes in result.values():
        notes.sort(key=lambda item: indexes[item["uid"]])
    return result


def _project_task(
    task: Mapping[str, Any],
    objective_ids: Mapping[str, str],
    task_ids: Mapping[str, str],
    planning: Mapping[str, list[Mapping[str, Any]]],
    notes: Mapping[str, list[Mapping[str, Any]]],
) -> dict[str, Any]:
    facts = planning.get(task["uid"], [])
    if not facts:
        raise SemanticProjectionError("TASK_PLANNING_HISTORY_REQUIRED")
    first, head = facts[0], facts[-1]
    projected = {
        "id": task["display_id"],
        "uid": task["uid"],
        "revision": task["revision"],
        "status_fact_id": head["legacy_fact_id"],
        "title": task["title"],
        "detail": task["detail"],
        "status": first["status"],
        "priority": task["priority"],
        "due": task["due"],
        "tags": copy.deepcopy(task["tags"]),
        "objective_ids": [objective_ids[uid] for uid in task["objective_uids"]],
        "parent_id": None if task["parent_uid"] is None else task_ids[task["parent_uid"]],
        "dependencies": [task_ids[uid] for uid in task["dependency_uids"]],
        "subtasks": [
            {
                "id": child["display_id"],
                "title": child["title"],
                "priority": child["priority"],
                "status": child["status"],
            }
            for child in task["subtasks"]
        ],
        "notes": [
            {"date": item["created_at"], "text": item["text"]}
            for item in notes.get(task["uid"], [])
        ],
        "created": task["created_at"],
        "updated_at": task["updated_at"],
    }
    _optional_copy(projected, task, "scheduled", "estimate_minutes")
    return projected


def _project_objective(record: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "id": record["display_id"],
        "quarter": record["quarter"],
        "objective": record["title"],
        "status": record["status"],
        "key_results": [
            {
                "id": child["display_id"],
                "text": child["title"],
                "target": child["target"],
                "progress": child["progress"],
                "status": child["status"],
            }
            for child in record["key_results"]
        ],
        "created": record["created_at"],
        "updated_at": record["updated_at"],
    }
    if record["revision_origin"] == "explicit":
        result["revision"] = record["revision"]
    return result


def _project_worklog(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    days: dict[str, dict[str, Any]] = {}
    for event in sorted(events, key=lambda item: (item["sequence"], item["event_uid"])):
        # v3 creates every worklog day with an explicit empty entry roster,
        # including check-in-only and session-only days.  Retain that semantic
        # coordinate so a normalized backend is observationally identical.
        day = days.setdefault(event["work_date"], {"entries": []})
        if event["kind"] == "check-in":
            day["start_time"] = event["start_time"]
        elif event["kind"] == "entry":
            entry = {
                "task_id": event["task_display_id"], "task": event["task_title"],
                "done": copy.deepcopy(event["done"]), "next": copy.deepcopy(event["next"]),
                "blockers": copy.deepcopy(event["blockers"]),
            }
            _optional_copy(entry, event, "session_id", "duration_seconds")
            day.setdefault("entries", []).append(entry)
        elif event["kind"] == "session":
            session = {
                "id": event["session_id"], "task_id": event["task_display_id"],
                "task": event["task_title"], "date": event["work_date"],
                "state": event["state"], "started_at": event["started_at"],
                "updated_at": event["updated_at"], "segments": copy.deepcopy(event["segments"]),
                "worklog_state": event["worklog_state"],
            }
            _upsert_worklog_session(day, session)
        else:
            raise SemanticProjectionError("UNKNOWN_WORKLOG_EVENT_KIND")
    return days


def _upsert_worklog_session(day: dict[str, Any], session: dict[str, Any]) -> None:
    """Fold append-only session snapshots into the single v3 session record."""

    sessions = day.setdefault("sessions", [])
    for index, current in enumerate(sessions):
        if current["id"] == session["id"]:
            sessions[index] = session
            return
    sessions.append(session)


def _project_capture(record: Mapping[str, Any], task_ids: Mapping[str, str]) -> dict[str, Any]:
    normalized = copy.deepcopy(record["normalized"])
    for item in normalized["action_items"]:
        display_id = item.pop("task_display_id")
        item.pop("task_uid")
        if display_id is not None:
            item["task_id"] = display_id
    result = {
        "id": record["display_id"], "schema_version": record["capture_packet_schema_version"],
        "source_key": record["source_key"], "source": copy.deepcopy(record["source"]),
        "normalized": normalized, "task_hints": copy.deepcopy(record["task_hints"]),
        "provenance": copy.deepcopy(record["provenance"]), "status": record["status"],
        "linked_task_ids": [task_ids[uid] for uid in record["linked_task_uids"]],
        "converted_task_ids": [task_ids[uid] for uid in record["converted_task_uids"]],
        "revision": record["revision"], "created_at": record["created_at"],
        "updated_at": record["updated_at"], "recent_revisions": copy.deepcopy(record["recent_revisions"]),
    }
    return result


def _project_reply(
    record: Mapping[str, Any], task_ids: Mapping[str, str], capture_ids: Mapping[str, str]
) -> dict[str, Any]:
    result = {
        "id": record["display_id"], "task_id": task_ids[record["task_uid"]],
        "capture_id": capture_ids[record["capture_uid"]],
        "capture_revision": record["capture_revision"], "provider": record["provider"],
        "capability": record["capability"], "target": copy.deepcopy(record["target"]),
        "body": record["body"], "body_digest": record["body_digest"],
        "target_digest": record["target_digest"], "state": record["state"],
        "approved_at": record["approved_at"], "receipt": copy.deepcopy(record["receipt"]),
        "created_at": record["created_at"], "updated_at": record["updated_at"],
    }
    if result["receipt"] is not None:
        result["receipt"]["reply_id"] = result["receipt"].pop("reply_display_id")
    return result


def _project_activity(
    events: Sequence[Mapping[str, Any]], task_ids: Mapping[str, str],
    capture_ids: Mapping[str, str], reply_ids: Mapping[str, str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda item: (item["sequence"], item["legacy_event_id"])):
        projected = {
            "id": event["legacy_event_id"], "type": event["event_type"],
            "created_at": event["created_at"], "details": copy.deepcopy(event["details"]),
        }
        for field, index in (("capture", capture_ids), ("task", task_ids), ("reply", reply_ids)):
            uid = event[f"{field}_uid"]
            if uid is not None:
                projected[f"{field}_id"] = index[uid]
        result.append(projected)
    return result


def _project_planning(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for event in sorted(events, key=lambda item: (item["sequence"], item["legacy_fact_id"])):
        result.append({
            "id": event["legacy_fact_id"], "type": "task.planning_status",
            "task_id": event["task_display_id"], "task_uid": event["task_uid"],
            "previous_fact_id": event["previous_legacy_fact_id"],
            "prior_revision": event["prior_revision"], "new_revision": event["new_revision"],
            "prior_status": event["prior_status"], "status": event["status"],
            "created_at": event["created_at"], "actor": event["actor"],
            "provenance": event["provenance"],
        })
    return result


def snapshot_from_v4(source: V4SemanticSource) -> WorkspaceSnapshot:
    """Reverse-project a validated, inactive v4 candidate into v3 semantics."""

    task_ids = _display_id_by_uid(source.tasks)
    objective_ids = _display_id_by_uid(source.objectives)
    capture_ids = _display_id_by_uid(source.captures)
    reply_ids = _display_id_by_uid(source.replies)
    planning, current_status = _planning_indexes(source.planning_events)
    task_notes = _task_notes(source)
    tasks = [
        _project_task(record, objective_ids, task_ids, planning, task_notes)
        for record in sorted(source.tasks, key=lambda item: item["display_id"])
    ]
    standalone_notes = [
        {"id": item["display_id"], "text": item["text"],
         "links": copy.deepcopy(item["links"]), "created": item["created_at"]}
        for item in sorted(source.notes, key=lambda item: item["display_id"])
        if item["note_kind"] == "standalone"
    ]
    value = {
        "format": "workstack.v3.semantic-snapshot",
        "workspace": {"version": 2, "id": source.workspace["workspace_uid"], "name": source.workspace["name"]},
        "tasks": tasks,
        "planning_status": dict(sorted(current_status.items())),
        "objectives": [_project_objective(item) for item in sorted(source.objectives, key=lambda item: item["display_id"])],
        "worklog_days": _project_worklog(source.worklog_events),
        "notes": standalone_notes,
        "captures": [_project_capture(item, task_ids) for item in sorted(source.captures, key=lambda item: item["display_id"])],
        "replies": [_project_reply(item, task_ids, capture_ids) for item in sorted(source.replies, key=lambda item: item["display_id"])],
        "activity": _project_activity(source.activity_events, task_ids, capture_ids, reply_ids),
        "idempotency": copy.deepcopy(list(source.idempotency_records)),
        "planning_facts": _project_planning(source.planning_events),
    }
    return WorkspaceSnapshot(value)
