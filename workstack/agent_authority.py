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
_REPORTS_MARKER: Final[str] = "reports.json"
# The document schema 6 adds. Presence separates a v6 collection store from
# the v5 one it was upgraded from, exactly as reports.json separates v5 from v3.
_KNOWLEDGE_MARKER: Final[str] = "knowledge.json"
_MAX_AUTHORITY_DOCUMENT_BYTES: Final[int] = 64 * 1024
_MARKER_ABSENT: Final[str] = "absent"
_MARKER_FILE: Final[str] = "file"
_MARKER_INVALID: Final[str] = "invalid"
# The label each admitted collection version is reported under. Schema 4 is
# refused in admit_authority, so it never reaches this table.
_COLLECTION_STORAGE_FORMATS: Final[dict[int, str]] = {3: "v3", 5: "v5", 6: "v6"}


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


_WORKSPACE_IDENTITY_KEYS: Final[frozenset[str]] = frozenset({"version", "id", "name"})
_WORKSPACE_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset(
    {"task_display_id_high_water"}
)


def _read_workspace_uid(root: pathlib.Path) -> str:
    doc = _read_document(root / "workspace.json")
    keys = set(doc)
    extra = keys - _WORKSPACE_IDENTITY_KEYS
    if (
        not _WORKSPACE_IDENTITY_KEYS <= keys
        or extra - _WORKSPACE_OPTIONAL_KEYS
        or doc.get("version") != 2
        or not isinstance(doc.get("name"), str)
        or not str(doc["name"]).strip()
    ):
        raise ValueError("invalid_authority")
    workspace_uid = _canonical_uuid(doc.get("id"))
    if workspace_uid is None:
        raise ValueError("invalid_authority")
    return workspace_uid


def _marker_state(path: pathlib.Path) -> str:
    try:
        if path.is_symlink():
            return _MARKER_INVALID
        if path.is_file():
            return _MARKER_FILE
        if path.exists():
            return _MARKER_INVALID
    except OSError:
        return _MARKER_INVALID
    return _MARKER_ABSENT


def _optional_collection_marker_states(root: pathlib.Path) -> tuple[str, ...]:
    states: list[str] = []
    for name in _V3_MARKERS:
        if name == "store-meta.json":
            continue
        states.append(_marker_state(root / name))
    return tuple(states)


def _read_metadata_schema(path: pathlib.Path) -> int | None:
    metadata = _read_document(path)
    if (
        set(metadata) != {"version", "store_schema_version", "migrations"}
        or metadata.get("version") != 2
        or not isinstance(metadata.get("migrations"), dict)
        or type(metadata.get("store_schema_version")) is not int
    ):
        return None
    return int(metadata["store_schema_version"])


def _admitted_collection_schema(
    metadata_schema: int | None,
    reports_state: str,
    knowledge_state: str,
    has_other_v3_marker: bool,
) -> int | None:
    """Which collection version this directory claims *and* carries files for.

    Each version is admitted only when the documents that version introduced
    are present and the ones it never had are absent, so a half-upgraded
    directory is refused rather than read as either neighbour.
    """

    if metadata_schema == 6:
        return (
            6
            if reports_state == _MARKER_FILE and knowledge_state == _MARKER_FILE
            else None
        )
    if metadata_schema == 5:
        return (
            5
            if reports_state == _MARKER_FILE and knowledge_state == _MARKER_ABSENT
            else None
        )
    if metadata_schema == 3 or (metadata_schema is None and has_other_v3_marker):
        return (
            3
            if reports_state == _MARKER_ABSENT and knowledge_state == _MARKER_ABSENT
            else None
        )
    return None


def _detect_format(root: pathlib.Path) -> int | None:
    store_path = root / "store.json"
    metadata_path = root / "store-meta.json"
    reports_path = root / _REPORTS_MARKER
    knowledge_path = root / _KNOWLEDGE_MARKER
    store_state = _marker_state(store_path)
    metadata_state = _marker_state(metadata_path)
    reports_state = _marker_state(reports_path)
    knowledge_state = _marker_state(knowledge_path)
    legacy_states = _optional_collection_marker_states(root)
    if _MARKER_INVALID in (
        store_state,
        metadata_state,
        reports_state,
        knowledge_state,
        *legacy_states,
    ):
        return None

    if store_state == _MARKER_FILE:
        store = _read_document(store_path)
        if store.get("format") != "workstack.ssot" or store.get("schema_version") != 4:
            return None

    metadata_schema: int | None = None
    if metadata_state == _MARKER_FILE:
        metadata_schema = _read_metadata_schema(metadata_path)
        if metadata_schema is None:
            return None

    has_other_v3_marker = _MARKER_FILE in legacy_states
    has_collection_marker = (
        metadata_state == _MARKER_FILE
        or has_other_v3_marker
        or reports_state == _MARKER_FILE
        or knowledge_state == _MARKER_FILE
    )
    if store_state == _MARKER_FILE and has_collection_marker:
        return None
    if store_state == _MARKER_FILE or metadata_schema == 4:
        return 4
    return _admitted_collection_schema(
        metadata_schema, reports_state, knowledge_state, has_other_v3_marker
    )


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
        data_dir=resolved,
        workspace_uid=actual_uid,
        storage_format=_COLLECTION_STORAGE_FORMATS[fmt],
    )
