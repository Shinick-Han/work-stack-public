"""One owner incarnation's attempt guard: real grammar, real concurrency.

Every identity here comes from the released
:func:`workstack.knowledge_request_issuer.derive_request_id` and every digest
from the released :func:`workstack.knowledge_owner_requests.request_digest`, so
a green run says the guard admits exactly what the ledger records rather than a
shape invented for the test. No Store, no server, no socket, no subprocess and
no fixture directory is touched.

A green run says: one identity can be spent at most once even when many threads
race for it; a replayed registration cannot rearm a spent identity; a digest
that does not match refuses without spending; the ledger's own 200-record bound
is the capacity; a fresh instance shares nothing with an old one; and every
refusal carries a closed code and none of the submitted values.

It does not say the guard proves authority, state, expiry or Task binding --
it proves none of those, and the caller is contracted to have checked them.
"""

from __future__ import annotations

import threading
import unittest
import uuid

from workstack.capture import SHA256_RE
from workstack.knowledge_attempt_guard import (
    KnowledgeAttemptError,
    KnowledgeAttemptGuard,
)
from workstack.knowledge_ledger_document import MAX_REQUESTS
from workstack.knowledge_owner_requests import request_digest
from workstack.knowledge_request_issuer import derive_request_id

WORKSPACE_UID = "6f1b8f52-9d0e-4c1a-9a3f-2b7c5e8d4a10"


def released_request_id(seed: int) -> str:
    """A real derived request identity, exactly as the issuer would derive it."""

    return derive_request_id(WORKSPACE_UID, str(uuid.UUID(int=seed + 1)))


def released_digest(marker: str) -> str:
    """A real ledger request digest over a closed projection."""

    return request_digest({"marker": marker, "result_limit": 5})


class AttemptGuardGrammarTests(unittest.TestCase):
    """The guard admits the released spellings and nothing looser."""

    def setUp(self) -> None:
        self.guard = KnowledgeAttemptGuard()
        self.request_id = released_request_id(0)
        self.digest = released_digest("grammar")

    def test_released_helpers_produce_the_admitted_shapes(self) -> None:
        self.assertEqual(str(uuid.UUID(self.request_id)), self.request_id)
        self.assertTrue(SHA256_RE.fullmatch(self.digest))

    def test_registered_identity_is_consumable_once(self) -> None:
        self.assertIsNone(self.guard.register_new(self.request_id, self.digest))
        self.assertIsNone(self.guard.consume(self.request_id, self.digest))
        with self.assertRaises(KnowledgeAttemptError) as raised:
            self.guard.consume(self.request_id, self.digest)
        self.assertEqual(raised.exception.code, "request_already_attempted")

    def test_noncanonical_identities_refuse_before_any_mutation(self) -> None:
        upper = self.request_id.upper()
        for value in (
            upper,
            "{" + self.request_id + "}",
            "urn:uuid:" + self.request_id,
            str(uuid.UUID(int=0)),
            self.request_id + "\n",
            b"not-text",
            None,
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(KnowledgeAttemptError) as raised:
                    self.guard.register_new(value, self.digest)
                self.assertEqual(raised.exception.code, "invalid_request_id")
        # Nothing was admitted, so the canonical identity is still unknown.
        with self.assertRaises(KnowledgeAttemptError) as raised:
            self.guard.consume(self.request_id, self.digest)
        self.assertEqual(raised.exception.code, "request_not_registered")

    def test_nondigest_values_refuse_before_any_mutation(self) -> None:
        bare = self.digest.split(":", 1)[1]
        for value in (
            bare,
            self.digest.upper(),
            "sha256:" + bare[:63],
            "sha256:" + bare + "0",
            self.digest + "\n",
            " " + self.digest,
            "sha1:" + bare,
            None,
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(KnowledgeAttemptError) as raised:
                    self.guard.register_new(self.request_id, value)
                self.assertEqual(raised.exception.code, "invalid_request_digest")
        with self.assertRaises(KnowledgeAttemptError) as raised:
            self.guard.consume(self.request_id, self.digest)
        self.assertEqual(raised.exception.code, "request_not_registered")

    def test_refusals_carry_a_closed_code_and_no_submitted_value(self) -> None:
        self.guard.register_new(self.request_id, self.digest)
        other = released_digest("other")
        with self.assertRaises(KnowledgeAttemptError) as raised:
            self.guard.consume(self.request_id, other)
        error = raised.exception
        self.assertEqual(error.code, "request_digest_mismatch")
        self.assertEqual(str(error), "request_digest_mismatch")
        text = repr(error.args)
        for secret in (self.request_id, self.digest, other, WORKSPACE_UID):
            self.assertNotIn(secret, text)
        self.assertEqual(vars(error), {"code": "request_digest_mismatch"})


class AttemptGuardTransitionTests(unittest.TestCase):
    """Ready to attempted is one-way, and only a matching digest moves it."""

    def setUp(self) -> None:
        self.guard = KnowledgeAttemptGuard()
        self.request_id = released_request_id(1)
        self.digest = released_digest("transition")
        self.guard.register_new(self.request_id, self.digest)

    def test_unknown_identity_is_refused_rather_than_admitted(self) -> None:
        with self.assertRaises(KnowledgeAttemptError) as raised:
            self.guard.consume(released_request_id(99), self.digest)
        self.assertEqual(raised.exception.code, "request_not_registered")

    def test_mismatched_digest_does_not_spend_the_attempt(self) -> None:
        with self.assertRaises(KnowledgeAttemptError) as raised:
            self.guard.consume(self.request_id, released_digest("wrong"))
        self.assertEqual(raised.exception.code, "request_digest_mismatch")
        # The real attempt is still available, which is the whole point of
        # refusing a different request before touching this one's state.
        self.assertIsNone(self.guard.consume(self.request_id, self.digest))

    def test_replayed_registration_is_a_noop_and_cannot_rearm(self) -> None:
        self.guard.consume(self.request_id, self.digest)
        self.assertIsNone(self.guard.register_new(self.request_id, self.digest))
        with self.assertRaises(KnowledgeAttemptError) as raised:
            self.guard.consume(self.request_id, self.digest)
        self.assertEqual(raised.exception.code, "request_already_attempted")

    def test_registration_with_a_different_digest_is_a_different_request(self) -> None:
        with self.assertRaises(KnowledgeAttemptError) as raised:
            self.guard.register_new(self.request_id, released_digest("rebind"))
        self.assertEqual(raised.exception.code, "request_digest_mismatch")
        # The original binding survived the refusal.
        self.assertIsNone(self.guard.consume(self.request_id, self.digest))

    def test_a_new_instance_shares_nothing_with_the_old_one(self) -> None:
        successor = KnowledgeAttemptGuard()
        with self.assertRaises(KnowledgeAttemptError) as raised:
            successor.consume(self.request_id, self.digest)
        self.assertEqual(raised.exception.code, "request_not_registered")
        # And the old instance is unaffected by the new one existing.
        self.assertIsNone(self.guard.consume(self.request_id, self.digest))


class AttemptGuardCapacityTests(unittest.TestCase):
    """The ledger's own record bound is the registration bound."""

    def test_capacity_boundary_is_the_ledger_maximum(self) -> None:
        guard = KnowledgeAttemptGuard()
        digest = released_digest("capacity")
        identities = [released_request_id(index) for index in range(MAX_REQUESTS)]
        self.assertEqual(len(set(identities)), MAX_REQUESTS)
        for identifier in identities:
            guard.register_new(identifier, digest)
        with self.assertRaises(KnowledgeAttemptError) as raised:
            guard.register_new(released_request_id(MAX_REQUESTS), digest)
        self.assertEqual(raised.exception.code, "attempt_capacity")
        # A full instance still recognises what it already holds: a replayed
        # issue at capacity is a no-op, not a capacity refusal, and the last
        # admitted identity is still spendable exactly once.
        self.assertIsNone(guard.register_new(identities[-1], digest))
        self.assertIsNone(guard.consume(identities[-1], digest))
        # Spending does not free a slot; there is no eviction.
        with self.assertRaises(KnowledgeAttemptError) as raised:
            guard.register_new(released_request_id(MAX_REQUESTS + 1), digest)
        self.assertEqual(raised.exception.code, "attempt_capacity")


class AttemptGuardConcurrencyTests(unittest.TestCase):
    """Real threads, one identity, exactly one winner."""

    def test_concurrent_consume_yields_exactly_one_success(self) -> None:
        guard = KnowledgeAttemptGuard()
        request_id = released_request_id(7)
        digest = released_digest("race")
        guard.register_new(request_id, digest)
        racers = 32
        start = threading.Barrier(racers)
        outcomes: list[str] = []
        collect = threading.Lock()

        def attempt() -> None:
            start.wait(timeout=10)
            try:
                guard.consume(request_id, digest)
            except KnowledgeAttemptError as error:
                result = error.code
            else:
                result = "consumed"
            with collect:
                outcomes.append(result)

        threads = [threading.Thread(target=attempt) for _ in range(racers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse([thread for thread in threads if thread.is_alive()])
        self.assertEqual(len(outcomes), racers)
        self.assertEqual(outcomes.count("consumed"), 1)
        self.assertEqual(
            outcomes.count("request_already_attempted"), racers - 1
        )

    def test_concurrent_registration_of_one_identity_admits_it_once(self) -> None:
        guard = KnowledgeAttemptGuard()
        request_id = released_request_id(8)
        digest = released_digest("register-race")
        racers = 16
        start = threading.Barrier(racers)
        failures: list[str] = []
        collect = threading.Lock()

        def register() -> None:
            start.wait(timeout=10)
            try:
                guard.register_new(request_id, digest)
            except KnowledgeAttemptError as error:
                with collect:
                    failures.append(error.code)

        threads = [threading.Thread(target=register) for _ in range(racers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse([thread for thread in threads if thread.is_alive()])
        self.assertEqual(failures, [])
        # Sixteen concurrent registrations of one identity are one identity:
        # it is spendable exactly once, not sixteen times.
        self.assertIsNone(guard.consume(request_id, digest))
        with self.assertRaises(KnowledgeAttemptError) as raised:
            guard.consume(request_id, digest)
        self.assertEqual(raised.exception.code, "request_already_attempted")


if __name__ == "__main__":
    unittest.main()
