from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from workstack.storage.contracts import require_valid_by_format
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.migration_idempotency import (
    MAX_LEDGER_RECORDS,
    MAX_REPLY_ROSTER,
    IdempotencyLedgerConversionError,
    convert_v3_idempotency_ledger,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "store-v3" / "populated"
CREATED_AT = "2026-09-01T06:00:00Z"


def _documents() -> dict[str, dict]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in FIXTURE.glob("*.json")
    }


def _conversion():
    return convert_v3_documents(_documents(), candidate_created_at=CREATED_AT)


def _convert(records, replies=None, *, updated_at=CREATED_AT):
    conversion = _conversion()
    return convert_v3_idempotency_ledger(
        records,
        workspace_uid=conversion.store["workspace_uid"],
        updated_at=updated_at,
        replies=conversion.records["replies"] if replies is None else replies,
    )


class StorageMigrationIdempotencyTests(unittest.TestCase):
    def test_fixture_converts_to_bounded_schema_valid_runtime_ledger(self) -> None:
        source = _documents()["activity.json"]["idempotency"]
        before = copy.deepcopy(source)

        ledger = _convert(source)

        require_valid_by_format(ledger)
        self.assertEqual(source, before)
        self.assertEqual(
            ledger["compaction_policy"],
            {"retention_days": 30, "max_records": 10_000},
        )
        self.assertEqual(
            [record["expires_at"] for record in ledger["records"]],
            ["2026-10-01T02:00:00Z", "2026-10-01T02:10:00Z"],
        )
        reply_record = _conversion().records["replies"][0]
        self.assertEqual(
            ledger["records"][1]["response_ref"],
            {"kind": "reply", "record_uid": reply_record["uid"]},
        )
        self.assertNotIn("id", ledger["records"][1]["response_ref"])

    def test_empty_and_shuffled_inputs_are_deterministic(self) -> None:
        source = _documents()["activity.json"]["idempotency"]
        self.assertEqual(_convert([])["records"], [])
        self.assertEqual(_convert(source), _convert(list(reversed(source))))

    def test_duplicate_keys_and_record_limit_fail_closed(self) -> None:
        record = _documents()["activity.json"]["idempotency"][0]
        with self.assertRaises(IdempotencyLedgerConversionError) as duplicate:
            _convert([record, copy.deepcopy(record)])
        self.assertEqual(duplicate.exception.code, "IDEMPOTENCY_KEY_DUPLICATE")

        with self.assertRaises(IdempotencyLedgerConversionError) as oversized:
            _convert([record] * (MAX_LEDGER_RECORDS + 1))
        self.assertEqual(
            oversized.exception.code, "IDEMPOTENCY_RECORD_LIMIT_EXCEEDED"
        )

    def test_reply_reference_must_resolve_to_one_unique_v4_uid(self) -> None:
        source = _documents()["activity.json"]["idempotency"]
        replies = list(_conversion().records["replies"])
        missing = copy.deepcopy(source[1])
        missing["response_ref"]["id"] = "R-9999"
        with self.assertRaises(IdempotencyLedgerConversionError) as unresolved:
            _convert([missing], replies)
        self.assertEqual(unresolved.exception.code, "REPLY_REFERENCE_UNRESOLVED")

        with self.assertRaises(IdempotencyLedgerConversionError) as duplicate_display:
            _convert([], replies + [copy.deepcopy(replies[0])])
        self.assertEqual(
            duplicate_display.exception.code, "REPLY_DISPLAY_ID_DUPLICATE"
        )

        duplicate_uid = copy.deepcopy(replies[0])
        duplicate_uid["display_id"] = "R-9999"
        with self.assertRaises(IdempotencyLedgerConversionError) as duplicate_stable:
            _convert([], replies + [duplicate_uid])
        self.assertEqual(duplicate_stable.exception.code, "REPLY_UID_DUPLICATE")

        with self.assertRaises(IdempotencyLedgerConversionError) as oversized:
            _convert([], [replies[0]] * (MAX_REPLY_ROSTER + 1))
        self.assertEqual(oversized.exception.code, "REPLY_ROSTER_INVALID")

    def test_invalid_response_forms_and_unknown_fields_are_not_silently_dropped(self) -> None:
        body = copy.deepcopy(_documents()["activity.json"]["idempotency"][0])
        reference = copy.deepcopy(_documents()["activity.json"]["idempotency"][1])
        neither = {key: value for key, value in body.items() if key != "response_body"}
        both = {**body, "response_ref": reference["response_ref"]}
        unknown = {**body, "private_extension": "not retained"}
        for source in (neither, both):
            with self.subTest(source=set(source)):
                with self.assertRaises(IdempotencyLedgerConversionError) as caught:
                    _convert([source])
                self.assertEqual(caught.exception.code, "IDEMPOTENCY_RESPONSE_FORM_INVALID")
        with self.assertRaises(IdempotencyLedgerConversionError) as fields:
            _convert([unknown])
        self.assertEqual(fields.exception.code, "IDEMPOTENCY_RECORD_FIELDS_INVALID")

    def test_bad_timestamps_and_unrepresentable_runtime_values_fail_closed(self) -> None:
        record = copy.deepcopy(_documents()["activity.json"]["idempotency"][0])
        record["created_at"] = "not-a-timestamp"
        with self.assertRaises(IdempotencyLedgerConversionError) as timestamp:
            _convert([record])
        self.assertEqual(timestamp.exception.code, "IDEMPOTENCY_TIMESTAMP_INVALID")

        record["created_at"] = "9999-12-31T23:59:59Z"
        with self.assertRaises(IdempotencyLedgerConversionError) as overflow:
            _convert([record])
        self.assertEqual(overflow.exception.code, "IDEMPOTENCY_TIMESTAMP_INVALID")

        record = copy.deepcopy(_documents()["activity.json"]["idempotency"][0])
        record["response_body"]["data"]["unsupported_float"] = 1.5
        with self.assertRaises(IdempotencyLedgerConversionError) as value:
            _convert([record])
        self.assertEqual(
            value.exception.code, "IDEMPOTENCY_RECORD_UNREPRESENTABLE"
        )

        with self.assertRaises(IdempotencyLedgerConversionError) as updated:
            _convert([], updated_at="not-a-timestamp")
        self.assertEqual(
            updated.exception.code, "IDEMPOTENCY_RECORD_UNREPRESENTABLE"
        )

    def test_fractional_timestamp_retains_instant_and_adds_exactly_thirty_days(self) -> None:
        record = copy.deepcopy(_documents()["activity.json"]["idempotency"][0])
        record["created_at"] = "2024-02-15T23:59:59.123456Z"
        ledger = _convert([record])
        self.assertEqual(
            ledger["records"][0]["expires_at"], "2024-03-16T23:59:59.123456Z"
        )


if __name__ == "__main__":
    unittest.main()
