"""Diagnostic vocabulary and bounded artifact reads for storage validation.

Read-only. Every helper here either names a content-free diagnostic or reads
one candidate artifact under a caller-supplied byte bound. Callers pass the
bound explicitly so the single authority for it stays in ``validation``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import CanonicalJsonError, canonical_json_bytes
from .contracts import StorageContractError, validate_instance


@dataclass(frozen=True)
class StorageValidationIssue:
    """One content-free validation diagnostic."""

    code: str
    artifact: str = ""
    instance_path: str = ""
    keyword: str = ""


@dataclass(frozen=True)
class StoragePathValidationReport:
    """Typed result of inspecting one candidate authority without mutating it."""

    format_version: int | None
    workspace_uid: str | None
    record_count: int
    issues: tuple[StorageValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues


class DuplicateKeyError(ValueError):
    pass


def _issue_key(issue: StorageValidationIssue) -> tuple[str, str, str, str]:
    return (issue.artifact, issue.instance_path, issue.keyword, issue.code)


def build_report(
    format_version: int | None,
    workspace_uid: str | None,
    record_count: int,
    issues: list[StorageValidationIssue],
) -> StoragePathValidationReport:
    return StoragePathValidationReport(
        format_version=format_version,
        workspace_uid=workspace_uid,
        record_count=record_count,
        issues=tuple(sorted(set(issues), key=_issue_key)),
    )


def relative(path: Path, root: Path) -> str:
    if path == root:
        return "."
    return path.relative_to(root).as_posix()


def without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError
        result[key] = value
    return result


def read_json(
    path: Path, root: Path, max_bytes: int
) -> tuple[dict[str, Any] | None, list[StorageValidationIssue]]:
    artifact = relative(path, root)
    if not path.is_file():
        return None, [StorageValidationIssue("REQUIRED_FILE_MISSING", artifact)]
    try:
        with path.open("rb") as stream:
            body = stream.read(max_bytes + 1)
        if len(body) > max_bytes:
            return None, [StorageValidationIssue("JSON_TOO_LARGE", artifact)]
        value = json.loads(body.decode("utf-8", errors="strict"), object_pairs_hook=without_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateKeyError):
        return None, [StorageValidationIssue("INVALID_JSON", artifact)]
    if not isinstance(value, dict):
        return None, [StorageValidationIssue("JSON_OBJECT_REQUIRED", artifact)]
    try:
        canonical = canonical_json_bytes(value)
    except CanonicalJsonError:
        return value, [StorageValidationIssue("CANONICAL_JSON_VIOLATION", artifact)]
    issues = [] if canonical == body else [
        StorageValidationIssue("CANONICAL_JSON_BYTES_MISMATCH", artifact)
    ]
    return value, issues


def schema_issues(
    schema_name: str,
    value: dict[str, Any],
    artifact: str,
) -> list[StorageValidationIssue]:
    try:
        violations = validate_instance(schema_name, value)
    except StorageContractError:
        return [StorageValidationIssue("CONTRACT_UNAVAILABLE", artifact)]
    return [
        StorageValidationIssue(
            "SCHEMA_VIOLATION",
            artifact,
            _content_free_instance_path(violation.instance_path),
            violation.code,
        )
        for violation in violations
    ]


def _content_free_instance_path(path: str) -> str:
    """Redact keys below schema-defined opaque maps from diagnostics."""

    if path.startswith("/details/"):
        return "/details/*"
    return path


def canonical_issues(value: dict[str, Any], artifact: str) -> list[StorageValidationIssue]:
    try:
        canonical_json_bytes(value)
    except CanonicalJsonError:
        return [StorageValidationIssue("CANONICAL_JSON_VIOLATION", artifact)]
    return []


def reference_issue(
    artifact: str,
    instance_path: str,
    uid: Any,
    targets: dict[str, dict[str, Any]],
) -> list[StorageValidationIssue]:
    if isinstance(uid, str) and uid not in targets:
        return [StorageValidationIssue("DANGLING_REFERENCE", artifact, instance_path)]
    return []


def list_reference_issues(
    artifact: str,
    instance_path: str,
    values: Any,
    targets: dict[str, dict[str, Any]],
) -> list[StorageValidationIssue]:
    if not isinstance(values, list):
        return []
    issues: list[StorageValidationIssue] = []
    for index, uid in enumerate(values):
        issues.extend(reference_issue(artifact, f"{instance_path}/{index}", uid, targets))
    return issues
