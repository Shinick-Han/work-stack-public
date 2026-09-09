"""Capture review and reply-receipt rules, independent of any store.

Two closely related admissions live here: whether a re-ingested capture packet
carries the SAME reviewed content as the one already stored, and whether a
provider receipt matches the reply it claims to complete. Both are pure
decisions over supplied documents; the caller owns the transaction and the
clock.
"""

from __future__ import annotations

import copy
import secrets
import uuid
from typing import Any, Callable

from .capture import SHA256_RE, canonical_digest, parse_rfc3339
from .knowledge_capture_packets import (
    CAPTURE_SCHEMA_VERSION as IMPORTED_CAPTURE_SCHEMA_VERSION,
    KnowledgeImportError,
    stored_retrieval_projection,
)
from .service_domain import (
    ERROR_CODE_RE,
    REPLY_CAPABILITIES,
    REPLY_OUTCOMES,
    REPLY_RECEIPT_OPTIONAL_FIELDS,
    REPLY_RECEIPT_REQUIRED_FIELDS,
)
from .service_errors import (
    DomainError,
    ReplyReceiptConflictError,
    SourceRevisionConflictError,
)
from .service_text import _microsoft_web_url, _remote_message_reference
from .store_errors import StoreCorruptError


def _capture_review_digest(capture: dict[str, Any]) -> str:
    """Digest reviewed fields while ignoring server-owned action links."""

    normalized = copy.deepcopy(capture.get("normalized", {}))
    for action in normalized.get("action_items", []):
        if isinstance(action, dict):
            action.pop("task_id", None)
    return canonical_digest({
        "normalized": normalized,
        "task_hints": copy.deepcopy(capture.get("task_hints", [])),
    })


def _require_matching_capture_review(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> None:
    if _capture_review_digest(existing) != _capture_review_digest(incoming):
        raise SourceRevisionConflictError(
            "the same source fingerprint has different reviewed capture content"
        )


def _projected_capture_retrieval(capture: dict[str, Any]) -> dict[str, Any] | None:
    """The retrieval state a stored 1.1 capture may be shown with, re-derived.

    A stored record keeps only the sanitized retrieval *wire*; the trusted
    parts of the projection -- ``origin``, ``origin_state``, every
    ``version_state`` and ``capture_source_type`` -- are recomputed here on
    every read with **no** caller-supplied ``RetrievalVerification``. Nothing a
    writer stored can promote them, so a reader is never handed an attestation
    the host does not hold.

    A stored 1.0 record has no retrieval state at all and is left exactly as it
    was. A stored 1.1 record whose retrieval no longer validates is a corrupt
    store, not a capture to display: refusing here is what keeps malformed
    evidence from being read back as trusted metadata after a restart.
    """

    if capture.get("schema_version") != IMPORTED_CAPTURE_SCHEMA_VERSION:
        return None
    try:
        return stored_retrieval_projection(capture.get("retrieval"))
    except KnowledgeImportError as error:
        raise StoreCorruptError(
            "captures.json knowledge import record is invalid"
        ) from error


def _refuse_imported_capture_overwrite(existing: dict[str, Any]) -> None:
    """Generic Capture ingestion stays 1.0-only and never lands on an import.

    The source key and the fingerprint are both derived from values a v1.0
    packet may spell for itself, so a packet can be built that collides with an
    imported record. Re-ingesting over one would erase evidence the ledger has
    already accounted for, so the collision refuses instead. Existing 1.0
    records keep their released re-ingest, stale and conflict behaviour
    unchanged.
    """

    if existing.get("schema_version") == IMPORTED_CAPTURE_SCHEMA_VERSION:
        raise SourceRevisionConflictError(
            "the stored capture was imported against a knowledge request"
        )


def _capture_task_intent_id(task_fields: dict[str, Any]) -> str | None:
    value = task_fields.get("intent_id")
    if value is None:
        return None
    try:
        normalized = str(uuid.UUID(value)) if isinstance(value, str) else ""
        valid = normalized == value and uuid.UUID(normalized).int != 0
    except ValueError:
        valid = False
    if not valid:
        raise DomainError(
            "intent_id must be a canonical non-nil UUID",
            {"field": "intent_id"},
        )
    return normalized


def _validate_capture_task_fields(
    task_fields: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    allowed = {
        "intent_id", "title", "detail", "priority", "due", "tags",
        "objective_ids", "parent_id", "dependencies",
    }
    unknown = sorted(set(task_fields) - allowed)
    if unknown:
        raise DomainError("unknown task fields", {"fields": unknown})
    if "title" not in task_fields or not isinstance(task_fields["title"], str):
        raise DomainError("title is required and must be a string", {"field": "title"})
    _validate_capture_task_scalars(task_fields)
    _validate_capture_task_arrays(task_fields)
    intent_id = _capture_task_intent_id(task_fields)
    task_input = {field: value for field, value in task_fields.items() if field != "intent_id"}
    return intent_id, task_input


def _validate_capture_task_scalars(task_fields: dict[str, Any]) -> None:
    for field in ("detail", "priority"):
        if field in task_fields and not isinstance(task_fields[field], str):
            raise DomainError("{} must be a string".format(field), {"field": field})
    due = task_fields.get("due")
    if "due" in task_fields and due is not None and not isinstance(due, str):
        raise DomainError("due must be an ISO date or null", {"field": "due"})
    parent_id = task_fields.get("parent_id")
    if "parent_id" in task_fields and parent_id is not None and not isinstance(parent_id, str):
        raise DomainError("parent_id must be a task ID or null", {"field": "parent_id"})


def _validate_capture_task_arrays(task_fields: dict[str, Any]) -> None:
    for field in ("tags", "objective_ids", "dependencies"):
        value = task_fields.get(field)
        if field in task_fields and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            raise DomainError("{} must be an array of strings".format(field), {"field": field})


def _validate_reply_receipt_shape(receipt: dict[str, Any]) -> None:
    unknown = sorted(
        set(receipt) - REPLY_RECEIPT_REQUIRED_FIELDS - REPLY_RECEIPT_OPTIONAL_FIELDS
    )
    missing = sorted(REPLY_RECEIPT_REQUIRED_FIELDS - set(receipt))
    if unknown or missing:
        raise DomainError(
            "reply receipt has unknown or missing fields",
            {"missing": missing, "unknown": unknown},
        )


def _reply_receipt_provider(receipt: dict[str, Any]) -> str:
    provider = receipt["provider"]
    if not isinstance(provider, str) or provider not in REPLY_CAPABILITIES:
        raise DomainError("provider is not supported", {"field": "provider"})
    return provider


def _reply_receipt_outcome(receipt: dict[str, Any]) -> str:
    outcome = receipt["outcome"]
    if outcome not in REPLY_OUTCOMES:
        raise DomainError(
            "outcome must be sent, failed, or unknown", {"field": "outcome"}
        )
    return outcome


def _reply_receipt_occurred_at(receipt: dict[str, Any]) -> str:
    occurred_at = receipt["occurred_at"]
    if not isinstance(occurred_at, str):
        raise DomainError("occurred_at must be a string", {"field": "occurred_at"})
    try:
        parse_rfc3339(occurred_at, "occurred_at")
    except ValueError as error:
        raise DomainError(
            "occurred_at must be strict RFC3339", {"field": "occurred_at"}
        ) from error
    return occurred_at


def _reply_receipt_digests(receipt: dict[str, Any]) -> dict[str, str]:
    digests: dict[str, str] = {}
    for field in ("body_digest", "target_digest"):
        value = receipt[field]
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise DomainError(
                "{} must be canonical SHA-256".format(field), {"field": field}
            )
        digests[field] = value
    return digests


def _project_reply_receipt_optional_fields(
    receipt: dict[str, Any], projected: dict[str, Any]
) -> None:
    if "remote_message_ref" in receipt:
        projected["remote_message_ref"] = _remote_message_reference(
            receipt["remote_message_ref"]
        )
    if "web_url" in receipt:
        projected["web_url"] = _microsoft_web_url(receipt["web_url"])
    if "error_code" in receipt:
        error_code = receipt["error_code"]
        if not isinstance(error_code, str) or not ERROR_CODE_RE.fullmatch(error_code):
            raise DomainError(
                "error_code must be a bounded symbolic code",
                {"field": "error_code"},
            )
        projected["error_code"] = error_code


def _reply_receipt_mismatches(
    receipt: dict[str, Any], reply: dict[str, Any]
) -> list[str]:
    mismatched: list[str] = []
    if receipt["reply_id"] != reply["id"]:
        mismatched.append("reply_id")
    if receipt["provider"] != reply["provider"]:
        mismatched.append("provider")
    for field in ("body_digest", "target_digest"):
        if not secrets.compare_digest(receipt[field], reply[field]):
            mismatched.append(field)
    return mismatched


def _apply_terminal_reply_state(
    reply: dict[str, Any], receipt: dict[str, Any], now: Callable[[], str]
) -> tuple[bool, dict[str, Any] | None]:
    """Apply one terminal receipt.

    ``now`` is the caller's clock reader rather than a value, so the duplicate
    and conflict paths still read no clock at all.
    """

    stored_receipt = reply.get("receipt")
    if stored_receipt is not None:
        if stored_receipt != receipt:
            raise ReplyReceiptConflictError(
                "reply already has a different terminal receipt",
                {"reply_id": reply["id"], "state": reply.get("state")},
            )
        return True, None
    if reply.get("state") != "approved":
        raise ReplyReceiptConflictError(
            "reply is already terminal",
            {"reply_id": reply["id"], "state": reply.get("state")},
        )
    reply["state"] = receipt["outcome"]
    reply["receipt"] = receipt
    reply["updated_at"] = now()
    event_details: dict[str, Any] = {
        "provider": reply["provider"],
        "state": reply["state"],
    }
    if "error_code" in receipt:
        event_details["error_code"] = receipt["error_code"]
    return False, event_details
