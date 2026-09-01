"""Opt-in filesystem commit and recovery engine for normalized v4 storage.

The engine consumes complete :class:`JournalTarget` values.  Domain proposal
construction remains above this layer; released repository admission remains
unchanged.  All runtime coordination lives outside the canonical authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .canonical import CanonicalJsonError, canonical_json_bytes
from .contracts import StorageContractError, require_valid_by_format
from .journal import (
    MAX_JOURNAL_BYTES,
    JournalTarget,
    JournalV2Error,
    WriteJournalV2,
    advance_journal_phase,
    build_write_journal,
    parse_write_journal,
)
from .lease import StorageWriterLease
from .manifest import V4Manifest, V4ManifestError, build_v4_manifest
from .manifest_store import (
    RuntimeManifestError,
    RuntimeManifestState,
    publish_runtime_manifest,
    read_runtime_manifest,
)
from .reader import StorageReadError, read_v4
from .runtime import RuntimeAuthority


FaultHook = Callable[[str], None]
_RECORD_ARTIFACT = re.compile(
    r"^records/(captures|notes|objectives|replies|tasks)/"
    r"([0-9a-f]{2})/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})\.json$"
)
_STREAM_ARTIFACT = re.compile(
    r"^streams/(activity|planning-status|worklog)/"
    r"[0-9]{4}-(?:0[1-9]|1[0-2])\.ndjson$"
)
_MAX_HASH_BYTES = 268_435_456


class V4WriteSessionError(RuntimeError):
    """A content-free refusal to commit or recover one v4 generation."""

    def __init__(self, code: str, artifact: str = "") -> None:
        super().__init__(code if not artifact else f"{code}: {artifact}")
        self.code = code
        self.artifact = artifact


@dataclass(frozen=True)
class WriteSessionResult:
    operation_id: str
    generation: int
    manifest: V4Manifest
    recovered: bool


def _signal(hook: FaultHook | None, transition: str) -> None:
    if hook is not None:
        hook(transition)


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    details = path.lstat()
    attributes = getattr(details, "st_file_attributes", 0)
    return stat.S_ISLNK(details.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _file_digest(path: Path) -> str | None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise V4WriteSessionError("TARGET_STAT_FAILED") from error
    if _is_link_or_reparse(path) or not stat.S_ISREG(details.st_mode):
        raise V4WriteSessionError("TARGET_FILE_UNSAFE")
    if details.st_size > _MAX_HASH_BYTES:
        raise V4WriteSessionError("TARGET_BYTE_LIMIT_EXCEEDED")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise V4WriteSessionError("TARGET_READ_FAILED") from error
    try:
        after = path.stat(follow_symlinks=False)
    except OSError as error:
        raise V4WriteSessionError("TARGET_STAT_FAILED") from error
    if (details.st_size, details.st_mtime_ns, getattr(details, "st_ino", 0)) != (
        after.st_size,
        after.st_mtime_ns,
        getattr(after, "st_ino", 0),
    ):
        raise V4WriteSessionError("TARGET_CHANGED_DURING_READ")
    return "sha256:" + digest.hexdigest()


def _validate_authority_artifact(artifact: str) -> None:
    if artifact in {"store.json", "workspace.json"}:
        return
    match = _RECORD_ARTIFACT.fullmatch(artifact)
    if match is not None:
        if match.group(2) != match.group(3)[:2]:
            raise V4WriteSessionError("AUTHORITY_TARGET_INVALID", artifact)
        return
    if _STREAM_ARTIFACT.fullmatch(artifact) is None:
        raise V4WriteSessionError("AUTHORITY_TARGET_INVALID", artifact)


def _validate_runtime_target(runtime: RuntimeAuthority, target: JournalTarget) -> None:
    if target.artifact != runtime.idempotency_path.name:
        raise V4WriteSessionError("RUNTIME_TARGET_INVALID", target.artifact)
    if target.action == "delete":
        return
    if target.proposed_bytes is None:
        raise V4WriteSessionError("RUNTIME_TARGET_CONTRACT_INVALID", target.artifact)
    try:
        value = json.loads(target.proposed_bytes.decode("utf-8", errors="strict"))
        if not isinstance(value, dict) or canonical_json_bytes(value) != target.proposed_bytes:
            raise V4WriteSessionError("RUNTIME_TARGET_CANONICAL_REQUIRED", target.artifact)
        require_valid_by_format(value)
    except V4WriteSessionError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        CanonicalJsonError,
        StorageContractError,
    ) as error:
        raise V4WriteSessionError("RUNTIME_TARGET_CONTRACT_INVALID", target.artifact) from error
    if value.get("workspace_uid") != runtime.workspace_uid:
        raise V4WriteSessionError("RUNTIME_TARGET_WORKSPACE_MISMATCH", target.artifact)


def _target_path(runtime: RuntimeAuthority, target: JournalTarget) -> Path:
    if target.scope == "authority":
        _validate_authority_artifact(target.artifact)
        root = runtime.authority_root
    elif target.scope == "runtime":
        _validate_runtime_target(runtime, target)
        root = runtime.runtime_root
    else:
        raise V4WriteSessionError("TARGET_SCOPE_INVALID", target.artifact)
    path = root.joinpath(*target.artifact.split("/"))
    try:
        if os.path.commonpath((str(root), str(path))) != str(root):
            raise V4WriteSessionError("TARGET_ESCAPES_ROOT", target.artifact)
    except ValueError as error:
        raise V4WriteSessionError("TARGET_ESCAPES_ROOT", target.artifact) from error
    return path


def _manifest_artifacts(manifest: V4Manifest) -> dict[str, str]:
    value = manifest.as_dict()
    metadata = value.get("metadata")
    records = value.get("records")
    streams = value.get("streams")
    if not isinstance(metadata, dict) or not isinstance(records, list) or not isinstance(streams, list):
        raise V4WriteSessionError("MANIFEST_ROSTER_INVALID")
    result = {
        "store.json": str(metadata.get("store_digest")),
        "workspace.json": str(metadata.get("workspace_digest")),
    }
    try:
        for entry in [*records, *streams]:
            artifact = str(entry["artifact"])
            if artifact in result:
                raise V4WriteSessionError("MANIFEST_ROSTER_INVALID")
            result[artifact] = str(entry["digest"])
    except (KeyError, TypeError) as error:
        raise V4WriteSessionError("MANIFEST_ROSTER_INVALID") from error
    return result


def _manifest_generation(manifest: V4Manifest) -> int:
    value = manifest.as_dict().get("generation")
    if type(value) is not int or value < 0:
        raise V4WriteSessionError("MANIFEST_GENERATION_INVALID")
    return value


def _manifest_workspace(manifest: V4Manifest) -> str:
    value = manifest.as_dict().get("workspace_uid")
    if not isinstance(value, str):
        raise V4WriteSessionError("MANIFEST_WORKSPACE_INVALID")
    return value


def _target_id(target: JournalTarget) -> tuple[str, str]:
    return target.scope, target.artifact


def _validate_target_roster(
    runtime: RuntimeAuthority, targets: Sequence[JournalTarget]
) -> None:
    identities = [_target_id(target) for target in targets]
    if not targets or identities != sorted(identities) or len(set(identities)) != len(identities):
        raise V4WriteSessionError("TARGET_ROSTER_INVALID")
    for target in targets:
        _target_path(runtime, target)


def _validate_manifest_transition(
    runtime: RuntimeAuthority,
    base: RuntimeManifestState,
    proposed: V4Manifest,
    targets: Sequence[JournalTarget],
) -> None:
    if _manifest_workspace(base.manifest) != runtime.workspace_uid:
        raise V4WriteSessionError("BASE_WORKSPACE_MISMATCH")
    if _manifest_workspace(proposed) != runtime.workspace_uid:
        raise V4WriteSessionError("PROPOSED_WORKSPACE_MISMATCH")
    if _manifest_generation(proposed) != base.generation + 1:
        raise V4WriteSessionError("GENERATION_TRANSITION_INVALID")
    base_artifacts = _manifest_artifacts(base.manifest)
    proposed_artifacts = _manifest_artifacts(proposed)
    authority_targets = {
        target.artifact: target for target in targets if target.scope == "authority"
    }
    changed = {
        artifact
        for artifact in set(base_artifacts) | set(proposed_artifacts)
        if base_artifacts.get(artifact) != proposed_artifacts.get(artifact)
    }
    if changed != set(authority_targets):
        raise V4WriteSessionError("MANIFEST_TARGET_ROSTER_MISMATCH")
    for artifact, target in authority_targets.items():
        if base_artifacts.get(artifact) != target.expected_digest:
            raise V4WriteSessionError("TARGET_BASE_DIGEST_MISMATCH", artifact)
        if proposed_artifacts.get(artifact) != target.proposed_digest:
            raise V4WriteSessionError("TARGET_PROPOSED_DIGEST_MISMATCH", artifact)


def _actual_manifest(runtime: RuntimeAuthority, generation: int) -> V4Manifest:
    try:
        result = read_v4(runtime.authority_root)
        return build_v4_manifest(result, generation=generation)
    except (OSError, ValueError, StorageReadError, V4ManifestError) as error:
        raise V4WriteSessionError("AUTHORITY_VERIFICATION_FAILED") from error


def _verify_baseline(runtime: RuntimeAuthority) -> RuntimeManifestState:
    try:
        state = read_runtime_manifest(runtime.manifest_path)
    except RuntimeManifestError as error:
        raise V4WriteSessionError(error.code) from error
    if state is None:
        raise V4WriteSessionError("BASE_MANIFEST_MISSING")
    actual = _actual_manifest(runtime, state.generation)
    if actual.digest != state.manifest.digest:
        raise V4WriteSessionError("BASE_MANIFEST_CAS_MISMATCH")
    return state


def _valid_authority_directory(artifact: str) -> bool:
    parts = artifact.split("/") if artifact else []
    if not parts:
        return True
    if parts == ["records"] or parts == ["streams"]:
        return True
    if len(parts) == 2 and parts[0] == "records":
        return parts[1] in {"captures", "notes", "objectives", "replies", "tasks"}
    if len(parts) == 3 and parts[0] == "records":
        return parts[1] in {"captures", "notes", "objectives", "replies", "tasks"} and bool(
            re.fullmatch(r"[0-9a-f]{2}", parts[2])
        )
    return len(parts) == 2 and parts[0] == "streams" and parts[1] in {
        "activity",
        "planning-status",
        "worklog",
    }


def _ignored_stage_paths(
    runtime: RuntimeAuthority, journal: WriteJournalV2
) -> set[Path]:
    operation_id = str(journal.value["operation_id"])
    return {
        _stage_path(_target_path(runtime, target), operation_id, target)
        for target in journal.targets
        if target.scope == "authority"
    }


def _physical_authority_artifacts(
    runtime: RuntimeAuthority, journal: WriteJournalV2
) -> set[str]:
    root = runtime.authority_root
    ignored = _ignored_stage_paths(runtime, journal)
    result: set[str] = set()
    try:
        for directory, names, files in os.walk(root, topdown=True, followlinks=False):
            current = Path(directory)
            relative_directory = current.relative_to(root).as_posix()
            if relative_directory == ".":
                relative_directory = ""
            if not _valid_authority_directory(relative_directory):
                raise V4WriteSessionError("AUTHORITY_LAYOUT_INVALID")
            for name in names:
                child = current / name
                if _is_link_or_reparse(child):
                    raise V4WriteSessionError("AUTHORITY_LAYOUT_INVALID")
            for name in files:
                path = current / name
                if path in ignored:
                    if _is_link_or_reparse(path) or not path.is_file():
                        raise V4WriteSessionError("STAGE_FILE_UNSAFE")
                    continue
                artifact = path.relative_to(root).as_posix()
                _validate_authority_artifact(artifact)
                result.add(artifact)
    except V4WriteSessionError:
        raise
    except (OSError, ValueError) as error:
        raise V4WriteSessionError("AUTHORITY_LAYOUT_INVALID") from error
    return result


def _verify_recovery_state(
    runtime: RuntimeAuthority,
    journal: WriteJournalV2,
    reference: V4Manifest,
) -> None:
    targets = {_target_id(target): target for target in journal.targets}
    reference_artifacts = _manifest_artifacts(reference)
    authority_targets = {
        target.artifact: target for target in journal.targets if target.scope == "authority"
    }
    allowed_artifacts = set(reference_artifacts) | set(authority_targets)
    actual_artifacts = _physical_authority_artifacts(runtime, journal)
    if not actual_artifacts <= allowed_artifacts:
        raise V4WriteSessionError("UNRELATED_ARTIFACT_ADDED")
    for artifact, expected in reference_artifacts.items():
        target = targets.get(("authority", artifact))
        if target is None and _file_digest(runtime.authority_root / artifact) != expected:
            raise V4WriteSessionError("UNRELATED_ARTIFACT_CHANGED", artifact)
    for target in journal.targets:
        current = _file_digest(_target_path(runtime, target))
        if current not in {target.expected_digest, target.proposed_digest}:
            raise V4WriteSessionError("TARGET_STATE_AMBIGUOUS", target.artifact)


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_safe_parent(root: Path, parent: Path) -> None:
    relative = parent.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        try:
            cursor.mkdir()
        except FileExistsError:
            pass
        except OSError as error:
            raise V4WriteSessionError("TARGET_DIRECTORY_CREATE_FAILED") from error
        try:
            if _is_link_or_reparse(cursor) or not cursor.is_dir():
                raise V4WriteSessionError("TARGET_DIRECTORY_UNSAFE")
        except OSError as error:
            raise V4WriteSessionError("TARGET_DIRECTORY_UNSAFE") from error


def _stage_path(path: Path, operation_id: str, target: JournalTarget) -> Path:
    identity = (
        f"{operation_id}\0{target.action}\0{target.scope}\0{target.artifact}"
    ).encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:20]
    return path.with_name(f".{path.name}.workstack-stage-{suffix}")


def _write_stage(path: Path, body: bytes) -> None:
    expected = _digest(body)
    created = False
    try:
        current = _file_digest(path)
        if current is not None:
            if current != expected:
                raise V4WriteSessionError("STAGE_FILE_CONFLICT")
            path.unlink()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(path), flags, 0o600)
        created = True
        try:
            output = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
    except V4WriteSessionError:
        raise
    except OSError as error:
        if created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise V4WriteSessionError("TARGET_STAGE_FAILED") from error
    if _file_digest(path) != expected:
        raise V4WriteSessionError("TARGET_STAGE_VERIFICATION_FAILED")


def _cleanup_stages(stages: Sequence[tuple[JournalTarget, Path, Path]]) -> None:
    for _, _, stage in stages:
        try:
            if stage.is_file() and not _is_link_or_reparse(stage):
                stage.unlink()
        except OSError:
            continue


def _stage_targets(
    runtime: RuntimeAuthority,
    journal: WriteJournalV2,
    hook: FaultHook | None,
) -> list[tuple[JournalTarget, Path, Path]]:
    stages: list[tuple[JournalTarget, Path, Path]] = []
    operation_id = str(journal.value["operation_id"])
    for target in journal.targets:
        path = _target_path(runtime, target)
        if _file_digest(path) == target.proposed_digest:
            continue
        if target.action == "delete":
            continue
        if target.proposed_bytes is None:
            raise V4WriteSessionError("TARGET_CONTENT_MISSING", target.artifact)
        root = runtime.authority_root if target.scope == "authority" else runtime.runtime_root
        _ensure_safe_parent(root, path.parent)
        stage = _stage_path(path, operation_id, target)
        _write_stage(stage, target.proposed_bytes)
        stages.append((target, path, stage))
        _signal(hook, f"target_staged:{target.scope}:{target.artifact}")
    return stages


def _apply_stages(
    runtime: RuntimeAuthority,
    journal: WriteJournalV2,
    reference: V4Manifest,
    stages: Sequence[tuple[JournalTarget, Path, Path]],
    hook: FaultHook | None,
) -> None:
    by_identity = {_target_id(target): (path, stage) for target, path, stage in stages}
    for target in journal.targets:
        current = _file_digest(_target_path(runtime, target))
        if current == target.proposed_digest:
            continue
        if current != target.expected_digest:
            raise V4WriteSessionError("TARGET_CAS_MISMATCH", target.artifact)
        if target.action == "delete":
            path = _target_path(runtime, target)
            try:
                path.unlink()
                _fsync_parent(path.parent)
            except OSError as error:
                raise V4WriteSessionError("TARGET_DELETE_FAILED", target.artifact) from error
            _signal(hook, f"target_deleted:{target.scope}:{target.artifact}")
            _verify_recovery_state(runtime, journal, reference)
            continue
        path, stage = by_identity[_target_id(target)]
        if _file_digest(stage) != target.proposed_digest:
            raise V4WriteSessionError("TARGET_STAGE_VERIFICATION_FAILED", target.artifact)
        try:
            os.replace(str(stage), str(path))
            _fsync_parent(path.parent)
        except OSError as error:
            raise V4WriteSessionError("TARGET_REPLACE_FAILED", target.artifact) from error
        _signal(hook, f"target_replaced:{target.scope}:{target.artifact}")
        _verify_recovery_state(runtime, journal, reference)


def _read_journal(path: Path) -> WriteJournalV2 | None:
    try:
        before = path.stat(follow_symlinks=False)
        if before.st_size > MAX_JOURNAL_BYTES:
            raise V4WriteSessionError("JOURNAL_BYTE_LIMIT_EXCEEDED")
        body = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except V4WriteSessionError:
        raise
    except OSError as error:
        raise V4WriteSessionError("JOURNAL_READ_FAILED") from error
    if (before.st_size, before.st_mtime_ns, getattr(before, "st_ino", 0)) != (
        after.st_size,
        after.st_mtime_ns,
        getattr(after, "st_ino", 0),
    ):
        raise V4WriteSessionError("JOURNAL_CHANGED_DURING_READ")
    try:
        return parse_write_journal(body)
    except JournalV2Error as error:
        raise V4WriteSessionError(error.code) from error


def _publish_journal(
    path: Path,
    journal: WriteJournalV2,
    *,
    expected_digest: str | None,
) -> None:
    current = _file_digest(path)
    if current != expected_digest:
        raise V4WriteSessionError("JOURNAL_CAS_MISMATCH")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".stage")
    created_temporary = False
    try:
        if temporary.exists():
            if expected_digest is None:
                raise V4WriteSessionError("JOURNAL_STAGE_EXISTS")
            if _is_link_or_reparse(temporary) or not temporary.is_file():
                raise V4WriteSessionError("JOURNAL_STAGE_UNSAFE")
            temporary.unlink()
        with temporary.open("xb") as output:
            created_temporary = True
            output.write(journal.canonical_bytes)
            output.flush()
            os.fsync(output.fileno())
        if _file_digest(path) != expected_digest:
            raise V4WriteSessionError("JOURNAL_CAS_MISMATCH")
        os.replace(str(temporary), str(path))
        _fsync_parent(path.parent)
    except V4WriteSessionError:
        raise
    except OSError as error:
        raise V4WriteSessionError("JOURNAL_PUBLISH_FAILED") from error
    finally:
        if created_temporary:
            temporary.unlink(missing_ok=True)
    persisted = _read_journal(path)
    if persisted is None or persisted.digest != journal.digest:
        raise V4WriteSessionError("JOURNAL_PUBLISH_VERIFICATION_FAILED")


def _advance_journal(
    runtime: RuntimeAuthority,
    journal: WriteJournalV2,
    phase: str,
    hook: FaultHook | None,
) -> WriteJournalV2:
    try:
        advanced = advance_journal_phase(journal, phase)
    except JournalV2Error as error:
        raise V4WriteSessionError(error.code) from error
    _publish_journal(runtime.journal_path, advanced, expected_digest=journal.digest)
    _signal(hook, f"journal_{phase}")
    return advanced


def _remove_journal(runtime: RuntimeAuthority, journal: WriteJournalV2) -> None:
    if _file_digest(runtime.journal_path) != journal.digest:
        raise V4WriteSessionError("JOURNAL_CAS_MISMATCH")
    try:
        runtime.journal_path.unlink()
        _fsync_parent(runtime.journal_path.parent)
    except OSError as error:
        raise V4WriteSessionError("JOURNAL_REMOVE_FAILED") from error


@contextmanager
def _writer_lease(runtime: RuntimeAuthority) -> Iterator[None]:
    lease = StorageWriterLease(runtime.runtime_root / "writer.lock")
    lease.acquire()
    try:
        yield
    finally:
        lease.release()


def _publish_manifest(
    runtime: RuntimeAuthority,
    manifest: V4Manifest,
    expected_digest: str,
) -> RuntimeManifestState:
    try:
        return publish_runtime_manifest(
            runtime.manifest_path,
            manifest,
            expected_digest=expected_digest,
        )
    except RuntimeManifestError as error:
        raise V4WriteSessionError(error.code) from error


def _complete_generation(
    runtime: RuntimeAuthority,
    journal: WriteJournalV2,
    manifest: V4Manifest,
    current_manifest_digest: str,
    hook: FaultHook | None,
) -> tuple[WriteJournalV2, RuntimeManifestState]:
    if current_manifest_digest == str(journal.value["base_manifest_digest"]):
        state = _publish_manifest(runtime, manifest, current_manifest_digest)
        _signal(hook, "manifest_published")
    elif current_manifest_digest == manifest.digest:
        state = read_runtime_manifest(runtime.manifest_path)
        if state is None:
            raise V4WriteSessionError("MANIFEST_MISSING_AFTER_PUBLICATION")
    else:
        raise V4WriteSessionError("MANIFEST_STATE_AMBIGUOUS")
    phase = str(journal.value["phase"])
    phases = ("prepared", "applying", "manifest-published", "generation-published")
    if phases.index(phase) < phases.index("manifest-published"):
        journal = _advance_journal(runtime, journal, "manifest-published", hook)
        phase = "manifest-published"
    if phases.index(phase) < phases.index("generation-published"):
        _signal(hook, "generation_published")
        journal = _advance_journal(runtime, journal, "generation-published", hook)
    _remove_journal(runtime, journal)
    _signal(hook, "journal_removed")
    return journal, state


def execute_write_session(
    runtime: RuntimeAuthority,
    targets: Iterable[JournalTarget],
    proposed_manifest: V4Manifest,
    *,
    operation_id: str,
    created_at: str,
    fault_hook: FaultHook | None = None,
) -> WriteSessionResult:
    """Commit one already-validated proposal behind an explicit opt-in call."""

    ordered = tuple(sorted(tuple(targets), key=_target_id))
    with _writer_lease(runtime):
        _signal(fault_hook, "lease_acquired")
        if _read_journal(runtime.journal_path) is not None:
            raise V4WriteSessionError("PENDING_JOURNAL_EXISTS")
        base = _verify_baseline(runtime)
        _signal(fault_hook, "baseline_verified")
        _validate_target_roster(runtime, ordered)
        _validate_manifest_transition(runtime, base, proposed_manifest, ordered)
        try:
            journal = build_write_journal(
                workspace_uid=runtime.workspace_uid,
                operation_id=operation_id,
                created_at=created_at,
                base_generation=base.generation,
                base_manifest_digest=base.manifest.digest,
                proposed_manifest_digest=proposed_manifest.digest,
                targets=ordered,
            )
        except JournalV2Error as error:
            raise V4WriteSessionError(error.code) from error
        _publish_journal(runtime.journal_path, journal, expected_digest=None)
        _signal(fault_hook, "journal_prepared")
        journal = _advance_journal(runtime, journal, "applying", fault_hook)
        stages: list[tuple[JournalTarget, Path, Path]] = []
        try:
            _verify_recovery_state(runtime, journal, base.manifest)
            stages = _stage_targets(runtime, journal, fault_hook)
            _signal(fault_hook, "targets_staged")
            _apply_stages(runtime, journal, base.manifest, stages, fault_hook)
        finally:
            _cleanup_stages(stages)
        actual = _actual_manifest(runtime, base.generation + 1)
        if actual.digest != proposed_manifest.digest:
            raise V4WriteSessionError("PROPOSED_MANIFEST_MISMATCH")
        _signal(fault_hook, "authority_verified")
        journal, state = _complete_generation(
            runtime, journal, actual, base.manifest.digest, fault_hook
        )
        return WriteSessionResult(operation_id, state.generation, state.manifest, False)


def _recover_locked(
    runtime: RuntimeAuthority,
    journal: WriteJournalV2,
    hook: FaultHook | None,
) -> WriteSessionResult:
    if journal.value["workspace_uid"] != runtime.workspace_uid:
        raise V4WriteSessionError("JOURNAL_WORKSPACE_MISMATCH")
    try:
        state = read_runtime_manifest(runtime.manifest_path)
    except RuntimeManifestError as error:
        raise V4WriteSessionError(error.code) from error
    if state is None:
        raise V4WriteSessionError("BASE_MANIFEST_MISSING")
    base_digest = str(journal.value["base_manifest_digest"])
    proposed_digest = str(journal.value["proposed_manifest_digest"])
    if state.manifest.digest not in {base_digest, proposed_digest}:
        raise V4WriteSessionError("MANIFEST_STATE_AMBIGUOUS")
    if state.manifest.digest == proposed_digest:
        actual = _actual_manifest(runtime, int(journal.value["proposed_generation"]))
        if actual.digest != proposed_digest:
            raise V4WriteSessionError("PROPOSED_MANIFEST_MISMATCH")
        journal, published = _complete_generation(
            runtime, journal, actual, proposed_digest, hook
        )
        return WriteSessionResult(
            str(journal.value["operation_id"]),
            published.generation,
            published.manifest,
            True,
        )
    if journal.value["phase"] in {"manifest-published", "generation-published"}:
        raise V4WriteSessionError("JOURNAL_PHASE_MANIFEST_MISMATCH")
    _verify_recovery_state(runtime, journal, state.manifest)
    if journal.value["phase"] == "prepared":
        journal = _advance_journal(runtime, journal, "applying", hook)
    stages: list[tuple[JournalTarget, Path, Path]] = []
    try:
        stages = _stage_targets(runtime, journal, hook)
        _signal(hook, "targets_staged")
        _apply_stages(runtime, journal, state.manifest, stages, hook)
    finally:
        _cleanup_stages(stages)
    actual = _actual_manifest(runtime, int(journal.value["proposed_generation"]))
    if actual.digest != proposed_digest:
        raise V4WriteSessionError("PROPOSED_MANIFEST_MISMATCH")
    _signal(hook, "authority_verified")
    journal, published = _complete_generation(
        runtime, journal, actual, base_digest, hook
    )
    return WriteSessionResult(
        str(journal.value["operation_id"]),
        published.generation,
        published.manifest,
        True,
    )


def recover_write_session(
    runtime: RuntimeAuthority,
    *,
    fault_hook: FaultHook | None = None,
) -> WriteSessionResult | None:
    """Finish one unambiguous pending journal or retain it and fail closed."""

    with _writer_lease(runtime):
        _signal(fault_hook, "lease_acquired")
        journal = _read_journal(runtime.journal_path)
        if journal is None:
            return None
        _validate_target_roster(runtime, journal.targets)
        return _recover_locked(runtime, journal, fault_hook)
