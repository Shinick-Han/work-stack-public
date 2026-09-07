"""Self-contained stdlib remote Linux provisioning facts collector.

Streamed on SSH stdin to ``python -I -B -``. This module never imports Work Stack
or local desktop siblings, never writes, and never enumerates a tree.
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import sys
import uuid
from collections.abc import Sequence


MAX_FACTS_BYTES = 4096
MAX_SOURCE_BYTES = 32768
MAX_IDENTITY_BYTES = 4096
MAX_STDERR_BYTES = 512
MAX_DETAIL_LENGTH = 256
MAX_CODE_LENGTH = 64
MAX_PRODUCT_VERSION_LENGTH = 64
MAX_PROTOCOL_VERSION = 1_000_000
COLLECTOR_COMMAND = "provision-facts"
SUPPORTED_OS = "linux"
OWNER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
POSIX_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._/-]+$")
PYTHON_VERSION_PATTERN = re.compile(
    r"^3\.([0-9]|[1-9][0-9])(?:\.([0-9]|[1-9][0-9]{0,2}))?$"
)
FACTS_KEYS = ("os", "python", "install", "data")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
RECEIPT_NAME = ".workstack-install.json"
RECEIPT_KEYS = frozenset(
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
FROZEN_TARGET_JSON = (
    b'{"glibc_min":[2,17],"implementation":"cpython","libc":"glibc",'
    b'"machine":"x86_64","os":"linux","python_major":3,"python_minor":12,'
    b'"python_tag":"cp312","soabi":"cpython-312-x86_64-linux-gnu",'
    b'"wheel_platform":"manylinux_2_17_x86_64"}'
)
STORE_META_KEYS = frozenset({"version", "store_schema_version", "migrations"})
MIGRATION_KEYS = frozenset({"identity", "planning_status"})
EVIDENCE_KEYS = frozenset({"id", "origin", "source_sha256"})


class ProbeError(RuntimeError):
    """Bounded probe failure that must not echo raw stderr or paths."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = _clip_text(code, MAX_CODE_LENGTH)
        self.detail = _clip_text(detail, MAX_DETAIL_LENGTH)
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


def _clip_text(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return value[: maximum - 1] + "…"


def _is_posix(value: object) -> bool:
    if type(value) is not str or not POSIX_PATH_PATTERN.fullmatch(value):
        return False
    if value == "/" or value.endswith("/"):
        return False
    segments = value.split("/")[1:]
    return bool(segments) and not any(segment in {"", ".", ".."} for segment in segments)


def _posix_arg(value: object) -> str:
    if not _is_posix(value):
        raise ProbeError("INVALID_PROBE", "path is not a valid Linux POSIX path")
    if any(ord(character) < 32 or character == "\x7f" for character in value):
        raise ProbeError("INVALID_PROBE", "path is not a valid Linux POSIX path")
    return value


def _owner_arg(value: object) -> str:
    if type(value) is not str or not OWNER_PATTERN.fullmatch(value):
        raise ProbeError("INVALID_PROBE", "owner is not a POSIX user name")
    return value


def _overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def parse_collector_argv(argv: Sequence[str]) -> tuple[str, str, str]:
    if len(argv) != 7:
        raise ProbeError("INVALID_PROBE", "malformed argv")
    if (
        argv[0] != COLLECTOR_COMMAND
        or argv[1] != "--install-root"
        or argv[3] != "--data-root"
        or argv[5] != "--owner"
    ):
        raise ProbeError("INVALID_PROBE", "malformed argv")
    install_root = _posix_arg(argv[2])
    data_root = _posix_arg(argv[4])
    owner = _owner_arg(argv[6])
    if _overlap(install_root, data_root):
        raise ProbeError("INVALID_PROBE", "install and data roots must be separate")
    return install_root, data_root, owner


def _running_on_linux() -> bool:
    return sys.platform.startswith("linux")


def _effective_username() -> str:
    try:
        import pwd

        name = pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError, OSError, AttributeError, TypeError):
        return ""
    if type(name) is not str:
        return ""
    return name


def _owner_name(uid: int) -> str | None:
    try:
        import pwd

        name = pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError, OSError, AttributeError, TypeError):
        return None
    if type(name) is not str or not OWNER_PATTERN.fullmatch(name):
        return None
    return name


def _interpreter_facts() -> dict[str, str] | None:
    path = sys.executable
    if not _is_posix(path):
        return None
    version = "{}.{}.{}".format(*sys.version_info[:3])
    if not PYTHON_VERSION_PATTERN.fullmatch(version):
        return None
    return {"path": path, "version": version}


def _host_open(path: str, flags: int) -> int:
    return os.open(path, flags)


def _host_openat(dirfd: int, name: str, flags: int) -> int:
    return os.open(name, flags, dir_fd=dirfd)


def _host_fstat(fd: int) -> os.stat_result:
    return os.fstat(fd)


def _host_lstatat(dirfd: int, name: str) -> os.stat_result:
    return os.lstat(name, dir_fd=dirfd)


def _host_read_fd(fd: int, limit: int) -> bytes:
    return os.read(fd, limit)


def _host_close(fd: int) -> None:
    os.close(fd)


def _open_flags(*names: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    for name in names:
        flags |= getattr(os, name, 0)
    return flags


def _inode(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _composition_race_hook(stage: str, path: str = "") -> None:
    return None


def _close_fd(fd: int | None) -> None:
    if fd is None:
        return
    try:
        _host_close(fd)
    except OSError:
        pass


def _child_stat(dirfd: int, name: str) -> os.stat_result | None:
    try:
        return _host_lstatat(dirfd, name)
    except FileNotFoundError:
        return None
    except OSError:
        raise ProbeError("INVALID_PROBE", "target path could not be inspected") from None


def _open_dir_child(dirfd: int, name: str, expected: tuple[int, int]) -> tuple[int, str | None] | None:
    try:
        nxt = _host_openat(dirfd, name, _open_flags("O_DIRECTORY", "O_NOFOLLOW"))
    except OSError:
        return None
    try:
        info = _host_fstat(nxt)
    except OSError:
        _close_fd(nxt)
        return None
    if _inode(info) != expected or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _close_fd(nxt)
        return None
    return nxt, _owner_name(info.st_uid)


def _hold_last(
    dirfd: int, name: str, path: str, chain: list[tuple[int, int]], owner: str | None, *, strict: bool
) -> tuple[int | None, tuple[tuple[int, int], ...], bool, str | None, bool]:
    if strict:
        _composition_race_hook("before_root_bind", path)
        info = _child_stat(dirfd, name)
        if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return None, tuple(chain), True, owner, False
        chain[-1] = _inode(info)
    opened = _open_dir_child(dirfd, name, chain[-1])
    if opened is None:
        return None, tuple(chain), True, owner, False
    return opened[0], tuple(chain), True, opened[1], False


def _walk_root(path: str, *, strict: bool = True) -> tuple[int | None, tuple[tuple[int, int], ...], bool, str | None, bool]:
    parts = path.split("/")[1:]
    current: int | None = None
    held: int | None = None
    chain: list[tuple[int, int]] = []
    absent = (None, (), False, None, False)
    try:
        try:
            current = _host_open("/", _open_flags("O_DIRECTORY", "O_NOFOLLOW"))
        except OSError:
            raise ProbeError("INVALID_PROBE", "target path could not be inspected") from None
        for index, part in enumerate(parts):
            info = _child_stat(current, part)
            if info is None:
                return absent
            chain.append(_inode(info))
            last = index == len(parts) - 1
            if stat.S_ISLNK(info.st_mode):
                if last:
                    return None, tuple(chain), True, _owner_name(info.st_uid), True
                if strict:
                    raise ProbeError(
                        "TARGET_REPARSE_AMBIGUITY",
                        "target path is a symlink or reparse point",
                    )
                return absent
            if last:
                owner = _owner_name(info.st_uid)
                if not stat.S_ISDIR(info.st_mode):
                    return None, tuple(chain), True, owner, False
                result = _hold_last(current, part, path, chain, owner, strict=strict)
                held = result[0]
                return result
            if not stat.S_ISDIR(info.st_mode):
                return absent
            opened = _open_dir_child(current, part, _inode(info))
            if opened is None:
                if strict:
                    raise ProbeError(
                        "TARGET_REPARSE_AMBIGUITY",
                        "target path is a symlink or reparse point",
                    )
                return absent
            _close_fd(current)
            current = opened[0]
        return absent
    finally:
        if current is not None and current != held:
            _close_fd(current)


def _chain_holds(path: str, expected: tuple[tuple[int, int], ...]) -> bool:
    fd, chain, exists, _owner, link = _walk_root(path, strict=False)
    _close_fd(fd)
    return exists and not link and chain == expected


def _openat_file(dirfd: int, relative: str) -> int | None:
    flags_dir = _open_flags("O_DIRECTORY", "O_NOFOLLOW")
    flags_file = _open_flags("O_NOFOLLOW", "O_NONBLOCK")
    current = dirfd
    opened: list[int] = []
    try:
        parts = relative.split("/")
        for index, part in enumerate(parts):
            if part in {"", ".", ".."}:
                return None
            last = index == len(parts) - 1
            try:
                nxt = _host_openat(current, part, flags_file if last else flags_dir)
            except (OSError, NotImplementedError, AttributeError, TypeError, ValueError):
                return None
            opened.append(nxt)
            try:
                info = _host_fstat(nxt)
            except OSError:
                return None
            if last:
                opened.pop()
                return nxt
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                return None
            current = nxt
        return None
    finally:
        for fd in opened:
            _close_fd(fd)


def _bounded_read(fd: int) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_IDENTITY_BYTES:
        try:
            piece = _host_read_fd(fd, MAX_IDENTITY_BYTES + 1 - total)
        except OSError:
            return None
        if not piece:
            return b"".join(chunks)
        chunks.append(piece)
        total += len(piece)
    return None


def _read_held_file(dirfd: int, relative: str, *, expected_owner: str | None = None) -> bytes | None:
    fd = _openat_file(dirfd, relative)
    if fd is None:
        return None
    try:
        info = _host_fstat(fd)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        if info.st_nlink != 1:
            return None
        if expected_owner is not None and _owner_name(info.st_uid) != expected_owner:
            return None
        _composition_race_hook("after_leaf_open", relative)
        return _bounded_read(fd)
    except OSError:
        return None
    finally:
        _close_fd(fd)


def _unique_object(pairs: list[tuple[object, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _reject_constant(value: str) -> object:
    raise ValueError("invalid JSON constant")


def _object_from_bytes(payload: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        return None
    if type(value) is not dict:
        return None
    return value


def _evidence_record(value: object, *, planning: bool) -> bool:
    if type(value) is not dict or set(value) != EVIDENCE_KEYS:
        return False
    if type(value["id"]) is not str or type(value["origin"]) is not str:
        return False
    digest = value["source_sha256"]
    origin = value["origin"]
    if planning:
        expected_id = "workstack.planning-status.v1"
        migrated_origins = {"migrated_v1", "migrated_v2"}
    else:
        expected_id = "workstack.store.v2" if origin == "fresh" else "workstack.store.v1-to-v2"
        migrated_origins = {"migrated_v1"}
    if value["id"] != expected_id:
        return False
    if origin == "fresh":
        return digest is None
    return (
        origin in migrated_origins
        and type(digest) is str
        and DIGEST_PATTERN.fullmatch(digest) is not None
    )


def _well_formed_store_meta(value: object) -> bool:
    if type(value) is not dict or set(value) != STORE_META_KEYS:
        return False
    if type(value["version"]) is not int or value["version"] != 2:
        return False
    if type(value["store_schema_version"]) is not int or value["store_schema_version"] != 3:
        return False
    migrations = value["migrations"]
    if type(migrations) is not dict or set(migrations) != MIGRATION_KEYS:
        return False
    return _evidence_record(migrations["identity"], planning=False) and _evidence_record(
        migrations["planning_status"], planning=True
    )


def _canonical_uid(text: object) -> str | None:
    if type(text) is not str:
        return None
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError):
        return None
    if text != str(parsed) or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        return None
    return text


def _workspace_uid(value: object) -> str | None:
    if type(value) is not dict:
        return None
    return _canonical_uid(value.get("id"))


def _assign_constants(tree: ast.AST) -> dict[str, object]:
    constants: dict[str, object] = {}
    wanted = {"__version__", "REMOTE_PROTOCOL_VERSION"}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in wanted and node.value is not None:
                    try:
                        constants[target.id] = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError, TypeError, MemoryError):
                        return {}
    return constants


def _literal_identity(payload: bytes) -> tuple[str | None, int | None]:
    try:
        tree = ast.parse(payload.decode("utf-8"))
        constants = _assign_constants(tree)
    except (UnicodeError, SyntaxError, ValueError, RecursionError, MemoryError):
        return None, None
    version = constants.get("__version__")
    protocol = constants.get("REMOTE_PROTOCOL_VERSION")
    if (
        not isinstance(version, str)
        or not version
        or len(version) > MAX_PRODUCT_VERSION_LENGTH
        or any(ord(character) < 32 for character in version)
    ):
        return None, None
    if type(protocol) is not int or not 0 <= protocol <= MAX_PROTOCOL_VERSION:
        return None, None
    return version, protocol


def _normalized_machine() -> str | None:
    try:
        import platform

        raw = platform.machine()
    except (ImportError, OSError, AttributeError, TypeError):
        return None
    if type(raw) is not str:
        return None
    token = raw.strip().lower().replace("-", "_")
    if token in {"x86_64", "amd64", "x64"}:
        return "x86_64"
    return None


def _soabi() -> object:
    try:
        import sysconfig

        return sysconfig.get_config_var("SOABI")
    except (ImportError, TypeError, ValueError, OSError):
        return None


def _glibc_version() -> tuple[int, int] | None:
    text = _glibc_version_text()
    if type(text) is not str:
        return None
    parts = text.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    major, minor = int(parts[0]), int(parts[1])
    if major > 999 or minor > 999:
        return None
    return major, minor


def _glibc_version_text() -> str | None:
    text = None
    confstr = getattr(os, "confstr", None)
    if callable(confstr):
        try:
            raw = confstr("CS_GNU_LIBC_VERSION")
        except (ValueError, OSError, TypeError, AttributeError):
            raw = None
        if type(raw) is str and raw.startswith("glibc "):
            text = raw[6:]
    if text is None:
        try:
            import ctypes

            fn = ctypes.CDLL("libc.so.6").gnu_get_libc_version
            fn.restype = ctypes.c_char_p
            payload = fn()
        except (OSError, AttributeError, TypeError, ValueError):
            return None
        if type(payload) is bytes:
            try:
                text = payload.decode("ascii")
            except UnicodeError:
                return None
        elif type(payload) is str:
            text = payload
        else:
            return None
    return text


def _runtime_target_ok() -> bool:
    if not _running_on_linux():
        return False
    if getattr(sys.implementation, "name", None) != "cpython":
        return False
    if sys.version_info[:2] != (3, 12):
        return False
    if _normalized_machine() != "x86_64":
        return False
    if _soabi() != "cpython-312-x86_64-linux-gnu":
        return False
    glibc = _glibc_version()
    return glibc is not None and glibc >= (2, 17)


def _frozen_target_ok(value: object) -> bool:
    if type(value) is not dict:
        return False
    try:
        encoded = json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, MemoryError):
        return False
    return encoded == FROZEN_TARGET_JSON


def _parse_canonical_receipt(payload: bytes) -> dict[str, object] | None:
    if payload.startswith(b"\xef\xbb\xbf") or b"\r" in payload:
        return None
    if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        return None
    value = _object_from_bytes(payload)
    if value is None:
        return None
    try:
        encoded = json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, RecursionError, MemoryError):
        return None
    if encoded != payload or set(value) != RECEIPT_KEYS:
        return None
    return value if _receipt_fields_valid(value) else None


def _receipt_fields_valid(value: dict[str, object]) -> bool:
    digest = value["artifact_digest"]
    manifest = value["artifact_manifest_sha256"]
    return (
        type(value["schema_version"]) is int and value["schema_version"] == 1
        and type(value["product_version"]) is str and bool(value["product_version"])
        and type(value["remote_protocol_version"]) is int
        and type(digest) is str and DIGEST_PATTERN.fullmatch(digest) is not None
        and type(manifest) is str and DIGEST_PATTERN.fullmatch(manifest) is not None
        and _canonical_uid(value["workspace_uid"]) is not None
        and type(value["owner"]) is str and OWNER_PATTERN.fullmatch(value["owner"]) is not None
        and _frozen_target_ok(value["target"])
    )


def _canonical_receipt_digest(
    dirfd: int,
    *,
    owner: str,
    product_version: str,
    protocol_version: int,
    workspace_uid: str,
) -> str | None:
    payload = _read_held_file(dirfd, RECEIPT_NAME, expected_owner=owner)
    if payload is None:
        return None
    receipt = _parse_canonical_receipt(payload)
    if receipt is None:
        return None
    if receipt["product_version"] != product_version:
        return None
    if receipt["remote_protocol_version"] != protocol_version:
        return None
    if receipt["owner"] != owner:
        return None
    if receipt["workspace_uid"] != workspace_uid:
        return None
    digest = receipt["artifact_digest"]
    return digest if type(digest) is str else None


def _read_install_identity(dirfd: int) -> tuple[str | None, int | None]:
    payload = _read_held_file(dirfd, "workstack/__init__.py")
    if payload is None:
        return None, None
    return _literal_identity(payload)


def _read_workspace_id(dirfd: int) -> str | None:
    metadata = _read_held_file(dirfd, "store-meta.json")
    workspace = _read_held_file(dirfd, "workspace.json")
    if metadata is None or workspace is None:
        return None
    if not _well_formed_store_meta(_object_from_bytes(metadata)):
        return None
    return _workspace_uid(_object_from_bytes(workspace))


def _install_fields(
    exists: bool,
    owner: str | None,
    symlink: bool,
    *,
    requested_owner: str,
    data_uid: str | None,
    dirfd: int | None,
) -> dict[str, object]:
    product = None
    protocol = None
    digest = None
    if exists and not symlink and dirfd is not None:
        product, protocol = _read_install_identity(dirfd)
        if (
            product is not None
            and protocol is not None
            and owner == requested_owner
            and type(data_uid) is str
            and _runtime_target_ok()
        ):
            digest = _canonical_receipt_digest(
                dirfd,
                owner=requested_owner,
                product_version=product,
                protocol_version=protocol,
                workspace_uid=data_uid,
            )
    return {
        "exists": exists,
        "owner": None if not exists else owner,
        "symlink": False if not exists else symlink,
        "product_version": product,
        "protocol_version": protocol,
        "digest": digest,
    }


def _data_fields(
    exists: bool, owner: str | None, symlink: bool, dirfd: int | None
) -> dict[str, object]:
    workspace_id = None
    if exists and not symlink and dirfd is not None:
        workspace_id = _read_workspace_id(dirfd)
    return {
        "exists": exists,
        "owner": None if not exists else owner,
        "symlink": False if not exists else symlink,
        "workspace_id": workspace_id,
    }


def _compose_facts(install_root: str, data_root: str, owner: str) -> dict[str, object]:
    install_fd = None
    data_fd = None
    try:
        install_fd, install_chain, install_exists, install_owner, install_link = _walk_root(install_root)
        data_fd, data_chain, data_exists, data_owner, data_link = _walk_root(data_root)
        _composition_race_hook("after_root_bind")
        data = _data_fields(data_exists, data_owner, data_link, data_fd)
        data_uid = data["workspace_id"] if type(data["workspace_id"]) is str else None
        install = _install_fields(
            install_exists, install_owner, install_link,
            requested_owner=owner, data_uid=data_uid, dirfd=install_fd,
        )
        _composition_race_hook("before_root_revalidate")
        stable = True
        if install_fd is not None and not _chain_holds(install_root, install_chain):
            stable = False
        if data_fd is not None and not _chain_holds(data_root, data_chain):
            stable = False
        if not stable:
            install = dict(install)
            install["digest"] = None
        return {
            "os": SUPPORTED_OS,
            "python": _interpreter_facts(),
            "install": install,
            "data": data,
        }
    finally:
        _close_fd(install_fd)
        _close_fd(data_fd)


def collect_provision_facts(
    install_root: str, data_root: str, owner: str
) -> dict[str, object]:
    """Return planner-shaped facts. Never writes and never inventories a tree."""

    if not _running_on_linux():
        raise ProbeError("INVALID_PROBE", "remote collector requires Linux")
    if _effective_username() != owner:
        raise ProbeError(
            "TARGET_OWNERSHIP_MISMATCH", "effective user does not match owner"
        )
    return _compose_facts(install_root, data_root, owner)


def encode_facts_line(facts: dict[str, object]) -> bytes:
    encoded = json.dumps(
        facts,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_FACTS_BYTES or b"\n" in encoded or b"\r" in encoded:
        raise ProbeError("INVALID_FACTS", "facts response is not a bounded JSON line")
    return encoded + b"\n"


def _write_collector_error(error: ProbeError) -> None:
    line = f"{error.code}: {error.detail}" if error.detail else error.code
    payload = (line[: MAX_STDERR_BYTES - 1] + "\n").encode("ascii", "replace")
    try:
        sys.stderr.buffer.write(payload[:MAX_STDERR_BYTES])
    except OSError:
        pass


def collector_main(argv: Sequence[str] | None = None) -> int:
    try:
        roots = parse_collector_argv(list(sys.argv[1:] if argv is None else argv))
        sys.stdout.buffer.write(encode_facts_line(collect_provision_facts(*roots)))
        return 0
    except ProbeError as error:
        _write_collector_error(error)
        return 2
    except (RecursionError, UnicodeError, ValueError, OSError, MemoryError):
        _write_collector_error(ProbeError("INVALID_PROBE", "probe failed"))
        return 2


def remote_main(argv: Sequence[str] | None = None) -> int:
    return collector_main(argv)


if __name__ == "__main__":
    raise SystemExit(remote_main(sys.argv[1:]))
