from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "workstack_desktop.py"
SPEC = importlib.util.spec_from_file_location("workstack_desktop_ssh_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    SPEC.loader.exec_module(MODULE)


def write_profile(root: Path, **overrides: object) -> None:
    payload: dict[str, object] = {
        "storage_mode": "ssh-remote",
        "ssh_host_alias": "work-linux",
        "remote_app_dir": "/srv/workstack/app",
        "remote_data_dir": "/srv/workstack/ssot",
        "local_forward_port": 18765,
        "workspace_id": WORKSPACE_ID,
    }
    payload.update(overrides)
    (root / "remote-connection.json").write_text(json.dumps(payload), encoding="utf-8")


class DesktopSshRemoteProfileTest(unittest.TestCase):
    def test_absent_profile_preserves_local_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(MODULE.load_remote_connection_profile(Path(directory)))

    def test_profile_is_strictly_validated_and_defaults_remote_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_profile(root)
            profile = MODULE.load_remote_connection_profile(root)

        self.assertEqual(profile.ssh_host_alias, "work-linux")
        self.assertEqual(profile.remote_port, 8765)
        self.assertEqual(profile.local_forward_port, 18765)

    def test_profile_rejects_shell_alias_and_relative_or_parent_paths(self) -> None:
        cases = (
            {"ssh_host_alias": "work-linux; calc.exe"},
            {"remote_app_dir": "relative/path"},
            {"remote_data_dir": "/srv/workstack/../private"},
            {"remote_data_dir": "/"},
            {"local_forward_port": True},
            {"workspace_id": "not-a-uuid"},
            {"surprise": "field"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_profile(root, **overrides)
                with self.assertRaises(RuntimeError):
                    MODULE.load_remote_connection_profile(root)

    def test_tunnel_is_loopback_only_strict_and_owns_remote_server_command(self) -> None:
        profile = MODULE.RemoteConnectionProfile(
            ssh_host_alias="work-linux",
            remote_app_dir="/srv/workstack/app files",
            remote_data_dir="/srv/workstack/private ssot",
            local_forward_port=18765,
            workspace_id=WORKSPACE_ID,
            remote_port=9876,
        )
        command = MODULE.build_ssh_tunnel_command(profile, r"C:\Windows\System32\OpenSSH\ssh.exe")

        self.assertIn("127.0.0.1:18765:127.0.0.1:9876", command)
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertNotIn("StrictHostKeyChecking=no", command)
        self.assertEqual(command[-2], "work-linux")
        self.assertIn("exec python3", command[-1])
        self.assertIn("--host 127.0.0.1", command[-1])
        self.assertIn("'/srv/workstack/private ssot'", command[-1])

    def test_check_is_read_only_and_uses_batch_mode(self) -> None:
        profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        command = MODULE.build_ssh_check_command(profile, "ssh")

        self.assertIn("BatchMode=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("test -f /app/run_work_stack.py", command[-1])
        self.assertIn("test -d /ssot", command[-1])
        self.assertIn("test -f /ssot/store-meta.json", command[-1])
        self.assertIn("python3 /app/run_work_stack.py --help", command[-1])
        self.assertNotIn("mkdir", command[-1])

    @mock.patch.object(MODULE, "find_ssh_executable", return_value="ssh")
    @mock.patch.object(MODULE.subprocess, "run")
    def test_check_reports_machine_readable_success(self, run: mock.Mock, _find: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_profile(root)
            with mock.patch("builtins.print") as output:
                result = MODULE.check_remote_connection(root)

        self.assertEqual(result, 0)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["storage_mode"], "ssh-remote")
        self.assertEqual(payload["workspace_id"], WORKSPACE_ID)

    @mock.patch.object(MODULE, "find_ssh_executable", return_value="ssh")
    @mock.patch.object(MODULE.subprocess, "run")
    def test_check_fails_closed_with_actionable_error(self, run: mock.Mock, _find: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 255, "", "Host key verification failed.")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_profile(root)
            with self.assertRaisesRegex(RuntimeError, "known-host key"):
                MODULE.check_remote_connection(root)

    def test_remote_host_uses_forwarded_url_without_reading_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_profile(root, local_forward_port=19876)
            options = argparse.Namespace(
                install_root=ROOT,
                state_root=root,
                url="",
                probe_provider="",
                probe_result=None,
                auto_close_seconds=0,
                check_remote_connection=False,
            )
            host = MODULE.WorkStackDesktopHost(options)

        self.assertEqual(host.workstack_url, "http://127.0.0.1:19876/")
        self.assertEqual(host.workstack_origin, ("http", "127.0.0.1", 19876))

    @mock.patch.object(MODULE.urllib.request, "urlopen")
    def test_remote_workspace_identity_mismatch_fails_closed(self, urlopen: mock.Mock) -> None:
        response = mock.MagicMock()
        response.read.return_value = json.dumps({
            "data": {"workspace_id": "22222222-2222-4222-8222-222222222222"}
        }).encode("utf-8")
        urlopen.return_value.__enter__.return_value = response
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host.workstack_url = "http://127.0.0.1:18765/"

        with self.assertRaisesRegex(RuntimeError, "workspace identity"):
            host._verify_remote_workspace()

    def test_remote_process_is_terminated_and_log_is_closed_on_shutdown(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.pid = 1234
        log = mock.Mock()
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_ssh_process = process
        host.remote_ssh_log = log

        host._stop_owned_remote_connection()

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        log.close.assert_called_once_with()
        self.assertIsNone(host.remote_ssh_process)
        self.assertIsNone(host.remote_ssh_log)


if __name__ == "__main__":
    unittest.main()
