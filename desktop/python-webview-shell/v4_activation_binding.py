"""Inactive v4 activation proof and restart-confirmation contract.

The released registry activation path remains v3-only.  This module is an
opt-in contract for a future v4 activation coordinator: it binds one exact
profile and registry candidate to one read-only authority inspection, while
keeping rollback evidence eligible until a restart performs the same
inspection again.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass, replace
from typing import Callable, Literal, Mapping, TypeAlias

from connection_registry import ConnectionProfile
from connection_registry_mutations import profile_digest
from profile_inspection import AuthorityInspection, ProfileTestResult


V4_ACTIVATION_CONTRACT_VERSION = 1
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
V4ReceiptState: TypeAlias = Literal["pending", "confirmed", "restored"]


class V4ActivationDisabledError(RuntimeError):
    """The inactive v4 activation contract was used without explicit opt-in."""

    code = "v4_activation_disabled"


class V4ActivationBindingError(RuntimeError):
    """An exact proof, receipt, or restart coordinate did not match."""

    code = "v4_activation_binding_mismatch"


@dataclass(frozen=True)
class V4ActivationCoordinates:
    profile_id: str
    profile_digest: str
    registry_digest: str
    workspace_uid: str
    storage_format: Literal["v4"]
    schema_version: Literal[4]
    authority_manifest_digest: str


@dataclass(frozen=True)
class V4ActivationProof:
    contract_version: Literal[1]
    proof_id: str
    coordinates: V4ActivationCoordinates


@dataclass(frozen=True)
class V4ActivationReceipt:
    contract_version: Literal[1]
    activation_id: str
    state: V4ReceiptState
    coordinates: V4ActivationCoordinates
    proof_digest: str
    previous_registry_digest: str
    rollback_artifact_digest: str

    @property
    def rollback_available(self) -> bool:
        return self.state == "pending"


ReinspectProfile: TypeAlias = Callable[[], ProfileTestResult]


def issue_v4_activation_proof(
    profile: ConnectionProfile,
    result: ProfileTestResult,
    *,
    registry_digest: str,
    enable_v4_activation: bool = False,
) -> V4ActivationProof:
    """Create one exact v4-only proof; unavailable unless explicitly enabled."""

    _require_enabled(enable_v4_activation)
    coordinates = _coordinates(profile, registry_digest, result)
    return V4ActivationProof(
        contract_version=V4_ACTIVATION_CONTRACT_VERSION,
        proof_id=str(uuid.uuid4()),
        coordinates=coordinates,
    )


def prepare_v4_activation_receipt(
    proof: V4ActivationProof,
    *,
    previous_registry_digest: str,
    rollback_artifact_digest: str,
    enable_v4_activation: bool = False,
) -> V4ActivationReceipt:
    """Bind immutable rollback evidence to one validated activation proof."""

    _require_enabled(enable_v4_activation)
    proof_document = v4_activation_proof_to_document(proof)
    return V4ActivationReceipt(
        contract_version=V4_ACTIVATION_CONTRACT_VERSION,
        activation_id=str(uuid.uuid4()),
        state="pending",
        coordinates=proof.coordinates,
        proof_digest=_document_digest(proof_document),
        previous_registry_digest=_validated_digest(
            previous_registry_digest, "previous_registry_digest"
        ),
        rollback_artifact_digest=_validated_digest(
            rollback_artifact_digest, "rollback_artifact_digest"
        ),
    )


def confirm_v4_activation_after_restart(
    receipt: V4ActivationReceipt,
    profile: ConnectionProfile,
    *,
    current_registry_digest: str,
    reinspect: ReinspectProfile,
    enable_v4_activation: bool = False,
) -> V4ActivationReceipt:
    """Reinspect and confirm only when every persisted coordinate still matches.

    Failure returns no replacement receipt.  Because receipts are immutable and
    the caller is not asked to persist anything on failure, the original pending
    receipt continues to advertise exact rollback eligibility.
    """

    _require_enabled(enable_v4_activation)
    _validate_receipt(receipt)
    if receipt.state != "pending":
        raise V4ActivationBindingError("Only a pending v4 activation can be confirmed")
    try:
        inspected = reinspect()
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise V4ActivationBindingError("V4 authority reinspection failed") from error
    current = _coordinates(profile, current_registry_digest, inspected)
    if not _coordinates_match(receipt.coordinates, current):
        raise V4ActivationBindingError(
            "Restart authority does not match the tested v4 activation"
        )
    return replace(receipt, state="confirmed")


def v4_activation_proof_to_document(
    proof: V4ActivationProof,
) -> dict[str, object]:
    _validate_proof(proof)
    return {
        "contract": "workstack.v4-activation-proof",
        "contract_version": proof.contract_version,
        "proof_id": proof.proof_id,
        "coordinates": _coordinates_document(proof.coordinates),
    }


def v4_activation_proof_from_document(value: object) -> V4ActivationProof:
    raw = _strict_document(
        value,
        {"contract", "contract_version", "proof_id", "coordinates"},
        "proof",
    )
    if (
        raw["contract"] != "workstack.v4-activation-proof"
        or raw["contract_version"] != V4_ACTIVATION_CONTRACT_VERSION
    ):
        raise V4ActivationBindingError("V4 activation proof contract is invalid")
    proof = V4ActivationProof(
        contract_version=1,
        proof_id=_canonical_uuid(raw["proof_id"], "proof_id"),
        coordinates=_coordinates_from_document(raw["coordinates"]),
    )
    _validate_proof(proof)
    return proof


def v4_activation_receipt_to_document(
    receipt: V4ActivationReceipt,
) -> dict[str, object]:
    _validate_receipt(receipt)
    return {
        "contract": "workstack.v4-activation-receipt",
        "contract_version": receipt.contract_version,
        "activation_id": receipt.activation_id,
        "state": receipt.state,
        "coordinates": _coordinates_document(receipt.coordinates),
        "proof_digest": receipt.proof_digest,
        "previous_registry_digest": receipt.previous_registry_digest,
        "rollback_artifact_digest": receipt.rollback_artifact_digest,
    }


def v4_activation_receipt_from_document(value: object) -> V4ActivationReceipt:
    raw = _strict_document(
        value,
        {
            "contract",
            "contract_version",
            "activation_id",
            "state",
            "coordinates",
            "proof_digest",
            "previous_registry_digest",
            "rollback_artifact_digest",
        },
        "receipt",
    )
    if (
        raw["contract"] != "workstack.v4-activation-receipt"
        or raw["contract_version"] != V4_ACTIVATION_CONTRACT_VERSION
    ):
        raise V4ActivationBindingError("V4 activation receipt contract is invalid")
    receipt = V4ActivationReceipt(
        contract_version=1,
        activation_id=_canonical_uuid(raw["activation_id"], "activation_id"),
        state=raw["state"],  # type: ignore[arg-type]
        coordinates=_coordinates_from_document(raw["coordinates"]),
        proof_digest=_validated_digest(raw["proof_digest"], "proof_digest"),
        previous_registry_digest=_validated_digest(
            raw["previous_registry_digest"], "previous_registry_digest"
        ),
        rollback_artifact_digest=_validated_digest(
            raw["rollback_artifact_digest"], "rollback_artifact_digest"
        ),
    )
    _validate_receipt(receipt)
    return receipt


def _coordinates(
    profile: ConnectionProfile,
    registry_digest: str,
    result: ProfileTestResult,
) -> V4ActivationCoordinates:
    authority = _validated_v4_result(profile, result)
    return V4ActivationCoordinates(
        profile_id=_canonical_uuid(profile.profile_id, "profile_id"),
        profile_digest=_validated_digest(profile_digest(profile), "profile_digest"),
        registry_digest=_validated_digest(registry_digest, "registry_digest"),
        workspace_uid=_canonical_uuid(result.actual_workspace_id, "workspace_uid"),
        storage_format="v4",
        schema_version=4,
        authority_manifest_digest=_validated_digest(
            authority.authority_manifest_digest, "authority_manifest_digest"
        ),
    )


def _validated_v4_result(
    profile: ConnectionProfile, result: ProfileTestResult
) -> AuthorityInspection:
    authority = result.authority
    if (
        result.status != "ready"
        or result.profile_id != profile.profile_id
        or result.kind != profile.kind
        or result.actual_workspace_id != profile.expected_workspace_id
        or authority is None
        or authority.storage_format != "v4"
        or authority.schema_version != 4
        or not authority.capabilities.read
    ):
        raise V4ActivationBindingError(
            "Profile Test result does not prove the requested v4 authority"
        )
    return authority


def _validate_proof(proof: V4ActivationProof) -> None:
    if proof.contract_version != V4_ACTIVATION_CONTRACT_VERSION:
        raise V4ActivationBindingError("V4 activation proof version is invalid")
    _canonical_uuid(proof.proof_id, "proof_id")
    _validate_coordinates(proof.coordinates)


def _validate_receipt(receipt: V4ActivationReceipt) -> None:
    if (
        receipt.contract_version != V4_ACTIVATION_CONTRACT_VERSION
        or receipt.state not in {"pending", "confirmed", "restored"}
    ):
        raise V4ActivationBindingError("V4 activation receipt is invalid")
    _canonical_uuid(receipt.activation_id, "activation_id")
    _validate_coordinates(receipt.coordinates)
    _validated_digest(receipt.proof_digest, "proof_digest")
    _validated_digest(receipt.previous_registry_digest, "previous_registry_digest")
    _validated_digest(receipt.rollback_artifact_digest, "rollback_artifact_digest")


def _validate_coordinates(value: V4ActivationCoordinates) -> None:
    if value.storage_format != "v4" or value.schema_version != 4:
        raise V4ActivationBindingError("V4 activation storage coordinate is invalid")
    _canonical_uuid(value.profile_id, "profile_id")
    _canonical_uuid(value.workspace_uid, "workspace_uid")
    _validated_digest(value.profile_digest, "profile_digest")
    _validated_digest(value.registry_digest, "registry_digest")
    _validated_digest(value.authority_manifest_digest, "authority_manifest_digest")


def _coordinates_document(value: V4ActivationCoordinates) -> dict[str, object]:
    _validate_coordinates(value)
    return {
        "profile_id": value.profile_id,
        "profile_digest": value.profile_digest,
        "registry_digest": value.registry_digest,
        "workspace_uid": value.workspace_uid,
        "storage_format": value.storage_format,
        "schema_version": value.schema_version,
        "authority_manifest_digest": value.authority_manifest_digest,
    }


def _coordinates_from_document(value: object) -> V4ActivationCoordinates:
    raw = _strict_document(
        value,
        {
            "profile_id",
            "profile_digest",
            "registry_digest",
            "workspace_uid",
            "storage_format",
            "schema_version",
            "authority_manifest_digest",
        },
        "coordinates",
    )
    coordinates = V4ActivationCoordinates(
        profile_id=_canonical_uuid(raw["profile_id"], "profile_id"),
        profile_digest=_validated_digest(raw["profile_digest"], "profile_digest"),
        registry_digest=_validated_digest(raw["registry_digest"], "registry_digest"),
        workspace_uid=_canonical_uuid(raw["workspace_uid"], "workspace_uid"),
        storage_format=raw["storage_format"],  # type: ignore[arg-type]
        schema_version=raw["schema_version"],  # type: ignore[arg-type]
        authority_manifest_digest=_validated_digest(
            raw["authority_manifest_digest"], "authority_manifest_digest"
        ),
    )
    _validate_coordinates(coordinates)
    return coordinates


def _coordinates_match(
    expected: V4ActivationCoordinates, actual: V4ActivationCoordinates
) -> bool:
    left = json.dumps(
        _coordinates_document(expected), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    right = json.dumps(
        _coordinates_document(actual), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return secrets.compare_digest(left, right)


def _document_digest(value: Mapping[str, object]) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _require_enabled(enabled: bool) -> None:
    if enabled is not True:
        raise V4ActivationDisabledError(
            "V4 connection activation is not enabled in this release"
        )


def _strict_document(
    value: object, expected: set[str], description: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise V4ActivationBindingError(
            f"V4 activation {description} has unknown or missing fields"
        )
    return value


def _validated_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise V4ActivationBindingError(f"{field} is invalid")
    return value


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise V4ActivationBindingError(f"{field} is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise V4ActivationBindingError(f"{field} is invalid") from error
    if parsed.int == 0 or str(parsed) != value or parsed.variant != uuid.RFC_4122:
        raise V4ActivationBindingError(f"{field} is invalid")
    return value
