"""Versioned persistence for independent Work Stack connection profiles.

The registry is deliberately independent from the desktop lifecycle.  It owns
only persisted profile configuration: runtime ports, processes, health, event
cursors, credentials, and projected Work Stack content do not belong here.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias


REGISTRY_FILE = "connection-registry.json"
REGISTRY_SCHEMA_VERSION = 1
MAX_REGISTRY_BYTES = 1_048_576
MAX_PROFILES = 128
SSH_HOST_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,254}$")


@dataclass(frozen=True)
class LocalConnectionProfile:
    profile_id: str
    label: str
    data_dir: str
    expected_workspace_id: str
    enabled: bool = True
    live_updates: bool = True
    kind: str = "local"


@dataclass(frozen=True)
class SshConnectionProfile:
    profile_id: str
    label: str
    ssh_host_alias: str
    remote_app_dir: str
    remote_data_dir: str
    expected_workspace_id: str
    preferred_forward_port: int
    remote_port: int = 8765
    enabled: bool = True
    live_updates: bool = True
    kind: str = "ssh"


ConnectionProfile: TypeAlias = LocalConnectionProfile | SshConnectionProfile


@dataclass(frozen=True)
class DuplicateWorkspaceAuthority:
    """Profiles which declare endpoints for the same workspace authority."""

    expected_workspace_id: str
    profile_ids: tuple[str, ...]


@dataclass(frozen=True)
class ConnectionRegistry:
    schema_version: int
    active_profile_id: str | None
    profiles: tuple[ConnectionProfile, ...]

    @property
    def duplicate_authorities(self) -> tuple[DuplicateWorkspaceAuthority, ...]:
        return detect_duplicate_workspace_identities(self)


def _require_exact_fields(
    raw: dict[object, object], required: set[str], context: str
) -> None:
    missing = required - set(raw)
    unexpected = set(raw) - required
    if missing:
        raise RuntimeError(f"{context} is missing: {', '.join(sorted(missing))}")
    if unexpected:
        fields = ", ".join(sorted(str(field) for field in unexpected))
        raise RuntimeError(f"{context} has unsupported fields: {fields}")


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{field} must be a canonical non-nil UUID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise RuntimeError(f"{field} must be a canonical non-nil UUID") from error
    canonical = str(parsed)
    if value != canonical or parsed.int == 0:
        raise RuntimeError(f"{field} must be a canonical non-nil UUID")
    return canonical


def _bounded_string(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise RuntimeError(f"{field} must be a non-empty string of at most {maximum} characters")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise RuntimeError(f"{field} contains an invalid control character")
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise RuntimeError(f"{field} must be a boolean")
    return value


def _port(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65_535:
        raise RuntimeError(f"{field} must be an integer from 1 to 65535")
    return value


def _remote_path(value: object, field: str) -> str:
    path = _bounded_string(value, field, 4096)
    if not path.startswith("/"):
        raise RuntimeError(f"{field} must be an absolute Linux path")
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise RuntimeError(f"{field} must not contain '.' or '..' path segments")
    normalized = path.rstrip("/") or "/"
    if normalized == "/":
        raise RuntimeError(f"{field} must not be the Linux filesystem root")
    return normalized


def _local_path(value: object) -> str:
    path = _bounded_string(value, "data_dir", 4096)
    normalized_separators = path.replace("/", "\\")
    if normalized_separators.startswith("\\\\"):
        raise RuntimeError("data_dir must not use a UNC or Windows device path")
    if not os.path.isabs(path):
        raise RuntimeError("data_dir must be an absolute local path")
    normalized = os.path.normpath(os.path.abspath(path))
    _drive, tail = os.path.splitdrive(normalized)
    if tail in {os.sep, "/", "\\"}:
        raise RuntimeError("data_dir must not be a filesystem or drive root")
    return normalized


def validate_local_data_dir(value: object) -> str:
    """Public form of the registry's local data_dir rules (absolute, non-UNC, non-root)."""

    return _local_path(value)


def _common_profile_values(raw: dict[object, object]) -> dict[str, object]:
    label = _bounded_string(raw["label"], "label", 100).strip()
    if not label:
        raise RuntimeError("label must contain at least one non-whitespace character")
    return {
        "profile_id": _canonical_uuid(raw["profile_id"], "profile_id"),
        "label": label,
        "enabled": _boolean(raw["enabled"], "enabled"),
        "live_updates": _boolean(raw["live_updates"], "live_updates"),
        "expected_workspace_id": _canonical_uuid(
            raw["expected_workspace_id"], "expected_workspace_id"
        ),
    }


def _validate_profile(raw: object, index: int) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise RuntimeError(f"profiles[{index}] must contain one JSON object")
    common = {
        "profile_id",
        "label",
        "kind",
        "enabled",
        "live_updates",
        "expected_workspace_id",
    }
    kind = raw.get("kind")
    if kind == "local":
        _require_exact_fields(raw, common | {"data_dir"}, f"profiles[{index}]")
        return {
            **_common_profile_values(raw),
            "kind": "local",
            "data_dir": _local_path(raw["data_dir"]),
        }
    if kind == "ssh":
        _require_exact_fields(
            raw,
            common
            | {
                "ssh_host_alias",
                "remote_app_dir",
                "remote_data_dir",
                "preferred_forward_port",
                "remote_port",
            },
            f"profiles[{index}]",
        )
        alias = _bounded_string(raw["ssh_host_alias"], "ssh_host_alias", 255)
        if not SSH_HOST_ALIAS_PATTERN.fullmatch(alias):
            raise RuntimeError(
                "ssh_host_alias must be a configured OpenSSH alias without spaces or shell characters"
            )
        return {
            **_common_profile_values(raw),
            "kind": "ssh",
            "ssh_host_alias": alias,
            "remote_app_dir": _remote_path(raw["remote_app_dir"], "remote_app_dir"),
            "remote_data_dir": _remote_path(raw["remote_data_dir"], "remote_data_dir"),
            "preferred_forward_port": _port(
                raw["preferred_forward_port"], "preferred_forward_port"
            ),
            "remote_port": _port(raw["remote_port"], "remote_port"),
        }
    raise RuntimeError(f"profiles[{index}].kind must be 'local' or 'ssh'")


def validate_connection_registry(raw: object) -> dict[str, object]:
    """Return the canonical JSON form or fail closed on any schema deviation."""

    if not isinstance(raw, dict):
        raise RuntimeError("Connection registry must contain one JSON object")
    _require_exact_fields(
        raw,
        {"schema_version", "active_profile_id", "profiles"},
        "Connection registry",
    )
    if raw["schema_version"] != REGISTRY_SCHEMA_VERSION or isinstance(
        raw["schema_version"], bool
    ):
        raise RuntimeError(
            f"schema_version must be exactly {REGISTRY_SCHEMA_VERSION}"
        )
    profiles_raw = raw["profiles"]
    if not isinstance(profiles_raw, list):
        raise RuntimeError("profiles must be an array")
    if len(profiles_raw) > MAX_PROFILES:
        raise RuntimeError(f"profiles must contain at most {MAX_PROFILES} entries")
    profiles = [_validate_profile(profile, index) for index, profile in enumerate(profiles_raw)]
    profile_ids = [str(profile["profile_id"]) for profile in profiles]
    if len(profile_ids) != len(set(profile_ids)):
        raise RuntimeError("Connection registry contains duplicate profile_id values")

    enabled_authorities: dict[str, list[str]] = {}
    for profile in profiles:
        if profile["enabled"]:
            enabled_authorities.setdefault(
                str(profile["expected_workspace_id"]), []
            ).append(str(profile["profile_id"]))
    ambiguous_authorities = {
        workspace_id: ids
        for workspace_id, ids in enabled_authorities.items()
        if len(ids) > 1
    }
    if ambiguous_authorities:
        raise RuntimeError(
            "Connection registry must have at most one enabled profile per "
            "expected_workspace_id"
        )

    active_raw = raw["active_profile_id"]
    active_profile_id = (
        None
        if active_raw is None
        else _canonical_uuid(active_raw, "active_profile_id")
    )
    if active_profile_id is not None and active_profile_id not in profile_ids:
        raise RuntimeError("active_profile_id must identify one registry profile")
    if active_profile_id is not None:
        active_profile = next(
            profile for profile in profiles if profile["profile_id"] == active_profile_id
        )
        if not active_profile["enabled"]:
            raise RuntimeError("active_profile_id must identify an enabled profile")
    if active_profile_id is None and profiles:
        raise RuntimeError("active_profile_id is required when profiles are present")
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "active_profile_id": active_profile_id,
        "profiles": profiles,
    }


def _profile_from_document(raw: dict[str, object]) -> ConnectionProfile:
    common = {
        "profile_id": str(raw["profile_id"]),
        "label": str(raw["label"]),
        "enabled": bool(raw["enabled"]),
        "live_updates": bool(raw["live_updates"]),
        "expected_workspace_id": str(raw["expected_workspace_id"]),
    }
    if raw["kind"] == "local":
        return LocalConnectionProfile(**common, data_dir=str(raw["data_dir"]))
    return SshConnectionProfile(
        **common,
        ssh_host_alias=str(raw["ssh_host_alias"]),
        remote_app_dir=str(raw["remote_app_dir"]),
        remote_data_dir=str(raw["remote_data_dir"]),
        preferred_forward_port=int(raw["preferred_forward_port"]),
        remote_port=int(raw["remote_port"]),
    )


def registry_from_document(raw: object) -> ConnectionRegistry:
    normalized = validate_connection_registry(raw)
    return ConnectionRegistry(
        schema_version=REGISTRY_SCHEMA_VERSION,
        active_profile_id=normalized["active_profile_id"],  # type: ignore[arg-type]
        profiles=tuple(
            _profile_from_document(profile)  # type: ignore[arg-type]
            for profile in normalized["profiles"]  # type: ignore[union-attr]
        ),
    )


def registry_to_document(registry: ConnectionRegistry) -> dict[str, object]:
    profiles: list[dict[str, object]] = []
    for profile in registry.profiles:
        common: dict[str, object] = {
            "profile_id": profile.profile_id,
            "label": profile.label,
            "kind": profile.kind,
            "enabled": profile.enabled,
            "live_updates": profile.live_updates,
            "expected_workspace_id": profile.expected_workspace_id,
        }
        if isinstance(profile, LocalConnectionProfile):
            common["data_dir"] = profile.data_dir
        else:
            common.update(
                {
                    "ssh_host_alias": profile.ssh_host_alias,
                    "remote_app_dir": profile.remote_app_dir,
                    "remote_data_dir": profile.remote_data_dir,
                    "preferred_forward_port": profile.preferred_forward_port,
                    "remote_port": profile.remote_port,
                }
            )
        profiles.append(common)
    return validate_connection_registry(
        {
            "schema_version": registry.schema_version,
            "active_profile_id": registry.active_profile_id,
            "profiles": profiles,
        }
    )


def detect_duplicate_workspace_identities(
    registry: ConnectionRegistry,
) -> tuple[DuplicateWorkspaceAuthority, ...]:
    grouped: dict[str, list[str]] = {}
    for profile in registry.profiles:
        grouped.setdefault(profile.expected_workspace_id, []).append(profile.profile_id)
    return tuple(
        DuplicateWorkspaceAuthority(workspace_id, tuple(profile_ids))
        for workspace_id, profile_ids in sorted(grouped.items())
        if len(profile_ids) > 1
    )


def load_connection_registry(state_root: Path) -> ConnectionRegistry | None:
    path = state_root / REGISTRY_FILE
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_REGISTRY_BYTES:
            raise RuntimeError("Connection registry is too large")
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except RuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Connection registry is invalid JSON: {path}") from error
    try:
        return registry_from_document(raw)
    except RuntimeError as error:
        raise RuntimeError(f"Connection registry is invalid: {error}") from error


def save_connection_registry(
    state_root: Path, registry: ConnectionRegistry | object
) -> ConnectionRegistry:
    normalized = (
        registry_to_document(registry)
        if isinstance(registry, ConnectionRegistry)
        else validate_connection_registry(registry)
    )
    result = registry_from_document(normalized)
    state_root.mkdir(parents=True, exist_ok=True)
    target = state_root / REGISTRY_FILE
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(normalized, ensure_ascii=True, separators=(",", ":")) + "\n"
    if len(payload.encode("utf-8")) > MAX_REGISTRY_BYTES:
        raise RuntimeError("Connection registry is too large")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory_best_effort(state_root)
    except OSError as error:
        raise RuntimeError("Could not save connection registry") from error
    finally:
        temporary.unlink(missing_ok=True)
    return result


def _fsync_directory_best_effort(directory: Path) -> None:
    """Persist the replace metadata where directory fsync is supported.

    Windows does not expose portable directory handles through ``os.open``;
    successful file fsync plus atomic replace remains the supported guarantee.
    """

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def migrate_singleton_draft(
    raw: object,
    *,
    profile_id: str | None = None,
    label: str | None = None,
    local_data_dir: str | None = None,
    local_workspace_id: str | None = None,
) -> ConnectionRegistry:
    """Normalize the legacy singleton draft into a one-profile registry.

    Local singleton drafts carried no path or identity, so both values are
    explicitly required from the installer's current local configuration.
    The function is in-memory only and never reads or mutates planning data.
    """

    if not isinstance(raw, dict):
        raise RuntimeError("Legacy connection draft must contain one JSON object")
    mode = raw.get("storage_mode")
    generated_id = _canonical_uuid(
        str(uuid.uuid4()) if profile_id is None else profile_id, "profile_id"
    )
    if mode == "local":
        _require_exact_fields(raw, {"storage_mode"}, "Legacy local connection draft")
        if local_data_dir is None or local_workspace_id is None:
            raise RuntimeError(
                "Local migration requires local_data_dir and local_workspace_id"
            )
        normalized_local_path = _local_path(local_data_dir)
        profile: ConnectionProfile = LocalConnectionProfile(
            profile_id=generated_id,
            label=Path(normalized_local_path).name if label is None else label,
            data_dir=normalized_local_path,
            expected_workspace_id=_canonical_uuid(
                local_workspace_id, "local_workspace_id"
            ),
        )
    elif mode == "ssh-remote":
        required = {
            "storage_mode",
            "ssh_host_alias",
            "remote_app_dir",
            "remote_data_dir",
            "local_forward_port",
            "workspace_id",
        }
        allowed = required | {"remote_port"}
        missing = required - set(raw)
        unexpected = set(raw) - allowed
        if missing:
            raise RuntimeError(
                f"Legacy SSH connection draft is missing: {', '.join(sorted(missing))}"
            )
        if unexpected:
            fields = ", ".join(sorted(str(field) for field in unexpected))
            raise RuntimeError(
                f"Legacy SSH connection draft has unsupported fields: {fields}"
            )
        alias = _bounded_string(raw["ssh_host_alias"], "ssh_host_alias", 255)
        if not SSH_HOST_ALIAS_PATTERN.fullmatch(alias):
            raise RuntimeError(
                "ssh_host_alias must be a configured OpenSSH alias without spaces or shell characters"
            )
        profile = SshConnectionProfile(
            profile_id=generated_id,
            label=alias if label is None else label,
            ssh_host_alias=alias,
            remote_app_dir=_remote_path(raw["remote_app_dir"], "remote_app_dir"),
            remote_data_dir=_remote_path(raw["remote_data_dir"], "remote_data_dir"),
            expected_workspace_id=_canonical_uuid(raw["workspace_id"], "workspace_id"),
            preferred_forward_port=_port(
                raw["local_forward_port"], "local_forward_port"
            ),
            remote_port=_port(raw.get("remote_port", 8765), "remote_port"),
        )
    else:
        raise RuntimeError("Legacy storage_mode must be 'local' or 'ssh-remote'")
    registry = ConnectionRegistry(REGISTRY_SCHEMA_VERSION, generated_id, (profile,))
    return registry_from_document(registry_to_document(registry))


def singleton_draft_from_registry(registry: ConnectionRegistry) -> dict[str, object]:
    """Return the legacy normalized draft for a one-profile active registry."""

    registry = registry_from_document(registry_to_document(registry))
    if len(registry.profiles) != 1 or registry.active_profile_id is None:
        raise RuntimeError("Legacy conversion requires exactly one active profile")
    profile = registry.profiles[0]
    if profile.profile_id != registry.active_profile_id:
        raise RuntimeError("Legacy conversion requires the sole profile to be active")
    if isinstance(profile, LocalConnectionProfile):
        return {"storage_mode": "local"}
    return {
        "storage_mode": "ssh-remote",
        "ssh_host_alias": profile.ssh_host_alias,
        "remote_app_dir": profile.remote_app_dir,
        "remote_data_dir": profile.remote_data_dir,
        "local_forward_port": profile.preferred_forward_port,
        "workspace_id": profile.expected_workspace_id,
        "remote_port": profile.remote_port,
    }
