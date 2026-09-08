"""Work sessions: persisted-shape validation, projection and transitions.

A work session is reconstructed from the Worklog on every read and completely
re-validated: identifiers, dates, states, the worklog-readiness pairing and the
segment ladder must all agree, and at most one session may be active across the
whole document. Elapsed time is derived from the segments rather than stored,
so a running session cannot drift from its own history.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from typing import Any

from .service_composition import _optional_command_backend, _transactional
from .service_domain import _find, _next_id
from .service_errors import DomainError, WorkSessionConflictError
from .storage.document_repository import WorkspaceDocument
from .store import StoreCorruptError


class WorkSessionServiceMixin:
    """Persisted work-session validation, projection and state transitions."""

    @staticmethod
    def _work_session_timestamp(value: Any, field: str) -> dt.datetime:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise StoreCorruptError("persisted work session {} is invalid".format(field))
        try:
            parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as error:
            raise StoreCorruptError(
                "persisted work session {} is invalid".format(field)
            ) from error
        if parsed.tzinfo != dt.timezone.utc or parsed.microsecond:
            raise StoreCorruptError("persisted work session {} is invalid".format(field))
        return parsed

    @classmethod
    def _work_session_elapsed(
        cls, session: dict[str, Any], *, current_time: str | None = None
    ) -> int:
        now = cls._work_session_timestamp(current_time, "current_time") if current_time else None
        elapsed = 0
        for segment in session["segments"]:
            started = cls._work_session_timestamp(segment["started_at"], "segment.started_at")
            ended_value = segment.get("ended_at")
            ended = (
                cls._work_session_timestamp(ended_value, "segment.ended_at")
                if ended_value is not None
                else now
            )
            if ended is None:
                raise StoreCorruptError("persisted running work session has no current time")
            if ended < started:
                raise StoreCorruptError("persisted work session segment has negative duration")
            elapsed += int((ended - started).total_seconds())
        return elapsed

    @classmethod
    def _work_session_day(
        cls, date: Any, day: Any
    ) -> tuple[str, list[Any]]:
        try:
            valid_date = cls._review_date(date)
        except DomainError as error:
            raise StoreCorruptError("persisted worklog date is invalid") from error
        if not isinstance(day, dict):
            raise StoreCorruptError("persisted worklog day is invalid")
        sessions = day.get("sessions", [])
        if not isinstance(sessions, list):
            raise StoreCorruptError("persisted work sessions are invalid")
        return valid_date, sessions

    @classmethod
    def _work_session_header(
        cls, candidate: Any, valid_date: str, seen: set[str]
    ) -> tuple[dict[str, Any], str]:
        if not isinstance(candidate, dict):
            raise StoreCorruptError("persisted work session is invalid")
        session_id = candidate.get("id")
        if (
            not isinstance(session_id, str)
            or not re.fullmatch(r"WS-\d{6,}", session_id)
            or session_id in seen
        ):
            raise StoreCorruptError("persisted work session id is invalid")
        seen.add(session_id)
        if candidate.get("date") != valid_date:
            raise StoreCorruptError("persisted work session date is invalid")
        task_id = candidate.get("task_id")
        if not isinstance(task_id, str) or not re.fullmatch(r"T-\d+", task_id):
            raise StoreCorruptError("persisted work session task id is invalid")
        task_title = candidate.get("task")
        if not isinstance(task_title, str) or not task_title.strip():
            raise StoreCorruptError("persisted work session task title is invalid")
        state = candidate.get("state")
        if state not in {"running", "paused", "stopped"}:
            raise StoreCorruptError("persisted work session state is invalid")
        expected_worklog_states = (
            {"not_ready"} if state in {"running", "paused"} else {"pending", "recorded"}
        )
        if candidate.get("worklog_state") not in expected_worklog_states:
            raise StoreCorruptError("persisted work session worklog state is invalid")
        cls._work_session_timestamp(candidate.get("started_at"), "started_at")
        cls._work_session_timestamp(candidate.get("updated_at"), "updated_at")
        return candidate, state

    @classmethod
    def _work_session_segment(
        cls,
        candidate: Any,
        *,
        index: int,
        count: int,
        previous_end: dt.datetime | None,
    ) -> tuple[dt.datetime | None, bool]:
        if not isinstance(candidate, dict) or set(candidate) != {"started_at", "ended_at"}:
            raise StoreCorruptError("persisted work session segment is invalid")
        segment_start = cls._work_session_timestamp(
            candidate["started_at"], "segment.started_at"
        )
        if previous_end is not None and segment_start < previous_end:
            raise StoreCorruptError("persisted work session segments overlap")
        if candidate["ended_at"] is None:
            if index != count - 1:
                raise StoreCorruptError("persisted work session open segment is invalid")
            return None, True
        segment_end = cls._work_session_timestamp(
            candidate["ended_at"], "segment.ended_at"
        )
        if segment_end < segment_start:
            raise StoreCorruptError("persisted work session segment has negative duration")
        return segment_end, False

    @classmethod
    def _validate_work_session_segments(
        cls, session: dict[str, Any], state: str
    ) -> None:
        segments = session.get("segments")
        if not isinstance(segments, list) or not segments:
            raise StoreCorruptError("persisted work session segments are invalid")
        previous_end: dt.datetime | None = None
        open_segments = 0
        for index, segment in enumerate(segments):
            previous_end, is_open = cls._work_session_segment(
                segment,
                index=index,
                count=len(segments),
                previous_end=previous_end,
            )
            open_segments += int(is_open)
        if (state == "running") != (open_segments == 1):
            raise StoreCorruptError("persisted work session open segment is inconsistent")
        if state in {"paused", "stopped"} and open_segments:
            raise StoreCorruptError("persisted work session open segment is inconsistent")

    @classmethod
    def _work_session_records(cls, worklog: dict[str, Any]) -> list[dict[str, Any]]:
        days = worklog.get("days")
        if not isinstance(days, dict):
            raise StoreCorruptError("persisted worklog days are invalid")
        sessions: list[dict[str, Any]] = []
        seen: set[str] = set()
        active_count = 0
        for date, day in days.items():
            valid_date, candidates = cls._work_session_day(date, day)
            for candidate in candidates:
                session, state = cls._work_session_header(candidate, valid_date, seen)
                cls._validate_work_session_segments(session, state)
                active_count += int(state in {"running", "paused"})
                sessions.append(session)
        if active_count > 1:
            raise StoreCorruptError("multiple active work sessions are persisted")
        return sessions

    @classmethod
    def _project_work_session(
        cls, session: dict[str, Any], *, current_time: str | None = None
    ) -> dict[str, Any]:
        return {
            "id": session["id"],
            "task_id": session["task_id"],
            "task": session["task"],
            "date": session["date"],
            "state": session["state"],
            "started_at": session["started_at"],
            "updated_at": session["updated_at"],
            "elapsed_seconds": cls._work_session_elapsed(
                session, current_time=current_time if session["state"] == "running" else None
            ),
            "worklog_state": session["worklog_state"],
        }

    @_optional_command_backend(
        "work_session_commands", "projection", "intent"
    )
    @_transactional
    def work_sessions_projection(self) -> dict[str, Any]:
        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        sessions = self._work_session_records(worklog)
        current_time = self._utc_now()
        current = next(
            (session for session in sessions if session["state"] in {"running", "paused"}),
            None,
        )
        pending = sorted(
            (
                session
                for session in sessions
                if session["state"] == "stopped" and session["worklog_state"] == "pending"
            ),
            key=lambda session: (session["updated_at"], session["id"]),
            reverse=True,
        )
        return {
            "current": (
                self._project_work_session(current, current_time=current_time)
                if current is not None
                else None
            ),
            "pending": [self._project_work_session(session) for session in pending],
        }

    @_optional_command_backend(
        "work_session_commands", "start", "intent"
    )
    @_transactional
    def start_work_session_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/work-sessions",
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise DomainError("task_id is required", {"field": "task_id"})
        task = self.get_task(task_id.strip().upper())
        canonical = {"task_id": task["id"]}
        request_digest = self._request_digest(canonical)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        sessions = self._work_session_records(worklog)
        if any(session["state"] in {"running", "paused"} for session in sessions):
            raise WorkSessionConflictError("another work session is already active")
        timestamp = self._utc_now()
        date = self._review_date(self._today())
        session = {
            "id": _next_id(sessions, "WS", 6),
            "task_id": task["id"],
            "task": task["title"],
            "date": date,
            "state": "running",
            "started_at": timestamp,
            "updated_at": timestamp,
            "segments": [{"started_at": timestamp, "ended_at": None}],
            "worklog_state": "not_ready",
        }
        worklog.setdefault("days", {}).setdefault(date, {"entries": []}).setdefault(
            "sessions", []
        ).append(session)
        response_body = {
            "data": self._project_work_session(session, current_time=timestamp),
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.WORKLOG: worklog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="work-session-start-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_optional_command_backend(
        "work_session_commands", "transition", "intent"
    )
    @_transactional
    def transition_work_session_v1(
        self,
        session_id: str,
        action: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        wanted_id = str(session_id).strip().upper()
        if action not in {"pause", "resume", "stop"}:
            raise DomainError("work session action is invalid", {"action": action})
        canonical: dict[str, Any] = {}
        request_digest = self._request_digest(canonical)
        path = path or "/api/v1/work-sessions/{}/{}".format(wanted_id, action)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        sessions = self._work_session_records(worklog)
        session = _find(sessions, wanted_id, "work session")
        expected_state = {"pause": "running", "resume": "paused"}.get(action)
        if action == "stop":
            allowed = {"running", "paused"}
        else:
            allowed = {expected_state}
        if session["state"] not in allowed:
            raise WorkSessionConflictError(
                "cannot {} a {} work session".format(action, session["state"]),
                {"session_id": session["id"], "state": session["state"]},
            )
        timestamp = self._utc_now()
        if action in {"pause", "stop"} and session["state"] == "running":
            session["segments"][-1]["ended_at"] = timestamp
        if action == "resume":
            session["segments"].append({"started_at": timestamp, "ended_at": None})
        session["state"] = {"pause": "paused", "resume": "running", "stop": "stopped"}[
            action
        ]
        if action == "stop":
            session["worklog_state"] = "pending"
        session["updated_at"] = timestamp
        response_body = {
            "data": self._project_work_session(session, current_time=timestamp),
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 200, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.WORKLOG: worklog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="work-session-{}-{}".format(action, idempotency_key),
        )
        return {"status": 200, "body": response_body}

    @_optional_command_backend(
        "work_session_commands", "record_worklog", "intent"
    )
    @_transactional
    def record_work_session_v1(
        self,
        session_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        wanted_id = str(session_id).strip().upper()
        canonical = {
            "done": self._review_items(body.get("done"), "done"),
            "next": self._review_items(body.get("next"), "next"),
            "blockers": self._review_items(body.get("blockers"), "blockers"),
        }
        if not any(canonical[field] for field in ("done", "next", "blockers")):
            raise DomainError("at least one worklog item is required")
        request_digest = self._request_digest(canonical)
        path = path or "/api/v1/work-sessions/{}/worklog".format(wanted_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        sessions = self._work_session_records(worklog)
        session = _find(sessions, wanted_id, "work session")
        if session["state"] != "stopped" or session["worklog_state"] != "pending":
            raise WorkSessionConflictError(
                "work session is not ready for a worklog",
                {"session_id": session["id"], "state": session["state"]},
            )
        entry = {
            "task_id": session["task_id"],
            "task": session["task"],
            "done": canonical["done"],
            "next": canonical["next"],
            "blockers": canonical["blockers"],
            "session_id": session["id"],
            "duration_seconds": self._work_session_elapsed(session),
        }
        worklog["days"][session["date"]].setdefault("entries", []).append(entry)
        session["worklog_state"] = "recorded"
        session["updated_at"] = self._utc_now()
        response_body = {
            "data": {"date": session["date"], **copy.deepcopy(entry)},
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.WORKLOG: worklog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="work-session-worklog-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}
