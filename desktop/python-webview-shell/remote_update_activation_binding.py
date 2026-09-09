"""Durable binding from one flow operation to one registry activation receipt.

``ConnectionRegistryMutationService.activate`` mints its own ``activation_id``
inside the mutation lock, while the remote update flow names its mutation with
its own ``operation_id``.  The two identities are not equal and neither one can
be derived from the other, so a desktop that dies between issuing an activation
and reading its answer has no way to ask "did *my* operation write a receipt?"
unless it wrote something down first.

This module is that something.  Before the effect, the caller records the exact
candidate fingerprint the receipt will carry if it is ever written:

    profile_id, profile_digest, previous_registry_digest,
    activated_registry_digest, proof_digest

Every one of those five fields is known before ``activate`` is called, and the
receipt the registry writes carries exactly those five values (only
``activation_id``, ``rollback_file`` and ``state`` are decided by the service).
So after a lost response the operation is recovered by matching the fingerprint
against the receipts already on disk, and the resolved ``activation_id`` is
written back so that later confirm/rollback target that exact receipt.

Scope and refusals:

* The store owns one small bounded JSON file in the desktop state root, next to
  ``connection-registry.json``.  It never writes the registry, never writes an
  activation receipt, never touches the SSOT, and holds no authority of its own:
  losing the file loses recovery, not correctness.
* A fingerprint is immutable once recorded.  Re-binding the same operation to a
  different candidate is refused, so a stale page cannot repoint an operation at
  another receipt.
* A fingerprint belongs to exactly one operation.  Recovery names a receipt by
  its fingerprint alone, so two operations sharing one fingerprint would make
  that name ambiguous and would let an operation that never issued adopt the
  receipt another operation really wrote.  The second binding is refused.
* A resolved ``activation_id`` is immutable once recorded, so a later
  observation cannot silently select a different receipt for the same flow.
* The file is bounded in entries and in bytes; an unreadable or over-large file
  is refused rather than silently reset.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

BINDING_FILE = "remote-update-activation-bindings.json"
BINDING_SCHEMA_VERSION = 1
MAX_BINDING_BYTES = 64 * 1024
MAX_BINDINGS = 16

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENTRY_FIELDS = (
    "operation_id",
    "profile_id",
    "profile_digest",
    "previous_registry_digest",
    "activated_registry_digest",
    "proof_digest",
    "activation_id",
    "issued",
)

_REFUSALS = {
    "binding_absent": "This update operation has no recorded activation binding.",
    "binding_conflict": "This update operation is already bound to a different activation candidate.",
    "binding_owned": "Another update operation already owns this activation candidate.",
    "binding_store_full": "Too many activation bindings are recorded to add another.",
    "binding_store_invalid": "The recorded activation bindings could not be read.",
    "binding_unwritable": "The activation binding could not be recorded durably.",
}


class ActivationBindingError(RuntimeError):
    """A bounded, fail-closed refusal from the activation binding store."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _REFUSALS else "binding_store_invalid"
        self.safe_message = _REFUSALS[self.code]
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class ActivationBinding:
    """One operation, the candidate it names, and the receipt it resolved to.

    ``issued`` records that the mutation was actually attempted; it is written
    before the call, so a crash inside the call still leaves the attempt
    visible.  ``activation_id`` is ``None`` until a receipt carrying this exact
    fingerprint has been observed on disk.
    """

    operation_id: str
    profile_id: str
    profile_digest: str
    previous_registry_digest: str
    activated_registry_digest: str
    proof_digest: str
    activation_id: str | None = None
    issued: bool = False

    def fingerprint(self) -> tuple[str, str, str, str, str]:
        """The five receipt fields that are decided before the effect."""

        return (
            self.profile_id,
            self.profile_digest,
            self.previous_registry_digest,
            self.activated_registry_digest,
            self.proof_digest,
        )

    def matches_receipt(self, receipt: object) -> bool:
        """Report whether one receipt is exactly this operation's candidate."""

        try:
            observed = (
                receipt.profile_id,  # type: ignore[attr-defined]
                receipt.profile_digest,  # type: ignore[attr-defined]
                receipt.previous_registry_digest,  # type: ignore[attr-defined]
                receipt.activated_registry_digest,  # type: ignore[attr-defined]
                receipt.proof_digest,  # type: ignore[attr-defined]
            )
        except AttributeError:
            return False
        return observed == self.fingerprint()


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ActivationBindingError("binding_store_invalid")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ActivationBindingError("binding_store_invalid") from None
    if str(parsed) != value.lower() or value != value.lower():
        raise ActivationBindingError("binding_store_invalid")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.match(value):
        raise ActivationBindingError("binding_store_invalid")
    return value


def _entry_document(binding: ActivationBinding) -> dict[str, object]:
    return {
        "operation_id": binding.operation_id,
        "profile_id": binding.profile_id,
        "profile_digest": binding.profile_digest,
        "previous_registry_digest": binding.previous_registry_digest,
        "activated_registry_digest": binding.activated_registry_digest,
        "proof_digest": binding.proof_digest,
        "activation_id": binding.activation_id,
        "issued": bool(binding.issued),
    }


def _entry_from_document(raw: object) -> ActivationBinding:
    if not isinstance(raw, dict) or set(raw) != set(_ENTRY_FIELDS):
        raise ActivationBindingError("binding_store_invalid")
    activation_id = raw["activation_id"]
    if activation_id is not None:
        activation_id = _canonical_uuid(activation_id, "activation_id")
    if not isinstance(raw["issued"], bool):
        raise ActivationBindingError("binding_store_invalid")
    return ActivationBinding(
        operation_id=_bounded_operation_id(raw["operation_id"]),
        profile_id=_canonical_uuid(raw["profile_id"], "profile_id"),
        profile_digest=_digest(raw["profile_digest"]),
        previous_registry_digest=_digest(raw["previous_registry_digest"]),
        activated_registry_digest=_digest(raw["activated_registry_digest"]),
        proof_digest=_digest(raw["proof_digest"]),
        activation_id=activation_id,
        issued=raw["issued"],
    )


def _bounded_operation_id(value: object) -> str:
    """Accept the flow's own operation naming without reinterpreting it."""

    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        raise ActivationBindingError("binding_store_invalid")
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        raise ActivationBindingError("binding_store_invalid")
    return value


def _atomic_write(path: Path, payload: bytes) -> None:
    """Replace the bounded store in one step, or leave the old bytes in place."""

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise ActivationBindingError("binding_unwritable") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class ActivationBindingStore:
    """Bounded durable operation -> activation candidate/receipt bindings."""

    def __init__(self, state_root: Path) -> None:
        self._state_root = Path(state_root)

    @property
    def path(self) -> Path:
        return self._state_root / BINDING_FILE

    def load(self) -> tuple[ActivationBinding, ...]:
        """Read every recorded binding without writing anything."""

        path = self.path
        try:
            if not path.exists():
                return ()
            if not path.is_file() or path.is_symlink():
                raise ActivationBindingError("binding_store_invalid")
            payload = path.read_bytes()
        except ActivationBindingError:
            raise
        except OSError as error:
            raise ActivationBindingError("binding_store_invalid") from error
        if len(payload) > MAX_BINDING_BYTES:
            raise ActivationBindingError("binding_store_invalid")
        try:
            raw = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise ActivationBindingError("binding_store_invalid") from error
        return _validated_document(raw)

    def find(self, operation_id: str) -> ActivationBinding | None:
        """Return the binding for one operation, or ``None`` if it has none."""

        wanted = _bounded_operation_id(operation_id)
        for binding in self.load():
            if binding.operation_id == wanted:
                return binding
        return None

    def require(self, operation_id: str) -> ActivationBinding:
        binding = self.find(operation_id)
        if binding is None:
            raise ActivationBindingError("binding_absent")
        return binding

    def bind(self, binding: ActivationBinding) -> ActivationBinding:
        """Record one candidate fingerprint before its effect, idempotently.

        Re-binding the same operation to the same fingerprint returns the
        already recorded entry unchanged, so a retry of the same operation
        cannot lose an ``activation_id`` that was already resolved.  Any other
        fingerprint for that operation is refused, and this fingerprint under
        any other operation is refused: one fingerprint names one operation.
        """

        candidate = _validated_binding(binding)
        entries = list(self.load())
        for index, existing in enumerate(entries):
            if existing.operation_id != candidate.operation_id:
                if existing.fingerprint() == candidate.fingerprint():
                    raise ActivationBindingError("binding_owned")
                continue
            if existing.fingerprint() != candidate.fingerprint():
                raise ActivationBindingError("binding_conflict")
            return entries[index]
        if len(entries) >= MAX_BINDINGS:
            raise ActivationBindingError("binding_store_full")
        entries.append(candidate)
        self._save(entries)
        return candidate

    def mark_issued(self, operation_id: str) -> ActivationBinding:
        """Record that the mutation is about to be attempted."""

        return self._update(operation_id, issued=True)

    def resolve(self, operation_id: str, activation_id: str) -> ActivationBinding:
        """Bind this operation to the exact receipt identity that was observed.

        A resolved identity is immutable: an attempt to resolve the same
        operation to a different receipt is refused rather than silently
        repointed.
        """

        resolved = _canonical_uuid(activation_id, "activation_id")
        existing = self.require(operation_id)
        if existing.activation_id is not None:
            if existing.activation_id != resolved:
                raise ActivationBindingError("binding_conflict")
            return existing
        return self._update(operation_id, activation_id=resolved, issued=True)

    def release(self, operation_id: str) -> None:
        """Drop one settled binding; absent is not an error."""

        wanted = _bounded_operation_id(operation_id)
        recorded = self.load()
        entries = [entry for entry in recorded if entry.operation_id != wanted]
        if len(entries) != len(recorded):
            self._save(entries)

    def _update(self, operation_id: str, **changes: object) -> ActivationBinding:
        wanted = _bounded_operation_id(operation_id)
        entries = list(self.load())
        for index, existing in enumerate(entries):
            if existing.operation_id != wanted:
                continue
            updated = replace(existing, **changes)  # type: ignore[arg-type]
            if updated == existing:
                return existing
            entries[index] = updated
            self._save(entries)
            return updated
        raise ActivationBindingError("binding_absent")

    def _save(self, entries: list[ActivationBinding]) -> None:
        if len(entries) > MAX_BINDINGS:
            raise ActivationBindingError("binding_store_full")
        document = {
            "schema_version": BINDING_SCHEMA_VERSION,
            "bindings": [_entry_document(entry) for entry in entries],
        }
        payload = (
            json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8")
        if len(payload) > MAX_BINDING_BYTES:
            raise ActivationBindingError("binding_store_full")
        try:
            self._state_root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ActivationBindingError("binding_unwritable") from error
        _atomic_write(self.path, payload)


def _validated_binding(binding: ActivationBinding) -> ActivationBinding:
    if not isinstance(binding, ActivationBinding):
        raise ActivationBindingError("binding_store_invalid")
    return _entry_from_document(_entry_document(binding))


def _validated_document(raw: object) -> tuple[ActivationBinding, ...]:
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "bindings"}:
        raise ActivationBindingError("binding_store_invalid")
    if raw["schema_version"] != BINDING_SCHEMA_VERSION:
        raise ActivationBindingError("binding_store_invalid")
    records = raw["bindings"]
    if not isinstance(records, list) or len(records) > MAX_BINDINGS:
        raise ActivationBindingError("binding_store_invalid")
    entries = tuple(_entry_from_document(record) for record in records)
    if len({entry.operation_id for entry in entries}) != len(entries):
        raise ActivationBindingError("binding_store_invalid")
    if len({entry.fingerprint() for entry in entries}) != len(entries):
        raise ActivationBindingError("binding_store_invalid")
    return entries
