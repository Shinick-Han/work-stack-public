from __future__ import annotations

import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT / "desktop" / "python-webview-shell" / "remote_connection_monitor.py"
)
SPEC = importlib.util.spec_from_file_location("remote_connection_monitor_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class MutableConnection:
    def __init__(self, *, alive: bool = True, healthy: bool = True) -> None:
        self.alive = alive
        self.healthy = healthy
        self.reconnects = 0
        self.states: list[str] = []
        self.reloads = 0
        self.reconnect_result = True
        self.reconnect_called = threading.Event()
        self.recovery_required = False
        self.fail_closed = 0

    def is_process_alive(self) -> bool:
        return self.alive

    def is_healthy(self) -> bool:
        return self.healthy

    def reconnect_once(self) -> bool:
        self.reconnects += 1
        self.reconnect_called.set()
        if self.reconnect_result:
            self.alive = True
            self.healthy = True
        return self.reconnect_result

    def publish_state(self, state: str) -> None:
        self.states.append(state)

    def reload_view(self) -> None:
        self.reloads += 1

    def on_recovery_required(self) -> None:
        self.fail_closed += 1


def monitor_for(
    connection: MutableConnection,
    *,
    failure_threshold: int = 2,
    backoff: tuple[float, ...] = (0.01, 0.01),
) -> object:
    return MODULE.RemoteConnectionMonitor(
        is_healthy=connection.is_healthy,
        is_process_alive=connection.is_process_alive,
        reconnect_once=connection.reconnect_once,
        publish_state=connection.publish_state,
        reload_view=connection.reload_view,
        initial_grace=0,
        poll_interval=0.005,
        failure_threshold=failure_threshold,
        reconnect_backoff=backoff,
        reconnect_grace=0,
        is_recovery_required=lambda: connection.recovery_required,
        on_recovery_required=connection.on_recovery_required,
    )


def wait_until(predicate: object, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.005)
    return False


class RemoteConnectionMonitorTest(unittest.TestCase):
    def test_authority_mismatch_fails_closed_without_auto_reconnect(self) -> None:
        connection = MutableConnection(healthy=False)
        connection.recovery_required = True
        monitor = monitor_for(connection, failure_threshold=1)

        self.assertTrue(monitor.start())
        self.assertTrue(wait_until(lambda: connection.fail_closed == 1))
        monitor.stop()

        self.assertEqual(connection.reconnects, 0)
        self.assertEqual(connection.reloads, 0)
        self.assertEqual(connection.states, ["disconnected"])

    def test_transient_health_failure_does_not_reconnect(self) -> None:
        connection = MutableConnection(healthy=False)
        monitor = monitor_for(connection, failure_threshold=3)

        self.assertTrue(monitor.start())
        time.sleep(0.006)
        connection.healthy = True
        self.assertTrue(wait_until(lambda: "ready" in connection.states))
        monitor.stop()

        self.assertEqual(connection.reconnects, 0)
        self.assertEqual(connection.reloads, 0)

    def test_dead_process_recovers_and_reloads_only_after_verified_health(self) -> None:
        connection = MutableConnection(alive=False, healthy=False)
        monitor = monitor_for(connection)

        self.assertTrue(monitor.start())
        self.assertTrue(wait_until(lambda: connection.reloads == 1))
        monitor.stop()

        self.assertEqual(connection.reconnects, 1)
        self.assertEqual(connection.states[:2], ["reconnecting", "ready"])

    def test_repeated_health_failures_trigger_recovery(self) -> None:
        connection = MutableConnection(alive=True, healthy=False)
        monitor = monitor_for(connection, failure_threshold=2)

        self.assertTrue(monitor.start())
        self.assertTrue(wait_until(lambda: connection.reloads == 1))
        monitor.stop()

        self.assertEqual(connection.reconnects, 1)
        self.assertEqual(connection.states, ["reconnecting", "ready"])

    def test_exhaustion_publishes_disconnected_without_reload(self) -> None:
        connection = MutableConnection(alive=False, healthy=False)
        connection.reconnect_result = False
        monitor = monitor_for(connection)

        self.assertTrue(monitor.start())
        self.assertTrue(wait_until(lambda: "disconnected" in connection.states))
        monitor.stop()

        self.assertEqual(connection.reconnects, 3)
        self.assertEqual(connection.reloads, 0)
        self.assertEqual(connection.states[-1], "disconnected")

    def test_stop_during_backoff_prevents_another_reconnect(self) -> None:
        connection = MutableConnection(alive=False, healthy=False)
        connection.reconnect_result = False
        monitor = monitor_for(connection, backoff=(5.0, 5.0))

        self.assertTrue(monitor.start())
        self.assertTrue(connection.reconnect_called.wait(1.0))
        monitor.stop(timeout=0.5)
        reconnects_at_stop = connection.reconnects
        time.sleep(0.02)

        self.assertEqual(reconnects_at_stop, 1)
        self.assertEqual(connection.reconnects, 1)
        self.assertFalse(monitor.is_running)

    def test_start_does_not_create_duplicate_monitor_threads(self) -> None:
        connection = MutableConnection()
        monitor = monitor_for(connection)

        self.assertTrue(monitor.start())
        self.assertFalse(monitor.start())
        monitor.stop()

        self.assertFalse(monitor.is_running)


if __name__ == "__main__":
    unittest.main()
