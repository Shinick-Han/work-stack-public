"""Pure stored model for one historical source-check observation per Capture.

This module is a closed document/list planner. It does not open a Store, read a
wall clock, reach a network, or persist a verifier command, environment or
token. Later slices own migration I/O and HTTP.

A stored row is a past accepted exchange, validated at its own ``accepted_at``.
Historical validation never uses today's clock, never renews a window, never
executes a request, and never promotes ``verified_*`` retrieval fields.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from .capture import SHA256_RE, parse_rfc3339
from .knowledge_request import KnowledgeRequestError, canonical_uuid
from .knowledge_verification_protocol import (
    VerificationError,
    validate_verification_request,
    validate_verification_result,
)

__all__ = (
    "MAX_OBSERVATION_BYTES",
    "MAX_OBSERVATIONS",
    "MAX_OBSERVATIONS_BYTES",
    "OBSERVATION_SCHEMA",
    "CaptureObservationError",
    "plan_capture_observations_upgrade",
    "plan_observation_upsert",
    "validate_observation",
    "validate_observations",
)

OBSERVATION_SCHEMA = "workstack.capture-observation.v1"
MAX_OBSERVATIONS = 128
MAX_OBSERVATION_BYTES = 40 * 1024
MAX_OBSERVATIONS_BYTES = 1024 * 1024

_RECORD_FIELDS = frozenset(
    {
        "schema",
        "capture_digest",
        "verifier_alias",
        "accepted_at",
        "request",
        "result",
    }
)
_V1_CONTAINER_FIELDS = frozenset({"version", "captures"})


class CaptureObservationError(ValueError):
    """Content-free model refusal: one closed code, and no submitted value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _refuse(code: str) -> CaptureObservationError:
    return CaptureObservationError(code)


def _compact(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _closed(value: Any, allowed: frozenset[str], invalid: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _refuse(invalid)
    keys = set(value)
    if keys - allowed:
        raise _refuse("unknown_field")
    if allowed - keys:
        raise _refuse("missing_field")
    return value


def _workspace(value: Any) -> str:
    try:
        return canonical_uuid(value, "workspace_uid")
    except KnowledgeRequestError as error:
        raise _refuse(error.code) from error


def _capture_id(record: dict[str, Any]) -> str:
    return str(record["request"]["binding"]["capture_id"])


def _verification_id(record: dict[str, Any]) -> str:
    return str(record["request"]["verification_id"])


def _accepted_instant(record: dict[str, Any]) -> Any:
    return parse_rfc3339(record["accepted_at"], "accepted_at")


def _protocol_pair(
    request: Any, result: Any, *, accepted_at: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        admitted = validate_verification_request(request, now=accepted_at)
        observed = validate_verification_result(
            result, request=admitted, now=accepted_at
        )
    except VerificationError as error:
        raise _refuse(error.code) from error
    return admitted, observed


def validate_observation(record: Any, *, workspace_uid: Any) -> dict[str, Any]:
    """Return one normalized historical observation, or refuse.

    ``accepted_at`` is the owner's post-I/O clock that originally admitted the
    result. It is passed through to the existing verification validators as
    ``now``; this function does not read a wall clock.
    """

    workspace = _workspace(workspace_uid)
    root = _closed(record, _RECORD_FIELDS, "invalid_observation")
    if root["schema"] != OBSERVATION_SCHEMA:
        raise _refuse("unsupported_observation_schema")
    digest = root["capture_digest"]
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise _refuse("invalid_capture_digest")
    accepted_at = root["accepted_at"]
    admitted, observed = _protocol_pair(
        root["request"], root["result"], accepted_at=accepted_at
    )
    alias = admitted["connection"]["alias"]
    if root["verifier_alias"] != alias:
        raise _refuse("verifier_alias_mismatch")
    if admitted["binding"]["workspace_uid"] != workspace:
        raise _refuse("workspace_mismatch")
    normalized = {
        "schema": OBSERVATION_SCHEMA,
        "capture_digest": digest,
        "verifier_alias": alias,
        "accepted_at": accepted_at,
        "request": admitted,
        "result": observed,
    }
    if len(_compact(normalized)) > MAX_OBSERVATION_BYTES:
        raise _refuse("observation_too_large")
    return normalized


def validate_observations(records: Any, *, workspace_uid: Any) -> list[dict[str, Any]]:
    """Return the canonical observation list, or refuse."""

    workspace = _workspace(workspace_uid)
    if not isinstance(records, list):
        raise _refuse("invalid_observations")
    if len(records) > MAX_OBSERVATIONS:
        raise _refuse("observations_too_many")
    normalized = [
        validate_observation(item, workspace_uid=workspace) for item in records
    ]
    capture_ids = [_capture_id(item) for item in normalized]
    if len(set(capture_ids)) != len(capture_ids):
        raise _refuse("duplicate_capture_id")
    nonces = [_verification_id(item) for item in normalized]
    if len(set(nonces)) != len(nonces):
        raise _refuse("observation_conflict")
    normalized.sort(key=_capture_id)
    if len(_compact(normalized)) > MAX_OBSERVATIONS_BYTES:
        raise _refuse("observations_too_large")
    return normalized


def _evict(rows: list[dict[str, Any]], retained: dict[str, Any]) -> list[dict[str, Any]]:
    """Drop other oldest rows until both list caps hold; keep ``retained``."""

    while (
        len(rows) > MAX_OBSERVATIONS
        or len(_compact(rows)) > MAX_OBSERVATIONS_BYTES
    ):
        others = [item for item in rows if item is not retained]
        if not others:
            raise _refuse("observations_too_large")
        victim = min(
            others,
            key=lambda item: (
                _accepted_instant(item),
                _verification_id(item),
                _capture_id(item),
            ),
        )
        rows.remove(victim)
    rows.sort(key=_capture_id)
    return rows


def plan_observation_upsert(
    records: Any, record: Any, *, workspace_uid: Any
) -> list[dict[str, Any]]:
    """Return the list after inserting or replacing one observation.

    Inputs are not mutated. A same-capture newer ``accepted_at`` replaces; an
    older one refuses. Same ``verification_id`` with an identical normalized
    record is idempotent; any other reuse of that id refuses.
    """

    workspace = _workspace(workspace_uid)
    existing = validate_observations(
        copy.deepcopy(records) if isinstance(records, list) else records,
        workspace_uid=workspace,
    )
    incoming = validate_observation(copy.deepcopy(record), workspace_uid=workspace)
    nonce = _verification_id(incoming)
    for item in existing:
        if _verification_id(item) != nonce:
            continue
        if item == incoming:
            return existing
        raise _refuse("observation_conflict")
    capture_id = _capture_id(incoming)
    prior = next((item for item in existing if _capture_id(item) == capture_id), None)
    if prior is not None and _accepted_instant(incoming) < _accepted_instant(prior):
        raise _refuse("stale_observation")
    rows = [item for item in existing if _capture_id(item) != capture_id]
    rows.append(incoming)
    return _evict(rows, incoming)


def plan_capture_observations_upgrade(document: Any) -> dict[str, Any]:
    """Plan container v1 -> v2 with an empty observations list.

    Version 2 is not migrated again. Captures must already be a list; this
    planner copies that list as a JSON value and does not validate Capture
    records, open a Store, or write a backup.
    """

    if not isinstance(document, dict) or any(
        not isinstance(key, str) for key in document
    ):
        raise _refuse("invalid_container")
    version = document.get("version")
    if type(version) is not int:
        raise _refuse("invalid_container")
    if version == 2:
        raise _refuse("unsupported_container_version")
    if version != 1:
        raise _refuse("unsupported_container_version")
    root = _closed(document, _V1_CONTAINER_FIELDS, "invalid_container")
    captures = root["captures"]
    if type(captures) is not list:
        raise _refuse("invalid_container")
    captures = copy.deepcopy(captures)
    return {"version": 2, "captures": captures, "observations": []}
