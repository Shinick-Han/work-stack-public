"""Shared immutable read values and protocol, independent of repository adapters."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .semantic import WorkspaceSnapshot


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class RepositoryReadError(ValueError):
    """A content-free refusal to construct a trustworthy repository read."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WorkspaceReadStamp:
    """Authority coordinates to which one semantic snapshot is bound."""

    format_version: int
    workspace_uid: str
    generation: int
    authority_manifest_digest: str
    snapshot_digest: str

    def __post_init__(self) -> None:
        valid = (
            self.format_version in {3, 4}
            and isinstance(self.workspace_uid, str)
            and bool(self.workspace_uid)
            and type(self.generation) is int
            and self.generation >= 0
            and _SHA256.fullmatch(self.authority_manifest_digest) is not None
            and _SHA256.fullmatch(self.snapshot_digest) is not None
        )
        if not valid:
            raise RepositoryReadError("READ_STAMP_INVALID")


@dataclass(frozen=True)
class WorkspaceReadResult:
    """One detached semantic snapshot and its exact authority stamp."""

    snapshot: WorkspaceSnapshot
    stamp: WorkspaceReadStamp


@runtime_checkable
class WorkspaceRepository(Protocol):
    """Small read contract shared by v3 and v4 storage adapters."""

    format_version: int

    def read(self) -> WorkspaceReadResult:
        """Return one consistent semantic snapshot; expose no mutation surface."""
