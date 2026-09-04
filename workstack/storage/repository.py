"""Storage-format admission without enabling normalized SSOT writes.

Wave 2 deliberately keeps this boundary smaller than the future application
repository.  Released callers can only admit the existing v3 format.  Tests
may explicitly open a validated v4 reader behind an immutable, read-only
handle; no Store or service object is constructed here.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..store import DEFAULTS, StoreReadiness
from .reader import V4ReadResult
from .semantic import (
    WorkspaceSnapshot,
    semantic_source_from_v4_read,
    snapshot_from_v4,
)


class RepositoryAdmissionError(ValueError):
    """A content-free refusal to admit a storage format."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class V4ReadOnlyStoreAdapter:
    """Legacy-shaped, memory-only Store surface for Wave 2 application reads."""

    def __init__(self, snapshot: WorkspaceSnapshot) -> None:
        self._documents = snapshot.to_v3_documents()
        self.generation = 0
        # The projected read model is already validated, so the adapter
        # publishes the same content-free readiness a legacy Store exposes
        # after initialize(); the shared active readers bind the workspace
        # identity through it (service._workspace_uid).
        self._readiness = StoreReadiness(
            schema_version=3,
            workspace_uid=self._documents["workspace.json"]["id"],
            task_count=len(self._documents["backlog.json"]["tasks"]),
            migration_origin="fresh",
        )

    @property
    def readiness(self) -> StoreReadiness | None:
        return self._readiness

    def initialize(self) -> None:
        return None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def load(self, name: str) -> dict[str, Any]:
        if name not in self._documents:
            raise RepositoryAdmissionError("READ_MODEL_DOCUMENT_UNKNOWN")
        return copy.deepcopy(self._documents[name])

    def save(self, _name: str, _value: object) -> None:
        raise RepositoryAdmissionError("V4_READ_ONLY")

    def save_many(self, _updates: Mapping[str, object]) -> None:
        raise RepositoryAdmissionError("V4_READ_ONLY")


@dataclass(frozen=True)
class V4ReadOnlyRepository:
    """Immutable access to one already validated v4 reader result."""

    root: Path
    result: V4ReadResult
    format_version: int = 4
    read_only: bool = True

    def read(self) -> V4ReadResult:
        """Return the immutable reader result without exposing write methods."""

        return self.result

    def snapshot(
        self,
        *,
        idempotency_records: Sequence[Mapping[str, Any]] = (),
        task_note_source_indexes: Mapping[str, int] | None = None,
    ) -> WorkspaceSnapshot:
        """Project this inactive candidate without enabling mutation or authority."""

        source = semantic_source_from_v4_read(
            self.result,
            idempotency_records=idempotency_records,
            task_note_source_indexes=task_note_source_indexes,
        )
        return snapshot_from_v4(source)

    def legacy_store(
        self,
        *,
        idempotency_records: Sequence[Mapping[str, Any]] = (),
        task_note_source_indexes: Mapping[str, int] | None = None,
    ) -> V4ReadOnlyStoreAdapter:
        """Return a memory-only adapter suitable for test-only application boot."""

        return V4ReadOnlyStoreAdapter(
            self.snapshot(
                idempotency_records=idempotency_records,
                task_note_source_indexes=task_note_source_indexes,
            )
        )


@dataclass(frozen=True)
class RepositoryAdmission:
    """The format decision made at the storage boundary."""

    root: Path
    format_version: int
    mode: str
    repository: V4ReadOnlyRepository | None = None


def _markers(root: Path) -> tuple[bool, bool]:
    has_v4 = (root / "store.json").is_file()
    legacy_names = set(DEFAULTS) - {"workspace.json"}
    has_v3 = any((root / name).exists() for name in legacy_names)
    return has_v3, has_v4


def _classify(root: Path) -> int:
    has_v3, has_v4 = _markers(root)
    if has_v3 and has_v4:
        raise RepositoryAdmissionError("AMBIGUOUS_STORAGE_FORMAT")
    if has_v4:
        return 4
    return 3


def _normalized_root(path: Path | str) -> Path:
    root = Path(path).expanduser()
    if root.exists() and not root.is_dir():
        raise RepositoryAdmissionError("ROOT_DIRECTORY_REQUIRED")
    return root.resolve(strict=False)


def admit_released_repository(path: Path | str) -> RepositoryAdmission:
    """Admit only released v3 behavior, without touching the target path."""

    root = _normalized_root(path)
    format_version = _classify(root)
    if format_version == 4:
        raise RepositoryAdmissionError("V4_NOT_RELEASED")
    return RepositoryAdmission(root, 3, "released-v3")


def admit_test_read_repository(
    path: Path | str,
    *,
    allow_v4: bool = False,
    reader: Callable[[Path], V4ReadResult] | None = None,
) -> RepositoryAdmission:
    """Explicitly admit a v4 fixture for read-only Wave 2 tests.

    The conspicuous API and opt-in flag prevent a future caller from silently
    routing released product traffic to the incomplete v4 backend.
    """

    root = _normalized_root(path)
    format_version = _classify(root)
    if format_version == 3:
        return RepositoryAdmission(root, 3, "test-v3")
    if not allow_v4:
        raise RepositoryAdmissionError("V4_TEST_OPT_IN_REQUIRED")
    if reader is None:
        from .reader import read_v4

        reader = read_v4
    result = reader(root)
    repository = V4ReadOnlyRepository(root, result)
    return RepositoryAdmission(root, 4, "test-v4-read-only", repository)
