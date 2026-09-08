from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import connection_activation_evidence as EVIDENCE


PROFILE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROFILE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ACTIVATION_1 = "11111111-1111-4111-8111-111111111111"
ACTIVATION_2 = "22222222-2222-4222-8222-222222222222"
BEFORE = "sha256:" + "a" * 64
AFTER = "sha256:" + "b" * 64
ELSEWHERE = "sha256:" + "c" * 64
PROFILE_DIGEST = "sha256:" + "d" * 64
OTHER_PROFILE_DIGEST = "sha256:" + "e" * 64
PROOF_DIGEST = "sha256:" + "f" * 64


def receipt(activation_id: str, previous: str, activated: str, **changes: object):
    values = {
        "activation_id": activation_id,
        "state": "pending",
        "previous_registry_digest": previous,
        "activated_registry_digest": activated,
        "profile_id": PROFILE_B,
        "profile_digest": PROFILE_DIGEST,
        "proof_digest": PROOF_DIGEST,
        "rollback_file": f"{activation_id}.rollback.json",
    }
    values.update(changes)
    return EVIDENCE.ActivationReceipt(**values)


def classify(*receipts, current: str = AFTER, candidate: str = AFTER):
    return EVIDENCE.classify_activation_attempt(
        receipts,
        current_registry_digest=current,
        candidate_registry_digest=candidate,
        profile_id=PROFILE_B,
        profile_digest=PROFILE_DIGEST,
    )


class ClassifyActivationAttemptTest(unittest.TestCase):
    def test_absent_or_closed_evidence_opens_a_new_activation(self) -> None:
        first = receipt(ACTIVATION_1, BEFORE, AFTER)
        self.assertEqual("create", classify().action)
        for state in ("confirmed", "restored", "superseded"):
            with self.subTest(state=state):
                decision = classify(replace(first, state=state))
                self.assertEqual("create", decision.action)
                self.assertIsNone(decision.receipt)

    def test_same_candidate_retry_resumes_from_either_transaction_side(self) -> None:
        first = receipt(ACTIVATION_1, BEFORE, AFTER)
        for state in ("prepared", "pending"):
            for current in (BEFORE, AFTER):
                with self.subTest(state=state, current=current):
                    decision = classify(replace(first, state=state), current=current)
                    self.assertEqual("resume", decision.action)
                    self.assertEqual(ACTIVATION_1, decision.receipt.activation_id)
                    self.assertEqual(
                        BEFORE, decision.receipt.previous_registry_digest
                    )

    def test_retry_from_an_unrelated_registry_state_is_refused(self) -> None:
        decision = classify(receipt(ACTIVATION_1, BEFORE, AFTER), current=ELSEWHERE)
        self.assertEqual("refuse", decision.action)
        self.assertEqual("activation_conflict", decision.code)

    def test_two_matching_retries_require_explicit_reconciliation(self) -> None:
        decision = classify(
            receipt(ACTIVATION_1, BEFORE, AFTER),
            receipt(ACTIVATION_2, AFTER, AFTER),
        )
        self.assertEqual("refuse", decision.action)
        self.assertEqual("activation_ambiguous", decision.code)

    def test_foreign_attempt_on_the_same_state_is_an_unknown_outcome(self) -> None:
        for foreign in (
            receipt(ACTIVATION_2, BEFORE, AFTER, profile_id=PROFILE_A),
            receipt(ACTIVATION_2, BEFORE, AFTER, profile_digest=OTHER_PROFILE_DIGEST),
        ):
            with self.subTest(foreign=foreign):
                decision = classify(foreign)
                self.assertEqual("refuse", decision.action)
                self.assertEqual("activation_conflict", decision.code)

    def test_unrelated_unconfirmed_activation_must_be_closed_first(self) -> None:
        decision = classify(
            receipt(ACTIVATION_2, BEFORE, ELSEWHERE, profile_id=PROFILE_A),
            current=BEFORE,
        )
        self.assertEqual("refuse", decision.action)
        self.assertEqual("activation_unconfirmed", decision.code)

    def test_a_matching_retry_next_to_other_evidence_still_fails_closed(self) -> None:
        decision = classify(
            receipt(ACTIVATION_1, BEFORE, AFTER),
            receipt(ACTIVATION_2, AFTER, ELSEWHERE, profile_id=PROFILE_A),
        )
        self.assertEqual("refuse", decision.action)
        self.assertEqual("activation_unconfirmed", decision.code)


class PlanActivationReconciliationTest(unittest.TestCase):
    def plan(self, *receipts, current: str = AFTER):
        return EVIDENCE.plan_activation_reconciliation(
            receipts, current_registry_digest=current
        )

    def test_no_evidence_plans_nothing(self) -> None:
        plan = self.plan()
        self.assertEqual("", plan.code)
        self.assertIsNone(plan.keep)
        self.assertEqual((), plan.supersede)

    def test_null_rollback_duplicates_are_superseded_and_ancestry_is_kept(self) -> None:
        root = receipt(ACTIVATION_1, BEFORE, AFTER)
        duplicate = receipt(ACTIVATION_2, AFTER, AFTER)

        plan = self.plan(root, duplicate)

        self.assertEqual("", plan.code)
        self.assertEqual(root, plan.keep)
        self.assertEqual((duplicate,), plan.supersede)

    def test_an_activation_that_never_reached_the_registry_is_superseded(self) -> None:
        unapplied = receipt(ACTIVATION_1, AFTER, ELSEWHERE, state="prepared")

        plan = self.plan(unapplied)

        self.assertIsNone(plan.keep)
        self.assertEqual((unapplied,), plan.supersede)

    def test_closed_receipts_are_never_touched(self) -> None:
        plan = self.plan(
            receipt(ACTIVATION_1, ELSEWHERE, BEFORE, state="confirmed"),
            receipt(ACTIVATION_2, AFTER, AFTER, state="restored"),
        )
        self.assertEqual("", plan.code)
        self.assertIsNone(plan.keep)
        self.assertEqual((), plan.supersede)

    def test_unprovable_or_competing_ancestry_refuses_the_whole_plan(self) -> None:
        for receipts in (
            (receipt(ACTIVATION_1, BEFORE, ELSEWHERE),),
            (
                receipt(ACTIVATION_1, BEFORE, AFTER),
                receipt(ACTIVATION_2, ELSEWHERE, AFTER, profile_id=PROFILE_A),
            ),
        ):
            with self.subTest(receipts=receipts):
                plan = self.plan(*receipts)
                self.assertEqual("activation_manual_review", plan.code)
                self.assertIsNone(plan.keep)
                self.assertEqual((), plan.supersede)


class ActivationAttemptRefusedErrorTest(unittest.TestCase):
    def test_every_code_carries_a_sanitized_message(self) -> None:
        for code in (
            "activation_ambiguous",
            "activation_conflict",
            "activation_unconfirmed",
            "activation_manual_review",
        ):
            with self.subTest(code=code):
                error = EVIDENCE.ActivationAttemptRefusedError(code)
                self.assertEqual(code, error.code)
                self.assertTrue(error.safe_message.endswith("."))
                self.assertIsInstance(error, EVIDENCE.RegistryConflictError)

    def test_an_unknown_code_falls_back_to_manual_review(self) -> None:
        error = EVIDENCE.ActivationAttemptRefusedError("../../etc/passwd")
        self.assertEqual("activation_manual_review", error.code)


if __name__ == "__main__":
    unittest.main()
