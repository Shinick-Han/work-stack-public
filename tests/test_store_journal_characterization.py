from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from workstack.store import Store, StoreCorruptError


def _digest(value: object) -> str:
    body = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _journal() -> dict[str, object]:
    value = {"version": 3, "tasks": []}
    return {
        "version": 1,
        "operation_id": "operation-1",
        "created_at": "2026-08-31T10:00:00Z",
        "writes": [
            {"name": "backlog.json", "value": value, "sha256": _digest(value)}
        ],
    }


class StoreJournalCharacterizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_invalid(self, journal: dict[str, object], message: str) -> None:
        with self.assertRaisesRegex(StoreCorruptError, "^" + re.escape(message) + "$"):
            self.store._validate_journal(journal)

    def test_valid_journal_returns_the_original_write_list_and_accepts_offset_timezone(self) -> None:
        journal = _journal()
        writes = self.store._validate_journal(journal)
        self.assertIs(writes, journal["writes"])
        self.assertIs(writes[0], journal["writes"][0])

        offset = copy.deepcopy(journal)
        offset["created_at"] = "2026-08-31T19:00:00+09:00"
        self.assertEqual(self.store._validate_journal(offset), offset["writes"])

    def test_header_and_timestamp_failures_are_stable(self) -> None:
        cases = (
            ("fields", lambda value: value.update(extra=True), "recovery journal has unknown or missing fields"),
            ("version bool", lambda value: value.update(version=True), "unsupported recovery journal version"),
            ("version", lambda value: value.update(version=2), "unsupported recovery journal version"),
            ("operation type", lambda value: value.update(operation_id=1), "recovery journal operation_id is invalid"),
            ("operation empty", lambda value: value.update(operation_id=""), "recovery journal operation_id is invalid"),
            ("operation long", lambda value: value.update(operation_id="x" * 201), "recovery journal operation_id is invalid"),
            ("created type", lambda value: value.update(created_at=1), "recovery journal created_at is invalid"),
            ("created invalid", lambda value: value.update(created_at="not-a-date"), "recovery journal created_at is invalid"),
            ("created naive", lambda value: value.update(created_at="2026-08-31T10:00:00"), "recovery journal created_at must include a timezone"),
        )
        for label, mutate, message in cases:
            value = _journal()
            mutate(value)
            with self.subTest(label=label):
                self.assert_invalid(value, message)

    def test_writes_envelope_and_entry_failures_are_stable(self) -> None:
        cases = (
            ("writes type", lambda value: value.update(writes={}), "recovery journal writes must be a non-empty array"),
            ("writes empty", lambda value: value.update(writes=[]), "recovery journal writes must be a non-empty array"),
            ("entry type", lambda value: value.update(writes=[None]), "recovery journal write entry is invalid"),
            ("entry fields", lambda value: value["writes"][0].update(extra=True), "recovery journal write entry is invalid"),
            ("unknown target", lambda value: value["writes"][0].update(name="unknown.json"), "recovery journal target is unknown or repeated"),
            ("value", lambda value: value["writes"][0].update(value=[]), "recovery journal target value must be an object"),
            ("digest", lambda value: value["writes"][0].update(sha256="sha256:" + "0" * 64), "recovery journal value digest mismatch"),
        )
        for label, mutate, message in cases:
            value = _journal()
            mutate(value)
            with self.subTest(label=label):
                self.assert_invalid(value, message)

    def test_repeated_target_is_rejected_before_replay(self) -> None:
        journal = _journal()
        journal["writes"].append(copy.deepcopy(journal["writes"][0]))
        self.assert_invalid(journal, "recovery journal target is unknown or repeated")


if __name__ == "__main__":
    unittest.main()
