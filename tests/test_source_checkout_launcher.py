from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workstack.service import WorkStack
from workstack.store import Store


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "run_work_stack.py"


class SourceCheckoutLauncherTests(unittest.TestCase):
    def run_launcher(
        self, cwd: Path, *, isolated: bool, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        command = [sys.executable]
        if isolated:
            command.append("-I")
        command.extend((str(LAUNCHER), "--help"))
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )

    def run_origin_probe(
        self, cwd: Path, *, isolated: bool, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        command = [sys.executable]
        if isolated:
            command.append("-I")
        command.extend(
            (
                "-c",
                "import pathlib,runpy,sys;"
                "runpy.run_path(sys.argv[1],run_name='workstack_launcher_probe');"
                "import workstack.cli;"
                "print(pathlib.Path(workstack.cli.__file__).resolve());"
                "print('bytecode-disabled=' + str(sys.dont_write_bytecode).lower())",
                str(LAUNCHER),
            )
        )
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )

    def assert_help(self, result: subprocess.CompletedProcess[bytes]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stderr, b"")
        self.assertIn(b"usage: work-stack", result.stdout)
        self.assertIn(b"agent", result.stdout)

    def assert_checkout_origin(self, result: subprocess.CompletedProcess[bytes]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stderr, b"")
        expected = str((ROOT / "workstack" / "cli.py").resolve())
        self.assertEqual(
            result.stdout.decode("utf-8").splitlines(),
            [expected, "bytecode-disabled=true"],
        )

    def test_absolute_launcher_works_in_isolated_mode_from_outside_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary)
            self.assert_help(self.run_launcher(outside, isolated=True))
            self.assert_checkout_origin(self.run_origin_probe(outside, isolated=True))

    def test_literal_isolated_agent_status_emits_canonical_lf_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary)
            authority = outside / "fresh-v3-authority"
            store = Store(authority)
            WorkStack(store)
            workspace_uid = str(store.load("workspace.json")["id"])
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(LAUNCHER),
                    "--data-dir",
                    str(authority),
                    "agent",
                    "--workspace-uid",
                    workspace_uid,
                    "status",
                ],
                cwd=outside,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                result.stderr.decode("utf-8", "replace"),
            )
            self.assertEqual(result.stderr, b"")
            self.assertTrue(result.stdout.endswith(b"\n"))
            self.assertNotIn(b"\r", result.stdout)
            self.assertEqual(result.stdout.count(b"\n"), 1)
            envelope = json.loads(result.stdout)
            self.assertEqual(envelope["contract"], "workstack.cli.v1")
            self.assertEqual(envelope["meta"]["command"], "agent.status")

    def test_checkout_package_wins_over_adversarial_cwd_in_both_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary)
            shadow = outside / "workstack"
            shadow.mkdir()
            marker = outside / "shadow-imported.txt"
            (shadow / "__init__.py").write_text(
                "from pathlib import Path\n"
                + "Path({!r}).write_text('shadowed', encoding='utf-8')\n".format(
                    str(marker)
                )
                + "raise RuntimeError('cwd workstack package was imported')\n",
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(outside)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"

            for isolated in (False, True):
                with self.subTest(isolated=isolated):
                    self.assert_help(
                        self.run_launcher(
                            outside, isolated=isolated, environment=environment
                        )
                    )
                    self.assert_checkout_origin(
                        self.run_origin_probe(
                            outside, isolated=isolated, environment=environment
                        )
                    )
                    self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
