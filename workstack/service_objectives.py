"""Objective and Key Result commands, reads and rollup.

Objectives carry their own revision ladder, distinct from the Task one: a Key
Result write advances the OWNING Objective's revision, so concurrent edits to
two Key Results of one Objective still conflict. Every write emits its audit
event beside the document in a single save.
"""

from __future__ import annotations

import copy
from typing import Any

from .service_composition import _optional_command_backend, _transactional
from .service_domain import (
    OBJECTIVE_STATUSES,
    _find,
    _next_id,
    _next_revision,
    _required_text,
)
from .service_errors import DomainError, RevisionConflictError, RevisionExhaustedError
from .storage.document_repository import WorkspaceDocument
from .store import MAX_REVISION, StoreCorruptError


class ObjectiveServiceMixin:
    """OKR writes, reads and the linked-Task rollup."""

    @_transactional
    def add_objective(self, text: str, quarter: str | None = None) -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        objective = {
            "id": _next_id(data["objectives"], "O"),
            "quarter": quarter or self._current_quarter(),
            "objective": _required_text(text, "objective"),
            "status": "active",
            "key_results": [],
            "created": self._today(),
            "updated_at": self._today(),
        }
        data["objectives"].append(objective)
        self.documents.save(WorkspaceDocument.OBJECTIVES, data)
        return objective

    @_optional_command_backend(
        "objective_commands", "create_objective", "intent"
    )
    @_transactional
    def create_objective_v1(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str = "/api/v1/objectives",
    ) -> dict[str, Any]:
        """Create one Objective for one logical browser intent."""

        self._validate_idempotency_key(idempotency_key)
        request_digest = self._request_digest(body)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        objective = {
            "id": _next_id(data["objectives"], "O"),
            "quarter": body["quarter"] or self._current_quarter(),
            "objective": _required_text(body["objective"], "objective"),
            "status": "active",
            "key_results": [],
            "created": self._today(),
            "updated_at": self._today(),
            "revision": 0,
        }
        data["objectives"].append(objective)
        self._event(
            activity,
            "objective.created",
            details={"objective_id": objective["id"], "revision": 0},
        )
        response_body = {"data": copy.deepcopy(objective), "meta": {"replayed": False}}
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.OBJECTIVES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="objective-create-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_transactional
    def add_key_result(self, objective_id: str, text: str, target: str = "") -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        objective = _find(data["objectives"], objective_id, "objective")
        current_revision = objective.get("revision", 0)
        next_revision = self._objective_next_revision(objective, current_revision)
        key_result = {
            "id": _next_id(objective.setdefault("key_results", []), "KR"),
            "text": _required_text(text, "text"),
            "target": str(target or "").strip(),
            "progress": 0,
            "status": "active",
        }
        objective["key_results"].append(key_result)
        objective["updated_at"] = self._today()
        objective["revision"] = next_revision
        self._event(
            activity,
            "key_result.created",
            details={
                "objective_id": objective["id"],
                "key_result_id": key_result["id"],
                "revision": next_revision,
            },
        )
        self.documents.save_many(
            {WorkspaceDocument.OBJECTIVES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="key-result-create-{}-r{}".format(key_result["id"], next_revision),
        )
        return key_result

    def list_objectives(self, status: str = "active") -> list[dict[str, Any]]:
        objectives = list(self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", []))
        if status != "all":
            if status not in OBJECTIVE_STATUSES:
                raise ValueError("invalid objective status")
            objectives = [item for item in objectives if item.get("status") == status]
        return sorted(
            [self._project_objective(item) for item in objectives],
            key=lambda item: (item.get("quarter", ""), item.get("id", "")),
        )

    @staticmethod
    def _project_objective(objective: dict[str, Any]) -> dict[str, Any]:
        projected = copy.deepcopy(objective)
        revision = projected.get("revision", 0)
        if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
            raise StoreCorruptError("persisted objective revision is invalid")
        projected["revision"] = revision
        projected.setdefault("key_results", [])
        return projected

    @staticmethod
    def _objective_next_revision(objective: dict[str, Any], expected_revision: int) -> int:
        current = objective.get("revision", 0)
        if type(current) is not int or not 0 <= current <= MAX_REVISION:
            raise StoreCorruptError("persisted objective revision is invalid")
        if type(expected_revision) is not int or expected_revision < 0:
            raise DomainError("revision is required and must be a non-negative integer")
        if expected_revision != current:
            raise RevisionConflictError(
                "objective revision is stale",
                {"expected": current, "received": expected_revision},
            )
        if current == MAX_REVISION:
            raise RevisionExhaustedError(
                "objective revision cannot advance beyond the safe integer limit",
                {"maximum": MAX_REVISION},
            )
        return current + 1

    def objective_detail(self, objective_id: str) -> dict[str, Any]:
        with self.store.transaction():
            objective = _find(
                self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", []), objective_id, "objective"
            )
            normalized_id = objective["id"]
            tasks = [
                self._project_task(task)
                for task in self.list_tasks(status="all")
                if normalized_id in task.get("objective_ids", [])
            ]
            activity = [
                copy.deepcopy(event)
                for event in self.documents.load(WorkspaceDocument.ACTIVITY).get("activity", [])
                if event.get("details", {}).get("objective_id") == normalized_id
            ]
            return {
                "objective": self._project_objective(objective),
                "tasks": tasks,
                "activity": activity,
            }

    @_optional_command_backend(
        "objective_commands", "add_key_result", "intent"
    )
    @_transactional
    def add_key_result_v1(
        self,
        objective_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        request_digest = self._request_digest(body)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay is not None:
            return replay

        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        objective = _find(data["objectives"], objective_id, "objective")
        next_revision = self._objective_next_revision(objective, body["revision"])
        key_result = {
            "id": _next_id(objective.setdefault("key_results", []), "KR"),
            "text": _required_text(body["text"], "text"),
            "target": body["target"].strip(),
            "progress": 0,
            "status": "active",
        }
        objective["key_results"].append(key_result)
        objective["revision"] = next_revision
        objective["updated_at"] = self._today()
        self._event(
            activity,
            "key_result.created",
            details={
                "objective_id": objective["id"],
                "key_result_id": key_result["id"],
                "revision": next_revision,
            },
        )
        response_body = {
            "data": self._project_objective(objective),
            "meta": {"replayed": False},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, 201, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.OBJECTIVES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="key-result-create-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_transactional
    def patch_key_result_v1(
        self,
        objective_id: str,
        key_result_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        objective = _find(data["objectives"], objective_id, "objective")
        next_revision = self._objective_next_revision(objective, body["revision"])
        key_result = _find(objective.get("key_results", []), key_result_id, "key result")
        fields = sorted(set(body) - {"revision"})
        if "text" in body:
            key_result["text"] = _required_text(body["text"], "text")
        if "target" in body:
            if not isinstance(body["target"], str):
                raise DomainError("key result target must be a string")
            key_result["target"] = body["target"].strip()
        if "progress" in body:
            if type(body["progress"]) is not int or not 0 <= body["progress"] <= 100:
                raise DomainError("key result progress is invalid")
            key_result["progress"] = body["progress"]
        if "status" in body:
            if body["status"] not in OBJECTIVE_STATUSES:
                raise DomainError("key result status is invalid")
            key_result["status"] = body["status"]
        objective["revision"] = next_revision
        objective["updated_at"] = self._today()
        self._event(
            activity,
            "key_result.updated",
            details={
                "objective_id": objective["id"],
                "key_result_id": key_result["id"],
                "fields": fields,
                "revision": next_revision,
            },
        )
        self.documents.save_many(
            {WorkspaceDocument.OBJECTIVES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="key-result-update-{}-r{}".format(key_result["id"], next_revision),
        )
        return self._project_objective(objective)

    @_transactional
    def patch_objective_v1(self, objective_id: str, body: dict[str, Any]) -> dict[str, Any]:
        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        objective = _find(data["objectives"], objective_id, "objective")
        next_revision = self._objective_next_revision(objective, body["revision"])
        fields = sorted(set(body) - {"revision"})
        prior_status = objective.get("status", "active")
        if "objective" in body:
            objective["objective"] = _required_text(body["objective"], "objective")
        if "quarter" in body:
            objective["quarter"] = _required_text(body["quarter"], "quarter")
        if "status" in body:
            if body["status"] not in OBJECTIVE_STATUSES:
                raise DomainError("invalid objective status")
            objective["status"] = body["status"]
        objective["revision"] = next_revision
        objective["updated_at"] = self._today()
        details: dict[str, Any] = {
            "objective_id": objective["id"],
            "fields": fields,
            "revision": next_revision,
        }
        event_type = "objective.updated"
        if fields == ["status"]:
            event_type = "objective.status_changed"
            details.update({"prior_status": prior_status, "status": body["status"]})
        self._event(activity, event_type, details=details)
        self.documents.save_many(
            {WorkspaceDocument.OBJECTIVES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="objective-update-{}-r{}".format(objective["id"], next_revision),
        )
        return self._project_objective(objective)

    @_transactional
    def link_task(self, objective_id: str, task_id: str) -> dict[str, Any]:
        _find(self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", []), objective_id, "objective")
        data = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(data["tasks"], task_id, "task")
        next_revision = _next_revision(task)
        links = set(task.setdefault("objective_ids", []))
        links.add(objective_id.strip().upper())
        task["objective_ids"] = sorted(links)
        task["updated_at"] = self._today()
        task["revision"] = next_revision
        self.documents.save(WorkspaceDocument.TASKS, data)
        return task

    @_transactional
    def set_key_result_progress(
        self,
        objective_id: str,
        key_result_id: str,
        progress: int,
    ) -> dict[str, Any]:
        progress = max(0, min(100, int(progress)))
        data = self.documents.load(WorkspaceDocument.OBJECTIVES)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        objective = _find(data["objectives"], objective_id, "objective")
        current_revision = objective.get("revision", 0)
        next_revision = self._objective_next_revision(objective, current_revision)
        key_result = _find(objective.get("key_results", []), key_result_id, "key result")
        prior = {
            "progress": key_result.get("progress", 0),
            "status": key_result.get("status", "active"),
        }
        key_result["progress"] = progress
        key_result["status"] = "done" if progress == 100 else "active"
        objective["updated_at"] = self._today()
        objective["revision"] = next_revision
        self._event(
            activity,
            "key_result.updated",
            details={
                "objective_id": objective["id"],
                "key_result_id": key_result["id"],
                "prior": prior,
                "current": {"progress": progress, "status": key_result["status"]},
                "revision": next_revision,
            },
        )
        self.documents.save_many(
            {WorkspaceDocument.OBJECTIVES: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="key-result-update-{}-r{}".format(key_result["id"], next_revision),
        )
        return key_result

    @_transactional
    def objective_rollup(self) -> list[dict[str, Any]]:
        tasks = self.list_tasks(status="all")
        output = []
        for objective in self.list_objectives(status="all"):
            linked = [task for task in tasks if objective["id"] in task.get("objective_ids", [])]
            output.append({
                "id": objective["id"],
                "objective": objective["objective"],
                "quarter": objective.get("quarter"),
                "status": objective.get("status"),
                "key_results": objective.get("key_results", []),
                "tasks": [
                    {
                        "id": task["id"],
                        "title": task["title"],
                        "status": task.get("status"),
                        "subtasks_done": sum(
                            1 for item in task.get("subtasks", []) if item.get("status") == "done"
                        ),
                        "subtasks_total": len(task.get("subtasks", [])),
                    }
                    for task in linked
                ],
            })
        return output
