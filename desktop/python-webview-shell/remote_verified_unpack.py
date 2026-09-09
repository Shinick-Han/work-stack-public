"""Verified new-directory unpack as a remote install alternative.

Reuses ``remote_provision_installer._admit_artifact`` for archive, sidecar,
and every payload hash. Linux production placement is the R4 helper core:
dirfd-anchored mkdir/open/write, O_NOFOLLOW/O_DIRECTORY, and same-user
directory permission checks. Creates an absent directory, re-checks hashes,
runs imports, and writes a completion receipt. Does not rename, replace,
publish atomically, touch SSOT, overwrite, or activate. Partial targets stay
inspectable and are never ready.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

def _shell_dir() -> str:
    source = globals().get("__file__")
    if type(source) is not str or not source:
        return ""
    try:
        return str(Path(source).resolve(strict=True).parent)
    except OSError:
        return ""


_SHELL_DIR = _shell_dir()
if _SHELL_DIR and _SHELL_DIR not in sys.path:
    sys.path.insert(0, _SHELL_DIR)

from remote_provision_installer import MAX_ARCHIVE  # noqa: E402
from remote_provision_installer import MAX_SIDECAR  # noqa: E402
from remote_provision_installer import PRODUCT  # noqa: E402
from remote_provision_installer import PROTOCOL  # noqa: E402
from remote_provision_installer import InstallerError  # noqa: E402
from remote_provision_installer import _admit_artifact  # noqa: E402


TOOL = "workstack-verified-unpack/1"
RECEIPT_NAME = ".workstack-unpacked.json"
ARTIFACT_MANIFEST_NAME = ".workstack-artifact.json"
UNIDATA_VERSION = "17.0.0"
IMPORT_TIMEOUT_SECS = 30
SMOKE_MODS = ("workstack", "jsonschema", "unicodedata2", "rpds")
METHOD = "verified_unpack"
OUTCOME_UNPACKED = "unpacked_verified"
OUTCOME_NOT_READY = "not_ready"
PLACEMENT_ABSENT = "absent"
PLACEMENT_INTERRUPTED = "interrupted_placement"
PLACEMENT_UNKNOWN = "unknown"
PLACEMENT_READY = "ready_candidate"
PLACEMENT_IDENTITY = "identity_verified"
ACTIVATION_NOT_ACTIVATED = "not_activated"
RECEIPT_KEYS = frozenset(
    {
        "activation",
        "artifact_digest",
        "artifact_manifest_sha256",
        "artifact_sha256",
        "atomic_directory_publish",
        "files_verified",
        "imports",
        "method",
        "placement",
        "product_version",
        "remote_protocol_version",
        "schema_version",
        "source_commit",
        "ssot_accessed",
        "tool",
    }
)
PRESERVE_ACTION = (
    "Do not activate or overwrite the new directory; preserve old app and SSOT."
)


class UnpackError(RuntimeError):
    """Stable-code unpack failure without path, user, or exception text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise UnpackError(code)


def _require_bytes(value: object, maximum: int) -> bytes:
    _require(type(value) is bytes and bool(value) and len(value) <= maximum, "REMOTE_ARTIFACT_INVALID")
    return value


def _require_absolute_new_path(value: object) -> Path:
    _require(type(value) is str and bool(value), "ABSOLUTE_NEW_APP_PATH_REQUIRED")
    path = Path(value)
    _require(path.is_absolute(), "ABSOLUTE_NEW_APP_PATH_REQUIRED")
    _require(path.name not in {"", ".", ".."} and ".." not in path.parts, "ABSOLUTE_NEW_APP_PATH_REQUIRED")
    for part in path.parts:
        for character in part:
            _require(ord(character) >= 32 and character != "\x7f", "ABSOLUTE_NEW_APP_PATH_REQUIRED")
    return path


def _open_flags() -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    return flags


def write_new(directory: Path, name: str, content: bytes) -> None:
    """Create ``name`` exclusively under ``directory``. Never rename or replace."""

    _require(type(name) is str and name not in {"", ".", ".."}, "REMOTE_ARTIFACT_INVALID")
    _require("/" not in name and "\\" not in name, "REMOTE_ARTIFACT_INVALID")
    path = directory / name
    fd = os.open(os.fspath(path), _open_flags(), 0o600)
    try:
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("WRITE_FAILED")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def read_bounded(path: str, limit: int, digest: str | None = None) -> bytes:
    handle = None
    try:
        handle = open(path, "rb")
        data = handle.read(limit + 1)
    except OSError as error:
        raise UnpackError("FILESYSTEM_ERROR_ERRNO_" + str(error.errno)) from None
    finally:
        if handle is not None:
            handle.close()
    _require(bool(data) and len(data) <= limit, "REMOTE_ARTIFACT_INVALID")
    if digest is not None:
        actual = hashlib.sha256(data).hexdigest()
        _require(actual == digest, "RELEASE_FILE_MISMATCH")
    return data


def admit_unpack_bundle(archive_bytes: bytes, sidecar_bytes: bytes) -> dict[str, object]:
    """Reuse the existing installer admission gate. Does not write or activate."""

    archive = _require_bytes(archive_bytes, MAX_ARCHIVE)
    sidecar = _require_bytes(sidecar_bytes, MAX_SIDECAR)
    return _admit_artifact(archive, sidecar)


def _sidecar_commit(sidecar_bytes: bytes) -> str:
    document = json.loads(sidecar_bytes.decode("utf-8"))
    _require(type(document) is dict and type(document.get("source_commit")) is str, "REMOTE_ARTIFACT_INVALID")
    return str(document["source_commit"])


def _payload_path(root: Path, relative: str) -> Path:
    current = root
    for part in relative.split("/"):
        _require(part not in {"", ".", ".."} and "\\" not in part, "REMOTE_ARTIFACT_INVALID")
        current = current / part
    return current


def _ensure_payload_dir(root: Path, relative: str) -> Path:
    parts = relative.split("/")
    current = root
    for part in parts[:-1]:
        current = current / part
        _require(not current.is_symlink(), "UNPACKED_FILE_MISMATCH")
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            _require(current.is_dir() and not current.is_symlink(), "UNPACKED_FILE_MISMATCH")
    return current


def _write_payload(root: Path, record: Mapping[str, object], blobs: Mapping[str, bytes]) -> None:
    relative = str(record["path"])
    parent = _ensure_payload_dir(root, relative)
    write_new(parent, relative.split("/")[-1], blobs[relative])


def _carried_manifest(admitted: Mapping[str, object]) -> bytes:
    """Original admitted artifact.json bytes, bound by receipt manifest digest."""

    manifest = admitted["manifest"]
    _require(type(manifest) is bytes and bool(manifest), "REMOTE_ARTIFACT_INVALID")
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    _require(digest == admitted["manifest_digest"], "REMOTE_ARTIFACT_INVALID")
    return manifest


def _verify_written_hashes(root: Path, records: Sequence[Mapping[str, object]]) -> None:
    for record in records:
        path = _payload_path(root, str(record["path"]))
        _require(not path.is_symlink(), "UNPACKED_FILE_MISMATCH")
        info = path.stat()
        _require(stat.S_ISREG(info.st_mode) and info.st_size == record["size"], "UNPACKED_FILE_MISMATCH")
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        _require(digest == record["sha256"], "UNPACKED_FILE_MISMATCH")


def _smoke_script(product: str) -> str:
    imported = ", ".join(SMOKE_MODS)
    return (
        "import sys, pathlib\n"
        "root = pathlib.Path(sys.argv[1]).resolve()\n"
        "sys.path.insert(0, str(root))\n"
        "import %s\n"
        "assert workstack.__version__ == %r\n"
        "assert unicodedata2.unidata_version == %r\n"
        "for module in (%s):\n"
        "    assert pathlib.Path(module.__file__).resolve().is_relative_to(root)\n"
        "print('IMPORTS_OK')\n"
    ) % (imported, product, UNIDATA_VERSION, imported)


def smoke_imports(root: Path, product: str = PRODUCT) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _smoke_script(product), os.fspath(root)],
        capture_output=True,
        text=True,
        timeout=IMPORT_TIMEOUT_SECS,
    )
    if result.returncode != 0 or result.stdout.strip() != "IMPORTS_OK":
        raise UnpackError("PYTHON_IMPORT_CHECK_FAILED")


def _receipt_document(admitted: Mapping[str, object], archive_bytes: bytes, source_commit: str) -> dict[str, object]:
    return {
        "activation": ACTIVATION_NOT_ACTIVATED,
        "artifact_digest": admitted["digest"],
        "artifact_manifest_sha256": admitted["manifest_digest"],
        "artifact_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "atomic_directory_publish": False,
        "files_verified": len(admitted["files"]),
        "imports": "PASS",
        "method": METHOD,
        "placement": PLACEMENT_READY,
        "product_version": PRODUCT,
        "remote_protocol_version": PROTOCOL,
        "schema_version": 1,
        "source_commit": source_commit,
        "ssot_accessed": False,
        "tool": TOOL,
    }


def _success_document(receipt: Mapping[str, object]) -> dict[str, object]:
    document = {"outcome": OUTCOME_UNPACKED}
    document.update(receipt)
    return document


def _not_ready(code: str, placement: str) -> dict[str, object]:
    return {
        "action": PRESERVE_ACTION,
        "activation": ACTIVATION_NOT_ACTIVATED,
        "atomic_directory_publish": False,
        "code": code,
        "method": METHOD,
        "outcome": OUTCOME_NOT_READY,
        "placement": placement,
        "ssot_accessed": False,
    }


def _unknown(code: str) -> dict[str, object]:
    return _not_ready(code, PLACEMENT_UNKNOWN)


def _filesystem_code(error: OSError) -> str:
    return "FILESYSTEM_ERROR_ERRNO_" + str(error.errno)


def _lstat_path(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(os.fspath(path))
    except FileNotFoundError:
        return None


def _load_receipt(root: Path) -> dict[str, object] | None:
    path = root / RECEIPT_NAME
    info = _lstat_path(path)
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    payload = path.read_bytes()
    document = json.loads(payload.decode("utf-8"))
    if type(document) is not dict or set(document) != RECEIPT_KEYS:
        return None
    if document.get("tool") != TOOL or document.get("imports") != "PASS":
        return None
    if document.get("activation") != ACTIVATION_NOT_ACTIVATED:
        return None
    if document.get("ssot_accessed") is not False:
        return None
    if document.get("atomic_directory_publish") is not False:
        return None
    if document.get("method") != METHOD:
        return None
    if document.get("product_version") != PRODUCT:
        return None
    if document.get("remote_protocol_version") != PROTOCOL:
        return None
    if document.get("placement") != PLACEMENT_READY:
        return None
    return document


def _inspect_receipt(root: Path) -> dict[str, object]:
    try:
        receipt = _load_receipt(root)
    except OSError as error:
        return _unknown(_filesystem_code(error))
    except (UnicodeError, ValueError):
        return _not_ready("UNPACK_INCOMPLETE", PLACEMENT_INTERRUPTED)
    if receipt is None:
        return _not_ready("UNPACK_INCOMPLETE", PLACEMENT_INTERRUPTED)
    return {
        "activation": ACTIVATION_NOT_ACTIVATED,
        "atomic_directory_publish": False,
        "method": METHOD,
        "outcome": OUTCOME_UNPACKED,
        "placement": PLACEMENT_READY,
        "receipt": receipt,
        "ssot_accessed": False,
    }


def inspect_unpack_target(app_dir: str) -> dict[str, object]:
    """Classify a path without activating or reading SSOT."""

    try:
        destination = _require_absolute_new_path(app_dir)
    except UnpackError as error:
        return _not_ready(error.code, PLACEMENT_ABSENT)
    try:
        info = _lstat_path(destination)
    except OSError as error:
        return _unknown(_filesystem_code(error))
    if info is None:
        return {
            "activation": ACTIVATION_NOT_ACTIVATED,
            "atomic_directory_publish": False,
            "method": METHOD,
            "outcome": OUTCOME_NOT_READY,
            "placement": PLACEMENT_ABSENT,
            "ssot_accessed": False,
        }
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return _not_ready("APP_DIRECTORY_ALREADY_EXISTS", PLACEMENT_INTERRUPTED)
    return _inspect_receipt(destination)


def verify_unpack_identity(
    app_dir: str,
    archive_bytes: bytes,
    sidecar_bytes: bytes,
) -> dict[str, object]:
    """Re-check a ready candidate against archive/sidecar identity. Never activates."""

    inspected = inspect_unpack_target(app_dir)
    if inspected.get("placement") != PLACEMENT_READY:
        return inspected
    try:
        admitted = admit_unpack_bundle(archive_bytes, sidecar_bytes)
        destination = _require_absolute_new_path(app_dir)
        receipt = inspected["receipt"]
        _require(type(receipt) is dict, "UNPACKED_FILE_MISMATCH")
        _require(receipt["artifact_digest"] == admitted["digest"], "UNPACKED_FILE_MISMATCH")
        _require(receipt["artifact_manifest_sha256"] == admitted["manifest_digest"], "UNPACKED_FILE_MISMATCH")
        _require(receipt["source_commit"] == _sidecar_commit(sidecar_bytes), "UNPACKED_FILE_MISMATCH")
        _require(receipt["files_verified"] == len(admitted["files"]), "UNPACKED_FILE_MISMATCH")
        _verify_written_hashes(destination, admitted["files"])
    except InstallerError as error:
        return _not_ready(error.code, PLACEMENT_READY)
    except UnpackError as error:
        return _not_ready(error.code, PLACEMENT_READY)
    except OSError as error:
        return _unknown(_filesystem_code(error))
    except (UnicodeError, ValueError):
        return _not_ready("UNPACKED_FILE_MISMATCH", PLACEMENT_READY)
    return {
        "activation": ACTIVATION_NOT_ACTIVATED,
        "atomic_directory_publish": False,
        "method": METHOD,
        "outcome": OUTCOME_UNPACKED,
        "placement": PLACEMENT_IDENTITY,
        "receipt": inspected["receipt"],
        "ssot_accessed": False,
    }


def linux_dirfd_available() -> bool:
    """Linux production uses the R4 helper dirfd core; Windows tests do not."""

    return bool(
        sys.platform.startswith("linux")
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "geteuid")
    )


def _dir_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _close_fd(fd: int) -> None:
    if fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        return


def _require_same_user_dir(info: os.stat_result) -> None:
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise UnpackError("APP_DIRECTORY_OWNERSHIP_CHANGED")


def write_new_at(dir_fd: int, name: str, content: bytes) -> None:
    """Exclusive create under an already-opened no-follow directory fd."""

    _require(type(name) is str and name not in {"", ".", ".."}, "REMOTE_ARTIFACT_INVALID")
    _require("/" not in name and "\\" not in name, "REMOTE_ARTIFACT_INVALID")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    try:
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("WRITE_FAILED")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _place_linux_dirfd(
    root: Path,
    admitted: Mapping[str, object],
    archive_bytes: bytes,
    source_commit: str,
) -> dict[str, object]:
    """Extracted R4 helper placement: dirfd, O_NOFOLLOW, same-user mode bits."""

    blobs = admitted["blobs"]
    _require(type(blobs) is dict, "REMOTE_ARTIFACT_INVALID")
    flags = _dir_flags()
    parent_fd = os.open(os.fspath(root.parent), flags)
    root_fd = -1
    try:
        os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        root_fd = os.open(root.name, flags, dir_fd=parent_fd)
        root_stat = os.fstat(root_fd)
        _require_same_user_dir(root_stat)
        root_identity = (root_stat.st_dev, root_stat.st_ino)
        for record in admitted["files"]:
            _require(type(record) is dict, "REMOTE_ARTIFACT_INVALID")
            relative = str(record["path"])
            opened: list[int] = []
            current = root_fd
            try:
                for part in relative.split("/")[:-1]:
                    try:
                        os.mkdir(part, 0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    current = os.open(part, flags, dir_fd=current)
                    opened.append(current)
                    _require_same_user_dir(os.fstat(current))
                write_new_at(current, relative.split("/")[-1], blobs[relative])
            finally:
                for fd in reversed(opened):
                    _close_fd(fd)
        _verify_written_hashes(root, admitted["files"])
        smoke_imports(root, PRODUCT)
        info = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != root_identity:
            raise UnpackError("APP_DIRECTORY_CHANGED")
        write_new_at(root_fd, ARTIFACT_MANIFEST_NAME, _carried_manifest(admitted))
        receipt = _receipt_document(admitted, archive_bytes, source_commit)
        write_new_at(root_fd, RECEIPT_NAME, (json.dumps(receipt, sort_keys=True) + "\n").encode("utf-8"))
        return _success_document(receipt)
    finally:
        _close_fd(root_fd)
        _close_fd(parent_fd)


def _place_portable(
    root: Path,
    admitted: Mapping[str, object],
    archive_bytes: bytes,
    source_commit: str,
) -> dict[str, object]:
    """Non-Linux exclusive create for unit tests. Not Linux production placement."""

    blobs = admitted["blobs"]
    _require(type(blobs) is dict, "REMOTE_ARTIFACT_INVALID")
    root.mkdir(mode=0o700)
    for record in admitted["files"]:
        _require(type(record) is dict, "REMOTE_ARTIFACT_INVALID")
        _write_payload(root, record, blobs)
    _verify_written_hashes(root, admitted["files"])
    smoke_imports(root, PRODUCT)
    write_new(root, ARTIFACT_MANIFEST_NAME, _carried_manifest(admitted))
    receipt = _receipt_document(admitted, archive_bytes, source_commit)
    write_new(root, RECEIPT_NAME, (json.dumps(receipt, sort_keys=True) + "\n").encode("utf-8"))
    return _success_document(receipt)


def _place_into(root: Path, admitted: Mapping[str, object], archive_bytes: bytes, source_commit: str) -> dict[str, object]:
    if linux_dirfd_available():
        return _place_linux_dirfd(root, admitted, archive_bytes, source_commit)
    return _place_portable(root, admitted, archive_bytes, source_commit)


def _failed_placement(root: Path) -> str:
    """Observe placement after an attempted write; entry into it proves nothing.

    A create may fail before creating anything, or commit with a lost response
    on a remote filesystem. Neither case establishes a partial target until a
    fresh lstat sees one. This observation authorizes no cleanup or activation.
    """
    try:
        info = _lstat_path(root)
    except OSError:
        return PLACEMENT_UNKNOWN
    return PLACEMENT_ABSENT if info is None else PLACEMENT_INTERRUPTED


def place_verified_unpack(
    *,
    archive_bytes: bytes,
    sidecar_bytes: bytes,
    app_dir: str,
) -> dict[str, object]:
    """Place a verified bundle into an absent directory. Never activates or touches SSOT."""

    placement_attempted = False
    try:
        destination = _require_absolute_new_path(app_dir)
        parent = destination.parent.resolve(strict=True)
        root = parent / destination.name
        archive = _require_bytes(archive_bytes, MAX_ARCHIVE)
        sidecar = _require_bytes(sidecar_bytes, MAX_SIDECAR)
        admitted = admit_unpack_bundle(archive, sidecar)
        source_commit = _sidecar_commit(sidecar)
        try:
            os.lstat(os.fspath(root))
        except FileNotFoundError:
            placement_attempted = True
            return _place_into(root, admitted, archive, source_commit)
        raise FileExistsError(root)
    except FileExistsError:
        return _not_ready("APP_DIRECTORY_ALREADY_EXISTS", _failed_placement(root) if placement_attempted else PLACEMENT_ABSENT)
    except InstallerError as error:
        return _not_ready(error.code, _failed_placement(root) if placement_attempted else PLACEMENT_ABSENT)
    except UnpackError as error:
        return _not_ready(error.code, _failed_placement(root) if placement_attempted else PLACEMENT_ABSENT)
    except subprocess.TimeoutExpired:
        return _not_ready("PYTHON_IMPORT_CHECK_TIMEOUT", _failed_placement(root) if placement_attempted else PLACEMENT_ABSENT)
    except FileNotFoundError as error:
        if placement_attempted:
            return _not_ready(_filesystem_code(error), _failed_placement(root))
        return _not_ready("ABSOLUTE_NEW_APP_PATH_REQUIRED", PLACEMENT_ABSENT)
    except OSError as error:
        return _not_ready(
            _filesystem_code(error),
            _failed_placement(root) if placement_attempted else PLACEMENT_UNKNOWN,
        )


def _cli_path(value: object) -> str:
    _require(type(value) is str and bool(value), "REMOTE_ARTIFACT_INVALID")
    return str(value)


def unpack_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "operation",
        nargs="?",
        default="place",
        choices=("place", "inspect", "verify"),
    )
    parser.add_argument("--archive")
    parser.add_argument("--sidecar")
    parser.add_argument("--app-dir", required=True, help="New absolute app path; existing parent required")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.operation == "inspect":
            result = inspect_unpack_target(args.app_dir)
        elif args.operation == "verify":
            archive = read_bounded(_cli_path(args.archive), MAX_ARCHIVE)
            sidecar = read_bounded(_cli_path(args.sidecar), MAX_SIDECAR)
            result = verify_unpack_identity(args.app_dir, archive, sidecar)
        else:
            archive = read_bounded(_cli_path(args.archive), MAX_ARCHIVE)
            sidecar = read_bounded(_cli_path(args.sidecar), MAX_SIDECAR)
            result = place_verified_unpack(archive_bytes=archive, sidecar_bytes=sidecar, app_dir=args.app_dir)
    except UnpackError as error:
        result = _not_ready(error.code, PLACEMENT_ABSENT)
    payload = json.dumps(result, sort_keys=True)
    print(payload)
    return 0 if result.get("outcome") == OUTCOME_UNPACKED else 2


if __name__ == "__main__":
    raise SystemExit(unpack_main())
