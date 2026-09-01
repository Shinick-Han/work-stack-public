"""Cross-platform exclusive writer lease for normalized storage runtimes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class StorageLeaseError(OSError):
    """A content-free refusal when another process owns the writer lease."""

    code = "WRITER_LEASE_HELD"


class StorageWriterLease:
    """Small non-blocking lease independent from the legacy v3 store adapter."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._file: BinaryIO | None = None

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
            _lock(handle)
        except (OSError, IOError) as error:
            handle.close()
            raise StorageLeaseError("WRITER_LEASE_HELD") from error
        self._file = handle

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            _unlock(self._file)
        finally:
            self._file.close()
            self._file = None


def _lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
