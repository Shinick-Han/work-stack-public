"""Lexical and on-disk containment for one owner-mapped NAS location.

This module answers exactly one question: *given a canonical root the owner
approved and a relative location the owner recorded, which single absolute path
inside that root may be read?* It never accepts a location from the party that
asks for a document, and it resolves nothing that a caller, an index chunk or a
model wrote. Its only inputs are the trusted mapping fields held by
:mod:`integrations.opendocuments.source_access`.

The split from ``source_access`` is deliberate: containment on Windows is a
pile of platform detail -- drive-relative paths, UNC prefixes, alternate data
streams, reserved device names, trailing dots, junctions that are not symlinks
-- and keeping it beside the status machine would bury both. Nothing here
touches Work Stack core, and nothing here decides a *status*; it raises a closed
refusal code and lets the caller map it onto a result.

Two containment checks run, not one. The lexical check refuses a location whose
normalised form leaves the root, before any filesystem call. The resolved check
refuses one whose real path leaves the root after the OS has had its say. A
location must pass both, because either alone is defeatable: lexical analysis
cannot see a junction, and resolution alone would happily follow one out.

What this module does not claim: it is not a sandbox. It refuses the reparse
points and traversal shapes described above at the moment it looks, and a
same-OS-user process that can write inside the approved root can still swap a
component after the check. That boundary is stated in ``SOURCE-ACCESS.md`` and
is not papered over here.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePath

# Ordinary document types an approved share is expected to hold. Executables,
# scripts and shortcut/launcher formats are absent by construction, and
# ``ALWAYS_REFUSED_EXTENSIONS`` keeps them absent even if an owner config tries
# to add one back.
DEFAULT_PERMITTED_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".txt",
        ".md",
        ".csv",
        ".rtf",
        ".odt",
        ".ods",
        ".odp",
        ".hwp",
        ".hwpx",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
    }
)
# Anything that a double click would *execute* rather than *display*. This set
# outranks a configured allow-list: an owner may narrow the permitted set, never
# widen it into one of these.
ALWAYS_REFUSED_EXTENSIONS = frozenset(
    {
        ".appref-ms",
        ".bat",
        ".chm",
        ".cmd",
        ".com",
        ".cpl",
        ".dll",
        ".exe",
        ".hta",
        ".jar",
        ".js",
        ".jse",
        ".lnk",
        ".msi",
        ".msp",
        ".pif",
        ".ps1",
        ".psm1",
        ".py",
        ".reg",
        ".scf",
        ".scr",
        ".sh",
        ".url",
        ".vbe",
        ".vbs",
        ".website",
        ".wsf",
        ".wsh",
    }
)

MAX_RELATIVE_LOCATION_CHARS = 1024
MAX_LOCATION_SEGMENTS = 32

# Device names Windows resolves before it ever looks at the directory tree.
_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {"com{}".format(digit) for digit in range(1, 10)}
    | {"lpt{}".format(digit) for digit in range(1, 10)}
)
# Wildcards and the stream separator are refused wholesale rather than
# per-platform: a location that means two different files on two hosts is not a
# mapping, whichever host happens to be running.
_FORBIDDEN_CHARS = frozenset('<>:"|?*\0')
# ``\Z`` rather than ``$``: in Python ``$`` also matches immediately before one
# trailing newline, which would let ``"quarterly.pdf\n"`` pass as an ordinary
# segment name.
_SEGMENT_RE = re.compile(r"^[^<>:\"|?*\x00-\x1f]+\Z")

_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class MappedLocationError(ValueError):
    """A closed refusal: a code, never a path, a segment or an OS message."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# Every ``MappedLocationError.code`` this module can raise. ``source_access``
# re-exports the set so a caller can enumerate refusals without matching text.
LOCATION_REFUSAL_CODES = (
    "location_not_text",
    "location_empty",
    "location_too_long",
    "location_too_many_segments",
    "location_absolute",
    "location_drive_qualified",
    "location_forbidden_character",
    "location_traversal",
    "location_empty_segment",
    "location_reserved_name",
    "location_trailing_dot_or_space",
    "location_escapes_root",
    "extension_missing",
    "extension_not_permitted",
    "root_not_absolute",
    "root_not_text",
    "symlink_component",
    "reparse_component",
    "component_access_denied",
    "component_indeterminate",
    "not_a_regular_file",
)

# The two codes above are *not* refusals in the policy sense: they say a
# filesystem question -- "is this component redirected?", "where does this path
# really land?" -- could not be answered, and they say which of the two
# answerable reasons the OS gave. ``source_access`` maps them onto ``denied``
# and ``unverifiable`` respectively, because telling a human "this is a reparse
# point" or "this location escapes the approved root" when the OS actually said
# "access denied" is a false assertion, even though both stop the read.
COMPONENT_ACCESS_DENIED = "component_access_denied"
COMPONENT_INDETERMINATE = "component_indeterminate"
UNANSWERED_COMPONENT_CODES = (COMPONENT_ACCESS_DENIED, COMPONENT_INDETERMINATE)


def canonical_root(root: object) -> Path:
    """Resolve the owner-approved root once, and require it to be absolute.

    The root itself is trusted configuration, so its own reparse status is not
    a refusal -- an owner may legitimately approve a mapped drive or a junction
    that stands for the share. What matters is that everything *below* it stays
    below its resolved form, which is what this returns.
    """

    if isinstance(root, PurePath):
        candidate = Path(root)
    elif isinstance(root, str):
        candidate = Path(root)
    else:
        raise MappedLocationError("root_not_text")
    if not candidate.is_absolute():
        raise MappedLocationError("root_not_absolute")
    try:
        resolved = candidate.resolve()
    except OSError:
        # An unreachable root is not a malformed one; the caller distinguishes
        # "offline" from "misconfigured" and needs the lexical form to do it.
        return Path(os.path.normpath(str(candidate)))
    return resolved


def _check_location_text(location: object) -> str:
    """Refuse the whole location on its text, before it is ever split.

    ``:`` is refused anywhere, which removes three shapes at once: the absolute
    ``C:\\share\\x.pdf``, the drive-*relative* ``C:x.pdf`` that means "whatever
    the current directory on C: happens to be", and the NTFS alternate data
    stream ``report.pdf:hidden`` that names bytes the extension check never saw.
    """

    if not isinstance(location, str):
        raise MappedLocationError("location_not_text")
    if not location:
        raise MappedLocationError("location_empty")
    if len(location) > MAX_RELATIVE_LOCATION_CHARS:
        raise MappedLocationError("location_too_long")
    if ":" in location:
        raise MappedLocationError("location_drive_qualified")
    if location[0] in "/\\":
        # Covers both the absolute override "/etc/passwd" and the UNC prefix
        # "\\\\host\\share", which starts with the same separator.
        raise MappedLocationError("location_absolute")
    for character in location:
        if character in _FORBIDDEN_CHARS or ord(character) < 0x20:
            raise MappedLocationError("location_forbidden_character")
    return location


def _check_segment(segment: str) -> None:
    """Refuse one name segment, on the rules that apply to a name alone."""

    if segment == "":
        raise MappedLocationError("location_empty_segment")
    if segment in (".", ".."):
        raise MappedLocationError("location_traversal")
    if not _SEGMENT_RE.match(segment):
        raise MappedLocationError("location_forbidden_character")
    if segment[-1] in ". ":
        # Windows silently strips these, so "report.pdf ." and "report.pdf"
        # would be the same file under two different names.
        raise MappedLocationError("location_trailing_dot_or_space")
    stem = segment.split(".", 1)[0].lower()
    if stem in _RESERVED_STEMS:
        raise MappedLocationError("location_reserved_name")


def split_relative_location(location: object) -> tuple[str, ...]:
    """Refuse anything that is not a plain relative chain of name segments.

    The whole-text rules run first and the per-segment rules second, in exactly
    the order and under exactly the codes they always had; splitting them only
    separates "this is not a relative location" from "this is not a name".
    """

    text = _check_location_text(location)
    segments = tuple(part for part in re.split(r"[\\/]", text))
    if len(segments) > MAX_LOCATION_SEGMENTS:
        raise MappedLocationError("location_too_many_segments")
    for segment in segments:
        _check_segment(segment)
    return segments


def check_extension(
    segments: tuple[str, ...], permitted_extensions: frozenset[str]
) -> str:
    """Allow-list the final segment's extension, denying executables outright.

    The extension is read the way the OS reads it -- the text after the last dot
    -- so ``quarterly.pdf.exe`` is an ``.exe`` here, exactly as it is when
    double clicked.
    """

    extension = os.path.splitext(segments[-1])[1].lower()
    if not extension:
        raise MappedLocationError("extension_missing")
    if extension in ALWAYS_REFUSED_EXTENSIONS:
        raise MappedLocationError("extension_not_permitted")
    if extension not in permitted_extensions:
        raise MappedLocationError("extension_not_permitted")
    return extension


def lexical_target(root: Path, segments: tuple[str, ...]) -> Path:
    """Join under the root and require the normalised result to stay inside.

    The segment rules already forbid ``..``, so this is a second, independent
    statement of the same invariant rather than the only one -- it would catch a
    normalisation the segment rules did not anticipate.
    """

    root_text = os.path.normpath(str(root))
    joined = os.path.normpath(os.path.join(root_text, *segments))
    prefix = root_text if root_text.endswith(os.sep) else root_text + os.sep
    if not joined.startswith(prefix) or joined == root_text:
        raise MappedLocationError("location_escapes_root")
    return Path(joined)


def resolved_within(root: Path, candidate: Path) -> bool | str:
    """Ask the OS where the path really is, and require it to still be inside.

    ``True`` and ``False`` are both *answers*: the OS said where the root and
    the candidate really are, and the candidate is either inside the root or
    outside it. When the OS will not say, one of
    :data:`UNANSWERED_COMPONENT_CODES` is returned instead of a boolean, on the
    same terms as :func:`link_status` -- ``PermissionError`` is a denial the OS
    itself identified, any other ``OSError`` stays undifferentiated.

    Either resolution can go unanswered, and neither is treated as an escape.
    ``False`` asserts that containment was checked and failed, which is a
    statement about the owner's policy; "the share denied the query" and "the
    OS did not characterise the failure" are statements about the query. All
    three stop the read, so nothing is opened either way, but only the first is
    honest about *why*.
    """

    try:
        real_root = root.resolve()
        real_candidate = candidate.resolve()
    except PermissionError:
        return COMPONENT_ACCESS_DENIED
    except OSError:
        return COMPONENT_INDETERMINATE
    try:
        return real_candidate.is_relative_to(real_root)
    except ValueError:  # pragma: no cover - different drives raise on 3.9-3.11
        # Two resolved paths on different drives is an answered question: the
        # candidate is genuinely not under the root.
        return False


def link_status(path: Path) -> bool | str:
    """``True`` if this component is a link or reparse point, ``False`` if not.

    ``os.path.islink`` is not sufficient on Windows: a directory junction, which
    any unprivileged user can create, reports ``islink() == False`` while still
    redirecting the descent. The reparse attribute catches it.

    When the question cannot be answered at all, one of
    :data:`UNANSWERED_COMPONENT_CODES` is returned instead of a boolean. An
    unanswerable question must never be read as "no" -- but it must not be read
    as "yes, a reparse point" either. ``PermissionError`` is a denial the OS
    itself identified and keeps its own code; any other ``OSError`` stays
    undifferentiated. Both stop the descent; they differ only in what the caller
    is honestly told.
    """

    try:
        entry = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except PermissionError:
        return COMPONENT_ACCESS_DENIED
    except OSError:
        return COMPONENT_INDETERMINATE
    if stat.S_ISLNK(entry.st_mode):
        return True
    attributes = getattr(entry, "st_file_attributes", 0)
    if attributes & _REPARSE_ATTRIBUTE:
        return True
    return False


def check_descent(root: Path, segments: tuple[str, ...]) -> None:
    """Refuse a link or reparse point at any component *below* the root.

    Each prefix is examined, not only the leaf, because a redirected directory
    high in the chain moves every file beneath it. The root is skipped: it is
    the owner's own designation, and ``canonical_root`` has already replaced it
    with its resolved form.
    """

    current = root
    for segment in segments:
        current = current / segment
        redirected = link_status(current)
        if isinstance(redirected, str):
            # The OS did not answer. Fail closed under the code that says which
            # question went unanswered, not under an invented reparse finding.
            raise MappedLocationError(redirected)
        if redirected:
            raise MappedLocationError(
                "symlink_component"
                if os.path.islink(current)
                else "reparse_component"
            )


def resolve_mapped_target(
    root: Path, location: str, permitted_extensions: frozenset[str]
) -> Path:
    """The whole containment decision, in the order that keeps cost off disk.

    Text rules first, then the lexical join, then the extension, and only then
    the filesystem: a malformed location never becomes a syscall. The returned
    path is absolute and inside ``root``; whether it *exists* is a separate
    question the caller answers, because "absent" and "unreachable" are
    different answers.
    """

    segments = split_relative_location(location)
    check_extension(segments, permitted_extensions)
    target = lexical_target(root, segments)
    check_descent(root, segments)
    contained = resolved_within(root, target)
    if isinstance(contained, str):
        # The OS did not place the path. Fail closed under the code that says
        # which question went unanswered, never under a containment failure this
        # check never established.
        raise MappedLocationError(contained)
    if not contained:
        raise MappedLocationError("location_escapes_root")
    return target
