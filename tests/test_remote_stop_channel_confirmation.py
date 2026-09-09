"""M12: why the first stop-owned could not be confirmed, and what proves it.

The close canary reproduced `stop_request_timed_out; owner=unknown` against a
corrected fixture, a real main-window close and a matching packaged remote, so
the timeout was not a fixture artifact.  Measured on a disposable WSL fixture
running this source under the packaged runtime, the cause is an ordering one
and not a budget one:

  * with nothing connected to the served port, a pidfd-bound SIGTERM is
    confirmed end to end in 0.19s;
  * with one socket open on that port -- exactly what the desktop's own `-L`
    forward is holding while it asks -- the signalled owner cannot finish
    exiting at all, because the served listener joins the request thread that
    socket is holding, so the stop spends its whole wait and reports an honest
    unconfirmed;
  * closing that socket ends the owner in 0.05s, and asking the same
    authenticated question again then answers it in 0.14s.

So the exit the first request waits for requires the caller to close the very
channel it is holding open in order to ask, and no larger transport budget can
ever collect it.  These tests pin the two halves of that: the remote decision
stays bounded well inside the transport budget, and the proven exit is earned
on the pass that runs after the channel is gone -- without any absence of
evidence being promoted into evidence of an exit.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
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

import remote_attempt_bundle as BUNDLE  # noqa: E402
import remote_attempt_resources as RESOURCES  # noqa: E402
import remote_attempt_shutdown as SHUTDOWN  # noqa: E402
import remote_owner as OWNER  # noqa: E402
import remote_owner_stop as OWNER_STOP  # noqa: E402
import remote_stop_result as STOP  # noqa: E402
import ssot_connection as SSOT  # noqa: E402
from remote_command_contract import token_hash  # noqa: E402

MODULE_PATH = SHELL / "workstack_desktop.py"
SPEC = importlib.util.spec_from_file_location("workstack_desktop_stop_confirmation", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    SPEC.loader.exec_module(MODULE)


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OWN_TOKEN = "r5pending-token-not-enforced-01"
HOST_IDENTITY = "b" * 64
BOOT_IDENTITY = "c" * 64
REQUIRED_REMOTE_PYTHON = "/srv/workstack/venv/bin/python"
OWNER_PID = 4242
OWNER_START = "start-1"


class FakeClock:
    """The stop's own injected clock, so a bounded wait costs no real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class ReapableProcess:
    """A captured SSH process double that really goes when terminated.

    The confirmation is coordinated with the observed reap, not with the
    terminate call returning, so a double whose ``poll`` never answers is a
    channel that is still open.
    """

    def __init__(self, pid: int = 77) -> None:
        self.pid = pid
        self.returncode: object = None

    def poll(self) -> object:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float) -> object:
        return self.returncode


class ProcessTicketBarrier(BUNDLE.AttemptCleanupClaim):
    """A bundle claim that holds one named thread at the process ticket.

    The interleavings under test are about which release reaches the process
    ticket first, so the test seam is the ticket itself rather than a sleep.
    """

    def __init__(self, paused_thread: str) -> None:
        super().__init__()
        self._paused = paused_thread
        self.reached = threading.Event()
        self.released = threading.Event()

    def take(self, effect: str) -> bool:
        if effect == BUNDLE.CLAIM_PROCESS and threading.current_thread().name == self._paused:
            self.reached.set()
            self.released.wait(timeout=10)
        return super().take(effect)


class GatedOwnedProcess:
    """A handle bound to the pid it opened on, like a pidfd."""

    def __init__(self, controller: "GatedController") -> None:
        self.controller = controller
        self.closes = 0

    def has_exited(self) -> bool | None:
        return self.controller.exited

    def send_terminate(self) -> None:
        self.controller.signals += 1

    def close(self) -> None:
        self.closes += 1


class GatedController:
    """An owner whose exit is gated on something this stop cannot do.

    That is the measured shape: the signal is delivered, and the exit it starts
    cannot complete while the caller still holds the channel.  ``release()`` is
    the caller closing that channel.
    """

    def __init__(self, *, observed: str = "live", exited: bool = False) -> None:
        self.observed = observed
        self.exited = exited
        self.signals = 0
        self.handles: list[GatedOwnedProcess] = []

    def release(self) -> None:
        self.exited = True
        self.observed = "exited"

    def current_pid(self) -> int:
        return OWNER_PID

    def start_identity(self, pid: int) -> str | None:
        return OWNER_START if pid == OWNER_PID else None

    def observe(self, pid: int, start_identity: str) -> str:
        if pid != OWNER_PID or start_identity != OWNER_START:
            return "replaced"
        return self.observed

    def open_owned_process(self, pid: int, start_identity: str):
        handle = GatedOwnedProcess(self)
        self.handles.append(handle)
        return handle

    def pidfd_available(self) -> bool | None:
        return True

    def host_identity(self) -> str | None:
        return HOST_IDENTITY

    def boot_identity(self) -> str | None:
        return BOOT_IDENTITY


def _fixture(root: Path) -> tuple[Path, Path]:
    app = root / "app"
    data = root / "data"
    (app / "workstack").mkdir(parents=True)
    data.mkdir()
    (app / "workstack" / "__init__.py").write_text(
        '__version__ = "1.0.14"\nREMOTE_PROTOCOL_VERSION = 1\n', encoding="utf-8"
    )
    (app / "run_work_stack.py").write_text("print(1)\n", encoding="utf-8")
    (data / "workspace.json").write_text(
        json.dumps({"id": WORKSPACE_ID, "name": "probe", "version": 2}), encoding="utf-8"
    )
    (data / "store-meta.json").write_text(json.dumps({"schema_version": 3}), encoding="utf-8")
    return app, data


def _write_receipt(data: Path, **overrides: object) -> bytes:
    payload: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "data_dir_digest": OWNER.local_path_digest(data),
        "app_dir_digest": "a" * 64,
        "pid": OWNER_PID,
        "start_identity": OWNER_START,
        "release_id": "1.0.14",
        "token_hash": token_hash(OWN_TOKEN),
        "host_identity": HOST_IDENTITY,
        "boot_identity": BOOT_IDENTITY,
    }
    payload.update(overrides)
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    (data / OWNER.OWNER_FILENAME).write_bytes(encoded)
    return encoded


class RemoteStopIsBoundedWhileTheChannelIsHeldTest(unittest.TestCase):
    """The remote half: it answers inside its budget instead of outlasting it."""

    def tearDown(self) -> None:
        OWNER.set_process_controller(None)

    def _default_wait(self, clock: FakeClock) -> OWNER.StopWait:
        """The shipped budget, on an injected clock. Not a budget of its own."""

        return OWNER.StopWait(
            OWNER.StopWait().timeout_seconds, 0.25, clock.monotonic, clock.sleep
        )

    def test_a_signalled_owner_that_cannot_exit_yet_is_bounded_and_honest(self) -> None:
        controller = GatedController()
        OWNER.set_process_controller(controller)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _fixture(Path(directory))
            original = _write_receipt(data)
            result = OWNER.stop_owned_result(data, OWN_TOKEN, self._default_wait(clock))

            # The owner was signalled exactly once and watched through the same
            # handle, and the answer says so rather than claiming an exit.
            self.assertEqual(controller.signals, 1)
            self.assertEqual(result.code, STOP.STOP_UNCONFIRMED_TIMEOUT)
            self.assertFalse(result.confirmed)
            self.assertEqual(result.state, "stopping")
            self.assertEqual(result.process_exit, "failed")
            # Nothing was removed: the receipt is the evidence that an owner
            # this session has not seen leave is still recorded.
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        # It cost its budget and not a second more, on its own clock.
        self.assertLessEqual(clock.now, OWNER.StopWait().timeout_seconds)
        self.assertEqual([handle.closes for handle in controller.handles], [1])

    def test_the_same_owner_is_confirmed_once_the_channel_is_closed(self) -> None:
        """The second half of the measured sequence, at the decision level."""

        controller = GatedController()
        OWNER.set_process_controller(controller)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _fixture(Path(directory))
            _write_receipt(data)
            OWNER.stop_owned_result(data, OWN_TOKEN, self._default_wait(clock))
            self.assertTrue((data / OWNER.OWNER_FILENAME).exists())

            controller.release()  # the desktop closed the channel it was holding
            after = OWNER.stop_owned_result(data, OWN_TOKEN, self._default_wait(clock))
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())

        self.assertEqual(after.code, STOP.STOP_CONFIRMED_ALREADY_EXITED)
        self.assertTrue(after.confirmed)
        self.assertEqual(after.process_exit, "verified")
        # The confirming pass classified the pid as gone before it signalled
        # anything, so the owner was signalled exactly once across both passes.
        self.assertEqual(controller.signals, 1)

    def test_an_ambiguous_owner_is_refused_without_being_signalled(self) -> None:
        controller = GatedController(observed="unknown")
        OWNER.set_process_controller(controller)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _fixture(Path(directory))
            original = _write_receipt(data)
            result = OWNER.stop_owned_result(data, OWN_TOKEN, self._default_wait(clock))
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)

        self.assertEqual(result.code, STOP.STOP_AMBIGUOUS_LIVENESS)
        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.process_exit, "unknown")
        self.assertEqual(controller.signals, 0)

    def test_a_foreign_host_receipt_is_refused_and_never_inspected(self) -> None:
        controller = GatedController()
        OWNER.set_process_controller(controller)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            _app, data = _fixture(Path(directory))
            original = _write_receipt(data, host_identity="d" * 64)
            result = OWNER.stop_owned_result(data, OWN_TOKEN, self._default_wait(clock))
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)

        self.assertEqual(result.code, STOP.STOP_REFUSED_FOREIGN_HOST)
        self.assertTrue(result.refused)
        self.assertEqual(result.process_exit, "unknown")
        self.assertEqual(controller.signals, 0)

    def test_the_bounded_remote_decision_fits_inside_the_shipped_budgets(self) -> None:
        """The budget ordering the timeout came from, pinned to the real join.

        The transport budget has to exceed the whole remote decision -- SSH
        start, remote interpreter start and the remote's own wait -- or the
        desktop gives up before the remote can hand back the verdict it
        already has.  Both budgets together have to fit inside the bounded
        join the close actually runs under.
        """

        self.assertLessEqual(
            OWNER.StopWait().timeout_seconds, OWNER_STOP.UNSIGNALLED_WAIT_SECONDS
        )
        self.assertLess(OWNER.StopWait().timeout_seconds, STOP.STOP_REQUEST_TIMEOUT_SECONDS)
        join_source = inspect.getsource(MODULE.WorkStackDesktopHost.run)
        self.assertIn("self.server_stop_thread.join(timeout=12)", join_source)
        self.assertLessEqual(
            STOP.STOP_REQUEST_TIMEOUT_SECONDS + STOP.STOP_CONFIRM_TIMEOUT_SECONDS, 12
        )


class DesktopConfirmsAfterItClosedTheChannelTest(unittest.TestCase):
    """The desktop half: the second look, and what it may and may not claim."""

    def _host(self) -> object:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        MODULE.initialize_attempt_resources(host)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID, 8765, REQUIRED_REMOTE_PYTHON
        )
        host.remote_lifecycle_state = "READY"
        host.remote_ready_attempt_id = 0
        host.remote_attempt_id = 0
        host.remote_session_token = None
        host.remote_session_token_hash = None
        host.remote_ssh_log = None
        host.remote_ssh_process = None
        host._trace = mock.Mock()
        return host

    def _bundle(self, host: object, process: object = None) -> object:
        return MODULE.RemoteAttemptResources(
            generation=1,
            profile=host.remote_profile,
            token="owned-token",
            process=process,
            log=None,
        )

    def _unconfirmed_first(self) -> object:
        return STOP.StopResult(
            code=STOP.STOP_UNCONFIRMED_TIMEOUT,
            state="stopping",
            token_available=True,
            pidfd_available=True,
            process_exit="failed",
        )

    def test_the_confirmation_runs_only_after_the_owned_channel_was_closed(self) -> None:
        order: list[str] = []
        process = ReapableProcess()

        confirmed = STOP.StopResult(
            code=STOP.STOP_CONFIRMED_ALREADY_EXITED,
            state="dead",
            token_available=True,
            pidfd_available=True,
            process_exit="verified",
        )

        def request(*_args: object) -> object:
            order.append("request")
            return self._unconfirmed_first()

        def close(closed: object) -> None:
            order.append("close")
            closed.terminate()

        def confirm(*_args: object) -> object:
            order.append("confirm")
            return confirmed

        host = self._host()
        host._request_remote_stop_owned = mock.Mock(side_effect=request)
        host._terminate_owned_process = mock.Mock(side_effect=close)
        host._confirm_remote_stop_owned = mock.Mock(side_effect=confirm)

        result = RESOURCES.release_captured(host, self._bundle(host, process))

        self.assertEqual(order, ["request", "close", "confirm"])
        self.assertTrue(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_CONFIRMED_ALREADY_EXITED)
        self.assertEqual(result.state, "dead")
        self.assertEqual(result.process_exit, "verified")
        # One request and one observation. A close spends no more round trips
        # than that, whatever the answers are.
        self.assertEqual(host._request_remote_stop_owned.call_count, 1)
        self.assertEqual(host._confirm_remote_stop_owned.call_count, 1)

    def test_an_absent_receipt_on_the_confirmation_is_not_a_proven_exit(self) -> None:
        """Nothing claims the directory. That is not the same as an exit.

        An owner that exited and cleaned up, a receipt that was never written
        and one removed by hand all look like this, and no process was observed
        either way, so the first pass's honest unconfirmed stands.
        """

        host = self._host()
        host._request_remote_stop_owned = mock.Mock(return_value=self._unconfirmed_first())
        host._confirm_remote_stop_owned = mock.Mock(
            return_value=STOP.StopResult(
                code=STOP.STOP_RECEIPT_ABSENT,
                state="unknown",
                detail="no owner receipt claims this data directory",
            )
        )

        result = RESOURCES.release_captured(host, self._bundle(host))

        self.assertEqual(host._confirm_remote_stop_owned.call_count, 1)
        self.assertFalse(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_UNCONFIRMED_TIMEOUT)
        self.assertEqual(result.process_exit, "failed")

    def test_an_owner_still_live_on_the_confirmation_leaves_the_first_answer(self) -> None:
        host = self._host()
        host._request_remote_stop_owned = mock.Mock(return_value=self._unconfirmed_first())
        host._confirm_remote_stop_owned = mock.Mock(
            return_value=STOP.StopResult(
                code=STOP.STOP_UNCONFIRMED_NO_PIDFD,
                state="live",
                token_available=True,
                pidfd_available=False,
                process_exit="failed",
            )
        )

        result = RESOURCES.release_captured(host, self._bundle(host))

        self.assertFalse(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_UNCONFIRMED_TIMEOUT)
        self.assertEqual(result.state, "stopping")

    def test_a_refusal_is_never_asked_a_second_time(self) -> None:
        """Same token, same host, same receipt: a second ask is noise."""

        for code in (STOP.STOP_REFUSED_FOREIGN_HOST, STOP.STOP_REFUSED_TOKEN_MISMATCH):
            with self.subTest(code=code):
                host = self._host()
                host._request_remote_stop_owned = mock.Mock(
                    return_value=STOP.StopResult(code=code, state="unknown", token_available=True)
                )
                host._confirm_remote_stop_owned = mock.Mock()

                result = RESOURCES.release_captured(host, self._bundle(host))

                host._confirm_remote_stop_owned.assert_not_called()
                self.assertEqual(result.code, code)
                self.assertTrue(result.refused)

    def test_a_pre_signal_refusal_about_this_pid_is_never_asked_twice(self) -> None:
        """An unreadable handle and an ambiguous liveness both land before any
        signal, and both are facts about this exact pid -- a permission
        refusal, a failed pin, a /proc this host could not read.  The stop
        deliberately does not route around them, so asking the same command
        again, which could classify that pid live and signal it, is exactly
        what must not happen here.
        """

        for code in (STOP.STOP_UNCONFIRMED_HANDLE, STOP.STOP_AMBIGUOUS_LIVENESS):
            with self.subTest(code=code):
                self.assertNotIn(code, SHUTDOWN.CONFIRMABLE_CODES)
                host = self._host()
                host._request_remote_stop_owned = mock.Mock(
                    return_value=STOP.StopResult(
                        code=code, state="unknown", token_available=True
                    )
                )
                host._confirm_remote_stop_owned = mock.Mock()

                result = RESOURCES.release_captured(host, self._bundle(host))

                host._confirm_remote_stop_owned.assert_not_called()
                self.assertEqual(result.code, code)
                self.assertFalse(result.confirmed)
                self.assertEqual(result.process_exit, "unknown")

    def test_a_remote_that_answered_nothing_readable_is_never_asked_twice(self) -> None:
        """A retry of a launch is not a second observation."""

        for code in (
            STOP.STOP_RESULT_ABSENT,
            STOP.STOP_RESULT_UNREADABLE,
            STOP.STOP_REQUEST_FAILED,
        ):
            with self.subTest(code=code):
                host = self._host()
                host._request_remote_stop_owned = mock.Mock(
                    return_value=STOP.StopResult(code=code)
                )
                host._confirm_remote_stop_owned = mock.Mock()

                result = RESOURCES.release_captured(host, self._bundle(host))

                host._confirm_remote_stop_owned.assert_not_called()
                self.assertEqual(result.code, code)
                self.assertFalse(result.confirmed)

    def test_a_transport_timeout_is_still_read_a_second_time(self) -> None:
        """The m11 shape: the remote had begun a decision this pass can read."""

        self.assertIn(STOP.STOP_REQUEST_TIMED_OUT, SHUTDOWN.CONFIRMABLE_CODES)
        host = self._host()
        host._request_remote_stop_owned = mock.Mock(
            side_effect=MODULE.subprocess.TimeoutExpired(cmd=["ssh"], timeout=8)
        )
        host._confirm_remote_stop_owned = mock.Mock(
            return_value=STOP.StopResult(
                code=STOP.STOP_CONFIRMED_ALREADY_EXITED,
                state="dead",
                token_available=True,
                process_exit="verified",
            )
        )

        result = RESOURCES.release_captured(host, self._bundle(host))

        self.assertEqual(host._confirm_remote_stop_owned.call_count, 1)
        self.assertTrue(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_CONFIRMED_ALREADY_EXITED)

    def test_a_release_that_did_not_close_the_channel_never_confirms(self) -> None:
        """The two-release race, made deterministic with a barrier.

        A wins this bundle's stop ticket and blocks in the request; B loses it,
        wins the process ticket and blocks inside the terminate. A comes back
        unconfirmed, finds the process ticket already taken -- taken, not
        finished -- and must not read that as a closed channel. Confirming
        there would ask the same unanswerable question on a channel that is
        still up, and would spend the bundle's one confirmation ticket doing
        it.
        """

        host = self._host()
        process = ReapableProcess()
        bundle = self._bundle(host, process)

        a_in_request = threading.Event()
        b_took_process = threading.Event()
        b_may_finish = threading.Event()
        a_finished = threading.Event()

        def request(*_args: object) -> object:
            # A holds the stop ticket and is inside the request while B takes
            # the process ticket, which is the interleaving under test.
            a_in_request.set()
            b_took_process.wait(timeout=5)
            return self._unconfirmed_first()

        def terminate(closed: object) -> None:
            b_took_process.set()
            b_may_finish.wait(timeout=10)
            closed.terminate()

        host._request_remote_stop_owned = mock.Mock(side_effect=request)
        host._terminate_owned_process = mock.Mock(side_effect=terminate)
        host._confirm_remote_stop_owned = mock.Mock(
            return_value=STOP.StopResult(
                code=STOP.STOP_CONFIRMED_ALREADY_EXITED,
                state="dead",
                token_available=True,
                process_exit="verified",
            )
        )

        outcomes: dict[str, object] = {}

        def release_a() -> None:
            outcomes["a"] = RESOURCES.release_captured(host, bundle)
            a_finished.set()

        def release_b() -> None:
            outcomes["b"] = RESOURCES.release_captured(host, bundle)

        first = threading.Thread(target=release_a)
        second = threading.Thread(target=release_b)
        first.start()
        self.assertTrue(a_in_request.wait(timeout=5))
        second.start()
        self.assertTrue(b_took_process.wait(timeout=5))
        self.assertTrue(a_finished.wait(timeout=10))
        first.join(timeout=5)

        # A owned the stop and never closed the channel, so it did not confirm.
        host._confirm_remote_stop_owned.assert_not_called()
        self.assertFalse(outcomes["a"].confirmed)
        self.assertEqual(outcomes["a"].code, STOP.STOP_UNCONFIRMED_TIMEOUT)

        b_may_finish.set()
        second.join(timeout=10)
        self.assertFalse(second.is_alive())
        # B is the release that actually closed the channel, so B earns the
        # bundle's one confirmation -- reading what A's stop request
        # established rather than inventing an observation of its own.
        self.assertEqual(host._confirm_remote_stop_owned.call_count, 1)
        self.assertTrue(outcomes["b"].confirmed)
        self.assertEqual(outcomes["b"].code, STOP.STOP_CONFIRMED_ALREADY_EXITED)
        self.assertEqual(outcomes["b"].process_exit, "verified")
        # And the confirmed outcome is the one the host ends up publishing.
        self.assertTrue(host.remote_stop_outcome.confirmed)

    def test_the_release_that_closes_the_channel_may_confirm(self) -> None:
        """The same coordination seen from the other side: a finished close."""

        host = self._host()
        process = ReapableProcess()
        bundle = self._bundle(host, process)
        host._request_remote_stop_owned = mock.Mock(return_value=self._unconfirmed_first())
        host._terminate_owned_process = mock.Mock(side_effect=lambda closed: closed.terminate())
        host._confirm_remote_stop_owned = mock.Mock(
            return_value=STOP.StopResult(
                code=STOP.STOP_CONFIRMED_ALREADY_EXITED,
                state="dead",
                token_available=True,
                process_exit="verified",
            )
        )

        result = RESOURCES.release_captured(host, bundle)

        self.assertTrue(bundle.claim.completed(SHUTDOWN.CLAIM_PROCESS))
        self.assertEqual(host._confirm_remote_stop_owned.call_count, 1)
        self.assertTrue(result.confirmed)

    def test_a_late_release_never_downgrades_the_exit_the_close_earned(self) -> None:
        """The same race in the other order, held at the process ticket.

        A wins the stop ticket, records its unconfirmed answer and is paused
        before the process ticket. B loses the stop ticket, reads what A
        established, closes the channel, earns the one confirmation and
        publishes a proven exit. A then resumes with the process ticket
        already taken and the confirmation already spent, and finishes last --
        holding the weaker record. Neither what A returns nor what the host
        ends up publishing may fall back to that record.
        """

        host = self._host()
        process = ReapableProcess()
        barrier = ProcessTicketBarrier("release-a")
        bundle = MODULE.RemoteAttemptResources(
            generation=1,
            profile=host.remote_profile,
            token="owned-token",
            process=process,
            log=None,
            claim=barrier,
        )
        confirmed = STOP.StopResult(
            code=STOP.STOP_CONFIRMED_ALREADY_EXITED,
            state="dead",
            token_available=True,
            pidfd_available=True,
            process_exit="verified",
        )
        host._request_remote_stop_owned = mock.Mock(return_value=self._unconfirmed_first())
        host._terminate_owned_process = mock.Mock(side_effect=lambda closed: closed.terminate())
        host._confirm_remote_stop_owned = mock.Mock(return_value=confirmed)

        outcomes: dict[str, object] = {}

        def release_a() -> None:
            outcomes["a"] = RESOURCES.release_captured(host, bundle)

        first = threading.Thread(target=release_a, name="release-a")
        first.start()
        self.assertTrue(barrier.reached.wait(timeout=5))

        # B runs to completion while A is held at the process ticket.
        outcomes["b"] = RESOURCES.release_captured(host, bundle)
        self.assertTrue(outcomes["b"].confirmed)
        self.assertTrue(host.remote_stop_outcome.confirmed)

        barrier.released.set()
        first.join(timeout=10)
        self.assertFalse(first.is_alive())

        # A asked for the stop but closed nothing and confirmed nothing, and
        # finishing last does not let it unpublish what B proved.
        self.assertEqual(host._confirm_remote_stop_owned.call_count, 1)
        self.assertTrue(outcomes["a"].confirmed)
        self.assertEqual(outcomes["a"].code, STOP.STOP_CONFIRMED_ALREADY_EXITED)
        self.assertTrue(host.remote_stop_outcome.confirmed)
        self.assertEqual(host.remote_stop_outcome_generation, 1)

    def test_a_proven_exit_is_not_unpublished_by_the_same_attempt(self) -> None:
        """The publication guard on its own, without the threads."""

        host = self._host()
        confirmed = STOP.StopResult(
            code=STOP.STOP_CONFIRMED_EXIT,
            state="dead",
            token_available=True,
            process_exit="verified",
        )
        weaker = STOP.StopResult(
            code=STOP.STOP_UNCONFIRMED_TIMEOUT,
            state="stopping",
            token_available=True,
            process_exit="failed",
        )

        SHUTDOWN.publish_stop_outcome(host, 1, confirmed, None, preserve_proven_exit=True)
        returned = SHUTDOWN.publish_stop_outcome(
            host, 1, weaker, None, preserve_proven_exit=True
        )

        self.assertIs(host.remote_stop_outcome, confirmed)
        self.assertIs(returned, confirmed)

        # The aggregate is the wider statement and is deliberately allowed to
        # lower the claim at the same generation: one bundle that confirmed
        # must never speak for one that did not.
        aggregate = SHUTDOWN.publish_stop_outcome(host, 1, weaker, None)
        self.assertIs(host.remote_stop_outcome, weaker)
        self.assertIs(aggregate, weaker)

    def test_a_newer_attempt_is_never_masked_by_an_older_proven_exit(self) -> None:
        """The guard is one attempt wide. A reopen owns its own field."""

        host = self._host()
        confirmed = STOP.StopResult(
            code=STOP.STOP_CONFIRMED_EXIT,
            state="dead",
            token_available=True,
            process_exit="verified",
        )
        newer = STOP.StopResult(
            code=STOP.STOP_UNCONFIRMED_TIMEOUT,
            state="stopping",
            token_available=True,
            process_exit="failed",
        )

        SHUTDOWN.publish_stop_outcome(host, 1, confirmed, None, preserve_proven_exit=True)
        # What a reopen does under this same lock before it stamps itself.
        host.remote_stop_outcome = None
        host.remote_stop_outcome_generation = 2

        returned = SHUTDOWN.publish_stop_outcome(
            host, 2, newer, None, preserve_proven_exit=True
        )

        self.assertIs(host.remote_stop_outcome, newer)
        self.assertIs(returned, newer)
        self.assertFalse(host.remote_stop_outcome.confirmed)
        # And the older attempt still cannot write over the newer one.
        SHUTDOWN.publish_stop_outcome(host, 1, confirmed, None, preserve_proven_exit=True)
        self.assertIs(host.remote_stop_outcome, newer)

    def test_the_shipped_seams_carry_the_two_product_budgets(self) -> None:
        """Delegation, not two numbers repeated on the close path."""

        host = self._host()
        seen: list[float] = []

        def fake_run(command, **kwargs):
            seen.append(kwargs["timeout"])
            return MODULE.subprocess.CompletedProcess(command, 0, b"", b"")

        with mock.patch.object(MODULE, "find_ssh_executable", return_value="ssh"), \
                mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(SSOT.subprocess, "run", side_effect=fake_run):
            host._request_remote_stop_owned(host.remote_profile, "owned-token")
            host._confirm_remote_stop_owned(host.remote_profile, "owned-token")

        self.assertEqual(
            seen, [STOP.STOP_REQUEST_TIMEOUT_SECONDS, STOP.STOP_CONFIRM_TIMEOUT_SECONDS]
        )


if __name__ == "__main__":
    unittest.main()
