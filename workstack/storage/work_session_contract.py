"""Backend-neutral Work Session lifecycle state machine."""

from __future__ import annotations

import copy
import datetime as dt
import re
from typing import Any, Mapping, Sequence

from .canonical import canonical_sha256
from .intent_contract import IntentContractError, NormalizedIntent


def normalize_session_start(
    task: Mapping[str, Any],
    sessions: Sequence[Mapping[str, Any]],
    *,
    timestamp: str,
    work_date: str,
    path: str = "/api/v1/work-sessions",
) -> NormalizedIntent:
    if any(item["state"] in {"running", "paused"} for item in sessions):
        raise IntentContractError("WORK_SESSION_ALREADY_ACTIVE")
    session = {
        "id": _next_session_id(sessions),
        "task_id": task["id"],
        "task": task["title"],
        "date": work_date,
        "state": "running",
        "started_at": timestamp,
        "updated_at": timestamp,
        "segments": [{"started_at": timestamp, "ended_at": None}],
        "worklog_state": "not_ready",
    }
    return NormalizedIntent(
        path,
        canonical_sha256({"task_id": task["id"]}),
        project_session(session, current_time=timestamp),
        session,
    )


def normalize_session_transition(
    session: Mapping[str, Any], action: str, *, timestamp: str, path: str
) -> NormalizedIntent:
    if action not in {"pause", "resume", "stop"}:
        raise IntentContractError("WORK_SESSION_ACTION_INVALID")
    current_state = session.get("state")
    expected = {"pause": "running", "resume": "paused"}.get(action)
    valid = (
        current_state in {"running", "paused"}
        if action == "stop"
        else current_state == expected
    )
    if not valid:
        raise IntentContractError("WORK_SESSION_TRANSITION_CONFLICT")
    updated = copy.deepcopy(dict(session))
    if action in {"pause", "stop"} and current_state == "running":
        updated["segments"][-1]["ended_at"] = timestamp
    if action == "resume":
        updated["segments"].append({"started_at": timestamp, "ended_at": None})
    updated["state"] = {"pause": "paused", "resume": "running", "stop": "stopped"}[action]
    if action == "stop":
        updated["worklog_state"] = "pending"
    updated["updated_at"] = timestamp
    return NormalizedIntent(
        path,
        canonical_sha256({}),
        project_session(updated, current_time=timestamp),
        updated,
    )


def normalize_session_worklog(
    session: Mapping[str, Any], body: Mapping[str, Any], *, timestamp: str, path: str
) -> tuple[NormalizedIntent, Mapping[str, Any]]:
    canonical = canonical_session_worklog(body)
    if session.get("state") != "stopped" or session.get("worklog_state") != "pending":
        raise IntentContractError("WORK_SESSION_WORKLOG_CONFLICT")
    updated = copy.deepcopy(dict(session))
    updated["worklog_state"] = "recorded"
    updated["updated_at"] = timestamp
    entry = {
        "task_id": session["task_id"],
        "task": session["task"],
        **canonical,
        "session_id": session["id"],
        "duration_seconds": elapsed_seconds(session),
    }
    response = {"date": session["date"], **copy.deepcopy(entry)}
    return (
        NormalizedIntent(path, canonical_sha256(canonical), response, updated),
        entry,
    )


def canonical_session_worklog(body: Mapping[str, Any]) -> dict[str, list[str]]:
    canonical = {
        "done": _items(body.get("done"), "WORK_SESSION_DONE_INVALID"),
        "next": _items(body.get("next"), "WORK_SESSION_NEXT_INVALID"),
        "blockers": _items(body.get("blockers"), "WORK_SESSION_BLOCKERS_INVALID"),
    }
    if not any(canonical.values()):
        raise IntentContractError("WORK_SESSION_WORKLOG_REQUIRED")
    return canonical


def project_session(
    session: Mapping[str, Any], *, current_time: str | None = None
) -> dict[str, Any]:
    return {
        "id": session["id"],
        "task_id": session["task_id"],
        "task": session["task"],
        "date": session["date"],
        "state": session["state"],
        "started_at": session["started_at"],
        "updated_at": session["updated_at"],
        "elapsed_seconds": elapsed_seconds(
            session, current_time=current_time if session["state"] == "running" else None
        ),
        "worklog_state": session["worklog_state"],
    }


def elapsed_seconds(
    session: Mapping[str, Any], *, current_time: str | None = None
) -> int:
    now = _timestamp(current_time) if current_time is not None else None
    elapsed = 0
    for segment in session["segments"]:
        started = _timestamp(segment["started_at"])
        ended = _timestamp(segment["ended_at"]) if segment["ended_at"] is not None else now
        if ended is None or ended < started:
            raise IntentContractError("WORK_SESSION_SEGMENT_INVALID")
        elapsed += int((ended - started).total_seconds())
    return elapsed


def fold_session_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ordered: dict[str, dict[str, Any]] = {}
    for event in sorted(events, key=lambda item: int(item["sequence"])):
        if event["kind"] != "session":
            continue
        ordered[str(event["session_id"])] = _legacy_session(event)
    sessions = list(ordered.values())
    for session in sessions:
        _validate_session(session)
    if sum(item["state"] in {"running", "paused"} for item in sessions) > 1:
        raise IntentContractError("WORK_SESSION_MULTIPLE_ACTIVE")
    return sessions


def _legacy_session(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": event["session_id"],
        "task_id": event["task_display_id"],
        "task": event["task_title"],
        "date": event["work_date"],
        "state": event["state"],
        "started_at": event["started_at"],
        "updated_at": event["updated_at"],
        "segments": copy.deepcopy(event["segments"]),
        "worklog_state": event["worklog_state"],
    }


def _validate_session(session: Mapping[str, Any]) -> None:
    state = session["state"]
    expected = {"not_ready"} if state in {"running", "paused"} else {
        "pending",
        "recorded",
    }
    if session["worklog_state"] not in expected:
        raise IntentContractError("WORK_SESSION_WORKLOG_STATE_INVALID")
    segments = session["segments"]
    if not isinstance(segments, list) or not segments:
        raise IntentContractError("WORK_SESSION_SEGMENT_INVALID")
    open_segments = _validate_segments(segments)
    if (state == "running") != (open_segments == 1):
        raise IntentContractError("WORK_SESSION_OPEN_SEGMENT_INCONSISTENT")


def _validate_segments(segments: Sequence[Mapping[str, Any]]) -> int:
    previous_end: dt.datetime | None = None
    open_segments = 0
    for index, segment in enumerate(segments):
        started, ended = _segment_bounds(segment)
        if previous_end is not None and started < previous_end:
            raise IntentContractError("WORK_SESSION_SEGMENTS_OVERLAP")
        if ended is None:
            if index != len(segments) - 1:
                raise IntentContractError("WORK_SESSION_OPEN_SEGMENT_INVALID")
            open_segments += 1
        previous_end = ended
    return open_segments


def _segment_bounds(
    segment: Mapping[str, Any],
) -> tuple[dt.datetime, dt.datetime | None]:
    if set(segment) != {"started_at", "ended_at"}:
        raise IntentContractError("WORK_SESSION_SEGMENT_INVALID")
    started = _timestamp(segment["started_at"])
    ended_value = segment["ended_at"]
    ended = None if ended_value is None else _timestamp(ended_value)
    if ended is not None and ended < started:
        raise IntentContractError("WORK_SESSION_SEGMENT_NEGATIVE")
    return started, ended


def _next_session_id(sessions: Sequence[Mapping[str, Any]]) -> str:
    largest = 0
    for session in sessions:
        match = re.fullmatch(r"WS-(\d+)", str(session.get("id", "")), re.I)
        if match:
            largest = max(largest, int(match.group(1)))
    return f"WS-{largest + 1:06d}"


def _items(value: object, code: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 20:
        raise IntentContractError(code)
    output = []
    for item in value:
        if not isinstance(item, str) or len(item.strip()) > 1000:
            raise IntentContractError(code)
        if item.strip():
            output.append(item.strip())
    return output


def _timestamp(value: object) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IntentContractError("WORK_SESSION_TIMESTAMP_INVALID")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise IntentContractError("WORK_SESSION_TIMESTAMP_INVALID") from error
    if parsed.utcoffset() != dt.timedelta(0) or parsed.microsecond:
        raise IntentContractError("WORK_SESSION_TIMESTAMP_INVALID")
    return parsed
