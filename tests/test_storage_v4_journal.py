from __future__ import annotations

import json
import unittest

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.journal import (
    JournalTarget,
    JournalV2Error,
    advance_journal_phase,
    build_write_journal,
    parse_write_journal,
)


WORKSPACE_UID = "11111111-1111-1111-1111-111111111111"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _journal():
    return build_write_journal(
        workspace_uid=WORKSPACE_UID,
        operation_id="operation.0001",
        created_at="2026-09-01T06:00:00Z",
        base_generation=7,
        base_manifest_digest=DIGEST_A,
        proposed_manifest_digest=DIGEST_B,
        targets=(
            JournalTarget.replace("records/tasks/22/22222222-2222-2222-2222-222222222222.json", b"two", expected_digest=None),
            JournalTarget.replace("workspace.json", b"one", expected_digest=DIGEST_A),
        ),
    )


class WriteJournalV2Tests(unittest.TestCase):
    def test_round_trip_is_canonical_complete_and_deterministically_ordered(self) -> None:
        journal = _journal()
        parsed = parse_write_journal(journal.canonical_bytes)
        self.assertEqual(parsed.digest, journal.digest)
        self.assertEqual(
            [(target.scope, target.artifact) for target in parsed.targets],
            sorted((target.scope, target.artifact) for target in parsed.targets),
        )
        self.assertEqual([target.proposed_bytes for target in parsed.targets], [b"two", b"one"])
        self.assertEqual(parsed.value["proposed_generation"], 8)

    def test_target_cas_records_absence_and_exact_previous_digest(self) -> None:
        targets = _journal().targets
        self.assertIsNone(targets[0].expected_digest)
        self.assertEqual(targets[1].expected_digest, DIGEST_A)

    def test_tampered_payload_fails_closed(self) -> None:
        value = json.loads(_journal().canonical_bytes)
        value["targets"][0]["proposed_base64"] = "dGFtcGVyZWQ="
        with self.assertRaisesRegex(JournalV2Error, "TARGET_CONTENT_MISMATCH"):
            parse_write_journal(canonical_json_bytes(value))

    def test_noncanonical_and_path_traversal_are_rejected(self) -> None:
        body = _journal().canonical_bytes + b"\n"
        with self.assertRaisesRegex(JournalV2Error, "JOURNAL_CANONICAL_BYTES_REQUIRED"):
            parse_write_journal(body)
        with self.assertRaisesRegex(JournalV2Error, "ARTIFACT_INVALID"):
            JournalTarget.replace("../private.json", b"x", expected_digest=None)
        with self.assertRaisesRegex(JournalV2Error, "TARGET_SCOPE_INVALID"):
            JournalTarget.replace(
                "idempotency-ledger.v1.json",
                b"x",
                expected_digest=None,
                scope="authority-and-runtime",
            )

    def test_duplicate_json_keys_are_rejected(self) -> None:
        body = _journal().canonical_bytes
        duplicate = body.replace(
            b'"format":"workstack.write-journal"',
            b'"format":"workstack.write-journal","format":"workstack.write-journal"',
            1,
        )
        with self.assertRaisesRegex(JournalV2Error, "JOURNAL_JSON_INVALID"):
            parse_write_journal(duplicate)

    def test_generation_and_phase_cannot_regress(self) -> None:
        journal = advance_journal_phase(_journal(), "applying")
        with self.assertRaisesRegex(JournalV2Error, "PHASE_REGRESSION"):
            advance_journal_phase(journal, "prepared")
        value = dict(journal.value)
        value["proposed_generation"] = value["base_generation"]
        with self.assertRaisesRegex(JournalV2Error, "GENERATION_TRANSITION_INVALID"):
            parse_write_journal(canonical_json_bytes(value))

    def test_duplicate_targets_and_invalid_digest_are_rejected(self) -> None:
        target = JournalTarget.replace("workspace.json", b"one", expected_digest=DIGEST_A)
        with self.assertRaisesRegex(JournalV2Error, "TARGET_ROSTER_INVALID"):
            build_write_journal(
                workspace_uid=WORKSPACE_UID,
                operation_id="operation.0002",
                created_at="2026-09-01T06:00:00Z",
                base_generation=0,
                base_manifest_digest=DIGEST_A,
                proposed_manifest_digest=DIGEST_B,
                targets=(target, target),
            )
        with self.assertRaisesRegex(JournalV2Error, "EXPECTED_DIGEST_INVALID"):
            JournalTarget.replace("workspace.json", b"one", expected_digest="private")


if __name__ == "__main__":
    unittest.main()
