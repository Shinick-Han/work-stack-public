"""Owner record-check and historical projection of a saved source observation.

This module is the trusted handler-facing sequence that *saves* one accepted
check, and the later read that projects that saved row without running a
source. It is not HTTP, not a Store implementation, and not a refresh.

The released read-only verify entry stays on
:mod:`workstack.knowledge_verification_runtime`. This module reuses that
module's admission, pin, gate, child, original-window validation and
reconfirm helper. Source I/O still happens outside the Store lock. After the
child, one outer transaction re-admits, compares the original capture digest
and non-timestamp authority facts, and only then calls the Store's trusted
``record_capture_observation`` sink.

A saved observation is historical evidence. History reads never consult a
verifier command or environment, never renew a window, never promote
freshness, and never start a child. ``binding_state`` ``unchanged`` means the
saved authority facts still agree with the capture the owner holds now, not
that the source is currently reachable.

Persistence that cannot be confirmed is ``observation_save_unknown``. That
code does not claim zero writes. There is no second child, no automatic
retry and no invented idempotency receipt after an unknown save.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .capture_observations import (
    OBSERVATION_SCHEMA,
    CaptureObservationError,
    validate_observation,
    validate_observations,
)
from .file_lease import StoreLockedError
from .knowledge_verification_admission import admit_capture_verification
from .knowledge_verification_runtime import (
    CAPTURE_REVISION_CHANGED,
    UNKNOWN_CAPTURE,
    VERIFICATION_BINDING_MISMATCH,
    VerificationRuntimeError,
    _ADMISSION_REFUSALS,
    _allocated_verification_id,
    _run_capture_source_check,
    _same_authority,
    parse_verification_body,
)
from .store_errors import (
    StoreAdoptionConflictError,
    StoreCorruptError,
    StoreExternalChangeError,
)

__all__ = (
    "OBSERVATION_READ_UNAVAILABLE",
    "OBSERVATION_SAVE_UNKNOWN",
    "get_capture_source_observation",
    "record_capture_source_check",
)

OBSERVATION_SAVE_UNKNOWN = "observation_save_unknown"
OBSERVATION_READ_UNAVAILABLE = "observation_read_unavailable"

_STORAGE_REFUSALS = (
    OSError,
    StoreLockedError,
    StoreCorruptError,
    StoreExternalChangeError,
    StoreAdoptionConflictError,
)
_SAVE_REFUSALS = _STORAGE_REFUSALS + (CaptureObservationError,)


def _identity(
    workspace_uid: Any, capture_id: Any, capture_revision: Any
) -> tuple[str, str, int]:
    return parse_verification_body(
        {
            "workspace_uid": workspace_uid,
            "capture_id": capture_id,
            "capture_revision": capture_revision,
        }
    )


def _client_binding(
    workspace_uid: str, capture_id: str, capture_revision: int
) -> dict[str, Any]:
    return {
        "workspace_uid": workspace_uid,
        "capture_id": capture_id,
        "capture_revision": capture_revision,
    }


def _projection(record: Mapping[str, Any], binding_state: str) -> dict[str, Any]:
    result = record["result"] if binding_state == "unchanged" else None
    return {
        "accepted_at": record["accepted_at"],
        "checked_at": record["result"]["checked_at"],
        "binding_state": binding_state,
        "result": result,
    }


def _persist_observation(
    store: Any, admitted: Any, result: Mapping[str, Any], accepted_at: str
) -> None:
    """Build A's six-field record and hand it to the trusted Store sink."""

    binding = admitted.document["binding"]
    record = validate_observation(
        {
            "schema": OBSERVATION_SCHEMA,
            "capture_digest": admitted.capture_digest,
            "verifier_alias": admitted.document["connection"]["alias"],
            "accepted_at": accepted_at,
            "request": admitted.document,
            "result": result,
        },
        workspace_uid=binding["workspace_uid"],
    )
    try:
        store.record_capture_observation(record)
    except _SAVE_REFUSALS as error:
        raise VerificationRuntimeError(OBSERVATION_SAVE_UNKNOWN) from error


def _require_current_capture(
    store: Any, workspace_uid: str, capture_id: str, capture_revision: int
) -> dict[str, Any]:
    """Match the three submitted fields to the store's current Capture.

    Policy, verifier configuration and source I/O are not consulted here. A
    revoked or revised policy must still be able to show that a past check
    occurred. A client describing the wrong workspace, Capture or revision
    is a refusal, not a silently reassigned row.
    """

    try:
        actual_uid = store.load("workspace.json")["id"]
        document = store.load("captures.json")
    except _STORAGE_REFUSALS as error:
        raise VerificationRuntimeError(OBSERVATION_READ_UNAVAILABLE) from error
    if actual_uid != workspace_uid:
        raise VerificationRuntimeError(VERIFICATION_BINDING_MISMATCH)
    captures = document.get("captures")
    if not isinstance(captures, list):
        raise VerificationRuntimeError(OBSERVATION_READ_UNAVAILABLE)
    capture = next(
        (
            entry
            for entry in captures
            if isinstance(entry, dict) and entry.get("id") == capture_id
        ),
        None,
    )
    if capture is None:
        raise VerificationRuntimeError(UNKNOWN_CAPTURE)
    if capture.get("revision") != capture_revision:
        raise VerificationRuntimeError(CAPTURE_REVISION_CHANGED)
    return document


def _retained_observation(
    document: Mapping[str, Any], *, capture_id: str, workspace_uid: str
) -> dict[str, Any] | None:
    version = document.get("version")
    if version == 1 and "observations" not in document:
        return None
    if version != 2:
        raise VerificationRuntimeError(OBSERVATION_READ_UNAVAILABLE)
    if "observations" not in document or document["observations"] is None:
        raise VerificationRuntimeError(OBSERVATION_READ_UNAVAILABLE)
    observations = document["observations"]
    try:
        retained = validate_observations(observations, workspace_uid=workspace_uid)
    except CaptureObservationError as error:
        raise VerificationRuntimeError(OBSERVATION_READ_UNAVAILABLE) from error
    for record in retained:
        if record["request"]["binding"]["capture_id"] == capture_id:
            return record
    return None


def _binding_state(
    store: Any,
    record: Mapping[str, Any],
    *,
    capture_id: str,
    capture_revision: int,
    clock: Callable[[], str],
) -> str:
    """Re-admit current authority; any ordinary refusal is ``changed``."""

    try:
        rechecked = admit_capture_verification(
            store,
            capture_id,
            capture_revision,
            verification_id=record["request"]["verification_id"],
            now=clock(),
        )
    except _ADMISSION_REFUSALS:
        return "changed"
    if _same_authority(record["capture_digest"], record["request"], rechecked):
        return "unchanged"
    return "changed"


def record_capture_source_check(
    store: Any,
    *,
    workspace_uid: str,
    capture_id: str,
    capture_revision: int,
    drivers: Mapping[str, Any],
    guard: Any,
    clock: Callable[[], str],
    verification_id_factory: Callable[[], str] = _allocated_verification_id,
) -> dict[str, Any]:
    """Run one owner check, persist the accepted row, and project it.

    Keyword parameters and the default nonce factory match released
    :func:`~workstack.knowledge_verification_runtime.verify_capture_source`.
    The three-field identity is re-admitted before any Store read. The
    returned mapping is ``{binding, observation}`` with ``binding_state``
    ``unchanged`` after a confirmed save.
    """

    workspace_uid, capture_id, capture_revision = _identity(
        workspace_uid, capture_id, capture_revision
    )
    admitted, result, accepted_at = _run_capture_source_check(
        store,
        workspace_uid=workspace_uid,
        capture_id=capture_id,
        capture_revision=capture_revision,
        drivers=drivers,
        guard=guard,
        clock=clock,
        verification_id_factory=verification_id_factory,
        on_confirmed=_persist_observation,
    )
    return {
        "binding": dict(admitted.document["binding"]),
        "observation": _projection(
            {"accepted_at": accepted_at, "result": result}, "unchanged"
        ),
    }


def get_capture_source_observation(
    store: Any,
    *,
    workspace_uid: str,
    capture_id: str,
    capture_revision: int,
    clock: Callable[[], str],
) -> dict[str, Any]:
    """Return the saved observation for one current Capture binding, or null.

    One owner transaction. No child, guard, verifier configuration, save,
    backup or format activation. The stored result is validated at its
    original ``accepted_at``, never against today's clock.
    """

    workspace_uid, capture_id, capture_revision = _identity(
        workspace_uid, capture_id, capture_revision
    )
    binding = _client_binding(workspace_uid, capture_id, capture_revision)
    with store.transaction():
        document = _require_current_capture(
            store, workspace_uid, capture_id, capture_revision
        )
        record = _retained_observation(
            document, capture_id=capture_id, workspace_uid=workspace_uid
        )
        if record is None:
            return {"binding": binding, "observation": None}
        state = _binding_state(
            store,
            record,
            capture_id=capture_id,
            capture_revision=capture_revision,
            clock=clock,
        )
        return {
            "binding": binding,
            "observation": _projection(record, state),
        }
