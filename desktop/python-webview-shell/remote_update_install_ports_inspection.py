"""Read-only mapping from existing inspect/unpack observations into D outcomes.

Nothing here SSHes, installs, activates, or writes SSOT. Callers pass already
collected facts, receipts, and UI digests. Artifact integrity is the selection
digest pair, never a product version string.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
if _SHELL_DIR not in sys.path:
    sys.path.insert(0, _SHELL_DIR)

from remote_provision_artifact import ArtifactSelection  # noqa: E402
from remote_provision_driver import (  # noqa: E402
    OUTCOME_INSTALLED,
    OUTCOME_REFUSED,
    OUTCOME_UNKNOWN,
    DriverProfile,
    InstallOutcome,
)
from remote_update_flow_contract import (  # noqa: E402
    PrepareOutcome,
    PreviewFacts,
    ProbeOutcome,
    VerifyOutcome,
)
from remote_verified_unpack import (  # noqa: E402
    OUTCOME_NOT_READY,
    OUTCOME_UNPACKED,
    PLACEMENT_ABSENT,
    PLACEMENT_IDENTITY,
    PLACEMENT_INTERRUPTED,
    PLACEMENT_READY,
    PLACEMENT_UNKNOWN,
)


MODE_TRANSACTIONAL = "transactional"
MODE_VERIFIED_UNPACK = "verified_unpack"
INSTALL_MODES = frozenset({MODE_TRANSACTIONAL, MODE_VERIFIED_UNPACK})
PUBLICATIONS = frozenset({"available", "unavailable", "unknown"})
DIGEST_PREFIX = "sha256:"
DIGEST_HEX = frozenset("0123456789abcdef")

UNPACK_READY = frozenset({PLACEMENT_READY, PLACEMENT_IDENTITY})


@dataclass(frozen=True)
class LoopbackSession:
    """Current loopback/session observation supplied by the desktop root."""

    base_url: str | None = None
    workspace_id: str | None = None
    session_generation: str | None = None
    desktop_version: str | None = None


@dataclass(frozen=True)
class OperationRecord:
    """Same-operation identity: fingerprint, whether a mutation left, placement."""

    operation_id: str
    fingerprint: str
    issued: bool
    placement: str
    method: str
    receipt: Mapping[str, object] | None
    prepare: PrepareOutcome | None


def target_profile(current: DriverProfile, target_app_dir: str) -> DriverProfile:
    """Bind the selected connection to the absent candidate directory."""

    return replace(current, remote_app_dir=target_app_dir)


def request_fingerprint(
    operation_id: str,
    mode: str,
    target_app_dir: str,
    artifact: ArtifactSelection,
    workspace_id: str,
) -> str:
    payload = "\n".join(
        (
            operation_id,
            mode,
            target_app_dir,
            artifact.digest,
            artifact.manifest_digest,
            workspace_id,
        )
    ).encode("utf-8")
    return DIGEST_PREFIX + hashlib.sha256(payload).hexdigest()


def mapping_of(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def publication_of(facts: object) -> str:
    block = mapping_of(facts)
    capability = mapping_of(block.get("capability") if block is not None else None)
    if capability is None:
        return "unknown"
    publication = capability.get("publication")
    return publication if publication in PUBLICATIONS else "unknown"


def install_exists(facts: object) -> bool | None:
    block = mapping_of(facts)
    install = mapping_of(block.get("install") if block is not None else None)
    if install is None:
        return None
    exists = install.get("exists")
    return exists if isinstance(exists, bool) else None


def install_digest(facts: object) -> str | None:
    block = mapping_of(facts)
    install = mapping_of(block.get("install") if block is not None else None)
    if install is None:
        return None
    digest = install.get("digest")
    return digest if isinstance(digest, str) and digest.startswith(DIGEST_PREFIX) else None


def data_workspace(facts: object) -> str | None:
    block = mapping_of(facts)
    data = mapping_of(block.get("data") if block is not None else None)
    if data is None:
        return None
    observed = data.get("workspace_id")
    return observed if isinstance(observed, str) and observed else None


def install_product(facts: object) -> str | None:
    block = mapping_of(facts)
    install = mapping_of(block.get("install") if block is not None else None)
    if install is None:
        return None
    product = install.get("product_version")
    return product if isinstance(product, str) and product else None


def install_protocol(facts: object) -> str | None:
    block = mapping_of(facts)
    install = mapping_of(block.get("install") if block is not None else None)
    if install is None:
        return None
    protocol = install.get("protocol_version")
    return str(protocol) if type(protocol) is int and not isinstance(protocol, bool) else None


def project_offer(facts: object, mode: str) -> tuple[str, str]:
    """Project measured publication against the explicitly selected mode.

    Transactional never silently becomes verified_unpack. Verified unpack is
    offered only when the caller selected it; filesystem rename capability
    does not have to be available for that method.
    """

    if mode == MODE_VERIFIED_UNPACK:
        exists = install_exists(facts)
        if exists is True:
            return "unavailable", MODE_VERIFIED_UNPACK
        if exists is False:
            return "available", MODE_VERIFIED_UNPACK
        return "unknown", MODE_VERIFIED_UNPACK
    return publication_of(facts), MODE_TRANSACTIONAL


def served_digest(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith(DIGEST_PREFIX):
        return None
    digest = value[len(DIGEST_PREFIX) :]
    if len(digest) != 64 or any(character not in DIGEST_HEX for character in digest):
        return None
    return value


def unpack_receipt_fields(document: object) -> Mapping[str, object] | None:
    """Project nested inspect/verify receipt or flat place document."""

    block = mapping_of(document)
    if block is None:
        return None
    nested = mapping_of(block.get("receipt"))
    return nested if nested is not None else block


def unpack_identity_matches(document: object, artifact: ArtifactSelection) -> bool:
    fields = unpack_receipt_fields(document)
    if fields is None:
        return False
    return (
        fields.get("artifact_digest") == artifact.digest
        and fields.get("artifact_manifest_sha256") == artifact.manifest_digest
    )


def unpack_runtime_digest(document: object) -> str | None:
    if unpack_placement(document) != PLACEMENT_IDENTITY:
        return None
    fields = unpack_receipt_fields(document)
    if fields is None:
        return None
    digest = fields.get("artifact_digest")
    return digest if isinstance(digest, str) and digest.startswith(DIGEST_PREFIX) else None


def unpack_product(document: object) -> str | None:
    fields = unpack_receipt_fields(document)
    if fields is None:
        return None
    product = fields.get("product_version")
    return product if isinstance(product, str) and product else None


def unpack_protocol(document: object) -> str | None:
    fields = unpack_receipt_fields(document)
    if fields is None:
        return None
    protocol = fields.get("remote_protocol_version")
    return str(protocol) if type(protocol) is int and not isinstance(protocol, bool) else None


def preparation_verified(record: OperationRecord) -> bool:
    prepared = record.prepare
    return prepared is not None and prepared.status == "verified"


@dataclass(frozen=True)
class RetainedPreparation:
    """Durable evidence that this exact candidate was verifiably staged.

    Everything here was written by the process that ran the preparation, and
    every field names the identity that preparation was admitted under.  It is
    not itself proof: it says which receipt to go and re-verify, and the
    re-verification is what admits the read-only stages.
    """

    operation_id: str
    target_app_dir: str
    expected_workspace_id: str
    artifact_digest: str
    artifact_manifest_sha256: str
    method: str = MODE_VERIFIED_UNPACK


def retained_matches_inputs(
    retained: object,
    target_app_dir: str,
    expected_workspace_id: str,
    artifact: ArtifactSelection,
) -> bool:
    """Whether the retained evidence names exactly this port's own selection."""

    if not isinstance(retained, RetainedPreparation):
        return False
    if retained.method != MODE_VERIFIED_UNPACK:
        return False
    return (
        retained.target_app_dir == target_app_dir
        and retained.expected_workspace_id == expected_workspace_id
        and retained.artifact_digest == artifact.digest
        and retained.artifact_manifest_sha256 == artifact.manifest_digest
    )


def unpack_app_dir(document: object) -> str | None:
    """The directory the unpack receipt itself says it is describing."""

    fields = unpack_receipt_fields(document)
    if fields is None:
        return None
    app_dir = fields.get("app_dir")
    if isinstance(app_dir, str) and app_dir:
        return app_dir
    block = mapping_of(document)
    if block is None:
        return None
    outer = block.get("app_dir")
    return outer if isinstance(outer, str) and outer else None


def reverified_preparation(
    document: object, artifact: ArtifactSelection, target_app_dir: str
) -> bool:
    """Whether this actual receipt re-establishes the staged candidate.

    The placement has to be the verify helper's own ``identity_verified`` --
    a merely ``ready_candidate`` directory is not a re-verified identity -- and
    the artifact and manifest digests have to be the admitted ones.

    The directory this answers for is the one the caller put in the verify
    command's own ``--app-dir``, and the caller builds that from the canonical
    target; the frozen helper's verify document does not repeat it.  A document
    that does name a directory is still required to name that same one, so a
    receipt describing anything else can never be read as this candidate.
    """

    if unpack_placement(document) != PLACEMENT_IDENTITY:
        return False
    if not unpack_identity_matches(document, artifact):
        return False
    declared = unpack_app_dir(document)
    return declared is None or declared == target_app_dir


def workspace_match(observed: object, expected: str) -> str:
    if not isinstance(observed, str) or not observed:
        return "unknown"
    return "verified" if observed == expected else "failed"


def preview_facts(
    *,
    desktop_version: str | None,
    remote_version: str | None,
    served_ui_version: str | None,
    protocol_version: str | None,
    capability: str,
    method: str,
) -> PreviewFacts:
    return PreviewFacts(
        desktop_version=desktop_version,
        remote_version=remote_version,
        served_ui_version=served_digest(served_ui_version),
        protocol_version=protocol_version,
        schema_version_before=None,
        schema_version_target=None,
        migration_required=None,
        install_capability=capability if capability in PUBLICATIONS else "unknown",
        install_method=method if method in INSTALL_MODES else "unknown",
    )


def unpack_placement(document: object) -> str:
    block = mapping_of(document)
    if block is None:
        return PLACEMENT_UNKNOWN
    placement = block.get("placement")
    if placement in {
        PLACEMENT_ABSENT,
        PLACEMENT_INTERRUPTED,
        PLACEMENT_UNKNOWN,
        PLACEMENT_READY,
        PLACEMENT_IDENTITY,
    }:
        return str(placement)
    return PLACEMENT_UNKNOWN


def unpack_code(document: object) -> str:
    block = mapping_of(document)
    if block is None:
        return "REMOTE_INSTALL_FAILED"
    code = block.get("code")
    return code if isinstance(code, str) and code else "REMOTE_INSTALL_FAILED"


def prepare_from_install(
    outcome: InstallOutcome,
    capability: str,
    method: str,
    previous_app: bool | None,
    previous_profile: bool | None,
) -> tuple[str, PrepareOutcome, Mapping[str, object] | None]:
    """Map a driver outcome. Kind is ``verified``, ``refused``, ``lost``, or ``failed``."""

    if outcome.outcome == OUTCOME_UNKNOWN:
        prepared = PrepareOutcome(
            status="unknown",
            capability=capability,
            method=method,
            previous_app_retained=previous_app,
            previous_profile_retained=previous_profile,
        )
        return "lost", prepared, None
    if outcome.outcome == OUTCOME_REFUSED:
        prepared = PrepareOutcome(
            status="failed",
            capability=capability,
            method=method,
            previous_app_retained=previous_app,
            previous_profile_retained=previous_profile,
        )
        return "refused", prepared, None
    if outcome.outcome != OUTCOME_INSTALLED or outcome.receipt is None:
        prepared = PrepareOutcome(
            status="unknown",
            capability=capability,
            method=method,
            previous_app_retained=previous_app,
            previous_profile_retained=previous_profile,
        )
        return "lost", prepared, None
    prepared = PrepareOutcome(
        status="verified",
        capability="available",
        method=method,
        previous_app_retained=previous_app,
        previous_profile_retained=previous_profile,
    )
    return "verified", prepared, outcome.receipt


def _unpack_prepare_outcome(
    status: str,
    capability: str,
    previous_app: bool | None,
    previous_profile: bool | None,
) -> PrepareOutcome:
    return PrepareOutcome(
        status=status,
        capability=capability,
        method=MODE_VERIFIED_UNPACK,
        previous_app_retained=previous_app,
        previous_profile_retained=previous_profile,
    )


def prepare_from_unpack(
    document: object,
    artifact: ArtifactSelection,
    previous_app: bool | None,
    previous_profile: bool | None,
) -> tuple[str, PrepareOutcome, Mapping[str, object] | None, str]:
    """Map an unpack JSON document. Kind is verified/refused/failed/lost."""

    placement = unpack_placement(document)
    block = mapping_of(document)
    if block is None:
        prepared = _unpack_prepare_outcome("unknown", "unknown", previous_app, previous_profile)
        return "lost", prepared, None, placement
    outcome = block.get("outcome")
    if (
        outcome == OUTCOME_UNPACKED
        and placement in UNPACK_READY
        and unpack_identity_matches(document, artifact)
    ):
        prepared = _unpack_prepare_outcome("verified", "available", previous_app, previous_profile)
        return "verified", prepared, block, placement
    if outcome == OUTCOME_UNPACKED and placement in UNPACK_READY:
        prepared = _unpack_prepare_outcome("failed", "available", previous_app, previous_profile)
        return "failed", prepared, block, placement
    if outcome == OUTCOME_NOT_READY and placement == PLACEMENT_ABSENT:
        prepared = _unpack_prepare_outcome("failed", "available", previous_app, previous_profile)
        return "refused", prepared, None, placement
    if placement == PLACEMENT_INTERRUPTED:
        prepared = _unpack_prepare_outcome("failed", "available", previous_app, previous_profile)
        return "failed", prepared, block, placement
    prepared = _unpack_prepare_outcome("unknown", "available", previous_app, previous_profile)
    return "lost", prepared, block, placement


def observe_prepare_from_facts(
    facts: object,
    artifact: ArtifactSelection,
    method: str,
    previous_app: bool | None,
    previous_profile: bool | None,
) -> PrepareOutcome:
    exists = install_exists(facts)
    digest = install_digest(facts)
    if exists is True and digest == artifact.digest:
        status = "verified"
        placement_capability = "available"
    elif exists is True:
        status = "failed"
        placement_capability = "available"
    elif exists is False:
        status = "unknown"
        placement_capability = project_offer(facts, method)[0]
    else:
        status = "unknown"
        placement_capability = "unknown"
    return PrepareOutcome(
        status=status,
        capability=placement_capability,
        method=method,
        previous_app_retained=previous_app,
        previous_profile_retained=previous_profile,
    )


def observe_prepare_from_unpack(
    document: object,
    artifact: ArtifactSelection,
    previous_app: bool | None,
    previous_profile: bool | None,
) -> PrepareOutcome:
    placement = unpack_placement(document)
    matched = unpack_identity_matches(document, artifact)
    if placement == PLACEMENT_IDENTITY and matched:
        status = "verified"
    elif placement == PLACEMENT_INTERRUPTED or (placement in UNPACK_READY and not matched):
        status = "failed"
    else:
        status = "unknown"
    capability = "unknown" if placement == PLACEMENT_UNKNOWN else "available"
    return _unpack_prepare_outcome(status, capability, previous_app, previous_profile)


def probe_from_facts(
    facts: object,
    artifact: ArtifactSelection,
    expected_workspace: str,
) -> ProbeOutcome:
    match = workspace_match(data_workspace(facts), expected_workspace)
    exists = install_exists(facts)
    digest = install_digest(facts)
    if exists is True and digest == artifact.digest:
        status = "verified"
    elif exists is False:
        status = "failed"
    elif exists is True and digest is not None:
        status = "failed"
    else:
        status = "unknown"
    return ProbeOutcome(
        status=status,
        workspace_match=match,
        served_ui_version=None,
        protocol_version=install_protocol(facts),
    )


def probe_from_unpack(
    document: object,
    facts: object,
    artifact: ArtifactSelection,
    expected_workspace: str,
) -> ProbeOutcome:
    match = workspace_match(data_workspace(facts), expected_workspace)
    protocol = unpack_protocol(document) or install_protocol(facts)
    placement = unpack_placement(document)
    matched = unpack_identity_matches(document, artifact)
    if placement == PLACEMENT_IDENTITY and matched:
        status = "verified"
    elif placement in {PLACEMENT_ABSENT, PLACEMENT_INTERRUPTED}:
        status = "failed"
    elif placement in UNPACK_READY and not matched:
        status = "failed"
    else:
        status = "unknown"
    return ProbeOutcome(
        status=status,
        workspace_match=match,
        served_ui_version=None,
        protocol_version=protocol,
    )


def verify_outcome(
    *,
    runtime_digest: str | None,
    artifact: ArtifactSelection,
    workspace: str,
    expected_workspace: str,
    served_ui: str | None,
    desktop_version: str | None,
    remote_version: str | None,
    protocol_version: str | None,
    expected_served_ui: str | None = None,
) -> VerifyOutcome:
    """Compare runtime digest, admitted served HTML digest, and workspace."""

    served = served_digest(served_ui)
    expected_served = served_digest(expected_served_ui)
    match = workspace_match(workspace, expected_workspace)
    runtime_ok = None if runtime_digest is None else runtime_digest == artifact.digest
    if expected_served is None or served is None:
        served_ok: bool | None = None
    else:
        served_ok = served == expected_served
    if runtime_ok is False or match == "failed" or served_ok is False:
        status = "failed"
    elif runtime_ok is True and match == "verified" and served_ok is True:
        status = "verified"
    else:
        status = "unknown"
    return VerifyOutcome(
        status=status,
        desktop_version=desktop_version,
        remote_version=remote_version,
        served_ui_version=served,
        protocol_version=protocol_version,
        schema_version=None,
    )


__all__ = [
    "DIGEST_PREFIX",
    "INSTALL_MODES",
    "LoopbackSession",
    "MODE_TRANSACTIONAL",
    "MODE_VERIFIED_UNPACK",
    "OperationRecord",
    "data_workspace",
    "install_digest",
    "install_exists",
    "install_product",
    "install_protocol",
    "observe_prepare_from_facts",
    "observe_prepare_from_unpack",
    "RetainedPreparation",
    "preparation_verified",
    "preview_facts",
    "probe_from_facts",
    "probe_from_unpack",
    "project_offer",
    "publication_of",
    "request_fingerprint",
    "retained_matches_inputs",
    "reverified_preparation",
    "served_digest",
    "target_profile",
    "unpack_app_dir",
    "unpack_code",
    "unpack_identity_matches",
    "unpack_placement",
    "unpack_product",
    "unpack_protocol",
    "unpack_receipt_fields",
    "unpack_runtime_digest",
    "verify_outcome",
    "workspace_match",
    "prepare_from_install",
    "prepare_from_unpack",
]
