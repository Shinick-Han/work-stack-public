from __future__ import annotations

import json
import pathlib
import uuid
from typing import Final

import workstack.agent_cli_contract


__all__ = ("admit_authority",)


_V3_MARKERS: Final[tuple[str, ...]] = (
    "backlog.json",
    "store-meta.json",
    "okr.json",
    "worklog.json",
    "notes.json",
    "captures.json",
    "replies.json",
    "activity.json",
)
_MAX_AUTHORITY_DOCUMENT_BYTES: Final[int] = 64 * 1024


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        return None
    if parsed.int == 0 or parsed.variant != uuid.RFC_4122 or str(parsed) != value:
        return None
    return value


def _read_document(path: pathlib.Path) -> dict[str, object]:
    try:
        with path.open("rb") as source:
            body = source.read(_MAX_AUTHORITY_DOCUMENT_BYTES + 1)
        if len(body) > _MAX_AUTHORITY_DOCUMENT_BYTES:
            raise ValueError("invalid_authority")
        doc = json.loads(body.decode("utf-8"))
    except (OSError, RecursionError, ValueError):
        raise ValueError("invalid_authority") from None
    if not isinstance(doc, dict):
        raise ValueError("invalid_authority")
    return doc


def _read_workspace_uid(root: pathlib.Path) -> str:
    doc = _read_document(root / "workspace.json")
    if (
        set(doc) != {"version", "id", "name"}
        or doc.get("version") != 2
        or not isinstance(doc.get("name"), str)
        or not str(doc["name"]).strip()
    ):
        raise ValueError("invalid_authority")
    workspace_uid = _canonical_uuid(doc.get("id"))
    if workspace_uid is None:
        raise ValueError("invalid_authority")
    return workspace_uid


def _detect_format(root: pathlib.Path) -> int | None:
    store_path = root / "store.json"
    metadata_path = root / "store-meta.json"
    has_v4_marker = store_path.exists()
    has_metadata = metadata_path.exists()
    has_other_v3_marker = any(
        (root / name).exists() for name in _V3_MARKERS if name != "store-meta.json"
    )

    if has_v4_marker:
        if not store_path.is_file():
            return None
        store = _read_document(store_path)
        if store.get("format") != "workstack.ssot" or store.get("schema_version") != 4:
            return None

    metadata_schema: int | None = None
    if has_metadata:
        if not metadata_path.is_file():
            return None
        metadata = _read_document(metadata_path)
        if (
            set(metadata) != {"version", "store_schema_version", "migrations"}
            or metadata.get("version") != 2
            or not isinstance(metadata.get("migrations"), dict)
            or type(metadata.get("store_schema_version")) is not int
        ):
            return None
        metadata_schema = int(metadata["store_schema_version"])

    if has_v4_marker and (has_metadata or has_other_v3_marker):
        return None
    if has_v4_marker or metadata_schema == 4:
        return 4
    if metadata_schema not in (None, 3):
        return None
    if metadata_schema == 3 or has_other_v3_marker:
        return 3
    return None


def admit_authority(
    *,
    data_dir: pathlib.Path,
    expected_workspace_uid: str,
) -> workstack.agent_cli_contract.AuthorityAdmission:
    try:
        resolved = data_dir.resolve(strict=False)
        is_existing_directory = resolved.exists() and resolved.is_dir()
    except (OSError, RuntimeError):
        raise ValueError("invalid_authority") from None
    if not is_existing_directory:
        raise ValueError("invalid_authority")

    try:
        fmt = _detect_format(resolved)
    except (OSError, RuntimeError):
        raise ValueError("invalid_authority") from None
    if fmt is None:
        raise ValueError("invalid_authority")
    if fmt == 4:
        raise ValueError("capability_not_enabled")

    actual_uid = _read_workspace_uid(resolved)
    expected = _canonical_uuid(expected_workspace_uid)

    if expected is None or actual_uid != expected:
        raise ValueError("workspace_mismatch")

    return workstack.agent_cli_contract.AuthorityAdmission(
        data_dir=resolved, workspace_uid=actual_uid
    )
