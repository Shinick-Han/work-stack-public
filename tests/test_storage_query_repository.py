from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from workstack.store import Store
from workstack.service import WorkStack
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.projection import ProjectionAuthority, build_and_publish_projection
from workstack.storage.query_repository import WorkspaceQueryRepository
from workstack.storage.read_repository import V3WorkspaceRepository, V4WorkspaceRepository


FIXTURE = Path(__file__).parent / "fixtures" / "store-v3" / "populated"


def _documents() -> dict:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in FIXTURE.glob("*.json")
    }


def _write_conversion(root: Path, conversion) -> None:
    def write(path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    write(root / "store.json", canonical_json_bytes(dict(conversion.store)))
    write(root / "workspace.json", canonical_json_bytes(dict(conversion.workspace)))
    for kind, records in conversion.records.items():
        for record in records:
            uid = str(record["uid"])
            write(root / "records" / kind / uid[:2] / f"{uid}.json", canonical_json_bytes(dict(record)))
    grouped = {}
    for kind, events in conversion.streams.items():
        for event in events:
            grouped.setdefault((kind, str(event["created_at"])[:7]), []).append(event)
    for (kind, segment), events in grouped.items():
        write(root / "streams" / kind / f"{segment}.ndjson", b"".join(
            canonical_json_bytes(dict(event)) + b"\n"
            for event in sorted(events, key=lambda item: item["sequence"])
        ))


def _authority(read) -> ProjectionAuthority:
    return ProjectionAuthority(
        read.stamp.workspace_uid, read.stamp.format_version, read.stamp.generation,
        read.stamp.authority_manifest_digest, read.stamp.snapshot_digest,
    )


class StorageQueryRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.v3_root = self.root / "v3"
        shutil.copytree(FIXTURE, self.v3_root)
        self.v3 = V3WorkspaceRepository(Store(self.v3_root))
        self.conversion = convert_v3_documents(
            _documents(), candidate_created_at="2026-09-01T00:00:00Z"
        )
        self.v4_root = self.root / "v4"
        _write_conversion(self.v4_root, self.conversion)
        self.v4 = V4WorkspaceRepository(
            self.v4_root,
            idempotency_ledger=self.conversion.idempotency_ledger,
            task_note_source_indexes=self.conversion.task_note_source_indexes,
            generation=0,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_backend_equivalence(self, repository, projection_root: Path) -> None:
        read = repository.read()
        canonical = WorkspaceQueryRepository(repository, projection_root)
        expected_search = {
            query: canonical.search(query) for query in ("rollback", "T-0001", "outlook")
        }
        expected_graph = canonical.graph()
        self.assertTrue(all(result.read_source == "canonical" for result in expected_search.values()))
        self.assertEqual(expected_graph.read_source, "canonical")

        build_and_publish_projection(projection_root, read.snapshot, _authority(read))
        projected = WorkspaceQueryRepository(repository, projection_root)
        for query, fallback in expected_search.items():
            result = projected.search(query)
            self.assertEqual(result.read_source, "projection")
            self.assertEqual(result.hits, fallback.hits)
        graph = projected.graph()
        self.assertEqual(graph.read_source, "projection")
        self.assertEqual(graph.edges, expected_graph.edges)

    def test_v3_projection_and_canonical_fallback_are_equivalent(self) -> None:
        self.assert_backend_equivalence(self.v3, self.root / "v3-projection")

    def test_v4_projection_and_canonical_fallback_are_equivalent(self) -> None:
        self.assert_backend_equivalence(self.v4, self.root / "v4-projection")

    def test_v3_and_v4_queries_are_backend_neutral(self) -> None:
        v3_query = WorkspaceQueryRepository(self.v3, self.root / "missing-v3")
        v4_query = WorkspaceQueryRepository(self.v4, self.root / "missing-v4")
        for query in ("rollback", "T-0001", "outlook"):
            self.assertEqual(v3_query.search(query).hits, v4_query.search(query).hits)
        self.assertEqual(v3_query.graph().edges, v4_query.graph().edges)

    def test_query_contract_reproduces_released_v3_search_and_graph(self) -> None:
        """Freeze the full public search item and relationship shapes."""

        stack = WorkStack(Store(self.v3_root))
        projection_root = self.root / "released-mapping"
        query = WorkspaceQueryRepository(self.v3, projection_root)
        for needle in ("rollback", "T-0001", "outlook"):
            released = stack.search_projection(needle, 30)
            self.assertEqual(
                query.search(needle, limit=30).to_released_projection(),
                released,
            )

        released_edges = [
            (edge["kind"], edge["source"], edge["target"])
            for edge in stack.workspace_projection()["edges"]
        ]
        self.assertEqual(query.graph().edges, tuple(sorted(released_edges)))
        self.assertTrue(any(edge[0] == "worklog" for edge in released_edges))
        self.assertTrue(any(edge[0] == "parent" and "-S-" in edge[1] for edge in released_edges))

        read = self.v3.read()
        build_and_publish_projection(projection_root, read.snapshot, _authority(read))
        for needle in ("rollback", "T-0001", "outlook"):
            self.assertEqual(
                query.search(needle, limit=30).to_released_projection(),
                stack.search_projection(needle, 30),
            )
        self.assertEqual(query.graph().edges, tuple(sorted(released_edges)))

    def test_stale_projection_is_bypassed_for_equivalent_canonical_results(self) -> None:
        projection_root = self.root / "stale"
        read = self.v4.read()
        build_and_publish_projection(projection_root, read.snapshot, _authority(read))
        query = WorkspaceQueryRepository(self.v4, projection_root)
        projected_search = query.search("rollback")
        projected_graph = query.graph()
        state_path = projection_root / "projection-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["authority_generation"] += 1
        state_path.write_bytes(canonical_json_bytes(state))

        fallback_search = query.search("rollback")
        fallback_graph = query.graph()
        self.assertEqual(fallback_search.read_source, "canonical")
        self.assertEqual(fallback_search.projection_reason, "PROJECTION_AUTHORITY_STALE")
        self.assertEqual(fallback_search.hits, projected_search.hits)
        self.assertEqual(fallback_graph.edges, projected_graph.edges)

    def test_corrupt_projection_never_exposes_partial_search_or_graph(self) -> None:
        projection_root = self.root / "corrupt"
        read = self.v3.read()
        publication = build_and_publish_projection(
            projection_root, read.snapshot, _authority(read)
        )
        query = WorkspaceQueryRepository(self.v3, projection_root)
        expected_search = query.search("rollback")
        expected_graph = query.graph()
        with publication.database_path.open("r+b") as target:
            target.seek(80)
            original = target.read(1)
            target.seek(80)
            target.write(bytes([original[0] ^ 0xFF]))

        fallback_search = query.search("rollback")
        fallback_graph = query.graph()
        self.assertEqual(fallback_search.read_source, "canonical")
        self.assertEqual(fallback_search.hits, expected_search.hits)
        self.assertEqual(fallback_graph.read_source, "canonical")
        self.assertEqual(fallback_graph.edges, expected_graph.edges)

    def test_search_does_not_index_reply_body_or_hidden_source_payloads(self) -> None:
        projection_root = self.root / "privacy"
        read = self.v4.read()
        query = WorkspaceQueryRepository(self.v4, projection_root)
        hidden = ("Please confirm the rollback owner.", "message:demo-release-review")
        for value in hidden:
            self.assertEqual(query.search(value).hits, ())
        build_and_publish_projection(projection_root, read.snapshot, _authority(read))
        for value in hidden:
            self.assertEqual(query.search(value).hits, ())


if __name__ == "__main__":
    unittest.main()
