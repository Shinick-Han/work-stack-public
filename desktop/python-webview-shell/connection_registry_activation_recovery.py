"""Read-only startup detection and explicit connection-activation recovery.

Startup inspection never changes the connection registry or activation records.
It only advertises recovery when one exact, unconfirmed activation is bound to
the currently active registry.  Recovery remains an explicit caller action and
is delegated to :class:`ConnectionRegistryMutationService` for its CAS write.
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
    ConnectionRegistryMutationService,
    RegistryConflictError,
    current_registry_snapshot,
    load_activation_receipt,
    profile_digest,
    validate_activation_rollback,
)


RecoveryState: TypeAlias = Literal["none", "recovery_required", "blocked"]

_SAFE_MESSAGES = {
    "no_recovery": "No connection activation requires recovery.",
    "recovery_required": (
        "An unconfirmed connection activation can be restored explicitly."
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


class ActivationRecoveryRefusedError(RuntimeError):
    """A sanitized, fail-closed refusal suitable for a native host response."""

    def __init__(self, code: str) -> None:
        if code not in _SAFE_MESSAGES or code in {"no_recovery", "recovery_required"}:
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


@dataclass(frozen=True)
class ActivationRecoveryResult:
    state: Literal["restored"]
    activation_id: str
    profile_id: str
    restored_registry_digest: str


class ConnectionRegistryActivationRecoveryService:
    """Bounded startup inspector plus explicit, exact recovery operation."""

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
                return _status("blocked", "multiple_pending_activations")
            if len(matching) != 1:
                return _status("blocked", "stale_activation")
            receipt = matching[0]
            active = [
                profile
                for profile in current.profiles
                if profile.profile_id == current.active_profile_id
            ]
            if (
                len(active) != 1
                or current.active_profile_id != receipt.profile_id
                or profile_digest(active[0]) != receipt.profile_digest
            ):
                return _status("blocked", "stale_activation")
            if validate_activation_rollback(self._state_root, receipt) != (
                receipt.previous_registry_digest
            ):
                return _status("blocked", "invalid_recovery_evidence")
            return ActivationRecoveryStatus(
                state="recovery_required",
                code="recovery_required",
                message=_SAFE_MESSAGES["recovery_required"],
                can_restore=True,
                activation_id=receipt.activation_id,
                profile_id=receipt.profile_id,
                current_registry_digest=current_digest,
            )
        except (OSError, RuntimeError, ValueError):
            return _status("blocked", "invalid_recovery_evidence")

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
        "activation_id": status.activation_id,
        "profile_id": status.profile_id,
        "current_registry_digest": status.current_registry_digest,
    }


def _validate_status_contract(status: ActivationRecoveryStatus) -> None:
    if status.state not in {"none", "recovery_required", "blocked"}:
        raise RuntimeError("Recovery status state is invalid")
    if status.code not in _SAFE_MESSAGES or status.message != _SAFE_MESSAGES[status.code]:
        raise RuntimeError("Recovery status message is invalid")
    if not _status_code_matches_state(status.state, status.code):
        raise RuntimeError("Recovery status code does not match its state")
    if status.state == "recovery_required":
        if not _recoverable_binding_is_valid(status):
            raise RuntimeError("Recoverable status binding is invalid")
    elif status.can_restore or _has_public_binding(status):
        raise RuntimeError("Non-recoverable status must not expose bindings")


def _status_code_matches_state(state: RecoveryState, code: str) -> bool:
    if state == "none":
        return code == "no_recovery"
    if state == "recovery_required":
        return code == "recovery_required"
    return code in {
        "multiple_pending_activations",
        "stale_activation",
        "invalid_recovery_evidence",
    }


def _recoverable_binding_is_valid(status: ActivationRecoveryStatus) -> bool:
    return (
        status.can_restore
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


def _status(state: RecoveryState, code: str) -> ActivationRecoveryStatus:
    return ActivationRecoveryStatus(
        state=state,
        code=code,
        message=_SAFE_MESSAGES[code],
        can_restore=False,
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
