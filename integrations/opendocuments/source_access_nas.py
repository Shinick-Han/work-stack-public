"""The NAS half: reach the mapped file, bound the read, hash what was read.

Everything in this module is about one filesystem question at a time and about
keeping the four possible answers apart -- the file is gone, the OS refused, the
share did not answer, or the question could not be characterised at all. It
resolves the mapped target exactly once per verification and returns that very
path with a ``current`` answer, so
:mod:`integrations.opendocuments.source_access` can hand an opener the same
bytes it hashed instead of resolving a second time.

This is an internal module; the supported import site remains
:mod:`integrations.opendocuments.source_access`. The bounded-read limits live
here, beside the bounded reader that is the only thing which honours them.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import Path

from integrations.opendocuments.nas_paths import (
    COMPONENT_ACCESS_DENIED,
    COMPONENT_INDETERMINATE,
    MappedLocationError,
    resolve_mapped_target,
)
from integrations.opendocuments.source_access_types import SourceMapping, SourceStatus


# 64 MiB. A document larger than this is not refused as dangerous; it is simply
# more than this bounded reader will hash, so the honest answer is "unverified".
MAX_VERIFIED_BYTES = 64 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024


def _read_chunk(handle, size: int) -> bytes:
    """The single read call, isolated so a test can make a mid-read edit land."""

    return handle.read(size)


def _hash_permitted_file(target: Path) -> tuple[str | None, str]:
    """Hash the permitted file's actual bytes under a bound, or say why not.

    Returns ``(version, code)``. The whole file is never held: it is consumed a
    chunk at a time into the digest and each chunk is dropped, so no body is
    retained, persisted or logged anywhere in this module.
    """

    digest = hashlib.sha256()
    total = 0
    try:
        with open(target, "rb") as handle:
            before = os.fstat(handle.fileno())
            if before.st_size > MAX_VERIFIED_BYTES:
                return None, "file_too_large"
            while True:
                chunk = _read_chunk(handle, READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_VERIFIED_BYTES:
                    return None, "file_too_large"
                digest.update(chunk)
            after = os.fstat(handle.fileno())
    except FileNotFoundError:
        return None, "file_absent"
    except PermissionError:
        return None, "file_access_denied"
    except IsADirectoryError:
        return None, "not_a_regular_file"
    except OSError:
        # An unreadable file whose reason the OS did not distinguish is not an
        # absent file; the caller must not be told the document was deleted.
        return None, "indeterminate_access"
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None, "changed_during_read"
    if total != after.st_size:
        return None, "changed_during_read"
    return "sha256:" + digest.hexdigest(), "hash_matched"


def _nas_target(mapping: SourceMapping) -> tuple[Path | None, str | None]:
    assert mapping.allowed_root is not None
    assert mapping.relative_location is not None
    try:
        target = resolve_mapped_target(
            mapping.allowed_root,
            mapping.relative_location,
            mapping.permitted_extensions,
        )
    except MappedLocationError as error:
        return None, error.code
    return target, None


# Errno values that mean, specifically, *the share or host did not answer*.
# A disconnected mapped drive or a dead NAS lands here, and only here, is it
# honest to call the root unavailable on the strength of an ``OSError`` alone.
_ROOT_UNREACHABLE_ERRNOS = frozenset(
    getattr(errno, name)
    for name in (
        "ECONNABORTED",
        "ECONNREFUSED",
        "ECONNRESET",
        "EHOSTDOWN",
        "EHOSTUNREACH",
        "ENETDOWN",
        "ENETRESET",
        "ENETUNREACH",
        "ENODEV",
        "ENOTCONN",
        "ENXIO",
        "EREMOTEIO",
        "ESTALE",
        "ETIMEDOUT",
    )
    if hasattr(errno, name)
)


def _root_state(root: Path) -> str | None:
    """Separate "the share did not answer" from anything about a file in it.

    Four answers, never collapsed: absent, denied, unreachable, and *unknown*.
    The last one exists because an ``OSError`` the OS did not characterise is
    not evidence that a share is offline; asserting "unavailable" from it would
    tell a human to go check a network link that may be perfectly healthy.
    """

    try:
        entry = os.stat(root)
    except FileNotFoundError:
        return "root_unavailable"
    except NotADirectoryError:
        return "root_unavailable"
    except PermissionError:
        return "root_access_denied"
    except OSError as error:
        if error.errno in _ROOT_UNREACHABLE_ERRNOS:
            return "root_unavailable"
        # Undifferentiated. Fail closed, and say only what is true: the
        # question could not be answered.
        return "root_indeterminate"
    if not stat.S_ISDIR(entry.st_mode):
        return "root_unavailable"
    return None


def _location_outcome(code: str) -> tuple[str, str]:
    """Map one closed containment code onto the (status, code) it really means.

    A containment code is normally a policy refusal. Two of them are not: they
    report that the OS would not say whether a mapped ancestor is redirected.
    Those become ``denied`` and ``unverifiable`` -- the descent stopped either
    way, but the caller is told the reason the OS actually gave.
    """

    if code == COMPONENT_ACCESS_DENIED:
        return "denied", COMPONENT_ACCESS_DENIED
    if code == COMPONENT_INDETERMINATE:
        return "unverifiable", "indeterminate_access"
    return "refused", code


def _target_state(target: Path) -> str | None:
    """``None`` when the mapped target is a regular file, else the access code.

    A directory or a device sitting where the mapped document should be is not
    a document, and must not be opened or hashed as one.
    """

    try:
        entry = os.stat(target)
    except FileNotFoundError:
        return "file_absent"
    except NotADirectoryError:
        return "file_absent"
    except PermissionError:
        return "file_access_denied"
    except OSError:
        return "indeterminate_access"
    if not stat.S_ISREG(entry.st_mode):
        return "not_a_regular_file"
    return None


def _root_outcome(code: str) -> tuple[str, str]:
    """Map one root state onto the (status, code) a caller is honestly told.

    ``root_indeterminate`` is the one that is not a finding about the share: it
    says the OS did not characterise the failure, so the answer is
    ``unverifiable``, never "the share is offline".
    """

    if code == "root_unavailable":
        return "unavailable", code
    if code == "root_access_denied":
        return "denied", code
    return "unverifiable", "indeterminate_access"


def _access_status(code: str) -> str:
    """The status one access-failure code honestly reports.

    Stated once, because the same codes arrive from two places -- the ``stat``
    before the read and the bounded read itself -- and must mean the same thing
    in both. "I could not tell" and "it is gone" lead a human to opposite
    actions, so ``indeterminate_access``, ``file_too_large`` and
    ``changed_during_read`` all stay ``unverifiable`` and never become
    ``missing``.
    """

    if code == "file_absent":
        return "missing"
    if code == "file_access_denied":
        return "denied"
    if code == "not_a_regular_file":
        return "refused"
    return "unverifiable"


def _verify_nas(
    mapping: SourceMapping, expected: str | None, indexed_digest: str | None
) -> tuple[SourceStatus, Path | None]:
    """Verify one NAS mapping, and return the path the answer is *about*.

    The second element is the target that was actually resolved, stat-ed and
    hashed in this call, and it is returned only when the answer is ``current``.
    It exists so that :func:`authorize_open` can hand the opener that same path
    instead of resolving the mapping a second time: a second resolution would
    open a window in which a newly observed file could be paired with the
    version of the file observed before it. It is private and never reaches a
    public projection.
    """

    def result(status: str, code: str, version: str | None = None) -> SourceStatus:
        return SourceStatus(
            document_id=mapping.document_id,
            corpus=mapping.corpus,
            backend=mapping.backend,
            status=status,
            code=code,
            source_version=version,
            expected_source_version=expected,
            indexed_digest=indexed_digest,
        )

    assert mapping.allowed_root is not None
    root_code = _root_state(mapping.allowed_root)
    if root_code is not None:
        return result(*_root_outcome(root_code)), None

    target, refusal = _nas_target(mapping)
    if refusal is not None:
        return result(*_location_outcome(refusal)), None
    assert target is not None

    access_code = _target_state(target)
    if access_code is not None:
        return result(_access_status(access_code), access_code), None

    version, code = _hash_permitted_file(target)
    if version is None:
        # The file vanished, the OS refused, or the read could not be bounded.
        # Each keeps its own code, and none of them becomes the other.
        return result(_access_status(code), code), None
    if expected is None:
        # A hash with nothing to compare it to is a baseline, not a proof.
        return result("unverifiable", "no_expected_version", version), None
    if version == expected:
        # The comparison just made is about ``target``, and ``target`` is what
        # travels onward with it.
        return result("current", "hash_matched", version), target
    return result("stale", "hash_differs", version), None
