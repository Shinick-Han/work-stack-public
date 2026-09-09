"""Capture v1.1 retrieval extension: bounded evidence for one KnowledgeRequest.

This is the answering half of the exchange :mod:`workstack.knowledge_request`
opens, and it is a *pure validation primitive* in exactly the same sense: no
Store, no network, no endpoint, no persistence, no provider allow-list change.
Capture Packet v1.0 is untouched -- :func:`workstack.capture.validate_capture_packet`
still projects exactly what it projected before, and importing this module does
not alter it. The extension travels *alongside* a Capture, not inside one,
until a later gate defines the 1.1 projection and its storage.

What this module refuses to let a retrieval engine claim:

* **A source it did not name.** ``document_ref`` and ``chunk_ref`` are opaque
  handles. A filesystem path, UNC share, drive letter, URL or credential in a
  ref is refused; ``web_url`` may only be ``null`` here, because no trusted
  resolver policy exists yet for NAS or Notion evidence.
* **A source or a version it merely asserts.** Everything the document says
  about provenance is projected under a ``reported_`` name and is never enough
  to make the projection's trusted ``origin``. Syntax proves only that a claim
  is *bounded*; it cannot attest that bytes came from OpenDocuments, that a
  named source is the real one, or that a version string is the source's. A
  trusted ``origin`` and a ``verified_*`` currentness state are derived only
  from :class:`RetrievalVerification`, which the caller supplies from its own
  attested state -- or they stay absent, for the later admission gate to decide.
* **Currentness it cannot prove.** ``source_version`` may be absent. When it is,
  the projection says ``unreported``; when it is present but unattested it says
  ``reported_unverified``. An ``indexed_digest`` is a digest of what the *index*
  holds; it is not the source's version, never becomes one, and never moves the
  version state.
* **A single original source for a multi-source answer.** ``answer_scope`` must
  be ``synthesized`` once the evidence spans more than one document, and a
  synthesized answer projects as ``knowledge.answer`` with no origin document.
  Naming the first evidence item as "the" source would be a fabrication.
* **A resolved location relabelled as a title.** ``title`` is display text. A
  decoded URL, UNC share, drive path (absolute or relative) or file-looking
  slash token is refused, so the location this module keeps out of the refs
  cannot re-enter as a label. A colon followed by whitespace is ordinary prose.
* **Truth.** ``confidence.score`` is a bounded finite retrieval score in
  ``[0, 1]``. It is not a probability that the answer is correct, and the
  contract says so in both directions so a reviewer is not misled by it.

Validating a document proves none of the things a host issuer must still prove:
that the transport was admitted under one of the two contract modes (a manual
user import carried by the authenticated, CSRF-protected user session, or an
automated adapter on an independently authenticated approved connection), and
that this ``request_id`` is open in a host-issued ledger and not a replay. A
``provider`` or tool string in a payload authorises neither mode. Those remain
the caller's obligations, listed in ``contracts/capture-retrieval-v1.1.md``.

:func:`validate_retrieval_payload` is the wire boundary: it measures the actual
UTF-8 payload, decodes it strictly, validates it and applies the Capture body
budget in one call. :func:`validate_retrieval_extension` takes an *already
decoded* object and therefore establishes no wire size or nesting compliance;
it exists for focused tests and for a caller that has already measured.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .capture import (
    CANARY_RE,
    EMAIL_RE,
    FORBIDDEN_KEYS,
    HTML_RE,
    QUOTED_REPLY_RE,
    SHA256_RE,
    PercentDecodingLimitError,
    decoded_for_validation,
    text_contains_credential_material,
)
from .knowledge_request import (
    KnowledgeRequestError,
    canonical_uuid,
    decode_strict_json,
    payload_bytes,
)

SCHEMA = "workstack.capture-retrieval.v1.1"
CAPTURE_SCHEMA_VERSION = "1.1"
# The Capture body bound is unchanged from ``workstack.cli``; the extension is
# budgeted inside it so adding retrieval evidence cannot grow the accepted body.
MAX_CAPTURE_BODY_BYTES = 65536
MAX_EXTENSION_BYTES = 16 * 1024
MIN_EVIDENCE = 1
MAX_EVIDENCE = 10
MAX_TITLE_CHARS = 500
MIN_REF_CHARS = 8
MAX_REF_CHARS = 256
MAX_QUERY_ID_CHARS = 128
# A closed schema is three levels deep. The bound exists so that a *directly
# supplied* object cannot drive the recursive key walk past Python's stack; the
# decoder refuses over-deep JSON before this on the wire path.
MAX_EXTENSION_DEPTH = 16

CONFIDENCE_LEVELS = ("low", "medium", "high")
SOURCE_TYPES = ("notion.page", "nas.file", "knowledge.answer")
ANSWER_SCOPES = ("single_source", "synthesized")
# What the projection is willing to say about one evidence item's currentness.
# The first two describe the *document's own claim*; only the last two are
# derived from caller-supplied verification.
VERSION_STATES = (
    "unreported",
    "reported_unverified",
    "verified_current",
    "verified_stale",
)
ORIGIN_STATES = ("synthesized", "reported_unverified", "verified")

# Keys this extension must never carry at any depth. The Capture v1.0 set is
# reused verbatim and widened with the retrieval-specific leaks: a resolved
# path, a live URL, a credential, the raw engine query, and any open-ended
# property bag that would smuggle all of the above past a closed schema.
RETRIEVAL_FORBIDDEN_KEYS = frozenset(FORBIDDEN_KEYS) | {
    "text",
    "snippet",
    "excerpt",
    "passage",
    "chunk_text",
    "path",
    "file_path",
    "filepath",
    "source_path",
    "share",
    "unc_path",
    "url",
    "uri",
    "href",
    "link",
    "token",
    "credential",
    "credentials",
    "secret",
    "password",
    "api_key",
    "query_raw",
    "raw_query",
    "metadata",
    "properties",
    "extra",
    "fields",
}

_ENVELOPE_FIELDS = frozenset(
    {
        "schema",
        "capture_schema_version",
        "request_id",
        "query_id",
        "answer_scope",
        "confidence",
        "evidence",
        "truncated",
    }
)
_CONFIDENCE_FIELDS = frozenset({"level", "score"})
_EVIDENCE_REQUIRED = frozenset({"source_type", "title", "document_ref"})
_EVIDENCE_OPTIONAL = frozenset(
    {"chunk_ref", "source_version", "indexed_digest", "web_url"}
)

# An opaque handle: no separator, no scheme, no drive letter, no traversal, no
# whitespace, no control character and no percent escape, so an encoded path or
# URL is not even representable. Notion page IDs and NAS content handles both
# fit; a path or URL cannot.
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]*")
_QUERY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:-]{0,127}")

# A resolved source location, spelled the ways a title can spell one. This is a
# bounded display-text heuristic, not a detector of every filesystem name: a
# file-looking slash token is refused, a bare filename is allowed, and a colon
# followed by whitespace is ordinary prose. A one-letter prefix is drive-like
# (absolute or relative); a longer RFC-style scheme with a non-whitespace
# payload is URI-shaped. There is no scheme allow-list.
_TITLE_LOCATION_RE = re.compile(
    r"(?ix)"
    r"   ^\s*[~.]{0,2}/"
    r" | \\"
    r" | (?<![A-Za-z0-9+.-])[A-Za-z]:(?=\S)"
    r" | //"
    r" | (?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]+:(?=\S)"
)
# Last segment of a tight slash token that looks like a filename, e.g.
# payroll.xlsx, not the unspaced prose of Q3/Q4.
_FILE_LIKE_SEGMENT_RE = re.compile(
    r"(?ix)\A[^\s/\\.][^/\\]*\.[A-Za-z0-9]{1,10}\Z"
)
# Two or more separator-shaped slashes -- ones with no space on either side --
# are a relative path ("nas/share/payroll.xlsx"), while the single spaced or
# unspaced slash of "Q3/Q4 planning" or "Budget / Forecast" is ordinary title
# text unless the last segment is file-looking.
MAX_TITLE_TIGHT_SLASHES = 1


@dataclass(frozen=True)
class VerifiedSource:
    """One provenance fact the *caller* attests, not one the document claims.

    ``source_version`` is the version the caller itself read at the source. It
    is ``None`` when the caller confirmed the document's identity but not its
    currentness -- a common and honest state for a share that exposes no
    version.
    """

    document_ref: str
    source_type: str
    source_version: str | None = None


@dataclass(frozen=True)
class RetrievalVerification:
    """The caller's attested view of the sources an answer cites.

    Empty is the default and the correct value whenever nothing has been
    verified yet: the projection then carries only ``reported_`` provenance and
    no trusted origin. Assembling this from the answering document would defeat
    the whole reported/verified split, exactly as assembling a
    ``RequestAuthority`` from a request body would.
    """

    verified_sources: tuple[VerifiedSource, ...] = ()


class CaptureRetrievalError(ValueError):
    """Closed refusal: a code and at most a field name, never a value."""

    def __init__(self, code: str, field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field = field

    @property
    def details(self) -> dict[str, str]:
        return {} if self.field is None else {"field": self.field}


def decode_retrieval_extension(payload: str | bytes) -> Any:
    """Decode an extension document under this module's own byte bound.

    Duplicate keys, non-finite numbers, bad UTF-8 and over-deep nesting are all
    refused by the shared strict decoder; only the bound differs from a request.
    """

    return decode_strict_json(payload, maximum_bytes=MAX_EXTENSION_BYTES)


def _reject_forbidden_keys(value: Any, path: str = "$", depth: int = 0) -> None:
    """Scan every depth first, so a canary gets its own code, not "unknown".

    The depth bound matters only for a directly supplied object: on the wire
    path the strict decoder has already refused over-deep JSON. Without it, a
    hand-built nest would leave this walk as a ``RecursionError``, which is not
    a closed refusal.
    """

    if depth > MAX_EXTENSION_DEPTH:
        raise CaptureRetrievalError("extension_too_deep", path)
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CaptureRetrievalError("invalid_extension", path)
            if key.casefold() in RETRIEVAL_FORBIDDEN_KEYS:
                raise CaptureRetrievalError("forbidden_field", "{}.{}".format(path, key))
            _reject_forbidden_keys(child, "{}.{}".format(path, key), depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, "{}[{}]".format(path, index), depth + 1)


def _object(
    value: Any, field: str, required: frozenset[str], optional: frozenset[str] = frozenset()
) -> dict[str, Any]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise CaptureRetrievalError("invalid_extension", field)
    keys = set(value)
    if keys - (required | optional):
        raise CaptureRetrievalError("unknown_field", field)
    if required - keys:
        raise CaptureRetrievalError("missing_field", field)
    return value


def _control_character(character: str) -> bool:
    """Report C0, DEL and C1. A one-line title has no whitespace layout."""

    code = ord(character)
    return code < 32 or code == 127 or 0x80 <= code <= 0x9F


def _bounded_string(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CaptureRetrievalError("invalid_text", field)
    return value


def _safe_title(value: Any, field: str) -> str:
    """A display title, held to the same raw-content gate as Capture v1.0 text."""

    title = _bounded_string(value, field, maximum=MAX_TITLE_CHARS)
    try:
        decoded = decoded_for_validation(title)
    except PercentDecodingLimitError as error:
        raise CaptureRetrievalError("invalid_text", field) from error
    if text_contains_credential_material(decoded):
        raise CaptureRetrievalError("credential_material_suspected", field)
    if any(_control_character(character) for character in decoded):
        raise CaptureRetrievalError("invalid_text", field)
    if (
        EMAIL_RE.search(decoded)
        or HTML_RE.search(decoded)
        or QUOTED_REPLY_RE.search(decoded)
        or CANARY_RE.search(decoded)
    ):
        # A title is a label the user reads, not a place to smuggle a rendered
        # document, an address book entry or a quoted thread.
        raise CaptureRetrievalError("raw_content_suspected", field)
    if _is_source_location(decoded):
        # The ref grammar cannot represent a path or a URL. Without this a
        # sender could simply move the resolved location into the label beside
        # it, and the contract's promise not to retain one would be untrue.
        raise CaptureRetrievalError("source_location_suspected", field)
    return title


def _file_looking_slash_token(decoded: str) -> bool:
    """Report a tight slash token whose last segment looks like a filename."""

    for token in re.finditer(r"\S+(?:/\S+)+", decoded):
        last = token.group(0).rsplit("/", 1)[-1]
        if _FILE_LIKE_SEGMENT_RE.fullmatch(last):
            return True
    return False


def _is_source_location(decoded: str) -> bool:
    """Report a decoded value that reads as a resolved path, share or URL."""

    if _TITLE_LOCATION_RE.search(decoded):
        return True
    if _file_looking_slash_token(decoded):
        return True
    tight = 0
    for index, character in enumerate(decoded):
        if character != "/":
            continue
        before = decoded[index - 1] if index else ""
        after = decoded[index + 1 : index + 2]
        if before and after and not before.isspace() and not after.isspace():
            tight += 1
    return tight > MAX_TITLE_TIGHT_SLASHES


def _opaque_ref(value: Any, field: str) -> str:
    if not isinstance(value, str) or not MIN_REF_CHARS <= len(value) <= MAX_REF_CHARS:
        raise CaptureRetrievalError("invalid_ref", field)
    if not _REF_RE.fullmatch(value):
        raise CaptureRetrievalError("invalid_ref", field)
    if text_contains_credential_material(value):
        # The grammar already excludes a path or URL, but a token-shaped handle
        # is still spelled in the characters it allows.
        raise CaptureRetrievalError("credential_material_suspected", field)
    return value


def _optional_ref(value: Any, field: str) -> str | None:
    return None if value is None else _opaque_ref(value, field)


def _source_version(value: Any, field: str) -> str | None:
    """Accept an opaque version handle or an explicit absence. Never invent one."""

    return None if value is None else _opaque_ref(value, field)


def _indexed_digest(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise CaptureRetrievalError("invalid_digest", field)
    return value


def _web_url(value: Any, field: str) -> None:
    if value is not None:
        # There is no trusted resolver policy for NAS or Notion evidence yet, so
        # the only honest value is null. A URL here would be an unverified
        # navigation target presented as if the host had vouched for it.
        raise CaptureRetrievalError("web_url_not_allowed", field)
    return None


def _confidence(value: Any) -> dict[str, Any]:
    source = _object(value, "confidence", _CONFIDENCE_FIELDS)
    level = source["level"]
    if level not in CONFIDENCE_LEVELS:
        raise CaptureRetrievalError("invalid_confidence", "confidence.level")
    score = source["score"]
    # ``type(...) is bool`` first: bool is an int subclass and True would
    # otherwise read as a perfect score.
    if type(score) is bool or type(score) not in (int, float):
        raise CaptureRetrievalError("invalid_confidence", "confidence.score")
    if type(score) is int:
        # Bound the integer *before* converting: a Python int has no width, and
        # float() on a large one raises OverflowError rather than answering.
        if not 0 <= score <= 1:
            raise CaptureRetrievalError("invalid_confidence", "confidence.score")
        return {"level": level, "score": float(score)}
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise CaptureRetrievalError("invalid_confidence", "confidence.score")
    return {"level": level, "score": score}


def _require_verification(verification: Any) -> dict[str, VerifiedSource]:
    """Check the caller's attested sources and index them by ``document_ref``.

    Trusted input still gets a shape check: a corrupt host object must refuse as
    ``invalid_verification``, not escape as a raw Python error, and it must not
    silently contribute a half-formed provenance fact to the projection.
    """

    if verification is None:
        return {}
    if not isinstance(verification, RetrievalVerification):
        raise CaptureRetrievalError("invalid_verification")
    sources = verification.verified_sources
    if not isinstance(sources, tuple):
        raise CaptureRetrievalError("invalid_verification", "verified_sources")
    attested: dict[str, VerifiedSource] = {}
    for entry in sources:
        if not isinstance(entry, VerifiedSource):
            raise CaptureRetrievalError("invalid_verification", "verified_sources")
        if not isinstance(entry.document_ref, str) or not _REF_RE.fullmatch(
            entry.document_ref
        ):
            raise CaptureRetrievalError("invalid_verification", "document_ref")
        if entry.source_type not in SOURCE_TYPES:
            raise CaptureRetrievalError("invalid_verification", "source_type")
        if entry.source_version is not None and not isinstance(
            entry.source_version, str
        ):
            raise CaptureRetrievalError("invalid_verification", "source_version")
        if entry.document_ref in attested:
            # Two attestations of one document have no single meaning; the
            # caller must decide which one it actually holds.
            raise CaptureRetrievalError("invalid_verification", "document_ref")
        attested[entry.document_ref] = entry
    return attested


def _version_state(reported: str | None, attested: VerifiedSource | None) -> str:
    """Say only what is known: reported, unreported, or actually verified."""

    if attested is None or attested.source_version is None:
        # Either nothing is attested, or identity was attested without a
        # version. Neither makes a wire claim current.
        return "unreported" if reported is None else "reported_unverified"
    if reported is None:
        # The source has a version this answer never reported. That is not
        # staleness -- it is an absent claim beside a known one.
        return "unreported"
    if reported == attested.source_version:
        return "verified_current"
    return "verified_stale"


def _evidence_item(
    value: Any, index: int, attested: dict[str, VerifiedSource]
) -> dict[str, Any]:
    field = "evidence[{}]".format(index)
    source = _object(value, field, _EVIDENCE_REQUIRED, _EVIDENCE_OPTIONAL)
    source_type = source["source_type"]
    if source_type not in SOURCE_TYPES:
        raise CaptureRetrievalError("invalid_source_type", field + ".source_type")
    version = _source_version(
        source.get("source_version"), field + ".source_version"
    )
    _web_url(source.get("web_url"), field + ".web_url")
    document_ref = _opaque_ref(source["document_ref"], field + ".document_ref")
    verified = attested.get(document_ref)
    if verified is not None and verified.source_type != source_type:
        # The caller attested this document and the document disagrees about
        # what it is. Projecting either half would be a guess.
        raise CaptureRetrievalError("verification_conflict", field + ".source_type")
    return {
        # ``reported_``: the document's own claim, bounded but unattested. The
        # name is the guard -- a reader cannot mistake it for provenance.
        "reported_source_type": source_type,
        "title": _safe_title(source["title"], field + ".title"),
        "document_ref": document_ref,
        "chunk_ref": _optional_ref(source.get("chunk_ref"), field + ".chunk_ref"),
        "reported_source_version": version,
        # Derived, never accepted from the wire, and ``verified_*`` only when
        # the caller's own attestation says so.
        "version_state": _version_state(version, verified),
        # A digest of the indexed copy. It is deliberately kept beside, not
        # inside, the version: equal digests do not prove equal sources, and it
        # never moves ``version_state``.
        "indexed_digest": _indexed_digest(
            source.get("indexed_digest"), field + ".indexed_digest"
        ),
        "web_url": None,
    }


def _evidence(value: Any, attested: dict[str, VerifiedSource]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise CaptureRetrievalError("invalid_evidence", "evidence")
    if not MIN_EVIDENCE <= len(value) <= MAX_EVIDENCE:
        raise CaptureRetrievalError("invalid_evidence", "evidence")
    items = [
        _evidence_item(entry, index, attested) for index, entry in enumerate(value)
    ]
    seen: set[tuple[str, str | None]] = set()
    for index, item in enumerate(items):
        key = (item["document_ref"], item["chunk_ref"])
        if key in seen:
            raise CaptureRetrievalError(
                "duplicate_evidence", "evidence[{}]".format(index)
            )
        seen.add(key)
    return items


def _reported_origin(
    answer_scope: str, evidence: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The single-source claim the document makes, which is still only a claim."""

    documents = {item["document_ref"] for item in evidence}
    if answer_scope == "single_source":
        if len(documents) != 1:
            # More than one document is not one source. Picking the first would
            # attribute the answer to a document that only partly supports it.
            raise CaptureRetrievalError("answer_scope_mismatch", "answer_scope")
        types = {item["reported_source_type"] for item in evidence}
        if len(types) != 1:
            raise CaptureRetrievalError("answer_scope_mismatch", "answer_scope")
        return {
            "document_ref": evidence[0]["document_ref"],
            "source_type": evidence[0]["reported_source_type"],
        }
    if len(documents) < 2:
        # ``synthesized`` claims a span the evidence does not have. Either the
        # scope is wrong or the evidence is incomplete; both are refusals.
        raise CaptureRetrievalError("answer_scope_mismatch", "answer_scope")
    return None


def _verified_origin(
    reported_origin: dict[str, Any] | None, attested: dict[str, VerifiedSource]
) -> dict[str, Any] | None:
    """Promote a claimed origin to a trusted one only on the caller's evidence.

    The source type is read from the attestation, not echoed from the wire. The
    two are already known to agree, because ``_evidence_item`` refuses the case
    where they do not.
    """

    if reported_origin is None:
        return None
    verified = attested.get(reported_origin["document_ref"])
    if verified is None:
        return None
    return {
        "document_ref": verified.document_ref,
        "source_type": verified.source_type,
    }


def validate_retrieval_extension(
    document: Any,
    *,
    request_id: str,
    verification: RetrievalVerification | None = None,
) -> dict[str, Any]:
    """Return the closed Capture v1.1 retrieval projection, or refuse.

    This entry point takes an **already decoded object**. It therefore
    establishes no wire-size and no nesting compliance: a caller that has not
    measured the payload itself must use :func:`validate_retrieval_payload`,
    which is the complete boundary. This one exists for focused tests and for a
    caller that already holds the measured bytes.

    ``request_id`` is the caller's obligation: it comes from the host-issued
    ledger entry that opened this exchange, never from the answering document.
    A body echoing an unknown identifier is refused here, but this check is a
    binding check, not ledger, replay or transport-admission enforcement.

    ``verification`` is likewise the caller's obligation and defaults to nothing
    verified. Only it can produce a trusted ``origin`` or a ``verified_*``
    version state; without it the projection carries the document's provenance
    claims under ``reported_`` names and says so.
    """

    attested = _require_verification(verification)
    _reject_forbidden_keys(document)
    root = _object(document, "extension", _ENVELOPE_FIELDS)
    if root["schema"] != SCHEMA:
        raise CaptureRetrievalError("unsupported_schema", "schema")
    if root["capture_schema_version"] != CAPTURE_SCHEMA_VERSION:
        raise CaptureRetrievalError("unsupported_schema", "capture_schema_version")
    expected = canonical_uuid_or_refuse(request_id, "request_id")
    echoed = canonical_uuid_or_refuse(root["request_id"], "request_id")
    if echoed != expected:
        raise CaptureRetrievalError("request_id_mismatch", "request_id")
    query_id = _bounded_string(root["query_id"], "query_id", maximum=MAX_QUERY_ID_CHARS)
    if not _QUERY_ID_RE.fullmatch(query_id):
        raise CaptureRetrievalError("invalid_text", "query_id")
    if text_contains_credential_material(query_id):
        # The grammar admits dots and dashes, so a JWT is spelled entirely in
        # characters it allows. A correlation ID is projected and summarised, so
        # a token wearing that name would leave the boundary in plain text.
        raise CaptureRetrievalError("credential_material_suspected", "query_id")
    if query_id == expected:
        # The engine's own correlation ID is not the host's request identity.
        # Collapsing them would let an engine mint request identities.
        raise CaptureRetrievalError("query_id_not_distinct", "query_id")
    answer_scope = root["answer_scope"]
    if answer_scope not in ANSWER_SCOPES:
        raise CaptureRetrievalError("invalid_answer_scope", "answer_scope")
    truncated = root["truncated"]
    if type(truncated) is not bool:
        raise CaptureRetrievalError("invalid_truncated", "truncated")
    confidence = _confidence(root["confidence"])
    evidence = _evidence(root["evidence"], attested)
    reported_origin = _reported_origin(answer_scope, evidence)
    origin = _verified_origin(reported_origin, attested)
    if answer_scope == "synthesized":
        origin_state = "synthesized"
    else:
        origin_state = "verified" if origin is not None else "reported_unverified"
    return {
        "schema": SCHEMA,
        "capture_schema_version": CAPTURE_SCHEMA_VERSION,
        "request_id": expected,
        "query_id": query_id,
        "answer_scope": answer_scope,
        "confidence": confidence,
        "evidence": evidence,
        "truncated": truncated,
        # The document's single-source claim, named as a claim.
        "reported_origin": reported_origin,
        # Trusted provenance, present only on the caller's own attestation.
        "origin": origin,
        "origin_state": origin_state,
        # What a later Capture 1.1 projection may record as the source type. An
        # unattested answer is recorded as an answer, not as the source it names:
        # a synthesized answer is its own kind of source, and a single-source
        # answer nobody verified has no attested source to record.
        "capture_source_type": (
            origin["source_type"] if origin is not None else "knowledge.answer"
        ),
    }


def validate_retrieval_payload(
    payload: str | bytes,
    *,
    request_id: str,
    capture_body_bytes: int = 0,
    verification: RetrievalVerification | None = None,
) -> dict[str, Any]:
    """The complete wire boundary: measure, decode strictly, validate, budget.

    This is the mandatory entry point for anything arriving as bytes. It
    measures the payload's own UTF-8 octets -- never a caller's assertion about
    them -- refuses over-bound or over-deep JSON in the decoder, validates the
    document, and only then holds the measured extension inside the unchanged
    Capture body budget alongside ``capture_body_bytes``.

    Order matters: the budget is applied last so that a malformed payload
    refuses as malformed rather than as oversized.
    """

    raw = payload_bytes(payload)
    document = decode_retrieval_extension(raw)
    projection = validate_retrieval_extension(
        document, request_id=request_id, verification=verification
    )
    require_within_capture_body_budget(capture_body_bytes, len(raw))
    return projection


def canonical_uuid_or_refuse(value: Any, field: str) -> str:
    """Reuse the request module's UUID rule under this module's closed error."""

    try:
        return canonical_uuid(value, field)
    except KnowledgeRequestError as error:
        raise CaptureRetrievalError("invalid_uuid", field) from error


def evidence_summary(extension: dict[str, Any]) -> dict[str, Any]:
    """A counts-only view for a reviewer or a later ledger entry.

    It carries no title, ref, digest or query -- only how much evidence there
    was, what is actually known about each item's currentness, whether the
    origin was verified at all, and whether the engine said it truncated. Each
    version state gets its own count rather than one blurred "unverified"
    number, so a reviewer reading only this cannot mistake a reported claim for
    a checked one.
    """

    evidence = extension["evidence"]
    counts = {state: 0 for state in VERSION_STATES}
    for item in evidence:
        counts[item["version_state"]] += 1
    return {
        "request_id": extension["request_id"],
        "query_id": extension["query_id"],
        "answer_scope": extension["answer_scope"],
        "origin_state": extension["origin_state"],
        "capture_source_type": extension["capture_source_type"],
        "evidence_count": len(evidence),
        "unreported_count": counts["unreported"],
        "reported_unverified_count": counts["reported_unverified"],
        "verified_current_count": counts["verified_current"],
        "verified_stale_count": counts["verified_stale"],
        "confidence_level": extension["confidence"]["level"],
        "truncated": extension["truncated"],
    }


def require_within_capture_body_budget(
    capture_body_bytes: int, extension_bytes: int
) -> None:
    """Hold Capture body plus extension to the unchanged 64 KiB body bound.

    The extension does not buy a larger Capture. A caller that would exceed the
    bound must send less evidence, not a bigger body.

    Both counts are the caller's. Called directly this checks arithmetic, not a
    payload: only :func:`validate_retrieval_payload` measures real octets and
    passes them here, and only that path establishes wire-size compliance.
    """

    for name, value in (
        ("capture_body_bytes", capture_body_bytes),
        ("extension_bytes", extension_bytes),
    ):
        if type(value) is not int or value < 0:
            raise CaptureRetrievalError("invalid_number", name)
    if extension_bytes > MAX_EXTENSION_BYTES:
        raise CaptureRetrievalError("extension_too_large", "extension_bytes")
    if capture_body_bytes + extension_bytes > MAX_CAPTURE_BODY_BYTES:
        raise CaptureRetrievalError("capture_body_too_large", "capture_body_bytes")
