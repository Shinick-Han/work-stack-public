"""Owner-issued KnowledgeRequests, derived from stored policy and real state.

This module is the *composition* layer between the loopback HTTP surface in
:mod:`workstack.knowledge_requests_http` and the released owner ledger in
:mod:`workstack.knowledge_owner_requests`. It re-implements nothing the ledger
already owns: the wire is judged by the released
:func:`workstack.knowledge_request.validate_knowledge_request`, the record is
planned and committed by the released
:func:`workstack.knowledge_owner_requests.issue_owner_knowledge_request`, and
the policy roster is replaced by the released
:func:`workstack.knowledge_owner_requests.set_owner_connection_policy`.

What it adds is the part a public issuer needs and the internal owner
operations deliberately do not provide:

**Identity the caller cannot choose.** A caller names an ``intent_id``; the
server derives ``request_id`` as a version-5 UUID over
:data:`REQUEST_INTENT_NAMESPACE` and the *actual* workspace identity joined to
that intent. A body that named its own ``request_id`` could mint an identity
inside another workspace's namespace, so there is nowhere in the issue body to
put one.

**A window the caller cannot choose.** ``requested_at`` is the server's clock
and ``expires_at`` is exactly :data:`REQUEST_LIFETIME_SECONDS` later, inside
the five minutes ``knowledge-request-v1`` already bounds. Nothing here reads an
expiry from a caller and nothing renews one.

**Idempotence without persisting the query.** The ledger stores a digest, never
a query, so a retry cannot be recognised by re-reading what was asked. Instead
the same intent reconstructs the same identity, and the *original stored*
``requested_at``/``expires_at`` are read back out of the ledger record before
the request is rebuilt — so an unchanged retry rebuilds a byte-identical
request whose digest matches, and a changed retry under the same intent refuses
with ``request_digest_mismatch`` rather than issuing a second authorization.

**Guard, not authority.** The caller's ``binding`` says what it believes the
workspace and Task to be. The server reads both from the Store and hands only
those to the validator, so a stale or hostile binding refuses
(``workspace_mismatch``, ``task_binding_mismatch``) instead of steering the
scope. The granted corpus aliases come from the stored connection policy the
same way.

Every refusal here is a closed :class:`~workstack.knowledge_request.
KnowledgeRequestError` or :class:`~workstack.knowledge_ledger_document.
KnowledgeLedgerError` code with at most a field *name*. No query text, alias,
identifier or timestamp is echoed.
"""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .capture import TASK_ID_RE
from .knowledge_ledger_document import (
    CONNECTION_ALIAS_RE,
    CONNECTION_SCOPE,
    KNOWLEDGE_DOCUMENT_NAME,
    MAX_KNOWLEDGE_BYTES,
    MAX_REQUESTS,
    KnowledgeLedgerError,
    compact_bytes,
)
from .knowledge_owner_requests import (
    ConnectionPolicy,
    issue_owner_knowledge_request,
    request_digest,
    set_owner_connection_policy,
)
from .knowledge_request import (
    MAX_ACTIVE_SECONDS,
    SCHEMA,
    KnowledgeRequestError,
    canonical_uuid,
)

__all__ = [
    "CONNECTION_FIELDS",
    "ConnectionPolicyReceipt",
    "ISSUE_FIELDS",
    "IssueIntent",
    "IssuedKnowledgeRequest",
    "OCCUPANCY_FIELDS",
    "POLICY_FIELDS",
    "REQUEST_INTENT_NAMESPACE",
    "REQUEST_LIFETIME_SECONDS",
    "connection_policies",
    "derive_request_id",
    "issue_knowledge_request",
    "parse_issue_body",
    "parse_policy_body",
    "project_knowledge_occupancy",
    "read_connection_policy",
    "read_connection_policy_receipt",
    "replace_connection_policy",
    "replace_connection_policy_receipt",
    "utc_now_rfc3339",
]


# The fixed application namespace for KnowledgeRequest identities. It is the
# constant `uuid.uuid5(uuid.NAMESPACE_URL,
# "https://work-stack.invalid/knowledge-request/v1/intent")`, written out so the
# value is auditable on its own and can never drift with a helper. It is part
# of the published contract: the same workspace and the same intent name the
# same request on every build, and no other application's namespace can collide
# with it.
REQUEST_INTENT_NAMESPACE = uuid.UUID("92a31ef4-d870-5657-a390-cbd452f0205b")

# A server-issued request is active for exactly this long. It is at most the
# `knowledge-request-v1` maximum, never more; the wire validator refuses a
# longer window outright rather than truncating it.
REQUEST_LIFETIME_SECONDS = MAX_ACTIVE_SECONDS

# The closed issue body. There is no `request_id`, no `requested_at`, no
# `expires_at`, no `schema`, no `provider`, no `authority` and no token: every
# one of those is derived by the server or does not exist.
ISSUE_FIELDS = frozenset(
    {
        "intent_id",
        "connection_alias",
        "binding",
        "query",
        "corpus_refs",
        "purpose",
        "result_limit",
    }
)

# The closed policy body. `expected_policy_revision` is the compare-and-set
# guard; a replacement that raced another one refuses instead of overwriting.
POLICY_FIELDS = frozenset({"expected_policy_revision", "connections"})

# The closed occupancy block published beside a policy projection. Counts and
# encoded size are taken from one already-loaded ledger snapshot; the bounds
# are the released ledger constants. Nothing from a request record is copied.
OCCUPANCY_FIELDS = frozenset(
    {"request_count", "request_bound", "encoded_bytes", "byte_bound"}
)

# The closed connection body. `scope` is deliberately absent: the ledger has
# exactly one legal scope, so it is a server constant rather than a caller
# field, and a body cannot even spell a project-level grant.
CONNECTION_FIELDS = frozenset({"alias", "upstream_workspace_uid", "corpus_refs"})

_BINDING_TASK_FIELDS = frozenset(
    {"workspace_uid", "task_uid", "task_id", "task_revision"}
)


@dataclass(frozen=True)
class IssueIntent:
    """One caller's issue request, shape-checked but not yet authorised.

    Every field except ``intent_id`` and ``connection_alias`` is carried
    through to the released wire validator as the caller sent it. Judging them
    here as well would fork the contract; the only normalisation applied is the
    projection the validator itself performs, so the document handed to the
    ledger is the document that comes back to the caller.
    """

    intent_id: str
    connection_alias: str
    binding: dict[str, Any]
    query: Any
    corpus_refs: Any
    purpose: Any
    result_limit: Any


@dataclass(frozen=True)
class ConnectionPolicyReceipt:
    """The closed policy answer plus occupancy of the same ledger snapshot.

    ``policy`` is the two-field projection existing callers already consume.
    ``occupancy`` is content-free capacity of that same document: every stored
    request record and the compact encoding of the whole ledger, including the
    connection roster. It is not a second Store load and not a stored field.
    """

    policy: dict[str, Any]
    occupancy: dict[str, int]


@dataclass(frozen=True)
class IssuedKnowledgeRequest:
    """The issued wire document plus the bounded facts about issuing it.

    ``state`` is the ledger record's own state. A replay of an already
    *completed* request is recognised, never re-authorised, and saying so here
    is what keeps that from being silent: the caller is told it is holding a
    replayed receipt for work the ledger has already accounted for.
    """

    document: dict[str, Any]
    replayed: bool
    connection_alias: str
    policy_revision: int
    state: str


def utc_now_rfc3339() -> str:
    """The production clock, as the strict RFC3339 instant the wire uses."""

    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def derive_request_id(workspace_uid: str, intent_id: str) -> str:
    """The one identity this workspace's caller may name with this intent.

    The name is ``"<workspace_uid>:<intent_id>"`` under
    :data:`REQUEST_INTENT_NAMESPACE`. Both halves are canonical UUID text
    before they are joined, so the name has exactly one spelling and the same
    intent replayed against a *different* workspace derives a different
    identity rather than colliding with an existing record.
    """

    name = "{}:{}".format(
        canonical_uuid(workspace_uid, "workspace_uid"),
        canonical_uuid(intent_id, "intent_id"),
    )
    return str(uuid.uuid5(REQUEST_INTENT_NAMESPACE, name))


def _closed_object(value: Any, field: str, allowed: frozenset[str]) -> dict[str, Any]:
    """An object with exactly ``allowed``; the reported name is the schema's."""

    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise KnowledgeRequestError("invalid_request", field)
    if set(value) - allowed:
        raise KnowledgeRequestError("unknown_field", field)
    if allowed - set(value):
        raise KnowledgeRequestError("missing_field", field)
    return value


def _alias(value: Any, field: str) -> str:
    """A nonsecret registry label, never a location and never a credential."""

    if not isinstance(value, str) or not CONNECTION_ALIAS_RE.fullmatch(value):
        # The grammar cannot express a scheme, a host, a share, a drive letter,
        # a path separator or a `user:secret@` pair, so an alias has nowhere to
        # smuggle an endpoint even before the ledger admits it.
        raise KnowledgeLedgerError("invalid_alias", field)
    return value


def _binding(value: Any) -> dict[str, Any]:
    """The caller's expected workspace/Task state, copied and left unjudged.

    Only ``task_id`` is inspected, and only because the server must look the
    Task up in the Store before the validator can compare anything. Everything
    else — including whether the key set is legal at all — is the released wire
    validator's decision.
    """

    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise KnowledgeRequestError("invalid_request", "binding")
    binding = copy.deepcopy(value)
    if set(binding) != _BINDING_TASK_FIELDS:
        return binding
    task_id = binding["task_id"]
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise KnowledgeRequestError("invalid_task_binding", "binding.task_id")
    # The validator projects the display ID uppercase; projecting it here too
    # keeps the document that is digested identical to the one that is issued.
    binding["task_id"] = task_id.upper()
    return binding


def parse_issue_body(body: Any) -> IssueIntent:
    """Admit one closed issue body, or refuse it with a closed code."""

    root = _closed_object(body, "request", ISSUE_FIELDS)
    return IssueIntent(
        intent_id=canonical_uuid(root["intent_id"], "intent_id"),
        connection_alias=_alias(root["connection_alias"], "connection_alias"),
        binding=_binding(root["binding"]),
        query=root["query"],
        corpus_refs=root["corpus_refs"],
        purpose=root["purpose"],
        result_limit=root["result_limit"],
    )


def connection_policies(value: Any) -> tuple[ConnectionPolicy, ...]:
    """The owner's replacement roster, as the ledger's own frozen policies."""

    if not isinstance(value, list):
        raise KnowledgeLedgerError("invalid_document", "connections")
    policies: list[ConnectionPolicy] = []
    for entry in value:
        fields = _closed_object(entry, "connections", CONNECTION_FIELDS)
        refs = fields["corpus_refs"]
        if not isinstance(refs, list) or any(
            not isinstance(ref, str) for ref in refs
        ):
            raise KnowledgeLedgerError("invalid_corpus_refs", "corpus_refs")
        policies.append(
            ConnectionPolicy(
                alias=_alias(fields["alias"], "alias"),
                upstream_workspace_uid=canonical_uuid(
                    fields["upstream_workspace_uid"], "upstream_workspace_uid"
                ),
                corpus_refs=tuple(refs),
                # The one legal scope, supplied by the server. A caller has no
                # field to widen it with.
                scope=CONNECTION_SCOPE,
            )
        )
    return tuple(policies)


def parse_policy_body(body: Any) -> tuple[int, tuple[ConnectionPolicy, ...]]:
    """Admit one closed policy replacement body: the CAS guard and the roster."""

    root = _closed_object(body, "policy", POLICY_FIELDS)
    expected = root["expected_policy_revision"]
    # `type(...) is not int` again: a JSON boolean is not a revision.
    if type(expected) is not int or expected < 0:
        raise KnowledgeLedgerError("invalid_number", "expected_policy_revision")
    return expected, connection_policies(root["connections"])


def _policy_projection(document: Mapping[str, Any]) -> dict[str, Any]:
    """The bounded, nonsecret policy answer, built field by field.

    Copying the stored connections wholesale would publish whatever a future
    schema adds to them. This names the four contract fields explicitly, so the
    read surface cannot widen by accident, and there is no query, token, path
    or digest anywhere in it.
    """

    return {
        "policy_revision": document["policy_revision"],
        "connections": [
            {
                "alias": entry["alias"],
                "upstream_workspace_uid": entry["upstream_workspace_uid"],
                "corpus_refs": list(entry["corpus_refs"]),
                "scope": entry["scope"],
            }
            for entry in document["connections"]
        ],
    }


def project_knowledge_occupancy(document: Mapping[str, Any]) -> dict[str, int]:
    """Content-free occupancy of one already-admitted ledger snapshot.

    ``request_count`` is ``len(requests)`` and therefore includes pending,
    completed and expired-unused records still held in the ledger.
    ``encoded_bytes`` is ``len(compact_bytes(document))``, including the
    connection roster; it is not the on-disk pretty JSON size. Bounds are the
    released ledger constants. This helper does not load the Store.
    """

    return {
        "request_count": len(document["requests"]),
        "request_bound": MAX_REQUESTS,
        "encoded_bytes": len(compact_bytes(document)),
        "byte_bound": MAX_KNOWLEDGE_BYTES,
    }


def _policy_receipt(document: Mapping[str, Any]) -> ConnectionPolicyReceipt:
    """Policy and occupancy projected from the same in-memory document."""

    return ConnectionPolicyReceipt(
        policy=_policy_projection(document),
        occupancy=project_knowledge_occupancy(document),
    )


def read_connection_policy_receipt(store: Any) -> ConnectionPolicyReceipt:
    """Load the ledger once and project policy plus occupancy from it."""

    with store.transaction():
        document = store.load(KNOWLEDGE_DOCUMENT_NAME)
    return _policy_receipt(document)


def read_connection_policy(store: Any) -> dict[str, Any]:
    """The stored owner policy and its revision. Nothing else is readable."""

    return read_connection_policy_receipt(store).policy


def replace_connection_policy_receipt(
    store: Any,
    *,
    expected_policy_revision: int,
    connections: Sequence[ConnectionPolicy],
) -> ConnectionPolicyReceipt:
    """Replace the roster and project occupancy from the committed plan.

    The read, the comparison and the released owner write all happen inside one
    outer transaction, so two owners replacing the policy at once cannot both
    observe the same revision and both commit: the loser refuses with
    ``policy_revision_changed`` and writes nothing. Occupancy is taken from the
    planned document that write already returned, not from a second load.
    """

    with store.transaction():
        document = store.load(KNOWLEDGE_DOCUMENT_NAME)
        if document["policy_revision"] != expected_policy_revision:
            raise KnowledgeLedgerError(
                "policy_revision_changed", "expected_policy_revision"
            )
        planned = set_owner_connection_policy(store, connections)
    return _policy_receipt(planned)


def replace_connection_policy(
    store: Any,
    *,
    expected_policy_revision: int,
    connections: Sequence[ConnectionPolicy],
) -> dict[str, Any]:
    """Replace the roster under a compare-and-set on the policy revision.

    The public return remains the two-field policy projection. Occupancy of the
    same committed snapshot is available from
    :func:`replace_connection_policy_receipt`.
    """

    return replace_connection_policy_receipt(
        store,
        expected_policy_revision=expected_policy_revision,
        connections=connections,
    ).policy


def _instant(value: str) -> dt.datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError) as error:
        raise KnowledgeRequestError("invalid_timestamp", "now") from error
    if parsed.tzinfo is None:
        raise KnowledgeRequestError("invalid_timestamp", "now")
    return parsed


def _window(now: str) -> tuple[str, str]:
    """The server's own request window: ``now`` truncated, plus the lifetime."""

    started = _instant(now).astimezone(dt.timezone.utc).replace(microsecond=0)
    ended = started + dt.timedelta(seconds=REQUEST_LIFETIME_SECONDS)
    return (
        started.isoformat().replace("+00:00", "Z"),
        ended.isoformat().replace("+00:00", "Z"),
    )


def _stored_record(
    document: Mapping[str, Any], request_id: str
) -> Mapping[str, Any] | None:
    """The record this identity already has, if any.

    Its window is what makes a retry idempotent without the ledger ever holding
    a query: an unchanged retry is rebuilt on the *original* timestamps, so its
    digest is the original digest. Minting a fresh window here would make every
    retry a different request that the ledger then refuses.
    """

    for entry in document["requests"]:
        if entry["request_id"] == request_id:
            return entry
    return None


def build_request_document(
    intent: IssueIntent,
    *,
    request_id: str,
    requested_at: str,
    expires_at: str,
) -> dict[str, Any]:
    """The KnowledgeRequest v1 wire document the server issues.

    The caller contributes the binding it expects, the query, the corpus refs,
    the purpose and the limit. The schema string, the identity and both
    timestamps are the server's, and there is no field for anything else.
    """

    return {
        "schema": SCHEMA,
        "request_id": request_id,
        "binding": copy.deepcopy(intent.binding),
        "purpose": intent.purpose,
        # The validator projects a trimmed query; trimming here keeps the
        # issued document and the digested projection the same bytes.
        "query": intent.query.strip() if isinstance(intent.query, str) else intent.query,
        "corpus_refs": (
            list(intent.corpus_refs)
            if isinstance(intent.corpus_refs, list)
            else intent.corpus_refs
        ),
        "result_limit": intent.result_limit,
        "requested_at": requested_at,
        "expires_at": expires_at,
    }


def issue_knowledge_request(
    store: Any,
    intent: IssueIntent,
    *,
    clock: Callable[[], str] = utc_now_rfc3339,
) -> IssuedKnowledgeRequest:
    """Issue — or recognise the reissue of — one scoped KnowledgeRequest.

    The whole decision happens inside one outer transaction: the ledger is read,
    the identity is derived from the *actual* workspace, the original window is
    recovered if this intent already has a record, the document is rebuilt and
    the released owner operation validates, plans and commits it. Two concurrent
    issues of the same intent therefore serialise: the second one sees the first
    one's record and replays it instead of minting a second authorization.

    ``clock`` is the injection point tests use. Production passes the UTC
    production clock; nothing below this call reads a wall clock at all.
    """

    now = clock()
    with store.transaction():
        workspace_uid = store.load("workspace.json")["id"]
        document = store.load(KNOWLEDGE_DOCUMENT_NAME)
        request_id = derive_request_id(workspace_uid, intent.intent_id)
        record = _stored_record(document, request_id)
        window = (
            _window(now)
            if record is None
            else (str(record["requested_at"]), str(record["expires_at"]))
        )
        state = "pending" if record is None else str(record["state"])
        request_document = build_request_document(
            intent,
            request_id=request_id,
            requested_at=window[0],
            expires_at=window[1],
        )
        issued = issue_owner_knowledge_request(
            store,
            request_document,
            connection_alias=intent.connection_alias,
            now=now,
            task_id=_expected_task_id(intent.binding),
        )
    if request_digest(request_document) != issued.request_digest:
        # The document returned to the caller is the document the ledger
        # digested. If those ever diverged the receipt would describe a request
        # that was never authorised, so the divergence refuses rather than ships.
        raise KnowledgeRequestError("invalid_request", "request")
    return IssuedKnowledgeRequest(
        document=request_document,
        replayed=issued.replayed,
        connection_alias=issued.connection_alias,
        policy_revision=issued.policy_revision,
        state=state,
    )


def _expected_task_id(binding: Mapping[str, Any]) -> str | None:
    """The Task display ID to look up, or None for a workspace-only request.

    It is a *lookup key*, not an authority: the uid and the revision the
    validator compares against come from the Task the Store actually holds.
    """

    if set(binding) != _BINDING_TASK_FIELDS:
        return None
    return str(binding["task_id"])
