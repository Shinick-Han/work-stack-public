from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "scripts"
    / "windows"
    / "Test-WorkStackConnectionRegistrySmoke.py"
)
SPEC = importlib.util.spec_from_file_location(
    "packaged_connection_registry_smoke_test", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PackagedConnectionRegistrySmokeTest(unittest.TestCase):
    def test_release_smoke_runs_every_scenario_without_process_or_network(self) -> None:
        with mock.patch(
            "ssot_connection.subprocess.Popen",
            side_effect=AssertionError("smoke must not launch OpenSSH"),
        ), mock.patch(
            "ssot_connection.subprocess.run",
            side_effect=AssertionError("smoke must not invoke a network command"),
        ):
            report = MODULE.run_smoke(ROOT)

        self.assertEqual(report.status, "passed")
        self.assertEqual(report.schema_version, 1)
        self.assertEqual(report.store_files_verified, 18)
        self.assertEqual(
            report.scenarios,
            (
                "gate-off-legacy-read-only",
                "gate-on-local-migrate-activate-restart-confirm",
                "ssh-selection-identity-and-command-no-network",
                "failed-startup-pending-receipt",
                "explicit-activation-restore",
                "store-sha256-unchanged",
            ),
        )

    def test_report_and_cli_output_are_deterministic_and_machine_readable(self) -> None:
        first = MODULE.run_smoke(ROOT).to_document()
        second = MODULE.run_smoke(ROOT).to_document()
        self.assertEqual(first, second)

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = MODULE.main(["--install-root", str(ROOT)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), first)

    def test_gate_off_legacy_probe_cannot_create_registry_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory)
            MODULE._assert_gate_off_is_read_only(state_root)
            self.assertEqual(tuple(state_root.iterdir()), ())

    def test_script_refuses_to_test_modules_from_another_install_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "install root it tests"):
                MODULE.run_smoke(Path(directory))

    def test_installer_builder_invokes_smoke_with_bundled_runtime(self) -> None:
        builder = (
            ROOT / "scripts" / "windows" / "Build-WindowsInstaller.ps1"
        ).read_text(encoding="utf-8-sig")
        script_name = "Test-WorkStackConnectionRegistrySmoke.py"
        self.assertIn(script_name, builder)
        self.assertRegex(builder, r"(?s)&\s*\$runtimePython\s+\$registrySmoke")
        self.assertIn("--install-root $payload", builder)
        self.assertLess(builder.index(script_name), builder.index("Compress-Archive"))


if __name__ == "__main__":
    unittest.main()
