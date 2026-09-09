"""Adversarial desktop remote-attempt lifecycle oracles.

Reproduces the independent-review races: stale waiters must release only
captured resources, failure cleanup must stay FAILED, and a newer generation
must survive old cleanup.
"""

from __future__ import annotations

import importlib.util
import inspect
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))
import remote_attempt_resources as RESOURCES
import remote_stop_result as STOP
import ssot_connection as SSOT
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "workstack_desktop.py"
SPEC = importlib.util.spec_from_file_location("workstack_desktop_attempt_lifecycle", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    SPEC.loader.exec_module(MODULE)


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
REQUIRED_REMOTE_PYTHON = "/srv/workstack/venv/bin/python"


def _process(pid: int = 1) -> mock.Mock:
    process = mock.Mock()
    process.poll.return_value = None
    process.pid = pid
    return process


class CountingProcess:
    """Owned SSH process double that exits on the first real termination."""

    def __init__(self, pid: int = 77) -> None:
        self.pid = pid
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 0

    def wait(self, timeout: float):
        return self.returncode

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


class UnreapableProcess(CountingProcess):
    """Both bounded waits expire; the reap is never confirmed."""

    def terminate(self) -> None:
        self.terminate_calls += 1

    def wait(self, timeout: float):
        raise subprocess.TimeoutExpired("ssh", timeout)

    def kill(self) -> None:
        self.kill_calls += 1


class CountingLog:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class FakeMonitor:
    """Monitor double that records its own start/stop and running state."""

    def __init__(self, **options: object) -> None:
        self.options = options
        self.start_calls = 0
        self.stop_calls = 0
        self.running = False
        self.start_gate: threading.Event | None = None
        self.start_entered = threading.Event()

    @property
    def is_running(self) -> bool:
        return self.running

    def start(self) -> bool:
        self.start_calls += 1
        self.start_entered.set()
        if self.start_gate is not None:
            assert self.start_gate.wait(timeout=5)
        self.running = True
        return True

    def stop(self, timeout: float | None = None) -> None:
        self.stop_calls += 1
        self.running = False


class DesktopRemoteAttemptLifecycleTest(unittest.TestCase):
    def _host(self) -> object:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        MODULE.initialize_attempt_resources(host)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID, 8765, REQUIRED_REMOTE_PYTHON
        )
        host.remote_startup = MODULE.RemoteStartupStateMachine()
        host.remote_monitor = None
        host.remote_monitor_attempt_id = ""
        host.remote_shutdown_requested = MODULE.threading.Event()
        host.remote_recovery_required = MODULE.threading.Event()
        host.remote_lifecycle_state = "IDLE"
        host.remote_attempt_id = 0
        host.remote_ready_attempt_id = 0
        host.remote_session_token = None
        host.remote_session_token_hash = None
        host.remote_ssh_log = None
        host.remote_ssh_process = None
        host._request_remote_stop_owned = mock.Mock()
        host._trace = mock.Mock()
        host.remote_wait_error = None
        host._is_ready = mock.Mock(return_value=False)
        host._start_remote_monitor = mock.Mock()
        return host

    def _waiting(self, host: object, token: str, process: mock.Mock) -> object:
        attempt = host._begin_remote_attempt()
        host.remote_session_token = token
        self.assertTrue(host._advance_remote_startup(attempt, "STARTING_TUNNEL"))
        self.assertTrue(host._advance_remote_startup(attempt, "WAITING_REMOTE_READY"))
        resources = MODULE.RemoteAttemptResources(
            generation=int(attempt),
            profile=host.remote_profile,
            token=token,
            process=process,
            log=None,
        )
        self.assertTrue(host._commit_remote_attempt_resources(resources))
        return resources

    def test_stale_waiter_releases_only_captured_resources_after_newer_install(self) -> None:
        host = self._host()
        first_process = _process(11)
        first = self._waiting(host, "older-token", first_process)
        second_process = _process(22)
        second_attempt = host._begin_remote_attempt()
        host.remote_session_token = "newer-token"
        second = MODULE.RemoteAttemptResources(
            generation=int(second_attempt),
            profile=host.remote_profile,
            token="newer-token",
            process=second_process,
            log=None,
        )
        self.assertTrue(host._advance_remote_startup(second_attempt, "STARTING_TUNNEL"))
        self.assertTrue(host._commit_remote_attempt_resources(second, "WAITING_REMOTE_READY"))

        host._wait_for_remote_ready(first, Path("remote-ssh.log"))

        host._request_remote_stop_owned.assert_called_with(host.remote_profile, "older-token")
        first_process.terminate.assert_called_once_with()
        second_process.terminate.assert_not_called()
        self.assertEqual(host.remote_session_token, "newer-token")
        self.assertIs(host.remote_ssh_process, second_process)
        self.assertEqual(host.remote_startup.state, MODULE.RemoteStartupState.WAITING_REMOTE_READY)
        self.assertNotEqual(host.remote_lifecycle_state, "STOPPED")
        self.assertNotEqual(host.remote_lifecycle_state, "FAILED")

    def test_early_exit_after_generation_check_cannot_stop_newer_token(self) -> None:
        host = self._host()
        first_process = _process(11)
        first_process.poll.return_value = 255
        first_process.returncode = 255
        first = self._waiting(host, "older-token", first_process)
        second_process = _process(22)

        def install_newer(_resources: object) -> None:
            second_attempt = host._begin_remote_attempt()
            host.remote_session_token = "newer-token"
            second = MODULE.RemoteAttemptResources(
                generation=int(second_attempt),
                profile=host.remote_profile,
                token="newer-token",
                process=second_process,
                log=None,
            )
            self.assertTrue(host._advance_remote_startup(second_attempt, "STARTING_TUNNEL"))
            self.assertTrue(host._commit_remote_attempt_resources(second, "WAITING_REMOTE_READY"))

        host._remote_barrier_generation_check = install_newer
        host._wait_for_remote_ready(first, Path("remote-ssh.log"))

        stopped_tokens = [call.args[1] for call in host._request_remote_stop_owned.call_args_list]
        self.assertEqual(stopped_tokens, ["older-token"])
        self.assertNotIn("newer-token", stopped_tokens)
        second_process.terminate.assert_not_called()
        self.assertEqual(host.remote_session_token, "newer-token")
        self.assertIs(host.remote_ssh_process, second_process)
        self.assertEqual(host.remote_startup.state, MODULE.RemoteStartupState.WAITING_REMOTE_READY)
        self.assertNotEqual(host.remote_startup.state, MODULE.RemoteStartupState.STOPPED)

    def test_stale_cleanup_cannot_stop_a_new_generation(self) -> None:
        host = self._host()
        first_process = _process(11)
        first = self._waiting(host, "older-token", first_process)
        second = host._begin_remote_attempt()
        self.assertTrue(host._advance_remote_startup(second, "STARTING_TUNNEL"))
        host._cleanup_captured_remote_resources(first, "stale")
        self.assertEqual(host.remote_startup.active_attempt_id, str(second))
        self.assertEqual(host.remote_startup.state, MODULE.RemoteStartupState.STARTING_TUNNEL)
        self.assertNotEqual(host.remote_lifecycle_state, "STOPPED")

    def test_concurrent_close_during_early_popen_releases_spawned_process(self) -> None:
        host = self._host()
        with tempfile.TemporaryDirectory() as directory:
            host.state_root = Path(directory)
            spawned = _process(77)
            started = threading.Event()
            closed = threading.Event()

            def popen(*_args: object, **_kwargs: object) -> mock.Mock:
                started.set()
                self.assertTrue(closed.wait(timeout=2))
                return spawned

            def closer() -> None:
                self.assertTrue(started.wait(timeout=2))
                host._stop_owned_remote_connection()
                closed.set()

            closer_thread = threading.Thread(target=closer)
            with mock.patch.object(RESOURCES, "find_ssh_executable", return_value="ssh"):
                with mock.patch.object(RESOURCES.subprocess, "Popen", side_effect=popen):
                    closer_thread.start()
                    host._ensure_remote_server()
                    closer_thread.join(timeout=2)
            self.assertFalse(closer_thread.is_alive())
        spawned.terminate.assert_called_once_with()
        self.assertEqual(host.remote_startup.state, MODULE.RemoteStartupState.STOPPED)
        self.assertIsNone(host.remote_ssh_process)
        self.assertIsNone(host.remote_session_token)

    def test_concurrent_close_after_waiting_commit_does_not_resurrect(self) -> None:
        host = self._host()
        with tempfile.TemporaryDirectory() as directory:
            host.state_root = Path(directory)
            spawned = _process(88)
            committed = threading.Event()
            closed = threading.Event()

            def after_waiting(_resources: object) -> None:
                committed.set()
                self.assertTrue(closed.wait(timeout=2))

            def closer() -> None:
                self.assertTrue(committed.wait(timeout=2))
                host._stop_owned_remote_connection()
                closed.set()

            host._remote_barrier_waiting_commit = after_waiting
            closer_thread = threading.Thread(target=closer)
            with mock.patch.object(RESOURCES, "find_ssh_executable", return_value="ssh"):
                with mock.patch.object(RESOURCES.subprocess, "Popen", return_value=spawned):
                    closer_thread.start()
                    host._ensure_remote_server()
                    closer_thread.join(timeout=2)
            self.assertFalse(closer_thread.is_alive())
        spawned.terminate.assert_called()
        self.assertEqual(host.remote_startup.state, MODULE.RemoteStartupState.STOPPED)
        self.assertIsNone(host.remote_session_token)

    def test_supersession_between_popen_and_waiting_releases_the_old_process(self) -> None:
        host = self._host()
        seen: dict[str, str] = {}
        with tempfile.TemporaryDirectory() as directory:
            host.state_root = Path(directory)
            first_process = _process(31)

            def after_popen(resources: object) -> None:
                seen["token"] = str(getattr(resources, "token"))
                host._begin_remote_attempt()

            host._remote_barrier_process_started = after_popen
            with mock.patch.object(RESOURCES, "find_ssh_executable", return_value="ssh"):
                with mock.patch.object(RESOURCES.subprocess, "Popen", return_value=first_process):
                    host._ensure_remote_server()
        first_process.terminate.assert_called_once_with()
        self.assertEqual(host._request_remote_stop_owned.call_args.args[1], seen["token"])
        self.assertNotEqual(host.remote_session_token, seen["token"])

    def _installed_waiting(self, host: object, process: object, log: object) -> object:
        """Commit one attempt through the production path, keeping its claim."""

        start = RESOURCES.start_remote_attempt(host)
        self.assertTrue(host._advance_remote_startup(start.generation, "STARTING_TUNNEL"))
        resources = MODULE.RemoteAttemptResources(
            generation=start.generation,
            profile=host.remote_profile,
            token=start.token,
            process=process,
            log=log,
            claim=start.claim,
        )
        self.assertTrue(host._commit_remote_attempt_resources(resources, "WAITING_REMOTE_READY"))
        return resources

    def test_lifecycle_lock_and_gate_are_published_eagerly_for_every_host(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        with self.assertRaises(RuntimeError):
            RESOURCES.resource_lock_for(host)
        with self.assertRaises(RuntimeError):
            RESOURCES.gate_for(host)

        MODULE.initialize_attempt_resources(host)
        observed: list[object] = []
        ready = threading.Barrier(2)

        def observe() -> None:
            ready.wait(timeout=2)
            observed.append(RESOURCES.resource_lock_for(host))

        threads = [threading.Thread(target=observe) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.assertEqual(len(observed), 2)
        self.assertIs(observed[0], observed[1])
        self.assertIs(observed[0], host.remote_resource_lock)
        self.assertIsInstance(host.remote_resource_lock, type(threading.RLock()))
        gate = RESOURCES.gate_for(host)
        self.assertIs(gate, host.remote_attempt_gate)
        self.assertFalse(hasattr(gate, "lock"))
        self.assertFalse(hasattr(gate, "_lock"))
        self.assertIsNone(host.remote_monitor_lease)
        source = inspect.getsource(MODULE.WorkStackDesktopHost.__init__)
        self.assertIn("initialize_remote_attempt_state(", source)
        self.assertNotIn("threading.RLock()", source)

    def test_remote_initialization_helper_publishes_the_whole_remote_slice(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        machine = MODULE.RemoteStartupStateMachine()

        MODULE.initialize_remote_attempt_state(host, machine)

        self.assertIs(host.remote_startup, machine)
        self.assertIs(RESOURCES.gate_for(host), host.remote_attempt_gate)
        self.assertIsInstance(host.remote_resource_lock, type(threading.RLock()))
        self.assertIsNone(host.remote_monitor)
        self.assertIsNone(host.remote_monitor_lease)
        self.assertEqual(host.remote_monitor_attempt_id, "")
        self.assertEqual(host.remote_lifecycle_state, "IDLE")
        self.assertEqual(host.remote_attempt_id, 0)
        self.assertEqual(host.remote_ready_attempt_id, 0)
        self.assertIsNone(host.remote_session_token)
        self.assertIsNone(host.remote_ssh_process)
        self.assertFalse(host.remote_shutdown_requested.is_set())
        self.assertFalse(host.remote_recovery_required.is_set())
        # Nothing is published lazily, so a monitor teardown before any start
        # is a no-op rather than a first-use construction.
        host._stop_remote_monitor()
        self.assertIsNone(host.remote_monitor)

    def test_close_and_stale_waiter_release_the_bundle_exactly_once(self) -> None:
        host = self._host()
        process = CountingProcess(77)
        log = CountingLog()
        resources = self._installed_waiting(host, process, log)
        sampled = threading.Event()
        resume = threading.Event()

        def after_generation_check(_resources: object) -> None:
            sampled.set()
            self.assertTrue(resume.wait(timeout=2))

        host._remote_barrier_generation_check = after_generation_check
        failures: list[BaseException] = []

        def waiter() -> None:
            try:
                host._wait_for_remote_ready(resources, Path("remote-ssh.log"))
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=waiter)
        thread.start()
        self.assertTrue(sampled.wait(timeout=2))
        host._stop_owned_remote_connection()
        resume.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

        self.assertEqual(failures, [])
        self.assertEqual(host._request_remote_stop_owned.call_count, 1)
        self.assertEqual(
            host._request_remote_stop_owned.call_args.args, (host.remote_profile, resources.token)
        )
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)
        self.assertEqual(log.close_calls, 1)
        self.assertEqual(host.remote_startup.state, MODULE.RemoteStartupState.STOPPED)
        self.assertIsNone(host.remote_ssh_process)
        self.assertIsNone(host.remote_session_token)

    def test_close_before_publication_releases_the_attempt_exactly_once(self) -> None:
        host = self._host()
        with tempfile.TemporaryDirectory() as directory:
            host.state_root = Path(directory)
            spawned = CountingProcess(88)
            started = threading.Event()
            closed = threading.Event()

            def popen(*_args: object, **_kwargs: object) -> object:
                started.set()
                self.assertTrue(closed.wait(timeout=2))
                return spawned

            def closer() -> None:
                self.assertTrue(started.wait(timeout=2))
                host._stop_owned_remote_connection()
                closed.set()

            closer_thread = threading.Thread(target=closer)
            with mock.patch.object(RESOURCES, "find_ssh_executable", return_value="ssh"):
                with mock.patch.object(RESOURCES.subprocess, "Popen", side_effect=popen):
                    closer_thread.start()
                    host._ensure_remote_server()
                    closer_thread.join(timeout=2)
            self.assertFalse(closer_thread.is_alive())

        self.assertEqual(host._request_remote_stop_owned.call_count, 1)
        self.assertEqual(spawned.terminate_calls, 1)
        self.assertEqual(spawned.kill_calls, 0)
        self.assertEqual(host.remote_startup.state, MODULE.RemoteStartupState.STOPPED)
        self.assertIsNone(host.remote_ssh_process)
        self.assertIsNone(host.remote_ssh_log)
        self.assertIsNone(host.remote_session_token)

    def test_ready_commit_is_atomic_and_a_close_after_it_wins(self) -> None:
        host = self._host()
        del host._start_remote_monitor
        process = CountingProcess(99)
        log = CountingLog()
        resources = self._installed_waiting(host, process, log)
        self.assertTrue(host._advance_remote_startup(resources.generation, "VERIFYING_AUTHORITY"))
        inside_commit = threading.Event()
        close_returned = threading.Event()

        def during_ready_commit(_generation: int) -> None:
            inside_commit.set()
            # The close thread is already blocked on the same lifecycle lock.
            self.assertFalse(close_returned.wait(timeout=0.25))

        host._remote_barrier_ready_commit = during_ready_commit

        def closer() -> None:
            self.assertTrue(inside_commit.wait(timeout=2))
            host._stop_owned_remote_connection()
            close_returned.set()

        closer_thread = threading.Thread(target=closer)
        closer_thread.start()
        committed = host._apply_remote_ready_if_current(resources.generation)
        closer_thread.join(timeout=2)
        self.assertFalse(closer_thread.is_alive())

        self.assertTrue(committed)
        self.assertEqual(host.remote_startup.state, MODULE.RemoteStartupState.STOPPED)
        self.assertEqual(host.remote_ready_attempt_id, 0)
        self.assertIsNone(host.remote_monitor)
        self.assertEqual(host.remote_monitor_attempt_id, "")
        self.assertEqual(host._request_remote_stop_owned.call_count, 1)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(log.close_calls, 1)

    def test_closed_attempt_never_reports_ready_or_republishes_metadata(self) -> None:
        host = self._host()
        del host._start_remote_monitor
        process = CountingProcess(101)
        log = CountingLog()
        resources = self._installed_waiting(host, process, log)
        self.assertTrue(host._advance_remote_startup(resources.generation, "VERIFYING_AUTHORITY"))
        host._stop_owned_remote_connection()

        self.assertFalse(host._apply_remote_ready_if_current(resources.generation))
        self.assertEqual(RESOURCES.finish_ready_wait(host, resources), RESOURCES.WAIT_STALE)

        self.assertEqual(host.remote_startup.state, MODULE.RemoteStartupState.STOPPED)
        self.assertEqual(host.remote_ready_attempt_id, 0)
        self.assertIsNone(host.remote_monitor)
        self.assertEqual(host._request_remote_stop_owned.call_count, 1)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(log.close_calls, 1)

    def _verifying_attempt(self, host: object) -> int:
        """Drive one fresh attempt to VERIFYING_AUTHORITY through the real path."""

        start = RESOURCES.start_remote_attempt(host)
        for state in ("STARTING_TUNNEL", "WAITING_REMOTE_READY", "VERIFYING_AUTHORITY"):
            self.assertTrue(host._advance_remote_startup(start.generation, state))
        return start.generation

    def test_stale_monitor_callback_cannot_clobber_the_newer_monitor(self) -> None:
        host = self._host()
        del host._start_remote_monitor
        built: dict[str, FakeMonitor] = {}

        def build(**options: object) -> FakeMonitor:
            # Keyed by the building thread, because the stale attempt builds
            # its monitor last: it is paused before construction.
            monitor = FakeMonitor(**options)
            built[threading.current_thread().name] = monitor
            return monitor

        first_admitted = threading.Event()
        resume_first = threading.Event()
        second_published = threading.Event()
        resume_second = threading.Event()

        def on_admitted(attempt_id: str) -> None:
            if str(attempt_id) == "1":
                first_admitted.set()
                self.assertTrue(resume_first.wait(timeout=5))

        def on_published(attempt_id: str) -> None:
            if str(attempt_id) == "2":
                second_published.set()
                self.assertTrue(resume_second.wait(timeout=5))

        host._remote_barrier_monitor_admitted = on_admitted
        host._remote_barrier_monitor_published = on_published
        failures: list[BaseException] = []

        def apply_ready(generation: int):
            def run() -> None:
                try:
                    host._apply_remote_ready_if_current(generation)
                except BaseException as error:  # pragma: no cover - reported below
                    failures.append(error)

            return run

        with mock.patch.object(MODULE, "RemoteConnectionMonitor", side_effect=build):
            first = self._verifying_attempt(host)
            stale_thread = threading.Thread(target=apply_ready(first), name="stale-attempt")
            stale_thread.start()
            self.assertTrue(first_admitted.wait(timeout=5))

            host._stop_owned_remote_connection()
            second = self._verifying_attempt(host)
            fresh_thread = threading.Thread(target=apply_ready(second), name="fresh-attempt")
            fresh_thread.start()
            self.assertTrue(second_published.wait(timeout=5))

            resume_first.set()
            stale_thread.join(timeout=5)
            self.assertFalse(stale_thread.is_alive())

            resume_second.set()
            fresh_thread.join(timeout=5)
            self.assertFalse(fresh_thread.is_alive())

        self.assertEqual(failures, [])
        self.assertEqual(sorted(built), ["fresh-attempt", "stale-attempt"])
        stale_monitor = built["stale-attempt"]
        fresh_monitor = built["fresh-attempt"]
        self.assertIs(host.remote_monitor, fresh_monitor)
        self.assertEqual(host.remote_monitor_attempt_id, str(second))
        self.assertEqual(stale_monitor.start_calls, 0)
        self.assertEqual(fresh_monitor.start_calls, 1)
        self.assertEqual(host.remote_startup.active_attempt_id, str(second))
        self.assertEqual(host.remote_startup.state, MODULE.RemoteStartupState.MONITORING)

        host._stop_remote_monitor()
        self.assertEqual(fresh_monitor.stop_calls, 1)
        self.assertEqual(stale_monitor.stop_calls, 0)
        self.assertIsNone(host.remote_monitor)
        self.assertEqual(host.remote_monitor_attempt_id, "")

    def test_stale_callback_cannot_publish_after_the_newer_monitor_finished(self) -> None:
        host = self._host()
        del host._start_remote_monitor
        built: dict[str, FakeMonitor] = {}

        def build(**options: object) -> FakeMonitor:
            monitor = FakeMonitor(**options)
            built[threading.current_thread().name] = monitor
            return monitor

        admitted = threading.Event()
        resume = threading.Event()

        def on_admitted(attempt_id: object) -> None:
            if str(attempt_id) == "1":
                admitted.set()
                self.assertTrue(resume.wait(timeout=5))

        host._remote_barrier_monitor_admitted = on_admitted
        failures: list[BaseException] = []

        def stale_ready() -> None:
            try:
                host._apply_remote_ready_if_current(first)
            except BaseException as error:  # pragma: no cover - reported below
                failures.append(error)

        with mock.patch.object(MODULE, "RemoteConnectionMonitor", side_effect=build):
            first = self._verifying_attempt(host)
            stale_thread = threading.Thread(target=stale_ready, name="stale-attempt")
            stale_thread.start()
            self.assertTrue(admitted.wait(timeout=5))

            host._stop_owned_remote_connection()
            second = self._verifying_attempt(host)
            self.assertTrue(host._apply_remote_ready_if_current(second))
            fresh_monitor = built["MainThread"]
            self.assertEqual(fresh_monitor.start_calls, 1)
            # The newer monitor's own thread has since finished, so the slot is
            # free: only the captured-generation check can refuse the stale one.
            fresh_monitor.running = False

            resume.set()
            stale_thread.join(timeout=5)
            self.assertFalse(stale_thread.is_alive())

        self.assertEqual(failures, [])
        self.assertIs(host.remote_monitor, fresh_monitor)
        self.assertEqual(host.remote_monitor_attempt_id, str(second))
        self.assertEqual(built["stale-attempt"].start_calls, 0)
        self.assertEqual(fresh_monitor.start_calls, 1)

    def test_close_between_publication_and_start_never_starts_the_monitor(self) -> None:
        host = self._host()
        del host._start_remote_monitor
        built: list[FakeMonitor] = []

        def build(**options: object) -> FakeMonitor:
            monitor = FakeMonitor(**options)
            built.append(monitor)
            return monitor

        published = threading.Event()
        resume = threading.Event()

        def on_published(attempt_id: object) -> None:
            if str(attempt_id) == "1":
                published.set()
                self.assertTrue(resume.wait(timeout=5))

        host._remote_barrier_monitor_published = on_published
        failures: list[BaseException] = []

        def ready() -> None:
            try:
                host._apply_remote_ready_if_current(attempt)
            except BaseException as error:  # pragma: no cover - reported below
                failures.append(error)

        with mock.patch.object(MODULE, "RemoteConnectionMonitor", side_effect=build):
            attempt = self._verifying_attempt(host)
            thread = threading.Thread(target=ready, name="ready-attempt")
            thread.start()
            self.assertTrue(published.wait(timeout=5))

            host._stop_owned_remote_connection()
            resume.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(failures, [])
        self.assertEqual(len(built), 1)
        monitor = built[0]
        self.assertEqual(monitor.start_calls, 0)
        self.assertEqual(monitor.stop_calls, 0)
        self.assertIsNone(host.remote_monitor)
        self.assertEqual(host.remote_monitor_attempt_id, "")
        self.assertEqual(host.remote_startup.state, MODULE.RemoteStartupState.STOPPED)

    def test_close_inside_the_start_window_is_stopped_by_the_starting_thread(self) -> None:
        host = self._host()
        del host._start_remote_monitor
        gate = threading.Event()
        built: list[FakeMonitor] = []

        def build(**options: object) -> FakeMonitor:
            monitor = FakeMonitor(**options)
            monitor.start_gate = gate
            built.append(monitor)
            return monitor

        failures: list[BaseException] = []

        def ready() -> None:
            try:
                host._apply_remote_ready_if_current(attempt)
            except BaseException as error:  # pragma: no cover - reported below
                failures.append(error)

        with mock.patch.object(MODULE, "RemoteConnectionMonitor", side_effect=build):
            attempt = self._verifying_attempt(host)
            thread = threading.Thread(target=ready, name="ready-attempt")
            thread.start()
            monitor = None
            for _ in range(500):
                if built and built[0].start_entered.wait(timeout=0.01):
                    monitor = built[0]
                    break
            self.assertIsNotNone(monitor)

            # The close must not wait on a monitor that is slow to start, even
            # though it holds the lifecycle lock while detaching the slot.
            host._stop_owned_remote_connection()
            self.assertFalse(monitor.running)

            gate.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(failures, [])
        self.assertEqual(monitor.start_calls, 1)
        self.assertEqual(monitor.stop_calls, 1)
        self.assertFalse(monitor.running)
        self.assertIsNone(host.remote_monitor)
        self.assertEqual(host.remote_monitor_attempt_id, "")
        self.assertEqual(host.remote_startup.state, MODULE.RemoteStartupState.STOPPED)

    def test_unconfirmed_reap_still_closes_the_captured_log(self) -> None:
        host = self._host()
        process = UnreapableProcess(120)
        log = CountingLog()
        resources = MODULE.RemoteAttemptResources(
            generation=1,
            profile=host.remote_profile,
            token="owned-token",
            process=process,
            log=log,
        )

        RESOURCES.release_captured(host, resources)

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(log.close_calls, 1)
        traced = " ".join(str(call.args[0]) for call in host._trace.call_args_list)
        self.assertIn("reap unconfirmed", traced)
        self.assertIn("120", traced)

        RESOURCES.release_captured(host, resources)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(log.close_calls, 1)
        self.assertEqual(host._request_remote_stop_owned.call_count, 1)

    def test_terminate_helper_keeps_both_bounded_waits(self) -> None:
        process = UnreapableProcess(121)
        with self.assertRaises(subprocess.TimeoutExpired):
            MODULE.WorkStackDesktopHost._terminate_owned_process(process)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)


class DesktopStopOutcomeOnTheRealHostTest(unittest.TestCase):
    """What the shipped host establishes about a stop, on the real class.

    The remote-side outcome is only as good as the seam that carries it back,
    and on this base that seam returns nothing.  These oracles pin what that
    means: the release path reports an honest unknown rather than a success,
    and the moment the host does return an outcome it is the one recorded.
    """

    def _host(self) -> object:
        # The same shipped host the oracles above build, without inheriting
        # and re-running their cases.
        return DesktopRemoteAttemptLifecycleTest._host(self)

    def _bundle(self, host: object, token: str = "owned-token") -> object:
        return MODULE.RemoteAttemptResources(
            generation=1, profile=host.remote_profile, token=token, process=None, log=None
        )

    def test_a_host_that_reports_nothing_never_yields_a_confirmed_stop(self) -> None:
        host = self._host()
        result = RESOURCES.release_captured(host, self._bundle(host))
        self.assertFalse(result.confirmed)
        self.assertEqual(result.process_exit, "unknown")
        self.assertEqual(result.code, STOP.STOP_RESULT_ABSENT)
        self.assertTrue(result.requested)

    def test_the_outcome_is_published_for_the_snapshot_to_project(self) -> None:
        host = self._host()
        result = RESOURCES.release_captured(host, self._bundle(host))
        self.assertIs(host.remote_stop_outcome, result)
        facts = result.owner_facts()
        self.assertEqual(
            set(facts),
            {
                "state",
                "token_available",
                "pidfd_available",
                "process_exit",
                "listener_release",
                "lease_release",
            },
        )

    def test_a_reported_outcome_reaches_the_host_unchanged(self) -> None:
        host = self._host()
        host._request_remote_stop_owned = mock.Mock(
            return_value=STOP.StopResult(
                code=STOP.STOP_CONFIRMED_EXIT,
                state="dead",
                token_available=True,
                process_exit="verified",
            )
        )
        result = RESOURCES.release_captured(host, self._bundle(host))
        self.assertTrue(result.confirmed)
        self.assertEqual(host.remote_stop_outcome.code, STOP.STOP_CONFIRMED_EXIT)

    def test_a_missing_token_is_reported_and_no_stop_is_addressed(self) -> None:
        host = self._host()
        result = RESOURCES.release_captured(host, self._bundle(host, ""))
        self.assertEqual(result.code, STOP.STOP_NOT_REQUESTED_NO_TOKEN)
        self.assertFalse(result.requested)
        host._request_remote_stop_owned.assert_not_called()

    def test_the_close_path_reports_the_outcome_it_obtained(self) -> None:
        host = self._host()
        host.remote_session_token = "owned-token"
        host.remote_session_token_hash = "hash-not-the-token"
        host._stop_remote_monitor = mock.Mock()
        host._request_remote_stop_owned = mock.Mock(
            return_value=STOP.StopResult(
                code=STOP.STOP_UNCONFIRMED_TIMEOUT, state="stopping", process_exit="failed"
            )
        )
        outcome = RESOURCES.stop_owned_connection(host)
        self.assertIsNotNone(outcome)
        self.assertFalse(outcome.confirmed)
        self.assertEqual(outcome.process_exit, "failed")

    def test_the_trace_of_a_stop_never_carries_the_session_token(self) -> None:
        host = self._host()
        token = "owned-token-not-enforced-99"
        RESOURCES.release_captured(host, self._bundle(host, token))
        traced = " ".join(str(call.args[0]) for call in host._trace.call_args_list)
        self.assertNotIn(token, traced)


class DesktopStopOutcomeCorrectionsOnTheRealHostTest(unittest.TestCase):
    """The A-R1..A-R6 corrections, driven through the shipped host class.

    These enter at the same places production does -- the real
    ``_request_remote_stop_owned`` and the ``ssot_connection`` runner it
    delegates its ``subprocess.run`` to, the real ``stop_owned_connection``
    and the real ``start_remote_attempt`` -- so what they pin is the reachable
    path and not a helper's return value.
    """

    def _host(self) -> object:
        return DesktopRemoteAttemptLifecycleTest._host(self)

    def _real_stop_seam(self, host: object) -> None:
        """Drop the fixture's Mock so the shipped method really runs."""

        del host._request_remote_stop_owned

    def _bundle(self, host: object, token: str = "owned-token", generation: int = 1) -> object:
        return MODULE.RemoteAttemptResources(
            generation=generation,
            profile=host.remote_profile,
            token=token,
            process=None,
            log=None,
        )

    def _confirmed(self) -> object:
        return STOP.StopResult(
            code=STOP.STOP_CONFIRMED_EXIT,
            state="dead",
            token_available=True,
            process_exit="verified",
        )

    # -- A-R4 --------------------------------------------------------------

    def test_the_real_seams_timeout_propagates_as_a_timeout(self) -> None:
        """Clarification 1: the shipped seam lets TimeoutExpired out."""

        host = self._host()
        self._real_stop_seam(host)
        with mock.patch.object(MODULE, "find_ssh_executable", return_value="ssh"), \
                mock.patch.object(
                    MODULE.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired(cmd=["ssh"], timeout=10),
                ):
            result = RESOURCES.release_captured(host, self._bundle(host))

        self.assertEqual(result.code, STOP.STOP_REQUEST_TIMED_OUT)
        self.assertFalse(result.confirmed)
        self.assertIs(result.token_available, True)

    def test_the_real_seams_launch_failure_stays_a_launch_failure(self) -> None:
        host = self._host()
        self._real_stop_seam(host)
        with mock.patch.object(MODULE, "find_ssh_executable", return_value="ssh"), \
                mock.patch.object(
                    MODULE.subprocess, "run", side_effect=OSError("ssh is missing")
                ):
            result = RESOURCES.release_captured(host, self._bundle(host))

        self.assertEqual(result.code, STOP.STOP_REQUEST_FAILED)

    # -- A-R3 --------------------------------------------------------------

    def test_a_nonzero_exit_carrying_a_confirmed_payload_never_confirms(self) -> None:
        """The clarified seam hands back the CompletedProcess; rc still counts."""

        host = self._host()
        host._request_remote_stop_owned = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], 3, STOP.encode_stop_result(self._confirmed()), b""
            )
        )
        result = RESOURCES.release_captured(host, self._bundle(host))
        self.assertFalse(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_RESULT_UNREADABLE)
        self.assertEqual(result.process_exit, "unknown")

    def test_an_agreeing_payload_on_the_real_host_still_confirms(self) -> None:
        host = self._host()
        host._request_remote_stop_owned = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], STOP.EXIT_CONFIRMED, STOP.encode_stop_result(self._confirmed()), b""
            )
        )
        result = RESOURCES.release_captured(host, self._bundle(host))
        self.assertTrue(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_CONFIRMED_EXIT)

    # -- A-R1 --------------------------------------------------------------

    def test_a_close_with_an_unresolved_second_owner_is_not_confirmed(self) -> None:
        host = self._host()
        host._stop_remote_monitor = mock.Mock()
        answers = {
            "old-token": self._confirmed(),
            "new-token": STOP.StopResult(
                code=STOP.STOP_UNCONFIRMED_TIMEOUT,
                state="stopping",
                token_available=True,
                process_exit="failed",
            ),
        }
        host._request_remote_stop_owned = mock.Mock(
            side_effect=lambda _profile, token: answers[token]
        )
        first = RESOURCES.start_remote_attempt(host)
        with RESOURCES.resource_lock_for(host):
            RESOURCES.gate_for(host).install_locked(
                self._bundle(host, "old-token", first.generation)
            )
        RESOURCES.start_remote_attempt(host)
        host.remote_session_token = "new-token"

        outcome = RESOURCES.stop_owned_connection(host)

        self.assertFalse(outcome.confirmed)
        self.assertEqual(outcome.code, STOP.STOP_UNCONFIRMED_TIMEOUT)
        self.assertIs(host.remote_stop_outcome, outcome)

    # -- A-R2 --------------------------------------------------------------

    def test_reopening_on_the_real_host_clears_the_previous_outcome(self) -> None:
        host = self._host()
        host._request_remote_stop_owned = mock.Mock(return_value=self._confirmed())
        first = RESOURCES.start_remote_attempt(host)
        RESOURCES.release_captured(
            host, self._bundle(host, "closed-token", first.generation)
        )
        self.assertTrue(host.remote_stop_outcome.confirmed)

        second = RESOURCES.start_remote_attempt(host)

        self.assertIsNone(host.remote_stop_outcome)
        self.assertEqual(host.remote_stop_outcome_generation, second.generation)

    def test_a_stale_cleanup_on_the_real_host_keeps_the_newer_outcome(self) -> None:
        host = self._host()
        answers = {
            "stale-token": self._confirmed(),
            "current-token": STOP.StopResult(
                code=STOP.STOP_UNCONFIRMED_NO_PIDFD,
                token_available=True,
                pidfd_available=False,
                process_exit="failed",
            ),
        }
        host._request_remote_stop_owned = mock.Mock(
            side_effect=lambda _profile, token: answers[token]
        )
        first = RESOURCES.start_remote_attempt(host)
        stale = self._bundle(host, "stale-token", first.generation)
        second = RESOURCES.start_remote_attempt(host)
        newer = RESOURCES.release_captured(
            host, self._bundle(host, "current-token", second.generation)
        )

        RESOURCES.cleanup_captured(host, stale, MODULE.CLEANUP_STALE)

        self.assertIs(host.remote_stop_outcome, newer)
        self.assertFalse(host.remote_stop_outcome.confirmed)

    # -- A-R6 --------------------------------------------------------------

    def test_the_real_host_never_reports_the_forward_as_the_owners_listener(self) -> None:
        host = self._host()
        host._request_remote_stop_owned = mock.Mock(return_value=self._confirmed())
        result = RESOURCES.release_captured(host, self._bundle(host))
        self.assertEqual(result.listener_release, "unknown")
        self.assertIn(result.forward_listener_release, ("verified", "unknown"))
        self.assertEqual(result.owner_facts()["listener_release"], "unknown")

    def test_the_forward_is_observed_on_a_lost_token_close_too(self) -> None:
        host = self._host()
        host._request_remote_stop_owned = mock.Mock()
        result = RESOURCES.release_captured(host, self._bundle(host, ""))
        self.assertEqual(result.code, STOP.STOP_NOT_REQUESTED_NO_TOKEN)
        host._request_remote_stop_owned.assert_not_called()
        self.assertIn(result.forward_listener_release, ("verified", "failed"))
        self.assertEqual(result.listener_release, "unknown")

    # -- A-R2: admission and publication are one critical section ----------

    def test_a_start_between_the_stale_check_and_its_write_cannot_get_in(self) -> None:
        """The interleaving no sequential stale case reaches.

        The sequential cases start generation 2 before generation 1 reads the
        stamp, so they never exercise the pair the review interleaved. Here a
        stale generation-1 publication is stopped between reading the stamp and
        writing the result, and a real ``start_remote_attempt`` runs from
        another thread in exactly that window. It must not get in: admission
        and the write are one critical section under the same lifecycle lock a
        start takes, so the starting thread waits, and generation 2 owns the
        field whichever side of the write it lands on.
        """

        host = self._host()
        host._request_remote_stop_owned = mock.Mock(return_value=self._confirmed())
        first = RESOURCES.start_remote_attempt(host)
        stale = self._bundle(host, "stale-token", first.generation)

        started = threading.Event()
        blocked: list[bool] = []
        reopened: list[object] = []
        starter: list[threading.Thread] = []

        def _reopen() -> None:
            reopened.append(RESOURCES.start_remote_attempt(host))
            started.set()

        def _between_the_check_and_the_write(generation: int) -> None:
            if starter or generation != first.generation:
                return
            thread = threading.Thread(target=_reopen, name="reopen-mid-publish")
            starter.append(thread)
            thread.start()
            # The window itself: generation 1 has been admitted and has not
            # written yet. A reopen that landed here would be overwritten by
            # the write below -- the reproduced defect -- so what is pinned is
            # that it cannot land here at all.
            blocked.append(not started.wait(timeout=0.5))

        host._remote_barrier_stop_publish = _between_the_check_and_the_write

        RESOURCES.cleanup_captured(host, stale, MODULE.CLEANUP_STALE)

        self.assertEqual(len(starter), 1)
        starter[0].join(timeout=10)
        self.assertFalse(starter[0].is_alive())
        self.assertEqual(blocked, [True])
        self.assertGreater(reopened[0].generation, first.generation)
        # The newer generation owns the field. A reopened connection is never
        # told its own owner is dead with a proven exit the previous attempt
        # established.
        self.assertIsNone(host.remote_stop_outcome)
        self.assertEqual(host.remote_stop_outcome_generation, reopened[0].generation)
        self.assertNotEqual(host.remote_stop_outcome_generation, first.generation)

    def test_a_start_that_wins_the_lock_first_makes_the_stale_publish_withhold(
        self,
    ) -> None:
        """The other side of the same race, through the same host path.

        The reopen completes before the stale publication starts, so this time
        the stale attempt reads the newer stamp and writes nothing. Both
        orderings are legal; neither may end with the older attempt's result on
        the field.
        """

        host = self._host()
        host._request_remote_stop_owned = mock.Mock(return_value=self._confirmed())
        first = RESOURCES.start_remote_attempt(host)
        stale = self._bundle(host, "stale-token", first.generation)
        second = RESOURCES.start_remote_attempt(host)

        RESOURCES.cleanup_captured(host, stale, MODULE.CLEANUP_STALE)

        self.assertIsNone(host.remote_stop_outcome)
        self.assertEqual(host.remote_stop_outcome_generation, second.generation)
        traced = " ".join(str(call.args[0]) for call in host._trace.call_args_list)
        self.assertIn("not published", traced)

    def test_publishing_under_a_caller_that_already_holds_the_lock_is_no_deadlock(
        self,
    ) -> None:
        """The lifecycle lock is reentrant, so the new critical section is safe.

        No production caller publishes while holding it -- ``cleanup_captured``
        releases before the release path begins -- but the release path is
        reachable from a caller that does, and taking the lock twice on one
        thread must stay a no-op rather than a hang.
        """

        host = self._host()
        host._request_remote_stop_owned = mock.Mock(return_value=self._confirmed())
        start = RESOURCES.start_remote_attempt(host)

        with RESOURCES.resource_lock_for(host):
            result = RESOURCES.release_captured(
                host, self._bundle(host, "owned-token", start.generation)
            )

        self.assertTrue(result.confirmed)
        self.assertIs(host.remote_stop_outcome, result)
        self.assertEqual(host.remote_stop_outcome_generation, start.generation)

    # -- A-R3: the confirmed family is closed over its own evidence --------

    def _reported_through_the_real_seam(
        self, host: object, payload: object, returncode: object = None
    ) -> object:
        """Hand one payload back as the CompletedProcess the seam returns."""

        status = STOP.exit_code_for(payload.code) if returncode is None else returncode
        host._request_remote_stop_owned = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], status, STOP.encode_stop_result(payload), b""
            )
        )
        return RESOURCES.release_captured(host, self._bundle(host))

    def test_a_confirmed_exit_without_the_token_that_authorized_it_never_confirms(
        self,
    ) -> None:
        """The review's first counterexample, through the real seam.

        Every honest confirmed path is downstream of the token admission, so a
        confirmed exit reported with the token refused is a claim about a
        process this session was never shown it had standing over.
        """

        host = self._host()
        result = self._reported_through_the_real_seam(
            host,
            STOP.StopResult(
                code=STOP.STOP_CONFIRMED_EXIT,
                state="dead",
                token_available=False,
                pidfd_available=True,
                process_exit="verified",
            ),
        )

        self.assertFalse(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_RESULT_UNREADABLE)
        self.assertEqual(result.process_exit, "unknown")
        self.assertEqual(result.state, "unknown")
        self.assertIn("token", result.detail)
        self.assertFalse(host.remote_stop_outcome.confirmed)

    def test_a_stop_confirmed_without_a_signal_cannot_claim_a_pidfd(self) -> None:
        """The review's second counterexample, through the real seam.

        ``stop_confirmed_without_signal`` exists only on the observation path
        taken where no pidfd can be opened, and that path hard-codes the
        capability to false. A host claiming both is not describing any stop
        this contract can produce.
        """

        host = self._host()
        result = self._reported_through_the_real_seam(
            host,
            STOP.StopResult(
                code=STOP.STOP_CONFIRMED_WITHOUT_SIGNAL,
                state="dead",
                token_available=True,
                pidfd_available=True,
                process_exit="verified",
            ),
        )

        self.assertFalse(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_RESULT_UNREADABLE)
        self.assertEqual(result.process_exit, "unknown")
        self.assertIn("pidfd", result.detail)
        self.assertFalse(host.remote_stop_outcome.confirmed)

    def test_every_legal_reported_outcome_is_still_read_as_itself(self) -> None:
        """The positives the closed predicate must not cost.

        One row per shape an owned stop actually emits, so completing the
        predicate cannot quietly make a real shutdown unsayable. ``unknown``
        still claims nothing: a confirmation that never measured the pidfd
        capability is under-specified, not inconsistent.
        """

        legal = [
            (
                "an exit watched through the signalled handle",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_EXIT,
                    state="dead",
                    token_available=True,
                    pidfd_available=True,
                    process_exit="verified",
                ),
                True,
            ),
            (
                "a pid that held no process on a pidfd host",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_ALREADY_EXITED,
                    state="dead",
                    token_available=True,
                    pidfd_available=True,
                    process_exit="verified",
                ),
                True,
            ),
            (
                "a pid already classified dead where no pidfd exists",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_ALREADY_EXITED,
                    state="dead",
                    token_available=True,
                    pidfd_available=False,
                    process_exit="verified",
                ),
                True,
            ),
            (
                "a controller that never answered the capability question",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_ALREADY_EXITED,
                    state="dead",
                    token_available=True,
                    process_exit="verified",
                ),
                True,
            ),
            (
                "the no-pidfd observation completing after the channel closed",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_WITHOUT_SIGNAL,
                    state="dead",
                    token_available=True,
                    pidfd_available=False,
                    process_exit="verified",
                ),
                True,
            ),
            (
                "a no-signal confirmation that measured no capability",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_WITHOUT_SIGNAL,
                    state="dead",
                    token_available=True,
                    process_exit="verified",
                ),
                True,
            ),
            (
                "an unfenced receipt refusing this caller",
                STOP.StopResult(
                    code=STOP.STOP_REFUSED_UNFENCED_RECEIPT,
                    state="unfenced",
                    token_available=True,
                    pidfd_available=True,
                ),
                False,
            ),
            (
                "a token mismatch that proves nothing about liveness",
                STOP.StopResult(
                    code=STOP.STOP_REFUSED_TOKEN_MISMATCH,
                    state="unknown",
                    token_available=False,
                ),
                False,
            ),
            (
                "an owner watched and still running where no pidfd exists",
                STOP.StopResult(
                    code=STOP.STOP_UNCONFIRMED_NO_PIDFD,
                    state="live",
                    token_available=True,
                    pidfd_available=False,
                    process_exit="failed",
                ),
                False,
            ),
            (
                "an absent receipt, which proves neither dead nor live",
                STOP.StopResult(
                    code=STOP.STOP_RECEIPT_ABSENT,
                    state="unknown",
                    token_available=True,
                ),
                False,
            ),
        ]

        for name, payload, expected in legal:
            with self.subTest(name):
                host = self._host()
                result = self._reported_through_the_real_seam(host, payload)
                self.assertEqual(result.code, payload.code)
                self.assertEqual(result.confirmed, expected)
                self.assertEqual(STOP.report_contradiction(result), "")
                self.assertIs(host.remote_stop_outcome, result)

    def test_no_contradictory_reported_outcome_survives_the_real_seam(self) -> None:
        """The negatives, one row per impossible code/evidence combination.

        Each is allowlisted field by field and agrees with the exit status its
        own code maps to, so nothing but the consistency predicate stands
        between it and a confirmed stop.
        """

        impossible = [
            (
                "a confirmed exit whose token was refused",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_EXIT,
                    state="dead",
                    token_available=False,
                    process_exit="verified",
                ),
                "token",
            ),
            (
                "a confirmed exit that reports no token at all",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_EXIT,
                    state="dead",
                    pidfd_available=True,
                    process_exit="verified",
                ),
                "token",
            ),
            (
                "an already-exited pid whose token was refused",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_ALREADY_EXITED,
                    state="dead",
                    token_available=False,
                    process_exit="verified",
                ),
                "token",
            ),
            (
                "a no-signal confirmation that reports no token",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_WITHOUT_SIGNAL,
                    state="dead",
                    pidfd_available=False,
                    process_exit="verified",
                ),
                "token",
            ),
            (
                "a stop confirmed without a signal on a pidfd host",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_WITHOUT_SIGNAL,
                    state="dead",
                    token_available=True,
                    pidfd_available=True,
                    process_exit="verified",
                ),
                "pidfd",
            ),
            (
                "a handle-watched exit on a host with no pidfd",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_EXIT,
                    state="dead",
                    token_available=True,
                    pidfd_available=False,
                    process_exit="verified",
                ),
                "pidfd",
            ),
            (
                "a proven exit for an owner said to be live",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_EXIT,
                    state="live",
                    token_available=True,
                    pidfd_available=True,
                    process_exit="verified",
                ),
                "live",
            ),
            (
                "a proven exit under a code that confirms nothing",
                STOP.StopResult(
                    code=STOP.STOP_UNCONFIRMED_NO_PIDFD,
                    state="unknown",
                    token_available=True,
                    pidfd_available=False,
                    process_exit="verified",
                ),
                "confirms nothing",
            ),
            (
                "a confirmed code with no proven exit behind it",
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_EXIT,
                    state="dead",
                    token_available=True,
                    pidfd_available=True,
                    process_exit="failed",
                ),
                "without a proven exit",
            ),
            (
                "a refusal for an owner said to be dead",
                STOP.StopResult(
                    code=STOP.STOP_REFUSED_FOREIGN_HOST,
                    state="dead",
                    token_available=True,
                ),
                "refusal",
            ),
            (
                "an owner said to be dead for a stop never asked for",
                STOP.StopResult(
                    code=STOP.STOP_NOT_REQUESTED_NO_TOKEN,
                    state="dead",
                    token_available=False,
                ),
                "never asked for",
            ),
        ]

        for name, payload, expected_reason in impossible:
            with self.subTest(name):
                host = self._host()
                result = self._reported_through_the_real_seam(host, payload)
                self.assertEqual(result.code, STOP.STOP_RESULT_UNREADABLE)
                self.assertFalse(result.confirmed)
                self.assertEqual(result.process_exit, "unknown")
                self.assertEqual(result.state, "unknown")
                self.assertIn("inconsistent", result.detail)
                self.assertIn(expected_reason, result.detail)
                self.assertFalse(host.remote_stop_outcome.confirmed)


if __name__ == "__main__":
    unittest.main()
