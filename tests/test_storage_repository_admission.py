from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workstack.service import WorkStack
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.repository import (
    RepositoryAdmissionError,
    admit_released_repository,
    admit_test_read_repository,
)


class StorageRepositoryAdmissionTests(unittest.TestCase):
    def test_released_admission_defaults_empty_or_missing_roots_to_v3_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "not-created"

            admission = admit_released_repository(missing)

            self.assertEqual(admission.format_version, 3)
            self.assertEqual(admission.mode, "released-v3")
            self.assertIsNone(admission.repository)
            self.assertFalse(missing.exists())

    def test_released_admission_fails_closed_for_v4_before_calling_a_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "store.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(RepositoryAdmissionError, "V4_NOT_RELEASED"):
                admit_released_repository(root)

    def test_test_admission_requires_an_explicit_v4_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "store.json").write_text("{}", encoding="utf-8")
            calls: list[Path] = []

            with self.assertRaisesRegex(RepositoryAdmissionError, "V4_TEST_OPT_IN_REQUIRED"):
                admit_test_read_repository(root, reader=lambda path: calls.append(path))

            self.assertEqual(calls, [])

    def test_test_admission_keeps_v3_legacy_and_uses_default_v4_reader_only_when_opted_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            v3 = admit_test_read_repository(root)
            self.assertEqual(v3.mode, "test-v3")
            self.assertIsNone(v3.repository)
            store = {
                "format": "workstack.ssot",
                "schema_version": 4,
                "schema_set": "workstack.ssot.v4",
                "workspace_uid": "11111111-1111-1111-1111-111111111111",
                "created_at": "2026-09-01T00:00:00Z",
            }
            workspace = {
                "format": "workstack.workspace",
                "schema_version": 1,
                "workspace_uid": store["workspace_uid"],
                "uid": store["workspace_uid"],
                "revision": 0,
                "created_at": None,
                "updated_at": None,
                "display_id": "work-stack",
                "name": "Work Stack",
            }
            (root / "store.json").write_bytes(canonical_json_bytes(store))
            (root / "workspace.json").write_bytes(canonical_json_bytes(workspace))

            v4 = admit_test_read_repository(root, allow_v4=True)

            self.assertEqual(v4.mode, "test-v4-read-only")
            self.assertEqual(v4.repository.read().workspace_uid, store["workspace_uid"])
            snapshot = v4.repository.snapshot().to_dict()
            self.assertEqual(snapshot["workspace"]["id"], store["workspace_uid"])
            self.assertEqual(snapshot["tasks"], [])
            application = WorkStack(store=v4.repository.legacy_store(), initialize=False)
            self.assertEqual(application.workspace_projection()["tasks"], [])
            self.assertEqual(application.review_projection("2026-09-01")["day"]["entries"], [])
            self.assertEqual(application.search_projection("nothing")["items"], [])
            with self.assertRaisesRegex(RepositoryAdmissionError, "V4_READ_ONLY"):
                application.add_note("must not persist")

    def test_explicit_v4_admission_exposes_only_an_immutable_read_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "store.json").write_text("{}", encoding="utf-8")
            marker = object()

            admission = admit_test_read_repository(
                root,
                allow_v4=True,
                reader=lambda path: marker if path == root.resolve() else None,
            )

            repository = admission.repository
            self.assertIsNotNone(repository)
            assert repository is not None
            self.assertIs(repository.read(), marker)
            self.assertTrue(repository.read_only)
            self.assertFalse(hasattr(repository, "write"))
            self.assertFalse(hasattr(repository, "save"))
            with self.assertRaises((AttributeError, TypeError)):
                repository.read_only = False  # type: ignore[misc]

    def test_ambiguous_v3_v4_markers_are_rejected_without_reading_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "store.json").write_text("{}", encoding="utf-8")
            (root / "backlog.json").write_text(json.dumps({"private": "not echoed"}), encoding="utf-8")

            with self.assertRaisesRegex(RepositoryAdmissionError, "AMBIGUOUS_STORAGE_FORMAT"):
                admit_released_repository(root)

    def test_non_directory_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "authority.json"
            root.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(RepositoryAdmissionError, "ROOT_DIRECTORY_REQUIRED"):
                admit_released_repository(root)


if __name__ == "__main__":
    unittest.main()
