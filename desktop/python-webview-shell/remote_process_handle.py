"""Bind the stop signal to the process itself, never to a reusable pid number.

A pid is a name the kernel hands out again.  Reading ``/proc/<pid>`` and then
calling ``kill(pid)`` signals whoever holds that number at kill time, so a reuse
inside that window redirects the signal to an unrelated process and the later
"the recorded start identity is gone" check reads that redirection as success.
A Linux pidfd is a handle to the process itself and the kernel will not
redirect it, so pinning the pid, confirming the recorded start identity while
the pin is held, and then polling, signalling and waiting through that same
handle cannot reach a replacement.

Holding a pidfd does not stop the pid number being reused, and it is not meant
to: the handle keeps naming the process it was opened on, so a reuse either
side of the pin shows up as a start-identity mismatch and is refused instead of
being signalled.

Where ``os.pidfd_open`` or ``signal.pidfd_send_signal`` is missing, the kernel
is too old, or the operation is refused, this module refuses.  It never falls
back to ``kill(pid)`` after a checked pid, because that fallback is the exact
defect the handle exists to remove.  Refusing costs one confirmed stop, not the
process lifetime: the desktop still closes its own SSH channel and the served
process still dies with its session through the exec/PDEATHSIG binding.
"""

from __future__ import annotations

import errno
import os
import select
import signal
from typing import Protocol

# One /proc/<pid>/stat line, and the 0-based index of starttime among the
# fields that follow the parenthesised comm.
MAX_PROC_STAT_BYTES = 4096
PROC_STARTTIME_INDEX = 19

PIDFD_UNAVAILABLE_DETAIL = (
    "this kernel or interpreter has no pidfd process signalling, so a stop "
    "cannot be bound to the owned process and is refused"
)
# poll(2) bits. Named constants are absent on Windows, where a pidfd is never
# opened; the numeric values match Linux poll.h.
POLLIN = getattr(select, "POLLIN", 0x0001)
POLLPRI = getattr(select, "POLLPRI", 0x0002)
POLLERR = getattr(select, "POLLERR", 0x0008)
POLLHUP = getattr(select, "POLLHUP", 0x0010)
POLLNVAL = getattr(select, "POLLNVAL", 0x0020)

# The only two complete masks this product's kernel reports for a pidfd exit:
# POLLIN while the process is unreaped and POLLIN|POLLHUP once it is reaped.
# The comparison is against the whole mask, not a test for one bit, so POLLIN
# paired with POLLPRI, POLLERR, POLLNVAL or any bit a future kernel adds stays
# unknown instead of being read as a confirmed exit.
PIDFD_EXIT_MASKS = (POLLIN, POLLIN | POLLHUP)


class ProcessHandleUnavailable(Exception):
    """No handle can name this process here, so nothing may be signalled."""


def interpret_pidfd_poll(fd: int, ready: object) -> bool | None:
    """True only for a legitimate pidfd exit; None for errors or odd masks.

    A live pidfd is silent.  On this product's Linux target the kernel reports
    ``POLLIN`` when the pinned process has exited and not yet been reaped, and
    ``POLLIN|POLLHUP`` after reaping.  Those two complete masks are the whole
    accepted vocabulary: ``POLLERR``, ``POLLNVAL``, ``POLLHUP`` alone, a
    foreign fd, and ``POLLIN`` carrying any further bit are all unknown.

    The mask is compared whole rather than tested for ``POLLIN``, because an
    unrecognised bit means this kernel is saying something this interpreter was
    not written to read, and a stop that treats that as an exit deletes the
    ownership evidence for a process it never proved gone.
    """

    if not ready:
        return False
    if not isinstance(ready, (list, tuple)) or len(ready) != 1:
        return None
    event = ready[0]
    if not isinstance(event, (list, tuple)) or len(event) != 2:
        return None
    observed_fd, revents = event
    if observed_fd != fd or not isinstance(revents, int):
        return None
    if revents not in PIDFD_EXIT_MASKS:
        return None
    return True


def read_bounded_ascii(path: str, limit: int) -> str | None:
    """Read one small kernel or identity file, or None when it is unusable.

    Kernel files report no size, so the read is bounded by one extra byte and
    anything longer is treated as not this file rather than truncated.
    """

    try:
        with open(path, "rb") as stream:
            payload = stream.read(limit + 1)
    except OSError:
        return None
    if len(payload) > limit:
        return None
    try:
        return payload.decode("ascii").strip() or None
    except UnicodeError:
        return None


def read_start_identity(pid: int) -> str | None:
    """This pid's kernel starttime in clock ticks, a practical discriminator.

    starttime is not a cryptographic identity and is not collision-free across
    every possible reuse.  Combined with a pidfd taken first, it is enough to
    refuse the pid-reuse races this stop has to close: a later holder of the
    same pid number will not match the recorded ticks.

    The comm field is attacker-controlled and may hold spaces and brackets, so
    the fields are taken from the last ``)`` rather than by splitting the line.
    """

    text = read_bounded_ascii(f"/proc/{pid}/stat", MAX_PROC_STAT_BYTES)
    if text is None:
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 2 :].split()
    if len(fields) <= PROC_STARTTIME_INDEX or not fields[PROC_STARTTIME_INDEX].isdigit():
        return None
    return fields[PROC_STARTTIME_INDEX]


class OwnedProcessHandle(Protocol):
    """A handle already bound to one exact process, closed exactly once."""

    def has_exited(self) -> bool | None:
        ...

    def send_terminate(self) -> None:
        ...

    def close(self) -> None:
        ...


class PidfdOwnedProcess:
    """One pidfd: the poll, the signal and the wait all use this same handle."""

    def __init__(self, fd: int, pid: int) -> None:
        self._fd = fd
        self._pid = pid
        self._closed = False

    @property
    def pid(self) -> int:
        return self._pid

    def has_exited(self) -> bool | None:
        """True once this exact process is gone, None when it cannot be read.

        A pidfd becomes readable when its process leaves the running state.
        Only an exact ``POLLIN`` or ``POLLIN|POLLHUP`` is treated as exit.
        Poller creation, registration, ``POLLERR``, ``POLLNVAL``, and every
        other mask -- ``POLLHUP`` alone, or ``POLLIN`` beside an unrecognised
        bit -- are unknown.
        """

        if self._closed:
            return None
        try:
            poller = select.poll()
            poller.register(self._fd, POLLIN)
            ready = poller.poll(0)
        except (AttributeError, OSError):
            return None
        return interpret_pidfd_poll(self._fd, ready)

    def send_terminate(self) -> None:
        """Send one SIGTERM through the handle. Never escalates, never sweeps."""

        if self._closed:
            raise ProcessHandleUnavailable("the owned process handle is already closed")
        try:
            signal.pidfd_send_signal(self._fd, signal.SIGTERM)
        except ProcessLookupError:
            # Gone between the poll and the signal, which is the asked-for end.
            return
        except PermissionError as error:
            raise ProcessHandleUnavailable("this session cannot signal the owned process") from error
        except OSError as error:
            raise ProcessHandleUnavailable("the owned process signal failed") from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._fd)


def pidfd_signalling_available() -> bool:
    return hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")


def open_owned_process(pid: int, start_identity: str) -> PidfdOwnedProcess | None:
    """Pin this pid, then keep the pin only while /proc still shows this start.

    The pin is taken first so nothing afterwards can be redirected, and the
    identity is read with the pin already held.  A mismatch means the pid was
    reused on one side of the pin or the other; either way this session can no
    longer say which process the receipt named, so the pin is dropped and the
    caller is told rather than handed something to signal.

    None means the pid holds no process at all, which is the outcome a stop is
    asking for and is reported as such instead of as a failure.
    """

    if pid <= 0:
        raise ProcessHandleUnavailable("the owned pid is invalid")
    if pid == os.getpid():
        raise ProcessHandleUnavailable("refusing to signal the current process")
    if not pidfd_signalling_available():
        raise ProcessHandleUnavailable(PIDFD_UNAVAILABLE_DETAIL)
    try:
        fd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError as error:
        raise ProcessHandleUnavailable("this session cannot pin the owned process") from error
    except OSError as error:
        if error.errno in (errno.ENOSYS, errno.EINVAL):
            raise ProcessHandleUnavailable(PIDFD_UNAVAILABLE_DETAIL) from error
        raise ProcessHandleUnavailable("the owned pid could not be pinned") from error
    handle = PidfdOwnedProcess(fd, pid)
    observed = read_start_identity(pid)
    if observed is None or observed != start_identity:
        handle.close()
        raise ProcessHandleUnavailable(
            f"owned pid {pid} no longer carries its recorded start identity"
        )
    return handle
