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
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as error:
            handle.close()
            raise StoreLockedError(
                "the Work Stack data directory is already owned by another writer"
            ) from error
        self.file = handle

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
