"""Atomic exclusive publication of one owner-receipt byte payload.

The destination pathname is never opened for writing.  Encoded bytes go to a
uniquely named 0600 temporary file in the same directory, are flushed and
fsynced, and only then become the destination through an atomic no-overwrite
hard link.  An existing receipt is therefore never replaced, a short write
cannot become permanent evidence, and a crash before the link can leave only
an inert temp file.

There is no fallback to rename-over or to a partial write of the destination.
A filesystem that cannot hard-link in this directory is refused.  The caller
already holds the receipt guard; this helper does not take it, does not touch
the writer lease, and removes only the temp path it created.  There is no
temp-directory sweep.

Cleanup never repeats a close whose outcome is already uncertain, always
attempts the removal of its own temp file, and never lets a cleanup error
replace the publication failure the caller has to act on.
"""

from __future__ import annotations

import errno
import os
import secrets
from pathlib import Path

_TEMP_ATTEMPTS = 8
PUBLICATION_UNSUPPORTED = (
    "this filesystem cannot publish an owner receipt without replacing or "
    "partially writing the destination"
)


class ReceiptPublicationUnavailable(Exception):
    """The payload could not become the destination without a partial write."""


class ReceiptAlreadyExists(ReceiptPublicationUnavailable):
    """The destination already names a receipt; it was not replaced."""


def _unlink_own_temp(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _close_surrendered(fd: int) -> None:
    """Close a descriptor its owner has already given up, once and quietly.

    Cleanup runs while a publication failure is already being reported, and a
    close error here says nothing about the destination, which no code path
    ever opened.  Letting it out would replace the stable publication error
    with a raw ``OSError`` and skip the removal of this helper's own temp file.
    """

    try:
        os.close(fd)
    except OSError:
        return


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    sent = 0
    try:
        while sent < len(view):
            written = os.write(fd, view[sent:])
            if written <= 0:
                raise ReceiptPublicationUnavailable("owner receipt write did not complete")
            sent += written
    except OSError as error:
        raise ReceiptPublicationUnavailable("owner receipt write did not complete") from error


def _create_temp(destination: Path) -> tuple[int, Path]:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # Windows test scaffolding: without O_BINARY the kernel translates LF, so a
    # later exact-byte cleanup would miss the file it had just published.
    flags |= getattr(os, "O_BINARY", 0)
    last_error: OSError | None = None
    for _ in range(_TEMP_ATTEMPTS):
        path = destination.with_name(
            f"{destination.name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"
        )
        try:
            return os.open(str(path), flags, 0o600), path
        except FileExistsError as error:
            last_error = error
            continue
        except OSError as error:
            raise ReceiptPublicationUnavailable(
                "owner receipt temp file could not be created"
            ) from error
    raise ReceiptPublicationUnavailable(
        "owner receipt temp file could not be created"
    ) from last_error


def _publish_link(source: Path, destination: Path) -> None:
    link = getattr(os, "link", None)
    if link is None:
        raise ReceiptPublicationUnavailable(PUBLICATION_UNSUPPORTED)
    try:
        link(str(source), str(destination))
    except FileExistsError as error:
        raise ReceiptAlreadyExists("pid=unknown") from error
    except OSError as error:
        if error.errno == errno.EEXIST:
            raise ReceiptAlreadyExists("pid=unknown") from error
        raise ReceiptPublicationUnavailable(PUBLICATION_UNSUPPORTED) from error


def publish_bytes_exclusive(destination: Path, payload: bytes) -> None:
    """Write ``payload`` as a new destination, or leave that path unchanged."""

    fd: int | None = None
    temp_path: Path | None = None
    try:
        fd, temp_path = _create_temp(destination)
        _write_all(fd, payload)
        os.fsync(fd)
        # The descriptor is surrendered before the close is attempted, so a
        # close that fails is reported once and never closed again: after a
        # failed close the descriptor is gone anyway and the number can already
        # have been handed to something else in this process.
        closing, fd = fd, None
        os.close(closing)
        _publish_link(temp_path, destination)
    except ReceiptPublicationUnavailable:
        raise
    except OSError as error:
        raise ReceiptPublicationUnavailable("owner receipt could not be published") from error
    finally:
        # Only a descriptor whose close was never attempted is still held here.
        if fd is not None:
            pending, fd = fd, None
            _close_surrendered(pending)
        if temp_path is not None:
            _unlink_own_temp(temp_path)
