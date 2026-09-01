"""Bounded runtime idempotency ledger operations for the v4 repository."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from typing import Any, Mapping

from .canonical import canonical_json_bytes
from .contracts import StorageContractError, require_valid_by_format
from .journal import JournalTarget
from .migration_idempotency import MAX_LEDGER_RECORDS, RETENTION_DAYS


MAX_LEDGER_BYTES = 16 * 1024 * 1024


class IdempotencyLedgerError(ValueError):
    """Content-free refusal to trust or mutate replay metadata."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _DuplicateKeyError(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKeyError
        value[key] = item
    return value


def _instant(value: object, code: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IdempotencyLedgerError(code)
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise IdempotencyLedgerError(code) from error
    if parsed.utcoffset() != dt.timedelta(0):
        raise IdempotencyLedgerError(code)
    return parsed


def _validate_temporal_policy(value: Mapping[str, Any]) -> None:
    retention = value["compaction_policy"]["retention_days"]
    for record in value["records"]:
        created = _instant(record["created_at"], "RECORD_CREATED_AT_INVALID")
        expires = _instant(record["expires_at"], "RECORD_EXPIRES_AT_INVALID")
        if expires != created + dt.timedelta(days=retention):
            raise IdempotencyLedgerError("RECORD_EXPIRY_POLICY_MISMATCH")


def _validate_ledger(value: object, expected_workspace_uid: str | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IdempotencyLedgerError("LEDGER_OBJECT_REQUIRED")
    try:
        require_valid_by_format(value)
    except StorageContractError as error:
        raise IdempotencyLedgerError("LEDGER_CONTRACT_INVALID") from error
    if expected_workspace_uid is not None and value["workspace_uid"] != expected_workspace_uid:
        raise IdempotencyLedgerError("LEDGER_WORKSPACE_MISMATCH")
    keys = [record["key"] for record in value["records"]]
    if len(keys) != len(set(keys)):
        raise IdempotencyLedgerError("LEDGER_KEY_DUPLICATE")
    if value["compaction_policy"]["max_records"] < len(keys):
        raise IdempotencyLedgerError("LEDGER_RECORD_LIMIT_EXCEEDED")
    _validate_temporal_policy(value)
    return value


def parse_idempotency_ledger(
    body: bytes, *, expected_workspace_uid: str | None = None
) -> dict[str, Any]:
    if not isinstance(body, bytes) or len(body) > MAX_LEDGER_BYTES:
        raise IdempotencyLedgerError("LEDGER_BYTE_LIMIT_EXCEEDED")
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        _DuplicateKeyError,
    ) as error:
        raise IdempotencyLedgerError("LEDGER_JSON_INVALID") from error
    validated = _validate_ledger(value, expected_workspace_uid)
    if canonical_json_bytes(validated) != body:
        raise IdempotencyLedgerError("LEDGER_CANONICAL_BYTES_REQUIRED")
    return copy.deepcopy(validated)


def new_idempotency_ledger(workspace_uid: str, *, updated_at: str) -> dict[str, Any]:
    value = {
        "format": "workstack.idempotency-ledger",
        "schema_version": 1,
        "workspace_uid": workspace_uid,
        "updated_at": updated_at,
        "compaction_policy": {
            "retention_days": RETENTION_DAYS,
            "max_records": MAX_LEDGER_RECORDS,
        },
        "records": [],
    }
    return copy.deepcopy(_validate_ledger(value, workspace_uid))


def compact_idempotency_ledger(
    ledger: Mapping[str, Any], *, now: str
) -> dict[str, Any]:
    value = _validate_ledger(copy.deepcopy(dict(ledger)), None)
    instant = _instant(now, "COMPACTION_INSTANT_INVALID")
    retained = [
        record
        for record in value["records"]
        if _instant(record["expires_at"], "RECORD_EXPIRES_AT_INVALID") > instant
    ]
    if len(retained) != len(value["records"]):
        value["records"] = retained
        value["updated_at"] = now
    return copy.deepcopy(_validate_ledger(value, value["workspace_uid"]))


def append_idempotency_record(
    ledger: Mapping[str, Any], record: Mapping[str, Any], *, now: str
) -> tuple[dict[str, Any], bool]:
    """Append one unexpired record, or report an exact duplicate operation."""

    value = compact_idempotency_ledger(ledger, now=now)
    candidate = copy.deepcopy(dict(record))
    probe = copy.deepcopy(value)
    probe["records"] = [candidate]
    _validate_ledger(probe, value["workspace_uid"])
    for existing in value["records"]:
        if existing["key"] != candidate["key"]:
            continue
        if existing != candidate:
            raise IdempotencyLedgerError("IDEMPOTENCY_KEY_CONFLICT")
        return value, True
    if _instant(candidate["expires_at"], "RECORD_EXPIRES_AT_INVALID") <= _instant(
        now, "APPEND_INSTANT_INVALID"
    ):
        raise IdempotencyLedgerError("EXPIRED_RECORD_NOT_APPENDABLE")
    maximum = value["compaction_policy"]["max_records"]
    if len(value["records"]) >= maximum:
        raise IdempotencyLedgerError("LEDGER_RECORD_LIMIT_EXCEEDED")
    value["records"].append(candidate)
    value["records"].sort(key=lambda item: (item["created_at"], item["key"]))
    value["updated_at"] = now
    return copy.deepcopy(_validate_ledger(value, value["workspace_uid"])), False


def stage_idempotency_ledger(
    ledger: Mapping[str, Any], *, current_body: bytes | None
) -> JournalTarget:
    value = _validate_ledger(copy.deepcopy(dict(ledger)), None)
    body = canonical_json_bytes(value)
    expected = None
    if current_body is not None:
        parse_idempotency_ledger(
            current_body, expected_workspace_uid=value["workspace_uid"]
        )
        expected = "sha256:" + hashlib.sha256(current_body).hexdigest()
    return JournalTarget.replace(
        "idempotency-ledger.v1.json",
        body,
        expected_digest=expected,
        scope="runtime",
    )
