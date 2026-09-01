from __future__ import annotations

import copy
import json
import random
import unittest
from pathlib import Path

from workstack.storage.canonical import canonical_sha256
from workstack.storage.contracts import require_valid_by_format
from workstack.storage.migration_conversion import (
    RECORD_KINDS,
    STREAM_KINDS,
    V3ConversionError,
    convert_v3_documents,
)
from workstack.storage.semantic import snapshot_from_v3_documents, snapshot_from_v4


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "store-v3"
CANDIDATE_CREATED_AT = "2026-09-01T12:00:00Z"
EXPECTED_SOURCE_DIGEST = "sha256:cca698f3d4137f0f4220eaa22102c6625a1e0de439cea364e9c0bcca0f15b36f"
EXPECTED_CONVERSION_DIGEST = "sha256:12a090beabcb3b0ebde5568201fcec8716154721c003200b990c05c0c3254140"


def _load(name: str) -> dict[str, dict]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((FIXTURES / name).glob("*.json"))
    }


def _shuffled_records(documents: dict[str, dict]) -> dict[str, dict]:
    shuffled = copy.deepcopy(documents)
    randomizer = random.Random(7801)
    for document, key in (
        ("backlog.json", "tasks"),
        ("okr.json", "objectives"),
        ("notes.json", "notes"),
        ("captures.json", "captures"),
        ("replies.json", "replies"),
    ):
        randomizer.shuffle(shuffled[document][key])
    randomizer.shuffle(shuffled["activity.json"]["idempotency"])
    return shuffled


class StorageMigrationConversionTests(unittest.TestCase):
    def test_populated_conversion_is_schema_valid_and_semantically_exact(self) -> None:
        documents = _load("populated")
        conversion = convert_v3_documents(
            documents, candidate_created_at=CANDIDATE_CREATED_AT
        )

        require_valid_by_format(conversion.store)
        require_valid_by_format(conversion.workspace)
        for kind in RECORD_KINDS:
            for record in conversion.records[kind]:
                require_valid_by_format(record)
        for kind in STREAM_KINDS:
            for event in conversion.streams[kind]:
                require_valid_by_format(event)
        require_valid_by_format(conversion.idempotency_ledger)
        self.assertEqual(
            snapshot_from_v3_documents(documents).to_dict(),
            snapshot_from_v4(conversion.semantic_source()).to_dict(),
        )
        self.assertEqual(conversion.source_snapshot_digest, EXPECTED_SOURCE_DIGEST)
        self.assertEqual(
            conversion.idempotency_ledger["compaction_policy"],
            {"retention_days": 30, "max_records": 10_000},
        )
        ledger_reference = conversion.idempotency_ledger["records"][1]["response_ref"]
        self.assertIn("record_uid", ledger_reference)
        self.assertNotIn("id", ledger_reference)
        semantic_reference = conversion.idempotency_records[1]["response_ref"]
        self.assertEqual(semantic_reference, {"kind": "reply", "id": "R-0001"})

    def test_empty_conversion_has_complete_empty_rosters_and_semantic_parity(self) -> None:
        documents = _load("empty")

        conversion = convert_v3_documents(
            documents, candidate_created_at=CANDIDATE_CREATED_AT
        )

        self.assertEqual(set(conversion.records), set(RECORD_KINDS))
        self.assertEqual(set(conversion.streams), set(STREAM_KINDS))
        self.assertTrue(all(not conversion.records[kind] for kind in RECORD_KINDS))
        self.assertTrue(all(not conversion.streams[kind] for kind in STREAM_KINDS))
        self.assertEqual(
            snapshot_from_v3_documents(documents).to_dict(),
            snapshot_from_v4(conversion.semantic_source()).to_dict(),
        )

    def test_shuffled_record_inputs_produce_identical_values_and_digest(self) -> None:
        documents = _load("populated")
        shuffled = _shuffled_records(documents)

        first = convert_v3_documents(
            documents, candidate_created_at=CANDIDATE_CREATED_AT
        )
        second = convert_v3_documents(
            shuffled, candidate_created_at=CANDIDATE_CREATED_AT
        )

        self.assertEqual(first.artifact_values(), second.artifact_values())
        self.assertEqual(first.conversion_digest, second.conversion_digest)

    def test_conversion_does_not_mutate_source_or_share_nested_values(self) -> None:
        documents = _load("populated")
        before = copy.deepcopy(documents)

        conversion = convert_v3_documents(
            documents, candidate_created_at=CANDIDATE_CREATED_AT
        )
        detached = conversion.artifact_values()
        detached["records"]["tasks"][0]["title"] = "mutated result"
        detached["runtime"]["idempotency_ledger"]["records"].clear()

        self.assertEqual(documents, before)
        self.assertNotEqual(
            conversion.records["tasks"][0]["title"], "mutated result"
        )
        self.assertEqual(len(conversion.idempotency_ledger["records"]), 2)
        self.assertNotIn("idempotency", conversion.artifact_values()["runtime"])

    def test_uid_relationships_and_task_note_roster_are_stable(self) -> None:
        conversion = convert_v3_documents(
            _load("populated"), candidate_created_at=CANDIDATE_CREATED_AT
        )
        tasks = {record["display_id"]: record for record in conversion.records["tasks"]}
        objectives = {
            record["display_id"]: record for record in conversion.records["objectives"]
        }
        captures = {
            record["display_id"]: record for record in conversion.records["captures"]
        }
        replies = {
            record["display_id"]: record for record in conversion.records["replies"]
        }
        annotation = next(
            record
            for record in conversion.records["notes"]
            if record["note_kind"] == "task_annotation"
        )

        self.assertEqual(tasks["T-0002"]["parent_uid"], tasks["T-0001"]["uid"])
        self.assertEqual(tasks["T-0002"]["dependency_uids"], [tasks["T-0001"]["uid"]])
        self.assertEqual(tasks["T-0001"]["objective_uids"], [objectives["O-1"]["uid"]])
        self.assertEqual(captures["C-0001"]["linked_task_uids"], [tasks["T-0001"]["uid"]])
        self.assertEqual(replies["R-0001"]["capture_uid"], captures["C-0001"]["uid"])
        self.assertEqual(annotation["task_uid"], tasks["T-0001"]["uid"])
        self.assertEqual(conversion.task_note_source_indexes[annotation["uid"]], 0)
        self.assertEqual(
            conversion.task_note_source_roster,
            ({
                "task_uid": tasks["T-0001"]["uid"],
                "source_index": 0,
                "generated_note_uid": annotation["uid"],
            },),
        )
        self.assertEqual(len(conversion.generated_id_roster), 15)
        self.assertEqual(
            len({item["generated_uid"] for item in conversion.generated_id_roster}),
            len(conversion.generated_id_roster),
        )
        self.assertNotIn("Normalize the SSOT", repr(conversion.generated_id_roster))

    def test_stream_sequences_and_digest_chain_are_global_and_deterministic(self) -> None:
        conversion = convert_v3_documents(
            _load("populated"), candidate_created_at=CANDIDATE_CREATED_AT
        )
        events = sorted(
            (event for kind in STREAM_KINDS for event in conversion.streams[kind]),
            key=lambda event: event["sequence"],
        )

        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        previous = None
        for event in events:
            self.assertEqual(event.get("previous_event_digest"), previous)
            candidate = dict(event)
            claimed = candidate.pop("event_digest")
            self.assertEqual(claimed, canonical_sha256(candidate))
            previous = claimed

    def test_planning_predecessor_order_does_not_depend_on_legacy_timestamps(self) -> None:
        documents = _load("populated")
        planning = documents["activity.json"]["planning_status"]
        planning[0]["created_at"] = "2026-09-01T03:00:00Z"
        planning[2]["created_at"] = "2026-09-01T00:00:00Z"

        conversion = convert_v3_documents(
            documents, candidate_created_at=CANDIDATE_CREATED_AT
        )
        events = conversion.streams["planning-status"]

        self.assertEqual(
            [event["legacy_fact_id"] for event in events],
            ["PS-000001", "PS-000002", "PS-000003"],
        )
        self.assertLess(events[0]["sequence"], events[2]["sequence"])
        self.assertEqual(events[2]["previous_event_uid"], events[0]["event_uid"])

    def test_conversion_digest_is_frozen_over_all_candidate_values(self) -> None:
        conversion = convert_v3_documents(
            _load("populated"), candidate_created_at=CANDIDATE_CREATED_AT
        )

        self.assertEqual(conversion.conversion_digest, canonical_sha256(conversion.artifact_values()))
        self.assertEqual(conversion.conversion_digest, EXPECTED_CONVERSION_DIGEST)

    def test_invalid_candidate_instant_fails_without_echoing_source(self) -> None:
        documents = _load("populated")
        documents["workspace.json"]["name"] = "PRIVATE_WORKSPACE_NAME"

        with self.assertRaisesRegex(V3ConversionError, "INVALID_V3_SOURCE") as caught:
            convert_v3_documents(documents, candidate_created_at="not-a-timestamp")

        self.assertNotIn("PRIVATE_WORKSPACE_NAME", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
