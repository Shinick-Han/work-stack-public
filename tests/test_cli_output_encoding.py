"""Behavioural conformance for `work-stack` output encoding on a non-UTF-8 host.

The defect this pins: on a Korean Windows shell the inherited stdio encoding is
cp949, and the CLI's ``print(json.dumps(..., ensure_ascii=False))`` raised
``UnicodeEncodeError`` for any character cp949 cannot represent -- an em dash, an
accent, an emoji, most of Unicode. ``UnicodeEncodeError`` subclasses
``ValueError``, so ``cli.main`` caught it as an ordinary refusal and returned
exit 2 with empty stdout and a diagnostic about a codec, *after* the SSOT write
had already committed. A successful write looked like a failed command.

What the cases below hold to:

* stdout is UTF-8 machine JSON -- no BOM, no replacement, no mixed encoding --
  on a pipe, on a file redirect and on a real Windows console, with
  ``PYTHONIOENCODING=cp949``, for Korean text, for a non-BMP surrogate pair, for
  ordinary pretty JSON and for the plain path line ``graph export`` prints;
* stderr stays readable for a synthetic in-product error and for argparse's own
  refusal, which is raised before the command's own error handling exists;
* exit codes and the JSON document are what they were, and the canonical agent
  envelope is still exactly one JSON object followed by one LF;
* nothing is installed at all off Windows -- a non-Windows host keeps its
  inherited encoding, its newlines and its codec refusals, whatever its locale;
* a stream that is already UTF-8, and a stream with no binary buffer
  (``StringIO``, a capture double), are left untouched. No live stream is
  ``reconfigure``d, detached or closed, so a test that collects text still
  collects text and the user's shell, environment and code page are never
  modified.

Every subprocess case runs the real ``run_work_stack.py`` entry point against a
synthetic data directory built by the test; no live Work Stack store, owner or
credential is read, and the Windows console case runs in a hidden window.
"""

from __future__ import annotations

import codecs
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPOSITORY_ROOT / "run_work_stack.py"

# One title a cp949 host cannot encode. The Korean text alone *is* representable
# in cp949; the em dash, the accent and the emoji are not, and the last character
# is non-BMP, so the fixture exercises a surrogate pair as well as the ordinary
# multi-byte case. CONSOLE_TEXT is the BMP-only prefix, used where the oracle is
# a console screen buffer whose cells are UTF-16.
NON_ASCII_TITLE = "한글 제목 — café ✅ 😀"
CONSOLE_TEXT = "한글 제목 — café"
NON_BMP = "😀"
HOST_ENCODING = "cp949"
REPLACEMENT = "\N{REPLACEMENT CHARACTER}"
# Every child here is one short local command against a synthetic store. A bound
# makes a wedged child fail the case instead of hanging the suite.
CHILD_TIMEOUT_SECONDS = 120


def _result_root() -> Path | None:
    """Fixture root; confined to the assigned results directory when provided."""

    configured = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
    if configured:
        root = Path(configured) / "cli-output-encoding-fixtures"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None


class _SyntheticStoreCase(unittest.TestCase):
    """A synthetic data directory plus a runtime redirected away from the host."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
        self.home = Path(self.temporary.name)
        self.root = self.home / "data"
        self.runtime = self.home / "runtime"
        self.scratch = self.home / "tmp"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.temporary.cleanup)

        self._saved_environment = {
            name: os.environ.get(name)
            for name in ("WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR")
        }
        os.environ["WORK_STACK_RUNTIME"] = str(self.runtime)
        os.environ["TEMP"] = str(self.scratch)
        os.environ["TMP"] = str(self.scratch)
        os.environ["TMPDIR"] = str(self.scratch)
        self.addCleanup(self._restore_environment)

        # Imported after the redirection so no Store can resolve a real runtime path.
        from workstack.service import WorkStack
        from workstack.store import Store

        self.stack = WorkStack(Store(self.root))
        self.task_id = self.stack.add_task(NON_ASCII_TITLE)["id"]

    def _restore_environment(self) -> None:
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def child_environment(self) -> dict[str, str]:
        """The host a Korean Windows shell hands a child: stdio pinned to cp949."""

        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = HOST_ENCODING
        environment.pop("PYTHONUTF8", None)
        environment["WORK_STACK_RUNTIME"] = str(self.runtime)
        environment["TEMP"] = str(self.scratch)
        environment["TMP"] = str(self.scratch)
        environment["TMPDIR"] = str(self.scratch)
        return environment

    def command(self, *arguments: str) -> list[str]:
        return [
            sys.executable,
            "-B",
            str(LAUNCHER),
            "--data-dir",
            str(self.root),
            *arguments,
        ]

    def workspace_uid(self) -> str:
        return json.loads((self.root / "workspace.json").read_text(encoding="utf-8"))["id"]


class CommandOutputSurvivesACp949Host(_SyntheticStoreCase):
    """stdout stays UTF-8 machine JSON however the command line is redirected."""

    def test_piped_stdout_is_utf8_json_and_the_command_succeeds(self) -> None:
        completed = subprocess.run(
            self.command("backlog", "show", self.task_id),
            capture_output=True,
            env=self.child_environment(),
            cwd=str(REPOSITORY_ROOT),
            timeout=CHILD_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        self.assertEqual(completed.stderr, b"")
        self.assertIn(NON_ASCII_TITLE.encode("utf-8"), completed.stdout)
        self.assertIn(NON_BMP.encode("utf-8"), completed.stdout)
        self.assertEqual(
            json.loads(completed.stdout.decode("utf-8"))["title"], NON_ASCII_TITLE
        )
        # No BOM and nothing substituted for a character the host codec refused.
        self.assertFalse(completed.stdout.startswith(codecs.BOM_UTF8))
        self.assertNotIn(REPLACEMENT, completed.stdout.decode("utf-8"))
        # The old path reported the codec failure as a refusal, so the regression
        # is a silent exit 2 with no stdout at all.
        self.assertNotIn(b"codec", completed.stdout)

    def test_a_file_redirect_receives_the_same_utf8_bytes(self) -> None:
        target = self.home / "redirected.json"
        with open(target, "wb") as handle:
            completed = subprocess.run(
                self.command("backlog", "show", self.task_id),
                stdout=handle,
                stderr=subprocess.PIPE,
                env=self.child_environment(),
                cwd=str(REPOSITORY_ROOT),
                timeout=CHILD_TIMEOUT_SECONDS,
            )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        written = target.read_bytes()
        self.assertIn(NON_ASCII_TITLE.encode("utf-8"), written)
        self.assertFalse(written.startswith(codecs.BOM_UTF8))
        self.assertEqual(json.loads(written.decode("utf-8"))["title"], NON_ASCII_TITLE)
        # One canonical JSON document terminated by LF, not by the text layer's CRLF.
        self.assertTrue(written.endswith(b"\n"))
        self.assertNotIn(b"\r", written)

    def test_machine_json_is_unchanged_by_the_encoding_boundary(self) -> None:
        """The payload is the encoding's subject, not its casualty."""

        host = subprocess.run(
            self.command("backlog", "list"),
            capture_output=True,
            env=self.child_environment(),
            cwd=str(REPOSITORY_ROOT),
            timeout=CHILD_TIMEOUT_SECONDS,
        )
        utf8_environment = self.child_environment()
        utf8_environment["PYTHONIOENCODING"] = "utf-8"
        reference = subprocess.run(
            self.command("backlog", "list"),
            capture_output=True,
            env=utf8_environment,
            cwd=str(REPOSITORY_ROOT),
            timeout=CHILD_TIMEOUT_SECONDS,
        )
        self.assertEqual(host.returncode, 0, host.stderr.decode("utf-8", "replace"))
        self.assertEqual(reference.returncode, 0)
        # The same document and the same UTF-8 text either way. The redirected
        # cp949 host ends its lines with LF instead of the text layer's CRLF,
        # which is the canonical form the agent envelope already emits.
        self.assertEqual(
            json.loads(host.stdout.decode("utf-8")),
            json.loads(reference.stdout.decode("utf-8")),
        )
        self.assertEqual(
            host.stdout.replace(b"\r\n", b"\n"),
            reference.stdout.replace(b"\r\n", b"\n"),
        )
        self.assertEqual(
            json.loads(host.stdout.decode("utf-8"))[0]["title"], NON_ASCII_TITLE
        )

    def test_the_graph_export_path_line_is_utf8(self) -> None:
        """`graph export` prints a path rather than JSON, and a path can be non-ASCII."""

        target = self.home / "그래프 — export.json"
        completed = subprocess.run(
            self.command("graph", "export", "--out", str(target)),
            capture_output=True,
            env=self.child_environment(),
            cwd=str(REPOSITORY_ROOT),
            timeout=CHILD_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        self.assertEqual(completed.stdout.strip(), str(target).encode("utf-8"))
        self.assertTrue(target.is_file())

    def test_the_agent_envelope_stays_one_json_object_and_one_lf(self) -> None:
        """The envelope already wrote canonical bytes; the boundary must not disturb it."""

        completed = subprocess.run(
            self.command("agent", "--workspace-uid", self.workspace_uid(), "status"),
            capture_output=True,
            env=self.child_environment(),
            cwd=str(REPOSITORY_ROOT),
            timeout=CHILD_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        self.assertEqual(completed.stderr, b"")
        self.assertTrue(completed.stdout.endswith(b"\n"))
        self.assertEqual(completed.stdout.count(b"\n"), 1)
        self.assertNotIn(b"\r", completed.stdout)
        envelope = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(envelope["contract"], "workstack.cli.v1")


class ErrorOutputStaysReadableAndKeepsItsExitCode(_SyntheticStoreCase):
    """A refusal is still a refusal, and its text is still legible."""

    def test_a_product_error_carrying_non_ascii_reaches_stderr_as_utf8(self) -> None:
        completed = subprocess.run(
            self.command("backlog", "show", NON_ASCII_TITLE),
            capture_output=True,
            env=self.child_environment(),
            cwd=str(REPOSITORY_ROOT),
            timeout=CHILD_TIMEOUT_SECONDS,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(NON_ASCII_TITLE.encode("utf-8"), completed.stderr)
        message = completed.stderr.decode("utf-8")
        self.assertTrue(message.startswith("error: "), message)
        self.assertNotIn(REPLACEMENT, message)
        self.assertNotIn("codec", message)

    def test_argparse_refusal_echoing_a_non_ascii_argument_still_exits_two(self) -> None:
        """argparse writes and exits before the command's own error handling exists."""

        completed = subprocess.run(
            self.command("backlog", "add", "title", "--priority", NON_ASCII_TITLE),
            capture_output=True,
            env=self.child_environment(),
            cwd=str(REPOSITORY_ROOT),
            timeout=CHILD_TIMEOUT_SECONDS,
        )
        self.assertEqual(completed.returncode, 2)
        message = completed.stderr.decode("utf-8")
        self.assertIn(NON_ASCII_TITLE, message)
        self.assertIn("usage:", message)
        self.assertNotIn("Traceback", message)


class InProcessStreamsAreLeftAsTheCallerBuiltThem(_SyntheticStoreCase):
    """The boundary encodes; it does not reconfigure, detach or replace."""

    def test_a_stringio_stderr_still_collects_text_and_the_exit_code_holds(self) -> None:
        from unittest.mock import patch

        from workstack import cli

        stderr = io.StringIO()
        with patch.object(cli.sys, "stderr", stderr):
            code = cli.main(["--data-dir", str(self.root), "backlog", "show", "T-9999"])
        self.assertEqual(code, 2)
        self.assertTrue(stderr.getvalue().startswith("error: "), stderr.getvalue())
        self.assertTrue(stderr.getvalue().endswith("\n"))

    def test_a_stringio_stdout_still_collects_decoded_json_text(self) -> None:
        from unittest.mock import patch

        from workstack import cli

        stdout = io.StringIO()
        with patch.object(cli.sys, "stdout", stdout):
            code = cli.main(
                ["--data-dir", str(self.root), "backlog", "show", self.task_id]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["title"], NON_ASCII_TITLE)

    def test_the_view_declines_streams_it_must_not_touch(self) -> None:
        from workstack import cli_output

        # No binary buffer to write through.
        self.assertIsNone(cli_output.utf8_view(io.StringIO()))
        self.assertIsNone(cli_output.utf8_view(object()))
        # Already UTF-8: the inherited encoding is not the defect, so every
        # ordinary POSIX shell and every UTF-8 Windows host keeps its behaviour.
        self.assertIsNone(
            cli_output.utf8_view(io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
        )
        self.assertIsNone(
            cli_output.utf8_view(io.TextIOWrapper(io.BytesIO(), encoding="UTF8"))
        )
        self.assertTrue(
            cli_output.encodes_as_utf8(io.TextIOWrapper(io.BytesIO(), encoding="utf8"))
        )
        self.assertFalse(cli_output.encodes_as_utf8(object()))
        # A detached or closed buffer cannot carry the bytes.
        closed = io.TextIOWrapper(io.BytesIO(), encoding=HOST_ENCODING)
        closed.buffer.close()
        self.assertIsNone(cli_output.utf8_view(closed))

    def test_live_streams_are_restored_and_never_reconfigured(self) -> None:
        from workstack import cli_output

        raw = io.BytesIO()
        host = io.TextIOWrapper(raw, encoding=HOST_ENCODING, newline="\r\n")
        saved_stdout, saved_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout = host
            sys.stderr = host
            with cli_output.utf8_streams():
                self.assertIsNot(sys.stdout, host)
                self.assertEqual(sys.stdout.encoding, "utf-8")
                sys.stdout.write(NON_ASCII_TITLE + "\n")
            self.assertIs(sys.stdout, host)
            self.assertIs(sys.stderr, host)
        finally:
            sys.stdout, sys.stderr = saved_stdout, saved_stderr
        # The wrapped stream keeps the host's own encoding and stays usable.
        self.assertEqual(host.encoding, HOST_ENCODING)
        self.assertFalse(host.closed)
        self.assertFalse(raw.closed)
        self.assertEqual(raw.getvalue(), (NON_ASCII_TITLE + "\n").encode("utf-8"))

    def test_a_utf8_host_stream_is_not_replaced_at_all(self) -> None:
        from workstack import cli_output

        host = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        saved_stdout = sys.stdout
        try:
            sys.stdout = host
            with cli_output.utf8_streams():
                self.assertIs(sys.stdout, host)
        finally:
            sys.stdout = saved_stdout

    def test_the_boundary_is_unwound_when_the_command_raises(self) -> None:
        from workstack import cli_output

        host = io.TextIOWrapper(io.BytesIO(), encoding=HOST_ENCODING)
        saved_stdout = sys.stdout
        try:
            sys.stdout = host
            with self.assertRaises(SystemExit):
                with cli_output.utf8_streams():
                    raise SystemExit(2)
            self.assertIs(sys.stdout, host)
        finally:
            sys.stdout = saved_stdout


class TheBoundaryIsInstalledOnWindowsOnly(unittest.TestCase):
    """The gate is the platform, not a guess about which codecs a platform uses.

    The packet promises that inherited encoding behaviour does not change on
    non-Windows. A POSIX host with a cp949, latin-1 or any other non-UTF-8
    locale therefore has to come out of ``utf8_streams`` with the very same
    stream objects it went in with, still refusing what its codec refuses.
    """

    NON_WINDOWS = ("linux", "darwin", "freebsd13", "aix", "cygwin")

    def _cp949_stream(self) -> io.TextIOWrapper:
        return io.TextIOWrapper(io.BytesIO(), encoding=HOST_ENCODING, newline="\r\n")

    def test_a_non_windows_host_never_wraps_a_non_utf8_stream(self) -> None:
        from unittest.mock import patch

        from workstack import cli_output

        for platform in self.NON_WINDOWS:
            with self.subTest(platform=platform):
                with patch.object(cli_output.sys, "platform", platform):
                    self.assertFalse(cli_output.runs_on_windows())
                    self.assertIsNone(cli_output.utf8_view(self._cp949_stream()))

    def test_a_non_windows_host_keeps_its_streams_and_its_codec_refusal(self) -> None:
        from unittest.mock import patch

        from workstack import cli_output

        raw = io.BytesIO()
        host = io.TextIOWrapper(raw, encoding=HOST_ENCODING, newline="\r\n")
        saved_stdout, saved_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout = host
            sys.stderr = host
            with patch.object(cli_output.sys, "platform", "linux"):
                with cli_output.utf8_streams():
                    # Same objects: nothing was replaced, so nothing downstream
                    # sees a different encoding, newline or buffering.
                    self.assertIs(sys.stdout, host)
                    self.assertIs(sys.stderr, host)
                    self.assertEqual(sys.stdout.encoding, HOST_ENCODING)
                    # And the inherited codec still refuses what it refused
                    # before this boundary existed.
                    with self.assertRaises(UnicodeEncodeError):
                        sys.stdout.write(NON_ASCII_TITLE + "\n")
                        sys.stdout.flush()
            self.assertIs(sys.stdout, host)
            self.assertIs(sys.stderr, host)
        finally:
            sys.stdout, sys.stderr = saved_stdout, saved_stderr
        self.assertNotIn(NON_ASCII_TITLE.encode("utf-8"), raw.getvalue())

    def test_the_same_stream_is_still_wrapped_on_windows(self) -> None:
        from unittest.mock import patch

        from workstack import cli_output

        # The negative above is caused by the platform and by nothing else: the
        # identical stream is wrapped when the platform is Windows.
        with patch.object(cli_output.sys, "platform", "win32"):
            self.assertTrue(cli_output.runs_on_windows())
            view = cli_output.utf8_view(self._cp949_stream())
        self.assertIsInstance(view, cli_output.Utf8Stream)


CONSOLE_PROBE = r'''
import ctypes, json, pathlib, sys

sys.path.insert(0, sys.argv[1])
target = pathlib.Path(sys.argv[2])
arguments = json.loads(sys.argv[3])

from workstack.cli import main

code = main(arguments)
sys.stdout.flush()
sys.stderr.flush()

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
handle = kernel32.CreateFileW(
    "CONOUT$", 0x40000000 | 0x80000000, 0x1 | 0x2, None, 3, 0, None
)


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", ctypes.c_short),
        ("Top", ctypes.c_short),
        ("Right", ctypes.c_short),
        ("Bottom", ctypes.c_short),
    ]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", COORD),
        ("dwCursorPosition", COORD),
        ("wAttributes", ctypes.c_ushort),
        ("srWindow", SMALL_RECT),
        ("dwMaximumWindowSize", COORD),
    ]


info = CONSOLE_SCREEN_BUFFER_INFO()
kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info))
width = info.dwSize.X
rows = []
read = ctypes.c_ulong(0)
for y in range(info.dwCursorPosition.Y + 1):
    buffer = ctypes.create_unicode_buffer(width)
    kernel32.ReadConsoleOutputCharacterW(
        handle, buffer, width, COORD(0, y), ctypes.byref(read)
    )
    rows.append(buffer[: read.value].rstrip())
target.write_text(
    json.dumps(
        {
            "code": code,
            "isatty": sys.stdout.isatty(),
            "encoding": sys.stdout.encoding,
            "console_output_code_page": kernel32.GetConsoleOutputCP(),
            "screen": "\n".join(rows),
            "joined": "".join(rows),
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
'''


@unittest.skipUnless(sys.platform == "win32", "the console path is Windows-only")
class ANativeWindowsConsoleRendersTheSameText(_SyntheticStoreCase):
    """A real console, not a pipe: what a Korean Windows terminal actually shows.

    A pipe and a file redirect prove the bytes; only an attached console proves
    the rendering, because that path goes through ``WriteConsoleW`` rather than
    through the byte stream. The child gets its own console in a hidden window,
    so nothing is displayed and no existing application, owner or terminal is
    disturbed.
    """

    def run_in_a_hidden_console(self, arguments: list[str]) -> dict[str, object]:
        target = self.home / "console.json"
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                CONSOLE_PROBE,
                str(REPOSITORY_ROOT),
                str(target),
                json.dumps(arguments),
            ],
            env=self.child_environment(),
            cwd=str(REPOSITORY_ROOT),
            startupinfo=startup,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            timeout=CHILD_TIMEOUT_SECONDS,
        )
        self.assertEqual(completed.returncode, 0)
        return json.loads(target.read_text(encoding="utf-8"))

    def test_the_console_shows_the_title_and_the_command_succeeds(self) -> None:
        probe = self.run_in_a_hidden_console(
            ["--data-dir", str(self.root), "backlog", "show", self.task_id]
        )
        self.assertTrue(probe["isatty"], probe)
        self.assertEqual(probe["code"], 0, probe)
        self.assertIn(CONSOLE_TEXT, probe["joined"], probe["screen"])
        # The screen buffer is the oracle only for BMP text: conhost stores a
        # non-BMP character as U+FFFD in its cells, so the emoji's fidelity is
        # settled by the pipe and file cases, not here.

    def test_a_console_refusal_is_legible_and_keeps_exit_two(self) -> None:
        probe = self.run_in_a_hidden_console(
            ["--data-dir", str(self.root), "backlog", "show", NON_ASCII_TITLE]
        )
        self.assertEqual(probe["code"], 2, probe)
        self.assertIn("error: ", probe["joined"], probe["screen"])
        self.assertIn(CONSOLE_TEXT, probe["joined"], probe["screen"])


if __name__ == "__main__":
    unittest.main()
