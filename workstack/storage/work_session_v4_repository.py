"""Explicit-opt-in v4 Work Session lifecycle repository."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Sequence

from .canonical import canonical_sha256
from .intent_contract import IntentContractError, NormalizedIntent, replay_response
from .intent_v4_repository import V4IntentRepository, V4IntentRepositoryError
from .journal import JournalTarget
from .reader import V4ReadResult
from .runtime import RuntimeAuthority
from .work_session_contract import (
    canonical_session_worklog,
    fold_session_events,
    normalize_session_start,
    normalize_session_transition,
    normalize_session_worklog,
    project_session,
)


class V4WorkSessionRepository(V4IntentRepository):
    """Append lifecycle snapshots while keeping replay state outside authority."""

    def __init__(
        self,
        runtime: RuntimeAuthority,
        *,
        enable_v4_work_sessions: bool = False,
        now: Callable[[], str],
        today: Callable[[], str] | None = None,
        uid_factory: Callable[[], str],
    ) -> None:
        if enable_v4_work_sessions is not True:
            raise V4IntentRepositoryError("V4_WORK_SESSIONS_DISABLED")
        super().__init__(
            runtime,
            enable_v4_intents=True,
            now=now,
            uid_factory=uid_factory,
        )
        self._today = today or (lambda: self._timestamp()[:10])

    def projection(self) -> dict[str, Any]:
        physical, _ledger, _body, _generation = self._baseline()
        sessions = fold_session_events(physical.streams["worklog"])
        current = next(
            (
                session
                for session in sessions
                if session["state"] in {"running", "paused"}
            ),
            None,
        )
        pending = sorted(
            (
                session
                for session in sessions
                if session["state"] == "stopped"
                and session["worklog_state"] == "pending"
            ),
            key=lambda session: (session["updated_at"], session["id"]),
            reverse=True,
        )
        return {
            "current": (
                project_session(current, current_time=self._timestamp())
                if current is not None
                else None
            ),
            "pending": [project_session(session) for session in pending],
        }

    def start(
        self,
        body: Mapping[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/work-sessions",
    ) -> dict[str, Any]:
        physical, ledger, ledger_body, generation = self._baseline()
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise IntentContractError("WORK_SESSION_TASK_REQUIRED")
        task = self._task(physical, task_id)
        digest = canonical_sha256({"task_id": task["display_id"]})
        replay = self._raw_replay(ledger, idempotency_key, path, digest)
        if replay is not None:
            return replay
        timestamp = self._timestamp()
        intent = normalize_session_start(
            {"id": task["display_id"], "title": task["title"]},
            fold_session_events(physical.streams["worklog"]),
            timestamp=timestamp,
            work_date=self._work_date(),
            path=path,
        )
        return self._persist_session(
            physical,
            ledger,
            ledger_body,
            generation,
            intent,
            idempotency_key,
            task,
            operation_id=f"work-session-start-{idempotency_key}",
            response_status=201,
        )

    def transition(
        self,
        session_id: str,
        action: str,
        body: Mapping[str, Any],
        idempotency_key: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        del body  # v3 intentionally canonicalizes every transition body to {}.
        wanted = str(session_id).strip().upper()
        if action not in {"pause", "resume", "stop"}:
            raise IntentContractError("WORK_SESSION_ACTION_INVALID")
        path = path or f"/api/v1/work-sessions/{wanted}/{action}"
        physical, ledger, ledger_body, generation = self._baseline()
        replay = self._raw_replay(
            ledger, idempotency_key, path, canonical_sha256({})
        )
        if replay is not None:
            return replay
        session = self._session(physical, wanted)
        intent = normalize_session_transition(
            session, action, timestamp=self._timestamp(), path=path
        )
        task = self._task(physical, session["task_id"])
        return self._persist_session(
            physical,
            ledger,
            ledger_body,
            generation,
            intent,
            idempotency_key,
            task,
            operation_id=f"work-session-{action}-{idempotency_key}",
            response_status=200,
        )

    def record_worklog(
        self,
        session_id: str,
        body: Mapping[str, Any],
        idempotency_key: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        wanted = str(session_id).strip().upper()
        canonical = canonical_session_worklog(body)
        path = path or f"/api/v1/work-sessions/{wanted}/worklog"
        physical, ledger, ledger_body, generation = self._baseline()
        replay = self._raw_replay(
            ledger, idempotency_key, path, canonical_sha256(canonical)
        )
        if replay is not None:
            return replay
        session = self._session(physical, wanted)
        intent, entry = normalize_session_worklog(
            session, canonical, timestamp=self._timestamp(), path=path
        )
        task = self._task(physical, session["task_id"])
        session_event = self._session_event(physical.workspace_uid, intent, task)
        entry_event = self._entry_event(
            physical.workspace_uid, session, entry, task
        )
        targets = self._stream_targets(
            physical, (("worklog", session_event), ("worklog", entry_event))
        )
        return self._commit(
            physical,
            ledger,
            ledger_body,
            generation,
            intent,
            idempotency_key,
            targets,
            operation_id=f"work-session-worklog-{idempotency_key}",
            response_status=201,
        )

    @staticmethod
    def _raw_replay(
        ledger: Mapping[str, Any], key: str, path: str, digest: str
    ) -> dict[str, Any] | None:
        return replay_response(
            ledger["records"],
            key,
            method="POST",
            path=path,
            request_digest=digest,
        )

    @staticmethod
    def _session(physical: V4ReadResult, wanted: str) -> Mapping[str, Any]:
        matches = [
            item
            for item in fold_session_events(physical.streams["worklog"])
            if item["id"] == wanted
        ]
        if len(matches) != 1:
            raise IntentContractError("WORK_SESSION_NOT_FOUND")
        return matches[0]

    def _persist_session(
        self,
        physical: V4ReadResult,
        ledger: Mapping[str, Any],
        ledger_body: bytes | None,
        generation: int,
        intent: NormalizedIntent,
        key: str,
        task: Mapping[str, Any],
        *,
        operation_id: str,
        response_status: int,
    ) -> dict[str, Any]:
        event = self._session_event(physical.workspace_uid, intent, task)
        targets = self._stream_targets(physical, (("worklog", event),))
        return self._commit(
            physical,
            ledger,
            ledger_body,
            generation,
            intent,
            key,
            targets,
            operation_id=operation_id,
            response_status=response_status,
        )

    def _session_event(
        self,
        workspace_uid: str,
        intent: NormalizedIntent,
        task: Mapping[str, Any],
    ) -> dict[str, Any]:
        session = intent.authority_value
        return {
            **self._event_envelope(workspace_uid, task["uid"], "session"),
            "work_date": session["date"],
            "task_uid": task["uid"],
            "task_display_id": task["display_id"],
            "task_title": task["title"],
            "session_id": session["id"],
            "state": session["state"],
            "started_at": session["started_at"],
            "updated_at": session["updated_at"],
            "segments": copy.deepcopy(session["segments"]),
            "worklog_state": session["worklog_state"],
        }

    def _entry_event(
        self,
        workspace_uid: str,
        session: Mapping[str, Any],
        entry: Mapping[str, Any],
        task: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            **self._event_envelope(workspace_uid, task["uid"], "entry"),
            "work_date": session["date"],
            "task_uid": task["uid"],
            "task_display_id": task["display_id"],
            "task_title": task["title"],
            "done": copy.deepcopy(entry["done"]),
            "next": copy.deepcopy(entry["next"]),
            "blockers": copy.deepcopy(entry["blockers"]),
            "session_id": session["id"],
            "duration_seconds": entry["duration_seconds"],
        }

    def _event_envelope(
        self, workspace_uid: str, record_uid: str, kind: str
    ) -> dict[str, Any]:
        return {
            "format": "workstack.worklog-event",
            "schema_version": 1,
            "workspace_uid": workspace_uid,
            "event_uid": self._uid(),
            "record_uid": record_uid,
            "created_at": self._timestamp(),
            "actor": "local.user",
            "provenance": "workstack.work-session-v4",
            "kind": kind,
        }

    def _work_date(self) -> str:
        value = self._today()
        try:
            # Strict ISO round-trip, matching the v3 review-date boundary.
            from datetime import date

            parsed = date.fromisoformat(value)
        except (TypeError, ValueError) as error:
            raise IntentContractError("WORK_SESSION_DATE_INVALID") from error
        if parsed.isoformat() != value:
            raise IntentContractError("WORK_SESSION_DATE_INVALID")
        return value
