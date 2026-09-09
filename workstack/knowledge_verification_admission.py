"""Read-only admission of one stored Capture for a transient source check.

This module answers one question: may this owner process treat *this* stored
Capture revision as the currently valid subject of an explicit verification
request? It is a Store composition, not a remote boundary, not a subprocess
and not a lifecycle owner.

The caller holds an outer reentrant :meth:`~workstack.store.Store.transaction`
across this admission and later I/O, then **releases before any child**. A
snapshot that passed here never promises the policy or Capture will still
hold later. Nothing here saves, allocates a nonce, opens a network or reads
a wall clock. ``now`` is the caller's clock.

Evidence and connection identity come from the stored Capture and the
COMPLETED ledger record that named it. A missing historical record is
refused rather than inferred. The original request's expiry does not expire
the Capture; the observation window is a fresh sixty seconds.
"""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Mapping

from .capture import canonical_digest
from .knowledge_capture_packets import (
    CAPTURE_SCHEMA_VERSION,
    stored_retrieval_projection,
)
from .knowledge_ledger_document import (
    CAPTURE_ID_RE,
    KNOWLEDGE_DOCUMENT_NAME,
    MAX_CAPTURE_ID_CHARS,
    KnowledgeLedgerError,
    request_authority_is_current,
    validate_knowledge_document,
)
from .knowledge_request import canonical_uuid
from .knowledge_verification_protocol import validate_verification_request

__all__ = [
    "AdmittedVerification",
    "KnowledgeVerificationAdmissionError",
    "admit_capture_verification",
]

CAPTURES_DOCUMENT_NAME = "captures.json"
OBSERVATION_WINDOW_SECONDS = 60
MAX_CAPTURE_REVISION = 2**53 - 1
VERIFY_SCHEMA = "workstack.knowledge-verify.v1"


class KnowledgeVerificationAdmissionError(ValueError):
    """Closed admission refusal: a code and at most a field name, never a value."""

    def __init__(self, code: str, field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field = field

    @property
    def details(self) -> dict[str, str]:
        return {} if self.field is None else {"field": self.field}


@dataclass(frozen=True)
class AdmittedVerification:
    """Validated protocol document and the Capture-binding digest to retain."""

    document: dict[str, Any] = field(repr=False)
    capture_digest: str


def _refuse(code: str, field: str | None = None) -> KnowledgeVerificationAdmissionError:
    return KnowledgeVerificationAdmissionError(code, field)


def _require_capture_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_CAPTURE_ID_CHARS
        or not CAPTURE_ID_RE.fullmatch(value)
    ):
        raise _refuse("invalid_capture_id", "capture_id")
    return value


def _require_revision(value: Any) -> int:
    if type(value) is not int:
        raise _refuse("invalid_number", "capture_revision")
    if not 0 <= value <= MAX_CAPTURE_REVISION:
        raise _refuse("out_of_range", "capture_revision")
    return value


def _capture_record(document: Mapping[str, Any], capture_id: str) -> dict[str, Any]:
    captures = document.get("captures")
    if not isinstance(captures, list):
        raise _refuse("unknown_capture", "capture_id")
    for entry in captures:
        if isinstance(entry, dict) and entry.get("id") == capture_id:
            return entry
    raise _refuse("unknown_capture", "capture_id")


def _require_capture(
    document: Mapping[str, Any], capture_id: Any, capture_revision: Any
) -> dict[str, Any]:
    wanted_id = _require_capture_id(capture_id)
    wanted_revision = _require_revision(capture_revision)
    capture = _capture_record(document, wanted_id)
    if capture.get("revision") != wanted_revision:
        raise _refuse("capture_revision_mismatch", "capture_revision")
    if capture.get("schema_version") != CAPTURE_SCHEMA_VERSION:
        raise _refuse("unsupported_schema", "schema_version")
    return capture


def _completed_record(
    ledger: Mapping[str, Any], request_id: str, capture_id: str
) -> dict[str, Any]:
    record = None
    for entry in ledger["requests"]:
        if entry["request_id"] == request_id:
            record = entry
            break
    if record is None:
        raise KnowledgeLedgerError("unknown_request", "request_id")
    if record["state"] != "completed":
        raise KnowledgeLedgerError("request_not_completed", "state")
    if capture_id not in record["capture_ids"]:
        raise KnowledgeLedgerError("capture_not_bound", "capture_ids")
    return record


def _require_source_binding(capture: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    source = capture.get("source")
    if not isinstance(source, dict):
        raise _refuse("capture_binding_mismatch", "source")
    if source.get("connection_ref") != record["connection_alias"]:
        raise _refuse("capture_binding_mismatch", "source")
    if source.get("container_ref") != record["request_id"]:
        raise _refuse("capture_binding_mismatch", "source")


def _connection(ledger: Mapping[str, Any], alias: str) -> dict[str, Any]:
    for entry in ledger["connections"]:
        if entry["alias"] == alias:
            return entry
    raise KnowledgeLedgerError("connection_not_found", "connection_alias")


def _evidence(projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "document_ref": item["document_ref"],
            "source_type": item["reported_source_type"],
            "expected_source_version": item["reported_source_version"],
        }
        for item in projection["evidence"]
    ]


def _observation_window(now: str) -> tuple[str, str]:
    if not isinstance(now, str):
        raise _refuse("invalid_timestamp", "now")
    text = now[:-1] + "+00:00" if now.endswith("Z") else now
    try:
        started = dt.datetime.fromisoformat(text)
    except ValueError as error:
        raise _refuse("invalid_timestamp", "now") from error
    if started.tzinfo is None:
        raise _refuse("invalid_timestamp", "now")
    ended = started.astimezone(dt.timezone.utc) + dt.timedelta(
        seconds=OBSERVATION_WINDOW_SECONDS
    )
    return now, ended.isoformat().replace("+00:00", "Z")


def _protocol_document(
    *,
    workspace_uid: str,
    capture: Mapping[str, Any],
    record: Mapping[str, Any],
    connection: Mapping[str, Any],
    evidence: list[dict[str, Any]],
    verification_id: Any,
    now: str,
) -> dict[str, Any]:
    requested_at, expires_at = _observation_window(now)
    return {
        "schema": VERIFY_SCHEMA,
        "verification_id": canonical_uuid(verification_id, "verification_id"),
        "binding": {
            "workspace_uid": workspace_uid,
            "capture_id": capture["id"],
            "capture_revision": capture["revision"],
        },
        "connection": {
            "alias": record["connection_alias"],
            "upstream_workspace_uid": connection["upstream_workspace_uid"],
            "policy_revision": record["policy_revision"],
        },
        "corpus_refs": list(record["corpus_refs"]),
        "evidence": copy.deepcopy(evidence),
        "requested_at": requested_at,
        "expires_at": expires_at,
    }


def _capture_digest(
    *,
    workspace_uid: str,
    capture: Mapping[str, Any],
    record: Mapping[str, Any],
    connection: Mapping[str, Any],
    evidence: list[dict[str, Any]],
) -> str:
    return canonical_digest(
        {
            "workspace_uid": workspace_uid,
            "capture_id": capture["id"],
            "capture_revision": capture["revision"],
            "request_id": record["request_id"],
            "request_digest": record["request_digest"],
            "completion_digest": record["completion_digest"],
            "completed_at": record["completed_at"],
            "capture_ids": list(record["capture_ids"]),
            "connection_alias": record["connection_alias"],
            "policy_revision": record["policy_revision"],
            "upstream_workspace_uid": connection["upstream_workspace_uid"],
            "corpus_refs": list(record["corpus_refs"]),
            "evidence": evidence,
        }
    )


def admit_capture_verification(
    store: Any,
    capture_id: Any,
    capture_revision: Any,
    *,
    verification_id: Any,
    now: str,
) -> AdmittedVerification:
    """Validate one stored Capture against the live ledger, or refuse.

    Reads the actual workspace, ``knowledge.json`` and ``captures.json`` under
    the Store transaction. Authority comes from the COMPLETED ledger record
    that lists this Capture, never from a caller-constructed claim. The
    protocol document is built here and judged by protocol A; this function
    does not execute a verifier.
    """

    with store.transaction():
        workspace_uid = store.load("workspace.json")["id"]
        ledger = store.load(KNOWLEDGE_DOCUMENT_NAME)
        validate_knowledge_document(ledger, workspace_uid=workspace_uid)
        capture = _require_capture(
            store.load(CAPTURES_DOCUMENT_NAME), capture_id, capture_revision
        )
        projection = stored_retrieval_projection(capture.get("retrieval"))
        record = _completed_record(ledger, projection["request_id"], capture["id"])
        if not request_authority_is_current(ledger, record):
            raise KnowledgeLedgerError("policy_revision_changed", "policy_revision")
        _require_source_binding(capture, record)
        connection = _connection(ledger, record["connection_alias"])
        evidence = _evidence(projection)
        validated = validate_verification_request(
            _protocol_document(
                workspace_uid=workspace_uid,
                capture=capture,
                record=record,
                connection=connection,
                evidence=evidence,
                verification_id=verification_id,
                now=now,
            ),
            now=now,
        )
        return AdmittedVerification(
            document=copy.deepcopy(validated),
            capture_digest=_capture_digest(
                workspace_uid=workspace_uid,
                capture=capture,
                record=record,
                connection=connection,
                evidence=evidence,
            ),
        )
