"""Closed Capture unlink receipt: validate, mint, and look up.

Pure mapping onto the existing v3 ``activity.json`` event list. This module
does not open a Store, start a transaction, or talk to HTTP. Callers persist
the activity document in the same transaction as the mutating unlink or Undo.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from typing import Any, Mapping

from .capture import SHA256_RE, canonical_digest
from .capture_unlink_policy import (
    CaptureUnlinkPolicyError,
    admit_displayed_capture_revision,
)


FORMAT = "workstack.capture-unlink-receipt"
SCHEMA_VERSION = 1
EVENT_TYPE = "capture.unlink_receipt"
DETAIL_KEY = "receipt"
COMMITTED = "committed"
CAPTURE_STATUSES = frozenset({"inbox", "linked", "converted", "dismissed"})
IDEMPOTENCY_MIN = 8
IDEMPOTENCY_MAX = 128
MAX_RECEIPT_BLOB = 8192
MAX_RECEIPT_ID_TEXT = 48
RECEIPT_FIELDS = (
    "format",
    "schema_version",
    "receipt_id",
    "workspace_uid",
    "capture_id",
    "task_id",
    "task_uid",
    "status_before",
    "before_revision",
    "after_revision",
    "after_digest",
    "idempotency_key",
    "commit_state",
)
_IDEMPOTENCY = re.compile(r"\A[A-Za-z0-9._:-]{8,128}\Z")
_CAPTURE_ID = re.compile(r"^C-\d{4,}$")
_TASK_ID = re.compile(r"^T-\d{4,}$")
_EVENT_NUMBER = re.compile(r"^E-(\d+)$", re.I)
_RECEIPT_NS = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://workstack.local/contracts/workstack-capture-unlink-receipt-v1",
)


class CaptureUnlinkReceiptError(ValueError):
    """Closed receipt refusal. Callers map the code onto their public error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _refuse(code: str) -> CaptureUnlinkReceiptError:
    return CaptureUnlinkReceiptError(code)


def _text(value: object, *, code: str = "malformed") -> str:
    if type(value) is not str:
        raise _refuse(code)
    return value


def _uuid(value: object) -> str:
    text = _text(value)
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, ValueError, TypeError) as error:
        raise _refuse("malformed") from error
    if str(parsed) != text or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        raise _refuse("malformed")
    return text


def admit_receipt_id(value: object) -> str:
    return _uuid(value)


def derive_receipt_id(*, workspace_uid: str, idempotency_key: str) -> str:
    return str(uuid.uuid5(_RECEIPT_NS, "{}:{}".format(workspace_uid, idempotency_key)))


def _idempotency_key(value: object) -> str:
    key = _text(value)
    if not IDEMPOTENCY_MIN <= len(key) <= IDEMPOTENCY_MAX:
        raise _refuse("malformed")
    if _IDEMPOTENCY.fullmatch(key) is None:
        raise _refuse("malformed")
    return key


def _display_id(value: object, pattern: re.Pattern[str]) -> str:
    text = _text(value)
    if pattern.fullmatch(text) is None:
        raise _refuse("malformed")
    return text


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def capture_row_digest(capture: Mapping[str, Any]) -> str:
    return canonical_digest(copy.deepcopy(dict(capture)))


def validate_receipt(value: object) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise _refuse("malformed")
    if set(value) != set(RECEIPT_FIELDS):
        raise _refuse("malformed")
    if value["format"] != FORMAT:
        raise _refuse("malformed")
    if value["schema_version"] != SCHEMA_VERSION or type(value["schema_version"]) is not int:
        raise _refuse("malformed")
    if value["commit_state"] != COMMITTED:
        raise _refuse("malformed")
    workspace_uid = _uuid(value["workspace_uid"])
    idempotency_key = _idempotency_key(value["idempotency_key"])
    receipt_id = admit_receipt_id(value["receipt_id"])
    if receipt_id != derive_receipt_id(
        workspace_uid=workspace_uid, idempotency_key=idempotency_key
    ):
        raise _refuse("malformed")
    try:
        before_revision = admit_displayed_capture_revision(value["before_revision"])
        after_revision = admit_displayed_capture_revision(value["after_revision"])
    except CaptureUnlinkPolicyError as error:
        raise _refuse("malformed") from error
    if after_revision != before_revision + 1:
        raise _refuse("malformed")
    digest = _text(value["after_digest"])
    if SHA256_RE.fullmatch(digest) is None:
        raise _refuse("malformed")
    status = _text(value["status_before"])
    if status not in CAPTURE_STATUSES:
        raise _refuse("malformed")
    return {
        "after_digest": digest,
        "after_revision": after_revision,
        "before_revision": before_revision,
        "capture_id": _display_id(value["capture_id"], _CAPTURE_ID),
        "commit_state": COMMITTED,
        "format": FORMAT,
        "idempotency_key": idempotency_key,
        "receipt_id": receipt_id,
        "schema_version": SCHEMA_VERSION,
        "status_before": status,
        "task_id": _display_id(value["task_id"], _TASK_ID),
        "task_uid": _uuid(value["task_uid"]),
        "workspace_uid": workspace_uid,
    }


def serialize_receipt(value: object) -> bytes:
    return _canonical_json(validate_receipt(value))


def build_receipt(
    *,
    workspace_uid: str,
    capture: Mapping[str, Any],
    task: Mapping[str, Any],
    status_before: str,
    before_revision: int,
    idempotency_key: str,
) -> dict[str, Any]:
    workspace = _uuid(workspace_uid)
    key = _idempotency_key(idempotency_key)
    try:
        after_revision = admit_displayed_capture_revision(capture.get("revision"))
    except CaptureUnlinkPolicyError as error:
        raise _refuse("malformed") from error
    if after_revision != before_revision + 1:
        raise _refuse("malformed")
    return validate_receipt(
        {
            "after_digest": capture_row_digest(capture),
            "after_revision": after_revision,
            "before_revision": before_revision,
            "capture_id": capture["id"],
            "commit_state": COMMITTED,
            "format": FORMAT,
            "idempotency_key": key,
            "receipt_id": derive_receipt_id(workspace_uid=workspace, idempotency_key=key),
            "schema_version": SCHEMA_VERSION,
            "status_before": status_before,
            "task_id": task["id"],
            "task_uid": task["uid"],
            "workspace_uid": workspace,
        }
    )


def next_activity_event_id(events: list[dict[str, Any]]) -> str:
    largest = 0
    for record in events:
        match = _EVENT_NUMBER.match(str(record.get("id", "")))
        if match:
            largest = max(largest, int(match.group(1)))
    return "E-{:06d}".format(largest + 1)


def record_committed_unlink_receipt(
    activity: dict[str, Any],
    receipt: Mapping[str, Any],
    created_at: str,
) -> dict[str, Any]:
    validated = validate_receipt(receipt)
    existing = _canonical_receipt_for_id(
        activity, validated["receipt_id"], tainted_alone="malformed"
    )
    if existing is not None:
        if serialize_receipt(existing) != serialize_receipt(validated):
            raise _refuse("idempotency_conflict")
        return existing
    events = activity.setdefault("activity", [])
    events.append(
        {
            "id": next_activity_event_id(events),
            "type": EVENT_TYPE,
            "created_at": created_at,
            "capture_id": validated["capture_id"],
            "task_id": validated["task_id"],
            "details": {DETAIL_KEY: serialize_receipt(validated).decode("utf-8")},
        }
    )
    return validated


def find_unlink_receipt(
    activity: Mapping[str, Any],
    receipt_id: object,
    *,
    workspace_uid: str,
    capture_id: str,
) -> dict[str, Any]:
    wanted = admit_receipt_id(receipt_id)
    workspace = _uuid(workspace_uid)
    capture = _display_id(capture_id, _CAPTURE_ID)
    receipt = _canonical_receipt_for_id(
        activity, wanted, tainted_alone="not_found"
    )
    if receipt is None:
        raise _refuse("not_found")
    if receipt["workspace_uid"] != workspace or receipt["capture_id"] != capture:
        raise _refuse("not_found")
    return receipt


def receipt_id_for_idempotency_key(
    activity: Mapping[str, Any], idempotency_key: str
) -> str | None:
    """The closed receipt minted under this unlink key, if one is durable."""

    related_ids: list[str] = []
    seen: set[str] = set()
    for event in activity.get("activity") or []:
        classified = _classified_receipt_event(event)
        if classified is None or classified["idempotency_key"] != idempotency_key:
            continue
        if classified["receipt_id"] not in seen:
            seen.add(classified["receipt_id"])
            related_ids.append(classified["receipt_id"])
    if not related_ids:
        return None
    if len(related_ids) != 1:
        raise _refuse("idempotency_conflict")
    try:
        receipt = _canonical_receipt_for_id(
            activity, related_ids[0], tainted_alone="not_found"
        )
    except CaptureUnlinkReceiptError as error:
        if error.code == "not_found":
            return None
        raise
    if receipt is None:
        return None
    return str(receipt["receipt_id"])


def _matching_receipt_events(
    activity: Mapping[str, Any], receipt_id: str
) -> list[dict[str, Any]]:
    """Every classified receipt event naming this identity, in stored order."""

    matches: list[dict[str, Any]] = []
    for event in activity.get("activity") or []:
        classified = _classified_receipt_event(event)
        if classified is None or classified["receipt_id"] != receipt_id:
            continue
        matches.append(classified)
    return matches


def _canonical_receipt_for_id(
    activity: Mapping[str, Any],
    receipt_id: str,
    *,
    tainted_alone: str,
) -> dict[str, Any] | None:
    matches = _matching_receipt_events(activity, receipt_id)
    if not matches:
        return None
    if any(
        item["receipt"] is not None and not item["envelope_ok"] for item in matches
    ):
        raise _refuse("malformed")
    canonical = [item["receipt"] for item in matches if item["receipt"] is not None]
    tainted = [item for item in matches if item["receipt"] is None]
    if tainted:
        if canonical:
            raise _refuse("malformed")
        raise _refuse(tainted_alone)
    blobs = {serialize_receipt(item) for item in canonical}
    if len(blobs) != 1:
        raise _refuse("idempotency_conflict")
    return canonical[0]


def _classified_receipt_event(event: object) -> dict[str, Any] | None:
    """Identify one receipt event without rewriting it.

    Ordinary activity and blobs that cannot name a receipt_id stay no-authority.
    A named but noncanonical or invalid closed body is tainted for that identity.
    """

    if not isinstance(event, dict) or event.get("type") != EVENT_TYPE:
        return None
    details = event.get("details")
    if type(details) is not dict:
        return None
    parsed = _parsed_receipt_blob(details.get(DETAIL_KEY))
    if parsed is None:
        return None
    receipt_id = _recovered_receipt_identity(parsed.get("receipt_id"))
    if receipt_id is None:
        return None
    canonical = None
    if set(details) == {DETAIL_KEY}:
        canonical = _canonical_receipt_body(parsed, details[DETAIL_KEY])
    return {
        "envelope_ok": _envelope_binds(event, canonical),
        "idempotency_key": _classified_idempotency_key(parsed, canonical),
        "receipt": canonical,
        "receipt_id": receipt_id,
    }


def _recovered_receipt_identity(value: object) -> str | None:
    """Recover an existing UUID identity from a stored blob, for matching only.

    Accepts the noncanonical spellings ``uuid.UUID`` already recognises so a
    same-ID sibling still taints its identity. The result never replaces a
    payload and never authorizes: strict input admission stays
    ``admit_receipt_id``, which keeps rejecting these spellings.
    """

    if type(value) is not str or not 1 <= len(value) <= MAX_RECEIPT_ID_TEXT:
        return None
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError, TypeError):
        return None
    if parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        return None
    return str(parsed)


def _parsed_receipt_blob(blob: object) -> dict[str, Any] | None:
    if type(blob) is not str or not 1 <= len(blob) <= MAX_RECEIPT_BLOB:
        return None
    try:
        parsed = json.loads(blob)
    except (TypeError, ValueError):
        return None
    return parsed if type(parsed) is dict else None


def _canonical_receipt_body(
    parsed: Mapping[str, Any], blob: str
) -> dict[str, Any] | None:
    try:
        receipt = validate_receipt(parsed)
    except (TypeError, ValueError, CaptureUnlinkReceiptError):
        return None
    if serialize_receipt(receipt) != blob.encode("utf-8"):
        return None
    return receipt


def _envelope_binds(
    event: Mapping[str, Any], canonical: Mapping[str, Any] | None
) -> bool:
    if canonical is None:
        return True
    return (
        event.get("capture_id") == canonical["capture_id"]
        and event.get("task_id") == canonical["task_id"]
    )


def _classified_idempotency_key(
    parsed: Mapping[str, Any], canonical: Mapping[str, Any] | None
) -> str | None:
    if canonical is not None:
        return str(canonical["idempotency_key"])
    raw_key = parsed.get("idempotency_key")
    return raw_key if type(raw_key) is str else None
