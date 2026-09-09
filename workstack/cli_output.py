"""One UTF-8 output boundary for the whole ``work-stack`` command line.

Work Stack stdout is machine JSON and stderr is a one-line diagnostic, so the
bytes on both are a property of the contract, not of whichever code page the
host console happens to advertise. On a Korean Windows shell the inherited
encoding is cp949, and ``print(json.dumps(..., ensure_ascii=False))`` raised
``UnicodeEncodeError`` for an em dash or an emoji in a Task title. Because
``UnicodeEncodeError`` is a ``ValueError``, ``cli.main`` reported it as an
ordinary refusal: exit 2, empty stdout, and a diagnostic about a codec rather
than about the command -- even when the SSOT write had already committed.

The encoding is therefore applied once, at the single place the command line
owns its streams, instead of at every command that prints. ``utf8_streams``
installs a view over ``sys.stdout``/``sys.stderr`` that encodes text itself and
hands the bytes to the stream's existing binary buffer, so:

* every writer -- ``emit``, the ``main`` error lines, argparse's own usage and
  refusal messages -- emits UTF-8 without a per-command encode hack;
* the inherited stream is never ``reconfigure``d and is never detached or
  closed, so its own ``encoding`` keeps reporting what the host chose and the
  process environment and the user's shell are untouched;
* a stream with no binary buffer -- a ``StringIO`` in a test, a capture double
  -- is left exactly as it was, and text written to it stays text;
* a stream that already encodes as UTF-8 is left exactly as it was too, so a
  host whose stdio is already UTF-8 -- every ordinary POSIX shell, a Windows
  console, ``PYTHONUTF8=1`` -- keeps byte-for-byte the behaviour it has today;
* and nothing at all is installed off Windows. The defect is a Windows host
  code page, and non-Windows callers -- direct unit invocations and redirects
  included -- keep whatever encoding and newline behaviour they inherited,
  including a non-UTF-8 locale and the strict ``UnicodeEncodeError`` it raises.

Writing the bytes directly also bypasses the Windows text layer's newline
translation, so a redirected JSON document is terminated by one LF. That is the
canonical form ``agent_runtime`` already emits for agent envelopes.
"""

from __future__ import annotations

import codecs
import contextlib
import sys
import typing
from collections.abc import Iterable, Iterator

ENCODING = "utf-8"


class Utf8Stream:
    """A text view of one stream that encodes as UTF-8 rather than as cp949.

    Only the text layer is replaced. Reads, ``isatty``, ``fileno`` and the
    binary ``buffer`` itself keep coming from the wrapped stream, so a caller
    that already writes canonical bytes -- ``agent_runtime`` does -- reaches the
    same buffer this view writes to and the two orderings stay consistent.
    """

    def __init__(self, stream: typing.TextIO, binary: typing.BinaryIO) -> None:
        self._stream = stream
        self._binary = binary

    def write(self, text: str) -> int:
        self._binary.write(text.encode(ENCODING))
        self._binary.flush()
        return len(text)

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._binary.flush()

    @property
    def encoding(self) -> str:
        return ENCODING

    @property
    def errors(self) -> str:
        return "strict"

    def __getattr__(self, name: str) -> typing.Any:
        return getattr(self._stream, name)


def runs_on_windows() -> bool:
    """Report whether this process is on the platform the boundary is for.

    The check is deliberately on the platform rather than on which codecs are
    common there. The oracle is literal -- inherited encoding behaviour must not
    change on non-Windows -- so a POSIX host with a cp949 or latin-1 locale has
    to keep its inherited encoding, its newlines and its
    ``UnicodeEncodeError``, exactly as it did before this boundary existed.
    """

    return sys.platform == "win32"


def encodes_as_utf8(stream: object) -> bool:
    """Report whether the stream already encodes text the way the contract needs."""

    name = getattr(stream, "encoding", None)
    if not isinstance(name, str):
        return False
    try:
        return codecs.lookup(name).name == "utf-8"
    except (LookupError, ValueError):
        return False


def utf8_view(stream: object) -> Utf8Stream | None:
    """Return a UTF-8 view of ``stream``, or ``None`` to leave it untouched.

    A stream is only wrapped on Windows, and there only when its inherited
    codec would corrupt or refuse the payload *and* it owns a writable binary
    buffer to carry the bytes instead. Everything else -- any stream on a
    non-Windows host, an already-UTF-8 stream, a ``StringIO``, a test double, a
    stream whose buffer is detached or closed -- keeps exactly the behaviour it
    has today, because there the encoding is not the defect this packet fixes
    and forcing bytes would change what the caller collects rather than what a
    console renders.
    """

    if not runs_on_windows():
        return None
    if encodes_as_utf8(stream):
        return None
    binary = getattr(stream, "buffer", None)
    if binary is None or not callable(getattr(binary, "write", None)):
        return None
    if getattr(binary, "closed", False):
        return None
    try:
        flush = getattr(stream, "flush", None)
        if callable(flush):
            flush()
    except (OSError, ValueError):
        return None
    return Utf8Stream(typing.cast(typing.TextIO, stream), typing.cast(typing.BinaryIO, binary))


@contextlib.contextmanager
def utf8_streams() -> Iterator[None]:
    """Emit ``sys.stdout``/``sys.stderr`` as UTF-8 for the duration of the block.

    The originals are restored on the way out, including when the block raises
    or argparse exits, and a stream the block deliberately replaced is left
    alone rather than reset underneath it.
    """

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    stdout_view = utf8_view(original_stdout)
    stderr_view = utf8_view(original_stderr)
    if stdout_view is not None:
        sys.stdout = typing.cast(typing.TextIO, stdout_view)
    if stderr_view is not None:
        sys.stderr = typing.cast(typing.TextIO, stderr_view)
    try:
        yield
    finally:
        for view, original, name in (
            (stderr_view, original_stderr, "stderr"),
            (stdout_view, original_stdout, "stdout"),
        ):
            if view is None:
                continue
            with contextlib.suppress(OSError, ValueError):
                view.flush()
            if getattr(sys, name) is view:
                setattr(sys, name, original)


__all__ = (
    "ENCODING",
    "Utf8Stream",
    "encodes_as_utf8",
    "runs_on_windows",
    "utf8_streams",
    "utf8_view",
)
