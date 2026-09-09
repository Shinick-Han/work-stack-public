"""Map an OpenDocuments chat body onto Capture retrieval extension v1.1.

This is a pure adapter mapper. It does not open a network, persist, log, call
an LLM, or convert source text into Task actions. Admission comes only from a
caller-supplied trusted catalog keyed by upstream ``documentId``. Chat
``sourcePath``, generated ``answer``, chunk ``content`` and confidence
``reason`` are ignored. Source versions stay absent: this lane does not wire a
verifier.

The returned object is the closed *wire* document. Derived projection fields
(``origin``, ``origin_state``, ``reported_origin``, ``capture_source_type``,
``version_state``) are not emitted here. The existing
:func:`workstack.capture_retrieval.validate_retrieval_payload` is the final
check; this module does not copy that validator's security heuristics.
"""

from __future__ import annotations

import json
from typing import Any

from integrations.opendocuments.retrieval_mapper_fields import (
    EvidenceDraft,
    MappingError,
    admit_catalog,
    admit_result_limit,
    answer_scope_for,
    collect_evidence,
    evidence_item_wire,
    read_chat_envelope,
    retains_mixed_documents,
)
from workstack.capture_retrieval import (
    CAPTURE_SCHEMA_VERSION,
    MAX_EXTENSION_BYTES,
    SCHEMA,
    CaptureRetrievalError,
    validate_retrieval_payload,
)
from workstack.knowledge_request import KnowledgeRequestError, canonical_uuid

__all__ = ["MappingError", "map_retrieval"]


def map_retrieval(
    chat: Any,
    *,
    request_id: str,
    result_limit: int,
    source_catalog: Any,
) -> dict[str, Any]:
    """Return a closed v1.1 retrieval extension, or raise ``MappingError``."""

    catalog = admit_catalog(source_catalog)
    bound_request = _admit_request_id(request_id)
    limit = admit_result_limit(result_limit)
    envelope = read_chat_envelope(chat)
    if envelope.query_id == bound_request:
        raise MappingError("query_id_not_distinct")
    admitted = collect_evidence(envelope.sources, catalog, limit)
    if not admitted.kept:
        raise MappingError("no_admitted_evidence")
    _require_representable_mix(admitted.mixed, admitted.kept)
    document = _fit_to_byte_budget(
        request_id=bound_request,
        query_id=envelope.query_id,
        level=envelope.confidence_level,
        score=envelope.confidence_score,
        drafts=admitted.kept,
        truncated=admitted.truncated,
        mixed=admitted.mixed,
    )
    return _seal_extension(document, bound_request)


def _require_representable_mix(mixed: bool, items: list[EvidenceDraft]) -> None:
    """Refuse rather than let a cap relabel mixed evidence as one source.

    The caps themselves are unchanged. When admission saw more than one
    approved document but the surviving selection no longer does, there is no
    honest ``answer_scope`` left to emit: ``synthesized`` would overstate the
    retained evidence and ``single_source`` would attribute the answer to the
    first document. Neither a second source nor a different selection is
    invented here; the mapper closes.
    """

    if mixed and not retains_mixed_documents(items):
        raise MappingError("mixed_evidence_not_representable")


def _admit_request_id(request_id: Any) -> str:
    try:
        return canonical_uuid(request_id, "request_id")
    except KnowledgeRequestError:
        raise MappingError("invalid_uuid") from None


def _assemble_extension(
    *,
    request_id: str,
    query_id: str,
    level: str,
    score: int | float,
    drafts: list[EvidenceDraft],
    truncated: bool,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "capture_schema_version": CAPTURE_SCHEMA_VERSION,
        "request_id": request_id,
        "query_id": query_id,
        "answer_scope": answer_scope_for(drafts),
        "confidence": {"level": level, "score": score},
        "evidence": [evidence_item_wire(draft) for draft in drafts],
        "truncated": truncated,
    }


def _encode_extension(document: dict[str, Any]) -> bytes:
    try:
        text = json.dumps(
            document, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError):
        raise MappingError("invalid_chat") from None
    return text.encode("utf-8")


def _fit_to_byte_budget(
    *,
    request_id: str,
    query_id: str,
    level: str,
    score: int | float,
    drafts: list[EvidenceDraft],
    truncated: bool,
    mixed: bool,
) -> dict[str, Any]:
    items = list(drafts)
    dropped = False
    while True:
        document = _assemble_extension(
            request_id=request_id,
            query_id=query_id,
            level=level,
            score=score,
            drafts=items,
            truncated=truncated or dropped,
        )
        raw = _encode_extension(document)
        if len(raw) <= MAX_EXTENSION_BYTES:
            return document
        if len(items) <= 1:
            raise MappingError("extension_too_large")
        items.pop()
        dropped = True
        _require_representable_mix(mixed, items)


def _seal_extension(document: dict[str, Any], request_id: str) -> dict[str, Any]:
    raw = _encode_extension(document)
    try:
        validate_retrieval_payload(raw, request_id=request_id)
    except CaptureRetrievalError as error:
        raise MappingError(error.code) from None
    except KnowledgeRequestError as error:
        raise MappingError(error.code) from None
    return document
