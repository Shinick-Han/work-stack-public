"""Read-only inspection of candidate Work Stack connection profiles.

The functions in this module deliberately do not construct ``Store`` because
its normal constructor creates data and runtime directories.  Local Store
inspection reads the existing authoritative JSON roster into memory, applies
the production validators, and verifies that the files did not change during
the read.  SSH metadata is supplied by an injected fixed-shape runner so this
pure contract never accepts commands, credentials, or arbitrary remote output.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal, Mapping, TypeAlias

from connection_registry import (
    LocalConnectionProfile,
    SshConnectionProfile,
    registry_from_document,
)
from workstack import REMOTE_PROTOCOL_VERSION, __version__
from workstack.planning_status import PlanningStatusValidationError, validate_and_project


MAX_LOCAL_PATH_LENGTH = 4096
MAX_STORE_FILE_BYTES = 64 * 1024 * 1024
MAX_STORE_TOTAL_BYTES = 128 * 1024 * 1024
MAX_PRODUCT_VERSION_LENGTH = 64
MAX_PROTOCOL_VERSION = 1_000_000
MAX_REVISION = 9_007_199_254_740_991
_DISCOVERY_WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
_PATH_SEGMENTS = re.compile(r"[\\/]+")
JOURNAL_NAME = ".workstack-journal.json"
STORE_SCHEMA_VERSION = 3
STORE_FILES = {
    "workspace.json": None,
    "backlog.json": None,
    "store-meta.json": None,
    "okr.json": (1, "objectives", list),
    "worklog.json": (1, "days", dict),
    "notes.json": (1, "notes", list),
    "captures.json": (1, "captures", list),
    "replies.json": (1, "replies", list),
    "activity.json": None,
}

ProfileTestStatus: TypeAlias = Literal["ready", "candidate", "identity_mismatch"]
StorageFormat: TypeAlias = Literal["v3", "v4"]


class ProfileInspectionError(RuntimeError):
    """A bounded, content-free profile inspection failure."""

    def __init__(self, code: str, message: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("profile inspection error code is invalid")
        if not message or len(message) > 256 or any(ord(char) < 32 for char in message):
            raise ValueError("profile inspection error message is invalid")
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(frozen=True)
class LocalProfileTestCandidate:
    profile: LocalConnectionProfile
    expected_workspace_id: str | None


@dataclass(frozen=True)
class SshProfileTestCandidate:
    profile: SshConnectionProfile
    expected_workspace_id: str | None


ProfileTestCandidate: TypeAlias = LocalProfileTestCandidate | SshProfileTestCandidate


@dataclass(frozen=True)
class SshProfileMetadata:
    """Only the safe metadata retained from a fixed-shape SSH profile test."""

    actual_workspace_id: str
    product_version: str
    protocol_version: int
    authority: "AuthorityInspection | None" = None


@dataclass(frozen=True)
class AuthorityCapabilities:
    read: bool
    write: bool
    migrate: bool
    projection: bool


@dataclass(frozen=True)
class AuthorityInspection:
    storage_format: StorageFormat
    schema_version: int
    authority_manifest_digest: str
    capabilities: AuthorityCapabilities


@dataclass(frozen=True)
class ProfileTestResult:
    profile_id: str
    kind: Literal["local", "ssh"]
    status: ProfileTestStatus
    actual_workspace_id: str | None
    product_version: str | None
    protocol_version: int | None
    authority: AuthorityInspection | None = None


SshProfileTester: TypeAlias = Callable[[SshConnectionProfile], SshProfileMetadata]
FormatNeutralLocalInspector: TypeAlias = Callable[[Path], Mapping[str, object]]


def profile_test_candidate_from_document(raw: object) -> ProfileTestCandidate:
    """Validate one strict registry-shaped profile, allowing unknown identity.

    New profiles cannot know ``expected_workspace_id`` until Test has inspected
    the authority.  The normal registry correctly requires that value, so this
    adapter substitutes one non-authoritative sentinel only for schema
    validation and retains ``None`` in the test candidate.
    """

    if not isinstance(raw, dict):
        raise RuntimeError("Test profile must contain one JSON object")
    expected = raw.get("expected_workspace_id")
    if expected is not None and not isinstance(expected, str):
        raise RuntimeError("expected_workspace_id must be null or a canonical UUID")
    enabled = raw.get("enabled")
    if type(enabled) is not bool:
        raise RuntimeError("enabled must be a boolean")
    if raw.get("kind") == "local":
        local_path = raw.get("data_dir")
        if isinstance(local_path, str) and any(
            segment in {".", ".."} for segment in _PATH_SEGMENTS.split(local_path)
        ):
            raise RuntimeError("data_dir must not contain dot path segments")
    profile_document = dict(raw)
    profile_document["enabled"] = True
    profile_document["expected_workspace_id"] = (
        _DISCOVERY_WORKSPACE_ID if expected is None else expected
    )
    profile_id = profile_document.get("profile_id")
    registry = registry_from_document(
        {
            "schema_version": 1,
            "active_profile_id": profile_id,
            "profiles": [profile_document],
        }
    )
    profile = replace(registry.profiles[0], enabled=enabled)
    if isinstance(profile, LocalConnectionProfile):
        return LocalProfileTestCandidate(profile, expected)
    return SshProfileTestCandidate(profile, expected)


def validate_local_directory_path(value: object) -> str:
    """Return one normalized safe local path without creating or resolving it."""

    if not isinstance(value, (str, os.PathLike)):
        raise ProfileInspectionError("invalid_local_path", "The local SSOT path is invalid.")
    raw = os.fspath(value)
    if (
        not raw
        or len(raw) > MAX_LOCAL_PATH_LENGTH
        or any(character in raw for character in ("\x00", "\r", "\n"))
    ):
        raise ProfileInspectionError("invalid_local_path", "The local SSOT path is invalid.")
    windows = raw.replace("/", "\\")
    if windows.startswith("\\\\") or windows.casefold().startswith(("\\\\?\\", "\\\\.\\")):
        raise ProfileInspectionError(
            "unsafe_local_path", "UNC and Windows device paths cannot be used for an SSOT."
        )
    if any(segment in {".", ".."} for segment in _PATH_SEGMENTS.split(raw)):
        raise ProfileInspectionError(
            "unsafe_local_path", "The local SSOT path cannot contain dot segments."
        )
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise ProfileInspectionError(
            "invalid_local_path", "The local SSOT path must be absolute."
        )
    normalized = os.path.normpath(os.path.abspath(expanded))
    path = Path(normalized)
    if path.anchor and path == Path(path.anchor):
        raise ProfileInspectionError(
            "unsafe_local_path", "A filesystem or drive root cannot be used for an SSOT."
        )
    drive, tail = os.path.splitdrive(normalized)
    if ":" in tail or (not drive and normalized.startswith("\\")):
        raise ProfileInspectionError("unsafe_local_path", "The local SSOT path is unsafe.")
    _reject_reparse_components(path)
    return normalized


def inspect_profile(
    candidate: ProfileTestCandidate,
    *,
    ssh_profile_tester: SshProfileTester | None = None,
    enable_format_neutral: bool = False,
    format_neutral_local_inspector: FormatNeutralLocalInspector | None = None,
) -> ProfileTestResult:
    """Inspect one candidate without modifying a local or remote Store."""

    if isinstance(candidate, LocalProfileTestCandidate):
        return _inspect_local_profile(
            candidate,
            enable_format_neutral=enable_format_neutral,
            format_neutral_local_inspector=format_neutral_local_inspector,
        )
    if ssh_profile_tester is None:
        raise ProfileInspectionError(
            "ssh_test_unavailable", "SSH profile testing is not available in this desktop session."
        )
    try:
        metadata = ssh_profile_tester(candidate.profile)
    except ProfileInspectionError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        raise ProfileInspectionError(
            "ssh_test_failed", "The SSH profile could not be verified."
        ) from error
    actual = _canonical_workspace_id(metadata.actual_workspace_id)
    product = _bounded_product_version(metadata.product_version)
    protocol = _bounded_protocol_version(metadata.protocol_version)
    return ProfileTestResult(
        profile_id=candidate.profile.profile_id,
        kind="ssh",
        status=_identity_status(candidate.expected_workspace_id, actual),
        actual_workspace_id=actual,
        product_version=product,
        protocol_version=protocol,
        authority=(
            _validated_authority_inspection(metadata.authority)
            if enable_format_neutral and metadata.authority is not None
            else None
        ),
    )


def profile_test_result_to_document(
    result: ProfileTestResult, *, include_authority: bool = False
) -> dict[str, object]:
    """Validate and return the exact safe bridge representation."""

    if result.kind not in {"local", "ssh"} or result.status not in {
        "ready",
        "candidate",
        "identity_mismatch",
    }:
        raise RuntimeError("Profile test result status is invalid")
    profile_id = _canonical_workspace_id(result.profile_id)
    actual = (
        None
        if result.actual_workspace_id is None
        else _canonical_workspace_id(result.actual_workspace_id)
    )
    product = (
        None
        if result.product_version is None
        else _bounded_product_version(result.product_version)
    )
    protocol = (
        None
        if result.protocol_version is None
        else _bounded_protocol_version(result.protocol_version)
    )
    if result.status == "candidate" and any(
        value is not None for value in (actual, product, protocol)
    ):
        raise RuntimeError("Candidate profile test result cannot claim Store metadata")
    if result.status == "candidate" and result.authority is not None:
        raise RuntimeError("Candidate profile test result cannot claim authority metadata")
    if result.status != "candidate" and None in (actual, product, protocol):
        raise RuntimeError("Verified profile test result is missing Store metadata")
    if include_authority and result.status != "candidate" and result.authority is None:
        raise RuntimeError("Verified profile test result is missing authority metadata")
    document: dict[str, object] = {
        "profile_id": profile_id,
        "kind": result.kind,
        "status": result.status,
        "actual_workspace_id": actual,
        "product_version": product,
        "protocol_version": protocol,
    }
    if include_authority:
        document.update(_authority_document(result.authority))
    return document


def _inspect_local_profile(
    candidate: LocalProfileTestCandidate,
    *,
    enable_format_neutral: bool = False,
    format_neutral_local_inspector: FormatNeutralLocalInspector | None = None,
) -> ProfileTestResult:
    normalized = validate_local_directory_path(candidate.profile.data_dir)
    root = Path(normalized)
    if not root.exists():
        return _candidate_result(candidate.profile.profile_id, "local")
    if not root.is_dir():
        raise ProfileInspectionError(
            "local_path_not_directory", "The selected local SSOT path is not a directory."
        )
    try:
        first_entry = next(root.iterdir(), None)
    except OSError as error:
        raise ProfileInspectionError(
            "local_path_unreadable", "The selected local SSOT directory cannot be read."
        ) from error
    if first_entry is None:
        return _candidate_result(candidate.profile.profile_id, "local")

    if enable_format_neutral and (root / "store.json").is_file():
        legacy = set(STORE_FILES) - {"workspace.json"}
        if any((root / name).exists() for name in legacy):
            raise ProfileInspectionError(
                "mixed_store", "The selected directory mixes incompatible Work Stack formats."
            )
        if format_neutral_local_inspector is None:
            raise ProfileInspectionError(
                "v4_inspection_unavailable",
                "Normalized Store inspection is not enabled in this desktop session.",
            )
        return _inspect_local_v4(candidate, root, format_neutral_local_inspector)

    required = frozenset(STORE_FILES)
    present = {name for name in required if (root / name).is_file()}
    if present != required:
        code = "partial_store" if present else "local_directory_not_empty"
        message = (
            "The selected directory contains only part of a Work Stack Store."
            if present
            else "A new local SSOT requires an empty directory."
        )
        raise ProfileInspectionError(code, message)
    if (root / JOURNAL_NAME).exists():
        raise ProfileInspectionError(
            "store_recovery_required",
            "The selected Store has a pending recovery journal and cannot be activated yet.",
        )

    values, snapshots = _read_store_values(root)
    try:
        workspace_id = _validate_workspace(values["workspace.json"])
        _validate_backlog(values["backlog.json"], workspace_id)
        _validate_store_metadata_document(values["store-meta.json"])
        _validate_auxiliary_store_documents(values)
        _validate_activity(values["activity.json"])
        validate_and_project(values["backlog.json"], values["activity.json"])
    except (PlanningStatusValidationError, KeyError, TypeError, ValueError) as error:
        raise ProfileInspectionError(
            "invalid_store", "The selected directory is not a valid current Work Stack Store."
        ) from error
    _assert_store_unchanged(root, snapshots)
    return ProfileTestResult(
        profile_id=candidate.profile.profile_id,
        kind="local",
        status=_identity_status(candidate.expected_workspace_id, workspace_id),
        actual_workspace_id=workspace_id,
        product_version=_bounded_product_version(__version__),
        protocol_version=_bounded_protocol_version(REMOTE_PROTOCOL_VERSION),
        authority=(
            _v3_authority_inspection(workspace_id, snapshots)
            if enable_format_neutral
            else None
        ),
    )


def _inspect_local_v4(
    candidate: LocalProfileTestCandidate,
    root: Path,
    inspector: FormatNeutralLocalInspector,
) -> ProfileTestResult:
    try:
        raw = inspector(root)
        inspected = _authority_from_local_document(raw)
        workspace_id = _canonical_workspace_id(raw.get("workspace_uid"))
    except ProfileInspectionError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        raise ProfileInspectionError(
            "invalid_store", "The selected directory is not a valid current Work Stack Store."
        ) from error
    return ProfileTestResult(
        profile_id=candidate.profile.profile_id,
        kind="local",
        status=_identity_status(candidate.expected_workspace_id, workspace_id),
        actual_workspace_id=workspace_id,
        product_version=_bounded_product_version(__version__),
        protocol_version=_bounded_protocol_version(REMOTE_PROTOCOL_VERSION),
        authority=inspected,
    )


def _v3_authority_inspection(
    workspace_id: str, snapshots: Mapping[str, tuple[int, int, str]]
) -> AuthorityInspection:
    roster = [
        {"path": name, "sha256": "sha256:" + snapshots[name][2], "size": snapshots[name][0]}
        for name in sorted(snapshots)
    ]
    digest = _canonical_authority_digest({
        "files": roster,
        "format": "workstack.inspected-authority-manifest",
        "schema_version": 1,
        "storage_format": "v3",
        "workspace_uid": workspace_id,
    })
    return AuthorityInspection(
        storage_format="v3",
        schema_version=STORE_SCHEMA_VERSION,
        authority_manifest_digest=digest,
        capabilities=AuthorityCapabilities(
            read=True, write=True, migrate=True, projection=True
        ),
    )


def _canonical_authority_digest(value: Mapping[str, object]) -> str:
    body = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _authority_from_local_document(value: object) -> AuthorityInspection:
    if not isinstance(value, Mapping) or set(value) != {
        "authority_manifest_digest",
        "capabilities",
        "schema_version",
        "storage_format",
        "workspace_uid",
    }:
        raise RuntimeError("Authority inspection response is invalid")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, Mapping) or set(capabilities) != {
        "read", "write", "migrate", "projection"
    }:
        raise RuntimeError("Authority capabilities are invalid")
    inspection = AuthorityInspection(
        storage_format=value.get("storage_format"),  # type: ignore[arg-type]
        schema_version=value.get("schema_version"),  # type: ignore[arg-type]
        authority_manifest_digest=value.get("authority_manifest_digest"),  # type: ignore[arg-type]
        capabilities=AuthorityCapabilities(
            read=capabilities.get("read"),  # type: ignore[arg-type]
            write=capabilities.get("write"),  # type: ignore[arg-type]
            migrate=capabilities.get("migrate"),  # type: ignore[arg-type]
            projection=capabilities.get("projection"),  # type: ignore[arg-type]
        ),
    )
    return _validated_authority_inspection(inspection)


def _validated_authority_inspection(
    value: AuthorityInspection,
) -> AuthorityInspection:
    if not isinstance(value, AuthorityInspection):
        raise RuntimeError("Authority inspection metadata is invalid")
    expected_schema = {"v3": 3, "v4": 4}.get(value.storage_format)
    if expected_schema is None or value.schema_version != expected_schema:
        raise RuntimeError("Authority storage format is invalid")
    if not isinstance(value.authority_manifest_digest, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", value.authority_manifest_digest
    ):
        raise RuntimeError("Authority manifest digest is invalid")
    capabilities = value.capabilities
    if not isinstance(capabilities, AuthorityCapabilities) or any(
        type(item) is not bool
        for item in (
            capabilities.read,
            capabilities.write,
            capabilities.migrate,
            capabilities.projection,
        )
    ):
        raise RuntimeError("Authority capabilities are invalid")
    return value


def _authority_document(value: AuthorityInspection | None) -> dict[str, object]:
    if value is None:
        return {
            "storage_format": None,
            "schema_version": None,
            "authority_manifest_digest": None,
            "capabilities": None,
        }
    inspected = _validated_authority_inspection(value)
    capabilities = inspected.capabilities
    return {
        "storage_format": inspected.storage_format,
        "schema_version": inspected.schema_version,
        "authority_manifest_digest": inspected.authority_manifest_digest,
        "capabilities": {
            "read": capabilities.read,
            "write": capabilities.write,
            "migrate": capabilities.migrate,
            "projection": capabilities.projection,
        },
    }


def _validate_workspace(value: Mapping[str, object]) -> str:
    if set(value) != {"version", "id", "name"} or value.get("version") != 2:
        raise ValueError("workspace identity schema is invalid")
    workspace_id = _canonical_workspace_id(value.get("id"))
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("workspace name is invalid")
    return workspace_id


def _validate_backlog(value: Mapping[str, object], workspace_id: str) -> None:
    if set(value) != {"version", "tasks"} or value.get("version") != 3:
        raise ValueError("backlog identity schema is invalid")
    tasks = value.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("backlog tasks are invalid")
    seen_ids: set[str] = set()
    seen_uids = {workspace_id}
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("backlog task is invalid")
        task_id = task.get("id")
        if (
            not isinstance(task_id, str)
            or not re.fullmatch(r"T-[0-9]{4,}", task_id)
            or task_id in seen_ids
        ):
            raise ValueError("backlog task identity is invalid")
        seen_ids.add(task_id)
        task_uid = _canonical_workspace_id(task.get("uid"))
        if task_uid in seen_uids:
            raise ValueError("backlog task UUID is duplicated")
        seen_uids.add(task_uid)
        revision = task.get("revision")
        if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
            raise ValueError("backlog task revision is invalid")
        status_fact_id = task.get("status_fact_id")
        if not isinstance(status_fact_id, str) or not re.fullmatch(
            r"PS-[0-9]{6,}", status_fact_id
        ):
            raise ValueError("backlog task status fact is invalid")


def _validate_store_metadata_document(value: Mapping[str, object]) -> None:
    if set(value) != {"version", "store_schema_version", "migrations"}:
        raise ValueError("store metadata schema is invalid")
    if value.get("version") != 2 or value.get("store_schema_version") != STORE_SCHEMA_VERSION:
        raise ValueError("store metadata version is invalid")
    migrations = value.get("migrations")
    if not isinstance(migrations, dict) or set(migrations) != {
        "identity",
        "planning_status",
    }:
        raise ValueError("store migration evidence is invalid")
    identity = migrations.get("identity")
    planning = migrations.get("planning_status")
    if not isinstance(identity, dict) or not isinstance(planning, dict):
        raise ValueError("store migration evidence is invalid")
    _validate_migration_evidence(identity, identity=True)
    _validate_migration_evidence(planning, identity=False)


def _validate_migration_evidence(value: Mapping[str, object], *, identity: bool) -> None:
    if set(value) != {"id", "origin", "source_sha256"}:
        raise ValueError("store migration evidence is invalid")
    origin = value.get("origin")
    digest = value.get("source_sha256")
    expected_id = "workstack.store.v2" if identity else "workstack.planning-status.v1"
    migrated_id = "workstack.store.v1-to-v2" if identity else expected_id
    if origin == "fresh":
        if value.get("id") != expected_id or digest is not None:
            raise ValueError("fresh store migration evidence is invalid")
        return
    allowed_origins = {"migrated_v1"} if identity else {"migrated_v1", "migrated_v2"}
    if (
        origin not in allowed_origins
        or value.get("id") != migrated_id
        or not isinstance(digest, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    ):
        raise ValueError("migrated store evidence is invalid")


def _validate_auxiliary_store_documents(
    values: Mapping[str, Mapping[str, object]],
) -> None:
    for name, shape in STORE_FILES.items():
        if shape is None:
            continue
        version, key, container = shape
        value = values[name]
        if (
            set(value) != {"version", key}
            or value.get("version") != version
            or not isinstance(value.get(key), container)
        ):
            raise ValueError("auxiliary Store schema is invalid")


def _validate_activity(value: Mapping[str, object]) -> None:
    if (
        set(value) != {"version", "activity", "idempotency", "planning_status"}
        or value.get("version") != 2
        or not isinstance(value.get("activity"), list)
        or not isinstance(value.get("idempotency"), list)
        or not isinstance(value.get("planning_status"), list)
    ):
        raise ValueError("activity Store schema is invalid")


def _read_store_values(
    root: Path,
) -> tuple[dict[str, dict[str, object]], dict[str, tuple[int, int, str]]]:
    values: dict[str, dict[str, object]] = {}
    snapshots: dict[str, tuple[int, int, str]] = {}
    total = 0
    for name in STORE_FILES:
        path = root / name
        try:
            _reject_reparse_components(path)
            before = path.stat()
            if not path.is_file() or before.st_size > MAX_STORE_FILE_BYTES:
                raise ProfileInspectionError(
                    "invalid_store", "The selected Store contains an invalid authoritative file."
                )
            total += before.st_size
            if total > MAX_STORE_TOTAL_BYTES:
                raise ProfileInspectionError(
                    "store_too_large", "The selected Store is too large to inspect safely."
                )
            payload = path.read_bytes()
            _reject_reparse_components(path)
            after = path.stat()
        except ProfileInspectionError:
            raise
        except OSError as error:
            raise ProfileInspectionError(
                "store_unreadable", "The selected Store could not be read."
            ) from error
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ProfileInspectionError(
                "store_changed", "The selected Store changed during inspection; try again."
            )
        try:
            value = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise ProfileInspectionError(
                "invalid_store", "The selected Store contains invalid JSON."
            ) from error
        if not isinstance(value, dict):
            raise ProfileInspectionError(
                "invalid_store", "The selected Store contains an invalid authoritative file."
            )
        values[name] = value
        snapshots[name] = (
            after.st_size,
            after.st_mtime_ns,
            hashlib.sha256(payload).hexdigest(),
        )
    return values, snapshots


def _assert_store_unchanged(
    root: Path, snapshots: Mapping[str, tuple[int, int, str]]
) -> None:
    try:
        current: dict[str, tuple[int, int, str]] = {}
        total = 0
        for name in STORE_FILES:
            path = root / name
            _reject_reparse_components(path)
            metadata = path.stat()
            if metadata.st_size > MAX_STORE_FILE_BYTES:
                raise ProfileInspectionError(
                    "store_changed", "The selected Store changed during inspection; try again."
                )
            total += metadata.st_size
            if total > MAX_STORE_TOTAL_BYTES:
                raise ProfileInspectionError(
                    "store_changed", "The selected Store changed during inspection; try again."
                )
            with path.open("rb") as stream:
                payload = stream.read(MAX_STORE_FILE_BYTES + 1)
            if len(payload) > MAX_STORE_FILE_BYTES:
                raise ProfileInspectionError(
                    "store_changed", "The selected Store changed during inspection; try again."
                )
            current[name] = (
                metadata.st_size,
                metadata.st_mtime_ns,
                hashlib.sha256(payload).hexdigest(),
            )
    except OSError as error:
        raise ProfileInspectionError(
            "store_changed", "The selected Store changed during inspection; try again."
        ) from error
    if current != dict(snapshots):
        raise ProfileInspectionError(
            "store_changed", "The selected Store changed during inspection; try again."
        )


def _reject_reparse_components(path: Path) -> None:
    chain = [path, *path.parents]
    for candidate in reversed(chain):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ProfileInspectionError(
                "local_path_unreadable", "The selected local SSOT path cannot be inspected."
            ) from error
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if stat.S_ISLNK(metadata.st_mode) or attributes & reparse_flag:
            raise ProfileInspectionError(
                "unsafe_local_path", "Reparse points cannot be used in an SSOT path."
            )


def _candidate_result(profile_id: str, kind: Literal["local", "ssh"]) -> ProfileTestResult:
    return ProfileTestResult(profile_id, kind, "candidate", None, None, None)


def _identity_status(expected: str | None, actual: str) -> ProfileTestStatus:
    return "identity_mismatch" if expected is not None and expected != actual else "ready"


def _canonical_workspace_id(value: object) -> str:
    import uuid

    if not isinstance(value, str):
        raise RuntimeError("Workspace identity is invalid")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise RuntimeError("Workspace identity is invalid") from error
    if parsed.int == 0 or str(parsed) != value or parsed.variant != uuid.RFC_4122:
        raise RuntimeError("Workspace identity is invalid")
    return value


def _bounded_product_version(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_PRODUCT_VERSION_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise RuntimeError("Product version is invalid")
    return value


def _bounded_protocol_version(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_PROTOCOL_VERSION:
        raise RuntimeError("Protocol version is invalid")
    return value
