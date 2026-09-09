"""The one loopback route that executes an already-issued KnowledgeRequest.

``POST /api/v1/knowledge/requests/execute``

The body is a KnowledgeRequest v1 document -- the very document the issue route
returned -- and nothing else. There is no field for a command, a driver alias,
an endpoint, an environment, a timeout or a corpus grant: everything the child
is run with comes from the operator's server configuration and from the owner's
own ledger, so a browser that reaches this route cannot widen what it reaches.

**Admission is the released one, unchanged.** This route is a v1 POST like the
others: the bounded body is drained first, then
:meth:`~workstack.server_admission.RequestAdmissionMixin._require_browser_mutation`
proves a same-origin ``Origin`` and the session CSRF token, in the position the
POST surface already ran it. No new bearer is introduced, and the Capture
ingestion token authorises nothing here. It shares the 16 KiB knowledge body
budget and the canonical-path rule with the other knowledge routes, and it is
deliberately absent from ``IDEMPOTENT_POST_ROUTES``: that mechanism stores the
whole response body in ``activity.json``, and both this request and this
response describe a query.

**A success is a proposal, not evidence.** The response carries the released
manual-import envelope, re-projected from the child's untrusted stdout by the
existing import validators, under ``meta.outcome == "proposal_ready"``. No
Capture is saved, no Task is touched and no receipt is cached: the user reviews
the proposal and confirms it through the existing import route, which is still
the only writer. Raw stdout, stderr, the pinned command, the environment and
any token are absent from the response by construction -- the response body is
exactly the validator's projection.

**The request-target is the route, byte for byte.** This route takes no
parameter, so a target carrying a query string, a bare ``?`` or a fragment is
not an unusual spelling of it -- it is a target this route never published, and
it is refused with the closed unknown-route answer before any Store
transaction, guard consume or child. The shared route table matches on the
stripped path, so that comparison is made here, on this one route's own target,
and changes nothing for any other endpoint.

**Every refusal is closed.** One code, one constant message, and at most the
*name* of a field in a closed schema. No query text, alias, identifier, digest,
path, command, environment value or child byte is ever echoed, and the handler
writes no log line for any of them.
"""

from __future__ import annotations

import re
from typing import Any

from .knowledge_attempt_guard import KnowledgeAttemptError
from .knowledge_execution_runtime import (
    DRIVER_BINDING_MISMATCH,
    DRIVER_INPUT_REFUSED,
    DRIVER_UNAVAILABLE,
    KnowledgeExecutionError,
    KnowledgeProposalError,
    execute_knowledge_request,
)
from .knowledge_ledger_document import KnowledgeLedgerError
from .knowledge_request import KnowledgeRequestError
from .knowledge_request_issuer import utc_now_rfc3339
from .knowledge_requests_http import KnowledgeRequestsHttpMixin
from .server_errors import RequestError

__all__ = ("EXECUTE_PATH", "KnowledgeExecutionHttpMixin")


EXECUTE_PATH = "/api/v1/knowledge/requests/execute"

# "The state you expected is not the state that exists." The ledger, the
# policy, the Task, the window or this incarnation's single attempt had moved
# between the client reading the request and asking for it to be executed.
_CONFLICT_CODES = frozenset(
    {
        DRIVER_BINDING_MISMATCH,
        "policy_revision_changed",
        "request_already_attempted",
        "request_digest_mismatch",
        "request_expired",
        "request_not_pending",
        "request_not_registered",
        "task_binding_mismatch",
        "task_binding_required",
        "unknown_request",
        "unknown_task",
        "workspace_mismatch",
    }
)

# The child could not be started at all, or the operator configured no driver
# for this connection. Both are "not now", and neither says anything happened.
_UNAVAILABLE_CODES = frozenset({DRIVER_UNAVAILABLE, "driver_not_started"})

# The child ran and its outcome cannot be described: it exited non-zero, the
# budget expired, cleanup could not be confirmed, or what it wrote is not a
# proposal this owner will project. An upstream request may well have happened,
# which is exactly why the attempt is not given back.
_BAD_GATEWAY_CODES = frozenset({"driver_outcome_unknown"})

_EXECUTE_MESSAGE = "the knowledge execution request was refused"
_DRIVER_MESSAGE = "the knowledge driver did not return a usable outcome"
_PROPOSAL_MESSAGE = "the driver proposal was refused"


def _status(code: str) -> int:
    if code in _CONFLICT_CODES:
        return 409
    if code in _UNAVAILABLE_CODES:
        return 503
    if code in _BAD_GATEWAY_CODES:
        return 502
    if code == DRIVER_INPUT_REFUSED:
        # A server whose configuration and whose own issued document cannot be
        # combined into an admissible child payload. Not the caller's input.
        return 500
    return 400


class KnowledgeExecutionHttpMixin(KnowledgeRequestsHttpMixin):
    """The owner execution route the loopback handler mixes in."""

    def _require_exact_execute_target(self) -> None:
        """The request-target is the route, byte for byte.

        The v1 POST dispatch routes on ``urlparse(self.path).path``, which is
        the *stripped* target: a query string, a bare ``?`` and a fragment all
        disappear before a route is chosen, and the shared canonical-path rule
        below compares that same stripped spelling. So an ordinary authorized
        ``.../execute?unexpected=1`` reaches this handler having been read as
        the canonical path, and its query is simply discarded.

        Executing an already-issued request takes no parameter of any kind. A
        target carrying one is not this route spelled unusually; it is a target
        this route never published, and answering it would mean silently
        ignoring something the caller asked for. So the raw target is compared
        exactly, and anything else gets the closed unknown-route answer this
        surface already gives a non-canonical spelling.

        This is a predicate on one route's own target. It changes no shared
        route parsing and no other endpoint, and it lands *before* any Store
        transaction, guard consume or child -- with the released Host,
        same-origin and CSRF admission still proven ahead of it, in the
        position the v1 POST surface already ran it.
        """

        if self.path != EXECUTE_PATH:
            raise RequestError("not_found", "API endpoint not found", 404)
        # Strictly weaker than the comparison above, and left in place
        # unchanged: this surface's released rule is that a knowledge route
        # answers only its own canonical spelling.
        self._require_canonical_knowledge_path(EXECUTE_PATH)

    def _post_knowledge_request_execute(
        self,
        path: str,
        match: re.Match[str],
        body: dict[str, Any],
        request_digest: str,
        idempotency_key: str,
    ) -> None:
        self._require_exact_execute_target()
        self._refuse_unsupported_idempotency_key(
            "the knowledge execution route accepts no Idempotency-Key"
        )
        store = self._knowledge_store()
        try:
            proposal = execute_knowledge_request(
                store,
                body,
                drivers=self.server.knowledge_drivers,
                guard=self.server.knowledge_attempt_guard,
                clock=getattr(self.server, "knowledge_clock", utc_now_rfc3339),
            )
        except KnowledgeExecutionError as error:
            raise RequestError(
                error.code,
                _DRIVER_MESSAGE
                if _status(error.code) in (500, 502, 503)
                else _EXECUTE_MESSAGE,
                _status(error.code),
            ) from error
        except KnowledgeAttemptError as error:
            raise RequestError(
                error.code, _EXECUTE_MESSAGE, _status(error.code)
            ) from error
        except KnowledgeProposalError as error:
            # The child answered, and what it answered is not a proposal the
            # released import validators admit. That is an upstream failure,
            # not a malformed client request: the client sent a document this
            # server itself issued. Every projection refusal arrives as this
            # one type, so a decoder refusal cannot be reported as a 400 about
            # the caller's body.
            raise RequestError(
                error.code, _PROPOSAL_MESSAGE, 502, dict(error.details)
            ) from error
        except (KnowledgeLedgerError, KnowledgeRequestError) as error:
            raise RequestError(
                error.code, _EXECUTE_MESSAGE, _status(error.code), dict(error.details)
            ) from error
        # `data` is the released manual-import envelope, exactly as the
        # validator projected it. Nothing is added: there is no raw stdout, no
        # stderr, no command, no environment, no driver path and no token here,
        # and `meta` says only what this outcome is.
        self.send_json({"data": proposal, "meta": {"outcome": "proposal_ready"}})
