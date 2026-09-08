"""Every write that changes a Task, its subtasks, its notes or its status.

The released v3 implementations live here; a configured Task, relationship or
planning backend replaces them through the decorators in
``service_composition``. All of them run inside the facade's outer transaction
and end in ONE ``save_many`` so a Task, its planning fact and its idempotency
receipt commit together.
"""

from __future__ import annotations

from typing import Any, Iterable

from .planning_status import append_bootstrap, append_transition, validate_and_project
from .service_composition import (
    _optional_command_backend,
    _released_v3_attributed_composition_owner,
    _transactional,
)
from .service_domain import (
    PRIORITIES,
    TASK_STATUSES,
    _find,
    _guard_revision,
    _next_id,
    _next_revision,
    _required_text,
    _revision,
)
from .service_errors import (
    DomainError,
    RevisionConflictError,
    TaskDisplayIdAuthorityError,
)
from .service_task_rules import (
    _new_task_record,
    _normalize_new_task_relationships,
    _task_create_date,
    _task_create_estimate,
    _task_create_string_list,
    _task_create_text_fields,
    _validate_new_task_schedule,
    _validate_task_create_shape,
)
from .storage.document_repository import WorkspaceDocument
from .task_display_id import (
    TaskDisplayIdError,
    allocate_create,
    persist_field,
    read_optional_high_water,
    task_ids_from_records,
)


class TaskCommandsMixin:
    """Task, subtask, note and status writes for the released v3 store."""

    def _next_task_display(
        self, tasks: list[dict[str, Any]]
    ) -> tuple[str, dict[str, Any]]:
        workspace = self.documents.load(WorkspaceDocument.WORKSPACE)
        try:
            display_id, high_water = allocate_create(
                read_optional_high_water(workspace),
                task_ids_from_records(tasks),
            )
        except TaskDisplayIdError as error:
            raise TaskDisplayIdAuthorityError(error.code) from error
        persist_field(workspace, high_water)
        return display_id, workspace

    def _append_task(
        self,
        data: dict[str, Any],
        title: str,
        detail: str = "",
        priority: str = "P2",
        due: str | None = None,
        tags: Iterable[str] = (),
        objective_ids: Iterable[str] = (),
        parent_id: str | None = None,
        dependencies: Iterable[str] = (),
        scheduled: str | None = None,
        estimate_minutes: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate the normal Task fields and append a new Task to ``data``."""

        _validate_new_task_schedule(priority, due, scheduled, estimate_minutes)
        normalized_parent, normalized_dependencies = _normalize_new_task_relationships(
            data["tasks"], parent_id, dependencies
        )
        task_id, workspace = self._next_task_display(data["tasks"])
        workspace_id = self.documents.load(WorkspaceDocument.WORKSPACE)["id"]
        task = _new_task_record(
            day=self._today,
            task_id=task_id,
            workspace_id=workspace_id,
            title=title,
            detail=detail,
            priority=priority,
            due=due,
            scheduled=scheduled,
            estimate_minutes=estimate_minutes,
            tags=tags,
            objective_ids=objective_ids,
            parent_id=normalized_parent,
            dependencies=normalized_dependencies,
        )
        known = {item["id"] for item in self.list_objectives(status="all")}
        unknown = sorted(set(task["objective_ids"]) - known)
        if unknown:
            raise ValueError("unknown objective ids: {}".format(", ".join(unknown)))
        data["tasks"].append(task)
        return task, workspace

    @_transactional
    def add_task(
        self,
        title: str,
        detail: str = "",
        priority: str = "P2",
        due: str | None = None,
        tags: Iterable[str] = (),
        objective_ids: Iterable[str] = (),
        parent_id: str | None = None,
        dependencies: Iterable[str] = (),
    ) -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.TASKS)
        task, workspace = self._append_task(
            data,
            title,
            detail,
            priority,
            due,
            tags,
            objective_ids,
            parent_id,
            dependencies,
        )
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        append_bootstrap(
            activity,
            task,
            created_at=self._utc_now(),
            actor="local.user",
            provenance="cli",
        )
        self.documents.save_many(
            {
                WorkspaceDocument.TASKS: data,
                WorkspaceDocument.ACTIVITY: activity,
                WorkspaceDocument.WORKSPACE: workspace,
            },
            operation_id="task-create-cli-{}".format(task["id"]),
        )
        return task

    @staticmethod
    def _validate_task_create_v1(body: dict[str, Any]) -> dict[str, Any]:
        """Return the canonical, strict v1 task-create payload."""

        body = _validate_task_create_shape(body)
        title, detail, priority = _task_create_text_fields(body)
        due = _task_create_date(body, "due")
        scheduled = _task_create_date(body, "scheduled")
        estimate_minutes = _task_create_estimate(body)

        return {
            "title": title,
            "detail": detail,
            "priority": priority,
            "due": due,
            "scheduled": scheduled,
            "estimate_minutes": estimate_minutes,
            "tags": _task_create_string_list(body, "tags"),
            "objective_ids": _task_create_string_list(
                body, "objective_ids", uppercase=True
            ),
        }

    @_optional_command_backend(
        "task_commands", "create_task_v1", "task"
    )
    @_transactional
    def create_task_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/tasks",
    ) -> dict[str, Any]:
        """Create one Task for one logical browser intent, or replay its frozen response."""

        self._validate_idempotency_key(idempotency_key)
        canonical_body = self._validate_task_create_v1(body)
        request_digest = self._request_digest(canonical_body)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity,
            idempotency_key,
            "POST",
            path,
            request_digest,
        )
        if replay is not None:
            return replay

        backlog = self.documents.load(WorkspaceDocument.TASKS)
        task, workspace = self._append_task(backlog, **canonical_body)
        append_bootstrap(
            activity,
            task,
            created_at=self._utc_now(),
            actor="local.user",
            provenance="api.v1",
        )
        response_body = {
            "data": self._project_task(task, planning_status="open"),
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity,
            idempotency_key,
            "POST",
            path,
            request_digest,
            201,
            response_body,
        )
        self.documents.save_many(
            {
                WorkspaceDocument.TASKS: backlog,
                WorkspaceDocument.ACTIVITY: activity,
                WorkspaceDocument.WORKSPACE: workspace,
            },
            operation_id="task-create-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_transactional
    def add_task_note_v1(
        self,
        task_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
    ) -> dict[str, Any]:
        """Add one Task note for one logical browser intent."""

        self._validate_idempotency_key(idempotency_key)
        request_digest = self._request_digest(body)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        backlog = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(backlog["tasks"], task_id, "task")
        next_revision = _guard_revision(task, body["revision"])
        note = {"date": self._today(), "text": _required_text(body["text"], "text")}
        task.setdefault("notes", []).append(note)
        task["updated_at"] = self._today()
        task["revision"] = next_revision
        planning_status = validate_and_project(backlog, activity)[task_id]
        response_body = {
            "data": self._project_task(task, planning_status=planning_status),
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 200, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.TASKS: backlog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="task-note-{}".format(idempotency_key),
        )
        return {"status": 200, "body": response_body}

    @_transactional
    def add_subtask_v1(
        self,
        task_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
    ) -> dict[str, Any]:
        """Add one subtask for one logical browser intent."""

        self._validate_idempotency_key(idempotency_key)
        request_digest = self._request_digest(body)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay
        if body["priority"] not in PRIORITIES:
            raise ValueError("invalid priority")

        backlog = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(backlog["tasks"], task_id, "task")
        next_revision = _guard_revision(task, body["revision"])
        subtask = {
            "id": _next_id(task.setdefault("subtasks", []), "S"),
            "title": _required_text(body["title"], "title"),
            "priority": body["priority"],
            "status": "open",
        }
        task["subtasks"].append(subtask)
        task["updated_at"] = self._today()
        task["revision"] = next_revision
        planning_status = validate_and_project(backlog, activity)[task_id]
        response_body = {
            "data": self._project_task(task, planning_status=planning_status),
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 200, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.TASKS: backlog, WorkspaceDocument.ACTIVITY: activity},
            operation_id="task-subtask-{}".format(idempotency_key),
        )
        return {"status": 200, "body": response_body}

    @_transactional
    def add_subtask(
        self,
        task_id: str,
        title: str,
        priority: str = "P2",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if priority not in PRIORITIES:
            raise ValueError("invalid priority")
        data = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(data["tasks"], task_id, "task")
        next_revision = _guard_revision(task, expected_revision)
        subtask = {
            "id": _next_id(task.setdefault("subtasks", []), "S"),
            "title": _required_text(title, "title"),
            "priority": priority,
            "status": "open",
        }
        task["subtasks"].append(subtask)
        task["updated_at"] = self._today()
        task["revision"] = next_revision
        self.documents.save(WorkspaceDocument.TASKS, data)
        return subtask

    @_transactional
    def set_subtask_status(
        self,
        task_id: str,
        subtask_id: str,
        status: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if status not in TASK_STATUSES:
            raise ValueError("invalid task status")
        data = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(data["tasks"], task_id, "task")
        next_revision = _guard_revision(task, expected_revision)
        subtask = _find(task.setdefault("subtasks", []), subtask_id, "subtask")
        subtask["status"] = status
        task["updated_at"] = self._today()
        task["revision"] = next_revision
        self.documents.save(WorkspaceDocument.TASKS, data)
        return subtask

    @_optional_command_backend(
        "planning_commands", "set_task_status", "task"
    )
    @_transactional
    def set_task_status(
        self,
        task_id: str,
        status: str,
        expected_revision: int | None = None,
        *,
        provenance: str = "cli",
    ) -> dict[str, Any]:
        if status not in TASK_STATUSES:
            raise ValueError("invalid task status")
        if provenance not in {"cli", "api.legacy"}:
            raise ValueError("invalid planning status provenance")
        data = self.documents.load(WorkspaceDocument.TASKS)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        task = _find(data["tasks"], task_id, "task")
        current_revision = _revision(task)
        if expected_revision is None:
            expected_revision = current_revision
        if type(expected_revision) is not int or expected_revision < 0:
            raise DomainError("revision is required and must be a non-negative integer")
        if expected_revision != current_revision:
            raise RevisionConflictError(
                "task revision is stale",
                {"expected": current_revision, "received": expected_revision},
            )
        current_status = validate_and_project(data, activity)[task["id"]]
        if status == current_status:
            return self._project_task(task, planning_status=current_status)
        next_revision = _next_revision(task)
        append_transition(
            activity,
            task,
            prior_status=current_status,
            status=status,
            prior_revision=current_revision,
            new_revision=next_revision,
            created_at=self._utc_now(),
            actor="local.user",
            provenance=provenance,
        )
        task["updated_at"] = self._today()
        task["revision"] = next_revision
        self._record_task_status_notice(activity, task, current_status, status, current_revision, next_revision, self._status_notice_key(task, current_revision), "cli" if provenance == "cli" else "gui")
        self.documents.save_many(
            {WorkspaceDocument.TASKS: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="task-status-{}-r{}".format(task["id"], next_revision),
        )
        return self._project_task(task, planning_status=status)

    @_transactional
    def set_task_status_v1(
        self,
        task_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
        request_digest: str | None = None,
    ) -> dict[str, Any]:
        """The keyed Task-status intent selected by a PRESENT Idempotency-Key.

        Ordering is the frozen one: the key and the body are completely
        validated, then the receipt is looked up, and only then is anything
        mutable consulted. So an exact replay returns its original Task even
        after unrelated revisions have moved on, and a key is never treated as
        authorization.

        A same-status action is not a no-op that skips checks: it still
        requires a successful revision check, and then persists ONLY its
        receipt, leaving the Task, its revision, its updated_at and the
        planning history untouched.
        """

        self._validate_keyed_intent_request(idempotency_key, body)
        if not _released_v3_attributed_composition_owner(self):
            # The keyed branch, no-op included, is released-v3 SAME-Store only,
            # bound by object identity. Refused before any read or write; the
            # ordinary unkeyed PATCH is untouched by this.
            raise DomainError(
                "keyed task status intents are not supported by this storage composition"
            )
        digest = self._raw_request_digest(body, request_digest)

        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(activity, idempotency_key, "PATCH", path, digest)
        if replay is not None:
            return replay

        data = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(data["tasks"], task_id, "task")
        current_revision = _revision(task)
        if body["revision"] != current_revision:
            raise RevisionConflictError(
                "task revision is stale",
                {"expected": current_revision, "received": body["revision"]},
            )
        status = body["status"]
        current_status = validate_and_project(data, activity)[task["id"]]

        if status == current_status:
            # A receipt, and nothing else. The Task is not rewritten, so its
            # revision and updated_at stay exactly where the caller saw them.
            projected = self._project_task(task, planning_status=current_status)
            response_body = {"data": projected, "meta": {"replayed": False}}
            self._record_idempotency(
                activity, idempotency_key, "PATCH", path, digest, 200, response_body
            )
            self.documents.save_many(
                {WorkspaceDocument.ACTIVITY: activity},
                operation_id="task-status-intent-{}".format(idempotency_key),
            )
            return {"status": 200, "body": response_body}

        next_revision = _next_revision(task)
        append_transition(
            activity,
            task,
            prior_status=current_status,
            status=status,
            prior_revision=current_revision,
            new_revision=next_revision,
            created_at=self._utc_now(),
            actor="local.user",
            provenance="cli",
        )
        task["updated_at"] = self._today()
        task["revision"] = next_revision
        self._record_task_status_notice(activity, task, current_status, status, current_revision, next_revision, idempotency_key, "gui")
        projected = self._project_task(task, planning_status=status)
        response_body = {"data": projected, "meta": {"replayed": False}}
        self._record_idempotency(
            activity, idempotency_key, "PATCH", path, digest, 200, response_body
        )
        # ONE save commits the Task, its planning fact and the receipt together.
        # An ordinary PATCH followed by a separate receipt save would leave a
        # window where the change exists without its receipt.
        self.documents.save_many(
            {WorkspaceDocument.TASKS: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="task-status-intent-{}".format(idempotency_key),
        )
        return {"status": 200, "body": response_body}

    def _validate_keyed_intent_request(
        self, idempotency_key: Any, body: Any
    ) -> None:
        """Everything about the key and the body, before any lookup or read."""

        if type(idempotency_key) is not str:
            raise DomainError(
                "Idempotency-Key must be a string", {"field": "idempotency_key"}
            )
        self._validate_idempotency_key(idempotency_key)
        if type(body) is not dict or set(body) != {"status", "revision"}:
            raise DomainError(
                "a keyed task status intent requires exactly status and revision"
            )
        if type(body["status"]) is not str or body["status"] not in TASK_STATUSES:
            raise DomainError("invalid task status", {"field": "status"})
        revision = body["revision"]
        if type(revision) is not int or revision < 0:
            raise DomainError(
                "revision is required and must be a non-negative integer",
                {"field": "revision"},
            )

    @_transactional
    def add_task_note(
        self,
        task_id: str,
        text: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(data["tasks"], task_id, "task")
        next_revision = _guard_revision(task, expected_revision)
        note = {"date": self._today(), "text": _required_text(text, "text")}
        task.setdefault("notes", []).append(note)
        task["updated_at"] = self._today()
        task["revision"] = next_revision
        self.documents.save(WorkspaceDocument.TASKS, data)
        return note
