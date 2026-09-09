from __future__ import annotations

import importlib.util
import json
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
import desktop_update_process as PROCESS


class DesktopUpdateLaunchTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows PowerShell process launch")
    def test_hidden_powershell_executes_and_signals_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);app=root / "custom app";state=root / "state"
            script=app / "scripts/windows/Apply-WorkStackUpdate.ps1"
            script.parent.mkdir(parents=True);(state / "updates").mkdir(parents=True)
            script.write_text('''param([string]$SetupPath,[string]$ChecksumPath,[string]$InstallRoot,[string]$StateRoot,[int]$ParentProcessId,[string]$TargetVersion,[string]$LaunchId,[switch]$NoShortcut)
$ErrorActionPreference='Stop'
if (-not $NoShortcut) { throw 'Existing shortcuts must be preserved' }
$value=@{status='waiting-for-parent';launch_id=$LaunchId;version=$TargetVersion;pid=$PID;parent_pid=$ParentProcessId} | ConvertTo-Json -Compress
$path=Join-Path $StateRoot "updates/.launch-$LaunchId.json"
[IO.File]::WriteAllText("$path.tmp",$value,[Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath "$path.tmp" -Destination $path
Wait-Process -Id $ParentProcessId -Timeout 30
''',encoding="utf-8")
            download=types.SimpleNamespace(setup_path=state / "setup",checksum_path=state / "hash",version="1.0.13")
            process=PROCESS.launch_update_process(app,state,download)
            try:
                self.assertIsNone(process.poll())
            finally:
                process.terminate();process.wait(timeout=5)

    def test_failed_handoff_keeps_window_open_and_disarms_install_on_exit(self):
        host = object.__new__(HOST.WorkStackDesktopHost)
        host.downloaded_update = types.SimpleNamespace(version="1.0.13", release_url="")
        host.install_update_on_exit = False
        host._launch_pending_update = mock.Mock(return_value=False)
        host._set_update_status = mock.Mock()
        host.window = mock.Mock()
        host._handle_update_message(HOST.UPDATE_HOST_PREFIX + "|install")
        host.window.destroy.assert_not_called()
        self.assertFalse(host.install_update_on_exit)

    def test_ready_handoff_is_reused_when_window_finally_exits(self):
        host = object.__new__(HOST.WorkStackDesktopHost)
        host.downloaded_update = types.SimpleNamespace(version="1.0.13", release_url="")
        host.install_update_on_exit = True
        host.install_root = Path("fixture-app")
        host.state_root = Path("fixture-state")
        host._trace = mock.Mock()
        host._set_update_status = mock.Mock()
        host.window = mock.Mock()
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(HOST, "launch_update_process", return_value=process) as launch:
            host._handle_update_message(HOST.UPDATE_HOST_PREFIX + "|install")
            host.window.destroy.assert_called_once()
            self.assertTrue(host._launch_pending_update())
            launch.assert_called_once()

    def test_matching_readiness_receipt_and_custom_install_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "custom app"
            state = root / "custom state"
            script = app / "scripts/windows/Apply-WorkStackUpdate.ps1"
            script.parent.mkdir(parents=True)
            script.write_text("# fixture")
            (state / "updates").mkdir(parents=True)
            download = types.SimpleNamespace(setup_path=state / "setup.ps1", checksum_path=state / "setup.sha256", version="1.0.13")
            child = mock.Mock(pid=12345)
            child.poll.return_value = None
            def launch(command, **options):
                self.assertIn("-NoShortcut", command)
                self.assertEqual(options["cwd"], state)
                self.assertFalse(any(key.casefold() == "psmodulepath" for key in options["env"]))
                launch_id = command[command.index("-LaunchId") + 1]
                (state / "updates" / f".launch-{launch_id}.json").write_text(json.dumps({
                    "status":"waiting-for-parent", "launch_id":launch_id,
                    "version":download.version, "pid":child.pid, "parent_pid":os.getpid(),
                }))
                return child
            with mock.patch.object(PROCESS.subprocess, "Popen", side_effect=launch):
                self.assertIs(PROCESS.launch_update_process(app, state, download), child)
            self.assertFalse(list((state / "updates").glob(".launch-*.json")))
            child.terminate.assert_not_called()

    def test_readiness_timeout_stops_only_the_waiting_applicator(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); app=root / "app"; state=root / "state"
            script=app / "scripts/windows/Apply-WorkStackUpdate.ps1"
            script.parent.mkdir(parents=True);script.write_text("# fixture")
            state.mkdir()
            download=types.SimpleNamespace(setup_path=state / "setup",checksum_path=state / "hash",version="1.0.13")
            child=mock.Mock();child.poll.return_value=None
            with mock.patch.object(PROCESS.subprocess, "Popen", return_value=child):
                with self.assertRaisesRegex(RuntimeError,"did not become ready"):
                    PROCESS.launch_update_process(app,state,download,timeout=0)
            child.terminate.assert_called_once()

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
            host._set_update_status = mock.Mock()
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
