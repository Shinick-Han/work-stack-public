"""Canonical tests for the standalone remote provision installer engine."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SHELL = ROOT / "desktop" / "python-webview-shell"
LINUX_PATH = SHELL / "remote_provision_installer_linux.py"
INSTALLER_PATH = SHELL / "remote_provision_installer.py"

import workstack
from workstack.service import WorkStack
from workstack.store import Store, StoreCorruptError
from workstack.store_rosters import V3_DOCUMENT_NAMES, V5_DOCUMENT_NAMES, V6_DOCUMENT_NAMES


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LINUX = _load(LINUX_PATH, "remote_provision_installer_linux")
MODULE = _load(INSTALLER_PATH, "remote_provision_installer")


# Happy-path fixtures state the release identity the shipped engine declares
# rather than a frozen literal, so a release bump moves them with the engine
# instead of leaving them describing a superseded release. ReleaseIdentityTests
# pins that declared identity to workstack/__init__.py and proves a neighbouring
# version, protocol, or archive name is still refused.
PRODUCT = MODULE.PRODUCT
PROTOCOL = MODULE.PROTOCOL
ARCHIVE_NAME = MODULE.ARCHIVE_NAME

COMMIT = "a" * 40
TREE = "b" * 40
LOCK = "sha256:" + "c" * 64
UID = "11111111-1111-4111-8111-111111111111"
INSTALL = "/workstack-fixture/owner/app"
DATA = "/workstack-fixture/owner/data"
OWNER = "probe_owner"
PAYLOAD_FILES = {
    "desktop/python-webview-shell/remote_command_contract.py": b"CONTRACT = 1\n",
    "desktop/python-webview-shell/remote_entry.py": b"print('entry')\n",
    "run_work_stack.py": b"print('run')\n",
    "workstack/__init__.py": ('__version__ = "%s"\nREMOTE_PROTOCOL_VERSION = %d\n' % (PRODUCT, PROTOCOL)).encode(
        "ascii"
    ),
}
STORE_META = {
    "migrations": {
        "identity": {"id": "workstack.store.v2", "origin": "fresh", "source_sha256": None},
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
EVENTS = (
    "admit_runtime",
    "open_roots",
    "admit_data",
    "create_stage",
    "write_file",
    "smoke_imports",
    "smoke_entrypoint",
    "write_receipt",
    "commit_noreplace",
    "fsync_parent",
    "cleanup_stage",
)
PRE_COMMIT = EVENTS[:8]


def dump(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def file_records(blobs: dict[str, bytes]) -> list[dict[str, object]]:
    records = []
    for path in sorted(blobs):
        payload = blobs[path]
        records.append({"mode": 420, "path": path, "sha256": digest_of(payload), "size": len(payload)})
    return records


def target_object() -> dict[str, object]:
    return {
        "glibc_min": [2, 17],
        "implementation": "cpython",
        "libc": "glibc",
        "machine": "x86_64",
        "os": "linux",
        "python_major": 3,
        "python_minor": 12,
        "python_tag": "cp312",
        "soabi": "cpython-312-x86_64-linux-gnu",
        "wheel_platform": "manylinux_2_17_x86_64",
    }


def add_zip_member(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, payload)


def frozen_wheels() -> list[dict[str, object]]:
    records = []
    for dist, version in MODULE.FROZEN_WHEELS:
        tag = "py3-none-any"
        filename = "%s-%s-%s.whl" % (dist.replace("-", "_"), version, tag)
        records.append(
            {
                "distribution": dist,
                "filename": filename,
                "sha256": digest_of(filename.encode("utf-8")),
                "tags": [tag],
                "version": version,
            }
        )
    return records


def wheels_with_first_tag(tag: str) -> list[dict[str, object]]:
    wheels = frozen_wheels()
    dist = str(wheels[0]["distribution"])
    version = str(wheels[0]["version"])
    wheels[0]["tags"] = [tag]
    wheels[0]["filename"] = "%s-%s-%s.whl" % (dist.replace("-", "_"), version, tag)
    return wheels


def wheels_with_filename(filename: str, tags: list[str]) -> list[dict[str, object]]:
    """State the first wheel's shipped filename and declared tags independently."""

    wheels = frozen_wheels()
    wheels[0]["filename"] = filename
    wheels[0]["tags"] = tags
    return wheels


# The two official binary wheels really ship compressed platform tag sets, so
# the shipped filename and the atomic tags it expands to are both pinned here.
COMPRESSED_RPDS = "rpds_py-2026.6.3-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
COMPRESSED_RPDS_TAGS = ["cp312-cp312-manylinux2014_x86_64", "cp312-cp312-manylinux_2_17_x86_64"]


def wheels_with_compressed_rpds(
    *, filename: str = COMPRESSED_RPDS, tags: list[str] | None = None
) -> list[dict[str, object]]:
    """Restate the rpds-py record, which is the fifth frozen wheel."""

    wheels = frozen_wheels()
    for record in wheels:
        if record["distribution"] == "rpds-py":
            record["filename"] = filename
            record["tags"] = list(COMPRESSED_RPDS_TAGS if tags is None else tags)
    return wheels


def make_artifact(
    *,
    blobs: dict[str, bytes] | None = None,
    wheels: list[object] | None = None,
    manifest_product: str = PRODUCT,
    manifest_protocol: object = PROTOCOL,
    sidecar_product: str = PRODUCT,
    sidecar_protocol: object = PROTOCOL,
    archive_name: str = ARCHIVE_NAME,
) -> tuple[bytes, bytes]:
    """Build a self-consistent artifact, optionally restating one identity field.

    The manifest and sidecar identity are stated separately so a negative case
    can differ from the accepted artifact in exactly one release-identity field
    while every hash, size, and canonical form stays correct. A refusal then
    isolates the identity gate rather than some incidental corruption.
    """

    files = dict(PAYLOAD_FILES if blobs is None else blobs)
    manifest = {
        "entrypoint": "desktop/python-webview-shell/remote_entry.py",
        "files": file_records(files),
        "product_version": manifest_product,
        "remote_protocol_version": manifest_protocol,
        "requirements_lock_sha256": LOCK,
        "schema_version": 1,
        "source_commit": COMMIT,
        "source_tree": TREE,
        "target": target_object(),
        "wheels": frozen_wheels() if wheels is None else wheels,
    }
    manifest_bytes = dump(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        add_zip_member(archive, "artifact.json", manifest_bytes)
        for path, payload in files.items():
            add_zip_member(archive, "payload/" + path, payload)
    archive_bytes = buffer.getvalue()
    sidecar = dump(
        {
            "archive": {"name": archive_name, "sha256": digest_of(archive_bytes), "size": len(archive_bytes)},
            "artifact_manifest_sha256": digest_of(manifest_bytes),
            "product_version": sidecar_product,
            "remote_protocol_version": sidecar_protocol,
            "schema_version": 1,
            "source_commit": COMMIT,
            "target_id": "cp312-manylinux_2_17_x86_64",
        }
    )
    return archive_bytes, sidecar


def valid_kwargs(**overrides: object) -> dict[str, object]:
    archive, sidecar = make_artifact()
    values = {
        "archive_bytes": archive,
        "sidecar_bytes": sidecar,
        "install_root": INSTALL,
        "data_root": DATA,
        "owner": OWNER,
        "expected_workspace_uid": UID,
    }
    values.update(overrides)
    return values


class RecordingOps:
    def __init__(self, fail_at: str | None = None, fail_code: str = "REMOTE_INSTALL_FAILED") -> None:
        self.events: list[str] = []
        self.fail_at = fail_at
        self.fail_code = fail_code
        self.stage_ready = False
        self.files: list[str] = []
        self.receipt: object = None
        self.cleaned = False
        self.closed = 0
        self.commit_unknown = False
        self.rename_attempted = False

    def close_fds(self) -> None:
        self.closed += 1

    def _bump(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name:
            if name == "commit_noreplace" and self.fail_code == "REMOTE_INSTALL_COMMIT_UNKNOWN":
                self.rename_attempted = True
                self.commit_unknown = True
            if name == "fsync_parent":
                self.commit_unknown = True
            raise MODULE.InstallerError(self.fail_code)

    def admit_runtime(self) -> None:
        self._bump("admit_runtime")

    def open_roots(self) -> None:
        self._bump("open_roots")

    def admit_data(self) -> None:
        self._bump("admit_data")

    def create_stage(self) -> None:
        self._bump("create_stage")
        self.stage_ready = True

    def write_file(self, relative_path: str, payload: bytes, digest: str) -> None:
        self._bump("write_file")
        self.files.append(relative_path)

    def smoke_imports(self) -> None:
        self._bump("smoke_imports")

    def smoke_entrypoint(self) -> None:
        self._bump("smoke_entrypoint")

    def write_receipt(self, document: object) -> None:
        self._bump("write_receipt")
        self.receipt = document

    def commit_noreplace(self) -> None:
        self.rename_attempted = True
        self._bump("commit_noreplace")
        self.stage_ready = False

    def fsync_parent(self) -> None:
        self._bump("fsync_parent")

    def cleanup_stage(self) -> None:
        self.events.append("cleanup_stage")
        self.cleaned = True
        self.stage_ready = False


class ImportAndSurfaceTests(unittest.TestCase):
    def test_module_body_is_effect_free(self) -> None:
        for path in (INSTALLER_PATH, LINUX_PATH):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                self.assertIsInstance(
                    node,
                    (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.ClassDef, ast.FunctionDef, ast.Expr),
                )
                if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Expr)):
                    continue
                dumped = ast.dump(node)
                self.assertNotIn("platform.machine", dumped)
                self.assertNotIn("os.environ", dumped)
                self.assertNotIn("sys.stdin", dumped)

    def test_import_isolation_across_split(self) -> None:
        self.assertIs(MODULE.InstallerError, LINUX.InstallerError)
        self.assertFalse(hasattr(LINUX, "install_remote_artifact"))
        linux_source = LINUX_PATH.read_text(encoding="utf-8")
        self.assertNotIn("remote_provision_installer import", linux_source)
        self.assertNotIn("import workstack", linux_source)
        self.assertNotIn("import workstack", INSTALLER_PATH.read_text(encoding="utf-8"))
        names = set(inspect.signature(MODULE.install_remote_artifact).parameters)
        self.assertEqual(
            names,
            {"archive_bytes", "sidecar_bytes", "install_root", "data_root", "owner", "expected_workspace_uid"},
        )

    def test_re_compile_at_import_is_not_platform_or_env(self) -> None:
        tree = ast.parse(INSTALLER_PATH.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Expr)):
                continue
            text = ast.dump(node)
            self.assertNotIn("platform.machine", text)
            self.assertNotIn("os.environ", text)
            self.assertNotIn("sys.stdin", text)


class PublicWindowsTests(unittest.TestCase):
    def test_public_call_is_unsupported_before_write(self) -> None:
        writes = {"n": 0}

        def counted(*_args: object, **_kwargs: object) -> None:
            writes["n"] += 1
            raise AssertionError("filesystem mutation")

        kwargs = valid_kwargs()
        with mock.patch.object(MODULE.sys, "platform", "win32"):
            with mock.patch("os.open", counted), mock.patch("os.mkdir", counted):
                with self.assertRaises(MODULE.InstallerError) as raised:
                    MODULE.install_remote_artifact(**kwargs)
        self.assertEqual(raised.exception.code, "REMOTE_INSTALLER_UNSUPPORTED")
        self.assertEqual(writes["n"], 0)
        self.assertNotIn("operations", inspect.signature(MODULE.install_remote_artifact).parameters)

    def test_wrong_byte_types_are_content_free(self) -> None:
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE.install_remote_artifact(
                archive_bytes="nope",  # type: ignore[arg-type]
                sidecar_bytes=b"{}",
                install_root=INSTALL,
                data_root=DATA,
                owner=OWNER,
                expected_workspace_uid=UID,
            )
        self.assertEqual(raised.exception.code, "INVALID_INSTALLER")
        self.assertEqual(str(raised.exception), "INVALID_INSTALLER")


class ArgvEnvelopeTests(unittest.TestCase):
    def test_installer_main_refuses_malformed_argv(self) -> None:
        archive, sidecar = make_artifact()
        stderr = io.BytesIO()
        stdout = io.BytesIO()
        fake_err = mock.Mock()
        fake_err.buffer = stderr
        fake_out = mock.Mock()
        fake_out.buffer = stdout
        with mock.patch.object(MODULE.sys, "stderr", fake_err), mock.patch.object(MODULE.sys, "stdout", fake_out):
            code = MODULE.installer_main(["provision-install"], archive, sidecar)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), b"")
        line = json.loads(stderr.getvalue().decode("utf-8"))
        self.assertEqual(line, {"schema_version": 1, "outcome": "refused", "code": "INVALID_INSTALLER"})
        self.assertLessEqual(len(stderr.getvalue()), 512)

    def test_installer_main_windows_unsupported_envelope(self) -> None:
        archive, sidecar = make_artifact()
        argv = [
            "provision-install",
            "--install-root",
            INSTALL,
            "--data-root",
            DATA,
            "--owner",
            OWNER,
            "--expected-workspace-uid",
            UID,
        ]
        stderr = io.BytesIO()
        stdout = io.BytesIO()
        fake_err = mock.Mock()
        fake_err.buffer = stderr
        fake_out = mock.Mock()
        fake_out.buffer = stdout
        with mock.patch.object(MODULE.sys, "platform", "win32"):
            with mock.patch.object(MODULE.sys, "stderr", fake_err), mock.patch.object(MODULE.sys, "stdout", fake_out):
                code = MODULE.installer_main(argv, archive, sidecar)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), b"")
        self.assertEqual(
            json.loads(stderr.getvalue().decode("utf-8")),
            {"schema_version": 1, "outcome": "refused", "code": "REMOTE_INSTALLER_UNSUPPORTED"},
        )
        with self.assertRaises(MODULE.InstallerError):
            MODULE._parse_argv(
                [
                    "provision-install",
                    "--install-root",
                    INSTALL,
                    "--data-root",
                    INSTALL + "/nested",
                    "--owner",
                    OWNER,
                    "--expected-workspace-uid",
                    UID,
                ]
            )
        with self.assertRaises(MODULE.InstallerError):
            MODULE._parse_argv(
                [
                    "provision-install",
                    "--install-root",
                    INSTALL,
                    "--data-root",
                    DATA,
                    "--owner",
                    OWNER,
                    "--expected-workspace-uid",
                    "00000000-0000-0000-0000-000000000000",
                ]
            )
        parsed = MODULE._parse_argv(
            [
                "provision-install",
                "--install-root",
                INSTALL,
                "--data-root",
                DATA,
                "--owner",
                OWNER,
                "--expected-workspace-uid",
                UID,
            ]
        )
        self.assertEqual(parsed, (INSTALL, DATA, OWNER, UID))
        with self.assertRaises(MODULE.InstallerError):
            MODULE._parse_argv(
                [
                    "provision-install",
                    "--install-root",
                    INSTALL,
                    "--data-root",
                    DATA,
                    "--owner",
                    OWNER,
                    "--expected-workspace-uid",
                    UID,
                    "--extra",
                ]
            )


class ArtifactAdmissionTests(unittest.TestCase):
    def test_positive_sidecar_and_manifest(self) -> None:
        archive, sidecar = make_artifact()
        admitted = MODULE._admit_artifact(archive, sidecar)
        self.assertEqual(admitted["digest"], digest_of(archive))
        self.assertEqual(set(admitted["blobs"]), set(PAYLOAD_FILES))

    def test_digest_mismatches_are_refused(self) -> None:
        archive, sidecar = make_artifact()
        document = json.loads(sidecar.decode("utf-8"))
        document["archive"]["sha256"] = "sha256:" + "d" * 64
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive, dump(document))
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")
        with self.assertRaises(MODULE.InstallerError):
            MODULE._admit_artifact(archive, b"\xef\xbb\xbf" + sidecar)

    def test_backslash_and_unlisted_member(self) -> None:
        info = mock.Mock()
        info.filename = "payload\\evil"
        info.flag_bits = 0
        info.compress_type = zipfile.ZIP_DEFLATED
        info.file_size = 1
        info.extra = b""
        info.external_attr = 0o100644 << 16
        self.assertTrue(MODULE._zip_rejected(info))
        archive, sidecar = make_artifact()
        extra = io.BytesIO()
        with zipfile.ZipFile(extra, "w") as handle:
            add_zip_member(handle, "artifact.json", b"{}")
            add_zip_member(handle, "payload/unlisted.txt", b"x")
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(extra.getvalue(), sidecar)
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")

    def test_symlink_mode_and_duplicate_name(self) -> None:
        info = zipfile.ZipInfo("payload/link")
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o120644 << 16
        self.assertTrue(MODULE._zip_rejected(info))
        files = dict(PAYLOAD_FILES)
        manifest = dump(
            {
                "entrypoint": "desktop/python-webview-shell/remote_entry.py",
                "files": file_records(files),
                "product_version": PRODUCT,
                "remote_protocol_version": 1,
                "requirements_lock_sha256": LOCK,
                "schema_version": 1,
                "source_commit": COMMIT,
                "source_tree": TREE,
                "target": target_object(),
                "wheels": frozen_wheels(),
            }
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            add_zip_member(archive, "artifact.json", manifest)
            for path, payload in files.items():
                add_zip_member(archive, "payload/" + path, payload)
            add_zip_member(archive, "payload/run_work_stack.py", b"dup\n")
        sidecar = dump(
            {
                "archive": {
                    "name": ARCHIVE_NAME,
                    "sha256": digest_of(buffer.getvalue()),
                    "size": len(buffer.getvalue()),
                },
                "artifact_manifest_sha256": digest_of(manifest),
                "product_version": PRODUCT,
                "remote_protocol_version": 1,
                "schema_version": 1,
                "source_commit": COMMIT,
                "target_id": "cp312-manylinux_2_17_x86_64",
            }
        )
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(buffer.getvalue(), sidecar)
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")

    def test_zip_bomb_header_and_crc(self) -> None:
        info = zipfile.ZipInfo("payload/big")
        info.compress_type = zipfile.ZIP_STORED
        info.file_size = MODULE.MAX_FILE + 1
        info.external_attr = 0o100644 << 16
        self.assertTrue(MODULE._zip_rejected(info))
        archive, sidecar = make_artifact()
        corrupted = bytearray(archive)
        corrupted[30] ^= 0xFF
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(bytes(corrupted), sidecar)
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")

    def test_missing_required_payload_file(self) -> None:
        blobs = dict(PAYLOAD_FILES)
        del blobs["run_work_stack.py"]
        archive, sidecar = make_artifact(blobs=blobs)
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive, sidecar)
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")

    def test_exact_scalar_types_are_required(self) -> None:
        archive, sidecar = make_artifact()
        document = json.loads(sidecar.decode("utf-8"))
        document["schema_version"] = True
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive, dump(document))
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")
        document = json.loads(sidecar.decode("utf-8"))
        document["remote_protocol_version"] = True
        with self.assertRaises(MODULE.InstallerError):
            MODULE._admit_artifact(archive, dump(document))
        files = dict(PAYLOAD_FILES)
        target = target_object()
        target["python_minor"] = 12.0
        manifest = dump(
            {
                "entrypoint": "desktop/python-webview-shell/remote_entry.py",
                "files": file_records(files),
                "product_version": PRODUCT,
                "remote_protocol_version": 1,
                "requirements_lock_sha256": LOCK,
                "schema_version": 1,
                "source_commit": COMMIT,
                "source_tree": TREE,
                "target": target,
                "wheels": frozen_wheels(),
            }
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as handle:
            add_zip_member(handle, "artifact.json", manifest)
            for path, payload in files.items():
                add_zip_member(handle, "payload/" + path, payload)
        archive_bytes = buffer.getvalue()
        sidecar = dump(
            {
                "archive": {
                    "name": ARCHIVE_NAME,
                    "sha256": digest_of(archive_bytes),
                    "size": len(archive_bytes),
                },
                "artifact_manifest_sha256": digest_of(manifest),
                "product_version": PRODUCT,
                "remote_protocol_version": 1,
                "schema_version": 1,
                "source_commit": COMMIT,
                "target_id": "cp312-manylinux_2_17_x86_64",
            }
        )
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive_bytes, sidecar)
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")
        target = target_object()
        target["glibc_min"] = [True, 17]
        manifest = dump(
            {
                "entrypoint": "desktop/python-webview-shell/remote_entry.py",
                "files": file_records(files),
                "product_version": PRODUCT,
                "remote_protocol_version": 1,
                "requirements_lock_sha256": LOCK,
                "schema_version": 1,
                "source_commit": COMMIT,
                "source_tree": TREE,
                "target": target,
                "wheels": frozen_wheels(),
            }
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as handle:
            add_zip_member(handle, "artifact.json", manifest)
            for path, payload in files.items():
                add_zip_member(handle, "payload/" + path, payload)
        archive_bytes = buffer.getvalue()
        sidecar = dump(
            {
                "archive": {
                    "name": ARCHIVE_NAME,
                    "sha256": digest_of(archive_bytes),
                    "size": len(archive_bytes),
                },
                "artifact_manifest_sha256": digest_of(manifest),
                "product_version": PRODUCT,
                "remote_protocol_version": 1,
                "schema_version": 1,
                "source_commit": COMMIT,
                "target_id": "cp312-manylinux_2_17_x86_64",
            }
        )
        with self.assertRaises(MODULE.InstallerError):
            MODULE._admit_artifact(archive_bytes, sidecar)

    def test_empty_wheels_and_filename_mismatch_are_invalid(self) -> None:
        archive, sidecar = make_artifact(wheels=[])
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive, sidecar)
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")
        wheels = frozen_wheels()
        wheels[0]["filename"] = "attrs-26.1.0-py2-none-any.whl"
        archive, sidecar = make_artifact(wheels=wheels)
        with self.assertRaises(MODULE.InstallerError):
            MODULE._admit_artifact(archive, sidecar)

    def test_wheel_tags_must_match_cp312_gnu_x86_64(self) -> None:
        positives = (
            "py3-none-any",
            "py312-none-any",
            "cp312-none-any",
            "cp312-cp312-manylinux_2_17_x86_64",
            "cp312-abi3-manylinux2014_x86_64",
            "cp312-cp312-manylinux1_x86_64",
            "cp32-abi3-manylinux_2_17_x86_64",
        )
        for tag in positives:
            with self.subTest(tag=tag):
                archive, sidecar = make_artifact(wheels=wheels_with_first_tag(tag))
                admitted = MODULE._admit_artifact(archive, sidecar)
                self.assertEqual(admitted["digest"], digest_of(archive))
        negatives = (
            "cp312-cp312-win_amd64",
            "cp312-cp312-musllinux_1_2_x86_64",
            "cp311-cp311-manylinux_2_17_x86_64",
            "cp312-cp312-manylinux_2_28_x86_64",
            "cp312-cp312-linux_x86_64",
            "py3-none-macosx_10_9_x86_64",
            "attacker-none-any",
            "cp300-abi3-manylinux_1_999_x86_64",
            "cp30-abi3-manylinux_2_17_x86_64",
            "cp312-cp312-manylinux_2_017_x86_64",
            "cp312-cp312-manylinux_0_999_x86_64",
        )
        for tag in negatives:
            with self.subTest(tag=tag):
                archive, sidecar = make_artifact(wheels=wheels_with_first_tag(tag))
                with self.assertRaises(MODULE.InstallerError) as raised:
                    MODULE._admit_artifact(archive, sidecar)
                self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")

    def test_compressed_platform_tag_wheel_filename_is_admitted(self) -> None:
        archive, sidecar = make_artifact(wheels=wheels_with_compressed_rpds())
        admitted = MODULE._admit_artifact(archive, sidecar)
        self.assertEqual(admitted["digest"], digest_of(archive))

    def test_compressed_filename_must_expand_to_exactly_the_declared_tags(self) -> None:
        cases = (
            ("dropped", [COMPRESSED_RPDS_TAGS[0]]),
            ("extra", sorted(COMPRESSED_RPDS_TAGS + ["cp312-cp312-manylinux1_x86_64"])),
            ("substituted", sorted([COMPRESSED_RPDS_TAGS[0], "cp312-abi3-manylinux2010_x86_64"])),
        )
        for label, tags in cases:
            with self.subTest(case=label):
                archive, sidecar = make_artifact(wheels=wheels_with_compressed_rpds(tags=tags))
                with self.assertRaises(MODULE.InstallerError) as raised:
                    MODULE._admit_artifact(archive, sidecar)
                self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")

    def test_malformed_wheel_filenames_are_refused(self) -> None:
        tag = "py3-none-any"
        cases = (
            "attrs-26.1.0-1-py3-none-any.whl",
            "attrs-26.1.0-py3-none-any.zip",
            "attrs-26.1.0-py3-none-any",
            "wheels/attrs-26.1.0-py3-none-any.whl",
            "wheels" + chr(92) + "attrs-26.1.0-py3-none-any.whl",
            "attrs-26.1.0-py3-none-any-extra.whl",
            "attrs-26.1.0-py3-none.whl",
            "attr-26.1.0-py3-none-any.whl",
            "attrs-26.1.1-py3-none-any.whl",
            "attrs-26.1.0-py3.py3-none-any.whl",
            "attrs-26.1.0-py3-none-.whl",
            ".whl",
        )
        for filename in cases:
            with self.subTest(filename=filename):
                archive, sidecar = make_artifact(wheels=wheels_with_filename(filename, [tag]))
                with self.assertRaises(MODULE.InstallerError) as raised:
                    MODULE._admit_artifact(archive, sidecar)
                self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")

    def test_compressed_filename_never_admits_an_incompatible_component(self) -> None:
        filename = "rpds_py-2026.6.3-cp312-cp312-musllinux_1_2_x86_64.manylinux2014_x86_64.whl"
        tags = sorted(["cp312-cp312-musllinux_1_2_x86_64", "cp312-cp312-manylinux2014_x86_64"])
        archive, sidecar = make_artifact(wheels=wheels_with_compressed_rpds(filename=filename, tags=tags))
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive, sidecar)
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")

    def test_float_mode_is_refused(self) -> None:
        files = dict(PAYLOAD_FILES)
        records = file_records(files)
        records[0]["mode"] = 420.0
        manifest = dump(
            {
                "entrypoint": "desktop/python-webview-shell/remote_entry.py",
                "files": records,
                "product_version": PRODUCT,
                "remote_protocol_version": 1,
                "requirements_lock_sha256": LOCK,
                "schema_version": 1,
                "source_commit": COMMIT,
                "source_tree": TREE,
                "target": target_object(),
                "wheels": frozen_wheels(),
            }
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as handle:
            add_zip_member(handle, "artifact.json", manifest)
            for path, payload in files.items():
                add_zip_member(handle, "payload/" + path, payload)
        archive_bytes = buffer.getvalue()
        sidecar = dump(
            {
                "archive": {
                    "name": ARCHIVE_NAME,
                    "sha256": digest_of(archive_bytes),
                    "size": len(archive_bytes),
                },
                "artifact_manifest_sha256": digest_of(manifest),
                "product_version": PRODUCT,
                "remote_protocol_version": 1,
                "schema_version": 1,
                "source_commit": COMMIT,
                "target_id": "cp312-manylinux_2_17_x86_64",
            }
        )
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive_bytes, sidecar)
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")

    def test_truncation_size_mismatch_and_absent_member(self) -> None:
        archive, sidecar = make_artifact()
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive[:-32], sidecar)
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")
        document = json.loads(sidecar.decode("utf-8"))
        document["archive"]["size"] = len(archive) - 1
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive, dump(document))
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")
        files = dict(PAYLOAD_FILES)
        manifest = dump(
            {
                "entrypoint": "desktop/python-webview-shell/remote_entry.py",
                "files": file_records(files),
                "product_version": PRODUCT,
                "remote_protocol_version": 1,
                "requirements_lock_sha256": LOCK,
                "schema_version": 1,
                "source_commit": COMMIT,
                "source_tree": TREE,
                "target": target_object(),
                "wheels": frozen_wheels(),
            }
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as handle:
            add_zip_member(handle, "artifact.json", manifest)
            for path, payload in files.items():
                if path == "run_work_stack.py":
                    continue
                add_zip_member(handle, "payload/" + path, payload)
        archive_bytes = buffer.getvalue()
        sidecar = dump(
            {
                "archive": {
                    "name": ARCHIVE_NAME,
                    "sha256": digest_of(archive_bytes),
                    "size": len(archive_bytes),
                },
                "artifact_manifest_sha256": digest_of(manifest),
                "product_version": PRODUCT,
                "remote_protocol_version": 1,
                "schema_version": 1,
                "source_commit": COMMIT,
                "target_id": "cp312-manylinux_2_17_x86_64",
            }
        )
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive_bytes, sidecar)
        self.assertEqual(raised.exception.code, "REMOTE_ARTIFACT_INVALID")


class ReleaseIdentityTests(unittest.TestCase):
    """The engine's release identity is single-sourced, current, and still exact.

    The install engine cannot import ``workstack`` on the remote host: it is
    executed as two standalone modules decoded from the stdin payload, before
    any product package exists there. Its release identity is therefore an
    explicit constant, and these tests are what keeps that constant honest --
    they fail when it drifts from workstack/__init__.py, when the admission
    module restates it instead of reading the leaf's, and when any identity gate
    is loosened from equality into acceptance.
    """

    # 1.0.8 is the release this engine was pinned to before the constants were
    # single-sourced and moved forward. It is kept here as the concrete
    # neighbouring release each gate must still refuse.
    SUPERSEDED = "1.0.8"

    def test_engine_identity_equals_the_source_release(self) -> None:
        self.assertEqual(workstack.__version__, LINUX.PRODUCT)
        self.assertEqual(workstack.REMOTE_PROTOCOL_VERSION, LINUX.PROTOCOL)
        self.assertNotEqual(self.SUPERSEDED, workstack.__version__)

    def test_admission_module_reads_the_leaf_identity(self) -> None:
        self.assertIs(MODULE.PRODUCT, LINUX.PRODUCT)
        self.assertIs(MODULE.PROTOCOL, LINUX.PROTOCOL)
        source = INSTALLER_PATH.read_text(encoding="utf-8")
        # One definition site: the admission module imports the pair, never
        # restates it, so a release bump cannot leave the two gates disagreeing.
        self.assertNotIn("PRODUCT = ", source)
        self.assertNotIn("PROTOCOL = ", source)
        self.assertIn("from remote_provision_installer_linux import PRODUCT", source)
        self.assertIn("from remote_provision_installer_linux import PROTOCOL", source)

    def test_expected_archive_name_is_derived_from_that_identity(self) -> None:
        self.assertEqual(
            "WorkStack-Linux-%s-%s.zip" % (workstack.__version__, MODULE.TARGET_ID),
            MODULE.ARCHIVE_NAME,
        )

    def test_current_release_artifact_is_admitted_and_installs(self) -> None:
        archive, sidecar = make_artifact()

        admitted = MODULE._admit_artifact(archive, sidecar)

        self.assertEqual(digest_of(archive), admitted["digest"])
        ops = RecordingOps()
        result = MODULE._install_with_operations(ops, **valid_kwargs())
        self.assertEqual("installed", result["outcome"])
        self.assertEqual(workstack.__version__, result["product_version"])
        self.assertEqual(workstack.REMOTE_PROTOCOL_VERSION, result["remote_protocol_version"])
        self.assertEqual(workstack.__version__, ops.receipt["product_version"])

    def test_sidecar_stating_the_superseded_release_is_refused(self) -> None:
        archive, sidecar = make_artifact(sidecar_product=self.SUPERSEDED, manifest_product=self.SUPERSEDED)

        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive, sidecar)

        self.assertEqual("REMOTE_ARTIFACT_INVALID", raised.exception.code)

    def test_manifest_stating_another_release_is_refused(self) -> None:
        # The sidecar is correct and its manifest digest is over these very
        # bytes, so only the manifest's own product_version differs.
        archive, sidecar = make_artifact(manifest_product=self.SUPERSEDED)

        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive, sidecar)

        self.assertEqual("REMOTE_ARTIFACT_INVALID", raised.exception.code)

    def test_protocol_mismatch_is_refused_on_both_documents(self) -> None:
        other = MODULE.PROTOCOL + 1
        for kwargs in ({"sidecar_protocol": other, "manifest_protocol": other}, {"manifest_protocol": other}):
            with self.subTest(**kwargs):
                archive, sidecar = make_artifact(**kwargs)

                with self.assertRaises(MODULE.InstallerError) as raised:
                    MODULE._admit_artifact(archive, sidecar)

                self.assertEqual("REMOTE_ARTIFACT_INVALID", raised.exception.code)

    def test_archive_named_for_another_release_is_refused(self) -> None:
        stale = "WorkStack-Linux-%s-%s.zip" % (self.SUPERSEDED, MODULE.TARGET_ID)
        self.assertNotEqual(MODULE.ARCHIVE_NAME, stale)
        archive, sidecar = make_artifact(archive_name=stale)

        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._admit_artifact(archive, sidecar)

        self.assertEqual("REMOTE_ARTIFACT_INVALID", raised.exception.code)

    def test_post_install_probe_must_report_the_same_release(self) -> None:
        ops = LINUX._LinuxInstallerOperations(INSTALL, DATA, OWNER, UID)

        def probe(product: object, protocol: object) -> bytes:
            document = {"workspace_id": UID, "product_version": product, "protocol_version": protocol}
            return json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"

        ops._accept_probe(probe(LINUX.PRODUCT, LINUX.PROTOCOL), b"")
        for product, protocol in (
            (self.SUPERSEDED, LINUX.PROTOCOL),
            (LINUX.PRODUCT, LINUX.PROTOCOL + 1),
        ):
            with self.subTest(product=product, protocol=protocol):
                with self.assertRaises(LINUX.InstallerError) as raised:
                    ops._accept_probe(probe(product, protocol), b"")

                self.assertEqual("REMOTE_SMOKE_FAILED", raised.exception.code)

    def test_staged_package_import_gate_uses_the_same_identity(self) -> None:
        # smoke_imports needs a real dirfd-anchored stage (Linux only), so the
        # equality itself is asserted at the source: the staged workstack must
        # match PRODUCT/PROTOCOL, not merely be present or importable.
        source = LINUX_PATH.read_text(encoding="utf-8")
        self.assertIn('getattr(workstack, "__version__", None) == PRODUCT', source)
        self.assertIn('getattr(workstack, "REMOTE_PROTOCOL_VERSION", None) == PROTOCOL', source)


class FakeInstallOrderTests(unittest.TestCase):
    def test_full_fake_event_order(self) -> None:
        ops = RecordingOps()
        result = MODULE._install_with_operations(ops, **valid_kwargs())
        self.assertEqual(result["outcome"], "installed")
        self.assertEqual(result["workspace_uid"], UID)
        self.assertEqual(result["product_version"], PRODUCT)
        names = [name for name in ops.events if name != "write_file"]
        self.assertEqual(
            names,
            [
                "admit_runtime",
                "open_roots",
                "admit_data",
                "create_stage",
                "smoke_imports",
                "smoke_entrypoint",
                "write_receipt",
                "commit_noreplace",
                "fsync_parent",
            ],
        )
        writes = [index for index, name in enumerate(ops.events) if name == "write_file"]
        stage_at = ops.events.index("create_stage")
        smoke_at = ops.events.index("smoke_imports")
        self.assertTrue(writes)
        self.assertTrue(all(stage_at < index < smoke_at for index in writes))
        self.assertEqual(ops.files, sorted(PAYLOAD_FILES))
        self.assertNotIn("cleanup_stage", ops.events)
        self.assertIsNotNone(ops.receipt)
        self.assertEqual(ops.closed, 1)
        self.assertFalse(ops.commit_unknown)

    def test_pre_commit_failures_clean_only_owned_stage(self) -> None:
        for event in PRE_COMMIT:
            with self.subTest(event=event):
                ops = RecordingOps(fail_at=event)
                with self.assertRaises(MODULE.InstallerError):
                    MODULE._install_with_operations(ops, **valid_kwargs())
                if event in {"admit_runtime", "open_roots", "admit_data", "create_stage"}:
                    if event == "create_stage":
                        self.assertFalse(ops.cleaned)
                    else:
                        self.assertFalse(ops.cleaned)
                    self.assertNotIn("commit_noreplace", ops.events)
                else:
                    self.assertTrue(ops.cleaned)
                    self.assertIn("cleanup_stage", ops.events)
                    self.assertNotIn("commit_noreplace", ops.events)
                self.assertEqual(ops.closed, 1)

    def test_fsync_failure_after_commit_is_unknown_and_keeps_target(self) -> None:
        ops = RecordingOps(fail_at="fsync_parent")
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._install_with_operations(ops, **valid_kwargs())
        self.assertEqual(raised.exception.code, "REMOTE_INSTALL_COMMIT_UNKNOWN")
        self.assertIn("commit_noreplace", ops.events)
        self.assertFalse(ops.cleaned)
        self.assertNotIn("cleanup_stage", ops.events)
        self.assertTrue(ops.commit_unknown)
        self.assertEqual(ops.closed, 1)

    def test_target_race_before_rename_does_not_retry(self) -> None:
        ops = RecordingOps(fail_at="commit_noreplace", fail_code="INSTALL_ROOT_EXISTS")
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._install_with_operations(ops, **valid_kwargs())
        self.assertEqual(raised.exception.code, "INSTALL_ROOT_EXISTS")
        self.assertTrue(ops.cleaned)
        self.assertEqual(ops.events.count("commit_noreplace"), 1)
        ops = RecordingOps(fail_at="open_roots", fail_code="TARGET_OWNERSHIP_MISMATCH")
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._install_with_operations(ops, **valid_kwargs())
        self.assertEqual(raised.exception.code, "TARGET_OWNERSHIP_MISMATCH")
        self.assertEqual(ops.events, ["admit_runtime", "open_roots"])
        self.assertFalse(ops.files)
        self.assertFalse(ops.cleaned)
        self.assertEqual(ops.closed, 1)

    def test_unknown_commit_does_not_cleanup(self) -> None:
        ops = RecordingOps(fail_at="commit_noreplace", fail_code="REMOTE_INSTALL_COMMIT_UNKNOWN")
        with self.assertRaises(MODULE.InstallerError) as raised:
            MODULE._install_with_operations(ops, **valid_kwargs())
        self.assertEqual(raised.exception.code, "REMOTE_INSTALL_COMMIT_UNKNOWN")
        self.assertTrue(ops.rename_attempted)
        self.assertTrue(ops.commit_unknown)
        self.assertFalse(ops.cleaned)
        self.assertNotIn("cleanup_stage", ops.events)
        self.assertEqual(ops.closed, 1)

    def test_runtime_and_data_refusal_happen_before_write(self) -> None:
        for event, code in (
            ("admit_runtime", "REMOTE_ARTIFACT_INCOMPATIBLE"),
            ("open_roots", "TARGET_OWNERSHIP_MISMATCH"),
            ("admit_data", "DATA_STATE_UNKNOWN"),
        ):
            with self.subTest(event=event):
                ops = RecordingOps(fail_at=event, fail_code=code)
                with self.assertRaises(MODULE.InstallerError) as raised:
                    MODULE._install_with_operations(ops, **valid_kwargs())
                self.assertEqual(raised.exception.code, code)
                self.assertFalse(ops.files)
                self.assertNotIn("create_stage", ops.events)
                self.assertNotIn("write_file", ops.events)
                self.assertFalse(ops.cleaned)
                self.assertEqual(ops.closed, 1)


class AncestorTrustTests(unittest.TestCase):
    def test_writable_nonsticky_ancestor_is_refused(self) -> None:
        mode = stat.S_IFDIR | 0o0777
        self.assertFalse(LINUX._ancestor_hop_allowed(0, mode, euid=1000, leaf=False, child_uid=1000))

    def test_sticky_tmp_with_euid_child_is_allowed(self) -> None:
        mode = stat.S_IFDIR | stat.S_ISVTX | 0o0777
        self.assertTrue(LINUX._ancestor_hop_allowed(0, mode, euid=1000, leaf=False, child_uid=1000))
        self.assertFalse(LINUX._ancestor_hop_allowed(0, mode, euid=1000, leaf=False, child_uid=2000))

    def test_final_parent_rejects_group_other_write(self) -> None:
        self.assertTrue(LINUX._ancestor_hop_allowed(1000, stat.S_IFDIR | 0o0755, euid=1000, leaf=True))
        self.assertFalse(LINUX._ancestor_hop_allowed(1000, stat.S_IFDIR | 0o0775, euid=1000, leaf=True))
        self.assertFalse(LINUX._ancestor_hop_allowed(0, stat.S_IFDIR | 0o0755, euid=1000, leaf=True))


class AstSeamTests(unittest.TestCase):
    def test_os_and_ctypes_stay_in_linux_leaf(self) -> None:
        installer_tree = ast.parse(INSTALLER_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(installer_tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                self.assertNotIn(node.value.id, {"os", "ctypes"})
        linux_tree = ast.parse(LINUX_PATH.read_text(encoding="utf-8"))
        uses_os = False
        for node in ast.walk(linux_tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                uses_os = True
        self.assertTrue(uses_os)

    def test_rebind_after_seal_and_metadata_before_use(self) -> None:
        source = LINUX_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        wanted = {
            "smoke_imports": ("_seal_directories", "import_module"),
            "smoke_entrypoint": ("_assert_data_files", "Popen"),
        }
        found: dict[str, ast.FunctionDef] = {}
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name != "_LinuxInstallerOperations":
                continue
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in wanted:
                    found[item.name] = item
        self.assertEqual(set(found), set(wanted))
        for name, (before, after) in wanted.items():
            events: list[tuple[int, int, str]] = []
            for node in ast.walk(found[name]):
                attrs = {before, after, "_bind_stage_and_data", "_held_path"}
                if isinstance(node, ast.Attribute) and node.attr in attrs:
                    events.append((node.lineno, node.col_offset, node.attr))
            events.sort()
            labels = [item[2] for item in events]
            self.assertIn(before, labels, name)
            self.assertIn(after, labels, name)
            last_bind = max(index for index, label in enumerate(labels) if label == "_bind_stage_and_data")
            last_held = max(index for index, label in enumerate(labels) if label == "_held_path")
            self.assertGreater(last_bind, labels.index(before), name)
            self.assertGreater(last_held, last_bind, name)
            self.assertLess(last_held, labels.index(after), name)
        entry_text = ast.get_source_segment(source, found["smoke_entrypoint"]) or ""
        self.assertIn("pass_fds", entry_text)
        self.assertIn("/proc/self/fd/", entry_text)

    def test_wsl_direct_invocation_is_opt_in(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertNotIn('"-d", ' + '"Ubuntu"', source)
        self.assertNotIn("wsl_" + "ubuntu_ready", source)
        self.assertIn("WORKSTACK_TEST_WSL_DISTRO", source)
        for path in (INSTALLER_PATH, LINUX_PATH):
            self.assertNotIn("Ubuntu", path.read_text(encoding="utf-8"))


class BoundedChildIoTests(unittest.TestCase):
    def test_smoke_reader_requests_one_extra_byte(self) -> None:
        source = LINUX_PATH.read_text(encoding="utf-8")
        self.assertIn("read(MAX_STDOUT + 1)", source)
        self.assertIn("read(MAX_STDERR + 1)", source)
        self.assertNotIn("communicate(", source)

    def _ops(self) -> object:
        return LINUX._LinuxInstallerOperations(INSTALL, DATA, OWNER, UID)

    def test_accept_probe_requires_canonical_line(self) -> None:
        ops = self._ops()
        good = (
            json.dumps(
                {"workspace_id": UID, "product_version": PRODUCT, "protocol_version": 1},
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        ops._accept_probe(good, b"")
        with self.assertRaises(LINUX.InstallerError):
            ops._accept_probe(good, b"x")
        pretty = (
            json.dumps(
                {"workspace_id": UID, "product_version": PRODUCT, "protocol_version": 1},
                indent=2,
            ).encode("utf-8")
            + b"\n"
        )
        with self.assertRaises(LINUX.InstallerError):
            ops._accept_probe(pretty, b"")
        extra = (
            json.dumps(
                {
                    "workspace_id": UID,
                    "product_version": PRODUCT,
                    "protocol_version": 1,
                    "extra": 1,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        with self.assertRaises(LINUX.InstallerError):
            ops._accept_probe(extra, b"")
        boolean_protocol = (
            b'{"workspace_id":"%s","product_version":"%s","protocol_version":true}\n'
            % (UID.encode("ascii"), PRODUCT.encode("ascii"))
        )
        with self.assertRaises(LINUX.InstallerError):
            ops._accept_probe(boolean_protocol, b"")
        duplicate = (
            b'{"workspace_id":"%s","product_version":"%s","protocol_version":1,'
            b'"workspace_id":"%s"}\n' % (UID.encode("ascii"), PRODUCT.encode("ascii"), UID.encode("ascii"))
        )
        with self.assertRaises(LINUX.InstallerError):
            ops._accept_probe(duplicate, b"")
        nan_const = b'{"workspace_id":"%s","product_version":"NaN","protocol_version":1}\n' % UID.encode(
            "ascii"
        )
        # product is a string "NaN" which fails product==PRODUCT, still a refusal
        with self.assertRaises(LINUX.InstallerError):
            ops._accept_probe(nan_const, b"")
        inf_payload = (
            b'{"workspace_id":"%s","product_version":"%s","protocol_version":Infinity}\n'
            % (UID.encode("ascii"), PRODUCT.encode("ascii"))
        )
        with self.assertRaises(LINUX.InstallerError):
            ops._accept_probe(inf_payload, b"")

    def test_run_child_timeout_oversize_and_nonzero(self) -> None:
        ops = self._ops()
        sleeping = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _out, _err, oversize, timed = ops._run_child(sleeping, timeout=0.2)
        finally:
            if sleeping.poll() is None:
                sleeping.kill()
                sleeping.wait(timeout=5)
            for stream in (sleeping.stdout, sleeping.stderr):
                if stream is not None:
                    stream.close()
        self.assertTrue(timed)
        self.assertFalse(oversize)
        huge = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000); sys.stdout.flush()"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, _err, oversize, timed = ops._run_child(huge, timeout=5)
        finally:
            if huge.poll() is None:
                huge.kill()
                huge.wait(timeout=5)
            for stream in (huge.stdout, huge.stderr):
                if stream is not None:
                    stream.close()
        self.assertTrue(oversize)
        self.assertFalse(timed)
        self.assertGreater(len(stdout), LINUX.MAX_STDOUT)
        failed = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _out, _err, oversize, timed = ops._run_child(failed, timeout=5)
        finally:
            if failed.poll() is None:
                failed.kill()
                failed.wait(timeout=5)
            for stream in (failed.stdout, failed.stderr):
                if stream is not None:
                    stream.close()
        self.assertFalse(timed)
        self.assertFalse(oversize)
        self.assertEqual(failed.returncode, 3)


class StoreMetaExactTypeTests(unittest.TestCase):
    def test_bool_and_float_store_meta_are_rejected(self) -> None:
        self.assertTrue(LINUX._store_meta_ok(STORE_META))
        version_bool = json.loads(json.dumps(STORE_META))
        version_bool["version"] = True
        self.assertFalse(LINUX._store_meta_ok(version_bool))
        schema_float = json.loads(json.dumps(STORE_META))
        schema_float["store_schema_version"] = 3.0
        self.assertFalse(LINUX._store_meta_ok(schema_float))


def _authority_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def disposable_v6_authority():
    temporary = tempfile.TemporaryDirectory(prefix="ws-install-v6-")
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
    temporary = tempfile.TemporaryDirectory(prefix="ws-install-v5-")
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


class RemoteSchemaAdmissionTests(unittest.TestCase):
    """Installer metadata predicate admits exact v3/v5/v6; v5/v6 oracles stay in tests."""

    def decode(self, meta: bytes, workspace: bytes):
        return LINUX._data_object(meta), LINUX._data_object(workspace)

    def mutated_meta(self, values: dict[str, object], mutator) -> bytes:
        metadata = json.loads(json.dumps(values["store-meta.json"]))
        mutator(metadata)
        return _authority_bytes(metadata)

    def test_genuine_v6_initialize_bytes_admit_and_bind_oracle_uid(self) -> None:
        temporary, oracle, values, meta, workspace = disposable_v6_authority()
        self.addCleanup(temporary.cleanup)
        parsed_meta, parsed_space = self.decode(meta, workspace)
        self.assertEqual(parsed_meta["store_schema_version"], 6)
        self.assertEqual(
            set(parsed_meta["migrations"]),
            {"identity", "planning_status", "reports", "knowledge"},
        )
        knowledge = parsed_meta["migrations"]["knowledge"]
        self.assertEqual(knowledge["id"], "workstack.knowledge.v6")
        self.assertEqual(knowledge["origin"], "fresh")
        self.assertIsNone(knowledge["source_sha256"])
        self.assertTrue(LINUX._store_meta_ok(parsed_meta))
        self.assertEqual(LINUX._data_uid(parsed_space), oracle.workspace_uid)
        self.assertEqual(oracle.schema_version, 6)
        self.assertEqual(set(values), set(V6_DOCUMENT_NAMES))
        self.assertNotEqual(oracle.schema_version, 5)

    def test_genuine_v5_bytes_admit_and_bind_oracle_uid(self) -> None:
        temporary, oracle, _values, meta, workspace = disposable_v5_authority()
        self.addCleanup(temporary.cleanup)
        parsed_meta, parsed_space = self.decode(meta, workspace)
        self.assertEqual(parsed_meta["store_schema_version"], 5)
        self.assertNotIn("knowledge", parsed_meta["migrations"])
        self.assertTrue(LINUX._store_meta_ok(parsed_meta))
        self.assertEqual(LINUX._data_uid(parsed_space), oracle.workspace_uid)
        self.assertEqual(oracle.schema_version, 5)

    def test_genuine_v3_fixture_still_admits(self) -> None:
        temporary, oracle, _values, meta, workspace = genuine_v3_authority()
        self.addCleanup(temporary.cleanup)
        parsed_meta, parsed_space = self.decode(meta, workspace)
        self.assertTrue(LINUX._store_meta_ok(parsed_meta))
        self.assertEqual(LINUX._data_uid(parsed_space), oracle.workspace_uid)
        self.assertEqual(oracle.schema_version, 3)

    def test_migrated_reports_evidence_is_admitted(self) -> None:
        temporary, oracle, values, _meta, workspace = disposable_v5_authority()
        self.addCleanup(temporary.cleanup)
        parsed_space = LINUX._data_object(workspace)
        for origin in ("migrated_v1", "migrated_v2", "migrated_v3"):
            with self.subTest(origin=origin):
                def mutate(metadata, chosen=origin):
                    metadata["migrations"]["reports"] = {
                        "id": "workstack.reports.v3-to-v5",
                        "origin": chosen,
                        "source_sha256": LOCK,
                    }

                meta = self.mutated_meta(values, mutate)
                Store.validate_document_values(
                    {**values, "store-meta.json": json.loads(meta.decode("utf-8"))},
                    schema_version=5,
                )
                parsed_meta = LINUX._data_object(meta)
                self.assertTrue(LINUX._store_meta_ok(parsed_meta))
                self.assertEqual(LINUX._data_uid(parsed_space), oracle.workspace_uid)

    def test_migrated_knowledge_evidence_is_admitted(self) -> None:
        temporary, oracle, values, _meta, workspace = disposable_v6_authority()
        self.addCleanup(temporary.cleanup)
        parsed_space = LINUX._data_object(workspace)
        for origin in ("migrated_v1", "migrated_v2", "migrated_v3", "migrated_v5"):
            with self.subTest(origin=origin):
                def mutate(metadata, chosen=origin):
                    metadata["migrations"]["knowledge"] = {
                        "id": "workstack.knowledge.v5-to-v6",
                        "origin": chosen,
                        "source_sha256": LOCK,
                    }

                meta = self.mutated_meta(values, mutate)
                Store.validate_document_values(
                    {**values, "store-meta.json": json.loads(meta.decode("utf-8"))},
                    schema_version=6,
                )
                parsed_meta = LINUX._data_object(meta)
                self.assertEqual(parsed_meta["store_schema_version"], 6)
                self.assertTrue(LINUX._store_meta_ok(parsed_meta))
                self.assertEqual(LINUX._data_uid(parsed_space), oracle.workspace_uid)

    def test_bool_float_and_unsupported_schema_versions_are_refused(self) -> None:
        temporary, _oracle, values, _meta, _workspace = disposable_v5_authority()
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
                parsed = LINUX._data_object(self.mutated_meta(values, mutator))
                self.assertFalse(LINUX._store_meta_ok(parsed))

    def test_partial_mixed_and_extra_migration_records_are_refused(self) -> None:
        temporary, _oracle, values, _meta, _workspace = disposable_v5_authority()
        self.addCleanup(temporary.cleanup)

        def drop_reports(metadata):
            del metadata["migrations"]["reports"]

        def extra_record(metadata):
            metadata["migrations"]["ssot"] = metadata["migrations"]["identity"]

        def mixed_v3_reports(metadata):
            metadata["store_schema_version"] = 3

        def extra_top(metadata):
            metadata["extra"] = 1

        def v6_claimed_as_v5(metadata):
            metadata["store_schema_version"] = 5
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
            ("v5_with_knowledge", v6_claimed_as_v5),
        ):
            with self.subTest(name=name):
                parsed = LINUX._data_object(self.mutated_meta(values, mutator))
                self.assertFalse(LINUX._store_meta_ok(parsed))

    def test_partial_v6_roster_and_v7_are_refused(self) -> None:
        temporary, _oracle, values, _meta, _workspace = disposable_v6_authority()
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
                parsed = LINUX._data_object(self.mutated_meta(values, mutator))
                self.assertFalse(LINUX._store_meta_ok(parsed))

    def test_invalid_reports_evidence_is_refused(self) -> None:
        temporary, _oracle, values, _meta, _workspace = disposable_v5_authority()
        self.addCleanup(temporary.cleanup)

        def set_reports(metadata, **fields):
            metadata["migrations"]["reports"].update(fields)

        cases = (
            ("fresh_digest", {"source_sha256": LOCK}),
            ("wrong_fresh_id", {"id": "workstack.reports.v3-to-v5"}),
            ("unknown_origin", {"origin": "migrated_v4", "id": "workstack.reports.v3-to-v5", "source_sha256": LOCK}),
            ("migrated_null_digest", {"origin": "migrated_v3", "id": "workstack.reports.v3-to-v5", "source_sha256": None}),
            ("migrated_wrong_id", {"origin": "migrated_v3", "id": "workstack.reports.v5", "source_sha256": LOCK}),
            ("uppercase_digest", {"origin": "migrated_v3", "id": "workstack.reports.v3-to-v5", "source_sha256": LOCK.replace("c", "C")}),
        )
        for name, fields in cases:
            with self.subTest(name=name):
                meta = self.mutated_meta(values, lambda metadata, payload=fields: set_reports(metadata, **payload))
                self.assertFalse(LINUX._store_meta_ok(LINUX._data_object(meta)))

    def test_invalid_knowledge_evidence_is_refused(self) -> None:
        temporary, _oracle, values, _meta, _workspace = disposable_v6_authority()
        self.addCleanup(temporary.cleanup)

        def set_knowledge(metadata, **fields):
            metadata["migrations"]["knowledge"].update(fields)

        cases = (
            ("fresh_digest", {"source_sha256": LOCK}),
            ("wrong_fresh_id", {"id": "workstack.knowledge.v5-to-v6"}),
            ("unknown_origin", {"origin": "migrated_v4", "id": "workstack.knowledge.v5-to-v6", "source_sha256": LOCK}),
            ("migrated_null_digest", {"origin": "migrated_v5", "id": "workstack.knowledge.v5-to-v6", "source_sha256": None}),
            ("migrated_wrong_id", {"origin": "migrated_v5", "id": "workstack.knowledge.v6", "source_sha256": LOCK}),
            ("uppercase_digest", {"origin": "migrated_v5", "id": "workstack.knowledge.v5-to-v6", "source_sha256": LOCK.replace("c", "C")}),
        )
        for name, fields in cases:
            with self.subTest(name=name):
                meta = self.mutated_meta(values, lambda metadata, payload=fields: set_knowledge(metadata, **payload))
                self.assertFalse(LINUX._store_meta_ok(LINUX._data_object(meta)))

    def test_noncanonical_nil_and_different_expected_uid(self) -> None:
        temporary, oracle, _values, meta, workspace = disposable_v5_authority()
        self.addCleanup(temporary.cleanup)
        parsed_meta = LINUX._data_object(meta)
        self.assertTrue(LINUX._store_meta_ok(parsed_meta))
        self.assertIsNone(LINUX._data_uid({"id": LETTER_UID.upper()}))
        self.assertIsNone(LINUX._data_uid({"id": NIL_UID}))
        self.assertIsNone(LINUX._data_uid({"id": "urn:uuid:" + LETTER_UID}))
        actual = LINUX._data_uid(LINUX._data_object(workspace))
        self.assertEqual(actual, oracle.workspace_uid)
        self.assertNotEqual(actual, OTHER_UID)

    def test_uid_swap_oracle_rejects_mixed_authority_metadata_still_admits(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ws-install-swap-")
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
        parsed_meta = LINUX._data_object((Path(temporary.name) / "store-meta.json").read_bytes())
        parsed_space = LINUX._data_object(_authority_bytes(mixed["workspace.json"]))
        self.assertTrue(LINUX._store_meta_ok(parsed_meta))
        self.assertEqual(LINUX._data_uid(parsed_space), task_uid)
        self.assertNotEqual(LINUX._data_uid(parsed_space), original.workspace_uid)
        self.assertNotEqual(LINUX._data_uid(parsed_space), OTHER_UID)


class QualityBudgetTests(unittest.TestCase):
    def test_owned_modules_stay_within_limits(self) -> None:
        def complexity(function: ast.AST) -> int:
            score = 1
            for node in ast.walk(function):
                if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.Assert)):
                    score += 1
                elif isinstance(node, ast.BoolOp):
                    score += max(0, len(node.values) - 1)
                elif isinstance(node, ast.Try):
                    score += len(node.handlers) + int(bool(node.orelse))
                elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                    score += sum(1 + len(generator.ifs) for generator in node.generators)
            return score

        class Collector(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scope: list[str] = []
                self.items: list[tuple[str, ast.AST]] = []

            def _visit_function(self, node: ast.AST) -> None:
                name = ".".join([*self.scope, getattr(node, "name")])
                self.items.append((name, node))
                self.scope.append(getattr(node, "name"))
                self.generic_visit(node)
                self.scope.pop()

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_function(node)

        for path in (INSTALLER_PATH, LINUX_PATH):
            source = path.read_text(encoding="utf-8")
            self.assertLessEqual(len(source.splitlines()), 800)
            tree = ast.parse(source)
            collector = Collector()
            collector.visit(tree)
            for name, node in collector.items:
                end = getattr(node, "end_lineno", None) or node.lineno
                length = end - node.lineno + 1
                self.assertLessEqual(length, 100, name)
                self.assertLessEqual(complexity(node), 15, name)


def win_to_wsl(path: Path) -> str:
    resolved = str(path.resolve())
    return "/mnt/%s%s" % (resolved[0].lower(), resolved[2:].replace("\\", "/"))


def wsl_exe() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return str(Path(root) / "System32" / "wsl.exe")


def wsl_host_env() -> dict[str, str]:
    environment = dict(os.environ)
    profile = environment.get("USERPROFILE")
    if profile:
        local = Path(profile) / "AppData" / "Local"
        environment["LOCALAPPDATA"] = str(local)
        environment["APPDATA"] = str(Path(profile) / "AppData" / "Roaming")
        environment["TEMP"] = str(local / "Temp")
        environment["TMP"] = environment["TEMP"]
    return environment


def primitive_command(script: Path) -> list[str] | None:
    if sys.platform.startswith("linux"):
        return [sys.executable, str(script), str(LINUX_PATH)]
    distro = os.environ.get("WORKSTACK_TEST_WSL_DISTRO")
    if not distro:
        return None
    return [wsl_exe(), "-d", distro, "-e", "python3", win_to_wsl(script), win_to_wsl(LINUX_PATH)]


WSL_PRIMITIVE_SCRIPT = r"""
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile


def load_linux(path):
    spec = importlib.util.spec_from_file_location("remote_provision_installer_linux", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The loaded engine is the only source of the release identity these fixtures
# must agree with; main() fills these in from it so a release bump cannot leave
# this script smoke-testing a superseded version.
PRODUCT = ""
PROTOCOL = 0


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def record(report, name, ok, **extra):
    item = {"name": name, "ok": bool(ok)}
    item.update(extra)
    report["results"].append(item)


def put_named(ops, name, payload):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=ops.data_fd)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def file_hashes(path):
    out = {}
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.isfile(full):
            with open(full, "rb") as handle:
                out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


def write_store(ops, uid):
    meta = {
        "migrations": {
            "identity": {"id": "workstack.store.v2", "origin": "fresh", "source_sha256": None},
            "planning_status": {"id": "workstack.planning-status.v1", "origin": "fresh", "source_sha256": None},
        },
        "store_schema_version": 3,
        "version": 2,
    }
    put_named(ops, "store-meta.json", json.dumps(meta, separators=(",", ":")).encode("utf-8"))
    put_named(ops, "workspace.json", json.dumps({"id": uid}, separators=(",", ":")).encode("utf-8"))


def swap_after_stat(orig_stat, child_name, replacement, stage_id, stage_fd):
    state = {"done": False}

    def racing_stat(path, *args, **kwargs):
        info = orig_stat(path, *args, **kwargs)
        dir_fd = kwargs.get("dir_fd")
        if path != child_name or dir_fd is None or kwargs.get("follow_symlinks") is not False or state["done"]:
            return info
        try:
            parent = os.fstat(dir_fd)
        except OSError:
            return info
        if (parent.st_dev, parent.st_ino) != stage_id:
            return info
        state["done"] = True
        os.rename(child_name, child_name + ".owned", src_dir_fd=stage_fd, dst_dir_fd=stage_fd)
        os.rename(replacement, child_name, dst_dir_fd=stage_fd)
        return info

    return racing_stat, state


def write_tree(root, files):
    for rel, payload in files.items():
        dest = os.path.join(root, rel)
        parent = os.path.dirname(dest)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, 0o700)
        with open(dest, "wb") as handle:
            handle.write(payload)


def smoke_files(marker=None):
    workstack = ('__version__ = "%s"\nREMOTE_PROTOCOL_VERSION = %d\n' % (PRODUCT, PROTOCOL)).encode()
    if marker is not None:
        workstack = ("open(%r,'w').write('executed')\n" % marker).encode() + workstack
    return {
        "workstack/__init__.py": workstack,
        "jsonschema/__init__.py": b"",
        "unicodedata2/__init__.py": b'unidata_version = "17.0.0"\n',
        "rpds/__init__.py": b"",
    }


def probe_bytes(uid, marker=None):
    line = json.dumps(
        {"workspace_id": uid, "product_version": PRODUCT, "protocol_version": PROTOCOL},
        ensure_ascii=True,
        separators=(",", ":"),
    ) + "\n"
    prefix = ""
    if marker is not None:
        prefix = "open(%r,'w').write('executed')\n" % marker
    return (prefix + "import sys\nsys.stdout.buffer.write(%r)\n" % line.encode()).encode()


def main():
    global PRODUCT, PROTOCOL
    linux = load_linux(sys.argv[1])
    PRODUCT = linux.PRODUCT
    PROTOCOL = linux.PROTOCOL
    uid = "11111111-1111-4111-8111-111111111111"
    report = {
        "python": sys.version.split()[0],
        "euid": os.geteuid(),
        "results": [],
        "cp312_install_claimed": False,
    }
    base = tempfile.mkdtemp(prefix="workstack-remote-installer-", dir="/tmp")
    try:
        os.chmod(base, 0o755)
        parent = os.path.join(base, "owner")
        data = os.path.join(base, "data")
        os.mkdir(parent, 0o755)
        os.mkdir(data, 0o755)
        install = os.path.join(parent, "app")

        missing = linux._LinuxInstallerOperations(install, os.path.join(base, "missing"), "owner", uid)
        code = ""
        try:
            missing.open_roots()
        except linux.InstallerError as error:
            code = error.code
        record(report, "open_roots_partial_close", missing.parent_fd == -1 and code == "DATA_STATE_UNKNOWN", code=code)

        v5_meta = {
            "migrations": {
                "identity": {"id": "workstack.store.v2", "origin": "fresh", "source_sha256": None},
                "planning_status": {"id": "workstack.planning-status.v1", "origin": "fresh", "source_sha256": None},
                "reports": {"id": "workstack.reports.v5", "origin": "fresh", "source_sha256": None},
            },
            "store_schema_version": 5,
            "version": 2,
        }

        data_v5 = os.path.join(base, "data-v5")
        os.mkdir(data_v5, 0o755)
        ops = linux._LinuxInstallerOperations(install, data_v5, "owner", uid)
        ops.open_roots()
        put_named(ops, "store-meta.json", json.dumps(v5_meta, separators=(",", ":")).encode("utf-8"))
        put_named(ops, "workspace.json", json.dumps({"id": uid}, separators=(",", ":")).encode("utf-8"))
        before = file_hashes(data_v5)
        v5_code = ""
        try:
            ops.admit_data()
        except linux.InstallerError as error:
            v5_code = error.code
        record(report, "v5_admit_data", v5_code == "" and file_hashes(data_v5) == before, code=v5_code)
        ops.close_fds()

        data_v3 = os.path.join(base, "data-v3")
        os.mkdir(data_v3, 0o755)
        ops = linux._LinuxInstallerOperations(install, data_v3, "owner", uid)
        ops.open_roots()
        write_store(ops, uid)
        before = file_hashes(data_v3)
        v3_code = ""
        try:
            ops.admit_data()
        except linux.InstallerError as error:
            v3_code = error.code
        record(report, "v3_admit_data", v3_code == "" and file_hashes(data_v3) == before, code=v3_code)
        ops.close_fds()

        data_v4 = os.path.join(base, "data-v4")
        os.mkdir(data_v4, 0o755)
        ops = linux._LinuxInstallerOperations(install, data_v4, "owner", uid)
        ops.open_roots()
        v4_meta = json.loads(json.dumps(v5_meta))
        v4_meta["store_schema_version"] = 4
        put_named(ops, "store-meta.json", json.dumps(v4_meta, separators=(",", ":")).encode("utf-8"))
        put_named(ops, "workspace.json", json.dumps({"id": uid}, separators=(",", ":")).encode("utf-8"))
        before = file_hashes(data_v4)
        v4_code = ""
        try:
            ops.admit_data()
            v4_ok = False
        except linux.InstallerError as error:
            v4_code = error.code
            v4_ok = v4_code == "DATA_STATE_UNKNOWN"
        record(report, "v4_schema_refuse", v4_ok and file_hashes(data_v4) == before, code=v4_code)
        ops.close_fds()

        data_mis = os.path.join(base, "data-mis")
        os.mkdir(data_mis, 0o755)
        ops = linux._LinuxInstallerOperations(install, data_mis, "owner", "22222222-2222-4222-8222-222222222222")
        ops.open_roots()
        put_named(ops, "store-meta.json", json.dumps(v5_meta, separators=(",", ":")).encode("utf-8"))
        put_named(ops, "workspace.json", json.dumps({"id": uid}, separators=(",", ":")).encode("utf-8"))
        before = file_hashes(data_mis)
        mis_code = ""
        try:
            ops.admit_data()
            mis_ok = False
        except linux.InstallerError as error:
            mis_code = error.code
            mis_ok = mis_code == "REMOTE_WORKSPACE_MISMATCH"
        record(report, "v5_expected_uid_mismatch", mis_ok and file_hashes(data_mis) == before, code=mis_code)
        ops.close_fds()

        ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
        ops.open_roots()
        ops.create_stage()
        first = b"shared-a\n"
        second = b"shared-b\n"
        ops.write_file("workstack/__init__.py", first, digest(first))
        ops.write_file("workstack/other.py", second, digest(second))
        listing = os.listdir("/proc/self/fd/%d" % ops.stage_fd)
        workstack = os.open("workstack", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=ops.stage_fd)
        try:
            names = set(os.listdir("/proc/self/fd/%d" % workstack))
            init_fd = os.open("__init__.py", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=workstack)
            other_fd = os.open("other.py", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=workstack)
            try:
                shared_ok = names >= {"__init__.py", "other.py"} and os.read(init_fd, 64) == first and os.read(other_fd, 64) == second
            finally:
                os.close(init_fd)
                os.close(other_fd)
        finally:
            os.close(workstack)
        record(report, "shared_dir_extract", shared_ok and "workstack" in ops.dir_ids, dirs=sorted(ops.dir_ids), listing=listing)

        try:
            ops._bind_stage_and_data()
            bind_ok = True
        except linux.InstallerError as error:
            bind_ok = False
            report["bind_error"] = error.code
        record(report, "bind_before_swap", bind_ok)

        os.rename(ops.stage_path, ops.stage_path + ".moved")
        os.mkdir(ops.stage_path, 0o700)
        bind_code = ""
        try:
            ops._bind_stage_and_data()
            swapped = False
        except linux.InstallerError as error:
            swapped = True
            bind_code = error.code
        record(report, "stage_swap_bind", swapped, code=bind_code)
        os.rmdir(ops.stage_path)
        os.rename(ops.stage_path + ".moved", ops.stage_path)

        os.rename(data, data + ".moved")
        os.mkdir(data, 0o755)
        data_code = ""
        try:
            ops._bind_stage_and_data()
            data_swapped = False
        except linux.InstallerError as error:
            data_swapped = True
            data_code = error.code
        record(report, "data_swap_bind", data_swapped, code=data_code)
        os.rmdir(data)
        os.rename(data + ".moved", data)

        os.rename(parent, parent + ".moved")
        os.mkdir(parent, 0o755)
        parent_code = ""
        try:
            ops._bind_stage_and_data()
            parent_swapped = False
        except linux.InstallerError as error:
            parent_swapped = True
            parent_code = error.code
        record(report, "parent_swap_bind", parent_swapped, code=parent_code)
        os.rmdir(parent)
        os.rename(parent + ".moved", parent)

        stage_path = ops.stage_path
        ops.close_fds()
        ops.close_fds()
        record(report, "close_fds_idempotent", ops.parent_fd == -1 and ops.stage_fd == -1 and os.path.isdir(stage_path))

        ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
        ops.open_roots()
        ops.create_stage()
        marker = b"owned-stage\n"
        ops.write_file("marker.txt", marker, digest(marker))
        os.rename(ops.stage_name, ops.stage_name + ".old", src_dir_fd=ops.parent_fd, dst_dir_fd=ops.parent_fd)
        os.mkdir(ops.stage_name, 0o700, dir_fd=ops.parent_fd)
        repl = os.open(ops.stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=ops.parent_fd)
        try:
            canary = os.open("canary.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=repl)
            try:
                os.write(canary, b"replacement\n")
            finally:
                os.close(canary)
        finally:
            os.close(repl)
        ops.cleanup_stage()
        repl = os.open(ops.stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=ops.parent_fd)
        try:
            canary_alive = "canary.txt" in os.listdir("/proc/self/fd/%d" % repl)
        finally:
            os.close(repl)
        old = os.open(ops.stage_name + ".old", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=ops.parent_fd)
        try:
            old_alive = "marker.txt" in os.listdir("/proc/self/fd/%d" % old)
        finally:
            os.close(old)
        record(report, "cleanup_skips_replacement", canary_alive and old_alive)
        ops.close_fds()

        orig_stat = os.stat
        ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
        ops.open_roots()
        ops.create_stage()
        nested = b"owned-nested\n"
        ops.write_file("nested/owned.txt", nested, digest(nested))
        foreign_dir = os.path.join(base, "foreign-dir")
        os.mkdir(foreign_dir, 0o700)
        with open(os.path.join(foreign_dir, "canary.txt"), "wb") as handle:
            handle.write(b"foreign-dir\n")
        racing_stat, state = swap_after_stat(orig_stat, "nested", foreign_dir, ops.stage_id, ops.stage_fd)
        os.stat = racing_stat
        wipe_code = ""
        try:
            try:
                ops.cleanup_stage()
            except linux.InstallerError as error:
                wipe_code = error.code
        finally:
            os.stat = orig_stat
        nested_fd = os.open("nested", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=ops.stage_fd)
        try:
            dir_canary = "canary.txt" in os.listdir("/proc/self/fd/%d" % nested_fd)
        finally:
            os.close(nested_fd)
        owned_fd = os.open("nested.owned", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=ops.stage_fd)
        try:
            owned_kept = "owned.txt" in os.listdir("/proc/self/fd/%d" % owned_fd)
        finally:
            os.close(owned_fd)
        record(report, "wipe_dir_swap_directory", dir_canary and owned_kept and state["done"] and wipe_code == "REMOTE_INSTALL_FAILED", code=wipe_code)
        ops.close_fds()

        ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
        ops.open_roots()
        ops.create_stage()
        owned_file = b"owned-file\n"
        ops.write_file("owned.txt", owned_file, digest(owned_file))
        foreign_file = os.path.join(base, "foreign-file.txt")
        with open(foreign_file, "wb") as handle:
            handle.write(b"foreign-file-canary\n")
        racing_stat, state = swap_after_stat(orig_stat, "owned.txt", foreign_file, ops.stage_id, ops.stage_fd)
        os.stat = racing_stat
        wipe_code = ""
        try:
            try:
                ops.cleanup_stage()
            except linux.InstallerError as error:
                wipe_code = error.code
        finally:
            os.stat = orig_stat
        file_fd = os.open("owned.txt", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=ops.stage_fd)
        try:
            file_canary = os.read(file_fd, 64) == b"foreign-file-canary\n"
        finally:
            os.close(file_fd)
        kept_fd = os.open("owned.txt.owned", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=ops.stage_fd)
        try:
            file_kept = os.read(kept_fd, 64) == owned_file
        finally:
            os.close(kept_fd)
        record(report, "wipe_dir_swap_file", file_canary and file_kept and state["done"] and wipe_code == "REMOTE_INSTALL_FAILED", code=wipe_code)
        ops.close_fds()

        ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
        ops.open_roots()
        ops.create_stage()
        owned_file = b"owned-file-unlink\n"
        ops.write_file("victim.txt", owned_file, digest(owned_file))
        foreign_file = os.path.join(base, "foreign-unlink.txt")
        with open(foreign_file, "wb") as handle:
            handle.write(b"foreign-unlink-canary\n")
        orig_reclaim = ops._reclaim_held
        state = {"done": False}

        def racing_reclaim(dir_fd, name, held, *, directory):
            if not directory and name == "victim.txt" and not state["done"]:
                state["done"] = True
                os.rename("victim.txt", "victim.txt.owned", src_dir_fd=ops.stage_fd, dst_dir_fd=ops.stage_fd)
                os.rename(foreign_file, "victim.txt", dst_dir_fd=ops.stage_fd)
            orig_reclaim(dir_fd, name, held, directory=directory)

        ops._reclaim_held = racing_reclaim
        wipe_code = ""
        try:
            ops.cleanup_stage()
        except linux.InstallerError as error:
            wipe_code = error.code
        file_fd = os.open("victim.txt", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=ops.stage_fd)
        try:
            file_canary = os.read(file_fd, 64) == b"foreign-unlink-canary\n"
        finally:
            os.close(file_fd)
        kept_fd = os.open("victim.txt.owned", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=ops.stage_fd)
        try:
            owned_kept = os.read(kept_fd, 64) == owned_file
        finally:
            os.close(kept_fd)
        record(
            report,
            "wipe_file_swap_at_unlink",
            file_canary and owned_kept and state["done"] and wipe_code == "REMOTE_INSTALL_FAILED",
            code=wipe_code,
        )
        ops.close_fds()

        ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
        ops.open_roots()
        ops.create_stage()
        os.mkdir("nested", 0o700, dir_fd=ops.stage_fd)
        foreign_dir = os.path.join(base, "foreign-dir-unlink")
        os.mkdir(foreign_dir, 0o700)
        with open(os.path.join(foreign_dir, "canary.txt"), "wb") as handle:
            handle.write(b"foreign-dir-unlink\n")
        orig_reclaim = ops._reclaim_held
        state = {"done": False}

        def racing_reclaim_dir(dir_fd, name, held, *, directory):
            if directory and name == "nested" and not state["done"]:
                state["done"] = True
                os.rename("nested", "nested.owned", src_dir_fd=ops.stage_fd, dst_dir_fd=ops.stage_fd)
                os.rename(foreign_dir, "nested", dst_dir_fd=ops.stage_fd)
            orig_reclaim(dir_fd, name, held, directory=directory)

        ops._reclaim_held = racing_reclaim_dir
        wipe_code = ""
        try:
            ops.cleanup_stage()
        except linux.InstallerError as error:
            wipe_code = error.code
        nested_fd = os.open("nested", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=ops.stage_fd)
        try:
            dir_canary = "canary.txt" in os.listdir("/proc/self/fd/%d" % nested_fd)
        finally:
            os.close(nested_fd)
        owned_kept = os.path.isdir(os.path.join(ops.stage_path, "nested.owned"))
        record(
            report,
            "wipe_dir_swap_at_unlink",
            dir_canary and owned_kept and state["done"] and wipe_code == "REMOTE_INSTALL_FAILED",
            code=wipe_code,
        )
        ops.close_fds()

        ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
        ops.open_roots()
        ops.create_stage()
        payload = ('__version__ = "%s"\nREMOTE_PROTOCOL_VERSION = %d\n' % (PRODUCT, PROTOCOL)).encode()
        ops.write_file("workstack/__init__.py", payload, digest(payload))
        orig_bind = ops._bind_stage_and_data
        binds = {"n": 0}

        def import_bind():
            binds["n"] += 1
            if binds["n"] == 2:
                os.rename(ops.stage_path, ops.stage_path + ".kept")
                os.mkdir(ops.stage_path, 0o700)
            orig_bind()

        ops._bind_stage_and_data = import_bind
        import_code = ""
        try:
            ops.smoke_imports()
        except linux.InstallerError as error:
            import_code = error.code
        kept_alive = os.path.isdir(ops.stage_path + ".kept")
        repl_alive = os.path.isdir(ops.stage_path)
        record(
            report,
            "import_seam_swap",
            import_code == "REMOTE_INSTALL_FAILED" and binds["n"] >= 2 and kept_alive and repl_alive,
            code=import_code,
            binds=binds["n"],
        )
        ops.close_fds()

        ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
        ops.open_roots()
        write_store(ops, uid)
        ops.admit_data()
        ops.create_stage()
        entry = b"print(1)\n"
        ops.write_file("desktop/python-webview-shell/remote_entry.py", entry, digest(entry))
        orig_bind = ops._bind_stage_and_data
        binds = {"n": 0}

        def popen_bind():
            binds["n"] += 1
            if binds["n"] == 2:
                os.rename(ops.stage_path, ops.stage_path + ".popen-kept")
                os.mkdir(ops.stage_path, 0o700)
            orig_bind()

        ops._bind_stage_and_data = popen_bind
        popen_code = ""
        try:
            ops.smoke_entrypoint()
        except linux.InstallerError as error:
            popen_code = error.code
        popen_kept = os.path.isdir(ops.stage_path + ".popen-kept")
        popen_repl = os.path.isdir(ops.stage_path)
        record(
            report,
            "popen_seam_swap",
            popen_code == "REMOTE_INSTALL_FAILED" and binds["n"] >= 2 and popen_kept and popen_repl,
            code=popen_code,
            binds=binds["n"],
        )
        ops.close_fds()

        ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
        ops.open_roots()
        ops.create_stage()
        for rel, payload in smoke_files().items():
            ops.write_file(rel, payload, digest(payload))
        marker = os.path.join(base, "import-executed")
        repl = os.path.join(base, "import-repl")
        write_tree(repl, smoke_files(marker))
        import importlib
        orig_import = importlib.import_module
        swapped = {"done": False}

        def racing_import(name, *a, **kw):
            if not swapped["done"] and name == "workstack":
                swapped["done"] = True
                os.rename(ops.stage_path, ops.stage_path + ".kept")
                os.rename(repl, ops.stage_path)
            return orig_import(name, *a, **kw)

        importlib.import_module = racing_import
        import_code = ""
        try:
            ops.smoke_imports()
        except linux.InstallerError as error:
            import_code = error.code
        finally:
            importlib.import_module = orig_import
        executed = os.path.isfile(marker)
        record(
            report,
            "import_swap_at_import_module",
            swapped["done"] and not executed and os.path.isdir(ops.stage_path + ".kept") and os.path.isdir(ops.stage_path) and import_code == "",
            code=import_code,
            executed=executed,
        )
        ops.close_fds()

        data_popen = os.path.join(base, "data-popen")
        os.mkdir(data_popen, 0o755)
        ops = linux._LinuxInstallerOperations(install, data_popen, "owner", uid)
        ops.open_roots()
        write_store(ops, uid)
        ops.admit_data()
        ops.create_stage()
        owned_entry = probe_bytes(uid)
        ops.write_file("desktop/python-webview-shell/remote_entry.py", owned_entry, digest(owned_entry))
        marker = os.path.join(base, "popen-executed")
        repl = os.path.join(base, "popen-repl")
        write_tree(repl, {"desktop/python-webview-shell/remote_entry.py": probe_bytes(uid, marker)})
        import subprocess
        orig_popen = subprocess.Popen
        swapped = {"done": False}

        def racing_popen(*a, **kw):
            if not swapped["done"]:
                swapped["done"] = True
                os.rename(ops.stage_path, ops.stage_path + ".popen-kept")
                os.rename(repl, ops.stage_path)
            return orig_popen(*a, **kw)

        subprocess.Popen = racing_popen
        popen_code = ""
        try:
            ops.smoke_entrypoint()
        except linux.InstallerError as error:
            popen_code = error.code
        finally:
            subprocess.Popen = orig_popen
        executed = os.path.isfile(marker)
        record(
            report,
            "popen_swap_at_popen",
            swapped["done"] and not executed and os.path.isdir(ops.stage_path + ".popen-kept") and os.path.isdir(ops.stage_path) and popen_code == "",
            code=popen_code,
            executed=executed,
        )
        ops.close_fds()

        ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
        ops.open_roots()
        ops.create_stage()
        foreign_root = os.path.join(base, "foreign-stage-root")
        os.mkdir(foreign_root, 0o700)
        with open(os.path.join(foreign_root, "canary.txt"), "wb") as handle:
            handle.write(b"foreign-stage-root\n")
        orig_reclaim = ops._reclaim_held
        state = {"done": False}

        def racing_reclaim_root(dir_fd, name, held, *, directory):
            if directory and name == ops.stage_name and not state["done"]:
                state["done"] = True
                os.rename(ops.stage_path, ops.stage_path + ".owned")
                os.rename(foreign_root, ops.stage_path)
            orig_reclaim(dir_fd, name, held, directory=directory)

        ops._reclaim_held = racing_reclaim_root
        root_code = ""
        try:
            ops.cleanup_stage()
        except linux.InstallerError as error:
            root_code = error.code
        root_canary = os.path.isfile(os.path.join(ops.stage_path, "canary.txt"))
        record(
            report,
            "stage_root_swap_at_unlink",
            root_canary
            and os.path.isdir(ops.stage_path + ".owned")
            and state["done"]
            and root_code == "REMOTE_INSTALL_FAILED",
            code=root_code,
        )
        ops.close_fds()
        shutil.rmtree(ops.stage_path, ignore_errors=True)
        shutil.rmtree(ops.stage_path + ".owned", ignore_errors=True)

        for label, members in (("empty", ()), ("file", ("nested/marker.txt", "top.txt"))):
            ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
            ops.open_roots()
            ops.create_stage()
            for rel in members:
                payload = ("no-race-%s\n" % rel).encode()
                ops.write_file(rel, payload, digest(payload))
            stage_path = ops.stage_path
            clean_code = ""
            try:
                ops.cleanup_stage()
            except linux.InstallerError as error:
                clean_code = error.code
            tokens = [name for name in os.listdir(parent) if ".reclaim-" in name]
            record(
                report,
                "no_race_owned_cleanup_" + label,
                clean_code == ""
                and not os.path.exists(stage_path)
                and not ops.stage_ready
                and not ops.stage_name
                and not tokens,
                code=clean_code,
                tokens=tokens,
            )
            ops.close_fds()

        ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
        ops.open_roots()
        ops.create_stage()
        keep = b"keep-eexist\n"
        ops.write_file("keep.txt", keep, digest(keep))
        os.mkdir(ops.install_base, 0o755, dir_fd=ops.parent_fd)
        eexist_code = ""
        try:
            ops.commit_noreplace()
        except linux.InstallerError as error:
            eexist_code = error.code
        stage_before = os.path.isdir(ops.stage_path)
        cleanup_code = ""
        try:
            ops.cleanup_stage()
        except linux.InstallerError as error:
            cleanup_code = error.code
        stage_after = os.path.isdir(ops.stage_path)
        record(
            report,
            "rename_eexist_cleanup",
            eexist_code == "INSTALL_ROOT_EXISTS"
            and not ops.commit_unknown
            and stage_before
            and cleanup_code == ""
            and not stage_after
            and not ops.stage_ready
            and not ops.stage_name,
            code=eexist_code,
            cleanup_code=cleanup_code,
        )
        os.rmdir(ops.install_base, dir_fd=ops.parent_fd)
        ops.close_fds()

        ops = linux._LinuxInstallerOperations(install, data, "owner", uid)
        ops.open_roots()
        ops.create_stage()
        keep = b"keep-unknown\n"
        ops.write_file("keep.txt", keep, digest(keep))
        ops.parent_fd = -1
        unknown_code = ""
        try:
            ops.commit_noreplace()
        except linux.InstallerError as error:
            unknown_code = error.code
        stage_after = os.path.isdir(ops.stage_path)
        target_after = os.path.exists(install)
        ops.cleanup_stage()
        record(
            report,
            "rename_unknown_no_delete",
            unknown_code == "REMOTE_INSTALL_COMMIT_UNKNOWN"
            and ops.commit_unknown
            and ops.rename_attempted
            and stage_after
            and os.path.isdir(ops.stage_path)
            and not target_after,
            code=unknown_code,
        )
        ops.close_fds()
    finally:
        shutil.rmtree(base, ignore_errors=True)
    json.dump(report, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
"""


@unittest.skipUnless(
    sys.platform.startswith("linux") or bool(os.environ.get("WORKSTACK_TEST_WSL_DISTRO")),
    "native Linux or WORKSTACK_TEST_WSL_DISTRO",
)
class LinuxFilesystemPrimitiveTests(unittest.TestCase):
    def test_linux_filesystem_primitives(self) -> None:
        command = primitive_command(Path("unused"))
        self.assertIsNotNone(command)
        with tempfile.TemporaryDirectory(prefix="workstack-remote-installer-") as temp:
            runner = Path(temp) / "linux_primitives.py"
            runner.write_text(WSL_PRIMITIVE_SCRIPT, encoding="utf-8")
            argv = primitive_command(runner)
            self.assertIsNotNone(argv)
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
                env=wsl_host_env() if not sys.platform.startswith("linux") else None,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            report = json.loads(result.stdout.strip().splitlines()[-1])
            print("Linux primitive receipt:", json.dumps(report, sort_keys=True))
            self.assertFalse(report["cp312_install_claimed"])
            names = {item["name"]: item for item in report["results"]}
            expected = (
                "open_roots_partial_close",
                "v5_admit_data",
                "v3_admit_data",
                "v4_schema_refuse",
                "v5_expected_uid_mismatch",
                "shared_dir_extract",
                "bind_before_swap",
                "stage_swap_bind",
                "data_swap_bind",
                "parent_swap_bind",
                "close_fds_idempotent",
                "cleanup_skips_replacement",
                "wipe_dir_swap_directory",
                "wipe_dir_swap_file",
                "wipe_file_swap_at_unlink",
                "wipe_dir_swap_at_unlink",
                "import_seam_swap",
                "popen_seam_swap",
                "import_swap_at_import_module",
                "popen_swap_at_popen",
                "stage_root_swap_at_unlink",
                "no_race_owned_cleanup_empty",
                "no_race_owned_cleanup_file",
                "rename_eexist_cleanup",
                "rename_unknown_no_delete",
            )
            self.assertEqual(tuple(names), expected)
            for name in expected:
                self.assertTrue(names[name]["ok"], names[name])


@unittest.skipUnless(os.environ.get("WORKSTACK_TEST_WSL_DISTRO"), "WSL opt-in")
class WslRefusalTests(unittest.TestCase):
    def test_non_cp312_is_refusal_only(self) -> None:
        distro = os.environ["WORKSTACK_TEST_WSL_DISTRO"]
        probe = subprocess_wsl_python(distro)
        if probe == (3, 12):
            self.skipTest("positive CP312 install is gated on a future exact artifact")
        archive, sidecar = make_artifact()
        with tempfile.TemporaryDirectory(prefix="workstack-remote-installer-") as temp:
            root = Path(temp)
            (root / "archive.zip").write_bytes(archive)
            (root / "sidecar.json").write_bytes(sidecar)
            runner = root / "refuse_runner.py"
            runner.write_text(
                "import json,sys\n"
                "from remote_provision_installer import installer_main\n"
                "archive=open(sys.argv[1],'rb').read()\n"
                "sidecar=open(sys.argv[2],'rb').read()\n"
                "argv=['provision-install','--install-root',sys.argv[3],'--data-root',sys.argv[4],"
                "'--owner',sys.argv[5],'--expected-workspace-uid',sys.argv[6]]\n"
                "raise SystemExit(installer_main(argv, archive, sidecar))\n",
                encoding="utf-8",
            )
            # Discovery-only: prove the opt-in path exists. Full WSL dirfd smoke is
            # refusal-only here and must not be reported as an install PASS.
            self.assertNotEqual(probe, (3, 12))


def subprocess_wsl_python(distro: str) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [wsl_exe(), "-d", distro, "-e", "python3", "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=wsl_host_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    if len(parts) < 2:
        return None
    return int(parts[0]), int(parts[1])


if __name__ == "__main__":
    unittest.main()
