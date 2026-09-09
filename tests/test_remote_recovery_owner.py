"""R1 remote recovery: a stop is judged by its outcome, never by its launch.

The 1.0.8 incident is the shape these oracles hold the line against.  An owner
outlived the GUI, kept its port and its lease, the session token that could
have proved authority over it was gone, and the host had no pidfd -- yet every
layer above reported a clean stop, because the only thing anyone checked was
that a stop command had been spawned.

So: a spawned command is not a shutdown, exit status zero with nothing said
about the owner is not a shutdown, process exit and port release are two
separate facts, and a missing token, a legacy unfenced receipt and a foreign
host are three separate conditions with three separate operator steps.  Where
a pidfd cannot be opened the stop observes and never signals, and says plainly
what it could not do.

No live SSH and no company host.  Loopback listeners are bounded and local.
"""

from __future__ import annotations

import errno
import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_attempt_resources as RESOURCES  # noqa: E402
import remote_attempt_shutdown as SHUTDOWN  # noqa: E402
import remote_owner as OWNER  # noqa: E402
import remote_process_handle as HANDLE  # noqa: E402
import remote_stop_result as STOP  # noqa: E402
import ssot_connection as SSOT  # noqa: E402
from remote_command_contract import token_hash  # noqa: E402


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OWN_TOKEN = "recovery-owner-token-not-enforced-01"
OTHER_TOKEN = "recovery-other-token-not-enforced-02"
HOST_IDENTITY = "b" * 64
BOOT_IDENTITY = "c" * 64
OTHER_HOST_IDENTITY = "d" * 64
OWNED_PID = 4242
OWNED_START = "start-1"
REQUIRED_REMOTE_PYTHON = "/srv/workstack/venv/bin/python"

SNAPSHOT_OWNER_KEYS = {
    "state",
    "token_available",
    "pidfd_available",
    "process_exit",
    "listener_release",
    "lease_release",
}


# --------------------------------------------------------------------------
# Fixtures


def _write_receipt(data: Path, *, legacy: bool = False, **overrides: object) -> bytes:
    payload: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "data_dir_digest": OWNER.local_path_digest(data),
        "app_dir_digest": "a" * 64,
        "pid": OWNED_PID,
        "start_identity": OWNED_START,
        "release_id": "1.0.13",
        "token_hash": token_hash(OWN_TOKEN),
    }
    if not legacy:
        payload["host_identity"] = HOST_IDENTITY
        payload["boot_identity"] = BOOT_IDENTITY
    payload.update(overrides)
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    (data / OWNER.OWNER_FILENAME).write_bytes(encoded)
    return encoded


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeHandle:
    def __init__(self, controller: "FakeController") -> None:
        self.controller = controller
        self.closes = 0

    def has_exited(self) -> bool | None:
        return self.controller.poll_handle()

    def send_terminate(self) -> None:
        self.controller.terminated.append(OWNED_PID)

    def close(self) -> None:
        self.closes += 1


class FakeController:
    """A controller whose pidfd capability and /proc answers are scripted.

    ``observations`` is consumed one entry per ``observe`` call and the last
    entry repeats, so a test can put the owner alive at classification time and
    gone a poll later without touching a real process.
    """

    def __init__(
        self,
        *,
        observations: tuple[str, ...] = ("live",),
        pidfd: bool | None = True,
        host: str | None = HOST_IDENTITY,
        boot: str | None = BOOT_IDENTITY,
        exit_after_polls: int = 0,
        open_error: OWNER.EntryError | None = None,
    ) -> None:
        self.observations = list(observations)
        self.pidfd = pidfd
        self.host = host
        self.boot = boot
        self.exit_after_polls = exit_after_polls
        self.open_error = open_error
        self.observe_calls: list[tuple[int, str]] = []
        self.opened: list[tuple[int, str]] = []
        self.terminated: list[int] = []
        self.handles: list[FakeHandle] = []
        self._polls = 0

    def current_pid(self) -> int:
        return OWNED_PID

    def start_identity(self, pid: int) -> str | None:
        return OWNED_START if pid == OWNED_PID else None

    def observe(self, pid: int, start_identity: str) -> str:
        self.observe_calls.append((pid, start_identity))
        index = min(len(self.observe_calls) - 1, len(self.observations) - 1)
        return self.observations[index]

    def open_owned_process(self, pid: int, start_identity: str):
        self.opened.append((pid, start_identity))
        if self.open_error is not None:
            raise self.open_error
        handle = FakeHandle(self)
        self.handles.append(handle)
        return handle

    def poll_handle(self) -> bool | None:
        if not self.terminated:
            return False
        self._polls += 1
        return self._polls > self.exit_after_polls

    def pidfd_available(self) -> bool | None:
        return self.pidfd

    def host_identity(self) -> str | None:
        return self.host

    def boot_identity(self) -> str | None:
        return self.boot


def _no_pidfd_error() -> OWNER.EntryError:
    """Exactly what a kernel or interpreter without pidfd signalling raises."""

    return OWNER.EntryError(
        "REMOTE_PROTOCOL_INVALID",
        HANDLE.PIDFD_UNAVAILABLE_DETAIL,
        reason=HANDLE.REASON_PIDFD_UNSUPPORTED,
    )


class OwnerStopOutcomeTest(unittest.TestCase):
    """What the remote side of stop-owned establishes, condition by condition."""

    def tearDown(self) -> None:
        OWNER.set_process_controller(None)

    def _controller(self, **kwargs: object) -> FakeController:
        controller = FakeController(**kwargs)  # type: ignore[arg-type]
        OWNER.set_process_controller(controller)
        return controller

    def _stop(self, data: Path, token: str = OWN_TOKEN, **kwargs: object) -> STOP.StopResult:
        clock = FakeClock()
        wait = OWNER.StopWait(1.0, 0.25, clock.monotonic, clock.sleep)
        return OWNER.stop_owned_result(data, token, wait, **kwargs)  # type: ignore[arg-type]

    def test_a_confirmed_exit_is_the_only_outcome_that_clears_the_receipt(self) -> None:
        controller = self._controller(exit_after_polls=1)
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_receipt(data)
            result = self._stop(data)
            self.assertEqual(result.code, STOP.STOP_CONFIRMED_EXIT)
            self.assertEqual(result.process_exit, "verified")
            self.assertTrue(result.confirmed)
            self.assertEqual(result.state, "dead")
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())
        self.assertEqual(controller.terminated, [OWNED_PID])

    def test_a_stop_that_times_out_reports_a_failed_exit_and_keeps_the_receipt(self) -> None:
        controller = self._controller(exit_after_polls=10**6)
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            original = _write_receipt(data)
            result = self._stop(data)
            self.assertEqual(result.code, STOP.STOP_UNCONFIRMED_TIMEOUT)
            # Signalled, watched through its own handle, still there: that is an
            # observed failure to exit, which is not the same as no evidence.
            self.assertEqual(result.process_exit, "failed")
            self.assertFalse(result.confirmed)
            self.assertEqual(result.state, "stopping")
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        # One SIGTERM to one pid. No escalation, no sweep, no second signal.
        self.assertEqual(controller.terminated, [OWNED_PID])

    def test_a_lost_token_is_refused_as_not_the_owner_and_nothing_is_touched(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            original = _write_receipt(data)
            result = self._stop(data, OTHER_TOKEN)
            self.assertEqual(result.code, STOP.STOP_REFUSED_TOKEN_MISMATCH)
            self.assertIs(result.token_available, False)
            self.assertEqual(result.process_exit, "unknown")
            self.assertTrue(result.refused)
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertEqual(controller.terminated, [])
        # A caller that cannot prove authority never gets to ask about the pid.
        self.assertEqual(controller.opened, [])

    def test_missing_token_legacy_receipt_and_foreign_host_are_three_conditions(self) -> None:
        """Three different facts, three codes, three different operator steps."""

        cases = {
            STOP.STOP_REFUSED_TOKEN_MISMATCH: (OTHER_TOKEN, {}, {}),
            STOP.STOP_REFUSED_UNFENCED_RECEIPT: (OWN_TOKEN, {"legacy": True}, {}),
            STOP.STOP_REFUSED_FOREIGN_HOST: (
                OWN_TOKEN,
                {"host_identity": OTHER_HOST_IDENTITY},
                {},
            ),
        }
        seen: dict[str, str] = {}
        for expected, (token, receipt_kwargs, controller_kwargs) in cases.items():
            with self.subTest(expected=expected):
                controller = self._controller(**controller_kwargs)  # type: ignore[arg-type]
                with tempfile.TemporaryDirectory() as directory:
                    data = Path(directory)
                    original = _write_receipt(data, **receipt_kwargs)  # type: ignore[arg-type]
                    result = self._stop(data, token)
                    self.assertEqual(result.code, expected)
                    self.assertEqual(result.process_exit, "unknown")
                    self.assertEqual(
                        (data / OWNER.OWNER_FILENAME).read_bytes(), original
                    )
                self.assertEqual(controller.terminated, [])
                seen[expected] = result.state
        # And the owner state each one reports is distinct too, so a view is
        # never left choosing one recovery step for three different problems.
        self.assertEqual(len(set(seen.values())), 3)

    def test_a_replaced_pid_is_never_reported_as_an_exit(self) -> None:
        controller = self._controller(observations=("replaced",))
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            original = _write_receipt(data)
            result = self._stop(data)
            self.assertEqual(result.code, STOP.STOP_REFUSED_REPLACED_PID)
            self.assertEqual(result.process_exit, "unknown")
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertEqual(controller.terminated, [])
        self.assertEqual(controller.opened, [])

    def test_ambiguous_liveness_stays_ambiguous(self) -> None:
        controller = self._controller(observations=("unknown",))
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            original = _write_receipt(data)
            result = self._stop(data)
            self.assertEqual(result.code, STOP.STOP_AMBIGUOUS_LIVENESS)
            self.assertEqual(result.state, "unknown")
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertEqual(controller.terminated, [])

    def test_an_owner_already_gone_is_confirmed_without_any_signal(self) -> None:
        controller = self._controller(observations=("exited",))
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_receipt(data)
            result = self._stop(data)
            self.assertEqual(result.code, STOP.STOP_CONFIRMED_ALREADY_EXITED)
            self.assertTrue(result.confirmed)
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())
        self.assertEqual(controller.terminated, [])

    def test_an_absent_receipt_is_its_own_code_and_claims_no_observation(self) -> None:
        self._controller()
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            result = self._stop(data)
            self.assertEqual(result.code, STOP.STOP_RECEIPT_ABSENT)
            # Nothing was watched, so nothing is claimed about a process exit.
            self.assertEqual(result.process_exit, "unknown")

    # -- no pidfd ----------------------------------------------------------

    def test_without_pidfd_the_stop_observes_and_never_signals(self) -> None:
        """The reviewed no-pidfd path: no handle, therefore no signal at all."""

        controller = self._controller(pidfd=False, open_error=_no_pidfd_error())
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            original = _write_receipt(data)
            result = self._stop(data)
            self.assertEqual(result.code, STOP.STOP_UNCONFIRMED_NO_PIDFD)
            self.assertIs(result.pidfd_available, False)
            self.assertIs(result.token_available, True)
            # Watched for its whole budget and still running.
            self.assertEqual(result.process_exit, "failed")
            self.assertEqual(result.state, "live")
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        # The whole point: nothing was signalled, and no pid number was killed
        # as a stand-in for the handle that could not be opened.
        self.assertEqual(controller.terminated, [])

    def test_without_pidfd_the_limit_is_named_in_the_reported_detail(self) -> None:
        self._controller(pidfd=False, open_error=_no_pidfd_error())
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_receipt(data)
            result = self._stop(data)
        self.assertIn("no pidfd", result.detail)
        self.assertIn("PDEATHSIG", result.detail)

    def test_without_pidfd_an_observed_exit_is_still_a_confirmed_stop(self) -> None:
        """Alive when classified, gone a poll later: the confirmation lands."""

        controller = self._controller(
            pidfd=False, open_error=_no_pidfd_error(), observations=("live", "exited")
        )
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_receipt(data)
            result = self._stop(data)
            self.assertEqual(result.code, STOP.STOP_CONFIRMED_WITHOUT_SIGNAL)
            self.assertTrue(result.confirmed)
            self.assertIs(result.pidfd_available, False)
            self.assertFalse((data / OWNER.OWNER_FILENAME).exists())
        self.assertEqual(controller.terminated, [])

    def test_without_pidfd_a_replaced_pid_is_still_refused_not_confirmed(self) -> None:
        controller = self._controller(
            pidfd=False, open_error=_no_pidfd_error(), observations=("live", "replaced")
        )
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            original = _write_receipt(data)
            result = self._stop(data)
            self.assertEqual(result.code, STOP.STOP_REFUSED_REPLACED_PID)
            self.assertEqual(result.process_exit, "unknown")
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertEqual(controller.terminated, [])

    def test_a_permission_refusal_is_not_routed_around_like_a_missing_pidfd(self) -> None:
        """Only the absent mechanism goes to the observation path."""

        controller = self._controller(
            pidfd=True,
            open_error=OWNER.EntryError(
                "REMOTE_PROTOCOL_INVALID",
                "this session cannot pin the owned process",
                reason=HANDLE.REASON_PERMISSION,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            original = _write_receipt(data)
            result = self._stop(data)
            self.assertEqual(result.code, STOP.STOP_UNCONFIRMED_HANDLE)
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertEqual(controller.terminated, [])
        # No fall-through means no second observation loop was entered.
        self.assertEqual(len(controller.observe_calls), 1)

    def test_the_raising_form_keeps_its_contract_over_the_same_decision(self) -> None:
        self._controller(exit_after_polls=10**6)
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_receipt(data)
            clock = FakeClock()
            with self.assertRaises(OWNER.EntryError) as caught:
                OWNER.run_stop_owned(
                    data, OWN_TOKEN, OWNER.StopWait(1.0, 0.25, clock.monotonic, clock.sleep)
                )
            self.assertIn("REMOTE_LOCK_OWNED", str(caught.exception))

    def test_every_outcome_projects_onto_the_six_snapshot_owner_keys(self) -> None:
        for code in STOP.STOP_CODES:
            with self.subTest(code=code):
                facts = STOP.StopResult(code=code).owner_facts()
                self.assertEqual(set(facts), SNAPSHOT_OWNER_KEYS)
                self.assertIn(facts["state"], STOP.OWNER_STATE_VALUES)
                for field in ("process_exit", "listener_release", "lease_release"):
                    self.assertIn(facts[field], STOP.EVIDENCE_VALUES)

    def test_the_owner_module_never_claims_a_listener_or_lease_it_did_not_measure(
        self,
    ) -> None:
        """It opens no port and never touches the writer lease, so it says unknown."""

        self._controller(exit_after_polls=1)
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_receipt(data)
            result = self._stop(data)
        self.assertTrue(result.confirmed)
        self.assertEqual(result.listener_release, "unknown")
        self.assertEqual(result.lease_release, "unknown")


class StopResultBoundaryTest(unittest.TestCase):
    """The desktop reads an untrusted remote, and a launch is not an outcome."""

    def test_exit_zero_without_a_reported_observation_is_not_a_shutdown(self) -> None:
        result = STOP.result_from_launch(returncode=0, stdout=b"", stderr=b"")
        self.assertEqual(result.code, STOP.STOP_RESULT_ABSENT)
        self.assertEqual(result.process_exit, "unknown")
        self.assertFalse(result.confirmed)

    def test_a_nonzero_stop_exit_is_carried_not_discarded(self) -> None:
        result = STOP.result_from_launch(
            returncode=2, stdout=b"", stderr=b"REMOTE_LOCK_OWNED: pid=4242\n"
        )
        self.assertFalse(result.confirmed)
        self.assertIn("REMOTE_LOCK_OWNED", result.detail)

    def test_a_stop_request_timeout_is_its_own_code(self) -> None:
        result = STOP.result_from_launch(returncode=None, timed_out=True)
        self.assertEqual(result.code, STOP.STOP_REQUEST_TIMED_OUT)
        self.assertFalse(result.confirmed)

    def test_a_launch_failure_is_its_own_code(self) -> None:
        result = STOP.result_from_launch(returncode=None, launch_error="ssh is missing")
        self.assertEqual(result.code, STOP.STOP_REQUEST_FAILED)
        self.assertFalse(result.confirmed)

    def test_a_confirmed_payload_round_trips(self) -> None:
        original = STOP.StopResult(
            code=STOP.STOP_CONFIRMED_EXIT,
            state="dead",
            token_available=True,
            pidfd_available=True,
            process_exit="verified",
        )
        decoded = STOP.decode_stop_result(STOP.encode_stop_result(original))
        self.assertEqual(decoded, original)

    def test_a_remote_cannot_invent_a_code_or_an_evidence_value(self) -> None:
        forged = json.dumps(
            {
                "schema_version": STOP.SCHEMA_VERSION,
                "code": "stop_definitely_worked",
                "process_exit": "verified",
            }
        ).encode("utf-8")
        result = STOP.decode_stop_result(forged)
        self.assertEqual(result.code, STOP.STOP_RESULT_UNREADABLE)
        self.assertEqual(result.process_exit, "unknown")

    def test_an_unknown_evidence_value_normalizes_to_unknown(self) -> None:
        forged = json.dumps(
            {
                "schema_version": STOP.SCHEMA_VERSION,
                "code": STOP.STOP_CONFIRMED_EXIT,
                "process_exit": "definitely",
                "listener_release": True,
                "lease_release": ["verified"],
                "state": "excellent",
                "token_available": "yes",
            }
        ).encode("utf-8")
        result = STOP.decode_stop_result(forged)
        self.assertEqual(result.process_exit, "unknown")
        self.assertEqual(result.listener_release, "unknown")
        self.assertEqual(result.lease_release, "unknown")
        self.assertEqual(result.state, "unknown")
        self.assertIsNone(result.token_available)

    def test_untrusted_detail_text_cannot_carry_control_characters(self) -> None:
        forged = json.dumps(
            {
                "schema_version": STOP.SCHEMA_VERSION,
                "code": STOP.STOP_UNCONFIRMED_TIMEOUT,
                "detail": "line\r\nrm -rf /\x00<script> ",
            }
        ).encode("utf-8")
        result = STOP.decode_stop_result(forged)
        for forbidden in ("\r", "\n", "\x00", " "):
            self.assertNotIn(forbidden, result.detail)
        self.assertLessEqual(len(result.detail), STOP.MAX_DETAIL_LENGTH)

    def test_an_oversized_payload_is_refused_rather_than_truncated(self) -> None:
        result = STOP.decode_stop_result(b"x" * (STOP.MAX_RESULT_BYTES + 1))
        self.assertEqual(result.code, STOP.STOP_RESULT_UNREADABLE)

    def test_noise_before_the_payload_is_ignored_and_never_surfaced(self) -> None:
        payload = (
            b"warning: some remote chatter\n"
            + STOP.encode_stop_result(
                STOP.StopResult(
                    code=STOP.STOP_CONFIRMED_EXIT,
                    token_available=True,
                    process_exit="verified",
                )
            )
        )
        result = STOP.decode_stop_result(payload)
        self.assertEqual(result.code, STOP.STOP_CONFIRMED_EXIT)
        self.assertNotIn("chatter", result.detail)

    def test_a_confirmation_never_downgrades_evidence_already_won(self) -> None:
        first = STOP.StopResult(
            code=STOP.STOP_CONFIRMED_EXIT, token_available=True, process_exit="verified"
        )
        weaker = STOP.StopResult(code=STOP.STOP_UNCONFIRMED_NO_PIDFD, process_exit="failed")
        self.assertEqual(STOP.merge_confirmation(first, weaker), first)

    def test_a_confirmation_upgrades_an_unconfirmed_first_pass(self) -> None:
        first = STOP.StopResult(
            code=STOP.STOP_UNCONFIRMED_NO_PIDFD,
            token_available=True,
            pidfd_available=False,
            process_exit="failed",
            listener_release="failed",
        )
        confirmation = STOP.StopResult(
            code=STOP.STOP_CONFIRMED_ALREADY_EXITED,
            state="dead",
            token_available=True,
            process_exit="verified",
        )
        merged = STOP.merge_confirmation(first, confirmation)
        self.assertEqual(merged.code, STOP.STOP_CONFIRMED_ALREADY_EXITED)
        self.assertTrue(merged.confirmed)
        # Facts the first pass established survive the upgrade.
        self.assertIs(merged.pidfd_available, False)
        self.assertEqual(merged.listener_release, "failed")

    def test_a_trace_line_never_says_stopped_without_a_proven_exit(self) -> None:
        line = STOP.summarize(
            STOP.StopResult(code=STOP.STOP_RESULT_ABSENT, detail="stop command exited 0")
        )
        self.assertIn("exit=unknown", line)
        self.assertNotIn("stop_confirmed", line)

    def test_the_exit_status_families_stay_separable(self) -> None:
        self.assertEqual(STOP.exit_code_for(STOP.STOP_CONFIRMED_EXIT), STOP.EXIT_CONFIRMED)
        self.assertEqual(
            STOP.exit_code_for(STOP.STOP_REFUSED_UNFENCED_RECEIPT), STOP.EXIT_REFUSED
        )
        self.assertEqual(
            STOP.exit_code_for(STOP.STOP_UNCONFIRMED_NO_PIDFD), STOP.EXIT_UNCONFIRMED
        )


class _Completed:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _profile(port: int = 18765) -> object:
    return SSOT.RemoteConnectionProfile(
        "work-linux",
        "/srv/workstack/app",
        "/srv/workstack/ssot",
        port,
        WORKSPACE_ID,
        8765,
        REQUIRED_REMOTE_PYTHON,
    )


class RemoteStopRequestTest(unittest.TestCase):
    """The desktop side of one stop request: read the outcome, leak nothing."""

    def test_the_outcome_comes_back_instead_of_being_dropped(self) -> None:
        payload = STOP.encode_stop_result(
            STOP.StopResult(
                code=STOP.STOP_CONFIRMED_EXIT,
                state="dead",
                token_available=True,
                process_exit="verified",
            )
        )
        result = SSOT.run_remote_stop_owned(
            _profile(), "ssh", OWN_TOKEN, runner=lambda _c: _Completed(0, payload)
        )
        self.assertTrue(result.confirmed)

    def test_a_runner_that_reports_nothing_yields_no_observation(self) -> None:
        result = SSOT.run_remote_stop_owned(
            _profile(), "ssh", OWN_TOKEN, runner=mock.Mock()
        )
        self.assertFalse(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_RESULT_ABSENT)

    def test_a_nonzero_remote_exit_reaches_the_caller(self) -> None:
        result = SSOT.run_remote_stop_owned(
            _profile(),
            "ssh",
            OWN_TOKEN,
            runner=lambda _c: _Completed(2, b"", b"REMOTE_LOCK_OWNED: pid=4242\n"),
        )
        self.assertFalse(result.confirmed)
        self.assertIn("REMOTE_LOCK_OWNED", result.detail)

    def test_the_session_token_never_reaches_the_reported_detail(self) -> None:
        """The token is an argv element of this very command; it stays out."""

        echoed = f"failed for --session-token {OWN_TOKEN}\n".encode("utf-8")
        result = SSOT.run_remote_stop_owned(
            _profile(), "ssh", OWN_TOKEN, runner=lambda _c: _Completed(2, b"", echoed)
        )
        self.assertNotIn(OWN_TOKEN, result.detail)
        self.assertNotIn(OWN_TOKEN, STOP.summarize(result))

    def test_the_command_is_still_the_bounded_stop_owned_argv(self) -> None:
        seen: list[list[str]] = []
        SSOT.run_remote_stop_owned(
            _profile(),
            "ssh",
            OWN_TOKEN,
            runner=lambda command: seen.append(command) or _Completed(0),
        )
        remote = seen[0][-1]
        self.assertIn("stop-owned", remote)
        self.assertNotIn("pkill", remote)
        self.assertNotIn("killall", remote)


class ForwardListenerObservationTest(unittest.TestCase):
    """Port release is measured on its own, never inferred from a process exit."""

    def test_a_retained_forward_port_reports_a_failed_listener_release(self) -> None:
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.assertEqual(
            RESOURCES.observe_forward_listener_release(_profile(port)), "failed"
        )

    def test_a_released_forward_port_reports_a_verified_listener_release(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.assertEqual(
            RESOURCES.observe_forward_listener_release(_profile(port)), "verified"
        )

    def test_an_unusable_profile_observes_nothing(self) -> None:
        self.assertEqual(RESOURCES.observe_forward_listener_release(None), "unknown")
        self.assertEqual(
            RESOURCES.observe_forward_listener_release(mock.Mock()), "unknown"
        )


class _StopHost:
    """The smallest host the attempt-resource protocol needs, plus the seams."""

    def __init__(self, *, profile: object = None, confirm: object = None) -> None:
        RESOURCES.initialize_attempt_resources(self)
        self.remote_attempt_id = 0
        self.remote_lifecycle_state = "IDLE"
        self.remote_startup = None
        self.remote_profile = profile
        self.remote_session_token = None
        self.remote_session_token_hash = None
        self.remote_ssh_process = None
        self.remote_ssh_log = None
        self.remote_monitor = None
        self.remote_monitor_attempt_id = ""
        self.remote_ready_attempt_id = 0
        self.remote_stop_outcome = None
        self.stop_requests: list[str] = []
        self.confirm_requests: list[str] = []
        self.traces: list[str] = []
        self._stop_reply: object = None
        self._token_replies: dict[str, object] = {}
        self._confirm_reply = confirm

    # -- seams the protocol calls -----------------------------------------
    def _trace(self, message: str) -> None:
        self.traces.append(str(message))

    def _stop_remote_monitor(self) -> None:
        return None

    def _terminate_owned_process(self, process: object) -> None:
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            terminate()

    def _request_remote_stop_owned(self, profile: object, token: str) -> object:
        self.stop_requests.append(token)
        reply = self._token_replies.get(token, self._stop_reply)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def reply_with(self, result: object) -> None:
        self._stop_reply = result

    def reply_for(self, token: str, result: object) -> None:
        """Answer one captured bundle differently from another.

        A close can release several bundles, each with its own token, and the
        whole point of the aggregation oracles is that they do not answer
        alike.
        """

        self._token_replies[token] = result


class _ConfirmingHost(_StopHost):
    def _confirm_remote_stop_owned(self, profile: object, token: str) -> object:
        self.confirm_requests.append(token)
        return self._confirm_reply


def _bundle(host: _StopHost, token: str, *, process: object = None) -> RESOURCES.RemoteAttemptResources:
    return RESOURCES.RemoteAttemptResources(1, host.remote_profile, token, process, None)


class DesktopStopOutcomeTest(unittest.TestCase):
    """The return code of the stop command is the point, so it is kept."""

    def _released_port(self) -> int:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def test_a_confirmed_remote_outcome_is_recorded_on_the_host(self) -> None:
        host = _StopHost(profile=_profile(self._released_port()))
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_CONFIRMED_EXIT,
                state="dead",
                token_available=True,
                process_exit="verified",
            )
        )
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertTrue(result.confirmed)
        self.assertIs(host.remote_stop_outcome, result)
        self.assertEqual(host.stop_requests, ["owned-token"])

    def test_a_host_that_reports_nothing_leaves_the_outcome_unknown(self) -> None:
        """The 1.0.13 shape: the stop command ran and its return was dropped."""

        host = _StopHost(profile=_profile(self._released_port()))
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertEqual(result.code, STOP.STOP_RESULT_ABSENT)
        self.assertEqual(result.process_exit, "unknown")
        self.assertFalse(result.confirmed)
        self.assertTrue(result.requested)

    def test_a_nonzero_stop_exit_is_visible_to_the_host(self) -> None:
        host = _StopHost(profile=_profile(self._released_port()))
        host.reply_with(
            STOP.result_from_launch(returncode=3, stderr=b"REMOTE_LOCK_OWNED: pid=4242\n")
        )
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertFalse(result.confirmed)
        self.assertIn("REMOTE_LOCK_OWNED", host.remote_stop_outcome.detail)

    def test_a_completed_process_from_the_host_is_normalized_not_ignored(self) -> None:
        """The seam the coordinator wires hands back the raw completed process."""

        payload = STOP.encode_stop_result(
            STOP.StopResult(
                code=STOP.STOP_CONFIRMED_EXIT,
                state="dead",
                token_available=True,
                process_exit="verified",
            )
        )
        host = _StopHost(profile=_profile(self._released_port()))
        host.reply_with(_Completed(0, payload))
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertTrue(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_CONFIRMED_EXIT)

    def test_a_completed_process_exiting_zero_alone_confirms_nothing(self) -> None:
        """rc=0 is about the command. The owner, the port and the lease are not."""

        host = _StopHost(profile=_profile(self._released_port()))
        host.reply_with(_Completed(0, b"", b""))
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertFalse(result.confirmed)
        self.assertEqual(result.process_exit, "unknown")
        self.assertEqual(result.lease_release, "unknown")
        self.assertEqual(result.code, STOP.STOP_RESULT_ABSENT)

    def test_a_completed_process_nonzero_exit_carries_its_reported_reason(self) -> None:
        host = _StopHost(profile=_profile(self._released_port()))
        host.reply_with(_Completed(3, b"", b"REMOTE_LOCK_OWNED: pid=4242\n"))
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertFalse(result.confirmed)
        self.assertIn("REMOTE_LOCK_OWNED", result.detail)

    def test_a_completed_process_cannot_echo_the_token_into_the_outcome(self) -> None:
        host = _StopHost(profile=_profile(self._released_port()))
        host.reply_with(
            _Completed(2, b"", f"refused --session-token {OWN_TOKEN}\n".encode("utf-8"))
        )
        result = RESOURCES.release_captured(host, _bundle(host, OWN_TOKEN))
        self.assertNotIn(OWN_TOKEN, result.detail)
        self.assertNotIn(OWN_TOKEN, " ".join(host.traces))

    def test_a_nonconforming_runner_result_normalizes_to_unknown(self) -> None:
        host = _StopHost(profile=_profile(self._released_port()))
        host.reply_with(mock.Mock())
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertEqual(result.code, STOP.STOP_RESULT_ABSENT)
        self.assertEqual(result.process_exit, "unknown")

    def test_a_stop_request_that_raises_is_a_failed_request_not_a_stop(self) -> None:
        host = _StopHost(profile=_profile(self._released_port()))
        host.reply_with(OSError("ssh is gone"))
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertEqual(result.code, STOP.STOP_REQUEST_FAILED)
        self.assertFalse(result.confirmed)

    def test_a_bundle_with_no_token_reports_that_no_stop_was_requested(self) -> None:
        """The lost-token shape: nothing to authorize a stop with, and it says so."""

        host = _StopHost(profile=_profile(self._released_port()))
        result = RESOURCES.release_captured(host, _bundle(host, ""))
        self.assertEqual(result.code, STOP.STOP_NOT_REQUESTED_NO_TOKEN)
        self.assertIs(result.token_available, False)
        self.assertFalse(result.requested)
        self.assertEqual(host.stop_requests, [])

    def test_a_bundle_with_no_profile_is_a_separate_fact(self) -> None:
        host = _StopHost(profile=None)
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertEqual(result.code, STOP.STOP_NOT_REQUESTED_NO_PROFILE)
        self.assertEqual(host.stop_requests, [])

    def test_a_retained_port_is_reported_beside_an_unconfirmed_exit(self) -> None:
        """Exactly the incident: the stop was not confirmed and the port is held.

        The port here is the desktop's own forward, so it is reported as
        ``forward_listener_release``.  The contract's original-listener field
        stays unknown: this lane never looks at the listener the remote owner
        published, and the incident is precisely a freed forward beside a
        surviving original listener.
        """

        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        host = _StopHost(profile=_profile(port))
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_UNCONFIRMED_NO_PIDFD,
                state="live",
                token_available=True,
                pidfd_available=False,
                process_exit="failed",
            )
        )
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertEqual(result.process_exit, "failed")
        self.assertEqual(result.forward_listener_release, "failed")
        self.assertEqual(result.listener_release, "unknown")
        self.assertIs(result.pidfd_available, False)

    def test_the_port_observation_is_taken_apart_from_the_exit_evidence(self) -> None:
        host = _StopHost(profile=_profile(self._released_port()))
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_UNCONFIRMED_TIMEOUT, state="stopping", process_exit="failed"
            )
        )
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        # A free forward port does not turn a failed exit into a successful
        # stop, and it is not the original listener either.
        self.assertEqual(result.forward_listener_release, "verified")
        self.assertEqual(result.listener_release, "unknown")
        self.assertEqual(result.process_exit, "failed")
        self.assertFalse(result.confirmed)

    def test_the_recorded_trace_never_leaks_the_session_token(self) -> None:
        host = _StopHost(profile=_profile(self._released_port()))
        host.reply_with(STOP.result_from_launch(returncode=2, stderr=b"refused\n"))
        RESOURCES.release_captured(host, _bundle(host, OWN_TOKEN))
        self.assertNotIn(OWN_TOKEN, " ".join(host.traces))

    # -- the confirmation pass --------------------------------------------

    def test_the_confirmation_pass_is_skipped_when_the_host_has_no_seam(self) -> None:
        host = _StopHost(profile=_profile(self._released_port()))
        host.reply_with(
            STOP.StopResult(code=STOP.STOP_UNCONFIRMED_NO_PIDFD, process_exit="failed")
        )
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertEqual(result.code, STOP.STOP_UNCONFIRMED_NO_PIDFD)
        self.assertEqual(host.stop_requests, ["owned-token"])

    def test_the_confirmation_pass_completes_a_no_pidfd_shutdown(self) -> None:
        """After the channel closed, PDEATHSIG has fired and the owner is gone."""

        host = _ConfirmingHost(
            profile=_profile(self._released_port()),
            confirm=STOP.StopResult(
                code=STOP.STOP_CONFIRMED_ALREADY_EXITED,
                state="dead",
                token_available=True,
                process_exit="verified",
            ),
        )
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_UNCONFIRMED_NO_PIDFD,
                token_available=True,
                pidfd_available=False,
                process_exit="failed",
            )
        )
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertTrue(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_CONFIRMED_ALREADY_EXITED)
        self.assertIs(result.pidfd_available, False)
        self.assertEqual(host.confirm_requests, ["owned-token"])

    def test_the_confirmation_pass_runs_after_the_channel_is_closed(self) -> None:
        order: list[str] = []

        class _Process:
            """A captured SSH process that really goes when it is terminated.

            The confirmation is coordinated with the reap and not with the
            terminate call returning, so a double that never exits is a
            channel that is still open, not a closed one.
            """

            pid = 77

            def __init__(self) -> None:
                self._code: object = None

            def poll(self) -> object:
                return self._code

            def terminate(self) -> None:
                order.append("ssh-terminate")
                self._code = 0

            def wait(self, timeout: float) -> object:
                return self._code

        host = _ConfirmingHost(
            profile=_profile(self._released_port()),
            confirm=STOP.StopResult(
                code=STOP.STOP_CONFIRMED_ALREADY_EXITED,
                token_available=True,
                process_exit="verified",
            ),
        )
        host.reply_with(STOP.StopResult(code=STOP.STOP_UNCONFIRMED_NO_PIDFD))
        original = host._confirm_remote_stop_owned

        def _record(profile: object, token: str) -> object:
            order.append("confirm")
            return original(profile, token)

        host._confirm_remote_stop_owned = _record  # type: ignore[method-assign]
        RESOURCES.release_captured(host, _bundle(host, "owned-token", process=_Process()))
        self.assertEqual(order, ["ssh-terminate", "confirm"])

    def test_a_confirmed_first_pass_never_opens_a_second_connection(self) -> None:
        host = _ConfirmingHost(profile=_profile(self._released_port()))
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_CONFIRMED_EXIT,
                token_available=True,
                process_exit="verified",
            )
        )
        RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertEqual(host.confirm_requests, [])

    def test_the_confirmation_pass_runs_at_most_once_per_attempt(self) -> None:
        host = _ConfirmingHost(
            profile=_profile(self._released_port()),
            confirm=STOP.StopResult(code=STOP.STOP_UNCONFIRMED_NO_PIDFD),
        )
        host.reply_with(STOP.StopResult(code=STOP.STOP_UNCONFIRMED_NO_PIDFD))
        bundle = _bundle(host, "owned-token")
        RESOURCES.release_captured(host, bundle)
        RESOURCES.release_captured(host, bundle)
        self.assertEqual(host.stop_requests, ["owned-token"])
        self.assertEqual(host.confirm_requests, ["owned-token"])

    def test_a_failing_confirmation_leaves_the_first_outcome_standing(self) -> None:
        host = _ConfirmingHost(profile=_profile(self._released_port()))
        host._confirm_reply = None

        def _boom(profile: object, token: str) -> object:
            raise OSError("no route to host")

        host._confirm_remote_stop_owned = _boom  # type: ignore[method-assign]
        host.reply_with(
            STOP.StopResult(code=STOP.STOP_UNCONFIRMED_NO_PIDFD, process_exit="failed")
        )
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        self.assertEqual(result.code, STOP.STOP_UNCONFIRMED_NO_PIDFD)
        self.assertFalse(result.confirmed)

    def test_a_confirmation_cannot_be_assembled_into_a_contradiction(self) -> None:
        """The merge is checked too, because it composes two admitted halves.

        Each pass is consistent on its own, and the merge deliberately carries
        forward what the first pass established -- here the pidfd capability a
        signalled, watched, unconfirmed first pass measured. Composed, that would be a stop confirmed without any signal
        wearing a pidfd, which no host emits and which the predicate refuses
        anywhere else. A confirmation that can only be believed by
        contradicting the first pass has proved nothing, so the honest
        unconfirmed outcome stands.
        """

        host = _ConfirmingHost(
            profile=_profile(self._released_port()),
            confirm=STOP.StopResult(
                code=STOP.STOP_CONFIRMED_WITHOUT_SIGNAL,
                state="dead",
                token_available=True,
                process_exit="verified",
            ),
        )
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_UNCONFIRMED_TIMEOUT,
                state="stopping",
                token_available=True,
                pidfd_available=True,
                process_exit="failed",
            )
        )

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertFalse(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_UNCONFIRMED_TIMEOUT)
        self.assertIs(result.pidfd_available, True)
        self.assertEqual(host.confirm_requests, ["owned-token"])
        self.assertEqual(STOP.report_contradiction(result), "")

    def test_a_consistent_confirmation_still_upgrades_the_first_pass(self) -> None:
        """The guard above must not cost the confirmation pass it protects."""

        host = _ConfirmingHost(
            profile=_profile(self._released_port()),
            confirm=STOP.StopResult(
                code=STOP.STOP_CONFIRMED_WITHOUT_SIGNAL,
                state="dead",
                token_available=True,
                pidfd_available=False,
                process_exit="verified",
            ),
        )
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_UNCONFIRMED_NO_PIDFD,
                state="live",
                token_available=True,
                pidfd_available=False,
                process_exit="failed",
            )
        )

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertTrue(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_CONFIRMED_WITHOUT_SIGNAL)
        self.assertIs(result.pidfd_available, False)


class CloseReopenCloseTest(unittest.TestCase):
    """GUI close, reopen, close again: each attempt answers for itself."""

    def _host(self) -> _StopHost:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return _StopHost(profile=_profile(port))

    def _install(self, host: _StopHost, token: str) -> None:
        start = RESOURCES.start_remote_attempt(host)
        resources = RESOURCES.RemoteAttemptResources(
            start.generation, host.remote_profile, token, None, None, start.claim
        )
        with RESOURCES.resource_lock_for(host):
            RESOURCES.gate_for(host).install_locked(resources)
            host.remote_session_token = token
            # An empty token is the lost-token state itself; there is nothing
            # to hash, and the desktop is left holding no proof of authority.
            host.remote_session_token_hash = token_hash(token) if token else None

    def test_each_close_stops_only_its_own_token_and_reports_its_own_outcome(self) -> None:
        host = self._host()
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_CONFIRMED_EXIT,
                token_available=True,
                process_exit="verified",
            )
        )
        self._install(host, "first-token")
        first = RESOURCES.stop_owned_connection(host)
        self.assertTrue(first.confirmed)

        # Reopen: a brand new attempt, with a token the first close never saw.
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_UNCONFIRMED_TIMEOUT, state="stopping", process_exit="failed"
            )
        )
        self._install(host, "second-token")
        second = RESOURCES.stop_owned_connection(host)
        self.assertFalse(second.confirmed)
        self.assertEqual(second.code, STOP.STOP_UNCONFIRMED_TIMEOUT)

        self.assertEqual(host.stop_requests, ["first-token", "second-token"])
        # The second close's outcome is what the host now reports; a stale
        # success from the first attempt never stands in for it.
        self.assertIs(host.remote_stop_outcome, second)

    def test_a_second_close_of_a_finished_attempt_asks_nothing_again(self) -> None:
        host = self._host()
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_CONFIRMED_EXIT,
                token_available=True,
                process_exit="verified",
            )
        )
        self._install(host, "only-token")
        RESOURCES.stop_owned_connection(host)
        second = RESOURCES.stop_owned_connection(host)
        self.assertEqual(host.stop_requests, ["only-token"])
        # Nothing was installed the second time, so there is nothing to report.
        self.assertIsNone(second)

    def test_a_reopen_after_a_lost_token_still_separates_the_two_facts(self) -> None:
        host = self._host()
        self._install(host, "")
        lost = RESOURCES.stop_owned_connection(host)
        self.assertEqual(lost.code, STOP.STOP_NOT_REQUESTED_NO_TOKEN)
        self.assertEqual(host.stop_requests, [])

        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_CONFIRMED_EXIT,
                token_available=True,
                process_exit="verified",
            )
        )
        self._install(host, "recovered-token")
        after = RESOURCES.stop_owned_connection(host)
        self.assertTrue(after.confirmed)
        self.assertEqual(host.stop_requests, ["recovered-token"])

    def test_concurrent_closes_still_issue_exactly_one_stop_request(self) -> None:
        host = self._host()
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_CONFIRMED_EXIT,
                token_available=True,
                process_exit="verified",
            )
        )
        self._install(host, "shared-token")
        barrier = threading.Barrier(4)

        def _close() -> None:
            barrier.wait(timeout=10)
            RESOURCES.stop_owned_connection(host)

        threads = [threading.Thread(target=_close) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(host.stop_requests, ["shared-token"])


# --------------------------------------------------------------------------
# Independent-review corrections A-R1 .. A-R6
#
# Every case below enters through a reachable seam -- the host's
# ``_request_remote_stop_owned``, ``release_captured``, ``cleanup_captured``,
# ``stop_owned_connection``, ``start_remote_attempt``, or the remote-side
# ``stop_owned_result`` over a real receipt file -- and never by calling the
# helper that was repaired. A helper can be made to say the right string; only
# the path a close actually takes can be made to stop claiming a shutdown.


def _confirmed_exit() -> STOP.StopResult:
    return STOP.StopResult(
        code=STOP.STOP_CONFIRMED_EXIT,
        state="dead",
        token_available=True,
        process_exit="verified",
    )


def _timed_out_exit() -> STOP.StopResult:
    return STOP.StopResult(
        code=STOP.STOP_UNCONFIRMED_TIMEOUT,
        state="stopping",
        token_available=True,
        process_exit="failed",
    )


def _unfenced_refusal() -> STOP.StopResult:
    return STOP.StopResult(
        code=STOP.STOP_REFUSED_UNFENCED_RECEIPT,
        state="unfenced",
        token_available=True,
    )


def _free_port() -> int:
    """A loopback port with nothing on it, for the forward observation."""

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _OtherProcess:
    """An in-flight captured SSH process that is not the installed one."""

    def __init__(self) -> None:
        self.pid = 5150
        self.terminate_calls = 0

    def poll(self) -> object:
        return None

    def terminate(self) -> None:
        self.terminate_calls += 1


class _DeniedSocket:
    """A probe socket whose bind refuses with one chosen errno."""

    def __init__(self, code: int = errno.EACCES) -> None:
        self.code = code

    def __enter__(self) -> "_DeniedSocket":
        return self

    def __exit__(self, *_exception: object) -> bool:
        return False

    def bind(self, _address: object) -> None:
        raise OSError(self.code, "refused")


class AggregateEveryCapturedBundleTest(unittest.TestCase):
    """A-R1: one confirmed bundle never speaks for one that is unresolved.

    ``close_resource_sets`` deliberately returns the installed bundle plus an
    in-flight one caught before publication. Each is a real owner with its own
    token, SSH process and port, so a close has stopped the connection only
    when every one of them proved an exit. Reporting the most successful
    outcome is the 1.0.8 claim in a new place: one stop read as the shutdown of
    everything captured.
    """

    def _host(self) -> _StopHost:
        return _StopHost(profile=_profile(_free_port()))

    def _stage_two_owners(self, host: _StopHost, installed: str, inflight: str) -> None:
        """An old installed attempt beside a newer in-flight one.

        This is the shape the review's counterexample used and the shape a
        reopen produces: attempt one is still on the gate while attempt two has
        already published its own token and its own cleanup claim, so the close
        really does have two owners to answer for.
        """

        first = RESOURCES.start_remote_attempt(host)
        resources = RESOURCES.RemoteAttemptResources(
            first.generation, host.remote_profile, installed, None, None, first.claim
        )
        with RESOURCES.resource_lock_for(host):
            RESOURCES.gate_for(host).install_locked(resources)
        RESOURCES.start_remote_attempt(host)
        host.remote_session_token = inflight
        host.remote_session_token_hash = token_hash(inflight)

    def test_an_old_confirmed_owner_cannot_mask_a_new_unconfirmed_one(self) -> None:
        """The exact counterexample: two bundles, one confirms, one times out."""

        host = self._host()
        host.reply_for("old-token", _confirmed_exit())
        host.reply_for("new-token", _timed_out_exit())
        self._stage_two_owners(host, "old-token", "new-token")

        outcome = RESOURCES.stop_owned_connection(host)

        self.assertEqual(host.stop_requests, ["old-token", "new-token"])
        self.assertFalse(outcome.confirmed)
        self.assertEqual(outcome.code, STOP.STOP_UNCONFIRMED_TIMEOUT)
        self.assertEqual(outcome.process_exit, "failed")
        # And the host reports the same conservative answer, not the success.
        self.assertIs(host.remote_stop_outcome, outcome)

    def test_a_refused_bundle_also_prevents_a_confirmed_close(self) -> None:
        host = self._host()
        host.reply_for("old-token", _confirmed_exit())
        host.reply_for("new-token", _unfenced_refusal())
        self._stage_two_owners(host, "old-token", "new-token")

        outcome = RESOURCES.stop_owned_connection(host)

        self.assertFalse(outcome.confirmed)
        self.assertEqual(outcome.code, STOP.STOP_REFUSED_UNFENCED_RECEIPT)
        self.assertTrue(outcome.refused)

    def test_a_captured_bundle_with_no_token_prevents_a_confirmed_close(self) -> None:
        """The 1.0.8 shape: a captured owner nobody could even ask to stop."""

        host = self._host()
        host.reply_with(_confirmed_exit())
        stranded = _OtherProcess()
        start = RESOURCES.start_remote_attempt(host)
        resources = RESOURCES.RemoteAttemptResources(
            start.generation, host.remote_profile, "owned-token", None, None, start.claim
        )
        with RESOURCES.resource_lock_for(host):
            RESOURCES.gate_for(host).install_locked(resources)
            host.remote_session_token = "owned-token"
            host.remote_session_token_hash = token_hash("owned-token")
            host.remote_ssh_process = stranded

        outcome = RESOURCES.stop_owned_connection(host)

        self.assertEqual(host.stop_requests, ["owned-token"])
        self.assertFalse(outcome.confirmed)
        self.assertEqual(outcome.code, STOP.STOP_NOT_REQUESTED_NO_TOKEN)
        # The stranded SSH process was still released; only the claim about
        # its owner is withheld.
        self.assertEqual(stranded.terminate_calls, 1)

    def test_a_close_whose_every_bundle_proved_an_exit_is_still_confirmed(self) -> None:
        """The conservative rule must not make a real shutdown unsayable."""

        host = self._host()
        host.reply_for("old-token", _confirmed_exit())
        host.reply_for("new-token", _confirmed_exit())
        self._stage_two_owners(host, "old-token", "new-token")

        outcome = RESOURCES.stop_owned_connection(host)

        self.assertEqual(host.stop_requests, ["old-token", "new-token"])
        self.assertTrue(outcome.confirmed)
        self.assertEqual(outcome.process_exit, "verified")

    def test_the_aggregate_is_never_a_confirmed_code_stripped_of_its_evidence(
        self,
    ) -> None:
        """A clamped confirmation would itself be internally contradictory."""

        host = self._host()
        host.reply_for("old-token", _confirmed_exit())
        host.reply_for("new-token", _timed_out_exit())
        self._stage_two_owners(host, "old-token", "new-token")

        outcome = RESOURCES.stop_owned_connection(host)

        self.assertNotIn(outcome.code, STOP.CONFIRMED_CODES)
        self.assertEqual(STOP.report_contradiction(outcome), "")

    def test_the_close_traces_how_many_bundles_actually_proved_an_exit(self) -> None:
        host = self._host()
        host.reply_for("old-token", _confirmed_exit())
        host.reply_for("new-token", _timed_out_exit())
        self._stage_two_owners(host, "old-token", "new-token")

        RESOURCES.stop_owned_connection(host)

        self.assertTrue(
            any("released 2 captured bundles, 1 with a proven exit" in line
                for line in host.traces),
            host.traces,
        )


class GenerationGatedOutcomeTest(unittest.TestCase):
    """A-R2: an outcome belongs to the attempt that earned it.

    The stale-cleanup wait runs outside every lock on purpose, so a
    generation-1 cleanup can land long after generation 2 started. Without an
    attempt identity on the publication it overwrites the newer result, and a
    reopened connection keeps projecting an owner that is already gone.
    """

    def _host(self) -> _StopHost:
        return _StopHost(profile=_profile(_free_port()))

    def _bundle_for(self, host: _StopHost, start: object, token: str) -> object:
        return RESOURCES.RemoteAttemptResources(
            start.generation, host.remote_profile, token, None, None, start.claim
        )

    def test_a_stale_cleanup_cannot_overwrite_a_newer_attempts_result(self) -> None:
        host = self._host()
        host.reply_for("first-token", _confirmed_exit())
        host.reply_for("second-token", _timed_out_exit())

        first = RESOURCES.start_remote_attempt(host)
        stale_bundle = self._bundle_for(host, first, "first-token")
        second = RESOURCES.start_remote_attempt(host)
        self.assertGreater(second.generation, first.generation)
        newer = RESOURCES.release_captured(
            host, self._bundle_for(host, second, "second-token")
        )
        self.assertIs(host.remote_stop_outcome, newer)

        # Attempt one's cleanup finally finishes, through the same reachable
        # path wait_until_ready takes for a stale attempt.
        RESOURCES.cleanup_captured(host, stale_bundle, RESOURCES.CLEANUP_STALE)

        self.assertEqual(host.stop_requests, ["second-token", "first-token"])
        self.assertIs(host.remote_stop_outcome, newer)
        self.assertFalse(host.remote_stop_outcome.confirmed)
        self.assertTrue(
            any("not published" in line for line in host.traces), host.traces
        )

    def test_reopening_clears_the_previous_owners_outcome(self) -> None:
        host = self._host()
        host.reply_with(_confirmed_exit())
        first = RESOURCES.start_remote_attempt(host)
        RESOURCES.release_captured(host, self._bundle_for(host, first, "first-token"))
        self.assertTrue(host.remote_stop_outcome.confirmed)

        second = RESOURCES.start_remote_attempt(host)

        # The new owner is starting and nothing here has observed it, so the
        # snapshot must not still be told the owner is dead with a proven exit.
        self.assertIsNone(host.remote_stop_outcome)
        self.assertEqual(host.remote_stop_outcome_generation, second.generation)

    def test_a_close_then_reopen_does_not_carry_the_dead_owner_forward(self) -> None:
        host = self._host()
        host.reply_with(_confirmed_exit())
        start = RESOURCES.start_remote_attempt(host)
        with RESOURCES.resource_lock_for(host):
            RESOURCES.gate_for(host).install_locked(
                self._bundle_for(host, start, "closed-token")
            )
            host.remote_session_token = "closed-token"
            host.remote_session_token_hash = token_hash("closed-token")
        closed = RESOURCES.stop_owned_connection(host)
        self.assertTrue(closed.confirmed)

        RESOURCES.start_remote_attempt(host)

        self.assertIsNone(host.remote_stop_outcome)

    def test_the_same_generation_may_still_republish_its_own_outcome(self) -> None:
        """Two releases of one bundle are idempotent, not a stale overwrite."""

        host = self._host()
        host.reply_with(_timed_out_exit())
        start = RESOURCES.start_remote_attempt(host)
        bundle = self._bundle_for(host, start, "owned-token")
        RESOURCES.release_captured(host, bundle)
        second = RESOURCES.release_captured(host, bundle)
        self.assertIs(host.remote_stop_outcome, second)
        self.assertEqual(host.stop_requests, ["owned-token"])

    def test_a_stale_cleanup_still_returns_its_own_honest_outcome(self) -> None:
        """Withholding the publication does not falsify what the stale attempt saw."""

        host = self._host()
        host.reply_for("stale-token", _timed_out_exit())
        first = RESOURCES.start_remote_attempt(host)
        stale_bundle = self._bundle_for(host, first, "stale-token")
        RESOURCES.start_remote_attempt(host)

        result = RESOURCES.release_captured(host, stale_bundle)

        self.assertEqual(result.code, STOP.STOP_UNCONFIRMED_TIMEOUT)
        self.assertIsNone(host.remote_stop_outcome)


class ReportedOutcomeReconciliationTest(unittest.TestCase):
    """A-R3: a payload overrules neither the exit status nor itself.

    The discarded return code was the packet's central defect. Decoding stdout
    first and returning it throws the status away whenever the payload happens
    to parse, and allowlisting each field on its own admits combinations no
    honest stop can produce.
    """

    def _host(self) -> _StopHost:
        return _StopHost(profile=_profile(_free_port()))

    def test_a_nonzero_exit_with_a_confirmed_payload_never_confirms(self) -> None:
        host = self._host()
        host.reply_with(_Completed(3, STOP.encode_stop_result(_confirmed_exit())))

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertFalse(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_RESULT_UNREADABLE)
        self.assertEqual(result.process_exit, "unknown")
        self.assertEqual(result.state, "unknown")
        self.assertIn("exited 3", result.detail)

    def test_a_payload_contradicting_itself_never_confirms(self) -> None:
        """An unconfirmed code beside a proven exit is not a stop."""

        host = self._host()
        forged = STOP.StopResult(
            code=STOP.STOP_UNCONFIRMED_NO_PIDFD, state="live", process_exit="verified"
        )
        host.reply_with(
            _Completed(STOP.EXIT_UNCONFIRMED, STOP.encode_stop_result(forged))
        )

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertFalse(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_RESULT_UNREADABLE)
        self.assertIn("inconsistent", result.detail)

    def test_a_confirmed_code_without_a_proven_exit_never_confirms(self) -> None:
        host = self._host()
        forged = STOP.StopResult(code=STOP.STOP_CONFIRMED_EXIT, process_exit="failed")
        host.reply_with(
            _Completed(STOP.EXIT_CONFIRMED, STOP.encode_stop_result(forged))
        )

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertFalse(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_RESULT_UNREADABLE)

    def test_an_already_decoded_host_reply_is_checked_the_same_way(self) -> None:
        """The seam may hand back a StopResult; it gets no free pass."""

        host = self._host()
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_REFUSED_FOREIGN_HOST, state="dead", token_available=True
            )
        )

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertEqual(result.code, STOP.STOP_RESULT_UNREADABLE)
        self.assertFalse(result.confirmed)

    def test_an_agreeing_refusal_payload_is_still_read_as_that_refusal(self) -> None:
        """Reconciliation is agreement, not a blanket refusal of nonzero exits."""

        host = self._host()
        host.reply_with(
            _Completed(STOP.EXIT_REFUSED, STOP.encode_stop_result(_unfenced_refusal()))
        )

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertEqual(result.code, STOP.STOP_REFUSED_UNFENCED_RECEIPT)
        self.assertTrue(result.refused)
        self.assertFalse(result.confirmed)

    def test_an_agreeing_confirmed_payload_is_still_a_confirmed_stop(self) -> None:
        host = self._host()
        host.reply_with(
            _Completed(STOP.EXIT_CONFIRMED, STOP.encode_stop_result(_confirmed_exit()))
        )

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertTrue(result.confirmed)
        self.assertEqual(result.code, STOP.STOP_CONFIRMED_EXIT)

    def test_a_contradiction_detail_never_carries_the_session_token(self) -> None:
        host = self._host()
        host.reply_with(_Completed(3, STOP.encode_stop_result(_confirmed_exit())))
        result = RESOURCES.release_captured(host, _bundle(host, OWN_TOKEN))
        self.assertNotIn(OWN_TOKEN, result.detail)
        self.assertNotIn(OWN_TOKEN, " ".join(host.traces))


class PropagatedTimeoutTest(unittest.TestCase):
    """A-R4: a propagated TimeoutExpired is a timeout, not a launch failure.

    Clarification 1 has the host seam propagate ``TimeoutExpired`` rather than
    swallowing it. ``TimeoutExpired`` is a ``SubprocessError``, so catching the
    base class first collapsed the clarified timeout into
    ``stop_request_failed`` and the primary close path could never report it.
    """

    def _host(self) -> _StopHost:
        return _StopHost(profile=_profile(_free_port()))

    def test_a_timeout_from_the_host_seam_is_its_own_code(self) -> None:
        host = self._host()
        host.reply_with(subprocess.TimeoutExpired(cmd=["ssh"], timeout=8.0))

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertEqual(result.code, STOP.STOP_REQUEST_TIMED_OUT)
        self.assertFalse(result.confirmed)
        self.assertIs(result.token_available, True)
        self.assertEqual(host.stop_requests, ["owned-token"])
        self.assertTrue(any("timed out" in line for line in host.traces), host.traces)

    def test_a_launch_failure_stays_a_different_code(self) -> None:
        host = self._host()
        host.reply_with(OSError("ssh is missing"))

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertEqual(result.code, STOP.STOP_REQUEST_FAILED)
        self.assertFalse(result.confirmed)

    def test_another_subprocess_error_stays_a_launch_failure(self) -> None:
        host = self._host()
        host.reply_with(subprocess.SubprocessError("broken pipe"))

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertEqual(result.code, STOP.STOP_REQUEST_FAILED)

    def test_a_timeout_never_carries_the_argv_or_the_token(self) -> None:
        host = self._host()
        host.reply_with(
            subprocess.TimeoutExpired(
                cmd=["ssh", "--session-token", OWN_TOKEN], timeout=8.0
            )
        )
        result = RESOURCES.release_captured(host, _bundle(host, OWN_TOKEN))
        self.assertNotIn(OWN_TOKEN, result.detail)
        self.assertNotIn(OWN_TOKEN, " ".join(host.traces))

    def test_a_timed_out_close_is_not_a_confirmed_close(self) -> None:
        host = self._host()
        host.reply_with(subprocess.TimeoutExpired(cmd=["ssh"], timeout=8.0))
        start = RESOURCES.start_remote_attempt(host)
        with RESOURCES.resource_lock_for(host):
            RESOURCES.gate_for(host).install_locked(
                RESOURCES.RemoteAttemptResources(
                    start.generation,
                    host.remote_profile,
                    "owned-token",
                    None,
                    None,
                    start.claim,
                )
            )
            host.remote_session_token = "owned-token"
            host.remote_session_token_hash = token_hash("owned-token")

        outcome = RESOURCES.stop_owned_connection(host)

        self.assertEqual(outcome.code, STOP.STOP_REQUEST_TIMED_OUT)
        self.assertFalse(outcome.confirmed)

    def test_a_confirmation_pass_timeout_leaves_the_first_outcome_standing(self) -> None:
        host = _StopHost(profile=_profile(_free_port()))
        host.reply_with(
            STOP.StopResult(
                code=STOP.STOP_UNCONFIRMED_NO_PIDFD,
                token_available=True,
                pidfd_available=False,
                process_exit="failed",
            )
        )
        asked: list[str] = []

        def _confirm(profile: object, token: str) -> object:
            asked.append(token)
            raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=3.0)

        host._confirm_remote_stop_owned = _confirm

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertEqual(result.code, STOP.STOP_UNCONFIRMED_NO_PIDFD)
        self.assertFalse(result.confirmed)
        self.assertEqual(asked, ["owned-token"])
        self.assertTrue(
            any("confirmation pass timed out" in line for line in host.traces),
            host.traces,
        )


class UnobservedOwnerStateTest(unittest.TestCase):
    """A-R5: absent and token-mismatch prove neither dead nor live.

    Both land before anything looks at the recorded process, so neither may
    name an owner state, and an absent receipt is not a successful stop with
    exit status 0.
    """

    def tearDown(self) -> None:
        OWNER.set_process_controller(None)

    def _controller(self, **kwargs: object) -> FakeController:
        controller = FakeController(**kwargs)  # type: ignore[arg-type]
        OWNER.set_process_controller(controller)
        return controller

    def _stop(self, data: Path, token: str = OWN_TOKEN) -> STOP.StopResult:
        clock = FakeClock()
        wait = OWNER.StopWait(1.0, 0.25, clock.monotonic, clock.sleep)
        return OWNER.stop_owned_result(data, token, wait)

    def test_an_absent_receipt_names_no_owner_state_at_all(self) -> None:
        self._controller()
        with tempfile.TemporaryDirectory() as directory:
            result = self._stop(Path(directory))
        self.assertEqual(result.code, STOP.STOP_RECEIPT_ABSENT)
        # An owner that exited and cleaned up, a receipt never written, and one
        # removed by hand all look exactly like this.
        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.process_exit, "unknown")
        self.assertFalse(result.confirmed)

    def test_an_absent_receipt_is_not_a_success_exit_status(self) -> None:
        """The status the remote entry point returns must not read as done."""

        self.assertNotIn(STOP.STOP_RECEIPT_ABSENT, STOP.CONFIRMED_CODES)
        self.assertEqual(
            STOP.exit_code_for(STOP.STOP_RECEIPT_ABSENT), STOP.EXIT_UNCONFIRMED
        )
        self.assertNotEqual(
            STOP.exit_code_for(STOP.STOP_RECEIPT_ABSENT), STOP.EXIT_CONFIRMED
        )

    def test_a_token_mismatch_names_no_owner_state_either(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            original = _write_receipt(data)
            result = self._stop(data, OTHER_TOKEN)
            self.assertEqual((data / OWNER.OWNER_FILENAME).read_bytes(), original)
        self.assertEqual(result.code, STOP.STOP_REFUSED_TOKEN_MISMATCH)
        # Lack of authority is not liveness: nothing looked at the pid.
        self.assertEqual(result.state, "unknown")
        self.assertIs(result.token_available, False)
        self.assertEqual(result.process_exit, "unknown")
        self.assertEqual(controller.opened, [])

    def test_an_observed_exit_still_names_a_dead_owner(self) -> None:
        """The rule removes fabrication, not the states that were observed."""

        self._controller(exit_after_polls=1)
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_receipt(data)
            result = self._stop(data)
        self.assertEqual(result.state, "dead")
        self.assertEqual(result.process_exit, "verified")
        self.assertTrue(result.confirmed)

    def test_an_observed_live_owner_still_names_a_live_owner(self) -> None:
        self._controller(pidfd=False, open_error=_no_pidfd_error())
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            _write_receipt(data)
            result = self._stop(data)
        self.assertEqual(result.state, "live")
        self.assertEqual(result.process_exit, "failed")
        self.assertFalse(result.confirmed)

    def test_a_bare_code_is_only_inconsistent_when_it_claims_an_exit(self) -> None:
        """Every code must be expressible without fabricating evidence."""

        for code in STOP.STOP_CODES:
            with self.subTest(code=code):
                contradiction = STOP.report_contradiction(STOP.StopResult(code=code))
                if code in STOP.CONFIRMED_CODES:
                    self.assertIn("without a proven exit", contradiction)
                else:
                    self.assertEqual(contradiction, "")


class ForwardPortIsNotTheOriginalListenerTest(unittest.TestCase):
    """A-R6: the desktop's forward is a different listener on a different host.

    Closing the local SSH process frees the forward port while the remote owner
    and the listener it published both survive. That is the incident, not a
    release, so the contract's ``listener_release`` stays unknown and the local
    measurement lives in its own field.
    """

    def _host(self, port: int) -> _StopHost:
        return _StopHost(profile=_profile(port))

    def _held_port(self) -> int:
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        return listener.getsockname()[1]

    def test_a_freed_forward_never_populates_the_contract_listener_field(self) -> None:
        host = self._host(_free_port())
        host.reply_with(_confirmed_exit())

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertEqual(result.forward_listener_release, "verified")
        # The listener the remote owner published was never looked at.
        self.assertEqual(result.listener_release, "unknown")
        self.assertTrue(result.confirmed)

    def test_the_forward_is_observed_even_when_no_stop_could_be_asked(self) -> None:
        """The lost-token close is where the observation matters most."""

        host = self._host(self._held_port())

        result = RESOURCES.release_captured(host, _bundle(host, ""))

        self.assertEqual(result.code, STOP.STOP_NOT_REQUESTED_NO_TOKEN)
        self.assertEqual(host.stop_requests, [])
        self.assertEqual(result.forward_listener_release, "failed")
        self.assertEqual(result.listener_release, "unknown")

    def test_a_refused_close_also_observes_its_own_forward_port(self) -> None:
        host = self._host(_free_port())
        host.reply_with(_unfenced_refusal())

        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertEqual(result.forward_listener_release, "verified")
        self.assertEqual(result.listener_release, "unknown")

    def test_the_snapshot_projection_still_names_exactly_six_owner_keys(self) -> None:
        host = self._host(_free_port())
        host.reply_with(_confirmed_exit())
        result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))
        facts = result.owner_facts()
        self.assertEqual(set(facts), SNAPSHOT_OWNER_KEYS)
        self.assertNotIn("forward_listener_release", facts)
        self.assertEqual(facts["listener_release"], "unknown")

    def test_a_remote_cannot_report_the_desktops_own_forward_port(self) -> None:
        """The field never crosses the wire, in either direction."""

        forged = json.dumps(
            {
                "schema_version": STOP.SCHEMA_VERSION,
                "code": STOP.STOP_UNCONFIRMED_NO_PIDFD,
                "forward_listener_release": "verified",
            }
        ).encode("utf-8")
        decoded = STOP.decode_stop_result(forged)
        self.assertEqual(decoded.forward_listener_release, "unknown")
        encoded = STOP.encode_stop_result(
            STOP.StopResult(
                code=STOP.STOP_UNCONFIRMED_NO_PIDFD, forward_listener_release="verified"
            )
        )
        self.assertNotIn(b"forward_listener_release", encoded)

    def test_a_permission_refused_bind_is_not_a_retained_listener(self) -> None:
        """EACCES says this process may not bind, not that anything listens."""

        host = self._host(_free_port())
        host.reply_with(_timed_out_exit())
        with mock.patch.object(
            SHUTDOWN.socket,
            "create_connection",
            side_effect=OSError(errno.ETIMEDOUT, "timed out"),
        ), mock.patch.object(
            SHUTDOWN.socket, "socket", return_value=_DeniedSocket()
        ):
            result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertEqual(result.forward_listener_release, "unknown")
        self.assertEqual(result.listener_release, "unknown")

    def test_an_address_already_in_use_bind_is_a_retained_forward(self) -> None:
        host = self._host(_free_port())
        host.reply_with(_timed_out_exit())
        with mock.patch.object(
            SHUTDOWN.socket,
            "create_connection",
            side_effect=OSError(errno.ETIMEDOUT, "timed out"),
        ), mock.patch.object(
            SHUTDOWN.socket, "socket", return_value=_DeniedSocket(errno.EADDRINUSE)
        ):
            result = RESOURCES.release_captured(host, _bundle(host, "owned-token"))

        self.assertEqual(result.forward_listener_release, "failed")
        self.assertEqual(result.listener_release, "unknown")


if __name__ == "__main__":
    unittest.main()
