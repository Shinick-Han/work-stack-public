#!/usr/bin/env python3
"""Deterministic archive writer and member gate for the Linux remote artifact builder.

Writes the canonical ZIP the standalone Linux installer admits: every member a
regular file at 0644 under a Unix create system, dated at one frozen timestamp,
emitted in sorted roster order, with no directory, symlink or special-file entry
and every size bound re-checked while writing. Stdlib only; the builder passes
its own error factory so refusal codes stay in one taxonomy.
"""

from __future__ import annotations

import hashlib
import os
import stat
import zipfile
from pathlib import Path
from typing import Any, Callable


ErrorFactory = Callable[[str, str], BaseException]

ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)
FILE_MODE = 0o644
# The installer admits a member only when its whole external mode is a regular
# file at 0644, so the builder writes the file type bit rather than bare 0644.
ZIP_MEMBER_MODE = stat.S_IFREG | FILE_MODE
ZIP_CREATE_SYSTEM = 3
ZIP_ALLOWED_FLAGS = 0x800
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILE_COUNT = 4096
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PATH_BYTES = 240
BYTECODE_SUFFIXES = {".pyc", ".pyo"}


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def refuse_special_file(path: Path, label: str, error: ErrorFactory) -> None:
    try:
        mode = os.lstat(path).st_mode
    except OSError as failure:
        raise error("NONREGULAR", label + " path is missing") from failure
    if stat.S_ISLNK(mode):
        raise error("SYMLINK", label + " path is a symlink")
    if not stat.S_ISREG(mode):
        raise error("NONREGULAR", label + " path is not a regular file")
    if path.suffix.lower() in BYTECODE_SUFFIXES:
        raise error("SOURCE_BYTECODE", str(path))


def posix_relative(root: Path, path: Path, error: ErrorFactory) -> str:
    relative = path.relative_to(root).as_posix()
    encoded = relative.encode("utf-8")
    if "\\" in relative:
        raise error("BACKSLASH", relative)
    if ".." in Path(relative).parts or relative.startswith("/"):
        raise error("TRAVERSAL", relative)
    if len(encoded) > MAX_PATH_BYTES:
        raise error("BOUNDS", "path exceeds 240 UTF-8 bytes")
    return relative


def validate_zip_path(raw: str, error: ErrorFactory) -> tuple[str, bool]:
    if "\\" in raw:
        raise error("BACKSLASH", raw)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise error("TRAVERSAL", "control character in zip path")
    if raw.startswith("/"):
        raise error("TRAVERSAL", raw)
    is_dir = raw.endswith("/")
    body = raw[:-1] if is_dir else raw
    if not body or any(part in ("", ".", "..") for part in body.split("/")):
        raise error("TRAVERSAL", raw)
    return body, is_dir


def refuse_zip_type(info: zipfile.ZipInfo, is_dir: bool, error: ErrorFactory) -> None:
    if getattr(info, "create_system", 0) != ZIP_CREATE_SYSTEM:
        return
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise error("SYMLINK", info.filename)
    allowed = (0, 0o040000) if is_dir else (0, 0o100000)
    if mode not in allowed:
        raise error("NONREGULAR", info.filename)


def refuse_zip_member(info: zipfile.ZipInfo, seen: set[str], error: ErrorFactory) -> int:
    name, is_dir = validate_zip_path(info.filename, error)
    refuse_zip_type(info, is_dir, error)
    if any(part.endswith(".data") for part in name.split("/")):
        raise error("DATA_SCRIPTS", name)
    if is_dir:
        return 0
    if name in seen:
        raise error("DUPLICATE_PATH", name)
    seen.add(name)
    if Path(name).suffix.lower() in BYTECODE_SUFFIXES:
        raise error("SOURCE_BYTECODE", name)
    if info.file_size > MAX_FILE_BYTES:
        raise error("BOUNDS", name + " exceeds 32 MiB")
    return info.file_size


def preflight_zip_members(archive: zipfile.ZipFile, label: str, error: ErrorFactory) -> None:
    seen: set[str] = set()
    total = 0
    names = archive.infolist()
    if len(names) > MAX_FILE_COUNT:
        raise error("BOUNDS", label + " has too many members")
    for info in names:
        total += refuse_zip_member(info, seen, error)
        if total > MAX_UNCOMPRESSED_BYTES:
            raise error("BOUNDS", label + " uncompressed size exceeds 256 MiB")


def staging_members(staging: Path, error: ErrorFactory) -> list[str]:
    """List every non-directory staging entry, so a stray file cannot hide."""

    names: list[str] = []
    for path in sorted(staging.rglob("*")):
        if stat.S_ISDIR(os.lstat(path).st_mode):
            continue
        names.append(posix_relative(staging, path, error))
    names.sort()
    return names


def archive_roster(staging: Path, files: list[dict[str, Any]], error: ErrorFactory) -> list[str]:
    """Derive the sorted archive roster and bind it to the staging tree."""

    roster = ["artifact.json"] + ["payload/" + str(item["path"]) for item in files]
    for name in roster:
        body, is_dir = validate_zip_path(name, error)
        if is_dir or body != name:
            raise error("VERIFY", "archive roster member is not a file path")
        if len(name.encode("utf-8")) > MAX_PATH_BYTES:
            raise error("BOUNDS", "path exceeds 240 UTF-8 bytes")
    roster.sort()
    if len(set(roster)) != len(roster):
        raise error("DUPLICATE_PATH", "archive roster repeats a member")
    if len(roster) > MAX_FILE_COUNT:
        raise error("BOUNDS", "archive has too many members")
    if roster != staging_members(staging, error):
        raise error("VERIFY", "staging tree does not match the archive roster")
    return roster


def create_canonical_zip(staging: Path, archive: Path, files: list[dict[str, Any]], error: ErrorFactory) -> None:
    """Write the deterministic archive the Linux installer admits."""

    roster = archive_roster(staging, files, error)
    total = 0
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name in roster:
            source = staging.joinpath(*name.split("/"))
            refuse_special_file(source, name, error)
            data = source.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                raise error("BOUNDS", name + " exceeds 32 MiB")
            total += len(data)
            if total > MAX_UNCOMPRESSED_BYTES:
                raise error("BOUNDS", "archive uncompressed size exceeds 256 MiB")
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.create_system = ZIP_CREATE_SYSTEM
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ZIP_MEMBER_MODE << 16
            output.writestr(info, data, compresslevel=9)


def require_plain_eocd(archive: Path, error: ErrorFactory) -> None:
    """Refuse an archive the installer would reject before it reads a member."""

    if archive.stat().st_size < 22:
        raise error("VERIFY", "archive is truncated")
    with archive.open("rb") as handle:
        head = handle.read(2)
        handle.seek(-22, os.SEEK_END)
        tail = handle.read(22)
    if head != b"PK" or tail[:4] != b"PK\x05\x06" or int.from_bytes(tail[-2:], "little") != 0:
        raise error("VERIFY", "archive does not end in a plain comment-free EOCD record")


def verify_member_shape(info: zipfile.ZipInfo, error: ErrorFactory) -> None:
    """Re-read one written member through the gates the installer applies."""

    if tuple(info.date_time) != ZIP_TIMESTAMP:
        raise error("VERIFY", "archive timestamp is not frozen")
    if getattr(info, "create_system", 0) != ZIP_CREATE_SYSTEM:
        raise error("VERIFY", "archive member is not written under the Unix create system")
    if info.external_attr >> 16 != ZIP_MEMBER_MODE:
        raise error("VERIFY", "archive mode is not a regular file at 0644")
    if info.extra or info.flag_bits & ~ZIP_ALLOWED_FLAGS:
        raise error("VERIFY", "archive member carries an extra field or an unsupported flag")
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise error("VERIFY", "archive member uses an unsupported compressor")


def verify_archive(archive: Path, staging: Path, files: list[dict[str, Any]], error: ErrorFactory) -> None:
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise error("BOUNDS", "archive exceeds 64 MiB")
    require_plain_eocd(archive, error)
    expected = ["artifact.json"] + ["payload/" + item["path"] for item in files]
    expected.sort()
    with zipfile.ZipFile(archive) as handle:
        preflight_zip_members(handle, archive.name, error)
        names = sorted(name.replace("\\", "/") for name in handle.namelist() if not name.endswith("/"))
        if names != expected:
            raise error("VERIFY", "archive member roster does not match the payload")
        for info in handle.infolist():
            if info.filename.endswith("/"):
                continue
            verify_member_shape(info, error)
            data = handle.read(info)
            relative = info.filename.replace("\\", "/")
            source = staging / Path(*relative.split("/"))
            if sha256_bytes(data) != sha256_bytes(source.read_bytes()) or len(data) != source.stat().st_size:
                raise error("VERIFY", "archive member hash does not match staging")


def write_archive(staging: Path, archive: Path, files: list[dict[str, Any]], error: ErrorFactory) -> None:
    create_canonical_zip(staging, archive, files, error)
    verify_archive(archive, staging, files, error)
