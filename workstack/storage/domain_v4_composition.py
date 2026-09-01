"""Single default-off composition boundary for the experimental v4 domain."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .capture_reply_repository import V4CaptureReplyRepository
from .idempotency import IdempotencyLedgerError, parse_idempotency_ledger
from .intent_v4_repository import V4IntentRepository
from .manifest import V4ManifestError, build_v4_manifest
from .manifest_store import RuntimeManifestError, read_runtime_manifest
from .mutation_repository import (
    V4MutationAdmissionError,
    V4WritableRepositorySession,
    admit_experimental_v4_mutation_repository,
)
from .objective_v4_repository import V4ObjectiveRepository
from .planning_v4_repository import V4PlanningRepository
from .query_repository import WorkspaceQueryRepository
from .read_repository import V4WorkspaceRepository
from .reader import StorageReadError, read_v4
from .runtime import RuntimeAuthority
from .task_relationship_repository import V4TaskRelationshipRepository
from .task_repository import V4TaskRepository
from .work_session_v4_repository import V4WorkSessionRepository
from .write_session import FaultHook


class V4DomainCompositionError(RuntimeError):
    """Content-free refusal to compose mismatched experimental components."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class V4DomainCoordinate:
    authority_root: Path
    runtime_root: Path
    workspace_uid: str
    authority_key: str
    generation: int
    manifest_digest: str


@dataclass(frozen=True)
class ExperimentalV4Domain:
    """All inactive v4 backends bound to one admitted authority generation."""

    coordinate: V4DomainCoordinate
    admission: V4WritableRepositorySession
    capture_reply: V4CaptureReplyRepository
    intents: V4IntentRepository
    objectives: V4ObjectiveRepository
    tasks: V4TaskRepository
    relationships: V4TaskRelationshipRepository
    planning: V4PlanningRepository
    work_sessions: V4WorkSessionRepository
    query: WorkspaceQueryRepository

    def assert_fresh(self) -> V4DomainCoordinate:
        """Refuse use after either canonical authority or runtime state advances."""

        current = _verified_coordinate(self.admission)
        if current != self.coordinate:
            raise V4DomainCompositionError("V4_DOMAIN_COMPOSITION_STALE")
        return current


def compose_experimental_v4_domain(
    authority_root: Path | str,
    runtime: RuntimeAuthority | None,
    *,
    enable_v4_domain: bool = False,
    clock: Callable[[], str],
    uid_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    today: Callable[[], str] | None = None,
    projection_root: Path | str | None = None,
    task_note_source_indexes: Mapping[str, int] | None = None,
    fault_hook: FaultHook | None = None,
) -> ExperimentalV4Domain:
    """Admit, verify, and compose every v4 domain backend exactly once."""

    if enable_v4_domain is not True:
        raise V4DomainCompositionError("V4_DOMAIN_OPT_IN_REQUIRED")
    try:
        admission = admit_experimental_v4_mutation_repository(
            authority_root,
            runtime,
            allow_v4_mutation=True,
            fault_hook=fault_hook,
        )
    except V4MutationAdmissionError as error:
        raise V4DomainCompositionError(error.code) from error
    coordinate = _verified_coordinate(admission)
    ledger = _read_ledger(admission.runtime)
    root = coordinate.authority_root
    runtime = admission.runtime
    projection = runtime.runtime_root if projection_root is None else projection_root
    domain = ExperimentalV4Domain(
        coordinate=coordinate,
        admission=admission,
        capture_reply=V4CaptureReplyRepository(
            root,
            runtime,
            task_note_source_indexes=task_note_source_indexes,
            clock=clock,
            fault_hook=fault_hook,
            enable_v4_capture_reply_commands=True,
        ),
        intents=V4IntentRepository(
            runtime, enable_v4_intents=True, now=clock, uid_factory=uid_factory
        ),
        objectives=V4ObjectiveRepository(
            runtime, enable_v4_objectives=True, now=clock, uid_factory=uid_factory
        ),
        tasks=V4TaskRepository(
            root,
            runtime,
            task_note_source_indexes=task_note_source_indexes,
            clock=clock,
            fault_hook=fault_hook,
            enable_v4_task_commands=True,
        ),
        relationships=V4TaskRelationshipRepository(admission, clock=clock),
        planning=V4PlanningRepository(
            root,
            runtime,
            task_note_source_indexes=task_note_source_indexes,
            clock=clock,
            enable_v4_planning=True,
        ),
        work_sessions=V4WorkSessionRepository(
            runtime,
            enable_v4_work_sessions=True,
            now=clock,
            today=today,
            uid_factory=uid_factory,
        ),
        query=WorkspaceQueryRepository(
            V4WorkspaceRepository(
                root,
                idempotency_ledger=ledger,
                task_note_source_indexes=task_note_source_indexes,
                generation=coordinate.generation,
            ),
            projection,
        ),
    )
    domain.assert_fresh()
    return domain


def _verified_coordinate(
    admission: V4WritableRepositorySession,
) -> V4DomainCoordinate:
    if not isinstance(admission, V4WritableRepositorySession):
        raise V4DomainCompositionError("V4_DOMAIN_ADMISSION_REQUIRED")
    root = admission.repository.root.resolve(strict=False)
    runtime = admission.runtime
    if root != runtime.authority_root or admission.repository.result.workspace_uid != runtime.workspace_uid:
        raise V4DomainCompositionError("V4_DOMAIN_AUTHORITY_MISMATCH")
    try:
        state = read_runtime_manifest(runtime.manifest_path)
        physical = read_v4(root)
        actual = build_v4_manifest(physical, generation=admission.generation)
    except (OSError, ValueError, RuntimeManifestError, StorageReadError, V4ManifestError) as error:
        raise V4DomainCompositionError("V4_DOMAIN_COORDINATE_INVALID") from error
    if state is None:
        raise V4DomainCompositionError("V4_DOMAIN_RUNTIME_MANIFEST_MISSING")
    if state.generation != admission.generation or state.manifest.digest != admission.manifest.digest:
        raise V4DomainCompositionError("V4_DOMAIN_ADMISSION_STALE")
    if actual.digest != admission.manifest.digest:
        raise V4DomainCompositionError("V4_DOMAIN_AUTHORITY_STALE")
    return V4DomainCoordinate(
        authority_root=root,
        runtime_root=runtime.runtime_root,
        workspace_uid=runtime.workspace_uid,
        authority_key=runtime.authority_key,
        generation=admission.generation,
        manifest_digest=admission.manifest.digest,
    )


def _read_ledger(runtime: RuntimeAuthority) -> dict:
    try:
        body = runtime.idempotency_path.read_bytes()
        return parse_idempotency_ledger(
            body, expected_workspace_uid=runtime.workspace_uid
        )
    except OSError as error:
        raise V4DomainCompositionError("V4_DOMAIN_LEDGER_MISSING") from error
    except IdempotencyLedgerError as error:
        raise V4DomainCompositionError(error.code) from error
