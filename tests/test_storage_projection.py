from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.projection import (
    PROJECTION_SCHEMA_VERSION,
    PROJECTION_STATE_NAME,
    ProjectionAuthority,
    ProjectionError,
    admit_projection,
    build_and_publish_projection,
    rebuilding_projection,
)
from workstack.storage.semantic import snapshot_from_v3_documents, snapshot_from_v4


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "store-v3" / "populated"
MANIFEST_DIGEST = "sha256:" + "1" * 64


def _snapshot():
    return snapshot_from_v3_documents(_documents())


def _documents():
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES.glob("*.json"))
    }


def _authority(snapshot, *, generation: int = 7, manifest_digest: str = MANIFEST_DIGEST):
    return ProjectionAuthority(
        workspace_uid=snapshot.to_dict()["workspace"]["id"],
        format_version=4,
        generation=generation,
        manifest_digest=manifest_digest,
        semantic_digest=snapshot.digest,
    )


def _rewrite_state(root: Path, mutate) -> dict:
    path = root / PROJECTION_STATE_NAME
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    path.write_bytes(canonical_json_bytes(value))
    return value


class StorageProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot = _snapshot()
        self.authority = _authority(self.snapshot)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_build_publishes_verified_versioned_database_with_fk_and_counts(self) -> None:
        publication = build_and_publish_projection(
            self.root, self.snapshot, self.authority
        )

        admission = admit_projection(self.root, self.authority)

        self.assertTrue(admission.verified)
        self.assertFalse(admission.canonical_fallback_required)
        self.assertEqual(admission.database_path, publication.database_path)
        self.assertTrue(publication.database_path.name.startswith("index-p1-g7-"))
        self.assertGreater(publication.record_count, 0)
        self.assertGreater(publication.search_count, 0)
        connection = sqlite3.connect(publication.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone(), (1,))
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertIsNone(connection.execute("PRAGMA foreign_key_check").fetchone())
            metadata = connection.execute(
                "SELECT schema_version, authority_generation, authority_manifest_digest, semantic_digest "
                "FROM projection_meta"
            ).fetchone()
            self.assertEqual(
                metadata,
                (
                    PROJECTION_SCHEMA_VERSION,
                    self.authority.generation,
                    self.authority.manifest_digest,
                    self.authority.semantic_digest,
                ),
            )
        finally:
            connection.close()

    def test_missing_projection_and_rebuilding_state_require_canonical_fallback(self) -> None:
        missing = admit_projection(self.root, self.authority)
        rebuilding = rebuilding_projection()

        self.assertEqual(missing.status, "Bypassed")
        self.assertTrue(missing.canonical_fallback_required)
        self.assertEqual(rebuilding.status, "Rebuilding")
        self.assertTrue(rebuilding.canonical_fallback_required)

    def test_generation_and_manifest_digest_are_both_freshness_gates(self) -> None:
        build_and_publish_projection(self.root, self.snapshot, self.authority)

        stale_generation = admit_projection(
            self.root, _authority(self.snapshot, generation=8)
        )
        stale_digest = admit_projection(
            self.root,
            _authority(self.snapshot, manifest_digest="sha256:" + "2" * 64),
        )

        self.assertEqual(stale_generation.reason, "PROJECTION_AUTHORITY_STALE")
        self.assertEqual(stale_digest.reason, "PROJECTION_AUTHORITY_STALE")
        self.assertTrue(stale_generation.canonical_fallback_required)
        self.assertTrue(stale_digest.canonical_fallback_required)

    def test_v3_and_v4_semantic_snapshots_build_equivalent_projection_rows(self) -> None:
        conversion = convert_v3_documents(
            _documents(), candidate_created_at="2026-09-01T12:00:00Z"
        )
        v4_snapshot = snapshot_from_v4(conversion.semantic_source())
        other_root = self.root / "v4-runtime"
        first = build_and_publish_projection(self.root / "v3-runtime", self.snapshot, self.authority)
        second_authority = ProjectionAuthority(
            self.authority.workspace_uid,
            4,
            self.authority.generation,
            "sha256:" + "5" * 64,
            v4_snapshot.digest,
        )
        second = build_and_publish_projection(other_root, v4_snapshot, second_authority)

        def rows(path: Path, table: str):
            connection = sqlite3.connect(path)
            try:
                return connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            finally:
                connection.close()

        for table in ("record_index", "graph_edge", "capture_task", "search_document", "search_term"):
            self.assertEqual(rows(first.database_path, table), rows(second.database_path, table))

    def test_concurrent_reader_keeps_old_version_while_new_pointer_is_published(self) -> None:
        first = build_and_publish_projection(self.root, self.snapshot, self.authority)
        reader = sqlite3.connect(first.database_path)
        try:
            before = reader.execute("SELECT COUNT(*) FROM record_index").fetchone()
            next_authority = _authority(
                self.snapshot,
                generation=self.authority.generation + 1,
                manifest_digest="sha256:" + "6" * 64,
            )
            second = build_and_publish_projection(self.root, self.snapshot, next_authority)
            self.assertNotEqual(first.database_path, second.database_path)
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM record_index").fetchone(), before)
            self.assertTrue(admit_projection(self.root, next_authority).verified)
            self.assertFalse(admit_projection(self.root, self.authority).verified)
        finally:
            reader.close()

    def test_semantic_mismatch_cannot_replace_last_verified_pointer(self) -> None:
        first = build_and_publish_projection(self.root, self.snapshot, self.authority)
        wrong = ProjectionAuthority(
            self.authority.workspace_uid,
            self.authority.format_version,
            self.authority.generation + 1,
            "sha256:" + "3" * 64,
            "sha256:" + "4" * 64,
        )

        with self.assertRaisesRegex(ProjectionError, "PROJECTION_SEMANTIC_DIGEST_MISMATCH"):
            build_and_publish_projection(self.root, self.snapshot, wrong)

        admission = admit_projection(self.root, self.authority)
        self.assertTrue(admission.verified)
        self.assertEqual(admission.database_path, first.database_path)
        self.assertFalse(any(path.name.endswith(".tmp") for path in self.root.iterdir()))

    def test_deleted_partial_or_byte_corrupt_database_is_bypassed(self) -> None:
        publication = build_and_publish_projection(
            self.root, self.snapshot, self.authority
        )
        publication.database_path.unlink()
        deleted = admit_projection(self.root, self.authority)
        self.assertEqual(deleted.reason, "PROJECTION_DATABASE_UNAVAILABLE")

        publication = build_and_publish_projection(
            self.root, self.snapshot, self.authority
        )
        with publication.database_path.open("r+b") as database:
            database.seek(64)
            byte = database.read(1)
            database.seek(64)
            database.write(bytes([byte[0] ^ 0xFF]))
        corrupt = admit_projection(self.root, self.authority)
        self.assertEqual(corrupt.reason, "PROJECTION_DATABASE_DIGEST_MISMATCH")
        self.assertTrue(corrupt.canonical_fallback_required)

    def test_foreign_key_violation_is_cache_loss_not_authority_failure(self) -> None:
        publication = build_and_publish_projection(
            self.root, self.snapshot, self.authority
        )
        connection = sqlite3.connect(publication.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            edge = connection.execute(
                "SELECT source_uid FROM graph_edge LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(edge)
            connection.execute("DELETE FROM record_index WHERE record_uid = ?", edge)
            connection.commit()
        finally:
            connection.close()
        digest = "sha256:" + hashlib.sha256(publication.database_path.read_bytes()).hexdigest()
        _rewrite_state(self.root, lambda value: value.update(database_sha256=digest))

        admission = admit_projection(self.root, self.authority)

        self.assertEqual(admission.status, "Bypassed")
        self.assertEqual(admission.reason, "PROJECTION_DATABASE_INVALID")
        self.assertTrue(admission.canonical_fallback_required)

    def test_future_schema_and_noncanonical_state_are_never_admitted(self) -> None:
        build_and_publish_projection(self.root, self.snapshot, self.authority)
        _rewrite_state(
            self.root,
            lambda value: value.update(
                projection_schema_version=PROJECTION_SCHEMA_VERSION + 1
            ),
        )
        future = admit_projection(self.root, self.authority)
        self.assertEqual(future.reason, "PROJECTION_SCHEMA_UNSUPPORTED")

        state_path = self.root / PROJECTION_STATE_NAME
        value = json.loads(state_path.read_text(encoding="utf-8"))
        state_path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        noncanonical = admit_projection(self.root, self.authority)
        self.assertEqual(noncanonical.reason, "PROJECTION_STATE_INVALID")


if __name__ == "__main__":
    unittest.main()
