"""Project one untrusted driver envelope into a closed manual-import proposal.

This module is a *pure projection primitive*. It opens no path, reads no
clock, mutates no caller object, logs nothing and mints no token. The caller
already admitted the request: ``request_id``, ``connection_alias``,
``result_limit`` and ``now`` are trusted structural facts from that admission,
not claims the driver may make. ``payload`` is the child's stdout and is
completely untrusted.

The projection reuses the released import seams rather than restating them.
:func:`workstack.knowledge_request.decode_strict_json` decodes the bytes;
:func:`workstack.knowledge_capture_packets.parse_import_envelope` and
:func:`workstack.knowledge_capture_packets.stage_import_item` admit the
envelope and rebuild each item. Staging is called with **no** retrieval
verifier, so the manual ``knowledge.answer`` / ``reported_unverified``
semantics stay exactly what the released importer already records.

What leaves is the *public* ``workstack.knowledge-import.v1`` item shape, not
staging's internal digest material. The two differ in one place:
:meth:`StagedCapture.digest_material` carries the Capture model's generated
action ``id``, a record identifier the released import wire has never had a
field for. Every level of the returned item is therefore rebuilt from the
public keys after the released validators have run, so an internal field the
Capture model gains later is dropped here instead of reaching an owner's
closed importer -- or browser parser -- as an unknown field.

Nothing here attests origin, currentness, a provider, a path or a
credential. A document that claims any of those is refused by the existing
closed validators, or those claims are dropped from the returned envelope
because they are not among the public keys rebuilt above. The returned
object is a canonical ``workstack.knowledge-import.v1`` envelope the owner
importer already understands; it is not a Capture packet, not a ledger write and not
an HTTP body.

Refusals are closed codes. Decoder refusals from
:func:`decode_strict_json` propagate as :class:`KnowledgeRequestError`. Import
and staging refusals propagate as :class:`KnowledgeImportError`. No payload
bytes, path, key, title or exception text is copied onto a code, a message or
``details``.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping, Sequence

from .capture import parse_rfc3339
from .knowledge_capture_packets import (
    IMPORT_SCHEMA,
    ImportEnvelope,
    KnowledgeImportError,
    StagedCapture,
    parse_import_envelope,
    stage_import_item,
)
from .knowledge_request import (
    CORPUS_REF_RE,
    MAX_CORPUS_REF_CHARS,
    MAX_RESULT_LIMIT,
    MAX_TIMESTAMP_CHARS,
    MIN_RESULT_LIMIT,
    KnowledgeRequestError,
    canonical_uuid,
    decode_strict_json,
)

__all__ = [
    "MAX_PROPOSAL_BYTES",
    "validate_execution_proposal",
]

# The released whole-envelope bound: the same 64 KiB the import route measures
# on the raw body, applied here to the untrusted stdout *and* to the canonical
# re-encoding. Projection can grow a compact document, so a payload that fitted
# still refuses if the rebuilt envelope does not.
MAX_PROPOSAL_BYTES = 64 * 1024

# The accepted public import wire, level by level. These are the released
# ``workstack.knowledge-import.v1`` keys the owner importer and the browser's
# closed parser both admit; neither has ever had a field for the Capture
# model's generated action ``id``. Rebuilding from these keys is the whole
# difference between the internal digest material and what is returned.
_PUBLIC_ITEM_KEYS = ("item_id", "title", "normalized", "retrieval")
_PUBLIC_NORMALIZED_KEYS = ("summary", "context", "action_items", "tags")
_PUBLIC_ACTION_KEYS = ("title", "detail", "priority", "due")


def validate_execution_proposal(
    payload: bytes,
    *,
    request_id: str,
    connection_alias: str,
    result_limit: int,
    now: str,
) -> dict[str, Any]:
    """Return the closed import envelope for one driver payload, or refuse.

    Trusted arguments are admitted first so a malformed caller fact never
    reaches the decoder. The payload must be ``bytes`` -- ``str`` and
    ``bytearray`` are refused before JSON is parsed. The envelope's
    ``request_id`` must equal the trusted identity; the item count may not
    exceed the issued ``result_limit``. Items are staged with that trusted
    identity, alias and clock, then projected onto the public import wire.
    """

    trusted_id = _admitted_request_id(request_id)
    alias = _admitted_alias(connection_alias)
    limit = _admitted_result_limit(result_limit)
    stamp = _admitted_now(now)
    raw = _require_bytes(payload)
    envelope = parse_import_envelope(
        decode_strict_json(raw, maximum_bytes=MAX_PROPOSAL_BYTES)
    )
    _require_matching_request(envelope, trusted_id)
    _require_within_issued_limit(envelope, limit)
    staged = _stage_items(
        envelope, request_id=trusted_id, connection_alias=alias, now=stamp
    )
    return _canonical_proposal(trusted_id, staged)


def _admitted_request_id(value: Any) -> str:
    try:
        return canonical_uuid(value, "request_id")
    except KnowledgeRequestError as error:
        raise KnowledgeImportError(error.code, error.field) from error


def _admitted_alias(value: Any) -> str:
    """The released connection-alias grammar, never a location or a secret."""

    if (
        not isinstance(value, str)
        or len(value) > MAX_CORPUS_REF_CHARS
        or not CORPUS_REF_RE.fullmatch(value)
    ):
        raise KnowledgeImportError("invalid_alias", "connection_alias")
    return value


def _admitted_result_limit(value: Any) -> int:
    """The issued bound: an integer in ``1 .. 10``. ``True`` is not ``1``."""

    if type(value) is not int:
        raise KnowledgeImportError("invalid_number", "result_limit")
    if not MIN_RESULT_LIMIT <= value <= MAX_RESULT_LIMIT:
        raise KnowledgeImportError("out_of_range", "result_limit")
    return value


def _admitted_now(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_TIMESTAMP_CHARS:
        raise KnowledgeImportError("invalid_timestamp", "now")
    try:
        parse_rfc3339(value, "now")
    except ValueError as error:
        raise KnowledgeImportError("invalid_timestamp", "now") from error
    return value


def _require_bytes(payload: Any) -> bytes:
    if type(payload) is not bytes:
        raise KnowledgeImportError("invalid_import")
    return payload


def _require_matching_request(envelope: ImportEnvelope, request_id: str) -> None:
    if envelope.request_id != request_id:
        raise KnowledgeImportError("request_id_mismatch", "request_id")


def _require_within_issued_limit(envelope: ImportEnvelope, result_limit: int) -> None:
    if len(envelope.items) > result_limit:
        raise KnowledgeImportError("result_limit_exceeded", "items")


def _stage_items(
    envelope: ImportEnvelope,
    *,
    request_id: str,
    connection_alias: str,
    now: str,
) -> list[StagedCapture]:
    """Stage every item with no verifier. Origin and version stay unattested."""

    return [
        stage_import_item(
            item,
            request_id=request_id,
            connection_alias=connection_alias,
            now=now,
            index=index,
        )
        for index, item in enumerate(envelope.items)
    ]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _projected(source: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """One object rebuilt from exactly the public wire keys, in wire order."""

    return {key: source[key] for key in keys}


def _public_item(entry: StagedCapture) -> dict[str, Any]:
    """One staged item in the accepted public import wire shape.

    ``digest_material`` is the *internal* material the completion digest
    covers. Its normalized projection carries the generated action ``id``
    that :func:`workstack.capture._project_actions` mints for the stored
    record; the public import wire has no such field, and the released
    importer mints the identifier again from the same admitted action at
    explicit import, so carrying it here would only make the owner's closed
    parsers refuse a proposal their own server produced.
    """

    material = copy.deepcopy(entry.digest_material())
    item = _projected(material, _PUBLIC_ITEM_KEYS)
    normalized = _projected(item["normalized"], _PUBLIC_NORMALIZED_KEYS)
    normalized["action_items"] = [
        _projected(action, _PUBLIC_ACTION_KEYS)
        for action in normalized["action_items"]
    ]
    item["normalized"] = normalized
    return item


def _canonical_proposal(
    request_id: str, staged: Sequence[StagedCapture]
) -> dict[str, Any]:
    """Rebuild the closed public envelope from staging's admitted items.

    The 64 KiB bound is measured on this final public projection, after the
    per-level rebuild, so the octets counted are exactly the octets a caller
    would send on.
    """

    proposal = {
        "schema": IMPORT_SCHEMA,
        "request_id": request_id,
        "items": [_public_item(entry) for entry in staged],
    }
    if len(_canonical_bytes(proposal)) > MAX_PROPOSAL_BYTES:
        raise KnowledgeImportError("request_too_large")
    return proposal
