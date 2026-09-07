"""Work Stack mutation-notice application: persist, list, and Undo.

Helpers and mixins extracted so ``service.py`` / ``server.py`` hotspot debt
does not grow. Persistence stays on the existing v3 Activity document.
This module does not import ``service`` or ``server``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote

from .mutation_notice import MutationNoticeError, build_compensation
from .mutation_receipts import (
    MutationReceiptError,
    find_notice,
    page_notices,
    parse_list_query,
    record_committed_status_notice,
    unkeyed_status_key,
)
from .planning_status import append_transition, validate_and_project
from .storage.document_repository import WorkspaceDocument
from .store import MAX_REVISION, StoreCorruptError


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _today() -> str:
    return dt.date.today().isoformat()


def _revision(record: Mapping[str, Any]) -> int:
    if "revision" not in record:
        raise StoreCorruptError("persisted task revision is missing")
    value = record["revision"]
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise StoreCorruptError("persisted task revision is invalid")
    return value


def _next_revision(record: Mapping[str, Any]) -> int:
    current = _revision(record)
    if current >= MAX_REVISION:
        raise MutationReceiptError("revision_exhausted", status=409)
    return current + 1


class MutationNoticeMixin:
    """Durable Task-status notices and bounded Undo on the owning WorkStack."""

    def _record_task_status_notice(
        self,
        activity: dict[str, Any],
        task: Mapping[str, Any],
        status_before: str,
        status_after: str,
        before_revision: int,
        after_revision: int,
        idempotency_key: str,
        source: str,
    ) -> dict[str, Any]:
        try:
            return record_committed_status_notice(
                activity,
                workspace_uid=self._workspace_uid(),
                task=task,
                status_before=status_before,
                status_after=status_after,
                before_revision=before_revision,
                after_revision=after_revision,
                idempotency_key=idempotency_key,
                source=source,
                created_at=_utc_now(),
            )
        except MutationNoticeError as error:
            raise MutationReceiptError("invalid_request", "invalid mutation notice") from error

    def _status_notice_key(self, task: Mapping[str, Any], before_revision: int) -> str:
        return unkeyed_status_key(str(task["uid"]), before_revision)

    def list_mutation_notices(
        self, cursor: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        with self.store.transaction():
            activity = self.documents.load(WorkspaceDocument.ACTIVITY)
            return page_notices(activity, self._workspace_uid(), cursor, limit)

    def undo_mutation_notice(
        self,
        notice_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        path: str,
        request_digest: str | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction():
            return self._undo_mutation_notice_locked(
                notice_id, body, idempotency_key, path, request_digest
            )

    def _undo_mutation_notice_locked(
        self,
        notice_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        path: str,
        request_digest: str | None,
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        revision = self._undo_revision(body)
        canonical_id = self._canonical_notice_id(notice_id)
        digest = self._raw_request_digest(body, request_digest)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, digest
        )
        if replay is not None:
            return replay
        notice = self._required_notice(activity, canonical_id)
        data = self.documents.load(WorkspaceDocument.TASKS)
        task = self._task_for_notice(data, notice)
        current_revision = _revision(task)
        if revision != current_revision:
            raise MutationReceiptError(
                "revision_conflict",
                "task revision is stale",
                details={"expected": current_revision, "received": revision},
            )
        compensation = self._compensation_for(notice, task, current_revision)
        projected_status = self._apply_compensation(
            activity, data, task, compensation, current_revision
        )
        projected = self._project_task(task, planning_status=projected_status)
        response_body = {
            "data": projected,
            "meta": {"replayed": False, "undone_notice_id": canonical_id},
        }
        self._record_idempotency(
            activity, idempotency_key, "POST", path, digest, 200, response_body
        )
        self.documents.save_many(
            {WorkspaceDocument.TASKS: data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="mutation-notice-undo-{}".format(idempotency_key),
        )
        return {"status": 200, "body": response_body}

    def _undo_revision(self, body: Any) -> int:
        if type(body) is not dict or set(body) != {"revision"}:
            raise MutationReceiptError("invalid_request", "undo requires exactly revision")
        revision = body["revision"]
        if type(revision) is not int or revision < 0:
            raise MutationReceiptError(
                "invalid_request",
                "revision is required and must be a non-negative integer",
                details={"field": "revision"},
            )
        return revision

    def _canonical_notice_id(self, value: str) -> str:
        if type(value) is not str:
            raise MutationReceiptError("invalid_request", "mutation notice id is invalid")
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, ValueError) as error:
            raise MutationReceiptError(
                "invalid_request", "mutation notice id is invalid"
            ) from error
        if str(parsed) != value or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
            raise MutationReceiptError("invalid_request", "mutation notice id is invalid")
        return value

    def _required_notice(
        self, activity: Mapping[str, Any], notice_id: str
    ) -> dict[str, Any]:
        notice = find_notice(activity, notice_id)
        if notice is None or notice["workspace_uid"] != self._workspace_uid():
            raise MutationReceiptError("not_found", "mutation notice not found")
        return notice

    def _task_for_notice(
        self, data: Mapping[str, Any], notice: Mapping[str, Any]
    ) -> dict[str, Any]:
        for task in data.get("tasks") or []:
            if isinstance(task, dict) and task.get("uid") == notice["entity_uid"]:
                return task
        raise MutationReceiptError("not_found", "task not found")

    def _compensation_for(
        self,
        notice: Mapping[str, Any],
        task: Mapping[str, Any],
        current_revision: int,
    ) -> dict[str, Any]:
        try:
            return build_compensation(
                notice,
                current_workspace_uid=self._workspace_uid(),
                current_entity_uid=task["uid"],
                current_revision=current_revision,
            )
        except MutationNoticeError as error:
            if error.code == "revision_mismatch":
                raise MutationReceiptError(
                    "revision_conflict",
                    "task revision is stale",
                    details={
                        "expected": notice.get("after_revision"),
                        "received": current_revision,
                    },
                ) from error
            raise MutationReceiptError(
                "invalid_request", "mutation notice is not undoable"
            ) from error

    def _apply_compensation(
        self,
        activity: dict[str, Any],
        data: dict[str, Any],
        task: dict[str, Any],
        compensation: Mapping[str, Any],
        current_revision: int,
    ) -> str:
        current_status = validate_and_project(data, activity)[task["id"]]
        requested = str(compensation["requested_status"])
        if requested == current_status:
            raise MutationReceiptError(
                "invalid_request", "mutation notice is not undoable"
            )
        next_revision = _next_revision(task)
        append_transition(
            activity,
            task,
            prior_status=current_status,
            status=requested,
            prior_revision=current_revision,
            new_revision=next_revision,
            created_at=_utc_now(),
            actor="local.user",
            provenance="api.v1",
        )
        task["updated_at"] = _today()
        task["revision"] = next_revision
        self._record_task_status_notice(
            activity,
            task,
            current_status,
            requested,
            current_revision,
            next_revision,
            str(compensation["idempotency_key"]),
            "gui",
        )
        return requested


class MutationNoticeHttpMixin:
    """Thin HTTP adapters for the bounded mutation-notice routes."""

    def _send_service_result(self, result: dict[str, Any]) -> None:
        self.send_json(result["body"], result["status"])

    def _get_mutation_notices(self, parsed: Any, match: Any) -> None:
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            cursor, limit = parse_list_query(query)
            payload = self.stack.list_mutation_notices(cursor, limit)
        except MutationReceiptError:
            raise
        except ValueError as error:
            self.send_api_error("invalid_query", str(error), 400)
            return
        self.send_json({"data": payload})

    def _post_mutation_notice_undo(
        self,
        path: str,
        match: Any,
        body: dict[str, Any],
        request_digest: str,
        idempotency_key: str,
    ) -> None:
        self._send_service_result(
            self.stack.undo_mutation_notice(
                unquote(match.group(1)),
                body,
                idempotency_key,
                path=path,
                request_digest=request_digest,
            )
        )
