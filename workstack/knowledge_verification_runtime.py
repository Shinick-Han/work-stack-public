"""One explicit owner check of one stored Capture's source versions.

This module is the *sequence* the R21 wiring contract chose, not any of its
steps. The R20 wave built those separately and had them reviewed on their own
terms -- the closed verification protocol, the read-only owner admission, the
operator's optional verifier binding and the bounded child transport -- and
what remains is the order one owner process is allowed to perform them in:

1. admit the browser's three-field body into an identity this owner can look
   up, before any Store transaction is opened;
2. under one Store transaction, admit the stored Capture revision against the
   live ledger, prove the submitted binding is the one the store actually
   holds, and choose the verifier the operator separately opted this alias
   into;
3. release the transaction, take the owner's single verification gate, and run
   the pinned child exactly once with the request document on its stdin;
4. validate the child's untrusted stdout against the request *this owner sent*
   and the actual post-I/O clock, then admit again and refuse to display an
   observation if the capture, the ledger or the policy moved while the child
   ran.

**Nothing here is saved, and nothing here is a refresh.** No Capture is
written, no Task is touched, no ledger record changes, no SSOT version moves
and no response is cached. What comes back is an observation of stored evidence
at one timestamp -- it does not update the saved answer, and it never claims
the summary above that evidence is correct.

**The browser cannot widen the check.** The body carries a workspace UUID, a
capture identifier and a revision, and nothing else exists to send: the corpus,
the connection, the evidence handles, the expected versions, the verifier's
argv and its environment are all derived here, from the owner's own store and
the operator's own configuration. A field a caller adds is a refused body, not
an ignored one.

**Search and verification are separate opt-ins.** The alias' search driver
being configured says nothing about whether it may be verified, and this module
consults only ``KnowledgeDriverBinding.verification``. An alias without one is
:data:`VERIFIER_UNAVAILABLE`, which is a reportable state rather than a reason
to fall back to the search command. No search attempt is registered, consumed
or consulted anywhere below.

**The window is the one the admission opened, and it is never renewed.** The
post-I/O re-admission builds a *fresh* document with its own clock so that the
capture digest and the authority facts can be recomputed, but the result is
validated against the ORIGINAL request, so a child that answers after the
original sixty seconds have passed is refused rather than accepted under a
renewed window. Only the non-timestamp facts of the two admissions are
compared.

**An unknown outcome is not retried.** A child whose cleanup this owner cannot
confirm latches the gate for the life of the process, and a child whose outcome
cannot be described simply refuses. Neither reruns anything: the user may
explicitly ask for a new read-only check after any settled outcome, and that is
the whole of the recovery model.

Every refusal is a :class:`VerificationRuntimeError` carrying one closed code
and, at most, the *name* of a field in the closed request body. The child's
stdout, its stderr, the pinned command, the environment, the store path, the
evidence handles and the exact protocol or admission code that was refused are
all dropped at this boundary.
"""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Any, Callable, Mapping

from .bounded_process_exchange import CLEANUP_SETTLED
from .capture import CaptureValidationError
from .capture_retrieval import CaptureRetrievalError
from .knowledge_driver_exchange import (
    DRIVER_OUTCOME_UNKNOWN,
    MAX_PAYLOAD_BYTES,
    run_knowledge_driver,
)
from .knowledge_execution_runtime import KnowledgeDriverBinding
from .knowledge_ledger_document import (
    CAPTURE_ID_RE,
    MAX_POLICY_REVISION,
    KnowledgeLedgerError,
)
from .knowledge_request import KnowledgeRequestError, canonical_uuid
from .knowledge_verification_admission import (
    KnowledgeVerificationAdmissionError,
    admit_capture_verification,
)
from .knowledge_verification_guard import KnowledgeVerificationGuard
from .knowledge_verification_protocol import (
    MAX_VERIFICATION_BYTES,
    VerificationError,
    validate_verification_result,
)

__all__ = (
    "BODY_FIELDS",
    "CAPTURE_NOT_VERIFIABLE",
    "CAPTURE_REVISION_CHANGED",
    "INVALID_VERIFICATION_BODY",
    "INVALID_VERIFICATION_FIELD",
    "UNKNOWN_CAPTURE",
    "VERIFICATION_AUTHORITY_CHANGED",
    "VERIFICATION_BINDING_MISMATCH",
    "VERIFICATION_RESULT_REFUSED",
    "VERIFICATION_TIMEOUT_SECONDS",
    "VERIFIER_UNAVAILABLE",
    "VerificationRuntimeError",
    "parse_verification_body",
    "verify_capture_source",
)


#: The acceptance budget for one verifier child. It is deliberately below the
#: sixty-second envelope the admission opens: a result that arrives after the
#: window has closed is refused by the protocol validator anyway, so waiting
#: past this point could only turn an unknown outcome into a refused one.
VERIFICATION_TIMEOUT_SECONDS = 45.0

#: The exact three fields a browser may send. Anything else is a refused body.
BODY_FIELDS = frozenset({"workspace_uid", "capture_id", "capture_revision"})

#: Closed codes this module owns. The transport's ``driver_not_started`` and
#: ``driver_outcome_unknown`` travel unchanged, and the gate's own two codes
#: come from :mod:`workstack.knowledge_verification_guard`.
VERIFIER_UNAVAILABLE = "knowledge_verifier_unavailable"
VERIFICATION_BINDING_MISMATCH = "verification_binding_mismatch"
CAPTURE_REVISION_CHANGED = "capture_revision_changed"
UNKNOWN_CAPTURE = "unknown_capture"
CAPTURE_NOT_VERIFIABLE = "capture_not_verifiable"
VERIFICATION_AUTHORITY_CHANGED = "verification_authority_changed"
VERIFICATION_RESULT_REFUSED = "verification_result_refused"
INVALID_VERIFICATION_BODY = "invalid_body"
INVALID_VERIFICATION_FIELD = "invalid_request"

# The refusals the owner admission and the released ledger, capture and request
# validators may raise. They are caught as a family so a new one cannot escape
# this boundary as an unhandled type and reach the client as a 500.
_ADMISSION_REFUSALS = (
    CaptureRetrievalError,
    CaptureValidationError,
    KnowledgeLedgerError,
    KnowledgeRequestError,
    KnowledgeVerificationAdmissionError,
    VerificationError,
)

# The categories the R21 contract publishes, keyed by the admission code that
# means each one. Everything absent from this table is `capture_not_verifiable`
# -- the honest default for a stored Capture this owner will not check -- and
# every refusal of the *second* admission is `verification_authority_changed`
# regardless of its code, because reaching that admission at all means the
# state moved while a child was running.
_ADMISSION_CATEGORIES = {
    "unknown_capture": UNKNOWN_CAPTURE,
    "capture_record_missing": UNKNOWN_CAPTURE,
    "capture_revision_changed": CAPTURE_REVISION_CHANGED,
    "capture_revision_mismatch": CAPTURE_REVISION_CHANGED,
    "policy_revision_changed": VERIFICATION_AUTHORITY_CHANGED,
    "workspace_mismatch": VERIFICATION_AUTHORITY_CHANGED,
}

# Everything else the admission may say -- a Capture whose historical request
# record is missing, one bound to no completed record, one whose stored source
# binding disagrees with that record, a connection the roster no longer holds,
# a legacy 1.0 Capture, a malformed stored projection -- lands on the default.
# All of them mean the same publishable thing: this stored Capture is not one
# this owner will check, and no amount of retrying changes that.

# The verification request is identity plus at most ten opaque handles, so it
# fits both bounds with room to spare. The tighter of the two is applied so a
# document this module composes can never be one the transport or the protocol
# decoder would refuse further down.
_MAX_REQUEST_BYTES = min(MAX_PAYLOAD_BYTES, MAX_VERIFICATION_BYTES)


class VerificationRuntimeError(ValueError):
    """Closed verification refusal: one code, and no submitted value.

    ``field`` is the *name* of a field in the closed three-field request body
    and is the only detail that ever travels. No capture identifier, revision,
    workspace UUID, evidence handle, digest, command, environment value or
    child byte is carried here.
    """

    def __init__(self, code: str, field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details: dict[str, Any] = {} if field is None else {"field": field}


def _allocated_verification_id() -> str:
    """This server's nonce for one check. Not the caller's, and not stored."""

    return str(uuid.uuid4())


def _admitted_workspace_uid(value: Any) -> str:
    try:
        return canonical_uuid(value, "workspace_uid")
    except KnowledgeRequestError as error:
        raise VerificationRuntimeError(
            INVALID_VERIFICATION_FIELD, "workspace_uid"
        ) from error


def _admitted_capture_id(value: Any) -> str:
    if not isinstance(value, str) or not CAPTURE_ID_RE.fullmatch(value):
        raise VerificationRuntimeError(INVALID_VERIFICATION_FIELD, "capture_id")
    return value


def _admitted_capture_revision(value: Any) -> int:
    """A nonnegative, exactly representable integer. ``True`` is not one."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise VerificationRuntimeError(INVALID_VERIFICATION_FIELD, "capture_revision")
    if not 0 <= value <= MAX_POLICY_REVISION:
        raise VerificationRuntimeError(INVALID_VERIFICATION_FIELD, "capture_revision")
    return value


def parse_verification_body(body: Any) -> tuple[str, str, int]:
    """The whole of what a browser may say, admitted before any Store read.

    Exactly three fields, each in the grammar the store already spells them in.
    A body with a fourth field is refused rather than having that field
    ignored: there is no corpus, connection, evidence, command, environment,
    verifier alias or configuration path a caller could add here, and silently
    dropping one would make this route look wider than it is.
    """

    if not isinstance(body, Mapping) or set(body) != BODY_FIELDS:
        raise VerificationRuntimeError(INVALID_VERIFICATION_BODY)
    return (
        _admitted_workspace_uid(body["workspace_uid"]),
        _admitted_capture_id(body["capture_id"]),
        _admitted_capture_revision(body["capture_revision"]),
    )


def _admission_category(error: Any) -> str:
    """One owner-admission refusal, as a category this route publishes."""

    code = getattr(error, "code", "")
    return _ADMISSION_CATEGORIES.get(code, CAPTURE_NOT_VERIFIABLE)


def _admitted_capture(
    store: Any,
    capture_id: str,
    capture_revision: int,
    *,
    verification_id: str,
    now: str,
) -> Any:
    """The released owner admission, with its codes reduced to categories."""

    try:
        return admit_capture_verification(
            store,
            capture_id,
            capture_revision,
            verification_id=verification_id,
            now=now,
        )
    except _ADMISSION_REFUSALS as error:
        raise VerificationRuntimeError(_admission_category(error)) from error


def _require_submitted_binding(
    admitted: Any, workspace_uid: str, capture_id: str, capture_revision: int
) -> None:
    """The store's own binding is the one the browser said it was looking at.

    The admission derives the binding from the actual workspace document and
    the stored Capture, so a disagreement here means the client is describing a
    different workspace or a different revision than the one this owner holds.
    That is a conflict about state, not a malformed body.
    """

    binding = admitted.document["binding"]
    if (
        binding["workspace_uid"] != workspace_uid
        or binding["capture_id"] != capture_id
        or binding["capture_revision"] != capture_revision
    ):
        raise VerificationRuntimeError(VERIFICATION_BINDING_MISMATCH)


def _pinned_verifier(drivers: Any, admitted: Any) -> Any:
    """The verifier the operator separately opted this admitted alias into.

    An alias the operator never configured, and an alias whose driver carries
    no ``verification`` binding, are the same answer: this connection has no
    verifier, and no child runs. An alias whose pinned upstream workspace is
    not the one the ledger's roster currently names is a mismatch -- the
    operator's configuration and the owner's policy disagree about where this
    alias points, and nothing is started while they do.
    """

    connection = admitted.document["connection"]
    binding = drivers.get(connection["alias"]) if isinstance(drivers, Mapping) else None
    if not isinstance(binding, KnowledgeDriverBinding):
        raise VerificationRuntimeError(VERIFIER_UNAVAILABLE)
    verification = getattr(binding, "verification", None)
    if verification is None:
        raise VerificationRuntimeError(VERIFIER_UNAVAILABLE)
    if binding.upstream_workspace_uid != connection["upstream_workspace_uid"]:
        raise VerificationRuntimeError(VERIFICATION_BINDING_MISMATCH)
    return verification


def _verifier_payload(document: Mapping[str, Any]) -> bytes:
    """The child's whole stdin: the validated verification request, and nothing.

    Compact canonical UTF-8. There is no wrapper envelope, no connection secret
    and no store path here -- the request document the admission built is
    exactly what the adapter's own validator expects to read.
    """

    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if not 0 < len(payload) <= _MAX_REQUEST_BYTES:
        # Unreachable for any document the protocol admitted -- identity plus
        # at most ten bounded handles -- and reported as a property of this
        # stored Capture rather than of the caller's three-field body, which is
        # what it would actually be.
        raise VerificationRuntimeError(CAPTURE_NOT_VERIFIABLE)
    return payload


def _bounded_observation(
    verifier: Any, payload: bytes, guard: KnowledgeVerificationGuard
) -> bytes:
    """One child, once, and a closed judgement about what may follow it.

    A cleanup the transport could not confirm is latched on the gate *before*
    the refusal is raised, so no later check can pile onto a child this owner
    can no longer account for. Every other failure is settled: it says the
    outcome is unknown, not that the process is unaccounted for, and the gate
    is released for an explicit new check by the user.
    """

    outcome = run_knowledge_driver(
        verifier.command,
        payload,
        environment=verifier.environment,
        timeout_seconds=VERIFICATION_TIMEOUT_SECONDS,
    )
    if outcome.cleanup != CLEANUP_SETTLED:
        guard.latch_unsettled()
        raise VerificationRuntimeError(DRIVER_OUTCOME_UNKNOWN)
    if outcome.error_code is not None:
        raise VerificationRuntimeError(outcome.error_code)
    return outcome.stdout


def _validated_observation(
    stdout: bytes, request: Mapping[str, Any], now: str
) -> dict[str, Any]:
    """The child's untrusted bytes, projected through the closed protocol.

    ``request`` is the document this owner actually sent, retained across the
    child I/O, so the nonce, the evidence count, the order, each handle, each
    source kind and each expected version are checked against it rather than
    against anything the child said. ``now`` is the actual post-I/O clock, and
    the window is the original one: nothing is renewed here.
    """

    try:
        return validate_verification_result(stdout, request=request, now=now)
    except VerificationError as error:
        raise VerificationRuntimeError(VERIFICATION_RESULT_REFUSED) from error


def _authority_facts(document: Mapping[str, Any]) -> dict[str, Any]:
    """Everything about a request except when it was asked.

    The two admissions are taken at different instants and therefore carry
    different ``requested_at`` and ``expires_at`` values by construction.
    Everything else -- the schema, this check's nonce, the capture binding, the
    connection and its policy revision, the corpus scope and every evidence
    handle with its expected version -- must be identical, or the authority
    this observation was taken under is not the authority that exists now.
    """

    return {
        name: value
        for name, value in document.items()
        if name not in ("requested_at", "expires_at")
    }


def _same_authority(capture_digest: str, request: Mapping[str, Any], rechecked: Any) -> bool:
    """True when the capture digest and non-timestamp facts still agree."""

    if not secrets.compare_digest(capture_digest, rechecked.capture_digest):
        return False
    return _authority_facts(request) == _authority_facts(rechecked.document)


def _reconfirmed(
    store: Any,
    admitted: Any,
    *,
    verification_id: str,
    clock: Callable[[], str],
    on_confirmed: Callable[[], None] | None = None,
) -> None:
    """Admit the same capture again, after the child, and compare the facts.

    Any refusal at all here is reported as a changed authority: this admission
    succeeded once already, so the only thing that can have refused it now is
    the state moving while the child ran. The comparison is over the
    deterministic capture digest and the non-timestamp request facts -- never
    over the renewed window, which is expected to differ.

    Compare and the optional trusted sink run before this transaction
    releases. The public read-only entry never selects a sink.
    """

    binding = admitted.document["binding"]
    with store.transaction():
        try:
            rechecked = admit_capture_verification(
                store,
                binding["capture_id"],
                binding["capture_revision"],
                verification_id=verification_id,
                now=clock(),
            )
        except _ADMISSION_REFUSALS as error:
            raise VerificationRuntimeError(VERIFICATION_AUTHORITY_CHANGED) from error
        if not _same_authority(admitted.capture_digest, admitted.document, rechecked):
            raise VerificationRuntimeError(VERIFICATION_AUTHORITY_CHANGED)
        if on_confirmed is not None:
            on_confirmed()


def _run_capture_source_check(
    store: Any,
    *,
    workspace_uid: str,
    capture_id: str,
    capture_revision: int,
    drivers: Mapping[str, KnowledgeDriverBinding],
    guard: KnowledgeVerificationGuard,
    clock: Callable[[], str],
    verification_id_factory: Callable[[], str] = _allocated_verification_id,
    on_confirmed: Callable[[Any, Any, dict[str, Any], str], None] | None = None,
) -> tuple[Any, dict[str, Any], str]:
    """One owner check: child outside the store lock, reconfirm inside it.

    ``on_confirmed`` is a trusted internal sink invoked after reconfirmation
    while that outer transaction still holds. The released public verify
    entry never selects it.
    """

    verification_id = verification_id_factory()
    with store.transaction():
        admitted = _admitted_capture(
            store,
            capture_id,
            capture_revision,
            verification_id=verification_id,
            now=clock(),
        )
        _require_submitted_binding(admitted, workspace_uid, capture_id, capture_revision)
        verifier = _pinned_verifier(drivers, admitted)
        payload = _verifier_payload(admitted.document)
    # The transaction is released here, deliberately, before the gate is taken
    # and before any child I/O: nothing this owner does while waiting on a
    # child holds the store, so an unrelated read or write can proceed for the
    # whole time the child is running.
    #
    # The acquire is OUTSIDE the try on purpose. `settle` releases whatever the
    # gate currently holds, so only the invocation that actually took it may
    # call it; a busy or latched check raises here, never enters the block
    # below, and therefore cannot release the check that is running.
    guard.acquire()
    try:
        stdout = _bounded_observation(verifier, payload, guard)
        accepted_at = clock()
        result = _validated_observation(stdout, admitted.document, accepted_at)

        def confirm() -> None:
            if on_confirmed is not None:
                on_confirmed(store, admitted, result, accepted_at)

        _reconfirmed(
            store,
            admitted,
            verification_id=verification_id,
            clock=clock,
            on_confirmed=confirm if on_confirmed is not None else None,
        )
        return admitted, result, accepted_at
    finally:
        # Settled outcomes release the gate for an explicit new user check; a
        # latched instance stays latched, and neither branch reruns anything.
        guard.settle()


def verify_capture_source(
    store: Any,
    *,
    workspace_uid: str,
    capture_id: str,
    capture_revision: int,
    drivers: Mapping[str, KnowledgeDriverBinding],
    guard: KnowledgeVerificationGuard,
    clock: Callable[[], str],
    verification_id_factory: Callable[[], str] = _allocated_verification_id,
) -> dict[str, Any]:
    """Run one explicit owner check and return one bounded observation.

    ``clock`` is the caller's injected clock -- the server's, in production the
    UTC wall clock. It is read once for the admission that opens the window,
    once for the result validation after the child, and once for the admission
    that re-proves the authority, so the later readings are what decide whether
    the observation is still inside its window and still describes the state
    the store holds.

    The returned mapping is ``{"binding": ..., "result": ...}``: the stored
    binding this check was about, and the validated closed protocol result. It
    is not saved anywhere, and it asserts nothing beyond what was observed at
    ``result["checked_at"]``.
    """

    admitted, result, _accepted_at = _run_capture_source_check(
        store,
        workspace_uid=workspace_uid,
        capture_id=capture_id,
        capture_revision=capture_revision,
        drivers=drivers,
        guard=guard,
        clock=clock,
        verification_id_factory=verification_id_factory,
    )
    return {"binding": dict(admitted.document["binding"]), "result": result}
