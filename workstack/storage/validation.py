"""Read-only validation for legacy and normalized SSOT directories.

This module owns the entry gate: root safety, format detection, the read
bounds every artifact is held to, and the order in which the v4 record and
stream validators run. The diagnostics and bounded reads live in
``validation_primitives``, the legacy probe in ``validation_v3_probe``, and
the v4 checks in ``validation_records`` and ``validation_streams``.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from ..store_rosters import V3_LEGACY_MARKER_NAMES, V3_SORTED_DOCUMENT_NAMES
from .validation_primitives import (
    StoragePathValidationReport,
    StorageValidationIssue,
    build_report,
    canonical_issues,
    read_json,
    relative,
    schema_issues,
)
from .validation_records import load_v4_records, record_semantic_issues
from .validation_streams import load_v4_streams, stream_semantic_issues
from .validation_v3_probe import validate_v3

MAX_V4_JSON_BYTES = 4 * 1024 * 1024
MAX_V4_STREAM_SEGMENT_BYTES = 16 * 1024 * 1024

__all__ = [
    "MAX_V4_JSON_BYTES",
    "MAX_V4_STREAM_SEGMENT_BYTES",
    "StoragePathValidationReport",
    "StorageValidationIssue",
    "validate_storage_path",
]


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _symlink_issues(root: Path) -> list[StorageValidationIssue]:
    issues: list[StorageValidationIssue] = []
    if _is_link(root):
        return [StorageValidationIssue("SYMLINK_REJECTED", ".")]
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories + files):
            candidate = current_path / name
            if _is_link(candidate):
                issues.append(StorageValidationIssue("SYMLINK_REJECTED", relative(candidate, root)))
    return issues


def _detect_format(root: Path) -> tuple[int | None, list[StorageValidationIssue]]:
    has_v4 = (root / "store.json").exists()
    has_v3 = any((root / name).exists() for name in V3_LEGACY_MARKER_NAMES)
    if has_v4 and has_v3:
        return None, [StorageValidationIssue("AMBIGUOUS_FORMAT")]
    if has_v4:
        return 4, []
    if has_v3 or (root / "workspace.json").exists():
        return 3, []
    return None, [StorageValidationIssue("FORMAT_NOT_DETECTED")]


def _v3_source_digests(root: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in V3_SORTED_DOCUMENT_NAMES
        if (root / name).exists()
    }


def _validate_v3(root: Path) -> StoragePathValidationReport:
    return validate_v3(root, source_digests=_v3_source_digests)


def _v4_authority_documents(
    root: Path,
) -> tuple[str | None, list[StorageValidationIssue]]:
    store_value, store_issues = read_json(root / "store.json", root, MAX_V4_JSON_BYTES)
    workspace_value, workspace_issues = read_json(
        root / "workspace.json", root, MAX_V4_JSON_BYTES
    )
    issues = store_issues + workspace_issues
    if store_value is not None:
        issues.extend(schema_issues("store.schema.json", store_value, "store.json"))
        issues.extend(canonical_issues(store_value, "store.json"))
    if workspace_value is not None:
        issues.extend(schema_issues("workspace.schema.json", workspace_value, "workspace.json"))
        issues.extend(canonical_issues(workspace_value, "workspace.json"))
    workspace_uid = store_value.get("workspace_uid") if store_value is not None else None
    if not isinstance(workspace_uid, str):
        workspace_uid = None
    if workspace_value is not None:
        if workspace_value.get("workspace_uid") != workspace_uid or workspace_value.get("uid") != workspace_uid:
            issues.append(StorageValidationIssue("WORKSPACE_UID_MISMATCH", "workspace.json"))
    return workspace_uid, issues


def _validate_v4(root: Path) -> StoragePathValidationReport:
    workspace_uid, issues = _v4_authority_documents(root)
    seen_uids = {workspace_uid} if workspace_uid is not None else set()
    records, record_count, record_issues = load_v4_records(
        root, workspace_uid, seen_uids, MAX_V4_JSON_BYTES
    )
    issues.extend(record_issues)
    issues.extend(record_semantic_issues(records, seen_uids))
    events, stream_issues = load_v4_streams(root, MAX_V4_STREAM_SEGMENT_BYTES)
    issues.extend(stream_issues)
    issues.extend(stream_semantic_issues(events, records, workspace_uid, seen_uids))
    return build_report(4, workspace_uid, record_count, issues)


def validate_storage_path(path: Path | str) -> StoragePathValidationReport:
    """Inspect one candidate SSOT path without writing to that path."""

    root = Path(path).expanduser()
    try:
        if not root.exists():
            return build_report(None, None, 0, [StorageValidationIssue("ROOT_NOT_FOUND")])
        if not root.is_dir():
            return build_report(None, None, 0, [StorageValidationIssue("ROOT_NOT_DIRECTORY")])
        link_issues = _symlink_issues(root)
        if link_issues:
            return build_report(None, None, 0, link_issues)
        format_version, format_issues = _detect_format(root)
        if format_issues:
            return build_report(format_version, None, 0, format_issues)
        if format_version == 3:
            return _validate_v3(root)
        return _validate_v4(root)
    except OSError:
        return build_report(None, None, 0, [StorageValidationIssue("IO_ERROR")])
