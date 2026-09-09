"""The owner verification gate, and the sequence a running owner performs.

Two things live here. The first is the *gate*: an in-memory object with no
store, no clock and no content, whose whole job is that one owner incarnation
runs at most one source check at a time and starts nothing at all once a
child's cleanup has gone unaccounted for. Nothing in that half spawns a
process, opens a socket or touches a file.

The second is the runtime sequence's two obligations that are only observable
by standing in for the subprocess itself: that an unconfirmed cleanup latches
the owner until it restarts, and that every outcome this owner *can* describe
releases the gate for an explicit new user check. Those reuse the loopback
harness the route suite already builds --
``tests/test_knowledge_verification_http.VerificationHttpCase`` -- over a real
store and a real imported Capture, with a fake bounded exchange substituted at
the one seam a real child cannot reach: a transport that reports a cleanup it
could not confirm. Everything else in that suite runs a real child.

The gate half imports nothing from the R20 wave, so it is evidence on its own.
The runtime half needs the R20 protocol and owner admission, and skips with a
named reason until they are integrated -- it is never satisfied by a stub, and
a skip is never reported as a pass.
"""

from __future__ import annotations

import threading
import unittest
from typing import Any

from workstack.knowledge_verification_guard import (
    VERIFICATION_BUSY,
    VERIFICATION_CLEANUP_UNSETTLED,
    KnowledgeVerificationGuard,
    KnowledgeVerificationGuardError,
)

try:  # The R20 protocol and owner admission arrive through the coordinator.
    from workstack.bounded_process_exchange import CLEANUP_SETTLED, CLEANUP_UNSETTLED
    from workstack.knowledge_driver_exchange import (
        DRIVER_NOT_STARTED,
        DRIVER_OUTCOME_UNKNOWN,
        DriverExchangeResult,
    )
    from workstack import knowledge_verification_runtime as runtime

    from tests.test_knowledge_verification_http import VerificationHttpCase
except ImportError as error:  # pragma: no cover - dependency wave
    runtime = None
    VerificationHttpCase = unittest.TestCase  # type: ignore[misc, assignment]
    DEPENDENCY_REASON = (
        "the R20 verification protocol/admission dependency is not integrated "
        "in this checkout: {}".format(error)
    )
else:
    DEPENDENCY_REASON = ""


class VerificationGuardTest(unittest.TestCase):
    """One owner incarnation's single gate: busy, settled and latched."""

    def setUp(self) -> None:
        self.guard = KnowledgeVerificationGuard()

    def test_a_fresh_gate_is_idle_and_admits_one_holder(self) -> None:
        self.assertFalse(self.guard.active)
        self.assertFalse(self.guard.unsettled)
        self.guard.acquire()
        self.assertTrue(self.guard.active)

    def test_a_second_holder_is_busy_and_the_first_still_holds_it(self) -> None:
        self.guard.acquire()
        with self.assertRaises(KnowledgeVerificationGuardError) as caught:
            self.guard.acquire()
        self.assertEqual(caught.exception.code, VERIFICATION_BUSY)
        self.assertTrue(self.guard.active)

    def test_a_settled_release_readmits_an_explicit_new_check(self) -> None:
        self.guard.acquire()
        self.guard.settle()
        self.assertFalse(self.guard.active)
        self.guard.acquire()
        self.assertTrue(self.guard.active)

    def test_a_refused_holder_never_becomes_the_holder(self) -> None:
        """Every later acquire is refused, and the first one still holds it."""

        self.guard.acquire()
        for _ in range(3):
            with self.assertRaises(KnowledgeVerificationGuardError) as caught:
                self.guard.acquire()
            self.assertEqual(caught.exception.code, VERIFICATION_BUSY)
            self.assertTrue(self.guard.active)

    def test_an_unconfirmed_cleanup_latches_for_the_life_of_the_instance(self) -> None:
        self.guard.acquire()
        self.guard.latch_unsettled()
        self.assertTrue(self.guard.unsettled)
        self.assertFalse(self.guard.active)
        for _ in range(3):
            with self.assertRaises(KnowledgeVerificationGuardError) as caught:
                self.guard.acquire()
            self.assertEqual(caught.exception.code, VERIFICATION_CLEANUP_UNSETTLED)

    def test_settling_after_a_latch_does_not_reopen_the_gate(self) -> None:
        """The ``finally`` that releases every check must not clear a latch."""

        self.guard.acquire()
        self.guard.latch_unsettled()
        self.guard.settle()
        with self.assertRaises(KnowledgeVerificationGuardError) as caught:
            self.guard.acquire()
        self.assertEqual(caught.exception.code, VERIFICATION_CLEANUP_UNSETTLED)

    def test_latching_twice_is_the_same_latch(self) -> None:
        self.guard.latch_unsettled()
        self.guard.latch_unsettled()
        self.assertTrue(self.guard.unsettled)

    def test_a_new_instance_is_the_only_way_back(self) -> None:
        """A latch is an owner-restart state, and instances share nothing."""

        self.guard.latch_unsettled()
        successor = KnowledgeVerificationGuard()
        self.assertFalse(successor.unsettled)
        successor.acquire()
        self.assertTrue(successor.active)
        self.assertTrue(self.guard.unsettled)

    def test_exactly_one_of_many_concurrent_holders_wins(self) -> None:
        start = threading.Barrier(8)
        held: list[int] = []
        refused: list[str] = []
        lock = threading.Lock()

        def contend(index: int) -> None:
            start.wait(10)
            try:
                self.guard.acquire()
            except KnowledgeVerificationGuardError as error:
                with lock:
                    refused.append(error.code)
                return
            with lock:
                held.append(index)

        threads = [threading.Thread(target=contend, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(len(held), 1, held)
        self.assertEqual(refused, [VERIFICATION_BUSY] * 7)


@unittest.skipIf(runtime is None, DEPENDENCY_REASON)
class VerificationExchangeSeamTest(VerificationHttpCase):
    """The one seam a real child cannot reach: an unconfirmed cleanup.

    Every other transport outcome is exercised by a real child in the route
    suite. A cleanup the transport could not confirm is a state this test
    cannot reliably provoke with a real process on either platform, so the
    bounded exchange itself is substituted here -- at the subprocess seam and
    nowhere else. The store, the ledger, the Capture, the admission, the
    protocol, the gate, the route and the HTTP session are all real.
    """

    def fake_exchange(self, result: Any) -> list[int]:
        """Replace the bounded exchange, counting how often it is reached."""

        calls: list[int] = []

        def exchange(*arguments: Any, **keywords: Any) -> Any:
            calls.append(1)
            return result

        original = runtime.run_knowledge_driver
        runtime.run_knowledge_driver = exchange
        self.addCleanup(setattr, runtime, "run_knowledge_driver", original)
        return calls

    def test_an_unconfirmed_cleanup_latches_the_owner_until_it_restarts(self) -> None:
        capture = self.ready()
        calls = self.fake_exchange(
            DriverExchangeResult(None, DRIVER_OUTCOME_UNKNOWN, CLEANUP_UNSETTLED)
        )
        status, payload = self.verify(capture)
        self.assertEqual(status, 502, payload)
        self.assertRefused(payload, DRIVER_OUTCOME_UNKNOWN)
        self.assertEqual(len(calls), 1)
        # No second child piles onto a child this owner cannot account for --
        # not even after the transport would have answered normally again.
        for _ in range(2):
            status, payload = self.verify(capture)
            self.assertEqual(status, 503, payload)
            self.assertRefused(payload, VERIFICATION_CLEANUP_UNSETTLED)
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.server.knowledge_verification_guard.unsettled)

    def test_a_restarted_owner_starts_from_an_unlatched_gate(self) -> None:
        """A new incarnation is the only way back, and it really is a way back.

        The substituted transport still reports an unconfirmed cleanup after
        the restart, so the status is the same 502. What changed is that the
        check reached the transport at all: the restarted owner admitted it
        rather than refusing at its own gate, which is exactly the latch being
        an incarnation's state and not the store's.
        """

        capture = self.ready()
        calls = self.fake_exchange(
            DriverExchangeResult(None, DRIVER_OUTCOME_UNKNOWN, CLEANUP_UNSETTLED)
        )
        status, _ = self.verify(capture)
        self.assertEqual(status, 502)
        status, payload = self.verify(capture)
        self.assertEqual(status, 503, payload)
        self.assertEqual(len(calls), 1)
        self.restart()
        self.assertFalse(self.server.knowledge_verification_guard.unsettled)
        status, payload = self.verify(capture)
        self.assertEqual(status, 502, payload)
        self.assertRefused(payload, DRIVER_OUTCOME_UNKNOWN)
        self.assertEqual(len(calls), 2)

    def test_a_child_that_never_started_releases_the_gate(self) -> None:
        """Nothing happened, so an explicit new user check may run at once."""

        capture = self.ready()
        calls = self.fake_exchange(
            DriverExchangeResult(None, DRIVER_NOT_STARTED, CLEANUP_SETTLED)
        )
        status, payload = self.verify(capture)
        self.assertEqual(status, 503, payload)
        self.assertRefused(payload, DRIVER_NOT_STARTED)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.server.knowledge_verification_guard.unsettled)
        self.assertFalse(self.server.knowledge_verification_guard.active)
        status, payload = self.verify(capture)
        self.assertEqual(status, 503, payload)
        self.assertEqual(len(calls), 2)


@unittest.skipIf(runtime is None, DEPENDENCY_REASON)
class VerificationBodyTest(unittest.TestCase):
    """The whole of what a browser may say, admitted before any Store read."""

    workspace_uid = "1f2e3d4c-5b6a-4798-8899-aabbccddeeff"

    def body(self, **overrides: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "workspace_uid": self.workspace_uid,
            "capture_id": "C-0001",
            "capture_revision": 0,
        }
        fields.update(overrides)
        return fields

    def refused(self, body: Any) -> Any:
        with self.assertRaises(runtime.VerificationRuntimeError) as caught:
            runtime.parse_verification_body(body)
        return caught.exception

    def test_the_three_fields_are_admitted_in_the_store_grammar(self) -> None:
        self.assertEqual(
            runtime.parse_verification_body(self.body(capture_revision=7)),
            (self.workspace_uid, "C-0001", 7),
        )

    def test_a_fourth_field_is_refused_rather_than_ignored(self) -> None:
        for name, value in (
            ("corpus_refs", ["nas-team-share"]),
            ("document_ref", "nas-doc-0001abcd"),
            ("connection_alias", "team-nas"),
            ("command", ["/bin/true"]),
        ):
            with self.subTest(field=name):
                error = self.refused(self.body(**{name: value}))
                self.assertEqual(error.code, runtime.INVALID_VERIFICATION_BODY)
                self.assertEqual(error.details, {})

    def test_a_missing_field_is_refused(self) -> None:
        for name in ("workspace_uid", "capture_id", "capture_revision"):
            with self.subTest(missing=name):
                body = self.body()
                del body[name]
                self.assertEqual(
                    self.refused(body).code, runtime.INVALID_VERIFICATION_BODY
                )

    def test_a_non_mapping_body_is_refused(self) -> None:
        for body in ([], "C-0001", 3, None):
            with self.subTest(body=repr(body)):
                self.assertEqual(
                    self.refused(body).code, runtime.INVALID_VERIFICATION_BODY
                )

    def test_each_field_is_held_to_its_own_grammar(self) -> None:
        cases = {
            "workspace_uid": ("not-a-uuid", 7, None, self.workspace_uid.upper()),
            "capture_id": ("C-001", "capture", "", 4, None),
            "capture_revision": (-1, True, "0", 1.5, None, 2**53),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(field=name, value=repr(value)):
                    error = self.refused(self.body(**{name: value}))
                    self.assertEqual(error.code, runtime.INVALID_VERIFICATION_FIELD)
                    self.assertEqual(error.details, {"field": name})

    def test_a_refusal_never_carries_a_submitted_value(self) -> None:
        error = self.refused(self.body(capture_id="C-0001-canary-4d21"))
        rendered = "{} {}".format(error, error.details)
        self.assertNotIn("canary-4d21", rendered)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
