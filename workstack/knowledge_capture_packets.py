"""The manual knowledge-import envelope, and the Capture records it builds.

This module is pure. It opens no path, reads no clock, allocates no identity
and reaches no store: it takes one closed import envelope plus the *actual*
ledger facts its caller already read, and returns the sanitized Capture v1.1
records that caller may stage. :mod:`workstack.knowledge_capture_import` owns
the transaction; this owns the shape.

**What the envelope may say, and what the server derives.** The envelope names
a ``request_id`` the ledger issued and, per item, an opaque ``item_id``, a
bounded display ``title``, the existing Capture ``normalized`` shape, and the
released ``workstack.capture-retrieval.v1.1`` wire. There is no ``provider``,
``tool``, ``verification``, ``source``, ``path``, ``web_url``, ``connection``
or credential field anywhere in it, at any depth, so none of those can be
asserted at all. Everything a Capture says about where its bytes came from is
built here from the *recorded* connection alias, the request identity, the item
identity and a digest of the item's own admitted content.

**These bytes were carried by hand, and the record says so.** ``provider`` is
``manual`` and ``provenance.capture_mode`` is ``manual``, exactly as the
released Capture v1.0 validator already means them. ``resource_type`` is
``knowledge.answer`` because that is what an unattested retrieval answer is --
the same value :mod:`workstack.capture_retrieval` projects as
``capture_source_type`` when nobody has attested provenance. No OpenDocuments
origin is invented, no provider allow-list is widened, and the stored retrieval
projection is re-derived on every read with **no** caller-supplied
``RetrievalVerification``, so ``origin`` is ``null`` and every reported version
stays ``reported_unverified``.

Refusals are :class:`KnowledgeImportError` with a closed ``code`` and at most
``details.field`` -- the *name* of a field in this closed schema. No submitted
value, title, summary, ref, digest or identifier is ever echoed.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .capture import (
    CaptureValidationError,
    canonical_digest,
    fingerprint_for,
    source_key_for,
    validate_capture_packet,
)
from .capture_retrieval import (
    CaptureRetrievalError,
    require_within_capture_body_budget,
    validate_retrieval_extension,
    validate_retrieval_payload,
)
from .knowledge_request import KnowledgeRequestError, canonical_uuid

__all__ = [
    "CAPTURE_SCHEMA_VERSION",
    "ENVELOPE_FIELDS",
    "IMPORT_SCHEMA",
    "ITEM_FIELDS",
    "ImportEnvelope",
    "ImportItem",
    "KnowledgeImportError",
    "MAX_IMPORT_ITEMS",
    "MAX_TITLE_CHARS",
    "NORMALIZED_OPTIONAL_FIELDS",
    "NORMALIZED_REQUIRED_FIELDS",
    "StagedCapture",
    "completion_digest",
    "imported_capture_defect",
    "parse_import_envelope",
    "stage_import_item",
    "stored_retrieval_projection",
]


IMPORT_SCHEMA = "workstack.knowledge-import.v1"

# The Capture record version a knowledge import writes. Generic ingestion still
# writes 1.0 and is not widened; this is the only writer of 1.1.
CAPTURE_SCHEMA_VERSION = "1.1"

MIN_IMPORT_ITEMS = 1
MAX_IMPORT_ITEMS = 10
MAX_TITLE_CHARS = 500

# The closed envelope. Every other field a caller might reach for -- a query, a
# provider, a tool claim, a verification, a source locator, a credential -- has
# nowhere to be written, which is the point.
ENVELOPE_FIELDS = frozenset({"schema", "request_id", "items"})
ITEM_FIELDS = frozenset({"item_id", "title", "normalized", "retrieval"})
NORMALIZED_REQUIRED_FIELDS = frozenset({"summary", "context", "action_items"})
NORMALIZED_OPTIONAL_FIELDS = frozenset({"tags"})

# What the server writes into a manual import's source and provenance. None of
# it is a caller field.
IMPORT_PROVIDER = "manual"
IMPORT_RESOURCE_TYPE = "knowledge.answer"
IMPORT_CAPTURE_MODE = "manual"
IMPORT_ADAPTER = "workstack.knowledge-import"
IMPORT_ADAPTER_VERSION = "1"
IMPORT_REDACTION_POLICY_VERSION = "1"

# A syntactically valid stand-in used only to derive the source key and the
# normalized projection, both of which are version-independent. The real
# version_ref is a digest of what that first pass admitted, and the packet is
# validated again with it before anything is staged.
_PLACEHOLDER_VERSION_REF = "sha256:" + "0" * 64

# The retrieval wire keys, rebuilt from the validator's own projection rather
# than echoed from the caller's object, so a stored record holds only values
# the released validator produced.
_WIRE_ENVELOPE_FIELDS = (
    "schema",
    "capture_schema_version",
    "request_id",
    "query_id",
    "answer_scope",
    "truncated",
)


class KnowledgeImportError(ValueError):
    """One closed import refusal, carrying at most a field *name*."""

    def __init__(self, code: str, field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details: dict[str, Any] = {} if field is None else {"field": field}


@dataclass(frozen=True)
class ImportItem:
    """One shape-checked envelope item, not yet built into a Capture."""

    item_id: str
    title: str
    normalized: dict[str, Any]
    retrieval: dict[str, Any]


@dataclass(frozen=True)
class ImportEnvelope:
    """One shape-checked import envelope. Authority is still the ledger's."""

    request_id: str
    items: tuple[ImportItem, ...]


@dataclass(frozen=True)
class StagedCapture:
    """One admitted Capture v1.1 record body, ready to be given an identity."""

    item_id: str
    packet: dict[str, Any]
    retrieval: dict[str, Any]

    def digest_material(self) -> dict[str, Any]:
        """Exactly what the completion digest covers for this item."""

        return {
            "item_id": self.item_id,
            "title": self.packet["source"]["display_title"],
            "normalized": self.packet["normalized"],
            "retrieval": self.retrieval,
        }


def _closed_object(
    value: Any, field: str, required: frozenset[str], optional: frozenset[str]
) -> dict[str, Any]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise KnowledgeImportError("invalid_import", field)
    keys = set(value)
    if keys - (required | optional):
        raise KnowledgeImportError("unknown_field", field)
    if required - keys:
        raise KnowledgeImportError("missing_field", field)
    return value


def _uuid(value: Any, field: str) -> str:
    try:
        return canonical_uuid(value, field)
    except KnowledgeRequestError as error:
        raise KnowledgeImportError("invalid_uuid", field) from error


def _title(value: Any, field: str) -> str:
    """Bounded display text. The safety gate itself is the Capture validator's."""

    if not isinstance(value, str) or not value or len(value) > MAX_TITLE_CHARS:
        raise KnowledgeImportError("invalid_text", field)
    return value


def _import_item(value: Any, index: int) -> ImportItem:
    field = "items[{}]".format(index)
    entry = _closed_object(value, field, ITEM_FIELDS, frozenset())
    normalized = _closed_object(
        entry["normalized"],
        field + ".normalized",
        NORMALIZED_REQUIRED_FIELDS,
        NORMALIZED_OPTIONAL_FIELDS,
    )
    retrieval = entry["retrieval"]
    if not isinstance(retrieval, dict):
        raise KnowledgeImportError("invalid_import", field + ".retrieval")
    return ImportItem(
        item_id=_uuid(entry["item_id"], field + ".item_id"),
        title=_title(entry["title"], field + ".title"),
        normalized=copy.deepcopy(normalized),
        retrieval=copy.deepcopy(retrieval),
    )


def parse_import_envelope(body: Any) -> ImportEnvelope:
    """Admit one closed import envelope, or refuse it with a closed code.

    This proves shape and identity only. Whether the ledger ever issued the
    named request, whether the batch fits its limit and whether it may still be
    completed are the importer's questions, answered inside its transaction.
    """

    root = _closed_object(body, "import", ENVELOPE_FIELDS, frozenset())
    if root["schema"] != IMPORT_SCHEMA:
        raise KnowledgeImportError("unsupported_schema", "schema")
    request_id = _uuid(root["request_id"], "request_id")
    raw_items = root["items"]
    if not isinstance(raw_items, list) or not (
        MIN_IMPORT_ITEMS <= len(raw_items) <= MAX_IMPORT_ITEMS
    ):
        raise KnowledgeImportError("invalid_items", "items")
    items = tuple(_import_item(entry, index) for index, entry in enumerate(raw_items))
    seen: set[str] = set()
    for index, item in enumerate(items):
        if item.item_id in seen:
            raise KnowledgeImportError(
                "duplicate_item_id", "items[{}].item_id".format(index)
            )
        seen.add(item.item_id)
    return ImportEnvelope(request_id=request_id, items=items)


def _compact(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sanitized_retrieval_wire(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the closed wire from the validator's own projection.

    Nothing here is echoed from the caller's object: every value is one the
    released validator produced, and the derived fields (``origin``,
    ``version_state``, ``capture_source_type``) are deliberately *not* stored,
    so a reader must re-derive them rather than trust a stored claim.
    """

    wire = {field: projection[field] for field in _WIRE_ENVELOPE_FIELDS}
    wire["confidence"] = dict(projection["confidence"])
    wire["evidence"] = [
        {
            "source_type": item["reported_source_type"],
            "title": item["title"],
            "document_ref": item["document_ref"],
            "chunk_ref": item["chunk_ref"],
            "source_version": item["reported_source_version"],
            "indexed_digest": item["indexed_digest"],
            "web_url": None,
        }
        for item in projection["evidence"]
    ]
    return wire


def stored_retrieval_projection(
    stored: Any, *, request_id: Any = None
) -> dict[str, Any]:
    """Re-derive a stored 1.1 record's retrieval projection, attesting nothing.

    A stored wire is re-validated on every read, with **no**
    ``RetrievalVerification``, so ``origin`` is ``null``, every reported version
    stays ``reported_unverified`` and ``capture_source_type`` stays
    ``knowledge.answer``. A malformed stored document refuses; it is never
    shown as trusted metadata, and there is no caller-supplied attestation that
    could promote it.
    """

    if not isinstance(stored, dict):
        raise KnowledgeImportError("invalid_retrieval", "retrieval")
    echoed = stored.get("request_id")
    try:
        return validate_retrieval_extension(
            stored,
            request_id=echoed if request_id is None else request_id,
            verification=None,
        )
    except (CaptureRetrievalError, KnowledgeRequestError) as error:
        raise KnowledgeImportError("invalid_retrieval", "retrieval") from error


def imported_capture_defect(document: Any) -> str | None:
    """Say how a stored captures document's 1.1 records are invalid, if at all.

    Historical 1.0 records are not judged here at all: this answers only for
    the records this importer writes. The message is a constant naming the
    document, never a stored value.
    """

    if not isinstance(document, dict) or not isinstance(
        document.get("captures"), list
    ):
        return None
    for record in document["captures"]:
        if not isinstance(record, dict):
            return "captures.json capture record is invalid"
        if record.get("schema_version") != CAPTURE_SCHEMA_VERSION:
            continue
        try:
            stored_retrieval_projection(record.get("retrieval"))
        except KnowledgeImportError:
            return "captures.json knowledge import record is invalid"
    return None


def _provisional_source(
    *, connection_alias: str, request_id: str, item_id: str
) -> dict[str, Any]:
    """The four locator fields the source key is taken over, plus their kind.

    Every one is the server's: the alias is the connection the *ledger record*
    names, and the two refs are the request and item identities. A caller
    cannot spell a share, a path or a URL here because it never supplies one.
    """

    return {
        "provider": IMPORT_PROVIDER,
        "resource_type": IMPORT_RESOURCE_TYPE,
        "connection_ref": connection_alias,
        "container_ref": request_id,
        "object_ref": item_id,
    }


def _packet(
    item: ImportItem,
    source: Mapping[str, Any],
    *,
    version_ref: str,
    now: str,
) -> dict[str, Any]:
    full_source = {
        **source,
        "version_ref": version_ref,
        "display_title": item.title,
        "web_url": None,
        "retrieved_at": now,
    }
    full_source["fingerprint"] = fingerprint_for(full_source)
    return {
        "schema_version": "1.0",
        "source_key": source_key_for(full_source),
        "source": full_source,
        "normalized": copy.deepcopy(item.normalized),
        # An import never proposes Task work. Linking stays an explicit user
        # action through the released capture routes.
        "task_hints": [],
        "provenance": {
            "capture_mode": IMPORT_CAPTURE_MODE,
            "adapter": IMPORT_ADAPTER,
            "adapter_version": IMPORT_ADAPTER_VERSION,
            "redaction_policy_version": IMPORT_REDACTION_POLICY_VERSION,
            "raw_retained": False,
            "created_at": now,
        },
    }


def _validated_packet(
    item: ImportItem,
    source: Mapping[str, Any],
    *,
    version_ref: str,
    now: str,
    index: int,
) -> dict[str, Any]:
    try:
        return validate_capture_packet(
            _packet(item, source, version_ref=version_ref, now=now)
        )
    except CaptureValidationError as error:
        # The released code is closed; the field name is one of *this* schema's,
        # so no caller-chosen key name reaches the diagnostic.
        raise KnowledgeImportError(error.code, "items[{}]".format(index)) from error


def _retrieval_projection(
    item: ImportItem, *, request_id: str, index: int
) -> dict[str, Any]:
    field = "items[{}].retrieval".format(index)
    try:
        payload = json.dumps(
            item.retrieval, ensure_ascii=False, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise KnowledgeImportError("invalid_retrieval", field) from error
    try:
        return validate_retrieval_payload(
            payload, request_id=request_id, capture_body_bytes=0
        )
    except (CaptureRetrievalError, KnowledgeRequestError) as error:
        raise KnowledgeImportError(error.code, field) from error


def _require_budget(
    packet: Mapping[str, Any], wire: Mapping[str, Any], *, index: int
) -> None:
    """The released Capture body budget, over this record's real octets."""

    try:
        require_within_capture_body_budget(len(_compact(packet)), len(_compact(wire)))
    except CaptureRetrievalError as error:
        raise KnowledgeImportError(error.code, "items[{}]".format(index)) from error


def stage_import_item(
    item: ImportItem,
    *,
    request_id: str,
    connection_alias: str,
    now: str,
    index: int,
) -> StagedCapture:
    """Build one admitted Capture v1.1 body from one envelope item.

    The retrieval wire is measured and validated through the released
    :func:`~workstack.capture_retrieval.validate_retrieval_payload`, so its own
    16 KiB bound and the strict decoder both apply to real octets. The Capture
    core is then built at v1.0 and validated by the released
    :func:`~workstack.capture.validate_capture_packet`, twice: once to admit the
    normalized projection and the display title, and once with the version
    reference derived from exactly what that first pass admitted.
    """

    projection = _retrieval_projection(item, request_id=request_id, index=index)
    wire = _sanitized_retrieval_wire(projection)
    source = _provisional_source(
        connection_alias=connection_alias,
        request_id=request_id,
        item_id=item.item_id,
    )
    admitted = _validated_packet(
        item, source, version_ref=_PLACEHOLDER_VERSION_REF, now=now, index=index
    )
    version_ref = canonical_digest(
        {
            "item_id": item.item_id,
            "title": admitted["source"]["display_title"],
            "normalized": admitted["normalized"],
            "retrieval": wire,
        }
    )
    packet = _validated_packet(
        item, source, version_ref=version_ref, now=now, index=index
    )
    _require_budget(packet, wire, index=index)
    packet["schema_version"] = CAPTURE_SCHEMA_VERSION
    return StagedCapture(item_id=item.item_id, packet=packet, retrieval=wire)


def completion_digest(request_id: str, staged: Sequence[StagedCapture]) -> str:
    """The digest that identifies one exact imported batch.

    It covers the request identity and, in order, every item's identity, its
    admitted display title, its admitted normalized projection and its
    sanitized retrieval wire. It deliberately does **not** cover the allocated
    Capture identifiers, so a retry can recognise its own completed work before
    any identifier is allocated. Reordering the items is a different batch and
    therefore a different digest: this guesses at no equivalence.
    """

    return canonical_digest(
        {
            "schema": IMPORT_SCHEMA,
            "request_id": request_id,
            "items": [entry.digest_material() for entry in staged],
        }
    )
