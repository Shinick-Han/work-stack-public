"""Pure v3-to-v4 conversion for the bounded runtime idempotency ledger.

The ledger is disposable runtime state, not part of the canonical v4 authority.
Migration nevertheless preserves still-replayable v3 operations so activating a
verified candidate does not make an ambiguous request safe to execute twice.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .contracts import StorageContractError, require_valid_by_format


RETENTION_DAYS = 30
MAX_LEDGER_RECORDS = 10_000
MAX_REPLY_ROSTER = 100_000
_BASE_FIELDS = frozenset(
    {
        "key",
        "method",
        "path",
        "request_digest",
        "response_status",
        "created_at",
    }
)


class IdempotencyLedgerConversionError(ValueError):
    """A content-free refusal to convert unsafe legacy replay state."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _reply_index(replies: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    if (
        not isinstance(replies, Sequence)
        or isinstance(replies, (str, bytes, bytearray))
        or len(replies) > MAX_REPLY_ROSTER
    ):
        raise IdempotencyLedgerConversionError("REPLY_ROSTER_INVALID")
    by_display_id: dict[str, str] = {}
    seen_uids: set[str] = set()
    try:
        for reply in replies:
            require_valid_by_format(reply)
            display_id = reply["display_id"]
            uid = reply["uid"]
            if display_id in by_display_id:
                raise IdempotencyLedgerConversionError("REPLY_DISPLAY_ID_DUPLICATE")
            if uid in seen_uids:
                raise IdempotencyLedgerConversionError("REPLY_UID_DUPLICATE")
            by_display_id[display_id] = uid
            seen_uids.add(uid)
    except IdempotencyLedgerConversionError:
        raise
    except (KeyError, TypeError, StorageContractError, ValueError) as error:
        raise IdempotencyLedgerConversionError("REPLY_ROSTER_INVALID") from error
    return by_display_id


def _expires_at(created_at: object) -> str:
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise IdempotencyLedgerConversionError("IDEMPOTENCY_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(created_at[:-1] + "+00:00")
    except ValueError as error:
        raise IdempotencyLedgerConversionError("IDEMPOTENCY_TIMESTAMP_INVALID") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise IdempotencyLedgerConversionError("IDEMPOTENCY_TIMESTAMP_INVALID")
    try:
        expiry = parsed.astimezone(timezone.utc) + timedelta(days=RETENTION_DAYS)
    except OverflowError as error:
        raise IdempotencyLedgerConversionError("IDEMPOTENCY_TIMESTAMP_INVALID") from error
    return expiry.isoformat(timespec="auto").replace("+00:00", "Z")


def _response_fields(
    source: Mapping[str, Any], reply_uids: Mapping[str, str]
) -> dict[str, Any]:
    has_body = "response_body" in source
    has_ref = "response_ref" in source
    if has_body == has_ref:
        raise IdempotencyLedgerConversionError("IDEMPOTENCY_RESPONSE_FORM_INVALID")
    if has_body:
        if set(source) != _BASE_FIELDS | {"response_body"}:
            raise IdempotencyLedgerConversionError("IDEMPOTENCY_RECORD_FIELDS_INVALID")
        return {"response_body": copy.deepcopy(source["response_body"])}

    allowed = _BASE_FIELDS | {"response_ref", "response_meta"}
    if not _BASE_FIELDS | {"response_ref"} <= set(source) or not set(source) <= allowed:
        raise IdempotencyLedgerConversionError("IDEMPOTENCY_RECORD_FIELDS_INVALID")
    reference = source["response_ref"]
    if (
        not isinstance(reference, Mapping)
        or set(reference) != {"kind", "id"}
        or reference.get("kind") != "reply"
        or not isinstance(reference.get("id"), str)
    ):
        raise IdempotencyLedgerConversionError("IDEMPOTENCY_RESPONSE_REFERENCE_INVALID")
    try:
        record_uid = reply_uids[reference["id"]]
    except KeyError as error:
        raise IdempotencyLedgerConversionError("REPLY_REFERENCE_UNRESOLVED") from error
    result: dict[str, Any] = {
        "response_ref": {"kind": "reply", "record_uid": record_uid}
    }
    if "response_meta" in source:
        result["response_meta"] = copy.deepcopy(source["response_meta"])
    return result


def _convert_record(
    source: Mapping[str, Any], reply_uids: Mapping[str, str]
) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        raise IdempotencyLedgerConversionError("IDEMPOTENCY_RECORD_INVALID")
    missing = _BASE_FIELDS - set(source)
    if missing:
        raise IdempotencyLedgerConversionError("IDEMPOTENCY_RECORD_FIELDS_INVALID")
    created_at = source["created_at"]
    result = {field: copy.deepcopy(source[field]) for field in sorted(_BASE_FIELDS)}
    result["expires_at"] = _expires_at(created_at)
    result.update(_response_fields(source, reply_uids))
    return result


def convert_v3_idempotency_ledger(
    records: Sequence[Mapping[str, Any]],
    *,
    workspace_uid: str,
    updated_at: str,
    replies: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Return one deterministic, schema-valid v4 runtime ledger.

    ``replies`` contains already converted ``workstack.reply`` records. Legacy
    response references use display IDs and are resolved to their stable v4
    record UIDs here. No input value is mutated or retained by reference.
    """

    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        raise IdempotencyLedgerConversionError("IDEMPOTENCY_RECORDS_INVALID")
    if len(records) > MAX_LEDGER_RECORDS:
        raise IdempotencyLedgerConversionError("IDEMPOTENCY_RECORD_LIMIT_EXCEEDED")
    reply_uids = _reply_index(replies)
    converted: list[dict[str, Any]] = []
    keys: set[str] = set()
    try:
        for source in records:
            record = _convert_record(source, reply_uids)
            key = record["key"]
            if key in keys:
                raise IdempotencyLedgerConversionError("IDEMPOTENCY_KEY_DUPLICATE")
            keys.add(key)
            converted.append(record)
        converted.sort(key=lambda item: (item["created_at"], item["key"]))
        ledger = {
            "format": "workstack.idempotency-ledger",
            "schema_version": 1,
            "workspace_uid": workspace_uid,
            "updated_at": updated_at,
            "compaction_policy": {
                "retention_days": RETENTION_DAYS,
                "max_records": MAX_LEDGER_RECORDS,
            },
            "records": converted,
        }
        require_valid_by_format(ledger)
        return copy.deepcopy(ledger)
    except IdempotencyLedgerConversionError:
        raise
    except (KeyError, TypeError, StorageContractError, ValueError) as error:
        raise IdempotencyLedgerConversionError(
            "IDEMPOTENCY_RECORD_UNREPRESENTABLE"
        ) from error
