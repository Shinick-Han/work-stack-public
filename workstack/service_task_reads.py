"""Coherent Task reads and the frozen snapshot export.

Every projection here derives the planning status from the validated activity
history rather than from the stored field, so a reader can never report a
status the history does not support. The snapshot readers additionally require
a non-recovering consistent read: a frozen artifact must not be built from a
store that is still repairing itself.
"""

from __future__ import annotations

import copy
import re
import secrets
import uuid
from typing import Any, Mapping

from .context_projection import group_context_by_task
from .planning_status import task_facts, validate_and_project
from .service_domain import PRIORITIES, TASK_STATUSES, _find, _revision
from .service_errors import (
    SnapshotDisclosureRequiredError,
    SnapshotExportConflictError,
    SnapshotExportRefusedError,
    SnapshotStoreNotReadyError,
)
from .snapshot import SnapshotValidationError
from .snapshot_export import SnapshotArtifact, create_snapshot_artifact
from .storage.document_repository import WorkspaceDocument
from .storage.task_deletion_transaction import (
    commit_v3_task_deletion,
    preview_v3_task_deletion,
)
from .store import StoreCorruptError, StoreLockedError


class TaskReadMixin:
    """Task listing, detail, projection and snapshot export."""

    def list_tasks(self, status: str = "active") -> list[dict[str, Any]]:
        backlog = self.documents.load(WorkspaceDocument.TASKS)
        projection = validate_and_project(backlog, self.documents.load(WorkspaceDocument.ACTIVITY))
        tasks = []
        for source in backlog.get("tasks", []):
            task = copy.deepcopy(source)
            task["status"] = projection[task["id"]]
            tasks.append(task)
        if status == "active":
            tasks = [task for task in tasks if task.get("status") in ("open", "started")]
        elif status != "all":
            if status not in TASK_STATUSES:
                raise ValueError("invalid task status")
            tasks = [task for task in tasks if task.get("status") == status]
        return sorted(
            tasks,
            key=lambda task: (
                TASK_STATUSES.index(task.get("status", "open")),
                PRIORITIES.index(task.get("priority", "P2")),
                task.get("due") or "9999-12-31",
                task.get("id", ""),
            ),
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        backlog = self.documents.load(WorkspaceDocument.TASKS)
        task = copy.deepcopy(_find(backlog.get("tasks", []), task_id, "task"))
        projection = validate_and_project(backlog, self.documents.load(WorkspaceDocument.ACTIVITY))
        task["status"] = projection[task["id"]]
        return task

    def preview_task_deletion(
        self, task_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        return preview_v3_task_deletion(self.store, task_id, body)

    def commit_task_deletion(
        self,
        task_id: str,
        body: Mapping[str, Any],
        *,
        request_digest: str,
        path: str,
        idempotency_key: str,
        if_match: str | None,
        force_backup_failure: bool = False,
    ) -> dict[str, Any]:
        return commit_v3_task_deletion(
            self.store,
            task_id=task_id,
            body=body,
            request_digest=request_digest,
            path=path,
            idempotency_key=idempotency_key,
            if_match=if_match,
            force_backup_failure=force_backup_failure,
        )

    def _project_task(
        self,
        task: dict[str, Any],
        context_count: int = 0,
        *,
        planning_status: str | None = None,
    ) -> dict[str, Any]:
        projected = copy.deepcopy(task)
        task_uid = task.get("uid")
        try:
            parsed_uid = uuid.UUID(task_uid) if isinstance(task_uid, str) else None
        except ValueError as error:
            raise StoreCorruptError("persisted task UUID is invalid") from error
        if parsed_uid is None or parsed_uid.int == 0 or str(parsed_uid) != task_uid:
            raise StoreCorruptError("persisted task UUID is invalid")
        projected["uid"] = task_uid
        projected["revision"] = _revision(task)
        if planning_status is None:
            planning_status = self.get_task(task["id"])["status"]
        projected["status"] = planning_status
        projected.pop("status_fact_id", None)
        projected.setdefault("scheduled", None)
        projected.setdefault("estimate_minutes", None)
        projected["context_count"] = context_count
        return projected

    def task_detail(self, task_id: str) -> dict[str, Any]:
        with self.store.transaction():
            tasks = self.documents.load(WorkspaceDocument.TASKS).get("tasks", [])
            task = _find(tasks, task_id, "task")
            normalized_id = task["id"]
            contexts = group_context_by_task(
                self.documents.load(WorkspaceDocument.NOTES).get("notes", []),
                (self._project_capture(capture) for capture in
                 self.documents.load(WorkspaceDocument.CAPTURES).get("captures", [])),
                (item["id"] for item in tasks),
                (item["id"] for item in
                 self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", [])),
            )
            context = contexts[normalized_id]
            capture_ids = {item["id"] for item in context if item["ref"]["kind"] == "capture"}
            replies = [
                reply
                for reply in self.documents.load(WorkspaceDocument.REPLIES).get("replies", [])
                if reply.get("task_id") == normalized_id
            ]
            activity_data = self.documents.load(WorkspaceDocument.ACTIVITY)
            activity = [
                copy.deepcopy(event)
                for event in activity_data.get("activity", [])
                if event.get("task_id") == normalized_id
                or (
                    event.get("capture_id") in capture_ids
                    and not str(event.get("type", "")).startswith("reply.")
                )
            ]
            activity.extend(task_facts(activity_data, normalized_id))
            projected_status = validate_and_project(
                self.documents.load(WorkspaceDocument.TASKS), activity_data
            )[normalized_id]
            return {
                "task": self._project_task(
                    task, len(context), planning_status=projected_status
                ),
                "context": context,
                "replies": [
                    self._project_reply(reply)
                    for reply in sorted(replies, key=lambda item: item["id"])
                ],
                "activity": activity,
            }

    def planning_snapshot(self, task_id: str) -> SnapshotArtifact:
        """Freeze one committed task under one validated, non-recovering read."""

        try:
            with self.store.consistent_read():
                workspace = self.documents.load(WorkspaceDocument.WORKSPACE)
                backlog = self.documents.load(WorkspaceDocument.TASKS)
                activity = self.documents.load(WorkspaceDocument.ACTIVITY)
                task = copy.deepcopy(_find(backlog.get("tasks", []), task_id, "task"))
                planning_status = validate_and_project(backlog, activity)[task["id"]]
                return create_snapshot_artifact(
                    workspace["id"], task, planning_status
                )
        except SnapshotValidationError as error:
            raise SnapshotExportRefusedError(error) from error
        except (StoreCorruptError, StoreLockedError) as error:
            raise SnapshotStoreNotReadyError(
                "Snapshot export requires a fully ready store with no pending recovery."
            ) from error

    def confirmed_snapshot_export(
        self,
        task_id: str,
        expected_revision: int,
        expected_digest: str,
        disclosure_confirmed: bool,
    ) -> SnapshotArtifact:
        """Return reviewed canonical bytes without recording or mutating export state."""

        if disclosure_confirmed is not True:
            raise SnapshotDisclosureRequiredError(
                "Explicit snapshot disclosure confirmation is required."
            )
        if type(expected_revision) is not int or expected_revision < 0:
            raise SnapshotExportConflictError(
                "The reviewed snapshot revision is invalid or stale."
            )
        if not isinstance(expected_digest, str) or re.fullmatch(
            r"sha256:[0-9a-f]{64}", expected_digest
        ) is None:
            raise SnapshotExportConflictError(
                "The reviewed snapshot digest is invalid or stale."
            )
        artifact = self.planning_snapshot(task_id)
        if (
            artifact.snapshot["revision"] != expected_revision
            or not secrets.compare_digest(artifact.digest, expected_digest)
        ):
            raise SnapshotExportConflictError(
                "The task changed after review; reopen the disclosure before export."
            )
        return artifact
