"""The refusal types the released collection store raises.

They live below every validator so the historical readers, the archive
verifier and the store itself all raise one class each. ``workstack.store``
re-exports all three, so `except store.StoreCorruptError` keeps catching
exactly what it caught before this module existed.
"""

from __future__ import annotations

from typing import Any, Mapping


class StoreCorruptError(ValueError):
    """Raised when persisted state cannot be safely interpreted."""


class StoreExternalChangeError(RuntimeError):
    """Raised when an unowned SSOT change freezes normal mutations."""


    def __init__(self, status: Mapping[str, Any]) -> None:
        super().__init__(
            "authoritative store changed outside Work Stack; review synchronization status"
        )
        self.status = dict(status)


class StoreAdoptionConflictError(RuntimeError):
    """Raised when one sync-adoption key is reused for another candidate."""
