"""Bounded field helpers for the OpenDocuments retrieval mapper.

Chat ``sourcePath``, ``content``, ``answer`` and ``reason`` are never copied
into the wire document. Catalog admission is the only source of
``document_ref``, ``source_type`` and display titles. Chunk refs are derived
from upstream document+chunk identity after a belongs-to-document check.
This module does not import Capture title/path heuristics.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, NamedTuple

CATALOG_FIELDS = frozenset({"document_ref", "source_type", "display_title"})
ADMITTED_SOURCE_TYPES = frozenset({"notion.page", "nas.file"})
CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})
KNOWN_WEB_DOCUMENT_IDS = frozenset({"web-search", "web_search", "websearch"})
MAX_CATALOG_ENTRIES = 64
MAX_CHAT_SOURCES = 100
MAX_RESULT_LIMIT = 10
MIN_RESULT_LIMIT = 1

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CHUNK_ID_RE = re.compile(rf"\A({_UUID})_chunk_([0-9]{{1,9}})\Z", re.IGNORECASE)
WEB_CHUNK_RE = re.compile(r"\Aweb_[0-9]+\Z", re.IGNORECASE)


class MappingError(ValueError):
    """Closed mapper refusal: a code, never a submitted value."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code:
            code = "invalid_mapping"
        super().__init__(code)
        self.code = code

    def __repr__(self) -> str:
        return "MappingError({!r})".format(self.code)

    def __str__(self) -> str:
        return self.code

    @property
    def details(self) -> dict[str, str]:
        return {}


class CatalogEntry(NamedTuple):
    document_ref: str
    source_type: str
    display_title: str


class ChatEnvelope(NamedTuple):
    query_id: str
    confidence_level: str
    confidence_score: int | float
    sources: list[Any]


class EvidenceDraft(NamedTuple):
    source_type: str
    title: str
    document_ref: str
    chunk_ref: str


class AdmittedEvidence(NamedTuple):
    """Result of admission, before any byte pruning.

    ``mixed`` is decided over *every* admitted draft, including the ones the
    result cap dropped, so a cap can never turn multi-document evidence into
    first-source attribution. Omitted untrusted, web and out-of-catalog hits
    are not admitted and never contribute a second document identity.
    """

    kept: list["EvidenceDraft"]
    truncated: bool
    mixed: bool


def admit_catalog(source_catalog: Any) -> dict[str, CatalogEntry]:
    """Copy a nonempty closed catalog. Extra keys and empty maps fail closed."""

    _require_catalog_mapping(source_catalog)
    admitted: dict[str, CatalogEntry] = {}
    for key, value in source_catalog.items():
        admitted[_catalog_key(key)] = _catalog_entry(value)
    return admitted


def _require_catalog_mapping(source_catalog: Any) -> None:
    if isinstance(source_catalog, Mapping):
        if 1 <= len(source_catalog) <= MAX_CATALOG_ENTRIES:
            return
    raise MappingError("invalid_catalog")


def _catalog_key(key: Any) -> str:
    if isinstance(key, str) and key:
        return key
    raise MappingError("invalid_catalog")


def _catalog_entry(value: Any) -> CatalogEntry:
    if not isinstance(value, Mapping):
        raise MappingError("invalid_catalog")
    if set(value) != CATALOG_FIELDS:
        raise MappingError("invalid_catalog")
    document_ref = value["document_ref"]
    source_type = value["source_type"]
    display_title = value["display_title"]
    if not isinstance(document_ref, str) or not document_ref:
        raise MappingError("invalid_catalog")
    if source_type not in ADMITTED_SOURCE_TYPES:
        raise MappingError("invalid_catalog")
    if not isinstance(display_title, str) or not display_title:
        raise MappingError("invalid_catalog")
    return CatalogEntry(document_ref, source_type, display_title)


def admit_result_limit(result_limit: Any) -> int:
    if type(result_limit) is not int:
        raise MappingError("invalid_result_limit")
    if MIN_RESULT_LIMIT <= result_limit <= MAX_RESULT_LIMIT:
        return result_limit
    raise MappingError("invalid_result_limit")


def read_chat_envelope(chat: Any) -> ChatEnvelope:
    """Read query id, confidence and sources. Ignore answer, path and reason."""

    if not isinstance(chat, Mapping):
        raise MappingError("invalid_chat")
    sources = chat.get("sources")
    if not isinstance(sources, list) or len(sources) > MAX_CHAT_SOURCES:
        raise MappingError("invalid_chat")
    query_id = chat.get("queryId")
    if not isinstance(query_id, str) or not query_id:
        raise MappingError("invalid_query_id")
    level, score = read_confidence(chat.get("confidence"))
    return ChatEnvelope(query_id, level, score, sources)


def read_confidence(value: Any) -> tuple[str, int | float]:
    """Require the engine's own bounded level/score. Never invent a default."""

    if not isinstance(value, Mapping):
        raise MappingError("invalid_confidence")
    level = value.get("level")
    score = value.get("score")
    if level not in CONFIDENCE_LEVELS:
        raise MappingError("invalid_confidence")
    if type(score) is bool:
        raise MappingError("invalid_confidence")
    if type(score) is int:
        if 0 <= score <= 1:
            return str(level), score
        raise MappingError("invalid_confidence")
    if type(score) is float and math.isfinite(score) and 0.0 <= score <= 1.0:
        return str(level), score
    raise MappingError("invalid_confidence")


def is_known_web(document_id: Any, chunk_id: Any) -> bool:
    if isinstance(document_id, str) and document_id.casefold() in KNOWN_WEB_DOCUMENT_IDS:
        return True
    return isinstance(chunk_id, str) and WEB_CHUNK_RE.fullmatch(chunk_id) is not None


def derive_chunk_ref(document_id: Any, chunk_id: Any) -> str | None:
    """Stable opaque chunk handle, or None when the chunk is not this document's."""

    if not isinstance(document_id, str) or not isinstance(chunk_id, str):
        return None
    match = CHUNK_ID_RE.fullmatch(chunk_id)
    if match is None:
        return None
    if match.group(1).casefold() != document_id.casefold():
        return None
    return "{}_chunk_{}".format(match.group(1).lower(), match.group(2))


def classify_source(item: Any, catalog: dict[str, CatalogEntry]) -> EvidenceDraft | None:
    if not isinstance(item, Mapping):
        raise MappingError("invalid_chat")
    document_id = item.get("documentId")
    chunk_id = item.get("chunkId")
    if is_known_web(document_id, chunk_id):
        return None
    if not isinstance(document_id, str):
        return None
    entry = catalog.get(document_id)
    if entry is None:
        return None
    chunk_ref = derive_chunk_ref(document_id, chunk_id)
    if chunk_ref is None:
        return None
    return EvidenceDraft(
        entry.source_type, entry.display_title, entry.document_ref, chunk_ref
    )


def collect_evidence(
    sources: list[Any], catalog: dict[str, CatalogEntry], limit: int
) -> AdmittedEvidence:
    """Preserve source order. Dedup and cap. Report genuine omission.

    Selection is unchanged: stable source order, first occurrence wins, the
    caller's cap decides how many survive. Only the ``mixed`` observation is
    additional, and it is read before the cap is applied.
    """

    seen: set[tuple[str, str]] = set()
    documents: set[str] = set()
    kept: list[EvidenceDraft] = []
    omitted = False
    overflow = False
    for item in sources:
        draft = classify_source(item, catalog)
        if draft is None:
            omitted = True
            continue
        documents.add(draft.document_ref)
        key = (draft.document_ref, draft.chunk_ref)
        if key in seen:
            omitted = True
            continue
        seen.add(key)
        if len(kept) >= limit:
            overflow = True
            continue
        kept.append(draft)
    return AdmittedEvidence(kept, omitted or overflow, len(documents) > 1)


def distinct_documents(items: list[EvidenceDraft]) -> int:
    return len({item.document_ref for item in items})


def retains_mixed_documents(items: list[EvidenceDraft]) -> bool:
    """True when at least two approved document identities survive pruning."""

    return distinct_documents(items) > 1


def answer_scope_for(items: list[EvidenceDraft]) -> str:
    if distinct_documents(items) == 1:
        return "single_source"
    return "synthesized"


def evidence_item_wire(draft: EvidenceDraft) -> dict[str, Any]:
    return {
        "source_type": draft.source_type,
        "title": draft.title,
        "document_ref": draft.document_ref,
        "chunk_ref": draft.chunk_ref,
        "source_version": None,
        "web_url": None,
    }
