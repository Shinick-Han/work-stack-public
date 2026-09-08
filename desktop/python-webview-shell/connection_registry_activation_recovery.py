"""Read-only startup detection and explicit connection-activation recovery.

Startup inspection never changes the connection registry or activation records.
It only advertises recovery when one exact, unconfirmed activation is bound to
the currently active registry, or reconciliation when a duplicate set left by an
older build provably reduces to exactly that.  Both remain explicit caller
actions and are delegated to :class:`ConnectionRegistryMutationService` for
their CAS writes.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from connection_registry_mutations import (
    ACTIVATION_DIRECTORY,
    MAX_ACTIVATION_RECORDS,
    ActivationReceipt,
    ActivationReconciliationOutcome,
    ConnectionRegistry,
    ConnectionRegistryMutationService,
    RegistryConflictError,
    current_registry_snapshot,
    load_activation_receipt,
    plan_activation_reconciliation,
    profile_digest,
    reconciliation_proved_zero_write,
    validate_activation_rollback,
)


RecoveryState: TypeAlias = Literal[
    "none", "recovery_required", "can_reconcile", "blocked"
]

_SAFE_MESSAGES = {
    "no_recovery": "No connection activation requires recovery.",
    "recovery_required": (
        "An unconfirmed connection activation can be restored explicitly."
    ),
    "reconcile_required": (
        "Duplicate connection activation records can be closed explicitly."
    ),
    "multiple_pending_activations": (
        "Multiple connection activations require manual review."
    ),
    "stale_activation": (
        "Connection activation evidence does not match the current registry."
    ),
    "invalid_recovery_evidence": (
        "Connection activation recovery evidence is invalid."
    ),
    "recovery_conflict": (
        "Connection activation changed; inspect recovery status again."
    ),
    "recovery_not_allowed": (
        "This connection activation is not eligible for recovery."
    ),
}


_ADVERTISED_CODES = frozenset({"no_recovery", "recovery_required", "reconcile_required"})

# Fixed copy for what a reconciliation actually left behind.  These describe a
# completed attempt, never an advertised action, so none of them may be reached
# by a click and none of them is ever built from evidence.
_RECONCILIATION_MESSAGES = {
    "resumed": "Duplicate connection activation records were closed.",
    "incomplete": (
        "Some duplicate connection records were closed and others were not. "
        "Every record and rollback file was kept. Close Work Stack and inspect "
        "the connection again."
    ),
    "changed": (
        "The duplicate connection records were closed and the connection "
        "activation then changed. Close Work Stack and inspect the connection "
        "again."
    ),
    "uncertain": (
        "Work Stack could not confirm the connection records after this "
        "change. Close Work Stack and review the connection records manually."
    ),
}

#: The one fixed sentence for a reconciliation that could not be confirmed.
#: The action boundary renders it for an unexpected failure of its own, so both
#: layers describe an unconfirmed reconciliation with the same words.
UNCERTAIN_RECONCILIATION_MESSAGE = _RECONCILIATION_MESSAGES["uncertain"]


class ActivationRecoveryRefusedError(RuntimeError):
    """A sanitized, fail-closed refusal suitable for a native host response."""

    def __init__(self, code: str) -> None:
        if code not in _SAFE_MESSAGES or code in _ADVERTISED_CODES:
            code = "recovery_not_allowed"
        self.code = code
        self.safe_message = _SAFE_MESSAGES[code]
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class ActivationRecoveryStatus:
    state: RecoveryState
    code: str
    message: str
    can_restore: bool
    activation_id: str | None = None
    profile_id: str | None = None
    current_registry_digest: str | None = None
    can_reconcile: bool = False


@dataclass(frozen=True)
class ActivationReconciliationReport:
    """A truthful account of one reconciliation and the page it may offer.

    ``resumed`` is the only state that may be acted on, and its status is the
    freshly inspected restore status: the core committed every record it
    planned and what remains is still exactly the advertised recovery.  Every
    other state describes a change on disk that no longer matches what the page
    advertised, so it offers no action at all.

    ``incomplete`` and ``uncertain`` also carry a status whenever the fresh
    inspection that follows a partial attempt could prove one.  That status is
    the exact current one and exists so a report never has to guess, but it is
    never an offer: only ``resumed`` may reach a click.
    """

    state: Literal["resumed", "incomplete", "changed", "uncertain"]
    message: str
    status: ActivationRecoveryStatus | None = None


@dataclass(frozen=True)
class ActivationRecoveryResult:
    state: Literal["restored"]
    activation_id: str
    profile_id: str
    restored_registry_digest: str


class ConnectionRegistryActivationRecoveryService:
    """Bounded startup inspector plus explicit, exact recovery operations."""

    def __init__(
        self,
        state_root: Path,
        *,
        mutation_service: ConnectionRegistryMutationService | None = None,
    ) -> None:
        self._state_root = Path(state_root)
        self._mutations = mutation_service or ConnectionRegistryMutationService(
            self._state_root
        )

    def inspect(self) -> ActivationRecoveryStatus:
        """Return sanitized status without writing any local or SSOT state."""

        try:
            receipts = _pending_receipts(self._state_root)
            if not receipts:
                return _status("none", "no_recovery")
            current, current_digest = current_registry_snapshot(self._state_root)
            matching = [
                receipt
                for receipt in receipts
                if receipt.activated_registry_digest == current_digest
            ]
            if len(receipts) > 1 or len(matching) > 1:
                return self._reconcilable_status(receipts, current, current_digest)
            if len(matching) != 1:
                return _status("blocked", "stale_activation")
            blocking = self._blocking_code(current, current_digest, matching[0])
            if blocking is not None:
                return _status("blocked", blocking)
            return _actionable(
                "recovery_required", "recovery_required", matching[0], current_digest
            )
        except (OSError, RuntimeError, ValueError):
            return _status("blocked", "invalid_recovery_evidence")

    def _reconcilable_status(
        self,
        receipts: tuple[ActivationReceipt, ...],
        current: ConnectionRegistry,
        current_digest: str,
    ) -> ActivationRecoveryStatus:
        """Advertise reconciliation only for a provably safe duplicate set.

        The plan must supersede at least one receipt, must keep exactly one, and
        that kept receipt must already satisfy every restore precondition.  Any
        other accumulation stays blocked and exposes no action at all, so an
        unprovable set can never be reached by a click.
        """

        plan = plan_activation_reconciliation(
            receipts, current_registry_digest=current_digest
        )
        if (
            plan.code
            or plan.keep is None
            or not plan.supersede
            or self._blocking_code(current, current_digest, plan.keep) is not None
        ):
            return _status("blocked", "multiple_pending_activations")
        return _actionable(
            "can_reconcile", "reconcile_required", plan.keep, current_digest
        )

    def _blocking_code(
        self,
        current: ConnectionRegistry,
        current_digest: str,
        receipt: ActivationReceipt,
    ) -> str | None:
        """Report why one receipt cannot hold live rollback authority."""

        active = [
            profile
            for profile in current.profiles
            if profile.profile_id == current.active_profile_id
        ]
        if (
            receipt.activated_registry_digest != current_digest
            or len(active) != 1
            or current.active_profile_id != receipt.profile_id
            or profile_digest(active[0]) != receipt.profile_digest
        ):
            return "stale_activation"
        if validate_activation_rollback(self._state_root, receipt) != (
            receipt.previous_registry_digest
        ):
            return "invalid_recovery_evidence"
        return None

    def reconcile(
        self,
        activation_id: str,
        *,
        expected_registry_digest: str,
    ) -> ActivationReconciliationReport:
        """Close the advertised duplicate evidence and report what really happened.

        The caller must name the exact receipt and registry digest that were
        advertised, and the core must keep that same receipt.  Nothing is chosen
        by timestamp and nothing is purged: superseded records keep every field
        and every rollback file.

        A raised refusal here always precedes the core's first transition, so
        the bytes on disk are exactly the advertised ones.  That is decided by
        the proof the core attaches to the error, never by the error's class.
        Once the core has written, this stops refusing: it reports the state it
        can actually prove, and only a fully committed reconciliation that
        still leaves the advertised recovery offers a next action.
        """

        status = self.inspect()
        if status.state != "can_reconcile" or not status.can_reconcile:
            code = status.code if status.state == "blocked" else "recovery_not_allowed"
            raise ActivationRecoveryRefusedError(code)
        if (
            activation_id != status.activation_id
            or expected_registry_digest != status.current_registry_digest
        ):
            raise ActivationRecoveryRefusedError("recovery_conflict")
        try:
            outcome = self._mutations.reconcile_activations(
                expected_registry_digest=expected_registry_digest,
                expected_kept_activation_id=activation_id,
            )
        except (OSError, RuntimeError, ValueError) as error:
            # Belonging to an expected family is not itself proof that nothing
            # was written: these same families are raised while the core closes
            # out its own mutation lock, long after a receipt has moved.  Only
            # the core's explicit zero-write proof may be told to the caller as
            # a refusal; without it a record may already have moved.
            if not reconciliation_proved_zero_write(error):
                return _report("uncertain")
            code = (
                "recovery_conflict"
                if isinstance(error, RegistryConflictError)
                else "recovery_not_allowed"
            )
            raise ActivationRecoveryRefusedError(code) from None
        except Exception:
            # Anything else is not that promise.  A record may already have
            # moved, so this is reported as uncertainty and never as a refusal.
            return _report("uncertain")
        try:
            return self._reconciliation_report(
                outcome, activation_id, expected_registry_digest
            )
        except Exception:
            return _report("uncertain")

    def _reconciliation_report(
        self,
        outcome: ActivationReconciliationOutcome,
        activation_id: str,
        expected_registry_digest: str,
    ) -> ActivationReconciliationReport:
        """Describe a completed reconciliation from freshly inspected evidence.

        The core has already written by the time it returns, so nothing here may
        refuse or reuse the status the page advertised.  Every outcome is
        inspected again, partial ones included: a fresh inspection can never
        make a partial attempt actionable, but the exact current status is worth
        keeping whenever it can be proven.  An inspection that proves nothing is
        itself uncertainty, so it reports ``uncertain`` and offers no action.
        """

        refreshed = self._freshly_inspected()
        if refreshed is None:
            return _report("uncertain")
        if outcome.state != "committed":
            return _report(outcome.state, refreshed)
        if (
            outcome.kept_activation_id != activation_id
            or refreshed.state != "recovery_required"
            or refreshed.activation_id != activation_id
            or refreshed.current_registry_digest != expected_registry_digest
        ):
            return _report("changed")
        return _report("resumed", refreshed)

    def _freshly_inspected(self) -> ActivationRecoveryStatus | None:
        """Inspect once more after a write, or report that nothing was proven.

        ``inspect`` already folds its expected read failures into
        ``invalid_recovery_evidence``.  An unexpected failure of this
        post-write inspection is uncertainty for the same reason: the caller is
        past the point where a refusal could promise anything.
        """

        try:
            refreshed = self.inspect()
        except Exception:
            return None
        return None if refreshed.code == "invalid_recovery_evidence" else refreshed

    def restore(
        self,
        activation_id: str,
        *,
        expected_registry_digest: str,
    ) -> ActivationRecoveryResult:
        """Explicitly restore the sole exact recovery advertised by inspect()."""

        status = self.inspect()
        if status.state != "recovery_required" or not status.can_restore:
            code = (
                status.code
                if status.state == "blocked"
                else "recovery_not_allowed"
            )
            raise ActivationRecoveryRefusedError(code)
        if (
            activation_id != status.activation_id
            or expected_registry_digest != status.current_registry_digest
        ):
            raise ActivationRecoveryRefusedError("recovery_conflict")
        try:
            receipt = self._mutations.restore(
                activation_id,
                expected_registry_digest=expected_registry_digest,
            )
            _current, current_digest = current_registry_snapshot(self._state_root)
        except (OSError, RuntimeError, ValueError) as error:
            code = (
                "recovery_conflict"
                if isinstance(error, RegistryConflictError)
                else "recovery_not_allowed"
            )
            raise ActivationRecoveryRefusedError(code) from None
        if (
            receipt.state != "restored"
            or current_digest != receipt.previous_registry_digest
        ):
            raise ActivationRecoveryRefusedError("recovery_conflict")
        return ActivationRecoveryResult(
            state="restored",
            activation_id=receipt.activation_id,
            profile_id=receipt.profile_id,
            restored_registry_digest=current_digest,
        )


def activation_recovery_status_to_document(
    status: ActivationRecoveryStatus,
) -> dict[str, object]:
    """Serialize the fixed public host contract without internal error details."""

    _validate_status_contract(status)
    return {
        "state": status.state,
        "code": status.code,
        "message": status.message,
        "can_restore": status.can_restore,
        "can_reconcile": status.can_reconcile,
        "activation_id": status.activation_id,
        "profile_id": status.profile_id,
        "current_registry_digest": status.current_registry_digest,
    }


def _validate_status_contract(status: ActivationRecoveryStatus) -> None:
    if status.state not in {"none", "recovery_required", "can_reconcile", "blocked"}:
        raise RuntimeError("Recovery status state is invalid")
    if status.code not in _SAFE_MESSAGES or status.message != _SAFE_MESSAGES[status.code]:
        raise RuntimeError("Recovery status message is invalid")
    if not _status_code_matches_state(status.state, status.code):
        raise RuntimeError("Recovery status code does not match its state")
    if status.state in {"recovery_required", "can_reconcile"}:
        if not _actionable_binding_is_valid(status):
            raise RuntimeError("Actionable status binding is invalid")
    elif status.can_restore or status.can_reconcile or _has_public_binding(status):
        raise RuntimeError("Non-actionable status must not expose bindings")


def _status_code_matches_state(state: RecoveryState, code: str) -> bool:
    if state == "none":
        return code == "no_recovery"
    if state == "recovery_required":
        return code == "recovery_required"
    if state == "can_reconcile":
        return code == "reconcile_required"
    return code in {
        "multiple_pending_activations",
        "stale_activation",
        "invalid_recovery_evidence",
    }


def _actionable_binding_is_valid(status: ActivationRecoveryStatus) -> bool:
    return (
        status.can_restore == (status.state == "recovery_required")
        and status.can_reconcile == (status.state == "can_reconcile")
        and _is_canonical_uuid(status.activation_id)
        and _is_canonical_uuid(status.profile_id)
        and _is_digest(status.current_registry_digest)
    )


def _has_public_binding(status: ActivationRecoveryStatus) -> bool:
    return any(
        value is not None
        for value in (
            status.activation_id,
            status.profile_id,
            status.current_registry_digest,
        )
    )


def activation_recovery_result_to_document(
    result: ActivationRecoveryResult,
) -> dict[str, object]:
    if (
        result.state != "restored"
        or not _is_canonical_uuid(result.activation_id)
        or not _is_canonical_uuid(result.profile_id)
        or not _is_digest(result.restored_registry_digest)
    ):
        raise RuntimeError("Recovery result is invalid")
    return {
        "state": result.state,
        "activation_id": result.activation_id,
        "profile_id": result.profile_id,
        "restored_registry_digest": result.restored_registry_digest,
    }


def _pending_receipts(state_root: Path) -> tuple[ActivationReceipt, ...]:
    root = state_root / ACTIVATION_DIRECTORY
    if not root.exists():
        return ()
    if not root.is_dir() or _is_link_like(root):
        raise RuntimeError("Activation record directory is invalid")
    pending: list[ActivationReceipt] = []
    count = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                count += 1
                if count > MAX_ACTIVATION_RECORDS:
                    raise RuntimeError("Too many activation records")
                if not entry.name.endswith(".receipt.json"):
                    continue
                activation_id = entry.name[: -len(".receipt.json")]
                if not _is_canonical_uuid(activation_id):
                    raise RuntimeError("Activation receipt filename is invalid")
                receipt = load_activation_receipt(state_root, activation_id)
                if receipt.state in {"prepared", "pending"}:
                    pending.append(receipt)
    except OSError as error:
        raise RuntimeError("Could not inspect activation records") from error
    return tuple(pending)


def _report(
    state: Literal["resumed", "incomplete", "changed", "uncertain"],
    status: ActivationRecoveryStatus | None = None,
) -> ActivationReconciliationReport:
    return ActivationReconciliationReport(
        state=state, message=_RECONCILIATION_MESSAGES[state], status=status
    )


def _status(state: RecoveryState, code: str) -> ActivationRecoveryStatus:
    return ActivationRecoveryStatus(
        state=state,
        code=code,
        message=_SAFE_MESSAGES[code],
        can_restore=False,
    )


def _actionable(
    state: RecoveryState,
    code: str,
    receipt: ActivationReceipt,
    current_digest: str,
) -> ActivationRecoveryStatus:
    """Bind the single advertised action to one receipt and one registry state."""

    return ActivationRecoveryStatus(
        state=state,
        code=code,
        message=_SAFE_MESSAGES[code],
        can_restore=state == "recovery_required",
        activation_id=receipt.activation_id,
        profile_id=receipt.profile_id,
        current_registry_digest=current_digest,
        can_reconcile=state == "can_reconcile",
    )


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.int != 0 and str(parsed) == value


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )
