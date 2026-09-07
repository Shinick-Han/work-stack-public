"""Verified, offline maintenance for the local Work Stack planning store."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import store_report_migration
from .store import DEFAULTS, LOCK_NAME, Store, StoreLockedError
from .storage.migration_source import freeze_v3_source, verify_v3_source_unchanged
from .storage.migration_v3_lease import hold_v3_source


BACKUP_SCHEMA_VERSION = store_report_migration.BACKUP_SCHEMA_VERSION
BACKUP_MANIFEST = store_report_migration.BACKUP_MANIFEST
MAX_BACKUP_BYTES = store_report_migration.MAX_ARCHIVE_BYTES


class BackupValidationError(ValueError):
    """Raised before any restore write when an archive is malformed or inconsistent."""


@dataclass(frozen=True)
class BackupArtifact:
    path: Path
    workspace_id: str
    created_at: str
    digest: str
    file_count: int


@dataclass(frozen=True)
class BackupDownload:
    body: bytes
    filename: str
    workspace_id: str
    created_at: str
    digest: str
    file_count: int


@dataclass(frozen=True)
class RestoreReceipt:
    destination: Path
    workspace_id: str
    backup_digest: str
    safety_backup: Path | None


@dataclass(frozen=True)
class InitializeReceipt:
    destination: Path
    workspace_id: str
    store_schema_version: int


class StoreInitializationRefused(ValueError):
    """Raised before any write when the target is not an absent or empty directory."""


def _sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _backup_roster(store_schema_version: object, /) -> tuple[str, ...]:
    try:
        return store_report_migration.backup_roster(store_schema_version)
    except store_report_migration.BackupPackError as error:
        raise BackupValidationError(str(error)) from error


def _build_backup_download_from_validated_bodies(
    bodies: object,
    /,
    *,
    workspace_id: object,
    store_schema_version: object,
    created: object,
) -> BackupDownload:
    try:
        packed = store_report_migration.pack_backup_archive(
            bodies,
            workspace_id=workspace_id,
            store_schema_version=store_schema_version,
            created=created,
        )
    except store_report_migration.BackupPackError as error:
        raise BackupValidationError(str(error)) from error
    return BackupDownload(
        body=packed["body"],
        filename=packed["filename"],
        workspace_id=packed["workspace_id"],
        created_at=packed["created_at"],
        digest=packed["digest"],
        file_count=packed["file_count"],
    )


def create_backup_download(store: Store) -> BackupDownload:
    """Build one full verified-store archive without changing the planning store."""

    with store.consistent_read() as readiness:
        roster = _backup_roster(readiness.schema_version)
        bodies = {name: store.path(name).read_bytes() for name in roster}
        return _build_backup_download_from_validated_bodies(
            bodies,
            workspace_id=readiness.workspace_uid,
            store_schema_version=readiness.schema_version,
            created=_utc_now(),
        )


def _persist_backup_download(
    download: BackupDownload, output_dir: Path | str
) -> BackupArtifact:
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / download.filename
    with destination.open("xb") as target:
        target.write(download.body)
    return BackupArtifact(
        path=destination,
        workspace_id=download.workspace_id,
        created_at=download.created_at,
        digest=download.digest,
        file_count=download.file_count,
    )


def backup_store(data_dir: Path | str, output_dir: Path | str) -> BackupArtifact:
    """Create one validated archive while holding the store's only-writer lease."""

    store = Store(data_dir)
    # This read selects a reader only. The historical path must independently
    # admit all held bytes as v3; a concurrent format change refuses there.
    metadata_path = store.path("store-meta.json")
    metadata = json.loads(metadata_path.read_bytes()) if metadata_path.is_file() else {}
    historical = type(metadata) is dict and type(metadata.get("store_schema_version")) is int and metadata["store_schema_version"] == 3
    download = _historical_backup_download(store) if historical else create_backup_download(store)
    return _persist_backup_download(
        download, output_dir
    )


def _historical_backup_download(store: Store) -> BackupDownload:
    """Back up pre-upgrade v3 bytes without initializing a current-version Store."""

    with hold_v3_source(store.root) as held:
        if os.path.lexists(held.root / "store.json"):
            raise BackupValidationError("historical backup source contains a v4 authority marker")
        frozen = freeze_v3_source(held.root)
        bodies = {artifact.name: frozen.body(artifact.name) for artifact in frozen.artifacts}
        values = store._decoded_documents(bodies)
        readiness = held.admit(values)
        store._assert_upgrade_source_owned_locked(bodies, values, readiness)
        download = _build_backup_download_from_validated_bodies(
            bodies, workspace_id=readiness.workspace_uid,
            store_schema_version=readiness.schema_version, created=_utc_now(),
        )
        verify_v3_source_unchanged(frozen)
        return download


def _read_verified_archive(
    path: Path | str,
) -> tuple[BackupArtifact, int, dict[str, dict[str, Any]]]:
    """Verify an archive read-only, and report it as this layer's artifact.

    The verifier itself sits below the store, because the migration must prove
    its own rollback archive from that archive's final path while it holds the
    writer lease. Wrapping it here keeps `BackupValidationError` the one
    refusal a maintenance caller catches, with the messages it already matches.
    """

    try:
        verified = store_report_migration.verify_archive_file(path)
    except store_report_migration.BackupPackError as error:
        raise BackupValidationError(str(error)) from error
    artifact = BackupArtifact(
        path=verified.path,
        workspace_id=verified.workspace_id,
        created_at=verified.created_at,
        digest=verified.digest,
        file_count=verified.file_count,
    )
    return artifact, verified.store_schema_version, verified.values


def verify_backup(path: Path | str) -> BackupArtifact:
    artifact, _schema, _values = _read_verified_archive(path)
    return artifact


def _has_store_files(destination: Path) -> bool:
    return any((destination / name).exists() for name in DEFAULTS)


def initialize_store(data_dir: Path | str) -> InitializeReceipt:
    """Create one fresh, complete Store only in an absent or empty directory.

    Never migrates, repairs or recovers: any entry other than the Store's
    writer-lease marker refuses before the lease is taken, and the result must
    report a fresh origin, so the server's own initialization stays the only path
    for existing data.
    """

    destination = Path(data_dir).expanduser().resolve()
    if os.path.lexists(destination) and not destination.is_dir():
        raise StoreInitializationRefused("destination is not a directory")
    occupied: list[str] = []
    if destination.is_dir():
        with os.scandir(destination) as entries:
            occupied = sorted(
                entry.name
                for entry in entries
                if not (entry.name == LOCK_NAME and entry.is_file(follow_symlinks=False))
            )
    if occupied:
        raise StoreInitializationRefused(
            "destination already contains entries; a new workspace requires an empty "
            "directory: " + ", ".join(occupied)
        )
    readiness = Store(destination).initialize()
    if readiness.migration_origin != "fresh":
        raise StoreInitializationRefused("destination was not initialized as a fresh store")
    return InitializeReceipt(
        destination=destination,
        workspace_id=readiness.workspace_uid,
        store_schema_version=readiness.schema_version,
    )


@dataclass(frozen=True)
class _HeldDestinationGeneration:
    workspace_id: str
    schema_version: int
    bodies: dict[str, bytes]
    manifest: bytes


def _runtime_manifest_bytes(store: Store) -> bytes:
    path = store.store_manifest_path
    if not path.is_file():
        return b""
    return path.read_bytes()


def _existing_destination_generation(store: Store) -> _HeldDestinationGeneration:
    """Read an occupied destination's identity and generation, writing nothing.

    `consistent_read` is the store's read boundary: it holds the writer lease,
    refuses a pending journal, validates the generation it finds and never
    recovers, migrates or writes. The bodies and runtime manifest returned here
    are the generation a replace must keep bound: the safety snapshot is packed
    from these bytes, and the destination commit revalidates them rather than
    accepting a later foreign replacement.
    """

    with store.consistent_read() as readiness:
        roster = _backup_roster(readiness.schema_version)
        return _HeldDestinationGeneration(
            workspace_id=readiness.workspace_uid,
            schema_version=readiness.schema_version,
            bodies={name: store.path(name).read_bytes() for name in roster},
            manifest=_runtime_manifest_bytes(store),
        )


def _assert_held_destination_generation(
    store: Store, held: _HeldDestinationGeneration
) -> None:
    try:
        bodies = {name: store.path(name).read_bytes() for name in held.bodies}
    except OSError:
        bodies = None
    if bodies != held.bodies or _runtime_manifest_bytes(store) != held.manifest:
        raise BackupValidationError(
            "destination holds a different workspace; restore refuses to replace it"
        )


def _safety_backup_from_held(
    held: _HeldDestinationGeneration, output_dir: Path | str
) -> BackupArtifact:
    return _persist_backup_download(
        _build_backup_download_from_validated_bodies(
            held.bodies,
            workspace_id=held.workspace_id,
            store_schema_version=held.schema_version,
            created=_utc_now(),
        ),
        output_dir,
    )


def _commit_restored_store(
    store: Store,
    artifact: BackupArtifact,
    values: dict[str, dict[str, Any]],
) -> None:
    store.initialize()
    store.save_many(
        values, operation_id="maintenance-restore-{}".format(artifact.digest[7:23])
    )
    restored = store.initialize()
    if restored.workspace_uid != artifact.workspace_id:
        raise BackupValidationError("restored workspace identity did not verify")


def _restore_occupied_destination(
    destination: Path,
    *,
    artifact: BackupArtifact,
    schema_version: int,
    values: dict[str, dict[str, Any]],
    safety_backup_dir: Path | str,
) -> RestoreReceipt:
    store = Store(destination)
    lease = store.try_acquire_writer_lease()
    if lease is None:
        raise StoreLockedError(
            "the Work Stack data directory is already owned by another writer"
        )
    try:
        held = _existing_destination_generation(store)
        if held.workspace_id != artifact.workspace_id:
            raise BackupValidationError(
                "destination holds a different workspace; restore refuses to replace it"
            )
        _assert_held_destination_generation(store, held)
        safety = _safety_backup_from_held(held, safety_backup_dir)
        writes = _restored_documents(schema_version, values)
        _assert_held_destination_generation(store, held)
        _commit_restored_store(store, artifact, writes)
        return RestoreReceipt(
            destination=destination,
            workspace_id=artifact.workspace_id,
            backup_digest=artifact.digest,
            safety_backup=safety.path,
        )
    finally:
        store.release_writer_lease(lease)


def restore_store(
    backup_path: Path | str,
    destination_dir: Path | str,
    *,
    replace: bool = False,
    safety_backup_dir: Path | str | None = None,
) -> RestoreReceipt:
    """Verify completely, optionally back up existing state, then journal-commit restore."""

    artifact, schema_version, values = _read_verified_archive(backup_path)
    destination = Path(destination_dir).expanduser().resolve()
    exists = _has_store_files(destination)
    if exists and not replace:
        raise BackupValidationError("destination already contains a Work Stack store")
    if exists:
        if safety_backup_dir is None:
            raise BackupValidationError("a safety backup directory is required when replacing")
        return _restore_occupied_destination(
            destination,
            artifact=artifact,
            schema_version=schema_version,
            values=values,
            safety_backup_dir=safety_backup_dir,
        )
    writes = _restored_documents(schema_version, values)
    store = Store(destination)
    _commit_restored_store(store, artifact, writes)
    return RestoreReceipt(
        destination=destination,
        workspace_id=artifact.workspace_id,
        backup_digest=artifact.digest,
        safety_backup=None,
    )


def _restored_documents(
    schema_version: int, values: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The documents to write, upgraded when the archive predates this build.

    An older archive is converted the same way an older directory is, so the
    destination ends up a coherent v5 authority with evidence naming the
    version the archive actually held. Dropping the archive's own metadata onto
    a fresh v5 store would leave a generation that fails its own readiness.
    """

    if schema_version == store_report_migration.CURRENT_SCHEMA_VERSION:
        return values
    try:
        writes, _operation = store_report_migration.plan_upgrade(
            schema_version,
            values,
            store_report_migration.source_digest(values),
            now=_utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )
    except ValueError as error:
        raise BackupValidationError("backup store failed semantic validation") from error
    return writes


def relocate_store(source_dir: Path | str, destination_dir: Path | str) -> RestoreReceipt:
    """Copy a verified workspace to an empty destination; never delete the source."""

    destination = Path(destination_dir).expanduser().resolve()
    if _has_store_files(destination):
        raise BackupValidationError("relocation destination already contains Work Stack data")
    with tempfile.TemporaryDirectory(prefix="workstack-relocate-") as temporary:
        artifact = backup_store(source_dir, temporary)
        return restore_store(artifact.path, destination)
