"""Opt-in install of the Work Stack agent Skill from a verified app.

Reads the three admitted payload Skill files from an already verified
installed or unpacked application directory and, only when the caller
asks to apply, places them at ``$HOME/.agents/skills/work-stack``.

Import is effect-free: no stdin, argv, filesystem, process, environment,
network, or platform reads run at import. Product packages are not imported.
This module does not admit archives, mutate PATH or shell rc, activate a
connection, or touch sibling skills.

Publication is per-file exclusive create or replace. Three files plus a
receipt are not one atomic directory rename, including on NFS. A failure
leaves staged bytes and a previous copy when an update had begun; it never
claims success. Default callers must not apply.

Each owned target is re-read and compared against the admitted bytes and stat
identity immediately before its own write, so a late edit or deletion refuses
instead of being overwritten or recreated. That narrows but cannot close the gap
before the final rename; no lock, watcher, or OS atomicity is claimed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path


SCHEMA_VERSION = 1
OPERATION = "install-skill"
SKILL_NAME = "work-stack"
RECEIPT_NAME = ".workstack-skill.json"
ARTIFACT_MANIFEST_NAME = ".workstack-artifact.json"
DEST_RELATIVE = ".agents/skills/work-stack"
STAGE_NAME = ".workstack-work-stack-stage"
PREVIOUS_NAME = ".workstack-work-stack-previous"
MAX_FILE = 32 * 1024 * 1024
MAX_RECEIPT = 16 * 1024
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
OWNER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
UNPACK_RECEIPT = ".workstack-unpacked.json"
INSTALL_RECEIPT = ".workstack-install.json"
APP_RECEIPTS = (INSTALL_RECEIPT, UNPACK_RECEIPT)
SKILL_PAYLOAD_PATHS = (
    "integrations/agent-skill/work-stack/SKILL.md",
    "integrations/agent-skill/work-stack/references/commands.md",
    "integrations/agent-skill/work-stack/references/journal-policy.md",
)
DEST_RELATIVE_PATHS = (
    "SKILL.md",
    "references/commands.md",
    "references/journal-policy.md",
)
OWNED_ORDER = DEST_RELATIVE_PATHS + (RECEIPT_NAME,)
OwnedState = dict[str, tuple[bytes, tuple[int, int, int]]]
OWNED_NAMES = frozenset(OWNED_ORDER)
PAYLOAD_TO_DEST = dict(zip(SKILL_PAYLOAD_PATHS, DEST_RELATIVE_PATHS))
RECEIPT_FILE_KEYS = frozenset({"sha256", "size"})
RECEIPT_KEYS = frozenset({"files", "product_version", "schema_version", "skill"})
MANIFEST_FILE_KEYS = frozenset({"mode", "path", "sha256", "size"})
UNPACK_RECEIPT_KEYS = frozenset(
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
INSTALL_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "product_version",
        "remote_protocol_version",
        "artifact_digest",
        "artifact_manifest_sha256",
        "workspace_uid",
        "owner",
        "target",
    }
)
UNPACK_TOOL = "workstack-verified-unpack/1"
UNPACK_METHOD = "verified_unpack"
FROZEN_TARGET_JSON = (
    b'{"glibc_min":[2,17],"implementation":"cpython","libc":"glibc",'
    b'"machine":"x86_64","os":"linux","python_major":3,"python_minor":12,'
    b'"python_tag":"cp312","soabi":"cpython-312-x86_64-linux-gnu",'
    b'"wheel_platform":"manylinux_2_17_x86_64"}'
)


class SkillInstallError(RuntimeError):
    """Stable-code skill install failure without path, home, or exception text."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def _require(ok: bool, code: str, detail: str = "") -> None:
    if not ok:
        raise SkillInstallError(code, detail)


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def encode_skill_document(document: Mapping[str, object]) -> bytes:
    return canonical_json(dict(document)) + b"\n"


def _control(text: str) -> bool:
    return any(ord(character) < 32 or character == "\x7f" for character in text)


def _absolute_path(value: object, code: str) -> Path:
    _require(type(value) is str and bool(value), code)
    path = Path(value)
    _require(path.is_absolute(), code)
    _require(path.name not in {"", ".", ".."} and ".." not in path.parts, code)
    for part in path.parts:
        _require(not _control(part), code)
    return path


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(os.fspath(path))
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SkillInstallError("SKILL_INSTALL_FAILED", "path could not be inspected") from error


def _is_regular(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode)


def _is_directory(info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode)


def _is_link(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _dirfd_available() -> bool:
    supported = getattr(os, "supports_dir_fd", None)
    return bool(
        supported is not None
        and os.open in supported
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
    )


def _read_regular_file(path: Path, limit: int, code: str) -> bytes:
    info = _lstat(path)
    _require(info is not None, code)
    assert info is not None
    _require(_is_regular(info) and not _is_link(info), code)
    _require(0 < info.st_size <= limit, code)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise SkillInstallError(code) from error
    _require(len(data) == info.st_size and len(data) <= limit, code)
    return data


def _open_flags() -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    return flags


def _require_owned_dir(info: os.stat_result, code: str) -> None:
    _require(_is_directory(info) and not _is_link(info), code)
    getter = getattr(os, "geteuid", None)
    if getter is None:
        return
    _require(info.st_uid == getter(), code)


def _ensure_directory(path: Path, code: str) -> None:
    """Create ``path`` as a real directory. Never follows a parent component."""

    missing: list[Path] = []
    cursor = path
    while True:
        info = _lstat(cursor)
        if info is not None:
            _require_owned_dir(info, code)
            break
        missing.append(cursor)
        parent = cursor.parent
        _require(parent != cursor, code)
        cursor = parent
    for create in reversed(missing):
        try:
            os.mkdir(os.fspath(create), 0o700)
        except OSError as error:
            raise SkillInstallError(code) from error
        created = _lstat(create)
        _require(created is not None, code)
        assert created is not None
        _require_owned_dir(created, code)


def _exclusive_write(path: Path, content: bytes) -> None:
    _require(path.name not in {"", ".", ".."}, "SKILL_INSTALL_FAILED")
    _ensure_directory(path.parent, "SKILL_INSTALL_FAILED")
    dir_fd = -1
    try:
        if _dirfd_available():
            dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            dir_fd = os.open(os.fspath(path.parent), dir_flags)
            fd = os.open(path.name, _open_flags(), 0o644, dir_fd=dir_fd)
        else:
            fd = os.open(os.fspath(path), _open_flags(), 0o644)
    except OSError as error:
        if dir_fd >= 0:
            os.close(dir_fd)
        raise SkillInstallError("SKILL_INSTALL_FAILED") from error
    try:
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("WRITE_FAILED")
            view = view[count:]
        os.fsync(fd)
    except OSError as error:
        os.close(fd)
        if dir_fd >= 0:
            os.close(dir_fd)
        raise SkillInstallError("SKILL_INSTALL_FAILED") from error
    os.close(fd)
    if dir_fd >= 0:
        os.close(dir_fd)


def _unique_pairs(pairs: list[tuple[object, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _load_json_object(payload: bytes, code: str) -> dict[str, object]:
    _require(not payload.startswith(b"\xef\xbb\xbf"), code)
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        raise SkillInstallError(code) from None
    _require(type(value) is dict, code)
    return value


def _product_version(value: object, code: str) -> str:
    _require(type(value) is str and bool(value) and not _control(value), code)
    _require(VERSION_RE.fullmatch(value) is not None, code)
    return value


def skill_destination(home: str) -> Path:
    root = _absolute_path(home, "HOME_REQUIRED")
    return root / ".agents" / "skills" / "work-stack"


def _file_record(data: bytes) -> dict[str, object]:
    return {"sha256": sha256_bytes(data), "size": len(data)}


def _records_from_blobs(blobs: Mapping[str, bytes]) -> dict[str, dict[str, object]]:
    return {name: _file_record(blobs[name]) for name in DEST_RELATIVE_PATHS}


def _digest_field(value: object, code: str) -> str:
    _require(type(value) is str and DIGEST_RE.fullmatch(value) is not None, code)
    return value


def _parse_unpack_receipt(payload: bytes) -> dict[str, object]:
    document = _load_json_object(payload, "APP_NOT_VERIFIED")
    _require(set(document) == UNPACK_RECEIPT_KEYS, "APP_NOT_VERIFIED")
    _require(document["tool"] == UNPACK_TOOL, "APP_NOT_VERIFIED")
    _require(document["method"] == UNPACK_METHOD, "APP_NOT_VERIFIED")
    _require(document["activation"] == "not_activated", "APP_NOT_VERIFIED")
    _require(document["imports"] == "PASS", "APP_NOT_VERIFIED")
    _require(document["placement"] == "ready_candidate", "APP_NOT_VERIFIED")
    _require(document["ssot_accessed"] is False, "APP_NOT_VERIFIED")
    _require(document["atomic_directory_publish"] is False, "APP_NOT_VERIFIED")
    _require(document["schema_version"] == 1, "APP_NOT_VERIFIED")
    _require(type(document["remote_protocol_version"]) is int, "APP_NOT_VERIFIED")
    _require(type(document["files_verified"]) is int and document["files_verified"] > 0, "APP_NOT_VERIFIED")
    commit = document["source_commit"]
    _require(type(commit) is str and COMMIT_RE.fullmatch(commit) is not None, "APP_NOT_VERIFIED")
    digest = document["artifact_sha256"]
    _require(type(digest) is str and HEX_DIGEST_RE.fullmatch(digest) is not None, "APP_NOT_VERIFIED")
    _digest_field(document["artifact_digest"], "APP_NOT_VERIFIED")
    _digest_field(document["artifact_manifest_sha256"], "APP_NOT_VERIFIED")
    document["product_version"] = _product_version(document["product_version"], "APP_NOT_VERIFIED")
    return document


def _parse_install_receipt(payload: bytes) -> dict[str, object]:
    _require(not payload.startswith(b"\xef\xbb\xbf") and b"\r" not in payload, "APP_NOT_VERIFIED")
    _require(payload.endswith(b"\n") and payload.count(b"\n") == 1, "APP_NOT_VERIFIED")
    document = _load_json_object(payload, "APP_NOT_VERIFIED")
    try:
        encoded = json.dumps(
            document, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, RecursionError, MemoryError):
        raise SkillInstallError("APP_NOT_VERIFIED") from None
    _require(encoded == payload and set(document) == INSTALL_RECEIPT_KEYS, "APP_NOT_VERIFIED")
    _require(document["schema_version"] == 1, "APP_NOT_VERIFIED")
    _require(type(document["remote_protocol_version"]) is int, "APP_NOT_VERIFIED")
    _digest_field(document["artifact_digest"], "APP_NOT_VERIFIED")
    _digest_field(document["artifact_manifest_sha256"], "APP_NOT_VERIFIED")
    owner = document["owner"]
    _require(type(owner) is str and OWNER_RE.fullmatch(owner) is not None, "APP_NOT_VERIFIED")
    try:
        parsed = uuid.UUID(str(document["workspace_uid"]))
    except (ValueError, AttributeError, TypeError):
        raise SkillInstallError("APP_NOT_VERIFIED") from None
    _require(str(document["workspace_uid"]) == str(parsed) and parsed.int != 0, "APP_NOT_VERIFIED")
    _require(parsed.variant == uuid.RFC_4122, "APP_NOT_VERIFIED")
    try:
        target = json.dumps(
            document["target"], ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, MemoryError):
        raise SkillInstallError("APP_NOT_VERIFIED") from None
    _require(target == FROZEN_TARGET_JSON, "APP_NOT_VERIFIED")
    document["product_version"] = _product_version(document["product_version"], "APP_NOT_VERIFIED")
    return document


def _parse_app_receipt(name: str, payload: bytes) -> dict[str, object]:
    if name == UNPACK_RECEIPT:
        return _parse_unpack_receipt(payload)
    return _parse_install_receipt(payload)


def _skill_records_from_manifest(payload: bytes) -> dict[str, dict[str, object]]:
    document = _load_json_object(payload, "APP_NOT_VERIFIED")
    files = document.get("files")
    _require(type(files) is list and bool(files), "APP_NOT_VERIFIED")
    wanted: dict[str, dict[str, object]] = {}
    for item in files:
        _require(type(item) is dict and set(item) == MANIFEST_FILE_KEYS, "APP_NOT_VERIFIED")
        path = item["path"]
        _require(type(path) is str, "APP_NOT_VERIFIED")
        if path not in SKILL_PAYLOAD_PATHS:
            continue
        digest = item["sha256"]
        _require(type(digest) is str and DIGEST_RE.fullmatch(digest) is not None, "APP_NOT_VERIFIED")
        size = item["size"]
        _require(type(size) is int and 0 < size <= MAX_FILE, "APP_NOT_VERIFIED")
        wanted[path] = {"sha256": digest, "size": size}
    _require(set(wanted) == set(SKILL_PAYLOAD_PATHS), "SKILL_PAYLOAD_MISSING")
    return wanted


def read_verified_app_skill(*, install_root: str) -> tuple[str, dict[str, bytes]]:
    """Return product version and dest-relative Skill bytes from a verified app."""

    root = _absolute_path(install_root, "APP_NOT_VERIFIED")
    info = _lstat(root)
    _require(info is not None and _is_directory(info) and not _is_link(info), "APP_NOT_VERIFIED")
    documents: list[dict[str, object]] = []
    for name in APP_RECEIPTS:
        receipt_path = root / name
        receipt_info = _lstat(receipt_path)
        if receipt_info is None:
            continue
        _require(_is_regular(receipt_info) and not _is_link(receipt_info), "APP_NOT_VERIFIED")
        payload = _read_regular_file(receipt_path, MAX_RECEIPT, "APP_NOT_VERIFIED")
        documents.append(_parse_app_receipt(name, payload))
    _require(bool(documents), "APP_NOT_VERIFIED")
    versions = {str(item["product_version"]) for item in documents}
    manifests = {str(item["artifact_manifest_sha256"]) for item in documents}
    _require(len(versions) == 1 and len(manifests) == 1, "APP_NOT_VERIFIED")
    expected_manifest = next(iter(manifests))
    carried = _read_regular_file(root / ARTIFACT_MANIFEST_NAME, MAX_FILE, "APP_NOT_VERIFIED")
    _require(sha256_bytes(carried) == expected_manifest, "APP_NOT_VERIFIED")
    records = _skill_records_from_manifest(carried)
    blobs: dict[str, bytes] = {}
    for payload_path, dest_name in PAYLOAD_TO_DEST.items():
        data = _read_regular_file(root / payload_path, MAX_FILE, "SKILL_PAYLOAD_MISSING")
        record = records[payload_path]
        _require(sha256_bytes(data) == record["sha256"] and len(data) == record["size"], "APP_NOT_VERIFIED")
        blobs[dest_name] = data
    return next(iter(versions)), blobs


def _iter_dest_files(dest: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(os.fspath(dest), followlinks=False):
        current = Path(dirpath)
        keep: list[str] = []
        for name in dirnames:
            full = current / name
            rel = full.relative_to(dest).as_posix()
            info = _lstat(full)
            if info is not None and _is_link(info):
                found[rel] = full
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            full = current / name
            rel = full.relative_to(dest).as_posix()
            found[rel] = full
    return found


def _dest_identity(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_size, info.st_dev, info.st_ino)


def _read_dest_state(dest: Path) -> OwnedState:
    """Read every destination entry once, keeping the bytes with their identity."""

    listed = _iter_dest_files(dest)
    extra = set(listed) - OWNED_NAMES
    _require(not extra, "SKILL_DEST_FOREIGN")
    state: OwnedState = {}
    for rel, path in listed.items():
        info = _lstat(path)
        _require(info is not None and _is_regular(info) and not _is_link(info), "SKILL_DEST_FOREIGN")
        assert info is not None
        _require(info.st_size <= MAX_FILE, "SKILL_DEST_FOREIGN")
        try:
            data = path.read_bytes()
        except OSError as error:
            raise SkillInstallError("SKILL_DEST_FOREIGN") from error
        _require(len(data) == info.st_size, "SKILL_DEST_FOREIGN")
        state[rel] = (data, _dest_identity(info))
    return state


def _read_dest_blobs(dest: Path) -> dict[str, bytes]:
    return {rel: item[0] for rel, item in _read_dest_state(dest).items()}


def _state_blobs(state: OwnedState) -> dict[str, bytes]:
    return {name: state[name][0] for name in DEST_RELATIVE_PATHS}


def _parse_skill_receipt(payload: bytes) -> dict[str, object]:
    document = _load_json_object(payload, "SKILL_DEST_FOREIGN")
    _require(set(document) == RECEIPT_KEYS, "SKILL_DEST_FOREIGN")
    _require(document["schema_version"] == SCHEMA_VERSION, "SKILL_DEST_FOREIGN")
    _require(document["skill"] == SKILL_NAME, "SKILL_DEST_FOREIGN")
    version = _product_version(document["product_version"], "SKILL_DEST_FOREIGN")
    files = document["files"]
    _require(type(files) is dict and set(files) == set(DEST_RELATIVE_PATHS), "SKILL_DEST_FOREIGN")
    parsed: dict[str, dict[str, object]] = {}
    for name in DEST_RELATIVE_PATHS:
        record = files[name]
        _require(type(record) is dict and set(record) == RECEIPT_FILE_KEYS, "SKILL_DEST_FOREIGN")
        digest = record["sha256"]
        _require(type(digest) is str and DIGEST_RE.fullmatch(digest) is not None, "SKILL_DEST_FOREIGN")
        size = record["size"]
        _require(type(size) is int and 0 < size <= MAX_FILE, "SKILL_DEST_FOREIGN")
        parsed[name] = {"sha256": digest, "size": size}
    document["product_version"] = version
    document["files"] = parsed
    return document


def _receipt_matches_blobs(receipt: Mapping[str, object], blobs: Mapping[str, bytes]) -> bool:
    files = receipt["files"]
    if type(files) is not dict:
        return False
    for name in DEST_RELATIVE_PATHS:
        if name not in blobs or _file_record(blobs[name]) != files[name]:
            return False
    return True


def _refused(code: str, *, product_version: str = "", blobs: Mapping[str, bytes] | None = None) -> dict[str, object]:
    records = (
        _records_from_blobs(blobs)
        if blobs is not None
        else {name: {"sha256": sha256_bytes(b""), "size": 0} for name in DEST_RELATIVE_PATHS}
    )
    return {
        "action": "refuse",
        "atomic_directory_publish": False,
        "code": code,
        "destination": DEST_RELATIVE,
        "files": records,
        "operation": OPERATION,
        "outcome": "refused",
        "product_version": product_version,
        "schema_version": SCHEMA_VERSION,
    }


def _build_receipt(product_version: str, blobs: Mapping[str, bytes]) -> dict[str, object]:
    return {
        "files": _records_from_blobs(blobs),
        "product_version": product_version,
        "schema_version": SCHEMA_VERSION,
        "skill": SKILL_NAME,
    }


def _planned(
    action: str,
    *,
    product_version: str,
    blobs: Mapping[str, bytes],
    code: str = "",
    outcome: str = "planned",
) -> dict[str, object]:
    document: dict[str, object] = {
        "action": action,
        "atomic_directory_publish": False,
        "destination": DEST_RELATIVE,
        "files": _records_from_blobs(blobs),
        "operation": OPERATION,
        "outcome": outcome,
        "product_version": product_version,
        "schema_version": SCHEMA_VERSION,
    }
    if code:
        document["code"] = code
    return document


def inspect_skill_install(*, install_root: str, home: str) -> dict[str, object]:
    """Classify dest against verified app Skill bytes. Never writes."""

    try:
        version, payload = read_verified_app_skill(install_root=install_root)
    except SkillInstallError as error:
        return _refused(error.code)
    try:
        dest = skill_destination(home)
        info = _lstat(dest)
        if info is None:
            return _planned("install", product_version=version, blobs=payload)
        _require(_is_directory(info) and not _is_link(info), "SKILL_DEST_FOREIGN")
        existing = _read_dest_blobs(dest)
        skill_blobs = {name: existing[name] for name in DEST_RELATIVE_PATHS if name in existing}
        if not skill_blobs and RECEIPT_NAME not in existing:
            return _planned("install", product_version=version, blobs=payload)
        if set(skill_blobs) == set(DEST_RELATIVE_PATHS) and skill_blobs == payload:
            return _planned("noop", product_version=version, blobs=payload, outcome="noop")
        receipt_payload = existing.get(RECEIPT_NAME)
        if receipt_payload is None or set(skill_blobs) != set(DEST_RELATIVE_PATHS):
            raise SkillInstallError("SKILL_DEST_FOREIGN")
        receipt = _parse_skill_receipt(receipt_payload)
        if not _receipt_matches_blobs(receipt, skill_blobs):
            raise SkillInstallError("SKILL_DEST_MODIFIED")
        return _planned("update", product_version=version, blobs=payload)
    except SkillInstallError as error:
        return _refused(error.code, product_version=version, blobs=payload)


def _remove_tree(path: Path) -> None:
    if _lstat(path) is None:
        return
    shutil.rmtree(os.fspath(path))


def _write_stage(stage: Path, blobs: Mapping[str, bytes], receipt_bytes: bytes) -> None:
    _remove_tree(stage)
    _ensure_directory(stage, "SKILL_INSTALL_FAILED")
    for name, data in blobs.items():
        _exclusive_write(stage / name, data)
        written = _read_regular_file(stage / name, MAX_FILE, "SKILL_INSTALL_FAILED")
        _require(written == data, "SKILL_INSTALL_FAILED")
    _exclusive_write(stage / RECEIPT_NAME, receipt_bytes)
    _require(
        _read_regular_file(stage / RECEIPT_NAME, MAX_RECEIPT, "SKILL_INSTALL_FAILED") == receipt_bytes,
        "SKILL_INSTALL_FAILED",
    )


def _verify_owned_unchanged(dest: Path, name: str, expected: OwnedState) -> bytes:
    """Re-read one owned target at its own mutation boundary.

    A late edit, deletion, or swap for a link or directory refuses here, so the
    user's bytes are never overwritten and a deleted file is never recreated.
    """

    want, identity = expected[name]
    target = dest / name
    info = _lstat(target)
    _require(info is not None, "SKILL_DEST_MODIFIED")
    assert info is not None
    _require(_is_regular(info) and not _is_link(info), "SKILL_DEST_MODIFIED")
    _require(info.st_size == len(want) and _dest_identity(info) == identity, "SKILL_DEST_MODIFIED")
    try:
        data = target.read_bytes()
    except OSError as error:
        raise SkillInstallError("SKILL_DEST_MODIFIED") from error
    _require(data == want, "SKILL_DEST_MODIFIED")
    return data


def _copy_previous(dest: Path, previous: Path, expected: OwnedState) -> None:
    _remove_tree(previous)
    _ensure_directory(previous, "SKILL_INSTALL_FAILED")
    for name in OWNED_ORDER:
        _exclusive_write(previous / name, _verify_owned_unchanged(dest, name, expected))


def _publish_new(dest: Path, stage: Path, names: tuple[str, ...]) -> None:
    info = _lstat(dest)
    if info is None:
        _ensure_directory(dest, "SKILL_INSTALL_FAILED")
    else:
        _require_owned_dir(info, "SKILL_DEST_FOREIGN")
    for name in names:
        target = dest / name
        target_info = _lstat(target)
        _require(target_info is None, "SKILL_INSTALL_FAILED")
        _exclusive_write(target, _read_regular_file(stage / name, MAX_FILE, "SKILL_INSTALL_FAILED"))


def _replace_owned(dest: Path, stage: Path, names: tuple[str, ...], expected: OwnedState) -> None:
    dest_info = _lstat(dest)
    _require(dest_info is not None, "SKILL_DEST_FOREIGN")
    assert dest_info is not None
    _require_owned_dir(dest_info, "SKILL_DEST_FOREIGN")
    for name in names:
        _verify_owned_unchanged(dest, name, expected)
        staged = stage / name
        target = dest / name
        parent = target.parent
        _ensure_directory(parent, "SKILL_INSTALL_FAILED")
        temporary = parent / (Path(name).name + ".workstack-new")
        info = _lstat(temporary)
        if info is not None:
            raise SkillInstallError("SKILL_INSTALL_FAILED")
        _exclusive_write(temporary, _read_regular_file(staged, MAX_FILE, "SKILL_INSTALL_FAILED"))
        os.replace(os.fspath(temporary), os.fspath(target))


def _current_owned_state(dest: Path) -> OwnedState:
    existing = _read_dest_state(dest)
    missing = [name for name in OWNED_ORDER if name not in existing]
    _require(not missing, "SKILL_DEST_FOREIGN")
    return {name: existing[name] for name in OWNED_ORDER}


def apply_skill_install(*, install_root: str, home: str) -> dict[str, object]:
    """Apply an inspect decision. No-op and refuse do not write."""

    planned = inspect_skill_install(install_root=install_root, home=home)
    action = planned["action"]
    if action == "refuse" or action == "noop":
        return planned
    payload: dict[str, bytes] | None = None
    version = str(planned.get("product_version") or "")
    try:
        version, payload = read_verified_app_skill(install_root=install_root)
        dest = skill_destination(home)
        parent = dest.parent
        stage = parent / STAGE_NAME
        previous = parent / PREVIOUS_NAME
        receipt_bytes = canonical_json(_build_receipt(version, payload)) + b"\n"
        _ensure_directory(parent, "SKILL_INSTALL_FAILED")
        _write_stage(stage, payload, receipt_bytes)
        if action == "install":
            info = _lstat(dest)
            if info is None:
                _ensure_directory(dest, "SKILL_INSTALL_FAILED")
            else:
                _require_owned_dir(info, "SKILL_DEST_FOREIGN")
                _require(not _iter_dest_files(dest), "SKILL_DEST_FOREIGN")
            _publish_new(dest, stage, OWNED_ORDER)
        elif action == "update":
            owned = _current_owned_state(dest)
            receipt = _parse_skill_receipt(owned[RECEIPT_NAME][0])
            if not _receipt_matches_blobs(receipt, _state_blobs(owned)):
                raise SkillInstallError("SKILL_DEST_MODIFIED")
            _copy_previous(dest, previous, owned)
            _replace_owned(dest, stage, DEST_RELATIVE_PATHS, owned)
            _replace_owned(dest, stage, (RECEIPT_NAME,), owned)
        else:
            raise SkillInstallError("SKILL_UNSUPPORTED")
        written = {name: _read_regular_file(dest / name, MAX_FILE, "SKILL_INSTALL_FAILED") for name in DEST_RELATIVE_PATHS}
        _require(written == payload, "SKILL_INSTALL_FAILED")
        _require(
            _read_regular_file(dest / RECEIPT_NAME, MAX_RECEIPT, "SKILL_INSTALL_FAILED") == receipt_bytes,
            "SKILL_INSTALL_FAILED",
        )
    except SkillInstallError as error:
        return _refused(error.code, product_version=version, blobs=payload)
    except OSError:
        return _refused("SKILL_INSTALL_FAILED", product_version=version, blobs=payload)
    try:
        _remove_tree(stage)
    except OSError:
        pass
    assert payload is not None
    outcome = "installed" if action == "install" else "updated"
    return _planned(action, product_version=version, blobs=payload, outcome=outcome)


def run_skill_install(*, install_root: str, home: str, apply: bool) -> dict[str, object]:
    """Inspect by default; apply only when ``apply`` is true."""

    if not apply:
        planned = inspect_skill_install(install_root=install_root, home=home)
        if planned["action"] in {"install", "update"}:
            planned["outcome"] = "planned"
        return planned
    return apply_skill_install(install_root=install_root, home=home)


def skill_main(argv: list[str] | None = None) -> int:
    """Guarded payload CLI. Inspect/plan by default; ``--apply`` writes."""

    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="remote_skill_install",
        description=(
            "Install the Work Stack agent Skill from this verified app into "
            "$HOME/.agents/skills/work-stack. Inspect/plan is the default."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--install-root", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the Skill. Without this flag the CLI stops after the plan.",
    )
    arguments = parser.parse_args(argv)
    home = os.environ.get("HOME")
    if not home:
        payload = json.dumps(
            {"code": "HOME_REQUIRED", "detail": "HOME is required for skill install"},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        sys.stderr.buffer.write(payload + b"\n")
        return 2
    document = run_skill_install(
        install_root=arguments.install_root,
        home=home,
        apply=arguments.apply,
    )
    sys.stdout.buffer.write(canonical_json(document) + b"\n")
    sys.stdout.buffer.flush()
    if document.get("action") == "refuse" or document.get("outcome") == "refused":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(skill_main())
