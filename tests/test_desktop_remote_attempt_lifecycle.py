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


if __name__ == "__main__":
    unittest.main()
