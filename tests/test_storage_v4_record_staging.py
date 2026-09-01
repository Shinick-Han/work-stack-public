from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import unittest

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.records import (
    V4RecordStagingError,
    stage_record_delete,
    stage_record_put,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "store-v3" / "populated"
CREATED_AT = "2026-09-01T00:00:00Z"


def _conversion():
    documents = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in FIXTURE.glob("*.json")
    }
    return convert_v3_documents(documents, candidate_created_at=CREATED_AT)


def _digest(value: dict[str, object]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class V4RecordStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task = copy.deepcopy(dict(_conversion().records["tasks"][0]))

    def assert_code(self, code: str, action) -> None:
        with self.assertRaises(V4RecordStagingError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_update_is_deterministic_canonical_and_does_not_mutate_inputs(self) -> None:
        current = copy.deepcopy(self.task)
        proposed = copy.deepcopy(current)
        proposed["title"] = "A reviewed next action"
        proposed["revision"] += 1
        before = copy.deepcopy((current, proposed))
        expected = _digest(current)

        first = stage_record_put(
            "tasks",
            proposed,
            current=current,
            expected_revision=current["revision"],
            expected_digest=expected,
        )
        second = stage_record_put(
            "tasks",
            proposed,
            current=current,
            expected_revision=current["revision"],
            expected_digest=expected,
        )

        self.assertEqual(first, second)
        self.assertEqual(first.body, canonical_json_bytes(proposed))
        self.assertEqual(
            first.artifact,
            f"records/tasks/{proposed['uid'][:2]}/{proposed['uid']}.json",
        )
        self.assertRegex(first.intended_digest or "", r"^sha256:[0-9a-f]{64}$")
        self.assertEqual((current, proposed), before)

    def test_create_requires_no_baseline_and_revision_zero(self) -> None:
        proposed = copy.deepcopy(self.task)
        proposed["uid"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        proposed["display_id"] = "T-9999"
        proposed["revision"] = 0

        staged = stage_record_put(
            "tasks", proposed, current=None, expected_revision=None, expected_digest=None
        )

        self.assertIsNone(staged.expected_revision)
        self.assertIsNone(staged.expected_digest)
        self.assertFalse(staged.deletes_target)

        self.assert_code(
            "CREATE_EXPECTATION_INVALID",
            lambda: stage_record_put(
                "tasks",
                proposed,
                current=None,
                expected_revision=0,
                expected_digest=None,
            ),
        )
        proposed["revision"] = 1
        self.assert_code(
            "CREATE_REVISION_INVALID",
            lambda: stage_record_put(
                "tasks",
                proposed,
                current=None,
                expected_revision=None,
                expected_digest=None,
            ),
        )

    def test_revision_digest_identity_and_contract_conflicts_fail_closed(self) -> None:
        current = copy.deepcopy(self.task)
        proposed = copy.deepcopy(current)
        proposed["revision"] += 1
        expected = _digest(current)

        self.assert_code(
            "STALE_RECORD_REVISION",
            lambda: stage_record_put(
                "tasks",
                proposed,
                current=current,
                expected_revision=current["revision"] + 1,
                expected_digest=expected,
            ),
        )
        self.assert_code(
            "STALE_RECORD_DIGEST",
            lambda: stage_record_put(
                "tasks",
                proposed,
                current=current,
                expected_revision=current["revision"],
                expected_digest="sha256:" + "0" * 64,
            ),
        )
        changed_identity = copy.deepcopy(proposed)
        changed_identity["uid"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.assert_code(
            "RECORD_IDENTITY_CHANGED",
            lambda: stage_record_put(
                "tasks",
                changed_identity,
                current=current,
                expected_revision=current["revision"],
                expected_digest=expected,
            ),
        )
        invalid = copy.deepcopy(proposed)
        invalid["title"] = ""
        self.assert_code(
            "RECORD_CONTRACT_INVALID",
            lambda: stage_record_put(
                "tasks",
                invalid,
                current=current,
                expected_revision=current["revision"],
                expected_digest=expected,
            ),
        )

    def test_delete_preserves_expected_cas_evidence_without_a_body(self) -> None:
        expected = _digest(self.task)
        staged = stage_record_delete(
            "tasks",
            self.task,
            expected_revision=self.task["revision"],
            expected_digest=expected,
        )

        self.assertTrue(staged.deletes_target)
        self.assertIsNone(staged.intended_digest)
        self.assertEqual(staged.expected_digest, expected)


if __name__ == "__main__":
    unittest.main()
