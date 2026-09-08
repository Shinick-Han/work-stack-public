"""Capture ingestion, linking, dismissal and conversion to Tasks.

Re-ingesting a source is the delicate path: an identical fingerprint must carry
identical reviewed content, an older retrieval is stale, and an equal retrieval
time with a different fingerprint is a source conflict. Action-item links
survive a revision, and conversion is replayable by ``intent_id`` as well as by
Idempotency-Key.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable

from .capture import canonical_digest, parse_rfc3339, validate_capture_packet
from .planning_status import append_bootstrap, validate_and_project
from .service_capture_rules import (
    _require_matching_capture_review,
    _validate_capture_task_fields,
)
from .service_composition import _capture_reply_backend, _transactional
from .service_domain import (
    CAPTURE_STATUSES,
    _find,
    _next_id,
    _next_revision,
    _task_uid,
)
from .service_errors import (
    DomainError,
    IdempotencyConflictError,
    SourceRevisionConflictError,
    StaleCaptureError,
)
from .storage.document_repository import WorkspaceDocument
from .store import StoreCorruptError


class CaptureServiceMixin:
    """Capture inbox writes and the capture-to-Task conversions."""

    @staticmethod
    def _project_capture(capture: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "id", "schema_version", "source_key", "source", "normalized", "task_hints",
            "provenance", "status", "linked_task_ids", "converted_task_ids", "revision",
            "created_at", "updated_at",
        )
        return {field: copy.deepcopy(capture[field]) for field in fields}

    def list_captures(self, status: str = "inbox") -> list[dict[str, Any]]:
        if status != "all" and status not in CAPTURE_STATUSES:
            raise DomainError("invalid capture status")
        captures = self.documents.load(WorkspaceDocument.CAPTURES).get("captures", [])
        if status != "all":
            captures = [capture for capture in captures if capture.get("status") == status]
        return [self._project_capture(capture) for capture in sorted(captures, key=lambda item: item["id"])]

    @_capture_reply_backend
    @_transactional
    def ingest_capture(
        self,
        packet: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        method: str = "POST",
        path: str = "/api/v1/captures",
    ) -> dict[str, Any]:
        request_digest = request_digest or self._request_digest(packet)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(activity, idempotency_key, method, path, request_digest)
        if replay:
            return replay
        sanitized = validate_capture_packet(packet)
        captures_data = self.documents.load(WorkspaceDocument.CAPTURES)
        captures = captures_data.setdefault("captures", [])
        existing = next(
            (item for item in captures if item.get("source_key") == sanitized["source_key"]),
            None,
        )
        now = self._utc_now()
        duplicate = False
        if existing is None:
            capture = {
                **sanitized,
                "id": _next_id(captures, "C", 4),
                "status": "inbox",
                "linked_task_ids": [],
                "converted_task_ids": [],
                "revision": 0,
                "created_at": now,
                "updated_at": now,
                "recent_revisions": [],
            }
            captures.append(capture)
            response_status = 201
            self._event(activity, "capture.ingested", capture_id=capture["id"])
        elif existing["source"].get("fingerprint") == sanitized["source"]["fingerprint"]:
            _require_matching_capture_review(existing, sanitized)
            capture = existing
            duplicate = True
            response_status = 200
        else:
            old_time = parse_rfc3339(existing["source"]["retrieved_at"], "stored source.retrieved_at")
            new_time = parse_rfc3339(sanitized["source"]["retrieved_at"], "source.retrieved_at")
            if new_time < old_time:
                raise StaleCaptureError("capture packet is older than the current source revision")
            if new_time == old_time:
                raise SourceRevisionConflictError(
                    "different fingerprints have the same retrieval time"
                )
            previous_actions = {
                item["id"]: item.get("task_id")
                for item in existing.get("normalized", {}).get("action_items", [])
                if item.get("task_id")
            }
            for action in sanitized["normalized"]["action_items"]:
                if action["id"] in previous_actions:
                    action["task_id"] = previous_actions[action["id"]]
            recent = list(existing.get("recent_revisions", []))
            recent.append({
                "fingerprint": existing["source"].get("fingerprint"),
                "version_ref": existing["source"].get("version_ref"),
                "retrieved_at": existing["source"].get("retrieved_at"),
                "provenance_digest": canonical_digest(existing.get("provenance", {})),
                "redaction_policy_version": existing.get("provenance", {}).get("redaction_policy_version"),
            })
            for field in ("schema_version", "source_key", "source", "normalized", "task_hints", "provenance"):
                existing[field] = sanitized[field]
            existing["recent_revisions"] = recent[-10:]
            existing["revision"] = _next_revision(existing)
            existing["updated_at"] = now
            capture = existing
            response_status = 200
            self._event(
                activity,
                "capture.updated",
                capture_id=capture["id"],
                details={"revision": capture["revision"]},
            )
        body: dict[str, Any] = {"data": self._project_capture(capture)}
        if duplicate:
            body["meta"] = {"duplicate": True}
        self._record_idempotency(
            activity, idempotency_key, method, path, request_digest, response_status, body
        )
        self.documents.save_many(
            {WorkspaceDocument.CAPTURES: captures_data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="capture-ingest-{}".format(idempotency_key),
        )
        return {"status": response_status, "body": body}

    @_capture_reply_backend
    @_transactional
    def link_capture(
        self,
        capture_id: str,
        task_id: str,
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        body_input = {"task_id": task_id}
        request_digest = request_digest or self._request_digest(body_input)
        path = path or "/api/v1/captures/{}/link".format(capture_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(activity, idempotency_key, "POST", path, request_digest)
        if replay:
            return replay
        task = _find(self.documents.load(WorkspaceDocument.TASKS).get("tasks", []), task_id, "task")
        captures_data = self.documents.load(WorkspaceDocument.CAPTURES)
        capture = _find(captures_data.get("captures", []), capture_id, "capture")
        duplicate = task["id"] in capture.setdefault("linked_task_ids", [])
        if not duplicate:
            capture["linked_task_ids"].append(task["id"])
            capture["linked_task_ids"].sort()
            if capture.get("status") != "converted":
                capture["status"] = "linked"
            capture["revision"] = _next_revision(capture)
            capture["updated_at"] = self._utc_now()
            self._event(activity, "capture.linked", capture_id=capture["id"], task_id=task["id"])
        body: dict[str, Any] = {"data": self._project_capture(capture)}
        if duplicate:
            body["meta"] = {"duplicate": True}
        self._record_idempotency(activity, idempotency_key, "POST", path, request_digest, 200, body)
        self.documents.save_many(
            {WorkspaceDocument.CAPTURES: captures_data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="capture-link-{}".format(idempotency_key),
        )
        return {"status": 200, "body": body}

    @staticmethod
    def _capture_action_matches_task(
        action: dict[str, Any], task: dict[str, Any]
    ) -> bool:
        return all(
            action.get(field, default) == task.get(field, default)
            for field, default in (
                ("title", ""),
                ("detail", ""),
                ("priority", "P2"),
                ("due", None),
            )
        )

    @classmethod
    def _link_unique_matching_capture_action(
        cls, capture: dict[str, Any], task: dict[str, Any]
    ) -> None:
        matches = [
            action
            for action in capture.get("normalized", {}).get("action_items", [])
            if not action.get("task_id") and cls._capture_action_matches_task(action, task)
        ]
        if len(matches) == 1:
            matches[0]["task_id"] = task["id"]

    def _capture_task_intent_replay(
        self,
        activity: dict[str, Any],
        backlog: dict[str, Any],
        capture: dict[str, Any],
        intent_id: str | None,
        request_digest: str,
        idempotency_key: str,
        path: str,
    ) -> dict[str, Any] | None:
        if intent_id is None:
            return None
        event = next(
            (
                item
                for item in activity.get("activity", [])
                if item.get("type") == "capture.task_created"
                and item.get("capture_id") == capture["id"]
                and item.get("details", {}).get("intent_id") == intent_id
            ),
            None,
        )
        if event is None:
            return None
        if event.get("details", {}).get("request_digest") != request_digest:
            raise IdempotencyConflictError(
                "intent_id was already used for different Task fields",
                {"intent_id": intent_id},
            )
        task = _find(backlog.get("tasks", []), event.get("task_id"), "task")
        if task["id"] not in capture.get("converted_task_ids", []):
            raise StoreCorruptError("capture task intent link is incomplete")
        planning_status = validate_and_project(backlog, activity)[task["id"]]
        body = {
            "data": self._project_task(task, 1, planning_status=planning_status),
            "meta": {"intent_replayed": True},
        }
        self._record_idempotency(
            activity,
            idempotency_key,
            "POST",
            path,
            request_digest,
            200,
            body,
        )
        self.documents.save_many(
            {WorkspaceDocument.ACTIVITY: activity},
            operation_id="capture-task-intent-replay-{}".format(idempotency_key),
        )
        return {"status": 200, "body": body}

    @_transactional
    def create_task_from_capture(
        self,
        capture_id: str,
        task_fields: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        intent_id, task_input = _validate_capture_task_fields(task_fields)

        request_digest = request_digest or self._request_digest(task_fields)
        path = path or "/api/v1/captures/{}/task".format(capture_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay:
            return replay

        captures_data = self.documents.load(WorkspaceDocument.CAPTURES)
        capture = _find(captures_data.get("captures", []), capture_id, "capture")
        backlog = self.documents.load(WorkspaceDocument.TASKS)
        intent_replay = self._capture_task_intent_replay(
            activity,
            backlog,
            capture,
            intent_id,
            request_digest,
            idempotency_key,
            path,
        )
        if intent_replay is not None:
            return intent_replay
        task, workspace = self._append_task(
            backlog,
            task_input["title"],
            task_input.get("detail", ""),
            task_input.get("priority", "P2"),
            task_input.get("due"),
            task_input.get("tags", ()),
            task_input.get("objective_ids", ()),
            task_input.get("parent_id"),
            task_input.get("dependencies", ()),
        )
        append_bootstrap(
            activity,
            task,
            created_at=self._utc_now(),
            actor="workstack.capture",
            provenance="api.v1.capture",
        )

        converted = set(capture.setdefault("converted_task_ids", []))
        converted.add(task["id"])
        capture["converted_task_ids"] = sorted(converted)
        self._link_unique_matching_capture_action(capture, task)
        capture["status"] = "converted"
        capture["revision"] = _next_revision(capture)
        capture["updated_at"] = self._utc_now()
        self._event(
            activity,
            "capture.task_created",
            capture_id=capture["id"],
            task_id=task["id"],
            details=(
                {"intent_id": intent_id, "request_digest": request_digest}
                if intent_id is not None
                else None
            ),
        )
        response_body = {
            "data": self._project_task(task, 1, planning_status="open")
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
                WorkspaceDocument.CAPTURES: captures_data,
                WorkspaceDocument.ACTIVITY: activity,
                WorkspaceDocument.WORKSPACE: workspace,
            },
            operation_id="capture-task-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @_transactional
    def dismiss_capture(
        self,
        capture_id: str,
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        request_digest = request_digest or self._request_digest({})
        path = path or "/api/v1/captures/{}/dismiss".format(capture_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(activity, idempotency_key, "POST", path, request_digest)
        if replay:
            return replay
        captures_data = self.documents.load(WorkspaceDocument.CAPTURES)
        capture = _find(captures_data.get("captures", []), capture_id, "capture")
        duplicate = capture.get("status") == "dismissed"
        if not duplicate:
            capture["status"] = "dismissed"
            capture["revision"] = _next_revision(capture)
            capture["updated_at"] = self._utc_now()
            self._event(activity, "capture.dismissed", capture_id=capture["id"])
        body: dict[str, Any] = {"data": self._project_capture(capture)}
        if duplicate:
            body["meta"] = {"duplicate": True}
        self._record_idempotency(activity, idempotency_key, "POST", path, request_digest, 200, body)
        self.documents.save_many(
            {WorkspaceDocument.CAPTURES: captures_data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="capture-dismiss-{}".format(idempotency_key),
        )
        return {"status": 200, "body": body}

    @_transactional
    def convert_capture_action(
        self,
        capture_id: str,
        action_id: str,
        objective_ids: Iterable[str],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        raw_objectives = list(objective_ids)
        if any(not isinstance(item, str) for item in raw_objectives):
            raise DomainError("objective_ids entries must be strings")
        normalized_objectives = sorted({item.strip().upper() for item in raw_objectives if item.strip()})
        body_input = {"objective_ids": normalized_objectives}
        request_digest = request_digest or self._request_digest(body_input)
        path = path or "/api/v1/captures/{}/actions/{}/task".format(capture_id, action_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(activity, idempotency_key, "POST", path, request_digest)
        if replay:
            return replay
        objectives = {item["id"] for item in self.documents.load(WorkspaceDocument.OBJECTIVES).get("objectives", [])}
        missing = sorted(set(normalized_objectives) - objectives)
        if missing:
            raise DomainError("unknown objective ids", {"ids": missing})
        captures_data = self.documents.load(WorkspaceDocument.CAPTURES)
        capture = _find(captures_data.get("captures", []), capture_id, "capture")
        action = _find(capture.get("normalized", {}).get("action_items", []), action_id, "capture action")
        backlog = self.documents.load(WorkspaceDocument.TASKS)
        workspace = None
        if action.get("task_id"):
            task = _find(backlog.get("tasks", []), action["task_id"], "task")
            response_status = 200
            duplicate = True
        else:
            task_id, workspace = self._next_task_display(backlog.setdefault("tasks", []))
            workspace_id = workspace["id"]
            task = {
                "id": task_id,
                "uid": _task_uid(workspace_id, task_id),
                "title": action["title"],
                "detail": action.get("detail", ""),
                "status": "open",
                "priority": action.get("priority", "P2"),
                "due": action.get("due"),
                "tags": copy.deepcopy(capture.get("normalized", {}).get("tags", [])),
                "objective_ids": normalized_objectives,
                "parent_id": None,
                "dependencies": [],
                "subtasks": [],
                "notes": [],
                "created": self._today(),
                "updated_at": self._today(),
                "revision": 0,
            }
            backlog["tasks"].append(task)
            append_bootstrap(
                activity,
                task,
                created_at=self._utc_now(),
                actor="workstack.capture",
                provenance="api.v1.capture",
            )
            action["task_id"] = task_id
            converted = set(capture.setdefault("converted_task_ids", []))
            converted.add(task_id)
            capture["converted_task_ids"] = sorted(converted)
            capture["status"] = "converted"
            capture["revision"] = _next_revision(capture)
            capture["updated_at"] = self._utc_now()
            self._event(
                activity,
                "capture.action_converted",
                capture_id=capture["id"],
                task_id=task_id,
                details={"action_id": action["id"]},
            )
            response_status = 201
            duplicate = False
        projected_status = validate_and_project(backlog, activity)[task["id"]]
        body: dict[str, Any] = {
            "data": self._project_task(task, 1, planning_status=projected_status)
        }
        if duplicate:
            body["meta"] = {"duplicate": True}
        self._record_idempotency(
            activity, idempotency_key, "POST", path, request_digest, response_status, body
        )
        writes = {
            WorkspaceDocument.TASKS: backlog,
            WorkspaceDocument.CAPTURES: captures_data,
            WorkspaceDocument.ACTIVITY: activity,
        }
        if workspace is not None:
            writes[WorkspaceDocument.WORKSPACE] = workspace
        self.documents.save_many(
            writes,
            operation_id="capture-convert-{}".format(idempotency_key),
        )
        return {"status": response_status, "body": body}
