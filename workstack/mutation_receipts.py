"""Activity-event receipts for validated A1 mutation notices.

Pure mapping onto the existing v3 ``activity.json`` event list. This module
does not open a Store, start a transaction, or talk to HTTP.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any, Mapping

from .mutation_notice import (
    COMMITTED,
    TASK_STATUS_OPERATION,
    MutationNoticeError,
    build_notice,
    serialize_notice,
    validate_notice,
)

EVENT_TYPE = "mutation.notice"
ACTOR = "local.user"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
NOTICE_DETAIL_KEY = "notice"
CURSOR_VERSION = 1
MIN_CURSOR_CHARS = 32
MAX_CURSOR_CHARS = 160
MAX_CURSOR_BYTES = 120
MAX_NOTICE_BLOB = 8192
EVENT_ID_RE = re.compile(r"^E-[0-9]{6,}$")
_EVENT_NUMBER = re.compile(r"^E-(\d+)$", re.I)
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_QUERY_INVALID = "mutation notice query is invalid"


class MutationReceiptError(ValueError):
    """Receipt refusal with a stable public code and HTTP status."""

    def __init__(
        self,
        code: str,
        message: str = "invalid mutation receipt",
        *,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}
        if status is not None:
            self.status = status
        elif code == "not_found":
            self.status = 404
        elif code in {"revision_conflict", "idempotency_conflict", "revision_exhausted"}:
            self.status = 409
        else:
            self.status = 400


def unkeyed_status_key(task_uid: str, before_revision: int) -> str:
    return "ts:{}:{}".format(task_uid, before_revision)


def notice_source_for(provenance: str) -> str:
    return "cli" if provenance == "cli" else "gui"


def next_activity_event_id(events: list[dict[str, Any]]) -> str:
    largest = 0
    for record in events:
        match = _EVENT_NUMBER.match(str(record.get("id", "")))
        if match:
            largest = max(largest, int(match.group(1)))
    return "E-{:06d}".format(largest + 1)


def encode_list_cursor(workspace_uid: str, event_id: str) -> str:
    payload = {"e": event_id, "v": CURSOR_VERSION, "w": workspace_uid}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_list_cursor(cursor: str, workspace_uid: str) -> str:
    raw = _cursor_bytes(cursor)
    payload = _cursor_payload(raw)
    _require_cursor_workspace(payload, workspace_uid)
    return _cursor_event_id(payload)


def parse_list_query(query: Mapping[str, list[str]]) -> tuple[str | None, int]:
    if set(query) - {"cursor", "limit"}:
        raise ValueError(_QUERY_INVALID)
    limit_values = query.get("limit", [str(DEFAULT_LIMIT)])
    cursor_values = query.get("cursor", [])
    if len(limit_values) != 1 or len(cursor_values) > 1:
        raise ValueError(_QUERY_INVALID)
    cursor = _query_cursor(cursor_values)
    return cursor, _query_limit(limit_values[0])


def find_notice(activity: Mapping[str, Any], notice_id: str) -> dict[str, Any] | None:
    for _event_id, notice in _workspace_pairs(activity, workspace_uid=None):
        if notice["notice_id"] == notice_id:
            return notice
    return None


def page_notices(
    activity: Mapping[str, Any],
    workspace_uid: str,
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    items = _workspace_pairs(activity, workspace_uid)
    start = _page_start(items, cursor, workspace_uid)
    window = items[start : start + limit]
    return {
        "items": [notice for _event_id, notice in window],
        "next_cursor": _next_page_cursor(items, window, start, limit, workspace_uid),
    }


def record_committed_status_notice(
    activity: dict[str, Any],
    *,
    workspace_uid: str,
    task: Mapping[str, Any],
    status_before: str,
    status_after: str,
    before_revision: int,
    after_revision: int,
    idempotency_key: str,
    source: str,
    created_at: str,
) -> dict[str, Any]:
    notice = build_notice(
        workspace_uid=workspace_uid,
        entity_kind="task",
        entity_uid=task["uid"],
        operation=TASK_STATUS_OPERATION,
        before_revision=before_revision,
        after_revision=after_revision,
        source=source,
        actor=ACTOR,
        idempotency_key=idempotency_key,
        commit_state=COMMITTED,
        status_before=status_before,
        status_after=status_after,
    )
    existing = find_notice(activity, str(notice["notice_id"]))
    if existing is not None:
        if serialize_notice(existing) != serialize_notice(notice):
            raise MutationReceiptError("idempotency_conflict")
        return existing
    events = activity.setdefault("activity", [])
    events.append(
        {
            "id": next_activity_event_id(events),
            "type": EVENT_TYPE,
            "created_at": created_at,
            "task_id": task["id"],
            "details": _opaque_details(notice),
        }
    )
    return notice


def _opaque_details(notice: Mapping[str, Any]) -> dict[str, str]:
    return {NOTICE_DETAIL_KEY: serialize_notice(notice).decode("utf-8")}


def _query_cursor(cursor_values: list[str]) -> str | None:
    if not cursor_values:
        return None
    cursor = cursor_values[0]
    _cursor_bytes(cursor)
    return cursor


def _query_limit(raw: str) -> int:
    try:
        limit = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(_QUERY_INVALID) from error
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(_QUERY_INVALID)
    return limit


def _cursor_bytes(cursor: str) -> bytes:
    if type(cursor) is not str:
        raise ValueError(_QUERY_INVALID)
    if not MIN_CURSOR_CHARS <= len(cursor) <= MAX_CURSOR_CHARS:
        raise ValueError(_QUERY_INVALID)
    if EVENT_ID_RE.fullmatch(cursor) is not None:
        raise ValueError(_QUERY_INVALID)
    if _CURSOR_RE.fullmatch(cursor) is None:
        raise ValueError(_QUERY_INVALID)
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as error:
        raise ValueError(_QUERY_INVALID) from error
    if not 1 <= len(raw) <= MAX_CURSOR_BYTES:
        raise ValueError(_QUERY_INVALID)
    return raw


def _cursor_payload(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as error:
        raise ValueError(_QUERY_INVALID) from error
    if type(payload) is not dict:
        raise ValueError(_QUERY_INVALID)
    if set(payload) != {"e", "v", "w"}:
        raise ValueError(_QUERY_INVALID)
    if payload.get("v") != CURSOR_VERSION:
        raise ValueError(_QUERY_INVALID)
    return payload


def _require_cursor_workspace(payload: Mapping[str, Any], workspace_uid: str) -> None:
    marked = payload.get("w")
    if type(marked) is not str:
        raise ValueError(_QUERY_INVALID)
    if marked != workspace_uid:
        raise ValueError(_QUERY_INVALID)


def _cursor_event_id(payload: Mapping[str, Any]) -> str:
    event_id = payload.get("e")
    if type(event_id) is not str:
        raise ValueError(_QUERY_INVALID)
    if EVENT_ID_RE.fullmatch(event_id) is None:
        raise ValueError(_QUERY_INVALID)
    return event_id


def _page_start(
    items: list[tuple[str, dict[str, Any]]],
    cursor: str | None,
    workspace_uid: str,
) -> int:
    if cursor is None:
        return 0
    event_id = decode_list_cursor(cursor, workspace_uid)
    ids = [item_id for item_id, _notice in items]
    if event_id not in ids:
        raise ValueError(_QUERY_INVALID)
    return ids.index(event_id) + 1


def _next_page_cursor(
    items: list[tuple[str, dict[str, Any]]],
    window: list[tuple[str, dict[str, Any]]],
    start: int,
    limit: int,
    workspace_uid: str,
) -> str | None:
    if start + limit >= len(items):
        return None
    if not window:
        return None
    return encode_list_cursor(workspace_uid, window[-1][0])


def _workspace_pairs(
    activity: Mapping[str, Any], workspace_uid: str | None
) -> list[tuple[str, dict[str, Any]]]:
    events = activity.get("activity")
    if not isinstance(events, list):
        return []
    pairs: list[tuple[str, dict[str, Any]]] = []
    for event in reversed(events):
        notice = _notice_from_event(event)
        if notice is None:
            continue
        if workspace_uid is not None and notice["workspace_uid"] != workspace_uid:
            continue
        pairs.append((str(event["id"]), notice))
    return pairs


def _notice_from_event(event: object) -> dict[str, Any] | None:
    if not isinstance(event, dict) or event.get("type") != EVENT_TYPE:
        return None
    return _notice_from_opaque_details(event.get("details"))


def _notice_from_opaque_details(details: object) -> dict[str, Any] | None:
    if type(details) is not dict:
        return None
    if set(details) != {NOTICE_DETAIL_KEY}:
        return None
    blob = details[NOTICE_DETAIL_KEY]
    if type(blob) is not str:
        raise MutationReceiptError("invalid_request")
    if not 1 <= len(blob) <= MAX_NOTICE_BLOB:
        raise MutationReceiptError("invalid_request")
    try:
        parsed = json.loads(blob)
        notice = validate_notice(parsed)
    except (TypeError, ValueError, MutationNoticeError) as error:
        raise MutationReceiptError("invalid_request") from error
    if serialize_notice(notice) != blob.encode("utf-8"):
        raise MutationReceiptError("invalid_request")
    return notice
