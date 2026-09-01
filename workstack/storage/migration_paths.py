"""Deterministic, non-mutating path planning for v3-to-v4 migration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_ARTIFACT_NAME = 120


class MigrationPathError(ValueError):
    """A content-free refusal to plan migration artifacts."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MigrationPaths:
    source_root: Path
    candidate_root: Path
    backup_path: Path


def _artifact_stem(source_name: str) -> str:
    stem = source_name.strip().rstrip(". ")
    if not stem or stem in {".", ".."}:
        raise MigrationPathError("SOURCE_NAME_INVALID")
    return stem[:72]


def _sibling_path(source: Path, value: Path | str | None, default_name: str) -> Path:
    candidate = Path(value).expanduser() if value is not None else Path(default_name)
    if not candidate.is_absolute():
        candidate = source.parent / candidate
    resolved = candidate.resolve(strict=False)
    if resolved.parent != source.parent:
        raise MigrationPathError("ARTIFACT_MUST_BE_SOURCE_SIBLING")
    if len(resolved.name) > _MAX_ARTIFACT_NAME:
        raise MigrationPathError("ARTIFACT_NAME_TOO_LONG")
    return resolved


def plan_migration_paths(
    source_root: Path | str,
    source_digest: str,
    *,
    candidate_override: Path | str | None = None,
    backup_override: Path | str | None = None,
    allow_existing: bool = False,
) -> MigrationPaths:
    """Resolve deterministic sibling paths without creating or changing them."""

    if not _SHA256.fullmatch(source_digest):
        raise MigrationPathError("SOURCE_DIGEST_INVALID")
    source = Path(source_root).expanduser()
    if not source.is_dir():
        raise MigrationPathError("SOURCE_DIRECTORY_REQUIRED")
    source = source.resolve(strict=True)
    stem = _artifact_stem(source.name)
    suffix = source_digest[7:19]
    candidate = _sibling_path(
        source,
        candidate_override,
        f"{stem}.workstack-v4-candidate-{suffix}",
    )
    backup = _sibling_path(
        source,
        backup_override,
        f"{stem}.workstack-v3-backup-{suffix}.zip",
    )
    if candidate == source or backup == source or candidate == backup:
        raise MigrationPathError("ARTIFACT_PATH_COLLISION")
    if candidate.exists() and not allow_existing:
        raise MigrationPathError("CANDIDATE_ALREADY_EXISTS")
    if backup.exists() and not allow_existing:
        raise MigrationPathError("BACKUP_ALREADY_EXISTS")
    return MigrationPaths(source, candidate, backup)
