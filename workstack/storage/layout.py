"""Safe, deterministic path resolution for Work Stack SSOT v4 packages."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


RECORD_KINDS = ("captures", "notes", "objectives", "replies", "tasks")
STREAM_KINDS = ("activity", "planning-status", "worklog")

_UID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_BUCKET = re.compile(r"^[0-9a-f]{2}$")
_SEGMENT = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])\.ndjson$")


class StorageLayoutError(ValueError):
    """A content-free rejection of an unsafe or non-v4 path layout."""

    def __init__(self, code: str, artifact: str = "") -> None:
        super().__init__(code if not artifact else f"{code}: {artifact}")
        self.code = code
        self.artifact = artifact


@dataclass(frozen=True)
class RecordLocation:
    kind: str
    uid: str
    bucket: str
    artifact: str
    path: Path


@dataclass(frozen=True)
class StreamLocation:
    kind: str
    segment: str
    artifact: str
    path: Path


def _is_device_or_unc(raw: str) -> bool:
    normalized = raw.replace("/", "\\")
    return normalized.startswith("\\\\")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError as error:
        raise StorageLayoutError("PATH_UNREADABLE") from error
    attributes = getattr(details, "st_file_attributes", 0)
    return stat.S_ISLNK(details.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _artifact(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as error:
        raise StorageLayoutError("PATH_ESCAPES_ROOT") from error


def _safe_entries(directory: Path, root: Path) -> tuple[Path, ...]:
    if _is_link_or_reparse(directory):
        raise StorageLayoutError("LINK_REJECTED", _artifact(directory, root))
    if not directory.is_dir():
        raise StorageLayoutError("DIRECTORY_REQUIRED", _artifact(directory, root))
    try:
        entries = tuple(Path(item.path) for item in os.scandir(directory))
    except OSError as error:
        raise StorageLayoutError("DIRECTORY_UNREADABLE", _artifact(directory, root)) from error
    _reject_case_collisions((entry.name for entry in entries), _artifact(directory, root))
    for entry in entries:
        if _is_link_or_reparse(entry):
            raise StorageLayoutError("LINK_REJECTED", _artifact(entry, root))
    return tuple(sorted(entries, key=lambda item: item.name))


def _reject_case_collisions(names: Iterable[str], artifact: str) -> None:
    folded: dict[str, str] = {}
    for name in names:
        text = str(name)
        key = text.casefold()
        if key in folded and folded[key] != text:
            raise StorageLayoutError("CASE_COLLISION", artifact)
        folded[key] = text


def _require_regular_file(path: Path, root: Path) -> None:
    if _is_link_or_reparse(path):
        raise StorageLayoutError("LINK_REJECTED", _artifact(path, root))
    if not path.is_file():
        raise StorageLayoutError("FILE_REQUIRED", _artifact(path, root))


@dataclass(frozen=True)
class V4Layout:
    """Admitted v4 authority root with safe deterministic enumeration."""

    root: Path

    @classmethod
    def open(cls, root: Path | str) -> "V4Layout":
        raw = os.fspath(root)
        if not raw or _is_device_or_unc(raw):
            raise StorageLayoutError("ROOT_PATH_REJECTED")
        candidate = Path(raw).expanduser()
        if any(part == ".." for part in candidate.parts):
            raise StorageLayoutError("PATH_TRAVERSAL_REJECTED")
        if not candidate.exists() or not candidate.is_dir():
            raise StorageLayoutError("ROOT_DIRECTORY_REQUIRED")
        if _is_link_or_reparse(candidate):
            raise StorageLayoutError("LINK_REJECTED", ".")
        resolved = candidate.resolve(strict=True)
        layout = cls(resolved)
        _safe_entries(resolved, resolved)
        _require_regular_file(layout.store_path, resolved)
        _require_regular_file(layout.workspace_path, resolved)
        return layout

    @property
    def store_path(self) -> Path:
        return self.root / "store.json"

    @property
    def workspace_path(self) -> Path:
        return self.root / "workspace.json"

    def record_path(self, kind: str, uid: str) -> Path:
        if kind not in RECORD_KINDS:
            raise StorageLayoutError("UNKNOWN_RECORD_KIND")
        if not _UID.fullmatch(uid):
            raise StorageLayoutError("INVALID_RECORD_UID")
        return self.root / "records" / kind / uid[:2] / f"{uid}.json"

    def record_files(self) -> tuple[RecordLocation, ...]:
        records_root = self.root / "records"
        if not records_root.exists():
            return ()
        locations: list[RecordLocation] = []
        for kind_path in _safe_entries(records_root, self.root):
            if kind_path.name not in RECORD_KINDS:
                raise StorageLayoutError("UNKNOWN_RECORD_KIND", _artifact(kind_path, self.root))
            if not kind_path.is_dir():
                raise StorageLayoutError("DIRECTORY_REQUIRED", _artifact(kind_path, self.root))
            locations.extend(self._kind_records(kind_path))
        return tuple(locations)

    def _kind_records(self, kind_path: Path) -> list[RecordLocation]:
        locations: list[RecordLocation] = []
        for bucket_path in _safe_entries(kind_path, self.root):
            if not _BUCKET.fullmatch(bucket_path.name) or not bucket_path.is_dir():
                raise StorageLayoutError("INVALID_RECORD_BUCKET", _artifact(bucket_path, self.root))
            for path in _safe_entries(bucket_path, self.root):
                location = self._record_location(kind_path.name, bucket_path.name, path)
                locations.append(location)
        return locations

    def _record_location(self, kind: str, bucket: str, path: Path) -> RecordLocation:
        _require_regular_file(path, self.root)
        if path.suffix != ".json" or not _UID.fullmatch(path.stem):
            raise StorageLayoutError("INVALID_RECORD_FILE", _artifact(path, self.root))
        uid = path.stem
        if bucket != uid[:2] or path != self.record_path(kind, uid):
            raise StorageLayoutError("RECORD_PATH_MISMATCH", _artifact(path, self.root))
        return RecordLocation(kind, uid, bucket, _artifact(path, self.root), path)

    def stream_files(self) -> tuple[StreamLocation, ...]:
        streams_root = self.root / "streams"
        if not streams_root.exists():
            return ()
        locations: list[StreamLocation] = []
        for kind_path in _safe_entries(streams_root, self.root):
            if kind_path.name not in STREAM_KINDS:
                raise StorageLayoutError("UNKNOWN_STREAM_KIND", _artifact(kind_path, self.root))
            if not kind_path.is_dir():
                raise StorageLayoutError("DIRECTORY_REQUIRED", _artifact(kind_path, self.root))
            for path in _safe_entries(kind_path, self.root):
                _require_regular_file(path, self.root)
                if not _SEGMENT.fullmatch(path.name):
                    raise StorageLayoutError("INVALID_STREAM_SEGMENT", _artifact(path, self.root))
                locations.append(
                    StreamLocation(kind_path.name, path.stem, _artifact(path, self.root), path)
                )
        return tuple(locations)
