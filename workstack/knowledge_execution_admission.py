"""Read-only admission of one already-issued KnowledgeRequest for execution.

This module answers one question: may this owner process treat *this*
document as the currently valid authorization for an explicitly reviewed
request the ledger already holds? It is a Store composition, not a remote
boundary, not a token issuer and not an executor.

The caller holds an outer reentrant :meth:`~workstack.store.Store.transaction`
across this admission and a later instance-guard consume, then **releases
before any child I/O**. After the child returns, the caller admits again
before returning a proposal. A snapshot that passed here never promises the
policy, Task or window will still hold later.

Nothing here saves, issues, claims, guards, opens a network or reads a wall
clock. ``now`` is the caller's clock. No token is returned. An unissued
``request_id`` is ``unknown_request``: :func:`plan_request_issue` is never
called, because that planner would record a new authorization.
"""

from __future__ import annotations

import copy
import secrets
from dataclasses import dataclass, field
from typing import Any

from .knowledge_ledger_document import (
    KNOWLEDGE_DOCUMENT_NAME,
    KnowledgeLedgerError,
    request_authority_is_current,
    validate_knowledge_document,
)
from .knowledge_owner_requests import (
    _active_task_from_store,
    owner_request_authority,
    request_digest,
)
from .knowledge_request import KnowledgeRequestError, canonical_uuid, validate_knowledge_request

__all__ = ["AdmittedExecution", "admit_execution_request"]


@dataclass(frozen=True)
class AdmittedExecution:
    """Projection and roster facts for one pending, currently authorised request."""

    document: dict[str, Any] = field(repr=False)
    request_digest: str
    connection_alias: str
    policy_revision: int
    upstream_workspace_uid: str


def _shape(request_document: Any) -> dict[str, Any]:
    """Admit a mapping so ``request_id`` can be read; the wire validator is closed."""

    if not isinstance(request_document, dict) or any(
        not isinstance(key, str) for key in request_document
    ):
        raise KnowledgeRequestError("invalid_request", "request")
    return request_document


def _record(ledger: dict[str, Any], request_id: str) -> dict[str, Any]:
    for entry in ledger["requests"]:
        if entry["request_id"] == request_id:
            return entry
    raise KnowledgeLedgerError("unknown_request", "request_id")


def _task_id(binding: dict[str, Any]) -> str | None:
    return str(binding["task_id"]) if "task_id" in binding else None


def _roster_connection(ledger: dict[str, Any], alias: str) -> dict[str, Any]:
    for entry in ledger["connections"]:
        if entry["alias"] == alias:
            return entry
    raise KnowledgeLedgerError("policy_revision_changed", "policy_revision")


def admit_execution_request(
    store: Any, request_document: Any, *, now: str
) -> AdmittedExecution:
    """Validate an issued request against the live ledger, or refuse.

    Reads the actual workspace and ``knowledge.json`` under the Store
    transaction. Task authority comes from the *stored* binding's ``task_id``
    and the backlog, never from a caller-constructed Task claim. Wire grammar,
    unknown keys, expiry and binding mismatches propagate from
    :func:`validate_knowledge_request` unchanged.
    """

    with store.transaction():
        shaped = _shape(request_document)
        request_id = canonical_uuid(shaped.get("request_id"), "request_id")
        workspace_uid = store.load("workspace.json")["id"]
        ledger = store.load(KNOWLEDGE_DOCUMENT_NAME)
        validate_knowledge_document(ledger, workspace_uid=workspace_uid)
        record = _record(ledger, request_id)
        if record["state"] != "pending":
            raise KnowledgeLedgerError("request_not_pending", "state")
        if not request_authority_is_current(ledger, record):
            raise KnowledgeLedgerError("policy_revision_changed", "policy_revision")
        active_task = _active_task_from_store(store, _task_id(record["binding"]))
        authority = owner_request_authority(
            ledger,
            workspace_uid=workspace_uid,
            connection_alias=record["connection_alias"],
            now=now,
            active_task=active_task,
        )
        projection = validate_knowledge_request(shaped, authority)
        digest = request_digest(projection)
        if not secrets.compare_digest(record["request_digest"], digest):
            raise KnowledgeLedgerError("request_digest_mismatch", "request_id")
        connection = _roster_connection(ledger, record["connection_alias"])
        return AdmittedExecution(
            document=copy.deepcopy(projection),
            request_digest=record["request_digest"],
            connection_alias=record["connection_alias"],
            policy_revision=record["policy_revision"],
            upstream_workspace_uid=connection["upstream_workspace_uid"],
        )
