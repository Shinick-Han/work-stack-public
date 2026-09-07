"""Durable Task display-ID high-water authority."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from workstack.service import WorkStack
from workstack.store import Store, StoreCorruptError
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.contracts import require_valid_by_format
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.task_deletion_transaction import commit_v3_hard_delete
from workstack.task_display_id import (
    FIELD,
    MAX_SAFE_INTEGER,
    TaskDisplayIdError,
    admitted_high_water,
    allocate_create,
    format_display_id,
    is_canonical_display_id,
    parse_display_number,
    persist_field,
    read_optional_high_water,
    roster_high_water,
)

from tests.test_storage_migration_conversion import CANDIDATE_CREATED_AT, _load
from tests.test_storage_task_contract import V3TaskBackend, V4TaskBackend


class PolicyUnitTests(unittest.TestCase):
    def test_canonical_parse_ignores_leading_zeros_in_the_numeric_sequence(self) -> None:
        self.assertEqual(parse_display_number("T-0001"), 1)
        self.assertEqual(parse_display_number("T-10000"), 10000)
        self.assertEqual(format_display_id(1), "T-0001")
        self.assertEqual(format_display_id(10000), "T-10000")

    def test_malformed_and_duplicate_numeric_identities_refuse(self) -> None:
        for value in ("t-0001", "T-1", "T-0001a", "O-0001", 1, None, "T-"):
            with self.subTest(value=value):
                with self.assertRaises(TaskDisplayIdError) as caught:
                    parse_display_number(value)
                self.assertEqual(caught.exception.code, "malformed_task_id")
        with self.assertRaises(TaskDisplayIdError) as caught:
            roster_high_water(["T-0001", "T-00001"])
        self.assertEqual(caught.exception.code, "duplicate_numeric_id")

    def test_non_ascii_decimal_digits_are_not_canonical(self) -> None:
        arabic = "T-" + ("\u0660" * 3) + "\u0661"
        fullwidth = "T-" + ("\uff10" * 3) + "\uff11"
        mixed = "T-000\u0661"
        for value in (arabic, fullwidth, mixed, "T-0001\u0660"):
            with self.subTest(value=repr(value)):
                self.assertFalse(is_canonical_display_id(value))
                with self.assertRaises(TaskDisplayIdError) as caught:
                    parse_display_number(value)
                self.assertEqual(caught.exception.code, "malformed_task_id")
                with self.assertRaises(TaskDisplayIdError) as caught:
                    allocate_create(None, [value])
                self.assertEqual(caught.exception.code, "malformed_task_id")
        self.assertEqual(unicodedata.normalize("NFKC", fullwidth), "T-0001")
        self.assertFalse(is_canonical_display_id(fullwidth))
        self.assertTrue(is_canonical_display_id("T-0001"))

    def test_missing_metadata_seeds_from_live_roster_and_create_increments(self) -> None:
        self.assertEqual(admitted_high_water(None, []), 0)
        self.assertEqual(admitted_high_water(None, ["T-0002", "T-0001"]), 2)
        display, water = allocate_create(None, ["T-0002"])
        self.assertEqual(display, "T-0003")
        self.assertEqual(water, 3)

    def test_delete_preserves_high_water_and_never_repairs_a_low_store(self) -> None:
        self.assertEqual(admitted_high_water(9, ["T-0002"]), 9)
        with self.assertRaises(TaskDisplayIdError) as caught:
            admitted_high_water(1, ["T-0002"])
        self.assertEqual(caught.exception.code, "high_water_below_live")

    def test_overflow_and_invalid_json_integers_refuse(self) -> None:
        with self.assertRaises(TaskDisplayIdError) as caught:
            allocate_create(MAX_SAFE_INTEGER, [])
        self.assertEqual(caught.exception.code, "high_water_overflow")
        with self.assertRaises(TaskDisplayIdError) as caught:
            read_optional_high_water({FIELD: True})
        self.assertEqual(caught.exception.code, "high_water_invalid")
        with self.assertRaises(TaskDisplayIdError) as caught:
            read_optional_high_water({FIELD: MAX_SAFE_INTEGER + 1})
        self.assertEqual(caught.exception.code, "high_water_invalid")


class V3HighWaterMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "v3"
        self.root.mkdir()
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)

    def test_create_seeds_persists_and_replay_does_not_increment_twice(self) -> None:
        first = self.stack.create_task_v1({"title": "One"}, "create.hw.0001")
        self.assertEqual(first["body"]["data"]["id"], "T-0001")
        workspace = self.store.load("workspace.json")
        self.assertEqual(workspace[FIELD], 1)
        replay = self.stack.create_task_v1({"title": "One"}, "create.hw.0001")
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(self.store.load("workspace.json")[FIELD], 1)
        second = self.stack.create_task_v1({"title": "Two"}, "create.hw.0002")
        self.assertEqual(second["body"]["data"]["id"], "T-0002")
        self.assertEqual(self.store.load("workspace.json")[FIELD], 2)

    def test_deleting_max_and_non_max_leaves_high_water_and_skips_reuse(self) -> None:
        self.stack.create_task_v1({"title": "One"}, "del.hw.0001")
        self.stack.create_task_v1({"title": "Two"}, "del.hw.0002")
        commit_v3_hard_delete(self.store, "T-0002", 0)
        self.assertEqual(self.store.load("workspace.json")[FIELD], 2)
        ids = {item["id"] for item in self.store.load("backlog.json")["tasks"]}
        self.assertEqual(ids, {"T-0001"})
        third = self.stack.create_task_v1({"title": "Three"}, "del.hw.0003")
        self.assertEqual(third["body"]["data"]["id"], "T-0003")
        commit_v3_hard_delete(self.store, "T-0001", 0)
        self.assertEqual(self.store.load("workspace.json")[FIELD], 3)
        fourth = self.stack.create_task_v1({"title": "Four"}, "del.hw.0004")
        self.assertEqual(fourth["body"]["data"]["id"], "T-0004")

    def test_startup_and_mutation_refuse_stored_high_water_below_live(self) -> None:
        planted = Path(self.temporary.name) / "planted"
        planted.mkdir()
        fixtures = Path(__file__).resolve().parent / "fixtures" / "store-v3" / "populated"
        for path in fixtures.glob("*.json"):
            (planted / path.name).write_bytes(path.read_bytes())
        workspace = json.loads((planted / "workspace.json").read_text(encoding="utf-8"))
        persist_field(workspace, 1)
        (planted / "workspace.json").write_bytes(canonical_json_bytes(workspace))
        before = (planted / "backlog.json").read_bytes()
        with self.assertRaises(StoreCorruptError):
            Store(planted).initialize()
        self.assertEqual((planted / "backlog.json").read_bytes(), before)
        self.assertEqual(
            json.loads((planted / "workspace.json").read_text(encoding="utf-8"))[FIELD],
            1,
        )

    def test_non_ascii_roster_refuses_create_and_does_not_write(self) -> None:
        created = self.stack.create_task_v1({"title": "One"}, "create.hw.ascii.0001")
        self.assertEqual(created["body"]["data"]["id"], "T-0001")
        planted = Path(self.temporary.name) / "unicode-roster"
        planted.mkdir()
        for path in self.root.glob("*.json"):
            (planted / path.name).write_bytes(path.read_bytes())
        backlog = json.loads((planted / "backlog.json").read_text(encoding="utf-8"))
        backlog["tasks"][0]["id"] = "T-" + ("\u0660" * 3) + "\u0661"
        (planted / "backlog.json").write_bytes(canonical_json_bytes(backlog))
        workspace_before = (planted / "workspace.json").read_bytes()
        backlog_before = (planted / "backlog.json").read_bytes()
        with self.assertRaises(StoreCorruptError):
            Store(planted).initialize()
        self.assertEqual((planted / "backlog.json").read_bytes(), backlog_before)
        self.assertEqual((planted / "workspace.json").read_bytes(), workspace_before)
        self.assertEqual(
            json.loads((planted / "workspace.json").read_text(encoding="utf-8"))[FIELD],
            1,
        )


class ConversionHighWaterTests(unittest.TestCase):
    def test_absent_field_stays_absent_and_preserves_frozen_digest(self) -> None:
        documents = _load("populated")
        conversion = convert_v3_documents(
            documents, candidate_created_at=CANDIDATE_CREATED_AT
        )
        self.assertNotIn(FIELD, conversion.store)
        self.assertNotIn(FIELD, conversion.workspace)
        require_valid_by_format(conversion.store)

    def test_present_field_is_copied_exactly_and_shuffle_stable(self) -> None:
        documents = _load("populated")
        persist_field(documents["workspace.json"], 9)
        first = convert_v3_documents(
            documents, candidate_created_at=CANDIDATE_CREATED_AT
        )
        shuffled = copy.deepcopy(documents)
        shuffled["backlog.json"]["tasks"].reverse()
        second = convert_v3_documents(
            shuffled, candidate_created_at=CANDIDATE_CREATED_AT
        )
        self.assertEqual(first.store[FIELD], 9)
        self.assertEqual(second.store[FIELD], 9)
        self.assertEqual(first.conversion_digest, second.conversion_digest)
        self.assertNotIn(FIELD, first.workspace)
        require_valid_by_format(first.store)

    def test_low_high_water_refuses_conversion(self) -> None:
        documents = _load("populated")
        persist_field(documents["workspace.json"], 1)
        with self.assertRaisesRegex(Exception, "INVALID_V3_SOURCE"):
            convert_v3_documents(documents, candidate_created_at=CANDIDATE_CREATED_AT)

    def test_non_ascii_digits_refuse_conversion(self) -> None:
        documents = _load("populated")
        persist_field(documents["workspace.json"], 9)
        documents["backlog.json"]["tasks"][0]["id"] = "T-" + ("\uff10" * 3) + "\uff11"
        with self.assertRaisesRegex(Exception, "INVALID_V3_SOURCE"):
            convert_v3_documents(documents, candidate_created_at=CANDIDATE_CREATED_AT)


class V4HighWaterMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime_environment = mock.patch.dict(
            os.environ, {"WORK_STACK_RUNTIME": str(Path(self.temporary.name) / "runtime")}
        )
        self.runtime_environment.start()
        self.addCleanup(self.runtime_environment.stop)
        self.backend = V4TaskBackend(Path(self.temporary.name) / "authority")

    def test_v4_create_materializes_store_metadata_and_increments(self) -> None:
        first = self.backend.create({"title": "One"}, "v4.hw.0001")
        self.assertEqual(first["status"], 201)
        store = json.loads((self.backend.root / "store.json").read_text(encoding="utf-8"))
        self.assertEqual(first["body"]["data"]["id"], "T-0001")
        self.assertEqual(store[FIELD], 1)
        require_valid_by_format(store)
        replay = self.backend.create({"title": "One"}, "v4.hw.0001")
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(
            json.loads((self.backend.root / "store.json").read_text(encoding="utf-8"))[FIELD],
            1,
        )
        second = self.backend.create({"title": "Two"}, "v4.hw.0002")
        self.assertEqual(second["body"]["data"]["id"], "T-0002")
        self.assertEqual(
            json.loads((self.backend.root / "store.json").read_text(encoding="utf-8"))[FIELD],
            2,
        )


class V3BackendContractHighWaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.backend = V3TaskBackend(Path(self.temporary.name) / "authority")

    def test_contract_backend_persists_workspace_high_water(self) -> None:
        result = self.backend.create({"title": "Contract"}, "task.hw.contract.0001")
        self.assertEqual(result["body"]["data"]["id"], "T-0001")
        self.assertEqual(self.backend.store.load("workspace.json")[FIELD], 1)


if __name__ == "__main__":
    unittest.main()
