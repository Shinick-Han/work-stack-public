"""The shared domain vocabulary: enumerations, limits and record primitives.

This is the leaf every other service module reads. It owns the closed status
and priority sets, the reply and reference limits, and the small primitives -
identifier allocation, record lookup, revision guards - that the task, OKR,
capture and reply modules all express their rules in terms of.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Iterable

from .service_errors import (
    DomainError,
    NotFoundError,
    RevisionConflictError,
    RevisionExhaustedError,
)
from .store import MAX_REVISION, StoreCorruptError


TASK_STATUSES = ("open", "started", "done", "dropped")
OBJECTIVE_STATUSES = ("active", "done", "dropped")
PRIORITIES = ("P0", "P1", "P2", "P3")
CAPTURE_STATUSES = ("inbox", "linked", "converted", "dismissed")
REPLY_CAPABILITIES = {
    "microsoft-outlook": "outlook.reply",
    "microsoft-teams": "teams.reply",
}
REPLY_STATES = ("approved", "sent", "failed", "unknown")
REPLY_OUTCOMES = REPLY_STATES[1:]
REPLY_TARGET_FIELDS = (
    "resource_type",
    "connection_ref",
    "container_ref",
    "object_ref",
    "version_ref",
)
REPLY_BODY_MAX = 12_000
REPLY_TARGET_REF_MAX = 512
REMOTE_MESSAGE_REF_MAX = 512
MICROSOFT_WEB_URL_MAX = 4096
ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
REMOTE_MESSAGE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:@/+%=-]{0,511}$")
REMOTE_HEADER_PREFIX_RE = re.compile(
    r"(?i)^(?:from|to|cc|bcc|subject|sent|date):"
)
HTML_TAG_RE = re.compile(r"<\/?[A-Za-z][^>]*>")
MAIL_HEADER_RE = re.compile(r"(?im)^(?:from|to|cc|bcc|subject|sent|date):\s*.+$")
QUOTED_REPLY_RE = re.compile(r"(?im)^on .{1,240} wrote:\s*$")
QUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")
SECRET_TEXT_RE = re.compile(
    r"(?i)(?:\bbearer\s+[A-Za-z0-9._~+/=-]{12,}|"
    r"\b(?:access_token|refresh_token|id_token)\b\s*[:=]\s*[^\s&]{8,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b)"
)
RAW_CANARY_RE = re.compile(r"(?:RAW|ATTACHMENT)_CANARY_DO_NOT_STORE", re.I)
TASK_PATCH_FIELDS = frozenset({
    "title",
    "detail",
    "status",
    "priority",
    "due",
    "scheduled",
    "estimate_minutes",
    "tags",
    "objective_ids",
    "parent_id",
    "dependencies",
    "key_result_refs",
    "revision",
})


KEY_RESULT_REF_FIELDS = frozenset({"objective_id", "key_result_id"})


REPLY_RECEIPT_REQUIRED_FIELDS = frozenset({
    "schema_version",
    "reply_id",
    "provider",
    "outcome",
    "occurred_at",
    "body_digest",
    "target_digest",
})


REPLY_RECEIPT_OPTIONAL_FIELDS = frozenset({
    "remote_message_ref", "web_url", "error_code"
})


TASK_CREATE_FIELDS = frozenset({
    "title", "detail", "priority", "due", "scheduled", "estimate_minutes",
    "tags", "objective_ids",
})


def _next_id(records: Iterable[dict[str, Any]], prefix: str, width: int = 0) -> str:
    pattern = re.compile(r"^{}-(\d+)$".format(re.escape(prefix)), re.I)
    largest = 0
    for record in records:
        match = pattern.match(str(record.get("id", "")))
        if match:
            largest = max(largest, int(match.group(1)))
    number = largest + 1
    return "{}-{:0{width}d}".format(prefix.upper(), number, width=width) if width else "{}-{}".format(prefix.upper(), number)


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("{} is required".format(field))
    return text


def _find(records: Iterable[dict[str, Any]], record_id: str, label: str) -> dict[str, Any]:
    wanted = record_id.strip().upper()
    for record in records:
        if str(record.get("id", "")).upper() == wanted:
            return record
    raise NotFoundError("unknown {}: {}".format(label, record_id), {"id": record_id})


def _task_uid(workspace_id: str, task_id: str) -> str:
    return str(uuid.uuid5(uuid.UUID(workspace_id), task_id))


def _revision(record: dict[str, Any]) -> int:
    if "revision" not in record:
        raise StoreCorruptError("persisted task revision is missing")
    value = record["revision"]
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise StoreCorruptError("persisted task revision is invalid")
    return value


def _guard_revision(task: dict[str, Any], expected_revision: int | None) -> int:
    current_revision = _revision(task)
    if expected_revision is None:
        return _next_revision(task)
    if type(expected_revision) is not int or expected_revision < 0:
        raise DomainError("revision is required and must be a non-negative integer")
    if expected_revision != current_revision:
        raise RevisionConflictError(
            "task revision is stale",
            {"expected": current_revision, "received": expected_revision},
        )
    return _next_revision(task)


def _next_revision(record: dict[str, Any]) -> int:
    current = _revision(record)
    if current == MAX_REVISION:
        raise RevisionExhaustedError(
            "task revision cannot advance beyond the safe integer limit",
            {"maximum": MAX_REVISION},
        )
    return current + 1
