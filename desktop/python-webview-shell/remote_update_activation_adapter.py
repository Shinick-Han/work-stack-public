"""Production activation adapter over the existing connection registry services.

This is the remote update flow's activation port expressed against the real
``ConnectionRegistryMutationService`` and
``ConnectionRegistryActivationRecoveryService``.  There is no second registry,
no second activation schema, no direct registry or receipt JSON write, and no
SSOT access: every mutation goes through the existing CAS-guarded service calls,
and every observation goes through the existing receipt inspection helpers.

The seam this module exists to close
------------------------------------

``ConnectionRegistryMutationService.activate`` mints its own ``activation_id``
inside the mutation lock.  The flow names its mutation with its own
``operation_id``.  The two identities are genuinely different and neither is
derivable from the other, so this adapter never pretends they are equal.  It
binds them, durably and before the effect, through
:mod:`remote_update_activation_binding`: the five receipt fields that are fully
decided *before* ``activate`` is called — ``profile_id``, ``profile_digest``,
``previous_registry_digest``, ``activated_registry_digest`` and
``proof_digest`` — are recorded as the operation's exact candidate fingerprint.

A lost response is then recovered by reading the receipts that exist and
matching that fingerprint:

* exactly one match  -> that receipt is this operation's, its identity is written
  back to the binding, and every later confirm/rollback targets it and only it;
* no match, with the live registry still at ``previous_registry_digest`` -> the
  activation provably wrote nothing (the receipt is written before the registry
  is replaced), so the attempt settles as refused rather than unknown;
* more than one match, or any unconfirmed receipt that is not ours -> refused.
  An ambiguous, duplicated or foreign receipt is never adopted and never
  mutated.

That recovery is sound only because one fingerprint names one operation: the
store refuses a second operation claiming a bound fingerprint, an operation
that never issued resolves nothing, and the duplicate/foreign check is
re-proven before *every* mutation, not only before the first activation.

A refusal — ``ActivationAdapterRefusal`` or ``ActivationBindingError`` — may
promise "committed nothing" only before a service call.  The services write
their real state before they answer (``activate`` publishes the receipt, then
replaces the registry, then transitions it; ``restore`` replaces the registry,
then transitions the receipt), so once one is entered every failure, a
binding-store write or read included, becomes ``ActivationResponseLost`` for
the same operation.  An unconfirmed receipt beside a registry that already
holds the previous selection is that interrupted restore: unknown, not refused.

What is deliberately *not* inferred
-----------------------------------

A receipt existing does not mean its profile is the selected authority.  Every
observation reports ``selection`` separately, computed from the live registry:
``selected`` only when the live registry digest equals the receipt's activated
digest *and* the live active profile is that receipt's profile with that
receipt's profile digest; ``not_selected`` when the live registry is the state
the receipt rolls back to; ``unknown`` otherwise.  Confirmation requires
``selected``; it is never granted because a receipt is on disk.

``observe``, ``observe_confirm`` and ``observe_rollback`` are passive.  They
never call a registry mutation.  They may write back a resolved
``activation_id`` into the desktop-side binding file, which is this adapter's
own bounded state next to ``connection-registry.json`` — never the registry,
never a receipt, never the SSOT.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from connection_activation_evidence import (
    UNCONFIRMED_ACTIVATION_STATES,
    ActivationAttemptRefusedError,
    ActivationReceipt,
    RegistryConflictError,
    _sha256,
    activation_receipts,
    load_activation_receipt,
)
from connection_registry import ConnectionRegistry
from connection_registry_activation_recovery import (
    ConnectionRegistryActivationRecoveryService,
)
from connection_registry_host_contract import ActivateProfileResponse
from connection_registry_mutations import (
    ActivationProofError,
    ConnectionRegistryMutationService,
    current_registry_snapshot,
    profile_digest,
    registry_digest,
)
from remote_update_activation_binding import (
    MAX_BINDINGS,
    ActivationBinding,
    ActivationBindingError,
    ActivationBindingStore,
)

#: The activation states this adapter reports.  ``rolled_back`` is the registry's
#: ``restored`` receipt state; a ``superseded`` receipt is never described as a
#: rollback or a failure, only as unknown.
ACTIVATION_STATES = ("prepared", "pending", "confirmed", "rolled_back", "unknown")
SELECTION_STATES = ("selected", "not_selected", "unknown")
UNCONFIRMED_STATES = ("prepared", "pending")

_RECEIPT_STATES = {
    "prepared": "prepared",
    "pending": "pending",
    "confirmed": "confirmed",
    "restored": "rolled_back",
}

#: ``state`` -> ``(status, code, committed, restart_required)`` when the live
#: registry agrees with what the receipt claims.
_AGREED = {
    "prepared": ("unknown", "activation_prepared_uncommitted", True, True),
    "pending": ("verified", "activation_pending_restart", True, True),
    "confirmed": ("verified", "activation_confirmed", True, False),
    "rolled_back": ("verified", "activation_rolled_back", True, None),
    "unknown": ("unknown", "activation_superseded", None, None),
}

#: The same states when the live registry does not agree.  Nothing here claims a
#: commit: a receipt whose selection cannot be proven stays unknown.
_DISAGREED = {
    "prepared": ("unknown", "activation_prepared_unselected", None, True),
    "pending": ("unknown", "activation_pending_not_selected", None, True),
    "confirmed": ("unknown", "activation_confirmed_not_selected", None, None),
    "rolled_back": ("unknown", "activation_rollback_unverified", None, None),
    "unknown": ("unknown", "activation_superseded", None, None),
}

_REFUSALS = {
    "activation_ambiguous": "Multiple activations match this update operation.",
    "activation_candidate_refused": "The activation candidate is not an exact enabled selection.",
    "activation_candidate_unavailable": "This operation has no issuable activation candidate in this process.",
    "activation_conflict": "Another unconfirmed activation already targets this connection state.",
    "activation_foreign_unconfirmed": "An unconfirmed activation that is not this operation's must be resolved first.",
    "activation_manual_review": "Connection activation evidence cannot be reconciled without manual review.",
    "activation_refused": "The connection registry refused this activation.",
    "activation_proof_refused": "The profile Test proof is missing, expired, or does not match this candidate.",
    "activation_unconfirmed": "Restore or confirm the unconfirmed connection activation before activating again.",
    "activation_unknown_not_reissued": "This activation's outcome is unknown; it is reconciled, never reissued.",
    "activation_unresolved": "No activation receipt is bound to this update operation yet.",
    "binding_absent": "This update operation has no recorded activation binding.",
    "binding_conflict": "This update operation is already bound to a different activation candidate.",
    "binding_owned": "Another update operation already owns this activation candidate.",
    "binding_store_full": "Too many activation bindings are recorded to add another.",
    "binding_store_invalid": "The recorded activation bindings could not be read.",
    "binding_unwritable": "The activation binding could not be recorded durably.",
    "confirm_not_selected": "This activation is not the current selection, so it cannot be confirmed.",
    "confirm_not_unconfirmed": "Only an unconfirmed activation can be confirmed.",
    "invalid_recovery_evidence": "The recorded activation evidence could not be validated.",
    "multiple_pending_activations": "Several unconfirmed activations require explicit reconciliation.",
    "receipt_ambiguous": "More than one activation receipt matches this operation's candidate.",
    "receipt_identity_mismatch": "The bound activation receipt is not this operation's candidate.",
    "registry_conflict": "Connection registry changed; reload it before trying again.",
    "rollback_foreign_receipt": "The advertised recovery is a different activation.",
    "rollback_not_advertised": "No exact activation recovery is currently advertised.",
    "rollback_not_unconfirmed": "Only an unconfirmed activation can be rolled back.",
    "stale_activation": "The recorded activation no longer answers for the live registry.",
}


class ActivationAdapterRefusal(RuntimeError):
    """A bounded refusal that committed nothing to the connection registry."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _REFUSALS else "activation_refused"
        self.safe_message = _REFUSALS[self.code]
        super().__init__(self.safe_message)


class ActivationResponseLost(RuntimeError):
    """A registry mutation was issued and its disposition cannot be proven.

    The operation's identity stays bound, so the caller reconciles the same
    operation through the matching ``observe`` call.  It is never permission to
    issue the mutation again.
    """

    code = "activation_response_lost"

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(operation_id)


@dataclass(frozen=True)
class ActivationCandidate:
    """One operation's exact activation request, before anything is written."""

    operation_id: str
    registry: ConnectionRegistry
    profile_id: str
    proof_id: str
    expected_registry_digest: str


@dataclass(frozen=True)
class ActivationObservation:
    """What the registry really holds for one operation's activation.

    ``committed`` says the registry durably wrote what was asked; ``state`` says
    what the receipt now is; ``selection`` says, independently, whether that
    receipt's profile is the authority the live registry currently selects.
    """

    status: str = "unknown"
    committed: bool | None = None
    state: str = "unknown"
    restart_required: bool | None = None
    selection: str = "unknown"
    code: str = "activation_unknown"
    activation_id: str | None = None
    profile_id: str | None = None
    current_registry_digest: str | None = None
    recovery_state: str = "unknown"
    recovery_code: str = "unknown"


@dataclass(frozen=True)
class RollbackObservation:
    """An activation rollback, and whether the previous selection is in force."""

    status: str = "unknown"
    committed: bool | None = None
    previous_activation_selected: bool | None = None
    code: str = "rollback_unknown"
    activation_id: str | None = None
    current_registry_digest: str | None = None
    recovery_state: str = "unknown"
    recovery_code: str = "unknown"


def _contract_restart_required() -> bool | None:
    """Read the shared host contract's own answer instead of hardcoding it.

    ``ActivateProfileResponse`` is the existing registry host contract for a
    successful activation and it declares ``restart_required`` as ``True``;
    ``encode_registry_host_response`` refuses any other value.  Reading the
    declaration keeps this adapter from asserting a restart the shared contract
    no longer promises: if it ever stops declaring ``True``, this becomes
    ``None`` and every pending activation reports the restart as unknown.
    """

    declared = ActivateProfileResponse.__dataclass_fields__["restart_required"].default
    return True if declared is True else None


RESTART_REQUIRED_BY_CONTRACT = _contract_restart_required()


def _refusal_code(error: BaseException) -> str:
    """Name a proven zero-write registry failure in this adapter's vocabulary."""

    if isinstance(error, ActivationProofError):
        return "activation_proof_refused"
    if isinstance(error, ActivationAttemptRefusedError):
        return error.code
    if isinstance(error, ActivationBindingError):
        return error.code
    if isinstance(error, RegistryConflictError):
        return "registry_conflict"
    if isinstance(error, RuntimeError):
        return "activation_candidate_refused"
    return "activation_refused"


def _selection_of(
    receipt: ActivationReceipt,
    registry: ConnectionRegistry | None,
    current_digest: str | None,
) -> str:
    """Prove the current selection from the live registry, not from the receipt."""

    if registry is None or current_digest is None:
        return "unknown"
    if current_digest == receipt.previous_registry_digest:
        return "not_selected"
    if current_digest != receipt.activated_registry_digest:
        return "unknown"
    active = [
        profile
        for profile in registry.profiles
        if profile.profile_id == registry.active_profile_id
    ]
    if len(active) != 1 or registry.active_profile_id != receipt.profile_id:
        return "unknown"
    if profile_digest(active[0]) != receipt.profile_digest:
        return "unknown"
    return "selected"


def _agrees(state: str, selection: str) -> bool:
    """Report whether the live selection matches what this receipt state claims."""

    if state in UNCONFIRMED_STATES or state == "confirmed":
        return selection == "selected"
    if state == "rolled_back":
        return selection == "not_selected"
    return False


class RegistryActivationAdapter:
    """Issue and observe one flow operation's registry activation receipt."""

    def __init__(
        self,
        state_root: Path,
        *,
        mutation_service: ConnectionRegistryMutationService | None = None,
        recovery_service: ConnectionRegistryActivationRecoveryService | None = None,
        binding_store: ActivationBindingStore | None = None,
    ) -> None:
        self._state_root = Path(state_root)
        self._mutations = mutation_service or ConnectionRegistryMutationService(
            self._state_root
        )
        self._recovery = recovery_service or ConnectionRegistryActivationRecoveryService(
            self._state_root, mutation_service=self._mutations
        )
        self._store = binding_store or ActivationBindingStore(self._state_root)
        self._candidates: dict[str, ActivationCandidate] = {}

    # -- binding ---------------------------------------------------------

    def bind(self, candidate: ActivationCandidate) -> ActivationBinding:
        """Key this operation to its exact candidate fingerprint before any effect.

        Durable and idempotent.  The proof itself is short-lived and lives only
        in the mutation service's memory, so the issuable candidate is kept in
        this process only: after a restart the binding still recovers the
        receipt, but the activation can only be observed, never reissued.
        """

        recorded = self._store.bind(self._fingerprint(candidate))
        if (
            candidate.operation_id not in self._candidates
            and len(self._candidates) >= MAX_BINDINGS
        ):
            raise ActivationAdapterRefusal("binding_store_full")
        self._candidates[candidate.operation_id] = candidate
        return recorded

    def binding(self, operation_id: str) -> ActivationBinding:
        return self._store.require(operation_id)

    def release(self, operation_id: str) -> None:
        """Retire one settled operation's binding.

        The caller owns when that is correct: a confirmed activation stays bound
        until the update is verified ready or its previous selection is verified
        restored.
        """

        self._candidates.pop(operation_id, None)
        self._store.release(operation_id)

    def _fingerprint(self, candidate: ActivationCandidate) -> ActivationBinding:
        registry = candidate.registry
        if not isinstance(registry, ConnectionRegistry):
            raise ActivationAdapterRefusal("activation_candidate_refused")
        matches = [
            profile
            for profile in registry.profiles
            if profile.profile_id == candidate.profile_id
        ]
        if (
            registry.active_profile_id != candidate.profile_id
            or len(matches) != 1
            or not matches[0].enabled
        ):
            raise ActivationAdapterRefusal("activation_candidate_refused")
        try:
            proof_digest = _sha256(candidate.proof_id.encode("ascii"))
            activated = registry_digest(registry)
        except (AttributeError, UnicodeError, RuntimeError, ValueError):
            raise ActivationAdapterRefusal("activation_candidate_refused") from None
        return ActivationBinding(
            operation_id=candidate.operation_id,
            profile_id=candidate.profile_id,
            profile_digest=profile_digest(matches[0]),
            previous_registry_digest=candidate.expected_registry_digest,
            activated_registry_digest=activated,
            proof_digest=proof_digest,
        )

    # -- activation ------------------------------------------------------

    def activate(self, operation_id: str) -> ActivationObservation:
        """Issue this operation's activation exactly once, or reconcile it."""

        observed = self.observe(operation_id)
        if observed.activation_id is not None:
            return observed
        if observed.code not in ("activation_not_issued", "activation_not_written"):
            raise ActivationAdapterRefusal("activation_unknown_not_reissued")
        candidate = self._candidates.get(operation_id)
        if candidate is None:
            raise ActivationAdapterRefusal("activation_candidate_unavailable")
        binding = self._store.require(operation_id)
        self._require_sole_unconfirmed(binding)
        self._store.mark_issued(operation_id)
        return self._issue(binding, candidate)

    def observe(self, operation_id: str) -> ActivationObservation:
        """Read this operation's activation without mutating the registry."""

        return self._observe_binding(self._store.require(operation_id))

    def confirm(self, operation_id: str) -> ActivationObservation:
        """Confirm the exact receipt this operation is bound to, and no other."""

        observed = self.observe(operation_id)
        if observed.state == "confirmed":
            return observed
        self._require_confirmable(observed)
        binding = self._store.require(operation_id)
        self._require_sole_unconfirmed(binding)
        try:
            self._mutations.confirm(
                str(observed.activation_id),
                expected_registry_digest=binding.activated_registry_digest,
            )
        except Exception as error:  # noqa: BLE001 - reclassified from disk evidence
            return self._settled_or_lost(operation_id, error, "confirmed")
        return self._observe_after_effect(operation_id)

    def observe_confirm(self, operation_id: str) -> ActivationObservation:
        """Passively distinguish confirmed from still pending for this identity."""

        return self.observe(operation_id)

    def rollback(self, operation_id: str) -> RollbackObservation:
        """Restore the previous selection through the advertised exact recovery."""

        binding = self._store.require(operation_id)
        observed = self.observe(operation_id)
        if observed.state == "rolled_back" or _rollback_interrupted(observed, binding):
            return _rollback_observation(observed, binding)
        self._require_rollbackable(observed, binding)
        try:
            self._recovery.restore(
                str(observed.activation_id),
                expected_registry_digest=binding.activated_registry_digest,
            )
        except Exception as error:  # noqa: BLE001 - reclassified from disk evidence
            settled = self._settled_or_lost(operation_id, error, "rolled_back")
            return _rollback_observation(settled, binding)
        return _rollback_observation(self._observe_after_effect(operation_id), binding)

    def observe_rollback(self, operation_id: str) -> RollbackObservation:
        """Read this operation's rollback without mutating the registry."""

        binding = self._store.require(operation_id)
        return _rollback_observation(self._observe_binding(binding), binding)

    # -- issuing ---------------------------------------------------------

    def _issue(
        self, binding: ActivationBinding, candidate: ActivationCandidate
    ) -> ActivationObservation:
        try:
            receipt = self._mutations.activate(
                candidate.registry,
                binding.profile_id,
                candidate.proof_id,
                expected_registry_digest=binding.previous_registry_digest,
            )
        except Exception as error:  # noqa: BLE001 - reclassified from disk evidence
            return self._issued_or_lost(binding, error)
        return self._settle_issued(binding, receipt)

    def _settle_issued(
        self, binding: ActivationBinding, receipt: ActivationReceipt
    ) -> ActivationObservation:
        """Record and read back a receipt the real service has already written.

        Everything from here on is post-effect, so a binding-store failure or
        an unresolvable receipt is a *lost response* for this same operation,
        never the zero-write refusal those exceptions mean before the effect.
        The operation stays bound and is reconciled by observing it again.
        """

        try:
            if not binding.matches_receipt(receipt):
                raise ActivationAdapterRefusal("receipt_identity_mismatch")
            self._store.resolve(binding.operation_id, receipt.activation_id)
            return self.observe(binding.operation_id)
        except (ActivationAdapterRefusal, ActivationBindingError) as error:
            raise ActivationResponseLost(binding.operation_id) from error

    def _observe_after_effect(self, operation_id: str) -> ActivationObservation:
        """Observe once a service call that may have written has returned."""

        try:
            return self.observe(operation_id)
        except (ActivationAdapterRefusal, ActivationBindingError) as error:
            raise ActivationResponseLost(operation_id) from error

    def _issued_or_lost(
        self, binding: ActivationBinding, error: Exception
    ) -> ActivationObservation:
        """Answer a failed activation from disk, never from the exception's class.

        ``activate`` publishes the rollback file and the receipt before it
        replaces the registry, and both are published atomically, so a readable
        activation directory that holds no receipt with this candidate's
        fingerprint is proof that this attempt changed no registry state.  Only
        an activation directory that cannot be read leaves the outcome unknown.
        """

        try:
            receipts = activation_receipts(self._state_root)
        except (OSError, RuntimeError, ValueError):
            raise ActivationResponseLost(binding.operation_id) from error
        mine = tuple(item for item in receipts if binding.matches_receipt(item))
        if len(mine) > 1:
            raise ActivationResponseLost(binding.operation_id) from error
        if mine:
            return self._settle_issued(binding, mine[0])
        raise ActivationAdapterRefusal(_refusal_code(error)) from None

    def _settled_or_lost(
        self, operation_id: str, error: Exception, expected: str
    ) -> ActivationObservation:
        """Reclassify a failed confirm/rollback from the receipt that is on disk.

        The refusal branch is deliberately narrow: both mutations move *away
        from* ``activated_registry_digest``, so only a live registry that still
        selects exactly that state proves the attempt wrote nothing.  Anything
        else — the interrupted ``restore`` included — stays unknown.
        """

        try:
            observed = self.observe(operation_id)
        except (ActivationAdapterRefusal, ActivationBindingError):
            raise ActivationResponseLost(operation_id) from error
        if observed.state == expected:
            return observed
        if observed.state in UNCONFIRMED_STATES and observed.selection == "selected":
            raise ActivationAdapterRefusal(_refusal_code(error)) from None
        raise ActivationResponseLost(operation_id) from error

    # -- preconditions ---------------------------------------------------

    def _require_sole_unconfirmed(self, binding: ActivationBinding) -> None:
        """Refuse while any unconfirmed receipt other than this one exists.

        Before an identity is resolved, this operation's exact fingerprint names
        its receipt.  Once one is resolved, that identity *alone* is this
        operation's evidence: a second receipt carrying the same fingerprint is
        a duplicate, not a synonym.  Re-proven before every mutation, because
        evidence appearing after resolution is exactly what a confirm or a
        restore would otherwise settle on top of.  It writes nothing and adds no
        service rule; it declines to ask the existing services to act at all.
        """

        try:
            receipts = activation_receipts(self._state_root)
        except (OSError, RuntimeError, ValueError):
            raise ActivationAdapterRefusal("invalid_recovery_evidence") from None
        mine: list[ActivationReceipt] = []
        others: list[ActivationReceipt] = []
        for item in receipts:
            if item.state not in UNCONFIRMED_ACTIVATION_STATES:
                continue
            (mine if _is_this_operations(binding, item) else others).append(item)
        if len(mine) > 1 or any(binding.matches_receipt(item) for item in others):
            raise ActivationAdapterRefusal("receipt_ambiguous")
        if others:
            raise ActivationAdapterRefusal("activation_foreign_unconfirmed")

    def _require_confirmable(self, observed: ActivationObservation) -> None:
        if observed.activation_id is None:
            raise ActivationAdapterRefusal("activation_unresolved")
        if observed.state not in UNCONFIRMED_STATES:
            raise ActivationAdapterRefusal("confirm_not_unconfirmed")
        if observed.selection != "selected":
            raise ActivationAdapterRefusal("confirm_not_selected")

    def _require_rollbackable(
        self, observed: ActivationObservation, binding: ActivationBinding
    ) -> None:
        if observed.activation_id is None:
            raise ActivationAdapterRefusal("activation_unresolved")
        if observed.state not in UNCONFIRMED_STATES:
            raise ActivationAdapterRefusal("rollback_not_unconfirmed")
        self._require_sole_unconfirmed(binding)
        status = self._recovery.inspect()
        if status.state != "recovery_required" or not status.can_restore:
            raise ActivationAdapterRefusal(
                status.code if status.state == "blocked" else "rollback_not_advertised"
            )
        if status.activation_id != observed.activation_id:
            raise ActivationAdapterRefusal("rollback_foreign_receipt")
        if status.current_registry_digest != binding.activated_registry_digest:
            raise ActivationAdapterRefusal("registry_conflict")

    # -- observation -----------------------------------------------------

    def _observe_binding(self, binding: ActivationBinding) -> ActivationObservation:
        registry, digest = self._live_registry()
        receipt, absent_code = self._locate(binding)
        recovery = self._recovery_view()
        if receipt is None:
            return _unresolved_observation(binding, absent_code, digest, recovery)
        return _resolved_observation(receipt, registry, digest, recovery)

    def _locate(
        self, binding: ActivationBinding
    ) -> tuple[ActivationReceipt | None, str]:
        """Find this operation's receipt by identity, else by exact fingerprint.

        An operation that never issued adopts nothing: the fingerprint is unique
        to one operation, and the receipt carrying it belongs to whichever
        operation actually issued it, so an unissued binding stays absent.
        """

        if binding.activation_id is not None:
            return self._bound_receipt(binding)
        if not binding.issued:
            return None, "activation_absent"
        try:
            receipts = activation_receipts(self._state_root)
        except (OSError, RuntimeError, ValueError):
            return None, "activation_evidence_unreadable"
        mine = tuple(item for item in receipts if binding.matches_receipt(item))
        if len(mine) > 1:
            raise ActivationAdapterRefusal("receipt_ambiguous")
        if not mine:
            return None, "activation_absent"
        self._store.resolve(binding.operation_id, mine[0].activation_id)
        return mine[0], ""

    def _bound_receipt(
        self, binding: ActivationBinding
    ) -> tuple[ActivationReceipt | None, str]:
        try:
            receipt = load_activation_receipt(
                self._state_root, str(binding.activation_id)
            )
        except (OSError, RuntimeError, ValueError):
            return None, "receipt_unreadable"
        if not binding.matches_receipt(receipt):
            raise ActivationAdapterRefusal("receipt_identity_mismatch")
        return receipt, ""

    def _live_registry(self) -> tuple[ConnectionRegistry | None, str | None]:
        try:
            registry, digest = current_registry_snapshot(self._state_root)
        except (OSError, RuntimeError, ValueError):
            return None, None
        return registry, digest

    def _recovery_view(self) -> tuple[str, str]:
        try:
            status = self._recovery.inspect()
        except Exception:  # noqa: BLE001 - a diagnostic view is never authority
            return "unknown", "unknown"
        return status.state, status.code


def _resolved_observation(
    receipt: ActivationReceipt,
    registry: ConnectionRegistry | None,
    digest: str | None,
    recovery: tuple[str, str],
) -> ActivationObservation:
    """Describe one located receipt and the selection the live registry proves."""

    state = _RECEIPT_STATES.get(receipt.state, "unknown")
    selection = _selection_of(receipt, registry, digest)
    table = _AGREED if _agrees(state, selection) else _DISAGREED
    status, code, committed, restart = table[state]
    if restart is True:
        restart = RESTART_REQUIRED_BY_CONTRACT
    return ActivationObservation(
        status=status,
        committed=committed,
        state=state,
        restart_required=restart,
        selection=selection,
        code=code,
        activation_id=receipt.activation_id,
        profile_id=receipt.profile_id,
        current_registry_digest=digest,
        recovery_state=recovery[0],
        recovery_code=recovery[1],
    )


def _unresolved_observation(
    binding: ActivationBinding,
    absent_code: str,
    digest: str | None,
    recovery: tuple[str, str],
) -> ActivationObservation:
    """Describe an operation whose receipt is absent, unreadable, or unknown."""

    status, committed, code = "unknown", None, absent_code
    if absent_code == "activation_absent":
        if not binding.issued:
            status, committed, code = "unknown", False, "activation_not_issued"
        elif digest is not None and digest == binding.previous_registry_digest:
            status, committed, code = "failed", False, "activation_not_written"
        else:
            code = "activation_unknown"
    return ActivationObservation(
        status=status,
        committed=committed,
        state="unknown",
        code=code,
        profile_id=binding.profile_id,
        current_registry_digest=digest,
        recovery_state=recovery[0],
        recovery_code=recovery[1],
    )


def _rollback_observation(
    observed: ActivationObservation, binding: ActivationBinding
) -> RollbackObservation:
    """Project one activation observation as this operation's rollback answer."""

    restored = (
        None
        if observed.current_registry_digest is None
        else observed.current_registry_digest == binding.previous_registry_digest
    )
    status, committed, code = _rollback_disposition(observed.state, restored)
    proven = observed.state == "rolled_back" or (
        restored is True and observed.state in UNCONFIRMED_STATES
    )
    return RollbackObservation(
        status=status,
        committed=committed,
        previous_activation_selected=restored if proven else None,
        code=code,
        activation_id=observed.activation_id,
        current_registry_digest=observed.current_registry_digest,
        recovery_state=observed.recovery_state,
        recovery_code=observed.recovery_code,
    )


def _rollback_disposition(
    state: str, restored: bool | None
) -> tuple[str, bool | None, str]:
    """Say what a rollback really is, from the receipt *and* the live registry.

    An unconfirmed receipt proves the rollback wrote nothing only while the
    live registry still holds the activated state.  Once the registry holds the
    previous selection, ``restore`` has committed its registry write and only
    its receipt transition is missing: ``committed`` is withheld, not denied.
    """

    if state == "rolled_back":
        if restored is True:
            return "verified", True, "activation_rolled_back"
        return "unknown", True, "activation_rollback_unverified"
    if state in UNCONFIRMED_STATES:
        if restored is False:
            return "failed", False, "rollback_not_committed"
        if restored is True:
            return "unknown", None, "rollback_interrupted"
        return "unknown", None, "rollback_unknown"
    if state == "confirmed":
        return "failed", False, "rollback_unavailable_confirmed"
    return "unknown", None, "rollback_unknown"


def _rollback_interrupted(
    observed: ActivationObservation, binding: ActivationBinding
) -> bool:
    """Report the real ``restore``'s crash window for this operation.

    ``ConnectionRegistryMutationService.restore`` replaces the live registry
    and only *afterwards* transitions the receipt to ``restored``.  An
    unconfirmed receipt beside a registry that already holds this operation's
    previous selection is therefore an interrupted restore, not a rollback that
    never happened: observed again as unknown, never retried, never refused.
    """

    return (
        observed.state in UNCONFIRMED_STATES
        and observed.current_registry_digest is not None
        and observed.current_registry_digest == binding.previous_registry_digest
    )


def _is_this_operations(binding: ActivationBinding, receipt: ActivationReceipt) -> bool:
    """Name one receipt as this operation's, by identity once one is resolved."""

    if binding.activation_id is None:
        return binding.matches_receipt(receipt)
    return receipt.activation_id == binding.activation_id
