"""Non-destructive orchestration for explicit SSOT v3-to-v4 migrations."""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from workstack.store import Store

from .canonical import canonical_json_bytes, canonical_sha256
from .contracts import require_valid_by_format
from .manifest import V4Manifest, build_v4_manifest
from .migration_conversion import RECORD_KINDS, STREAM_KINDS, V4Conversion, convert_v3_documents
from .migration_paths import MigrationPaths, plan_migration_paths
from .migration_source import (
    FrozenV3Source,
    V3BackupArtifact,
    V3BackupVerification,
    V3SourceLimits,
    create_verified_v3_backup,
    freeze_v3_source,
    verify_v3_backup,
    verify_v3_source_unchanged,
)
from .reader import V4ReadResult, read_v4
from .semantic import semantic_source_from_v4_read, snapshot_from_v4
from .validation import validate_storage_path


MIGRATION_ALGORITHM_VERSION = "workstack.v3-to-v4.v1"
FaultHook = Callable[[str], None]
MAX_MIGRATION_RECEIPT_BYTES = 16 * 1024 * 1024


class StorageMigrationError(ValueError):
    """Stable, content-free refusal from the migration state machine."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MigrationPreview:
    frozen: FrozenV3Source
    paths: MigrationPaths
    conversion: V4Conversion
    candidate_created_at: str

    @property
    def receipt_path(self) -> Path:
        return _receipt_path(
            self.paths.candidate_root,
            _migration_uid(self.conversion, self.candidate_created_at),
        )


@dataclass(frozen=True)
class MigrationExecution:
    preview: MigrationPreview
    backup: V3BackupArtifact | V3BackupVerification
    candidate_manifest: V4Manifest
    receipt_path: Path
    receipt: Mapping[str, Any]


@dataclass(frozen=True)
class MigrationPlan:
    frozen: FrozenV3Source
    paths: MigrationPaths


def _signal(hook: FaultHook | None, state: str) -> None:
    if hook is not None:
        hook(state)


def _receipt_path(candidate_root: Path, migration_uid: str) -> Path:
    return candidate_root / "migrations" / f"{migration_uid}.json"


def _decode_documents(frozen: FrozenV3Source) -> dict[str, Mapping[str, Any]]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise StorageMigrationError("SOURCE_JSON_DUPLICATE_KEY")
            value[key] = item
        return value

    documents: dict[str, Mapping[str, Any]] = {}
    try:
        for artifact in frozen.artifacts:
            value = json.loads(
                frozen.body(artifact.name).decode("utf-8", errors="strict"),
                object_pairs_hook=reject_duplicates,
            )
            if not isinstance(value, dict):
                raise StorageMigrationError("SOURCE_JSON_OBJECT_REQUIRED")
            documents[artifact.name] = value
    except StorageMigrationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StorageMigrationError("SOURCE_JSON_INVALID") from error
    return documents


def plan_v3_migration(
    source_root: Path | str,
    *,
    candidate_override: Path | str | None = None,
    backup_override: Path | str | None = None,
    limits: V3SourceLimits | None = None,
) -> MigrationPlan:
    """Freeze source identity and plan sibling paths without writing artifacts."""

    frozen = freeze_v3_source(source_root, limits=limits)
    paths = plan_migration_paths(
        frozen.root,
        frozen.aggregate_digest,
        candidate_override=candidate_override,
        backup_override=backup_override,
    )
    return MigrationPlan(frozen=frozen, paths=paths)


def preview_v3_migration(
    source_root: Path | str,
    *,
    candidate_created_at: str,
    candidate_override: Path | str | None = None,
    backup_override: Path | str | None = None,
    limits: V3SourceLimits | None = None,
) -> MigrationPreview:
    """Build a deterministic conversion preview without writing any artifact."""

    plan = plan_v3_migration(
        source_root,
        candidate_override=candidate_override,
        backup_override=backup_override,
        limits=limits,
    )
    conversion = convert_v3_documents(
        _decode_documents(plan.frozen), candidate_created_at=candidate_created_at
    )
    return MigrationPreview(
        frozen=plan.frozen,
        paths=plan.paths,
        conversion=conversion,
        candidate_created_at=candidate_created_at,
    )


def _write_file(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with path.open("xb") as target:
            created = True
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
    except FileExistsError as error:
        raise StorageMigrationError("CANDIDATE_ARTIFACT_EXISTS") from error
    except OSError as error:
        if created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise StorageMigrationError("CANDIDATE_WRITE_FAILED") from error


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_file(path, canonical_json_bytes(value))


def _write_conversion(
    root: Path, conversion: V4Conversion, *, mark_owned: Callable[[], None]
) -> None:
    try:
        root.mkdir(parents=False, exist_ok=False)
    except FileExistsError as error:
        raise StorageMigrationError("CANDIDATE_STAGING_EXISTS") from error
    except OSError as error:
        raise StorageMigrationError("CANDIDATE_STAGING_CREATE_FAILED") from error
    mark_owned()
    _write_json(root / "store.json", conversion.store)
    _write_json(root / "workspace.json", conversion.workspace)
    for kind in RECORD_KINDS:
        for record in conversion.records[kind]:
            uid = str(record["uid"])
            _write_json(root / "records" / kind / uid[:2] / f"{uid}.json", record)
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for kind in STREAM_KINDS:
        for event in conversion.streams[kind]:
            segment = str(event["created_at"])[:7]
            grouped.setdefault((kind, segment), []).append(event)
    for (kind, segment), events in sorted(grouped.items()):
        body = b"".join(
            canonical_json_bytes(event) + b"\n"
            for event in sorted(events, key=lambda item: (item["sequence"], item["event_uid"]))
        )
        _write_file(root / "streams" / kind / f"{segment}.ndjson", body)


def _safe_remove_staging(path: Path, parent: Path, marker: str) -> None:
    if not path.exists():
        return
    try:
        details = path.lstat()
    except OSError as error:
        raise StorageMigrationError("STAGING_CLEANUP_REFUSED") from error
    attributes = getattr(details, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(details.st_mode) or bool(attributes & reparse):
        raise StorageMigrationError("STAGING_CLEANUP_REFUSED")
    resolved = path.resolve(strict=True)
    if resolved.parent != parent or marker not in resolved.name or not resolved.is_dir():
        raise StorageMigrationError("STAGING_CLEANUP_REFUSED")
    try:
        shutil.rmtree(resolved)
    except OSError as error:
        raise StorageMigrationError("STAGING_CLEANUP_FAILED") from error


@contextmanager
def _candidate_cleanup(parent: Path, current: Callable[[], Path | None]):
    try:
        yield
    finally:
        staging = current()
        if staging is not None:
            _safe_remove_staging(staging, parent, ".staging-")


def _verify_candidate(
    root: Path, conversion: V4Conversion
) -> tuple[V4ReadResult, V4Manifest]:
    report = validate_storage_path(root)
    if not report.valid or report.format_version != 4:
        raise StorageMigrationError("CANDIDATE_CROSS_INVARIANTS_INVALID")
    result = read_v4(root)
    source = semantic_source_from_v4_read(
        result,
        idempotency_records=conversion.semantic_idempotency_records(),
        task_note_source_indexes=conversion.task_note_source_indexes,
    )
    if snapshot_from_v4(source).digest != conversion.source_snapshot_digest:
        raise StorageMigrationError("CANDIDATE_SEMANTIC_PARITY_MISMATCH")
    return result, build_v4_manifest(result)


def _migration_uid(conversion: V4Conversion, created_at: str) -> str:
    workspace_uid = str(conversion.store["workspace_uid"])
    return str(
        uuid.uuid5(
            uuid.UUID(workspace_uid),
            f"{MIGRATION_ALGORITHM_VERSION}:{conversion.conversion_digest}:{created_at}",
        )
    )


def _artifact_uid(migration_uid: str, kind: str) -> str:
    return str(uuid.uuid5(uuid.UUID(migration_uid), kind))


def _runtime_evidence(conversion: V4Conversion) -> dict[str, Any]:
    return {
        "idempotency_ledger_digest": canonical_sha256(
            conversion.idempotency_ledger
        ),
        "idempotency_record_count": len(conversion.idempotency_ledger["records"]),
    }


def _receipt(
    preview: MigrationPreview,
    backup: V3BackupArtifact | V3BackupVerification,
    result: V4ReadResult,
    manifest: V4Manifest,
    created_at: str,
) -> dict[str, Any]:
    conversion = preview.conversion
    migration_uid = _migration_uid(conversion, created_at)
    receipt = {
        "format": "workstack.migration-receipt",
        "schema_version": 1,
        "workspace_uid": conversion.store["workspace_uid"],
        "migration_uid": migration_uid,
        "algorithm_version": MIGRATION_ALGORITHM_VERSION,
        "created_at": created_at,
        "state": "verified_candidate",
        "source": {
            "format_version": 3,
            "authority_digest": preview.frozen.aggregate_digest,
            "semantic_digest": conversion.source_snapshot_digest,
            "authoritative_file_count": len(preview.frozen.artifacts),
            "byte_count": sum(item.size for item in preview.frozen.artifacts),
            "migration_evidence": conversion.legacy_store_metadata["migrations"],
        },
        "candidate": {
            "format_version": 4,
            "authority_digest": manifest.digest,
            "semantic_digest": conversion.source_snapshot_digest,
            "record_count": result.record_count,
            "stream_event_count": result.event_count,
        },
        "generated_id_roster": list(conversion.generated_id_roster),
        "task_note_source_roster": list(conversion.task_note_source_roster),
        "runtime_evidence": _runtime_evidence(conversion),
        "artifacts": {
            "backup": {
                "artifact_uid": _artifact_uid(migration_uid, "backup"),
                "digest": backup.archive_digest,
                "byte_count": backup.path.stat().st_size,
                "state": "verified",
            },
            "candidate": {
                "artifact_uid": _artifact_uid(migration_uid, "candidate"),
                "digest": manifest.digest,
                "byte_count": sum(item.byte_count for item in result.artifacts),
                "state": "parity_verified",
            },
        },
        "checks": {
            "source_unchanged": True,
            "schemas_valid": True,
            "cross_invariants_valid": True,
            "semantic_parity": True,
        },
    }
    require_valid_by_format(receipt)
    return receipt


UNSUPPORTED_V3_TASK_FIELD = "key_result_refs"


def _refuse_unsupported_task_fields(source: Path) -> None:
    """Refuse known-unconvertible v3 input before any Store or lease side effect.

    The under-lease conversion remains authoritative; this read-only preflight
    only moves the existing refusal ahead of Store construction and does not
    close concurrent-writer races.
    """

    try:
        raw = (source / "backlog.json").read_bytes()
        documents = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    tasks = documents.get("tasks") if isinstance(documents, dict) else None
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if isinstance(task, dict) and UNSUPPORTED_V3_TASK_FIELD in task:
            raise StorageMigrationError("SEMANTIC_PARITY_MISMATCH")


def execute_v3_migration(
    source_root: Path | str,
    *,
    candidate_created_at: str,
    candidate_override: Path | str | None = None,
    backup_override: Path | str | None = None,
    expected_source_digest: str | None = None,
    expected_conversion_digest: str | None = None,
    limits: V3SourceLimits | None = None,
    fault_hook: FaultHook | None = None,
) -> MigrationExecution:
    """Create a verified sibling candidate and backup; never activate either."""

    source = Path(source_root).expanduser().resolve(strict=True)
    _refuse_unsupported_task_fields(source)
    staging: Path | None = None
    staging_owned = [False]
    store = Store(source)
    with store.consistent_read(), _candidate_cleanup(
        source.parent, lambda: staging if staging_owned[0] else None
    ):
        _signal(fault_hook, "lease_acquired")
        preview = preview_v3_migration(
            source,
            candidate_created_at=candidate_created_at,
            candidate_override=candidate_override,
            backup_override=backup_override,
            limits=limits,
        )
        _signal(fault_hook, "source_frozen")
        if (
            expected_source_digest is not None
            and preview.frozen.aggregate_digest != expected_source_digest
        ):
            raise StorageMigrationError("EXPECTED_SOURCE_DIGEST_MISMATCH")
        if (
            expected_conversion_digest is not None
            and preview.conversion.conversion_digest != expected_conversion_digest
        ):
            raise StorageMigrationError("EXPECTED_CONVERSION_DIGEST_MISMATCH")
        migration_uid = _migration_uid(preview.conversion, candidate_created_at)
        backup_staging = preview.paths.backup_path.with_name(
            preview.paths.backup_path.name + f".staging-{migration_uid}"
        )
        backup = create_verified_v3_backup(
            preview.frozen,
            staging_path=backup_staging,
            output_path=preview.paths.backup_path,
            limits=limits,
        )
        _signal(fault_hook, "backup_verified")
        staging = preview.paths.candidate_root.with_name(
            preview.paths.candidate_root.name + f".staging-{migration_uid}"
        )
        _write_conversion(
            staging,
            preview.conversion,
            mark_owned=lambda: staging_owned.__setitem__(0, True),
        )
        _signal(fault_hook, "candidate_written")
        staged_result, staged_manifest = _verify_candidate(staging, preview.conversion)
        _signal(fault_hook, "candidate_verified")
        verify_v3_source_unchanged(preview.frozen, limits=limits)
        _signal(fault_hook, "before_candidate_publish")
        if preview.paths.candidate_root.exists():
            raise StorageMigrationError("CANDIDATE_ALREADY_EXISTS")
        try:
            os.rename(staging, preview.paths.candidate_root)
        except OSError as error:
            raise StorageMigrationError("CANDIDATE_PUBLICATION_FAILED") from error
        staging = None
        staging_owned[0] = False
        published_result, published_manifest = _verify_candidate(
            preview.paths.candidate_root, preview.conversion
        )
        if published_manifest.digest != staged_manifest.digest:
            raise StorageMigrationError("CANDIDATE_PUBLICATION_MISMATCH")
        _signal(fault_hook, "candidate_published")
        verify_v3_source_unchanged(preview.frozen, limits=limits)
        receipt = _receipt(
            preview,
            backup,
            published_result,
            published_manifest,
            candidate_created_at,
        )
        receipt_path = preview.receipt_path
        _write_file(receipt_path, canonical_json_bytes(receipt))
        _, persisted_receipt, _ = _load_receipt(receipt_path)
        if persisted_receipt != receipt:
            raise StorageMigrationError("RECEIPT_PUBLICATION_MISMATCH")
        _signal(fault_hook, "receipt_written")
        verify_v3_source_unchanged(preview.frozen, limits=limits)
        return MigrationExecution(
            preview=preview,
            backup=backup,
            candidate_manifest=published_manifest,
            receipt_path=receipt_path,
            receipt=receipt,
        )


def resume_v3_migration(
    source_root: Path | str,
    *,
    candidate_created_at: str,
    candidate_path: Path | str,
    backup_path: Path | str,
    expected_source_digest: str,
    expected_conversion_digest: str,
    limits: V3SourceLimits | None = None,
) -> MigrationExecution:
    """Finish receipt publication for an already published inactive candidate."""

    source = Path(source_root).expanduser().resolve(strict=True)
    _refuse_unsupported_task_fields(source)
    store = Store(source)
    with store.consistent_read():
        frozen = freeze_v3_source(source, limits=limits)
        conversion = convert_v3_documents(
            _decode_documents(frozen), candidate_created_at=candidate_created_at
        )
        if frozen.aggregate_digest != expected_source_digest:
            raise StorageMigrationError("EXPECTED_SOURCE_DIGEST_MISMATCH")
        if conversion.conversion_digest != expected_conversion_digest:
            raise StorageMigrationError("EXPECTED_CONVERSION_DIGEST_MISMATCH")
        paths = plan_migration_paths(
            source,
            frozen.aggregate_digest,
            candidate_override=candidate_path,
            backup_override=backup_path,
            allow_existing=True,
        )
        preview = MigrationPreview(
            frozen=frozen,
            paths=paths,
            conversion=conversion,
            candidate_created_at=candidate_created_at,
        )
        if preview.receipt_path.exists():
            raise StorageMigrationError("RECEIPT_ALREADY_EXISTS")
        if not paths.candidate_root.is_dir() or not paths.backup_path.is_file():
            raise StorageMigrationError("RESUME_ARTIFACTS_REQUIRED")
        backup = verify_v3_backup(paths.backup_path, limits=limits)
        if backup.aggregate_digest != frozen.aggregate_digest:
            raise StorageMigrationError("BACKUP_SOURCE_DIGEST_MISMATCH")
        result, manifest = _verify_candidate(paths.candidate_root, conversion)
        verify_v3_source_unchanged(frozen, limits=limits)
        receipt = _receipt(
            preview, backup, result, manifest, candidate_created_at
        )
        _write_file(preview.receipt_path, canonical_json_bytes(receipt))
        _, persisted, _ = _load_receipt(preview.receipt_path)
        if persisted != receipt:
            raise StorageMigrationError("RECEIPT_PUBLICATION_MISMATCH")
        verify_v3_source_unchanged(frozen, limits=limits)
        return MigrationExecution(
            preview=preview,
            backup=backup,
            candidate_manifest=manifest,
            receipt_path=preview.receipt_path,
            receipt=receipt,
        )
def _load_receipt(path: Path | str) -> tuple[Path, dict[str, Any], bytes]:
    receipt_path = Path(path).expanduser().resolve(strict=True)
    try:
        details = receipt_path.lstat()
        attributes = getattr(details, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(details.st_mode)
            or bool(attributes & reparse)
            or not stat.S_ISREG(details.st_mode)
            or details.st_size > MAX_MIGRATION_RECEIPT_BYTES
        ):
            raise StorageMigrationError("RECEIPT_FILE_REJECTED")
        with receipt_path.open("rb") as source:
            opened = os.fstat(source.fileno())
            body = source.read(MAX_MIGRATION_RECEIPT_BYTES + 1)
        after = receipt_path.stat(follow_symlinks=False)
        signature = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
        )
        if (
            len(body) > MAX_MIGRATION_RECEIPT_BYTES
            or signature(details) != signature(opened)
            or signature(opened) != signature(after)
        ):
            raise StorageMigrationError("RECEIPT_CHANGED_DURING_READ")
        value = json.loads(body.decode("utf-8", errors="strict"))
    except StorageMigrationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StorageMigrationError("RECEIPT_INVALID") from error
    if not isinstance(value, dict):
        raise StorageMigrationError("RECEIPT_INVALID")
    require_valid_by_format(value)
    if canonical_json_bytes(value) != body:
        raise StorageMigrationError("RECEIPT_NOT_CANONICAL")
    return receipt_path, value, body


def load_migration_receipt(path: Path | str) -> Mapping[str, Any]:
    """Load and contract-validate one canonical content-free receipt."""

    return _load_receipt(path)[1]


def verify_v3_migration(execution: MigrationExecution) -> None:
    """Re-verify a completed, inactive migration from its retained evidence."""

    preview = execution.preview
    backup = verify_v3_backup(execution.backup.path)
    if backup.aggregate_digest != preview.frozen.aggregate_digest:
        raise StorageMigrationError("BACKUP_SOURCE_DIGEST_MISMATCH")
    result, manifest = _verify_candidate(preview.paths.candidate_root, preview.conversion)
    if manifest.digest != execution.candidate_manifest.digest:
        raise StorageMigrationError("CANDIDATE_DIGEST_MISMATCH")
    _, value, _ = _load_receipt(execution.receipt_path)
    if value != execution.receipt:
        raise StorageMigrationError("RECEIPT_MISMATCH")
    if value["candidate"]["authority_digest"] != manifest.digest:
        raise StorageMigrationError("RECEIPT_CANDIDATE_DIGEST_MISMATCH")
    if value["candidate"]["record_count"] != result.record_count:
        raise StorageMigrationError("RECEIPT_CANDIDATE_COUNT_MISMATCH")
    if value["runtime_evidence"] != _runtime_evidence(preview.conversion):
        raise StorageMigrationError("RECEIPT_RUNTIME_EVIDENCE_MISMATCH")
    verify_v3_source_unchanged(preview.frozen)


def verify_v3_migration_artifacts(
    source_root: Path | str,
    *,
    candidate_root: Path | str,
    backup_path: Path | str,
    receipt_path: Path | str,
    limits: V3SourceLimits | None = None,
) -> Mapping[str, Any]:
    """Reconstruct and verify retained migration evidence in a fresh process."""

    _, receipt, _ = _load_receipt(receipt_path)
    frozen = freeze_v3_source(source_root, limits=limits)
    if receipt["source"]["authority_digest"] != frozen.aggregate_digest:
        raise StorageMigrationError("RECEIPT_SOURCE_DIGEST_MISMATCH")
    conversion = convert_v3_documents(
        _decode_documents(frozen), candidate_created_at=receipt["created_at"]
    )
    if receipt["source"]["semantic_digest"] != conversion.source_snapshot_digest:
        raise StorageMigrationError("RECEIPT_SEMANTIC_DIGEST_MISMATCH")
    backup = verify_v3_backup(backup_path, limits=limits)
    if backup.aggregate_digest != frozen.aggregate_digest:
        raise StorageMigrationError("BACKUP_SOURCE_DIGEST_MISMATCH")
    result, manifest = _verify_candidate(Path(candidate_root), conversion)
    preview = MigrationPreview(
        frozen=frozen,
        paths=MigrationPaths(frozen.root, Path(candidate_root), Path(backup_path)),
        conversion=conversion,
        candidate_created_at=receipt["created_at"],
    )
    expected = _receipt(
        preview, backup, result, manifest, receipt["created_at"]
    )
    if receipt != expected:
        raise StorageMigrationError("RECEIPT_EVIDENCE_MISMATCH")
    verify_v3_source_unchanged(frozen, limits=limits)
    return receipt
