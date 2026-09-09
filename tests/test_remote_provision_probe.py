from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

from workstack.service import WorkStack
from workstack.store import Store, StoreCorruptError
from workstack.store_rosters import V3_DOCUMENT_NAMES, V5_DOCUMENT_NAMES, V6_DOCUMENT_NAMES

PROBE_PATH = SHELL / "remote_provision_probe.py"
COLLECTOR_PATH = SHELL / "remote_provision_collector.py"
PLAN_PATH = SHELL / "remote_provision_plan.py"
COLLECTOR_SPEC = importlib.util.spec_from_file_location(
    "remote_provision_collector", COLLECTOR_PATH
)
assert COLLECTOR_SPEC is not None and COLLECTOR_SPEC.loader is not None
COLLECTOR = importlib.util.module_from_spec(COLLECTOR_SPEC)
sys.modules["remote_provision_collector"] = COLLECTOR
COLLECTOR_SPEC.loader.exec_module(COLLECTOR)

PROBE_SPEC = importlib.util.spec_from_file_location("remote_provision_probe_test", PROBE_PATH)
assert PROBE_SPEC is not None and PROBE_SPEC.loader is not None
MODULE = importlib.util.module_from_spec(PROBE_SPEC)
sys.modules[PROBE_SPEC.name] = MODULE
PROBE_SPEC.loader.exec_module(MODULE)

PLAN_SPEC = importlib.util.spec_from_file_location("remote_provision_plan_probe_comp", PLAN_PATH)
assert PLAN_SPEC is not None and PLAN_SPEC.loader is not None
PLAN = importlib.util.module_from_spec(PLAN_SPEC)
sys.modules[PLAN_SPEC.name] = PLAN
PLAN_SPEC.loader.exec_module(PLAN)

POSIX_PYTHON = "/workstack-fixture/probe-owner/opt/python"
POSIX_INSTALL = "/workstack-fixture/probe-owner/app"
POSIX_DATA = "/workstack-fixture/probe-owner/data"
OWNER = "probe_owner"
ALIAS = "fixture-linux"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
ARTIFACT_DIGEST = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
MANIFEST_DIGEST = "sha256:fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
CANARY = "CANARY_QX7M2"
PRODUCT_SOURCE = '__version__ = "1.0.7"\nREMOTE_PROTOCOL_VERSION = 1\n'
FROZEN_TARGET = {
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
STORE_META = {
    "migrations": {
        "identity": {
            "id": "workstack.store.v2",
            "origin": "fresh",
            "source_sha256": None,
        },
        "planning_status": {
            "id": "workstack.planning-status.v1",
            "origin": "fresh",
            "source_sha256": None,
        },
    },
    "store_schema_version": 3,
    "version": 2,
}
OTHER_UID = "22222222-2222-4222-8222-222222222222"
NIL_UID = "00000000-0000-0000-0000-000000000000"
LETTER_UID = "abcdefab-cdef-4abc-8def-abcdefabcdef"


def _authority_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def disposable_v6_authority():
    temporary = tempfile.TemporaryDirectory(prefix="ws-remote-v6-")
    store = Store(Path(temporary.name))
    readiness = store.initialize()
    values = {
        name: json.loads((store.root / name).read_text(encoding="utf-8"))
        for name in V6_DOCUMENT_NAMES
    }
    oracle = Store.validate_document_values(values, schema_version=6)
    if oracle.workspace_uid != readiness.workspace_uid:
        raise AssertionError("initialize UID and oracle UID diverged")
    if oracle.schema_version != 6:
        raise AssertionError("current initialize did not admit schema 6")
    meta = (store.root / "store-meta.json").read_bytes()
    workspace = (store.root / "workspace.json").read_bytes()
    return temporary, oracle, values, meta, workspace


def disposable_v5_authority():
    temporary = tempfile.TemporaryDirectory(prefix="ws-remote-v5-")
    store = Store(Path(temporary.name))
    readiness = store.initialize()
    values = {
        name: json.loads((store.root / name).read_text(encoding="utf-8"))
        for name in V5_DOCUMENT_NAMES
    }
    # This build writes schema 6, so a genuine v5 authority is the ten v5 names
    # with the metadata stepped back: the current version and the evidence
    # record schema 6 introduced. The remaining nine payloads are already
    # exactly what a v5 store held.
    metadata = values["store-meta.json"]
    metadata["store_schema_version"] = 5
    metadata["migrations"].pop("knowledge", None)
    oracle = Store.validate_document_values(values, schema_version=5)
    if oracle.workspace_uid != readiness.workspace_uid:
        raise AssertionError("initialize UID and oracle UID diverged")
    meta = _authority_bytes(metadata)
    workspace = (store.root / "workspace.json").read_bytes()
    return temporary, oracle, values, meta, workspace


def genuine_v3_authority():
    temporary, _v5, values, _meta, workspace = disposable_v5_authority()
    v3 = {name: values[name] for name in V3_DOCUMENT_NAMES}
    metadata = json.loads(json.dumps(v3["store-meta.json"]))
    metadata["store_schema_version"] = 3
    del metadata["migrations"]["reports"]
    v3["store-meta.json"] = metadata
    oracle = Store.validate_document_values(v3, schema_version=3)
    return temporary, oracle, v3, _authority_bytes(metadata), workspace


def load_profile(**overrides: object):
    values = {
        "profile_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "label": "Fixture",
        "ssh_host_alias": ALIAS,
        "remote_app_dir": POSIX_INSTALL,
        "remote_data_dir": POSIX_DATA,
        "expected_workspace_id": WORKSPACE_ID,
        "preferred_forward_port": 18765,
        "remote_python": POSIX_PYTHON,
    }
    values.update(overrides)
    from connection_registry import SshConnectionProfile

    return SshConnectionProfile(**values)


def missing_identity() -> dict[str, object]:
    return {
        "exists": False,
        "owner": None,
        "symlink": False,
        "product_version": None,
        "protocol_version": None,
        "digest": None,
    }


def missing_data() -> dict[str, object]:
    return {
        "exists": False,
        "owner": None,
        "symlink": False,
        "workspace_id": None,
    }


def success_facts(*, install=None, data=None, digest=None) -> dict[str, object]:
    install_fields = missing_identity() if install is None else dict(install)
    if digest is not None:
        install_fields["digest"] = digest
    return {
        "os": "linux",
        "python": {"path": POSIX_PYTHON, "version": "3.12.10"},
        "install": install_fields,
        "data": missing_data() if data is None else dict(data),
    }


def encode_line(document: dict[str, object]) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class FakeStdin:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> int:
        self.buffer.extend(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


class HangingStream:
    def read(self, _size: int = -1) -> bytes:
        time.sleep(5)
        return b""

    def close(self) -> None:
        return None


class FakeProcess:
    def __init__(
        self,
        payload: bytes,
        returncode: int = 0,
        *,
        hang: bool = False,
        stderr: bytes = b"",
    ) -> None:
        self.stdin = FakeStdin()
        self.stdout = HangingStream() if hang else io.BytesIO(payload)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.killed = False
        self.hang = hang

    def wait(self, timeout: float | None = None) -> int:
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired(cmd="ssh.exe", timeout=timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.hang = False
        self.returncode = -9


class RecordingFactory:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.command: list[str] | None = None
        self.kwargs: dict[str, object] | None = None
        self.called = 0

    def __call__(self, command: list[str], **kwargs: object) -> FakeProcess:
        self.called += 1
        self.command = list(command)
        self.kwargs = dict(kwargs)
        return self.process


class MappedFS:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.symlinks: set[str] = set()
        self.fifos: set[str] = set()
        self.nlinks: dict[str, int] = {}
        self.uids: dict[str, int] = {}
        self.unreadable: set[str] = set()
        self.short_first: dict[str, int] = {}
        self.open_flags: dict[str, list[int]] = {}
        self.dev = 1
        self.inos: dict[str, int] = {}
        self._next_ino = 100
        self._next_fd = 20
        self._fds: dict[int, dict[str, object]] = {}

    def local(self, posix: str) -> Path:
        return self.root.joinpath(*posix.lstrip("/").split("/"))

    def join(self, parent: str, name: str) -> str:
        if parent == "/":
            return f"/{name}"
        return f"{parent.rstrip('/')}/{name}"

    def ino(self, path: str) -> int:
        if path not in self.inos:
            self._next_ino += 1
            self.inos[path] = self._next_ino
        return self.inos[path]

    def bump_ino(self, path: str) -> None:
        self._next_ino += 1
        self.inos[path] = self._next_ino

    def lstat(self, path: str) -> SimpleNamespace:
        uid = self.uids.get(path, 1000)
        nlink = self.nlinks.get(path, 1)
        identity = {"st_dev": self.dev, "st_ino": self.ino(path)}
        if path in self.symlinks:
            return SimpleNamespace(
                st_mode=stat.S_IFLNK | 0o777, st_uid=uid, st_nlink=nlink, **identity
            )
        if path in self.fifos:
            return SimpleNamespace(
                st_mode=stat.S_IFIFO | 0o644, st_uid=uid, st_nlink=nlink, **identity
            )
        local = self.local(path)
        if not local.exists():
            raise FileNotFoundError(path)
        mode = stat.S_IFDIR | 0o755 if local.is_dir() else stat.S_IFREG | 0o644
        return SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=nlink, **identity)

    def lstatat(self, dirfd: int, name: str) -> SimpleNamespace:
        record = self._fds.get(dirfd)
        if record is None:
            raise OSError("bad fd")
        if name in {"", ".", ".."} or "/" in name:
            raise OSError("invalid name")
        return self.lstat(self.join(str(record["path"]), name))

    def open_path(self, path: str, flags: int) -> int:
        self.open_flags.setdefault(path, []).append(flags)
        directory = bool(flags & getattr(os, "O_DIRECTORY", 0))
        if path in self.unreadable:
            raise OSError("unreadable")
        if path in self.symlinks:
            raise OSError("symlink")
        if path in self.fifos:
            nonblock = getattr(os, "O_NONBLOCK", 0)
            expected = COLLECTOR._open_flags("O_NOFOLLOW", "O_NONBLOCK")
            if (nonblock and not (flags & nonblock)) or (not nonblock and flags != expected):
                raise BlockingIOError("fifo would block")
            info = self.lstat(path)
            fd = self._next_fd
            self._next_fd += 1
            self._fds[fd] = {"path": path, "info": info, "payload": b"", "off": 0}
            return fd
        info = self.lstat(path)
        if directory and not stat.S_ISDIR(info.st_mode):
            raise OSError("not a directory")
        payload = b""
        local = self.local(path)
        if local.is_file():
            payload = local.read_bytes()
        fd = self._next_fd
        self._next_fd += 1
        self._fds[fd] = {"path": path, "info": info, "payload": payload, "off": 0}
        return fd

    def openat(self, dirfd: int, name: str, flags: int) -> int:
        record = self._fds.get(dirfd)
        if record is None:
            raise OSError("bad fd")
        if name in {"", ".", ".."} or "/" in name:
            raise OSError("invalid name")
        return self.open_path(self.join(str(record["path"]), name), flags)

    def fstat(self, fd: int) -> SimpleNamespace:
        return self._fds[fd]["info"]  # type: ignore[return-value]

    def read_fd(self, fd: int, limit: int) -> bytes:
        record = self._fds[fd]
        payload = record["payload"]
        if not isinstance(payload, bytes):
            return b""
        offset = int(record.get("off", 0))
        remaining = len(payload) - offset
        if remaining <= 0:
            return b""
        first = self.short_first.get(str(record["path"]))
        if offset == 0 and first is not None:
            size = min(limit, first, remaining)
        else:
            size = min(limit, remaining)
        chunk = payload[offset : offset + size]
        record["off"] = offset + len(chunk)
        return chunk

    def close(self, fd: int) -> None:
        self._fds.pop(fd, None)


def bind_linux(
    test: unittest.TestCase,
    owner: str = OWNER,
    uid_owners: dict[int, str] | None = None,
) -> None:
    names = {1000: owner}
    if uid_owners:
        names.update(uid_owners)

    def owner_name(uid: int) -> str | None:
        return names.get(uid)

    patches = (
        mock.patch.object(COLLECTOR, "_running_on_linux", return_value=True),
        mock.patch.object(COLLECTOR, "_effective_username", return_value=owner),
        mock.patch.object(
            COLLECTOR,
            "_interpreter_facts",
            return_value={"path": POSIX_PYTHON, "version": "3.12.10"},
        ),
        mock.patch.object(COLLECTOR, "_owner_name", side_effect=owner_name),
    )
    for item in patches:
        item.start()
        test.addCleanup(item.stop)


def bind_runtime(test: unittest.TestCase, ok: bool = True) -> None:
    patcher = mock.patch.object(COLLECTOR, "_runtime_target_ok", return_value=ok)
    patcher.start()
    test.addCleanup(patcher.stop)


def bind_fs(test: unittest.TestCase, mapped: MappedFS) -> None:
    patches = (
        mock.patch.object(COLLECTOR, "_host_lstatat", mapped.lstatat),
        mock.patch.object(COLLECTOR, "_host_open", mapped.open_path),
        mock.patch.object(COLLECTOR, "_host_openat", mapped.openat),
        mock.patch.object(COLLECTOR, "_host_fstat", mapped.fstat),
        mock.patch.object(COLLECTOR, "_host_read_fd", mapped.read_fd),
        mock.patch.object(COLLECTOR, "_host_close", mapped.close),
    )
    for item in patches:
        item.start()
        test.addCleanup(item.stop)


def make_tree(existing_install: bool = False, existing_data: bool = False) -> tuple[Path, MappedFS]:
    raw = tempfile.mkdtemp(prefix="ws-probe-")
    root = Path(raw)
    mapped = MappedFS(root)
    mapped.local("/workstack-fixture/probe-owner").mkdir(parents=True)
    if existing_install:
        package = mapped.local(f"{POSIX_INSTALL}/workstack")
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(PRODUCT_SOURCE, encoding="utf-8")
    if existing_data:
        data = mapped.local(POSIX_DATA)
        data.mkdir(parents=True)
        (data / "workspace.json").write_text(
            json.dumps({"id": WORKSPACE_ID, "name": "Fixture", "version": 2}),
            encoding="utf-8",
        )
        (data / "store-meta.json").write_text(json.dumps(STORE_META), encoding="utf-8")
    return root, mapped


def canonical_receipt(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "product_version": "1.0.7",
        "remote_protocol_version": 1,
        "artifact_digest": ARTIFACT_DIGEST,
        "artifact_manifest_sha256": MANIFEST_DIGEST,
        "workspace_uid": WORKSPACE_ID,
        "owner": OWNER,
        "target": dict(FROZEN_TARGET),
    }
    for key, value in overrides.items():
        if key == "target" and isinstance(value, dict):
            target = dict(FROZEN_TARGET)
            target.update(value)
            document["target"] = target
        else:
            document[key] = value
    return document


def encode_receipt(document: dict[str, object]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def write_receipt(
    mapped: MappedFS,
    document: dict[str, object] | None = None,
    raw: bytes | None = None,
) -> str:
    posix = f"{POSIX_INSTALL}/{MODULE.RECEIPT_NAME}"
    mapped.local(posix).write_bytes(
        raw if raw is not None else encode_receipt(document or canonical_receipt())
    )
    return posix


def existing_install_fields(digest: str | None = None) -> dict[str, object]:
    return {
        "exists": True,
        "owner": OWNER,
        "symlink": False,
        "product_version": "1.0.7",
        "protocol_version": 1,
        "digest": digest,
    }


def existing_data_fields() -> dict[str, object]:
    return {
        "exists": True,
        "owner": OWNER,
        "symlink": False,
        "workspace_id": WORKSPACE_ID,
    }


class RemoteProvisionProbeCommandTest(unittest.TestCase):
    def test_command_mirrors_metadata_ssh_options_and_streams_module_stdin(self) -> None:
        command = MODULE.build_ssh_provision_probe_command(
            load_profile(), OWNER, "ssh.exe"
        )
        self.assertEqual(
            command[:14],
            [
                "ssh.exe",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "PermitLocalCommand=no",
                "-o",
                "ClearAllForwardings=yes",
                "--",
                ALIAS,
            ],
        )
        remote = command[-1]
        self.assertEqual(
            remote,
            f"{POSIX_PYTHON} -I -B - provision-facts --install-root {POSIX_INSTALL} "
            f"--data-root {POSIX_DATA} --owner {OWNER}",
        )
        tokens = remote.split()
        self.assertNotIn("-c", tokens)
        self.assertNotIn("&&", remote)
        self.assertNotIn("$(", remote)
        self.assertNotIn("bash", remote)
        self.assertNotIn("login", remote)
        self.assertNotIn("2>&1", remote)
        self.assertEqual(MODULE.PROBE_TIMEOUT_SECONDS, 15.0)
        self.assertLessEqual(len(COLLECTOR_PATH.read_bytes()), MODULE.MAX_SOURCE_BYTES)
        self.assertLessEqual(len(PROBE_PATH.read_text(encoding="utf-8").splitlines()), 800)
        self.assertLessEqual(len(COLLECTOR_PATH.read_text(encoding="utf-8").splitlines()), 800)

    def test_invalid_config_does_not_open_a_process(self) -> None:
        factory = RecordingFactory(FakeProcess(b"{}\n"))
        cases = (
            load_profile(remote_python=None),
            load_profile(remote_app_dir="/"),
            load_profile(remote_data_dir=POSIX_INSTALL),
        )
        for profile in cases:
            with self.subTest(profile=profile):
                with self.assertRaises(MODULE.ProbeError):
                    MODULE.run_remote_provision_probe(
                        profile, OWNER, ssh_executable="ssh.exe", process_factory=factory
                    )
        with self.assertRaises(MODULE.ProbeError):
            MODULE.run_remote_provision_probe(
                load_profile(), "bad owner", ssh_executable="ssh.exe", process_factory=factory
            )
        self.assertEqual(factory.called, 0)


class RemoteProvisionProbeOracleTest(unittest.TestCase):
    def run_probe(self, process: FakeProcess) -> tuple[dict[str, object], RecordingFactory]:
        factory = RecordingFactory(process)
        result = MODULE.run_remote_provision_probe(
            load_profile(), OWNER, ssh_executable="ssh.exe", process_factory=factory
        )
        return result, factory

    def test_success_facts_and_exact_source_stdin(self) -> None:
        payload = encode_line(success_facts())
        process = FakeProcess(payload)
        result, factory = self.run_probe(process)
        self.assertEqual(result, success_facts())
        self.assertEqual(result["install"]["digest"], None)
        self.assertEqual(factory.command[:14], [
            "ssh.exe", "-T", "-o", "BatchMode=yes", "-o",
            "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o",
            "PermitLocalCommand=no", "-o", "ClearAllForwardings=yes", "--", ALIAS,
        ])
        self.assertEqual(process.stdin.buffer, COLLECTOR_PATH.read_bytes())
        self.assertTrue(process.stdin.closed)
        self.assertEqual(factory.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(factory.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(factory.kwargs["stderr"], subprocess.PIPE)
        self.assertIsNot(factory.kwargs.get("shell"), True)

    def test_extra_duplicate_multiline_and_cr_output_are_invalid(self) -> None:
        extra = success_facts()
        extra["secrets"] = {"token": CANARY}
        duplicate = encode_line(success_facts()).replace(
            b'"os":"linux"', b'"os":"linux","os":"linux"', 1
        )
        line = encode_line(success_facts())
        cases = (
            encode_line(extra),
            duplicate,
            line + line,
            line.replace(b"\n", b"\r\n"),
            line.rstrip() + b"\n \n",
            b"[]\n",
        )
        for payload in cases:
            with self.subTest(payload=payload[:40]):
                process = FakeProcess(payload)
                factory = RecordingFactory(process)
                with self.assertRaises(MODULE.ProbeError) as raised:
                    MODULE.run_remote_provision_probe(
                        load_profile(),
                        OWNER,
                        ssh_executable="ssh.exe",
                        process_factory=factory,
                    )
                self.assertEqual(raised.exception.code, "INVALID_FACTS")
                self.assertNotIn(CANARY, str(raised.exception))

    def test_non_null_digest_round_trips_without_schema_growth(self) -> None:
        facts = success_facts(
            install=existing_install_fields(ARTIFACT_DIGEST),
            data=existing_data_fields(),
        )
        encoded = MODULE.encode_facts_line(facts)
        parsed = MODULE._facts_from_stdout(encoded)
        self.assertEqual(parsed, facts)
        self.assertEqual(parsed["install"]["digest"], ARTIFACT_DIGEST)
        self.assertEqual(set(parsed), set(MODULE.FACTS_KEYS))
        self.assertEqual(
            set(parsed["install"]),
            {"exists", "owner", "symlink", "product_version", "protocol_version", "digest"},
        )
        process = FakeProcess(encoded)
        observed, _factory = self.run_probe(process)
        self.assertEqual(observed["install"]["digest"], ARTIFACT_DIGEST)

    def test_malformed_digest_from_ssh_is_invalid_facts(self) -> None:
        facts = success_facts(
            install=existing_install_fields("sha256:ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef0123456789"),
            data=existing_data_fields(),
        )
        process = FakeProcess(encode_line(facts))
        factory = RecordingFactory(process)
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.run_remote_provision_probe(
                load_profile(), OWNER, ssh_executable="ssh.exe", process_factory=factory
            )
        self.assertEqual(raised.exception.code, "INVALID_FACTS")

    def test_oversize_output_is_killed_and_never_parsed(self) -> None:
        process = FakeProcess(b"x" * (MODULE.MAX_FACTS_BYTES + 1))
        factory = RecordingFactory(process)
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.run_remote_provision_probe(
                load_profile(), OWNER, ssh_executable="ssh.exe", process_factory=factory
            )
        self.assertEqual(raised.exception.code, "PROBE_OVERSIZE")
        self.assertTrue(process.killed)

    def test_timeout_kills_and_reaps(self) -> None:
        process = FakeProcess(b"", hang=True)
        factory = RecordingFactory(process)
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.run_remote_provision_probe(
                load_profile(),
                OWNER,
                ssh_executable="ssh.exe",
                timeout=0.1,
                process_factory=factory,
            )
        self.assertEqual(raised.exception.code, "PROBE_TIMEOUT")
        self.assertTrue(process.killed)

    def test_nonzero_output_is_sanitized(self) -> None:
        secret = f"{CANARY} /workstack-fixture/probe-owner/id_rsa".encode("ascii")
        process = FakeProcess(b"", returncode=255, stderr=secret)
        factory = RecordingFactory(process)
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.run_remote_provision_probe(
                load_profile(), OWNER, ssh_executable="ssh.exe", process_factory=factory
            )
        message = str(raised.exception)
        self.assertEqual(raised.exception.code, "INVALID_PROBE")
        self.assertNotIn(CANARY, message)
        self.assertNotIn("id_rsa", message)
        self.assertNotIn("/workstack-fixture/", message)


class RemoteProvisionProbeCollectorTest(unittest.TestCase):
    def collect(self, mapped: MappedFS) -> dict[str, object]:
        bind_linux(self)
        bind_fs(self, mapped)
        return MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)

    def test_missing_roots_are_false_with_null_identity(self) -> None:
        root, mapped = make_tree()
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        facts = self.collect(mapped)
        self.assertEqual(facts["os"], "linux")
        self.assertEqual(facts["python"], {"path": POSIX_PYTHON, "version": "3.12.10"})
        self.assertEqual(facts["install"], missing_identity())
        self.assertEqual(facts["data"], missing_data())

    def test_existing_roots_read_bounded_metadata_and_keep_digest_null(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        before = tree_hashes(root)
        facts = self.collect(mapped)
        self.assertEqual(tree_hashes(root), before)
        self.assertEqual(facts["install"]["exists"], True)
        self.assertEqual(facts["install"]["owner"], OWNER)
        self.assertEqual(facts["install"]["symlink"], False)
        self.assertEqual(facts["install"]["product_version"], "1.0.7")
        self.assertEqual(facts["install"]["protocol_version"], 1)
        self.assertIsNone(facts["install"]["digest"])
        self.assertEqual(facts["data"]["exists"], True)
        self.assertEqual(facts["data"]["workspace_id"], WORKSPACE_ID)
        self.assertIsNone(facts["install"]["digest"])

    def test_malformed_identity_files_leave_uid_null_and_do_not_write(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        mapped.local(f"{POSIX_DATA}/workspace.json").write_text("{", encoding="utf-8")
        mapped.local(f"{POSIX_INSTALL}/workstack/__init__.py").write_text(
            "version = 1\n", encoding="utf-8"
        )
        before = tree_hashes(root)
        facts = self.collect(mapped)
        self.assertEqual(tree_hashes(root), before)
        self.assertIsNone(facts["data"]["workspace_id"])
        self.assertIsNone(facts["install"]["product_version"])
        self.assertIsNone(facts["install"]["protocol_version"])
        self.assertIsNone(facts["install"]["digest"])

    def test_owner_mismatch_refuses_without_writing(self) -> None:
        root, mapped = make_tree(existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        bind_linux(self, owner="other_owner")
        bind_fs(self, mapped)
        before = tree_hashes(root)
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertEqual(raised.exception.code, "TARGET_OWNERSHIP_MISMATCH")
        self.assertEqual(tree_hashes(root), before)
        self.assertNotIn(POSIX_DATA, str(raised.exception))

    def test_ancestor_symlink_refuses_without_writing(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        mapped.symlinks.add("/workstack-fixture/probe-owner")
        bind_linux(self)
        bind_fs(self, mapped)
        before = tree_hashes(root)
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertEqual(raised.exception.code, "TARGET_REPARSE_AMBIGUITY")
        self.assertEqual(tree_hashes(root), before)
        self.assertNotIn(POSIX_INSTALL, str(raised.exception))
        self.assertNotIn(CANARY, str(raised.exception))

    def test_leaf_symlink_is_reported_without_identity(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        mapped.symlinks.add(POSIX_INSTALL)
        facts = self.collect(mapped)
        self.assertTrue(facts["install"]["exists"])
        self.assertTrue(facts["install"]["symlink"])
        self.assertIsNone(facts["install"]["product_version"])
        self.assertIsNone(facts["install"]["digest"])

    def test_malformed_argv_and_non_linux_are_refused(self) -> None:
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.parse_collector_argv(["provision-facts"])
        self.assertEqual(raised.exception.code, "INVALID_PROBE")
        patcher = mock.patch.object(COLLECTOR, "_running_on_linux", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertIn("Linux", raised.exception.detail)


class RemoteProvisionProbeCompositionTest(unittest.TestCase):
    def plan_document(self, facts: dict[str, object]) -> dict[str, object]:
        document = {
            "schema_version": 1,
            "target": {
                "os": "linux",
                "install_root": POSIX_INSTALL,
                "data_root": POSIX_DATA,
                "owner": OWNER,
                "expected_workspace_id": WORKSPACE_ID,
            },
            "artifact": {
                "product_version": PLAN.__version__,
                "protocol_version": PLAN.REMOTE_PROTOCOL_VERSION,
                "digest": ARTIFACT_DIGEST,
            },
            "facts": facts,
        }
        return PLAN.plan_remote_provision(json.dumps(document, separators=(",", ":")))

    def test_missing_install_stays_plan_only_and_is_not_current(self) -> None:
        payload = encode_line(success_facts())
        process = FakeProcess(payload)
        facts = MODULE.run_remote_provision_probe(
            load_profile(),
            OWNER,
            ssh_executable="ssh.exe",
            process_factory=RecordingFactory(process),
        )
        result = self.plan_document(facts)
        self.assertEqual(result["mode"], "plan_only")
        self.assertEqual(result["verification"], "pending_live_probe")
        self.assertEqual(result["provenance"]["observed"], "unverified")
        self.assertEqual(result["decision"], "install_needed")
        self.assertNotEqual(result["decision"], "current")

    def test_existing_install_without_digest_never_fabricates_current(self) -> None:
        facts = success_facts(
            install={
                "exists": True,
                "owner": OWNER,
                "symlink": False,
                "product_version": PLAN.__version__,
                "protocol_version": PLAN.REMOTE_PROTOCOL_VERSION,
                "digest": None,
            },
            data={
                "exists": True,
                "owner": OWNER,
                "symlink": False,
                "workspace_id": WORKSPACE_ID,
            },
        )
        process = FakeProcess(encode_line(facts))
        observed = MODULE.run_remote_provision_probe(
            load_profile(),
            OWNER,
            ssh_executable="ssh.exe",
            process_factory=RecordingFactory(process),
        )
        self.assertIsNone(observed["install"]["digest"])
        result = self.plan_document(observed)
        self.assertEqual(result["mode"], "plan_only")
        self.assertEqual(result["verification"], "pending_live_probe")
        self.assertEqual(result["decision"], "refused")
        self.assertNotEqual(result["decision"], "current")
        self.assertIn(
            "INSTALL_STATE_UNKNOWN",
            {item["code"] for item in result["diagnostics"]},
        )

    def test_unknown_metadata_refuses_composed_plan_without_writes(self) -> None:
        root, mapped = make_tree(existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        bind_linux(self)
        bind_fs(self, mapped)
        cases = [
            ("store_schema_version", None, 999),
            ("version", None, 2.0),
            ("identity", "id", "unknown"),
            ("identity", "origin", "unknown"),
            ("identity", "source_sha256", ARTIFACT_DIGEST),
            ("planning_status", "id", "unknown"),
            ("planning_status", "origin", "unknown"),
            ("planning_status", "source_sha256", ARTIFACT_DIGEST),
        ]
        for section, field, value in cases:
            with self.subTest(section=section, field=field):
                metadata = json.loads(json.dumps(STORE_META))
                if field is None:
                    metadata[section] = value
                else:
                    metadata["migrations"][section][field] = value
                mapped.local(f"{POSIX_DATA}/store-meta.json").write_text(
                    json.dumps(metadata), encoding="utf-8"
                )
                before = tree_hashes(root)
                facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
                self.assertTrue(facts["data"]["exists"])
                self.assertIsNone(facts["data"]["workspace_id"])
                result = self.plan_document(facts)
                self.assertEqual(result["decision"], "refused")
                self.assertIn("DATA_STATE_UNKNOWN", {item["code"] for item in result["diagnostics"]})
                self.assertEqual(tree_hashes(root), before)

    def test_supported_migration_evidence_retains_identity(self) -> None:
        root, mapped = make_tree(existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        bind_linux(self)
        bind_fs(self, mapped)
        for origin in ("migrated_v1", "migrated_v2"):
            with self.subTest(origin=origin):
                metadata = json.loads(json.dumps(STORE_META))
                metadata["migrations"]["identity"] = {
                    "id": "workstack.store.v1-to-v2",
                    "origin": "migrated_v1",
                    "source_sha256": ARTIFACT_DIGEST,
                }
                metadata["migrations"]["planning_status"].update(
                    origin=origin, source_sha256=ARTIFACT_DIGEST
                )
                path = mapped.local(f"{POSIX_DATA}/store-meta.json")
                path.write_text(json.dumps(metadata), encoding="utf-8")
                facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
                self.assertEqual(facts["data"]["workspace_id"], WORKSPACE_ID)
                metadata["migrations"]["identity"]["source_sha256"] = "invalid"
                path.write_text(json.dumps(metadata), encoding="utf-8")
                facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
                self.assertIsNone(facts["data"]["workspace_id"])

    def test_module_does_not_enumerate_trees_or_use_a_shell(self) -> None:
        called: set[str] = set()
        imported: set[str] = set()
        for path in (PROBE_PATH, COLLECTOR_PATH):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
                if isinstance(node, ast.keyword) and node.arg == "shell":
                    self.fail("shell keyword must not appear")
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
        self.assertNotIn("walk", called)
        self.assertNotIn("listdir", called)
        self.assertNotIn("scandir", called)
        self.assertNotIn("rglob", called)
        self.assertNotIn("communicate", called)
        self.assertNotIn("system", called)
        for name in imported:
            self.assertFalse(name == "workstack" or name.startswith("workstack."))
        collector_tree = ast.parse(COLLECTOR_PATH.read_text(encoding="utf-8"))
        collector_imported: set[str] = set()
        for node in ast.walk(collector_tree):
            if isinstance(node, ast.Import):
                collector_imported.update(alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                collector_imported.add(node.module)
        self.assertNotIn("remote_provision_probe", collector_imported)
        self.assertNotIn("remote_provision_plan", collector_imported)
        self.assertNotIn("remote_command_contract", collector_imported)
        self.assertLessEqual(len(COLLECTOR_PATH.read_bytes()), MODULE.MAX_SOURCE_BYTES)
        self.assertLessEqual(len(PROBE_PATH.read_text(encoding="utf-8").splitlines()), 800)
        self.assertLessEqual(len(COLLECTOR_PATH.read_text(encoding="utf-8").splitlines()), 800)


class RemoteProvisionReceiptProjectionTest(unittest.TestCase):
    def collect(
        self,
        mapped: MappedFS,
        *,
        runtime: bool = True,
        owner: str = OWNER,
        uid_owners: dict[int, str] | None = None,
    ) -> dict[str, object]:
        bind_linux(self, owner=owner, uid_owners=uid_owners)
        bind_runtime(self, runtime)
        bind_fs(self, mapped)
        return MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)

    def plan_projected(self, facts: dict[str, object]) -> dict[str, object]:
        document = {
            "schema_version": 1,
            "target": {
                "os": "linux",
                "install_root": POSIX_INSTALL,
                "data_root": POSIX_DATA,
                "owner": OWNER,
                "expected_workspace_id": WORKSPACE_ID,
            },
            "artifact": {
                "product_version": PLAN.__version__,
                "protocol_version": PLAN.REMOTE_PROTOCOL_VERSION,
                "digest": ARTIFACT_DIGEST,
            },
            "facts": facts,
        }
        return PLAN.plan_remote_provision(json.dumps(document, separators=(",", ":")))

    def test_valid_receipt_projects_digest_and_planner_stays_plan_only_current(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        mapped.local(f"{POSIX_INSTALL}/workstack/__init__.py").write_text(
            f'__version__ = "{PLAN.__version__}"\nREMOTE_PROTOCOL_VERSION = 1\n',
            encoding="utf-8",
        )
        write_receipt(mapped, canonical_receipt(product_version=PLAN.__version__))
        before = tree_hashes(root)
        facts = self.collect(mapped)
        self.assertEqual(tree_hashes(root), before)
        self.assertEqual(facts["install"]["digest"], ARTIFACT_DIGEST)
        self.assertEqual(facts["install"]["product_version"], PLAN.__version__)
        self.assertEqual(facts["install"]["protocol_version"], 1)
        self.assertEqual(facts["data"]["workspace_id"], WORKSPACE_ID)
        parsed = MODULE._facts_from_stdout(MODULE.encode_facts_line(facts))
        self.assertEqual(parsed["install"]["digest"], ARTIFACT_DIGEST)
        result = self.plan_projected(facts)
        self.assertEqual(result["decision"], "current")
        self.assertEqual(result["plan"]["action"], "noop")
        self.assertEqual(result["mode"], "plan_only")
        self.assertEqual(result["verification"], "pending_live_probe")
        self.assertNotIn(CANARY, json.dumps(result))

    def test_migrated_v3_same_uid_projects_digest(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        metadata = json.loads(json.dumps(STORE_META))
        metadata["migrations"]["identity"] = {
            "id": "workstack.store.v1-to-v2",
            "origin": "migrated_v1",
            "source_sha256": ARTIFACT_DIGEST,
        }
        metadata["migrations"]["planning_status"].update(
            origin="migrated_v2", source_sha256=ARTIFACT_DIGEST
        )
        mapped.local(f"{POSIX_DATA}/store-meta.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        write_receipt(mapped)
        before = tree_hashes(root)
        facts = self.collect(mapped)
        self.assertEqual(tree_hashes(root), before)
        self.assertEqual(facts["data"]["workspace_id"], WORKSPACE_ID)
        self.assertEqual(facts["install"]["digest"], ARTIFACT_DIGEST)

    def test_legacy_install_without_receipt_keeps_literals_and_null_digest(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        before = tree_hashes(root)
        facts = self.collect(mapped)
        self.assertEqual(tree_hashes(root), before)
        self.assertEqual(facts["install"]["product_version"], "1.0.7")
        self.assertEqual(facts["install"]["protocol_version"], 1)
        self.assertIsNone(facts["install"]["digest"])
        result = self.plan_projected(facts)
        self.assertEqual(result["decision"], "refused")
        self.assertIn("INSTALL_STATE_UNKNOWN", {item["code"] for item in result["diagnostics"]})

    def test_receipt_leaf_failures_leave_digest_null(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        posix = f"{POSIX_INSTALL}/{MODULE.RECEIPT_NAME}"
        cases = (
            "missing",
            "unreadable",
            "oversize",
            "symlink",
            "directory",
            "wrong_owner",
            "nlink",
        )
        for name in cases:
            with self.subTest(name=name):
                path = mapped.local(posix)
                if path.exists() or path.is_dir():
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.unlink()
                mapped.symlinks.discard(posix)
                mapped.unreadable.discard(posix)
                mapped.nlinks.pop(posix, None)
                mapped.uids.pop(posix, None)
                if name != "missing":
                    if name == "directory":
                        path.mkdir()
                    else:
                        write_receipt(mapped)
                if name == "unreadable":
                    mapped.unreadable.add(posix)
                if name == "oversize":
                    path.write_bytes(b"{" + b"x" * (MODULE.MAX_IDENTITY_BYTES + 1))
                if name == "symlink":
                    mapped.symlinks.add(posix)
                if name == "wrong_owner":
                    mapped.uids[posix] = 2001
                if name == "nlink":
                    mapped.nlinks[posix] = 2
                before = tree_hashes(root)
                facts = self.collect(
                    mapped,
                    uid_owners={1000: OWNER, 2001: "other_owner"} if name == "wrong_owner" else None,
                )
                self.assertEqual(tree_hashes(root), before)
                self.assertEqual(facts["install"]["product_version"], "1.0.7")
                self.assertIsNone(facts["install"]["digest"])
                self.assertNotIn(CANARY, json.dumps(facts))

    def test_canonical_byte_refusals_leave_digest_null(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        valid = encode_receipt(canonical_receipt())
        duplicate = valid.replace(
            b'"owner":"probe_owner"', b'"owner":"probe_owner","owner":"probe_owner"', 1
        )
        extra = canonical_receipt()
        extra["extra"] = True
        missing_key = canonical_receipt()
        del missing_key["owner"]
        unsorted = {
            "workspace_uid": WORKSPACE_ID,
            "owner": OWNER,
            "schema_version": 1,
            "product_version": "1.0.7",
            "remote_protocol_version": 1,
            "artifact_digest": ARTIFACT_DIGEST,
            "artifact_manifest_sha256": MANIFEST_DIGEST,
            "target": dict(FROZEN_TARGET),
        }
        payloads = {
            "bom": b"\xef\xbb\xbf" + valid,
            "utf8": b"\xff" + valid[1:],
            "duplicate": duplicate,
            "extra": encode_receipt(extra),
            "missing": encode_receipt(missing_key),
            "nan": valid.replace(b'"schema_version":1', b'"schema_version":NaN', 1),
            "inf": valid.replace(b'"schema_version":1', b'"schema_version":Infinity', 1),
            "cr": valid.replace(b"\n", b"\r\n"),
            "spaces": json.dumps(canonical_receipt(), indent=2).encode("utf-8") + b"\n",
            "order": json.dumps(unsorted, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            + b"\n",
            "no_lf": valid[:-1],
        }
        for name, raw in payloads.items():
            with self.subTest(name=name):
                write_receipt(mapped, raw=raw)
                before = tree_hashes(root)
                facts = self.collect(mapped)
                self.assertEqual(tree_hashes(root), before)
                self.assertEqual(facts["install"]["product_version"], "1.0.7")
                self.assertIsNone(facts["install"]["digest"], name)

    def test_scalar_and_cross_evidence_refusals_leave_digest_null(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        other_uid = "22222222-2222-4222-8222-222222222222"
        documents = {
            "schema_bool": canonical_receipt(schema_version=True),
            "schema_two": canonical_receipt(schema_version=2),
            "protocol_bool": canonical_receipt(remote_protocol_version=True),
            "uppercase_digest": canonical_receipt(
                artifact_digest=ARTIFACT_DIGEST.replace("abcdef", "ABCDEF")
            ),
            "bad_manifest": canonical_receipt(artifact_manifest_sha256="sha256:not-a-digest"),
            "product_mismatch": canonical_receipt(product_version="1.0.8"),
            "protocol_mismatch": canonical_receipt(remote_protocol_version=2),
            "owner_mismatch": canonical_receipt(owner="other_owner"),
            "uid_mismatch": canonical_receipt(workspace_uid=other_uid),
            "machine": canonical_receipt(target={"machine": "aarch64"}),
            "python": canonical_receipt(target={"python_minor": 11}),
            "soabi": canonical_receipt(target={"soabi": "cpython-311-x86_64-linux-gnu"}),
            "libc": canonical_receipt(target={"libc": "musl"}),
            "old_glibc_target": canonical_receipt(target={"glibc_min": [2, 16]}),
        }
        for name, document in documents.items():
            with self.subTest(name=name):
                write_receipt(mapped, document)
                before = tree_hashes(root)
                facts = self.collect(mapped)
                self.assertEqual(tree_hashes(root), before)
                self.assertEqual(facts["install"]["product_version"], "1.0.7")
                self.assertEqual(facts["install"]["protocol_version"], 1)
                self.assertIsNone(facts["install"]["digest"], name)

    def test_absent_or_different_data_uid_does_not_advertise_digest(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        write_receipt(mapped)
        mapped.local(f"{POSIX_DATA}/workspace.json").write_text("{", encoding="utf-8")
        facts = self.collect(mapped)
        self.assertIsNone(facts["data"]["workspace_id"])
        self.assertEqual(facts["install"]["product_version"], "1.0.7")
        self.assertIsNone(facts["install"]["digest"])
        mapped.local(f"{POSIX_DATA}/workspace.json").write_text(
            json.dumps({"id": "22222222-2222-4222-8222-222222222222", "name": "Fixture", "version": 2}),
            encoding="utf-8",
        )
        facts = self.collect(mapped)
        self.assertEqual(facts["data"]["workspace_id"], "22222222-2222-4222-8222-222222222222")
        self.assertIsNone(facts["install"]["digest"])
        mapped.local(f"{POSIX_DATA}/store-meta.json").write_text("{}", encoding="utf-8")
        mapped.local(f"{POSIX_DATA}/workspace.json").write_text(
            json.dumps({"id": WORKSPACE_ID, "name": "Fixture", "version": 2}),
            encoding="utf-8",
        )
        facts = self.collect(mapped)
        self.assertIsNone(facts["data"]["workspace_id"])
        self.assertIsNone(facts["install"]["digest"])

    def test_runtime_and_product_leaf_failures_leave_digest_null(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        write_receipt(mapped)
        facts = self.collect(mapped, runtime=False)
        self.assertEqual(facts["install"]["product_version"], "1.0.7")
        self.assertIsNone(facts["install"]["digest"])
        mapped.symlinks.add(f"{POSIX_INSTALL}/workstack")
        facts = self.collect(mapped)
        self.assertIsNone(facts["install"]["product_version"])
        self.assertIsNone(facts["install"]["digest"])
        mapped.symlinks.discard(f"{POSIX_INSTALL}/workstack")
        mapped.symlinks.add(f"{POSIX_INSTALL}/workstack/__init__.py")
        facts = self.collect(mapped)
        self.assertIsNone(facts["install"]["product_version"])
        self.assertIsNone(facts["install"]["digest"])
        mapped.symlinks.discard(f"{POSIX_INSTALL}/workstack/__init__.py")
        mapped.local(f"{POSIX_INSTALL}/workstack/__init__.py").write_bytes(
            b"x" * (MODULE.MAX_IDENTITY_BYTES + 1)
        )
        facts = self.collect(mapped)
        self.assertIsNone(facts["install"]["product_version"])
        self.assertIsNone(facts["install"]["digest"])

    def test_unknown_glibc_and_wrong_soabi_leave_digest_null(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        write_receipt(mapped)
        bind_linux(self)
        bind_fs(self, mapped)
        version = mock.patch.object(sys, "version_info", (3, 12, 10, "final", 0))
        version.start()
        self.addCleanup(version.stop)
        with mock.patch.object(COLLECTOR, "_normalized_machine", return_value="x86_64"):
            with mock.patch.object(
                COLLECTOR, "_soabi", return_value="cpython-312-x86_64-linux-gnu"
            ):
                with mock.patch.object(COLLECTOR, "_glibc_version", return_value=None):
                    facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertEqual(facts["install"]["product_version"], "1.0.7")
        self.assertIsNone(facts["install"]["digest"])
        with mock.patch.object(COLLECTOR, "_normalized_machine", return_value="x86_64"):
            with mock.patch.object(COLLECTOR, "_soabi", return_value="cpython-312-aarch64-linux-gnu"):
                with mock.patch.object(COLLECTOR, "_glibc_version", return_value=(2, 17)):
                    facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertIsNone(facts["install"]["digest"])
        with mock.patch.object(COLLECTOR, "_normalized_machine", return_value="x86_64"):
            with mock.patch.object(
                COLLECTOR, "_soabi", return_value="cpython-312-x86_64-linux-gnu"
            ):
                with mock.patch.object(COLLECTOR, "_glibc_version", return_value=(2, 16)):
                    facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertIsNone(facts["install"]["digest"])


class RemoteProvisionRuntimeTargetGateTest(unittest.TestCase):
    """The runtime gate must short-circuit before it ever touches the C library."""

    def refusing_glibc(self) -> mock._patch:
        return mock.patch.object(
            COLLECTOR,
            "_glibc_version",
            side_effect=AssertionError("glibc probed before the earlier gates refused"),
        )

    def test_non_linux_host_refuses_without_probing_glibc(self) -> None:
        with mock.patch.object(COLLECTOR, "_running_on_linux", return_value=False):
            with self.refusing_glibc() as glibc:
                self.assertFalse(COLLECTOR._runtime_target_ok())
        glibc.assert_not_called()

    def test_wrong_implementation_refuses_without_probing_glibc(self) -> None:
        with mock.patch.object(COLLECTOR, "_running_on_linux", return_value=True):
            with mock.patch.object(sys, "implementation", SimpleNamespace(name="pypy")):
                with self.refusing_glibc() as glibc:
                    self.assertFalse(COLLECTOR._runtime_target_ok())
        glibc.assert_not_called()

    def test_wrong_interpreter_version_refuses_without_probing_glibc(self) -> None:
        with mock.patch.object(COLLECTOR, "_running_on_linux", return_value=True):
            with mock.patch.object(sys, "implementation", SimpleNamespace(name="cpython")):
                with mock.patch.object(sys, "version_info", (3, 13, 0, "final", 0)):
                    with self.refusing_glibc() as glibc:
                        self.assertFalse(COLLECTOR._runtime_target_ok())
        glibc.assert_not_called()

    def test_wrong_machine_refuses_without_probing_glibc(self) -> None:
        with mock.patch.object(COLLECTOR, "_running_on_linux", return_value=True):
            with mock.patch.object(sys, "implementation", SimpleNamespace(name="cpython")):
                with mock.patch.object(sys, "version_info", (3, 12, 10, "final", 0)):
                    with mock.patch.object(
                        COLLECTOR, "_normalized_machine", return_value="aarch64"
                    ):
                        with self.refusing_glibc() as glibc:
                            self.assertFalse(COLLECTOR._runtime_target_ok())
        glibc.assert_not_called()

    def test_wrong_soabi_refuses_without_probing_glibc(self) -> None:
        with mock.patch.object(COLLECTOR, "_running_on_linux", return_value=True):
            with mock.patch.object(sys, "implementation", SimpleNamespace(name="cpython")):
                with mock.patch.object(sys, "version_info", (3, 12, 10, "final", 0)):
                    with mock.patch.object(
                        COLLECTOR, "_normalized_machine", return_value="x86_64"
                    ):
                        with mock.patch.object(
                            COLLECTOR, "_soabi", return_value="cpython-312-aarch64-linux-gnu"
                        ):
                            with self.refusing_glibc() as glibc:
                                self.assertFalse(COLLECTOR._runtime_target_ok())
        glibc.assert_not_called()

    def test_raising_glibc_probe_never_escapes_a_non_linux_refusal(self) -> None:
        with mock.patch.object(COLLECTOR, "_running_on_linux", return_value=False):
            with mock.patch.object(
                COLLECTOR, "_glibc_version", side_effect=RuntimeError("no libc here")
            ):
                self.assertFalse(COLLECTOR._runtime_target_ok())

    def test_matching_linux_host_still_admits_and_probes_glibc(self) -> None:
        with mock.patch.object(COLLECTOR, "_running_on_linux", return_value=True):
            with mock.patch.object(sys, "implementation", SimpleNamespace(name="cpython")):
                with mock.patch.object(sys, "version_info", (3, 12, 10, "final", 0)):
                    with mock.patch.object(
                        COLLECTOR, "_normalized_machine", return_value="x86_64"
                    ):
                        with mock.patch.object(
                            COLLECTOR, "_soabi", return_value="cpython-312-x86_64-linux-gnu"
                        ):
                            with mock.patch.object(
                                COLLECTOR, "_glibc_version", return_value=(2, 17)
                            ) as glibc:
                                self.assertTrue(COLLECTOR._runtime_target_ok())
                            self.assertEqual(glibc.call_count, 1)
                            with mock.patch.object(
                                COLLECTOR, "_glibc_version", return_value=(2, 16)
                            ):
                                self.assertFalse(COLLECTOR._runtime_target_ok())
                            with mock.patch.object(
                                COLLECTOR, "_glibc_version", return_value=None
                            ):
                                self.assertFalse(COLLECTOR._runtime_target_ok())


class RemoteProvisionIsolationAndRaceTest(unittest.TestCase):
    def test_streamed_isolated_collector_runs_without_workstack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            (outside / "workstack.py").write_text(
                "raise RuntimeError('malicious lookalike executed')\n",
                encoding="utf-8",
            )
            (outside / "remote_provision_probe.py").write_text(
                "raise RuntimeError('malicious lookalike executed')\n",
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(outside)
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-",
                    "provision-facts",
                    "--install-root",
                    POSIX_INSTALL,
                    "--data-root",
                    POSIX_DATA,
                    "--owner",
                    OWNER,
                ],
                cwd=outside,
                env=environment,
                input=COLLECTOR_PATH.read_bytes(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )
        stderr = result.stderr.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 2, stderr)
        self.assertNotIn("ModuleNotFoundError", stderr)
        self.assertNotIn("malicious lookalike executed", stderr)
        self.assertIn("INVALID_PROBE", stderr)

    def test_root_path_replacement_nulls_digest_via_path_identity(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        write_receipt(mapped)
        before = tree_hashes(root)

        def hook(stage: str, path: str = "") -> None:
            if stage == "after_root_bind":
                mapped.bump_ino(POSIX_INSTALL)

        bind_linux(self)
        bind_runtime(self)
        bind_fs(self, mapped)
        with mock.patch.object(COLLECTOR, "_composition_race_hook", hook):
            facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertEqual(tree_hashes(root), before)
        self.assertEqual(facts["install"]["product_version"], "1.0.7")
        self.assertEqual(facts["data"]["workspace_id"], WORKSPACE_ID)
        self.assertIsNone(facts["install"]["digest"])

    def test_data_root_replacement_nulls_digest_via_path_identity(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        write_receipt(mapped)

        def hook(stage: str, path: str = "") -> None:
            if stage == "before_root_revalidate":
                mapped.bump_ino(POSIX_DATA)

        bind_linux(self)
        bind_runtime(self)
        bind_fs(self, mapped)
        with mock.patch.object(COLLECTOR, "_composition_race_hook", hook):
            facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertEqual(facts["install"]["product_version"], "1.0.7")
        self.assertIsNone(facts["install"]["digest"])

    def test_receipt_leaf_replacement_after_open_reads_held_inode(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        posix = write_receipt(mapped)
        replaced = {"done": False}

        def hook(stage: str, path: str = "") -> None:
            if stage == "after_leaf_open" and path == MODULE.RECEIPT_NAME and not replaced["done"]:
                mapped.local(posix).write_text(
                    '{"canary":"%s"}\n' % CANARY,
                    encoding="utf-8",
                )
                mapped.bump_ino(posix)
                replaced["done"] = True

        bind_linux(self)
        bind_runtime(self)
        bind_fs(self, mapped)
        with mock.patch.object(COLLECTOR, "_composition_race_hook", hook):
            facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertEqual(facts["install"]["digest"], ARTIFACT_DIGEST)
        self.assertNotIn(CANARY, json.dumps(facts))

    def test_ancestor_symlink_after_bind_nulls_digest(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        write_receipt(mapped)
        before = tree_hashes(root)

        def hook(stage: str, path: str = "") -> None:
            if stage == "after_root_bind":
                mapped.symlinks.add("/workstack-fixture/probe-owner")

        bind_linux(self)
        bind_runtime(self)
        bind_fs(self, mapped)
        with mock.patch.object(COLLECTOR, "_composition_race_hook", hook):
            facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertEqual(tree_hashes(root), before)
        self.assertEqual(facts["install"]["product_version"], "1.0.7")
        self.assertEqual(facts["data"]["workspace_id"], WORKSPACE_ID)
        self.assertIsNone(facts["install"]["digest"])

    def test_root_replaced_with_other_owner_before_bind_nulls_digest(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        write_receipt(mapped)

        def hook(stage: str, path: str = "") -> None:
            if stage == "before_root_bind" and path == POSIX_INSTALL:
                mapped.bump_ino(POSIX_INSTALL)
                mapped.uids[POSIX_INSTALL] = 2001

        bind_linux(self, uid_owners={1000: OWNER, 2001: "other_owner"})
        bind_runtime(self)
        bind_fs(self, mapped)
        with mock.patch.object(COLLECTOR, "_composition_race_hook", hook):
            facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertEqual(facts["install"]["owner"], "other_owner")
        self.assertEqual(facts["install"]["product_version"], "1.0.7")
        self.assertIsNone(facts["install"]["digest"])

    def test_short_first_read_of_oversize_receipt_nulls_digest(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        posix = write_receipt(mapped)
        canonical = mapped.local(posix).read_bytes()
        mapped.local(posix).write_bytes(canonical + b"X" * MODULE.MAX_IDENTITY_BYTES)
        mapped.short_first[posix] = len(canonical)
        bind_linux(self)
        bind_runtime(self)
        bind_fs(self, mapped)
        facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertEqual(facts["install"]["product_version"], "1.0.7")
        self.assertIsNone(facts["install"]["digest"])
        self.assertNotIn(CANARY, json.dumps(facts))

    def test_fifo_receipt_opens_nonblock_and_nulls_digest(self) -> None:
        root, mapped = make_tree(existing_install=True, existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        posix = f"{POSIX_INSTALL}/{MODULE.RECEIPT_NAME}"
        mapped.fifos.add(posix)
        bind_linux(self)
        bind_runtime(self)
        bind_fs(self, mapped)
        facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertEqual(facts["install"]["product_version"], "1.0.7")
        self.assertIsNone(facts["install"]["digest"])
        expected = COLLECTOR._open_flags("O_NOFOLLOW", "O_NONBLOCK")
        self.assertIn(expected, mapped.open_flags.get(posix, []))


class RemoteProvisionSchemaAdmissionTest(unittest.TestCase):
    """Collector admits exact v3/v5/v6 metadata+workspace; v5/v6 oracles stay in tests."""

    def plant(self, meta_bytes: bytes, workspace_bytes: bytes) -> tuple[Path, MappedFS]:
        root, mapped = make_tree(existing_data=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        mapped.local(f"{POSIX_DATA}/store-meta.json").write_bytes(meta_bytes)
        mapped.local(f"{POSIX_DATA}/workspace.json").write_bytes(workspace_bytes)
        bind_linux(self)
        bind_fs(self, mapped)
        return root, mapped

    def facts_unwritten(self, root: Path) -> dict[str, object]:
        before = tree_hashes(root)
        facts = MODULE.collect_provision_facts(POSIX_INSTALL, POSIX_DATA, OWNER)
        self.assertEqual(tree_hashes(root), before)
        return facts

    def mutated_meta(self, values: dict[str, object], mutator) -> bytes:
        metadata = json.loads(json.dumps(values["store-meta.json"]))
        mutator(metadata)
        return _authority_bytes(metadata)

    def test_genuine_v6_initialize_bytes_return_oracle_uid(self) -> None:
        temporary, oracle, values, meta, workspace = disposable_v6_authority()
        self.addCleanup(temporary.cleanup)
        parsed = json.loads(meta.decode("utf-8"))
        self.assertEqual(parsed["store_schema_version"], 6)
        self.assertEqual(
            set(parsed["migrations"]),
            {"identity", "planning_status", "reports", "knowledge"},
        )
        knowledge = parsed["migrations"]["knowledge"]
        self.assertEqual(knowledge["id"], "workstack.knowledge.v6")
        self.assertEqual(knowledge["origin"], "fresh")
        self.assertIsNone(knowledge["source_sha256"])
        self.assertEqual(oracle.schema_version, 6)
        self.assertNotEqual(oracle.schema_version, 5)
        self.assertEqual(set(values), set(V6_DOCUMENT_NAMES))
        root, _mapped = self.plant(meta, workspace)
        facts = self.facts_unwritten(root)
        self.assertEqual(facts["data"]["workspace_id"], oracle.workspace_uid)

    def test_genuine_v5_initialize_bytes_return_oracle_uid(self) -> None:
        temporary, oracle, _values, meta, workspace = disposable_v5_authority()
        self.addCleanup(temporary.cleanup)
        parsed = json.loads(meta.decode("utf-8"))
        self.assertEqual(parsed["store_schema_version"], 5)
        self.assertNotIn("knowledge", parsed["migrations"])
        root, _mapped = self.plant(meta, workspace)
        facts = self.facts_unwritten(root)
        self.assertEqual(facts["data"]["workspace_id"], oracle.workspace_uid)
        self.assertEqual(oracle.schema_version, 5)

    def test_genuine_v3_fixture_still_returns_uid(self) -> None:
        temporary, oracle, _values, meta, workspace = genuine_v3_authority()
        self.addCleanup(temporary.cleanup)
        root, _mapped = self.plant(meta, workspace)
        facts = self.facts_unwritten(root)
        self.assertEqual(facts["data"]["workspace_id"], oracle.workspace_uid)
        self.assertEqual(oracle.schema_version, 3)

    def test_migrated_reports_evidence_is_admitted(self) -> None:
        temporary, oracle, values, _meta, workspace = disposable_v5_authority()
        self.addCleanup(temporary.cleanup)
        for origin in ("migrated_v1", "migrated_v2", "migrated_v3"):
            with self.subTest(origin=origin):
                def mutate(metadata, chosen=origin):
                    metadata["migrations"]["reports"] = {
                        "id": "workstack.reports.v3-to-v5",
                        "origin": chosen,
                        "source_sha256": ARTIFACT_DIGEST,
                    }

                meta = self.mutated_meta(values, mutate)
                Store.validate_document_values(
                    {**values, "store-meta.json": json.loads(meta.decode("utf-8"))},
                    schema_version=5,
                )
                root, _mapped = self.plant(meta, workspace)
                facts = self.facts_unwritten(root)
                self.assertEqual(facts["data"]["workspace_id"], oracle.workspace_uid)

    def test_migrated_knowledge_evidence_is_admitted(self) -> None:
        temporary, oracle, values, _meta, workspace = disposable_v6_authority()
        self.addCleanup(temporary.cleanup)
        for origin in ("migrated_v1", "migrated_v2", "migrated_v3", "migrated_v5"):
            with self.subTest(origin=origin):
                def mutate(metadata, chosen=origin):
                    metadata["migrations"]["knowledge"] = {
                        "id": "workstack.knowledge.v5-to-v6",
                        "origin": chosen,
                        "source_sha256": ARTIFACT_DIGEST,
                    }

                meta = self.mutated_meta(values, mutate)
                Store.validate_document_values(
                    {**values, "store-meta.json": json.loads(meta.decode("utf-8"))},
                    schema_version=6,
                )
                root, _mapped = self.plant(meta, workspace)
                facts = self.facts_unwritten(root)
                self.assertEqual(facts["data"]["workspace_id"], oracle.workspace_uid)

    def test_bool_float_and_unsupported_schema_versions_null_without_writes(self) -> None:
        temporary, _oracle, values, _meta, workspace = disposable_v5_authority()
        self.addCleanup(temporary.cleanup)
        cases = (
            ("version_bool", lambda metadata: metadata.__setitem__("version", True)),
            ("version_float", lambda metadata: metadata.__setitem__("version", 2.0)),
            ("schema_bool", lambda metadata: metadata.__setitem__("store_schema_version", True)),
            ("schema_float", lambda metadata: metadata.__setitem__("store_schema_version", 5.0)),
            ("schema_1", lambda metadata: metadata.__setitem__("store_schema_version", 1)),
            ("schema_2", lambda metadata: metadata.__setitem__("store_schema_version", 2)),
            ("schema_4", lambda metadata: metadata.__setitem__("store_schema_version", 4)),
            ("schema_6_without_knowledge", lambda metadata: metadata.__setitem__("store_schema_version", 6)),
            ("schema_future", lambda metadata: metadata.__setitem__("store_schema_version", 7)),
        )
        for name, mutator in cases:
            with self.subTest(name=name):
                root, _mapped = self.plant(self.mutated_meta(values, mutator), workspace)
                facts = self.facts_unwritten(root)
                self.assertIsNone(facts["data"]["workspace_id"])

    def test_partial_mixed_and_extra_migration_records_null_without_writes(self) -> None:
        temporary, _oracle, values, _meta, workspace = disposable_v5_authority()
        self.addCleanup(temporary.cleanup)

        def drop_reports(metadata):
            del metadata["migrations"]["reports"]

        def extra_record(metadata):
            metadata["migrations"]["ssot"] = metadata["migrations"]["identity"]

        def mixed_v3_reports(metadata):
            metadata["store_schema_version"] = 3

        def extra_top(metadata):
            metadata["extra"] = 1

        def v5_with_knowledge(metadata):
            metadata["migrations"]["knowledge"] = {
                "id": "workstack.knowledge.v6",
                "origin": "fresh",
                "source_sha256": None,
            }

        for name, mutator in (
            ("partial_v5", drop_reports),
            ("extra_record", extra_record),
            ("v3_with_reports", mixed_v3_reports),
            ("extra_top_field", extra_top),
            ("v5_with_knowledge", v5_with_knowledge),
        ):
            with self.subTest(name=name):
                root, _mapped = self.plant(self.mutated_meta(values, mutator), workspace)
                facts = self.facts_unwritten(root)
                self.assertIsNone(facts["data"]["workspace_id"])

    def test_partial_v6_roster_and_v7_null_without_writes(self) -> None:
        temporary, _oracle, values, _meta, workspace = disposable_v6_authority()
        self.addCleanup(temporary.cleanup)

        def drop_knowledge(metadata):
            del metadata["migrations"]["knowledge"]

        def drop_reports(metadata):
            del metadata["migrations"]["reports"]

        def future(metadata):
            metadata["store_schema_version"] = 7

        def relabel_v5(metadata):
            metadata["store_schema_version"] = 5

        for name, mutator in (
            ("partial_v6_no_knowledge", drop_knowledge),
            ("partial_v6_no_reports", drop_reports),
            ("v6_relabeled_v5", relabel_v5),
            ("schema_7", future),
        ):
            with self.subTest(name=name):
                root, _mapped = self.plant(self.mutated_meta(values, mutator), workspace)
                facts = self.facts_unwritten(root)
                self.assertIsNone(facts["data"]["workspace_id"])

    def test_invalid_reports_evidence_null_without_writes(self) -> None:
        temporary, _oracle, values, _meta, workspace = disposable_v5_authority()
        self.addCleanup(temporary.cleanup)

        def set_reports(metadata, **fields):
            metadata["migrations"]["reports"].update(fields)

        cases = (
            ("fresh_digest", {"source_sha256": ARTIFACT_DIGEST}),
            ("wrong_fresh_id", {"id": "workstack.reports.v3-to-v5"}),
            ("unknown_origin", {"origin": "migrated_v4", "id": "workstack.reports.v3-to-v5", "source_sha256": ARTIFACT_DIGEST}),
            ("migrated_null_digest", {"origin": "migrated_v3", "id": "workstack.reports.v3-to-v5", "source_sha256": None}),
            ("migrated_wrong_id", {"origin": "migrated_v3", "id": "workstack.reports.v5", "source_sha256": ARTIFACT_DIGEST}),
            ("uppercase_digest", {"origin": "migrated_v3", "id": "workstack.reports.v3-to-v5", "source_sha256": ARTIFACT_DIGEST.replace("abcdef", "ABCDEF")}),
        )
        for name, fields in cases:
            with self.subTest(name=name):
                root, _mapped = self.plant(
                    self.mutated_meta(values, lambda metadata, payload=fields: set_reports(metadata, **payload)),
                    workspace,
                )
                facts = self.facts_unwritten(root)
                self.assertIsNone(facts["data"]["workspace_id"])

    def test_invalid_knowledge_evidence_null_without_writes(self) -> None:
        temporary, _oracle, values, _meta, workspace = disposable_v6_authority()
        self.addCleanup(temporary.cleanup)

        def set_knowledge(metadata, **fields):
            metadata["migrations"]["knowledge"].update(fields)

        cases = (
            ("fresh_digest", {"source_sha256": ARTIFACT_DIGEST}),
            ("wrong_fresh_id", {"id": "workstack.knowledge.v5-to-v6"}),
            ("unknown_origin", {"origin": "migrated_v4", "id": "workstack.knowledge.v5-to-v6", "source_sha256": ARTIFACT_DIGEST}),
            ("migrated_null_digest", {"origin": "migrated_v5", "id": "workstack.knowledge.v5-to-v6", "source_sha256": None}),
            ("migrated_wrong_id", {"origin": "migrated_v5", "id": "workstack.knowledge.v6", "source_sha256": ARTIFACT_DIGEST}),
            ("uppercase_digest", {"origin": "migrated_v5", "id": "workstack.knowledge.v5-to-v6", "source_sha256": ARTIFACT_DIGEST.replace("abcdef", "ABCDEF")}),
        )
        for name, fields in cases:
            with self.subTest(name=name):
                root, _mapped = self.plant(
                    self.mutated_meta(values, lambda metadata, payload=fields: set_knowledge(metadata, **payload)),
                    workspace,
                )
                facts = self.facts_unwritten(root)
                self.assertIsNone(facts["data"]["workspace_id"])

    def test_noncanonical_and_nil_uid_null_without_writes(self) -> None:
        temporary, _oracle, _values, meta, _workspace = disposable_v5_authority()
        self.addCleanup(temporary.cleanup)
        for name, uid in (
            ("uppercase", LETTER_UID.upper()),
            ("nil", NIL_UID),
            ("urn", "urn:uuid:" + LETTER_UID),
        ):
            with self.subTest(name=name):
                workspace = _authority_bytes({"id": uid, "name": "Fixture", "version": 2})
                root, _mapped = self.plant(meta, workspace)
                facts = self.facts_unwritten(root)
                self.assertIsNone(facts["data"]["workspace_id"])

    def test_uid_swap_oracle_rejects_mixed_authority_collector_sees_workspace_only(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ws-remote-swap-")
        self.addCleanup(temporary.cleanup)
        service = WorkStack(Store(Path(temporary.name)))
        service.add_task("Held task")
        values = {
            name: json.loads((Path(temporary.name) / name).read_text(encoding="utf-8"))
            for name in V6_DOCUMENT_NAMES
        }
        original = Store.validate_document_values(values, schema_version=6)
        task_uid = values["backlog.json"]["tasks"][0]["uid"]
        mixed = json.loads(json.dumps(values))
        mixed["workspace.json"]["id"] = task_uid
        with self.assertRaises(StoreCorruptError):
            Store.validate_document_values(mixed, schema_version=6)
        meta = (Path(temporary.name) / "store-meta.json").read_bytes()
        workspace = _authority_bytes(mixed["workspace.json"])
        root, _mapped = self.plant(meta, workspace)
        facts = self.facts_unwritten(root)
        self.assertEqual(facts["data"]["workspace_id"], task_uid)
        self.assertNotEqual(facts["data"]["workspace_id"], original.workspace_uid)


if __name__ == "__main__":
    unittest.main()
