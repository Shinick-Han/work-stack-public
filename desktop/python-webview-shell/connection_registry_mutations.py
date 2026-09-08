"""Fail-closed mutation boundary for the desktop connection registry.

This module owns configuration files under the desktop state root only.  It
never constructs a :class:`workstack.store.Store`, creates an SSOT directory,
or runs SSH.  Metadata edits use compare-and-swap, while activation additionally
requires a short-lived in-process proof produced from an exact successful
read-only profile test.  Activation evidence, and the rules that decide whether
an attempt continues or competes with it, live in
:mod:`connection_activation_evidence`.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal

# ``ACTIVATION_DIRECTORY``, ``ACTIVATION_RECEIPT_VERSION`` and
# ``MAX_ACTIVATION_RECORDS`` stay importable here for the recovery boundary that
# already reads activation evidence through this module.
from connection_activation_evidence import (
    ACTIVATION_DIRECTORY,
    ACTIVATION_RECEIPT_VERSION,
    MAX_ACTIVATION_RECORDS,
    UNCONFIRMED_ACTIVATION_STATES,
    ActivationAttemptRefusedError,
    ActivationReceipt,
    ActivationReconciliationPlan,
    RegistryConflictError,
    _atomic_replace,
    _canonical_uuid,
    _read_bounded_regular_file,
    _read_receipt,
    _receipt_bytes,
    _receipt_path,
    _replace_receipt_if_digest,
    _safe_rollback_path,
    _sha256,
    _validated_digest,
    _write_new,
    activation_receipts,
    classify_activation_attempt,
    load_activation_receipt,
    plan_activation_reconciliation,
)
from connection_registry import (
    MAX_REGISTRY_BYTES,
    REGISTRY_FILE,
    ConnectionProfile,
    ConnectionRegistry,
    load_connection_registry,
    registry_from_document,
    registry_to_document,
)
from profile_inspection import ProfileTestResult, profile_test_result_to_document


MUTATION_LOCK_FILE = "connection-registry-mutation.lock"
MAX_PROOFS = 128
MAX_PROOF_TTL_SECONDS = 300.0


class ActivationProofError(RuntimeError):
    """A recent exact profile-test proof is absent, stale, or mismatched."""

    code = "test_required"
    safe_message = "Run Test connection again before activating this profile."


@dataclass(frozen=True)
class ActivationReconciliationOutcome:
    """What a reconciliation actually wrote, read back from disk afterwards.

    Superseding several receipts is not one atomic write, so this never claims
    more than it can prove.  ``committed`` means every planned receipt now
    reads as superseded.  ``incomplete`` means some still hold their pending
    claim; every field and every rollback file is still on disk, so a later
    inspection can resume from what remains.  ``uncertain`` means the attempt could
    not be proven complete, either from re-reading or on closing it out.

    The three id tuples are disjoint and each says exactly what was proven:
    ``superseded_activation_ids`` and ``unresolved_activation_ids`` were read
    back, so they really are closed and really do still hold a pending claim.
    ``unknown_activation_ids`` were never read back, so their state is unknown;
    they are kept separate precisely so an unreadable record is never described
    as a confirmed pending one.
    """

    state: Literal["committed", "incomplete", "uncertain"]
    kept_activation_id: str | None
    superseded_activation_ids: tuple[str, ...] = ()
    unresolved_activation_ids: tuple[str, ...] = ()
    unknown_activation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileTestProof:
    proof_id: str
    profile_id: str
    profile_digest: str
    base_registry_digest: str
    expires_at: float


def canonical_registry_bytes(registry: ConnectionRegistry | object | None) -> bytes:
    if registry is None:
        return b"null\n"
    normalized = (
        registry_to_document(registry)
        if isinstance(registry, ConnectionRegistry)
        else registry_to_document(registry_from_document(registry))
    )
    payload = (
        json.dumps(normalized, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_REGISTRY_BYTES:
        raise RuntimeError("Connection registry is too large")
    return payload


def registry_digest(registry: ConnectionRegistry | object | None) -> str:
    return _sha256(canonical_registry_bytes(registry))


def profile_digest(profile: ConnectionProfile) -> str:
    document = _profile_document(profile)
    payload = json.dumps(
        document, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return _sha256(payload)


def current_registry_snapshot(state_root: Path) -> tuple[ConnectionRegistry, str]:
    registry, _payload = _read_registry(Path(state_root))
    return registry, registry_digest(registry)


class ConnectionRegistryMutationService:
    """Serialize CAS edits and retain only bounded, short-lived Test proofs."""

    def __init__(
        self,
        state_root: Path,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
        proof_ttl_seconds: float = 120.0,
    ) -> None:
        if (
            isinstance(proof_ttl_seconds, bool)
            or not isinstance(proof_ttl_seconds, (int, float))
            or not 1.0 <= proof_ttl_seconds <= MAX_PROOF_TTL_SECONDS
        ):
            raise ValueError("proof_ttl_seconds must be between 1 and 300 seconds")
        self._state_root = Path(state_root)
        self._clock = monotonic_clock
        self._proof_ttl = float(proof_ttl_seconds)
        self._proofs: dict[str, ProfileTestProof] = {}
        self._proof_lock = threading.Lock()

    def issue_successful_test_proof(
        self,
        profile: ConnectionProfile,
        result: ProfileTestResult,
        *,
        base_registry_digest: str,
    ) -> ProfileTestProof:
        """Record one exact successful Test result without persisting authority."""

        base_registry_digest = _validated_digest(
            base_registry_digest, "base_registry_digest"
        )
        with connection_registry_mutation_lock(self._state_root):
            current, _raw = _read_registry(self._state_root)
            _require_digest(current, base_registry_digest)
        profile_test_result_to_document(result)
        if (
            result.status != "ready"
            or result.profile_id != profile.profile_id
            or result.kind != profile.kind
            or result.actual_workspace_id != profile.expected_workspace_id
        ):
            raise ActivationProofError(
                "Profile Test result does not prove the requested workspace authority"
            )
        now = float(self._clock())
        if not 0 <= now < float("inf"):
            raise RuntimeError("Monotonic clock returned an invalid value")
        proof = ProfileTestProof(
            proof_id=str(uuid.uuid4()),
            profile_id=profile.profile_id,
            profile_digest=profile_digest(profile),
            base_registry_digest=base_registry_digest,
            expires_at=now + self._proof_ttl,
        )
        with self._proof_lock:
            self._prune_proofs_locked(now)
            if len(self._proofs) >= MAX_PROOFS:
                oldest = min(self._proofs.values(), key=lambda item: item.expires_at)
                self._proofs.pop(oldest.proof_id, None)
            self._proofs[proof.proof_id] = proof
        return proof

    def save_metadata(
        self,
        registry: ConnectionRegistry | object,
        *,
        expected_registry_digest: str,
    ) -> tuple[ConnectionRegistry, str]:
        """CAS-save metadata without changing the running authority selection."""

        candidate = _normalized_registry(registry)
        expected = _validated_digest(expected_registry_digest, "expected_registry_digest")
        with connection_registry_mutation_lock(self._state_root):
            current, _raw = _read_registry(self._state_root)
            _require_digest(current, expected)
            _require_metadata_only_change(current, candidate)
            _replace_registry_if_digest(self._state_root, candidate, expected)
        return candidate, registry_digest(candidate)

    def activate(
        self,
        registry: ConnectionRegistry | object,
        profile_id: str,
        proof_id: str,
        *,
        expected_registry_digest: str,
    ) -> ActivationReceipt:
        """CAS-activate one exactly tested profile and preserve rollback evidence."""

        candidate = _normalized_registry(registry)
        expected = _validated_digest(expected_registry_digest, "expected_registry_digest")
        profile_id = _canonical_uuid(profile_id, "profile_id")
        proof_id = _canonical_uuid(proof_id, "proof_id")
        if candidate.active_profile_id != profile_id:
            raise RuntimeError("Activated profile must be selected by the candidate registry")
        matches = [profile for profile in candidate.profiles if profile.profile_id == profile_id]
        if len(matches) != 1 or not matches[0].enabled:
            raise RuntimeError("Activated profile must exist exactly once and be enabled")
        target = matches[0]

        with self._proof_lock:
            now = float(self._clock())
            self._prune_proofs_locked(now)
            proof = self._proofs.get(proof_id)
            if proof is None or proof.expires_at <= now:
                raise ActivationProofError("Profile Test proof is missing or expired")
            if (
                proof.profile_id != profile_id
                or proof.profile_digest != profile_digest(target)
                or proof.base_registry_digest != expected
            ):
                raise ActivationProofError(
                    "Profile Test proof does not match the exact activation candidate"
                )

        activation_id = str(uuid.uuid4())
        rollback_name = f"{activation_id}.rollback.json"
        receipt_path = _receipt_path(self._state_root, activation_id)
        rollback_path = _safe_rollback_path(self._state_root, rollback_name)
        candidate_digest = registry_digest(candidate)
        prepared = ActivationReceipt(
            activation_id=activation_id,
            state="prepared",
            previous_registry_digest=expected,
            activated_registry_digest=candidate_digest,
            profile_id=profile_id,
            profile_digest=profile_digest(target),
            proof_digest=_sha256(proof_id.encode("ascii")),
            rollback_file=rollback_name,
        )

        with connection_registry_mutation_lock(self._state_root):
            current, current_raw = _read_registry(self._state_root)
            _require_digest(current, expected)
            decision = classify_activation_attempt(
                activation_receipts(self._state_root),
                current_registry_digest=expected,
                candidate_registry_digest=candidate_digest,
                profile_id=profile_id,
                profile_digest=prepared.profile_digest,
            )
            if decision.action == "refuse":
                raise ActivationAttemptRefusedError(decision.code)
            if decision.receipt is not None:
                pending = self._continue_activation(decision.receipt, candidate, expected)
            else:
                _write_new(rollback_path, current_raw, "activation rollback")
                _write_new(receipt_path, _receipt_bytes(prepared), "activation receipt")
                _replace_registry_if_digest(self._state_root, candidate, expected)
                pending = replace(prepared, state="pending")
                _replace_receipt_if_digest(
                    receipt_path, pending, _sha256(_receipt_bytes(prepared))
                )
        with self._proof_lock:
            self._proofs.pop(proof_id, None)
        return pending

    def _continue_activation(
        self,
        receipt: ActivationReceipt,
        candidate: ConnectionRegistry,
        expected: str,
    ) -> ActivationReceipt:
        """Drive the first attempt's evidence forward instead of opening a second.

        The caller already holds the mutation lock and has proven that the
        registry is ``expected``.  The receipt keeps its original ancestry, so a
        later restore still reaches the state that preceded the first attempt
        rather than the state a failed retry observed.
        """

        persisted, persisted_raw = _read_receipt(
            self._state_root, receipt.activation_id
        )
        if persisted != receipt:
            raise RegistryConflictError("Activation receipt changed before retry")
        validate_activation_rollback(self._state_root, persisted)
        if expected != persisted.activated_registry_digest:
            _replace_registry_if_digest(self._state_root, candidate, expected)
        if persisted.state != "prepared":
            return persisted
        pending = replace(persisted, state="pending")
        _replace_receipt_if_digest(
            _receipt_path(self._state_root, persisted.activation_id),
            pending,
            _sha256(persisted_raw),
        )
        return pending

    def reconcile_activations(
        self,
        *,
        expected_registry_digest: str,
        expected_kept_activation_id: str | None = None,
    ) -> ActivationReconciliationOutcome:
        """Supersede provably no-op activation evidence without deleting it.

        This is the explicit escape from receipts that older builds accumulated.
        It refuses unless every unconfirmed receipt is either the one holding
        real rollback authority for the live registry or one whose rollback is
        digest-equal to that same live registry.  Superseded records keep every
        field and every rollback file; only their pending claim is closed.

        Every precondition is proven under this lock before the first
        transition: the live registry digest, the plan, the exact receipt the
        caller expects to keep, that receipt's active-profile and rollback
        bindings, and each target's compare-and-swap digest.  Only that phase
        raises, and it marks what it raises as the proven zero-write refusal it
        is, so a caller never has to read that promise out of an exception's
        class.  From the first transition on, releasing the lock included,
        nothing ordinary escapes: disk is reported instead.
        """

        expected = _validated_digest(expected_registry_digest, "expected_registry_digest")
        observed = ActivationReconciliationOutcome("uncertain", None)
        transition_began = False
        try:
            with connection_registry_mutation_lock(self._state_root):
                current, _raw = _read_registry(self._state_root)
                _require_digest(current, expected)
                plan = plan_activation_reconciliation(
                    activation_receipts(self._state_root),
                    current_registry_digest=expected,
                )
                if plan.code:
                    raise ActivationAttemptRefusedError(plan.code)
                targets = self._reconciliation_targets(
                    current, expected, plan, expected_kept_activation_id
                )
                transition_began = True
                observed = _apply_reconciliation(self._state_root, plan, targets)
            return observed
        except Exception as error:
            if not transition_began:
                setattr(error, _PROVEN_ZERO_WRITE, True)
                raise
            # Releasing the lock runs after the result was computed, so this
            # keeps every state already proven and withdraws only the claim
            # that the whole run completed.
            if observed.state != "committed":
                return observed
            return replace(observed, state="uncertain")

    def _reconciliation_targets(
        self,
        current: ConnectionRegistry,
        current_digest: str,
        plan: ActivationReconciliationPlan,
        expected_kept_activation_id: str | None,
    ) -> tuple[tuple[ActivationReceipt, str], ...]:
        """Prove every reconciliation precondition before any receipt moves."""

        if expected_kept_activation_id is not None:
            expected_kept = _canonical_uuid(
                expected_kept_activation_id, "expected_kept_activation_id"
            )
            if plan.keep is None or plan.keep.activation_id != expected_kept:
                raise RegistryConflictError(
                    "Reconciliation would not keep the expected activation"
                )
        if plan.keep is not None:
            _require_live_rollback_authority(
                self._state_root, current, current_digest, plan.keep
            )
        targets: list[tuple[ActivationReceipt, str]] = []
        for receipt in plan.supersede:
            persisted, persisted_raw = _read_receipt(
                self._state_root, receipt.activation_id
            )
            if persisted != receipt:
                raise RegistryConflictError(
                    "Activation evidence changed before reconciliation"
                )
            _require_readable_rollback(self._state_root, persisted)
            targets.append((receipt, _sha256(persisted_raw)))
        return tuple(targets)

    def restore(
        self,
        activation_id: str,
        *,
        expected_registry_digest: str,
    ) -> ActivationReceipt:
        """Explicitly restore the exact rollback while activation is unconfirmed."""

        activation_id = _canonical_uuid(activation_id, "activation_id")
        expected = _validated_digest(expected_registry_digest, "expected_registry_digest")
        with connection_registry_mutation_lock(self._state_root):
            receipt, receipt_raw = _read_receipt(self._state_root, activation_id)
            if receipt.state not in UNCONFIRMED_ACTIVATION_STATES:
                raise RuntimeError("Only an unconfirmed activation can be restored")
            current, _current_raw = _read_registry(self._state_root)
            current_digest = registry_digest(current)
            if current_digest != expected:
                raise RegistryConflictError("Connection registry changed before restore")
            try:
                pending = pending_activation_for_registry(
                    self._state_root, current_digest
                )
            except RuntimeError as error:
                raise RegistryConflictError(
                    "Pending activation evidence changed before restore"
                ) from error
            if pending is None or pending.activation_id != activation_id:
                raise RegistryConflictError(
                    "Activation is not the sole pending record for this registry"
                )
            rollback_path = _safe_rollback_path(self._state_root, receipt.rollback_file)
            rollback_raw = _read_bounded_regular_file(
                rollback_path, MAX_REGISTRY_BYTES, "activation rollback"
            )
            rollback = _registry_from_bytes(rollback_raw, "activation rollback")
            if registry_digest(rollback) != receipt.previous_registry_digest:
                raise RuntimeError("Activation rollback digest is invalid")
            if current_digest == receipt.activated_registry_digest:
                _replace_registry_if_digest(
                    self._state_root, rollback, receipt.activated_registry_digest
                )
            elif current_digest != receipt.previous_registry_digest:
                raise RegistryConflictError(
                    "Connection registry is neither the activated nor rollback state"
                )
            restored = replace(receipt, state="restored")
            _replace_receipt_if_digest(
                _receipt_path(self._state_root, activation_id),
                restored,
                _sha256(receipt_raw),
            )
            return restored

    def confirm(
        self,
        activation_id: str,
        *,
        expected_registry_digest: str,
    ) -> ActivationReceipt:
        """Explicitly close rollback eligibility after verifying the active digest."""

        activation_id = _canonical_uuid(activation_id, "activation_id")
        expected = _validated_digest(expected_registry_digest, "expected_registry_digest")
        with connection_registry_mutation_lock(self._state_root):
            receipt, receipt_raw = _read_receipt(self._state_root, activation_id)
            if receipt.state not in UNCONFIRMED_ACTIVATION_STATES:
                raise RuntimeError("Only an unconfirmed activation can be confirmed")
            current, _raw = _read_registry(self._state_root)
            current_digest = registry_digest(current)
            if current_digest != expected or current_digest != receipt.activated_registry_digest:
                raise RegistryConflictError(
                    "Connection registry does not match the activated state"
                )
            confirmed = replace(receipt, state="confirmed")
            _replace_receipt_if_digest(
                _receipt_path(self._state_root, activation_id),
                confirmed,
                _sha256(receipt_raw),
            )
            return confirmed

    def _prune_proofs_locked(self, now: float) -> None:
        expired = [key for key, proof in self._proofs.items() if proof.expires_at <= now]
        for key in expired:
            self._proofs.pop(key, None)


_PROVEN_ZERO_WRITE = "activation_reconciliation_proved_zero_write"


def _apply_reconciliation(
    state_root: Path,
    plan: ActivationReconciliationPlan,
    targets: tuple[tuple[ActivationReceipt, str], ...],
) -> ActivationReconciliationOutcome:
    """Transition every proven target, then report what disk really holds.

    Nothing raises from here on.  A failed transition stops the run instead of
    unwinding it, because superseded records are exactly the evidence a later
    inspection resumes from, and no rollback file is ever removed.

    The transition guard is deliberately every ordinary exception rather than
    the expected families alone.  From the first compare-and-swap onward a
    receipt may already have moved, so an unexpected failure must still be
    reported from disk instead of escaping as a refusal that promises no write
    happened.  ``BaseException`` still propagates, so process termination and
    other control flow are never swallowed.
    """

    for receipt, expected_digest in targets:
        try:
            _replace_receipt_if_digest(
                _receipt_path(state_root, receipt.activation_id),
                replace(receipt, state="superseded"),
                expected_digest,
            )
        except Exception:
            break
    kept = None if plan.keep is None else plan.keep.activation_id
    return _observed_reconciliation(state_root, kept, targets)


def reconciliation_proved_zero_write(error: BaseException) -> bool:
    """Answer whether the core itself proved this error wrote nothing.

    The exception's class cannot: the same ordinary families are raised by the
    proving phase and by the mutation lock's own exit after a receipt moved.
    """

    return getattr(error, _PROVEN_ZERO_WRITE, False) is True


def _observed_reconciliation(
    state_root: Path,
    kept_activation_id: str | None,
    targets: tuple[tuple[ActivationReceipt, str], ...],
) -> ActivationReconciliationOutcome:
    """Read every target back so the reported outcome is never a guess.

    A read that fails keeps every state already proven and names the records it
    could not reach, so an uncertain result still reports the progress it
    actually observed rather than discarding it.  Every ordinary exception is
    caught for the same reason the transition loop catches one: this runs after
    a receipt may already have moved.
    """

    superseded: list[str] = []
    unresolved: list[str] = []
    for index, (receipt, _expected_digest) in enumerate(targets):
        try:
            persisted = load_activation_receipt(state_root, receipt.activation_id)
        except Exception:
            return ActivationReconciliationOutcome(
                "uncertain",
                kept_activation_id,
                tuple(superseded),
                tuple(unresolved),
                tuple(item.activation_id for item, _digest in targets[index:]),
            )
        observed = superseded if persisted.state == "superseded" else unresolved
        observed.append(receipt.activation_id)
    return ActivationReconciliationOutcome(
        "incomplete" if unresolved else "committed",
        kept_activation_id,
        tuple(superseded),
        tuple(unresolved),
    )


def _require_live_rollback_authority(
    state_root: Path,
    current: ConnectionRegistry,
    current_digest: str,
    receipt: ActivationReceipt,
) -> None:
    """Prove one receipt still answers for the live registry and its profile."""

    active = _active_profile(current)
    if (
        receipt.activated_registry_digest != current_digest
        or current.active_profile_id != receipt.profile_id
        or profile_digest(active) != receipt.profile_digest
    ):
        raise RegistryConflictError(
            "Kept activation no longer matches the live connection registry"
        )
    _require_readable_rollback(state_root, receipt)


def _require_readable_rollback(state_root: Path, receipt: ActivationReceipt) -> None:
    """Prove the rollback bytes this receipt answers for are still intact."""

    rollback_raw = _read_bounded_regular_file(
        _safe_rollback_path(state_root, receipt.rollback_file),
        MAX_REGISTRY_BYTES,
        "activation rollback",
    )
    rollback = _registry_from_bytes(rollback_raw, "activation rollback")
    if registry_digest(rollback) != receipt.previous_registry_digest:
        raise RuntimeError("Activation rollback digest is invalid")


def validate_activation_rollback(
    state_root: Path, receipt: ActivationReceipt
) -> str:
    """Verify an exact persisted receipt/rollback pair and return its digest.

    This is a read-only recovery-boundary primitive.  It intentionally exposes
    only the previous registry digest, not profile paths or rollback bytes.
    """

    state_root = Path(state_root)
    persisted = load_activation_receipt(state_root, receipt.activation_id)
    if persisted != receipt:
        raise RegistryConflictError("Activation receipt changed before recovery")
    _require_readable_rollback(state_root, persisted)
    return persisted.previous_registry_digest


def pending_activation_for_registry(
    state_root: Path, expected_registry_digest: str
) -> ActivationReceipt | None:
    """Find the sole pending activation bound to the current registry digest."""

    expected = _validated_digest(
        expected_registry_digest, "expected_registry_digest"
    )
    matches = [
        receipt
        for receipt in activation_receipts(Path(state_root))
        if receipt.state in UNCONFIRMED_ACTIVATION_STATES
        and receipt.activated_registry_digest == expected
    ]
    if len(matches) > 1:
        raise RuntimeError("Multiple pending activations match the current registry")
    return matches[0] if matches else None


def _normalized_registry(registry: ConnectionRegistry | object) -> ConnectionRegistry:
    return (
        registry_from_document(registry_to_document(registry))
        if isinstance(registry, ConnectionRegistry)
        else registry_from_document(registry)
    )


def _profile_document(profile: ConnectionProfile) -> dict[str, object]:
    # The registry schema requires its active profile to be enabled.  A profile
    # Test may still inspect a disabled draft, so validate every other field
    # through a temporary enabled representation and then restore the exact
    # enabled bit into the canonical profile document.
    validated = replace(profile, enabled=True)
    registry = ConnectionRegistry(1, validated.profile_id, (validated,))
    document = dict(registry_to_document(registry)["profiles"][0])
    document["enabled"] = profile.enabled
    return document


def _active_profile(registry: ConnectionRegistry) -> ConnectionProfile:
    matches = [
        profile
        for profile in registry.profiles
        if profile.profile_id == registry.active_profile_id
    ]
    if len(matches) != 1:
        raise RuntimeError("Connection registry active profile is invalid")
    return matches[0]


def _authority_document(profile: ConnectionProfile) -> dict[str, object]:
    document = _profile_document(profile)
    document.pop("label", None)
    document.pop("live_updates", None)
    return document


def _require_metadata_only_change(
    current: ConnectionRegistry, candidate: ConnectionRegistry
) -> None:
    if candidate.active_profile_id != current.active_profile_id:
        raise RuntimeError("Metadata save cannot change the active profile")
    if _authority_document(_active_profile(candidate)) != _authority_document(
        _active_profile(current)
    ):
        raise RuntimeError("Metadata save cannot change the active profile authority")


def _read_registry(state_root: Path) -> tuple[ConnectionRegistry, bytes]:
    path = state_root / REGISTRY_FILE
    payload = _read_bounded_regular_file(path, MAX_REGISTRY_BYTES, "connection registry")
    return _registry_from_bytes(payload, "connection registry"), payload


def _registry_from_bytes(payload: bytes, description: str) -> ConnectionRegistry:
    try:
        raw = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise RuntimeError(f"{description} is invalid JSON") from error
    try:
        return registry_from_document(raw)
    except RuntimeError as error:
        raise RuntimeError(f"{description} is invalid") from error


def _require_digest(registry: ConnectionRegistry, expected: str) -> None:
    if registry_digest(registry) != expected:
        raise RegistryConflictError("Connection registry changed after it was read")


def _replace_registry_if_digest(
    state_root: Path, registry: ConnectionRegistry, expected_digest: str
) -> None:
    current, _payload = _read_registry(state_root)
    _require_digest(current, expected_digest)
    _atomic_replace(state_root / REGISTRY_FILE, canonical_registry_bytes(registry))


@contextmanager
def connection_registry_mutation_lock(state_root: Path):
    """Acquire the one cross-process lock shared by all registry writers."""

    state_root = Path(state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / MUTATION_LOCK_FILE
    stream = path.open("a+b")
    acquired = False
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError as error:
                raise RegistryConflictError(
                    "Connection registry mutation is already in progress"
                ) from error
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                raise RegistryConflictError(
                    "Connection registry mutation is already in progress"
                ) from error
        yield
    finally:
        try:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
