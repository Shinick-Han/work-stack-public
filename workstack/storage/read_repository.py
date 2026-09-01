"""Backend-neutral, read-only workspace repository contract.

The application-facing value is a semantic :class:`WorkspaceSnapshot`, never a
physical document or record path.  The accompanying stamp binds that snapshot
to the exact authority generation and manifest observed by the adapter.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from ..store import DEFAULTS, Store
from .contracts import StorageContractError, require_valid_by_format
from .manifest import build_v4_manifest
from .reader import read_v4
from .semantic import (
    WorkspaceSnapshot,
    semantic_source_from_v4_read,
    snapshot_from_v3_documents,
    snapshot_from_v4,
)


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


class V3WorkspaceRepository:
    """Read the legacy nine-document authority through the existing Store lease."""

    format_version = 3

    def __init__(self, store: Store) -> None:
        self._store = store

    def read(self) -> WorkspaceReadResult:
        with self._store.consistent_read() as readiness:
            documents = {name: self._store.load(name) for name in DEFAULTS}
            status = self._store.sync_status()
            snapshot = snapshot_from_v3_documents(documents)
        if status.get("workspace_id") != readiness.workspace_uid:
            raise RepositoryReadError("V3_WORKSPACE_IDENTITY_MISMATCH")
        return WorkspaceReadResult(
            snapshot,
            WorkspaceReadStamp(
                format_version=self.format_version,
                workspace_uid=readiness.workspace_uid,
                generation=status["generation"],
                authority_manifest_digest=status["manifest_digest"],
                snapshot_digest=snapshot.digest,
            ),
        )


def _semantic_idempotency_records(
    ledger: Mapping[str, Any], replies: tuple[Mapping[str, Any], ...]
) -> list[dict[str, Any]]:
    reply_display_ids = {reply["uid"]: reply["display_id"] for reply in replies}
    records: list[dict[str, Any]] = []
    for source in ledger["records"]:
        record = {
            key: copy.deepcopy(value)
            for key, value in source.items()
            if key != "expires_at"
        }
        reference = record.get("response_ref")
        if reference is not None:
            try:
                display_id = reply_display_ids[reference["record_uid"]]
            except (KeyError, TypeError) as error:
                raise RepositoryReadError(
                    "RUNTIME_LEDGER_REPLY_UNRESOLVED"
                ) from error
            record["response_ref"] = {"kind": "reply", "id": display_id}
        records.append(record)
    return records


class V4WorkspaceRepository:
    """Read normalized authority plus its separate runtime replay ledger."""

    format_version = 4

    def __init__(
        self,
        root: Path | str,
        *,
        idempotency_ledger: Mapping[str, Any],
        task_note_source_indexes: Mapping[str, int] | None = None,
        generation: int = 0,
    ) -> None:
        if type(generation) is not int or generation < 0:
            raise RepositoryReadError("GENERATION_INVALID")
        self._root = Path(root).expanduser().resolve(strict=False)
        self._idempotency_ledger = copy.deepcopy(dict(idempotency_ledger))
        self._task_note_source_indexes = copy.deepcopy(task_note_source_indexes)
        self._generation = generation

    def _runtime_idempotency(
        self, workspace_uid: str, replies: tuple[Mapping[str, Any], ...]
    ) -> list[dict[str, Any]]:
        try:
            require_valid_by_format(self._idempotency_ledger)
        except (StorageContractError, TypeError, ValueError) as error:
            raise RepositoryReadError("RUNTIME_LEDGER_INVALID") from error
        if self._idempotency_ledger.get("workspace_uid") != workspace_uid:
            raise RepositoryReadError("RUNTIME_LEDGER_WORKSPACE_MISMATCH")
        return _semantic_idempotency_records(self._idempotency_ledger, replies)

    def read(self) -> WorkspaceReadResult:
        physical = read_v4(self._root)
        manifest = build_v4_manifest(physical, generation=self._generation)
        workspace_uid = str(physical.store["workspace_uid"])
        idempotency = self._runtime_idempotency(
            workspace_uid, physical.records.get("replies", ())
        )
        source = semantic_source_from_v4_read(
            physical,
            idempotency_records=idempotency,
            task_note_source_indexes=self._task_note_source_indexes,
        )
        snapshot = snapshot_from_v4(source)
        return WorkspaceReadResult(
            snapshot,
            WorkspaceReadStamp(
                format_version=self.format_version,
                workspace_uid=workspace_uid,
                generation=self._generation,
                authority_manifest_digest=manifest.digest,
                snapshot_digest=snapshot.digest,
            ),
        )
