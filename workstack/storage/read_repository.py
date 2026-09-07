"""Backend-neutral, read-only workspace repository contract.

The application-facing value is a semantic :class:`WorkspaceSnapshot`, never a
physical document or record path.  The accompanying stamp binds that snapshot
to the exact authority generation and manifest observed by the adapter.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

from ..store import Store
from .contracts import StorageContractError, require_valid_by_format
from .manifest import build_v4_manifest
from .reader import read_v4
from .read_contract import (
    RepositoryReadError, WorkspaceReadResult, WorkspaceReadStamp, WorkspaceRepository,
)
from .semantic import (
    WorkspaceSnapshot,
    semantic_source_from_v4_read,
    snapshot_from_v4,
)


class V3WorkspaceRepository:
    """Read a genuine nine-document v3 authority under the historical writer lease."""

    format_version = 3

    def __init__(self, store: Store) -> None:
        self._store = store

    def read(self) -> WorkspaceReadResult:
        from .read_v3_snapshot import read_historical_v3

        return read_historical_v3(self._store)


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
