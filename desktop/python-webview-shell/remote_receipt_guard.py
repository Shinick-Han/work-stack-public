"""One advisory guard that serializes this product's receipt mutations.

Comparing receipt bytes and then unlinking by pathname is two operations, and a
new owner published between them is deleted by a decision made about older
bytes.  The filesystem offers no compare-and-swap to close that, so every
cooperating create, reclaim, stop and remove in this product instead takes this
one guard around the whole decide-and-act section: read, judge, signal, wait
and unlink all happen while no other cooperating worker can publish.

The guard is a separate long-lived file.  It is never the real writer lease,
which stays untouched, and its inode is never unlinked: unlinking a lock file
hands the next two holders two different inodes and therefore no exclusion at
all.  The descriptor is opened close-on-exec and released before any exec, so a
served process never inherits it.

The no-symlink guarantee is POSIX: ``O_NOFOLLOW`` refuses a guard path that
is a symlink.  Supported remote execution is Linux, where that flag exists.
The Windows adapter is test scaffolding for the advisory lock itself; it
does not claim an equivalent reparse-point or no-follow guarantee, and it
is not a supported remote serve surface.

The boundary is exactly this product's cooperating writers.  An advisory lock
does not stop an arbitrary same-user process, a hand-edit, or a legacy build
that writes the receipt without taking the guard; nothing available here can
fence those, and no claim is made that it does.

Acquisition is bounded.  A guard held by another worker is refused with an
actionable message rather than spun on forever, and the guard is taken once at
the top of a mutation: the private helpers underneath it never re-enter it.
"""

from __future__ import annotations

import errno
import os
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

try:  # POSIX, including the Linux payload this ships to.
    import fcntl
except ImportError:  # pragma: no cover - exercised by the Windows fixture path
    fcntl = None  # type: ignore[assignment]

try:  # Windows, where the focused tests run.
    import msvcrt
except ImportError:  # pragma: no cover - exercised by the POSIX path
    msvcrt = None  # type: ignore[assignment]


GUARD_FILENAME = ".workstack-remote-owner.lock"
GUARD_HELD_DETAIL = "another owner receipt writer is holding the guard"
# One byte at offset zero is the whole contended region; the file stays empty.
_GUARD_REGION_BYTES = 1


class GuardUnavailable(Exception):
    """The guard could not be taken, so no receipt mutation may proceed."""


class GuardContended(GuardUnavailable):
    """Another cooperating writer held the guard for the whole budget."""


@dataclass(frozen=True)
class GuardWait:
    """Bounded acquisition budget. Tests inject the clock."""

    timeout_seconds: float = 10.0
    poll_seconds: float = 0.02
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def guard_path(data_dir: Path) -> Path:
    return Path(data_dir) / GUARD_FILENAME


def _open_guard(path: Path) -> int:
    """Open the guard inode itself, refusing a symlink or a non-regular file."""

    flags = os.O_CREAT | os.O_RDWR
    # Python opens descriptors non-inheritable (PEP 446); O_CLOEXEC where it
    # exists makes that explicit for the exec that follows a serve.
    flags |= getattr(os, "O_CLOEXEC", 0)
    # O_NOFOLLOW is the supported-POSIX (Linux payload) guarantee.  On Windows
    # the flag is absent, so this adapter follows the path and only checks that
    # the opened target is a regular file; that is not equivalent hardening.
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except OSError as error:
        raise GuardUnavailable("the owner receipt guard could not be opened") from error
    try:
        status = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise GuardUnavailable("the owner receipt guard could not be inspected") from error
    if not stat.S_ISREG(status.st_mode):
        os.close(descriptor)
        raise GuardUnavailable("the owner receipt guard is not a regular file")
    return descriptor


def _try_lock(descriptor: int) -> bool:
    """Take the guard without blocking. False means another worker holds it."""

    if fcntl is not None:
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise GuardUnavailable("the owner receipt guard could not be locked") from error
        return True
    if msvcrt is not None:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, _GUARD_REGION_BYTES)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EDEADLOCK, errno.EAGAIN):
                return False
            raise GuardUnavailable("the owner receipt guard could not be locked") from error
        return True
    raise GuardUnavailable("no advisory file lock is available on this host")


def _unlock(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.lockf(descriptor, fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, _GUARD_REGION_BYTES)


@contextmanager
def owner_receipt_guard(data_dir: Path, wait: GuardWait = GuardWait()) -> Iterator[None]:
    """Hold the receipt guard for one whole decide-and-act section.

    Take this once at the top of a mutation. The helpers underneath assume it
    is already held and never re-enter it, because a second take of the same
    region is a deadlock on the platforms that do not count re-entry.
    """

    descriptor = _open_guard(guard_path(data_dir))
    try:
        deadline = wait.monotonic() + wait.timeout_seconds
        while not _try_lock(descriptor):
            if wait.monotonic() >= deadline:
                raise GuardContended(
                    f"{GUARD_HELD_DETAIL}; waited {wait.timeout_seconds:g}s"
                )
            wait.sleep(wait.poll_seconds)
        try:
            yield
        finally:
            _unlock(descriptor)
    finally:
        # The inode stays. Unlinking it would hand the next two holders two
        # different inodes and no exclusion between them.
        os.close(descriptor)
