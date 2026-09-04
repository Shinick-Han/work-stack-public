"""One-way legacy compatibility and CAS-bound remote rebind helpers.

The connection registry is the only input authority in this module.  The
legacy connection file is a generated downgrade mirror: it is overwritten
from the active registry profile and is never loaded or merged back.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from connection_registry import (
    ConnectionProfile,
    ConnectionRegistry,
    LocalConnectionProfile,
    SshConnectionProfile,
    load_connection_registry,
    registry_from_document,
    registry_to_document,
    save_connection_registry,
)
from connection_registry_mutations import connection_registry_mutation_lock
from ssot_connection import validate_connection_draft


LEGACY_MIRROR_FILE = "remote-connection.json"
LEGACY_MIRROR_RECEIPT_FILE = "remote-connection.generated-receipt.json"
LEGACY_MIRROR_RECEIPT_VERSION = 1
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class LegacyMirrorExport:
    profile_id: str
    registry_digest: str
    mirror_digest: str
    path: Path


@dataclass(frozen=True)
class RemoteRebindResult:
    registry: ConnectionRegistry
    registry_digest: str
    previous_workspace_id: str
    current_workspace_id: str


@dataclass(frozen=True)
class LocalRebindResult:
    registry: ConnectionRegistry
    registry_digest: str
    previous_workspace_id: str
    current_workspace_id: str
    data_dir: str


def _canonical_payload(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def connection_registry_digest(registry: ConnectionRegistry | object) -> str:
    normalized = (
        registry_to_document(registry)
        if isinstance(registry, ConnectionRegistry)
        else registry_to_document(registry_from_document(registry))
    )
    return _sha256(_canonical_payload(normalized))


def _require_digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
        raise RuntimeError("expected registry digest must be a sha256 digest")
    return value


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{field} must be a canonical non-nil UUID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise RuntimeError(f"{field} must be a canonical non-nil UUID") from error
    if str(parsed) != value or parsed.int == 0:
        raise RuntimeError(f"{field} must be a canonical non-nil UUID")
    return value


def _load_registry_cas(
    state_root: Path, expected_registry_digest: object
) -> ConnectionRegistry:
    expected = _require_digest(expected_registry_digest)
    registry = load_connection_registry(state_root)
    if registry is None:
        raise RuntimeError("Connection registry is not configured")
    if connection_registry_digest(registry) != expected:
        raise RuntimeError("Refusing stale registry digest; reload and retry")
    return registry


def _active_profile(registry: ConnectionRegistry) -> ConnectionProfile:
    active_id = registry.active_profile_id
    if active_id is None:
        raise RuntimeError("Connection registry has no active profile")
    matches = [profile for profile in registry.profiles if profile.profile_id == active_id]
    if len(matches) != 1 or not matches[0].enabled:
        raise RuntimeError("Connection registry active profile is missing or disabled")
    return matches[0]


def _legacy_draft(profile: ConnectionProfile) -> dict[str, object]:
    if isinstance(profile, LocalConnectionProfile):
        raw: dict[str, object] = {"storage_mode": "local"}
    else:
        raw = {
            "storage_mode": "ssh-remote",
            "ssh_host_alias": profile.ssh_host_alias,
            "remote_app_dir": profile.remote_app_dir,
            "remote_data_dir": profile.remote_data_dir,
            "local_forward_port": profile.preferred_forward_port,
            "workspace_id": profile.expected_workspace_id,
            "remote_port": profile.remote_port,
        }
    return validate_connection_draft(raw)


def _atomic_replace(path: Path, payload: bytes, description: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise RuntimeError(f"Could not save {description}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _write_generated_mirror(
    state_root: Path, registry: ConnectionRegistry, profile: ConnectionProfile
) -> LegacyMirrorExport:
    registry_digest = connection_registry_digest(registry)
    mirror_payload = _canonical_payload(_legacy_draft(profile))
    mirror_digest = _sha256(mirror_payload)
    mirror_path = state_root / LEGACY_MIRROR_FILE
    _atomic_replace(mirror_path, mirror_payload, "generated legacy mirror")
    receipt = {
        "schema_version": LEGACY_MIRROR_RECEIPT_VERSION,
        "authority": "connection-registry",
        "profile_id": profile.profile_id,
        "registry_sha256": registry_digest,
        "mirror_sha256": mirror_digest,
    }
    _atomic_replace(
        state_root / LEGACY_MIRROR_RECEIPT_FILE,
        _canonical_payload(receipt),
        "generated legacy mirror receipt",
    )
    return LegacyMirrorExport(
        profile_id=profile.profile_id,
        registry_digest=registry_digest,
        mirror_digest=mirror_digest,
        path=mirror_path,
    )


def export_active_legacy_mirror(
    state_root: Path,
    *,
    expected_registry_digest: str,
) -> LegacyMirrorExport:
    """Generate the downgrade mirror solely from the current active profile."""

    state_root = Path(state_root)
    with connection_registry_mutation_lock(state_root):
        registry = _load_registry_cas(state_root, expected_registry_digest)
        return _write_generated_mirror(state_root, registry, _active_profile(registry))


def _confirmed_rebind_ids(
    *,
    expected_profile_id: object,
    expected_previous_workspace_id: object,
    observed_workspace_id: object,
    confirmation_workspace_id: object,
) -> tuple[str, str, str]:
    profile_id = _canonical_uuid(expected_profile_id, "expected_profile_id")
    previous = _canonical_uuid(
        expected_previous_workspace_id, "expected_previous_workspace_id"
    )
    observed = _canonical_uuid(observed_workspace_id, "observed_workspace_id")
    confirmation = _canonical_uuid(
        confirmation_workspace_id, "confirmation_workspace_id"
    )
    if confirmation != observed:
        raise RuntimeError("Remote rebind confirmation does not match observed workspace")
    if observed == previous:
        raise RuntimeError("Remote rebind requires a changed workspace identity")
    return profile_id, previous, observed


def _registry_with_rebound_active(
    registry: ConnectionRegistry,
    *,
    expected_profile_id: str,
    expected_previous_workspace_id: str,
    observed_workspace_id: str,
) -> ConnectionRegistry:
    active = _active_profile(registry)
    if active.profile_id != expected_profile_id:
        raise RuntimeError("Confirmed rebind profile is not the active profile")
    if not isinstance(active, SshConnectionProfile):
        raise RuntimeError("Only an active SSH profile can be remotely rebound")
    if active.expected_workspace_id != expected_previous_workspace_id:
        raise RuntimeError("Active profile workspace changed before rebind")
    rebound = replace(active, expected_workspace_id=observed_workspace_id)
    profiles = tuple(
        rebound if profile.profile_id == active.profile_id else profile
        for profile in registry.profiles
    )
    return registry_from_document(
        registry_to_document(
            ConnectionRegistry(
                schema_version=registry.schema_version,
                active_profile_id=registry.active_profile_id,
                profiles=profiles,
            )
        )
    )


def rebind_active_remote_workspace(
    state_root: Path,
    *,
    expected_registry_digest: str,
    expected_profile_id: str,
    expected_previous_workspace_id: str,
    observed_workspace_id: str,
    confirmation_workspace_id: str,
) -> RemoteRebindResult:
    """CAS-update only the confirmed active SSH profile authority metadata."""

    profile_id, previous, observed = _confirmed_rebind_ids(
        expected_profile_id=expected_profile_id,
        expected_previous_workspace_id=expected_previous_workspace_id,
        observed_workspace_id=observed_workspace_id,
        confirmation_workspace_id=confirmation_workspace_id,
    )
    state_root = Path(state_root)
    with connection_registry_mutation_lock(state_root):
        current = _load_registry_cas(state_root, expected_registry_digest)
        candidate = _registry_with_rebound_active(
            current,
            expected_profile_id=profile_id,
            expected_previous_workspace_id=previous,
            observed_workspace_id=observed,
        )
        saved = save_connection_registry(state_root, candidate)
        return RemoteRebindResult(
            registry=saved,
            registry_digest=connection_registry_digest(saved),
            previous_workspace_id=previous,
            current_workspace_id=observed,
        )


def _registry_with_rebound_active_local(
    registry: ConnectionRegistry,
    *,
    expected_profile_id: str,
    expected_previous_workspace_id: str,
    expected_data_dir: str,
    observed_workspace_id: str,
) -> tuple[ConnectionRegistry, str]:
    """Change only ``expected_workspace_id`` on the confirmed active local profile.

    The active profile must still be the same enabled LOCAL profile, still
    carry the previous identity, and still point at the same directory. Any of
    those having moved means a newer selection or metadata edit happened after
    the user confirmed, and the caller must refuse rather than overwrite it.
    """

    active = _active_profile(registry)
    if active.profile_id != expected_profile_id:
        raise RuntimeError("Confirmed rebind profile is not the active profile")
    if not isinstance(active, LocalConnectionProfile):
        raise RuntimeError("Only an active local profile can be locally rebound")
    if active.expected_workspace_id != expected_previous_workspace_id:
        raise RuntimeError("Active profile workspace changed before rebind")
    if Path(active.data_dir).resolve() != Path(expected_data_dir).resolve():
        raise RuntimeError("Active profile data directory changed before rebind")
    rebound = replace(active, expected_workspace_id=observed_workspace_id)
    profiles = tuple(
        rebound if profile.profile_id == active.profile_id else profile
        for profile in registry.profiles
    )
    normalized = registry_from_document(
        registry_to_document(
            ConnectionRegistry(
                schema_version=registry.schema_version,
                active_profile_id=registry.active_profile_id,
                profiles=profiles,
            )
        )
    )
    return normalized, active.data_dir


def rebind_active_local_workspace(
    state_root: Path,
    *,
    expected_registry_digest: str,
    expected_profile_id: str,
    expected_previous_workspace_id: str,
    expected_data_dir: str,
    observed_workspace_id: str,
    confirmation_workspace_id: str,
) -> LocalRebindResult:
    """CAS-update only the confirmed active LOCAL profile authority metadata.

    The local counterpart of :func:`rebind_active_remote_workspace`, using the
    same mutation lock and the same registry-digest compare-and-swap. The caller
    is responsible for having independently re-read the Store's own confirmation
    evidence first; this function performs no adoption of its own.
    """

    profile_id, previous, observed = _confirmed_rebind_ids(
        expected_profile_id=expected_profile_id,
        expected_previous_workspace_id=expected_previous_workspace_id,
        observed_workspace_id=observed_workspace_id,
        confirmation_workspace_id=confirmation_workspace_id,
    )
    state_root = Path(state_root)
    with connection_registry_mutation_lock(state_root):
        current = _load_registry_cas(state_root, expected_registry_digest)
        candidate, data_dir = _registry_with_rebound_active_local(
            current,
            expected_profile_id=profile_id,
            expected_previous_workspace_id=previous,
            expected_data_dir=expected_data_dir,
            observed_workspace_id=observed,
        )
        saved = save_connection_registry(state_root, candidate)
        return LocalRebindResult(
            registry=saved,
            registry_digest=connection_registry_digest(saved),
            previous_workspace_id=previous,
            current_workspace_id=observed,
            data_dir=data_dir,
        )
