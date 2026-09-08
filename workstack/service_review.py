"""Check-in, worklog entries and the review/weekly projections.

The attributed review entry is the one write that publishes: its immutable
commit facts are built BEFORE any document is mutated, the committed notice is
proven buildable under the same outer lock, and publication happens only after
the save succeeded. Ordinary and browser entries record their fact and stay
silent.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from typing import Any, Iterable

from .checkpoint_change import (
    CheckpointChangeError,
    build_checkpoint_facts,
    build_committed_notice,
)
from .service_composition import (
    _attributed_released_v3_only,
    _optional_command_backend,
    _transactional,
)
from .service_domain import _next_id
from .service_errors import DomainError
from .service_graph import _weekly_objectives, _weekly_projects, _weekly_range
from .storage.document_repository import WorkspaceDocument


class ReviewServiceMixin:
    """Daily check-in, worklog entries and the review and weekly reads."""

    @_transactional
    def checkin(self, time: str | None = None, date: str | None = None) -> dict[str, Any]:
        date = date or self._today()
        dt.date.fromisoformat(date)
        if time is None:
            time = self._local_time()
        if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", time):
            raise ValueError("time must use HH:MM")
        data = self.documents.load(WorkspaceDocument.WORKLOG)
        day = data.setdefault("days", {}).setdefault(date, {"entries": []})
        day["start_time"] = time
        self.documents.save(WorkspaceDocument.WORKLOG, data)
        return {"date": date, "start_time": time}

    @_transactional
    def add_worklog(
        self,
        task_id: str,
        done: Iterable[str] = (),
        next_items: Iterable[str] = (),
        blockers: Iterable[str] = (),
        date: str | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        date = date or self._today()
        dt.date.fromisoformat(date)
        data = self.documents.load(WorkspaceDocument.WORKLOG)
        day = data.setdefault("days", {}).setdefault(date, {"entries": []})
        entry = {
            "task_id": task["id"],
            "task": task["title"],
            "done": [str(item).strip() for item in done if str(item).strip()],
            "next": [str(item).strip() for item in next_items if str(item).strip()],
            "blockers": [str(item).strip() for item in blockers if str(item).strip()],
        }
        if not any(entry[key] for key in ("done", "next", "blockers")):
            raise ValueError("at least one worklog item is required")
        day["entries"].append(entry)
        self.documents.save(WorkspaceDocument.WORKLOG, data)
        return {"date": date, **entry}

    @staticmethod
    def _review_date(value: Any) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise DomainError("date must use YYYY-MM-DD", {"field": "date"})
        try:
            parsed = dt.date.fromisoformat(value)
        except ValueError as error:
            raise DomainError("date is invalid", {"field": "date"}) from error
        if parsed.isoformat() != value:
            raise DomainError("date must use YYYY-MM-DD", {"field": "date"})
        return value

    @staticmethod
    def _review_items(value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or len(value) > 20:
            raise DomainError(
                "{} must be an array with at most 20 items".format(field),
                {"field": field},
            )
        output: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise DomainError("{} items must be strings".format(field), {"field": field})
            normalized = item.strip()
            if len(normalized) > 1000:
                raise DomainError(
                    "{} items must be at most 1000 characters".format(field),
                    {"field": field},
                )
            if normalized:
                output.append(normalized)
        return output

    @_optional_command_backend("intent_commands", "checkin", "intent")
    @_transactional
    def checkin_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/review/checkin",
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        date = self._review_date(body.get("date"))
        time = body.get("time")
        if not isinstance(time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time):
            raise DomainError("time must use HH:MM", {"field": "time"})
        canonical = {"date": date, "time": time}
        request_digest = self._request_digest(canonical)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        day = worklog.setdefault("days", {}).setdefault(date, {"entries": []})
        day["start_time"] = time
        response_body = {
            "data": {"date": date, "start_time": time},
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.WORKLOG: worklog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="review-checkin-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    def _canonical_review_entry(
        self, body: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The resolved Task and the canonical entry, before any receipt lookup."""

        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise DomainError("task_id is required", {"field": "task_id"})
        task = self.get_task(task_id.strip().upper())
        canonical = {
            "date": self._review_date(body.get("date")),
            "task_id": task["id"],
            "done": self._review_items(body.get("done"), "done"),
            "next": self._review_items(body.get("next"), "next"),
            "blockers": self._review_items(body.get("blockers"), "blockers"),
        }
        if not any(canonical[field] for field in ("done", "next", "blockers")):
            raise DomainError("at least one worklog item is required")
        return task, canonical

    @staticmethod
    def _prior_physical_entries(
        days: dict[str, Any], date: str, ordinal: int
    ) -> list[dict[str, Any]]:
        """EVERY previously accepted physical entry, flattened across ALL dates.

        There is deliberately no date filter. A stored entry dated LATER than
        the one being appended was still accepted earlier, so a backdated
        append is not the first for its Task. Filtering by date made exactly
        that case report first_for_task true. Browser and legacy entries count
        too: "first for this Task" is a fact about the stored Worklog, not
        about attributed writes, and nothing here rewrites or reorders it.

        ``ordinal`` is the date-local PHYSICAL ordinal captured before the
        append, so the entry's own date contributes only the rows ahead of it.
        """

        prior_entries: list[dict[str, Any]] = []
        for stored_date in sorted(days):
            stored = days[stored_date].get("entries")
            if not isinstance(stored, list):
                continue
            prior_entries.extend(stored[:ordinal] if stored_date == date else stored)
        return prior_entries

    @_attributed_released_v3_only("intent_commands", "add_worklog", "intent")
    @_transactional
    def add_worklog_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/review/entries",
        origin: str | None = None,
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        task, canonical = self._canonical_review_entry(body)
        request_digest = self._request_digest(canonical)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        worklog = self.documents.load(WorkspaceDocument.WORKLOG)
        days = worklog.setdefault("days", {})
        day = days.setdefault(canonical["date"], {"entries": []})
        entries = day.setdefault("entries", [])
        entry = {
            "task_id": task["id"],
            "task": task["title"],
            "done": canonical["done"],
            "next": canonical["next"],
            "blockers": canonical["blockers"],
        }
        ordinal = len(entries)
        prior_entries = self._prior_physical_entries(days, canonical["date"], ordinal)
        facts = self._checkpoint_facts(
            idempotency_key=idempotency_key,
            date=canonical["date"],
            entry=entry,
            ordinal=ordinal,
            prior_entries=prior_entries,
            origin=origin,
        )
        # TP-F3 preflight, under the SAME outer transaction lock and before any
        # fresh document mutation: the committed notice must already be
        # buildable, and the shared event sequence must still have room for both
        # the manifest event this commit will emit and the typed event published
        # after it. Publication itself still happens after a successful save.
        # This validates capacity; it allocates nothing, adds no counter and
        # makes no crash-atomicity claim.
        if facts["recorded"]["origin"] is not None:
            self._preflight_committed_notice(facts)
        entries.append(entry)
        response_body = {
            "data": {"date": canonical["date"], **copy.deepcopy(entry)},
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        # The recorded fact rides the EXISTING Worklog+Activity save_many beside
        # the existing idempotency receipt. It is carried as an ordinary
        # Activity record rather than a new document key: the Activity document
        # schema is validated by exact key set, so inventing a top-level list would be
        # a storage schema change and would fail every existing store. No
        # Worklog entry field, public response, Task status or revision changes,
        # and no history is rewritten.
        entries_feed = activity.setdefault("activity", [])
        entries_feed.append({
            "id": _next_id(entries_feed, "E", 6),
            "type": "worklog.recorded",
            "created_at": self._utc_now(),
            "task_id": facts["recorded"]["task_id"],
            "details": copy.deepcopy(facts["recorded"]),
        })
        self.documents.save_many(
            {WorkspaceDocument.WORKLOG: worklog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="review-entry-{}".format(idempotency_key),
        )
        # Published after the save succeeded and while the SAME outer
        # transaction lock is still held, before any HTTP serialization. Only an
        # attributed write publishes; an ordinary or browser write records its
        # fact and stays silent.
        if facts["recorded"]["origin"] is not None:
            self.store.publish_change_notice(
                lambda event_id: build_committed_notice(facts=facts, event_id=event_id)
            )
        return {"status": 201, "body": response_body}

    def _preflight_committed_notice(self, facts: dict[str, Any]) -> None:
        """Prove the notice is buildable at the id publication would use.

        The projected id accounts for the manifest event the committing save
        emits before publication, so the safe-integer ceiling is refused here
        rather than after Worklog, Activity, the recorded fact and the receipt
        have already been committed. The pure builder and its frozen twelve
        fields are reused unchanged; the result is discarded.
        """

        try:
            build_committed_notice(
                facts=facts, event_id=self.store.projected_change_event_id()
            )
        except CheckpointChangeError as error:
            raise DomainError("the review entry could not be recorded") from error

    def _checkpoint_facts(
        self,
        *,
        idempotency_key: str,
        date: str,
        entry: dict[str, Any],
        ordinal: int,
        prior_entries: list[dict[str, Any]],
        origin: str | None,
    ) -> dict[str, Any]:
        """Build the immutable commit facts BEFORE any document is mutated.

        The admitted pure builder decides identity, locator, counts and
        physical-first semantics; nothing is recomputed here. A refusal is
        content-free and carries no input value.
        """

        readiness = self.store.readiness
        if readiness is None:
            raise DomainError("the workspace is not ready to record a checkpoint")
        try:
            return build_checkpoint_facts(
                workspace_uid=readiness.workspace_uid,
                idempotency_key=idempotency_key,
                date=date,
                entry=entry,
                ordinal=ordinal,
                prior_entries=prior_entries,
                origin=origin,
            )
        except CheckpointChangeError as error:
            raise DomainError("the review entry could not be recorded") from error

    @_transactional
    def review_projection(self, date: str, days: int = 7) -> dict[str, Any]:
        date = self._review_date(date)
        if type(days) is not int or days < 1 or days > 31:
            raise DomainError("days must be between 1 and 31", {"field": "days"})
        worklog = self._active_worklog()
        day = worklog.get("days", {}).get(date, {})
        return {
            "day": {
                "date": date,
                "start_time": day.get("start_time"),
                "entries": copy.deepcopy(day.get("entries", [])),
            },
            "weekly": self.weekly_report(end=date, days=days),
        }

    @_transactional
    def list_worklog(self, date: str | None = None) -> dict[str, Any]:
        # Transactional because the active view assembles TWO documents: without
        # one outer owner the Worklog and the Activity could come from different
        # committed states. The already transactional readers are unchanged.
        data = self._active_worklog()
        if date:
            dt.date.fromisoformat(date)
            return {"date": date, "entries": data.get("days", {}).get(date, {}).get("entries", [])}
        return data

    @_transactional
    def weekly_report(self, end: str | None = None, days: int = 7) -> dict[str, Any]:
        start_day, end_day = _weekly_range(end, days, self._local_day())
        tasks = {task["id"]: task for task in self.list_tasks(status="all")}
        objectives = {item["id"]: item for item in self.list_objectives(status="all")}
        worklog = self._active_worklog().get("days", {})
        projects = _weekly_projects(worklog, tasks, start_day, end_day)
        return {
            "range": {"start": start_day.isoformat(), "end": end_day.isoformat(), "days": days},
            "objectives": _weekly_objectives(projects, objectives),
            "projects": list(projects.values()),
        }
