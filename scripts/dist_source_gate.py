#!/usr/bin/env python3
"""Bind ``frontend/dist`` to the build inputs it was actually produced from.

Stdlib only. Every judgement here is a SHA-256 over file *content*: no mtime,
no git commit SHA and no dirty-tree status is ever treated as evidence that a
generated bundle corresponds to the source that is being packaged.

How the binding is obtained
---------------------------
``refresh`` never builds out of the live checkout. It first *captures* every
build input into a private scratch tree under ``.artifacts/``, hashing each
file from the same single read that writes the captured copy, and then runs the
supported build with its working directory inside that captured tree. The
receipt therefore names the exact bytes the build read, not two separate
observations of a mutable checkout. A concurrent change/read/restore of the
live checkout cannot reach the build: the build has no path to the live tree
except the dependency directory (below). After the build the live checkout is
compared against the capture, and a difference refuses installation, so a
receipt is never written for a tree the working copy no longer matches.

What a package consumer must do
-------------------------------
``frontend/dist`` is generated and git-ignored, so any ordinary process on the
machine may rewrite it the instant after ``refresh``/``verify`` returns. A
package must therefore never treat a successful gate call as permission to ship
whatever the live directory holds later. ``verify`` and ``refresh`` return the
manifest and digest of the tree they *admitted* (``dist_files`` and
``dist_digest``); the caller copies the tree into its own private staging area
and passes that admitted value to :func:`verify_staged_dist`, which re-hashes
the bytes that were actually copied. A file mutated, added or removed between
the gate call and the copy -- including a live change that is restored again
afterwards -- leaves the private copy disagreeing with the admitted manifest and
fails packaging closed. Recomputing a fresh self-consistent hash of the copied
tree proves nothing on its own, and neither does a second live re-check: only
the retained admitted value binds the shipped bytes to the receipt.

Deliberate limits (stated, not implied)
---------------------------------------
* The toolchain is *not* attested. ``frontend/node_modules`` is far too large
  to capture, so the captured tree reaches the live dependency directory
  through one directory junction/symlink created by this gate. Dependencies are
  neither installed nor modified by the gate, but the gate offers no defence
  against a hostile build executable or a hostile dependency tree: such a build
  can read and write the live checkout directly. The dependency *set* is bound
  only by the ``frontend/package-lock.json`` digest.
* ``tsc -b`` writes its incremental ``tsBuildInfoFile`` under
  ``frontend/.artifacts/tsbuild`` exactly as the ordinary ``npm run build``
  does, so that cache now stays inside the captured input tree instead of
  landing in the shared dependency directory. The captured build still
  *reads* the live dependencies through the junction above; only the cache
  write moved. That cache is not part of the source roster, so it neither
  enters the receipt nor drifts the source digest.
* Replacing ``frontend/dist`` is **not** one atomic directory swap. It is two
  same-volume renames: the previous tree is moved into the scratch directory,
  then the freshly built tree is renamed into place. Between the two renames
  ``frontend/dist`` is absent, which every reader of this gate treats as a
  closed refusal (``DIST_MISSING``). A failed second rename restores the
  preserved tree; if that restore also fails, the gate fails closed and names
  the preserved path so it can be moved back by hand.
* Hard interruption can leave an abandoned ``.artifacts/dist-build-*``
  directory. It is inert: nothing reads it, and the next refresh uses a new one.

Schema
------
``dist-source-receipt.json`` is a **separate schema**, ``workstack-dist-source``
version 2. It deliberately reuses the ``{path,size,sha256}`` entry shape of
``release_gate._tree_entries`` so the two manifests read alike, but it does not
share that code: ``release_gate`` loads *this* module, so importing it back
would be circular. Neither ``build-receipt.json`` (release payload identity)
nor ``frozen-dist-manifest.json`` (frozen tree) is reused or replaced here.

The receipt is written next to the tree it describes, under the already ignored
``.artifacts/`` directory, so it never enters git status, the release bundle or
the shipped payload.

Module map
----------
This file is the only supported import site, including the dynamic file-path
loading used by ``release_gate`` and ``build_linux_remote_artifact``. Helpers
are loaded as siblings of this file; they are not a package.

* ``dist_source_gate_receipt.py`` — receipt schema, closed-shape validation,
  digest folding, and receipt read/write.
* ``dist_source_gate_manifest.py`` — input roster, content-addressed capture,
  dist-tree walking, and containment.
* this module — capture-build-replace orchestration, ``verify``, ``refresh``,
  and the copy-after-admission binding ``verify_staged_dist``. Every public
  name is re-exported here so existing import sites keep working.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence


def _load_helper(filename: str) -> Any:
    name = "workstack_" + filename[:-3]
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{filename} could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_RECEIPT = _load_helper("dist_source_gate_receipt.py")
_MANIFEST = _load_helper("dist_source_gate_manifest.py")

DistSourceGateError = _RECEIPT.DistSourceGateError
SCHEMA_VERSION = _RECEIPT.SCHEMA_VERSION
DIGEST_TAG = _RECEIPT.DIGEST_TAG
DIST_TREE_TAG = _RECEIPT.DIST_TREE_TAG
DIST_DIR = _RECEIPT.DIST_DIR
ARTIFACT_DIR = _RECEIPT.ARTIFACT_DIR
RECEIPT_NAME = _RECEIPT.RECEIPT_NAME
REPAIR_COMMAND = _RECEIPT.REPAIR_COMMAND
_repair = _RECEIPT._repair
_is_reparse = _RECEIPT._is_reparse
receipt_path = _RECEIPT.receipt_path
source_digest = _RECEIPT.source_digest
dist_digest = _RECEIPT.dist_digest
build_receipt = _RECEIPT.build_receipt
write_receipt = _RECEIPT.write_receipt
read_receipt = _RECEIPT.read_receipt
_require_manifest = _RECEIPT._require_manifest
_first_differences = _RECEIPT._first_differences
_admitted_dist_files = _RECEIPT._admitted_dist_files

ABSENT = _MANIFEST.ABSENT
REQUIRED_SOURCE_DIRS = _MANIFEST.REQUIRED_SOURCE_DIRS
OPTIONAL_SOURCE_DIRS = _MANIFEST.OPTIONAL_SOURCE_DIRS
REQUIRED_SOURCE_FILES = _MANIFEST.REQUIRED_SOURCE_FILES
ENV_FILES = _MANIFEST.ENV_FILES
SKIP_PARTS = _MANIFEST.SKIP_PARTS
SKIP_SUFFIXES = _MANIFEST.SKIP_SUFFIXES
MAX_FILES = _MANIFEST.MAX_FILES
MAX_FILE_BYTES = _MANIFEST.MAX_FILE_BYTES
_sha256 = _MANIFEST._sha256
_copy_digest = _MANIFEST._copy_digest
_is_skipped = _MANIFEST._is_skipped
_regular_file = _MANIFEST._regular_file
_walk = _MANIFEST._walk
_norm = _MANIFEST._norm
assert_inside = _MANIFEST.assert_inside
_env_policy_entries = _MANIFEST._env_policy_entries
roster_paths = _MANIFEST.roster_paths
_ordered = _MANIFEST._ordered
source_entries = _MANIFEST.source_entries
capture_inputs = _MANIFEST.capture_inputs
dist_entries = _MANIFEST.dist_entries

DEPENDENCY_DIR = "frontend/node_modules"
# Deterministic build environment. The child sees only these names; everything
# else -- including any ambient VITE_* -- is dropped rather than inherited.
ENV_ALLOWLIST = frozenset(
    name.upper()
    for name in (
        "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
        "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL", "SHELL",
    )
)
BLOCKED_ENV_PREFIXES = ("VITE_",)
BLOCKED_ENV_NAMES = frozenset({"NODE_ENV", "NODE_OPTIONS", "VITE_USER_NODE_ENV"})
BUILD_TIMEOUT_SECONDS = 1800
# The one supported release invocation. `release_gate.py refresh-dist` exposes
# no flag that can change it; the keyword argument exists for contract tests
# that must not run npm, and never carries a plugin or an arbitrary script into
# a release.
DEFAULT_BUILD_COMMAND = ("npm", "run", "build", "--")


def verify(repo: Path, *, receipt: Path | None = None) -> dict[str, Any]:
    """Refuse unless the dist on disk is the tree the recorded source built."""

    repo = Path(repo).resolve()
    location = Path(receipt) if receipt is not None else receipt_path(repo)
    try:
        recorded = read_receipt(location)
    except DistSourceGateError as failure:
        raise DistSourceGateError(
            failure.code, f"{failure.detail}; run: {_repair(repo)}"
        ) from failure
    entries = source_entries(repo)
    digest = source_digest(entries)
    if recorded["source_digest"] != digest:
        raise DistSourceGateError(
            "DIST_SOURCE_DRIFT",
            f"{DIST_DIR} was built from source digest {recorded['source_digest']} but the frontend "
            f"build inputs now digest to {digest} (changed: "
            f"{_first_differences(recorded['source_inputs'], entries)}); run: {_repair(repo)}",
        )
    dist = assert_inside(repo, repo / "frontend" / "dist", DIST_DIR)
    files = dist_entries(dist)
    if [dict(item) for item in recorded["dist"]["files"]] != files:
        raise DistSourceGateError(
            "DIST_CONTENT_DRIFT",
            f"{DIST_DIR} no longer matches the tree recorded in {location.as_posix()}; "
            f"run: {_repair(repo)}",
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "source_digest": digest,
        "source_input_count": len(entries),
        "dist_file_count": len(files),
        # The admitted tree, for the caller to hold on to. Whatever it ships
        # must be checked against this, not against a later reading of the live
        # directory: see `verify_staged_dist` and the module docstring.
        "dist_digest": dist_digest(files),
        "dist_files": files,
        "receipt": location.as_posix(),
    }


def verify_staged_dist(staged_root: Path, admitted: Mapping[str, Any] | str) -> dict[str, Any]:
    """Refuse unless ``staged_root`` holds exactly the dist the gate admitted.

    ``admitted`` is what a successful :func:`verify` or :func:`refresh` returned:
    the summary itself for an in-process caller, or its ``dist_digest`` string
    for a caller that crossed a process boundary. ``staged_root`` is the
    package's own private copy -- the bytes that will actually be shipped.

    Nothing here reads the live ``frontend/dist`` again. That is the point: the
    live directory is generated and ignored, so it may have been rewritten since
    the gate ran, and a second live check would only move the same race. Only
    the retained admitted value can bind the copied bytes to the receipt.
    """

    if isinstance(admitted, str):
        expected, recorded = admitted, None
    else:
        recorded = _admitted_dist_files(admitted)
        expected = dist_digest(recorded)
    if not expected.startswith("sha256:") or len(expected) != 71:
        raise DistSourceGateError(
            "DIST_STAGED_UNBOUND",
            "no admitted dist digest was supplied, so the staged tree cannot be bound to the "
            "gate result",
        )
    staged = Path(staged_root)
    files = dist_entries(staged)
    actual = dist_digest(files)
    if actual != expected:
        difference = (
            f" (differs at: {_first_differences(recorded, files)})" if recorded is not None else ""
        )
        raise DistSourceGateError(
            "DIST_STAGED_DRIFT",
            f"the staged copy at {staged.as_posix()} is not the {DIST_DIR} tree the gate admitted"
            f"{difference}; it digests to {actual} but {expected} was admitted, so nothing was "
            "packaged",
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "dist_digest": actual,
        "dist_file_count": len(files),
        "staged": staged.as_posix(),
    }


def assert_build_environment(repo: Path) -> None:
    """Refuse an ambient build override rather than silently baking it in.

    Only variable *names* reach the message; no value is read into an error, a
    log or the receipt.
    """

    offenders = sorted(
        name
        for name, value in os.environ.items()
        if value.strip()
        and (
            name.upper() in BLOCKED_ENV_NAMES
            or any(name.upper().startswith(prefix) for prefix in BLOCKED_ENV_PREFIXES)
        )
    )
    if offenders:
        raise DistSourceGateError(
            "DIST_BUILD_ENV_OVERRIDE",
            "the release build does not support environment overrides; unset "
            f"{', '.join(offenders)}, then run: {_repair(repo)}",
        )


def _child_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if name.upper() in ENV_ALLOWLIST}


def _link_dependencies(repo: Path, captured: Path) -> None:
    """Point the captured tree at the live dependency directory.

    The gate never installs, adds, removes or rewrites a dependency. This is the
    one seam where the captured build reaches outside its scratch tree, and it
    is the stated limit of the binding: the dependency *set* is bound only by
    the ``frontend/package-lock.json`` digest.
    """

    live = repo / "frontend" / "node_modules"
    if not live.is_dir():
        raise DistSourceGateError(
            "DIST_DEPENDENCIES_MISSING",
            f"{DEPENDENCY_DIR} is not an installed directory, so the frontend cannot be built; "
            "install the locked dependencies yourself -- the gate never installs -- then run: "
            f"{_repair(repo)}",
        )
    link = captured / "frontend" / "node_modules"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(live), str(link))
        else:
            os.symlink(str(live), str(link), target_is_directory=True)
    except (OSError, AttributeError, ImportError, NotImplementedError) as failure:
        raise DistSourceGateError(
            "DIST_DEPENDENCY_LINK_FAILED",
            f"{DEPENDENCY_DIR} could not be linked into the captured build tree: {failure}",
        ) from failure


def _unlink_dependencies(captured: Path) -> None:
    """Detach the dependency link before the scratch tree is deleted.

    Never rely on a recursive delete to distinguish a junction from a real
    directory: the link is removed explicitly, so the live dependency tree is
    unreachable from the cleanup.
    """

    link = captured / "frontend" / "node_modules"
    if not _is_reparse(link):
        return
    try:
        os.rmdir(link) if os.name == "nt" else os.unlink(link)
    except OSError:
        pass


def _resolve_build_executable(command: Sequence[str], repo: Path) -> str:
    executable = shutil.which(command[0])
    if executable is None:
        raise DistSourceGateError(
            "DIST_BUILD_TOOL_MISSING",
            f"{command[0]} is not on PATH, so the frontend cannot be rebuilt; install it and "
            f"run: {_repair(repo)}",
        )
    return executable


def _run_build(
    captured: Path, executable: str, command: Sequence[str], out_dir: Path, timeout: int
) -> None:
    argv = [executable, *command[1:], "--outDir", str(out_dir), "--emptyOutDir"]
    try:
        completed = subprocess.run(
            argv,
            cwd=str(captured / "frontend"),
            env=_child_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as failure:
        raise DistSourceGateError(
            "DIST_BUILD_FAILED", f"the frontend build exceeded {timeout} seconds"
        ) from failure
    except OSError as failure:
        raise DistSourceGateError("DIST_BUILD_FAILED", str(failure)) from failure
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-12:]
        raise DistSourceGateError(
            "DIST_BUILD_FAILED",
            "the frontend build failed with exit {0}: {1}".format(
                completed.returncode, " | ".join(tail) or "no output"
            ),
        )


def _current_dist(dist: Path) -> list[dict[str, Any]] | None:
    """The installed tree, or None when there is nothing usable to compare."""

    if not dist.is_dir():
        return None
    try:
        return dist_entries(dist)
    except DistSourceGateError as failure:
        if failure.code == "DIST_MISSING":
            return None
        raise


def _replace_dist(repo: Path, dist: Path, fresh: Path, preserved: Path) -> None:
    """Install ``fresh`` as ``frontend/dist``, keeping the prior tree recoverable.

    Two same-volume renames, never one atomic directory swap; see the module
    docstring. On a failed install the previous tree is renamed back, and if
    that also fails the error names the preserved copy so it can be restored by
    hand.
    """

    kept = False
    if os.path.lexists(dist):
        try:
            os.replace(dist, preserved)
        except OSError as failure:
            raise DistSourceGateError(
                "DIST_REPLACE_FAILED",
                f"the existing {DIST_DIR} could not be moved aside: {failure}",
            ) from failure
        kept = True
    try:
        dist.parent.mkdir(parents=True, exist_ok=True)
        os.replace(fresh, dist)
    except OSError as failure:
        if kept:
            try:
                os.replace(preserved, dist)
            except OSError as restore:
                raise DistSourceGateError(
                    "DIST_REPLACE_FAILED",
                    f"{DIST_DIR} could not be replaced ({failure}) and the previous tree could not "
                    f"be restored ({restore}); it is preserved at {preserved.as_posix()} -- move it "
                    f"back, then run: {_repair(repo)}",
                    preserved=preserved.as_posix(),
                ) from failure
        outcome = "the previous tree was restored" if kept else "nothing was replaced"
        raise DistSourceGateError(
            "DIST_REPLACE_FAILED", f"{DIST_DIR} could not be replaced ({failure}); {outcome}"
        ) from failure


def refresh(
    repo: Path,
    *,
    receipt: Path | None = None,
    build_command: Sequence[str] = DEFAULT_BUILD_COMMAND,
    timeout: int = BUILD_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Capture the build inputs, build from the capture, then record both.

    The build is never conditional on the existing tree agreeing with the new
    one: a difference replaces ``frontend/dist``, an equality leaves the bytes
    on disk untouched so an already frozen tree is not disturbed.
    """

    repo = Path(repo).resolve()
    location = Path(receipt) if receipt is not None else receipt_path(repo)
    assert_build_environment(repo)
    executable = _resolve_build_executable(build_command, repo)
    dist = assert_inside(repo, repo / "frontend" / "dist", DIST_DIR)
    if os.path.lexists(dist) and not dist.is_dir():
        raise DistSourceGateError("DIST_NONREGULAR", f"{DIST_DIR} exists but is not a directory")
    scratch = assert_inside(
        repo, repo / ARTIFACT_DIR / f"dist-build-{uuid.uuid4().hex}", "the build scratch tree"
    )
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.mkdir()
    captured_root = scratch / "input"
    out_dir = scratch / "out"
    abandon = False
    try:
        captured_root.mkdir()
        before = capture_inputs(repo, captured_root)
        _link_dependencies(repo, captured_root)
        _run_build(captured_root, executable, build_command, out_dir, timeout)
        if source_entries(repo) != before:
            raise DistSourceGateError(
                "DIST_SOURCE_MOVED",
                "the frontend build inputs in the checkout changed while the build was running, so "
                f"the tree built from the captured inputs was not installed; run: {_repair(repo)}",
            )
        fresh = dist_entries(out_dir)
        current = _current_dist(dist)
        replaced = current != fresh
        if replaced:
            _replace_dist(repo, dist, out_dir, scratch / "previous")
    except DistSourceGateError as failure:
        abandon = failure.preserved is not None
        raise
    finally:
        _unlink_dependencies(captured_root)
        if not abandon:
            shutil.rmtree(scratch, ignore_errors=True)
    write_receipt(location, build_receipt(before, dist_entries(dist)))
    verified = verify(repo, receipt=location)
    verified["replaced_dist"] = replaced
    return verified
