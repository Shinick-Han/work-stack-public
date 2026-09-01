"""Shared, inactive v4 command-backend write support.

This module owns mechanical storage work only.  Command validation, response
projection, opt-in admission, and boundary-specific error types remain in each
repository.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from .idempotency import parse_idempotency_ledger
from .journal import JournalTarget
from .manifest import V4Manifest, build_v4_manifest
from .manifest_store import read_runtime_manifest
from .reader import V4ReadResult, read_v4
from .runtime import RuntimeAuthority
from .write_session import FaultHook, WriteSessionResult, execute_write_session


class V4CommandBackendSupportError(RuntimeError):
    """Content-free storage refusal translated by a command boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VerifiedCommandBaseline:
    physical: V4ReadResult
    ledger: Mapping[str, object]
    ledger_body: bytes
    generation: int


def load_verified_command_baseline(
    authority_root: Path | str,
    runtime: RuntimeAuthority,
) -> VerifiedCommandBaseline:
    """Read one ledger-bound authority and reject a stale runtime manifest."""

    state = read_runtime_manifest(runtime.manifest_path)
    if state is None:
        raise V4CommandBackendSupportError("runtime_manifest_missing")
    try:
        ledger_body = runtime.idempotency_path.read_bytes()
    except OSError as error:
        raise V4CommandBackendSupportError("idempotency_ledger_missing") from error
    ledger = parse_idempotency_ledger(
        ledger_body, expected_workspace_uid=runtime.workspace_uid
    )
    physical = read_v4(authority_root)
    actual = build_v4_manifest(physical, generation=state.generation)
    if actual.digest != state.manifest.digest:
        raise V4CommandBackendSupportError("runtime_manifest_stale")
    return VerifiedCommandBaseline(
        physical, ledger, ledger_body, state.generation
    )


def _apply_authority_target(proposal: Path, target: JournalTarget) -> None:
    if target.scope != "authority":
        return
    path = proposal.joinpath(*target.artifact.split("/"))
    if target.action == "delete":
        path.unlink()
        return
    if target.proposed_bytes is None:
        raise V4CommandBackendSupportError("proposal_content_missing")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(target.proposed_bytes)


@contextmanager
def materialized_authority_proposal(
    authority_root: Path | str,
    proposal_parent: Path | str,
    targets: Sequence[JournalTarget],
    *,
    prefix: str = "command-proposal-",
) -> Iterator[Path]:
    """Yield a delete-aware authority copy and always remove it afterwards."""

    parent = Path(proposal_parent)
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    proposal = temporary / "authority"
    try:
        shutil.copytree(Path(authority_root), proposal)
        for target in targets:
            _apply_authority_target(proposal, target)
        yield proposal
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def build_proposed_manifest(
    authority_root: Path | str,
    proposal_parent: Path | str,
    targets: Sequence[JournalTarget],
    *,
    generation: int,
    prefix: str = "command-proposal-",
) -> V4Manifest:
    with materialized_authority_proposal(
        authority_root, proposal_parent, targets, prefix=prefix
    ) as proposal:
        return build_v4_manifest(read_v4(proposal), generation=generation)


def commit_command_proposal(
    authority_root: Path | str,
    runtime: RuntimeAuthority,
    targets: Sequence[JournalTarget],
    *,
    generation: int,
    operation_id: str,
    created_at: str,
    fault_hook: FaultHook | None = None,
    proposal_prefix: str = "command-proposal-",
    executor: Callable[..., WriteSessionResult] = execute_write_session,
) -> WriteSessionResult:
    """Build and commit one proposal through the existing CAS write session."""

    manifest = build_proposed_manifest(
        authority_root,
        runtime.runtime_root,
        targets,
        generation=generation,
        prefix=proposal_prefix,
    )
    return executor(
        runtime,
        targets,
        manifest,
        operation_id=operation_id,
        created_at=created_at,
        fault_hook=fault_hook,
    )
