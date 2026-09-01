from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workstack.storage.migration_paths import MigrationPathError, plan_migration_paths


DIGEST = "sha256:" + "a" * 64


class MigrationPathTests(unittest.TestCase):
    def test_default_paths_are_deterministic_non_mutating_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "My Workspace"
            source.mkdir()
            before = tuple(source.parent.iterdir())
            first = plan_migration_paths(source, DIGEST)
            second = plan_migration_paths(source, DIGEST)
            self.assertEqual(first, second)
            expected_parent = source.parent.resolve()
            self.assertEqual(first.candidate_root.parent, expected_parent)
            self.assertEqual(first.backup_path.parent, expected_parent)
            self.assertFalse(first.candidate_root.exists())
            self.assertFalse(first.backup_path.exists())
            self.assertEqual(tuple(source.parent.iterdir()), before)

    def test_explicit_relative_sibling_overrides_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            planned = plan_migration_paths(
                source,
                DIGEST,
                candidate_override="review-candidate",
                backup_override="review-backup.zip",
            )
            self.assertEqual(planned.candidate_root.name, "review-candidate")
            self.assertEqual(planned.backup_path.name, "review-backup.zip")

    def test_invalid_digest_escape_collision_and_existing_targets_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            with self.assertRaisesRegex(MigrationPathError, "SOURCE_DIGEST_INVALID"):
                plan_migration_paths(source, "sha256:bad")
            with self.assertRaisesRegex(MigrationPathError, "ARTIFACT_MUST_BE_SOURCE_SIBLING"):
                plan_migration_paths(source, DIGEST, candidate_override=base / "nested" / "candidate")
            with self.assertRaisesRegex(MigrationPathError, "ARTIFACT_PATH_COLLISION"):
                plan_migration_paths(
                    source,
                    DIGEST,
                    candidate_override="same",
                    backup_override="same",
                )
            existing = base / "existing"
            existing.mkdir()
            with self.assertRaisesRegex(MigrationPathError, "CANDIDATE_ALREADY_EXISTS"):
                plan_migration_paths(source, DIGEST, candidate_override=existing)


if __name__ == "__main__":
    unittest.main()
