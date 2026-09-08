"""Contract tests for the remote installer stdin payload builder.

The transport tests execute the actual generated source with
``[sys.executable, "-I", "-B", "-", ...]``. They exercise the real installer's
refusal path only: a refusal proves that both fixed modules loaded under their
required identities and that the admission engine ran, and it is never evidence
that a Windows or Linux install would succeed.
"""

from __future__ import annotations

import ast
import base64
import builtins
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

PAYLOAD_PATH = SHELL / "remote_provision_payload.py"
LEAF_PATH = SHELL / "remote_provision_installer_linux.py"
ADMISSION_PATH = SHELL / "remote_provision_installer.py"

PAYLOAD_SPEC = importlib.util.spec_from_file_location(
    "remote_provision_payload_test", PAYLOAD_PATH
)
assert PAYLOAD_SPEC is not None and PAYLOAD_SPEC.loader is not None
MODULE = importlib.util.module_from_spec(PAYLOAD_SPEC)
sys.modules[PAYLOAD_SPEC.name] = MODULE
PAYLOAD_SPEC.loader.exec_module(MODULE)

INSTALLER_SPEC = importlib.util.spec_from_file_location(
    "remote_provision_installer_payload_comp", ADMISSION_PATH
)
assert INSTALLER_SPEC is not None and INSTALLER_SPEC.loader is not None

TRANSPORT_TIMEOUT_SECONDS = 60.0
CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

POSIX_INSTALL = "/workstack-fixture/payload-owner/app"
POSIX_DATA = "/workstack-fixture/payload-owner/data"
OWNER = "payload_owner"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
INSTALL_ARGV = (
    "provision-install",
    "--install-root",
    POSIX_INSTALL,
    "--data-root",
    POSIX_DATA,
    "--owner",
    OWNER,
    "--expected-workspace-uid",
    WORKSPACE_ID,
)

# Quotes, a newline, a NUL, a backslash, a triple quote, a literal-closing
# paren, and code shaped to write a marker file if it were ever interpolated
# into the generated program instead of base64 encoded.
CANARY_MARKER = "workstack-payload-canary.txt"
CANARY = (
    b'"""\n\'\'\'\n\\\n\x00\n)\n'
    b"import os\n"
    b'open("' + CANARY_MARKER.encode("ascii") + b'", "w").write("breached")\n'
    b"raise SystemExit(0)\n#"
)
VALID_SIDECAR = b'{"schema_version": 1}'


def _tree(root: str) -> set[str]:
    entries: set[str] = set()
    for base, directories, files in os.walk(root):
        for name in list(directories) + list(files):
            entries.add(os.path.relpath(os.path.join(base, name), root))
    return entries


def _disposable_env() -> dict[str, str]:
    """Minimal environment: only what the interpreter needs to start."""

    env = {"PATH": ""}
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        env["SYSTEMROOT"] = system_root
    return env


def _run_payload(source: bytes, cwd: str) -> subprocess.CompletedProcess[bytes]:
    command = [sys.executable, "-I", "-B", "-", *INSTALL_ARGV]
    return subprocess.run(
        command,
        input=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=TRANSPORT_TIMEOUT_SECONDS,
        cwd=cwd,
        env=_disposable_env(),
        creationflags=CREATION_FLAGS,
        check=False,
    )


def _assign(source: bytes, name: str) -> str:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                assert isinstance(node.value, ast.Constant)
                assert isinstance(node.value.value, str)
                return node.value.value
    raise AssertionError("generated source has no %s assignment" % name)


def _installer_module_names() -> tuple[str, str]:
    return (MODULE.LEAF_MODULE_NAME, MODULE.ADMISSION_MODULE_NAME)


def _restore_installer_modules(
    names: tuple[str, ...], saved: dict[str, types.ModuleType]
) -> None:
    for name in names:
        sys.modules.pop(name, None)
    sys.modules.update(saved)


class PayloadBuilderInputTests(unittest.TestCase):
    def test_import_is_effect_free(self) -> None:
        # Earlier installer tests in the same interpreter may already have
        # loaded the canonical names. Absence from sys.modules is not the
        # contract: import must not read product files, add installer
        # modules, or replace/remove whatever objects were already there.
        names = _installer_module_names()
        saved = {name: sys.modules[name] for name in names if name in sys.modules}
        compiled = compile(PAYLOAD_PATH.read_bytes(), str(PAYLOAD_PATH), "exec")

        def refuse(*args: object, **kwargs: object) -> object:
            raise AssertionError("import performed a filesystem read")

        def run_import() -> None:
            namespace: dict[str, object] = {
                "__file__": str(PAYLOAD_PATH),
                "__name__": "remote_provision_payload_effect_free",
            }
            with mock.patch.object(builtins, "open", refuse):
                with mock.patch.object(io, "open_code", refuse):
                    exec(compiled, namespace)
            self.assertIn("build_installer_payload_source", namespace)

        try:
            with self.subTest(modules="preexisting"):
                before = {name: sys.modules.get(name) for name in names}
                run_import()
                for name, module in before.items():
                    if module is None:
                        self.assertNotIn(name, sys.modules)
                    else:
                        self.assertIs(sys.modules.get(name), module)

            with self.subTest(modules="preloaded"):
                sentinels = {name: types.ModuleType("preloaded_" + name) for name in names}
                sys.modules.update(sentinels)
                try:
                    run_import()
                    for name, sentinel in sentinels.items():
                        self.assertIn(name, sys.modules)
                        self.assertIs(sys.modules[name], sentinel)
                finally:
                    _restore_installer_modules(names, saved)

            with self.subTest(modules="absent"):
                for name in names:
                    sys.modules.pop(name, None)
                try:
                    run_import()
                    for name in names:
                        self.assertNotIn(name, sys.modules)
                finally:
                    _restore_installer_modules(names, saved)
        finally:
            _restore_installer_modules(names, saved)

    def test_wrong_typed_inputs_refused_before_sibling_reads(self) -> None:
        rejected: list[object] = ["bytes", bytearray(b"x"), memoryview(b"x"), None, 7]
        with mock.patch.object(
            MODULE, "_read_module_source", side_effect=AssertionError("sibling read")
        ) as reader:
            for value in rejected:
                with self.subTest(value=type(value).__name__):
                    with self.assertRaises(MODULE.PayloadError) as caught:
                        MODULE.build_installer_payload_source(
                            archive_bytes=value, sidecar_bytes=VALID_SIDECAR
                        )
                    self.assertEqual(caught.exception.code, MODULE.INVALID_INPUT)
                    with self.assertRaises(MODULE.PayloadError) as caught:
                        MODULE.build_installer_payload_source(
                            archive_bytes=b"zip", sidecar_bytes=value
                        )
                    self.assertEqual(caught.exception.code, MODULE.INVALID_INPUT)
            reader.assert_not_called()

    def test_oversized_inputs_refused_before_sibling_reads(self) -> None:
        oversized_archive = bytes(MODULE.MAX_ARCHIVE_BYTES + 1)
        oversized_sidecar = bytes(MODULE.MAX_SIDECAR_BYTES + 1)
        with mock.patch.object(
            MODULE, "_read_module_source", side_effect=AssertionError("sibling read")
        ) as reader:
            with self.assertRaises(MODULE.PayloadError) as caught:
                MODULE.build_installer_payload_source(
                    archive_bytes=oversized_archive, sidecar_bytes=VALID_SIDECAR
                )
            self.assertEqual(caught.exception.code, MODULE.INVALID_INPUT)
            with self.assertRaises(MODULE.PayloadError) as caught:
                MODULE.build_installer_payload_source(
                    archive_bytes=b"zip", sidecar_bytes=oversized_sidecar
                )
            self.assertEqual(caught.exception.code, MODULE.INVALID_INPUT)
            reader.assert_not_called()

    def test_builder_checks_size_and_type_only(self) -> None:
        """Empty and junk bytes build; admission stays with the installer."""

        source = MODULE.build_installer_payload_source(archive_bytes=b"", sidecar_bytes=b"")
        self.assertTrue(source.startswith(b"import base64"))
        self.assertIn(b'_ARCHIVE_B64 = (\n""\n)', source)

    def test_missing_module_bytes_are_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            with mock.patch.object(MODULE, "_SHELL_DIR", empty):
                with self.assertRaises(MODULE.PayloadError) as caught:
                    MODULE.build_installer_payload_source(
                        archive_bytes=b"zip", sidecar_bytes=VALID_SIDECAR
                    )
        self.assertEqual(caught.exception.code, MODULE.SOURCE_UNAVAILABLE)
        self._assert_sanitized(caught.exception)

    def test_oversized_module_bytes_are_sanitized(self) -> None:
        with mock.patch.object(MODULE, "MAX_MODULE_BYTES", 64):
            with self.assertRaises(MODULE.PayloadError) as caught:
                MODULE.build_installer_payload_source(
                    archive_bytes=b"zip", sidecar_bytes=VALID_SIDECAR
                )
        self.assertEqual(caught.exception.code, MODULE.SOURCE_UNAVAILABLE)
        self._assert_sanitized(caught.exception)

    def test_generated_source_bound_is_enforced(self) -> None:
        with mock.patch.object(MODULE, "MAX_PAYLOAD_BYTES", 128):
            with self.assertRaises(MODULE.PayloadError) as caught:
                MODULE.build_installer_payload_source(
                    archive_bytes=b"zip", sidecar_bytes=VALID_SIDECAR
                )
        self.assertEqual(caught.exception.code, MODULE.PAYLOAD_TOO_LARGE)
        self._assert_sanitized(caught.exception)

    def _assert_sanitized(self, error: Exception) -> None:
        text = str(error)
        self.assertLessEqual(len(text), 200)
        for leak in (
            os.sep,
            "/",
            "remote_provision_installer",
            "Traceback",
            "Errno",
            "import ",
            str(SHELL),
        ):
            self.assertNotIn(leak, text)
        # No underlying exception is reachable through the raised error.
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__context__ is None or error.__suppress_context__)


class PayloadBoundTests(unittest.TestCase):
    def test_caps_match_the_installer_engine(self) -> None:
        installer = importlib.util.module_from_spec(INSTALLER_SPEC)
        leaf_spec = importlib.util.spec_from_file_location(
            MODULE.LEAF_MODULE_NAME, LEAF_PATH
        )
        assert leaf_spec is not None and leaf_spec.loader is not None
        leaf = importlib.util.module_from_spec(leaf_spec)
        sys.modules[MODULE.LEAF_MODULE_NAME] = leaf
        try:
            leaf_spec.loader.exec_module(leaf)
            INSTALLER_SPEC.loader.exec_module(installer)
            self.assertEqual(MODULE.MAX_ARCHIVE_BYTES, installer.MAX_ARCHIVE)
            self.assertEqual(MODULE.MAX_SIDECAR_BYTES, installer.MAX_SIDECAR)
            self.assertTrue(callable(installer.installer_main))
        finally:
            sys.modules.pop(MODULE.LEAF_MODULE_NAME, None)

    def test_payload_bound_is_derived_from_constants(self) -> None:
        expected = (
            MODULE.BOOTSTRAP_BUDGET_BYTES
            + MODULE._literal_bound(MODULE.MAX_MODULE_BYTES) * 2
            + MODULE._literal_bound(MODULE.MAX_ARCHIVE_BYTES)
            + MODULE._literal_bound(MODULE.MAX_SIDECAR_BYTES)
        )
        self.assertEqual(MODULE.MAX_PAYLOAD_BYTES, expected)

    def test_bootstrap_fits_its_budget(self) -> None:
        source = MODULE.build_installer_payload_source(archive_bytes=b"", sidecar_bytes=b"")
        literals = sum(
            MODULE._literal_bound(len(payload))
            for payload in (LEAF_PATH.read_bytes(), ADMISSION_PATH.read_bytes(), b"", b"")
        )
        self.assertLessEqual(len(source) - literals, MODULE.BOOTSTRAP_BUDGET_BYTES)
        self.assertLess(len(source), MODULE.MAX_PAYLOAD_BYTES)

    def test_base64_inflates_by_roughly_four_thirds(self) -> None:
        """The documented trade-off: transfer grows, it is not streamed."""

        raw = len(LEAF_PATH.read_bytes()) + len(ADMISSION_PATH.read_bytes()) + len(CANARY)
        source = MODULE.build_installer_payload_source(
            archive_bytes=CANARY, sidecar_bytes=b""
        )
        self.assertGreater(len(source), raw * 4 // 3)
        self.assertLess(len(source), raw * 4 // 3 + MODULE.BOOTSTRAP_BUDGET_BYTES + 4096)


class GeneratedSourceShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = MODULE.build_installer_payload_source(
            archive_bytes=CANARY, sidecar_bytes=VALID_SIDECAR
        )

    def test_source_is_ascii_and_carries_no_raw_binary(self) -> None:
        self.source.decode("ascii")
        self.assertNotIn(b"\x00", self.source)
        self.assertNotIn(b"\r", self.source)
        for fragment in (
            b'"""',
            b"'''",
            CANARY_MARKER.encode("ascii"),
            b'open("',
            b"raise SystemExit",
        ):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, self.source)
        # The only backslash in the program is the fixed refusal line's escape.
        self.assertEqual(self.source.count(b"\\"), 1)

    def test_every_literal_line_is_base64_only(self) -> None:
        for line in self.source.decode("ascii").splitlines():
            if not line.startswith('"'):
                continue
            self.assertTrue(line.endswith('"'), line[:32])
            self.assertIsNotNone(MODULE._BASE64_RE.fullmatch(line[1:-1]))

    def test_embedded_module_sources_are_the_exact_checked_in_files(self) -> None:
        self.assertEqual(
            base64.b64decode(_assign(self.source, "_LEAF_B64")), LEAF_PATH.read_bytes()
        )
        self.assertEqual(
            base64.b64decode(_assign(self.source, "_MAIN_B64")), ADMISSION_PATH.read_bytes()
        )
        self.assertEqual(base64.b64decode(_assign(self.source, "_ARCHIVE_B64")), CANARY)
        self.assertEqual(
            base64.b64decode(_assign(self.source, "_SIDECAR_B64")), VALID_SIDECAR
        )

    def test_import_namespace_is_deterministic(self) -> None:
        tree = ast.parse(self.source)
        self.assertEqual(_assign(self.source, "_LEAF_NAME"), MODULE.LEAF_MODULE_NAME)
        self.assertEqual(_assign(self.source, "_MAIN_NAME"), MODULE.ADMISSION_MODULE_NAME)
        loaded = [
            node.args[0].id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_load"
            and isinstance(node.args[0], ast.Name)
        ]
        self.assertEqual(loaded, ["_LEAF_NAME", "_MAIN_NAME"])
        registrations = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Subscript)
        ]
        self.assertEqual(len(registrations), 1)
        self.assertEqual(registrations[0].value.id, "module")

    def test_top_level_imports_are_fixed(self) -> None:
        tree = ast.parse(self.source)
        names = sorted(
            alias.name
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertEqual(names, ["base64", "sys", "types"])

    def test_repeated_builds_of_the_same_bytes_are_identical(self) -> None:
        again = MODULE.build_installer_payload_source(
            archive_bytes=CANARY, sidecar_bytes=VALID_SIDECAR
        )
        self.assertEqual(again, self.source)
        different = MODULE.build_installer_payload_source(
            archive_bytes=CANARY + b"x", sidecar_bytes=VALID_SIDECAR
        )
        self.assertNotEqual(different, self.source)


class PayloadTransportTests(unittest.TestCase):
    """Executes the generated source. Refusal only - never an install claim."""

    def _assert_bounded_refusal(self, result: subprocess.CompletedProcess[bytes]) -> str:
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        lines = result.stderr.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(result.stderr), 512)
        text = result.stderr.decode("ascii")
        for leak in ("Traceback", "File \"", "b64decode", "installer_main", ".py", "\\", "sys."):
            self.assertNotIn(leak, text)
        self.assertIn('"outcome":"refused"', text)
        self.assertNotIn("installed", text)
        return text

    def test_invalid_artifact_hits_the_real_installer_refusal(self) -> None:
        source = MODULE.build_installer_payload_source(
            archive_bytes=b"PK\x03\x04 not a real archive", sidecar_bytes=VALID_SIDECAR
        )
        with tempfile.TemporaryDirectory() as fixture:
            before = _tree(fixture)
            result = _run_payload(source, fixture)
            self.assertEqual(_tree(fixture), before)
        text = self._assert_bounded_refusal(result)
        # REMOTE_ARTIFACT_INVALID is raised by the admission module using
        # InstallerError from the leaf, so reaching it proves both modules
        # loaded under their required fixed identities.
        self.assertIn('"code":"REMOTE_ARTIFACT_INVALID"', text)

    def test_code_shaped_canary_is_inert(self) -> None:
        source = MODULE.build_installer_payload_source(
            archive_bytes=CANARY, sidecar_bytes=CANARY
        )
        with tempfile.TemporaryDirectory() as fixture:
            result = _run_payload(source, fixture)
            self.assertEqual(_tree(fixture), set())
            self.assertFalse(os.path.exists(os.path.join(fixture, CANARY_MARKER)))
        self._assert_bounded_refusal(result)

    def test_repeated_transport_runs_are_deterministic(self) -> None:
        source = MODULE.build_installer_payload_source(
            archive_bytes=b"not-an-archive", sidecar_bytes=VALID_SIDECAR
        )
        with tempfile.TemporaryDirectory() as fixture:
            first = _run_payload(source, fixture)
            second = _run_payload(source, fixture)
            self.assertEqual(_tree(fixture), set())
        self.assertEqual(first.returncode, second.returncode)
        self.assertEqual(first.stderr, second.stderr)
        self._assert_bounded_refusal(first)

    def test_malformed_argv_is_refused_without_leakage(self) -> None:
        source = MODULE.build_installer_payload_source(
            archive_bytes=b"not-an-archive", sidecar_bytes=VALID_SIDECAR
        )
        with tempfile.TemporaryDirectory() as fixture:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-", "provision-install"],
                input=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=TRANSPORT_TIMEOUT_SECONDS,
                cwd=fixture,
                env=_disposable_env(),
                creationflags=CREATION_FLAGS,
                check=False,
            )
            self.assertEqual(_tree(fixture), set())
        text = self._assert_bounded_refusal(result)
        self.assertIn('"code":"INVALID_INSTALLER"', text)


if __name__ == "__main__":
    unittest.main()
