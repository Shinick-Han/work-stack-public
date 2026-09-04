from __future__ import annotations

import copy
from decimal import localcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from workstack.context_projection import group_context_by_task, project_context_items
from workstack.service import WorkStack
from workstack.store import Store
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.query_repository import WorkspaceQueryRepository
from workstack.storage.read_repository import V3WorkspaceRepository, V4WorkspaceRepository
from workstack.storage.repository import V4ReadOnlyStoreAdapter
from tests.test_storage_intent_dual_backend import _write_conversion
from tests.test_storage_semantic_parity import FIXTURES, _load


def keys(items):
    return ["{}:{}".format(item["ref"]["kind"], item["ref"]["id"]) for item in items]


class ContextProjectionTest(unittest.TestCase):
    def setUp(self):
        self.notes = [
            {"id": "same", "text": "Same text", "links": ["T-1", "T-2", "O-1", "missing", "T-1"], "created": "2026-09-02"},
            {"id": "orphan", "text": "Orphan", "links": [], "created": "2026-09-02"},
            {"id": "objective", "text": "Objective only", "links": ["O-1"], "created": "2026-09-02"},
        ]
        self.captures = [
            {"id": "same", "source": {"web_url": "https://example.invalid/a"},
             "normalized": {"context": "Same text"}, "provenance": {"capture_mode": "manual"},
             "linked_task_ids": ["T-1", "T-1"], "converted_task_ids": ["T-1", "T-2"],
             "status": "dismissed", "created_at": "2026-09-02T01:00:00Z"},
            {"id": "other", "source": {"web_url": "https://example.invalid/a"},
             "normalized": {"context": "Same text"}, "linked_task_ids": ["T-1"],
             "converted_task_ids": [], "created_at": "2026-09-02T01:00:00Z"},
        ]

    def test_distinct_typed_identity_and_all_connection_reasons(self):
        grouped = group_context_by_task(self.notes, self.captures, ["T-1", "T-2", "T-3"], ["O-1"])
        self.assertEqual(keys(grouped["T-1"]), ["capture:other", "capture:same", "note:same"])
        self.assertEqual(keys(grouped["T-2"]), ["capture:same", "note:same"])
        self.assertEqual(grouped["T-3"], [])
        capture = grouped["T-1"][1]
        self.assertEqual(capture["connections"], [
            {"target": {"kind": "task", "id": "T-1"}, "reasons": ["capture-link", "capture-conversion"]},
            {"target": {"kind": "task", "id": "T-2"}, "reasons": ["capture-conversion"]},
        ])
        self.assertEqual(capture["status"], "dismissed")
        self.assertEqual(grouped["T-1"][2]["connections"], [
            {"target": {"kind": "objective", "id": "O-1"}, "reasons": ["note-link"]},
            {"target": {"kind": "task", "id": "T-1"}, "reasons": ["note-link"]},
            {"target": {"kind": "task", "id": "T-2"}, "reasons": ["note-link"]},
        ])

    def test_original_fields_provenance_and_inputs_are_preserved(self):
        before = copy.deepcopy((self.notes, self.captures))
        projected = project_context_items(self.notes, self.captures, ["T-1", "T-2"], ["O-1"])
        self.assertEqual(len(projected), 5)
        self.assertIn("note:orphan", keys(projected))
        self.assertIn("note:objective", keys(projected))
        for kind, records in (("note", self.notes), ("capture", self.captures)):
            for original in records:
                item = next(item for item in projected if item["ref"] == {"kind": kind, "id": original["id"]})
                self.assertEqual({key: item[key] for key in original}, original)
        self.assertEqual((self.notes, self.captures), before)
        projected[0]["source"]["web_url"] = "changed only in result"
        self.assertEqual((self.notes, self.captures), before)

    def test_repeated_record_is_one_identity_but_conflicting_record_refuses(self):
        projected = project_context_items(self.notes + [copy.deepcopy(self.notes[0])], [], ["T-1"], ["O-1"])
        self.assertEqual(len(projected), 3)
        with self.assertRaisesRegex(ValueError, "conflicting shared context identity"):
            project_context_items(self.notes + [{**self.notes[0], "text": "conflict"}], [], ["T-1"], [])

    def test_offset_instants_dates_and_unknowns_have_one_timezone_independent_order(self):
        notes = [
            {"id": "day2", "text": "Day", "links": [], "created": "2026-09-02"},
            {"id": "day1", "text": "Day", "links": [], "created": "2026-09-01"},
            {"id": "invalid", "text": "Invalid", "links": [], "created": "2026-02-30"},
            {"id": "missing", "text": "Missing", "links": []},
        ]
        captures = [
            {"id": "a", "created_at": "2026-09-02T01:00:00+09:00"},
            {"id": "z", "created_at": "2026-09-01T16:00:00Z"},
            {"id": "later", "created_at": "2026-09-01T17:00:00Z"},
            {"id": "naive", "created_at": "2026-09-02T20:00:00"},
        ]
        result = project_context_items(notes, captures, [], [])
        self.assertEqual(keys(result), [
            "note:day2", "capture:later", "capture:a", "capture:z", "note:day1",
            "capture:naive", "note:invalid", "note:missing",
        ])
        self.assertEqual([item["date_precision"] for item in result], [
            "date", "instant", "instant", "instant", "date", "unknown", "unknown", "unknown",
        ])
        self.assertEqual(result[2]["created_at"], "2026-09-02T01:00:00+09:00")
        self.assertEqual(result[0]["created"], "2026-09-02")

    def test_invalid_offsets_and_naive_times_do_not_gain_invented_instants(self):
        for created in (
            "2026-09-02T01:00:00+00:99", "2026-09-02T01:00:00+24:00",
            "2026-09-02T01:00:00", "2026-02-30T01:00:00Z", "not a date",
        ):
            with self.subTest(created=created):
                result = project_context_items([], [{"id": "C-1", "created_at": created}], [], [])
                self.assertEqual(result[0]["date_precision"], "unknown")
                self.assertEqual(result[0]["created_at"], created)

    def test_fractional_instants_keep_every_digit_before_typed_key_tiebreak(self):
        for older, newer in (
            ("123456789", "123456790"),
            ("1234567", "1234568"),
            ("1", "100000001"),
            ("000000001", "000000002"),
            ("123456789012345678901234567890123456788", "123456789012345678901234567890123456789"),
        ):
            with self.subTest(older=older, newer=newer):
                captures = [
                    {"id": "a-older", "created_at": "2026-09-02T01:00:00." + older + "Z"},
                    {"id": "z-newer", "created_at": "2026-09-02T01:00:00." + newer + "Z"},
                ]
                before = copy.deepcopy(captures)
                # Ordering must not depend on the process decimal precision either.
                with localcontext() as context:
                    context.prec = 6
                    result = project_context_items([], captures, [], [])
                self.assertEqual(keys(result), ["capture:z-newer", "capture:a-older"])
                self.assertEqual([item["date_precision"] for item in result], ["instant", "instant"])
                self.assertEqual([item["created_at"] for item in result], [
                    captures[1]["created_at"], captures[0]["created_at"],
                ])
                self.assertEqual(captures, before)

    def test_equal_fractional_instants_use_typed_keys_across_offset_and_zero_suffixes(self):
        notes = [{"id": "a", "created": "2026-09-02T01:00:00.123456789000Z"}]
        captures = [
            {"id": "z", "created_at": "2026-09-01T20:00:00.123456789-05:00"},
            {"id": "a", "created_at": "2026-09-02T10:00:00.1234567890+09:00"},
        ]
        result = project_context_items(notes, captures, [], [])
        self.assertEqual(keys(result), ["capture:a", "capture:z", "note:a"])
        self.assertEqual([item["date_precision"] for item in result], ["instant"] * 3)
        zeros = [{"id": name, "created_at": timestamp} for name, timestamp in (
            ("z", "2026-09-02T01:00:00Z"),
            ("a", "2026-09-02T01:00:00.000000000000Z"),
        )]
        self.assertEqual(keys(project_context_items([], zeros, [], [])), ["capture:a", "capture:z"])

    def test_fractional_offsets_group_by_utc_day_without_promoting_civil_dates(self):
        notes = [
            {"id": "day2", "created": "2026-09-02"},
            {"id": "day1", "created": "2026-09-01"},
            {"id": "unknown"},
        ]
        captures = [
            {"id": "a-before-midnight", "created_at": "2026-09-02T00:59:59.999999999+01:00"},
            {"id": "z-after-midnight", "created_at": "2026-09-01T19:00:00.000000001-05:00"},
            {"id": "invalid", "created_at": "2026-09-02T01:00:00.123456789+00:99"},
        ]
        before = copy.deepcopy((notes, captures))
        result = project_context_items(notes, captures, [], [])
        self.assertEqual(keys(result), [
            "capture:z-after-midnight", "note:day2", "capture:a-before-midnight", "note:day1",
            "capture:invalid", "note:unknown",
        ])
        self.assertEqual([item["date_precision"] for item in result], [
            "instant", "date", "instant", "date", "unknown", "unknown",
        ])
        self.assertEqual((notes, captures), before)


class ContextServiceProjectionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        data = (self.base / "data").resolve()
        runtime_base = (self.base / "runtime").resolve()
        root_key = hashlib.sha256(os.path.normcase(str(data)).encode("utf-8")).hexdigest()[:20]
        runtime = (runtime_base / root_key).resolve()
        admitted = Path(os.environ.get("WORK_STACK_TEST_RESULT_ROOT", tempfile.gettempdir())).resolve()
        for destination in (self.base, data, runtime):
            self.assertTrue(destination.is_relative_to(admitted))
        environment = mock.patch.dict(os.environ, {
            "WORK_STACK_HOME": str(data), "WORK_STACK_RUNTIME": str(runtime_base),
            "LOCALAPPDATA": str(self.base / "local-app-data"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.store = Store(data)
        self.stack = WorkStack(self.store)
        self.first = self.stack.add_task("First")
        self.second = self.stack.add_task("Second")
        self.objective = self.stack.add_objective("Context objective", "2026-Q3")
        self.note = self.stack.add_note("Shared context", [self.first["id"], self.second["id"], self.objective["id"]])
        self.stack.add_note("Orphan")
        self.stack.add_note("Objective only", [self.objective["id"]])
        self.stack.add_task_note_v1(
            self.first["id"], {"text": "Embedded annotation", "revision": 0},
            "context.embedded.0001", path="/api/v1/tasks/{}/notes".format(self.first["id"]),
        )
        packet_path = Path(__file__).resolve().parents[1] / "contracts" / "capture-packet-v1.fixture.json"
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        self.capture = self.stack.ingest_capture(packet, "context.ingest.0001")["body"]["data"]
        self.stack.link_capture(self.capture["id"], self.first["id"], "context.link.0001")
        self.stack.dismiss_capture(self.capture["id"], "context.dismiss.0001")

    def _bytes(self):
        return {str(path): path.read_bytes() for root in (self.store.root, self.store.runtime_root)
                for path in root.iterdir() if path.is_file()}

    def test_shared_note_count_detail_and_capture_reply_fields_agree_without_writes(self):
        before = self._bytes()
        workspace = self.stack.workspace_projection()
        counts = {task["id"]: task["context_count"] for task in workspace["tasks"]}
        self.assertEqual(counts, {self.first["id"]: 2, self.second["id"]: 1})
        for task in workspace["tasks"]:
            detail = self.stack.task_detail(task["id"])
            self.assertEqual(set(detail), {"task", "context", "activity", "replies"})
            self.assertEqual(task["context_count"], len(detail["context"]))
            self.assertEqual(detail["task"]["context_count"], len(detail["context"]))
        detail = self.stack.task_detail(self.first["id"])
        capture = next(item for item in detail["context"] if item["ref"]["kind"] == "capture")
        self.assertEqual(capture["source"], self.capture["source"])
        self.assertEqual(capture["provenance"], self.capture["provenance"])
        self.assertEqual(capture["normalized"], self.capture["normalized"])
        self.assertEqual(capture["status"], "dismissed")
        self.assertEqual(workspace["notes"], self.store.load("notes.json")["notes"])
        self.assertEqual(before, self._bytes())

    def test_optional_query_backend_keeps_shared_context_counts(self):
        query_root = self.base / "query"
        query = WorkspaceQueryRepository(V3WorkspaceRepository(self.store), query_root)
        queried = WorkStack(self.store, initialize=False, query_commands=query)
        self.assertEqual(queried.workspace_projection(), self.stack.workspace_projection())
        self.assertEqual(queried.task_detail(self.first["id"]), self.stack.task_detail(self.first["id"]))

    def test_v4_read_projection_preserves_nonempty_note_capture_counts_and_detail(self):
        # Reuse the established schema-valid v3/v4 parity fixture; this test
        # exercises the new read contract, not the migration of new fixture shapes.
        documents = _load("populated")
        v3_root = self.base / "parity-v3"
        shutil.copytree(FIXTURES / "populated", v3_root)
        v3 = WorkStack(Store(v3_root))
        v3.query_commands = WorkspaceQueryRepository(V3WorkspaceRepository(v3.store), self.base / "v3-query")
        conversion = convert_v3_documents(documents, candidate_created_at="2026-09-02T00:00:00Z")
        authority = self.base / "v4"
        authority.mkdir()
        _write_conversion(authority, conversion)
        reader = V4WorkspaceRepository(
            authority, idempotency_ledger=conversion.idempotency_ledger,
            task_note_source_indexes=conversion.task_note_source_indexes, generation=0,
        )
        query = WorkspaceQueryRepository(reader, self.base / "v4-query")
        projected = WorkStack(V4ReadOnlyStoreAdapter(reader.read().snapshot), initialize=False, query_commands=query)
        self.assertEqual(projected.workspace_projection(), v3.workspace_projection())
        seen_kinds = set()
        for task in v3.workspace_projection()["tasks"]:
            detail = projected.task_detail(task["id"])
            self.assertEqual(detail, v3.task_detail(task["id"]))
            self.assertEqual(task["context_count"], len(detail["context"]))
            seen_kinds.update(item["ref"]["kind"] for item in detail["context"])
        self.assertEqual(seen_kinds, {"note", "capture"})


if __name__ == "__main__":
    unittest.main()
