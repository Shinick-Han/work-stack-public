"""Proposed OpenDocuments chat -> Work Stack evidence mapping.

This module is fixture-local. It is not a product API, not C1, and not a
frozen production schema. Unknown upstream fields are dropped. Unknown
adapter-request fields fail closed.

Revision notes (A0 re-review fixes):

* Upstream ``sourceType`` is never trusted to admit a ``sourcePath``. The
  locator class is derived from the path alone; a declared type that
  disagrees with the parsed path redacts the locator entirely.
* A candidate must carry a well-formed ``chunkId`` that belongs to the same
  indexed document. Heading hierarchy is descriptive index metadata and is
  never used as an address.
* The retriever tuple is labelled as *indexed* identity. Source-qualified
  identity stays unknown: ``/api/v1/chat`` cannot prove it.
* Every retained string is bounded to the limits declared by
  ``proposed-normalized.schema.json``.

Boundary-fix notes (independent A0-final re-review):

* URI schemes are case-insensitive, so the scheme is parsed exactly once and
  compared lowercased. Every spelling of ``http``/``https`` reaches the same
  credential and query handling; no spelling falls through to a display label.
* ``sourceType`` is reconciled against the declarations the frozen upstream
  actually produces. A nonempty declaration that cannot be reconciled with the
  parsed locator class redacts the locator instead of retaining it.
* Identity strings are matched against the whole string, and the chunk
  position is bounded before the candidate is constructed, so an accepted
  source cannot produce a schema-invalid document.
* Numeric normalization is exception-safe: a JSON-representable integer too
  large for a float yields a bounded score instead of raising.
"""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlsplit

FORMAT = "workstack.opendocuments-adapter.proposed"
SCHEMA_VERSION = 0
RETRIEVER_ID = "opendocuments"

MAX_EXCERPT_CHARS = 1200
MAX_TITLE_CHARS = 240
MAX_HEADING_SEGMENTS = 8
MAX_HEADING_CHARS = 120
MAX_QUERY_CHARS = 1000
MAX_CONNECTION_ID_CHARS = 128
MAX_SOURCE_TYPE_CHARS = 120
MAX_URI_CHARS = 512
MAX_ROUTE_CHARS = 64
MAX_REASON_CHARS = 256
MAX_VERSION_CHARS = 128
MAX_SOURCES = 100
# proposed-normalized.schema.json bounds locator.position at 999999999 and the
# chunk id suffix at nine digits. The mapper enforces the same ceiling before
# it builds a candidate.
MAX_CHUNK_POSITION = 999999999

# ``$`` also matches immediately before a trailing line feed, so every identity
# pattern is anchored with ``\A``/``\Z`` and compared against the whole string.
# ``[0-9]`` is used instead of ``\d`` so non-ASCII digits cannot reach ``int``.
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
UUID_RE = re.compile(rf"\A{_UUID}\Z", re.IGNORECASE)
CHUNK_ID_RE = re.compile(rf"\A({_UUID})_chunk_([0-9]{{1,9}})\Z", re.IGNORECASE)
# A document id that merely *looks* like a chunk id stays reportable as such
# even when its suffix is out of range, so the A0-2 confusion remains
# distinguishable from a plain malformed identifier.
CHUNK_ID_SHAPE_RE = re.compile(rf"\A{_UUID}_chunk_[0-9]+\Z", re.IGNORECASE)
NOTION_PAGE_RE = re.compile(rf"\A(?:[0-9a-f]{{32}}|{_UUID})\Z", re.IGNORECASE)
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
WINDOWS_DRIVE_ONLY_RE = re.compile(r"^[A-Za-z]:$")
CONTROL_RE = re.compile(r"[\0-\x08\x0b\x0c\x0e-\x1f\x7f]")
# An opaque connection identifier must carry no C0 control at all, including
# the tab, carriage return, and line feed that CONTROL_RE tolerates inside
# free text.
IDENTIFIER_CONTROL_RE = re.compile(r"[\0-\x1f\x7f]")
HOST_RE = re.compile(r"\A[a-z0-9]([a-z0-9\-.]{0,253}[a-z0-9])?\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
WEB_CHUNK_RE = re.compile(r"\Aweb_[0-9]+\Z", re.IGNORECASE)
# RFC 3986 scheme grammar. Schemes are case-insensitive, so the scheme is
# lowercased once here and never re-spelled downstream.
SCHEME_RE = re.compile(r"\A([A-Za-z][A-Za-z0-9+.\-]*):")
# ``scheme://userinfo@host`` in any spelling. A fail-closed guard for the
# schemes this fixture deliberately does not parse.
USERINFO_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9+.\-]*://[^/?#]*@")

# packages/core/src/rag/engine.ts merges web-search hits into the same
# sources[] array with a synthetic identity that is not an indexed
# OpenDocuments workspace document.
NON_WORKSPACE_DOCUMENT_IDS = frozenset({"web-search", "web_search", "websearch"})

CLOSED_REQUEST_KEYS = frozenset(
    {
        "request_id",
        "connection_id",
        "query",
        "collection_id",
        "conversation_id",
        "workspace_id",
    }
)

# Schemes whose identifier form this fixture cannot validate. The source
# system class is retained; the opaque remainder is dropped. Frozen upstream
# f3aba15f emits them from plugins/connector-{gdrive,confluence,github,s3,
# swagger}/src/index.ts.
OPAQUE_SCHEMES = frozenset({"gdrive", "confluence", "github", "s3", "gcs", "swagger"})
# Every scheme this mapping recognises, lowercased. Nothing here is inferred:
# each one is emitted by the frozen upstream. No further protocol is admitted.
KNOWN_SCHEMES = frozenset({"notion", "upload", "http", "https"}) | OPAQUE_SCHEMES

# The complete set of ``SearchResult.sourceType`` values the frozen upstream can
# produce, mapped to the locator classes such a declaration may legitimately
# carry. Anything outside this table is a declaration this fixture cannot
# reconcile, so its locator is redacted rather than retained.
DECLARED_SOURCE_TYPES: dict[str, frozenset[str]] = {
    # packages/cli/src/commands/index-cmd.ts:78,106 and
    # packages/server/src/mcp/server.ts:289 ingest local files by path. A bare
    # relative label is already reduced to a safe basename, so it cannot
    # disclose more than the local branch does.
    "local": frozenset({"local", "label"}),
    # packages/server/src/http/routes/documents.ts:84 -> upload:{hash}:{name}.
    "upload": frozenset({"upload"}),
    # packages/core/src/rag/engine.ts:427 synthesises web hits. They are
    # already omitted as non_workspace_result before this table is consulted.
    "web": frozenset({"url"}),
    # packages/core/src/connector/manager.ts:196-204 stores plugin.name.
    "@opendocuments/connector-notion": frozenset({"notion"}),
    "@opendocuments/connector-gdrive": frozenset({"opaque:gdrive"}),
    "@opendocuments/connector-confluence": frozenset({"opaque:confluence"}),
    "@opendocuments/connector-github": frozenset({"opaque:github"}),
    # plugins/connector-s3/src/index.ts:99,133 - one plugin, two providers.
    "@opendocuments/connector-s3": frozenset({"opaque:s3", "opaque:gcs"}),
    "@opendocuments/connector-swagger": frozenset({"opaque:swagger"}),
    # plugins/connector-web-crawler/src/index.ts:115 uses the crawled URL.
    "@opendocuments/connector-web-crawler": frozenset({"url"}),
    "@opendocuments/connector-web-search": frozenset({"url"}),
}


def map_opendocuments_chat(
    request: dict[str, Any],
    chat: dict[str, Any],
    documents: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map a synthetic /api/v1/chat body onto the proposed evidence shape."""
    refused = _refuse_request(request)
    if refused is not None:
        return refused

    chat_error = _refuse_chat(chat)
    if chat_error is not None:
        return chat_error

    documents = documents if isinstance(documents, dict) else {}
    candidates: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    sources = chat.get("sources")
    if not isinstance(sources, list):
        sources = []

    for index, source in enumerate(sources):
        mapped, omit_reason = _map_source(source, documents)
        if omit_reason is not None:
            omitted.append({"index": index, "reason": omit_reason})
            continue
        assert mapped is not None
        candidates.append(mapped)

    raw_confidence = chat.get("confidence")
    confidence = raw_confidence if isinstance(raw_confidence, dict) else {}
    result = {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "mapping_status": "proposed",
        "request_id": request["request_id"].lower(),
        "connection_id": request["connection_id"],
        "query": request["query"],
        "retriever": {
            "id": RETRIEVER_ID,
            "role": "retriever",
            "capabilities": ["search"],
            "unsupported": [
                "verify_origin",
                "open_origin",
                "collection_filter",
                "conversation_persistence",
                "source_document_identity",
            ],
        },
        "engine": {
            "query_id": chat["queryId"].lower(),
            "route": _single_line(chat.get("route"), MAX_ROUTE_CHARS) or "unknown",
            "profile": _single_line(chat.get("profile"), MAX_ROUTE_CHARS) or "balanced",
            "confidence": {
                "score": _bounded_score(confidence.get("score")),
                "level": _confidence_level(confidence.get("level")),
                "kind": "engine_relevance",
                "reason": _single_line(confidence.get("reason"), MAX_REASON_CHARS)
                or "unspecified",
            },
        },
        "generated_answer": "discarded",
        "candidates": candidates,
        "omitted": omitted,
        "limitations": {
            # chat.ts calls persistQueryLog on every successful non-stream
            # query and the adapter cannot suppress it, but persistQueryLog
            # swallows database errors, so persistence itself is not proven.
            "query_logging_attempted_by_route": True,
            "query_log_persistence_confirmed": False,
            "source_version_from_chat": False,
            "origin_verified": False,
            "collection_filter_applied": False,
            "source_document_identity_proven": False,
            "non_workspace_results_omitted": True,
        },
    }
    return {"ok": True, "result": result}


def _refuse_request(request: Any) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return _error("invalid_request", "request must be an object")
    extra = sorted(set(request) - CLOSED_REQUEST_KEYS)
    if extra:
        return _error("invalid_request", "unknown request fields are refused")
    if request.get("collection_id") is not None:
        return _error(
            "collection_filter_unsupported",
            "POST /api/v1/chat has no collection filter; one OpenDocuments workspace is one corpus",
        )
    if request.get("conversation_id") is not None:
        return _error(
            "conversation_persistence_out_of_scope",
            "first adapter omits conversationId so answers are not stored on the conversation",
        )
    request_id = request.get("request_id")
    connection_id = request.get("connection_id")
    query = request.get("query")
    if not isinstance(request_id, str) or not UUID_RE.match(request_id):
        return _error("invalid_request", "request_id must be a UUID")
    if not isinstance(connection_id, str) or not connection_id:
        return _error("invalid_request", "connection_id is required")
    if (
        connection_id != connection_id.strip()
        or IDENTIFIER_CONTROL_RE.search(connection_id)
        or len(connection_id) > MAX_CONNECTION_ID_CHARS
    ):
        return _error(
            "invalid_request",
            "connection_id must be trimmed, control-free, and at most "
            f"{MAX_CONNECTION_ID_CHARS} characters",
        )
    if not isinstance(query, str) or not query.strip() or query != query.strip():
        return _error("invalid_request", "query must be a nonempty trimmed string")
    if len(query) > MAX_QUERY_CHARS or CONTROL_RE.search(query):
        return _error("invalid_request", "query is empty, too long, or contains controls")
    if "workspace_id" in request:
        return _error(
            "invalid_request",
            "adapter workspace binding is connection config, not a chat collection/workspace override",
        )
    return None


def _refuse_chat(chat: Any) -> dict[str, Any] | None:
    if not isinstance(chat, dict):
        return _error("invalid_chat_result", "chat result must be an object")
    query_id = chat.get("queryId")
    if not isinstance(query_id, str) or not UUID_RE.match(query_id):
        return _error("invalid_chat_result", "queryId must be a UUID")
    sources = chat.get("sources")
    if "sources" in chat and not isinstance(sources, list):
        return _error("invalid_chat_result", "sources must be an array")
    if isinstance(sources, list) and len(sources) > MAX_SOURCES:
        return _error(
            "invalid_chat_result", f"at most {MAX_SOURCES} sources are mapped"
        )
    return None


def _map_source(
    source: Any,
    documents: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(source, dict):
        return None, "invalid_source"

    document_id = source.get("documentId")
    if not isinstance(document_id, str) or not document_id:
        return None, "missing_document_id"
    if CHUNK_ID_SHAPE_RE.match(document_id):
        return None, "chunk_id_used_as_document_id"

    chunk_id_raw = source.get("chunkId")
    # A0-6: web-search hits merged by the RAG engine are not workspace
    # documents and are omitted with a distinct reason.
    if document_id.lower() in NON_WORKSPACE_DOCUMENT_IDS or (
        isinstance(chunk_id_raw, str) and WEB_CHUNK_RE.match(chunk_id_raw)
    ):
        return None, "non_workspace_result"
    if not UUID_RE.match(document_id):
        return None, "invalid_document_id"
    document_id = document_id.lower()

    # A0-2: a well-formed, document-matching indexed chunk id is the only
    # address this mapping accepts. Heading hierarchy is not a unique or
    # stable source locator and must not make a hit attachable.
    if chunk_id_raw is None:
        return None, "missing_chunk_id"
    if not isinstance(chunk_id_raw, str):
        return None, "invalid_chunk_id"
    match = CHUNK_ID_RE.match(chunk_id_raw)
    if match is None:
        return None, "invalid_chunk_id"
    if match.group(1).lower() != document_id:
        return None, "mismatched_chunk_id"
    chunk_position = int(match.group(2))
    # A0-4: the ceiling is checked before the candidate exists, so an accepted
    # source can never carry a position the closed schema would reject. The
    # nine-digit suffix bound in CHUNK_ID_RE already implies this; the explicit
    # check keeps the invariant true if that pattern is ever widened.
    if chunk_position > MAX_CHUNK_POSITION:
        return None, "invalid_chunk_id"
    chunk_id = f"{document_id}_chunk_{chunk_position}"

    content = source.get("content")
    if not isinstance(content, str) or content == "":
        return None, "missing_content"
    excerpt, truncated, sanitized = _clip_excerpt(content)
    if excerpt == "":
        return None, "missing_content"

    source_path = source.get("sourcePath")
    source_type = source.get("sourceType")
    raw_metadata = documents.get(document_id)
    metadata = raw_metadata if isinstance(raw_metadata, dict) else None
    source_kind, source_locator, agreement = _source_locator(source_type, source_path)
    heading_path = _heading_path(source)
    title = _title(metadata, heading_path, source_locator)
    version = _version_from_metadata(metadata)

    candidate = {
        # A0-3: this tuple is the OpenDocuments *index* identity. It is not
        # the Notion page identity and not the NAS file identity.
        "indexed_identity": {
            "retriever_id": RETRIEVER_ID,
            "connection_id": None,  # filled by bind_connection
            "indexed_document_id": document_id,
            "indexed_chunk_id": chunk_id,
            "identity_scope": "opendocuments_index",
        },
        "source": {
            "kind": source_kind,
            "declared_source_type": _single_line(source_type, MAX_SOURCE_TYPE_CHARS)
            or "unknown",
            "type_locator_agreement": agreement,
            "locator": source_locator,
            "identity": {
                "status": "unknown",
                "value": None,
                "basis": "chat_response_cannot_prove_source_identity",
            },
        },
        "title": title,
        "locator": {
            "type": "chunk_index",
            "position": chunk_position,
            "index_chunk_id": chunk_id,
            "source_locator": False,
        },
        "index_heading_path": {"segments": heading_path, "addressable": False},
        "excerpt": excerpt,
        "excerpt_truncated": truncated,
        "excerpt_sanitized": sanitized,
        "engine_score": _bounded_score(source.get("score")),
        "source_version": version["source_version"],
        "content_hash": version["content_hash"],
        "verification": {
            "status": "indexed_snapshot_unverified",
            "checked_at": None,
            "origin_current": False,
        },
        "trust": "external_reference",
        "read_only": True,
    }
    return candidate, None


def bind_connection(result: dict[str, Any], connection_id: str) -> dict[str, Any]:
    for candidate in result["candidates"]:
        candidate["indexed_identity"]["connection_id"] = connection_id
    return result


def map_opendocuments_chat_bound(
    request: dict[str, Any],
    chat: dict[str, Any],
    documents: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mapped = map_opendocuments_chat(request, chat, documents)
    if not mapped.get("ok"):
        return mapped
    mapped["result"] = bind_connection(mapped["result"], request["connection_id"])
    return mapped


def _redacted(reason: str) -> dict[str, Any]:
    return {"type": "redacted", "value": None, "reason": reason}


def _source_locator(
    source_type: Any,
    source_path: Any,
) -> tuple[str, dict[str, Any], str]:
    """Derive the source locator from the path alone, then reconcile it.

    A0-1: ``sourceType`` never admits a ``sourcePath``. The locator class comes
    from the parsed path; the declaration can only *lose* information:

    * no declaration at all -> ``unverified``, the parsed locator is kept;
    * a declaration the frozen upstream can emit -> ``agree`` when the parsed
      class is one it may carry, otherwise ``mismatch`` and a redacted locator;
    * any other nonempty declaration -> ``unverified`` and a redacted locator,
      because this fixture cannot reconcile it and must not guess.
    """
    if not isinstance(source_path, str) or not source_path:
        path_kind, locator = "none", _redacted("no_source_path")
    else:
        safe_path = _single_line(source_path, MAX_URI_CHARS)
        if safe_path is None or safe_path != source_path.strip():
            path_kind, locator = "none", _redacted("unsafe_source_path")
        else:
            path_kind, locator = _parse_source_path(safe_path)

    declaration = _single_line(source_type, MAX_SOURCE_TYPE_CHARS)
    if declaration is None:
        return _display_kind(path_kind), locator, "unverified"

    allowed = DECLARED_SOURCE_TYPES.get(declaration.lower())
    if allowed is None:
        # An unknown declaration cannot be reconciled with anything. Keep an
        # already fail-closed reason; otherwise redact conservatively.
        if locator["type"] == "redacted":
            return "unknown", locator, "unverified"
        return "unknown", _redacted("unreconcilable_declared_source_type"), "unverified"

    if path_kind == "none":
        # There is no parsed class to disagree with, and the locator is
        # already redacted, so nothing can leak either way.
        return "unknown", locator, "unverified"

    if path_kind not in allowed:
        return "unknown", _redacted("source_type_locator_mismatch"), "mismatch"
    return _display_kind(path_kind), locator, "agree"


def _scheme_of(path: str) -> str | None:
    """Return the lowercased scheme when it is one this mapping recognises."""
    match = SCHEME_RE.match(path)
    if match is None:
        return None
    scheme = match.group(1).lower()
    return scheme if scheme in KNOWN_SCHEMES else None


def _parse_source_path(path: str) -> tuple[str, dict[str, Any]]:
    # A0-1: the scheme is parsed once, case-insensitively. Dispatch never
    # depends on the spelling, and no known scheme can fall through to the
    # display-label branch below.
    scheme = _scheme_of(path)
    remainder = path[path.index(":") + 1 :] if scheme is not None else path

    if scheme == "notion":
        page_id = remainder[2:] if remainder.startswith("//") else remainder
        if not NOTION_PAGE_RE.match(page_id):
            return "notion", _redacted("malformed_notion_page_id")
        return "notion", {
            "type": "uri",
            "value": f"notion://{page_id.lower()}",
            "reason": None,
        }

    if scheme == "upload":
        return "upload", {
            "type": "display_label",
            "value": _safe_basename(remainder.rsplit(":", 1)[-1]),
            "reason": None,
        }

    if scheme in ("http", "https"):
        # Every spelling, with or without an authority, goes through the same
        # credential and query handling.
        return "url", _url_locator(path)

    if scheme in OPAQUE_SCHEMES:
        return f"opaque:{scheme}", {
            "type": "opaque",
            "value": scheme,
            "reason": "opaque_scheme_identifier_dropped",
        }

    if USERINFO_RE.match(path):
        # An unrecognised scheme that embeds userinfo is not parsed further;
        # a basename of such a URI would still be attacker-chosen text.
        return "label", _redacted("credentialed_url")

    if looks_like_filesystem_path(path):
        return "local", {
            "type": "display_label",
            "value": _safe_basename(path),
            "reason": None,
        }

    return "label", {
        "type": "display_label",
        "value": _safe_basename(path),
        "reason": None,
    }


def _url_locator(path: str) -> dict[str, Any]:
    try:
        parts = urlsplit(path)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return _redacted("unparsable_url")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        # A0-1: never retain a credential-bearing URL.
        return _redacted("credentialed_url")
    if not host or not HOST_RE.match(host):
        return _redacted("unsupported_url_host")
    value = f"{parts.scheme}://{host}"
    if port is not None:
        value += f":{port}"
    value += parts.path or "/"
    if len(value) > MAX_URI_CHARS:
        return _redacted("url_too_long")
    # Query and fragment routinely carry api keys or tokens; drop them.
    reason = "url_query_and_fragment_dropped" if (parts.query or parts.fragment) else None
    return {"type": "uri", "value": value, "reason": reason}


def _display_kind(path_kind: str) -> str:
    if path_kind.startswith("opaque:"):
        return path_kind.split(":", 1)[1]
    if path_kind in {"label", "none"}:
        return "unknown"
    return path_kind


def looks_like_filesystem_path(value: str) -> bool:
    if not value:
        return False
    # A recognised scheme is handled by _parse_source_path in any spelling and
    # is never re-read as a path. ``C:\...`` is not a recognised scheme.
    if _scheme_of(value) is not None:
        return False
    if value.startswith("\\\\") or value.startswith("//"):
        return True
    if WINDOWS_DRIVE_RE.match(value):
        return True
    if value.startswith("/"):
        return True
    parts = value.replace("\\", "/").split("/")
    if ".." in parts:
        return True
    return "\\" in value


def _safe_basename(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    if ".." in parts or any(part in {".", ".."} for part in parts[-1:]):
        return "document"
    cleaned = path.replace("\\", "/").rstrip("/")
    name = cleaned.split("/")[-1] if cleaned else "document"
    if WINDOWS_DRIVE_RE.match(name) or WINDOWS_DRIVE_ONLY_RE.match(name):
        return "document"
    name = _single_line(name, MAX_TITLE_CHARS) or "document"
    if name in {".", ".."} or "\\" in name or "/" in name:
        return "document"
    return name


def _heading_path(source: dict[str, Any]) -> list[str]:
    headings = source.get("headingHierarchy")
    path: list[str] = []
    if isinstance(headings, list):
        for item in headings[:MAX_HEADING_SEGMENTS]:
            label = _single_line(item, MAX_HEADING_CHARS)
            if label:
                path.append(label)
    return path


def _title(
    metadata: dict[str, Any] | None,
    heading_path: list[str],
    source_locator: dict[str, Any],
) -> str:
    if metadata is not None:
        label = _single_line(metadata.get("title"), MAX_TITLE_CHARS)
        if label:
            return label
    if heading_path:
        return heading_path[-1][:MAX_TITLE_CHARS]
    value = source_locator.get("value")
    if source_locator.get("type") == "display_label" and isinstance(value, str) and value:
        return value[:MAX_TITLE_CHARS]
    return "Untitled"


def _version_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        unavailable = {"status": "unavailable", "value": None, "basis": "chat_response"}
        return {"source_version": dict(unavailable), "content_hash": dict(unavailable)}

    mapped_version = {"status": "unavailable", "value": None, "basis": "document_read"}
    mapped_hash = {"status": "unavailable", "value": None, "basis": "document_read"}

    revision = _single_line(metadata.get("source_version"), MAX_VERSION_CHARS)
    if revision:
        mapped_version = {
            "status": "index_revision",
            "value": revision,
            "basis": "document_read",
        }

    content_hash = metadata.get("content_hash")
    if isinstance(content_hash, str) and SHA256_RE.match(content_hash):
        mapped_hash = {
            "status": "index_hash",
            "value": content_hash,
            "basis": "document_read",
        }
    return {"source_version": mapped_version, "content_hash": mapped_hash}


def _clip_excerpt(content: str) -> tuple[str, bool, bool]:
    """Return (excerpt, truncated, sanitized).

    ``truncated`` means the length bound removed trailing text. ``sanitized``
    means control characters were stripped before bounding.
    """
    text = CONTROL_RE.sub("", content.replace("\r\n", "\n"))
    sanitized = len(text) != len(content)
    if len(text) <= MAX_EXCERPT_CHARS:
        return text, False, sanitized
    return text[:MAX_EXCERPT_CHARS], True, sanitized


def _bounded_score(value: Any) -> float:
    """Normalise an engine score to [0, 1] without ever raising.

    A0-4: JSON has no integer ceiling, so ``float()`` on a decoded score can
    raise ``OverflowError``. Integers are clamped without ever reaching
    ``float()``; anything else is converted defensively. A score is advisory
    metadata, so an unusable one becomes 0.0 rather than aborting the mapping.
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int):
        # Exact, and immune to the float range: an integer is only ever 0 or
        # 1 inside the bound, so it never reaches float().
        return 0.0 if value <= 0 else 1.0
    if not isinstance(value, float):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _confidence_level(value: Any) -> str:
    if value in {"high", "medium", "low", "none"}:
        return value
    return "none"


def _single_line(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.replace("\r", " ").replace("\n", " ").strip()
    if not text or CONTROL_RE.search(text):
        return None
    return text[:limit]


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message[:MAX_REASON_CHARS]}}
