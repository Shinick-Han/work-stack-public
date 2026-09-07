"""Honest historical v3 reads under the current schema-5 Store build.

``Store.consistent_read`` and ``Store.sync_status`` judge the running build's
roster, so once that roster gained ``reports.json`` they could no longer open a
genuine nine-file v3 directory. This module keeps the parts of that boundary
that still belong to a historical source — the exclusive writer lease, the
pending-journal refusal, and the committed-manifest coordinates — and replaces
the rest with the version-correct freeze and validation seams the migration
already uses.

Nothing here initializes or upgrades the source, writes a runtime manifest, or
relabels a schema-5 store as format 3.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from workstack.store import STORE_MANIFEST_VERSION, Store, StoreCorruptError, _task_semantics

from .migration_source import (
    FrozenV3Source,
    V3MigrationSourceError,
    V3_SOURCE_FILES,
    freeze_v3_source,
    verify_v3_source_unchanged,
)
from .migration_v3_lease import (
    HISTORICAL_SCHEMA_VERSION,
    HeldV3Source,
    V3SourceLeaseError,
    hold_v3_source,
)
from .read_contract import RepositoryReadError, WorkspaceReadResult, WorkspaceReadStamp
from .semantic import snapshot_from_v3_documents


@contextmanager
def _historical_v3_errors() -> Iterator[None]:
    """Translate historical admission refusals into the read vocabulary."""

    try:
        yield
    except RepositoryReadError:
        raise
    except V3SourceLeaseError as error:
        raise RepositoryReadError(error.code) from error
    except V3MigrationSourceError as error:
        raise RepositoryReadError(error.code) from error
    except StoreCorruptError as error:
        raise RepositoryReadError("SOURCE_NOT_VERSION3") from error


def _documents_from_frozen(frozen: FrozenV3Source) -> dict[str, dict[str, Any]]:
    """Decode the frozen nine-file bytes through Store's shared decoder."""

    bodies = {name: frozen.body(name) for name in V3_SOURCE_FILES}
    try:
        return Store._decoded_documents(bodies)
    except StoreCorruptError as error:
        raise RepositoryReadError("SOURCE_NOT_VERSION3") from error


def _generation0_manifest(
    frozen: FrozenV3Source,
    documents: Mapping[str, Mapping[str, Any]],
    workspace_uid: str,
) -> dict[str, Any]:
    """The manifest a schema-3 Store would first record for these exact bytes.

    Generation 0 is the truthful coordinate when no runtime manifest exists:
    the files are the frozen v3 roster hashes, the tasks are the Store's
    baseline rule, and the digest uses ``Store._manifest_digest``. The adapter
    never writes this object.
    """

    return {
        "version": STORE_MANIFEST_VERSION,
        "workspace_id": workspace_uid,
        "store_schema_version": HISTORICAL_SCHEMA_VERSION,
        "generation": 0,
        "files": {artifact.name: artifact.sha256 for artifact in frozen.artifacts},
        "tasks": _task_semantics(documents["backlog.json"]),
    }


def _existing_v3_manifest(store: Store) -> dict[str, Any] | None:
    """Read the runtime manifest through Store's header/roster seam, or None.

    ``Store.sync_status`` is not used: it validates the current build's roster
    and would write a missing manifest. ``_read_manifest_locked`` only admits
    an existing file against the roster its own schema version implies.
    """

    try:
        return store._read_manifest_locked()
    except StoreCorruptError as error:
        raise RepositoryReadError("V3_RUNTIME_MANIFEST_INVALID") from error


def _schema_refusal(schema: object) -> str:
    if type(schema) is int and schema > HISTORICAL_SCHEMA_VERSION:
        return "SOURCE_SCHEMA_NEWER_THAN_V3"
    return "SOURCE_NOT_VERSION3"


def _bind_v3_stamp(
    store: Store,
    frozen: FrozenV3Source,
    documents: Mapping[str, Mapping[str, Any]],
    workspace_uid: str,
) -> tuple[int, str]:
    """Bind UID/generation/digest to the held bytes, without writing."""

    expected = _generation0_manifest(frozen, documents, workspace_uid)
    recorded = _existing_v3_manifest(store)
    if recorded is None:
        return 0, store._manifest_digest(expected)
    if recorded["store_schema_version"] != HISTORICAL_SCHEMA_VERSION:
        raise RepositoryReadError(_schema_refusal(recorded["store_schema_version"]))
    if recorded["workspace_id"] != workspace_uid:
        raise RepositoryReadError("V3_WORKSPACE_IDENTITY_MISMATCH")
    if recorded["files"] != expected["files"] or recorded["tasks"] != expected["tasks"]:
        raise RepositoryReadError("V3_AUTHORITY_BINDING_MISMATCH")
    return recorded["generation"], store._manifest_digest(recorded)


def _read_held_v3_source(store: Store, held: HeldV3Source) -> WorkspaceReadResult:
    frozen = freeze_v3_source(held.root)
    documents = _documents_from_frozen(frozen)
    readiness = held.admit(documents)
    snapshot = snapshot_from_v3_documents(documents)
    generation, manifest_digest = _bind_v3_stamp(
        store, frozen, documents, readiness.workspace_uid
    )
    verify_v3_source_unchanged(frozen)
    return WorkspaceReadResult(
        snapshot,
        WorkspaceReadStamp(
            format_version=HISTORICAL_SCHEMA_VERSION,
            workspace_uid=readiness.workspace_uid,
            generation=generation,
            authority_manifest_digest=manifest_digest,
            snapshot_digest=snapshot.digest,
        ),
    )


def read_historical_v3(store: Store) -> WorkspaceReadResult:
    """Read one genuine nine-file v3 source under the real writer lease."""

    with _historical_v3_errors():
        with hold_v3_source(store.root) as held:
            return _read_held_v3_source(store, held)
