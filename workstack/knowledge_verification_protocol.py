"""KnowledgeVerification v1: one transient, explicit read-time source check.

This module is a *pure validation primitive*, the same kind of thing
:mod:`workstack.knowledge_request` is. It opens no Store, spawns no child,
reaches no network, reads no wall clock and imports nothing from the execution
runtime or from any provider adapter. It decides two questions only:

* is this document a well-formed verification **request** at the caller's
  ``now``, and
* is this document a well-formed verification **result** for exactly that
  request at the caller's ``now``?

The separation from the R14 search exchange is deliberate. A verification is
*not* a search: it names an already stored Capture revision and the evidence
that Capture already holds, and it asks a separately opted-in verifier for one
bounded observation about those exact documents. Nothing here re-runs a query,
opens a source, or writes a stored currentness flag.

What the wire is allowed to say is narrow on purpose:

* **The request carries identity and expectation, never content.** A
  ``document_ref`` is the retrieval layer's own opaque handle -- reused from
  :mod:`workstack.capture_retrieval`, not respelled here -- whose grammar
  cannot represent a path, a share, a URL, a percent escape or whitespace. An
  ``expected_source_version`` is the same opaque handle or an explicit
  ``None``. There is no field for a title, a query, an ``indexed_digest``, a
  path or a free-form payload, and the closed schemas refuse one.
* **Sixty seconds bounds one observation, on both sides.** ``expires_at`` must
  be strictly forward of ``requested_at`` and at most
  :data:`MAX_ACTIVE_SECONDS` later. The request is refused once that window has
  closed, and so is a *result* that arrives after it, however early the result
  says it looked: a late answer cannot be admitted by backdating itself. There
  is no renew, on either side; a lapsed check is a fresh explicit owner action.
* **The result answers the exact request or it is refused.** Same nonce, same
  evidence length, same order, and per entry the same ``document_ref``,
  ``source_type`` and ``expected_source_version``. A verifier cannot reorder,
  drop, add or re-bind an entry, so a "current" answer can never be displayed
  against evidence the owner did not ask about.
* **The status vocabulary is deliberately coarse.** Only the pairs in
  :data:`VERIFICATION_OUTCOMES` exist. A richer source-access reason is the
  adapter's to map *down* into one of these; it never travels outward. An
  observation is a statement at a timestamp, not a permanent correctness claim,
  and an unsupported or unavailable check stays unverifiable rather than
  becoming a guess.

Refusals leave as :class:`VerificationError` carrying one closed code and
nothing else -- no submitted value, no field name, no caught message. The
strict decoder's own codes (``request_too_large``, ``duplicate_json_key``,
``non_finite_number``, ``request_too_deep``, ``invalid_encoding``,
``invalid_json``) travel unchanged, because a malformed verification wire is
malformed the same way every other document in this process is.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Mapping

from .capture_retrieval import SOURCE_TYPES, CaptureRetrievalError
# The retrieval layer's own opaque-handle and version predicates, reused rather
# than restated. They are module-private because they are not a public
# boundary, and this caller is not making them one: a second spelling of "an
# opaque ref cannot be a path" would be exactly the spelling that drifts.
from .capture_retrieval import (  # noqa: PLC2701
    _opaque_ref,
    _source_version,
)
from .knowledge_ledger_document import (
    CAPTURE_ID_RE,
    CONNECTION_ALIAS_RE,
    MAX_CAPTURE_ID_CHARS,
    MAX_POLICY_REVISION,
)
from .knowledge_request import (
    CORPUS_REF_RE,
    MAX_CORPUS_REF_CHARS,
    MAX_CORPUS_REFS,
    MIN_CORPUS_REFS,
    KnowledgeRequestError,
    canonical_uuid,
    decode_strict_json,
)
# The request module's closed-object, bounded-integer and RFC3339 predicates,
# reused for the same reason: one grammar per rule in one process.
from .knowledge_request import (  # noqa: PLC2701
    _exact_int,
    _instant,
    _object,
)

__all__ = (
    "MAX_ACTIVE_SECONDS",
    "MAX_CAPTURE_REVISION",
    "MAX_EVIDENCE",
    "MAX_VERIFICATION_BYTES",
    "MIN_EVIDENCE",
    "REQUEST_SCHEMA",
    "RESULT_SCHEMA",
    "SOURCE_TYPES",
    "VERIFICATION_OUTCOMES",
    "KnowledgeVerificationBinding",
    "VerificationError",
    "decode_verification_document",
    "validate_verification_request",
    "validate_verification_result",
)

#: The request the owner writes to a verifier's stdin.
REQUEST_SCHEMA = "workstack.knowledge-verify.v1"

#: The observation the verifier writes back to stdout.
RESULT_SCHEMA = "workstack.knowledge-verification.v1"

#: Either direction, bounded before it is decoded. A verification document is
#: identity plus at most ten opaque handles; nothing here is large.
MAX_VERIFICATION_BYTES = 16 * 1024

#: One observation is active for at most sixty seconds. This is *not* the
#: five-minute search window: a read-time check is answered now or not at all.
MAX_ACTIVE_SECONDS = 60

#: The evidence bound is the Capture extension's own, restated as this wire's
#: because a verification names evidence that already fits in a Capture.
MIN_EVIDENCE = 1
MAX_EVIDENCE = 10

#: JSON's exactly-representable integer range, shared with the ledger's own
#: policy-revision bound. A larger revision is a malformed document.
MAX_CAPTURE_REVISION = MAX_POLICY_REVISION

#: The only status/code pairs that exist. ``current`` and ``stale`` are the two
#: answers that carry an observation; everything else is an honest inability to
#: say, and must not be shown as either.
VERIFICATION_OUTCOMES = (
    ("current", "hash_matched"),
    ("stale", "hash_differs"),
    ("missing", "file_absent"),
    ("unavailable", "root_unavailable"),
    ("denied", "access_denied"),
    ("refused", "source_refused"),
    ("revoked", "mapping_revoked"),
    ("unverifiable", "no_expected_version"),
    ("unverifiable", "no_origin_verifier"),
    ("unverifiable", "unsupported_source_type"),
    ("unverifiable", "verification_unavailable"),
)

_OUTCOMES = frozenset(VERIFICATION_OUTCOMES)

_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "verification_id",
        "binding",
        "connection",
        "corpus_refs",
        "evidence",
        "requested_at",
        "expires_at",
    }
)
_BINDING_FIELDS = frozenset({"workspace_uid", "capture_id", "capture_revision"})
_CONNECTION_FIELDS = frozenset(
    {"alias", "upstream_workspace_uid", "policy_revision"}
)
_REQUEST_EVIDENCE_FIELDS = frozenset(
    {"document_ref", "source_type", "expected_source_version"}
)
_RESULT_FIELDS = frozenset({"schema", "verification_id", "checked_at", "evidence"})
_RESULT_EVIDENCE_FIELDS = _REQUEST_EVIDENCE_FIELDS | {
    "observed_source_version",
    "status",
    "code",
}


class VerificationError(ValueError):
    """Closed refusal: one code, and nothing the sender wrote.

    Not even a field name travels. A verification document is bound to a stored
    Capture the owner is already looking at, and the codes this module raises
    are the whole vocabulary a caller -- an owner surface, an adapter CLI or a
    test -- is given.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class KnowledgeVerificationBinding:
    """One alias' separately opted-in verifier, as the operator pinned it.

    ``command`` and ``environment`` are excluded from ``repr`` for the same
    reason :class:`workstack.knowledge_execution_runtime.KnowledgeDriverBinding`
    excludes its own: an operator's argv holds a filesystem path and its
    environment may name a credential file. Neither belongs in a traceback, a
    log line or a test failure message.

    Presence of this binding is the *whole* opt-in. There is no default, no
    inherited command and no derivation from the search driver's argv: an alias
    without one is simply not verifiable, which is a reportable state and not
    an error to work around.
    """

    command: tuple[str, ...] = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)


def _closed(
    value: Any, name: str, allowed: frozenset[str], code: str
) -> dict[str, Any]:
    """Exactly ``allowed``, no more and no fewer, under one closed code."""

    try:
        return _object(value, name, allowed)
    except KnowledgeRequestError as error:
        raise VerificationError(code) from error


def _uuid(value: Any, name: str, code: str) -> str:
    try:
        return canonical_uuid(value, name)
    except KnowledgeRequestError as error:
        raise VerificationError(code) from error


def _revision(value: Any, name: str, code: str) -> int:
    try:
        return _exact_int(value, name, minimum=0, maximum=MAX_CAPTURE_REVISION)
    except KnowledgeRequestError as error:
        raise VerificationError(code) from error


def _timestamp(value: Any, name: str) -> Any:
    try:
        return _instant(value, name)
    except KnowledgeRequestError as error:
        raise VerificationError("invalid_verification_timestamp") from error


def _ref(value: Any, code: str) -> str:
    try:
        return _opaque_ref(value, "document_ref")
    except CaptureRetrievalError as error:
        raise VerificationError(code) from error


def _version(value: Any, code: str) -> str | None:
    try:
        return _source_version(value, "source_version")
    except CaptureRetrievalError as error:
        raise VerificationError(code) from error


def decode_verification_document(payload: str | bytes) -> Any:
    """Decode one bounded verification document with the strict decoder.

    The same decoder the rest of the process uses, under this wire's own 16 KiB
    bound, so a duplicate key, a non-finite number, an over-deep document, a
    bad encoding and an oversized payload are refused by one grammar rather
    than by a second copy of it. The decoder's codes travel unchanged.
    """

    try:
        return decode_strict_json(payload, maximum_bytes=MAX_VERIFICATION_BYTES)
    except KnowledgeRequestError as error:
        raise VerificationError(error.code) from error


def _decoded(document: Any) -> Any:
    """Accept either an already-decoded document or its bounded wire octets.

    A caller that holds a mapping -- the owner building a request, or a test --
    passes it straight through. A caller reading a child's stdout or its own
    stdin passes the octets and gets this module's decoder, so the byte bound
    and the strict grammar cannot be forgotten at that boundary.
    """

    if isinstance(document, (bytes, bytearray, str)):
        return decode_verification_document(document)
    return document


def _window(requested: Any, expires: Any) -> None:
    """A strictly positive window of at most sixty seconds."""

    if not requested < expires:
        raise VerificationError("invalid_verification_window")
    limit_second = requested.utc_second + dt.timedelta(seconds=MAX_ACTIVE_SECONDS)
    # Sub-second parts compare as decimal digit strings with trailing zeros
    # stripped, which orders them exactly; the whole seconds carry the rest.
    if (expires.utc_second, expires.fraction) > (limit_second, requested.fraction):
        raise VerificationError("invalid_verification_window")


def _binding(value: Any) -> dict[str, Any]:
    """The stored Capture revision this one check is about."""

    source = _closed(
        value, "binding", _BINDING_FIELDS, "invalid_verification_binding"
    )
    workspace_uid = _uuid(
        source["workspace_uid"],
        "binding.workspace_uid",
        "invalid_verification_binding",
    )
    capture_id = source["capture_id"]
    if (
        not isinstance(capture_id, str)
        or len(capture_id) > MAX_CAPTURE_ID_CHARS
        or not CAPTURE_ID_RE.fullmatch(capture_id)
    ):
        raise VerificationError("invalid_verification_binding")
    revision = _revision(
        source["capture_revision"],
        "binding.capture_revision",
        "invalid_verification_binding",
    )
    return {
        "workspace_uid": workspace_uid,
        "capture_id": capture_id,
        "capture_revision": revision,
    }


def _connection(value: Any) -> dict[str, Any]:
    """The alias, its upstream identity and the policy revision that bound it.

    The same three facts the search exchange already carries, so a verifier is
    pinned to one upstream workspace exactly the way a search driver is.
    """

    source = _closed(
        value, "connection", _CONNECTION_FIELDS, "invalid_verification_connection"
    )
    alias = source["alias"]
    if not isinstance(alias, str) or not CONNECTION_ALIAS_RE.fullmatch(alias):
        raise VerificationError("invalid_verification_connection")
    upstream = _uuid(
        source["upstream_workspace_uid"],
        "connection.upstream_workspace_uid",
        "invalid_verification_connection",
    )
    revision = _revision(
        source["policy_revision"],
        "connection.policy_revision",
        "invalid_verification_connection",
    )
    return {
        "alias": alias,
        "upstream_workspace_uid": upstream,
        "policy_revision": revision,
    }


def _corpus_refs(value: Any) -> list[str]:
    """One to eight distinct nonsecret corpus labels.

    Scope is the *caller's* to prove: this module checks the grammar and the
    bound, and the owner admission that builds the request is what derives
    these from the original completed request rather than from a client.
    """

    if not isinstance(value, list):
        raise VerificationError("invalid_verification_corpus_refs")
    if not MIN_CORPUS_REFS <= len(value) <= MAX_CORPUS_REFS:
        raise VerificationError("invalid_verification_corpus_refs")
    refs: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or len(entry) > MAX_CORPUS_REF_CHARS:
            raise VerificationError("invalid_verification_corpus_refs")
        if not CORPUS_REF_RE.fullmatch(entry):
            raise VerificationError("invalid_verification_corpus_refs")
        if entry in refs:
            raise VerificationError("duplicate_verification_corpus_ref")
        refs.append(entry)
    return refs


def _request_entry(value: Any) -> dict[str, Any]:
    """One evidence item as the owner asks about it: handle, kind, expectation."""

    source = _closed(
        value, "evidence", _REQUEST_EVIDENCE_FIELDS, "invalid_verification_evidence"
    )
    source_type = source["source_type"]
    if source_type not in SOURCE_TYPES:
        raise VerificationError("invalid_verification_source_type")
    return {
        "document_ref": _ref(
            source["document_ref"], "invalid_verification_evidence"
        ),
        "source_type": source_type,
        "expected_source_version": _version(
            source["expected_source_version"], "invalid_verification_evidence"
        ),
    }


def _request_evidence(value: Any) -> list[dict[str, Any]]:
    """One to ten entries, in the owner's order.

    A repeated ``document_ref`` is *allowed*: distinct chunks of one document
    are distinct evidence items, and collapsing them would silently change the
    count and the order the result must answer with.
    """

    if not isinstance(value, list):
        raise VerificationError("invalid_verification_evidence")
    if not MIN_EVIDENCE <= len(value) <= MAX_EVIDENCE:
        raise VerificationError("invalid_verification_evidence")
    return [_request_entry(entry) for entry in value]


def validate_verification_request(document: Any, *, now: str) -> dict[str, Any]:
    """Return the closed verification request projection, or refuse.

    ``now`` is the caller's clock as a strict RFC3339 instant; this module
    never reads a wall clock, so activation and expiry are decided by the host,
    reproducibly. A request that is not yet active or has already lapsed is
    refused here rather than being handed to a verifier.
    """

    root = _closed(
        _decoded(document),
        "verification_request",
        _REQUEST_FIELDS,
        "invalid_verification_request",
    )
    if root["schema"] != REQUEST_SCHEMA:
        raise VerificationError("unsupported_verification_schema")
    verification_id = _uuid(
        root["verification_id"], "verification_id", "invalid_verification_id"
    )
    binding = _binding(root["binding"])
    connection = _connection(root["connection"])
    corpus_refs = _corpus_refs(root["corpus_refs"])
    evidence = _request_evidence(root["evidence"])
    requested = _timestamp(root["requested_at"], "requested_at")
    expires = _timestamp(root["expires_at"], "expires_at")
    _window(requested, expires)
    instant = _timestamp(now, "now")
    if instant < requested:
        raise VerificationError("verification_not_yet_active")
    if not instant < expires:
        # Expiry is terminal. Nothing here renews a window; a lapsed check is a
        # fresh explicit owner action the user takes again.
        raise VerificationError("verification_expired")
    return {
        "schema": REQUEST_SCHEMA,
        "verification_id": verification_id,
        "binding": binding,
        "connection": connection,
        "corpus_refs": corpus_refs,
        "evidence": evidence,
        "requested_at": root["requested_at"],
        "expires_at": root["expires_at"],
    }


def _admitted_request(request: Any) -> Mapping[str, Any]:
    """Shape-check the caller's own normalized request before trusting it.

    The request is the caller's retained obligation, not the answering side's
    claim, and it is always something :func:`validate_verification_request`
    returned. "Trusted" is not "well formed", though: a corrupt or hand-built
    mapping would otherwise surface as a ``KeyError`` from the middle of a
    comparison, which is not an outcome a closed refusal model can return.
    """

    if not isinstance(request, Mapping) or set(request) != _REQUEST_FIELDS:
        raise VerificationError("invalid_verification_request")
    if not isinstance(request["verification_id"], str):
        raise VerificationError("invalid_verification_request")
    evidence = request["evidence"]
    if not isinstance(evidence, list):
        raise VerificationError("invalid_verification_request")
    if not MIN_EVIDENCE <= len(evidence) <= MAX_EVIDENCE:
        raise VerificationError("invalid_verification_request")
    for entry in evidence:
        if not isinstance(entry, Mapping) or set(entry) != _REQUEST_EVIDENCE_FIELDS:
            raise VerificationError("invalid_verification_request")
    return request


def _checked_at(value: Any, request: Mapping[str, Any], now: str) -> str:
    """The instant the verifier says it looked, inside this request's window.

    Two different things are bounded here, and both have to hold.

    The **arrival** must be inside the original window: if the owner's own
    clock is already at or past ``expires_at``, this observation is refused
    however honest its own timestamp is. A verifier that took two minutes to
    answer a one-minute check cannot have that answer accepted by backdating
    it -- ``checked_at`` is the child's claim, and a claim inside a window that
    has since closed is exactly the case a bounded observation exists to
    exclude. Without this, retaining the original timestamps would bound
    nothing at runtime.

    The **observation itself** must then be inside that window too: not before
    the request was made, not after the owner's own clock, and strictly before
    the window closes. An observation dated outside its window describes a
    moment this check never covered.
    """

    checked = _timestamp(value, "checked_at")
    requested = _timestamp(request["requested_at"], "requested_at")
    expires = _timestamp(request["expires_at"], "expires_at")
    instant = _timestamp(now, "now")
    if not instant < expires:
        # The same terminal expiry the request side reports, decided again on
        # the way out. There is no renew here either.
        raise VerificationError("verification_expired")
    if checked < requested or instant < checked or not checked < expires:
        raise VerificationError("verification_checked_at_out_of_window")
    return value


def _consistent(status: str, expected: str | None, observed: str | None) -> None:
    """The status must be the one the two versions actually support.

    ``current`` and ``stale`` are the only answers that may carry an observed
    version, and each is the *specific* relation between the two. Every other
    status is an inability to observe, so it reports no version at all rather
    than an empty-looking one a surface could misread as agreement.
    """

    if status == "current":
        if expected is None or observed is None or observed != expected:
            raise VerificationError("verification_status_inconsistent")
        return
    if status == "stale":
        if expected is None or observed is None or observed == expected:
            raise VerificationError("verification_status_inconsistent")
        return
    if observed is not None:
        raise VerificationError("verification_status_inconsistent")


def _result_entry(value: Any, expected: Mapping[str, Any]) -> dict[str, Any]:
    """One answered evidence item, bound to the exact entry that was asked."""

    source = _closed(
        value, "evidence", _RESULT_EVIDENCE_FIELDS, "invalid_verification_evidence"
    )
    source_type = source["source_type"]
    if source_type not in SOURCE_TYPES:
        raise VerificationError("invalid_verification_source_type")
    document_ref = _ref(source["document_ref"], "invalid_verification_evidence")
    expected_version = _version(
        source["expected_source_version"], "invalid_verification_evidence"
    )
    if (
        document_ref != expected["document_ref"]
        or source_type != expected["source_type"]
        or expected_version != expected["expected_source_version"]
    ):
        # The answer re-bound this position to other evidence. Displaying it
        # against the entry the owner asked about is exactly the mistake this
        # per-entry echo exists to make impossible.
        raise VerificationError("verification_evidence_mismatch")
    observed = _version(
        source["observed_source_version"], "invalid_verification_observed_version"
    )
    status = source["status"]
    code = source["code"]
    if (
        not isinstance(status, str)
        or not isinstance(code, str)
        or (status, code) not in _OUTCOMES
    ):
        raise VerificationError("invalid_verification_status")
    _consistent(status, expected_version, observed)
    return {
        "document_ref": document_ref,
        "source_type": source_type,
        "expected_source_version": expected_version,
        "observed_source_version": observed,
        "status": status,
        "code": code,
    }


def _result_evidence(value: Any, expected: list[Any]) -> list[dict[str, Any]]:
    """Same length, same order, one answer per asked entry."""

    if not isinstance(value, list) or len(value) != len(expected):
        raise VerificationError("verification_evidence_mismatch")
    return [_result_entry(entry, asked) for entry, asked in zip(value, expected)]


def validate_verification_result(
    document: Any, *, request: Any, now: str
) -> dict[str, Any]:
    """Return the closed verification result projection, or refuse.

    ``request`` is the normalized request this owner actually sent, retained
    across the child I/O. Every binding check below is against that retained
    document: the nonce, the evidence count, the order and each entry's handle,
    kind and expected version. A verifier that answers a different question
    gets no display at all.
    """

    admitted = _admitted_request(request)
    root = _closed(
        _decoded(document),
        "verification_result",
        _RESULT_FIELDS,
        "invalid_verification_result",
    )
    if root["schema"] != RESULT_SCHEMA:
        raise VerificationError("unsupported_verification_schema")
    verification_id = _uuid(
        root["verification_id"], "verification_id", "invalid_verification_id"
    )
    if verification_id != admitted["verification_id"]:
        # A stale or replayed observation answering some other check.
        raise VerificationError("verification_id_mismatch")
    checked_at = _checked_at(root["checked_at"], admitted, now)
    evidence = _result_evidence(root["evidence"], list(admitted["evidence"]))
    return {
        "schema": RESULT_SCHEMA,
        "verification_id": verification_id,
        "checked_at": checked_at,
        "evidence": evidence,
    }
