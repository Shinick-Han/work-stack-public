"""Fail-closed mutation boundary for the desktop connection registry.

This module owns configuration files under the desktop state root only.  It
never constructs a :class:`workstack.store.Store`, creates an SSOT directory,
or runs SSH.  Metadata edits use compare-and-swap, while activation additionally
requires a short-lived in-process proof produced from an exact successful
read-only profile test.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal, TypeAlias

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
ACTIVATION_DIRECTORY = "connection-registry-activations"
ACTIVATION_RECEIPT_VERSION = 1
MAX_RECEIPT_BYTES = 32 * 1024
MAX_PROOFS = 128
MAX_ACTIVATION_RECORDS = 512
MAX_PROOF_TTL_SECONDS = 300.0
_DIGEST_PREFIX = "sha256:"

ActivationState: TypeAlias = Literal["prepared", "pending", "confirmed", "restored"]


class RegistryConflictError(RuntimeError):
    """The registry changed after the caller observed it."""

    code = "registry_conflict"
    safe_message = "Connection registry changed; reload it before trying again."


class ActivationProofError(RuntimeError):
    """A recent exact profile-test proof is absent, stale, or mismatched."""

    code = "test_required"
    safe_message = "Run Test connection again before activating this profile."


@dataclass(frozen=True)
class ProfileTestProof:
    proof_id: str
    profile_id: str
    profile_digest: str
    base_registry_digest: str
    expires_at: float


@dataclass(frozen=True)
class ActivationReceipt:
    activation_id: str
    state: ActivationState
    previous_registry_digest: str
    activated_registry_digest: str
    profile_id: str
    profile_digest: str
    proof_digest: str
    rollback_file: str

    @property
    def current_registry_digest(self) -> str:
        """Digest expected to be current while this activation is pending."""

        return self.activated_registry_digest


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
        rollback_path = _activation_root(self._state_root) / rollback_name
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
            if receipt.state not in {"prepared", "pending"}:
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
            if receipt.state not in {"prepared", "pending"}:
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


def load_activation_receipt(state_root: Path, activation_id: str) -> ActivationReceipt:
    receipt, _payload = _read_receipt(
        Path(state_root), _canonical_uuid(activation_id, "activation_id")
    )
    return receipt


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
    rollback_path = _safe_rollback_path(state_root, persisted.rollback_file)
    rollback_raw = _read_bounded_regular_file(
        rollback_path, MAX_REGISTRY_BYTES, "activation rollback"
    )
    rollback = _registry_from_bytes(rollback_raw, "activation rollback")
    digest = registry_digest(rollback)
    if digest != persisted.previous_registry_digest:
        raise RuntimeError("Activation rollback digest is invalid")
    return digest


def pending_activation_for_registry(
    state_root: Path, expected_registry_digest: str
) -> ActivationReceipt | None:
    """Find the sole pending activation bound to the current registry digest."""

    expected = _validated_digest(
        expected_registry_digest, "expected_registry_digest"
    )
    root = _activation_root(Path(state_root))
    if not root.exists():
        return None
    if not root.is_dir() or _is_link_like(root):
        raise RuntimeError("Activation record directory is invalid")
    try:
        entries = tuple(root.iterdir())
    except OSError as error:
        raise RuntimeError("Could not inspect activation records") from error
    if len(entries) > MAX_ACTIVATION_RECORDS:
        raise RuntimeError("Too many activation records require manual review")
    matches: list[ActivationReceipt] = []
    for path in entries:
        suffix = ".receipt.json"
        if not path.name.endswith(suffix):
            continue
        activation_id = path.name[: -len(suffix)]
        try:
            activation_id = _canonical_uuid(activation_id, "activation_id")
        except RuntimeError:
            raise RuntimeError("Activation receipt filename is invalid") from None
        receipt = load_activation_receipt(root.parent, activation_id)
        if (
            receipt.state in {"prepared", "pending"}
            and receipt.activated_registry_digest == expected
        ):
            matches.append(receipt)
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


def _sha256(payload: bytes) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(payload).hexdigest()


def _validated_digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != len(_DIGEST_PREFIX) + 64
        or not value.startswith(_DIGEST_PREFIX)
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise RuntimeError(f"{field} must be a canonical SHA-256 digest")
    return value


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{field} must be a canonical non-nil UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise RuntimeError(f"{field} must be a canonical non-nil UUID") from error
    if parsed.int == 0 or str(parsed) != value:
        raise RuntimeError(f"{field} must be a canonical non-nil UUID")
    return value


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


def _read_bounded_regular_file(path: Path, maximum: int, description: str) -> bytes:
    if not path.is_file() or _is_link_like(path):
        raise RuntimeError(f"{description} is missing or not a regular file")
    try:
        before = path.stat()
        with path.open("rb") as stream:
            payload = stream.read(maximum + 1)
        after = path.stat()
    except OSError as error:
        raise RuntimeError(f"Could not read {description}") from error
    if len(payload) > maximum:
        raise RuntimeError(f"{description} is too large")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RegistryConflictError(f"{description} changed while it was read")
    return payload


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


def _require_digest(registry: ConnectionRegistry, expected: str) -> None:
    if registry_digest(registry) != expected:
        raise RegistryConflictError("Connection registry changed after it was read")


def _replace_registry_if_digest(
    state_root: Path, registry: ConnectionRegistry, expected_digest: str
) -> None:
    current, _payload = _read_registry(state_root)
    _require_digest(current, expected_digest)
    _atomic_replace(state_root / REGISTRY_FILE, canonical_registry_bytes(registry))


def _activation_root(state_root: Path) -> Path:
    return state_root / ACTIVATION_DIRECTORY


def _receipt_path(state_root: Path, activation_id: str) -> Path:
    return _activation_root(state_root) / f"{activation_id}.receipt.json"


def _safe_rollback_path(state_root: Path, name: str) -> Path:
    if (
        not isinstance(name, str)
        or Path(name).name != name
        or not name.endswith(".rollback.json")
        or len(name) > 100
    ):
        raise RuntimeError("Activation rollback filename is invalid")
    return _activation_root(state_root) / name


def _receipt_document(receipt: ActivationReceipt) -> dict[str, object]:
    activation_id = _canonical_uuid(receipt.activation_id, "activation_id")
    if receipt.rollback_file != f"{activation_id}.rollback.json":
        raise RuntimeError("Activation rollback is not bound to its receipt")
    return {
        "schema_version": ACTIVATION_RECEIPT_VERSION,
        "activation_id": activation_id,
        "state": receipt.state,
        "previous_registry_digest": _validated_digest(
            receipt.previous_registry_digest, "previous_registry_digest"
        ),
        "activated_registry_digest": _validated_digest(
            receipt.activated_registry_digest, "activated_registry_digest"
        ),
        "profile_id": _canonical_uuid(receipt.profile_id, "profile_id"),
        "profile_digest": _validated_digest(receipt.profile_digest, "profile_digest"),
        "proof_digest": _validated_digest(receipt.proof_digest, "proof_digest"),
        "rollback_file": _safe_rollback_path(Path("."), receipt.rollback_file).name,
    }


def _receipt_bytes(receipt: ActivationReceipt) -> bytes:
    if receipt.state not in {"prepared", "pending", "confirmed", "restored"}:
        raise RuntimeError("Activation receipt state is invalid")
    payload = (
        json.dumps(
            _receipt_document(receipt), ensure_ascii=True, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_RECEIPT_BYTES:
        raise RuntimeError("Activation receipt is too large")
    return payload


def _read_receipt(
    state_root: Path, activation_id: str
) -> tuple[ActivationReceipt, bytes]:
    payload = _read_bounded_regular_file(
        _receipt_path(state_root, activation_id),
        MAX_RECEIPT_BYTES,
        "activation receipt",
    )
    try:
        raw = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise RuntimeError("Activation receipt is invalid JSON") from error
    expected = {
        "schema_version",
        "activation_id",
        "state",
        "previous_registry_digest",
        "activated_registry_digest",
        "profile_id",
        "profile_digest",
        "proof_digest",
        "rollback_file",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise RuntimeError("Activation receipt has unknown or missing fields")
    receipt = ActivationReceipt(
        activation_id=_canonical_uuid(raw["activation_id"], "activation_id"),
        state=raw["state"],
        previous_registry_digest=_validated_digest(
            raw["previous_registry_digest"], "previous_registry_digest"
        ),
        activated_registry_digest=_validated_digest(
            raw["activated_registry_digest"], "activated_registry_digest"
        ),
        profile_id=_canonical_uuid(raw["profile_id"], "profile_id"),
        profile_digest=_validated_digest(raw["profile_digest"], "profile_digest"),
        proof_digest=_validated_digest(raw["proof_digest"], "proof_digest"),
        rollback_file=_safe_rollback_path(state_root, raw["rollback_file"]).name,
    )
    if raw["schema_version"] != ACTIVATION_RECEIPT_VERSION:
        raise RuntimeError("Activation receipt schema version is invalid")
    if receipt.activation_id != activation_id:
        raise RuntimeError("Activation receipt identity does not match its filename")
    if receipt.rollback_file != f"{activation_id}.rollback.json":
        raise RuntimeError("Activation rollback is not bound to its receipt")
    _receipt_bytes(receipt)
    return receipt, payload


def _write_new(path: Path, payload: bytes, description: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_link_like(path.parent):
        raise RuntimeError(f"{description} directory must not be a link or junction")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise RegistryConflictError(f"{description} already exists") from error
    except (RegistryConflictError, RuntimeError):
        raise
    except OSError as error:
        raise RuntimeError(f"Could not save {description}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_replace(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise RuntimeError("Could not atomically replace connection registry state") from error
    finally:
        temporary.unlink(missing_ok=True)


def _replace_receipt_if_digest(
    path: Path, receipt: ActivationReceipt, expected_digest: str
) -> None:
    current = _read_bounded_regular_file(path, MAX_RECEIPT_BYTES, "activation receipt")
    if _sha256(current) != expected_digest:
        raise RegistryConflictError("Activation receipt changed before transition")
    _atomic_replace(path, _receipt_bytes(receipt))


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
