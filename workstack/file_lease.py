"""Cross-platform exclusive writer lease for the data-directory lock file."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class StoreLockedError(OSError):
    """Raised when another process owns the data-directory writer lease."""


class _FileLease:
    """Small non-blocking cross-platform exclusive file lease."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            self._take_platform_lock(handle)
        except (OSError, IOError) as error:
            handle.close()
            raise StoreLockedError(
                "the Work Stack data directory is already owned by another writer"
            ) from error
        self.file = handle

    def acquire_existing(self) -> None:
        """Take the same lease on a lock file that must ALREADY exist.

        The one difference from :meth:`acquire` is that this creates nothing:
        no parent ``mkdir``, no ``O_CREAT``, no sentinel byte, no write and no
        fsync.  A lock file that is absent -- or that vanishes in the window
        between a caller's check and this open -- raises the underlying
        ``OSError`` rather than being brought into being, so a read-only
        observer can ask whether the writer lease is free without making the
        very file it is asking about.

        It is the *same* lease, not a second protocol: the identical
        non-blocking exclusive platform lock on the identical first byte, so
        it interlocks with every :meth:`acquire` holder and is refused with
        the same :class:`StoreLockedError` while one holds it.  Only
        contention becomes ``StoreLockedError``; every other failure keeps its
        own ``OSError``, so a caller can tell "another writer holds this" from
        "this could not be opened at all" instead of reading one as the other.

        What an absent, empty or replaced lock file *means* is the caller's
        decision.  This method only refuses to invent one.
        """

        descriptor = os.open(self.path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            handle = os.fdopen(descriptor, "r+b")
        except BaseException:
            os.close(descriptor)
            raise
        try:
            self._take_platform_lock(handle)
        except (OSError, IOError) as error:
            handle.close()
            raise StoreLockedError(
                "the Work Stack data directory is already owned by another writer"
            ) from error
        self.file = handle

    def _take_platform_lock(self, handle: Any) -> None:
        """The single non-blocking exclusive lock both acquisitions take."""

        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self.file is None:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None
