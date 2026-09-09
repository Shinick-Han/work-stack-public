"""Self-contained stdlib remote Linux provisioning facts collector.

Streamed on SSH stdin to ``python -I -B -``. This module never imports Work Stack
or local desktop siblings and never enumerates a tree. The only write is an
opt-in disposable capability scratch under the selected application parent.

Its four checked-in stdlib-only siblings -- ``remote_provision_contract``,
``remote_provision_host``, ``remote_provision_identity`` and
``remote_provision_capability`` -- are composed into the same bounded stdin
stream by ``remote_provision_probe``, so the remote interpreter resolves them
without a filesystem or package dependency.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from collections.abc import Sequence

from remote_provision_capability import (
    NFS_TYPES,
    measured_capability,
    scratch_parent_admitted,
    unmeasured_capability,
)
from remote_provision_contract import (
    COLLECTOR_COMMAND,
    SCRATCH_FLAG,
    ProbeError,
    _link_target_segments,
    _overlap,
    _owner_arg,
    parse_collector_argv,
)
from remote_provision_host import (
    _effective_username,
    _glibc_version,
    _host_close,
    _host_fstat,
    _host_fstatvfs,
    _host_fstype,
    _host_lstatat,
    _host_open,
    _host_openat,
    _host_read_fd,
    _host_readlinkat,
    _inode,
    _interpreter_facts,
    _normalized_machine,
    _open_flags,
    _owner_name,
    _running_on_linux,
    _soabi,
)
from remote_provision_identity import (
    FROZEN_TARGET_JSON,
    _literal_identity,
    _object_from_bytes,
    _parse_canonical_receipt,
    _reject_constant,
    _unique_object,
    _well_formed_store_meta,
    _workspace_uid,
)


MAX_FACTS_BYTES = 4096
MAX_SOURCE_BYTES = 32768
MAX_IDENTITY_BYTES = 4096
MAX_STDERR_BYTES = 512
SUPPORTED_OS = "linux"
FACTS_KEYS = ("os", "python", "install", "data")
OPTIONAL_FACTS_KEYS = ("resolution", "host", "capability")
MAX_SYMLINK_HOPS = 8
SYS_RENAMEAT2 = 316
RENAME_NOREPLACE = 1
RECEIPT_NAME = ".workstack-install.json"


def _rename_noreplace(parent_fd: int, old: str, new: str) -> int:
    import ctypes
    import errno

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    args = (parent_fd, os.fsencode(old), parent_fd, os.fsencode(new), RENAME_NOREPLACE)
    try:
        func = libc.renameat2
        func.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        func.restype = ctypes.c_int
        result = func(*args)
    except AttributeError:
        libc.syscall.restype = ctypes.c_long
        result = libc.syscall(SYS_RENAMEAT2, *args)
    return 0 if result == 0 else (ctypes.get_errno() or errno.EIO)


def _host_capability_scratch(parent: str) -> str:
    import errno

    fd = None
    try:
        fd = _host_open(parent, _open_flags("O_DIRECTORY", "O_NOFOLLOW"))
        for _ in range(2):
            token = "wscap-" + uuid.uuid4().hex[:16]
            dest = token + "d"
            try:
                os.mkdir(token, 0o700, dir_fd=fd)
            except FileExistsError:
                continue
            except PermissionError:
                return "ownership"
            err = _rename_noreplace(fd, token, dest)
            leftover = dest if err == 0 else token
            try:
                os.rmdir(leftover, dir_fd=fd)
            except OSError:
                pass
            if err == 0:
                return "ok"
            if err in {errno.ENOSYS, errno.EINVAL}:
                return "unavailable"
            if err in {errno.EACCES, errno.EPERM}:
                return "ownership"
            if err in {errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP), errno.EIO, errno.ESTALE, errno.EXDEV}:
                return "nfs_failed"
            return "unknown"
        return "unknown"
    except AttributeError:
        return "unavailable"
    except PermissionError:
        return "ownership"
    except OSError:
        return "unknown"
    finally:
        _close_fd(fd)


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


def _parent_posix(path: str) -> str:
    parent = path.rsplit("/", 1)[0]
    return "/" if parent == "" else parent


def _reparse() -> ProbeError:
    return ProbeError("TARGET_REPARSE_AMBIGUITY", "target path is a symlink or reparse point")


def _resolve_configured(path: str) -> str:
    parts = path.split("/")[1:]
    hops = 0
    while True:
        current: int | None = None
        restarted = False
        try:
            try:
                current = _host_open("/", _open_flags("O_DIRECTORY", "O_NOFOLLOW"))
            except OSError:
                raise ProbeError("INVALID_PROBE", "target path could not be inspected") from None
            built: list[str] = []
            index = 0
            while index < len(parts):
                part = parts[index]
                last = index == len(parts) - 1
                info = _child_stat(current, part)
                if info is None:
                    built.extend(parts[index:])
                    return "/" + "/".join(built)
                if stat.S_ISLNK(info.st_mode):
                    if last:
                        built.append(part)
                        return "/" + "/".join(built)
                    hops += 1
                    if hops > MAX_SYMLINK_HOPS:
                        raise _reparse()
                    try:
                        target = _host_readlinkat(current, part)
                    except (OSError, TypeError, ValueError, UnicodeError):
                        raise _reparse() from None
                    resolved = _link_target_segments(target, built)
                    if resolved is None:
                        raise _reparse()
                    parts = resolved + parts[index + 1 :]
                    restarted = True
                    break
                built.append(part)
                if last:
                    return "/" + "/".join(built)
                if not stat.S_ISDIR(info.st_mode):
                    built.extend(parts[index + 1 :])
                    return "/" + "/".join(built)
                opened = _open_dir_child(current, part, _inode(info))
                if opened is None:
                    raise _reparse()
                _close_fd(current)
                current = opened[0]
                index += 1
            if not restarted:
                return "/" + "/".join(built) if built else "/"
        finally:
            _close_fd(current)


def _measure_fs(dirfd: int) -> tuple[str, bool | None]:
    noexec = None
    try:
        flag = getattr(os, "ST_NOEXEC", None)
        if flag is not None:
            noexec = bool(_host_fstatvfs(dirfd).f_flag & flag)
    except (OSError, AttributeError, TypeError):
        noexec = None
    try:
        name = _host_fstype(_host_fstat(dirfd).st_dev)
    except (OSError, AttributeError, TypeError):
        name = None
    if name in NFS_TYPES:
        return "nfs", noexec
    if name:
        return "other", noexec
    return "unknown", noexec


def _capability_fields(
    fstype: str,
    noexec: bool | None,
    scratch_parent: str | None,
    canonical_install: str,
    canonical_data: str,
) -> dict[str, object]:
    if scratch_parent is None:
        return unmeasured_capability(fstype, noexec)
    scratch = _resolve_configured(scratch_parent)
    if not scratch_parent_admitted(
        scratch, _parent_posix(canonical_install), canonical_install, canonical_data
    ):
        raise ProbeError("INVALID_PROBE", "scratch parent must be the application parent")
    return measured_capability(fstype, noexec, _host_capability_scratch(scratch))


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


def _canonical_receipt_digest(
    dirfd: int,
    *,
    owner: str,
    product_version: str,
    protocol_version: int,
    workspace_uid: str,
) -> str | None:
    payload = _read_held_file(dirfd, RECEIPT_NAME, expected_owner=owner)
    receipt = None if payload is None else _parse_canonical_receipt(payload)
    if (
        receipt is None
        or receipt["product_version"] != product_version
        or receipt["remote_protocol_version"] != protocol_version
        or receipt["owner"] != owner
        or receipt["workspace_uid"] != workspace_uid
    ):
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


def _compose_facts(
    install_root: str, data_root: str, owner: str, scratch_parent: str | None
) -> dict[str, object]:
    install_fd = None
    data_fd = None
    parent_fd = None
    try:
        canonical_install = _resolve_configured(install_root)
        canonical_data = _resolve_configured(data_root)
        if _overlap(canonical_install, canonical_data):
            raise ProbeError("INVALID_PROBE", "install and data roots must be separate")
        install_fd, install_chain, install_exists, install_owner, install_link = _walk_root(
            canonical_install
        )
        data_fd, data_chain, data_exists, data_owner, data_link = _walk_root(canonical_data)
        _composition_race_hook("after_root_bind")
        data = _data_fields(data_exists, data_owner, data_link, data_fd)
        data_uid = data["workspace_id"] if type(data["workspace_id"]) is str else None
        install = _install_fields(
            install_exists, install_owner, install_link,
            requested_owner=owner, data_uid=data_uid, dirfd=install_fd,
        )
        _composition_race_hook("before_root_revalidate")
        stable = True
        try:
            if _resolve_configured(install_root) != canonical_install:
                stable = False
            if _resolve_configured(data_root) != canonical_data:
                stable = False
        except ProbeError:
            stable = False
        if install_fd is not None and not _chain_holds(canonical_install, install_chain):
            stable = False
        if data_fd is not None and not _chain_holds(canonical_data, data_chain):
            stable = False
        if not stable:
            install = dict(install)
            install["digest"] = None
        fstype, noexec = "unknown", None
        parent = _parent_posix(canonical_install)
        try:
            parent_fd, _pchain, parent_exists, _powner, parent_link = _walk_root(parent)
            if parent_fd is not None and parent_exists and not parent_link:
                fstype, noexec = _measure_fs(parent_fd)
        except ProbeError:
            pass
        return {
            "os": SUPPORTED_OS,
            "python": _interpreter_facts(),
            "install": install,
            "data": data,
            "resolution": {
                "install": {
                    "configured": install_root,
                    "canonical": canonical_install,
                    "stable": stable,
                },
                "data": {
                    "configured": data_root,
                    "canonical": canonical_data,
                    "stable": stable,
                },
            },
            "host": {
                "runtime": SUPPORTED_OS,
                "machine": _normalized_machine(),
                "binding": "unknown",
            },
            "capability": _capability_fields(
                fstype, noexec, scratch_parent, canonical_install, canonical_data
            ),
        }
    finally:
        _close_fd(install_fd)
        _close_fd(data_fd)
        _close_fd(parent_fd)


def collect_provision_facts(
    install_root: str,
    data_root: str,
    owner: str,
    scratch_parent: str | None = None,
) -> dict[str, object]:
    """Return planner-shaped facts. Never inventories a tree.

    Read-only unless ``scratch_parent`` is supplied. That opt-in argument is
    the only write path: one disposable directory under the selected
    application parent, created and removed to measure atomic publication.
    """

    if not _running_on_linux():
        raise ProbeError("INVALID_PROBE", "remote collector requires Linux")
    if _effective_username() != owner:
        raise ProbeError(
            "TARGET_OWNERSHIP_MISMATCH", "effective user does not match owner"
        )
    return _compose_facts(install_root, data_root, owner, scratch_parent)


def revalidate_provision_resolution(
    install_root: str,
    data_root: str,
    expected_install: str,
    expected_data: str,
) -> None:
    """Refuse apply when configured paths no longer resolve to inspected canonicals."""

    if (
        _resolve_configured(install_root) != expected_install
        or _resolve_configured(data_root) != expected_data
    ):
        raise ProbeError(
            "TARGET_RESOLUTION_DRIFT",
            "configured path no longer resolves to the inspected canonical target",
        )


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
