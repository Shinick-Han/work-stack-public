from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "desktop_update_launch_test",
    ROOT / "desktop/python-webview-shell/workstack_desktop.py",
)
assert SPEC and SPEC.loader
HOST = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HOST
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    SPEC.loader.exec_module(HOST)


class DesktopUpdateLaunchTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows directory handle regression")
    def test_pending_updater_does_not_prevent_install_directory_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / "app"
            state = root / "state"
            script = install / "scripts/windows/Apply-WorkStackUpdate.ps1"
            script.parent.mkdir(parents=True)
            script.write_text("# disposable canary", encoding="utf-8")
            state.mkdir()
            host = object.__new__(HOST.WorkStackDesktopHost)
            host.install_root = install
            host.state_root = state
            host.install_update_on_exit = True
            host.downloaded_update = types.SimpleNamespace(
                setup_path=state / "setup.ps1", checksum_path=state / "setup.sha256",
                version="1.0.11",
            )
            host._trace = mock.Mock()
            backup = root / "app.rollback"
            self.assertTrue(install.resolve().is_relative_to(root.resolve()))
            self.assertTrue(backup.resolve().is_relative_to(root.resolve()))
            mover = root / "move-fixture.ps1"
            mover.write_text(
                "param([string]$Source, [string]$Target)\n"
                "$ErrorActionPreference = 'Stop'\n"
                "Move-Item -LiteralPath $Source -Destination $Target\n",
                encoding="utf-8",
            )
            real_popen = subprocess.Popen
            children = []

            def harmless_applicator(command, **options):
                # Exercise the same PowerShell Move-Item operation as Install,
                # using the host-selected working directory and only this
                # isolated fixture; no installer, user data or shortcut writes.
                child = real_popen(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(mover), "-Source", str(install), "-Target", str(backup)],
                    cwd=options["cwd"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                children.append(child)
                return child

            try:
                with mock.patch.object(HOST.subprocess, "Popen", side_effect=harmless_applicator):
                    host._launch_pending_update()
                self.assertEqual(len(children), 1)
                _, errors = children[0].communicate(timeout=15)
                self.assertEqual(children[0].returncode, 0, errors.decode(errors="replace"))
                self.assertEqual(
                    (backup / "scripts/windows/Apply-WorkStackUpdate.ps1").read_text(),
                    "# disposable canary",
                )
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
