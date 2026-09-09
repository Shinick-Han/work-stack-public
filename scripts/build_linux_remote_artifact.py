#!/usr/bin/env python3
"""Build a deterministic CPython 3.12 x86_64 glibc Linux remote payload.

Stdlib only. No pip, network, compiler, WSL, SSH or product import. Linux
wheels come from a read-only wheelhouse against the frozen official 7-wheel
CPython 3.12 lock. Missing manylinux unicodedata2 is LINUX_WHEEL_LOCK_MISSING.
Arbitrary requirements graphs are not resolved.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping


TARGET_ID = "cp312-manylinux_2_17_x86_64"
TARGET = {
    "os": "linux",
    "machine": "x86_64",
    "implementation": "cpython",
    "python_major": 3,
    "python_minor": 12,
    "python_tag": "cp312",
    "soabi": "cpython-312-x86_64-linux-gnu",
    "libc": "glibc",
    "glibc_min": [2, 17],
    "wheel_platform": "manylinux_2_17_x86_64",
}
ENTRYPOINT = "desktop/python-webview-shell/remote_entry.py"
SCHEMA_VERSION = 1
MANIFEST_MODE = 420
GLIBC_MIN = (2, 17)
REQ_NAME = re.compile(r"\A([A-Za-z0-9][A-Za-z0-9._-]*)==(.*)\Z")
HASH_LINE = re.compile(r"\A--hash=sha256:([0-9a-f]{64})\Z")
ROSTER_DIRS = (
    "workstack",
    "contracts",
    "web",
    "frontend/dist",
    "licenses",
)
ROSTER_FILES = (
    "run_work_stack.py",
    "desktop/python-webview-shell/remote_entry.py",
    "desktop/python-webview-shell/remote_command_contract.py",
    "desktop/python-webview-shell/remote_owner.py",
    "desktop/python-webview-shell/remote_process_handle.py",
    "desktop/python-webview-shell/remote_receipt_guard.py",
    "desktop/python-webview-shell/remote_receipt_io.py",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
)
# Exactly the roster dist_source_gate binds frontend/dist to, including the
# Python theme output the prebuild check reads, the fixture tsconfig.app pulls
# in from outside frontend/, and the Vite mode env files whose presence would
# change emitted bytes.
GENERATED_SOURCE_PATHS = (
    "desktop/python-webview-shell/generated/theme_tokens.py",
    "frontend/.env",
    "frontend/.env.local",
    "frontend/.env.production",
    "frontend/.env.production.local",
    "frontend/index.html",
    "frontend/package-lock.json",
    "frontend/package.json",
    "frontend/public",
    "frontend/src",
    "frontend/tsconfig.app.json",
    "frontend/tsconfig.json",
    "frontend/tsconfig.node.json",
    "frontend/vite.config.ts",
    "scripts/generate-theme-tokens.mjs",
    "tests/fixtures/checkpoint_change_v1.json",
    "theme/theme-tokens.json",
)
ADMISSION_PATHS = ROSTER_DIRS + ROSTER_FILES + ("requirements.txt",)
GENERATED_DIR = "frontend/dist"
FROZEN_DIRS = tuple(path for path in ROSTER_DIRS if path != GENERATED_DIR)
FROZEN_ROOTS = tuple(path for path in ADMISSION_PATHS if path != GENERATED_DIR)
# Cleanliness only. These inputs are never payload: the gate binds frontend/dist to
# their content, and this pathspec binds their content to source_commit.
CLEAN_PATHS = ADMISSION_PATHS + GENERATED_SOURCE_PATHS
SKIP_PARTS = frozenset({".git", ".venv", "__pycache__", "venv"})
APPROVED_LOCK = {
    "attrs": "26.1.0",
    "jsonschema": "4.26.0",
    "jsonschema-specifications": "2025.9.1",
    "referencing": "0.37.0",
    "rpds-py": "2026.6.3",
    "typing-extensions": "4.16.0",
    "unicodedata2": "17.0.0",
}


class ArtifactBuildError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json(value: Any) -> bytes:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load_sibling(filename: str, attribute: str | None = None) -> Any:
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location("workstack_" + filename.replace(".", "_"), path)
    if spec is None or spec.loader is None:
        raise ArtifactBuildError("VERIFY", filename + " could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if attribute is None:
        return module
    return getattr(module, attribute)


GIT = load_sibling("linux_remote_artifact_git.py")
ZIP = load_sibling("linux_remote_artifact_zip.py")
DIST_GATE = load_sibling("dist_source_gate.py")
ZIP_TIMESTAMP = ZIP.ZIP_TIMESTAMP
FILE_MODE = ZIP.FILE_MODE
ZIP_MEMBER_MODE = ZIP.ZIP_MEMBER_MODE
ZIP_CREATE_SYSTEM = ZIP.ZIP_CREATE_SYSTEM
ZIP_ALLOWED_FLAGS = ZIP.ZIP_ALLOWED_FLAGS
MAX_ARCHIVE_BYTES = ZIP.MAX_ARCHIVE_BYTES
MAX_FILE_COUNT = ZIP.MAX_FILE_COUNT
MAX_UNCOMPRESSED_BYTES = ZIP.MAX_UNCOMPRESSED_BYTES
MAX_FILE_BYTES = ZIP.MAX_FILE_BYTES
MAX_PATH_BYTES = ZIP.MAX_PATH_BYTES
BYTECODE_SUFFIXES = ZIP.BYTECODE_SUFFIXES


def refuse_special_file(path: Path, label: str) -> None:
    ZIP.refuse_special_file(path, label, ArtifactBuildError)


def posix_relative(root: Path, path: Path) -> str:
    return ZIP.posix_relative(root, path, ArtifactBuildError)


def validate_zip_path(raw: str) -> tuple[str, bool]:
    return ZIP.validate_zip_path(raw, ArtifactBuildError)


def preflight_zip_members(archive: zipfile.ZipFile, label: str) -> None:
    ZIP.preflight_zip_members(archive, label, ArtifactBuildError)


def write_archive(staging: Path, archive: Path, files: list[dict[str, Any]]) -> None:
    ZIP.write_archive(staging, archive, files, ArtifactBuildError)


def parse_requirements(text: str) -> dict[str, tuple[str, frozenset[str]]]:
    if any(token in text for token in (";", "@", "://", "-e ", "--editable", "[")):
        raise ArtifactBuildError("LOCK_BYPASS", "requirements use a marker, URL, extra or editable")
    locked: dict[str, tuple[str, frozenset[str]]] = {}
    current: str | None = None
    version = ""
    hashes: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip().rstrip("\\").strip()
        if not line:
            continue
        matched = REQ_NAME.match(line)
        if matched is not None:
            current, version, hashes = _commit_requirement(locked, current, version, hashes, matched)
            continue
        hash_match = HASH_LINE.match(line)
        if hash_match is None or current is None:
            raise ArtifactBuildError("LOCK_BYPASS", "requirements line is not an exact pin with hashes")
        hashes.add("sha256:" + hash_match.group(1))
    _commit_requirement(locked, current, version, hashes, None)
    if not locked:
        raise ArtifactBuildError("LOCK_BYPASS", "requirements lock is empty")
    return locked


def _commit_requirement(
    locked: dict[str, tuple[str, frozenset[str]]],
    current: str | None,
    version: str,
    hashes: set[str],
    matched: re.Match[str] | None,
) -> tuple[str | None, str, set[str]]:
    if current is not None:
        name = canonical_name(current)
        if name in locked:
            raise ArtifactBuildError("DUPLICATE_WHEEL", "lock repeats " + name)
        if not hashes or not version or any(ch.isspace() for ch in version):
            raise ArtifactBuildError("LOCK_BYPASS", "lock pin is missing version or hash")
        locked[name] = (version, frozenset(hashes))
    if matched is None:
        return None, "", set()
    return matched.group(1), matched.group(2), set()


def require_approved_lock(locked: Mapping[str, tuple[str, frozenset[str]]]) -> None:
    versions = {name: pin[0] for name, pin in locked.items()}
    if versions != APPROVED_LOCK:
        raise ArtifactBuildError(
            "LOCK_BYPASS",
            "builder admits only the frozen official 7-wheel CPython 3.12 requirements lock",
        )


def parse_wheel_filename(name: str) -> tuple[str, str, str, str, str]:
    if not name.endswith(".whl") or name.endswith(".tar.gz"):
        raise ArtifactBuildError("SDIST", name)
    parts = name[:-4].split("-")
    if len(parts) == 6:
        distribution, version, _build, python, abi, platform = parts
    elif len(parts) == 5:
        distribution, version, python, abi, platform = parts
    else:
        raise ArtifactBuildError("WRONG_TAG", "wheel filename is not PEP 427: " + name)
    return distribution, version, python, abi, platform


def expand_tags(python: str, abi: str, platform: str) -> list[str]:
    tags = [
        "-".join((one, two, three))
        for one in python.split(".")
        for two in abi.split(".")
        for three in platform.split(".")
    ]
    return sorted(set(tags))


def manylinux_glibc(platform: str) -> tuple[int, int] | None:
    aliases = {
        "manylinux1_x86_64": (2, 5),
        "manylinux2010_x86_64": (2, 12),
        "manylinux2014_x86_64": (2, 17),
    }
    if platform in aliases:
        return aliases[platform]
    prefix = "manylinux_"
    suffix = "_x86_64"
    if not platform.startswith(prefix) or not platform.endswith(suffix):
        return None
    middle = platform[len(prefix) : -len(suffix)]
    parts = middle.split("_")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return int(parts[0]), int(parts[1])


def python_abi_ok(python: str, abi: str) -> bool:
    if abi == "none":
        return python in {"py3", "py312", "cp312"}
    if abi == "cp312":
        return python == "cp312"
    if abi != "abi3" or not python.startswith("cp3") or not python[3:].isdigit():
        return False
    return int(python[3:]) <= 12


def tag_error(tag: str) -> str | None:
    parts = tag.split("-")
    if len(parts) != 3:
        return "WRONG_TAG"
    python, abi, platform = parts
    if not python_abi_ok(python, abi):
        return "WRONG_TAG"
    if platform == "any":
        return None
    if "musl" in platform:
        return "MUSL"
    glibc = manylinux_glibc(platform)
    if glibc is None:
        return "WRONG_TAG"
    if glibc > GLIBC_MIN:
        return "GLIBC_MISMATCH"
    return None


def compatible_tags(tags: Iterable[str]) -> list[str]:
    """Admit a wheel's whole declared tag set, or none of it.

    A compressed PEP 427 filename expands to several atomic tags, and the
    installer binds the manifest's declared tags back to that expansion. Keeping
    only the compatible subset would therefore build an artifact no installer
    can admit, so a single incompatible component refuses the wheel outright.
    """

    errors: set[str] = set()
    accepted: list[str] = []
    for tag in tags:
        error = tag_error(tag)
        if error is None:
            accepted.append(tag)
        else:
            errors.add(error)
    if accepted and not errors:
        return sorted(set(accepted))
    if "MUSL" in errors:
        raise ArtifactBuildError("MUSL", "wheel tags are musl")
    if "GLIBC_MISMATCH" in errors:
        raise ArtifactBuildError("GLIBC_MISMATCH", "wheel glibc baseline is newer than 2.17")
    raise ArtifactBuildError("WRONG_TAG", "wheel tags are not cp312 manylinux x86_64")


def wheel_metadata_tags(archive: zipfile.ZipFile) -> set[str]:
    names = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
    if len(names) != 1:
        raise ArtifactBuildError("WRONG_TAG", "wheel does not contain exactly one WHEEL metadata file")
    tags: set[str] = set()
    for raw in archive.read(names[0]).decode("utf-8").splitlines():
        if not raw.startswith("Tag:"):
            continue
        parts = raw.split(":", 1)[1].strip().split("-")
        if len(parts) != 3:
            raise ArtifactBuildError("WRONG_TAG", "WHEEL Tag is not python-abi-platform")
        tags.update(expand_tags(parts[0], parts[1], parts[2]))
    if not tags:
        raise ArtifactBuildError("WRONG_TAG", "WHEEL metadata has no Tag lines")
    return tags


def wheel_metadata_identity(archive: zipfile.ZipFile, distribution: str, version: str) -> None:
    names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    if len(names) != 1:
        raise ArtifactBuildError("WRONG_TAG", "wheel does not contain exactly one METADATA file")
    meta_name = ""
    meta_version = ""
    for raw in archive.read(names[0]).decode("utf-8").splitlines():
        if raw.startswith("Name:"):
            meta_name = raw.split(":", 1)[1].strip()
        elif raw.startswith("Version:"):
            meta_version = raw.split(":", 1)[1].strip()
    if canonical_name(meta_name) != canonical_name(distribution):
        raise ArtifactBuildError("WRONG_TAG", "METADATA Name does not match filename")
    if meta_version != version:
        raise ArtifactBuildError("WRONG_VERSION", "METADATA Version does not match filename")


def inspect_wheel(path: Path) -> dict[str, Any]:
    refuse_special_file(path, "wheelhouse")
    if path.name.endswith(".tar.gz") or path.suffix == ".tar":
        raise ArtifactBuildError("SDIST", path.name)
    if path.suffix != ".whl":
        raise ArtifactBuildError("EXTRA_WHEEL", "wheelhouse contains a non-wheel file")
    distribution, version, python, abi, platform = parse_wheel_filename(path.name)
    filename_tags = set(expand_tags(python, abi, platform))
    with zipfile.ZipFile(path) as archive:
        preflight_zip_members(archive, path.name)
        metadata_tags = wheel_metadata_tags(archive)
        if filename_tags != metadata_tags:
            raise ArtifactBuildError("FILENAME_WHEEL_TAG_MISMATCH", path.name)
        wheel_metadata_identity(archive, distribution, version)
    tags = compatible_tags(filename_tags)
    digest = sha256_file(path)
    return {
        "distribution": canonical_name(distribution),
        "version": version,
        "filename": path.name,
        "sha256": digest,
        "tags": tags,
        "path": path,
    }


def manylinux_unicodedata2(wheels: Mapping[str, Mapping[str, Any]]) -> bool:
    record = wheels.get("unicodedata2")
    if record is None:
        return False
    return any(tag_error(tag) is None and tag.split("-")[2] != "any" for tag in record["tags"])


def admit_wheels(wheelhouse: Path, locked: Mapping[str, tuple[str, frozenset[str]]]) -> dict[str, dict[str, Any]]:
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise ArtifactBuildError("MISSING_WHEEL", "wheelhouse is not a regular directory")
    admitted: dict[str, dict[str, Any]] = {}
    for child in sorted(wheelhouse.iterdir(), key=lambda item: item.name):
        record = inspect_wheel(child)
        name = record["distribution"]
        if name in admitted:
            raise ArtifactBuildError("DUPLICATE_WHEEL", name)
        pin = locked.get(name)
        if pin is None:
            raise ArtifactBuildError("EXTRA_WHEEL", record["filename"])
        version, hashes = pin
        if record["version"] != version:
            raise ArtifactBuildError("WRONG_VERSION", record["filename"])
        if record["sha256"] not in hashes:
            raise ArtifactBuildError("UNKNOWN_HASH", record["filename"])
        admitted[name] = record
    if "unicodedata2" in locked and not manylinux_unicodedata2(admitted):
        raise ArtifactBuildError(
            "LINUX_WHEEL_LOCK_MISSING",
            "verified CPython 3.12 manylinux x86_64 unicodedata2 wheel is not in the lock/wheelhouse",
        )
    missing = sorted(name for name in locked if name not in admitted)
    if missing:
        raise ArtifactBuildError("MISSING_WHEEL", ",".join(missing))
    return admitted


def is_cache_path(relative: str) -> bool:
    posix = relative.replace("\\", "/").strip()
    parts = tuple(part for part in posix.split("/") if part and part != ".")
    if any(part in SKIP_PARTS for part in parts):
        return True
    return Path(posix).suffix.lower() in BYTECODE_SUFFIXES


def porcelain_relpath(line: str) -> str:
    rest = line[3:] if len(line) >= 3 else line
    if " -> " in rest:
        rest = rest.split(" -> ", 1)[1]
    return rest.replace("\\", "/")


def assert_clean_source(source: Path) -> None:
    tracked = GIT.git_text(
        source,
        ("status", "--porcelain=v1", "-uall", "--", *CLEAN_PATHS),
        ArtifactBuildError,
    )
    dirty = [line for line in tracked.splitlines() if line and not is_cache_path(porcelain_relpath(line))]
    if dirty:
        raise ArtifactBuildError(
            "SOURCE_DIRTY", "payload roster, requirements lock or frontend build inputs are dirty"
        )


def read_literals(data: bytes) -> tuple[str, int]:
    try:
        parsed = ast.parse(data.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise ArtifactBuildError("SOURCE_DIRTY", "product version literal is missing") from exc
    version = None
    protocol = None
    for node in parsed.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "__version__" and isinstance(node.value, ast.Constant):
            version = node.value.value
        if target.id == "REMOTE_PROTOCOL_VERSION" and isinstance(node.value, ast.Constant):
            protocol = node.value.value
    if not isinstance(version, str) or not version:
        raise ArtifactBuildError("SOURCE_DIRTY", "product version literal is missing")
    if not isinstance(protocol, int):
        raise ArtifactBuildError("SOURCE_DIRTY", "remote protocol literal is missing")
    return version, protocol

def assert_dist_matches_source(source: Path) -> dict[str, Any]:
    """Refuse a dist that is not the tree its recorded build inputs produced.

    Content only: the receipt binds frontend/dist to a SHA-256 digest of the
    frontend build inputs, and assert_clean_source separately binds those
    inputs to source_commit. Neither the commit nor a mtime is evidence here.
    """

    try:
        return DIST_GATE.verify(source)
    except DIST_GATE.DistSourceGateError as failure:
        raise ArtifactBuildError(failure.code, failure.detail) from failure


def assert_staged_dist_admitted(payload: Path, admitted: Mapping[str, Any]) -> dict[str, Any]:
    """Bind the dist bytes actually staged for the archive to the admitted tree.

    frontend/dist is generated and git-ignored, so an ordinary concurrent
    process may rewrite it between the gate call above and the copy inside
    materialize_payload -- and may put it back afterwards. Recomputing a hash of
    the staged copy on its own would only certify whatever was copied. This
    compares that private copy to the manifest assert_dist_matches_source
    admitted, and the live directory is never read again after it.
    """

    try:
        return DIST_GATE.verify_staged_dist(payload / "frontend" / "dist", admitted)
    except DIST_GATE.DistSourceGateError as failure:
        raise ArtifactBuildError(failure.code, failure.detail) from failure


def list_generated_files(source: Path) -> list[Path]:
    files: list[Path] = []
    root = source / "frontend/dist"
    if root.is_symlink() or not root.is_dir():
        raise ArtifactBuildError("NONREGULAR", "frontend/dist must be a real directory")
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = path.relative_to(source).as_posix()
        if is_cache_path(relative):
            continue
        refuse_special_file(path, relative)
        files.append(path)
    if len(files) > MAX_FILE_COUNT:
        raise ArtifactBuildError("BOUNDS", "source roster exceeds 4096 files")
    return files


def write_payload_bytes(payload: Path, occupied: set[str], relative: str, data: bytes) -> None:
    posix = posix_relative(payload, payload.joinpath(*relative.split("/")))
    if posix != relative:
        raise ArtifactBuildError("TRAVERSAL", relative)
    if relative in occupied:
        raise ArtifactBuildError("COLLISION", relative)
    if len(data) > MAX_FILE_BYTES:
        raise ArtifactBuildError("BOUNDS", relative)
    occupied.add(relative)
    dest = payload.joinpath(*relative.split("/"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def copy_tree_file(src: Path, dest: Path, occupied: set[str], relative: str) -> None:
    if relative in occupied:
        raise ArtifactBuildError("COLLISION", relative)
    occupied.add(relative)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest, follow_symlinks=False)


def extract_wheel(record: Mapping[str, Any], payload: Path, occupied: set[str]) -> None:
    with zipfile.ZipFile(record["path"]) as archive:
        preflight_zip_members(archive, record["filename"])
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            relative = posix_relative(payload, payload.joinpath(*name.split("/")))
            data = archive.read(info)
            if len(data) > MAX_FILE_BYTES:
                raise ArtifactBuildError("BOUNDS", relative)
            destination = payload.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative in occupied:
                raise ArtifactBuildError("COLLISION", relative)
            occupied.add(relative)
            destination.write_bytes(data)


def materialize_payload(
    source: Path,
    payload: Path,
    wheels: Mapping[str, Mapping[str, Any]],
    blobs: list[tuple[str, bytes]],
) -> None:
    occupied: set[str] = set()
    for relative, data in blobs:
        if is_cache_path(relative):
            continue
        write_payload_bytes(payload, occupied, relative, data)
    for src in list_generated_files(source):
        relative = posix_relative(source, src)
        copy_tree_file(src, payload.joinpath(*relative.split("/")), occupied, relative)
    if len(occupied) > MAX_FILE_COUNT:
        raise ArtifactBuildError("BOUNDS", "source roster exceeds 4096 files")
    for name in sorted(wheels):
        extract_wheel(wheels[name], payload, occupied)


def file_records(payload: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    uncompressed = 0
    files = [path for path in payload.rglob("*") if path.is_file()]
    if len(files) > MAX_FILE_COUNT:
        raise ArtifactBuildError("BOUNDS", "payload exceeds 4096 files")
    for path in files:
        refuse_special_file(path, "payload")
        relative = posix_relative(payload, path)
        data = path.read_bytes()
        size = len(data)
        if size > MAX_FILE_BYTES:
            raise ArtifactBuildError("BOUNDS", relative)
        uncompressed += size
        if uncompressed > MAX_UNCOMPRESSED_BYTES:
            raise ArtifactBuildError("BOUNDS", "payload uncompressed size exceeds 256 MiB")
        records.append(
            {
                "path": relative,
                "size": size,
                "sha256": sha256_bytes(data),
                "mode": MANIFEST_MODE,
            }
        )
    records.sort(key=lambda item: item["path"].encode("utf-8"))
    require_payload_roster({item["path"] for item in records})
    return records


def require_payload_roster(paths: set[str]) -> None:
    GIT.require_roster(paths, ROSTER_FILES, ROSTER_DIRS, ArtifactBuildError, "payload")


def wheel_records(wheels: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for name in sorted(wheels):
        item = wheels[name]
        records.append({key: item[key] if key != "tags" else list(item["tags"]) for key in (
            "distribution", "version", "filename", "sha256", "tags")})
    return records


def build_manifest(
    *,
    version: str,
    protocol: int,
    commit: str,
    tree: str,
    lock_digest: str,
    wheels: Mapping[str, Mapping[str, Any]],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "product_version": version,
        "remote_protocol_version": protocol,
        "source_commit": commit,
        "source_tree": tree,
        "target": dict(TARGET),
        "entrypoint": ENTRYPOINT,
        "requirements_lock_sha256": lock_digest,
        "wheels": wheel_records(wheels),
        "files": files,
    }


def exclusive_output(output_dir: Path) -> tuple[Path, tuple[int, int]]:
    if not output_dir.is_absolute():
        raise ArtifactBuildError("OUTPUT_EXISTS", "output directory must be an absolute path")
    lexical = Path(os.path.normpath(str(output_dir)))
    if os.path.lexists(lexical):
        raise ArtifactBuildError("OUTPUT_EXISTS", "output directory already exists")
    dest = lexical.parent.resolve() / lexical.name
    if os.path.lexists(dest):
        raise ArtifactBuildError("OUTPUT_EXISTS", "output directory already exists")
    os.mkdir(dest)
    status = os.lstat(dest)
    return dest, (int(status.st_dev), int(status.st_ino))


def cleanup_owned(path: Path | None, identity: tuple[int, int] | None) -> None:
    if path is None or identity is None or not os.path.lexists(path):
        return
    status = os.lstat(path)
    if (int(status.st_dev), int(status.st_ino)) != identity:
        return
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        return
    shutil.rmtree(path)


def write_sidecar(
    path: Path,
    *,
    version: str,
    protocol: int,
    commit: str,
    archive: Path,
    manifest_digest: str,
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "product_version": version,
        "remote_protocol_version": protocol,
        "source_commit": commit,
        "target_id": TARGET_ID,
        "archive": {
            "name": archive.name,
            "size": archive.stat().st_size,
            "sha256": sha256_file(archive),
        },
        "artifact_manifest_sha256": manifest_digest,
    }
    path.write_bytes(canonical_json(payload))


def build_artifact(source_root: Path, wheelhouse: Path, output_dir: Path, target: str) -> tuple[Path, Path]:
    if target != TARGET_ID:
        raise ArtifactBuildError("TARGET", "unsupported target")
    source = source_root.resolve()
    wheels_root = wheelhouse.resolve()
    identity: tuple[int, int] | None = None
    output: Path | None = None
    try:
        commit, tree = GIT.read_identity(source, ArtifactBuildError)
        assert_clean_source(source)
        lock_bytes, payload_blobs = GIT.frozen_payload_and_lock(
            source, commit, FROZEN_ROOTS, "requirements.txt", ArtifactBuildError
        )
        GIT.require_identity(source, commit, tree, ArtifactBuildError)
        frozen = {path for path, _data in payload_blobs}
        GIT.require_roster(frozen, ROSTER_FILES, FROZEN_DIRS, ArtifactBuildError, "frozen commit")
        init_bytes = dict(payload_blobs).get("workstack/__init__.py")
        if init_bytes is None:
            raise ArtifactBuildError("SOURCE_DIRTY", "workstack/__init__.py is missing")
        version, protocol = read_literals(init_bytes)
        locked = parse_requirements(lock_bytes.decode("utf-8"))
        require_approved_lock(locked)
        wheels = admit_wheels(wheels_root, locked)
        admitted_dist = assert_dist_matches_source(source)
        list_generated_files(source)
        output, identity = exclusive_output(output_dir)
        staging = output / "staging"
        payload = staging / "payload"
        payload.mkdir(parents=True)
        materialize_payload(source, payload, wheels, payload_blobs)
        assert_staged_dist_admitted(payload, admitted_dist)
        files = file_records(payload)
        manifest = build_manifest(
            version=version,
            protocol=protocol,
            commit=commit,
            tree=tree,
            lock_digest=sha256_bytes(lock_bytes),
            wheels=wheels,
            files=files,
        )
        manifest_bytes = canonical_json(manifest)
        (staging / "artifact.json").write_bytes(manifest_bytes)
        archive_name = f"WorkStack-Linux-{version}-{TARGET_ID}.zip"
        archive = output / archive_name
        write_archive(staging, archive, files)
        sidecar = output / f"WorkStack-Linux-{version}-{TARGET_ID}.json"
        write_sidecar(
            sidecar,
            version=version,
            protocol=protocol,
            commit=commit,
            archive=archive,
            manifest_digest=sha256_bytes(manifest_bytes),
        )
        shutil.rmtree(staging)
        leftover = sorted(path.name for path in output.iterdir())
        if leftover != sorted([archive.name, sidecar.name]):
            raise ArtifactBuildError("VERIFY", "output directory must contain exactly two files")
        return archive, sidecar
    except Exception:
        cleanup_owned(output, identity)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--target", required=True)
    options = parser.parse_args(argv)
    try:
        build_artifact(options.source_root, options.wheelhouse, options.output_dir, options.target)
    except ArtifactBuildError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
