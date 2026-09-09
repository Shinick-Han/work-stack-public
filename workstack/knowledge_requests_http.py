"""The loopback owner surface for knowledge connections and requests.

Three canonical routes and nothing else:

``GET  /api/v1/knowledge/connections``   the stored nonsecret owner policy
``POST /api/v1/knowledge/connections``   replace that policy under a CAS guard
``POST /api/v1/knowledge/requests``      issue one scoped KnowledgeRequest

Successful connection responses keep the closed two-field ``data`` policy and
may attach content-free ``meta.occupancy`` projected from that same admitted
document. There is no occupancy route and no stored occupancy field.

This module is a *route surface*. It parses no schema of its own, holds no
state of its own and reaches no network: every decision below the closed body
shape is made by :mod:`workstack.knowledge_request_issuer` over the released
owner ledger. What it owns is the boundary itself.

The one thing the issue route does beyond answering is tell *this server's*
attempt guard about a request this server has just newly committed, so the
execution route can later spend that request's single automatic attempt. That
is a callback into the owning process, not a second authorization: a replay
registers nothing, the guard is never consulted here, and a registration that
fails changes neither the ledger nor the response.

**Admission is the released one, unchanged.** Both writes reach a handler only
after :meth:`~workstack.server_admission.RequestAdmissionMixin.
_require_browser_mutation` has proven a same-origin ``Origin`` and the session
CSRF token, in the position the POST surface already ran it. An Agent or
Capture bearer token alone reaches neither: it authorises Capture ingestion,
not policy configuration and not issuing. Nothing here is reached by a
cross-origin page, and a refused request has already returned before any Store
transaction is opened, so an unauthorised attempt performs no policy or ledger
write of any kind.

**The v4 backend is refused, not improvised.** The knowledge ledger is a
collection-store document (schema 6). The experimental v4 application composes
a different store adapter that has no such document, so this surface says so
with a closed code rather than inventing a second writer for it.

**No receipt is cached.** These routes deliberately take no ``Idempotency-Key``
and are absent from ``IDEMPOTENT_POST_ROUTES``: that mechanism stores the whole
response body in ``activity.json``, and an issued response contains the user's
query. Idempotence comes from the caller's ``intent_id`` instead, which the
ledger recognises through a digest it never has to store a query for.

Every refusal carries a closed code and, at most, the *name* of a field in a
closed schema. No query text, alias, identifier, token or path is echoed.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .knowledge_attempt_guard import KnowledgeAttemptError
from .knowledge_ledger_document import KnowledgeLedgerError
from .knowledge_owner_requests import request_digest
from .knowledge_request import KnowledgeRequestError
from .knowledge_request_issuer import (
    ConnectionPolicyReceipt,
    IssuedKnowledgeRequest,
    issue_knowledge_request,
    parse_issue_body,
    parse_policy_body,
    read_connection_policy_receipt,
    replace_connection_policy_receipt,
    utc_now_rfc3339,
)
from .server_errors import RequestError
from .store import Store

__all__ = (
    "CONNECTIONS_PATH",
    "KNOWLEDGE_BODY_LIMIT",
    "KNOWLEDGE_PATH_PREFIX",
    "KnowledgeRequestsHttpMixin",
    "REQUESTS_PATH",
)


CONNECTIONS_PATH = "/api/v1/knowledge/connections"
REQUESTS_PATH = "/api/v1/knowledge/requests"
KNOWLEDGE_PATH_PREFIX = "/api/v1/knowledge/"

# An issue body is one bounded query (1000 characters) plus identity; a policy
# body is at most eight connections of an alias, a UUID and eight aliases. This
# is generous for both and far below the 1 MiB default, and it deliberately
# does not touch the Capture 64 KiB budget, which belongs to a different route.
KNOWLEDGE_BODY_LIMIT = 16 * 1024

# The refusals that mean "the state you expected is not the state that exists".
# They are conflicts, not malformed input: the body was well formed and the
# ledger, the policy, the Task or the clock had moved.
_CONFLICT_CODES = frozenset(
    {
        "document_too_large",
        "ledger_full",
        "policy_revision_changed",
        "request_digest_mismatch",
        "request_expired",
        "task_binding_mismatch",
        "task_binding_required",
        "unknown_task",
        "workspace_mismatch",
    }
)

_POLICY_MESSAGE = "the knowledge connection policy request was refused"
_ISSUE_MESSAGE = "the knowledge request was refused"


def _refusal(error: Any, message: str) -> RequestError:
    """One closed refusal, as an HTTP envelope that echoes no submitted value."""

    status = 409 if error.code in _CONFLICT_CODES else 400
    return RequestError(error.code, message, status, dict(error.details))


def _connections_envelope(receipt: ConnectionPolicyReceipt) -> dict[str, Any]:
    """The closed policy object plus occupancy of that same snapshot."""

    return {"data": receipt.policy, "meta": {"occupancy": receipt.occupancy}}


class KnowledgeRequestsHttpMixin:
    """The owner knowledge surface the loopback handler mixes in."""

    def _knowledge_store(self) -> Store:
        """The collection store the ledger lives in, or an explicit refusal."""

        store = self.stack.store
        if not isinstance(store, Store):
            # `workstack.ssot` (schema 4) is a different backend with no
            # knowledge document. Refusing here is the whole of this surface's
            # v4 support; nothing below invents an alternative writer.
            raise RequestError(
                "knowledge_backend_unsupported",
                "the knowledge ledger requires the collection store",
                409,
            )
        return store

    def _require_canonical_knowledge_path(self, canonical: str) -> None:
        """The route is the exact path, not a spelling that resolves to it.

        ``urlparse`` strips a trailing ``;params`` run off the last segment, so
        a router that reads the parsed path can be reached by a target it never
        saw. These routes answer only their own canonical spelling, and the
        refusal lands before any Store transaction is opened.
        """

        if urlsplit(self.path).path != canonical:
            raise RequestError("not_found", "API endpoint not found", 404)

    def _get_knowledge_connections(self, parsed: Any, match: re.Match[str]) -> None:
        self._require_canonical_knowledge_path(CONNECTIONS_PATH)
        if parsed.query:
            raise RequestError(
                "invalid_query", "the knowledge policy read takes no query", 400
            )
        store = self._knowledge_store()
        try:
            receipt = read_connection_policy_receipt(store)
        except (KnowledgeLedgerError, KnowledgeRequestError) as error:
            raise _refusal(error, _POLICY_MESSAGE) from error
        self.send_json(_connections_envelope(receipt))

    def _post_knowledge_connections(
        self,
        path: str,
        match: re.Match[str],
        body: dict[str, Any],
        request_digest: str,
        idempotency_key: str,
    ) -> None:
        self._require_canonical_knowledge_path(CONNECTIONS_PATH)
        self._refuse_unsupported_idempotency_key(
            "the knowledge policy route accepts no Idempotency-Key"
        )
        store = self._knowledge_store()
        try:
            expected, connections = parse_policy_body(body)
            receipt = replace_connection_policy_receipt(
                store,
                expected_policy_revision=expected,
                connections=connections,
            )
        except (KnowledgeLedgerError, KnowledgeRequestError) as error:
            raise _refusal(error, _POLICY_MESSAGE) from error
        self.send_json(_connections_envelope(receipt))

    def _register_new_attempt(self, issued: IssuedKnowledgeRequest) -> None:
        """Make one *newly committed* request eligible for one execution.

        Only a fresh commit registers. A replay is not a new authorization and
        must never rearm an identity this incarnation has already spent, so a
        replayed receipt registers nothing -- and neither does anything the
        ledger held before this server started, because this guard has no way
        to reach the ledger and never tries.

        Registration happens *after* the commit, so it can only ever fail on
        this instance's own capacity bound, which is the ledger's own 200
        record bound: a request the ledger accepted therefore has room here
        unless earlier records were removed from the ledger without this
        process learning of it. If it does fail, the issue is not undone and
        the response is not changed -- pretending to roll back a committed
        authorization would be a lie. The request is simply never eligible for
        automatic execution, which is the conservative direction: it withholds
        an attempt, and grants no authority that was not already granted.
        """

        if issued.replayed:
            return
        guard = getattr(self.server, "knowledge_attempt_guard", None)
        if guard is None:
            return
        try:
            guard.register_new(
                issued.document["request_id"], request_digest(issued.document)
            )
        except KnowledgeAttemptError:
            return

    def _post_knowledge_request(
        self,
        path: str,
        match: re.Match[str],
        body: dict[str, Any],
        request_digest: str,
        idempotency_key: str,
    ) -> None:
        self._require_canonical_knowledge_path(REQUESTS_PATH)
        self._refuse_unsupported_idempotency_key(
            "the knowledge request route derives idempotence from intent_id"
        )
        store = self._knowledge_store()
        try:
            issued = issue_knowledge_request(
                store,
                parse_issue_body(body),
                clock=getattr(self.server, "knowledge_clock", utc_now_rfc3339),
            )
        except (KnowledgeLedgerError, KnowledgeRequestError) as error:
            raise _refusal(error, _ISSUE_MESSAGE) from error
        self._register_new_attempt(issued)
        # `data` is the KnowledgeRequest v1 wire document itself: it is what the
        # user carries out of band, so it is copied verbatim rather than
        # re-shaped for a UI. The bounded ledger facts travel beside it.
        self.send_json(
            {
                "data": issued.document,
                "meta": {
                    "replayed": issued.replayed,
                    "state": issued.state,
                    "connection_alias": issued.connection_alias,
                    "policy_revision": issued.policy_revision,
                },
            }
        )
