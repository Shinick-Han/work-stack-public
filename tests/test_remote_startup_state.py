from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "remote_startup_state.py"
SPEC = importlib.util.spec_from_file_location("remote_startup_state_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

State = MODULE.RemoteStartupState


def machine(observer=None):
    return MODULE.RemoteStartupStateMachine(observer=observer)


class RemoteStartupStateMachineTest(unittest.TestCase):
    def test_legal_transition_table_and_refusals(self) -> None:
        expected = {
            State.IDLE: {State.PROBING},
            State.PROBING: {State.STARTING_TUNNEL, State.FAILED, State.STOPPED},
            State.STARTING_TUNNEL: {State.WAITING_REMOTE_READY, State.FAILED, State.STOPPED},
            State.WAITING_REMOTE_READY: {State.VERIFYING_AUTHORITY, State.FAILED, State.STOPPED},
            State.VERIFYING_AUTHORITY: {State.READY, State.FAILED, State.STOPPED},
            State.READY: {State.MONITORING, State.FAILED, State.STOPPED},
            State.MONITORING: {State.FAILED, State.STOPPED},
            State.FAILED: set(),
            State.STOPPED: set(),
        }
        for source in State:
            allowed = expected[source]
            for target in State:
                self.assertEqual(
                    MODULE.is_legal_transition(source, target),
                    target in allowed,
                    f"{source.value} -> {target.value}",
                )

        startup = machine()
        first = startup.begin()
        self.assertFalse(startup.advance(first, State.READY))
        self.assertFalse(startup.advance(first, State.MONITORING))
        self.assertFalse(startup.advance(first, State.VERIFYING_AUTHORITY))
        self.assertFalse(startup.advance(first, State.WAITING_REMOTE_READY))
        self.assertEqual(startup.state, State.PROBING)

    def test_happy_path_reaches_monitoring_only_after_ready(self) -> None:
        startup = machine()
        attempt = startup.begin()
        self.assertTrue(startup.advance(attempt, State.STARTING_TUNNEL))
        self.assertTrue(startup.advance(attempt, State.WAITING_REMOTE_READY))
        self.assertFalse(startup.can_start_monitor(attempt))
        self.assertTrue(startup.advance(attempt, State.VERIFYING_AUTHORITY))
        self.assertFalse(startup.can_start_monitor(attempt))
        self.assertTrue(startup.advance(attempt, State.READY))
        self.assertTrue(startup.can_start_monitor(attempt))
        self.assertTrue(startup.mark_monitor_started(attempt))
        self.assertEqual(startup.state, State.MONITORING)
        self.assertFalse(startup.can_start_monitor(attempt))
        self.assertFalse(startup.mark_monitor_started(attempt))
        self.assertTrue(startup.mark_monitor_stopped(attempt))
        self.assertFalse(startup.mark_monitor_stopped(attempt))

    def test_http_ready_alone_cannot_fabricate_ready_or_monitoring(self) -> None:
        startup = machine()
        attempt = startup.begin()
        self.assertTrue(startup.advance(attempt, State.STARTING_TUNNEL))
        self.assertTrue(startup.advance(attempt, State.WAITING_REMOTE_READY))
        self.assertFalse(startup.observe(attempt, State.READY))
        self.assertFalse(startup.observe(attempt, State.MONITORING))
        self.assertEqual(startup.state, State.WAITING_REMOTE_READY)

    def test_stale_callbacks_from_attempt_one_are_inert(self) -> None:
        publications: list[tuple[str, str]] = []
        startup = machine(observer=lambda attempt, state: publications.append((attempt, state)))
        first = startup.begin()
        self.assertTrue(startup.advance(first, State.STARTING_TUNNEL))
        second = startup.begin()
        self.assertNotEqual(first, second)
        self.assertTrue(int(second) > int(first))
        self.assertFalse(startup.observe(first, State.WAITING_REMOTE_READY))
        self.assertFalse(startup.observe(first, State.VERIFYING_AUTHORITY))
        self.assertFalse(startup.observe(first, State.READY))
        self.assertFalse(startup.observe(first, State.MONITORING))
        self.assertFalse(startup.fail(first))
        self.assertTrue(startup.advance(second, State.STARTING_TUNNEL))
        self.assertEqual(startup.active_attempt_id, second)
        self.assertEqual(startup.state, State.STARTING_TUNNEL)
        self.assertNotIn((first, State.READY.value), publications)
        self.assertNotIn((first, State.MONITORING.value), publications)

    def test_timeout_from_attempt_one_cannot_overwrite_attempt_two_success(self) -> None:
        publications: list[tuple[str, str]] = []
        startup = machine(observer=lambda attempt, state: publications.append((attempt, state)))
        first = startup.begin()
        second = startup.begin()
        for state in (
            State.STARTING_TUNNEL,
            State.WAITING_REMOTE_READY,
            State.VERIFYING_AUTHORITY,
            State.READY,
        ):
            self.assertTrue(startup.advance(second, state))
        self.assertFalse(startup.fail(first))
        self.assertFalse(startup.observe(first, State.FAILED))
        self.assertEqual(startup.state, State.READY)
        self.assertEqual(startup.active_attempt_id, second)
        self.assertEqual(publications[-1], (second, State.READY.value))

    def test_stop_then_late_success_cannot_resurrect_the_attempt(self) -> None:
        startup = machine()
        attempt = startup.begin()
        self.assertTrue(startup.advance(attempt, State.STARTING_TUNNEL))
        stopped = startup.stop()
        self.assertEqual(stopped, attempt)
        self.assertIsNone(startup.active_attempt_id)
        self.assertEqual(startup.state, State.STOPPED)
        self.assertFalse(startup.observe(attempt, State.WAITING_REMOTE_READY))
        self.assertFalse(startup.observe(attempt, State.READY))
        self.assertFalse(startup.can_start_monitor(attempt))
        self.assertFalse(startup.mark_monitor_started(attempt))

    def test_observer_exception_and_duplicate_publication_do_not_advance_state(self) -> None:
        calls = {"count": 0}

        def boom(_attempt: str, _state: str) -> None:
            calls["count"] += 1
            raise RuntimeError("observer failed")

        startup = machine(observer=boom)
        attempt = startup.begin()
        self.assertEqual(startup.state, State.PROBING)
        self.assertEqual(calls["count"], 1)
        self.assertTrue(startup.advance(attempt, State.STARTING_TUNNEL))
        self.assertEqual(startup.state, State.STARTING_TUNNEL)
        self.assertEqual(calls["count"], 2)
        self.assertFalse(startup.advance(attempt, State.STARTING_TUNNEL))
        self.assertEqual(startup.state, State.STARTING_TUNNEL)
        self.assertEqual(calls["count"], 2)

    def test_failure_is_stable_and_cannot_become_ready(self) -> None:
        startup = machine()
        attempt = startup.begin()
        self.assertTrue(startup.fail(attempt))
        self.assertEqual(startup.state, State.FAILED)
        self.assertFalse(startup.advance(attempt, State.READY))
        self.assertFalse(startup.advance(attempt, State.MONITORING))
        self.assertTrue(startup.is_current(attempt))
        newer = startup.begin()
        self.assertNotEqual(newer, attempt)
        self.assertEqual(startup.state, State.PROBING)


if __name__ == "__main__":
    unittest.main()
