"""Fail-closed legacy migration and registry-driven startup selection.

This module deliberately stops at configuration selection.  It never starts a
server, opens an SSH process, or constructs a Store.  Identity readers are
read-only capabilities supplied by the caller; the local default only reads
``workspace.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeAlias

from connection_registry import (
    REGISTRY_FILE,
    ConnectionProfile,
    ConnectionRegistry,
    LocalConnectionProfile,
    SshConnectionProfile,
    load_connection_registry,
    migrate_singleton_draft,
    registry_to_document,
    save_connection_registry,
)


LEGACY_CONNECTION_FILE = "remote-connection.json"
LEGACY_BACKUP_FILE = "remote-connection.pre-registry-v1.backup"
LEGACY_ABSENT_MARKER = "remote-connection.pre-registry-v1.absent"
MIGRATION_RECEIPT_FILE = "connection-registry-migration-receipt.json"
MIGRATION_INTENT_FILE = "connection-registry-migration-intent.json"
MIGRATION_LOCK_FILE = "connection-registry-migration.lock"
MIGRATION_RECEIPT_VERSION = 1
MIGRATION_INTENT_VERSION = 1
MAX_LEGACY_BYTES = 1_048_576
MAX_RECEIPT_BYTES = 16_384
MAX_INTENT_BYTES = 16_384
MAX_WORKSPACE_BYTES = 1_048_576
PROFILE_ID_NAMESPACE = uuid.UUID("1dc0c4ae-28f6-45a5-9d6b-28030b80f639")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
MINIMUM_LOCAL_STORE_FILES = frozenset(
    {
        "workspace.json",
        "backlog.json",
        "store-meta.json",
        "okr.json",
        "worklog.json",
        "notes.json",
        "captures.json",
        "replies.json",
        "activity.json",
    }
)


class MigrationInterrupted(RuntimeError):
    """Testable interruption boundary; persisted files remain recoverable."""


@dataclass(frozen=True)
class LocalStartupSelection:
    profile_id: str
    label: str
    expected_workspace_id: str
    data_dir: Path
    backup_dir: Path
    live_updates: bool
    kind: str = "local"


@dataclass(frozen=True)
class SshStartupSelection:
    profile_id: str
    label: str
    expected_workspace_id: str
    ssh_host_alias: str
    remote_app_dir: str
    remote_data_dir: str
    preferred_forward_port: int
    remote_port: int
    live_updates: bool
    kind: str = "ssh"


StartupSelection: TypeAlias = LocalStartupSelection | SshStartupSelection
IdentityReader: TypeAlias = Callable[[ConnectionProfile], str]
InterruptionHook: TypeAlias = Callable[[str], None]


@dataclass(frozen=True)
class _LegacySnapshot:
    existed: bool
    payload: bytes


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


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


def _stable_profile_id(installation_identity: str, workspace_id: str) -> str:
    if (
        not isinstance(installation_identity, str)
        or not installation_identity.strip()
        or len(installation_identity) > 4096
        or any(character in installation_identity for character in ("\x00", "\r", "\n"))
    ):
        raise RuntimeError("installation_identity must be a bounded non-empty string")
    workspace_id = _canonical_uuid(workspace_id, "workspace_id")
    return str(
        uuid.uuid5(
            PROFILE_ID_NAMESPACE,
            installation_identity.strip().casefold() + "\x00" + workspace_id,
        )
    )


def _read_bounded_bytes(path: Path, maximum: int, description: str) -> bytes:
    try:
        with path.open("rb") as stream:
            payload = stream.read(maximum + 1)
        if len(payload) > maximum:
            raise RuntimeError(f"{description} is too large")
        return payload
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(f"Could not read {description}: {path}") from error


def read_local_workspace_identity(profile: ConnectionProfile) -> str:
    """Read a local profile's workspace identity without initializing a Store."""

    if not isinstance(profile, LocalConnectionProfile):
        raise RuntimeError("Local identity reader requires a local profile")
    data_dir = Path(profile.data_dir)
    if not data_dir.is_dir():
        raise RuntimeError("Local SSOT data_dir does not exist or is not a directory")
    if any(_is_link_like(component) for component in _path_and_ancestors(data_dir)):
        raise RuntimeError("Local SSOT data_dir must not be a link or junction")
    for name in MINIMUM_LOCAL_STORE_FILES:
        required = data_dir / name
        if not required.is_file():
            raise RuntimeError(f"Local SSOT is missing required Store file: {name}")
        if _is_link_like(required):
            raise RuntimeError("Local Store files must not be links or junctions")
    workspace_path = data_dir / "workspace.json"
    payload = _read_bounded_bytes(workspace_path, MAX_WORKSPACE_BYTES, "workspace identity")
    try:
        document = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Local workspace identity is invalid JSON") from error
    if not isinstance(document, dict):
        raise RuntimeError("Local workspace identity must contain one JSON object")
    return _canonical_uuid(document.get("id"), "workspace identity")


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


def _path_and_ancestors(path: Path):
    current = path.absolute()
    while True:
        yield current
        parent = current.parent
        if parent == current:
            return
        current = parent


def _read_identity(profile: ConnectionProfile, identity_reader: IdentityReader) -> str:
    try:
        actual = identity_reader(profile)
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError("Workspace identity verification failed") from error
    return _canonical_uuid(actual, "actual workspace identity")


def _verify_identity(profile: ConnectionProfile, identity_reader: IdentityReader) -> str:
    actual = _read_identity(profile, identity_reader)
    if actual != profile.expected_workspace_id:
        raise RuntimeError(
            "Workspace identity mismatch: configured authority does not match the inspected SSOT"
        )
    return actual


def _identity_reader_for(
    profile: ConnectionProfile,
    remote_identity_reader: IdentityReader | None,
    local_identity_reader: IdentityReader,
) -> IdentityReader:
    if isinstance(profile, LocalConnectionProfile):
        return local_identity_reader
    if remote_identity_reader is None:
        raise RuntimeError("Remote workspace identity verification is required")
    return remote_identity_reader


def _parse_legacy(snapshot: _LegacySnapshot) -> object:
    if not snapshot.existed:
        return {"storage_mode": "local"}
    try:
        return json.loads(snapshot.payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Legacy connection profile is invalid JSON") from error


def _capture_legacy(state_root: Path) -> _LegacySnapshot:
    path = state_root / LEGACY_CONNECTION_FILE
    if not path.exists():
        return _LegacySnapshot(False, b"")
    if not path.is_file():
        raise RuntimeError("Legacy connection profile is not a regular file")
    return _LegacySnapshot(
        True, _read_bounded_bytes(path, MAX_LEGACY_BYTES, "legacy connection profile")
    )


def _atomic_write_new(path: Path, payload: bytes, description: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise RuntimeError(f"{description} already exists") from error
        _fsync_directory_best_effort(path.parent)
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(f"Could not save {description}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _validated_lock_timing(timeout: float, poll_interval: float) -> tuple[float, float]:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 <= timeout <= 30
    ):
        raise RuntimeError("migration lock timeout must be between 0 and 30 seconds")
    if (
        isinstance(poll_interval, bool)
        or not isinstance(poll_interval, (int, float))
        or not 0 < poll_interval <= 1
    ):
        raise RuntimeError("migration lock poll interval must be between 0 and 1 second")
    return float(timeout), float(poll_interval)


def _prepare_lock_file(stream) -> None:
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
        os.fsync(stream.fileno())
    stream.seek(0)


def _try_acquire_os_lock(stream) -> bool:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_os_lock(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _migration_lock(state_root: Path, timeout: float, poll_interval: float):
    timeout, poll_interval = _validated_lock_timing(timeout, poll_interval)
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / MIGRATION_LOCK_FILE
    deadline = time.monotonic() + timeout
    try:
        stream = path.open("a+b")
        _prepare_lock_file(stream)
    except OSError as error:
        raise RuntimeError("Could not open connection registry migration lock") from error
    acquired = False
    try:
        while not acquired:
            acquired = _try_acquire_os_lock(stream)
            if acquired:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "Connection registry migration is already in progress"
                )
            time.sleep(min(poll_interval, remaining))
        yield
    finally:
        try:
            if acquired:
                _release_os_lock(stream)
        finally:
            stream.close()


def _fsync_directory_best_effort(directory: Path) -> None:
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


def _load_backup(state_root: Path) -> _LegacySnapshot | None:
    backup = state_root / LEGACY_BACKUP_FILE
    absent = state_root / LEGACY_ABSENT_MARKER
    if backup.exists() and absent.exists():
        raise RuntimeError("Legacy migration backup state is ambiguous")
    if backup.exists():
        if not backup.is_file():
            raise RuntimeError("Legacy migration backup is invalid")
        return _LegacySnapshot(
            True, _read_bounded_bytes(backup, MAX_LEGACY_BYTES, "legacy migration backup")
        )
    if absent.exists():
        if not absent.is_file() or absent.stat().st_size != 0:
            raise RuntimeError("Legacy absence marker is invalid")
        return _LegacySnapshot(False, b"")
    return None


def _write_backup(state_root: Path, snapshot: _LegacySnapshot) -> None:
    backup = state_root / LEGACY_BACKUP_FILE
    absent = state_root / LEGACY_ABSENT_MARKER
    target = backup if snapshot.existed else absent
    _atomic_write_new(target, snapshot.payload, "legacy migration backup")


def _provisional_registry(
    legacy: object,
    *,
    local_data_dir: str | None,
    local_identity_reader: IdentityReader,
) -> ConnectionRegistry:
    provisional = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    if isinstance(legacy, dict) and legacy.get("storage_mode") == "local":
        if local_data_dir is None:
            raise RuntimeError("Local legacy migration requires local_data_dir")
        candidate = LocalConnectionProfile(
            profile_id=provisional,
            label=Path(local_data_dir).name,
            data_dir=local_data_dir,
            expected_workspace_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        )
        provisional_workspace = _read_identity(candidate, local_identity_reader)
        return migrate_singleton_draft(
            legacy,
            profile_id=provisional,
            local_data_dir=local_data_dir,
            local_workspace_id=provisional_workspace,
        )
    return migrate_singleton_draft(legacy, profile_id=provisional)


def _expected_migrated_registry(
    snapshot: _LegacySnapshot,
    *,
    installation_identity: str,
    local_data_dir: str | None,
    remote_identity_reader: IdentityReader | None,
    local_identity_reader: IdentityReader,
) -> ConnectionRegistry:
    legacy = _parse_legacy(snapshot)
    provisional = _provisional_registry(
        legacy,
        local_data_dir=local_data_dir,
        local_identity_reader=local_identity_reader,
    )
    provisional_profile = provisional.profiles[0]
    reader = _identity_reader_for(
        provisional_profile, remote_identity_reader, local_identity_reader
    )
    actual = _verify_identity(provisional_profile, reader)
    profile_id = _stable_profile_id(installation_identity, actual)
    if isinstance(provisional_profile, LocalConnectionProfile):
        return migrate_singleton_draft(
            legacy,
            profile_id=profile_id,
            local_data_dir=provisional_profile.data_dir,
            local_workspace_id=actual,
        )
    return migrate_singleton_draft(legacy, profile_id=profile_id)


def _canonical_registry_payload(registry: ConnectionRegistry) -> bytes:
    document = registry_to_document(registry)
    return (
        json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _local_path_digest(profile: ConnectionProfile) -> str | None:
    if not isinstance(profile, LocalConnectionProfile):
        return None
    normalized = os.path.normcase(os.path.normpath(profile.data_dir))
    return _sha256(normalized.encode("utf-8"))


def _intent_document(
    snapshot: _LegacySnapshot, registry: ConnectionRegistry
) -> dict[str, object]:
    profile = registry.profiles[0]
    return {
        "schema_version": MIGRATION_INTENT_VERSION,
        "legacy_existed": snapshot.existed,
        "legacy_sha256": _sha256(snapshot.payload),
        "profile_id": profile.profile_id,
        "workspace_id": profile.expected_workspace_id,
        "profile_kind": profile.kind,
        "local_data_dir_sha256": _local_path_digest(profile),
    }


def _validate_intent(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise RuntimeError("Migration intent must contain one JSON object")
    expected = {
        "schema_version",
        "legacy_existed",
        "legacy_sha256",
        "profile_id",
        "workspace_id",
        "profile_kind",
        "local_data_dir_sha256",
    }
    if set(raw) != expected:
        raise RuntimeError("Migration intent has unknown or missing fields")
    if raw["schema_version"] != MIGRATION_INTENT_VERSION or isinstance(
        raw["schema_version"], bool
    ):
        raise RuntimeError("Migration intent schema version is invalid")
    if type(raw["legacy_existed"]) is not bool:
        raise RuntimeError("Migration intent legacy_existed must be boolean")
    if not isinstance(raw["legacy_sha256"], str) or not _DIGEST_PATTERN.fullmatch(
        raw["legacy_sha256"]
    ):
        raise RuntimeError("Migration intent legacy_sha256 is invalid")
    _canonical_uuid(raw["profile_id"], "Migration intent profile_id")
    _canonical_uuid(raw["workspace_id"], "Migration intent workspace_id")
    kind = raw["profile_kind"]
    path_digest = raw["local_data_dir_sha256"]
    if kind == "local":
        if not isinstance(path_digest, str) or not _DIGEST_PATTERN.fullmatch(path_digest):
            raise RuntimeError("Migration intent local path binding is invalid")
    elif kind == "ssh":
        if path_digest is not None:
            raise RuntimeError("SSH migration intent must not contain a local path binding")
    else:
        raise RuntimeError("Migration intent profile_kind is invalid")
    return dict(raw)


def _canonical_document_payload(document: dict[str, object]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _load_intent(state_root: Path) -> dict[str, object] | None:
    path = state_root / MIGRATION_INTENT_FILE
    if not path.exists():
        return None
    if not path.is_file():
        raise RuntimeError("Migration intent is not a regular file")
    payload = _read_bounded_bytes(path, MAX_INTENT_BYTES, "migration intent")
    try:
        raw = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Migration intent is invalid JSON") from error
    return _validate_intent(raw)


def _write_intent(state_root: Path, document: dict[str, object]) -> None:
    _atomic_write_new(
        state_root / MIGRATION_INTENT_FILE,
        _canonical_document_payload(_validate_intent(document)),
        "migration intent",
    )


def _require_matching_intent(
    actual: dict[str, object], expected: dict[str, object]
) -> None:
    if actual != expected:
        raise RuntimeError(
            "Current migration input does not match the authority bound before interruption"
        )


def _receipt_document(
    snapshot: _LegacySnapshot,
    registry: ConnectionRegistry,
    intent: dict[str, object],
) -> dict[str, object]:
    profile = registry.profiles[0]
    return {
        "schema_version": MIGRATION_RECEIPT_VERSION,
        "legacy_existed": snapshot.existed,
        "legacy_sha256": _sha256(snapshot.payload),
        "registry_sha256": _sha256(_canonical_registry_payload(registry)),
        "profile_id": profile.profile_id,
        "workspace_id": profile.expected_workspace_id,
        "profile_kind": profile.kind,
        "intent_sha256": _sha256(_canonical_document_payload(intent)),
    }


def _validate_receipt(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise RuntimeError("Migration receipt must contain one JSON object")
    expected = {
        "schema_version",
        "legacy_existed",
        "legacy_sha256",
        "registry_sha256",
        "profile_id",
        "workspace_id",
        "profile_kind",
        "intent_sha256",
    }
    if set(raw) != expected:
        raise RuntimeError("Migration receipt has unknown or missing fields")
    if raw["schema_version"] != MIGRATION_RECEIPT_VERSION or isinstance(
        raw["schema_version"], bool
    ):
        raise RuntimeError("Migration receipt schema version is invalid")
    if type(raw["legacy_existed"]) is not bool:
        raise RuntimeError("Migration receipt legacy_existed must be boolean")
    for field in ("legacy_sha256", "registry_sha256", "intent_sha256"):
        if not isinstance(raw[field], str) or not _DIGEST_PATTERN.fullmatch(raw[field]):
            raise RuntimeError(f"Migration receipt {field} is invalid")
    _canonical_uuid(raw["profile_id"], "Migration receipt profile_id")
    _canonical_uuid(raw["workspace_id"], "Migration receipt workspace_id")
    if raw["profile_kind"] not in {"local", "ssh"}:
        raise RuntimeError("Migration receipt profile_kind is invalid")
    return dict(raw)


def _load_receipt(state_root: Path) -> dict[str, object] | None:
    path = state_root / MIGRATION_RECEIPT_FILE
    if not path.exists():
        return None
    if not path.is_file():
        raise RuntimeError("Migration receipt is not a regular file")
    payload = _read_bounded_bytes(path, MAX_RECEIPT_BYTES, "migration receipt")
    try:
        raw = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Migration receipt is invalid JSON") from error
    return _validate_receipt(raw)


def _backup_snapshot_for_receipt(
    state_root: Path, receipt: dict[str, object]
) -> _LegacySnapshot:
    backup = state_root / LEGACY_BACKUP_FILE
    absent = state_root / LEGACY_ABSENT_MARKER
    if receipt["legacy_existed"]:
        if absent.exists() or not backup.is_file():
            raise RuntimeError("Migration receipt legacy backup is missing or inconsistent")
        snapshot = _LegacySnapshot(
            True, _read_bounded_bytes(backup, MAX_LEGACY_BYTES, "legacy migration backup")
        )
    else:
        if backup.exists() or not absent.is_file() or absent.stat().st_size != 0:
            raise RuntimeError("Migration receipt legacy absence marker is inconsistent")
        snapshot = _LegacySnapshot(False, b"")
    if _sha256(snapshot.payload) != receipt["legacy_sha256"]:
        raise RuntimeError("Migration legacy backup digest does not match its receipt")
    return snapshot


def _registry_for_receipt(state_root: Path) -> ConnectionRegistry:
    registry = load_connection_registry(state_root)
    if registry is None:
        raise RuntimeError("Migration receipt exists but connection registry is missing")
    return registry


def _verify_receipt_history(
    receipt: dict[str, object],
    intent: dict[str, object],
    installation_identity: str,
) -> None:
    if receipt["intent_sha256"] != _sha256(_canonical_document_payload(intent)):
        raise RuntimeError("Migration intent digest does not match migration receipt")
    for field in (
        "legacy_existed",
        "legacy_sha256",
        "profile_id",
        "workspace_id",
        "profile_kind",
    ):
        if receipt[field] != intent[field]:
            raise RuntimeError("Migration receipt does not match its authority intent")
    expected_profile_id = _stable_profile_id(
        installation_identity, str(receipt["workspace_id"])
    )
    if receipt["profile_id"] != expected_profile_id:
        raise RuntimeError("Migration receipt does not belong to this installation")


def _write_receipt(
    state_root: Path,
    snapshot: _LegacySnapshot,
    registry: ConnectionRegistry,
    intent: dict[str, object],
) -> None:
    payload = (
        json.dumps(
            _receipt_document(snapshot, registry, intent),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    _atomic_write_new(
        state_root / MIGRATION_RECEIPT_FILE, payload, "migration receipt"
    )


def _notify(hook: InterruptionHook | None, phase: str) -> None:
    if hook is not None:
        hook(phase)


def _snapshot_bound_to_intent(
    state_root: Path, intent: dict[str, object]
) -> _LegacySnapshot:
    snapshot = _load_backup(state_root)
    if snapshot is None:
        snapshot = _capture_legacy(state_root)
        if (
            snapshot.existed != intent["legacy_existed"]
            or _sha256(snapshot.payload) != intent["legacy_sha256"]
        ):
            raise RuntimeError(
                "Current legacy input does not match the migration intent"
            )
        _write_backup(state_root, snapshot)
    if (
        snapshot.existed != intent["legacy_existed"]
        or _sha256(snapshot.payload) != intent["legacy_sha256"]
    ):
        raise RuntimeError("Legacy migration backup does not match its intent")
    return snapshot


def _save_new_connection_registry(
    state_root: Path, registry: ConnectionRegistry
) -> ConnectionRegistry:
    payload = _canonical_registry_payload(registry)
    _atomic_write_new(
        state_root / REGISTRY_FILE, payload, "connection registry migration target"
    )
    saved = load_connection_registry(state_root)
    if saved is None:
        raise RuntimeError("Connection registry migration target was not persisted")
    return saved


def ensure_connection_registry(
    state_root: Path,
    *,
    installation_identity: str,
    local_data_dir: str | None = None,
    remote_identity_reader: IdentityReader | None = None,
    local_identity_reader: IdentityReader = read_local_workspace_identity,
    interruption_hook: InterruptionHook | None = None,
    lock_timeout: float = 2.0,
    lock_poll_interval: float = 0.05,
) -> ConnectionRegistry:
    """Load a native registry or migrate one legacy singleton exactly once.

    A valid receipt is the migration commit marker.  If it exists, the legacy
    file is never read.  If an interruption left only a backup and/or registry,
    the exact backup is replayed deterministically and the transaction resumes.
    """

    state_root = Path(state_root)
    with _migration_lock(state_root, lock_timeout, lock_poll_interval):
        return _ensure_connection_registry_locked(
            state_root,
            installation_identity=installation_identity,
            local_data_dir=local_data_dir,
            remote_identity_reader=remote_identity_reader,
            local_identity_reader=local_identity_reader,
            interruption_hook=interruption_hook,
        )


def _ensure_connection_registry_locked(
    state_root: Path,
    *,
    installation_identity: str,
    local_data_dir: str | None,
    remote_identity_reader: IdentityReader | None,
    local_identity_reader: IdentityReader,
    interruption_hook: InterruptionHook | None,
) -> ConnectionRegistry:
    receipt = _load_receipt(state_root)
    intent = _load_intent(state_root)
    if receipt is not None:
        if intent is None:
            raise RuntimeError("Migration receipt exists but migration intent is missing")
        _verify_receipt_history(receipt, intent, installation_identity)
        _backup_snapshot_for_receipt(state_root, receipt)
        return _registry_for_receipt(state_root)

    existing = load_connection_registry(state_root)
    backup_present = _load_backup(state_root) is not None
    if existing is not None and intent is None and not backup_present:
        return existing
    if intent is None and backup_present:
        raise RuntimeError(
            "Migration backup exists without an authority-binding migration intent"
        )

    if intent is None:
        snapshot = _capture_legacy(state_root)
        expected = _expected_migrated_registry(
            snapshot,
            installation_identity=installation_identity,
            local_data_dir=local_data_dir,
            remote_identity_reader=remote_identity_reader,
            local_identity_reader=local_identity_reader,
        )
        intent = _intent_document(snapshot, expected)
        _write_intent(state_root, intent)
        _notify(interruption_hook, "intent-saved")
        _write_backup(state_root, snapshot)
    else:
        snapshot = _snapshot_bound_to_intent(state_root, intent)
        expected = _expected_migrated_registry(
            snapshot,
            installation_identity=installation_identity,
            local_data_dir=local_data_dir,
            remote_identity_reader=remote_identity_reader,
            local_identity_reader=local_identity_reader,
        )
        _require_matching_intent(intent, _intent_document(snapshot, expected))
    _notify(interruption_hook, "backup-saved")

    if existing is None:
        registry = _save_new_connection_registry(state_root, expected)
    elif _canonical_registry_payload(existing) == _canonical_registry_payload(expected):
        registry = existing
    else:
        raise RuntimeError(
            "Interrupted migration registry does not match the exact legacy backup"
        )
    _notify(interruption_hook, "registry-saved")
    _write_receipt(state_root, snapshot, registry, intent)
    _notify(interruption_hook, "receipt-saved")
    return registry


def select_active_profile_for_startup(
    state_root: Path,
    *,
    remote_identity_reader: IdentityReader | None = None,
    local_identity_reader: IdentityReader = read_local_workspace_identity,
) -> StartupSelection:
    """Return a verified typed startup selection without launching anything."""

    state_root = Path(state_root)
    registry = load_connection_registry(state_root)
    if registry is None:
        raise RuntimeError("Connection registry is not configured")
    active_id = registry.active_profile_id
    if active_id is None:
        raise RuntimeError("Connection registry has no active profile")
    profiles = [profile for profile in registry.profiles if profile.profile_id == active_id]
    if len(profiles) != 1:
        raise RuntimeError("Active profile is missing or ambiguous")
    profile = profiles[0]
    if not profile.enabled:
        raise RuntimeError("Active profile is disabled")
    reader = _identity_reader_for(profile, remote_identity_reader, local_identity_reader)
    _verify_identity(profile, reader)
    if isinstance(profile, LocalConnectionProfile):
        return LocalStartupSelection(
            profile_id=profile.profile_id,
            label=profile.label,
            expected_workspace_id=profile.expected_workspace_id,
            data_dir=Path(profile.data_dir),
            backup_dir=state_root / "workspace-backups" / profile.profile_id,
            live_updates=profile.live_updates,
        )
    return SshStartupSelection(
        profile_id=profile.profile_id,
        label=profile.label,
        expected_workspace_id=profile.expected_workspace_id,
        ssh_host_alias=profile.ssh_host_alias,
        remote_app_dir=profile.remote_app_dir,
        remote_data_dir=profile.remote_data_dir,
        preferred_forward_port=profile.preferred_forward_port,
        remote_port=profile.remote_port,
        live_updates=profile.live_updates,
    )
