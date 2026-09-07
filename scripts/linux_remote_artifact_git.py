#!/usr/bin/env python3
"""Binary-safe frozen Git tree/blob reader for the Linux remote artifact builder.

Reads tracked payload and lock bytes from commit objects with git cat-file.
Does not transform newlines, follow worktree checkout, or resolve submodules.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Iterable


COMMIT_HEX = re.compile(r"\A[0-9a-f]{40}\Z")
REGULAR_MODES = frozenset({"100644", "100755"})
ErrorFactory = Callable[[str, str], BaseException]


def git_bytes(source: Path, arguments: tuple[str, ...], error: ErrorFactory, stdin: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=str(source),
            check=False,
            capture_output=True,
            input=stdin,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise error("SOURCE_UNPINNED", str(exc)) from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip() or "git failed"
        raise error("SOURCE_UNPINNED", detail)
    return completed.stdout


def git_text(source: Path, arguments: tuple[str, ...], error: ErrorFactory) -> str:
    return git_bytes(source, arguments, error).decode("utf-8").strip()


def read_identity(source: Path, error: ErrorFactory) -> tuple[str, str]:
    if not (source / ".git").exists():
        raise error("SOURCE_UNPINNED", "source root is not a git checkout")
    commit = git_text(source, ("rev-parse", "HEAD"), error).lower()
    tree = git_text(source, ("rev-parse", "HEAD^{tree}"), error).lower()
    if COMMIT_HEX.match(commit) is None or COMMIT_HEX.match(tree) is None:
        raise error("SOURCE_UNPINNED", "HEAD is not a pinned 40-hex commit")
    return commit, tree


def require_identity(source: Path, commit: str, tree: str, error: ErrorFactory) -> None:
    found_commit, found_tree = read_identity(source, error)
    if found_commit != commit or found_tree != tree:
        raise error("SOURCE_UNPINNED", "HEAD commit or tree moved during the build")


def _split_tree_line(item: bytes, error: ErrorFactory) -> tuple[str, str, str, str]:
    try:
        meta, path_b = item.split(b"\t", 1)
        mode, kind, sha = meta.decode("ascii").split(" ", 2)
        path = path_b.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise error("TRAVERSAL", "git ls-tree entry is not parseable") from exc
    return mode, kind, sha, path


def _illegal_path(path: str) -> bool:
    parts = path.split("/")
    return any((not path, path == ".", "\\" in path, path.startswith("/"), ".." in parts))


def _refuse_entry(mode: str, kind: str, sha: str, path: str, error: ErrorFactory) -> None:
    if _illegal_path(path) or COMMIT_HEX.match(sha) is None:
        raise error("TRAVERSAL", path or "empty git path")
    if mode == "120000":
        raise error("SYMLINK", path)
    if mode == "160000" or kind == "commit":
        raise error("NONREGULAR", path + " is a git submodule")
    if kind != "blob" or mode not in REGULAR_MODES:
        raise error("NONREGULAR", path)


def list_tree_entries(
    source: Path,
    commit: str,
    roots: Iterable[str],
    error: ErrorFactory,
) -> list[tuple[str, str, str]]:
    if COMMIT_HEX.match(commit) is None:
        raise error("SOURCE_UNPINNED", "commit is not a pinned 40-hex object")
    prefixes = tuple(roots)
    raw = git_bytes(source, ("ls-tree", "-r", "-z", commit, "--", *prefixes), error)
    entries: list[tuple[str, str, str]] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        mode, kind, sha, path = _split_tree_line(item, error)
        _refuse_entry(mode, kind, sha, path, error)
        entries.append((mode, sha, path))
    return entries


def _parse_batch(data: bytes, entries: list[tuple[str, str, str]], error: ErrorFactory) -> list[tuple[str, bytes]]:
    offset = 0
    blobs: list[tuple[str, bytes]] = []
    for _mode, sha, path in entries:
        newline = data.find(b"\n", offset)
        if newline < 0:
            raise error("SOURCE_UNPINNED", "git cat-file batch truncated")
        header = data[offset:newline].decode("ascii", "replace").split(" ")
        if len(header) != 3 or header[0] != sha or header[1] != "blob":
            raise error("SOURCE_UNPINNED", "git cat-file batch header mismatch for " + path)
        size = int(header[2])
        start = newline + 1
        blob = data[start : start + size]
        trailer = start + size
        if len(blob) != size or trailer >= len(data) or data[trailer] != 10:
            raise error("SOURCE_UNPINNED", "git cat-file batch short read for " + path)
        offset = trailer + 1
        blobs.append((path, blob))
    if offset != len(data):
        raise error("SOURCE_UNPINNED", "git cat-file batch has trailing data")
    return blobs


def read_blobs(source: Path, entries: list[tuple[str, str, str]], error: ErrorFactory) -> list[tuple[str, bytes]]:
    if not entries:
        return []
    payload = b"".join(sha.encode("ascii") + b"\n" for _mode, sha, _path in entries)
    data = git_bytes(source, ("cat-file", "--batch"), error, stdin=payload)
    return _parse_batch(data, entries, error)


def tracked_regular_blobs(
    source: Path,
    commit: str,
    roots: Iterable[str],
    error: ErrorFactory,
) -> list[tuple[str, bytes]]:
    return read_blobs(source, list_tree_entries(source, commit, roots, error), error)


def require_roster(
    paths: Iterable[str],
    files: Iterable[str],
    directories: Iterable[str],
    error: ErrorFactory,
    label: str,
) -> None:
    """Admit every required file by name; directory enumeration is not proof."""
    present = set(paths)
    missing = sorted(name for name in files if name not in present)
    missing += sorted(
        root for root in directories if not any(item.startswith(root + "/") for item in present)
    )
    if missing:
        raise error("ROSTER_MISSING", label + " is missing required " + ",".join(missing))


def payload_and_lock(
    blobs: list[tuple[str, bytes]],
    lock_name: str,
    error: ErrorFactory,
) -> tuple[bytes, list[tuple[str, bytes]]]:
    by_path: dict[str, bytes] = {}
    for path, data in blobs:
        if path in by_path:
            raise error("DUPLICATE_PATH", path)
        by_path[path] = data
    lock = by_path.pop(lock_name, None)
    if lock is None:
        raise error("SOURCE_DIRTY", lock_name + " is missing from the frozen commit")
    return lock, sorted(by_path.items())


def frozen_payload_and_lock(
    source: Path,
    commit: str,
    roots: Iterable[str],
    lock_name: str,
    error: ErrorFactory,
) -> tuple[bytes, list[tuple[str, bytes]]]:
    return payload_and_lock(tracked_regular_blobs(source, commit, roots, error), lock_name, error)
