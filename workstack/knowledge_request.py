"""KnowledgeRequest v1: a closed, short-lived request for retrieval evidence.

This module is a *pure validation primitive*. It does not open a Store, reach a
network, hold a ledger, authenticate a connection or expose an endpoint. It
decides one question only: is this document a well-formed KnowledgeRequest v1
that the trusted caller's own authority already permits?

The separation is deliberate and load-bearing:

* The **request body carries no authority.** Active workspace, active Task,
  granted corpus aliases and the clock all arrive from the caller as
  :class:`RequestAuthority`. A body that names a corpus the caller was not
  granted is refused; a body cannot widen its own scope by asserting one.
* The **query is data, never execution.** It is bounded, control-free text the
  user can read before submitting. Nothing here interpolates it into a command,
  a path or a URL, and nothing here resolves it.
* **Five minutes bounds an active request.** ``expires_at`` must be strictly
  forward of ``requested_at`` and at most :data:`MAX_ACTIVE_SECONDS` later. An
  expired request is refused. There is deliberately no renew, extend or refresh
  entry point: a lapsed manual result requires a *fresh* request the user
  explicitly reviews, and silence is not consent.
* **The Task binding is identity only.** ``task_uid``/``task_id``/
  ``task_revision`` match :func:`workstack.knowledge_context.task_binding`, the
  binding shape already in this repository. No Task title, detail, note or body
  is attached, and none may be: attaching Task detail automatically would hand
  the retrieval side content it was never granted.

Refusals leave as :class:`KnowledgeRequestError` carrying a closed code and, at
most, the *name* of the offending field. No submitted value, query text, corpus
alias or identifier is ever echoed into a diagnostic.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import uuid
from dataclasses import dataclass
from typing import Any

from .capture import (
    PercentDecodingLimitError,
    TASK_ID_RE,
    decoded_for_validation,
    parse_rfc3339,
    text_contains_credential_material,
)

SCHEMA = "workstack.knowledge-request.v1"

# The wire is small on purpose: a request is one bounded query plus identity.
MAX_REQUEST_BYTES = 8 * 1024
# Matches the query bound the desktop knowledge host already enforces
# (``knowledge_host_search.MAX_QUERY_CHARS``). It is restated rather than
# imported: ``workstack`` must not depend on the desktop shell package.
MAX_QUERY_CHARS = 1000
MIN_CORPUS_REFS = 1
MAX_CORPUS_REFS = 8
MAX_CORPUS_REF_CHARS = 64
MIN_RESULT_LIMIT = 1
MAX_RESULT_LIMIT = 10
# A request is active for at most five minutes. Automatic submission and the
# manual out-of-band exchange are bounded by the same window.
MAX_ACTIVE_SECONDS = 300
# JSON's exactly-representable integer range. A larger revision is a malformed
# export, not a Task that really moved that far.
MAX_TASK_REVISION = 2**53 - 1
MAX_TIMESTAMP_CHARS = 64

PURPOSES = ("find_context", "extract_actions", "refresh_capture")

# A corpus alias is a nonsecret registry label: lowercase, no separators, no
# path or URL shape. It names a grant the caller already holds; it never
# describes a filesystem root, a share, a host or a credential.
CORPUS_REF_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")

_ENVELOPE_FIELDS = frozenset(
    {
        "schema",
        "request_id",
        "binding",
        "purpose",
        "query",
        "corpus_refs",
        "result_limit",
        "requested_at",
        "expires_at",
    }
)
_BINDING_WORKSPACE_ONLY = frozenset({"workspace_uid"})
_BINDING_WITH_TASK = frozenset(
    {"workspace_uid", "task_uid", "task_id", "task_revision"}
)


class KnowledgeRequestError(ValueError):
    """Closed refusal: a code and at most a field name, never a value."""

    def __init__(self, code: str, field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field = field

    @property
    def details(self) -> dict[str, str]:
        return {} if self.field is None else {"field": self.field}


@dataclass(frozen=True)
class ActiveTask:
    """The Task the trusted caller says is open, at the revision it read."""

    task_uid: str
    task_id: str
    task_revision: int


@dataclass(frozen=True)
class RequestAuthority:
    """Everything the request body is not allowed to assert about itself.

    ``granted_corpus_refs`` is the caller's own nonsecret corpus registry view.
    ``now`` is the caller's clock as a strict RFC3339 instant; this module never
    reads a wall clock, so expiry is decided by the host, reproducibly.
    """

    workspace_uid: str
    granted_corpus_refs: tuple[str, ...]
    now: str
    active_task: ActiveTask | None = None


def _require_authority(authority: Any) -> RequestAuthority:
    """Check the *shape* of the trusted authority before trusting its contents.

    The authority is host state and must never be assembled from request fields.
    But "trusted" does not mean "well formed": a corrupted host object would
    otherwise surface as a raw ``AttributeError`` or ``TypeError`` from the
    middle of a comparison, which is not a wire result a closed refusal model
    can return. Only the shape is checked, and only field *names* are reported.
    """

    if not isinstance(authority, RequestAuthority):
        raise KnowledgeRequestError("invalid_authority")
    if not isinstance(authority.workspace_uid, str):
        raise KnowledgeRequestError("invalid_authority", "workspace_uid")
    if not isinstance(authority.now, str):
        raise KnowledgeRequestError("invalid_authority", "now")
    granted = authority.granted_corpus_refs
    if not isinstance(granted, tuple) or any(
        not isinstance(entry, str) for entry in granted
    ):
        raise KnowledgeRequestError("invalid_authority", "granted_corpus_refs")
    active = authority.active_task
    if active is None:
        return authority
    if not isinstance(active, ActiveTask):
        raise KnowledgeRequestError("invalid_authority", "active_task")
    if not isinstance(active.task_uid, str):
        raise KnowledgeRequestError("invalid_authority", "active_task.task_uid")
    if not isinstance(active.task_id, str):
        raise KnowledgeRequestError("invalid_authority", "active_task.task_id")
    # ``type(...) is not int`` again: a boolean revision is a corrupt read, not
    # revision 0 or 1.
    if type(active.task_revision) is not int:
        raise KnowledgeRequestError("invalid_authority", "active_task.task_revision")
    return authority


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            # A duplicate key has no single meaning; last-wins would let a
            # sender show the user one value and the validator another.
            raise KnowledgeRequestError("duplicate_json_key")
        document[key] = value
    return document


def _reject_constant(_literal: str) -> Any:
    # NaN and +/-Infinity are not JSON and are not orderable against a bound.
    raise KnowledgeRequestError("non_finite_number")


def _finite_float(literal: str) -> float:
    """Refuse a *standard* JSON number that overflows to infinity.

    ``_reject_constant`` only sees the nonstandard literals. ``1e9999`` is legal
    JSON syntax, and Python's decoder answers ``inf`` for it; a decoder that
    promises finite numbers must refuse it here, not leave every downstream
    consumer to re-check.
    """

    value = float(literal)
    if not math.isfinite(value):
        raise KnowledgeRequestError("non_finite_number")
    return value


def payload_bytes(payload: str | bytes) -> bytes:
    """The exact UTF-8 octets a payload occupies on the wire.

    Shared so that a byte bound and a body budget measure the *same* thing: the
    encoded payload, not a caller's assertion about its size.
    """

    if isinstance(payload, str):
        try:
            return payload.encode("utf-8")
        except UnicodeEncodeError as error:
            raise KnowledgeRequestError("invalid_encoding") from error
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    raise KnowledgeRequestError("invalid_request")


def decode_strict_json(payload: str | bytes, *, maximum_bytes: int) -> Any:
    """Decode bounded UTF-8 JSON with no duplicate keys and no non-finite numbers.

    Shared with :mod:`workstack.capture_retrieval`, which decodes the answering
    half of the same exchange under its own byte bound.
    """

    raw = payload_bytes(payload)
    if len(raw) > maximum_bytes:
        raise KnowledgeRequestError("request_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise KnowledgeRequestError("invalid_encoding") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except RecursionError as error:
        # Nesting past the decoder's depth is malformed input, and a
        # RecursionError is not a ValueError, so it needs its own arm.
        raise KnowledgeRequestError("request_too_deep") from error
    except KnowledgeRequestError:
        raise
    except ValueError as error:
        raise KnowledgeRequestError("invalid_json") from error


def decode_knowledge_request(payload: str | bytes) -> Any:
    """Decode a KnowledgeRequest v1 document under the request byte bound."""

    return decode_strict_json(payload, maximum_bytes=MAX_REQUEST_BYTES)


def canonical_uuid(value: Any, field: str) -> str:
    """Accept only the canonical lowercase textual form of a non-nil UUID."""

    if not isinstance(value, str):
        raise KnowledgeRequestError("invalid_uuid", field)
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise KnowledgeRequestError("invalid_uuid", field) from error
    if str(parsed) != value or parsed.int == 0:
        # Braces, a urn: prefix, uppercase and the nil UUID are all rejected so
        # one request has exactly one identity on the wire and in a ledger.
        raise KnowledgeRequestError("invalid_uuid", field)
    return value


def _object(value: Any, field: str, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise KnowledgeRequestError("invalid_request", field)
    for key in value:
        if not isinstance(key, str):
            raise KnowledgeRequestError("invalid_request", field)
    if set(value) - allowed:
        # The reported field name is the closed schema's own, not submitted text.
        raise KnowledgeRequestError("unknown_field", field)
    if allowed - set(value):
        raise KnowledgeRequestError("missing_field", field)
    return value


def _exact_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    # ``type(...) is not int`` rejects ``True``/``False``: a JSON boolean is not
    # a number here, and bool is an int subclass.
    if type(value) is not int:
        raise KnowledgeRequestError("invalid_number", field)
    if not minimum <= value <= maximum:
        raise KnowledgeRequestError("out_of_range", field)
    return value


def _control_character(character: str) -> bool:
    """Report C0, DEL and C1. A one-line query has no whitespace layout."""

    code = ord(character)
    return code < 32 or code == 127 or 0x80 <= code <= 0x9F


def _query(value: Any) -> str:
    if not isinstance(value, str):
        raise KnowledgeRequestError("invalid_query", "query")
    query = value.strip()
    if not query or len(query) > MAX_QUERY_CHARS:
        raise KnowledgeRequestError("invalid_query", "query")
    if any(_control_character(character) for character in query):
        raise KnowledgeRequestError("invalid_query", "query")
    try:
        decoded = decoded_for_validation(query)
    except PercentDecodingLimitError as error:
        raise KnowledgeRequestError("invalid_query", "query") from error
    if text_contains_credential_material(decoded):
        # A query is shown to the user and handed to a retrieval engine. A
        # secret pasted into it would leave the trust boundary in plain text.
        raise KnowledgeRequestError("credential_material_suspected", "query")
    return query


def _corpus_refs(value: Any, granted: tuple[str, ...]) -> list[str]:
    if not isinstance(value, list):
        raise KnowledgeRequestError("invalid_corpus_refs", "corpus_refs")
    if not MIN_CORPUS_REFS <= len(value) <= MAX_CORPUS_REFS:
        raise KnowledgeRequestError("invalid_corpus_refs", "corpus_refs")
    granted_set = set(granted)
    refs: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or len(entry) > MAX_CORPUS_REF_CHARS:
            raise KnowledgeRequestError("invalid_corpus_refs", "corpus_refs")
        if not CORPUS_REF_RE.fullmatch(entry):
            raise KnowledgeRequestError("invalid_corpus_refs", "corpus_refs")
        if entry in refs:
            raise KnowledgeRequestError("duplicate_corpus_ref", "corpus_refs")
        if entry not in granted_set:
            # The body asked for a scope the caller does not hold. A request
            # cannot grant itself corpus access by naming one.
            raise KnowledgeRequestError("corpus_not_granted", "corpus_refs")
        refs.append(entry)
    return refs


def _task_binding(value: dict[str, Any], active: ActiveTask | None) -> dict[str, Any]:
    if active is None:
        raise KnowledgeRequestError("task_binding_mismatch", "binding")
    task_uid = canonical_uuid(value["task_uid"], "binding.task_uid")
    task_id = value["task_id"]
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise KnowledgeRequestError("invalid_task_binding", "binding.task_id")
    task_id = task_id.upper()
    revision = _exact_int(
        value["task_revision"],
        "binding.task_revision",
        minimum=0,
        maximum=MAX_TASK_REVISION,
    )
    if (
        task_uid != active.task_uid
        or task_id != active.task_id.upper()
        or revision != active.task_revision
    ):
        # Identity, display ID and revision must all match what the caller
        # actually has open. A stale revision is a different Task state.
        raise KnowledgeRequestError("task_binding_mismatch", "binding")
    return {"task_uid": task_uid, "task_id": task_id, "task_revision": revision}


def _binding(value: Any, authority: RequestAuthority) -> dict[str, Any]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise KnowledgeRequestError("invalid_request", "binding")
    keys = set(value)
    if keys == _BINDING_WORKSPACE_ONLY:
        with_task = False
    elif keys == _BINDING_WITH_TASK:
        with_task = True
    else:
        # The Task trio is all-or-nothing: a uid without its revision would
        # bind evidence to a Task state nobody read.
        raise KnowledgeRequestError("invalid_task_binding", "binding")
    workspace_uid = canonical_uuid(value["workspace_uid"], "binding.workspace_uid")
    if workspace_uid != canonical_uuid(authority.workspace_uid, "workspace_uid"):
        raise KnowledgeRequestError("workspace_mismatch", "binding.workspace_uid")
    binding: dict[str, Any] = {"workspace_uid": workspace_uid}
    if not with_task:
        if authority.active_task is not None:
            # A Task is open; a request that omits the binding would attach its
            # evidence to the workspace at large instead.
            raise KnowledgeRequestError("task_binding_required", "binding")
        return binding
    binding.update(_task_binding(value, authority.active_task))
    return binding


def _instant(value: Any, field: str):
    if not isinstance(value, str) or len(value) > MAX_TIMESTAMP_CHARS:
        raise KnowledgeRequestError("invalid_timestamp", field)
    try:
        return parse_rfc3339(value, field)
    except ValueError as error:
        # ``parse_rfc3339`` refuses with a CaptureValidationError naming the
        # field; the value itself never reaches this closed code.
        raise KnowledgeRequestError("invalid_timestamp", field) from error


def _window(requested: Any, expires: Any) -> None:
    if not requested < expires:
        # Zero-length and backwards windows are both refused: an expiry that is
        # not strictly forward of the request never bounds anything.
        raise KnowledgeRequestError("invalid_request_window", "expires_at")
    limit_second = requested.utc_second + dt.timedelta(seconds=MAX_ACTIVE_SECONDS)
    # Sub-second parts are compared as decimal digit strings with trailing
    # zeros stripped, which orders them exactly; the whole-second parts carry
    # the rest of the interval.
    if (expires.utc_second, expires.fraction) > (limit_second, requested.fraction):
        raise KnowledgeRequestError("invalid_request_window", "expires_at")


def validate_knowledge_request(
    document: Any, authority: RequestAuthority
) -> dict[str, Any]:
    """Return the closed KnowledgeRequest v1 projection, or refuse.

    ``authority`` is the caller's obligation, not the sender's claim. Passing a
    ``RequestAuthority`` assembled from the request body itself would defeat
    every scope check in this module. Its shape is checked first, so corrupt
    host state refuses as ``invalid_authority`` rather than escaping as a raw
    Python error.
    """

    authority = _require_authority(authority)
    root = _object(document, "request", _ENVELOPE_FIELDS)
    if root["schema"] != SCHEMA:
        raise KnowledgeRequestError("unsupported_schema", "schema")
    request_id = canonical_uuid(root["request_id"], "request_id")
    binding = _binding(root["binding"], authority)
    purpose = root["purpose"]
    if purpose not in PURPOSES:
        raise KnowledgeRequestError("invalid_purpose", "purpose")
    query = _query(root["query"])
    corpus_refs = _corpus_refs(root["corpus_refs"], authority.granted_corpus_refs)
    result_limit = _exact_int(
        root["result_limit"],
        "result_limit",
        minimum=MIN_RESULT_LIMIT,
        maximum=MAX_RESULT_LIMIT,
    )
    requested = _instant(root["requested_at"], "requested_at")
    expires = _instant(root["expires_at"], "expires_at")
    _window(requested, expires)
    now = _instant(authority.now, "now")
    if now < requested:
        raise KnowledgeRequestError("request_not_yet_active", "requested_at")
    if not now < expires:
        # Expiry is terminal. Nothing in this module renews a window; a lapsed
        # request must be raised again and reviewed again by the user.
        raise KnowledgeRequestError("request_expired", "expires_at")
    return {
        "schema": SCHEMA,
        "request_id": request_id,
        "binding": binding,
        "purpose": purpose,
        "query": query,
        "corpus_refs": corpus_refs,
        "result_limit": result_limit,
        "requested_at": root["requested_at"],
        "expires_at": root["expires_at"],
    }


def is_expired(request: dict[str, Any], now: str) -> bool:
    """Report whether an already-validated request has lapsed at ``now``.

    This is a read, not a renewal. A ``True`` answer means the caller must
    obtain a fresh request that the user explicitly reviews.
    """

    expires = _instant(request["expires_at"], "expires_at")
    return not _instant(now, "now") < expires
