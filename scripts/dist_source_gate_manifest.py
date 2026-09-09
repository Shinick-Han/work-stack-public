#!/usr/bin/env python3
"""Input roster, content-addressed capture, dist-tree walking, and containment.

Stdlib only. Digests are computed from the same read that produces a captured
copy. Loaded as a sibling of ``dist_source_gate.py`` so file-path callers keep
working without a package import. Receipt identity types come from
``dist_source_gate_receipt``; this module does not read or write a receipt.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping


def _load_receipt() -> Any:
    name = "workstack_dist_source_gate_receipt"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / "dist_source_gate_receipt.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("dist_source_gate_receipt.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_RECEIPT = _load_receipt()
DistSourceGateError = _RECEIPT.DistSourceGateError
DIST_DIR = _RECEIPT.DIST_DIR
_is_reparse = _RECEIPT._is_reparse
_repair = _RECEIPT._repair

ABSENT = "absent"
# The bundle is produced by `npm run build`, which is
# `npm run theme:check && tsc -b && vite build`. The roster is every input that
# command reads: the application sources (including `frontend/src/generated`),
# the HTML entry, the resolved dependency lock, the vite and TypeScript
# configuration, the theme source and generator with BOTH generated outputs it
# checks, and the one fixture `frontend/src` imports from outside `frontend`
# (`tsconfig.app.json` includes all of `src`, so `tsc` reads it).
REQUIRED_SOURCE_DIRS = ("frontend/src",)
OPTIONAL_SOURCE_DIRS = ("frontend/public",)
REQUIRED_SOURCE_FILES = (
    "desktop/python-webview-shell/generated/theme_tokens.py",
    "frontend/index.html",
    "frontend/package-lock.json",
    "frontend/package.json",
    "frontend/tsconfig.app.json",
    "frontend/tsconfig.json",
    "frontend/tsconfig.node.json",
    "frontend/vite.config.ts",
    "scripts/generate-theme-tokens.mjs",
    "tests/fixtures/checkpoint_change_v1.json",
    "theme/theme-tokens.json",
)
# Vite loads these mode files from the frontend root for a production build and
# inlines matching `VITE_*` values into the bundle. A release must not embed
# unreviewed local environment, so a non-empty one refuses; every one of them is
# recorded present-or-absent so that merely adding one invalidates the receipt.
ENV_FILES = (
    "frontend/.env",
    "frontend/.env.local",
    "frontend/.env.production",
    "frontend/.env.production.local",
)
SKIP_PARTS = frozenset({".git", "__pycache__", "node_modules"})
SKIP_SUFFIXES = frozenset({".pyc", ".pyo"})
MAX_FILES = 4096
MAX_FILE_BYTES = 64 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _copy_digest(source: Path, target: Path) -> str:
    """Copy and hash in one read, so the digest names the captured bytes."""

    digest = hashlib.sha256()
    with Path(source).open("rb") as reader, Path(target).open("wb") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
            writer.write(chunk)
    return "sha256:" + digest.hexdigest()


def _is_skipped(relative: str) -> bool:
    parts = relative.split("/")
    if any(part in SKIP_PARTS for part in parts):
        return True
    return Path(relative).suffix.lower() in SKIP_SUFFIXES


def _regular_file(path: Path, label: str, code: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise DistSourceGateError(code, f"{label} must be a regular file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DistSourceGateError("DIST_BOUNDS", f"{label} exceeds 64 MiB")


def _walk(root: Path, base: Path, code: str) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(base).as_posix()
        if _is_skipped(relative):
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        _regular_file(path, relative, code)
        found.append((relative, path))
    return found


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def assert_inside(root: Path, target: Path, label: str) -> Path:
    """Refuse unless ``target`` stays under ``root`` lexically and resolved.

    Called before every destructive effect. A redirected ancestor -- a
    symlinked or junctioned ``frontend`` or ``.artifacts`` -- is named and
    refused rather than followed out of the checkout.
    """

    root = Path(os.path.normpath(str(root)))
    lexical = Path(os.path.normpath(str(target)))
    if _norm(lexical) != _norm(root) and not _norm(lexical).startswith(_norm(root) + os.sep):
        raise DistSourceGateError(
            "DIST_PATH_ESCAPE", f"{label} {lexical.as_posix()} is outside {root.as_posix()}"
        )
    node = lexical
    chain: list[Path] = []
    while _norm(node) != _norm(root):
        chain.append(node)
        if node.parent == node:
            break
        node = node.parent
    for candidate in reversed(chain):
        if _is_reparse(candidate):
            raise DistSourceGateError(
                "DIST_PATH_ESCAPE",
                f"{candidate.as_posix()} is a symlink or junction, so {label} would leave "
                f"{root.as_posix()}",
            )
    if _norm(os.path.realpath(str(lexical))) != _norm(lexical):
        raise DistSourceGateError(
            "DIST_PATH_ESCAPE", f"{label} {lexical.as_posix()} resolves outside its own location"
        )
    return lexical


def _env_policy_entries(repo: Path) -> list[tuple[str, str]]:
    """Record every Vite mode env file present-or-absent, refusing a non-empty one."""

    entries: list[tuple[str, str]] = []
    for relative in ENV_FILES:
        path = repo.joinpath(*relative.split("/"))
        if not os.path.lexists(path):
            entries.append((relative, ABSENT))
            continue
        _regular_file(path, relative, "DIST_SOURCE_NONREGULAR")
        if path.stat().st_size > 0:
            raise DistSourceGateError(
                "DIST_ENV_FILE_UNSUPPORTED",
                f"{relative} would inline unreviewed environment values into the release bundle; "
                f"remove it, then run: {_repair(repo)}",
            )
        entries.append((relative, _sha256(path)))
    return entries


def roster_paths(repo: Path) -> list[tuple[str, Path]]:
    """Every build input, as (repo-relative POSIX path, live path), sorted."""

    repo = Path(repo)
    collected: list[tuple[str, Path]] = []
    for relative in REQUIRED_SOURCE_DIRS + OPTIONAL_SOURCE_DIRS:
        root = repo.joinpath(*relative.split("/"))
        if not root.is_dir() or root.is_symlink():
            if relative in OPTIONAL_SOURCE_DIRS and not os.path.lexists(root):
                continue
            raise DistSourceGateError(
                "DIST_SOURCE_MISSING", f"{relative} must be a real directory"
            )
        collected.extend(_walk(root, repo, "DIST_SOURCE_NONREGULAR"))
    for relative in REQUIRED_SOURCE_FILES:
        path = repo.joinpath(*relative.split("/"))
        if not os.path.lexists(path):
            raise DistSourceGateError("DIST_SOURCE_MISSING", f"{relative} is missing")
        _regular_file(path, relative, "DIST_SOURCE_NONREGULAR")
        collected.append((relative, path))
    if len(collected) > MAX_FILES:
        raise DistSourceGateError("DIST_BOUNDS", "frontend build inputs exceed 4096 files")
    if not collected:
        raise DistSourceGateError("DIST_SOURCE_MISSING", "no frontend build input was found")
    collected.sort(key=lambda item: item[0].encode("utf-8"))
    return collected


def _ordered(entries: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    return sorted(
        ({"path": entry["path"], "sha256": entry["sha256"]} for entry in entries),
        key=lambda entry: entry["path"].encode("utf-8"),
    )


def source_entries(repo: Path) -> list[dict[str, str]]:
    """Content-address every frontend build input in the live checkout."""

    repo = Path(repo)
    hashed = [{"path": relative, "sha256": _sha256(path)} for relative, path in roster_paths(repo)]
    hashed.extend(
        {"path": relative, "sha256": value} for relative, value in _env_policy_entries(repo)
    )
    return _ordered(hashed)


def capture_inputs(repo: Path, destination: Path) -> list[dict[str, str]]:
    """Copy every build input into ``destination`` and hash the captured bytes.

    Each digest is computed from the same read that produced the captured copy,
    so the receipt describes the bytes the build will read even if the live
    checkout changes an instant later.
    """

    repo = Path(repo)
    destination = Path(destination)
    captured: list[dict[str, str]] = []
    for relative, path in roster_paths(repo):
        target = destination.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            captured.append({"path": relative, "sha256": _copy_digest(path, target)})
        except OSError as failure:
            raise DistSourceGateError(
                "DIST_SOURCE_UNREADABLE", f"{relative} could not be captured: {failure}"
            ) from failure
    captured.extend(
        {"path": relative, "sha256": value} for relative, value in _env_policy_entries(repo)
    )
    return _ordered(captured)


def dist_entries(dist_root: Path) -> list[dict[str, Any]]:
    dist_root = Path(dist_root)
    if _is_reparse(dist_root):
        raise DistSourceGateError("DIST_NONREGULAR", f"{DIST_DIR} must not be a symlink or junction")
    if os.path.lexists(dist_root) and not dist_root.is_dir():
        raise DistSourceGateError("DIST_NONREGULAR", f"{DIST_DIR} exists but is not a directory")
    if not dist_root.is_dir():
        raise DistSourceGateError("DIST_MISSING", f"{DIST_DIR} must be a real directory")
    found = _walk(dist_root, dist_root, "DIST_NONREGULAR")
    if len(found) > MAX_FILES:
        raise DistSourceGateError("DIST_BOUNDS", f"{DIST_DIR} exceeds 4096 files")
    if not found:
        raise DistSourceGateError("DIST_MISSING", f"{DIST_DIR} contains no file")
    return [
        {"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)}
        for relative, path in found
    ]
