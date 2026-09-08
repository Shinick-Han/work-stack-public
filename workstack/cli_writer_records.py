"""CLI-owned worklog, backlog, and OKR link/progress owner-write forwarding.

These commands post to the frozen ``/api/v1/cli/...`` routes, judge both
status and body, and raise ``CommitUnknownError`` rather than a determinate
refusal after a possible commit. Clock defaults honor the facade
``datetime`` monkeypatch seam.
"""

from __future__ import annotations

import datetime
import re
import uuid
from typing import Mapping, Sequence

from .cli_writer_owner import (
    CommitUnknownError,
    CoordinatesReader,
    RequestJson,
    expected_workspace_uid,
)
from .cli_writer_transport import _forward_write
from .store import MAX_REVISION


def _default_clock():
    return datetime


_clock_provider = _default_clock


def bind_clock_provider(provider):
    global _clock_provider
    _clock_provider = provider


def _clock():
    return _clock_provider()


def _checkin_result(
    status: int, payload: Mapping[str, object], body: dict[str, object],
) -> dict[str, object]:
    message = "checkin commit is unknown; inspect the worklog before retrying"
    if status != 200 or type(payload) is not dict or set(payload) != {"data"}:
        raise CommitUnknownError(message)
    data = payload["data"]
    if type(data) is not dict or list(data) != ["date", "start_time"]:
        raise CommitUnknownError(message)
    for field, sent in (("date", "date"), ("start_time", "time")):
        if type(data[field]) is not str or data[field] != body[sent]:
            raise CommitUnknownError(message)
    return data


def forward_checkin(
    store: object, owner_state: str, time: str | None, date: str | None,
    *, coordinates_reader: CoordinatesReader, request_json: RequestJson,
) -> dict[str, object]:
    """Freeze CLI clock defaults, then invoke the owner's unchanged checkin."""
    clock = _clock()
    frozen_date = date or clock.date.today().isoformat()
    frozen_time = clock.datetime.now().strftime("%H:%M") if time is None else time
    body = {"date": frozen_date, "time": frozen_time}
    return _forward_write(
        store, owner_state, path="/api/v1/cli/worklog/checkin", body=body,
        coordinates_reader=coordinates_reader, request_json=request_json,
        idempotency_key=None, keyless_post=True, replay=False,
        project=lambda payload: {},
        project_result=lambda status, payload: _checkin_result(status, payload, body),
        changed_message="Work Stack server runtime metadata changed before the checkin was sent",
        unknown_message="checkin commit is unknown; inspect the worklog before retrying",
        refused_message="the running Work Stack server refused the checkin (HTTP {})",
    )


def _worklog_entry_categories_match(data: dict, body: dict) -> bool:
    """Compare a response view without normalizing the request or returned data."""
    for field, sent in (("done", "done"), ("next", "next_items"), ("blockers", "blockers")):
        values = data[field]
        if type(values) is not list or any(type(item) is not str for item in values):
            return False
        expected = [item.strip() for item in body[sent] if item.strip()]
        if values != expected:
            return False
    return True


def _worklog_entry_result(status: int, payload: Mapping[str, object], body: dict) -> dict[str, object]:
    message = "worklog entry commit is unknown; inspect the worklog before retrying"
    if status != 200 or type(payload) is not dict or set(payload) != {"data"}:
        raise CommitUnknownError(message)
    data = payload["data"]
    if type(data) is not dict or list(data) != ["date", "task_id", "task", "done", "next", "blockers"]:
        raise CommitUnknownError(message)
    if any(type(data[field]) is not str for field in ("date", "task_id", "task")):
        raise CommitUnknownError(message)
    if data["date"] != body["date"] or data["task_id"] != body["task_id"].strip().upper():
        raise CommitUnknownError(message)
    if not _worklog_entry_categories_match(data, body):
        raise CommitUnknownError(message)
    return data


def forward_worklog_entry(
    store: object, owner_state: str, task_id: str, date: str | None,
    done: Sequence[str], next_items: Sequence[str], blockers: Sequence[str],
    *, coordinates_reader: CoordinatesReader, request_json: RequestJson,
) -> dict[str, object]:
    body = {"task_id": task_id, "date": date or _clock().date.today().isoformat(),
            "done": list(done), "next_items": list(next_items), "blockers": list(blockers)}
    return _forward_write(
        store, owner_state, path="/api/v1/cli/worklog/add", body=body,
        coordinates_reader=coordinates_reader, request_json=request_json,
        idempotency_key=None, keyless_post=True, replay=False,
        project=lambda payload: {},
        project_result=lambda status, payload: _worklog_entry_result(status, payload, body),
        changed_message="Work Stack server runtime metadata changed before the worklog entry was sent",
        unknown_message="worklog entry commit is unknown; inspect the worklog before retrying",
        refused_message="the running Work Stack server refused the worklog entry (HTTP {})",
    )


def _cli_result_data(status: int, payload: Mapping[str, object], message: str) -> dict:
    if status != 200 or type(payload) is not dict or set(payload) != {"data"}:
        raise CommitUnknownError(message)
    if type(payload["data"]) is not dict:
        raise CommitUnknownError(message)
    return payload["data"]


def _cli_calendar_date(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return datetime.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _cli_record_uid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.int != 0 and parsed.variant == uuid.RFC_4122 and str(parsed) == value


def _cli_string_list(value: object) -> bool:
    return type(value) is list and all(type(item) is str for item in value)


def _backlog_identity_matches(data: dict, workspace_uid: str) -> bool:
    if type(data["id"]) is not str or re.fullmatch(r"T-[0-9]{4,}", data["id"]) is None:
        return False
    if not _cli_record_uid(data["uid"]):
        return False
    if data["uid"] != str(uuid.uuid5(uuid.UUID(workspace_uid), data["id"])):
        return False
    fact = data["status_fact_id"]
    return type(fact) is str and re.fullmatch(r"PS-[0-9]{6,}", fact) is not None


def _backlog_values_match(data: dict, body: dict) -> bool:
    expected = {
        "title": body["title"].strip(), "detail": body["detail"].strip(),
        "priority": body["priority"], "due": body["due"] or None,
        "parent_id": body["parent_id"].strip().upper() if body["parent_id"] else None,
        "status": "open", "revision": 0, "scheduled": None, "estimate_minutes": None,
        "subtasks": [], "notes": [],
    }
    if any(type(data[field]) is not type(value) or data[field] != value for field, value in expected.items()):
        return False
    return _cli_calendar_date(data["created"]) and _cli_calendar_date(data["updated_at"])


def _backlog_collections_match(data: dict, body: dict) -> bool:
    """Bind a received view; leave original input and output values untouched."""
    for field in ("tags", "objective_ids", "dependencies"):
        if not _cli_string_list(data[field]):
            return False
        stripped = [item.strip() for item in body[field] if item.strip()]
        expected = stripped if field == "tags" else [item.upper() for item in stripped]
        if data[field] != sorted(set(expected)):
            return False
    return True


def _backlog_add_result(status: int, payload: Mapping[str, object], body: dict, workspace_uid: str) -> dict:
    message = "backlog add commit is unknown; inspect the backlog before retrying"
    data = _cli_result_data(status, payload, message)
    fields = ["id", "uid", "title", "detail", "status", "priority", "due", "scheduled",
              "estimate_minutes", "tags", "objective_ids", "parent_id", "dependencies",
              "subtasks", "notes", "created", "updated_at", "revision", "status_fact_id"]
    if list(data) != fields:
        raise CommitUnknownError(message)
    if not (_backlog_identity_matches(data, workspace_uid) and _backlog_values_match(data, body)
            and _backlog_collections_match(data, body)):
        raise CommitUnknownError(message)
    return data


def forward_backlog_add(
    store: object, owner_state: str, title: str, detail: str, priority: str, due: str | None,
    tags: Sequence[str], objective_ids: Sequence[str], parent_id: str | None, dependencies: Sequence[str],
    *, coordinates_reader: CoordinatesReader, request_json: RequestJson,
) -> dict[str, object]:
    body = {"title": title, "detail": detail, "priority": priority, "due": due, "tags": list(tags),
            "objective_ids": list(objective_ids), "parent_id": parent_id, "dependencies": list(dependencies)}
    workspace_uid = expected_workspace_uid(store)
    return _forward_write(
        store, owner_state, path="/api/v1/cli/backlog/add", body=body,
        coordinates_reader=coordinates_reader, request_json=request_json,
        idempotency_key=None, keyless_post=True, replay=False, project=lambda payload: {},
        project_result=lambda status, payload: _backlog_add_result(status, payload, body, workspace_uid),
        changed_message="Work Stack server runtime metadata changed before the backlog add was sent",
        unknown_message="backlog add commit is unknown; inspect the backlog before retrying",
        refused_message="the running Work Stack server refused the backlog add (HTTP {})",
    )


def _okr_link_identity_matches(data: dict, body: dict) -> bool:
    task_id = data.get("id")
    if type(task_id) is not str or task_id != body["task_id"].strip().upper():
        return False
    if not _cli_record_uid(data.get("uid")):
        return False
    fact = data.get("status_fact_id")
    return type(fact) is str and re.fullmatch(r"PS-[0-9]{6,}", fact) is not None


def _okr_link_result(status: int, payload: Mapping[str, object], body: dict) -> dict:
    message = "OKR link commit is unknown; inspect the task before retrying"
    data = _cli_result_data(status, payload, message)
    if not _okr_link_identity_matches(data, body):
        raise CommitUnknownError(message)
    revision = data.get("revision")
    if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
        raise CommitUnknownError(message)
    if not _cli_calendar_date(data.get("updated_at")):
        raise CommitUnknownError(message)
    objectives = data.get("objective_ids")
    if not _cli_string_list(objectives) or objectives != sorted(set(objectives)):
        raise CommitUnknownError(message)
    if body["objective_id"].strip().upper() not in objectives:
        raise CommitUnknownError(message)
    # The envelope binds this link; the actual one-step revision and unchanged
    # history are established by the owner operation, not a prior client GET.
    return data


def forward_okr_link(
    store: object, owner_state: str, objective_id: str, task_id: str,
    *, coordinates_reader: CoordinatesReader, request_json: RequestJson,
) -> dict[str, object]:
    body = {"objective_id": objective_id, "task_id": task_id}
    return _forward_write(
        store, owner_state, path="/api/v1/cli/okr/link", body=body,
        coordinates_reader=coordinates_reader, request_json=request_json,
        idempotency_key=None, keyless_post=True, replay=False, project=lambda payload: {},
        project_result=lambda status, payload: _okr_link_result(status, payload, body),
        changed_message="Work Stack server runtime metadata changed before the OKR link was sent",
        unknown_message="OKR link commit is unknown; inspect the task before retrying",
        refused_message="the running Work Stack server refused the OKR link (HTTP {})",
    )


def _okr_progress_result(status: int, payload: Mapping[str, object], body: dict) -> dict:
    message = "OKR progress commit is unknown; inspect the objective before retrying"
    data = _cli_result_data(status, payload, message)
    if "id" not in data or str(data["id"]).upper() != body["key_result_id"].strip().upper():
        raise CommitUnknownError(message)
    expected = max(0, min(100, body["progress"]))
    if type(data.get("progress")) is not int or data["progress"] != expected:
        raise CommitUnknownError(message)
    expected_status = "done" if expected == 100 else "active"
    if type(data.get("status")) is not str or data["status"] != expected_status:
        raise CommitUnknownError(message)
    # The raw KR carries neither owning Objective identity nor revision. Do not
    # invent those fields or perform another read to infer an unknown commit.
    return data


def forward_okr_progress(
    store: object, owner_state: str, objective_id: str, key_result_id: str, progress: int,
    *, coordinates_reader: CoordinatesReader, request_json: RequestJson,
) -> dict[str, object]:
    body = {"objective_id": objective_id, "key_result_id": key_result_id, "progress": progress}
    return _forward_write(
        store, owner_state, path="/api/v1/cli/okr/progress", body=body,
        coordinates_reader=coordinates_reader, request_json=request_json,
        idempotency_key=None, keyless_post=True, replay=False, project=lambda payload: {},
        project_result=lambda status, payload: _okr_progress_result(status, payload, body),
        changed_message="Work Stack server runtime metadata changed before the OKR progress was sent",
        unknown_message="OKR progress commit is unknown; inspect the objective before retrying",
        refused_message="the running Work Stack server refused the OKR progress (HTTP {})",
    )
