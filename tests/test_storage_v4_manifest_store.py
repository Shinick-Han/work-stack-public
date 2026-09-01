from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workstack.storage.canonical import canonical_json_bytes, canonical_sha256
from workstack.storage.manifest import MANIFEST_FORMAT, MANIFEST_VERSION, V4Manifest
from workstack.storage.manifest_store import (
    RuntimeManifestError,
    publish_runtime_manifest,
    read_runtime_manifest,
)


def _manifest(generation: int) -> V4Manifest:
    value = {
        "canonical_json": "workstack.canonical-json.v1",
        "format": MANIFEST_FORMAT,
        "version": MANIFEST_VERSION,
        "generation": generation,
        "metadata": {
            "store_digest": "sha256:" + "a" * 64,
            "workspace_digest": "sha256:" + "b" * 64,
        },
        "record_count": 0,
        "records": [],
        "schema_set": "workstack.ssot.v4",
        "semantic_task_baselines": [],
        "store_format": "workstack.ssot",
        "store_schema_version": 4,
        "stream_event_count": 0,
        "streams": [],
        "workspace_uid": "11111111-1111-1111-1111-111111111111",
    }
    return V4Manifest(canonical_json_bytes(value), canonical_sha256(value))


class RuntimeManifestStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "runtime" / "authority-manifest.v2.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_missing_then_atomic_publish_and_read(self) -> None:
        self.assertIsNone(read_runtime_manifest(self.path))
        published = publish_runtime_manifest(
            self.path, _manifest(0), expected_digest=None
        )
        self.assertEqual(published.generation, 0)
        self.assertEqual(read_runtime_manifest(self.path), published)

    def test_compare_and_swap_refuses_stale_and_unrelated_runtime_change(self) -> None:
        first = publish_runtime_manifest(self.path, _manifest(0), expected_digest=None)
        second = publish_runtime_manifest(
            self.path, _manifest(1), expected_digest=first.manifest.digest
        )
        with self.assertRaisesRegex(RuntimeManifestError, "MANIFEST_CAS_MISMATCH"):
            publish_runtime_manifest(
                self.path, _manifest(2), expected_digest=first.manifest.digest
            )
        self.assertEqual(read_runtime_manifest(self.path), second)

    def test_fault_before_replace_preserves_previous_manifest(self) -> None:
        first = publish_runtime_manifest(self.path, _manifest(0), expected_digest=None)

        def interrupt(transition: str) -> None:
            if transition == "before_manifest_replace":
                raise RuntimeError("interrupt")

        with self.assertRaisesRegex(RuntimeError, "interrupt"):
            publish_runtime_manifest(
                self.path,
                _manifest(1),
                expected_digest=first.manifest.digest,
                fault_hook=interrupt,
            )
        self.assertEqual(read_runtime_manifest(self.path), first)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_noncanonical_and_candidate_manifests_are_rejected(self) -> None:
        manifest = _manifest(0)
        malformed = V4Manifest(manifest.canonical_bytes + b"\n", manifest.digest)
        with self.assertRaisesRegex(RuntimeManifestError, "MANIFEST_CANONICAL_BYTES_REQUIRED"):
            publish_runtime_manifest(self.path, malformed, expected_digest=None)

        value = dict(manifest.as_dict())
        value.pop("generation")
        value["candidate_digest"] = "sha256:" + "a" * 64
        candidate = V4Manifest(canonical_json_bytes(value), canonical_sha256(value))
        with self.assertRaisesRegex(RuntimeManifestError, "MANIFEST_FIELDS_INVALID"):
            publish_runtime_manifest(self.path, candidate, expected_digest=None)

    def test_task_baseline_roster_cannot_diverge_from_task_records(self) -> None:
        value = _manifest(0).as_dict()
        value["semantic_task_baselines"] = [{"task_uid": value["workspace_uid"]}]
        malformed = V4Manifest(canonical_json_bytes(value), canonical_sha256(value))
        with self.assertRaisesRegex(RuntimeManifestError, "MANIFEST_TASK_BASELINES_INVALID"):
            publish_runtime_manifest(self.path, malformed, expected_digest=None)


if __name__ == "__main__":
    unittest.main()
