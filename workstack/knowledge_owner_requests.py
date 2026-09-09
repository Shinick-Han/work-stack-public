"""Owner-held issue and stage-completion for the durable knowledge ledger.

This is the foundation the following API and Capture-import lanes stand on. It
answers three questions and no others:

1. Which upstream connections has this workspace's *owner* approved, and at
   which policy revision (:func:`plan_policy_revision`).
2. Which KnowledgeRequests has that owner actually issued against them, and
   what does a reissue of one mean (:func:`plan_request_issue`).
3. When the Capture importer has finished importing one request's whole batch,
   how does that request stop being outstanding
   (:func:`plan_ledger_stage_completion`).

Nothing here is a remote boundary. Every entry point takes *trusted owner
inputs* — a workspace identity the Store already proved, a Task the Store
actually holds, a clock the caller supplies — and none of them may be handed a
caller-constructed authority arriving over HTTP. A public issuer would derive
its authority from the stored policy plus the real Store workspace and Task,
exactly the way :func:`owner_request_authority` does here; it would not pass an
authority through.

Two separations are load-bearing:

**Planning is pure; only the caller writes.** Every ``plan_*`` function takes
the document the caller read and returns the document the caller should write.
None of them opens a path, reads a clock, allocates an identity or saves
anything. That is what lets a Capture importer commit a ledger completion in
the *same* :meth:`workstack.store.Store.save_many` as the captures and the
activity it belongs to. There is deliberately no standalone "consume this
request" operation that durably spends a request before its batch lands: a
completion that commits separately from the evidence it completes is exactly
the split this module exists to prevent.

**The wire validator is reused, not re-implemented.**
:func:`workstack.knowledge_request.validate_knowledge_request` decides whether
a KnowledgeRequest document is well formed and inside its authority; it is
untouched, and its :class:`~workstack.knowledge_request.KnowledgeRequestError`
propagates from :func:`plan_request_issue` unchanged. This module adds only
what a ledger adds: identity, replay, policy binding and completion.

Only a digest of an issued request is persisted. The query text, the request
body and any receipt payload stay out of ``knowledge.json`` and out of every
refusal raised here.
"""

from __future__ import annotations

import copy
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .capture import SHA256_RE
from .knowledge_ledger_document import (
    CONNECTION_SCOPE,
    KNOWLEDGE_DOCUMENT_NAME,
    MAX_CONNECTIONS,
    MAX_POLICY_REVISION,
    MAX_REQUESTS,
    KnowledgeLedgerError,
    compact_bytes,
    request_authority_is_current,
    validate_knowledge_document,
)
from .knowledge_request import (
    ActiveTask,
    RequestAuthority,
    is_expired,
    validate_knowledge_request,
)

__all__ = [
    "ConnectionPolicy",
    "IssuedRequest",
    "KnowledgeLedgerError",
    "PendingRequest",
    "StageCompletion",
    "issue_owner_knowledge_request",
    "owner_pending_requests",
    "owner_request_authority",
    "plan_ledger_stage_completion",
    "plan_policy_revision",
    "plan_request_issue",
    "pending_requests",
    "request_digest",
    "set_owner_connection_policy",
]


@dataclass(frozen=True)
class ConnectionPolicy:
    """One approved upstream connection, as the owner stated it.

    ``alias`` names the connection; it is not an endpoint. ``corpus_refs`` are
    the nonsecret corpus aliases this connection grants, and they are the whole
    of what a request issued under it may name. There is no field for a URL, a
    credential, a query, a path or a collection authority, and there is no
    project subject: ``scope`` is workspace-wide by construction.
    """

    alias: str
    upstream_workspace_uid: str
    corpus_refs: tuple[str, ...]
    scope: str = CONNECTION_SCOPE


@dataclass(frozen=True)
class IssuedRequest:
    """What issuing — or reissuing — one request produced.

    ``document`` is the ledger to persist. On a replay it is the document that
    was handed in, unchanged, and ``replayed`` is ``True``: an identical
    reissue is the same authorization, not a second one.
    """

    document: dict[str, Any]
    replayed: bool
    request_id: str
    request_digest: str
    connection_alias: str
    policy_revision: int
    result_limit: int
    requested_at: str
    expires_at: str


@dataclass(frozen=True)
class PendingRequest:
    """An outstanding record, reconstructed from the document on disk."""

    request_id: str
    request_digest: str
    connection_alias: str
    policy_revision: int
    result_limit: int
    requested_at: str
    expires_at: str
    binding: dict[str, Any]
    authority_current: bool
    expired: bool


@dataclass(frozen=True)
class StageCompletion:
    """The ledger after one request's whole imported batch is accounted for."""

    document: dict[str, Any]
    replayed: bool
    request_id: str
    capture_ids: tuple[str, ...]
    completion_digest: str
    completed_at: str


def _refuse(code: str, field: str | None = None) -> KnowledgeLedgerError:
    return KnowledgeLedgerError(code, field)


def _admitted(document: Any, workspace_uid: str) -> dict[str, Any]:
    """Trusted input is still checked input, and never shared with the caller."""

    validate_knowledge_document(document, workspace_uid=workspace_uid)
    return copy.deepcopy(document)


def _record(document: Mapping[str, Any], request_id: str) -> dict[str, Any] | None:
    for entry in document["requests"]:
        if entry["request_id"] == request_id:
            return entry
    return None


def request_digest(request: Mapping[str, Any]) -> str:
    """The digest that identifies one exact issued KnowledgeRequest.

    Taken over the validator's own closed projection, so it covers the query,
    the binding, the corpus refs, the limit and the window without any of them
    being stored. Two issues of the same ``request_id`` are the same issue only
    when this value matches.
    """

    return "sha256:" + hashlib.sha256(compact_bytes(dict(request))).hexdigest()


def _connection(
    document: Mapping[str, Any], connection_alias: str
) -> dict[str, Any]:
    for entry in document["connections"]:
        if entry["alias"] == connection_alias:
            return entry
    raise _refuse("connection_not_found", "connection_alias")


def plan_policy_revision(
    document: Any,
    connections: Sequence[ConnectionPolicy],
    *,
    workspace_uid: str,
) -> dict[str, Any]:
    """Replace the connection roster and advance the policy revision by one.

    Every outstanding request keeps its record and keeps the revision it was
    issued under, which is precisely how its authority is invalidated: a
    completion planned after this returns refuses with ``policy_revision_``
    ``changed`` rather than spending a request the owner has since re-scoped.
    Deleting the records instead would erase the evidence that they were
    issued at all.
    """

    planned = _admitted(document, workspace_uid)
    if not isinstance(connections, (list, tuple)) or len(connections) > MAX_CONNECTIONS:
        raise _refuse("invalid_document", "connections")
    roster: list[dict[str, Any]] = []
    for entry in connections:
        if not isinstance(entry, ConnectionPolicy):
            raise _refuse("invalid_document", "connections")
        roster.append(
            {
                "alias": entry.alias,
                "upstream_workspace_uid": entry.upstream_workspace_uid,
                "corpus_refs": list(entry.corpus_refs),
                "scope": entry.scope,
            }
        )
    revision = planned["policy_revision"] + 1
    if revision > MAX_POLICY_REVISION:
        raise _refuse("out_of_range", "policy_revision")
    planned["connections"] = roster
    planned["policy_revision"] = revision
    validate_knowledge_document(planned, workspace_uid=workspace_uid)
    return planned


def owner_request_authority(
    document: Any,
    *,
    workspace_uid: str,
    connection_alias: str,
    now: str,
    active_task: ActiveTask | None = None,
) -> RequestAuthority:
    """Build the wire authority from stored policy and real Store state.

    Nothing in the request body reaches this. The granted corpus aliases come
    from the stored connection the owner approved, the workspace and the Task
    come from the Store, and the clock comes from the caller. A later public
    issuer derives its authority the same way; it never accepts one.
    """

    admitted = _admitted(document, workspace_uid)
    connection = _connection(admitted, connection_alias)
    return RequestAuthority(
        workspace_uid=workspace_uid,
        granted_corpus_refs=tuple(connection["corpus_refs"]),
        now=now,
        active_task=active_task,
    )


def plan_request_issue(
    document: Any,
    request_document: Any,
    *,
    workspace_uid: str,
    connection_alias: str,
    now: str,
    active_task: ActiveTask | None = None,
) -> IssuedRequest:
    """Record one validated KnowledgeRequest, or recognise it as already issued.

    ``request_document`` is judged by the released wire validator against the
    authority derived here; a ``KnowledgeRequestError`` from it propagates
    unchanged, so scope expansion, a stale Task revision, a foreign workspace
    and an expired window all refuse with the codes that contract already
    publishes.

    Only the digest of the resulting projection is stored. Reissuing the same
    ``request_id`` with byte-identical input returns the original record's
    result and leaves the document alone; reissuing it with anything else
    refuses, because one identifier may name exactly one authorization.
    """

    planned = _admitted(document, workspace_uid)
    authority = owner_request_authority(
        planned,
        workspace_uid=workspace_uid,
        connection_alias=connection_alias,
        now=now,
        active_task=active_task,
    )
    projection = validate_knowledge_request(request_document, authority)
    digest = request_digest(projection)
    request_id = projection["request_id"]
    existing = _record(planned, request_id)
    if existing is not None:
        if not secrets.compare_digest(existing["request_digest"], digest):
            raise _refuse("request_digest_mismatch", "request_id")
        if existing["connection_alias"] != connection_alias:
            raise _refuse("request_digest_mismatch", "connection_alias")
        return IssuedRequest(
            document=planned,
            replayed=True,
            request_id=request_id,
            request_digest=existing["request_digest"],
            connection_alias=existing["connection_alias"],
            policy_revision=existing["policy_revision"],
            result_limit=existing["result_limit"],
            requested_at=existing["requested_at"],
            expires_at=existing["expires_at"],
        )
    if len(planned["requests"]) >= MAX_REQUESTS:
        raise _refuse("ledger_full", "requests")
    record = {
        "request_id": request_id,
        "request_digest": digest,
        "connection_alias": connection_alias,
        "binding": copy.deepcopy(projection["binding"]),
        "corpus_refs": list(projection["corpus_refs"]),
        "policy_revision": planned["policy_revision"],
        "result_limit": projection["result_limit"],
        "requested_at": projection["requested_at"],
        "expires_at": projection["expires_at"],
        "state": "pending",
        "capture_ids": [],
        "completion_digest": None,
        "completed_at": None,
    }
    planned["requests"].append(record)
    validate_knowledge_document(planned, workspace_uid=workspace_uid)
    return IssuedRequest(
        document=planned,
        replayed=False,
        request_id=request_id,
        request_digest=digest,
        connection_alias=connection_alias,
        policy_revision=record["policy_revision"],
        result_limit=record["result_limit"],
        requested_at=record["requested_at"],
        expires_at=record["expires_at"],
    )


def _binding_still_matches(
    binding: Mapping[str, Any], workspace_uid: str, active_task: ActiveTask | None
) -> None:
    if binding["workspace_uid"] != workspace_uid:
        raise _refuse("workspace_mismatch", "binding.workspace_uid")
    has_task = "task_uid" in binding
    if has_task != (active_task is not None):
        # A request bound to a Task may not be completed with none open, and a
        # workspace-only request may not be completed against one.
        raise _refuse("task_binding_mismatch", "binding")
    if active_task is None:
        return
    if (
        binding["task_uid"] != active_task.task_uid
        or binding["task_id"] != active_task.task_id.upper()
        or binding["task_revision"] != active_task.task_revision
    ):
        # A moved Task is a different Task state, so the evidence this request
        # authorised is no longer the evidence being imported.
        raise _refuse("task_binding_mismatch", "binding")


def plan_ledger_stage_completion(
    document: Any,
    *,
    request_id: str,
    completion_digest: str,
    capture_ids: Sequence[str],
    workspace_uid: str,
    now: str,
    active_task: ActiveTask | None = None,
) -> StageCompletion:
    """INTERNAL: retire one request once its whole imported batch is accounted for.

    This is the operation the following Capture importer composes into its own
    Store transaction, after every Capture in the batch has been staged and in
    the same :meth:`~workstack.store.Store.save_many` that persists them. It is
    called once per request, never once per Capture, and it never writes: an
    importer that fails after this returns simply never saves, and the request
    is still pending on the next attempt.

    It is not an operational import route and must not be exposed as one. A
    standalone externally callable consume would spend a request without the
    evidence that justifies spending it.

    Replay is decided *before* anything else, so an importer retried after the
    request's window has closed still recognises its own completed work.
    Identical ``completion_digest`` and ``capture_ids`` return the recorded
    result; a different digest for the same request refuses. Only a *new*
    completion is held to the policy actually in force — the current revision,
    and a connection roster that still grants the corpora the record names —
    plus the current workspace, the Task revision the request was bound to, and
    the request's expiry.
    """

    planned = _admitted(document, workspace_uid)
    if not isinstance(request_id, str):
        raise _refuse("invalid_uuid", "request_id")
    if not isinstance(completion_digest, str) or not SHA256_RE.fullmatch(
        completion_digest
    ):
        # Checked before any comparison, so a malformed digest refuses as one
        # rather than as a mismatch or as a raw comparison error.
        raise _refuse("invalid_digest", "completion_digest")
    if isinstance(capture_ids, (str, bytes)) or not isinstance(
        capture_ids, (list, tuple)
    ):
        raise _refuse("invalid_capture_ids", "capture_ids")
    submitted = list(capture_ids)
    record = _record(planned, request_id)
    if record is None:
        raise _refuse("unknown_request", "request_id")
    if record["state"] == "completed":
        if not secrets.compare_digest(
            str(record["completion_digest"]), completion_digest
        ):
            raise _refuse("completion_digest_mismatch", "completion_digest")
        if list(record["capture_ids"]) != submitted:
            raise _refuse("completion_replay_mismatch", "capture_ids")
        return StageCompletion(
            document=planned,
            replayed=True,
            request_id=request_id,
            capture_ids=tuple(record["capture_ids"]),
            completion_digest=str(record["completion_digest"]),
            completed_at=str(record["completed_at"]),
        )
    if len(submitted) > record["result_limit"]:
        # The batch is wider than the limit the request was issued under.
        raise _refuse("result_limit_exceeded", "capture_ids")
    if not request_authority_is_current(planned, record):
        # The one predicate the durable validator also uses. A record issued
        # under an older revision is history, not authority, and a record whose
        # connection or grants no longer cover it never was: neither may be
        # spent by a *new* completion. A replay of work already completed was
        # answered above, before this.
        raise _refuse("policy_revision_changed", "policy_revision")
    _binding_still_matches(record["binding"], workspace_uid, active_task)
    if is_expired({"expires_at": record["expires_at"]}, now):
        # Expiry is terminal. A lapsed request is raised again and reviewed
        # again; it is never quietly completed.
        raise _refuse("request_expired", "expires_at")
    record["state"] = "completed"
    record["capture_ids"] = submitted
    record["completion_digest"] = completion_digest
    record["completed_at"] = now
    validate_knowledge_document(planned, workspace_uid=workspace_uid)
    return StageCompletion(
        document=planned,
        replayed=False,
        request_id=request_id,
        capture_ids=tuple(submitted),
        completion_digest=completion_digest,
        completed_at=now,
    )


def pending_requests(
    document: Any, *, workspace_uid: str, now: str | None = None
) -> tuple[PendingRequest, ...]:
    """Every outstanding request the stored ledger still holds.

    This is what a restart reconstructs from: the document on disk is the only
    state, so a process that died between issuing and importing comes back
    knowing exactly which requests are still open, at which policy revision and
    under which limit. ``authority_current`` is the same predicate a new
    completion is held to: the record's revision is the one in force *and* the
    current roster still grants what the record names. ``expired`` is reported
    only when the caller supplies its clock.
    """

    admitted = _admitted(document, workspace_uid)
    return tuple(
        PendingRequest(
            request_id=entry["request_id"],
            request_digest=entry["request_digest"],
            connection_alias=entry["connection_alias"],
            policy_revision=entry["policy_revision"],
            result_limit=entry["result_limit"],
            requested_at=entry["requested_at"],
            expires_at=entry["expires_at"],
            binding=copy.deepcopy(entry["binding"]),
            authority_current=request_authority_is_current(admitted, entry),
            expired=(
                False
                if now is None
                else is_expired({"expires_at": entry["expires_at"]}, now)
            ),
        )
        for entry in admitted["requests"]
        if entry["state"] == "pending"
    )


# --------------------------------------------------------------------------
# Store-bound owner operations.
#
# These are the trusted-owner callers of the planners above: they read the real
# workspace identity and the real Task from the Store under its own lease, plan
# against the document they just read, and save through the released
# transaction. They are internal product operations, not an HTTP surface.
# --------------------------------------------------------------------------


def _stored_ledger(store: Any) -> tuple[dict[str, Any], str]:
    workspace = store.load("workspace.json")
    workspace_uid = workspace["id"]
    return store.load(KNOWLEDGE_DOCUMENT_NAME), workspace_uid


def _active_task_from_store(store: Any, task_id: str | None) -> ActiveTask | None:
    """The Task the Store really holds, at the revision it really carries."""

    if task_id is None:
        return None
    wanted = task_id.upper()
    for task in store.load("backlog.json")["tasks"]:
        if str(task.get("id", "")).upper() == wanted:
            return ActiveTask(
                task_uid=task["uid"],
                task_id=str(task["id"]).upper(),
                task_revision=task["revision"],
            )
    raise _refuse("unknown_task", "task_id")


def set_owner_connection_policy(
    store: Any, connections: Sequence[ConnectionPolicy]
) -> dict[str, Any]:
    """Commit a new owner connection policy, advancing the policy revision."""

    with store.transaction():
        document, workspace_uid = _stored_ledger(store)
        planned = plan_policy_revision(
            document, connections, workspace_uid=workspace_uid
        )
        store.save_many({KNOWLEDGE_DOCUMENT_NAME: planned})
        return planned


def issue_owner_knowledge_request(
    store: Any,
    request_document: Any,
    *,
    connection_alias: str,
    now: str,
    task_id: str | None = None,
) -> IssuedRequest:
    """Issue one request against the stored policy and the real Store state.

    The workspace binding and, when a Task is named, that Task's uid, display
    ID and revision are read from the Store inside this transaction, so the
    authority the wire validator is handed describes what the Store actually
    holds at commit time rather than what a caller believed a moment earlier.
    """

    with store.transaction():
        document, workspace_uid = _stored_ledger(store)
        issued = plan_request_issue(
            document,
            request_document,
            workspace_uid=workspace_uid,
            connection_alias=connection_alias,
            now=now,
            active_task=_active_task_from_store(store, task_id),
        )
        if not issued.replayed:
            store.save_many({KNOWLEDGE_DOCUMENT_NAME: issued.document})
        return issued


def owner_pending_requests(
    store: Any, *, now: str | None = None
) -> tuple[PendingRequest, ...]:
    """Reconstruct outstanding requests from the store, after a restart."""

    with store.transaction():
        document, workspace_uid = _stored_ledger(store)
        return pending_requests(document, workspace_uid=workspace_uid, now=now)
