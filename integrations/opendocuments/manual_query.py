"""Join the released chat transport to the frozen manual-import composer.

One call, at most one upstream ``POST /api/v1/chat``. This module owns no
policy of its own: it admits the structural inputs with the *existing*
admission helpers, hands the query to the released
:func:`integrations.opendocuments.od_client.post_opendocuments_chat` unedited,
and hands the body that transport returned to the released
:func:`integrations.opendocuments.manual_import.build_manual_import` unedited.
It adds no title rule, no evidence rule, no origin rule and no retry.

**The caller is a trusted internal operator seam, not a wire boundary.** The
query reaching :func:`run_reviewed_query` is one a human operator already
reviewed, and ``config`` and ``source_catalog`` are pinned from operator state.
Nothing here authenticates anybody, and exposing this function on a network
port would publish an unauthenticated OpenDocuments query relay. A catalog can
never be derived from a chat body: admission comes only from the operator's own
trusted map, which is why it is a required argument.

**No authority is asserted and none is minted.** This does not prove that
``request_id`` names an owner-issued knowledge request, that the request is
still open, or that the caller may complete it. Those questions belong to the
released importer, which answers them inside its own transaction when the owner
carries the returned envelope into
``POST /api/v1/knowledge/captures/import``. This module mints no identifier,
opens no path, persists nothing, submits nothing and logs nothing.

**Exactly-once is not claimed.** One invocation issues at most one upstream
POST and never retries it. A second invocation is a second POST; nothing here
survives a restart, so there is no across-restart exactly-once guarantee. When
the transport cannot tell whether the upstream saw the request it answers
``outcome_unknown``, and that answer is returned *unchanged* -- this module
never re-issues, re-composes or reclassifies it.

**The query is never returned and never recorded.** It reaches the transport
and nothing else: it is not copied into the envelope, into a refusal message,
or into any object this module returns. Refusals are closed codes, and no chat
body, response byte, API key or config value reaches ``message``.
"""

from __future__ import annotations

from typing import Any

from integrations.opendocuments.manual_import import build_manual_import
from integrations.opendocuments.od_client import post_opendocuments_chat
from integrations.opendocuments.retrieval_mapper_fields import (
    MappingError,
    admit_catalog,
    admit_result_limit,
)
from workstack.knowledge_request import KnowledgeRequestError, canonical_uuid

__all__ = [
    "COMPOSE_REFUSED_MESSAGE",
    "REVIEWED_QUERY_OUTCOME",
    "run_reviewed_query",
]

# The one success outcome this seam reports: a composed envelope, not a
# submitted import and not a verified answer.
REVIEWED_QUERY_OUTCOME = "composed"

# The single message every compose-stage refusal carries. The closed code says
# which rule closed; the message is a constant so no admitted value, chat text,
# title, identifier or key can ride out on it.
COMPOSE_REFUSED_MESSAGE = "the retrieval could not be composed"


def run_reviewed_query(
    query: str,
    *,
    config: Any,
    request_id: str,
    item_id: str,
    result_limit: int,
    source_catalog: Any,
) -> dict[str, Any]:
    """Ask the pinned backend one reviewed query and compose one envelope.

    Returns ``{"ok": True, "outcome": "composed", "envelope": {...}}`` with the
    released ``workstack.knowledge-import.v1`` envelope, or a closed refusal
    ``{"ok": False, "error": {"code": ..., "message": ...}}``.

    A transport refusal is the transport's own object, returned byte-for-byte:
    its ``code`` is one of :data:`od_client.ERROR_CODES`, and ``outcome_unknown``
    in particular passes through untouched. A compose refusal carries the
    mapper's or composer's own closed code with
    :data:`COMPOSE_REFUSED_MESSAGE`.

    ``query`` is validated by the transport's own bound, and ``config`` by the
    transport's own trusted-config binding; neither policy is restated here.
    The structural arguments are admitted *before* the network by the same
    ``canonical_uuid``, ``admit_result_limit`` and ``admit_catalog`` the
    composer will run again, so a malformed one costs zero upstream requests.
    """

    refused = _admit_structural_inputs(
        request_id=request_id,
        item_id=item_id,
        result_limit=result_limit,
        source_catalog=source_catalog,
    )
    if refused is not None:
        return refused
    # The only upstream request. There is no loop and no second call site.
    result = post_opendocuments_chat(query, config)
    if result.get("ok") is not True:
        return result
    # The released success shape. A missing or already-taken response is a
    # contract break in the transport, not a runtime outcome, so it is left to
    # raise rather than being flattened into a guessed refusal code.
    body = result["response"].take_for_mapper()
    try:
        envelope = build_manual_import(
            body,
            request_id=request_id,
            item_id=item_id,
            result_limit=result_limit,
            source_catalog=source_catalog,
        )
    except MappingError as error:
        # The composer and the mapper below it refuse with a closed code. The
        # POST already happened and is not re-issued.
        return _compose_refusal(error.code)
    return {"ok": True, "outcome": REVIEWED_QUERY_OUTCOME, "envelope": envelope}


def _admit_structural_inputs(
    *,
    request_id: Any,
    item_id: Any,
    result_limit: Any,
    source_catalog: Any,
) -> dict[str, Any] | None:
    """Run the existing admission helpers before any socket is opened.

    These are the composer's own helpers, called with the caller's own values.
    Nothing is normalised or carried forward: :func:`build_manual_import` runs
    them again over the same arguments and owns the admitted results.
    """

    try:
        canonical_uuid(request_id, "request_id")
        canonical_uuid(item_id, "item_id")
        admit_result_limit(result_limit)
        admit_catalog(source_catalog)
    except KnowledgeRequestError as error:
        return _compose_refusal(error.code)
    except MappingError as error:
        return _compose_refusal(error.code)
    return None


def _compose_refusal(code: Any) -> dict[str, Any]:
    if not isinstance(code, str) or not code:
        code = "invalid_mapping"
    return {"ok": False, "error": {"code": code, "message": COMPOSE_REFUSED_MESSAGE}}
