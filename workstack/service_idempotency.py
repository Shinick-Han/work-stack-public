"""Activity events and the keyed-intent receipt ledger.

One replay rule serves every keyed command: a key bound to a different method,
path or request digest is a conflict, and a matching key returns its frozen
response with ``replayed`` set. A receipt may store its response by reference
so a reply projection is never frozen into the ledger.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .capture import canonical_digest
from .service_domain import _find, _next_id
from .service_errors import DomainError, IdempotencyConflictError
from .storage.document_repository import WorkspaceDocument


class IdempotencyMixin:
    """Activity event append, request digests and the receipt ledger."""

    def _raw_request_digest(
        self, body: dict[str, Any], supplied: str | None
    ) -> str:
        """The digest of the ORIGINAL parsed body, verifying any supplied one.

        Serialization can still fail on a value the domain accepted, so the
        failure is mapped to the same constant refusal rather than escaping as
        a Python encoding or recursion error carrying input detail.
        """

        try:
            computed = canonical_digest(body)
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
            raise DomainError("the checkpoint transition request is invalid") from error
        if supplied is not None and supplied != computed:
            raise DomainError("the request digest does not match the request body")
        return computed

    @classmethod
    def _event(
        cls,
        activity: dict[str, Any],
        event_type: str,
        *,
        capture_id: str | None = None,
        task_id: str | None = None,
        reply_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": _next_id(activity.setdefault("activity", []), "E", 6),
            "type": event_type,
            "created_at": cls._utc_now(),
            "details": details or {},
        }
        if capture_id:
            event["capture_id"] = capture_id
        if task_id:
            event["task_id"] = task_id
        if reply_id:
            event["reply_id"] = reply_id
        activity["activity"].append(event)
        return event

    @staticmethod
    def _request_digest(body: dict[str, Any]) -> str:
        return canonical_digest(body)

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value):
            raise DomainError(
                "Idempotency-Key must match [A-Za-z0-9._:-]{8,128}",
                {"header": "Idempotency-Key"},
            )
        return value

    def _idempotency_replay(
        self,
        activity: dict[str, Any],
        key: str,
        method: str,
        path: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        self._validate_idempotency_key(key)
        for record in activity.setdefault("idempotency", []):
            if record.get("key") != key:
                continue
            if (
                record.get("method") != method
                or record.get("path") != path
                or record.get("request_digest") != request_digest
            ):
                raise IdempotencyConflictError(
                    "Idempotency-Key was already used for a different request",
                    {"key": key},
                )
            response_ref = record.get("response_ref")
            if response_ref is not None:
                if (
                    not isinstance(response_ref, dict)
                    or response_ref.get("kind") != "reply"
                    or not isinstance(response_ref.get("id"), str)
                ):
                    raise DomainError("stored idempotency response reference is invalid")
                reply = _find(
                    self.documents.load(WorkspaceDocument.REPLIES).get("replies", []),
                    response_ref["id"],
                    "reply",
                )
                body = {"data": self._project_reply(reply)}
                stored_meta = record.get("response_meta")
                if isinstance(stored_meta, dict) and stored_meta:
                    body["meta"] = copy.deepcopy(stored_meta)
            else:
                body = copy.deepcopy(record["response_body"])
            body.setdefault("meta", {})["replayed"] = True
            return {"status": 200, "body": body}
        return None

    @classmethod
    def _record_idempotency(
        cls,
        activity: dict[str, Any],
        key: str,
        method: str,
        path: str,
        request_digest: str,
        response_status: int,
        response_body: dict[str, Any] | None,
        *,
        response_ref: dict[str, str] | None = None,
        response_meta: dict[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "key": key,
            "method": method,
            "path": path,
            "request_digest": request_digest,
            "response_status": response_status,
            "created_at": cls._utc_now(),
        }
        if response_ref is None:
            if response_body is None:
                raise ValueError("response_body is required without response_ref")
            record["response_body"] = copy.deepcopy(response_body)
        else:
            record["response_ref"] = copy.deepcopy(response_ref)
            if response_meta:
                record["response_meta"] = copy.deepcopy(response_meta)
        activity.setdefault("idempotency", []).append(record)
