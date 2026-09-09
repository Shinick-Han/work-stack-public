"""One bounded Notion *access* observation, and deliberately nothing more.

This module answers a single narrow question about one operator-mapped Notion
page: **can this verifier reach it right now, and does the origin still admit
it as a live page?** It is not a Notion client, not a connector and not a
freshness oracle. It never opens, crawls, indexes or persists page content, and
it never returns ``current`` or ``stale``.

Why currentness is not on the table
-----------------------------------

``GET /v1/pages/{id}`` returns a page's *properties*, not its content, and a
page's ``last_edited_time`` says nothing about the blocks beneath it or about
any child page. Turning a property timestamp into "the captured text is still
right" would be exactly the invented verification the rest of this adapter
refuses to make. So every observation here preserves the caller's
``expected_source_version`` untouched and reports no observed version at all:
the useful distinction this slice adds is between *revoked*, *refused*,
*denied*, *unavailable* and *readable-but-unverified*, which are five different
actions for a human, where today they are one ``unsupported_source_type``.

The trust boundary, stated once
-------------------------------

The operator's mapping is the only thing that selects a page. The pinned
``page_url`` has already passed the released Notion host allow-list when
:class:`~integrations.opendocuments.source_access.SourceMapping` was
constructed; this module then extracts *only* the terminal page UUID from it
and builds the request URL itself. A request never travels to an operator- or
request-supplied origin: host, port, path prefix and API version are the module
constants below, there is no origin environment variable, and redirects are
never followed -- a ``3xx`` is a refusal, not a hop.

The token is read from a file the operator names in
:data:`TOKEN_ENVIRONMENT_VARIABLE`, and only *after* the document is mapped, in
a granted corpus, not revoked, on the Notion backend and carrying an extractable
page UUID. It is held for the duration of one request and appears in no
exception, no diagnostic, no observation and no ``repr``. Neither the token nor
its path nor any page title, property or text is a value this module returns.

Seams
-----

:class:`NotionBatch` owns the wall budget, the request spacing and the
transport. Tests inject a private transport and clock through its keyword-only
underscore parameters; production constructs it with no arguments and gets the
stdlib HTTPS path. There is no configuration that can redirect a production
request, because there is no configuration at all.
"""

from __future__ import annotations

import http.client
import os
import re
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from integrations.opendocuments.source_access import (
    SourceMapping,
    unsafe_notion_url_code,
)
from workstack.knowledge_request import KnowledgeRequestError, decode_strict_json

__all__ = [
    "BATCH_BUDGET_SECONDS",
    "MAX_CALL_TIMEOUT_SECONDS",
    "MAX_RESPONSE_BYTES",
    "MAX_TOKEN_BYTES",
    "MAX_TOKEN_PATH_CHARS",
    "MIN_REQUEST_INTERVAL_SECONDS",
    "NOTION_HOST",
    "NOTION_PATH_PREFIX",
    "NOTION_PORT",
    "NOTION_VERSION",
    "TOKEN_ENVIRONMENT_VARIABLE",
    "NotionBatch",
    "canonical_page_uuid",
    "observe_notion_mapping",
]

#: The one origin this module ever contacts. Not configurable, by design.
NOTION_HOST = "api.notion.com"
NOTION_PORT = 443
NOTION_PATH_PREFIX = "/v1/pages/"

#: Pinned per the Notion versioning contract: an explicit older version stays
#: supported, so this adapter does not silently follow "latest".
NOTION_VERSION = "2025-09-03"

#: The operator's optional absolute path to a file holding one integration
#: token. Read by this external child only, and never by Work Stack.
TOKEN_ENVIRONMENT_VARIABLE = "WORKSTACK_OD_NOTION_TOKEN_FILE"

MAX_RESPONSE_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 4096
MAX_TOKEN_PATH_CHARS = 4096

#: At most one Notion request per 350 ms, at most 30 s of wall clock for the
#: whole batch, and no single call may outlast 5 s or the remaining budget.
MIN_REQUEST_INTERVAL_SECONDS = 0.35
BATCH_BUDGET_SECONDS = 30.0
MAX_CALL_TIMEOUT_SECONDS = 5.0

_HYPHENATED_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
    r"-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z"
)
#: A Notion page slug ends in the page id with its hyphens removed. The id may
#: also stand alone; a bare 32 hex run must be the whole final segment.
_SLUG_UUID_RE = re.compile(r"^(?:.*-)?([0-9A-Fa-f]{32})\Z")

#: A bounded, printable, single-line secret. Nothing here inspects its shape
#: beyond that: an integration token's internal spelling is Notion's business.
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{8,512}\Z")
_TOKEN_TRIM = " \t\r\n"

_ABSENT_TOKEN = object()
_UNUSABLE_TOKEN = object()

# The five public answers this module may reach, named once so the mapping
# below reads as the contract table it implements.
_REVOKED = ("revoked", "mapping_revoked")
_REFUSED = ("refused", "source_refused")
_DENIED = ("denied", "access_denied")
_UNAVAILABLE = ("unavailable", "root_unavailable")
_NO_VERIFIER = ("unverifiable", "no_origin_verifier")
_NO_EXPECTED = ("unverifiable", "no_expected_version")
_UNVERIFIABLE = ("unverifiable", "verification_unavailable")


@dataclass(frozen=True)
class _NotionWire:
    """What came back off the socket, before any of it is believed.

    ``body`` is bounded to one byte past :data:`MAX_RESPONSE_BYTES` so an
    oversize response is *detectable* rather than silently truncated into
    something that might still parse. Nothing here is a public value: the whole
    record stays inside this module.
    """

    status: int
    body: bytes


class NotionBatch:
    """One verification's whole Notion budget: spacing, wall clock, transport.

    Constructed once per request and shared by every Notion evidence entry in
    it, so ten entries cannot become ten independent budgets. The underscore
    keyword parameters are a *test* seam; production calls ``NotionBatch()``.
    """

    def __init__(
        self,
        *,
        _transport: Callable[[str, str, float], _NotionWire] | None = None,
        _clock: Callable[[], float] | None = None,
        _sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._transport = _stdlib_transport if _transport is None else _transport
        self._clock = time.monotonic if _clock is None else _clock
        self._sleep = time.sleep if _sleep is None else _sleep
        self._started = self._clock()
        #: The batch's absolute edge, fixed once at construction. Every later
        #: budget question is asked against *this* instant rather than against
        #: a freshly derived remainder, so no two clock reads can disagree
        #: about where the thirty seconds end.
        self._deadline = self._started + BATCH_BUDGET_SECONDS
        self._last_request: float | None = None

    def __repr__(self) -> str:
        # No token, no page id, no elapsed detail: a batch is not evidence.
        return "NotionBatch()"

    def remaining_seconds(self) -> float:
        return self._deadline - self._clock()

    def fetch_page(self, page_uuid: str, token: str) -> _NotionWire | None:
        """One spaced, bounded, *timely* GET, or ``None``.

        ``None`` covers every reason this call produced no usable status --
        exhausted batch budget, connect/read timeout, TLS failure, reset, or an
        answer that arrived after a deadline -- all of which are the same fact
        for a reader: the root did not answer in time. The caught exception is
        never inspected or echoed, because it can carry the request headers.

        Admission and lateness are decided from **one** issue sample. Reading
        the clock twice -- once to derive the remaining budget and again to
        stamp the issue instant -- was a real defect: a scheduler pause between
        the two reads let a call sampled at 29.9 s be stamped at 30.2 s and
        granted a window ending at 30.3 s, so a valid answer completing at
        30.21 s was accepted although the batch's own edge was 30.0 s. With a
        single sample, the granted window can never end past that edge.

        The completion check needs the clock rather than the socket option. A
        socket timeout bounds one connect or one ``recv``; it does not bound
        the exchange, so a trickling response can satisfy every individual read
        and still outlast the whole batch. The clock is therefore read once
        more on the way out and compared against **both** deadlines this call
        is subject to: the per-call window it was granted, and the batch's
        absolute edge. The second is redundant arithmetically -- the granted
        window is clamped inside the edge -- and is kept anyway, because it is
        the invariant a reader actually cares about and it does not depend on
        that clamping staying true.

        What this does *not* claim: that the request necessarily leaves before
        the deadline. A scheduler may pause after any check, including between
        the admission test below and the first socket write, and nothing short
        of threads or async could narrow that -- neither of which belongs in a
        one-shot child. The guarantee is the one that matters for an
        observation: a result completing after the batch has expired is never
        accepted, whatever happened in between.
        """

        self._space()
        issued = self._clock()
        remaining = self._deadline - issued
        if remaining <= 0:
            # Exhausted at this call's own admission sample: nothing reaches
            # the transport, and the reader is told the root did not answer.
            return None
        timeout = min(MAX_CALL_TIMEOUT_SECONDS, remaining)
        self._last_request = issued
        try:
            wire = self._transport(page_uuid, token, timeout)
        except Exception:  # noqa: BLE001 - every wire failure is one fact here
            return None
        completed = self._clock()
        if completed > issued + timeout or completed > self._deadline:
            # Late. An observation is a statement about a moment, and this one
            # is no longer inside the moment it was authorised for -- nor,
            # under the second test, inside the batch at all.
            return None
        return wire

    def _space(self) -> None:
        if self._last_request is None:
            return
        gap = MIN_REQUEST_INTERVAL_SECONDS - (self._clock() - self._last_request)
        if gap > 0:
            self._sleep(gap)


def _stdlib_transport(page_uuid: str, token: str, timeout: float) -> _NotionWire:
    """The only production wire: stdlib HTTPS, verified TLS, no redirects.

    :class:`http.client.HTTPSConnection` returns the ``3xx`` to the caller
    rather than following it, which is the behaviour this adapter wants: a
    redirect off the pinned path is a refusal, not a hop to somewhere else.
    """

    connection = http.client.HTTPSConnection(
        NOTION_HOST,
        NOTION_PORT,
        timeout=timeout,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "GET",
            NOTION_PATH_PREFIX + page_uuid,
            headers={
                "Host": NOTION_HOST,
                "Authorization": "Bearer " + token,
                "Notion-Version": NOTION_VERSION,
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        return _NotionWire(
            status=int(response.status), body=response.read(MAX_RESPONSE_BYTES + 1)
        )
    finally:
        connection.close()


def canonical_page_uuid(page_url: object) -> str | None:
    """The terminal page UUID of an allow-listed Notion URL, or ``None``.

    The operator's URL supplies *only* this identifier. It is re-checked against
    the released host allow-list first -- the mapping already did, and doing it
    again here costs nothing and means this function cannot be misused by a
    future caller that skipped the mapping. Everything after the final path
    segment's page id is a human-readable slug and is discarded.
    """

    if unsafe_notion_url_code(page_url) is not None:
        return None
    if not isinstance(page_url, str):  # pragma: no cover - refused just above
        # A plain guard rather than an ``assert``: this narrows the type for a
        # reader and for a checker, and it survives ``python -O``.
        return None
    # The *path* only. Reading the terminal segment off the whole URL would let
    # a host label stand in for a page id.
    segments = [segment for segment in urlsplit(page_url).path.split("/") if segment]
    if not segments:
        return None
    terminal = segments[-1]
    if _HYPHENATED_UUID_RE.match(terminal):
        return terminal.lower()
    slug = _SLUG_UUID_RE.match(terminal)
    if slug is None:
        return None
    return _hyphenated(slug.group(1).lower())


def _hyphenated(hex32: str) -> str:
    return "-".join(
        (hex32[0:8], hex32[8:12], hex32[12:16], hex32[16:20], hex32[20:32])
    )


def _canonical_id_text(value: object) -> str | None:
    """Canonicalise an id *Notion* sent, under the same grammar as the URL's."""

    if not isinstance(value, str):
        return None
    if _HYPHENATED_UUID_RE.match(value):
        return value.lower()
    if re.fullmatch(r"[0-9A-Fa-f]{32}", value):
        return _hyphenated(value.lower())
    return None


def load_operator_token() -> object:
    """The operator's token, ``_ABSENT_TOKEN`` or ``_UNUSABLE_TOKEN``.

    "Not configured" and "configured but unreadable" are different facts for an
    operator -- one is "you have not set this up", the other is "your setup is
    broken" -- so they stay different sentinels here and different public codes
    at the call site. Neither the path nor the bytes appear in either.
    """

    path = os.environ.get(TOKEN_ENVIRONMENT_VARIABLE)
    if not path:
        return _ABSENT_TOKEN
    if len(path) > MAX_TOKEN_PATH_CHARS or not os.path.isabs(path):
        return _UNUSABLE_TOKEN
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_TOKEN_BYTES + 1)
    except OSError:
        return _UNUSABLE_TOKEN
    if not raw or len(raw) > MAX_TOKEN_BYTES:
        return _UNUSABLE_TOKEN
    try:
        text = raw.decode("ascii").strip(_TOKEN_TRIM)
    except UnicodeDecodeError:
        return _UNUSABLE_TOKEN
    return text if _TOKEN_RE.match(text) else _UNUSABLE_TOKEN


def observe_notion_mapping(
    mapping: SourceMapping,
    *,
    in_request_corpus: bool,
    expected_source_version: object,
    batch: NotionBatch,
) -> tuple[str, str]:
    """One access observation for one already-looked-up Notion mapping.

    The gates run in the order that keeps a secret and a socket out of a
    question that policy already answered: corpus, then revocation, then the
    page selector, then the token, then -- only then -- the network.
    ``in_request_corpus`` is the caller's, because only the caller knows which
    corpora this request was admitted for.
    """

    if not in_request_corpus:
        return _REFUSED
    if mapping.revoked:
        return _REVOKED
    page_uuid = canonical_page_uuid(mapping.page_url)
    if page_uuid is None:
        return _REFUSED
    token = load_operator_token()
    if token is _ABSENT_TOKEN:
        return _NO_VERIFIER
    if not isinstance(token, str):
        return _UNVERIFIABLE
    return _from_wire(
        batch.fetch_page(page_uuid, token), page_uuid, expected_source_version
    )


def _from_wire(
    wire: _NotionWire | None, page_uuid: str, expected: object
) -> tuple[str, str]:
    """The HTTP status half of the contract table, in one place."""

    if wire is None:
        return _UNAVAILABLE
    status = wire.status
    if status == 200:
        return _from_page_object(wire.body, page_uuid, expected)
    if status in (401, 403):
        return _DENIED
    if status == 429 or 500 <= status <= 599:
        return _UNAVAILABLE
    if 300 <= status <= 499:
        # Redirect, 404 and every other 4xx alike: the origin declined to hand
        # this verifier the page at its pinned path. Never ``file_absent`` -- a
        # 404 here also covers "exists, but this integration cannot see it" --
        # and never ``mapping_revoked``, which is the operator's word, not
        # Notion's.
        return _REFUSED
    return _UNVERIFIABLE


def _from_page_object(
    body: bytes, page_uuid: str, expected: object
) -> tuple[str, str]:
    """A ``200`` is not yet an answer: it has to be *this* page, and live."""

    document = _decoded_page(body)
    if document is None or document.get("object") != "page":
        return _UNVERIFIABLE
    if _canonical_id_text(document.get("id")) != page_uuid:
        # An answer about some other page is not an answer about this one.
        return _UNVERIFIABLE
    closed = _closed_page_state(document)
    if closed is not None:
        return closed
    if expected is None:
        return _NO_EXPECTED
    # Readable, live, and the caller brought an expectation this endpoint
    # cannot test. Saying anything but "I could not verify" here would turn a
    # reachability check into a currentness claim.
    return _UNVERIFIABLE


def _closed_page_state(document: dict[str, Any]) -> tuple[str, str] | None:
    """``refused`` for an archived or trashed page, and nothing for a live one.

    Only these two flags are consulted, and only when present: Notion adds
    fields over time, and requiring an unrelated evolving one would make this
    check fail for a reason that has nothing to do with access.
    """

    for name in ("archived", "in_trash"):
        if name not in document:
            continue
        flag = document[name]
        if type(flag) is not bool:
            return _UNVERIFIABLE
        if flag:
            return _REFUSED
    return None


def _decoded_page(body: bytes) -> dict[str, Any] | None:
    """Bounded strict JSON, or ``None`` for oversize, truncated or malformed."""

    if not body or len(body) > MAX_RESPONSE_BYTES:
        return None
    try:
        document = decode_strict_json(body, maximum_bytes=MAX_RESPONSE_BYTES)
    except KnowledgeRequestError:
        return None
    return document if isinstance(document, dict) else None
