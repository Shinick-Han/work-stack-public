"""Verified, offline maintenance for the local Work Stack planning store."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import secrets
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .store import DEFAULTS, Store


BACKUP_SCHEMA_VERSION = 1
BACKUP_MANIFEST = "manifest.json"
MAX_BACKUP_BYTES = 128 * 1024 * 1024


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


def _sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def create_backup_download(store: Store) -> BackupDownload:
    """Build one full verified-store archive without changing the planning store."""

    with store.consistent_read() as readiness:
        bodies = {name: store.path(name).read_bytes() for name in sorted(DEFAULTS)}
        created = _utc_now()
        created_at = created.isoformat(timespec="microseconds").replace("+00:00", "Z")
        files = [
            {"name": name, "sha256": _sha256(body), "size": len(body)}
            for name, body in bodies.items()
        ]
        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "product_version": __version__,
            "created_at": created_at,
            "workspace_id": readiness.workspace_uid,
            "store_schema_version": readiness.schema_version,
            "files": files,
        }
        filename = "workstack-backup-{}-{}.zip".format(
            created.strftime("%Y%m%dT%H%M%S%fZ"), readiness.workspace_uid[:8]
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(BACKUP_MANIFEST, _manifest_bytes(manifest))
            for name, body in bodies.items():
                archive.writestr(name, body)
        archive_body = buffer.getvalue()
    return BackupDownload(
        body=archive_body,
        filename=filename,
        workspace_id=readiness.workspace_uid,
        created_at=created_at,
        digest=_sha256(archive_body),
        file_count=len(bodies),
    )


def backup_store(data_dir: Path | str, output_dir: Path | str) -> BackupArtifact:
    """Create one validated archive while holding the store's only-writer lease."""

    download = create_backup_download(Store(data_dir))
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


def _backup_candidate(path: Path | str) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise BackupValidationError("backup archive does not exist")
    if candidate.stat().st_size > MAX_BACKUP_BYTES:
        raise BackupValidationError("backup archive exceeds the size limit")
    return candidate


def _read_archive_members(candidate: Path) -> dict[str, bytes]:
    expected_names = {BACKUP_MANIFEST, *DEFAULTS.keys()}
    try:
        with zipfile.ZipFile(candidate, "r") as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or set(names) != expected_names:
                raise BackupValidationError("backup archive member set is invalid")
            if any(
                item.is_dir() or item.file_size > MAX_BACKUP_BYTES
                for item in infos
            ):
                raise BackupValidationError("backup archive contains an invalid member")
            if sum(item.file_size for item in infos) > MAX_BACKUP_BYTES:
                raise BackupValidationError("expanded backup exceeds the size limit")
            return {name: archive.read(name) for name in names}
    except (zipfile.BadZipFile, OSError) as error:
        raise BackupValidationError("backup archive is unreadable") from error


def _decode_backup_manifest(bodies: dict[str, bytes]) -> dict[str, Any]:
    try:
        manifest = json.loads(bodies.pop(BACKUP_MANIFEST).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupValidationError("backup manifest is invalid") from error
    expected = {
        "schema_version",
        "product_version",
        "created_at",
        "workspace_id",
        "store_schema_version",
        "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise BackupValidationError("backup manifest fields are invalid")
    return manifest


def _validate_backup_manifest_header(manifest: dict[str, Any]) -> None:
    if manifest["schema_version"] != BACKUP_SCHEMA_VERSION:
        raise BackupValidationError("backup schema version is unsupported")
    if (
        not isinstance(manifest["product_version"], str)
        or not manifest["product_version"]
    ):
        raise BackupValidationError("backup product version is invalid")
    if not isinstance(manifest["created_at"], str):
        raise BackupValidationError("backup creation time is invalid")
    try:
        parsed_time = dt.datetime.fromisoformat(
            manifest["created_at"].replace("Z", "+00:00")
        )
    except ValueError as error:
        raise BackupValidationError("backup creation time is invalid") from error
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise BackupValidationError("backup creation time must include a timezone")
    if not isinstance(manifest["workspace_id"], str) or not manifest["workspace_id"]:
        raise BackupValidationError("backup workspace identity is invalid")


def _validate_backup_file_record(
    record: Any,
    bodies: dict[str, bytes],
    indexed: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(record, dict) or set(record) != {"name", "sha256", "size"}:
        raise BackupValidationError("backup file record is invalid")
    name = record["name"]
    if name not in DEFAULTS or name in indexed:
        raise BackupValidationError("backup file record is unknown or repeated")
    body = bodies[name]
    if type(record["size"]) is not int or record["size"] != len(body):
        raise BackupValidationError("backup member size mismatch")
    if not isinstance(record["sha256"], str) or not secrets.compare_digest(
        record["sha256"], _sha256(body)
    ):
        raise BackupValidationError("backup member digest mismatch")
    indexed[name] = record


def _verify_backup_file_manifest(
    manifest: dict[str, Any], bodies: dict[str, bytes]
) -> None:
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != len(DEFAULTS):
        raise BackupValidationError("backup file manifest is invalid")
    indexed: dict[str, dict[str, Any]] = {}
    for record in files:
        _validate_backup_file_record(record, bodies, indexed)


def _validate_backup_store(
    manifest: dict[str, Any], bodies: dict[str, bytes]
) -> None:
    with tempfile.TemporaryDirectory(prefix="workstack-backup-verify-") as temporary:
        validation_root = Path(temporary)
        for name, body in bodies.items():
            (validation_root / name).write_bytes(body)
        try:
            readiness = Store(validation_root).initialize()
        except (OSError, ValueError) as error:
            raise BackupValidationError(
                "backup store failed semantic validation"
            ) from error
        if readiness.workspace_uid != manifest["workspace_id"]:
            raise BackupValidationError("backup workspace identity mismatch")
        if readiness.schema_version != manifest["store_schema_version"]:
            raise BackupValidationError("backup store schema mismatch")


def _read_verified_archive(path: Path | str) -> tuple[BackupArtifact, dict[str, bytes]]:
    candidate = _backup_candidate(path)
    bodies = _read_archive_members(candidate)
    manifest = _decode_backup_manifest(bodies)
    _validate_backup_manifest_header(manifest)
    _verify_backup_file_manifest(manifest, bodies)
    _validate_backup_store(manifest, bodies)
    artifact = BackupArtifact(
        path=candidate,
        workspace_id=manifest["workspace_id"],
        created_at=manifest["created_at"],
        digest=_sha256(candidate.read_bytes()),
        file_count=len(bodies),
    )
    return artifact, bodies


def verify_backup(path: Path | str) -> BackupArtifact:
    artifact, _ = _read_verified_archive(path)
    return artifact


def _has_store_files(destination: Path) -> bool:
    return any((destination / name).exists() for name in DEFAULTS)


def restore_store(
    backup_path: Path | str,
    destination_dir: Path | str,
    *,
    replace: bool = False,
    safety_backup_dir: Path | str | None = None,
) -> RestoreReceipt:
    """Verify completely, optionally back up existing state, then journal-commit restore."""

    artifact, bodies = _read_verified_archive(backup_path)
    destination = Path(destination_dir).expanduser().resolve()
    exists = _has_store_files(destination)
    if exists and not replace:
        raise BackupValidationError("destination already contains a Work Stack store")
    safety_backup: Path | None = None
    if exists:
        if safety_backup_dir is None:
            raise BackupValidationError("a safety backup directory is required when replacing")
        safety_backup = backup_store(destination, safety_backup_dir).path

    values: dict[str, dict[str, Any]] = {}
    for name, body in bodies.items():
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:  # guarded above
            raise BackupValidationError("backup member JSON is invalid") from error
        if not isinstance(value, dict):
            raise BackupValidationError("backup member must contain an object")
        values[name] = value

    store = Store(destination)
    store.initialize()
    store.save_many(values, operation_id="maintenance-restore-{}".format(artifact.digest[7:23]))
    restored = Store(destination).initialize()
    if restored.workspace_uid != artifact.workspace_id:
        raise BackupValidationError("restored workspace identity did not verify")
    return RestoreReceipt(
        destination=destination,
        workspace_id=artifact.workspace_id,
        backup_digest=artifact.digest,
        safety_backup=safety_backup,
    )


def relocate_store(source_dir: Path | str, destination_dir: Path | str) -> RestoreReceipt:
    """Copy a verified workspace to an empty destination; never delete the source."""

    destination = Path(destination_dir).expanduser().resolve()
    if _has_store_files(destination):
        raise BackupValidationError("relocation destination already contains Work Stack data")
    with tempfile.TemporaryDirectory(prefix="workstack-relocate-") as temporary:
        artifact = backup_store(source_dir, temporary)
        return restore_store(artifact.path, destination)
