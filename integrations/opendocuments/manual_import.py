"""Compose one manual knowledge-import envelope from one mapped retrieval.

This is a pure composer over the released seams and nothing else. It calls the
existing :func:`integrations.opendocuments.retrieval_mapper.map_retrieval`
unedited, wraps the wire that mapper admitted in the released
``workstack.knowledge-import.v1`` envelope, and returns it. It opens no path
and no network, reads no clock, allocates no identifier, stores nothing and
logs nothing. Given the same admitted inputs it returns the same object.

**This is not an automated Adapter and it submits nothing.** The envelope it
returns is a document the owner still carries by hand into
``POST /api/v1/knowledge/captures/import`` under the browser session they
already have. Nothing here proves that the named request was issued, that it is
still open, or that the caller may complete it: the released importer remains
the sole authority on submission, and it answers those questions inside its own
transaction. No credential, provider, connection, path or verification is
representable in what this returns, because the released envelope has nowhere
to write one.

**What the item says, and what it does not.** The whole admitted retrieval wire
becomes exactly *one* item: the evidence set the mapper admitted is never split
across items, and mixed evidence is never relabelled to one source. The display
title is the mapper's own already-catalog-derived evidence title for a
single-source answer and the constant :data:`SYNTHESIZED_TITLE` otherwise;
``normalized.summary`` repeats that title and ``normalized.context`` is the
constant :data:`REVIEW_CONTEXT`. **No summary is synthesized and no action is
extracted**: ``action_items`` and ``tags`` are empty, and the chat's generated
``answer``, chunk ``content``, confidence ``reason``, ``sourcePath``, URLs,
query text and index keys are copied nowhere. The evidence stays unverified --
this lane adds no ``source_version`` and no origin -- and saying so is
documentation of a bounded slice, not a claim that the customer's job is done.

Refusals are closed codes. The mapper's own :class:`MappingError` propagates
unchanged, and this module's own :class:`ManualImportError` carries a code and
nothing else: no submitted value, title, identifier, path or traceback text
reaches ``code``, ``str``, ``repr`` or ``details``.
"""

from __future__ import annotations

import json
from typing import Any

from integrations.opendocuments.retrieval_mapper import MappingError, map_retrieval
from workstack.knowledge_request import KnowledgeRequestError, canonical_uuid

__all__ = [
    "IMPORT_SCHEMA",
    "MAX_IMPORT_BYTES",
    "ManualImportError",
    "REVIEW_CONTEXT",
    "SYNTHESIZED_TITLE",
    "build_manual_import",
]

# The released import envelope. It is spelled here rather than imported because
# this adapter package may not reach the importer's module: the value is part of
# the released contract, and the acceptance tests run the real
# ``parse_import_envelope`` over what this builds.
IMPORT_SCHEMA = "workstack.knowledge-import.v1"

# The released route's whole-body bound, 64 KiB of UTF-8. The retrieval wire
# keeps its own 16 KiB mapper bound inside this one.
MAX_IMPORT_BYTES = 64 * 1024

# The title of an answer drawn from more than one approved document. A
# multi-document answer has no honest single evidence title, and the first
# document's title is not it.
SYNTHESIZED_TITLE = "Search evidence"

# The whole context an unverified carried answer honestly supports.
REVIEW_CONTEXT = "Review the listed evidence before linking it to a task."


class ManualImportError(MappingError):
    """Closed composer refusal: a code, never a submitted value."""

    def __repr__(self) -> str:
        return "ManualImportError({!r})".format(self.code)


def build_manual_import(
    chat: Any,
    *,
    request_id: str,
    item_id: str,
    result_limit: int,
    source_catalog: Any,
) -> dict[str, Any]:
    """Return one ``workstack.knowledge-import.v1`` envelope, or raise.

    ``item_id`` is the caller's: the same value replays the same completion
    against the released importer, and a different value is a different
    completion identity. Nothing is allocated here.
    """

    item = _admit_item_id(item_id)
    wire = map_retrieval(
        chat,
        request_id=request_id,
        result_limit=result_limit,
        source_catalog=source_catalog,
    )
    title = _display_title(wire)
    envelope = {
        "schema": IMPORT_SCHEMA,
        "request_id": wire["request_id"],
        "items": [
            {
                "item_id": item,
                "title": title,
                "normalized": {
                    "summary": title,
                    "context": REVIEW_CONTEXT,
                    "action_items": [],
                    "tags": [],
                },
                "retrieval": wire,
            }
        ],
    }
    _require_within_body_budget(envelope)
    return envelope


def _admit_item_id(item_id: Any) -> str:
    try:
        return canonical_uuid(item_id, "item_id")
    except KnowledgeRequestError:
        raise ManualImportError("invalid_uuid") from None


def _display_title(wire: dict[str, Any]) -> str:
    """The mapper's own admitted title, or the constant for a mixed answer.

    A ``single_source`` wire names one approved document, so its first evidence
    title -- already derived from the caller's trusted catalog and already held
    to the released display-text gate -- is that answer's honest title. A
    ``synthesized`` wire names more than one, and no single document's title
    describes it.
    """

    if wire["answer_scope"] == "single_source":
        return str(wire["evidence"][0]["title"])
    return SYNTHESIZED_TITLE


def _require_within_body_budget(envelope: dict[str, Any]) -> None:
    """The released 64 KiB whole-body bound, over real compact UTF-8 octets.

    The mapper's 16 KiB wire plus one bounded title, its repeat as the summary
    and two constants cannot currently reach this bound, so this is a guard
    rather than a reachable refusal: it exists so a later bound change closes
    here instead of composing a body the released route would drop.
    """

    encoded = json.dumps(
        envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_IMPORT_BYTES:
        raise ManualImportError("import_too_large")
