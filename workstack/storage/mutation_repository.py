"""Explicit, experimental admission for normalized v4 mutation sessions.

Nothing in the released product imports this module.  Callers must provide a
fully resolved local runtime authority and opt in on every admission.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .journal import JournalTarget
from .lease import StorageLeaseError
from .manifest import V4Manifest, V4ManifestError, build_v4_manifest
from .manifest_store import (
    RuntimeManifestError,
    RuntimeManifestState,
    read_runtime_manifest,
)
from .reader import StorageReadError, V4ReadResult, read_v4
from .repository import (
    RepositoryAdmissionError,
    V4ReadOnlyRepository,
    admit_test_read_repository,
)
from .runtime import (
    RuntimeAuthority,
    RuntimeLayoutError,
    resolve_runtime_authority,
)
from .write_session import (
    FaultHook,
    V4WriteSessionError,
    WriteSessionResult,
    execute_write_session,
    recover_write_session,
)


class V4MutationAdmissionError(RuntimeError):
    """A content-free refusal to admit or use an experimental writer."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class V4WritableRepositorySession:
    """A writer bound to one verified runtime manifest generation."""

    format_version = 4
    mode = "experimental-v4-write-session"
    released = False

    def __init__(
        self,
        repository: V4ReadOnlyRepository,
        runtime: RuntimeAuthority,
        state: RuntimeManifestState,
        recovered: WriteSessionResult | None,
    ) -> None:
        self.repository = repository
        self.runtime = runtime
        self._state = state
        self.recovered = recovered

    @property
    def generation(self) -> int:
        return self._state.generation

    @property
    def manifest(self) -> V4Manifest:
        return self._state.manifest

    def commit(
        self,
        targets: Iterable[JournalTarget],
        proposed_manifest: V4Manifest,
        *,
        operation_id: str,
        created_at: str,
        fault_hook: FaultHook | None = None,
    ) -> WriteSessionResult:
        """Commit one generic proposal only while this admission remains fresh."""

        current = _verified_runtime_state(self.runtime)
        if (
            current.generation != self._state.generation
            or current.manifest.digest != self._state.manifest.digest
        ):
            raise V4MutationAdmissionError("MUTATION_SESSION_STALE")
        try:
            result = execute_write_session(
                self.runtime,
                targets,
                proposed_manifest,
                operation_id=operation_id,
                created_at=created_at,
                fault_hook=fault_hook,
            )
        except (StorageLeaseError, V4WriteSessionError) as error:
            code = getattr(error, "code", "WRITE_SESSION_REFUSED")
            raise V4MutationAdmissionError(str(code)) from error
        self._state = RuntimeManifestState(result.manifest, result.generation)
        return result


def _runtime_binding(
    root: Path, runtime: RuntimeAuthority, workspace_uid: str
) -> None:
    if runtime.workspace_uid != workspace_uid:
        raise V4MutationAdmissionError("RUNTIME_WORKSPACE_MISMATCH")
    try:
        expected = resolve_runtime_authority(
            root, runtime.runtime_root.parent, workspace_uid
        )
    except RuntimeLayoutError as error:
        raise V4MutationAdmissionError(error.code) from error
    if runtime != expected:
        raise V4MutationAdmissionError("RUNTIME_AUTHORITY_MISMATCH")


def _read_runtime_state(runtime: RuntimeAuthority) -> RuntimeManifestState:
    try:
        state = read_runtime_manifest(runtime.manifest_path)
    except RuntimeManifestError as error:
        raise V4MutationAdmissionError(error.code) from error
    if state is None:
        raise V4MutationAdmissionError("RUNTIME_MANIFEST_MISSING")
    manifest_workspace = state.manifest.as_dict().get("workspace_uid")
    if manifest_workspace != runtime.workspace_uid:
        raise V4MutationAdmissionError("RUNTIME_MANIFEST_WORKSPACE_MISMATCH")
    return state


def _verified_runtime_state(runtime: RuntimeAuthority) -> RuntimeManifestState:
    return _verified_runtime(runtime)[0]


def _verified_runtime(
    runtime: RuntimeAuthority,
) -> tuple[RuntimeManifestState, V4ReadResult]:
    state = _read_runtime_state(runtime)
    try:
        physical = read_v4(runtime.authority_root)
        actual = build_v4_manifest(physical, generation=state.generation)
    except (OSError, ValueError, StorageReadError, V4ManifestError) as error:
        raise V4MutationAdmissionError("AUTHORITY_VERIFICATION_FAILED") from error
    if actual.digest != state.manifest.digest:
        raise V4MutationAdmissionError("RUNTIME_MANIFEST_STALE")
    return state, physical


def _recover_pending(
    runtime: RuntimeAuthority, fault_hook: FaultHook | None
) -> WriteSessionResult | None:
    if not runtime.journal_path.exists():
        return None
    try:
        return recover_write_session(runtime, fault_hook=fault_hook)
    except (StorageLeaseError, V4WriteSessionError) as error:
        raise V4MutationAdmissionError("PENDING_RECOVERY_REFUSED") from error


def admit_experimental_v4_mutation_repository(
    authority_root: Path | str,
    runtime: RuntimeAuthority | None,
    *,
    allow_v4_mutation: bool = False,
    fault_hook: FaultHook | None = None,
) -> V4WritableRepositorySession:
    """Admit one verified v4 writer without activating released product paths."""

    if not allow_v4_mutation:
        raise V4MutationAdmissionError("V4_MUTATION_OPT_IN_REQUIRED")
    if not isinstance(runtime, RuntimeAuthority):
        raise V4MutationAdmissionError("RUNTIME_AUTHORITY_REQUIRED")
    try:
        admission = admit_test_read_repository(authority_root, allow_v4=True)
    except RepositoryAdmissionError as error:
        raise V4MutationAdmissionError(error.code) from error
    except (OSError, ValueError, StorageReadError) as error:
        raise V4MutationAdmissionError("AUTHORITY_VERIFICATION_FAILED") from error
    if admission.format_version != 4 or admission.repository is None:
        raise V4MutationAdmissionError("V4_AUTHORITY_REQUIRED")
    workspace_uid = str(admission.repository.result.store.get("workspace_uid", ""))
    _runtime_binding(admission.root, runtime, workspace_uid)
    _read_runtime_state(runtime)
    recovered = _recover_pending(runtime, fault_hook)
    state, physical = _verified_runtime(runtime)
    repository = V4ReadOnlyRepository(admission.root, physical)
    return V4WritableRepositorySession(repository, runtime, state, recovered)
