"""Deterministic backup and empty-destination restore for inactive v4 authorities.

Only canonical artifacts admitted by ``read_v4`` enter the archive.  Runtime
manifests, journals, leases, idempotency ledgers, migration receipts, SQLite
projections, and other local state are intentionally outside that roster.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .canonical import CANONICAL_JSON_FORMAT, CanonicalJsonError, canonical_json_bytes
from .manifest import V4ManifestError, build_v4_manifest
from .reader import StorageReadError, V4ReadLimits, V4ReadResult, read_v4
from .validation import validate_storage_path


V4_BACKUP_FORMAT = "workstack.v4-backup"
V4_BACKUP_SCHEMA_VERSION = 1
V4_BACKUP_MANIFEST = "v4-export-manifest.json"
MAX_V4_BACKUP_BYTES = 268_435_456
MAX_V4_BACKUP_MEMBERS = 110_000
MAX_V4_BACKUP_MEMBER_NAME = 512
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class V4BackupError(ValueError):
    """A stable content-free backup or restore refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class V4Backup:
    body: bytes
    filename: str
    digest: str
    authority_digest: str
    workspace_uid: str
    file_count: int
    record_count: int
    event_count: int


@dataclass(frozen=True)
class V4BackupArtifact:
    path: Path
    digest: str
    authority_digest: str
    workspace_uid: str
    file_count: int


@dataclass(frozen=True)
class V4RestoreReceipt:
    destination: Path
    backup_digest: str
    authority_digest: str
    workspace_uid: str
    file_count: int


@dataclass(frozen=True)
class _VerifiedArchive:
    body: bytes
    manifest: Mapping[str, Any]
    artifacts: Mapping[str, bytes]


class _DuplicateKeyError(ValueError):
    pass


def _sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKeyError
        value[key] = item
    return value


def _verified_source(root: Path | str, limits: V4ReadLimits | None) -> tuple[V4ReadResult, str]:
    report = validate_storage_path(root)
    if not report.valid or report.format_version != 4:
        raise V4BackupError("V4_SOURCE_INVALID")
    try:
        result = read_v4(root, limits=limits)
        manifest = build_v4_manifest(result)
    except (OSError, ValueError, StorageReadError, V4ManifestError) as error:
        raise V4BackupError("V4_SOURCE_INVALID") from error
    return result, manifest.digest


def _read_source_artifacts(result: V4ReadResult) -> dict[str, bytes]:
    bodies: dict[str, bytes] = {}
    for artifact in sorted(result.artifacts, key=lambda item: item.artifact):
        path = result.root / PurePosixPath(artifact.artifact)
        try:
            body = path.read_bytes()
        except OSError as error:
            raise V4BackupError("V4_SOURCE_CHANGED") from error
        if len(body) != artifact.byte_count or _sha256(body) != artifact.sha256:
            raise V4BackupError("V4_SOURCE_CHANGED")
        bodies[artifact.artifact] = body
    return bodies


def _file_roster(result: V4ReadResult, bodies: Mapping[str, bytes]) -> list[dict[str, Any]]:
    metadata = {artifact.artifact: artifact for artifact in result.artifacts}
    return [
        {
            "category": metadata[name].category,
            "kind": metadata[name].kind,
            "path": name,
            "sha256": _sha256(body),
            "size": len(body),
        }
        for name, body in sorted(bodies.items())
    ]


def _export_manifest(
    result: V4ReadResult, bodies: Mapping[str, bytes], authority_digest: str
) -> dict[str, Any]:
    return {
        "authority_digest": authority_digest,
        "canonical_json": CANONICAL_JSON_FORMAT,
        "event_count": result.event_count,
        "files": _file_roster(result, bodies),
        "format": V4_BACKUP_FORMAT,
        "record_count": result.record_count,
        "schema_set": "workstack.ssot.v4",
        "schema_version": V4_BACKUP_SCHEMA_VERSION,
        "store_schema_version": 4,
        "workspace_uid": result.workspace_uid,
    }


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _archive_bytes(manifest: Mapping[str, Any], bodies: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        archive.writestr(_zip_info(V4_BACKUP_MANIFEST), canonical_json_bytes(manifest))
        for name, body in sorted(bodies.items()):
            archive.writestr(_zip_info(name), body)
    return output.getvalue()


def create_v4_backup(
    root: Path | str, *, limits: V4ReadLimits | None = None
) -> V4Backup:
    """Return a deterministic ZIP of one stable, verified v4 authority."""

    result, authority_digest = _verified_source(root, limits)
    bodies = _read_source_artifacts(result)
    after, after_digest = _verified_source(root, limits)
    if authority_digest != after_digest:
        raise V4BackupError("V4_SOURCE_CHANGED")
    if {item.artifact: item.sha256 for item in result.artifacts} != {
        item.artifact: item.sha256 for item in after.artifacts
    }:
        raise V4BackupError("V4_SOURCE_CHANGED")
    manifest = _export_manifest(result, bodies, authority_digest)
    with tempfile.TemporaryDirectory(prefix="workstack-v4-backup-freeze-") as temporary:
        frozen = Path(temporary)
        _write_artifacts(frozen, bodies)
        _validate_materialized(frozen, manifest, list(manifest["files"]))
    body = _archive_bytes(manifest, bodies)
    filename = "workstack-v4-{}-{}.zip".format(
        result.workspace_uid[:8], authority_digest[7:19]
    )
    return V4Backup(
        body=body,
        filename=filename,
        digest=_sha256(body),
        authority_digest=authority_digest,
        workspace_uid=result.workspace_uid,
        file_count=len(bodies),
        record_count=result.record_count,
        event_count=result.event_count,
    )


def write_v4_backup(
    root: Path | str,
    destination: Path | str,
    *,
    limits: V4ReadLimits | None = None,
) -> V4BackupArtifact:
    """Write a verified archive without overwriting an existing path."""

    backup = create_v4_backup(root, limits=limits)
    path = Path(destination).expanduser().resolve(strict=False)
    if not path.parent.is_dir():
        raise V4BackupError("BACKUP_PARENT_REQUIRED")
    try:
        with path.open("xb") as output:
            output.write(backup.body)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError as error:
        raise V4BackupError("BACKUP_DESTINATION_EXISTS") from error
    except OSError as error:
        path.unlink(missing_ok=True)
        raise V4BackupError("BACKUP_WRITE_FAILED") from error
    return V4BackupArtifact(
        path, backup.digest, backup.authority_digest, backup.workspace_uid, backup.file_count
    )


def _archive_body(source: Path | str | bytes) -> bytes:
    if isinstance(source, bytes):
        body = source
    else:
        path = Path(source).expanduser().resolve(strict=False)
        try:
            if not path.is_file() or path.stat().st_size > MAX_V4_BACKUP_BYTES:
                raise V4BackupError("BACKUP_ARCHIVE_UNAVAILABLE")
            body = path.read_bytes()
        except OSError as error:
            raise V4BackupError("BACKUP_ARCHIVE_UNAVAILABLE") from error
    if not body or len(body) > MAX_V4_BACKUP_BYTES:
        raise V4BackupError("BACKUP_ARCHIVE_SIZE_INVALID")
    return body


def _safe_member_name(name: str) -> bool:
    if not name or len(name) > MAX_V4_BACKUP_MEMBER_NAME or "\\" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _safe_zip_info(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    return (
        _safe_member_name(info.filename)
        and not info.is_dir()
        and not info.flag_bits & 0x1
        and info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        and file_type in {0, stat.S_IFREG}
        and 0 <= info.file_size <= MAX_V4_BACKUP_BYTES
    )


def _read_members(body: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(body), "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if (
                not infos
                or len(infos) > MAX_V4_BACKUP_MEMBERS
                or len(names) != len(set(names))
                or any(not _safe_zip_info(info) for info in infos)
                or sum(info.file_size for info in infos) > MAX_V4_BACKUP_BYTES
            ):
                raise V4BackupError("BACKUP_ARCHIVE_MEMBERS_INVALID")
            return {info.filename: archive.read(info) for info in infos}
    except V4BackupError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise V4BackupError("BACKUP_ARCHIVE_INVALID") from error


def _decode_manifest(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            body.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKeyError) as error:
        raise V4BackupError("BACKUP_MANIFEST_INVALID") from error
    if not isinstance(value, dict):
        raise V4BackupError("BACKUP_MANIFEST_INVALID")
    try:
        if canonical_json_bytes(value) != body:
            raise V4BackupError("BACKUP_MANIFEST_INVALID")
    except CanonicalJsonError as error:
        raise V4BackupError("BACKUP_MANIFEST_INVALID") from error
    return value


def _valid_header(value: Mapping[str, Any]) -> bool:
    expected = {
        "authority_digest", "canonical_json", "event_count", "files", "format",
        "record_count", "schema_set", "schema_version", "store_schema_version",
        "workspace_uid",
    }
    return (
        set(value) == expected
        and value.get("format") == V4_BACKUP_FORMAT
        and value.get("schema_version") == V4_BACKUP_SCHEMA_VERSION
        and value.get("canonical_json") == CANONICAL_JSON_FORMAT
        and value.get("schema_set") == "workstack.ssot.v4"
        and value.get("store_schema_version") == 4
        and isinstance(value.get("workspace_uid"), str)
        and bool(_UUID.fullmatch(value["workspace_uid"]))
        and isinstance(value.get("authority_digest"), str)
        and bool(_SHA256.fullmatch(value["authority_digest"]))
        and type(value.get("record_count")) is int
        and value["record_count"] >= 0
        and type(value.get("event_count")) is int
        and value["event_count"] >= 0
    )


def _validated_file_record(
    record: Any, members: Mapping[str, bytes]
) -> tuple[dict[str, Any], str]:
    expected_fields = {"category", "kind", "path", "sha256", "size"}
    if not isinstance(record, dict) or set(record) != expected_fields:
        raise V4BackupError("BACKUP_FILE_MANIFEST_INVALID")
    path = record.get("path")
    if not isinstance(path, str) or not _safe_member_name(path):
        raise V4BackupError("BACKUP_FILE_MANIFEST_INVALID")
    if not isinstance(record.get("category"), str) or not isinstance(record.get("kind"), str):
        raise V4BackupError("BACKUP_FILE_MANIFEST_INVALID")
    body = members.get(path)
    if (
        body is None
        or type(record.get("size")) is not int
        or record["size"] != len(body)
        or not isinstance(record.get("sha256"), str)
        or record["sha256"] != _sha256(body)
    ):
        raise V4BackupError("BACKUP_FILE_MISMATCH")
    return record, path


def _file_manifest(value: Mapping[str, Any], members: Mapping[str, bytes]) -> list[dict[str, Any]]:
    files = value.get("files")
    if not isinstance(files, list) or not files:
        raise V4BackupError("BACKUP_FILE_MANIFEST_INVALID")
    validated = [_validated_file_record(record, members) for record in files]
    paths = [path for _, path in validated]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise V4BackupError("BACKUP_FILE_MANIFEST_INVALID")
    if set(paths) | {V4_BACKUP_MANIFEST} != set(members):
        raise V4BackupError("BACKUP_ARCHIVE_MEMBER_SET_INVALID")
    return [record for record, _ in validated]


def _write_artifacts(root: Path, artifacts: Mapping[str, bytes]) -> None:
    for name, body in sorted(artifacts.items()):
        path = root.joinpath(*PurePosixPath(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
        except OSError as error:
            raise V4BackupError("BACKUP_ARTIFACT_WRITE_FAILED") from error


def _validate_materialized(
    root: Path, manifest: Mapping[str, Any], files: list[dict[str, Any]]
) -> None:
    report = validate_storage_path(root)
    if not report.valid or report.format_version != 4:
        raise V4BackupError("BACKUP_AUTHORITY_INVALID")
    try:
        result = read_v4(root)
        authority = build_v4_manifest(result)
    except (OSError, ValueError, StorageReadError, V4ManifestError) as error:
        raise V4BackupError("BACKUP_AUTHORITY_INVALID") from error
    if (
        result.workspace_uid != manifest["workspace_uid"]
        or result.record_count != manifest["record_count"]
        or result.event_count != manifest["event_count"]
        or authority.digest != manifest["authority_digest"]
    ):
        raise V4BackupError("BACKUP_AUTHORITY_MISMATCH")
    expected = {(item["path"], item["category"], item["kind"], item["sha256"], item["size"]) for item in files}
    actual = {(item.artifact, item.category, item.kind, item.sha256, item.byte_count) for item in result.artifacts}
    if expected != actual:
        raise V4BackupError("BACKUP_ARTIFACT_ROSTER_MISMATCH")


def _verify_archive(source: Path | str | bytes) -> _VerifiedArchive:
    body = _archive_body(source)
    members = _read_members(body)
    manifest_body = members.get(V4_BACKUP_MANIFEST)
    if manifest_body is None:
        raise V4BackupError("BACKUP_MANIFEST_MISSING")
    manifest = _decode_manifest(manifest_body)
    if not _valid_header(manifest):
        raise V4BackupError("BACKUP_MANIFEST_UNSUPPORTED")
    files = _file_manifest(manifest, members)
    artifacts = {name: item for name, item in members.items() if name != V4_BACKUP_MANIFEST}
    with tempfile.TemporaryDirectory(prefix="workstack-v4-backup-verify-") as temporary:
        root = Path(temporary)
        _write_artifacts(root, artifacts)
        _validate_materialized(root, manifest, files)
    return _VerifiedArchive(body, manifest, artifacts)


def verify_v4_backup(source: Path | str | bytes) -> V4BackupArtifact:
    """Verify an archive fully without writing into an authority destination."""

    verified = _verify_archive(source)
    path = Path(source).expanduser().resolve() if not isinstance(source, bytes) else Path()
    manifest = verified.manifest
    return V4BackupArtifact(
        path,
        _sha256(verified.body),
        str(manifest["authority_digest"]),
        str(manifest["workspace_uid"]),
        len(verified.artifacts),
    )


def _is_link_or_reparse(path: Path) -> bool:
    details = path.lstat()
    attributes = getattr(details, "st_file_attributes", 0)
    return stat.S_ISLNK(details.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _restore_destination(path: Path | str) -> tuple[Path, bool]:
    destination = Path(path).expanduser().resolve(strict=False)
    parent = destination.parent
    if not parent.is_dir() or _is_link_or_reparse(parent):
        raise V4BackupError("RESTORE_PARENT_INVALID")
    existed = destination.exists()
    if existed:
        if _is_link_or_reparse(destination) or not destination.is_dir():
            raise V4BackupError("RESTORE_DESTINATION_INVALID")
        try:
            if next(destination.iterdir(), None) is not None:
                raise V4BackupError("RESTORE_DESTINATION_NOT_EMPTY")
        except OSError as error:
            raise V4BackupError("RESTORE_DESTINATION_INVALID") from error
    return destination, existed


def _remove_staging(path: Path, parent: Path) -> None:
    if path.parent == parent and path.name.startswith(".workstack-v4-restore-"):
        shutil.rmtree(path, ignore_errors=True)


def restore_v4_backup(
    source: Path | str | bytes, destination: Path | str
) -> V4RestoreReceipt:
    """Restore a verified archive only by publishing into an empty destination."""

    verified = _verify_archive(source)
    target, target_existed = _restore_destination(destination)
    parent = target.parent
    staging = parent / f".workstack-v4-restore-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        _write_artifacts(staging, verified.artifacts)
        files = list(verified.manifest["files"])
        _validate_materialized(staging, verified.manifest, files)
        if target_existed:
            target.rmdir()
        try:
            os.rename(staging, target)
        except OSError:
            if target_existed and not target.exists():
                target.mkdir()
            raise
    except V4BackupError:
        _remove_staging(staging, parent)
        raise
    except OSError as error:
        _remove_staging(staging, parent)
        raise V4BackupError("RESTORE_PUBLICATION_FAILED") from error
    _validate_materialized(target, verified.manifest, list(verified.manifest["files"]))
    return V4RestoreReceipt(
        target,
        _sha256(verified.body),
        str(verified.manifest["authority_digest"]),
        str(verified.manifest["workspace_uid"]),
        len(verified.artifacts),
    )
