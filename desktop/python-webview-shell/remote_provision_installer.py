"""Standalone Linux remote artifact installer engine.

Import is effect-free: no stdin, argv, filesystem, process, environment,
network, or platform reads run at import. Product packages are not imported.
Public install always selects the real Linux implementation.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import uuid
import zipfile
import zlib
from collections.abc import Mapping, Sequence

from remote_provision_installer_linux import InstallerError
from remote_provision_installer_linux import _LinuxInstallerOperations


MAX_ARCHIVE = 64 * 1024 * 1024
MAX_SIDECAR = 16 * 1024
MAX_STDOUT = 4096
MAX_STDERR = 512
MAX_MEMBERS = 4096
MAX_UNCOMPRESSED = 256 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_PATH = 240
PRODUCT = "1.0.8"
PROTOCOL = 1
SCHEMA = 1
ARCHIVE_NAME = "WorkStack-Linux-1.0.8-cp312-manylinux_2_17_x86_64.zip"
TARGET_ID = "cp312-manylinux_2_17_x86_64"
ENTRYPOINT = "desktop/python-webview-shell/remote_entry.py"
SOABI = "cpython-312-x86_64-linux-gnu"
COMMAND = "provision-install"
RECEIPT = ".workstack-install.json"
ZIP_FLAGS = 0x800
OWNER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
POSIX_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SIDECAR_KEYS = frozenset(
    {
        "archive",
        "artifact_manifest_sha256",
        "product_version",
        "remote_protocol_version",
        "schema_version",
        "source_commit",
        "target_id",
    }
)
ARCHIVE_KEYS = frozenset({"name", "sha256", "size"})
MANIFEST_KEYS = frozenset(
    {
        "entrypoint",
        "files",
        "product_version",
        "remote_protocol_version",
        "requirements_lock_sha256",
        "schema_version",
        "source_commit",
        "source_tree",
        "target",
        "wheels",
    }
)
WHEEL_KEYS = frozenset({"distribution", "filename", "sha256", "tags", "version"})
FILE_KEYS = frozenset({"mode", "path", "sha256", "size"})
TARGET = {
    "glibc_min": [2, 17],
    "implementation": "cpython",
    "libc": "glibc",
    "machine": "x86_64",
    "os": "linux",
    "python_major": 3,
    "python_minor": 12,
    "python_tag": "cp312",
    "soabi": SOABI,
    "wheel_platform": "manylinux_2_17_x86_64",
}
REQUIRED = (
    ENTRYPOINT,
    "desktop/python-webview-shell/remote_command_contract.py",
    "run_work_stack.py",
    "workstack/__init__.py",
)
FROZEN_WHEELS = (
    ("attrs", "26.1.0"),
    ("jsonschema", "4.26.0"),
    ("jsonschema-specifications", "2025.9.1"),
    ("referencing", "0.37.0"),
    ("rpds-py", "2026.6.3"),
    ("typing-extensions", "4.16.0"),
    ("unicodedata2", "17.0.0"),
)
SUCCESS_KEYS = (
    "schema_version",
    "outcome",
    "workspace_uid",
    "product_version",
    "remote_protocol_version",
    "artifact_digest",
    "artifact_manifest_sha256",
)
REFUSE_KEYS = ("schema_version", "outcome", "code")
RECEIPT_KEYS = (
    "schema_version",
    "product_version",
    "remote_protocol_version",
    "artifact_digest",
    "artifact_manifest_sha256",
    "workspace_uid",
    "owner",
    "target",
)


def _require(ok: bool, code: str = "REMOTE_ARTIFACT_INVALID") -> None:
    if not ok:
        raise InstallerError(code)


def _control(text: str) -> bool:
    for character in text:
        if ord(character) < 32 or character == "\x7f":
            return True
    return False


def _unique_pairs(pairs: list[tuple[object, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _no_const(value: str) -> object:
    raise ValueError("invalid JSON constant")


def _dump_sorted(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _dump_line(values: Mapping[str, object], keys: Sequence[str]) -> bytes:
    payload = json.dumps({key: values[key] for key in keys}, ensure_ascii=True, separators=(",", ":"))
    return payload.encode("utf-8") + b"\n"


def _as_dict(value: object, keys: frozenset[str]) -> dict[str, object]:
    _require(type(value) is dict)
    _require(set(value) == keys)
    return value


def _as_text(value: object) -> str:
    _require(type(value) is str)
    _require(not _control(value))
    return value


def _as_int(value: object) -> int:
    _require(type(value) is int)
    return value


def _digest(value: object) -> str:
    text = _as_text(value)
    _require(DIGEST_RE.fullmatch(text) is not None)
    return text


def _commit(value: object) -> str:
    text = _as_text(value)
    _require(COMMIT_RE.fullmatch(text) is not None)
    return text


def _same_value(value: object, expected: object) -> None:
    if type(expected) is list:
        _require(type(value) is list)
        _require(len(value) == len(expected))
        for item, want in zip(value, expected):
            _same_value(item, want)
        return
    _require(type(value) is type(expected))
    _require(value == expected)


def _identity_fields(document: Mapping[str, object]) -> None:
    _same_value(document["schema_version"], SCHEMA)
    _same_value(document["product_version"], PRODUCT)
    _same_value(document["remote_protocol_version"], PROTOCOL)


def _load_object(payload: bytes, code: str, *, canonical: bool) -> dict[str, object]:
    _require(not payload.startswith(b"\xef\xbb\xbf"), code)
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_no_const)
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        raise InstallerError(code) from None
    _require(type(value) is dict, code)
    if canonical:
        dumped = _dump_sorted(value)
        _require(payload in (dumped, dumped + b"\n"), code)
    return value


def _require_bytes(value: object, maximum: int) -> bytes:
    _require(type(value) is bytes, "INVALID_INSTALLER")
    _require(len(value) <= maximum)
    return value


def _posix_root(value: object) -> str:
    _require(type(value) is str, "INVALID_INSTALLER")
    _require(POSIX_RE.fullmatch(value) is not None, "INVALID_INSTALLER")
    _require(value != "/" and not value.endswith("/") and not _control(value), "INVALID_INSTALLER")
    parts = value.split("/")[1:]
    _require(bool(parts), "INVALID_INSTALLER")
    for part in parts:
        _require(part not in {"", ".", ".."}, "INVALID_INSTALLER")
    return value


def _owner_name(value: object) -> str:
    _require(type(value) is str, "INVALID_INSTALLER")
    _require(OWNER_RE.fullmatch(value) is not None, "INVALID_INSTALLER")
    return value


def _workspace_text(value: object) -> str:
    _require(type(value) is str, "INVALID_INSTALLER")
    _require(not _control(value), "INVALID_INSTALLER")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise InstallerError("INVALID_INSTALLER") from None
    rfc = value == str(parsed) and parsed.int != 0 and parsed.variant == uuid.RFC_4122
    _require(rfc, "INVALID_INSTALLER")
    return value


def _overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _validated_request(
    archive_bytes: object,
    sidecar_bytes: object,
    install_root: object,
    data_root: object,
    owner: object,
    expected_workspace_uid: object,
) -> tuple[bytes, bytes, str, str, str, str]:
    archive = _require_bytes(archive_bytes, MAX_ARCHIVE)
    sidecar = _require_bytes(sidecar_bytes, MAX_SIDECAR)
    _require(bool(archive) and bool(sidecar))
    install = _posix_root(install_root)
    data = _posix_root(data_root)
    _require(not _overlap(install, data), "INVALID_INSTALLER")
    return archive, sidecar, install, data, _owner_name(owner), _workspace_text(expected_workspace_uid)


def _parse_argv(argv: Sequence[str]) -> tuple[str, str, str, str]:
    _require(isinstance(argv, (list, tuple)), "INVALID_INSTALLER")
    _require(len(argv) == 9, "INVALID_INSTALLER")
    wanted = (COMMAND, "--install-root", "--data-root", "--owner", "--expected-workspace-uid")
    for token in argv:
        _require(type(token) is str, "INVALID_INSTALLER")
        _require(not _control(token), "INVALID_INSTALLER")
    _require((argv[0], argv[1], argv[3], argv[5], argv[7]) == wanted, "INVALID_INSTALLER")
    install = _posix_root(argv[2])
    data = _posix_root(argv[4])
    _require(not _overlap(install, data), "INVALID_INSTALLER")
    return install, data, _owner_name(argv[6]), _workspace_text(argv[8])


def _payload_path(path: str) -> None:
    _require(not path.startswith("/") and "\\" not in path and ":" not in path)
    _require(len(path.encode("utf-8")) <= MAX_PATH and not _control(path))
    _require(path != RECEIPT)
    for part in path.split("/"):
        _require(part not in {"", ".", ".."})


def _cp312_gnu_x86_64_tags() -> frozenset[str]:
    platforms = ["manylinux_2_%d_x86_64" % minor for minor in range(17, 4, -1)]
    platforms.extend(("manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"))
    tags = {"py3-none-any", "py312-none-any", "cp312-none-any"}
    for platform in platforms:
        tags.add("cp312-cp312-" + platform)
        tags.add("cp312-abi3-" + platform)
        tags.add("cp312-none-" + platform)
        for minor in range(11, 1, -1):
            tags.add("cp3%d-abi3-%s" % (minor, platform))
    return frozenset(tags)


_CP312_GNU_X86_64_TAGS = _cp312_gnu_x86_64_tags()


def _tag_compatible(tag: str) -> bool:
    return tag in _CP312_GNU_X86_64_TAGS


def _admit_tags(value: object) -> None:
    _require(type(value) is list and bool(value))
    previous = ""
    for tag in value:
        text = _as_text(tag)
        _require(text > previous)
        previous = text
        _require(_tag_compatible(text))


def _wheel_filename_tags(filename: str, dist: str, version: str) -> frozenset[str]:
    """Expand the tag set a PEP 427 wheel filename declares.

    A real manylinux wheel compresses several platform tags into the one
    filename segment, so the atomic tags are the cartesian product of the
    dot-separated python, abi and platform components. The distribution and
    version still have to be the record's own, and a build-tag name is refused
    exactly as it was before, so the wheel's shipped filename is never rewritten
    to fit the manifest.
    """

    _require("/" not in filename and "\\" not in filename and not _control(filename))
    _require(filename.endswith(".whl"))
    parts = filename[: -len(".whl")].split("-")
    _require(len(parts) == 5)
    _require(parts[0] == dist.replace("-", "_") and parts[1] == version)
    pythons, abis, platforms = (segment.split(".") for segment in parts[2:])
    for components in (pythons, abis, platforms):
        _require(all(components) and len(set(components)) == len(components))
    return frozenset(
        "-".join((python, abi, platform))
        for python in pythons
        for abi in abis
        for platform in platforms
    )


def _admit_wheels(value: object) -> None:
    _require(type(value) is list)
    _require(len(value) == len(FROZEN_WHEELS))
    previous = ""
    for item, frozen in zip(value, FROZEN_WHEELS):
        record = _as_dict(item, WHEEL_KEYS)
        dist = _as_text(record["distribution"])
        version = _as_text(record["version"])
        filename = _as_text(record["filename"])
        canon = re.sub(r"[-_.]+", "-", dist).lower()
        _require(dist == frozen[0] and version == frozen[1])
        _require(dist == canon and dist > previous)
        previous = dist
        _digest(record["sha256"])
        _admit_tags(record["tags"])
        tags = record["tags"]
        _require(type(tags) is list)
        _require(_wheel_filename_tags(filename, dist, version) == frozenset(tags))


def _admit_files(value: object) -> list[dict[str, object]]:
    _require(type(value) is list and bool(value))
    previous = ""
    paths: list[str] = []
    records: list[dict[str, object]] = []
    for item in value:
        record = _as_dict(item, FILE_KEYS)
        path = _as_text(record["path"])
        _payload_path(path)
        _require(path > previous)
        previous = path
        size = _as_int(record["size"])
        _require(0 <= size <= MAX_FILE)
        _digest(record["sha256"])
        _same_value(record["mode"], 420)
        paths.append(path)
        records.append(record)
    for required in REQUIRED:
        _require(required in paths)
    return records


def _admit_target(value: object) -> None:
    record = _as_dict(value, frozenset(TARGET))
    for key, expected in TARGET.items():
        _same_value(record[key], expected)


def _parse_sidecar(payload: bytes, archive_len: int, archive_digest: str) -> dict[str, object]:
    document = _as_dict(_load_object(payload, "REMOTE_ARTIFACT_INVALID", canonical=True), SIDECAR_KEYS)
    _identity_fields(document)
    _commit(document["source_commit"])
    _require(document["target_id"] == TARGET_ID)
    archive = _as_dict(document["archive"], ARCHIVE_KEYS)
    _require(archive["name"] == ARCHIVE_NAME)
    _require(_as_int(archive["size"]) == archive_len)
    _require(_digest(archive["sha256"]) == archive_digest)
    _digest(document["artifact_manifest_sha256"])
    return document


def _parse_manifest(payload: bytes, source_commit: str) -> list[dict[str, object]]:
    document = _as_dict(_load_object(payload, "REMOTE_ARTIFACT_INVALID", canonical=True), MANIFEST_KEYS)
    _identity_fields(document)
    _require(_commit(document["source_commit"]) == source_commit)
    _commit(document["source_tree"])
    _admit_target(document["target"])
    _require(document["entrypoint"] == ENTRYPOINT)
    _digest(document["requirements_lock_sha256"])
    _admit_wheels(document["wheels"])
    return _admit_files(document["files"])


def _eocd_plain(archive: bytes) -> bool:
    if len(archive) < 22 or archive[:2] != b"PK" or archive[-22:-18] != b"PK\x05\x06":
        return False
    return int.from_bytes(archive[-2:], "little") == 0


def _zip_rejected(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    mode = (info.external_attr >> 16) & 0xFFFF
    if "\\" in name or name.startswith("/") or name.endswith("/") or _control(name):
        return True
    if len(name.encode("utf-8")) > MAX_PATH or info.flag_bits & ~ZIP_FLAGS or info.extra:
        return True
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        return True
    if info.file_size < 0 or info.file_size > MAX_FILE or mode != 0o100644:
        return True
    for part in name.split("/"):
        if part in {"", ".", ".."}:
            return True
    return False


def _read_member(handle: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    try:
        with handle.open(info, "r") as stream:
            chunks: list[bytes] = []
            total = 0
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                _require(total <= info.file_size and total <= MAX_FILE)
                chunks.append(block)
    except (zipfile.BadZipFile, OSError, RuntimeError, zlib.error):
        raise InstallerError("REMOTE_ARTIFACT_INVALID") from None
    data = b"".join(chunks)
    _require(len(data) == info.file_size)
    return data


def _ingest_zip(handle: zipfile.ZipFile, manifest_digest: str) -> tuple[bytes, dict[str, bytes]]:
    infos = handle.infolist()
    _require(bool(infos) and len(infos) <= MAX_MEMBERS)
    total = 0
    raw: set[str] = set()
    normalized: set[str] = set()
    blobs: dict[str, bytes] = {}
    manifest = b""
    for info in infos:
        total += info.file_size
        _require(total <= MAX_UNCOMPRESSED and not _zip_rejected(info))
        name = info.filename
        _require(name not in raw)
        raw.add(name)
        norm = "/".join(name.split("/"))
        _require(norm not in normalized)
        normalized.add(norm)
        data = _read_member(handle, info)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if name == "artifact.json":
            _require(not manifest and digest == manifest_digest)
            manifest = data
            continue
        _require(name.startswith("payload/"))
        blobs[name[8:]] = data
    _require(bool(manifest))
    return manifest, blobs


def _match_files(blobs: dict[str, bytes], records: list[dict[str, object]]) -> None:
    _require(set(blobs) == {str(item["path"]) for item in records})
    for item in records:
        path = str(item["path"])
        data = blobs[path]
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        _require(len(data) == item["size"] and digest == item["sha256"])


def _admit_artifact(archive_bytes: bytes, sidecar_bytes: bytes) -> dict[str, object]:
    digest = "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
    sidecar = _parse_sidecar(sidecar_bytes, len(archive_bytes), digest)
    _require(_eocd_plain(archive_bytes))
    try:
        handle = zipfile.ZipFile(io.BytesIO(archive_bytes), "r")
    except (zipfile.BadZipFile, OSError, RuntimeError):
        raise InstallerError("REMOTE_ARTIFACT_INVALID") from None
    try:
        manifest_bytes, blobs = _ingest_zip(handle, str(sidecar["artifact_manifest_sha256"]))
    finally:
        handle.close()
    records = _parse_manifest(manifest_bytes, str(sidecar["source_commit"]))
    _match_files(blobs, records)
    return {"digest": digest, "manifest_digest": sidecar["artifact_manifest_sha256"], "blobs": blobs, "files": records}


def _success_document(artifact: Mapping[str, object], uid: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA,
        "outcome": "installed",
        "workspace_uid": uid,
        "product_version": PRODUCT,
        "remote_protocol_version": PROTOCOL,
        "artifact_digest": artifact["digest"],
        "artifact_manifest_sha256": artifact["manifest_digest"],
    }


def _receipt_document(artifact: Mapping[str, object], uid: str, owner: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA,
        "product_version": PRODUCT,
        "remote_protocol_version": PROTOCOL,
        "artifact_digest": artifact["digest"],
        "artifact_manifest_sha256": artifact["manifest_digest"],
        "workspace_uid": uid,
        "owner": owner,
        "target": {key: TARGET[key] for key in TARGET},
    }


def _extract_payload(operations: object, artifact: Mapping[str, object]) -> None:
    blobs = artifact["blobs"]
    _require(type(blobs) is dict, "REMOTE_INSTALL_FAILED")
    for item in artifact["files"]:
        _require(type(item) is dict, "REMOTE_INSTALL_FAILED")
        path = str(item["path"])
        operations.write_file(path, blobs[path], str(item["sha256"]))


def _maybe_cleanup(operations: object, committed: bool) -> None:
    if committed or getattr(operations, "commit_unknown", False):
        return
    if not getattr(operations, "stage_ready", False):
        return
    try:
        operations.cleanup_stage()
    except Exception:
        return


def _close_operations(operations: object) -> None:
    closer = getattr(operations, "close_fds", None)
    if closer is None:
        return
    try:
        closer()
    except Exception:
        return


def _fsync_committed(operations: object) -> None:
    try:
        operations.fsync_parent()
    except InstallerError as error:
        _require(error.code == "REMOTE_INSTALL_COMMIT_UNKNOWN", "REMOTE_INSTALL_COMMIT_UNKNOWN")
        raise
    except Exception:
        raise InstallerError("REMOTE_INSTALL_COMMIT_UNKNOWN") from None


def _apply_install(operations: object, artifact: dict[str, object], uid: str, owner: str) -> dict[str, object]:
    committed = False
    try:
        operations.admit_runtime()
        operations.open_roots()
        operations.admit_data()
        operations.create_stage()
        _extract_payload(operations, artifact)
        operations.smoke_imports()
        operations.smoke_entrypoint()
        operations.write_receipt(_receipt_document(artifact, uid, owner))
        operations.commit_noreplace()
        committed = True
        _fsync_committed(operations)
        return _success_document(artifact, uid)
    except InstallerError:
        _maybe_cleanup(operations, committed)
        raise
    except Exception:
        _maybe_cleanup(operations, committed)
        raise InstallerError("REMOTE_INSTALL_FAILED") from None
    finally:
        _close_operations(operations)


def _install_with_operations(
    operations: object,
    *,
    archive_bytes: bytes,
    sidecar_bytes: bytes,
    install_root: str,
    data_root: str,
    owner: str,
    expected_workspace_uid: str,
) -> dict[str, object]:
    _validated_request(archive_bytes, sidecar_bytes, install_root, data_root, owner, expected_workspace_uid)
    artifact = _admit_artifact(archive_bytes, sidecar_bytes)
    return _apply_install(operations, artifact, expected_workspace_uid, owner)


def install_remote_artifact(
    *,
    archive_bytes: bytes,
    sidecar_bytes: bytes,
    install_root: str,
    data_root: str,
    owner: str,
    expected_workspace_uid: str,
) -> dict[str, object]:
    archive, sidecar, install, data, owner_name, uid = _validated_request(
        archive_bytes, sidecar_bytes, install_root, data_root, owner, expected_workspace_uid
    )
    artifact = _admit_artifact(archive, sidecar)
    _require(sys.platform.startswith("linux"), "REMOTE_INSTALLER_UNSUPPORTED")
    operations = _LinuxInstallerOperations(install, data, owner_name, uid)
    return _apply_install(operations, artifact, uid, owner_name)


def _write_refuse(code: str) -> int:
    payload = _dump_line({"schema_version": SCHEMA, "outcome": "refused", "code": code}, REFUSE_KEYS)
    if len(payload) > MAX_STDERR:
        payload = b'{"schema_version":1,"outcome":"refused","code":"REMOTE_INSTALL_FAILED"}\n'
    try:
        sys.stderr.buffer.write(payload)
        sys.stderr.buffer.flush()
    except OSError:
        pass
    return 2


def installer_main(argv: Sequence[str], archive_bytes: bytes, sidecar_bytes: bytes) -> int:
    try:
        install, data, owner, uid = _parse_argv(argv)
        result = install_remote_artifact(
            archive_bytes=archive_bytes,
            sidecar_bytes=sidecar_bytes,
            install_root=install,
            data_root=data,
            owner=owner,
            expected_workspace_uid=uid,
        )
    except InstallerError as error:
        return _write_refuse(error.code)
    except Exception:
        return _write_refuse("REMOTE_INSTALL_FAILED")
    payload = _dump_line(result, SUCCESS_KEYS)
    if len(payload) > MAX_STDOUT:
        return _write_refuse("REMOTE_INSTALL_FAILED")
    try:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    except OSError:
        return _write_refuse("REMOTE_INSTALL_FAILED")
    return 0

