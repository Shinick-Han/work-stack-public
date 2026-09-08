"""The CLI owner routes: framing only, on the one supported composition.

Each method validates the exact field set the CLI sends and then calls the
long-standing domain operation unchanged, so the CLI keeps the legacy rules
rather than growing a second copy of them. All of them first require the
released same-Store composition with no alternate delegate configured: an owner
route may not silently run against a backend that would answer differently.
"""

from __future__ import annotations

from typing import Any

from .service_composition import _attributed_released_composition
from .service_errors import DomainError


class CliOwnerCommandsMixin:
    """Field framing and composition gating for the CLI owner routes."""

    def add_task_cli(self, body: dict[str, Any]) -> dict[str, Any]:
        """Frame an ordinary create without changing its legacy domain rules."""
        fields = {"title", "detail", "priority", "due", "tags", "objective_ids", "parent_id", "dependencies"}
        if type(body) is not dict or set(body) != fields:
            raise DomainError("CLI backlog add requires exactly its eight creation fields")
        if any(type(body[field]) is not str for field in ("title", "detail", "priority")):
            raise DomainError("CLI backlog title, detail and priority must be strings")
        if any(body[field] is not None and type(body[field]) is not str for field in ("due", "parent_id")):
            raise DomainError("CLI backlog due and parent_id must be strings or null")
        for field in ("tags", "objective_ids", "dependencies"):
            if type(body[field]) is not list or any(type(item) is not str for item in body[field]):
                raise DomainError("CLI backlog collections must be arrays of strings")
        self._require_cli_owner_composition("backlog add")
        return self.add_task(
            title=body["title"], detail=body["detail"], priority=body["priority"], due=body["due"],
            tags=body["tags"], objective_ids=body["objective_ids"], parent_id=body["parent_id"],
            dependencies=body["dependencies"],
        )

    def link_task_cli(self, body: dict[str, Any]) -> dict[str, Any]:
        """Preserve the legacy link lookup, revision and retained-reference rules."""
        if type(body) is not dict or set(body) != {"objective_id", "task_id"}:
            raise DomainError("CLI OKR link requires only objective_id and task_id")
        if any(type(body[field]) is not str for field in ("objective_id", "task_id")):
            raise DomainError("CLI OKR link identifiers must be strings")
        self._require_cli_owner_composition("OKR link")
        return self.link_task(objective_id=body["objective_id"], task_id=body["task_id"])

    def set_key_result_progress_cli(self, body: dict[str, Any]) -> dict[str, Any]:
        """Frame legacy progress; its setter owns clamping, revisions and audit."""
        if type(body) is not dict or set(body) != {"objective_id", "key_result_id", "progress"}:
            raise DomainError("CLI OKR progress requires only objective_id, key_result_id and progress")
        if any(type(body[field]) is not str for field in ("objective_id", "key_result_id")):
            raise DomainError("CLI OKR progress identifiers must be strings")
        if type(body["progress"]) is not int:
            raise DomainError("CLI OKR progress must be an integer")
        self._require_cli_owner_composition("OKR progress")
        return self.set_key_result_progress(
            objective_id=body["objective_id"], key_result_id=body["key_result_id"], progress=body["progress"],
        )

    def checkin_cli(self, body: dict[str, Any]) -> dict[str, Any]:
        """Run the existing CLI operation only on its supported owner Store."""
        if type(body) is not dict or set(body) != {"date", "time"}:
            raise DomainError("CLI checkin requires only date and time")
        if any(type(body[field]) is not str for field in ("date", "time")):
            raise DomainError("CLI checkin date and time must be strings")
        self._require_cli_owner_composition("checkin")
        return self.checkin(time=body["time"], date=body["date"])

    def _require_cli_owner_composition(self, operation: str) -> None:
        """Require released same-Store documents and no alternate delegates."""
        delegates = (
            self.capture_reply_commands, self.intent_commands,
            self.objective_commands, self.task_commands,
            self.relationship_commands, self.planning_commands,
            self.work_session_commands, self.query_commands,
        )
        if not _attributed_released_composition(self) or any(
            delegate is not None for delegate in delegates
        ):
            raise DomainError("CLI {} is not supported by this storage composition".format(operation))

    def add_worklog_cli(self, body: dict[str, Any]) -> dict[str, Any]:
        """Frame an ordinary entry, leaving its domain rules to add_worklog."""
        fields = {"task_id", "date", "done", "next_items", "blockers"}
        if type(body) is not dict or set(body) != fields:
            raise DomainError("CLI worklog add requires only task_id, date and categories")
        if any(type(body[field]) is not str for field in ("task_id", "date")):
            raise DomainError("CLI worklog task_id and date must be strings")
        for field in ("done", "next_items", "blockers"):
            if type(body[field]) is not list or any(type(item) is not str for item in body[field]):
                raise DomainError("CLI worklog categories must be arrays of strings")
        self._require_cli_owner_composition("worklog add")
        return self.add_worklog(
            task_id=body["task_id"], date=body["date"], done=body["done"],
            next_items=body["next_items"], blockers=body["blockers"],
        )
