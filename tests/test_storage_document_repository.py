from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workstack.storage.document_repository import (
    StoreDocumentRepository,
    WorkspaceDocument,
)


ROOT = Path(__file__).resolve().parents[1]


class _RecordingStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.loads: list[str] = []
        self.saves: list[tuple[str, object]] = []
        self.batches: list[tuple[dict[str, object], str | None]] = []

    def load(self, name: str) -> dict:
        self.loads.append(name)
        return {"document": name}

    def save(self, name: str, value: object) -> None:
        self.saves.append((name, value))

    def save_many(self, writes: dict[str, object], operation_id: str | None = None) -> None:
        self.batches.append((dict(writes), operation_id))

    def path(self, name: str) -> Path:
        return self.root / name


class StoreDocumentRepositoryTests(unittest.TestCase):
    def test_translates_semantic_documents_only_at_the_storage_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _RecordingStore(root)
            repository = StoreDocumentRepository(store)

            self.assertEqual(
                repository.load(WorkspaceDocument.TASKS),
                {"document": "backlog.json"},
            )
            repository.save(WorkspaceDocument.OBJECTIVES, {"objectives": []})
            repository.save_many(
                {
                    WorkspaceDocument.CAPTURES: {"captures": []},
                    WorkspaceDocument.ACTIVITY: {"activity": []},
                },
                operation_id="capture-1",
            )

            self.assertEqual(store.loads, ["backlog.json"])
            self.assertEqual(store.saves, [("okr.json", {"objectives": []})])
            self.assertEqual(
                store.batches,
                [
                    (
                        {
                            "captures.json": {"captures": []},
                            "activity.json": {"activity": []},
                        },
                        "capture-1",
                    )
                ],
            )

    def test_total_bytes_covers_every_released_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _RecordingStore(root)
            repository = StoreDocumentRepository(store)
            sizes = range(1, 9)
            for name, size in zip(
                (
                    "workspace.json",
                    "backlog.json",
                    "activity.json",
                    "okr.json",
                    "worklog.json",
                    "notes.json",
                    "captures.json",
                    "replies.json",
                ),
                sizes,
            ):
                (root / name).write_bytes(b"x" * size)

            self.assertEqual(repository.total_bytes(), sum(sizes))


class ServiceDocumentBoundaryTests(unittest.TestCase):
    def test_service_does_not_name_or_open_physical_documents(self) -> None:
        source = (ROOT / "workstack" / "service.py").read_text(encoding="utf-8")
        for physical_name in (
            "workspace.json",
            "backlog.json",
            "activity.json",
            "okr.json",
            "worklog.json",
            "notes.json",
            "captures.json",
            "replies.json",
        ):
            with self.subTest(physical_name=physical_name):
                self.assertNotIn(physical_name, source)
        for direct_access in (
            "self.store.load(",
            "self.store.save(",
            "self.store.save_many(",
            "self.store.path(",
        ):
            with self.subTest(direct_access=direct_access):
                self.assertNotIn(direct_access, source)


if __name__ == "__main__":
    unittest.main()
