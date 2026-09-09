"""One bounded stdin/stdout exchange with one already-created child process.

This module owns no transport, builds no command, and never finds a process.
It is handed exactly one process object that its caller created, and it only
ever writes to, reads from, kills, and reaps *that* object.  There is no
search by name or pid and no way to reach a process this exchange did not
receive.

The cleanup contract is the point of the module.  A bounded exchange can end
in three ways:

* the child exited on its own -- ``close`` settles the reader threads and the
  pipes and reports ``settled``;
* the exchange timed out or overflowed -- ``stop`` kills and reaps the child,
  joins the reader/writer threads under a bounded grace, and reports whether
  every handle actually settled;
* cleanup itself failed -- the kill was refused, the reap did not complete, or
  a reader thread was still alive after the grace.  That is reported as
  ``unsettled`` on the raised :class:`ExchangeError` instead of being
  swallowed, so the caller can say so rather than implying a clean stop.

Nothing here joins or closes without a bound.  A Windows pipe read cannot be
interrupted from another thread, so a reader that is still alive after the
grace deliberately keeps its stream open: closing a handle underneath a live
reader is worse than leaving it, and a daemon thread holds nothing open.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

#: Bound on every post-kill wait and thread join in this module.
SETTLE_GRACE_SECONDS = 5.0

CLEANUP_SETTLED = "settled"
CLEANUP_UNSETTLED = "unsettled"

#: Codes raised by this module.  ``NO_STDIN`` happens before any payload is
#: written; every other code can happen after the payload has left.
NOT_STARTED = "NOT_STARTED"
NO_STDIN = "NO_STDIN"
NO_OUTPUT = "NO_OUTPUT"
TIMEOUT = "TIMEOUT"
OVERSIZE = "OVERSIZE"


@dataclass(frozen=True)
class ExchangeOutput:
    """Bounded output of one completed exchange."""

    stdout: bytes
    stderr: bytes
    returncode: int


class ExchangeError(RuntimeError):
    """One bounded exchange failure, carrying what cleanup actually achieved."""

    def __init__(
        self, code: str, detail: str = "", cleanup: str = CLEANUP_SETTLED
    ) -> None:
        self.code = code
        self.detail = detail
        self.cleanup = cleanup
        super().__init__(f"{code}: {detail}" if detail else code)


class BoundedExchange:
    """Bounded IO with exactly the process object handed to this instance."""

    def __init__(self, process: object) -> None:
        self._process = process
        self._threads: list[tuple[str, threading.Thread]] = []
        self._failures: list[str] = []
        self._stopped = False

    @property
    def cleanup(self) -> str:
        """``settled`` only when every handle of this exchange was accounted for."""

        return CLEANUP_UNSETTLED if self._failures else CLEANUP_SETTLED

    @property
    def cleanup_failures(self) -> tuple[str, ...]:
        """Fixed reason words -- never process output, host names, or paths."""

        return tuple(self._failures)

    def _fail(self, reason: str) -> None:
        if reason not in self._failures:
            self._failures.append(reason)

    def _start(
        self, kind: str, name: str, target: Callable[[], None]
    ) -> threading.Thread:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append((kind, thread))
        thread.start()
        return thread

    def _kill(self) -> None:
        try:
            getattr(self._process, "kill")()
        except (OSError, subprocess.SubprocessError, TypeError, AttributeError):
            self._fail("kill")

    def _reap(self) -> None:
        try:
            getattr(self._process, "wait")(timeout=SETTLE_GRACE_SECONDS)
        except (
            subprocess.TimeoutExpired,
            OSError,
            subprocess.SubprocessError,
            TypeError,
            AttributeError,
        ):
            self._fail("reap")
            return
        if getattr(self._process, "returncode", None) is None:
            self._fail("reap")

    def _settle_threads(self) -> None:
        for kind, thread in self._threads:
            thread.join(SETTLE_GRACE_SECONDS)
            if thread.is_alive():
                self._fail(kind)

    def _close_streams(self) -> None:
        if any(thread.is_alive() for _kind, thread in self._threads):
            return
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(self._process, name, None)
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                self._fail("close")

    def stop(self) -> str:
        """Kill and reap the one process handed here, then settle its handles."""

        if not self._stopped:
            self._stopped = True
            self._kill()
            self._reap()
        self._settle_threads()
        self._close_streams()
        return self.cleanup

    def close(self) -> str:
        """Settle the handles of a child that already exited on its own."""

        self._settle_threads()
        self._close_streams()
        return self.cleanup

    def _stopped_error(self, code: str, detail: str) -> ExchangeError:
        return ExchangeError(code, detail, self.stop())

    def write(self, payload: bytes, timeout: float) -> None:
        """Write the whole payload and close stdin, or stop the exchange."""

        stream = getattr(self._process, "stdin", None)
        if stream is None:
            raise self._stopped_error(NO_STDIN, "the channel exposed no stdin")
        failed: list[str] = []

        def write_all() -> None:
            try:
                stream.write(payload)
            except (OSError, ValueError):
                failed.append("write")
            try:
                stream.close()
            except (OSError, ValueError):
                failed.append("close")

        thread = self._start("writer", "workstack-bounded-exchange-stdin", write_all)
        thread.join(timeout)
        if thread.is_alive() or failed:
            raise self._stopped_error(TIMEOUT, "the channel did not accept the payload")

    def _read(self, stream: object, limit: int, timeout: float) -> bytes:
        chunks: list[object] = []

        def read_bounded() -> None:
            try:
                chunks.append(getattr(stream, "read")(limit + 1))
            except (OSError, ValueError):
                chunks.append(b"")

        thread = self._start("reader", "workstack-bounded-exchange-read", read_bounded)
        thread.join(timeout)
        if thread.is_alive():
            raise self._stopped_error(TIMEOUT, "the channel produced no bounded result")
        payload = chunks[0] if chunks else b""
        return payload if isinstance(payload, bytes) else b""

    def _wait(self, timeout: float) -> int:
        try:
            getattr(self._process, "wait")(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise self._stopped_error(
                TIMEOUT, "the process did not exit in time"
            ) from None
        returncode = getattr(self._process, "returncode", 1)
        if not isinstance(returncode, int) or isinstance(returncode, bool):
            return 1
        return returncode

    def drain(
        self, stdout_limit: int, stderr_limit: int, timeout: float
    ) -> ExchangeOutput:
        """Read both bounded streams and reap the child inside one deadline."""

        stdout_stream = getattr(self._process, "stdout", None)
        stderr_stream = getattr(self._process, "stderr", None)
        if stdout_stream is None or stderr_stream is None:
            raise self._stopped_error(NO_OUTPUT, "the channel exposed no output")
        started = time.monotonic()
        stdout_payload = self._read(stdout_stream, stdout_limit, timeout)
        if len(stdout_payload) > stdout_limit:
            raise self._stopped_error(OVERSIZE, "the result exceeded the safe limit")
        remaining = max(0.1, timeout - (time.monotonic() - started))
        stderr_payload = self._read(stderr_stream, stderr_limit, remaining)
        remaining = max(0.1, timeout - (time.monotonic() - started))
        returncode = self._wait(remaining)
        self.close()
        return ExchangeOutput(stdout_payload, stderr_payload[:stderr_limit], returncode)


def start_exchange(
    process_factory: Callable[..., object], command: list[str]
) -> BoundedExchange:
    """Create one child on bounded pipes and own it from that moment on."""

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = process_factory(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
    except OSError:
        raise ExchangeError(NOT_STARTED, "the command could not be started") from None
    return BoundedExchange(process)


__all__ = [
    "BoundedExchange", "CLEANUP_SETTLED", "CLEANUP_UNSETTLED", "ExchangeError",
    "ExchangeOutput", "NOT_STARTED", "NO_OUTPUT", "NO_STDIN", "OVERSIZE",
    "SETTLE_GRACE_SECONDS", "TIMEOUT", "start_exchange",
]
