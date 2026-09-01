from __future__ import annotations

import copy
import unittest

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.idempotency import (
    IdempotencyLedgerError,
    append_idempotency_record,
    compact_idempotency_ledger,
    new_idempotency_ledger,
    parse_idempotency_ledger,
    stage_idempotency_ledger,
)


WORKSPACE_UID = "11111111-1111-1111-1111-111111111111"


def _record(key: str = "operation.0001") -> dict:
    return {
        "key": key,
        "method": "POST",
        "path": "/api/v1/tasks",
        "request_digest": "sha256:" + "a" * 64,
        "response_status": 201,
        "created_at": "2026-09-01T06:00:00Z",
        "expires_at": "2026-10-01T06:00:00Z",
        "response_body": {"data": {"id": "T-0001"}},
    }


class IdempotencyLedgerTests(unittest.TestCase):
    def test_create_append_round_trip_and_runtime_stage(self) -> None:
        ledger = new_idempotency_ledger(
            WORKSPACE_UID, updated_at="2026-09-01T06:00:00Z"
        )
        appended, replayed = append_idempotency_record(
            ledger, _record(), now="2026-09-01T06:00:00Z"
        )
        self.assertFalse(replayed)
        body = canonical_json_bytes(appended)
        self.assertEqual(parse_idempotency_ledger(body), appended)
        staged = stage_idempotency_ledger(appended, current_body=None)
        self.assertEqual(staged.scope, "runtime")
        self.assertEqual(staged.artifact, "idempotency-ledger.v1.json")
        self.assertEqual(staged.proposed_bytes, body)

    def test_exact_duplicate_replays_but_changed_request_conflicts(self) -> None:
        ledger = new_idempotency_ledger(
            WORKSPACE_UID, updated_at="2026-09-01T06:00:00Z"
        )
        ledger, _ = append_idempotency_record(
            ledger, _record(), now="2026-09-01T06:00:00Z"
        )
        unchanged, replayed = append_idempotency_record(
            ledger, _record(), now="2026-09-01T06:00:00Z"
        )
        self.assertTrue(replayed)
        self.assertEqual(unchanged, ledger)
        conflict = _record()
        conflict["request_digest"] = "sha256:" + "b" * 64
        with self.assertRaisesRegex(IdempotencyLedgerError, "IDEMPOTENCY_KEY_CONFLICT"):
            append_idempotency_record(
                ledger, conflict, now="2026-09-01T06:00:00Z"
            )

    def test_compaction_removes_only_expired_records(self) -> None:
        ledger = new_idempotency_ledger(
            WORKSPACE_UID, updated_at="2026-09-01T06:00:00Z"
        )
        expired = _record("operation.old1")
        expired["created_at"] = "2026-07-01T06:00:00Z"
        expired["expires_at"] = "2026-07-31T06:00:00Z"
        ledger["records"] = [expired, _record()]
        compacted = compact_idempotency_ledger(
            ledger, now="2026-09-01T06:00:00Z"
        )
        self.assertEqual([item["key"] for item in compacted["records"]], ["operation.0001"])

    def test_wrong_expiry_workspace_and_noncanonical_bytes_fail_closed(self) -> None:
        ledger = new_idempotency_ledger(
            WORKSPACE_UID, updated_at="2026-09-01T06:00:00Z"
        )
        ledger["records"] = [_record()]
        wrong = copy.deepcopy(ledger)
        wrong["records"][0]["expires_at"] = "2026-09-02T06:00:00Z"
        with self.assertRaisesRegex(IdempotencyLedgerError, "RECORD_EXPIRY_POLICY_MISMATCH"):
            parse_idempotency_ledger(canonical_json_bytes(wrong))
        with self.assertRaisesRegex(IdempotencyLedgerError, "LEDGER_WORKSPACE_MISMATCH"):
            parse_idempotency_ledger(
                canonical_json_bytes(ledger),
                expected_workspace_uid="22222222-2222-2222-2222-222222222222",
            )
        with self.assertRaisesRegex(IdempotencyLedgerError, "LEDGER_CANONICAL_BYTES_REQUIRED"):
            parse_idempotency_ledger(canonical_json_bytes(ledger) + b"\n")


if __name__ == "__main__":
    unittest.main()
