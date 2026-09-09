"""The manual knowledge Capture importer: one transaction, one ``save_many``.

This is the answering half of the owner knowledge surface, and it is the
**manual** half only. A user carries a retrieval answer out of band and imports
it under the authentication they already have: the loopback owner session and
its CSRF token. There is no adapter credential here because there is no
connection to authenticate -- nothing in this module contacts a provider,
resolves a connection alias to an endpoint, or accepts a ``provider``/tool
string as authority. An automated adapter would need its own independently
authenticated connection, and that is deliberately later work, not a claim made
here.

**One decision, one commit.** Everything happens inside one
:meth:`~workstack.store.Store.transaction`: the workspace, the ledger, the
captures, the activity log and the backlog are read; the *whole* envelope is
validated before anything is staged; the released
:func:`~workstack.knowledge_owner_requests.plan_ledger_stage_completion` is
called **once**, after the whole batch, never once per Capture; and the ledger,
the captures and the activity log are written in a single ``save_many``. The
released :meth:`~workstack.service.WorkStack.ingest_capture` is deliberately not
reused: it writes per call, so importing a batch through it would commit a
partial batch and could not carry the ledger transition with it.

**Replay is decided on identity the caller cannot fork.** The logical key is
the ``request_id`` plus the digest of the entire admitted completion, and the
Capture identifiers are recovered from the ledger record *before* the released
planner is asked anything -- so a legitimate retry is recognised rather than
refused for allocating new identifiers. Nothing about a response body is
persisted: there is no plaintext idempotency receipt for this route, and the
route refuses an ``Idempotency-Key`` outright.

**The audit trail is content free.** One ``knowledge.capture_ingested`` event
records the request identity, the imported Capture identifiers and the count.
It carries no envelope, no normalized summary, no query, no source title, no
returned body and no exception text.
"""

from __future__ import annotations

import copy
import secrets
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .knowledge_capture_packets import (
    ImportEnvelope,
    KnowledgeImportError,
    StagedCapture,
    completion_digest,
    parse_import_envelope,
    stage_import_item,
)
from .knowledge_ledger_document import KNOWLEDGE_DOCUMENT_NAME
from .knowledge_owner_requests import plan_ledger_stage_completion
from .knowledge_request import ActiveTask
from .service_domain import _next_id

__all__ = [
    "ACTIVITY_DOCUMENT_NAME",
    "CAPTURES_DOCUMENT_NAME",
    "ImportReceipt",
    "IMPORT_EVENT_TYPE",
    "KnowledgeImportError",
    "import_knowledge_captures",
]


CAPTURES_DOCUMENT_NAME = "captures.json"
ACTIVITY_DOCUMENT_NAME = "activity.json"
BACKLOG_DOCUMENT_NAME = "backlog.json"
WORKSPACE_DOCUMENT_NAME = "workspace.json"

IMPORT_EVENT_TYPE = "knowledge.capture_ingested"

# The Task binding key set the ledger stores for a Task-bound request.
_TASK_BINDING_FIELDS = frozenset(
    {"workspace_uid", "task_uid", "task_id", "task_revision"}
)


@dataclass(frozen=True)
class ImportReceipt:
    """What one import produced, or recognised as already produced."""

    request_id: str
    capture_ids: tuple[str, ...]
    completion_digest: str
    completed_at: str
    replayed: bool
    captures: tuple[dict[str, Any], ...]


def _record(ledger: Mapping[str, Any], request_id: str) -> dict[str, Any]:
    for entry in ledger["requests"]:
        if entry["request_id"] == request_id:
            return entry
    # The importer may only import against a request the *ledger* issued. It
    # never mints one, and there is no body field that could ask it to.
    raise KnowledgeImportError("unknown_request", "request_id")


def _active_task(
    backlog: Mapping[str, Any], binding: Mapping[str, Any]
) -> ActiveTask | None:
    """The Task the Store really holds for a Task-bound request, or None.

    The display ID stored on the record is a *lookup key*. The uid and the
    revision compared against it come from the backlog, so a Task that moved
    since the request was issued refuses in the released planner rather than
    being imported against silently.
    """

    if set(binding) != _TASK_BINDING_FIELDS:
        return None
    wanted = str(binding["task_id"]).upper()
    for task in backlog.get("tasks", []):
        if str(task.get("id", "")).upper() == wanted:
            return ActiveTask(
                task_uid=task["uid"],
                task_id=str(task["id"]).upper(),
                task_revision=task["revision"],
            )
    raise KnowledgeImportError("unknown_task", "binding.task_id")


def _staged(
    envelope: ImportEnvelope, record: Mapping[str, Any], *, now: str
) -> tuple[StagedCapture, ...]:
    """Admit the whole batch before any of it is staged into a document."""

    if len(envelope.items) > record["result_limit"]:
        raise KnowledgeImportError("result_limit_exceeded", "items")
    alias = str(record["connection_alias"])
    return tuple(
        stage_import_item(
            item,
            request_id=envelope.request_id,
            connection_alias=alias,
            now=now,
            index=index,
        )
        for index, item in enumerate(envelope.items)
    )


def _existing_by_id(captures: Sequence[Any], capture_id: str) -> dict[str, Any]:
    for entry in captures:
        if isinstance(entry, dict) and entry.get("id") == capture_id:
            return entry
    # A completed record naming a Capture the store no longer holds is not a
    # replay this importer may answer: it would have to invent the evidence.
    raise KnowledgeImportError("capture_record_missing", "capture_ids")


def _refuse_source_key_collision(
    captures: Sequence[Any], staged: Sequence[StagedCapture]
) -> None:
    """No import may land on top of a Capture that already exists.

    A collision here means some other record already claims this source key.
    Overwriting it would silently replace unrelated reviewed content, so the
    whole batch refuses and nothing is written.
    """

    taken = {
        entry.get("source_key")
        for entry in captures
        if isinstance(entry, dict)
    }
    for index, entry in enumerate(staged):
        if entry.packet["source_key"] in taken:
            raise KnowledgeImportError(
                "source_key_conflict", "items[{}]".format(index)
            )


def _new_capture_records(
    captures: list[Any], staged: Sequence[StagedCapture], *, now: str
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Build the records this batch adds, allocating identifiers in order."""

    allocated: list[dict[str, Any]] = []
    for entry in staged:
        record = {
            **copy.deepcopy(entry.packet),
            "id": _next_id(captures + allocated, "C", 4),
            "retrieval": copy.deepcopy(entry.retrieval),
            # Inbox only. No link, no conversion and no Task mutation, even when
            # the request that authorised this import was bound to a Task.
            "status": "inbox",
            "linked_task_ids": [],
            "converted_task_ids": [],
            "revision": 0,
            "created_at": now,
            "updated_at": now,
            "recent_revisions": [],
        }
        allocated.append(record)
    return allocated, tuple(record["id"] for record in allocated)


def _append_event(
    activity: dict[str, Any], *, request_id: str, capture_ids: Sequence[str], now: str
) -> None:
    """One content-free audit event: identities and a count, nothing else."""

    events = activity.setdefault("activity", [])
    events.append(
        {
            "id": _next_id(events, "E", 6),
            "type": IMPORT_EVENT_TYPE,
            "created_at": now,
            "details": {
                "request_id": request_id,
                "capture_ids": list(capture_ids),
                "imported_count": len(capture_ids),
            },
        }
    )


def _receipt(
    envelope: ImportEnvelope,
    completion: Any,
    captures: Sequence[dict[str, Any]],
) -> ImportReceipt:
    return ImportReceipt(
        request_id=envelope.request_id,
        capture_ids=tuple(completion.capture_ids),
        completion_digest=completion.completion_digest,
        completed_at=completion.completed_at,
        replayed=completion.replayed,
        captures=tuple(copy.deepcopy(entry) for entry in captures),
    )


def _replay_capture_ids(record: Mapping[str, Any], digest: str) -> tuple[str, ...]:
    """The identifiers this completed request already holds.

    They are recovered *before* the released planner is called, so a legitimate
    retry replays its own work instead of being refused for having allocated a
    second set of identifiers. The digest is compared here as well so a changed
    batch refuses as a digest conflict rather than reaching the allocator.
    """

    stored = str(record["completion_digest"])
    if not secrets.compare_digest(stored, digest):
        raise KnowledgeImportError("completion_digest_mismatch", "completion_digest")
    return tuple(str(value) for value in record["capture_ids"])


def import_knowledge_captures(
    store: Any, body: Any, *, now: str
) -> ImportReceipt:
    """Import one manually carried batch against one ledger-issued request.

    ``now`` is the owner's clock, supplied by the caller exactly as every other
    knowledge entry point takes one, so expiry and the stamped instants are
    reproducible in a test and nothing below reads a wall clock.
    """

    envelope = parse_import_envelope(body)
    with store.transaction():
        workspace_uid = store.load(WORKSPACE_DOCUMENT_NAME)["id"]
        ledger = store.load(KNOWLEDGE_DOCUMENT_NAME)
        record = _record(ledger, envelope.request_id)
        staged = _staged(envelope, record, now=now)
        digest = completion_digest(envelope.request_id, staged)
        captures_document = store.load(CAPTURES_DOCUMENT_NAME)
        captures = captures_document.setdefault("captures", [])
        if record["state"] == "completed":
            return _completed_replay(
                store, envelope, record, digest, captures, workspace_uid, now=now
            )
        return _new_completion(
            store, envelope, record, digest, staged, captures_document, workspace_uid,
            now=now,
        )


def _completed_replay(
    store: Any,
    envelope: ImportEnvelope,
    record: Mapping[str, Any],
    digest: str,
    captures: Sequence[Any],
    workspace_uid: str,
    *,
    now: str,
) -> ImportReceipt:
    """Answer a retry of work the ledger already accounts for, writing nothing.

    The released planner still makes the decision, on the *original* capture
    identifiers, so replay stays the one definition it has. It is reached with
    no active Task because a replay is answered before any binding, policy or
    expiry check -- which is what lets a late retry, or one made after the
    policy was retired, still recognise its own completed work.
    """

    original = _replay_capture_ids(record, digest)
    completion = plan_ledger_stage_completion(
        store.load(KNOWLEDGE_DOCUMENT_NAME),
        request_id=envelope.request_id,
        completion_digest=digest,
        capture_ids=original,
        workspace_uid=workspace_uid,
        now=now,
        active_task=None,
    )
    saved = [_existing_by_id(captures, capture_id) for capture_id in original]
    return _receipt(envelope, completion, saved)


def _new_completion(
    store: Any,
    envelope: ImportEnvelope,
    record: Mapping[str, Any],
    digest: str,
    staged: Sequence[StagedCapture],
    captures_document: dict[str, Any],
    workspace_uid: str,
    *,
    now: str,
) -> ImportReceipt:
    """Stage the whole batch, retire the request, and commit all three at once."""

    captures = captures_document["captures"]
    _refuse_source_key_collision(captures, staged)
    activity = store.load(ACTIVITY_DOCUMENT_NAME)
    active_task = _active_task(
        store.load(BACKLOG_DOCUMENT_NAME), record["binding"]
    )
    allocated, capture_ids = _new_capture_records(captures, staged, now=now)
    completion = plan_ledger_stage_completion(
        store.load(KNOWLEDGE_DOCUMENT_NAME),
        request_id=envelope.request_id,
        completion_digest=digest,
        capture_ids=capture_ids,
        workspace_uid=workspace_uid,
        now=now,
        active_task=active_task,
    )
    captures.extend(allocated)
    _append_event(
        activity, request_id=envelope.request_id, capture_ids=capture_ids, now=now
    )
    store.save_many(
        {
            KNOWLEDGE_DOCUMENT_NAME: completion.document,
            CAPTURES_DOCUMENT_NAME: captures_document,
            ACTIVITY_DOCUMENT_NAME: activity,
        },
        operation_id="knowledge-capture-import-{}".format(digest[7:39]),
    )
    return _receipt(envelope, completion, allocated)
