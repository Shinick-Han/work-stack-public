"""Writer-lease held admission of a historical v3 collection source.

The v3-to-v4 migration reads a directory that is *not* this build's store: it
holds the nine documents schema 3 released, and it has to keep holding exactly
those, byte for byte, while the sibling v4 candidate is built beside it. Until
schema 5 was activated the migration reached for ``Store.consistent_read`` to
get that, and ``consistent_read`` is two things at once: the real writer lease
over the data directory, and a ready-state check. The ready-state check follows
the running build's ``STORE_SCHEMA_VERSION``, so once that became 5 it began
demanding ``reports.json`` and refusing every genuine v3 source before the
migration read a single byte.

Only the first of those two things belongs to a historical source. This module
keeps it and replaces the second with the version the caller actually claims:

* the exclusive writer lease is the released one, taken through the
  ``Store.try_acquire_writer_lease`` / ``Store.release_writer_lease`` pair, so a
  competing writer is refused rather than raced;
* a pending recovery journal is refused, never replayed. Recovery is a write,
  and the one thing a migration source must survive is being written to;
* the held documents are judged by
  ``Store.validate_document_values(values, schema_version=3)`` -- the accepted
  pure seam, which opens nothing, initializes nothing and never consults
  ``STORE_SCHEMA_VERSION``. The caller hands over the documents it froze, so
  what is admitted is exactly what is converted.

Nothing here initializes a store, and a nine-file directory is never admitted
on its file names alone.

A directory that carries a document belonging to a newer released roster --
today only ``reports.json`` -- is a live store of that newer version rather
than a damaged v3 one, and is refused under the lease with its own code. This
module converts v3, and claims nothing about migrating v5 to v4.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from workstack.store import (
    Store,
    StoreCorruptError,
    StoreLockedError,
    StoreReadiness,
)
from workstack.store_rosters import V3_DOCUMENT_NAMES, V5_DOCUMENT_NAMES


# The schema version a historical source is admitted as. Written here once so
# the roster below and the validation seam cannot drift apart.
HISTORICAL_SCHEMA_VERSION = 3

# What a released roster newer than v3 adds. Presence of any of these names
# identifies the directory as that newer store, not as a broken v3 one.
POST_V3_DOCUMENT_NAMES = frozenset(V5_DOCUMENT_NAMES - V3_DOCUMENT_NAMES)


class V3SourceLeaseError(ValueError):
    """Stable, content-free refusal from historical source admission."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HeldV3Source:
    """One historical directory held under the genuine writer lease."""

    root: Path

    def admit(self, documents: Mapping[str, Mapping[str, Any]]) -> StoreReadiness:
        """Judge the held documents as exactly schema 3, or refuse them.

        The caller passes the documents it already decoded from the frozen
        source, so the bytes that are admitted are the bytes that are
        converted; this opens nothing and writes nothing.
        """

        try:
            return Store.validate_document_values(
                documents, schema_version=HISTORICAL_SCHEMA_VERSION
            )
        except StoreCorruptError as error:
            raise V3SourceLeaseError("SOURCE_NOT_VERSION3") from error


def _refuse_newer_roster(root: Path) -> None:
    """Refuse a directory that is really a store of a newer released version."""

    try:
        present = {entry.name for entry in root.iterdir()}
    except OSError as error:
        raise V3SourceLeaseError("SOURCE_ROSTER_UNREADABLE") from error
    if present & POST_V3_DOCUMENT_NAMES:
        raise V3SourceLeaseError("SOURCE_SCHEMA_NEWER_THAN_V3")


@contextmanager
def hold_v3_source(source_root: Path | str) -> Iterator[HeldV3Source]:
    """Hold the writer lease over one historical v3 directory for the body.

    The lease is acquired once, non-blocking, and released exactly once on the
    way out, so a refused or faulted migration leaves the source free for the
    next attempt.

    An absent directory is refused before the ``Store`` is built, because
    constructing one creates its root: a migration must never bring the source
    it claims to be reading into existence.
    """

    root = Path(source_root).expanduser()
    if not root.is_dir():
        raise V3SourceLeaseError("SOURCE_DIRECTORY_REQUIRED")
    store = Store(root)
    try:
        handle = store.try_acquire_writer_lease()
    except StoreLockedError as error:  # pragma: no cover - fresh Store holds none
        raise V3SourceLeaseError("SOURCE_WRITER_LEASE_UNAVAILABLE") from error
    if handle is None:
        raise V3SourceLeaseError("SOURCE_WRITER_LEASE_UNAVAILABLE")
    try:
        if store.journal_path.exists():
            raise V3SourceLeaseError("SOURCE_RECOVERY_JOURNAL_PENDING")
        _refuse_newer_roster(store.root)
        yield HeldV3Source(root=store.root)
    finally:
        store.release_writer_lease(handle)
