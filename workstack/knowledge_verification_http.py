"""The one loopback route that checks a stored Capture's source versions.

``POST /api/v1/knowledge/captures/verify``

The body is exactly ``{workspace_uid, capture_id, capture_revision}``. There is
no field for a document reference, an expected version, a corpus, a connection
alias, a verifier alias, a command, an environment or a configuration path:
everything the child is run with is derived by the owner from its own store and
from the operator's server configuration, so a browser that reaches this route
cannot widen what it reaches. A body carrying a fourth field is refused rather
than having that field ignored.

**Admission is the released one, unchanged.** This route is a v1 POST like the
others: the bounded body is drained first, then
:meth:`~workstack.server_admission.RequestAdmissionMixin._require_browser_mutation`
proves a same-origin ``Origin`` and the session CSRF token, in the position the
POST surface already ran it. No new bearer is introduced, and the Capture
ingestion token authorises nothing here -- its rights are not widened by a
single byte. The route shares the 16 KiB knowledge body budget and the
canonical-path rule with the other ``/api/v1/knowledge/`` routes, and it is
deliberately absent from ``IDEMPOTENT_POST_ROUTES``: that mechanism stores the
whole response body in ``activity.json``, and a verification response describes
a user's stored evidence at one instant, which is not a receipt to replay.

**A success is an observation, not a refresh.** The response carries the stored
binding this check was about and the validated closed protocol result, under
``meta.outcome == "verification_ready"``. Nothing is written: no Capture, no
Task, no ledger record, no SSOT version and no cached receipt. The client
displays the observation beside the evidence it already holds and must not fold
it back into the saved Capture.

**The request-target is the route, byte for byte.** This route takes no
parameter, so a target carrying a query string, a bare ``?`` or a fragment is
not an unusual spelling of it -- it is a target this route never published, and
it is refused with the closed unknown-route answer before any Store
transaction, verification gate or child. The unsupported ``Idempotency-Key``
refusal lands in the same position, for the same reason.

**Every refusal is closed.** One code, one constant message, and at most the
*name* of a field in the three-field body. The exact owner-admission and
protocol codes are mapped into the categories this route publishes without
their values, and no evidence handle, digest, capture identifier, workspace
UUID, command, environment value, child byte or raw exception string is ever
echoed. The handler writes no log line for any of them.
"""

from __future__ import annotations

import re
from typing import Any

from .knowledge_driver_exchange import DRIVER_NOT_STARTED, DRIVER_OUTCOME_UNKNOWN
from .knowledge_request_issuer import utc_now_rfc3339
from .knowledge_requests_http import KnowledgeRequestsHttpMixin
from .knowledge_verification_guard import (
    VERIFICATION_BUSY,
    VERIFICATION_CLEANUP_UNSETTLED,
    KnowledgeVerificationGuard,
    KnowledgeVerificationGuardError,
)
from .knowledge_verification_runtime import (
    CAPTURE_REVISION_CHANGED,
    UNKNOWN_CAPTURE,
    VERIFICATION_AUTHORITY_CHANGED,
    VERIFICATION_BINDING_MISMATCH,
    VERIFICATION_RESULT_REFUSED,
    VERIFIER_UNAVAILABLE,
    VerificationRuntimeError,
    parse_verification_body,
    verify_capture_source,
)
from .server_errors import RequestError

__all__ = ("VERIFY_PATH", "KnowledgeVerificationHttpMixin")


VERIFY_PATH = "/api/v1/knowledge/captures/verify"

# "The state you expected is not the state that exists." The body was well
# formed, and either another check already holds this owner's single gate or
# the capture, the roster or the operator's pin disagreed with what the client
# said it was looking at.
_CONFLICT_CODES = frozenset(
    {
        CAPTURE_REVISION_CHANGED,
        VERIFICATION_AUTHORITY_CHANGED,
        VERIFICATION_BINDING_MISMATCH,
        VERIFICATION_BUSY,
    }
)

# "Not now, and nothing happened." No verifier is configured for this
# connection, the child could not be started at all, or an earlier child's
# cleanup is still unaccounted for and this owner will not start another until
# it restarts.
_UNAVAILABLE_CODES = frozenset(
    {DRIVER_NOT_STARTED, VERIFICATION_CLEANUP_UNSETTLED, VERIFIER_UNAVAILABLE}
)

# The child ran and its outcome cannot be described, or what it wrote is not an
# observation this owner will project. Either way it is an upstream failure and
# not a malformed client request: the client sent three fields this server's
# own store resolved.
_BAD_GATEWAY_CODES = frozenset({DRIVER_OUTCOME_UNKNOWN, VERIFICATION_RESULT_REFUSED})

_VERIFY_MESSAGE = "the knowledge verification request was refused"
_VERIFIER_MESSAGE = "the knowledge verifier did not return a usable observation"


def _status(code: str) -> int:
    if code == UNKNOWN_CAPTURE:
        return 404
    if code in _CONFLICT_CODES:
        return 409
    if code in _UNAVAILABLE_CODES:
        return 503
    if code in _BAD_GATEWAY_CODES:
        return 502
    return 400


def _refusal(error: Any) -> RequestError:
    """One closed refusal, as an HTTP envelope that echoes no submitted value."""

    status = _status(error.code)
    message = _VERIFIER_MESSAGE if status in (502, 503) else _VERIFY_MESSAGE
    return RequestError(
        error.code, message, status, dict(getattr(error, "details", {}))
    )


class KnowledgeVerificationHttpMixin(KnowledgeRequestsHttpMixin):
    """The owner source-verification route the loopback handler mixes in."""

    def _require_exact_verify_target(self) -> None:
        """The request-target is the route, byte for byte.

        The v1 POST dispatch routes on ``urlparse(self.path).path``, so an
        ordinary authorized ``.../verify?unexpected=1`` would otherwise reach
        this handler having been read as the canonical path, with its query
        silently discarded. Checking a stored Capture takes no parameter of any
        kind, so a target carrying one is a target this route never published,
        and it gets the closed unknown-route answer instead.

        This is a predicate on one route's own target. It changes no shared
        route parsing and no other endpoint, and it lands *before* any Store
        transaction, verification gate or child -- with the released Host,
        same-origin and CSRF admission still proven ahead of it, in the
        position the v1 POST surface already ran it.
        """

        if self.path != VERIFY_PATH:
            raise RequestError("not_found", "API endpoint not found", 404)
        # Strictly weaker than the comparison above, and left in place
        # unchanged: this surface's released rule is that a knowledge route
        # answers only its own canonical spelling.
        self._require_canonical_knowledge_path(VERIFY_PATH)

    def _verification_guard(self) -> KnowledgeVerificationGuard:
        """This owner incarnation's single verification gate.

        A server built before this route existed carries none, and a check it
        cannot serialise is one it will not start: that is reported as the
        unavailable verifier rather than by quietly running unguarded.
        """

        guard = getattr(self.server, "knowledge_verification_guard", None)
        if not isinstance(guard, KnowledgeVerificationGuard):
            raise RequestError(VERIFIER_UNAVAILABLE, _VERIFIER_MESSAGE, 503)
        return guard

    def _post_knowledge_capture_verify(
        self,
        path: str,
        match: re.Match[str],
        body: dict[str, Any],
        request_digest: str,
        idempotency_key: str,
    ) -> None:
        self._require_exact_verify_target()
        self._refuse_unsupported_idempotency_key(
            "the knowledge verification route accepts no Idempotency-Key"
        )
        try:
            workspace_uid, capture_id, capture_revision = parse_verification_body(body)
        except VerificationRuntimeError as error:
            raise _refusal(error) from error
        store = self._knowledge_store()
        guard = self._verification_guard()
        try:
            observation = verify_capture_source(
                store,
                workspace_uid=workspace_uid,
                capture_id=capture_id,
                capture_revision=capture_revision,
                drivers=self.server.knowledge_drivers,
                guard=guard,
                clock=getattr(self.server, "knowledge_clock", utc_now_rfc3339),
            )
        except (KnowledgeVerificationGuardError, VerificationRuntimeError) as error:
            raise _refusal(error) from error
        # `data` is the stored binding and the validated protocol result,
        # exactly as the closed validator projected it. Nothing is added: there
        # is no raw stdout, no stderr, no command, no environment, no verifier
        # path and no token here, and `meta` says only what this outcome is --
        # an observation, not a saved refresh.
        self.send_json({"data": observation, "meta": {"outcome": "verification_ready"}})
