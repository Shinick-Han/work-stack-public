"""Wave 4 D3A: pure v4 Task deletion staging.

Tests use converted populated fixture copies only. They must not open the live
SSOT, bind port 8765, or invent a canonical display-ID high-water field.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.idempotency import parse_idempotency_ledger
from workstack.storage.layout import RECORD_KINDS
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.reader import read_v4
from workstack.storage.task_deletion_plan import TaskDeletionPlan, plan_v4_task_deletion
from workstack.storage.v4_task_deletion_staging import (
    V4DeletionStagingError,
    stage_v4_task_deletion,
)
from workstack.storage.validation import validate_storage_path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "store-v3" / "populated"
LIVE_SSOT = Path.home() / "WorkStack" / "SSOT" / "main"
CANDIDATE_CREATED_AT = "2026-09-01T12:00:00Z"
TARGET_ID = "T-0001"
NEIGHBOR_ID = "T-0002"
TARGET_UID = "9aaaf471-bf4f-59c7-9087-2be28e899cac"
TARGET_REVISION = 2
FORBIDDEN_BODIES = (
    "Normalize the SSOT",
    "Preserve every authoritative field while normalizing storage.",
    "Keep the v3 source bytes unchanged.",
)


def _assert_not_live_ssot() -> None:
    resolved = FIXTURE.resolve()
    live = LIVE_SSOT.resolve() if LIVE_SSOT.exists() else LIVE_SSOT
    if resolved == live or live in resolved.parents or resolved in live.parents:
        raise AssertionError("refusing to use live SSOT path")


def _load_documents() -> dict[str, dict]:
    _assert_not_live_ssot()
    documents = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURE.glob("*.json"))
    }
    if "backlog.json" not in documents:
        raise AssertionError("missing populated fixture backlog.json")
    return documents


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _conversion_bundle() -> tuple[object, dict[str, object], bytes]:
    documents = _load_documents()
    conversion = convert_v3_documents(documents, candidate_created_at=CANDIDATE_CREATED_AT)
    physical = {
        "records": {
            kind: [dict(item) for item in conversion.records[kind]]
            for kind in conversion.records
        },
        "streams": {
            kind: [dict(item) for item in conversion.streams[kind]]
            for kind in conversion.streams
        },
        "idempotency_ledger": copy.deepcopy(dict(conversion.idempotency_ledger)),
    }
    ledger_bytes = canonical_json_bytes(dict(conversion.idempotency_ledger))
    return conversion, physical, ledger_bytes


def _artifact_digests(physical: dict[str, object], ledger_bytes: bytes) -> dict[str, str]:
    digests = {"idempotency-ledger.v1.json": _digest(ledger_bytes)}
    records = physical["records"]
    for kind, items in records.items():
        for record in items:
            uid = str(record["uid"])
            body = canonical_json_bytes(dict(record))
            digests[f"records/{kind}/{uid[:2]}/{uid}.json"] = _digest(body)
    streams = physical["streams"]
    grouped: dict[str, list[dict]] = {}
    for kind, events in streams.items():
        for event in events:
            artifact = f"streams/{kind}/{str(event['created_at'])[:7]}.ndjson"
            grouped.setdefault(artifact, []).append(event)
    for artifact, events in grouped.items():
        ordered = sorted(events, key=lambda item: int(item["sequence"]))
        body = b"\n".join(canonical_json_bytes(dict(event)) for event in ordered) + b"\n"
        digests[artifact] = _digest(body)
    return digests


def _plan_for(physical: dict[str, object]) -> TaskDeletionPlan:
    return plan_v4_task_deletion(
        physical, task_id=TARGET_ID, expected_revision=TARGET_REVISION
    )


def _stage(physical, plan, ledger_bytes, digests=None):
    return stage_v4_task_deletion(
        physical,
        task_id=TARGET_ID,
        expected_revision=TARGET_REVISION,
        plan=plan,
        artifact_digests=digests or _artifact_digests(physical, ledger_bytes),
        ledger_bytes=ledger_bytes,
    )


def _freeze(targets) -> tuple:
    return tuple(
        (
            item.action,
            item.scope,
            item.artifact,
            item.expected_digest,
            item.proposed_digest,
            item.proposed_bytes,
        )
        for item in targets
    )


def _shuffle_value(value: object, rng: random.Random) -> object:
    if not isinstance(value, dict):
        return value
    records = {}
    record_items = list(value["records"].items())
    rng.shuffle(record_items)
    for kind, items in record_items:
        copied = [dict(item) for item in items]
        rng.shuffle(copied)
        records[kind] = copied
    streams = {}
    stream_items = list(value["streams"].items())
    rng.shuffle(stream_items)
    for kind, events in stream_items:
        copied = [dict(event) for event in events]
        rng.shuffle(copied)
        streams[kind] = copied
    ledger = copy.deepcopy(dict(value["idempotency_ledger"]))
    return {
        "records": records,
        "streams": streams,
        "idempotency_ledger": ledger,
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
            write(
                root / "records" / kind / uid[:2] / f"{uid}.json",
                canonical_json_bytes(dict(record)),
            )
    grouped = {}
    for kind, events in conversion.streams.items():
        for event in events:
            grouped.setdefault((kind, str(event["created_at"])[:7]), []).append(event)
    for (kind, segment), events in sorted(grouped.items()):
        body = b"".join(
            canonical_json_bytes(dict(event)) + b"\n"
            for event in sorted(events, key=lambda item: int(item["sequence"]))
        )
        write(root / "streams" / kind / f"{segment}.ndjson", body)


def _apply_authority(root: Path, targets) -> None:
    for target in targets:
        if target.scope != "authority":
            continue
        path = root / target.artifact
        if target.action == "delete":
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(target.proposed_bytes)


def _json_blobs(root: Path) -> str:
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


class V4DeletionStagingInventoryTest(unittest.TestCase):
    def test_populated_target_inventory_and_canonical_ordering(self) -> None:
        _conversion, physical, ledger_bytes = _conversion_bundle()
        before = copy.deepcopy(physical)
        ledger_before = bytes(ledger_bytes)
        plan = _plan_for(physical)
        targets = _stage(physical, plan, ledger_bytes)
        self.assertEqual(physical, before)
        self.assertEqual(ledger_bytes, ledger_before)
        keys = [(item.scope, item.artifact, item.action) for item in targets]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len({(item.scope, item.artifact) for item in targets}))
        artifacts = [item.artifact for item in targets]
        self.assertTrue(any(item.startswith("records/tasks/") and targets[index].action == "delete" for index, item in enumerate(artifacts)))
        deletes = [item for item in targets if item.action == "delete"]
        self.assertEqual(
            {item.artifact.split("/")[1] for item in deletes if item.artifact.startswith("records/")},
            {"tasks", "notes", "replies"},
        )
        replaces = [item for item in targets if item.action == "replace" and item.scope == "authority"]
        kinds = {item.artifact.split("/")[1] for item in replaces if item.artifact.startswith("records/")}
        self.assertEqual(kinds, {"tasks", "notes", "captures"})
        self.assertIn("idempotency-ledger.v1.json", artifacts)
        self.assertTrue(any(item.startswith("streams/") for item in artifacts))
        self.assertNotIn("store.json", artifacts)
        self.assertNotIn("store-meta.json", artifacts)

    def test_repeated_and_shuffled_inputs_are_byte_identical(self) -> None:
        _conversion, physical, ledger_bytes = _conversion_bundle()
        plan = _plan_for(physical)
        first = _stage(physical, plan, ledger_bytes)
        second = _stage(physical, plan, ledger_bytes)
        self.assertEqual(_freeze(first), _freeze(second))
        shuffled = _shuffle_value(copy.deepcopy(physical), random.Random(17))
        shuffled_plan = _plan_for(shuffled)
        self.assertEqual(plan.canonical_bytes(), shuffled_plan.canonical_bytes())
        third = _stage(
            shuffled,
            shuffled_plan,
            ledger_bytes,
            _artifact_digests(shuffled, ledger_bytes),
        )
        self.assertEqual(_freeze(first), _freeze(third))

    def test_staging_does_not_open_files_or_read_clocks(self) -> None:
        _conversion, physical, ledger_bytes = _conversion_bundle()
        plan = _plan_for(physical)
        digests = _artifact_digests(physical, ledger_bytes)

        def refuse_open(*_args, **_kwargs):
            raise AssertionError("staging opened a file")

        def refuse_clock(*_args, **_kwargs):
            raise AssertionError("staging read a clock")

        with mock.patch("builtins.open", refuse_open), mock.patch(
            "time.time", refuse_clock
        ), mock.patch("time.monotonic", refuse_clock):
            stage_v4_task_deletion(
                physical,
                task_id=TARGET_ID,
                expected_revision=TARGET_REVISION,
                plan=plan,
                artifact_digests=digests,
                ledger_bytes=ledger_bytes,
            )


class V4DeletionStagingRewriteTest(unittest.TestCase):
    def test_task_note_reply_delete_and_inbound_rewrites(self) -> None:
        conversion, physical, ledger_bytes = _conversion_bundle()
        plan = _plan_for(physical)
        targets = _stage(physical, plan, ledger_bytes)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "authority"
            _write_conversion(root, conversion)
            _apply_authority(root, targets)
            result = read_v4(root)
            display_ids = {record["display_id"] for record in result.records["tasks"]}
            self.assertNotIn(TARGET_ID, display_ids)
            self.assertIn(NEIGHBOR_ID, display_ids)
            neighbor = next(item for item in result.records["tasks"] if item["display_id"] == NEIGHBOR_ID)
            self.assertIsNone(neighbor["parent_uid"])
            self.assertNotIn(TARGET_UID, neighbor["dependency_uids"])
            self.assertEqual(neighbor["revision"], 1)
            self.assertFalse(any(item.get("task_uid") == TARGET_UID for item in result.records["notes"]))
            self.assertFalse(any(item.get("task_uid") == TARGET_UID for item in result.records["replies"]))
            note = next(item for item in result.records["notes"] if item["display_id"] == "N-0001")
            self.assertNotIn(TARGET_ID, note["links"])
            self.assertEqual(note["links"], ["O-1"])
            self.assertEqual(note["text"], "Readable canonical data is part of the recovery contract.")
            capture = next(item for item in result.records["captures"] if item["display_id"] == "C-0001")
            self.assertNotIn(TARGET_UID, capture["linked_task_uids"])
            self.assertNotIn(TARGET_ID, capture["task_hints"])
            action = capture["normalized"]["action_items"][0]
            self.assertIsNone(action["task_uid"])
            self.assertTrue(action["title"])

    def test_content_free_ledger_rewrite_preserves_unrelated_records(self) -> None:
        _conversion, physical, ledger_bytes = _conversion_bundle()
        plan = _plan_for(physical)
        targets = _stage(physical, plan, ledger_bytes)
        ledger_target = next(
            item for item in targets if item.artifact == "idempotency-ledger.v1.json"
        )
        self.assertEqual(ledger_target.scope, "runtime")
        rewritten = parse_idempotency_ledger(ledger_target.proposed_bytes)
        original = parse_idempotency_ledger(ledger_bytes)
        by_key = {item["key"]: item for item in rewritten["records"]}
        original_by_key = {item["key"]: item for item in original["records"]}
        purged = by_key["fixture.reply.0001"]
        self.assertEqual(purged["response_body"], {"data": {"purged": True}})
        self.assertNotIn("response_ref", purged)
        self.assertEqual(purged["key"], original_by_key["fixture.reply.0001"]["key"])
        self.assertEqual(purged["method"], original_by_key["fixture.reply.0001"]["method"])
        self.assertEqual(purged["path"], original_by_key["fixture.reply.0001"]["path"])
        self.assertEqual(purged["expires_at"], original_by_key["fixture.reply.0001"]["expires_at"])
        self.assertEqual(by_key["fixture.capture.0001"], original_by_key["fixture.capture.0001"])
        self.assertEqual(rewritten["updated_at"], original["updated_at"])
        self.assertEqual(rewritten["workspace_uid"], original["workspace_uid"])


class V4DeletionStagingMaterializedTest(unittest.TestCase):
    def test_materialized_proposal_reads_validates_and_drops_target_material(self) -> None:
        conversion, physical, ledger_bytes = _conversion_bundle()
        physical_before = copy.deepcopy(physical)
        ledger_before = bytes(ledger_bytes)
        fixture_before = {
            path.name: path.read_bytes() for path in sorted(FIXTURE.glob("*.json"))
        }
        plan = _plan_for(physical)
        targets = _stage(physical, plan, ledger_bytes)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            original = base / "original"
            proposed = base / "proposed"
            original.mkdir()
            _write_conversion(original, conversion)
            shutil.copytree(original, proposed)
            original_bytes = {
                path.relative_to(original).as_posix(): path.read_bytes()
                for path in original.rglob("*")
                if path.is_file()
            }
            _apply_authority(proposed, targets)
            result = read_v4(proposed)
            manifest = build_v4_manifest(result, generation=1)
            self.assertTrue(manifest.digest.startswith("sha256:"))
            report = validate_storage_path(proposed)
            codes = {issue.code for issue in report.issues}
            self.assertTrue(report.valid, report.issues)
            self.assertNotIn("DANGLING_REFERENCE", codes)
            self.assertNotIn("STREAM_SEQUENCE_GAP", codes)
            self.assertNotIn("DUPLICATE_STREAM_SEQUENCE", codes)
            self.assertNotIn("EVENT_DIGEST_MISMATCH", codes)
            self.assertNotIn("PLANNING_CHAIN_INVALID", codes)
            self.assertNotIn("PLANNING_FACT_ORDER_INVALID", codes)
            blob = _json_blobs(proposed)
            self.assertNotIn(TARGET_UID, blob)
            self.assertNotIn('"T-0001"', blob)
            for body in FORBIDDEN_BODIES:
                self.assertNotIn(body, blob)
            self.assertIn("Verify relationships", blob)
            self.assertIn("E-000001", blob)
            self.assertIn("PS-000001", blob)
            self.assertNotIn("E-000002", blob)
            self.assertNotIn("PS-000003", blob)
            after_original = {
                path.relative_to(original).as_posix(): path.read_bytes()
                for path in original.rglob("*")
                if path.is_file()
            }
            self.assertEqual(original_bytes, after_original)
        self.assertEqual(physical, physical_before)
        self.assertEqual(ledger_bytes, ledger_before)
        after_fixture = {
            path.name: path.read_bytes() for path in sorted(FIXTURE.glob("*.json"))
        }
        self.assertEqual(fixture_before, after_fixture)


class V4DeletionStagingFailClosedTest(unittest.TestCase):
    def test_unknown_malformed_and_unconsumed_operations_refuse(self) -> None:
        _conversion, physical, ledger_bytes = _conversion_bundle()
        plan = _plan_for(physical)
        unknown = TaskDeletionPlan(
            layout=plan.layout,
            target_display_id=plan.target_display_id,
            target_uid=plan.target_uid,
            expected_revision=plan.expected_revision,
            display_id_high_water=plan.display_id_high_water,
            operations=tuple([*plan.operations, {"op": "explode"}]),
        )
        with self.assertRaises(V4DeletionStagingError) as ctx:
            _stage(physical, unknown, ledger_bytes)
        self.assertEqual(ctx.exception.code, "invalid_plan")
        self.assertEqual(str(ctx.exception), "invalid_plan")
        malformed_ops = [dict(item) for item in plan.operations]
        removed = False
        for item in malformed_ops:
            if item.get("op") == "remove_task_record":
                item.pop("uid", None)
                removed = True
        self.assertTrue(removed)
        malformed = TaskDeletionPlan(
            layout=plan.layout,
            target_display_id=plan.target_display_id,
            target_uid=plan.target_uid,
            expected_revision=plan.expected_revision,
            display_id_high_water=plan.display_id_high_water,
            operations=tuple(malformed_ops),
        )
        with self.assertRaises(V4DeletionStagingError):
            _stage(physical, malformed, ledger_bytes)
        self.assertNotIn("Normalize the SSOT", str(ctx.exception))

    def test_stale_and_missing_artifact_digest_refuse(self) -> None:
        _conversion, physical, ledger_bytes = _conversion_bundle()
        plan = _plan_for(physical)
        digests = _artifact_digests(physical, ledger_bytes)
        stale = dict(digests)
        first = next(iter(stale))
        stale[first] = "sha256:" + ("cd" * 32)
        with self.assertRaises(V4DeletionStagingError) as ctx:
            _stage(physical, plan, ledger_bytes, stale)
        self.assertIn(ctx.exception.code, {"stale_digest", "invalid_stream", "invalid_plan"})
        missing = dict(digests)
        missing.pop(first)
        with self.assertRaises(V4DeletionStagingError):
            _stage(physical, plan, ledger_bytes, missing)


class HighWaterCharacterizationTest(unittest.TestCase):
    def test_canonical_display_id_high_water_contract_is_absent(self) -> None:
        conversion, physical, ledger_bytes = _conversion_bundle()
        plan = _plan_for(physical)
        targets = _stage(physical, plan, ledger_bytes)
        self.assertEqual(plan.display_id_high_water, 2)
        self.assertNotIn("display_id_high_water", conversion.store)
        self.assertNotIn("display_id_high_water", conversion.workspace)
        self.assertNotIn("display_id_high_water", conversion.idempotency_ledger)
        for kind in RECORD_KINDS:
            for record in physical["records"][kind]:
                self.assertNotIn("display_id_high_water", record)
        artifacts = {item.artifact for item in targets}
        self.assertNotIn("store.json", artifacts)
        self.assertNotIn("store-meta.json", artifacts)
        self.assertFalse(any("high-water" in item or "high_water" in item for item in artifacts))
        live_ids = [record["display_id"] for record in physical["records"]["tasks"]]
        self.assertEqual(sorted(live_ids), ["T-0001", "T-0002"])
        remainder_if_max_deleted = [
            item for item in live_ids if item != "T-0002"
        ]
        remainder_max = max(int(item.split("-")[1]) for item in remainder_if_max_deleted)
        self.assertEqual(remainder_max, 1)
        self.assertNotEqual(remainder_max, plan.display_id_high_water)
        maximum_plan = plan_v4_task_deletion(
            physical, task_id="T-0002", expected_revision=0
        )
        self.assertEqual(maximum_plan.display_id_high_water, 2)
        ledger_target = next(
            item for item in targets if item.artifact == "idempotency-ledger.v1.json"
        )
        ledger = parse_idempotency_ledger(ledger_target.proposed_bytes)
        self.assertNotIn("display_id_high_water", ledger)
        for record in ledger["records"]:
            self.assertNotIn("tombstone", record)
            self.assertNotIn("high_water", record)


def _note_n0001(physical: dict[str, object]) -> dict:
    return next(
        item for item in physical["records"]["notes"] if item["display_id"] == "N-0001"
    )


def _capture_c0001(physical: dict[str, object]) -> dict:
    return next(
        item for item in physical["records"]["captures"] if item["display_id"] == "C-0001"
    )


def _target_action(capture: dict) -> dict:
    return next(
        item
        for item in capture["normalized"]["action_items"]
        if item.get("task_uid") == TARGET_UID
    )


def _overwrite_records(root: Path, physical: dict[str, object]) -> None:
    for kind, records in physical["records"].items():
        for record in records:
            uid = str(record["uid"])
            path = root / "records" / kind / uid[:2] / f"{uid}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(canonical_json_bytes(dict(record)))


def _stage_from(physical: dict[str, object], ledger_bytes: bytes):
    plan = _plan_for(physical)
    return plan, _stage(
        physical, plan, ledger_bytes, _artifact_digests(physical, ledger_bytes)
    )


class AdversarialDisplayTokenTest(unittest.TestCase):
    def test_lowercase_and_whitespace_display_tokens_are_removed(self) -> None:
        conversion, physical, ledger_bytes = _conversion_bundle()
        note = _note_n0001(physical)
        capture = _capture_c0001(physical)
        note["links"] = ["O-1", "t-0001", " T-0001 ", "KEEP"]
        capture["task_hints"] = ["keep-hint", "t-0001", " T-0001 "]
        before = copy.deepcopy(physical)
        plan, targets = _stage_from(physical, ledger_bytes)
        self.assertEqual(physical, before)
        self.assertTrue(
            any(item.get("op") == "rewrite_note_links" for item in plan.operations)
        )
        self.assertTrue(
            any(
                item.get("op") == "unlink_capture_field" and item.get("field") == "task_hints"
                for item in plan.operations
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "authority"
            _write_conversion(root, conversion)
            _overwrite_records(root, physical)
            _apply_authority(root, targets)
            result = read_v4(root)
            rewritten_note = next(
                item for item in result.records["notes"] if item["display_id"] == "N-0001"
            )
            self.assertEqual(rewritten_note["links"], ["O-1", "KEEP"])
            rewritten_capture = next(
                item for item in result.records["captures"] if item["display_id"] == "C-0001"
            )
            self.assertEqual(rewritten_capture["task_hints"], ["keep-hint"])

    def test_multiple_case_variants_are_all_removed(self) -> None:
        _conversion, physical, ledger_bytes = _conversion_bundle()
        note = _note_n0001(physical)
        capture = _capture_c0001(physical)
        note["links"] = ["O-1", "T-0001", "t-0001"]
        capture["task_hints"] = ["T-0001", "t-0001", "keep-hint"]
        plan, targets = _stage_from(physical, ledger_bytes)
        note_target = next(
            item
            for item in targets
            if item.action == "replace" and "records/notes/" in item.artifact
            and item.scope == "authority"
        )
        payload = json.loads(note_target.proposed_bytes.decode("utf-8"))
        if payload.get("display_id") == "N-0001":
            self.assertEqual(payload["links"], ["O-1"])
        capture_target = next(
            item
            for item in targets
            if item.action == "replace" and "records/captures/" in item.artifact
        )
        capture_payload = json.loads(capture_target.proposed_bytes.decode("utf-8"))
        self.assertEqual(capture_payload["task_hints"], ["keep-hint"])
        del plan


class AdversarialActionIdentityTest(unittest.TestCase):
    def test_contradictory_action_display_id_fails_closed_without_roster(self) -> None:
        _conversion, physical, ledger_bytes = _conversion_bundle()
        action = _target_action(_capture_c0001(physical))
        action["task_display_id"] = NEIGHBOR_ID
        before = copy.deepcopy(physical)
        ledger_before = bytes(ledger_bytes)
        with self.assertRaises(V4DeletionStagingError) as ctx:
            _stage_from(physical, ledger_bytes)
        self.assertEqual(ctx.exception.code, "invalid_plan")
        self.assertEqual(str(ctx.exception), "invalid_plan")
        self.assertNotIn(NEIGHBOR_ID, str(ctx.exception))
        self.assertNotIn(TARGET_UID, str(ctx.exception))
        self.assertEqual(physical, before)
        self.assertEqual(ledger_bytes, ledger_before)

    def test_matching_and_null_action_display_ids_remain_valid(self) -> None:
        conversion, physical, ledger_bytes = _conversion_bundle()
        matching = _target_action(_capture_c0001(physical))
        self.assertEqual(matching["task_display_id"], TARGET_ID)
        plan, targets = _stage_from(physical, ledger_bytes)
        self.assertTrue(targets)
        self.assertTrue(any(item.get("op") == "unlink_capture_action" for item in plan.operations))
        physical_null = copy.deepcopy(physical)
        _target_action(_capture_c0001(physical_null))["task_display_id"] = None
        _plan_null, targets_null = _stage_from(physical_null, ledger_bytes)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "authority"
            _write_conversion(root, conversion)
            _overwrite_records(root, physical_null)
            _apply_authority(root, targets_null)
            result = read_v4(root)
            capture = next(
                item for item in result.records["captures"] if item["display_id"] == "C-0001"
            )
            action = capture["normalized"]["action_items"][0]
            self.assertIsNone(action["task_uid"])
            self.assertIsNone(action["task_display_id"])
            self.assertTrue(action["title"])
