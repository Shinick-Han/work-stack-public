"""Focused tests for the bounded knowledge-driver transport.

Nothing here runs a real driver, opens a socket, reads a credential, or
touches a user directory.  The "driver" is a synthetic Python script written
into a temporary directory for the duration of the test class: it is a real
child on real pipes, so the stdin-only query, the explicit environment, the
discarded stderr, the deadline and the kill/reap boundary are exercised for
real, while the branches a real child cannot produce on demand -- an
unkillable process, a channel with no stdin -- use in-process fakes in the
same style as the existing remote-provision driver tests.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
for entry in (str(ROOT), str(SHELL)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

# The legacy spelling the desktop shell still uses.  It must resolve to the
# very same module object as the package spelling below, or a test that
# monkeypatches a constant on it would silently stop reaching the code.
import bounded_process_exchange as LEGACY  # noqa: E402

from workstack import bounded_process_exchange as EXCHANGE  # noqa: E402
from workstack import knowledge_driver_exchange as TRANSPORT  # noqa: E402
from workstack.knowledge_driver_exchange import (  # noqa: E402
    DEFAULT_TIMEOUT_SECONDS,
    DRIVER_NOT_STARTED,
    DRIVER_OUTCOME_UNKNOWN,
    INVALID_DRIVER_INPUT,
    MAX_COMMAND_PARTS,
    MAX_COMMAND_PART_CHARS,
    MAX_PAYLOAD_BYTES,
    MAX_STDOUT_BYTES,
    MAX_TIMEOUT_SECONDS,
    DriverExchangeResult,
    run_knowledge_driver,
)

CANARY_NAME = "WORKSTACK_DRIVER_CANARY"
AMBIENT_NAME = "WORKSTACK_DRIVER_AMBIENT"
CANARY_VALUE = "handed-over-explicitly"
AMBIENT_VALUE = "inherited-by-accident"
STDERR_MARKER = "driver diagnostic text"
PAYLOAD = b'{"schema_version":1,"query":"synthetic"}\n'

#: A synthetic driver.  ``echo`` reports exactly what crossed the boundary:
#: the stdin bytes it received and the environment it was actually given.
DRIVER_SOURCE = f'''
import os
import sys
import time

payload = sys.stdin.buffer.read()
mode = sys.argv[1]
if mode == "echo":
    sys.stderr.write({STDERR_MARKER!r} + "\\n")
    sys.stderr.flush()
    report = "\\n".join(
        [
            "payload=" + payload.decode("utf-8"),
            "canary=" + os.environ.get({CANARY_NAME!r}, ""),
            "ambient=" + os.environ.get({AMBIENT_NAME!r}, ""),
            "names=" + ",".join(sorted(os.environ)),
            "argv=" + " ".join(sys.argv[1:]),
        ]
    )
    sys.stdout.buffer.write(report.encode("utf-8"))
elif mode == "fail":
    sys.stdout.buffer.write(b"a partial answer that must not be returned")
    sys.stdout.flush()
    sys.exit(3)
elif mode == "flood":
    sys.stdout.buffer.write(b"x" * ({MAX_STDOUT_BYTES} * 4))
elif mode == "stall":
    time.sleep(30.0)
elif mode == "late":
    quiet = os.open(os.devnull, os.O_WRONLY)
    os.dup2(quiet, 1)
    os.dup2(quiet, 2)
    time.sleep(0.07)
'''


def exchange_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("workstack-bounded-exchange")
    ]


def await_exchange_threads(limit: float = 10.0) -> None:
    """Let a deliberately unsettled reader finish before the next test looks."""

    deadline = time.monotonic() + limit
    while exchange_threads() and time.monotonic() < deadline:
        time.sleep(0.02)


class RefusingFactory:
    """A launcher that proves admission passed without ever spawning."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error or OSError(2, "no such file")
        self.calls = 0

    def __call__(self, command: list[str], **keywords: object) -> object:
        self.calls += 1
        raise self.error


class NeverCalledFactory:
    """A launcher that fails the test if an inadmissible call reaches it."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, command: list[str], **keywords: object) -> object:
        self.calls += 1
        raise AssertionError("a refused request must never reach the launcher")


class RecordingLauncher:
    """Records every argv and keyword, then delegates to the real launcher."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.keywords: list[dict[str, object]] = []

    def __call__(self, command: list[str], **keywords: object) -> object:
        self.commands.append(list(command))
        self.keywords.append(dict(keywords))
        return subprocess.Popen(command, **keywords)  # type: ignore[arg-type]


class StallingStream:
    """A pipe read that only returns after the stall it was given."""

    def __init__(self, seconds: float = 0.5) -> None:
        self.seconds = seconds
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        time.sleep(self.seconds)
        return b""

    def close(self) -> None:
        self.closed = True


class BlockingStream:
    """A pipe read the test releases explicitly, so a grace can be measured."""

    def __init__(self) -> None:
        self.released = threading.Event()
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        self.released.wait(30.0)
        return b""

    def close(self) -> None:
        self.closed = True


class UnclosableStream(io.BytesIO):
    """A readable stream whose handle cannot be settled afterwards."""

    def close(self) -> None:
        raise OSError(9, "the handle could not be closed")


class FakeStdin:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> int:
        self.buffer.extend(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


#: Distinguishes "no stdout was given" from "stdout is deliberately absent".
MISSING = object()


class FakeProcess:
    """One in-process child, for the branches a real child cannot stage."""

    def __init__(
        self,
        *,
        stdout: object = MISSING,
        returncode: int = 0,
        killable: bool = True,
        no_stdin: bool = False,
        no_stdout: bool = False,
    ) -> None:
        self.stdin = None if no_stdin else FakeStdin()
        if no_stdout:
            self.stdout = None
        else:
            self.stdout = io.BytesIO(b"") if stdout is MISSING else stdout
        self.stderr = io.BytesIO(b"")
        self.returncode = returncode
        self.killable = killable
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if not self.killed and isinstance(self.stdout, (StallingStream, BlockingStream)):
            raise subprocess.TimeoutExpired(cmd="driver", timeout=timeout)
        return self.returncode

    def kill(self) -> None:
        if not self.killable:
            raise OSError(5, "the child refused to die")
        self.killed = True
        self.returncode = -9


class ScriptedClock:
    """A monotonic clock the child, not the wall, is allowed to advance.

    Only the transport's own clock is scripted; the shared primitive keeps
    reading the real one, so the exchange itself behaves exactly as shipped.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def spend(self, seconds: float) -> None:
        self.now += seconds

    def installed(self) -> object:
        """Swap only ``knowledge_driver_exchange.time`` for this clock."""

        original = TRANSPORT.time
        TRANSPORT.time = self  # type: ignore[assignment]
        return original


class CountingStdout(io.BytesIO):
    """Stdout that records whether the drain ever reached it."""

    def __init__(self, data: bytes = b"") -> None:
        super().__init__(data)
        self.reads = 0

    def read(self, size: int = -1) -> bytes:
        self.reads += 1
        return super().read(size)


class SlowStdin(FakeStdin):
    """A stdin whose write spends a scripted amount of the one budget."""

    def __init__(self, clock: ScriptedClock, seconds: float) -> None:
        super().__init__()
        self.clock = clock
        self.seconds = seconds

    def write(self, data: bytes) -> int:
        self.clock.spend(self.seconds)
        return super().write(data)


class LateProcess:
    """A child that exits zero, but only after the budget has been spent."""

    def __init__(self, clock: ScriptedClock, answer: bytes, overrun: float) -> None:
        self.clock = clock
        self.overrun = overrun
        self.stdin = FakeStdin()
        self.stdout = CountingStdout(answer)
        self.stderr = io.BytesIO(b"")
        self.returncode = 0
        self.killed = False
        self.waits = 0

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.waits == 1:
            # The shared drain's own floor is what outlasts the deadline.
            self.clock.spend(self.overrun)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class QueuedFactory:
    """Hands out prepared processes and counts how often it was called."""

    def __init__(self, *processes: FakeProcess) -> None:
        self.queue = list(processes)
        self.calls = 0
        self.keywords: list[dict[str, object]] = []

    def __call__(self, command: list[str], **keywords: object) -> FakeProcess:
        self.calls += 1
        self.keywords.append(dict(keywords))
        if not self.queue:
            raise AssertionError("the transport spawned more than once")
        return self.queue.pop(0)


class DriverScriptCase(unittest.TestCase):
    """Base class owning the one synthetic driver script on disk."""

    directory: str
    script: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.mkdtemp(prefix="workstack-driver-")
        cls.script = str(Path(cls.directory) / "synthetic_driver.py")
        Path(cls.script).write_text(DRIVER_SOURCE, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    def command(self, mode: str) -> list[str]:
        return [sys.executable, self.script, mode]

    def environment(self) -> dict[str, str]:
        return {CANARY_NAME: CANARY_VALUE}


class DriverInputAdmissionTests(unittest.TestCase):
    """Every inadmissible request is refused before anything is spawned."""

    def refuse(self, **overrides: object) -> DriverExchangeResult:
        launcher = NeverCalledFactory()
        request: dict[str, object] = {
            "command": ["/opt/driver/bin/driver", "--once"],
            "payload": PAYLOAD,
            "environment": {},
            "timeout_seconds": 5.0,
        }
        request.update(overrides)
        result = run_knowledge_driver(
            request.pop("command"),  # type: ignore[arg-type]
            request.pop("payload"),  # type: ignore[arg-type]
            process_factory=launcher,
            **request,  # type: ignore[arg-type]
        )
        self.assertEqual(launcher.calls, 0, "a refusal never reaches the launcher")
        self.assertEqual(result, DriverExchangeResult(None, INVALID_DRIVER_INPUT, "settled"))
        return result

    def test_a_command_that_is_not_a_pinned_argv_is_refused(self) -> None:
        for command in (
            "/opt/driver/bin/driver",
            b"/opt/driver/bin/driver",
            [],
            (),
            ["driver", "--once"],
            ["../driver"],
            [""],
            [None],
            ["/opt/driver/bin/driver", 3],
            ["/opt/driver/bin/driver", ""],
            ["/opt/driver/bin/driver", "a" * (MAX_COMMAND_PART_CHARS + 1)],
            ["/opt/driver/bin/driver"] + ["--x"] * MAX_COMMAND_PARTS,
        ):
            with self.subTest(command=repr(command)[:60]):
                self.refuse(command=command)

    def test_a_payload_that_is_not_bounded_bytes_is_refused(self) -> None:
        for payload in (
            b"",
            "a string query",
            bytearray(PAYLOAD),
            None,
            b"x" * (MAX_PAYLOAD_BYTES + 1),
        ):
            with self.subTest(payload=repr(payload)[:40]):
                self.refuse(payload=payload)

    def test_an_environment_that_is_not_an_explicit_string_mapping_is_refused(self) -> None:
        for environment in (
            None,
            [("PATH", "/usr/bin")],
            {"PATH": None},
            {"PATH": 1},
            {1: "value"},
        ):
            with self.subTest(environment=repr(environment)[:40]):
                self.refuse(environment=environment)

    def test_a_timeout_that_is_not_a_finite_positive_budget_is_refused(self) -> None:
        for timeout in (
            0,
            0.0,
            -1.0,
            MAX_TIMEOUT_SECONDS + 0.001,
            float("nan"),
            float("inf"),
            True,
            False,
            "5",
            None,
        ):
            with self.subTest(timeout=repr(timeout)):
                self.refuse(timeout_seconds=timeout)

    def test_the_admitted_boundary_values_reach_the_launcher(self) -> None:
        """The bounds are inclusive: proven by a launcher that then refuses."""

        for overrides in (
            {"command": ["/opt/driver/bin/driver"] + ["--x"] * (MAX_COMMAND_PARTS - 1)},
            {"command": ["/opt/" + "a" * (MAX_COMMAND_PART_CHARS - 6)]},
            {"payload": b"x" * MAX_PAYLOAD_BYTES},
            {"timeout_seconds": MAX_TIMEOUT_SECONDS},
            {"timeout_seconds": 1},
            {"environment": {}},
        ):
            with self.subTest(overrides=sorted(overrides)):
                launcher = RefusingFactory()
                request: dict[str, object] = {
                    "command": ["/opt/driver/bin/driver", "--once"],
                    "payload": PAYLOAD,
                    "environment": {},
                    "timeout_seconds": 5.0,
                }
                request.update(overrides)
                result = run_knowledge_driver(
                    request.pop("command"),  # type: ignore[arg-type]
                    request.pop("payload"),  # type: ignore[arg-type]
                    process_factory=launcher,
                    **request,  # type: ignore[arg-type]
                )
                self.assertEqual(launcher.calls, 1)
                self.assertEqual(result.error_code, DRIVER_NOT_STARTED)

    def test_the_default_timeout_is_the_bounded_provider_budget(self) -> None:
        self.assertEqual(DEFAULT_TIMEOUT_SECONDS, 75.0)
        self.assertLessEqual(DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)


class DriverSuccessTests(DriverScriptCase):
    """One real child, one stdin query, one bounded answer."""

    def setUp(self) -> None:
        os.environ[AMBIENT_NAME] = AMBIENT_VALUE
        self.addCleanup(os.environ.pop, AMBIENT_NAME, None)

    def test_one_query_crosses_on_stdin_and_the_answer_comes_back(self) -> None:
        launcher = RecordingLauncher()
        result = run_knowledge_driver(
            self.command("echo"),
            PAYLOAD,
            environment=self.environment(),
            timeout_seconds=20.0,
            process_factory=launcher,
        )
        self.assertIsNone(result.error_code)
        self.assertEqual(result.cleanup, "settled")
        assert result.stdout is not None
        report = result.stdout.decode("utf-8")
        self.assertIn("payload=" + PAYLOAD.decode("utf-8").strip(), report)
        self.assertEqual(len(launcher.commands), 1, "at most one launch, ever")

    def test_the_child_environment_is_the_given_one_and_not_the_ambient_one(self) -> None:
        result = run_knowledge_driver(
            self.command("echo"),
            PAYLOAD,
            environment=self.environment(),
            timeout_seconds=20.0,
        )
        assert result.stdout is not None
        report = result.stdout.decode("utf-8")
        self.assertIn("canary=" + CANARY_VALUE, report)
        self.assertIn("ambient=\n", report + "\n", "the ambient canary never crossed")
        self.assertIn("names=" + CANARY_NAME + "\n", report)
        self.assertIn(AMBIENT_NAME, os.environ, "the ambient canary was really set")

    def test_the_launcher_is_bound_to_an_explicit_environment_and_no_shell(self) -> None:
        environment = self.environment()
        launcher = RecordingLauncher()
        run_knowledge_driver(
            self.command("echo"),
            PAYLOAD,
            environment=environment,
            timeout_seconds=20.0,
            process_factory=launcher,
        )
        keywords = launcher.keywords[0]
        self.assertIs(keywords["shell"], False)
        self.assertEqual(keywords["env"], environment)
        self.assertIsNot(keywords["env"], environment, "the mapping is copied, not held")
        self.assertIsNot(keywords["env"], os.environ)
        self.assertEqual(keywords["stdin"], subprocess.PIPE)

    def test_the_payload_never_appears_on_the_argv(self) -> None:
        launcher = RecordingLauncher()
        run_knowledge_driver(
            self.command("echo"),
            PAYLOAD,
            environment=self.environment(),
            timeout_seconds=20.0,
            process_factory=launcher,
        )
        self.assertEqual(launcher.commands[0], self.command("echo"))
        for part in launcher.commands[0]:
            self.assertNotIn(PAYLOAD.decode("utf-8").strip(), part)

    def test_child_diagnostics_are_read_and_discarded(self) -> None:
        result = run_knowledge_driver(
            self.command("echo"),
            PAYLOAD,
            environment=self.environment(),
            timeout_seconds=20.0,
        )
        assert result.stdout is not None
        self.assertNotIn(STDERR_MARKER, result.stdout.decode("utf-8"))
        self.assertEqual(
            [field.name for field in fields(DriverExchangeResult)],
            ["stdout", "error_code", "cleanup"],
            "the result carries no channel for child diagnostics",
        )


class DriverFailureTests(DriverScriptCase):
    """Every failing branch is closed, unknown, and never retried."""

    def run_once(
        self, mode: str, timeout: float
    ) -> tuple[DriverExchangeResult, RecordingLauncher]:
        launcher = RecordingLauncher()
        result = run_knowledge_driver(
            self.command(mode),
            PAYLOAD,
            environment=self.environment(),
            timeout_seconds=timeout,
            process_factory=launcher,
        )
        self.assertEqual(len(launcher.commands), 1, "no branch retries the driver")
        return result, launcher

    def test_a_nonzero_exit_is_an_unknown_outcome_and_returns_no_answer(self) -> None:
        result, _launcher = self.run_once("fail", 20.0)
        self.assertEqual(result.error_code, DRIVER_OUTCOME_UNKNOWN)
        self.assertIsNone(result.stdout, "a failed driver's partial answer is dropped")
        self.assertEqual(result.cleanup, "settled")

    def test_a_driver_that_never_answers_is_stopped_at_the_deadline(self) -> None:
        self.addCleanup(await_exchange_threads)
        started = time.monotonic()
        result, _launcher = self.run_once("stall", 0.4)
        elapsed = time.monotonic() - started
        self.assertEqual(result.error_code, DRIVER_OUTCOME_UNKNOWN)
        self.assertIsNone(result.stdout)
        self.assertLess(elapsed, 20.0, "the deadline, not the child, ends the exchange")

    def test_a_flooding_driver_is_refused_at_the_output_bound(self) -> None:
        self.addCleanup(await_exchange_threads)
        result, _launcher = self.run_once("flood", 20.0)
        self.assertEqual(result.error_code, DRIVER_OUTCOME_UNKNOWN)
        self.assertIsNone(result.stdout)

    def test_a_command_that_cannot_start_never_sent_the_payload(self) -> None:
        for error in (OSError(2, "no such file"), ValueError("embedded null byte")):
            with self.subTest(error=type(error).__name__):
                launcher = RefusingFactory(error)
                result = run_knowledge_driver(
                    self.command("echo"),
                    PAYLOAD,
                    environment=self.environment(),
                    process_factory=launcher,
                )
                self.assertEqual(launcher.calls, 1)
                self.assertEqual(
                    result, DriverExchangeResult(None, DRIVER_NOT_STARTED, "settled")
                )

    def test_a_channel_with_no_stdin_never_sent_the_payload(self) -> None:
        process = FakeProcess(no_stdin=True)
        launcher = QueuedFactory(process)
        result = run_knowledge_driver(
            self.command("echo"),
            PAYLOAD,
            environment=self.environment(),
            process_factory=launcher,
        )
        self.assertEqual(launcher.calls, 1)
        self.assertEqual(result.error_code, DRIVER_NOT_STARTED)
        self.assertIsNone(result.stdout)
        self.assertTrue(process.killed, "the child this exchange owns is stopped")

    def test_a_channel_with_no_output_is_an_unknown_outcome(self) -> None:
        process = FakeProcess(no_stdout=True)
        launcher = QueuedFactory(process)
        result = run_knowledge_driver(
            self.command("echo"),
            PAYLOAD,
            environment=self.environment(),
            process_factory=launcher,
        )
        self.assertEqual(launcher.calls, 1)
        self.assertEqual(result.error_code, DRIVER_OUTCOME_UNKNOWN)
        self.assertIsNone(result.stdout)


class DriverCleanupTests(DriverScriptCase):
    """A cleanup that did not work is reported, never softened into success."""

    def unsettled(self, timeout: float = 0.2) -> DriverExchangeResult:
        process = FakeProcess(stdout=StallingStream(0.4), killable=False)
        launcher = QueuedFactory(process)
        result = run_knowledge_driver(
            self.command("echo"),
            PAYLOAD,
            environment=self.environment(),
            timeout_seconds=timeout,
            process_factory=launcher,
        )
        self.assertEqual(launcher.calls, 1)
        return result

    def test_a_cleanup_that_could_not_be_confirmed_is_reported_as_unsettled(self) -> None:
        self.addCleanup(await_exchange_threads)
        result = self.unsettled()
        self.assertEqual(result.error_code, DRIVER_OUTCOME_UNKNOWN)
        self.assertEqual(result.cleanup, "unsettled")
        self.assertIsNone(result.stdout)

    def test_a_zero_exit_with_an_unsettled_cleanup_is_not_a_success(self) -> None:
        """The driver answered and exited zero, but a handle never settled."""

        process = FakeProcess(stdout=UnclosableStream(b"an answer nobody may trust"))
        launcher = QueuedFactory(process)
        result = run_knowledge_driver(
            self.command("echo"),
            PAYLOAD,
            environment=self.environment(),
            timeout_seconds=5.0,
            process_factory=launcher,
        )
        self.assertEqual(launcher.calls, 1)
        self.assertEqual(process.returncode, 0, "the child really did exit zero")
        self.assertEqual(result.cleanup, "unsettled")
        self.assertEqual(result.error_code, DRIVER_OUTCOME_UNKNOWN)
        self.assertIsNone(result.stdout, "an unsettled exchange returns no answer")


class LegacyExchangeIdentityTests(DriverScriptCase):
    """The desktop spelling and the package spelling are one module."""

    def test_the_two_spellings_are_the_same_module_object(self) -> None:
        self.assertIs(LEGACY, EXCHANGE)
        self.assertIs(
            sys.modules["bounded_process_exchange"],
            sys.modules["workstack.bounded_process_exchange"],
        )
        self.assertIs(LEGACY.BoundedExchange, EXCHANGE.BoundedExchange)

    def test_the_legacy_settle_grace_monkeypatch_still_reaches_the_transport(self) -> None:
        """The bound a desktop test patches is the bound this transport waits."""

        stream = BlockingStream()
        self.addCleanup(await_exchange_threads)
        self.addCleanup(stream.released.set)
        grace = LEGACY.SETTLE_GRACE_SECONDS
        self.assertGreaterEqual(grace, 1.0, "the shipped grace is seconds, not instant")
        LEGACY.SETTLE_GRACE_SECONDS = 0.05
        try:
            self.assertEqual(EXCHANGE.SETTLE_GRACE_SECONDS, 0.05)
            launcher = QueuedFactory(FakeProcess(stdout=stream, killable=False))
            started = time.monotonic()
            result = run_knowledge_driver(
                self.command("echo"),
                PAYLOAD,
                environment=self.environment(),
                timeout_seconds=0.2,
                process_factory=launcher,
            )
            elapsed = time.monotonic() - started
        finally:
            LEGACY.SETTLE_GRACE_SECONDS = grace
        self.assertEqual(result.cleanup, "unsettled")
        self.assertLess(
            elapsed,
            1.0,
            "the patched grace, not the shipped one, bounded the settle",
        )
        self.assertEqual(EXCHANGE.SETTLE_GRACE_SECONDS, grace, "the bound is restored")


class DriverDeadlineAcceptanceTests(DriverScriptCase):
    """The one budget governs acceptance, not merely the arguments passed on."""

    def test_a_real_child_that_answers_after_the_deadline_is_not_a_success(self) -> None:
        """The oracle: a pinned local child, a real clock, a tiny budget.

        The child reads the payload, closes both output streams and only then
        sleeps past a 0.03 second budget before exiting zero.  Whichever way
        the scheduler lands -- the drain times out, or the shared 0.1 second
        floors let the reap finish late -- the answer arrived after the
        deadline, so it may never be reported as a success.
        """

        self.addCleanup(await_exchange_threads)
        launcher = RecordingLauncher()
        result = run_knowledge_driver(
            self.command("late"),
            PAYLOAD,
            environment=self.environment(),
            timeout_seconds=0.03,
            process_factory=launcher,
        )
        self.assertEqual(len(launcher.commands), 1, "no branch retries the driver")
        self.assertEqual(result.error_code, DRIVER_OUTCOME_UNKNOWN)
        self.assertIsNone(result.stdout, "a late answer is discarded, not returned")

    def test_a_nominally_clean_result_past_the_deadline_is_checked_and_refused(self) -> None:
        """Deterministic: the drain succeeds, but the budget is already gone."""

        clock = ScriptedClock()
        process = LateProcess(clock, b"an answer that came too late", overrun=9.0)
        launcher = QueuedFactory(process)
        original = clock.installed()
        try:
            result = run_knowledge_driver(
                self.command("echo"),
                PAYLOAD,
                environment=self.environment(),
                timeout_seconds=1.0,
                process_factory=launcher,
            )
        finally:
            TRANSPORT.time = original  # type: ignore[assignment]
        self.assertEqual(launcher.calls, 1, "the late check never re-runs the driver")
        self.assertEqual(process.returncode, 0, "the child really did exit zero")
        self.assertGreaterEqual(process.stdout.reads, 1, "the drain really did run")
        self.assertEqual(result.error_code, DRIVER_OUTCOME_UNKNOWN)
        self.assertIsNone(result.stdout, "the late bytes never reach the caller")
        self.assertEqual(result.cleanup, "settled", "the actual cleanup is preserved")

    def test_a_budget_spent_by_the_write_never_starts_the_drain(self) -> None:
        """Deterministic: the write consumes the same budget the drain needs."""

        clock = ScriptedClock()
        process = FakeProcess(stdout=CountingStdout(b"never read"))
        process.stdin = SlowStdin(clock, 4.0)
        launcher = QueuedFactory(process)
        original = clock.installed()
        try:
            result = run_knowledge_driver(
                self.command("echo"),
                PAYLOAD,
                environment=self.environment(),
                timeout_seconds=1.0,
                process_factory=launcher,
            )
        finally:
            TRANSPORT.time = original  # type: ignore[assignment]
        self.assertEqual(launcher.calls, 1)
        self.assertEqual(bytes(process.stdin.buffer), PAYLOAD, "the payload did leave")
        self.assertEqual(process.stdout.reads, 0, "an exhausted budget starts no drain")
        self.assertTrue(process.killed, "the one child is stopped, not left running")
        self.assertEqual(result.error_code, DRIVER_OUTCOME_UNKNOWN)
        self.assertIsNone(result.stdout)
        self.assertEqual(result.cleanup, "settled", "the actual cleanup is preserved")


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()
