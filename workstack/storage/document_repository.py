"""Semantic document boundary for the released v3 workspace store.

Application services name domain documents, while this adapter alone knows how
those documents are represented by the released physical store.  Keeping the
mapping here lets later storage formats implement the same boundary without
leaking filenames back into domain logic.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Protocol


class WorkspaceDocument(Enum):
    WORKSPACE = "workspace"
    TASKS = "tasks"
    ACTIVITY = "activity"
    OBJECTIVES = "objectives"
    WORKLOG = "worklog"
    NOTES = "notes"
    CAPTURES = "captures"
    REPLIES = "replies"


_PHYSICAL_NAMES = {
    WorkspaceDocument.WORKSPACE: "workspace.json",
    WorkspaceDocument.TASKS: "backlog.json",
    WorkspaceDocument.ACTIVITY: "activity.json",
    WorkspaceDocument.OBJECTIVES: "okr.json",
    WorkspaceDocument.WORKLOG: "worklog.json",
    WorkspaceDocument.NOTES: "notes.json",
    WorkspaceDocument.CAPTURES: "captures.json",
    WorkspaceDocument.REPLIES: "replies.json",
}


class DocumentStore(Protocol):
    """Minimum released-store surface needed by the semantic adapter."""

    def load(self, name: str) -> dict[str, Any]: ...

    def save(self, name: str, value: object) -> None: ...

    def save_many(
        self, writes: Mapping[str, object], operation_id: str | None = None
    ) -> None: ...

    def path(self, name: str) -> Any: ...


class DocumentRepository(Protocol):
    """Application-facing access to workspace documents."""

    def load(self, document: WorkspaceDocument) -> dict[str, Any]: ...

    def save(self, document: WorkspaceDocument, value: object) -> None: ...

    def save_many(
        self,
        writes: Mapping[WorkspaceDocument, object],
        operation_id: str | None = None,
    ) -> None: ...

    def total_bytes(self) -> int: ...


class StoreDocumentRepository:
    """Translate semantic documents to the released store's physical layout."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    def load(self, document: WorkspaceDocument) -> dict[str, Any]:
        return self._store.load(_PHYSICAL_NAMES[document])

    def save(self, document: WorkspaceDocument, value: object) -> None:
        self._store.save(_PHYSICAL_NAMES[document], value)

    def save_many(
        self,
        writes: Mapping[WorkspaceDocument, object],
        operation_id: str | None = None,
    ) -> None:
        physical = {_PHYSICAL_NAMES[document]: value for document, value in writes.items()}
        if operation_id is None:
            self._store.save_many(physical)
            return
        self._store.save_many(physical, operation_id=operation_id)

    def total_bytes(self) -> int:
        return sum(self._store.path(name).stat().st_size for name in _PHYSICAL_NAMES.values())
