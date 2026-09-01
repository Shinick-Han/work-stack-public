"""Explicit-opt-in v4 planning-status transition repository."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from ..planning_status import append_transition, validate_and_project
from .runtime import RuntimeAuthority
from .task_repository import TaskRepositoryError, V4TaskRepository, _project_task


_STATUSES = {"open", "started", "done", "dropped"}


class V4PlanningRepository(V4TaskRepository):
    """Persist one status fact plus its Task revision without runtime replay state."""

    def __init__(
        self,
        authority_root: Path | str,
        runtime: RuntimeAuthority,
        *,
        enable_v4_planning: bool = False,
        task_note_source_indexes: Mapping[str, int] | None = None,
        clock: Callable[[], str],
    ) -> None:
        if enable_v4_planning is not True:
            raise TaskRepositoryError("v4_planning_disabled")
        super().__init__(
            authority_root,
            runtime,
            task_note_source_indexes=task_note_source_indexes,
            clock=clock,
            enable_v4_task_commands=True,
        )

    def set_task_status(
        self,
        task_id: str,
        status: str,
        expected_revision: int | None = None,
        *,
        provenance: str = "cli",
    ) -> dict:
        if status not in _STATUSES:
            raise TaskRepositoryError("status_invalid")
        if provenance not in {"cli", "api.legacy"}:
            raise TaskRepositoryError("provenance_invalid")
        return self._transition_status(
            task_id, status, expected_revision, provenance=provenance
        )

    def patch_status(
        self, task_id: str, status: str, expected_revision: int
    ) -> dict:
        if status not in _STATUSES:
            raise TaskRepositoryError("status_invalid")
        return self._transition_status(
            task_id, status, expected_revision, provenance="api.v1"
        )

    def _transition_status(
        self,
        task_id: str,
        status: str,
        expected_revision: int | None,
        *,
        provenance: str,
    ) -> dict:
        current, ledger, documents, generation = self._load()
        task = next(
            (
                item
                for item in documents["backlog.json"]["tasks"]
                if item["id"].upper() == task_id.upper()
            ),
            None,
        )
        if task is None:
            raise TaskRepositoryError("not_found")
        if expected_revision is None:
            expected_revision = task["revision"]
        if type(expected_revision) is not int or expected_revision < 0:
            raise TaskRepositoryError("revision_invalid")
        if task["revision"] != expected_revision:
            raise TaskRepositoryError("revision_conflict")
        current_status = validate_and_project(
            documents["backlog.json"], documents["activity.json"]
        )[task["id"]]
        if current_status == status:
            projected = _project_task(task)
            projected["status"] = current_status
            return projected
        now = self.clock()
        next_revision = expected_revision + 1
        append_transition(
            documents["activity.json"],
            task,
            prior_status=current_status,
            status=status,
            prior_revision=expected_revision,
            new_revision=next_revision,
            created_at=now,
            actor="local.user",
            provenance=provenance,
        )
        task["updated_at"] = now[:10]
        task["revision"] = next_revision
        self._commit(
            current,
            ledger,
            documents,
            generation,
            now,
            f"task-status-{task['id']}-r{next_revision}",
            False,
        )
        projected = _project_task(task)
        projected["status"] = status
        return projected
