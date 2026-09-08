"""Exact connection-activation evidence and retry/reconciliation decisions.

This module owns the on-disk shape of activation receipts, the bounded reads
and writes of those records, and the rules that decide whether a new activation
attempt continues existing evidence, competes with it, or has run into ambiguity
that only an explicit reconciliation can resolve.  It never loads, writes, or
deletes the connection registry itself, and it never removes a receipt or a
rollback file: an outcome that cannot be proven is refused, not cleaned up.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, TypeAlias


ACTIVATION_DIRECTORY = "connection-registry-activations"
ACTIVATION_RECEIPT_VERSION = 1
MAX_RECEIPT_BYTES = 32 * 1024
MAX_ACTIVATION_RECORDS = 512
_DIGEST_PREFIX = "sha256:"
_RECEIPT_SUFFIX = ".receipt.json"

ActivationState: TypeAlias = Literal[
    "prepared", "pending", "confirmed", "restored", "superseded"
]
ActivationAttemptAction: TypeAlias = Literal["create", "resume", "refuse"]

#: States in which an activation still owns rollback authority.  Everything
#: else is closed evidence that no longer answers for the live registry.
UNCONFIRMED_ACTIVATION_STATES = frozenset({"prepared", "pending"})

_REFUSALS = {
    "activation_ambiguous": (
        "Multiple unconfirmed connection activations require explicit reconciliation."
    ),
    "activation_conflict": (
        "Another unconfirmed activation already targets this connection state."
    ),
    "activation_unconfirmed": (
        "Restore or confirm the unconfirmed connection activation before activating again."
    ),
    "activation_manual_review": (
        "Connection activation evidence cannot be reconciled without manual review."
    ),
}


class RegistryConflictError(RuntimeError):
    """The registry changed after the caller observed it."""

    code = "registry_conflict"
    safe_message = "Connection registry changed; reload it before trying again."


class ActivationAttemptRefusedError(RegistryConflictError):
    """A competing, ambiguous, or unprovable activation attempt is refused.

    It is a registry conflict so that the existing sanitized host boundary keeps
    reporting it as a conflict, while ``code`` distinguishes a retry that must
    resolve its own evidence from a foreign attempt with an unknown outcome.
    """

    def __init__(self, code: str) -> None:
        self.code = code if code in _REFUSALS else "activation_manual_review"
        self.safe_message = _REFUSALS[self.code]
        super().__init__(self.safe_message)


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


@dataclass(frozen=True)
class ActivationAttemptDecision:
    """Whether an activation may open, continue, or must refuse evidence."""

    action: ActivationAttemptAction
    receipt: ActivationReceipt | None = None
    code: str = ""


@dataclass(frozen=True)
class ActivationReconciliationPlan:
    """The single receipt that keeps rollback authority, plus provable no-ops."""

    keep: ActivationReceipt | None = None
    supersede: tuple[ActivationReceipt, ...] = ()
    code: str = ""


@dataclass(frozen=True)
class ActivationReconciliation:
    kept_activation_id: str | None
    superseded_activation_ids: tuple[str, ...]


def classify_activation_attempt(
    receipts: Iterable[ActivationReceipt],
    *,
    current_registry_digest: str,
    candidate_registry_digest: str,
    profile_id: str,
    profile_digest: str,
) -> ActivationAttemptDecision:
    """Decide one activation attempt against every unconfirmed receipt.

    A retry of the exact same candidate continues the receipt the first attempt
    opened, so the original rollback ancestry stays the authoritative one and a
    failed activation cannot accumulate a second pending record.  A retry is
    exact only when the profile identity, the profile digest, and the activated
    registry digest all match, and the registry is still either the state the
    receipt rolls back to or the state it activated.

    Any other unconfirmed receipt fails closed.  ``activation_conflict`` marks a
    foreign attempt that targets the same registry state and whose outcome is
    therefore unknown; ``activation_unconfirmed`` marks an unrelated activation
    that the caller must restore or confirm first.  Nothing here proves that an
    earlier attempt failed, so nothing here discards its evidence.
    """

    unconfirmed = tuple(
        receipt
        for receipt in receipts
        if receipt.state in UNCONFIRMED_ACTIVATION_STATES
    )
    if not unconfirmed:
        return ActivationAttemptDecision("create")
    retries = tuple(
        receipt
        for receipt in unconfirmed
        if _continues_attempt(
            receipt,
            current_registry_digest=current_registry_digest,
            candidate_registry_digest=candidate_registry_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        )
    )
    if len(retries) > 1:
        return ActivationAttemptDecision("refuse", code="activation_ambiguous")
    competing = tuple(receipt for receipt in unconfirmed if receipt not in retries)
    if competing:
        targets_same_state = any(
            receipt.activated_registry_digest == candidate_registry_digest
            for receipt in competing
        )
        return ActivationAttemptDecision(
            "refuse",
            code=(
                "activation_conflict"
                if targets_same_state
                else "activation_unconfirmed"
            ),
        )
    return ActivationAttemptDecision("resume", retries[0])


def _continues_attempt(
    receipt: ActivationReceipt,
    *,
    current_registry_digest: str,
    candidate_registry_digest: str,
    profile_id: str,
    profile_digest: str,
) -> bool:
    """Report whether one receipt is the same attempt as the requested one."""

    if receipt.profile_id != profile_id or receipt.profile_digest != profile_digest:
        return False
    if receipt.activated_registry_digest != candidate_registry_digest:
        return False
    return current_registry_digest in {
        receipt.previous_registry_digest,
        receipt.activated_registry_digest,
    }


def plan_activation_reconciliation(
    receipts: Iterable[ActivationReceipt],
    *,
    current_registry_digest: str,
) -> ActivationReconciliationPlan:
    """Plan which unconfirmed receipts are provably no-op rollback evidence.

    A receipt whose rollback state is the live registry state restores nothing:
    its rollback bytes are digest-equal to what is already active, whether the
    activation never reached the registry or a later attempt re-activated the
    same state.  Those are superseded, which keeps every field and every
    rollback file on disk and only stops them from answering as pending.

    At most one receipt may keep real rollback authority, and every receipt must
    fall into one of those two provable classes.  Anything else is refused with
    ``activation_manual_review`` and nothing is planned.
    """

    keep: list[ActivationReceipt] = []
    supersede: list[ActivationReceipt] = []
    for receipt in receipts:
        if receipt.state not in UNCONFIRMED_ACTIVATION_STATES:
            continue
        rolls_back_to_current = (
            receipt.previous_registry_digest == current_registry_digest
        )
        activated_current = (
            receipt.activated_registry_digest == current_registry_digest
        )
        if activated_current and not rolls_back_to_current:
            keep.append(receipt)
        elif rolls_back_to_current:
            supersede.append(receipt)
        else:
            return ActivationReconciliationPlan(code="activation_manual_review")
    if len(keep) > 1:
        return ActivationReconciliationPlan(code="activation_manual_review")
    return ActivationReconciliationPlan(
        keep[0] if keep else None, tuple(supersede)
    )


def load_activation_receipt(state_root: Path, activation_id: str) -> ActivationReceipt:
    receipt, _payload = _read_receipt(
        Path(state_root), _canonical_uuid(activation_id, "activation_id")
    )
    return receipt


def activation_receipts(state_root: Path) -> tuple[ActivationReceipt, ...]:
    """Read every bounded activation receipt without changing any of them."""

    root = _activation_root(Path(state_root))
    if not root.exists():
        return ()
    if not root.is_dir() or _is_link_like(root):
        raise RuntimeError("Activation record directory is invalid")
    try:
        entries = tuple(root.iterdir())
    except OSError as error:
        raise RuntimeError("Could not inspect activation records") from error
    if len(entries) > MAX_ACTIVATION_RECORDS:
        raise RuntimeError("Too many activation records require manual review")
    receipts: list[ActivationReceipt] = []
    for path in entries:
        if not path.name.endswith(_RECEIPT_SUFFIX):
            continue
        name = path.name[: -len(_RECEIPT_SUFFIX)]
        try:
            activation_id = _canonical_uuid(name, "activation_id")
        except RuntimeError:
            raise RuntimeError("Activation receipt filename is invalid") from None
        receipts.append(load_activation_receipt(root.parent, activation_id))
    return tuple(sorted(receipts, key=lambda receipt: receipt.activation_id))


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


def _activation_root(state_root: Path) -> Path:
    return state_root / ACTIVATION_DIRECTORY


def _receipt_path(state_root: Path, activation_id: str) -> Path:
    return _activation_root(state_root) / f"{activation_id}{_RECEIPT_SUFFIX}"


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
    if receipt.state not in {
        "prepared",
        "pending",
        "confirmed",
        "restored",
        "superseded",
    }:
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
