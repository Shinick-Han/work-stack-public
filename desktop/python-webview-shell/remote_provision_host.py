"""Raw host observation seams for the streamed remote provisioning collector.

Every function here is a single, individually replaceable observation of the
machine the collector runs on: the directory/file syscalls the bounded walk
needs, the POSIX user names behind an inode, the running interpreter, and the
CPython/machine/glibc facts the frozen runtime target is compared against. No
policy lives here; refusals and composition belong to the collector.
"""

from __future__ import annotations

import os
import sys

from remote_provision_contract import (
    OWNER_PATTERN,
    PYTHON_VERSION_PATTERN,
    _is_posix,
)


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


def _host_readlinkat(dirfd: int, name: str) -> str:
    target = os.readlink(name, dir_fd=dirfd)
    return target if type(target) is str else os.fsdecode(target)


def _host_fstatvfs(fd: int) -> object:
    return os.fstatvfs(fd)


def _host_fstype(dev: int) -> str | None:
    try:
        token = "%d:%d" % (os.major(dev), os.minor(dev))
        with open("/proc/self/mountinfo", "rb") as handle:
            payload = handle.read(65536)
    except (OSError, AttributeError, OverflowError, ValueError, TypeError):
        return None
    for raw in payload.split(b"\n"):
        try:
            line = raw.decode("ascii")
        except UnicodeError:
            continue
        parts = line.split()
        try:
            dash = parts.index("-")
            name = parts[dash + 1]
        except (ValueError, IndexError):
            continue
        if len(parts) > 2 and parts[2] == token and name:
            return name
    return None


def _open_flags(*names: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    for name in names:
        flags |= getattr(os, name, 0)
    return flags


def _inode(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


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
