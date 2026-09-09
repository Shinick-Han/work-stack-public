"""Tests for verified new-directory unpack as a remote install alternative.

Synthetic fixtures only. No live SSOT, company host, release overwrite, or
transactional installer edits. Recomputes real hashes and runs real imports
from planted payload stubs.
"""

from __future__ import annotations

import ast
import errno
import hashlib
import importlib.util
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
SCRIPTS = ROOT / "scripts"
UNPACK_PATH = SHELL / "remote_verified_unpack.py"
INSTALLER_PATH = SHELL / "remote_provision_installer.py"
GENERATOR_PATH = SCRIPTS / "build_linux_verified_unpack.py"
COMMIT = "a" * 40
TREE = "b" * 40
LOCK = "sha256:" + "c" * 64


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INSTALLER = _load(INSTALLER_PATH, "remote_provision_installer")
MODULE = _load(UNPACK_PATH, "remote_verified_unpack")
GENERATOR = _load(GENERATOR_PATH, "build_linux_verified_unpack")

PRODUCT = INSTALLER.PRODUCT
PROTOCOL = INSTALLER.PROTOCOL
ARCHIVE_NAME = INSTALLER.ARCHIVE_NAME


def dump(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def add_zip_member(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, payload)


def frozen_wheels() -> list[dict[str, object]]:
    records = []
    for dist, version in INSTALLER.FROZEN_WHEELS:
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


def payload_blobs(*, workstack_source: bytes | None = None) -> dict[str, bytes]:
    version = (
        workstack_source
        if workstack_source is not None
        else ('__version__ = "%s"\nREMOTE_PROTOCOL_VERSION = %d\n' % (PRODUCT, PROTOCOL)).encode("ascii")
    )
    return {
        "desktop/python-webview-shell/remote_command_contract.py": b"CONTRACT = 1\n",
        "desktop/python-webview-shell/remote_entry.py": b"print('entry')\n",
        "jsonschema/__init__.py": b"__version__ = '4.26.0'\n",
        "rpds/__init__.py": b"__version__ = '0.1'\n",
        "run_work_stack.py": b"print('run')\n",
        "unicodedata2/__init__.py": b"unidata_version = '17.0.0'\n",
        "workstack/__init__.py": version,
    }


def make_artifact(blobs: dict[str, bytes] | None = None) -> tuple[bytes, bytes]:
    files = dict(payload_blobs() if blobs is None else blobs)
    manifest = {
        "entrypoint": "desktop/python-webview-shell/remote_entry.py",
        "files": file_records(files),
        "product_version": PRODUCT,
        "remote_protocol_version": PROTOCOL,
        "requirements_lock_sha256": LOCK,
        "schema_version": 1,
        "source_commit": COMMIT,
        "source_tree": TREE,
        "target": target_object(),
        "wheels": frozen_wheels(),
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
            "archive": {
                "name": ARCHIVE_NAME,
                "sha256": digest_of(archive_bytes),
                "size": len(archive_bytes),
            },
            "artifact_manifest_sha256": digest_of(manifest_bytes),
            "product_version": PRODUCT,
            "remote_protocol_version": PROTOCOL,
            "schema_version": 1,
            "source_commit": COMMIT,
            "target_id": "cp312-manylinux_2_17_x86_64",
        }
    )
    return archive_bytes, sidecar


def no_rename(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("No rename operation may be used by unpack path")


class UnpackSurfaceTests(unittest.TestCase):
    def test_place_signature_has_no_ssot_or_activation(self) -> None:
        names = set(inspect.signature(MODULE.place_verified_unpack).parameters)
        self.assertEqual(names, {"archive_bytes", "sidecar_bytes", "app_dir"})
        source = UNPACK_PATH.read_text(encoding="utf-8")
        self.assertNotIn("commit_noreplace", source)
        self.assertNotIn("renameat2", source)
        self.assertNotIn("workspace.json", source)
        self.assertNotIn("store-meta.json", source)
        self.assertIn("dir_fd", source)
        self.assertIn("O_NOFOLLOW", source)
        self.assertIn("O_DIRECTORY", source)
        self.assertIn("geteuid", source)
        if not sys.platform.startswith("linux"):
            self.assertFalse(MODULE.linux_dirfd_available())
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertNotIn("workstack", [alias.name.split(".")[0] for alias in node.names])
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotEqual(node.module.split(".")[0], "workstack")

    def test_admission_reuses_installer_gate(self) -> None:
        archive, sidecar = make_artifact()
        admitted = MODULE.admit_unpack_bundle(archive, sidecar)
        expected = INSTALLER._admit_artifact(archive, sidecar)
        self.assertEqual(admitted["digest"], expected["digest"])
        self.assertEqual(set(admitted["blobs"]), set(expected["blobs"]))


class VerifiedUnpackPlacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="workstack-unpack-")
        self.base = Path(self.workspace.name)
        self.old_app = self.base / "existing-app"
        self.data = self.base / "existing-data"
        self.old_app.mkdir()
        self.data.mkdir()
        (self.old_app / "sentinel").write_text("old release", encoding="utf-8")
        (self.data / "workspace.json").write_text('{"must":"remain untouched"}', encoding="utf-8")
        self.before = (
            (self.old_app / "sentinel").read_bytes(),
            (self.data / "workspace.json").read_bytes(),
        )

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def assert_existing_preserved(self) -> None:
        self.assertEqual(
            self.before,
            (
                (self.old_app / "sentinel").read_bytes(),
                (self.data / "workspace.json").read_bytes(),
            ),
        )

    def test_positive_unpack_without_rename_verifies_hashes_and_imports(self) -> None:
        archive, sidecar = make_artifact()
        admitted = INSTALLER._admit_artifact(archive, sidecar)
        target = self.base / "new-app"
        with mock.patch.object(os, "rename", no_rename), mock.patch.object(os, "replace", no_rename):
            result = MODULE.place_verified_unpack(
                archive_bytes=archive,
                sidecar_bytes=sidecar,
                app_dir=str(target),
            )
        self.assertEqual(result["outcome"], "unpacked_verified")
        self.assertEqual(result["placement"], "ready_candidate")
        self.assertEqual(result["activation"], "not_activated")
        self.assertFalse(result["ssot_accessed"])
        self.assertFalse(result["atomic_directory_publish"])
        self.assertEqual(result["method"], "verified_unpack")
        self.assertEqual(result["product_version"], PRODUCT)
        self.assertEqual(result["source_commit"], COMMIT)
        self.assertEqual(result["files_verified"], len(admitted["files"]))
        self.assertEqual(result["imports"], "PASS")
        for record in admitted["files"]:
            payload = (target / str(record["path"])).read_bytes()
            self.assertEqual(digest_of(payload), record["sha256"])
            self.assertEqual(len(payload), record["size"])
        receipt = json.loads((target / MODULE.RECEIPT_NAME).read_text(encoding="utf-8"))
        carried = (target / MODULE.ARTIFACT_MANIFEST_NAME).read_bytes()
        self.assertEqual(digest_of(carried), receipt["artifact_manifest_sha256"])
        self.assertEqual(digest_of(carried), admitted["manifest_digest"])
        self.assertEqual(receipt["activation"], "not_activated")
        self.assertEqual(receipt["placement"], "ready_candidate")
        self.assert_existing_preserved()
        inspected = MODULE.inspect_unpack_target(str(target))
        self.assertEqual(inspected["placement"], "ready_candidate")
        verified = MODULE.verify_unpack_identity(str(target), archive, sidecar)
        self.assertEqual(verified["placement"], "identity_verified")
        self.assertEqual(verified["activation"], "not_activated")

    def test_default_unpack_does_not_write_under_home(self) -> None:
        home = self.base / "posix-home"
        home.mkdir()
        marker = home / "keep-home.txt"
        marker.write_bytes(b"home-untouched\n")
        archive, sidecar = make_artifact()
        target = self.base / "new-app"
        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
            result = MODULE.place_verified_unpack(
                archive_bytes=archive,
                sidecar_bytes=sidecar,
                app_dir=str(target),
            )
        self.assertEqual(result["outcome"], "unpacked_verified")
        self.assertEqual(marker.read_bytes(), b"home-untouched\n")
        self.assertFalse((home / ".agents").exists())
        self.assertFalse((home / ".agents" / "skills" / "work-stack").exists())
        self.assert_existing_preserved()

    def test_existing_target_is_refused_and_left_untouched(self) -> None:
        archive, sidecar = make_artifact()
        target = self.base / "occupied"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        result = MODULE.place_verified_unpack(
            archive_bytes=archive,
            sidecar_bytes=sidecar,
            app_dir=str(target),
        )
        self.assertEqual(result["outcome"], "not_ready")
        self.assertEqual(result["code"], "APP_DIRECTORY_ALREADY_EXISTS")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertFalse((target / MODULE.RECEIPT_NAME).exists())
        self.assert_existing_preserved()

    def test_bad_bundle_is_refused_before_write(self) -> None:
        archive, sidecar = make_artifact()
        target = self.base / "must-not-exist"
        broken = archive[:-1] + b"X"
        result = MODULE.place_verified_unpack(
            archive_bytes=broken,
            sidecar_bytes=sidecar,
            app_dir=str(target),
        )
        self.assertEqual(result["outcome"], "not_ready")
        self.assertEqual(result["code"], "REMOTE_ARTIFACT_INVALID")
        self.assertEqual(result["placement"], "absent")
        self.assertFalse(target.exists())
        self.assert_existing_preserved()

    def test_interrupted_write_has_no_completion_receipt(self) -> None:
        archive, sidecar = make_artifact()
        target = self.base / "incomplete"
        attempts = [0]
        writer_name = "write_new_at" if MODULE.linux_dirfd_available() else "write_new"
        original = getattr(MODULE, writer_name)

        def fail_write(*args: object, **kwargs: object) -> None:
            attempts[0] += 1
            if attempts[0] == 3:
                raise OSError(28, "synthetic full disk")
            return original(*args, **kwargs)

        with mock.patch.object(MODULE, writer_name, fail_write):
            result = MODULE.place_verified_unpack(
                archive_bytes=archive,
                sidecar_bytes=sidecar,
                app_dir=str(target),
            )
        self.assertEqual(result["outcome"], "not_ready")
        self.assertEqual(result["code"], "FILESYSTEM_ERROR_ERRNO_28")
        self.assertTrue(target.exists())
        self.assertFalse((target / MODULE.RECEIPT_NAME).exists())
        inspected = MODULE.inspect_unpack_target(str(target))
        self.assertEqual(inspected["placement"], "interrupted_placement")
        self.assertNotEqual(inspected.get("placement"), "ready_candidate")
        self.assert_existing_preserved()

    def test_failed_imports_have_no_completion_receipt(self) -> None:
        blobs = payload_blobs(workstack_source=b'__version__ = "0.0.0"\nREMOTE_PROTOCOL_VERSION = 1\n')
        archive, sidecar = make_artifact(blobs)
        target = self.base / "bad-import"
        result = MODULE.place_verified_unpack(
            archive_bytes=archive,
            sidecar_bytes=sidecar,
            app_dir=str(target),
        )
        self.assertEqual(result["outcome"], "not_ready")
        self.assertEqual(result["code"], "PYTHON_IMPORT_CHECK_FAILED")
        self.assertTrue(target.exists())
        self.assertFalse((target / MODULE.RECEIPT_NAME).exists())
        inspected = MODULE.inspect_unpack_target(str(target))
        self.assertEqual(inspected["placement"], "interrupted_placement")
        self.assert_existing_preserved()

    def test_tampered_ready_candidate_fails_identity_verification(self) -> None:
        archive, sidecar = make_artifact()
        target = self.base / "tamper"
        placed = MODULE.place_verified_unpack(
            archive_bytes=archive,
            sidecar_bytes=sidecar,
            app_dir=str(target),
        )
        self.assertEqual(placed["outcome"], "unpacked_verified")
        payload = target / "run_work_stack.py"
        payload.write_bytes(payload.read_bytes() + b"#tamper\n")
        verified = MODULE.verify_unpack_identity(str(target), archive, sidecar)
        self.assertEqual(verified["outcome"], "not_ready")
        self.assertEqual(verified["code"], "UNPACKED_FILE_MISMATCH")
        self.assertEqual(verified["placement"], "ready_candidate")
        self.assertEqual(verified["activation"], "not_activated")

    def test_inspect_absent_target(self) -> None:
        inspected = MODULE.inspect_unpack_target(str(self.base / "missing-app"))
        self.assertEqual(inspected["placement"], "absent")
        self.assertEqual(inspected["activation"], "not_activated")

    def test_pre_root_create_failure_observes_absent_or_unknown(self) -> None:
        archive, sidecar = make_artifact()
        target = self.base / "cannot-create"
        with mock.patch.object(MODULE, "_place_into", side_effect=PermissionError(errno.EACCES, "refused")):
            result = MODULE.place_verified_unpack(archive_bytes=archive, sidecar_bytes=sidecar, app_dir=str(target))
        self.assertEqual(result["placement"], "absent")
        self.assertFalse(target.exists())
        with mock.patch.object(MODULE, "_place_into", side_effect=OSError(errno.ESTALE, "ambiguous")), \
             mock.patch.object(MODULE, "_lstat_path", side_effect=OSError(errno.ESTALE, "unreadable")):
            result = MODULE.place_verified_unpack(archive_bytes=archive, sidecar_bytes=sidecar, app_dir=str(target))
        self.assertEqual(result["placement"], "unknown")
        self.assertEqual(result["activation"], "not_activated")

    def test_verify_unreadable_payload_is_unknown_not_mismatch(self) -> None:
        archive, sidecar = make_artifact()
        target = self.base / "verify-unreadable"
        placed = MODULE.place_verified_unpack(archive_bytes=archive, sidecar_bytes=sidecar, app_dir=str(target))
        self.assertEqual(placed["outcome"], "unpacked_verified")
        original = Path.read_bytes
        for number in (errno.EACCES, errno.ESTALE):
            def read(path):
                if path.name == "run_work_stack.py":
                    raise OSError(number, "synthetic unreadable payload")
                return original(path)
            with self.subTest(errno=number), mock.patch.object(Path, "read_bytes", read):
                result = MODULE.verify_unpack_identity(str(target), archive, sidecar)
                self.assertEqual(result["placement"], "unknown")
                self.assertEqual(result["code"], "FILESYSTEM_ERROR_ERRNO_" + str(number))
                self.assertEqual(result["activation"], "not_activated")

    def test_placement_time_file_exists_reconciles_the_target(self) -> None:
        archive, sidecar = make_artifact()
        for observation in ("present", "absent", "unknown"):
            target = self.base / ("raced-" + observation)
            def race(root, *_args):
                if observation == "present":
                    root.mkdir()
                raise FileExistsError(errno.EEXIST, "synthetic creation race")
            with self.subTest(observation=observation), mock.patch.object(MODULE, "_place_into", race):
                if observation == "unknown":
                    with mock.patch.object(MODULE, "_lstat_path", side_effect=OSError(errno.ESTALE, "unreadable")):
                        result = MODULE.place_verified_unpack(archive_bytes=archive, sidecar_bytes=sidecar, app_dir=str(target))
                else:
                    result = MODULE.place_verified_unpack(archive_bytes=archive, sidecar_bytes=sidecar, app_dir=str(target))
                expected = "interrupted_placement" if observation == "present" else observation
                self.assertEqual(result["placement"], expected)
                self.assertEqual(result["activation"], "not_activated")

    def test_cli_positive_path(self) -> None:
        archive, sidecar = make_artifact()
        archive_path = self.base / "bundle.zip"
        sidecar_path = self.base / "bundle.json"
        archive_path.write_bytes(archive)
        sidecar_path.write_bytes(sidecar)
        target = self.base / "cli-app"
        with mock.patch.object(sys, "stdout", io.StringIO()) as stdout:
            code = MODULE.unpack_main(
                ["--archive", str(archive_path), "--sidecar", str(sidecar_path), "--app-dir", str(target)]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["outcome"], "unpacked_verified")
        self.assertTrue((target / MODULE.RECEIPT_NAME).is_file())
        self.assert_existing_preserved()

    def test_unreadable_receipt_is_unknown_not_interrupted(self) -> None:
        archive, sidecar = make_artifact()
        target = self.base / "unreadable-receipt"
        placed = MODULE.place_verified_unpack(
            archive_bytes=archive,
            sidecar_bytes=sidecar,
            app_dir=str(target),
        )
        self.assertEqual(placed["outcome"], "unpacked_verified")
        original = Path.read_bytes
        numbers = [errno.EACCES]
        stale = getattr(errno, "ESTALE", 116)
        if stale not in numbers:
            numbers.append(stale)
        for number in numbers:
            def fail_receipt(self: Path, _number: int = number) -> bytes:
                if self.name == MODULE.RECEIPT_NAME:
                    raise OSError(_number, "synthetic receipt io")
                return original(self)

            with mock.patch.object(Path, "read_bytes", fail_receipt):
                inspected = MODULE.inspect_unpack_target(str(target))
            self.assertEqual(inspected["outcome"], "not_ready")
            self.assertEqual(inspected["placement"], "unknown")
            self.assertEqual(inspected["activation"], "not_activated")
            self.assertFalse(inspected["ssot_accessed"])
            self.assertEqual(inspected["code"], "FILESYSTEM_ERROR_ERRNO_" + str(number))
            self.assertNotEqual(inspected["placement"], "interrupted_placement")

    def test_unreadable_target_path_is_unknown(self) -> None:
        archive, sidecar = make_artifact()
        target = self.base / "blocked-app"
        original = os.lstat

        def fail_target(path: object, *args: object, **kwargs: object) -> os.stat_result:
            candidate = Path(os.fspath(path))
            if candidate.name == target.name and candidate.parent.name == target.parent.name:
                raise OSError(errno.EACCES, "synthetic path io")
            return original(path, *args, **kwargs)

        with mock.patch.object(os, "lstat", fail_target):
            inspected = MODULE.inspect_unpack_target(str(target))
            placed = MODULE.place_verified_unpack(
                archive_bytes=archive,
                sidecar_bytes=sidecar,
                app_dir=str(target),
            )
        self.assertEqual(inspected["outcome"], "not_ready")
        self.assertEqual(inspected["placement"], "unknown")
        self.assertEqual(inspected["activation"], "not_activated")
        self.assertEqual(inspected["code"], "FILESYSTEM_ERROR_ERRNO_" + str(errno.EACCES))
        self.assertEqual(placed["outcome"], "not_ready")
        self.assertEqual(placed["placement"], "unknown")
        self.assertEqual(placed["activation"], "not_activated")
        self.assertFalse(target.exists())
        self.assert_existing_preserved()


@unittest.skipUnless(sys.platform.startswith("linux"), "native Linux dirfd placement")
class LinuxDirfdPlacementTests(unittest.TestCase):
    def test_linux_production_backend_is_dirfd(self) -> None:
        self.assertTrue(MODULE.linux_dirfd_available())

    def test_dirfd_unpack_without_rename(self) -> None:
        workspace = tempfile.TemporaryDirectory(prefix="workstack-unpack-linux-")
        try:
            base = Path(workspace.name)
            archive, sidecar = make_artifact()
            target = base / "new-app"
            with mock.patch.object(os, "rename", no_rename), mock.patch.object(os, "replace", no_rename):
                result = MODULE.place_verified_unpack(
                    archive_bytes=archive,
                    sidecar_bytes=sidecar,
                    app_dir=str(target),
                )
            self.assertEqual(result["outcome"], "unpacked_verified")
            self.assertTrue((target / MODULE.RECEIPT_NAME).is_file())
            verified = MODULE.verify_unpack_identity(str(target), archive, sidecar)
            self.assertEqual(verified["placement"], "identity_verified")
        finally:
            workspace.cleanup()


EMBEDDED_MODULE_NAMES = (
    "remote_provision_installer_linux",
    "remote_provision_installer",
    "remote_verified_unpack",
)


class UnpackGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="workstack-unpack-gen-")
        self.base = Path(self.workspace.name)
        self._saved_modules = {name: sys.modules.get(name) for name in EMBEDDED_MODULE_NAMES}

    def tearDown(self) -> None:
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        self.workspace.cleanup()

    def write_helper(self, archive: bytes, sidecar: bytes, name: str = "generated-unpack.py") -> Path:
        output = self.base / name
        GENERATOR.write_verified_unpack_script(archive, sidecar, output)
        return output

    def run_helper(self, script: Path, args: list[str]) -> tuple[int, dict[str, object]]:
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", str(script), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        return completed.returncode, json.loads(completed.stdout)

    def load_embedded(self, script: Path):
        helper = _load(script, "generated_linux_verified_unpack")
        return helper, helper.load_engine()

    def test_generated_script_carries_source_dist_release_identity(self) -> None:
        archive, sidecar = make_artifact()
        source, identity = GENERATOR.render_verified_unpack_script(archive, sidecar)
        compile(source, "<generated-unpack>", "exec")
        self.assertEqual(identity["product_version"], PRODUCT)
        self.assertEqual(identity["remote_protocol_version"], PROTOCOL)
        self.assertEqual(identity["source_commit"], COMMIT)
        self.assertEqual(identity["archive_name"], ARCHIVE_NAME)
        self.assertIn(PRODUCT, source)
        self.assertIn(COMMIT, source)
        self.assertIn(ARCHIVE_NAME, source)
        self.assertIn(identity["archive_sha256"], source)
        self.assertIn("place_verified_unpack", source)
        self.assertIn("inspect_unpack_target", source)
        self.assertIn("verify_unpack_identity", source)
        self.assertIn("remote_verified_unpack", source)
        output = self.base / ("Unpack-WorkStack-Linux-%s.py" % PRODUCT)
        written = GENERATOR.write_verified_unpack_script(archive, sidecar, output)
        self.assertEqual(written["source_commit"], COMMIT)
        self.assertTrue(output.is_file())
        _helper, unpack = self.load_embedded(output)
        admitted = unpack.admit_unpack_bundle(archive, sidecar)
        self.assertEqual(admitted["digest"], INSTALLER._admit_artifact(archive, sidecar)["digest"])
        self.assertTrue(callable(unpack.place_verified_unpack))
        self.assertTrue(callable(unpack.inspect_unpack_target))
        self.assertTrue(callable(unpack.verify_unpack_identity))
        self.assertTrue(callable(unpack.write_new_at))

    def test_generator_refuses_frozen_1_0_13_release_tree(self) -> None:
        archive, sidecar = make_artifact()
        frozen = self.base / "workstack-linux-1.0.13-distribution"
        frozen.mkdir()
        target = frozen / "Unpack-WorkStack-Linux-1.0.13.py"
        with self.assertRaises(GENERATOR.UnpackBuildError) as raised:
            GENERATOR.write_verified_unpack_script(archive, sidecar, target)
        self.assertEqual(raised.exception.code, "FROZEN_RELEASE_ASSET")
        self.assertFalse(target.exists())

    def test_generated_helper_place_inspect_verify_positive_path(self) -> None:
        archive, sidecar = make_artifact()
        script = self.write_helper(archive, sidecar)
        archive_path = self.base / "bundle.zip"
        sidecar_path = self.base / "bundle.json"
        archive_path.write_bytes(archive)
        sidecar_path.write_bytes(sidecar)
        target = self.base / "new-app"
        code, placed = self.run_helper(
            script,
            ["place", "--archive", str(archive_path), "--sidecar", str(sidecar_path), "--app-dir", str(target)],
        )
        self.assertEqual(code, 0)
        self.assertEqual(placed["outcome"], "unpacked_verified")
        self.assertEqual(placed["placement"], "ready_candidate")
        self.assertEqual(placed["activation"], "not_activated")
        self.assertFalse(placed["ssot_accessed"])
        inspect_code, inspected = self.run_helper(script, ["inspect", "--app-dir", str(target)])
        self.assertEqual(inspect_code, 0)
        self.assertEqual(inspected["placement"], "ready_candidate")
        verify_code, verified = self.run_helper(
            script,
            ["verify", "--archive", str(archive_path), "--sidecar", str(sidecar_path), "--app-dir", str(target)],
        )
        self.assertEqual(verify_code, 0)
        self.assertEqual(verified["placement"], "identity_verified")
        self.assertEqual(verified["activation"], "not_activated")

    def test_generated_helper_bad_bundle_is_absent(self) -> None:
        archive, sidecar = make_artifact()
        script = self.write_helper(archive, sidecar)
        archive_path = self.base / "bundle.zip"
        sidecar_path = self.base / "bundle.json"
        archive_path.write_bytes(archive[:-1] + b"X")
        sidecar_path.write_bytes(sidecar)
        target = self.base / "must-not-exist"
        code, result = self.run_helper(
            script,
            ["place", "--archive", str(archive_path), "--sidecar", str(sidecar_path), "--app-dir", str(target)],
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["outcome"], "not_ready")
        self.assertEqual(result["placement"], "absent")
        self.assertEqual(result["code"], "RELEASE_FILE_MISMATCH")
        self.assertFalse(target.exists())
        inspect_code, inspected = self.run_helper(script, ["inspect", "--app-dir", str(target)])
        self.assertEqual(inspect_code, 2)
        self.assertEqual(inspected["placement"], "absent")
        _helper, unpack = self.load_embedded(script)
        admitted_result = unpack.place_verified_unpack(
            archive_bytes=archive[:-1] + b"X",
            sidecar_bytes=sidecar,
            app_dir=str(self.base / "admit-must-not-exist"),
        )
        self.assertEqual(admitted_result["code"], "REMOTE_ARTIFACT_INVALID")
        self.assertEqual(admitted_result["placement"], "absent")
        self.assertFalse((self.base / "admit-must-not-exist").exists())

    def test_generated_helper_existing_target_and_failed_import(self) -> None:
        archive, sidecar = make_artifact()
        script = self.write_helper(archive, sidecar)
        archive_path = self.base / "bundle.zip"
        sidecar_path = self.base / "bundle.json"
        archive_path.write_bytes(archive)
        sidecar_path.write_bytes(sidecar)
        occupied = self.base / "occupied"
        occupied.mkdir()
        marker = occupied / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        code, result = self.run_helper(
            script,
            ["place", "--archive", str(archive_path), "--sidecar", str(sidecar_path), "--app-dir", str(occupied)],
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["outcome"], "not_ready")
        self.assertEqual(result["code"], "APP_DIRECTORY_ALREADY_EXISTS")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        inspect_code, inspected = self.run_helper(script, ["inspect", "--app-dir", str(occupied)])
        self.assertEqual(inspect_code, 2)
        self.assertEqual(inspected["placement"], "interrupted_placement")

        blobs = payload_blobs(workstack_source=b'__version__ = "0.0.0"\nREMOTE_PROTOCOL_VERSION = 1\n')
        bad_archive, bad_sidecar = make_artifact(blobs)
        bad_script = self.write_helper(bad_archive, bad_sidecar, "generated-unpack-import.py")
        bad_archive_path = self.base / "bad.zip"
        bad_sidecar_path = self.base / "bad.json"
        bad_archive_path.write_bytes(bad_archive)
        bad_sidecar_path.write_bytes(bad_sidecar)
        incomplete = self.base / "bad-import"
        import_code, import_result = self.run_helper(
            bad_script,
            [
                "place",
                "--archive", str(bad_archive_path),
                "--sidecar", str(bad_sidecar_path),
                "--app-dir", str(incomplete),
            ],
        )
        self.assertEqual(import_code, 2)
        self.assertEqual(import_result["outcome"], "not_ready")
        self.assertEqual(import_result["code"], "PYTHON_IMPORT_CHECK_FAILED")
        self.assertEqual(import_result["placement"], "interrupted_placement")
        self.assertTrue(incomplete.exists())
        self.assertFalse((incomplete / MODULE.RECEIPT_NAME).exists())
        inspect_code, inspected = self.run_helper(bad_script, ["inspect", "--app-dir", str(incomplete)])
        self.assertEqual(inspect_code, 2)
        self.assertEqual(inspected["placement"], "interrupted_placement")

    def test_generated_helper_interrupted_write_is_partial(self) -> None:
        archive, sidecar = make_artifact()
        script = self.write_helper(archive, sidecar)
        helper, unpack = self.load_embedded(script)
        target = self.base / "incomplete"
        archive_path, sidecar_path = self.base / "fixture.zip", self.base / "fixture.json"
        archive_path.write_bytes(archive)
        sidecar_path.write_bytes(sidecar)
        attempts = [0]
        writer_name = "write_new_at" if unpack.linux_dirfd_available() else "write_new"
        original = getattr(unpack, writer_name)

        def fail_write(*args: object, **kwargs: object) -> None:
            attempts[0] += 1
            if attempts[0] == 3:
                raise OSError(28, "synthetic full disk")
            return original(*args, **kwargs)

        with mock.patch.object(unpack, writer_name, fail_write), \
             mock.patch.object(helper, "load_engine", return_value=unpack), \
             mock.patch.object(sys, "stdout", io.StringIO()) as stdout:
            exit_code = helper.main(["place", "--archive", str(archive_path),
                                     "--sidecar", str(sidecar_path), "--app-dir", str(target)])
        self.assertEqual(exit_code, 2)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["outcome"], "not_ready")
        self.assertEqual(result["code"], "FILESYSTEM_ERROR_ERRNO_28")
        self.assertEqual(result["placement"], "interrupted_placement")
        self.assertTrue(target.exists())
        self.assertFalse((target / unpack.RECEIPT_NAME).exists())
        inspected = unpack.inspect_unpack_target(str(target))
        self.assertEqual(inspected["placement"], "interrupted_placement")
        code, cli_inspected = self.run_helper(script, ["inspect", "--app-dir", str(target)])
        self.assertEqual(code, 2)
        self.assertEqual(cli_inspected["placement"], "interrupted_placement")


if __name__ == "__main__":
    unittest.main()
