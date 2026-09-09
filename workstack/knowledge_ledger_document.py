"""The owner-held knowledge ledger document: its shape, and nothing else.

``knowledge.json`` is the eleventh collection-store document and the whole of
schema 6. It holds two facts the following API and import lanes both need to
be durable and owner-owned: which upstream connections this workspace's owner
has approved, and which KnowledgeRequests that owner has actually issued
against them.

This module is the *schema* half of that, split out from
:mod:`workstack.knowledge_owner_requests` for one reason: the store's own
document validator has to admit ``knowledge.json`` when it admits a v6 store,
and the owner operations reach the Store. A validator that imported the owner
module would import the Store from underneath the Store. So the shape lives
here, below everything, importing only the stdlib and the pure grammar
primitives ``workstack.capture`` and ``workstack.knowledge_request`` already
own.

Three rules decide what may appear here at all:

* **No secret, no location, no plaintext query.** Every level is a closed key
  set, so there is nowhere to put an endpoint URL, a credential, a filesystem
  path, a share name or the text of a query. A connection is named by a
  nonsecret alias from the same grammar a corpus alias already uses, which
  cannot express a scheme, a path separator or a host.
* **A request is recorded by digest.** ``request_digest`` is what binds a
  ledger record to the exact KnowledgeRequest that was issued; the query that
  produced it never lands on disk. A reissue is recognised by comparing
  digests, not by re-reading a stored query.
* **Workspace-wide, user-owned corpora only.** ``scope`` is the single literal
  ``"workspace"``. This slice deliberately has no project-level ACL and no
  place to express one; adding one is a schema change, not a value.

Refusals are :class:`KnowledgeLedgerError`, carrying a closed ``code`` and at
most the *name* of a field in this closed schema. No submitted value, alias,
identifier, digest, timestamp or query text is ever echoed into a diagnostic.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from typing import Any, Final, Mapping

from .capture import SHA256_RE, TASK_ID_RE, parse_rfc3339
from .knowledge_request import (
    CORPUS_REF_RE,
    MAX_ACTIVE_SECONDS,
    MAX_CORPUS_REFS,
    MAX_CORPUS_REF_CHARS,
    MAX_RESULT_LIMIT,
    MAX_TASK_REVISION,
    MIN_CORPUS_REFS,
    MIN_RESULT_LIMIT,
)

__all__ = [
    "CAPTURE_ID_RE",
    "CONNECTION_ALIAS_RE",
    "CONNECTION_SCOPE",
    "KNOWLEDGE_DEFAULT",
    "KNOWLEDGE_DOCUMENT_NAME",
    "KNOWLEDGE_DOCUMENT_VERSION",
    "MAX_CAPTURE_ID_CHARS",
    "MAX_CONNECTIONS",
    "MAX_KNOWLEDGE_BYTES",
    "MAX_POLICY_REVISION",
    "MAX_REQUESTS",
    "REQUEST_STATES",
    "KnowledgeLedgerError",
    "compact_bytes",
    "request_authority_is_current",
    "validate_knowledge_document",
]

KNOWLEDGE_DOCUMENT_NAME: Final[str] = "knowledge.json"
KNOWLEDGE_DOCUMENT_VERSION: Final[int] = 1

# The ledger is a small owner-held index, not a log. These bounds are what keep
# one document readable in one read and keep a looping or captured caller from
# growing an authoritative document without limit.
MAX_CONNECTIONS: Final[int] = 8
MAX_REQUESTS: Final[int] = 200
MAX_KNOWLEDGE_BYTES: Final[int] = 256 * 1024
MAX_POLICY_REVISION: Final[int] = 2**53 - 1
MAX_CAPTURE_ID_CHARS: Final[int] = 32

# A connection alias is a nonsecret registry label, exactly like a corpus
# alias: lowercase, no separator, no scheme, no host and no path shape. Reusing
# the grammar rather than restating it means the two labels cannot drift into
# meaning different things.
CONNECTION_ALIAS_RE: Final[re.Pattern[str]] = CORPUS_REF_RE
# The identifier ``workstack.service_captures`` actually allocates for a stored
# capture record. The ledger stores these and nothing else from a capture.
CAPTURE_ID_RE: Final[re.Pattern[str]] = re.compile(r"C-[0-9]{4,}")

REQUEST_STATES: Final[tuple[str, ...]] = ("pending", "completed")

# The one scope this slice grants. Workspace-wide and user-owned; explicitly
# not a project-level ACL, which would need its own subject and its own schema.
CONNECTION_SCOPE: Final[str] = "workspace"

_MAX_ACTIVE_DELTA: Final[dt.timedelta] = dt.timedelta(seconds=MAX_ACTIVE_SECONDS)

KNOWLEDGE_DEFAULT: Final[dict[str, Any]] = {
    "version": KNOWLEDGE_DOCUMENT_VERSION,
    "policy_revision": 0,
    "connections": [],
    "requests": [],
}

_TOP_KEYS: Final[frozenset[str]] = frozenset(
    {"version", "policy_revision", "connections", "requests"}
)
_CONNECTION_KEYS: Final[frozenset[str]] = frozenset(
    {"alias", "upstream_workspace_uid", "corpus_refs", "scope"}
)
_REQUEST_KEYS: Final[frozenset[str]] = frozenset(
    {
        "request_id",
        "request_digest",
        "connection_alias",
        "binding",
        "corpus_refs",
        "policy_revision",
        "result_limit",
        "requested_at",
        "expires_at",
        "state",
        "capture_ids",
        "completion_digest",
        "completed_at",
    }
)
_BINDING_WORKSPACE_ONLY: Final[frozenset[str]] = frozenset({"workspace_uid"})
_BINDING_WITH_TASK: Final[frozenset[str]] = frozenset(
    {"workspace_uid", "task_uid", "task_id", "task_revision"}
)


class KnowledgeLedgerError(ValueError):
    """Closed refusal: a code and at most a field name, never a value."""

    def __init__(self, code: str, field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field = field

    @property
    def details(self) -> dict[str, str]:
        return {} if self.field is None else {"field": self.field}


def compact_bytes(value: Any) -> bytes:
    """The canonical compact encoding this module measures and digests."""

    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _refuse(code: str, field: str | None = None) -> KnowledgeLedgerError:
    return KnowledgeLedgerError(code, field)


def _closed_object(value: Any, field: str, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _refuse("invalid_document", field)
    keys = set(value)
    if keys - allowed:
        # The reported name is this schema's own field, never submitted text.
        raise _refuse("unknown_field", field)
    if allowed - keys:
        raise _refuse("missing_field", field)
    return value


def _exact_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    # ``type(...) is not int`` again: a JSON boolean is not a number here.
    if type(value) is not int:
        raise _refuse("invalid_number", field)
    if not minimum <= value <= maximum:
        raise _refuse("out_of_range", field)
    return value


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise _refuse("invalid_uuid", field)
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise _refuse("invalid_uuid", field) from error
    if str(parsed) != value or parsed.int == 0:
        raise _refuse("invalid_uuid", field)
    return value


def _alias(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_CORPUS_REF_CHARS
        or not CONNECTION_ALIAS_RE.fullmatch(value)
    ):
        raise _refuse("invalid_alias", field)
    return value


def _corpus_refs(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise _refuse("invalid_corpus_refs", field)
    if not MIN_CORPUS_REFS <= len(value) <= MAX_CORPUS_REFS:
        raise _refuse("invalid_corpus_refs", field)
    seen: list[str] = []
    for entry in value:
        if (
            not isinstance(entry, str)
            or len(entry) > MAX_CORPUS_REF_CHARS
            or not CORPUS_REF_RE.fullmatch(entry)
        ):
            raise _refuse("invalid_corpus_refs", field)
        if entry in seen:
            raise _refuse("duplicate_corpus_ref", field)
        seen.append(entry)
    return seen


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise _refuse("invalid_digest", field)
    return value


def _instant(value: Any, field: str):
    if not isinstance(value, str):
        raise _refuse("invalid_timestamp", field)
    try:
        return parse_rfc3339(value, field)
    except ValueError as error:
        # ``parse_rfc3339`` refuses with a message naming the field; the value
        # itself never reaches this closed code.
        raise _refuse("invalid_timestamp", field) from error


def _binding(value: Any, workspace_uid: str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _refuse("invalid_document", field)
    keys = set(value)
    if keys == _BINDING_WORKSPACE_ONLY:
        with_task = False
    elif keys == _BINDING_WITH_TASK:
        with_task = True
    else:
        # The Task trio is all-or-nothing here for the same reason it is on the
        # wire: a uid without its revision names a Task state nobody read.
        raise _refuse("invalid_task_binding", field)
    stored = _canonical_uuid(value["workspace_uid"], field + ".workspace_uid")
    if stored != workspace_uid:
        # A ledger record naming another workspace is not this store's record.
        raise _refuse("workspace_mismatch", field + ".workspace_uid")
    if not with_task:
        return {"workspace_uid": stored}
    task_id = value["task_id"]
    if (
        not isinstance(task_id, str)
        or not TASK_ID_RE.fullmatch(task_id)
        or task_id != task_id.upper()
    ):
        # The wire validator projects the display ID uppercase, so exactly one
        # spelling ever reaches the ledger and a digest cannot fork on case.
        raise _refuse("invalid_task_binding", field + ".task_id")
    return {
        "workspace_uid": stored,
        "task_uid": _canonical_uuid(value["task_uid"], field + ".task_uid"),
        "task_id": task_id,
        "task_revision": _exact_int(
            value["task_revision"],
            field + ".task_revision",
            minimum=0,
            maximum=MAX_TASK_REVISION,
        ),
    }


def _capture_ids(value: Any, field: str, *, limit: int) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        # A batch wider than the limit the request was issued under is scope
        # expansion, whatever the importer believes it fetched.
        raise _refuse("invalid_capture_ids", field)
    seen: list[str] = []
    for entry in value:
        if (
            not isinstance(entry, str)
            or len(entry) > MAX_CAPTURE_ID_CHARS
            or not CAPTURE_ID_RE.fullmatch(entry)
        ):
            raise _refuse("invalid_capture_ids", field)
        if entry in seen:
            raise _refuse("duplicate_capture_id", field)
        seen.append(entry)
    return seen


def _validate_connection(value: Any, field: str, seen: set[str]) -> None:
    record = _closed_object(value, field, _CONNECTION_KEYS)
    alias = _alias(record["alias"], field + ".alias")
    if alias in seen:
        raise _refuse("duplicate_connection", field + ".alias")
    seen.add(alias)
    _canonical_uuid(record["upstream_workspace_uid"], field + ".upstream_workspace_uid")
    _corpus_refs(record["corpus_refs"], field + ".corpus_refs")
    if record["scope"] != CONNECTION_SCOPE:
        # Workspace-wide, user-owned corpora only. A project-level grant has no
        # subject in this schema and must not be spelled by widening a value.
        raise _refuse("invalid_scope", field + ".scope")


def _current_policy_defect(
    connections: Any, record: Mapping[str, Any]
) -> str | None:
    """Why a *current-revision* record is not covered by the current roster.

    Only ever asked about a record whose ``policy_revision`` already equals the
    document's. Such a record claims to stand on the policy that is in force
    right now, so the roster in force right now has to actually say so: one
    connection with that alias, granting at least the corpora the record names.
    Returns the refusal code, or ``None`` when the record checks out.
    """

    for connection in connections:
        if connection["alias"] != record["connection_alias"]:
            continue
        if set(record["corpus_refs"]) <= set(connection["corpus_refs"]):
            return None
        # The record reaches past what the connection actually grants, which is
        # scope expansion however the record came to be written.
        return "corpus_not_granted"
    return "connection_not_found"


def request_authority_is_current(
    document: Mapping[str, Any], record: Mapping[str, Any]
) -> bool:
    """Does one stored request still stand on the policy in force?

    Three things have to hold together, and this is the single predicate that
    says so: the record was issued at the document's current ``policy_``
    ``revision``, the connection it names is still on the roster, and the
    corpora it names are still granted by that connection. A record issued
    under an older revision is a readable historical record, never a current
    authority.

    Both the durable validator and the new-completion path ask this same
    question, so "admitted as current" and "may still be completed" cannot
    drift apart.
    """

    if record["policy_revision"] != document["policy_revision"]:
        return False
    return _current_policy_defect(document["connections"], record) is None


def _validate_request(
    value: Any,
    field: str,
    workspace_uid: str,
    connections: Any,
    policy_revision: int,
    seen: set[str],
) -> None:
    record = _closed_object(value, field, _REQUEST_KEYS)
    request_id = _canonical_uuid(record["request_id"], field + ".request_id")
    if request_id in seen:
        raise _refuse("duplicate_request", field + ".request_id")
    seen.add(request_id)
    _digest(record["request_digest"], field + ".request_digest")
    _alias(record["connection_alias"], field + ".connection_alias")
    _binding(record["binding"], workspace_uid, field + ".binding")
    _corpus_refs(record["corpus_refs"], field + ".corpus_refs")
    revision = _exact_int(
        record["policy_revision"],
        field + ".policy_revision",
        minimum=0,
        maximum=policy_revision,
    )
    if revision == policy_revision:
        # A record claiming the policy in force must be covered by the roster
        # in force. An older record is admitted without this check on purpose:
        # it is historical evidence, and is already void as authority.
        defect = _current_policy_defect(connections, record)
        if defect is not None:
            raise _refuse(
                defect,
                field
                + (
                    ".corpus_refs"
                    if defect == "corpus_not_granted"
                    else ".connection_alias"
                ),
            )
    result_limit = _exact_int(
        record["result_limit"],
        field + ".result_limit",
        minimum=MIN_RESULT_LIMIT,
        maximum=MAX_RESULT_LIMIT,
    )
    requested = _instant(record["requested_at"], field + ".requested_at")
    expires = _instant(record["expires_at"], field + ".expires_at")
    if not requested < expires:
        raise _refuse("invalid_request_window", field + ".expires_at")
    limit_second = requested.utc_second + _MAX_ACTIVE_DELTA
    if (expires.utc_second, expires.fraction) > (limit_second, requested.fraction):
        # The same five minutes the wire contract bounds an active request by.
        # A stored record claiming a longer window would outlive its own rule.
        raise _refuse("invalid_request_window", field + ".expires_at")
    state = record["state"]
    if state not in REQUEST_STATES:
        raise _refuse("invalid_state", field + ".state")
    capture_ids = _capture_ids(
        record["capture_ids"], field + ".capture_ids", limit=result_limit
    )
    if state == "pending":
        if capture_ids:
            raise _refuse("invalid_capture_ids", field + ".capture_ids")
        if record["completion_digest"] is not None:
            raise _refuse("invalid_digest", field + ".completion_digest")
        if record["completed_at"] is not None:
            raise _refuse("invalid_timestamp", field + ".completed_at")
        return
    _digest(record["completion_digest"], field + ".completion_digest")
    _instant(record["completed_at"], field + ".completed_at")


def validate_knowledge_document(value: Any, *, workspace_uid: str) -> None:
    """Admit ``knowledge.json`` as this workspace's owner ledger, or refuse.

    ``workspace_uid`` is the identity the surrounding store already proved, so
    a ledger carried in from another workspace is refused here rather than
    silently answering questions about a store it never belonged to.

    Referential integrity is required of exactly the records that claim it. A
    request whose stored ``policy_revision`` equals the document's must name a
    connection the current roster still holds, and its ``corpus_refs`` must be
    contained in that connection's grants: a record standing on the policy in
    force is not admitted unless the policy in force actually authorises it.

    A request issued under an *older* revision is admitted without that check,
    and deliberately so. A policy change may retire a connection while a
    request issued under the old policy is still on record; that request is
    already void as authority because its revision no longer matches, and
    dropping the record instead would erase the evidence that it was ever
    issued. It stays readable history — including a completed record and its
    replay — and it can never newly complete.
    """

    if not isinstance(workspace_uid, str) or not workspace_uid:
        raise _refuse("invalid_document", "workspace_uid")
    document = _closed_object(value, "knowledge", _TOP_KEYS)
    if document["version"] != KNOWLEDGE_DOCUMENT_VERSION:
        raise _refuse("unsupported_version", "version")
    if len(compact_bytes(document)) > MAX_KNOWLEDGE_BYTES:
        raise _refuse("document_too_large", "knowledge")
    policy_revision = _exact_int(
        document["policy_revision"],
        "policy_revision",
        minimum=0,
        maximum=MAX_POLICY_REVISION,
    )
    connections = document["connections"]
    if not isinstance(connections, list) or len(connections) > MAX_CONNECTIONS:
        raise _refuse("invalid_document", "connections")
    seen_aliases: set[str] = set()
    for index, entry in enumerate(connections):
        _validate_connection(entry, "connections[{}]".format(index), seen_aliases)
    requests = document["requests"]
    if not isinstance(requests, list) or len(requests) > MAX_REQUESTS:
        raise _refuse("invalid_document", "requests")
    seen_requests: set[str] = set()
    for index, entry in enumerate(requests):
        _validate_request(
            entry,
            "requests[{}]".format(index),
            workspace_uid,
            connections,
            policy_revision,
            seen_requests,
        )
