from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from workstack.storage.canonical import canonical_json_bytes, canonical_sha256
from workstack.storage.manifest import (
    V4ManifestError,
    build_v4_manifest,
    construct_v4_manifest,
)
from workstack.storage.reader import ReadArtifact, V4ReadResult, read_v4


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads(
    (ROOT / "contracts" / "workstack-ssot-v4" / "examples" / "valid" / "cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]


def _case(name: str) -> dict[str, object]:
    return copy.deepcopy(next(item["instance"] for item in CASES if item["name"] == name))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _write_stream(root: Path, kind: str, events: list[dict[str, object]]) -> None:
    path = root / "streams" / kind / "2026-09.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(canonical_json_bytes(event) + b"\n" for event in events))


def _make_v4(root: Path, *, sensitive: str = "PRIVATE WORK ITEM") -> Path:
    store = _case("store-metadata")
    workspace = _case("workspace")
    task = _case("task")
    task["title"] = sensitive
    task["detail"] = sensitive + " detail"
    uid = str(task["uid"])
    planning = _case("planning-status-event")
    planning["task_uid"] = uid
    planning["record_uid"] = uid
    planning["task_display_id"] = task["display_id"]
    activity = _case("activity-event")
    activity["task_uid"] = uid
    activity["record_uid"] = uid
    _write_json(root / "store.json", store)
    _write_json(root / "workspace.json", workspace)
    _write_json(root / "records" / "tasks" / uid[:2] / f"{uid}.json", task)
    _write_stream(root, "planning-status", [planning])
    _write_stream(root, "activity", [activity])
    return root


def _replace_collection(
    source: MappingProxyType,
    kind: str,
    values: tuple[MappingProxyType, ...],
) -> MappingProxyType:
    changed = dict(source)
    changed[kind] = values
    return MappingProxyType(changed)


def _build_stubbed(result: V4ReadResult) -> None:
    with patch("workstack.storage.manifest._verified_result", return_value=result):
        build_v4_manifest(result, generation=1)


class StorageV4ManifestTests(unittest.TestCase):
    def test_runtime_manifest_is_deterministic_content_free_and_generation_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sensitive = "PRIVATE-TITLE-MUST-NOT-ESCAPE"
            root = _make_v4(Path(temporary), sensitive=sensitive)

            first = construct_v4_manifest(root, generation=7)
            second = construct_v4_manifest(root, generation=7)
            value = first.as_dict()

            self.assertEqual(first.canonical_bytes, second.canonical_bytes)
            self.assertEqual(first.digest, second.digest)
            self.assertEqual(value["version"], 2)
            self.assertEqual(value["generation"], 7)
            self.assertNotIn("candidate_digest", value)
            self.assertEqual(value["record_count"], 1)
            self.assertEqual(value["stream_event_count"], 2)
            self.assertNotIn(sensitive, first.canonical_bytes.decode("utf-8"))
            self.assertEqual(first.digest, canonical_sha256(value))

    def test_candidate_manifest_uses_a_stable_candidate_digest_not_a_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_v4(Path(temporary))

            built = construct_v4_manifest(root)
            value = built.as_dict()
            candidate_digest = value.pop("candidate_digest")

            self.assertNotIn("generation", value)
            self.assertEqual(candidate_digest, canonical_sha256(value))

    def test_record_and_stream_rosters_include_exact_artifact_and_head_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_v4(Path(temporary))

            value = construct_v4_manifest(root, generation=1).as_dict()

            record = value["records"][0]
            self.assertEqual(record["artifact"], f"records/tasks/22/{record['uid']}.json")
            self.assertRegex(record["digest"], r"^sha256:[0-9a-f]{64}$")

            baseline = value["semantic_task_baselines"][0]
            self.assertEqual(baseline["task_uid"], record["uid"])
            self.assertEqual(baseline["record_revision"], record["revision"])
            self.assertEqual(baseline["record_value_digest"], record["value_digest"])
            self.assertEqual(baseline["status"], "open")
            self.assertRegex(baseline["planning_head_digest"], r"^sha256:[0-9a-f]{64}$")
            streams = {item["kind"]: item for item in value["streams"]}
            self.assertEqual(streams["activity"]["first_sequence"], 2)
            self.assertEqual(streams["activity"]["last_sequence"], 2)
            self.assertEqual(streams["activity"]["event_count"], 1)
            self.assertEqual(
                streams["activity"]["head_event_uid"],
                "77777777-7777-7777-7777-777777777772",
            )
            self.assertRegex(streams["activity"]["head_digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(streams["activity"]["value_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_shuffled_reader_rosters_produce_identical_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_v4(Path(temporary))
            result = read_v4(root)
            shuffled = replace(result, artifacts=tuple(reversed(result.artifacts)))

            expected = build_v4_manifest(result, generation=4)
            actual = build_v4_manifest(shuffled, generation=4)

            self.assertEqual(actual.canonical_bytes, expected.canonical_bytes)

    def test_inconsistent_or_stale_reader_input_fails_closed_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sensitive = "SENSITIVE-READER-MISMATCH"
            root = _make_v4(Path(temporary))
            result = read_v4(root)
            changed_store = dict(result.store)
            changed_store["schema_set"] = sensitive
            inconsistent = replace(result, store=MappingProxyType(changed_store))

            with self.assertRaises(V4ManifestError) as caught:
                build_v4_manifest(inconsistent, generation=1)

            self.assertEqual(caught.exception.code, "READER_RESULT_INCONSISTENT")
            self.assertNotIn(sensitive, str(caught.exception))

            task_path = next(root.glob("records/tasks/*/*.json"))
            task = json.loads(task_path.read_text(encoding="utf-8"))
            task["title"] = sensitive
            _write_json(task_path, task)
            with self.assertRaisesRegex(V4ManifestError, "READER_RESULT_INCONSISTENT"):
                build_v4_manifest(result, generation=1)

    def test_invalid_generation_is_rejected_before_authority_read(self) -> None:
        phantom = V4ReadResult(
            root=Path("not-read"),
            store=MappingProxyType({}),
            workspace=MappingProxyType({}),
            records=MappingProxyType({}),
            streams=MappingProxyType({}),
            artifacts=(),
        )
        for generation in (-1, True, 9_007_199_254_740_992):
            with self.subTest(generation=generation):
                with self.assertRaisesRegex(V4ManifestError, "GENERATION_INVALID"):
                    build_v4_manifest(phantom, generation=generation)  # type: ignore[arg-type]

    def test_claimed_stream_digest_chain_is_verified_before_manifesting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_v4(Path(temporary))
            activity_path = root / "streams" / "activity" / "2026-09.ndjson"
            activity = json.loads(activity_path.read_text(encoding="utf-8"))
            activity["previous_event_digest"] = "sha256:" + "0" * 64
            activity["event_digest"] = "sha256:" + "1" * 64
            _write_stream(root, "activity", [activity])

            with self.assertRaisesRegex(V4ManifestError, "STREAM_DIGEST_CHAIN_INVALID"):
                construct_v4_manifest(root, generation=1)

    def test_manifest_integrity_branches_fail_closed_with_stable_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = read_v4(_make_v4(Path(temporary)))
            artifacts = list(result.artifacts)
            task_index = next(i for i, item in enumerate(artifacts) if item.category == "record")
            activity_index = next(
                i for i, item in enumerate(artifacts) if item.category == "stream" and item.kind == "activity"
            )
            cases: list[tuple[str, V4ReadResult]] = []
            cases.append(("ARTIFACT_ROSTER_INVALID", replace(result, artifacts=result.artifacts + (result.artifacts[0],))))
            invalid_digest = artifacts.copy()
            invalid_digest[0] = replace(invalid_digest[0], sha256="not-a-digest")
            cases.append(("ARTIFACT_ROSTER_INVALID", replace(result, artifacts=tuple(invalid_digest))))
            negative_size = artifacts.copy()
            negative_size[0] = replace(negative_size[0], byte_count=-1)
            cases.append(("ARTIFACT_ROSTER_INVALID", replace(result, artifacts=tuple(negative_size))))
            metadata_count = artifacts.copy()
            metadata_count[0] = replace(metadata_count[0], item_count=2)
            cases.append(("ARTIFACT_ROSTER_INVALID", replace(result, artifacts=tuple(metadata_count))))
            record_count = artifacts.copy()
            record_count[task_index] = replace(record_count[task_index], item_count=2)
            cases.append(("RECORD_ROSTER_INVALID", replace(result, artifacts=tuple(record_count))))
            task = dict(result.records["tasks"][0])
            task.pop("revision")
            invalid_records = _replace_collection(
                result.records, "tasks", (MappingProxyType(task),)
            )
            cases.append(("RECORD_ROSTER_INVALID", replace(result, records=invalid_records)))
            segment_count = artifacts.copy()
            segment_count[activity_index] = replace(segment_count[activity_index], item_count=2)
            cases.append(("STREAM_SEGMENT_METADATA_INVALID", replace(result, artifacts=tuple(segment_count))))
            segment_head = artifacts.copy()
            segment_head[activity_index] = replace(segment_head[activity_index], last_sequence=99)
            cases.append(("STREAM_SEGMENT_METADATA_INVALID", replace(result, artifacts=tuple(segment_head))))
            missing_segment = tuple(item for item in artifacts if item.kind != "activity")
            cases.append(("STREAM_SEGMENT_MISSING", replace(result, artifacts=missing_segment)))
            extra = ReadArtifact("extra", "future", "future", 0, "sha256:" + "0" * 64)
            cases.append(("ARTIFACT_ROSTER_INVALID", replace(result, artifacts=result.artifacts + (extra,))))
            invalid_store = dict(result.store)
            invalid_store["schema_set"] = "future"
            cases.append(
                ("AUTHORITY_HEADER_INVALID", replace(result, store=MappingProxyType(invalid_store)))
            )

            for code, candidate in cases:
                with self.subTest(code=code):
                    with self.assertRaisesRegex(V4ManifestError, code):
                        _build_stubbed(candidate)

    def test_manifest_rejects_invalid_event_rosters_and_stream_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = read_v4(_make_v4(Path(temporary)))
            activity = dict(result.streams["activity"][0])
            planning = dict(result.streams["planning-status"][0])
            cases: list[tuple[str, V4ReadResult]] = []
            missing_sequence = dict(activity)
            missing_sequence.pop("sequence")
            streams = _replace_collection(
                result.streams, "activity", (MappingProxyType(missing_sequence),)
            )
            cases.append(("STREAM_ROSTER_INVALID", replace(result, streams=streams)))
            duplicate_uid = dict(activity)
            duplicate_uid["event_uid"] = planning["event_uid"]
            streams = _replace_collection(
                result.streams, "activity", (MappingProxyType(duplicate_uid),)
            )
            cases.append(("STREAM_ROSTER_INVALID", replace(result, streams=streams)))
            invalid_date = dict(activity)
            invalid_date["created_at"] = None
            streams = _replace_collection(
                result.streams, "activity", (MappingProxyType(invalid_date),)
            )
            cases.append(("STREAM_ROSTER_INVALID", replace(result, streams=streams)))
            gap = dict(activity)
            gap["sequence"] = 3
            streams = _replace_collection(result.streams, "activity", (MappingProxyType(gap),))
            cases.append(("STREAM_SEQUENCE_INVALID", replace(result, streams=streams)))
            stream_artifacts = list(result.artifacts)
            index = next(i for i, item in enumerate(stream_artifacts) if item.kind == "activity")
            stream_artifacts[index] = replace(stream_artifacts[index], kind="future")
            cases.append(
                ("STREAM_ROSTER_INVALID", replace(result, artifacts=tuple(stream_artifacts)))
            )
            stream_artifacts = list(result.artifacts)
            stream_artifacts[index] = replace(
                stream_artifacts[index], artifact="streams/activity/nested/2026-09.ndjson"
            )
            cases.append(
                ("STREAM_ROSTER_INVALID", replace(result, artifacts=tuple(stream_artifacts)))
            )

            for code, candidate in cases:
                with self.subTest(code=code):
                    with self.assertRaisesRegex(V4ManifestError, code):
                        _build_stubbed(candidate)

    def test_construct_wraps_reader_and_layout_failures_in_content_free_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(V4ManifestError, "AUTHORITY_READ_FAILED") as caught:
                construct_v4_manifest(Path(temporary) / "missing", generation=1)
            self.assertNotIn(str(temporary), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
