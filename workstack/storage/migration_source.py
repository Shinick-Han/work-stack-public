"""Read-only freeze and deterministic backup primitives for v3 migration sources."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from workstack.store import DEFAULTS

from .canonical import CanonicalJsonError, canonical_json_bytes, canonical_sha256


V3_SOURCE_BACKUP_FORMAT = "workstack.v3-source-backup.v1"
V3_SOURCE_FILES = tuple(sorted(DEFAULTS))
V3_SOURCE_BACKUP_MANIFEST = "manifest.json"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class V3SourceLimits:
    """Bounds applied before migration source bytes enter memory."""

    max_file_bytes: int = 16 * 1024 * 1024
    max_archive_bytes: int = 160 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_file_bytes < 1 or self.max_archive_bytes < 1:
            raise ValueError("migration source limits must be positive")


class V3MigrationSourceError(ValueError):
    """Stable, content-free refusal raised by migration source primitives."""

    def __init__(self, code: str, artifact: str | None = None) -> None:
        message = code if artifact is None else f"{code}: {artifact}"
        super().__init__(message)
        self.code = code
        self.artifact = artifact


@dataclass(frozen=True)
class V3SourceArtifact:
    name: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class FrozenV3Source:
    root: Path
    artifacts: tuple[V3SourceArtifact, ...]
    aggregate_digest: str
    _bodies: Mapping[str, bytes] = field(repr=False, compare=False)

    def body(self, name: str) -> bytes:
        if name not in V3_SOURCE_FILES:
            raise V3MigrationSourceError("UNKNOWN_SOURCE_ARTIFACT")
        return self._bodies[name]


@dataclass(frozen=True)
class V3BackupVerification:
    path: Path
    artifacts: tuple[V3SourceArtifact, ...]
    aggregate_digest: str
    archive_digest: str


@dataclass(frozen=True)
class V3BackupArtifact(V3BackupVerification):
    staging_path: Path


def _sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError as error:
        raise V3MigrationSourceError("PATH_INSPECTION_FAILED") from error
    attributes = getattr(value, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(value.st_mode) or bool(attributes & reparse)


def _resolve_existing_directory(path: Path | str, code: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.exists() or not candidate.is_dir():
        raise V3MigrationSourceError(code)
    if _is_link_or_reparse(candidate):
        raise V3MigrationSourceError("LINK_REJECTED")
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        raise V3MigrationSourceError(code) from error


def _source_roster(root: Path) -> tuple[Path, ...]:
    try:
        entries = tuple(root.iterdir())
    except OSError as error:
        raise V3MigrationSourceError("SOURCE_ROSTER_UNREADABLE") from error
    if any(_is_link_or_reparse(entry) for entry in entries):
        raise V3MigrationSourceError("LINK_REJECTED")
    present = {entry.name for entry in entries if entry.is_file()}
    missing = [name for name in V3_SOURCE_FILES if name not in present]
    if missing:
        raise V3MigrationSourceError("SOURCE_FILE_MISSING", missing[0])
    extras = sorted(name for name in present if name.endswith(".json") and name not in V3_SOURCE_FILES)
    if extras:
        raise V3MigrationSourceError("EXTRA_AUTHORITATIVE_FILE")
    return tuple(root / name for name in V3_SOURCE_FILES)


def _file_fingerprint(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )


def _read_regular_bounded(path: Path, root: Path, limit: int) -> bytes:
    artifact = path.relative_to(root).as_posix()
    if _is_link_or_reparse(path) or not path.is_file():
        raise V3MigrationSourceError("SOURCE_FILE_NOT_REGULAR", artifact)
    try:
        before = path.stat()
        if before.st_size > limit:
            raise V3MigrationSourceError("SOURCE_FILE_TOO_LARGE", artifact)
        with path.open("rb") as source:
            body = source.read(limit + 1)
            opened = os.fstat(source.fileno())
        after = path.stat()
    except V3MigrationSourceError:
        raise
    except OSError as error:
        raise V3MigrationSourceError("SOURCE_FILE_READ_FAILED", artifact) from error
    if len(body) > limit:
        raise V3MigrationSourceError("SOURCE_FILE_TOO_LARGE", artifact)
    stable_identity = {
        _file_fingerprint(before),
        _file_fingerprint(opened),
        _file_fingerprint(after),
    }
    stable_path_metadata = (before.st_ctime_ns, before.st_mode) == (
        after.st_ctime_ns,
        after.st_mode,
    )
    if len(stable_identity) != 1 or not stable_path_metadata or len(body) != after.st_size:
        raise V3MigrationSourceError("SOURCE_CHANGED_DURING_READ", artifact)
    return body


def _artifact_roster(bodies: Mapping[str, bytes]) -> tuple[V3SourceArtifact, ...]:
    return tuple(
        V3SourceArtifact(name, len(bodies[name]), _sha256(bodies[name]))
        for name in V3_SOURCE_FILES
    )


def _aggregate_digest(artifacts: tuple[V3SourceArtifact, ...]) -> str:
    value = {
        "format": V3_SOURCE_BACKUP_FORMAT,
        "artifacts": [artifact.as_dict() for artifact in artifacts],
    }
    return canonical_sha256(value)


def _read_source_pass(root: Path, limits: V3SourceLimits) -> dict[str, bytes]:
    paths = _source_roster(root)
    return {
        path.name: _read_regular_bounded(path, root, limits.max_file_bytes)
        for path in paths
    }


def freeze_v3_source(
    root: Path | str, *, limits: V3SourceLimits | None = None
) -> FrozenV3Source:
    """Freeze one exact nine-file v3 source without writing to it."""

    active_limits = limits or V3SourceLimits()
    resolved = _resolve_existing_directory(root, "SOURCE_DIRECTORY_REQUIRED")
    first = _read_source_pass(resolved, active_limits)
    second = _read_source_pass(resolved, active_limits)
    if first != second:
        raise V3MigrationSourceError("SOURCE_CHANGED_DURING_FREEZE")
    artifacts = _artifact_roster(first)
    return FrozenV3Source(
        root=resolved,
        artifacts=artifacts,
        aggregate_digest=_aggregate_digest(artifacts),
        _bodies=MappingProxyType(first),
    )


def verify_v3_source_unchanged(
    frozen: FrozenV3Source, *, limits: V3SourceLimits | None = None
) -> None:
    """Fail closed if a frozen source no longer has the exact same bytes."""

    current = freeze_v3_source(frozen.root, limits=limits)
    if (
        current.artifacts != frozen.artifacts
        or current.aggregate_digest != frozen.aggregate_digest
    ):
        raise V3MigrationSourceError("SOURCE_CHANGED_AFTER_FREEZE")


def _backup_manifest(frozen: FrozenV3Source) -> dict[str, object]:
    return {
        "aggregate_digest": frozen.aggregate_digest,
        "artifacts": [artifact.as_dict() for artifact in frozen.artifacts],
        "format": V3_SOURCE_BACKUP_FORMAT,
    }


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100600 << 16
    return info


def _archive_bytes(frozen: FrozenV3Source) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            _zip_info(V3_SOURCE_BACKUP_MANIFEST),
            canonical_json_bytes(_backup_manifest(frozen)),
        )
        for artifact in frozen.artifacts:
            archive.writestr(_zip_info(artifact.name), frozen.body(artifact.name))
    return buffer.getvalue()


def _bounded_file(path: Path, limit: int, *, code: str) -> bytes:
    if _is_link_or_reparse(path) or not path.is_file():
        raise V3MigrationSourceError("BACKUP_FILE_NOT_REGULAR")
    try:
        if path.stat().st_size > limit:
            raise V3MigrationSourceError(code)
        with path.open("rb") as source:
            body = source.read(limit + 1)
    except V3MigrationSourceError:
        raise
    except OSError as error:
        raise V3MigrationSourceError("BACKUP_READ_FAILED") from error
    if len(body) > limit:
        raise V3MigrationSourceError(code)
    return body


def _parse_manifest_artifact(item: object, index: int) -> V3SourceArtifact:
    if not isinstance(item, dict) or set(item) != {"name", "sha256", "size"}:
        raise V3MigrationSourceError("BACKUP_ARTIFACT_INVALID")
    name, digest, size = item["name"], item["sha256"], item["size"]
    identity_valid = name == V3_SOURCE_FILES[index] and isinstance(digest, str)
    size_valid = not isinstance(size, bool) and isinstance(size, int) and size >= 0
    if not identity_valid or not size_valid or not _SHA256.fullmatch(digest):
        raise V3MigrationSourceError("BACKUP_ARTIFACT_INVALID")
    return V3SourceArtifact(name, size, digest)


def _manifest_roster(value: dict[str, object]) -> list[object]:
    if value["format"] != V3_SOURCE_BACKUP_FORMAT:
        raise V3MigrationSourceError("BACKUP_FORMAT_UNSUPPORTED")
    raw = value["artifacts"]
    if not isinstance(raw, list) or len(raw) != len(V3_SOURCE_FILES):
        raise V3MigrationSourceError("BACKUP_ROSTER_INVALID")
    return raw


def _manifest_artifacts(value: object) -> tuple[V3SourceArtifact, ...]:
    fields = {"aggregate_digest", "artifacts", "format"}
    if not isinstance(value, dict) or set(value) != fields:
        raise V3MigrationSourceError("BACKUP_MANIFEST_INVALID")
    raw = _manifest_roster(value)
    artifacts = [
        _parse_manifest_artifact(item, index) for index, item in enumerate(raw)
    ]
    result = tuple(artifacts)
    if value["aggregate_digest"] != _aggregate_digest(result):
        raise V3MigrationSourceError("BACKUP_AGGREGATE_DIGEST_MISMATCH")
    return result


def _read_archive_members(
    body: bytes, limits: V3SourceLimits
) -> tuple[dict[str, object], dict[str, bytes]]:
    expected = (V3_SOURCE_BACKUP_MANIFEST, *V3_SOURCE_FILES)
    try:
        with zipfile.ZipFile(io.BytesIO(body), "r") as archive:
            infos = archive.infolist()
            names = tuple(info.filename for info in infos)
            if names != expected or len(set(names)) != len(names):
                raise V3MigrationSourceError("BACKUP_ARCHIVE_ROSTER_INVALID")
            if any(
                info.is_dir()
                or info.flag_bits & 0x1
                or info.file_size > limits.max_file_bytes
                for info in infos
            ):
                raise V3MigrationSourceError("BACKUP_ARCHIVE_MEMBER_INVALID")
            manifest_body = archive.read(V3_SOURCE_BACKUP_MANIFEST)
            bodies = {name: archive.read(name) for name in V3_SOURCE_FILES}
    except V3MigrationSourceError:
        raise
    except (
        OSError,
        KeyError,
        NotImplementedError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise V3MigrationSourceError("BACKUP_ARCHIVE_INVALID") from error
    try:
        manifest = json.loads(manifest_body.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V3MigrationSourceError("BACKUP_MANIFEST_INVALID") from error
    try:
        canonical_body = canonical_json_bytes(manifest)
    except CanonicalJsonError as error:
        raise V3MigrationSourceError("BACKUP_MANIFEST_INVALID") from error
    if canonical_body != manifest_body:
        raise V3MigrationSourceError("BACKUP_MANIFEST_NOT_CANONICAL")
    return manifest, bodies


def verify_v3_backup(
    path: Path | str, *, limits: V3SourceLimits | None = None
) -> V3BackupVerification:
    """Verify a deterministic v3 migration backup without extracting it."""

    active_limits = limits or V3SourceLimits()
    candidate = Path(path).expanduser()
    body = _bounded_file(candidate, active_limits.max_archive_bytes, code="BACKUP_TOO_LARGE")
    manifest, bodies = _read_archive_members(body, active_limits)
    artifacts = _manifest_artifacts(manifest)
    actual = _artifact_roster(bodies)
    if actual != artifacts:
        raise V3MigrationSourceError("BACKUP_ARTIFACT_DIGEST_MISMATCH")
    return V3BackupVerification(
        path=candidate.resolve(strict=True),
        artifacts=artifacts,
        aggregate_digest=_aggregate_digest(artifacts),
        archive_digest=_sha256(body),
    )


def _safe_output_path(path: Path | str, source_root: Path, code: str) -> Path:
    candidate = Path(path).expanduser()
    parent = _resolve_existing_directory(candidate.parent, "BACKUP_OUTPUT_DIRECTORY_REQUIRED")
    resolved = parent / candidate.name
    if not candidate.name or resolved.exists() or resolved.is_relative_to(source_root):
        raise V3MigrationSourceError(code)
    return resolved


def _write_exclusive(path: Path, body: bytes) -> None:
    try:
        with path.open("xb") as target:
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
    except FileExistsError as error:
        raise V3MigrationSourceError("BACKUP_PATH_EXISTS") from error
    except OSError as error:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise V3MigrationSourceError("BACKUP_WRITE_FAILED") from error


def _remove_owned(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise V3MigrationSourceError("BACKUP_CLEANUP_FAILED") from error


def create_verified_v3_backup(
    frozen: FrozenV3Source,
    *,
    staging_path: Path | str,
    output_path: Path | str,
    limits: V3SourceLimits | None = None,
) -> V3BackupArtifact:
    """Create and verify an exact backup at caller-supplied staging/output paths."""

    active_limits = limits or V3SourceLimits()
    staging = _safe_output_path(staging_path, frozen.root, "BACKUP_STAGING_PATH_INVALID")
    output = _safe_output_path(output_path, frozen.root, "BACKUP_OUTPUT_PATH_INVALID")
    if staging == output:
        raise V3MigrationSourceError("BACKUP_PATHS_MUST_DIFFER")
    verify_v3_source_unchanged(frozen, limits=active_limits)
    body = _archive_bytes(frozen)
    if len(body) > active_limits.max_archive_bytes:
        raise V3MigrationSourceError("BACKUP_TOO_LARGE")
    _write_exclusive(staging, body)
    output_created = False
    try:
        staged = verify_v3_backup(staging, limits=active_limits)
        if staged.aggregate_digest != frozen.aggregate_digest:
            raise V3MigrationSourceError("BACKUP_SOURCE_DIGEST_MISMATCH")
        verify_v3_source_unchanged(frozen, limits=active_limits)
        _write_exclusive(output, body)
        output_created = True
        verified = verify_v3_backup(output, limits=active_limits)
        if verified.archive_digest != staged.archive_digest:
            raise V3MigrationSourceError("BACKUP_PUBLICATION_MISMATCH")
        verify_v3_source_unchanged(frozen, limits=active_limits)
    except Exception:
        if output_created:
            _remove_owned(output)
        raise
    finally:
        _remove_owned(staging)
    return V3BackupArtifact(
        path=verified.path,
        artifacts=verified.artifacts,
        aggregate_digest=verified.aggregate_digest,
        archive_digest=verified.archive_digest,
        staging_path=staging,
    )
