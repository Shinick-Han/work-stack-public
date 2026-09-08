"""Transport adapter for authoritative report documents (§13 wire shapes).

This is batch B2 of `docs/REPORT-DOCUMENT-STORAGE-CONTRACT.md` §15: the HTTP
surface over the already-released report service, and nothing else. It decodes
a query, names an operation, hands the raw admitted body to the activated
`WorkStack` report surfaces, and turns the one refusal taxonomy those surfaces
raise into a status and a closed error envelope. It holds no transaction,
opens no document, computes no digest, allocates no identity, reads no clock
and keeps no state between requests.

What this module deliberately does **not** do, because the service already
does it and doing it twice would be two answers to one question:

- no idempotency ledger, no replay decision and no receipt. The report
  service keeps its own immutable receipts (§9.2); this adapter never routes a
  report through `WorkStack`'s generic `_idempotency_replay`, whose recorded
  status is not returned as recorded and whose `response_ref` branch
  re-projects rather than replays. The `Idempotency-Key` header is admitted by
  the shipped `server.py::_idempotency_key` — the same enforcement every other
  writer gets, which §13 names — and then passed through verbatim;
- no revision CAS, no state machine, no duplicate-period check, no cap and no
  staleness comparison. Those are `report_documents` and
  `report_command_service` decisions and they are reached by calling, not by
  restating;
- no body reshaping. The parsed JSON object is handed to the service exactly
  as it arrived, so `normalize_report_request`'s exact-key admission is the
  only thing that decides what a body may contain, and the canonical
  `request_digest` the shipped `read_json` already computed is the digest the
  receipt is bound to. A body this adapter edited would be a body no receipt
  described;
- no second workspace comparison. `workspace_uid` is a required query
  parameter on every route (§8.1) and the service compares the body's copy
  against it, so the mismatch has exactly one origin.

Two spellings of one identity are refused rather than merged. The `{uid}`
path segment is **not** percent-decoded: a canonical UUIDv4 needs no escape,
the receipt ledger records the route `path` verbatim, and accepting `%61aa...`
beside `aaa...` would let one report own two receipt paths. A segment that is
not already canonical reaches the service and comes back as `invalid_query`.

The same rule is what `is_report_route_alias` enforces one layer up, because
the router cannot enforce it on its own. `urlparse` splits a trailing
`;params` off the last path segment and hands the router only what is left,
so `/api/v1/reports;shadow` and `/api/v1/reports` arrive at the router as one
string and at the receipt as one `path`. That is exactly the merge this module
refuses everywhere else: the alias would create under a receipt describing the
canonical route, and the canonical route replaying the same key afterwards
would answer a request nobody sent there. So the report routes are matched
against the request target losslessly — anything `urlparse` dropped between
the raw target and the routed path (a `;params` run, a `#fragment`, an
absolute-form prefix) makes the spelling a different one, and a different
spelling of a report route is not a report route. It is answered `not_found`
by the shipped dispatcher before the mixin, the service, the transaction and
the ledger are reached, so a refused alias leaves no receipt and no write, and
its `Idempotency-Key` is still free for the canonical route.

A fragment needs its own clause rather than riding on that path comparison,
because `urlparse` cuts the fragment off *before* it splits the query. A
target whose `#` follows the `?` therefore loses its fragment from both sides
of the comparison at once — `/api/v1/reports?workspace_uid=…#shadow` compares
the same path against itself and reads as canonical, while the query the
handlers decode, the `path` the receipt binds and the route the dispatcher
matched all have the fragment already gone. So a literal `#` anywhere in the
raw target of a report-document route makes the spelling a second one, wherever
it sits. Only the literal delimiter counts: `%23` is data inside a path segment
or a query value, is never a fragment cut, survives to the service intact, and
is left alone here.

That guard is deliberately scoped to the four report-document handlers.
`/api/v1/reports/daily-preview` and `/api/v1/reports/weekly-preview` are
shipped, frozen reads that allocate no identity, bind no receipt and write
nothing, so an alias of one of them cannot split an identity in two and there
is nothing here to justify changing their answers; every endpoint outside this
batch keeps its shipped spelling tolerance for the same reason.

The generic `{uid}` GET route cannot shadow the two shipped preview reads. It
is registered after them *and* structurally excludes their exact segments, so
route order alone is not what keeps `GET /api/v1/reports/daily-preview`
working.

Every refusal here carries a code from §12, the fixed sentence its own raiser
wrote, and details limited to the structural `field` name and, for ledger
capacity, `retry_after_seconds`. No identity, markdown, note, digest, cursor,
changed-file name or store content ever reaches a message or a detail. There
is no transport-owned refusal type: a query this adapter decodes badly is the
same `ReportQueryError` the query leaf raises for the same code, so one code
keeps one class and one sentence across the whole report surface. The status
table is exhaustive by assertion, not by fallback: a refusal code this module
has not classified raises rather than being guessed into a 400.
"""

from __future__ import annotations

import re
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

from .report_documents import ReportDocumentError
from .report_queries import LIST_PAGE_SIZE, ReportQueryError
from .report_repository_service import ReportRepositoryServiceError


__all__ = [
    "REPORT_ACTION_OPERATIONS",
    "REPORT_LIST_QUERY_KEYS",
    "REPORT_QUERY_KEYS",
    "REPORT_REFUSALS",
    "REPORT_REFUSAL_STATUS",
    "REPORT_ROUTE_HANDLERS",
    "ReportDocumentsHttpMixin",
    "is_report_route_alias",
    "list_report_payload",
    "read_report_payload",
    "report_command_result",
]

# `workspace_uid` is required on every route; the list route alone accepts the
# three paging parameters, each optional and each single-valued when present.
REPORT_QUERY_KEYS = frozenset({"workspace_uid"})
REPORT_LIST_QUERY_KEYS = frozenset({"workspace_uid", "state", "limit", "cursor"})

# The path verb a mutation route spells, mapped onto the operation name the
# service admits. The wire says `revisions` because the POST appends one; the
# service calls the operation `revise`.
REPORT_ACTION_OPERATIONS = {
    "revisions": "revise",
    "finalize": "finalize",
    "archive": "archive",
    "restore": "restore",
}

# §12, restated here because status is a transport decision and the layers
# below deliberately refuse to make it. Every code any report layer can raise
# appears exactly once; the suite asserts the table covers all of them.
REPORT_REFUSAL_STATUS = {
    "invalid_query": 400,
    "report_body_invalid": 400,
    "report_cursor_invalid": 400,
    "idempotency_key_required": 400,
    "invalid_idempotency_key": 400,
    "report_not_found": 404,
    "workspace_mismatch": 409,
    "store_sync_required": 409,
    "idempotency_conflict": 409,
    "report_revision_conflict": 409,
    "report_source_changed": 409,
    "report_state_invalid": 409,
    "report_duplicate_period": 409,
    "report_revision_limit": 409,
    "report_document_limit": 409,
    "report_storage_full": 409,
    "report_template_unsupported": 422,
    "report_capability_unavailable": 422,
    "report_idempotency_capacity": 429,
}

# Canonical decimal only, so `50` is the one spelling of the page size and a
# zero-padded `0050` is refused instead of quietly meaning the same thing.
_DIGITS = re.compile(r"0|[1-9][0-9]{0,3}")

# The three refusal types every report layer raises. Nothing else is caught,
# so a store fault or a programming error still reaches the shipped dispatcher
# instead of being answered as a client mistake.
REPORT_REFUSALS = (
    ReportRepositoryServiceError,
    ReportQueryError,
    ReportDocumentError,
)

# The four handlers this batch registered, and the whole surface the alias
# rule covers. Naming handlers rather than re-spelling the four path patterns
# keeps the route table the single description of what a report route is: a
# renamed handler fails the suite's agreement check instead of silently
# leaving a route unguarded.
REPORT_ROUTE_HANDLERS = frozenset({
    "_get_report_documents",
    "_get_report_document",
    "_post_report_create",
    "_post_report_action",
})


# --------------------------------------------------------- route canonicality


def is_report_route_alias(handler: str, request_target: str) -> bool:
    """Did a report route get reached by a spelling the router never saw?

    `handler` is the handler name of the route that matched, and
    `request_target` is the untouched request-line target.

    A literal `#` is asked about first and on its own, because the path
    comparison below cannot see it once it follows the `?`: `urlparse` cuts the
    fragment off ahead of the query, so `/api/v1/reports?workspace_uid=…#shadow`
    compares one already-trimmed path against itself. Every position of the
    delimiter is one answer here — a fragment is a client-side spelling that
    never belonged on a request line, and the route it names is not the route
    the router matched. `%23` is not that delimiter and is not asked about: it
    is data, it reaches the service unchanged, and refusing it would be
    refusing a value rather than a spelling.

    Otherwise the comparison is the lossless one: everything before the first
    `?` is the path as it was written, and `urlparse().path` is the path as it
    was routed. They differ only when something was dropped in between — the
    `;params` run `urlparse` strips off the last segment, a fragment written
    ahead of the query, or an absolute-form prefix — and each of those is a
    second spelling of a route that binds receipts to the first one.

    Returns False for every non-report handler, so this decides nothing about
    endpoints outside this batch.
    """

    if handler not in REPORT_ROUTE_HANDLERS:
        return False
    if "#" in request_target:
        return True
    return request_target.split("?", 1)[0] != urlparse(request_target).path


# ------------------------------------------------------------ query decoding


def _invalid_query(field: str | None = None) -> ReportQueryError:
    """The query leaf's own refusal, for the query the transport decoded.

    Reusing it rather than inventing a transport twin keeps `invalid_query`
    to one class and one sentence whether the query was rejected here or one
    layer down. `field` names a position and is left out entirely when the
    defect is an unknown key, whose name is the caller's word and not one of
    the leaf's allowlisted positions.
    """

    return ReportQueryError("invalid_query", field)


def _canonical_workspace_uid(value: str) -> bool:
    """The shipped preview's canonical-identity rule, restated not imported.

    `reporting_http` is frozen read-only for this batch and keeps this test
    private; repeating the four clauses keeps one spelling of an identity
    across both surfaces without reaching into another module's namespace.
    """

    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.int != 0 and parsed.variant == uuid.RFC_4122 and str(parsed) == value


def _single(parsed: dict[str, list[str]], key: str) -> str:
    """Exactly one non-empty occurrence, or the query is invalid.

    A repeated `workspace_uid` is refused rather than resolved by first- or
    last-wins: two asserted owners in one request is a defect in the caller,
    and picking one would admit a request nobody wrote.
    """

    items = parsed[key]
    if len(items) != 1 or items[0] == "":
        raise _invalid_query(key)
    return items[0]


def _limit(raw: str) -> int:
    if _DIGITS.fullmatch(raw) is None:
        raise _invalid_query("limit")
    return int(raw)


def parse_report_query(query: str, /, *, paged: bool = False) -> dict[str, object]:
    """Decode one report route's query with no store access at all (§8.1 step 1).

    Returns the admitted owner plus, for the list route, the three paging
    values with their documented defaults. `state` and `cursor` travel to the
    query leaf as they arrived; only `limit` is turned into the integer the
    leaf's exact-type check requires, and a `limit` that is not a short run of
    digits never becomes one.
    """

    allowed = REPORT_LIST_QUERY_KEYS if paged else REPORT_QUERY_KEYS
    decoded = parse_qs(query, keep_blank_values=True)
    if not set(decoded) <= allowed:
        raise _invalid_query()
    if "workspace_uid" not in decoded:
        raise _invalid_query("workspace_uid")
    workspace_uid = _single(decoded, "workspace_uid")
    if not _canonical_workspace_uid(workspace_uid):
        raise _invalid_query("workspace_uid")
    admitted: dict[str, object] = {"workspace_uid": workspace_uid}
    if not paged:
        return admitted
    admitted["state"] = _single(decoded, "state") if "state" in decoded else "active"
    admitted["cursor"] = _single(decoded, "cursor") if "cursor" in decoded else None
    admitted["limit"] = (
        _limit(_single(decoded, "limit")) if "limit" in decoded else LIST_PAGE_SIZE
    )
    return admitted


# --------------------------------------------------------- refusal translation


def _details(error: Any) -> dict[str, Any]:
    """Structural position and retry budget only — never a value.

    `field` names a position in a request or a document and is drawn from a
    closed allowlist below this layer, so it discloses nothing about what was
    sent. `retry_after_seconds` is populated for ledger capacity alone.
    """

    details: dict[str, Any] = {}
    field = getattr(error, "field", None)
    if field is not None:
        details["field"] = field
    retry_after = getattr(error, "retry_after_seconds", None)
    if retry_after is not None:
        details["retry_after_seconds"] = retry_after
    return details


def _refusal(error: Any) -> tuple[str, str, int, dict[str, Any]]:
    """Turn one report refusal into its declared status, keeping its sentence.

    The status is looked up, never defaulted: an unclassified code raises here
    rather than being answered with a plausible 400, because a wrong status is
    a client that retries the wrong way.
    """

    code = error.code
    return code, str(error), REPORT_REFUSAL_STATUS[code], _details(error)


# ------------------------------------------------------------------ payloads


def list_report_payload(stack: Any, query: str) -> dict[str, Any]:
    """The `data` object for one bounded list page."""

    admitted = parse_report_query(query, paged=True)
    return stack.list_report_documents(
        workspace_uid=admitted["workspace_uid"],
        state=admitted["state"],
        limit=admitted["limit"],
        cursor=admitted["cursor"],
    )


def read_report_payload(stack: Any, query: str, report_uid: str) -> dict[str, Any]:
    """The `data` object for one report, its history and its staleness."""

    admitted = parse_report_query(query)
    return stack.get_report_document(
        workspace_uid=admitted["workspace_uid"], report_uid=report_uid
    )


def report_command_result(
    stack: Any,
    /,
    *,
    operation: str,
    request: dict[str, Any],
    query: str,
    path: str,
    target_report_uid: str | None,
    idempotency_key: str,
    request_digest: str,
) -> dict[str, object]:
    """Run one mutation and return the service's own status and body.

    The returned status is the recorded one on a replay and the fresh one
    otherwise; neither is recomputed here. `path` is the matched route without
    its query, which is what the receipt binds to, so one report keeps one
    receipt path.
    """

    admitted = parse_report_query(query)
    return stack.execute_report_document_command(
        operation,
        request,
        workspace_uid=admitted["workspace_uid"],
        target_report_uid=target_report_uid,
        idempotency_key=idempotency_key,
        path=path,
        request_digest=request_digest,
    )


# -------------------------------------------------------------------- transport


class ReportDocumentsHttpMixin:
    """Four thin handlers; host, Origin, CSRF and body limits stay on the handler.

    Each method decodes, calls one payload function and answers. The refusal
    translation is shared so a code can never be given one status on a read
    and another on a write.
    """

    def _send_report_refusal(self, error: Any) -> None:
        self.send_api_error(*_refusal(error))

    def _get_report_documents(self, parsed: Any, match: Any) -> None:
        try:
            payload = list_report_payload(self.stack, parsed.query)
        except REPORT_REFUSALS as error:
            self._send_report_refusal(error)
            return
        self.send_json({"data": payload})

    def _get_report_document(self, parsed: Any, match: Any) -> None:
        try:
            payload = read_report_payload(self.stack, parsed.query, match.group(1))
        except REPORT_REFUSALS as error:
            self._send_report_refusal(error)
            return
        self.send_json({"data": payload})

    def _post_report_create(
        self,
        path: str,
        match: Any,
        body: dict[str, Any],
        request_digest: str,
        idempotency_key: str,
    ) -> None:
        self._answer_report_command(
            operation="create",
            path=path,
            target_report_uid=None,
            body=body,
            request_digest=request_digest,
            idempotency_key=idempotency_key,
        )

    def _post_report_action(
        self,
        path: str,
        match: Any,
        body: dict[str, Any],
        request_digest: str,
        idempotency_key: str,
    ) -> None:
        self._answer_report_command(
            operation=REPORT_ACTION_OPERATIONS[match.group(2)],
            path=path,
            target_report_uid=match.group(1),
            body=body,
            request_digest=request_digest,
            idempotency_key=idempotency_key,
        )

    def _answer_report_command(
        self,
        *,
        operation: str,
        path: str,
        target_report_uid: str | None,
        body: dict[str, Any],
        request_digest: str,
        idempotency_key: str,
    ) -> None:
        """The one place a report mutation is answered, replay or not.

        The POST dispatcher hands handlers the matched path without its query,
        so the query is taken from the request line here rather than being
        threaded through a changed dispatch signature.
        """

        try:
            outcome = report_command_result(
                self.stack,
                operation=operation,
                request=body,
                query=urlparse(self.path).query,
                path=path,
                target_report_uid=target_report_uid,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
        except REPORT_REFUSALS as error:
            self._send_report_refusal(error)
            return
        self.send_json(outcome["body"], outcome["status"])
