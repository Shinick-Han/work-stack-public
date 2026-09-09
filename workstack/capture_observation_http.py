"""The two loopback routes for one Capture's saved source observation.

``POST /api/v1/knowledge/captures/record-check``
``GET  /api/v1/knowledge/captures/observation``

These are the wire for a *past* observation, not for a fresh claim about a
source. The released read-only ``.../captures/verify`` route is untouched and
keeps its exact request, response and semantics: it still runs a check and
still saves nothing. What is new here is a check whose result the owner
retains, and a read of what was retained.

**The record route is the verify route's admission, byte for byte.** It is a
v1 POST like the others: the bounded body is drained first, then
:meth:`~workstack.server_admission.RequestAdmissionMixin._require_browser_mutation`
proves a same-origin ``Origin`` and the session CSRF token, in the position the
POST surface already ran it. No new bearer is introduced and the Capture
ingestion token authorises nothing here. The body is exactly
``{workspace_uid, capture_id, capture_revision}`` and is admitted by the same
released :func:`~workstack.knowledge_verification_runtime.parse_verification_body`:
there is no field for a document reference, an expected version, a corpus, a
connection alias, a verifier alias, a command, an environment or a
configuration path, and a body carrying a fourth field is refused rather than
having that field ignored. The route shares the 16 KiB knowledge body budget,
and it is deliberately absent from ``IDEMPOTENT_POST_ROUTES``: that mechanism
stores the whole response body in ``activity.json``, and a saved observation is
not a receipt to replay. There is no response cache, no activity write and no
idempotency registration of any kind.

**The read route adds no authority.** ``GET`` runs under the loopback ``Host``
admission the whole read surface already runs under -- no new token, no new
header, and no permission that any other GET gains from this one existing. It
never starts a child, never reads or consults the operator's verifier
configuration, never saves and never activates a storage format. It takes
exactly three single-valued, nonempty query keys, which are handed to the same
three-field parser the POST body is; ordinary query encoding and key order are
accepted, and a duplicated, unknown or missing key is refused before any Store
read.

**The request-target is the route.** Neither route publishes a ``;params``
spelling, a percent-encoded path alias, a trailing slash or a fragment, and the
record route publishes no query at all -- not even a bare ``?``. Those targets
get the closed unknown-route answer before any Store transaction, verification
gate or child, in the position the surface already refuses an unknown route.

**A success says only what happened.** ``meta.outcome`` is
``observation_recorded`` for a confirmed save and ``observation_ready`` for a
read; ``data`` is the runtime's ``{binding, observation}`` projection exactly as
it was returned. Having no retained observation is a 200 whose ``observation``
is ``null`` -- it is not a 404, because the Capture was found and the honest
answer is "nothing was kept".

**Every refusal is closed.** The released verification status mapping is
reused unchanged, and the two new refusals are the ones this surface owes the
client. ``observation_save_unknown`` (503) says the check ran and its record
could not be *confirmed* saved; it deliberately does not claim that nothing was
written, because after a fault at an unknown commit point no such claim is
available. ``observation_read_unavailable`` (503) is every ordinary storage
read failure and every invalid persisted record: a corrupt history is refused
rather than shown as "no history". No raw exception text, path, digest,
identifier, command, environment value or child byte travels in either, and the
handler writes no log line for any of them.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .capture_observation_runtime import (
    OBSERVATION_READ_UNAVAILABLE,
    OBSERVATION_SAVE_UNKNOWN,
    get_capture_source_observation,
    record_capture_source_check,
)
from .capture_observations import CaptureObservationError
from .file_lease import StoreLockedError
from .knowledge_driver_exchange import DRIVER_NOT_STARTED, DRIVER_OUTCOME_UNKNOWN
from .knowledge_request_issuer import utc_now_rfc3339
from .knowledge_verification_guard import (
    VERIFICATION_BUSY,
    VERIFICATION_CLEANUP_UNSETTLED,
    KnowledgeVerificationGuardError,
)
from .knowledge_verification_http import KnowledgeVerificationHttpMixin
from .knowledge_verification_runtime import (
    CAPTURE_REVISION_CHANGED,
    INVALID_VERIFICATION_BODY,
    INVALID_VERIFICATION_FIELD,
    UNKNOWN_CAPTURE,
    VERIFICATION_AUTHORITY_CHANGED,
    VERIFICATION_BINDING_MISMATCH,
    VERIFICATION_RESULT_REFUSED,
    VERIFIER_UNAVAILABLE,
    VerificationRuntimeError,
    parse_verification_body,
)
from .server_errors import RequestError
from .server_read_routes import _single_valued_query
from .store_errors import (
    StoreAdoptionConflictError,
    StoreCorruptError,
    StoreExternalChangeError,
)

__all__ = (
    "OBSERVATION_PATH",
    "OBSERVATION_READ_UNAVAILABLE",
    "OBSERVATION_SAVE_UNKNOWN",
    "RECORD_CHECK_PATH",
    "CaptureObservationHttpMixin",
)


RECORD_CHECK_PATH = "/api/v1/knowledge/captures/record-check"
OBSERVATION_PATH = "/api/v1/knowledge/captures/observation"

# The two new closed codes are the runtime's own, re-exported rather than
# respelled here, so the code this surface publishes is by construction the
# code the runtime raised:
#
# `observation_save_unknown` -- the check ran and the owner cannot confirm its
# record reached storage. It is NOT a statement that nothing was written: a
# fault at an unknown commit point leaves the answer to journal recovery, and
# the client must read the saved state rather than assume either outcome.
#
# `observation_read_unavailable` -- the retained history could not be read: an
# ordinary storage failure, or a persisted record this owner will not project.
# Corrupt history is refused, never flattened into "no observation".

#: The exact three query keys the read route publishes.
OBSERVATION_QUERY_FIELDS = ("workspace_uid", "capture_id", "capture_revision")

#: A canonical base-10 unsigned integer: ``0``, or digits with no leading zero.
#: A sign, a space, an underscore, ``0x``, an exponent and ``007`` are all a
#: different spelling of a number this route never published.
_CANONICAL_REVISION = re.compile(r"0|[1-9][0-9]*")

#: The digit budget applied *before* ``int()``. The parser below refuses
#: anything past the released revision ceiling anyway; this bound exists so an
#: arbitrarily long digit run is refused as a shape rather than converted.
_MAX_REVISION_DIGITS = 16

# The released categories, reused unchanged. "The state you expected is not the
# state that exists" (409), "not now, and nothing happened" (503) and "the
# child ran and its outcome cannot be described" (502) mean here exactly what
# they mean on the released verify route.
_CONFLICT_CODES = frozenset(
    {
        CAPTURE_REVISION_CHANGED,
        VERIFICATION_AUTHORITY_CHANGED,
        VERIFICATION_BINDING_MISMATCH,
        VERIFICATION_BUSY,
    }
)
_UNAVAILABLE_CODES = frozenset(
    {DRIVER_NOT_STARTED, VERIFICATION_CLEANUP_UNSETTLED, VERIFIER_UNAVAILABLE}
)
_BAD_GATEWAY_CODES = frozenset({DRIVER_OUTCOME_UNKNOWN, VERIFICATION_RESULT_REFUSED})

# The malformed family: the three-field parser's own two codes. On the read
# route these are the *only* codes that may mean "the client said something
# wrong"; everything else it can raise describes retained state.
_MALFORMED_CODES = frozenset({INVALID_VERIFICATION_BODY, INVALID_VERIFICATION_FIELD})

# The storage failures the read route publishes as one closed code. They are
# named classes, not a category guess: `StoreCorruptError` is persisted state
# that cannot be safely interpreted, `StoreLockedError` is an unavailable
# lease, `CaptureObservationError` is a stored record this owner will not
# project, and `OSError` is the ordinary file failure underneath all of them.
#
# The two synchronisation refusals are folded in with them, for this new route
# only. The runtime already answers some direct loads of an external change as
# an unreadable history, so letting the same condition surface as a 409 from a
# different phase of the same read would make the outcome depend on where the
# store happened to notice it. This is the new route's own boundary and it
# changes no shared dispatch: every released route keeps `store_sync_required`
# and its 409 exactly as it shipped.
_READ_STORAGE_FAILURES = (
    CaptureObservationError,
    StoreAdoptionConflictError,
    StoreCorruptError,
    StoreExternalChangeError,
    StoreLockedError,
    OSError,
)

_RECORD_MESSAGE = "the source check record request was refused"
_HISTORY_MESSAGE = "the saved source check request was refused"
_VERIFIER_MESSAGE = "the knowledge verifier did not return a usable observation"
_QUERY_MESSAGE = "the saved source check query is invalid"
_SAVE_UNKNOWN_MESSAGE = (
    "the source check ran but its record could not be confirmed as saved; "
    "this does not mean that nothing was written"
)
_READ_MESSAGE = "the saved source check could not be read"


def _record_status(code: str) -> int:
    """The released verification mapping, plus this route's one new code."""

    if code == OBSERVATION_SAVE_UNKNOWN:
        return 503
    if code == UNKNOWN_CAPTURE:
        return 404
    if code in _CONFLICT_CODES:
        return 409
    if code in _UNAVAILABLE_CODES:
        return 503
    if code in _BAD_GATEWAY_CODES:
        return 502
    return 400


def _record_refusal(error: Any) -> RequestError:
    """One closed record refusal, as an envelope that echoes no submitted value."""

    code = getattr(error, "code", "")
    status = _record_status(code)
    if code == OBSERVATION_SAVE_UNKNOWN:
        return RequestError(code, _SAVE_UNKNOWN_MESSAGE, status)
    message = _VERIFIER_MESSAGE if status in (502, 503) else _RECORD_MESSAGE
    return RequestError(code, message, status, dict(getattr(error, "details", {})))


def _read_refusal(error: Any) -> RequestError:
    """One closed history refusal.

    The identity families keep their released codes, because the client has to
    tell them apart: ``unknown_capture`` is a 404 about a Capture, and it is
    emphatically not the ``not_found`` a server built before this route existed
    answers with. Everything else this read can refuse describes *retained
    state* rather than the request -- an invalid persisted record, a model this
    owner will not project -- and all of it becomes the one closed
    ``observation_read_unavailable``, so no internal spelling reaches the wire.
    """

    code = getattr(error, "code", "")
    if code == UNKNOWN_CAPTURE:
        status = 404
    elif code in _CONFLICT_CODES:
        status = 409
    elif code in _MALFORMED_CODES:
        status = 400
    else:
        return RequestError(OBSERVATION_READ_UNAVAILABLE, _READ_MESSAGE, 503)
    return RequestError(
        code, _HISTORY_MESSAGE, status, dict(getattr(error, "details", {}))
    )


def _confirmed_save(projection: Any) -> None:
    """A 200 ``observation_recorded`` is a *confirmed* save, or it is not sent.

    The contract for this outcome is exact: a non-null observation whose
    binding state is ``unchanged``, because the record was written under the
    same transaction that re-proved the binding. A projection that says
    anything else is not a saved observation this route may announce, and the
    honest answer is the conservative one -- the save could not be confirmed,
    which does not claim that nothing was written.
    """

    observation = (
        projection.get("observation") if isinstance(projection, Mapping) else None
    )
    if (
        not isinstance(observation, Mapping)
        or observation.get("binding_state") != "unchanged"
    ):
        raise RequestError(OBSERVATION_SAVE_UNKNOWN, _SAVE_UNKNOWN_MESSAGE, 503)


class CaptureObservationHttpMixin(KnowledgeVerificationHttpMixin):
    """The saved source-observation routes the loopback handler mixes in.

    It inherits the released verification mixin for one reason: the owner's
    single verification gate is reached through the same
    :meth:`_verification_guard` accessor, and a second check surface must take
    the *same* gate rather than an equivalent-looking one of its own. Nothing
    on the released route is overridden, extended or re-registered here.
    """

    # -- targets ---------------------------------------------------------

    def _require_exact_record_check_target(self) -> None:
        """The record route is its exact path, and publishes no parameter.

        The v1 POST dispatch routes on ``urlparse(self.path).path``, so an
        otherwise authorized ``.../record-check?unexpected=1`` would reach this
        handler having been read as the canonical path with its query silently
        discarded. Recording a check takes no parameter of any kind, so a
        target carrying one -- a query, a bare ``?``, a fragment, a ``;params``
        run or a trailing slash -- is a target this route never published.
        """

        if self.path != RECORD_CHECK_PATH:
            raise RequestError("not_found", "API endpoint not found", 404)
        # Strictly weaker than the comparison above, and applied anyway: this
        # surface's released rule is that a knowledge route answers only its
        # own canonical spelling.
        self._require_canonical_knowledge_path(RECORD_CHECK_PATH)

    def _require_exact_observation_target(self) -> None:
        """The read route is its exact raw path plus a query, and nothing else.

        The comparison is against the *raw* request-target, before any
        normalization: ``urlparse`` would move a trailing ``;params`` run out of
        the path and drop a fragment, and percent-decoding would make several
        spellings resolve to one route. This route answers the one canonical
        path it published, so every alias gets the unknown-route answer here --
        before the query is even looked at, and before any Store read.
        """

        if "#" in self.path:
            raise RequestError("not_found", "API endpoint not found", 404)
        if self.path.partition("?")[0] != OBSERVATION_PATH:
            raise RequestError("not_found", "API endpoint not found", 404)
        # As above: strictly weaker, and left in place as the surface rule.
        self._require_canonical_knowledge_path(OBSERVATION_PATH)

    # -- the read route's query ------------------------------------------

    def _admitted_observation_query(self, query: str) -> tuple[str, str, int]:
        """The three query keys, admitted by the released three-field parser.

        Shape first, in the shared single-valued-query helper the read surface
        already uses: exactly the three published keys, each present exactly
        once, so a duplicate, an unknown key and a missing key are one refusal.
        Then the values, which must be nonempty and -- for the revision -- one
        canonical base-10 unsigned integer rather than any spelling that
        happens to parse. Only then is the identity handed to
        ``parse_verification_body``, the same admission the POST body gets, so
        the two routes cannot drift into accepting different identities.

        Nothing here reads the Store, and the refusal is one closed
        ``invalid_query`` whose details carry at most the *name* of one of the
        three published keys.
        """

        allowed = set(OBSERVATION_QUERY_FIELDS)
        parsed = _single_valued_query(
            query, allowed, {name: [] for name in OBSERVATION_QUERY_FIELDS}, _QUERY_MESSAGE
        )
        values = {name: parsed[name][0] for name in OBSERVATION_QUERY_FIELDS}
        if not all(values.values()):
            raise RequestError("invalid_query", _QUERY_MESSAGE, 400)
        revision = values["capture_revision"]
        if (
            len(revision) > _MAX_REVISION_DIGITS
            or _CANONICAL_REVISION.fullmatch(revision) is None
        ):
            raise RequestError("invalid_query", _QUERY_MESSAGE, 400)
        try:
            return parse_verification_body(
                {
                    "workspace_uid": values["workspace_uid"],
                    "capture_id": values["capture_id"],
                    "capture_revision": int(revision),
                }
            )
        except VerificationRuntimeError as error:
            raise RequestError(
                "invalid_query", _QUERY_MESSAGE, 400, dict(error.details)
            ) from error

    # -- the routes ------------------------------------------------------

    def _post_knowledge_capture_record_check(
        self,
        path: str,
        match: re.Match[str],
        body: dict[str, Any],
        request_digest: str,
        idempotency_key: str,
    ) -> None:
        self._require_exact_record_check_target()
        self._refuse_unsupported_idempotency_key(
            "the source check record route accepts no Idempotency-Key"
        )
        try:
            workspace_uid, capture_id, capture_revision = parse_verification_body(body)
        except VerificationRuntimeError as error:
            raise _record_refusal(error) from error
        store = self._knowledge_store()
        guard = self._verification_guard()
        try:
            projection = record_capture_source_check(
                store,
                workspace_uid=workspace_uid,
                capture_id=capture_id,
                capture_revision=capture_revision,
                drivers=self.server.knowledge_drivers,
                guard=guard,
                clock=getattr(self.server, "knowledge_clock", utc_now_rfc3339),
            )
        except (KnowledgeVerificationGuardError, VerificationRuntimeError) as error:
            raise _record_refusal(error) from error
        _confirmed_save(projection)
        # `data` is the runtime's projection and nothing else: the stored
        # binding, the two historical times, the binding state and the closed
        # protocol result. There is no stored record, no request envelope, no
        # capture digest, no verifier command, environment or alias, and `meta`
        # says only that a past observation was recorded.
        self.send_json(
            {"data": projection, "meta": {"outcome": "observation_recorded"}}
        )

    def _get_knowledge_capture_observation(
        self, parsed: Any, match: re.Match[str]
    ) -> None:
        self._require_exact_observation_target()
        workspace_uid, capture_id, capture_revision = self._admitted_observation_query(
            parsed.query
        )
        store = self._knowledge_store()
        try:
            projection = get_capture_source_observation(
                store,
                workspace_uid=workspace_uid,
                capture_id=capture_id,
                capture_revision=capture_revision,
                clock=getattr(self.server, "knowledge_clock", utc_now_rfc3339),
            )
        except VerificationRuntimeError as error:
            raise _read_refusal(error) from error
        except _READ_STORAGE_FAILURES as error:
            # An invalid persisted record, or an ordinary storage failure. The
            # `try` covers the WHOLE runtime call on purpose, so a refusal
            # raised while the outer transaction is being entered, while a
            # later re-admission loads a document, or while that transaction is
            # being exited lands here rather than escaping as a 500 -- and none
            # of them is ever quietly reported as a changed binding.
            #
            # The catch is deliberately narrow and lives on this route only: it
            # is not BaseException, it is not a bare `Exception`, and it adds no
            # handling to any other endpoint -- the released routes keep their
            # own envelopes, including `store_sync_required`, untouched.
            # Nothing of the failure travels: no message, path, or stack.
            raise RequestError(
                OBSERVATION_READ_UNAVAILABLE, _READ_MESSAGE, 503
            ) from error
        # Having kept nothing is a 200 whose `observation` is null. It is not a
        # 404: the Capture was found, and "no saved check" is the answer rather
        # than a missing route or a missing Capture.
        self.send_json({"data": projection, "meta": {"outcome": "observation_ready"}})
