"""Dirfd-anchored Linux operations for the remote installer engine.

Import is effect-free. This leaf does not import the admission module or any
product package. Callers load it under the fixed identity
``remote_provision_installer_linux``.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
import uuid
from collections.abc import Mapping


MAX_STDOUT = 4096
MAX_STDERR = 512
MAX_FILE = 32 * 1024 * 1024
MAX_META = 4096
STAGE_TRIES = 3
SMOKE_SECS = 15.0
# Sole release identity; imported by the admission module, test-pinned to workstack/__init__.py.
PRODUCT = "1.0.13"
PROTOCOL = 1
ENTRYPOINT = "desktop/python-webview-shell/remote_entry.py"
SOABI = "cpython-312-x86_64-linux-gnu"
RECEIPT = ".workstack-install.json"
SYS_RENAMEAT2 = 316
RENAME_NOREPLACE = 1
PROBE_KEYS = ("workspace_id", "product_version", "protocol_version")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STORE_KEYS = frozenset({"version", "store_schema_version", "migrations"})
MIGRATION_KEYS = frozenset({"identity", "planning_status"})
EVIDENCE_KEYS = frozenset({"id", "origin", "source_sha256"})
SMOKE_MODS = ("workstack", "jsonschema", "unicodedata2", "rpds")


class InstallerError(RuntimeError):
    """Stable-code failure without path, user, member, or exception text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(ok: bool, code: str = "REMOTE_INSTALL_FAILED") -> None:
    if not ok:
        raise InstallerError(code)


def _ancestor_hop_allowed(
    uid: int,
    mode: int,
    *,
    euid: int,
    leaf: bool,
    child_uid: int | None = None,
) -> bool:
    """Trusted hop for absolute-path smoke, not a substitute for dirfd writes."""

    if not stat.S_ISDIR(mode):
        return False
    writable = bool(mode & 0o022)
    sticky = bool(mode & stat.S_ISVTX)
    if leaf:
        return uid == euid and not writable
    if uid not in {0, euid}:
        return False
    if not writable:
        return True
    if uid != 0 or not sticky:
        return False
    return child_uid in {0, euid}


def _parent_name(path: str) -> tuple[str, str]:
    parent, name = path.rsplit("/", 1)
    return ("/" if parent == "" else parent), name


def _unique_pairs(pairs: list[tuple[object, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _no_const(value: str) -> object:
    raise ValueError("invalid JSON constant")


def _data_object(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_no_const)
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        raise InstallerError("DATA_STATE_UNKNOWN") from None
    _require(type(value) is dict, "DATA_STATE_UNKNOWN")
    return value


def _evidence_record(value: object, *, planning: bool = False, reports: bool = False, knowledge: bool = False) -> bool:
    if type(value) is not dict or set(value) != EVIDENCE_KEYS:
        return False
    if type(value["id"]) is not str or type(value["origin"]) is not str:
        return False
    digest, origin = value["source_sha256"], value["origin"]
    if knowledge:
        expected = "workstack.knowledge.v6" if origin == "fresh" else "workstack.knowledge.v5-to-v6"
        migrated = {"migrated_v1", "migrated_v2", "migrated_v3", "migrated_v5"}
    elif reports:
        expected = "workstack.reports.v5" if origin == "fresh" else "workstack.reports.v3-to-v5"
        migrated = {"migrated_v1", "migrated_v2", "migrated_v3"}
    elif planning:
        expected = "workstack.planning-status.v1"
        migrated = {"migrated_v1", "migrated_v2"}
    else:
        expected = "workstack.store.v2" if origin == "fresh" else "workstack.store.v1-to-v2"
        migrated = {"migrated_v1"}
    if value["id"] != expected:
        return False
    if origin == "fresh":
        return digest is None
    if origin not in migrated or type(digest) is not str:
        return False
    return DIGEST_RE.fullmatch(digest) is not None


def _store_meta_ok(value: object) -> bool:
    if type(value) is not dict or set(value) != STORE_KEYS:
        return False
    if type(value["version"]) is not int or value["version"] != 2:
        return False
    schema, migrations = value["store_schema_version"], value["migrations"]
    keys = {3: MIGRATION_KEYS, 5: MIGRATION_KEYS | {"reports"}, 6: MIGRATION_KEYS | {"reports", "knowledge"}}
    if type(schema) is not int or schema not in keys or type(migrations) is not dict or set(migrations) != keys[schema]:
        return False
    identity = _evidence_record(migrations["identity"])
    planning = _evidence_record(migrations["planning_status"], planning=True)
    reports = schema == 3 or _evidence_record(migrations["reports"], reports=True)
    return identity and planning and reports and (
        schema != 6 or _evidence_record(migrations["knowledge"], knowledge=True)
    )


def _data_uid(value: object) -> str | None:
    if type(value) is not dict or type(value.get("id")) is not str:
        return None
    text = str(value["id"])
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError):
        return None
    if text != str(parsed) or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        return None
    return text


class _LinuxInstallerOperations:
    """Dirfd-anchored Linux filesystem and runtime operations."""

    def __init__(self, install_root: str, data_root: str, owner: str, uid: str) -> None:
        self.install_root = install_root
        self.data_root = data_root
        self.owner = owner
        self.expected_uid = uid
        self.parent_path, self.install_base = _parent_name(install_root)
        self.parent_fd = -1
        self.data_fd = -1
        self.stage_fd = -1
        self.parent_id = (0, 0)
        self.data_id = (0, 0)
        self.stage_id = (0, 0)
        self.meta_id = (0, 0)
        self.workspace_file_id = (0, 0)
        self.stage_name = ""
        self.stage_path = ""
        self.stage_ready = False
        self.commit_unknown = False
        self.rename_attempted = False
        self.dir_ids: dict[str, tuple[int, int]] = {}

    def _dir_flags(self, *, follow_root: bool = False) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        return flags if follow_root else flags | os.O_NOFOLLOW

    def _close_fd(self, fd: int) -> None:
        try:
            if fd >= 0:
                os.close(fd)
        except OSError:
            return

    def close_fds(self) -> None:
        for name in ("stage_fd", "data_fd", "parent_fd"):
            fd = getattr(self, name)
            setattr(self, name, -1)
            self._close_fd(fd)

    def _identity(self, fd: int) -> tuple[int, int]:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino

    def _map_open(self, error: OSError, missing: str) -> InstallerError:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            return InstallerError("TARGET_REPARSE_AMBIGUITY")
        if error.errno == errno.EACCES:
            return InstallerError("TARGET_OWNERSHIP_MISMATCH")
        if error.errno == errno.ENOENT:
            return InstallerError(missing)
        return InstallerError("REMOTE_INSTALL_FAILED")

    def _require_leaf_dir(self, fd: int) -> None:
        info = os.fstat(fd)
        euid = os.geteuid()
        _require(_ancestor_hop_allowed(info.st_uid, info.st_mode, euid=euid, leaf=True), "TARGET_OWNERSHIP_MISMATCH")

    def _require_ancestor(self, info: os.stat_result, child_uid: int | None) -> None:
        euid = os.geteuid()
        allowed = _ancestor_hop_allowed(
            info.st_uid,
            info.st_mode,
            euid=euid,
            leaf=False,
            child_uid=child_uid,
        )
        _require(allowed, "TARGET_OWNERSHIP_MISMATCH")

    def _needs_sticky_child(self, info: os.stat_result) -> bool:
        return bool(info.st_uid == 0 and info.st_mode & 0o022 and info.st_mode & stat.S_ISVTX)

    def _glibc_version(self) -> tuple[int, int] | None:
        try:
            text = os.confstr("CS_GNU_LIBC_VERSION")
        except (OSError, ValueError, AttributeError):
            text = None
        parsed = self._parse_glibc(text)
        if parsed is not None:
            return parsed
        try:
            import ctypes

            func = ctypes.CDLL("libc.so.6").gnu_get_libc_version
            func.restype = ctypes.c_char_p
            raw = func()
            return self._parse_glibc(raw.decode("ascii") if raw else None)
        except (OSError, AttributeError, TypeError, ValueError, UnicodeError):
            return None

    def _parse_glibc(self, text: object) -> tuple[int, int] | None:
        if type(text) is not str or not text.startswith("glibc "):
            return None
        parts = text[6:].split(".")
        try:
            return int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            return None

    def admit_runtime(self) -> None:
        import platform
        import pwd
        import sysconfig

        _require(sys.platform.startswith("linux"), "REMOTE_INSTALLER_UNSUPPORTED")
        _require(sys.implementation.name == "cpython", "REMOTE_ARTIFACT_INCOMPATIBLE")
        _require(sys.version_info[:2] == (3, 12), "REMOTE_ARTIFACT_INCOMPATIBLE")
        _require(platform.machine() == "x86_64", "REMOTE_ARTIFACT_INCOMPATIBLE")
        _require(sysconfig.get_config_var("SOABI") == SOABI, "REMOTE_ARTIFACT_INCOMPATIBLE")
        glibc = self._glibc_version()
        _require(glibc is not None and glibc >= (2, 17), "REMOTE_ARTIFACT_INCOMPATIBLE")
        euid = os.geteuid()
        _require(euid != 0, "TARGET_OWNERSHIP_MISMATCH")
        try:
            name = pwd.getpwuid(euid).pw_name
        except (KeyError, OSError, TypeError, AttributeError):
            raise InstallerError("TARGET_OWNERSHIP_MISMATCH") from None
        _require(name == self.owner, "TARGET_OWNERSHIP_MISMATCH")

    def _walk_abs(self, path: str, missing: str) -> int:
        try:
            fd = os.open("/", self._dir_flags(follow_root=True))
        except OSError as error:
            raise self._map_open(error, missing) from None
        root_info = os.fstat(fd)
        parts = [part for part in path.split("/") if part]
        previous = root_info
        try:
            if not parts:
                self._require_leaf_dir(fd)
                return fd
            pending_sticky = self._needs_sticky_child(root_info)
            if not pending_sticky:
                self._require_ancestor(root_info, None)
            for index, part in enumerate(parts):
                nxt = os.open(part, self._dir_flags(), dir_fd=fd)
                os.close(fd)
                fd = nxt
                child = os.fstat(fd)
                if pending_sticky:
                    self._require_ancestor(previous, child.st_uid)
                    pending_sticky = False
                previous = child
                if index == len(parts) - 1:
                    self._require_leaf_dir(fd)
                    continue
                pending_sticky = self._needs_sticky_child(child)
                if not pending_sticky:
                    self._require_ancestor(child, None)
            return fd
        except InstallerError:
            self._close_fd(fd)
            raise
        except OSError as error:
            self._close_fd(fd)
            raise self._map_open(error, missing) from None

    def _path_identity(self, path: str, missing: str) -> tuple[int, int]:
        fd = self._walk_abs(path, missing)
        try:
            return self._identity(fd)
        finally:
            self._close_fd(fd)

    def _require_bound(self, path: str, recorded: tuple[int, int], missing: str, code: str) -> None:
        _require(self._path_identity(path, missing) == recorded, code)

    def _bind_stage_and_data(self) -> None:
        self._require_bound(self.parent_path, self.parent_id, "INVALID_INSTALLER", "REMOTE_INSTALL_FAILED")
        self._require_bound(self.stage_path, self.stage_id, "REMOTE_INSTALL_FAILED", "REMOTE_INSTALL_FAILED")
        self._require_bound(self.data_root, self.data_id, "DATA_STATE_UNKNOWN", "DATA_STATE_UNKNOWN")

    def _held_path(self, fd: int, recorded: tuple[int, int], code: str) -> str:
        _require(fd >= 0 and self._identity(fd) == recorded, code)
        path = "/proc/self/fd/%d" % fd
        try:
            info = os.stat(path)
        except OSError:
            raise InstallerError(code) from None
        _require((info.st_dev, info.st_ino) == recorded, code)
        return path

    def _open_under(self, dir_fd: int, relative: str) -> int:
        fds = [dir_fd]
        try:
            parts = relative.split("/")
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                if index < len(parts) - 1:
                    flags = self._dir_flags()
                fds.append(os.open(part, flags, dir_fd=fds[-1]))
            info = os.fstat(fds[-1])
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "REMOTE_SMOKE_FAILED")
            return fds.pop()
        except OSError:
            raise InstallerError("REMOTE_SMOKE_FAILED") from None
        finally:
            for fd in reversed(fds[1:]):
                self._close_fd(fd)

    def open_roots(self) -> None:
        self.parent_fd = self._walk_abs(self.parent_path, "INVALID_INSTALLER")
        self.parent_id = self._identity(self.parent_fd)
        try:
            self.data_fd = self._walk_abs(self.data_root, "DATA_STATE_UNKNOWN")
        except Exception:
            self._close_fd(self.parent_fd)
            self.parent_fd = -1
            raise
        self.data_id = self._identity(self.data_fd)
        try:
            os.stat(self.install_base, dir_fd=self.parent_fd, follow_symlinks=False)
        except OSError as error:
            if error.errno == errno.ENOENT:
                return
        raise InstallerError("INSTALL_ROOT_EXISTS")

    def _read_named(self, name: str) -> tuple[bytes, tuple[int, int]]:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.data_fd)
        except OSError:
            raise InstallerError("DATA_STATE_UNKNOWN") from None
        try:
            info = os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "DATA_STATE_UNKNOWN")
            _require(0 <= info.st_size <= MAX_META, "DATA_STATE_UNKNOWN")
            data = os.read(fd, MAX_META + 1)
            _require(len(data) == info.st_size, "DATA_STATE_UNKNOWN")
            return data, (info.st_dev, info.st_ino)
        finally:
            self._close_fd(fd)

    def admit_data(self) -> None:
        meta_bytes, self.meta_id = self._read_named("store-meta.json")
        space_bytes, self.workspace_file_id = self._read_named("workspace.json")
        meta = _data_object(meta_bytes)
        space = _data_object(space_bytes)
        _require(_store_meta_ok(meta), "DATA_STATE_UNKNOWN")
        uid = _data_uid(space)
        _require(uid is not None, "DATA_STATE_UNKNOWN")
        _require(uid == self.expected_uid, "REMOTE_WORKSPACE_MISMATCH")
        _require(self._path_identity(self.data_root, "DATA_STATE_UNKNOWN") == self.data_id, "DATA_STATE_UNKNOWN")

    def create_stage(self) -> None:
        for _attempt in range(STAGE_TRIES):
            name = ".%s.staging-%s" % (self.install_base, os.urandom(16).hex())
            try:
                os.mkdir(name, 0o700, dir_fd=self.parent_fd)
            except FileExistsError:
                continue
            except OSError:
                raise InstallerError("REMOTE_INSTALL_FAILED") from None
            self.stage_name = name
            try:
                self.stage_fd = os.open(name, self._dir_flags(), dir_fd=self.parent_fd)
            except OSError:
                try:
                    os.rmdir(name, dir_fd=self.parent_fd)
                except OSError:
                    pass
                self.stage_name = ""
                raise InstallerError("REMOTE_INSTALL_FAILED") from None
            self.stage_id = self._identity(self.stage_fd)
            prefix = "" if self.parent_path == "/" else self.parent_path
            self.stage_path = prefix + "/" + name
            self.stage_ready = True
            return
        raise InstallerError("REMOTE_STAGE_UNAVAILABLE")

    def _ensure_dir(self, dir_fd: int, name: str, rel: str) -> int:
        recorded = self.dir_ids.get(rel)
        try:
            if recorded is None:
                os.mkdir(name, 0o700, dir_fd=dir_fd)
            fd = os.open(name, self._dir_flags(), dir_fd=dir_fd)
        except OSError:
            raise InstallerError("REMOTE_INSTALL_FAILED") from None
        info = os.fstat(fd)
        ident = (info.st_dev, info.st_ino)
        if not stat.S_ISDIR(info.st_mode) or recorded not in {None, ident}:
            self._close_fd(fd)
            raise InstallerError("REMOTE_INSTALL_FAILED")
        self.dir_ids[rel] = ident
        return fd

    def _put_file(self, dir_fd: int, name: str, data: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        except OSError:
            raise InstallerError("REMOTE_INSTALL_FAILED") from None
        try:
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(fd, view[offset:])
                _require(written > 0, "REMOTE_INSTALL_FAILED")
                offset += written
            os.fchmod(fd, 0o644)
            os.fsync(fd)
        finally:
            self._close_fd(fd)

    def write_file(self, relative_path: str, payload: bytes, digest: str) -> None:
        parts = relative_path.split("/")
        fds = [self.stage_fd]
        try:
            prefix = ""
            for part in parts[:-1]:
                prefix = part if not prefix else prefix + "/" + part
                fds.append(self._ensure_dir(fds[-1], part, prefix))
            self._put_file(fds[-1], parts[-1], payload)
            check = "sha256:" + hashlib.sha256(payload).hexdigest()
            _require(check == digest and len(payload) <= MAX_FILE, "REMOTE_ARTIFACT_INVALID")
        finally:
            for fd in reversed(fds[1:]):
                self._close_fd(fd)

    def _chmod_rel(self, rel: str) -> None:
        fds = [self.stage_fd]
        try:
            for part in rel.split("/"):
                fds.append(os.open(part, self._dir_flags(), dir_fd=fds[-1]))
            os.fchmod(fds[-1], 0o755)
            os.fsync(fds[-1])
        except OSError:
            raise InstallerError("REMOTE_INSTALL_FAILED") from None
        finally:
            for fd in reversed(fds[1:]):
                self._close_fd(fd)

    def _seal_directories(self) -> None:
        rels = sorted(self.dir_ids, key=lambda item: item.count("/"), reverse=True)
        for rel in rels:
            self._chmod_rel(rel)
        os.fchmod(self.stage_fd, 0o755)
        os.fsync(self.stage_fd)

    def _tracked_name(self, name: str) -> bool:
        return any(name == prefix or name.startswith(prefix + ".") for prefix in SMOKE_MODS)

    def _module_in_stage(self, module: object, stage: str) -> None:
        path = getattr(module, "__file__", None)
        _require(type(path) is str, "REMOTE_SMOKE_FAILED")
        real_stage = os.path.realpath(stage)
        real_file = os.path.realpath(path)
        inside = real_file == real_stage or real_file.startswith(real_stage + os.sep)
        inside = inside or path == stage or path.startswith(stage.rstrip("/") + "/")
        _require(inside, "REMOTE_SMOKE_FAILED")

    def smoke_imports(self) -> None:
        import importlib

        self._bind_stage_and_data()
        self._seal_directories()
        self._bind_stage_and_data()
        stage = self._held_path(self.stage_fd, self.stage_id, "REMOTE_INSTALL_FAILED")
        saved_path = sys.path[:]
        saved_flag = sys.dont_write_bytecode
        removed: list[tuple[str, object]] = []
        try:
            sys.dont_write_bytecode = True
            sys.path.insert(0, stage)
            for name in list(sys.modules):
                if self._tracked_name(name):
                    removed.append((name, sys.modules.pop(name)))
            loaded = []
            stage = self._held_path(self.stage_fd, self.stage_id, "REMOTE_INSTALL_FAILED")
            for name in SMOKE_MODS:
                loaded.append(importlib.import_module(name))
            for module in loaded:
                self._module_in_stage(module, stage)
            workstack = loaded[0]
            _require(getattr(workstack, "__version__", None) == PRODUCT, "REMOTE_SMOKE_FAILED")
            _require(getattr(workstack, "REMOTE_PROTOCOL_VERSION", None) == PROTOCOL, "REMOTE_SMOKE_FAILED")
            _require(getattr(loaded[2], "unidata_version", None) == "17.0.0", "REMOTE_SMOKE_FAILED")
        except InstallerError:
            raise
        except Exception:
            raise InstallerError("REMOTE_SMOKE_FAILED") from None
        finally:
            sys.path[:] = saved_path
            sys.dont_write_bytecode = saved_flag
            for name in list(sys.modules):
                if self._tracked_name(name):
                    del sys.modules[name]
            for name, module in removed:
                sys.modules[name] = module

    def _kill_reap(self, process: object) -> None:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except Exception:
            return

    def _run_child(self, process: object, timeout: float | None = None) -> tuple[bytes, bytes, bool, bool]:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        limit = SMOKE_SECS if timeout is None else timeout

        def read_out() -> None:
            stream = getattr(process, "stdout", None)
            stdout_chunks.append(stream.read(MAX_STDOUT + 1) if stream else b"")

        def read_err() -> None:
            stream = getattr(process, "stderr", None)
            stderr_chunks.append(stream.read(MAX_STDERR + 1) if stream else b"")

        deadline = time.monotonic() + limit
        out_thread = threading.Thread(target=read_out, daemon=True)
        err_thread = threading.Thread(target=read_err, daemon=True)
        out_thread.start()
        err_thread.start()
        out_thread.join(limit)
        err_thread.join(max(0.0, deadline - time.monotonic()))
        if out_thread.is_alive() or err_thread.is_alive():
            self._kill_reap(process)
            out_thread.join(5)
            err_thread.join(5)
            return b"", b"", False, True
        stdout = stdout_chunks[0] if stdout_chunks else b""
        stderr = stderr_chunks[0] if stderr_chunks else b""
        oversize = len(stdout) > MAX_STDOUT or len(stderr) > MAX_STDERR
        if oversize:
            self._kill_reap(process)
            return stdout, stderr, True, False
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except Exception:
            self._kill_reap(process)
            return stdout, stderr, False, True
        return stdout, stderr, False, False

    def _accept_probe(self, stdout: bytes, stderr: bytes) -> None:
        _require(not stderr, "REMOTE_SMOKE_FAILED")
        _require(stdout.endswith(b"\n") and stdout.count(b"\n") == 1, "REMOTE_SMOKE_FAILED")
        line = stdout[:-1]
        try:
            parsed = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_no_const)
        except (UnicodeError, ValueError, RecursionError, MemoryError):
            raise InstallerError("REMOTE_SMOKE_FAILED") from None
        _require(type(parsed) is dict, "REMOTE_SMOKE_FAILED")
        _require(set(parsed) == set(PROBE_KEYS), "REMOTE_SMOKE_FAILED")
        uid = parsed["workspace_id"]
        product = parsed["product_version"]
        protocol = parsed["protocol_version"]
        _require(type(uid) is str and uid == self.expected_uid, "REMOTE_SMOKE_FAILED")
        _require(type(product) is str and product == PRODUCT, "REMOTE_SMOKE_FAILED")
        _require(type(protocol) is int and protocol == PROTOCOL, "REMOTE_SMOKE_FAILED")
        encoded = json.dumps({key: parsed[key] for key in PROBE_KEYS}, ensure_ascii=True, separators=(",", ":"))
        _require(line == encoded.encode("utf-8"), "REMOTE_SMOKE_FAILED")

    def _assert_data_files(self) -> None:
        _meta, meta_id = self._read_named("store-meta.json")
        _space, space_id = self._read_named("workspace.json")
        _require(meta_id == self.meta_id and space_id == self.workspace_file_id, "DATA_STATE_UNKNOWN")

    def smoke_entrypoint(self) -> None:
        import subprocess

        self._bind_stage_and_data()
        self._assert_data_files()
        self._bind_stage_and_data()
        script_fd = self._open_under(self.stage_fd, ENTRYPOINT)
        try:
            stage = self._held_path(self.stage_fd, self.stage_id, "REMOTE_INSTALL_FAILED")
            data = self._held_path(self.data_fd, self.data_id, "DATA_STATE_UNKNOWN")
            script = "/proc/self/fd/%d" % script_fd
            argv = [sys.executable, "-I", "-B", script, "probe", "--app-dir", stage, "--data-dir", data]
            inherited = (self.stage_fd, self.data_fd, script_fd)
            try:
                process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=None, pass_fds=inherited)
            except OSError:
                raise InstallerError("REMOTE_SMOKE_FAILED") from None
            stdout, stderr, oversize, timed = self._run_child(process)
            _require(not timed and not oversize and getattr(process, "returncode", 1) == 0, "REMOTE_SMOKE_FAILED")
            self._accept_probe(stdout, stderr)
        finally:
            self._close_fd(script_fd)

    def write_receipt(self, document: Mapping[str, object]) -> None:
        payload = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        wanted = {"schema_version", "product_version", "remote_protocol_version", "artifact_digest",
                  "artifact_manifest_sha256", "workspace_uid", "owner", "target"}
        _require(set(document) == wanted and len(payload) <= MAX_STDOUT, "REMOTE_INSTALL_FAILED")
        self._put_file(self.stage_fd, RECEIPT, payload)
        os.fsync(self.stage_fd)

    def _rename_noreplace(self, old_fd: int, old: str, new_fd: int, new: str) -> int:
        """Atomic renameat2 RENAME_NOREPLACE; returns 0 or the failing errno."""

        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        args = (old_fd, os.fsencode(old), new_fd, os.fsencode(new), RENAME_NOREPLACE)
        try:
            func = libc.renameat2
            func.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            func.restype = ctypes.c_int
            result = func(*args)
        except AttributeError:
            libc.syscall.restype = ctypes.c_long
            result = libc.syscall(SYS_RENAMEAT2, *args)
        return 0 if result == 0 else (ctypes.get_errno() or errno.EIO)

    def _renameat2(self) -> None:
        self.rename_attempted = True
        try:
            err = self._rename_noreplace(self.parent_fd, self.stage_name, self.parent_fd, self.install_base)
        except AttributeError:
            raise InstallerError("REMOTE_ATOMIC_COMMIT_UNAVAILABLE") from None
        except OSError:
            self.commit_unknown = True
            raise InstallerError("REMOTE_INSTALL_COMMIT_UNKNOWN") from None
        if err == 0:
            self.stage_ready = False
            return
        if err in {errno.ENOSYS, errno.EINVAL}:
            raise InstallerError("REMOTE_ATOMIC_COMMIT_UNAVAILABLE")
        if err == errno.EEXIST:
            raise InstallerError("INSTALL_ROOT_EXISTS")
        self.commit_unknown = True
        raise InstallerError("REMOTE_INSTALL_COMMIT_UNKNOWN")

    def commit_noreplace(self) -> None:
        self._bind_stage_and_data()
        self._renameat2()

    def fsync_parent(self) -> None:
        try:
            os.fsync(self.parent_fd)
        except OSError:
            self.commit_unknown = True
            raise InstallerError("REMOTE_INSTALL_COMMIT_UNKNOWN") from None

    def _detached_is_held(self, dir_fd: int, token: str, held: int, *, directory: bool) -> bool:
        try:
            info = os.stat(token, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            return False
        kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        return kind and (info.st_dev, info.st_ino) == self._identity(held)

    def _reclaim_held(self, dir_fd: int, name: str, held: int, *, directory: bool) -> None:
        """Detach ``name`` atomically, then reclaim it only if it is the held inode.

        The exposed stage pathname is never an unlink argument. RENAME_NOREPLACE
        moves whatever currently occupies it onto a fresh unguessable sibling
        token; the detached entry is re-identified against the descriptor already
        held, and a foreign occupant is renamed back under its own name instead of
        being removed. Linux has no unlink-by-descriptor call, so reclamation of
        the confirmed-owned inode is the token unlink. A same-uid process that can
        read this directory could still race that token, which no user-space
        scheme can prevent; the boundary this closes is deletion through the
        predictable pathname an attacker can target in advance.
        """

        token = ".reclaim-%s" % os.urandom(16).hex()
        try:
            _require(self._rename_noreplace(dir_fd, name, dir_fd, token) == 0, "REMOTE_INSTALL_FAILED")
            if not self._detached_is_held(dir_fd, token, held, directory=directory):
                self._rename_noreplace(dir_fd, token, dir_fd, name)
                raise InstallerError("REMOTE_INSTALL_FAILED")
            if directory:
                os.rmdir(token, dir_fd=dir_fd)
            else:
                os.unlink(token, dir_fd=dir_fd)
        except (AttributeError, OSError):
            raise InstallerError("REMOTE_INSTALL_FAILED") from None

    def _delete_child(self, dir_fd: int, name: str, prior: tuple[int, int], *, directory: bool) -> None:
        flags = self._dir_flags() if directory else os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            child = os.open(name, flags, dir_fd=dir_fd)
        except OSError:
            raise InstallerError("REMOTE_INSTALL_FAILED") from None
        try:
            opened = os.fstat(child)
            kind = stat.S_ISDIR(opened.st_mode) if directory else stat.S_ISREG(opened.st_mode)
            _require((opened.st_dev, opened.st_ino) == prior and kind, "REMOTE_INSTALL_FAILED")
            if directory:
                self._wipe_dir(child)
            self._reclaim_held(dir_fd, name, child, directory=directory)
        finally:
            self._close_fd(child)

    def _wipe_dir(self, dir_fd: int) -> None:
        try:
            names = os.listdir("/proc/self/fd/%d" % dir_fd)
        except OSError:
            raise InstallerError("REMOTE_INSTALL_FAILED") from None
        for name in names:
            if name in {".", ".."}:
                continue
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                raise InstallerError("REMOTE_INSTALL_FAILED") from None
            prior = (info.st_dev, info.st_ino)
            if stat.S_ISREG(info.st_mode):
                self._delete_child(dir_fd, name, prior, directory=False)
                continue
            _require(stat.S_ISDIR(info.st_mode), "REMOTE_INSTALL_FAILED")
            self._delete_child(dir_fd, name, prior, directory=True)

    def cleanup_stage(self) -> None:
        if not self.stage_name or self.commit_unknown:
            return
        try:
            parent_now = self._path_identity(self.parent_path, "INVALID_INSTALLER")
        except InstallerError:
            return
        if parent_now != self.parent_id:
            return
        try:
            fd = os.open(self.stage_name, self._dir_flags(), dir_fd=self.parent_fd)
        except OSError:
            return
        try:
            if self._identity(fd) != self.stage_id:
                return
            self._wipe_dir(fd)
            self._reclaim_held(self.parent_fd, self.stage_name, fd, directory=True)
        finally:
            self._close_fd(fd)
        self.stage_ready = False
        self.stage_name = ""
