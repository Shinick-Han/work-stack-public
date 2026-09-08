"""Reply approval and the provider receipt that makes a reply terminal.

Approval freezes what was reviewed: the body and the opaque target references
are digested at approval time, and the receipt must match those digests with a
constant-time comparison before it may move the reply to its terminal state. A
second identical receipt is a duplicate, not a conflict.
"""

from __future__ import annotations

import copy
from typing import Any

from .capture import canonical_digest
from .service_capture_rules import (
    _apply_terminal_reply_state,
    _project_reply_receipt_optional_fields,
    _reply_receipt_digests,
    _reply_receipt_mismatches,
    _reply_receipt_occurred_at,
    _reply_receipt_outcome,
    _reply_receipt_provider,
    _validate_reply_receipt_shape,
)
from .service_composition import _capture_reply_backend, _transactional
from .service_domain import (
    REPLY_CAPABILITIES,
    REPLY_TARGET_FIELDS,
    REPLY_TARGET_REF_MAX,
    _find,
    _next_id,
    _revision,
)
from .service_errors import DomainError, ReplyReceiptConflictError
from .service_text import _approved_plain_text, _opaque_reference
from .storage.document_repository import WorkspaceDocument


class ReplyServiceMixin:
    """Reply approval, receipt validation and terminal state application."""

    @staticmethod
    def _project_reply(reply: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "id",
            "task_id",
            "capture_id",
            "capture_revision",
            "provider",
            "capability",
            "target",
            "body",
            "body_digest",
            "target_digest",
            "state",
            "approved_at",
            "receipt",
            "created_at",
            "updated_at",
        )
        return {field: copy.deepcopy(reply[field]) for field in fields}

    @staticmethod
    def _approved_reply_body(request: dict[str, Any]) -> str:
        """The exact approval shape, then the reviewed body's own admission."""

        required = {"task_id", "capture_id", "body", "approved"}
        if set(request) != required:
            raise DomainError(
                "reply approval requires only task_id, capture_id, body, and approved",
                {
                    "missing": sorted(required - set(request)),
                    "unknown": sorted(set(request) - required),
                },
            )
        if not isinstance(request["task_id"], str) or not request["task_id"]:
            raise DomainError("task_id must be a non-empty string", {"field": "task_id"})
        if not isinstance(request["capture_id"], str) or not request["capture_id"]:
            raise DomainError("capture_id must be a non-empty string", {"field": "capture_id"})
        if request["approved"] is not True:
            raise DomainError("approved must be true", {"field": "approved"})
        return _approved_plain_text(request["body"])

    @staticmethod
    def _reply_capability_target(
        capture: dict[str, Any]
    ) -> tuple[Any, str, dict[str, str]]:
        """The provider capability and the opaque target the reply is frozen to."""

        source = capture.get("source", {})
        provider = source.get("provider")
        capability = REPLY_CAPABILITIES.get(provider) if isinstance(provider, str) else None
        if capability is None:
            raise DomainError(
                "capture provider does not support replies",
                {"capture_id": capture["id"], "provider": provider},
            )
        target = {
            field: _opaque_reference(
                source.get(field), "source.{}".format(field), REPLY_TARGET_REF_MAX
            )
            for field in REPLY_TARGET_FIELDS
        }
        return provider, capability, target

    @_capture_reply_backend
    @_transactional
    def approve_reply(
        self,
        request: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str = "/api/v1/replies",
    ) -> dict[str, Any]:
        body = self._approved_reply_body(request)

        request_digest = request_digest or self._request_digest(request)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay:
            return replay

        backlog = self.documents.load(WorkspaceDocument.TASKS)
        task = _find(backlog.get("tasks", []), request["task_id"], "task")
        captures_data = self.documents.load(WorkspaceDocument.CAPTURES)
        capture = _find(captures_data.get("captures", []), request["capture_id"], "capture")
        task_links = set(capture.get("linked_task_ids", [])) | set(
            capture.get("converted_task_ids", [])
        )
        if task["id"] not in task_links:
            raise DomainError(
                "capture is not linked to the task",
                {"task_id": task["id"], "capture_id": capture["id"]},
            )

        provider, capability, target = self._reply_capability_target(capture)
        now = self._utc_now()
        replies_data = self.documents.load(WorkspaceDocument.REPLIES)
        replies = replies_data.setdefault("replies", [])
        reply = {
            "id": _next_id(replies, "R", 4),
            "task_id": task["id"],
            "capture_id": capture["id"],
            "capture_revision": _revision(capture),
            "provider": provider,
            "capability": capability,
            "target": target,
            "body": body,
            "body_digest": canonical_digest(body),
            "target_digest": canonical_digest(target),
            "state": "approved",
            "approved_at": now,
            "receipt": None,
            "created_at": now,
            "updated_at": now,
        }
        replies.append(reply)
        self._event(
            activity,
            "reply.approved",
            capture_id=capture["id"],
            task_id=task["id"],
            reply_id=reply["id"],
            details={"provider": provider, "state": "approved"},
        )
        response_body = {"data": self._project_reply(reply)}
        self._record_idempotency(
            activity,
            idempotency_key,
            "POST",
            path,
            request_digest,
            201,
            None,
            response_ref={"kind": "reply", "id": reply["id"]},
        )
        self.documents.save_many(
            {WorkspaceDocument.REPLIES: replies_data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="reply-approve-{}".format(idempotency_key),
        )
        return {"status": 201, "body": response_body}

    @staticmethod
    def _validate_reply_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
        _validate_reply_receipt_shape(receipt)
        if receipt["schema_version"] != "1.0":
            raise DomainError("schema_version must be 1.0", {"field": "schema_version"})
        reply_id = _opaque_reference(receipt["reply_id"], "reply_id", 64)
        provider = _reply_receipt_provider(receipt)
        outcome = _reply_receipt_outcome(receipt)
        occurred_at = _reply_receipt_occurred_at(receipt)
        digests = _reply_receipt_digests(receipt)
        projected: dict[str, Any] = {
            "schema_version": "1.0",
            "reply_id": reply_id,
            "provider": provider,
            "outcome": outcome,
            "occurred_at": occurred_at,
            **digests,
        }
        _project_reply_receipt_optional_fields(receipt, projected)
        return projected

    @_capture_reply_backend
    @_transactional
    def apply_reply_receipt(
        self,
        reply_id: str,
        receipt_input: dict[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        request_digest = request_digest or self._request_digest(receipt_input)
        path = path or "/api/v1/replies/{}/receipt".format(reply_id)
        activity = self.documents.load(WorkspaceDocument.ACTIVITY)
        replay = self._idempotency_replay(
            activity, idempotency_key, "POST", path, request_digest
        )
        if replay:
            return replay

        receipt = self._validate_reply_receipt(receipt_input)
        replies_data = self.documents.load(WorkspaceDocument.REPLIES)
        reply = _find(replies_data.get("replies", []), reply_id, "reply")
        mismatched = _reply_receipt_mismatches(receipt, reply)
        if mismatched:
            raise ReplyReceiptConflictError(
                "reply receipt does not match the approved command",
                {"fields": mismatched},
            )

        duplicate, event_details = _apply_terminal_reply_state(
            reply, receipt, self._utc_now
        )
        if event_details is not None:
            self._event(
                activity,
                "reply.{}".format(reply["state"]),
                capture_id=reply["capture_id"],
                task_id=reply["task_id"],
                reply_id=reply["id"],
                details=event_details,
            )

        response_body: dict[str, Any] = {"data": self._project_reply(reply)}
        if duplicate:
            response_body["meta"] = {"duplicate": True}
        self._record_idempotency(
            activity,
            idempotency_key,
            "POST",
            path,
            request_digest,
            200,
            None,
            response_ref={"kind": "reply", "id": reply["id"]},
            response_meta={"duplicate": True} if duplicate else None,
        )
        self.documents.save_many(
            {WorkspaceDocument.REPLIES: replies_data, WorkspaceDocument.ACTIVITY: activity},
            operation_id="reply-receipt-{}".format(idempotency_key),
        )
        return {"status": 200, "body": response_body}
