from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.parse
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
    def test_local_workspace_identity_is_read_from_the_configured_ssot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_path = Path(directory)
            (data_path / "workspace.json").write_text(
                json.dumps({"id": WORKSPACE_ID, "name": "Stable workspace", "version": 2}),
                encoding="utf-8",
            )

            actual = MODULE.WorkStackDesktopHost._read_local_workspace_identity(data_path)

        self.assertEqual(actual, WORKSPACE_ID)

    def test_ready_local_server_is_reused_only_for_the_configured_workspace(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = None
        host._local_runtime_config = mock.Mock(return_value=(
            {"port": 8765, "data_dir": "unused", "backup_dir": "unused", "backup_retention": 7},
            Path("config.json"),
        ))
        host._read_local_workspace_identity = mock.Mock(return_value=WORKSPACE_ID)
        host._is_ready = mock.Mock(return_value=True)
        host._read_server_workspace_identity = mock.Mock(return_value=WORKSPACE_ID)
        host._select_available_local_port = mock.Mock()
        host._trace = mock.Mock()

        host._ensure_server()

        host._select_available_local_port.assert_not_called()
        host._trace.assert_called_once()

    def test_ready_local_server_with_another_identity_moves_to_a_session_port(self) -> None:
        different_workspace_id = "22222222-2222-4222-8222-222222222222"
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = None
        host._local_runtime_config = mock.Mock(return_value=(
            {"port": 8765, "data_dir": "unused", "backup_dir": "unused", "backup_retention": 7},
            Path("config.json"),
        ))
        host._read_local_workspace_identity = mock.Mock(return_value=WORKSPACE_ID)
        host._is_ready = mock.Mock(return_value=True)
        host._read_server_workspace_identity = mock.Mock(return_value=different_workspace_id)
        host._select_available_local_port = mock.Mock(side_effect=RuntimeError("session port selected"))

        with self.assertRaisesRegex(RuntimeError, "session port selected"):
            host._ensure_server()

        host._select_available_local_port.assert_called_once_with(
            8765,
            expected_workspace_id=WORKSPACE_ID,
            active_workspace_id=different_workspace_id,
        )

    def test_session_port_updates_both_navigation_url_and_trusted_origin(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.workstack_url = "http://127.0.0.1:8765/"
        host.workstack_origin = "http://127.0.0.1:8765"
        host._trace = mock.Mock()

        with mock.patch.object(MODULE, "resolve_runtime_forward_port", return_value=18766):
            selected = host._select_available_local_port(
                8765,
                expected_workspace_id=WORKSPACE_ID,
                active_workspace_id="22222222-2222-4222-8222-222222222222",
            )

        self.assertEqual(selected, 18766)
        self.assertEqual(host.workstack_url, "http://127.0.0.1:18766/")
        self.assertEqual(host.workstack_origin, ("http", "127.0.0.1", 18766))

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
        self.assertIn("--public-port 18765", command[-1])
        self.assertIn("--exit-with-parent", command[-1])
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

    @mock.patch("ssot_connection.find_ssh_executable", return_value="ssh")
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

    @mock.patch("ssot_connection.find_ssh_executable", return_value="ssh")
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

    def test_remote_workspace_identity_mismatch_fails_closed(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = json.dumps({
            "data": {
                "workspace_id": "22222222-2222-4222-8222-222222222222",
                "product_version": "1.0.5",
                "remote_protocol_version": 1,
            }
        }).encode("utf-8")
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host.workstack_url = "http://127.0.0.1:18765/"

        with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response):
            response.__enter__.return_value = response
            with self.assertRaisesRegex(RuntimeError, "workspace identity"):
                host._verify_remote_workspace()

    def test_remote_protocol_is_read_from_storage_endpoint_and_old_server_is_blocked(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"data": {
            "workspace_id": WORKSPACE_ID,
            "product_version": "0.9.0",
            "remote_protocol_version": 0,
        }}).encode("utf-8")
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host.workstack_url = "http://127.0.0.1:18765/"

        with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response):
            response.__enter__.return_value = response
            with self.assertRaisesRegex(RuntimeError, r"0\.9\.0.*protocol 0.*Upgrade"):
                host._verify_remote_workspace()

    def test_monitor_rejects_out_of_band_runtime_protocol_change(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"data": {
            "workspace_id": WORKSPACE_ID,
            "product_version": "1.0.7",
            "remote_protocol_version": 2,
        }}).encode("utf-8")
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host.workstack_url = "http://127.0.0.1:18765/"
        host.remote_protocol_version = 1
        host.remote_authority_lock = MODULE.threading.RLock()
        host.remote_rebind_target = ""
        host.remote_rebind_deadline = 0.0
        host.remote_recovery_required = MODULE.threading.Event()
        host.remote_recovery_message = ""

        with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response):
            response.__enter__.return_value = response
            self.assertFalse(host._is_remote_session_healthy())

        self.assertTrue(host.remote_recovery_required.is_set())
        self.assertIn("protocol changed from 1 to 2", host.remote_recovery_message)

    def test_update_gate_compares_manifest_minimum_to_actual_remote_endpoint(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"data": {
            "workspace_id": WORKSPACE_ID,
            "product_version": "1.0.5",
            "remote_protocol_version": 1,
        }}).encode("utf-8")
        manifest = types.SimpleNamespace(
            is_newer=True,
            version="1.0.7",
            release_url="https://github.com/Shinick-Han/work-stack-public/releases/tag/v1.0.7",
            minimum_remote_protocol=2,
        )
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host.workstack_url = "http://127.0.0.1:18765/"
        host.update_preferences = types.SimpleNamespace(auto_download=False, install_on_exit=True)
        host.downloaded_update = None
        host.install_update_on_exit = False
        host._set_update_status = mock.Mock()

        with (
            mock.patch.object(MODULE, "fetch_url_bytes", return_value=b"manifest"),
            mock.patch.object(MODULE, "parse_update_manifest", return_value=manifest),
            mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response) as urlopen,
        ):
            response.__enter__.return_value = response
            host._check_update_worker(force_download=False)

        urlopen.assert_called_once()
        status_call = host._set_update_status.call_args
        self.assertEqual(status_call.args[0], "blocked")
        self.assertRegex(status_call.kwargs["message"], r"protocol 1.*requires protocol 2.*Upgrade")

    def test_update_check_reports_a_lagging_stable_channel_as_current(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.downloaded_update = object()
        host.install_update_on_exit = True
        host._set_update_status = mock.Mock()
        rollback = MODULE.OlderUpdateManifest("1.0.5", "1.0.6")

        with (
            mock.patch.object(MODULE, "fetch_url_bytes", return_value=b"manifest"),
            mock.patch.object(MODULE, "parse_update_manifest", side_effect=rollback),
        ):
            host._check_update_worker(force_download=False)

        self.assertIsNone(host.downloaded_update)
        self.assertFalse(host.install_update_on_exit)
        host._set_update_status.assert_called_once_with(
            "current",
            latest_version="1.0.5",
            release_url="https://github.com/Shinick-Han/work-stack-public/releases/tag/v1.0.5",
            message="Installed Work Stack 1.0.6 is newer than the stable channel 1.0.5",
        )

    def test_remote_rebind_completion_verifies_server_then_atomically_updates_profile(self) -> None:
        next_workspace_id = "22222222-2222-4222-8222-222222222222"
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"data": {
            "workspace_id": next_workspace_id,
            "product_version": "1.0.5",
            "remote_protocol_version": 1,
        }}).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_profile(root)
            host = object.__new__(MODULE.WorkStackDesktopHost)
            host.state_root = root
            host.active_connection_draft = MODULE.load_connection_draft(root)
            host.remote_profile = MODULE.connection_profile_from_draft(host.active_connection_draft)
            host.workstack_url = "http://127.0.0.1:18765/"
            host._dispatch_ssot_status = mock.Mock()

            with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response):
                response.__enter__.return_value = response
                host._coordinate_remote_workspace_rebind(next_workspace_id)

            saved = MODULE.load_connection_draft(root)

        self.assertEqual(saved["workspace_id"], next_workspace_id)
        self.assertEqual(host.active_connection_draft["workspace_id"], next_workspace_id)
        self.assertEqual(host.remote_profile.workspace_id, next_workspace_id)
        payload = host._dispatch_ssot_status.call_args.args[0]
        self.assertEqual(payload["state"], "ready")
        self.assertFalse(payload["restart_required"])

    def test_remote_rebind_coordination_failure_stops_tunnel_and_requires_recovery(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.active_connection_draft = {
            "storage_mode": "ssh-remote",
            "ssh_host_alias": "work-linux",
            "remote_app_dir": "/app",
            "remote_data_dir": "/ssot",
            "local_forward_port": 18765,
            "remote_port": 8765,
            "workspace_id": WORKSPACE_ID,
        }
        host.remote_profile = MODULE.connection_profile_from_draft(host.active_connection_draft)
        host.state_root = Path("C:/state")
        host._coordinate_remote_workspace_rebind = mock.Mock(side_effect=RuntimeError("disk full"))
        host._stop_remote_monitor = mock.Mock()
        host._stop_owned_remote_connection = mock.Mock()
        host._dispatch_ssot_status = mock.Mock()

        host._run_remote_workspace_rebind("22222222-2222-4222-8222-222222222222")

        host._stop_remote_monitor.assert_called_once_with()
        host._stop_owned_remote_connection.assert_called_once_with()
        payload = host._dispatch_ssot_status.call_args.args[0]
        self.assertEqual(payload["state"], "error")
        self.assertTrue(payload["restart_required"])
        self.assertIn("Enter the verified remote Workspace ID", payload["message"])

    def test_source_zoom_is_persisted_per_provider_and_invalid_data_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {"outlook": 90, "teams": 120, "onenote": 100}
            MODULE.save_source_zoom(root, values)
            self.assertEqual(MODULE.load_source_zoom(root), values)
            (root / MODULE.SOURCE_ZOOM_FILE).write_text('{"outlook":201}', encoding="utf-8")
            self.assertEqual(MODULE.load_source_zoom(root), {
                "outlook": 100,
                "teams": 100,
                "onenote": 100,
            })

    def test_source_zoom_command_updates_native_view_and_reports_status(self) -> None:
        core = mock.Mock()
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.state_root = Path(tempfile.mkdtemp())
        host.source_zoom = {"outlook": 100, "teams": 100, "onenote": 100}
        host.source_webviews = {"teams": types.SimpleNamespace(ZoomFactor=1.0)}
        host.workstack_webview = types.SimpleNamespace(CoreWebView2=core)

        host._set_source_zoom("workstack-source-host|zoom|teams|130")

        self.assertEqual(host.source_zoom["teams"], 130)
        self.assertEqual(host.source_webviews["teams"].ZoomFactor, 1.3)
        payload = json.loads(core.PostWebMessageAsJson.call_args.args[0])
        self.assertEqual(payload["values"]["teams"], 130)

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

    def test_source_resume_cannot_resurrect_a_view_after_inbox_deactivation(self) -> None:
        viewport = types.SimpleNamespace(Visible=True, BringToFront=mock.Mock())
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.source_viewports = {"outlook": viewport}
        host.source_suspended = False
        host.source_host_active = True
        host.active_provider = "outlook"

        host._deactivate_source()
        host._restore_source()

        self.assertFalse(viewport.Visible)
        self.assertFalse(host.source_host_active)
        viewport.BringToFront.assert_not_called()

    def test_ssot_draft_validation_rejects_secrets_and_arbitrary_ssh_arguments(self) -> None:
        base = {
            "storage_mode": "ssh-remote",
            "ssh_host_alias": "work-linux",
            "remote_app_dir": "/srv/workstack/app",
            "remote_data_dir": "/srv/workstack/ssot",
            "local_forward_port": 18765,
            "workspace_id": WORKSPACE_ID,
        }
        for forbidden in ("password", "private_key", "identity_file", "ssh_args"):
            with self.subTest(forbidden=forbidden):
                with self.assertRaisesRegex(RuntimeError, "unsupported fields"):
                    MODULE.validate_connection_draft({**base, forbidden: "secret"})

    def test_local_ssot_profile_is_saved_as_the_exact_minimal_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            MODULE.save_connection_draft(root, {"storage_mode": "local"})
            path = root / MODULE.REMOTE_CONNECTION_FILE

            self.assertEqual(path.read_text(encoding="utf-8"), '{"storage_mode":"local"}\n')
            self.assertEqual(MODULE.load_connection_draft(root), {"storage_mode": "local"})

    @mock.patch.object(MODULE, "find_ssh_executable", return_value="ssh")
    @mock.patch.object(MODULE.subprocess, "run")
    def test_ssot_test_command_checks_remote_without_persisting(
        self, run: mock.Mock, _find: mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        draft = {
            "storage_mode": "ssh-remote",
            "ssh_host_alias": "work-linux",
            "remote_app_dir": "/srv/workstack/app",
            "remote_data_dir": "/srv/workstack/ssot",
            "local_forward_port": 18765,
            "workspace_id": WORKSPACE_ID,
            "remote_port": 8765,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = object.__new__(MODULE.WorkStackDesktopHost)
            host.state_root = root
            payload = host._test_ssot_connection(draft)

            self.assertFalse((root / MODULE.REMOTE_CONNECTION_FILE).exists())

        self.assertEqual(payload["type"], "workstack-ssot-connection-status")
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["storage_mode"], "ssh-remote")
        self.assertNotIn("storage_mode", payload["profile"])
        self.assertFalse(payload["restart_required"])
        run.assert_called_once()

    def test_ssot_save_reports_restart_when_profile_differs_from_active_launch(self) -> None:
        core = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            host = object.__new__(MODULE.WorkStackDesktopHost)
            host.state_root = Path(directory)
            host.active_connection_draft = {"storage_mode": "local"}
            host.workstack_webview = types.SimpleNamespace(CoreWebView2=core)
            draft = {
                "storage_mode": "ssh-remote",
                "ssh_host_alias": "work-linux",
                "remote_app_dir": "/srv/workstack/app",
                "remote_data_dir": "/srv/workstack/ssot",
                "local_forward_port": 18765,
                "workspace_id": WORKSPACE_ID,
            }
            encoded = urllib.parse.quote(json.dumps(draft, separators=(",", ":")), safe="")

            host._handle_ssot_message(f"workstack-ssot-host|save|{encoded}")
            saved = MODULE.load_connection_draft(host.state_root)

        self.assertEqual(saved["storage_mode"], "ssh-remote")
        response = json.loads(core.PostWebMessageAsJson.call_args.args[0])
        self.assertEqual(response["state"], "saved")
        self.assertTrue(response["restart_required"])
        self.assertEqual(response["profile"]["ssh_host_alias"], "work-linux")
        self.assertNotIn("password", json.dumps(response))

    def test_remote_status_reports_runtime_port_and_session_change_detection(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.state_root = Path("C:/state")
        host.active_connection_draft = {
            "storage_mode": "ssh-remote",
            "ssh_host_alias": "work-linux",
            "remote_app_dir": "/srv/workstack/app",
            "remote_data_dir": "/srv/workstack/ssot",
            "local_forward_port": 18765,
            "remote_port": 8765,
            "workspace_id": WORKSPACE_ID,
        }
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/srv/workstack/app", "/srv/workstack/ssot",
            24567, WORKSPACE_ID, 8765,
        )

        payload = host._ssot_status_payload(host.active_connection_draft, "ready")

        self.assertTrue(payload["session_change_detection"])
        self.assertEqual(payload["runtime_forward_port"], 24567)

    def test_remote_prerequisite_test_marks_changed_draft_for_save_and_restart(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.state_root = Path("C:/state")
        host.active_connection_draft = {"storage_mode": "local"}
        host.remote_profile = None
        draft = {
            "storage_mode": "ssh-remote",
            "ssh_host_alias": "work-linux",
            "remote_app_dir": "/srv/workstack/app",
            "remote_data_dir": "/srv/workstack/ssot",
            "local_forward_port": 18765,
            "remote_port": 8765,
            "workspace_id": WORKSPACE_ID,
        }

        with mock.patch.object(MODULE, "run_remote_connection_check"):
            payload = host._test_ssot_connection(draft)

        self.assertTrue(payload["restart_required"])
        self.assertIn("Save settings", payload["message"])

    def test_ssot_host_routes_only_exact_reconnect_and_diagnostics_commands(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host._start_manual_remote_reconnect = mock.Mock()
        host._open_ssot_diagnostics = mock.Mock()

        host._handle_ssot_message("workstack-ssot-host|reconnect")
        host._handle_ssot_message("workstack-ssot-host|open-diagnostics")
        host._handle_ssot_message("workstack-ssot-host|reconnect|unexpected")

        host._start_manual_remote_reconnect.assert_called_once_with()
        host._open_ssot_diagnostics.assert_called_once_with()

    def test_rebind_start_is_acknowledged_only_after_native_coordination_is_active(self) -> None:
        workspace_id = "22222222-2222-4222-8222-222222222222"
        core = mock.Mock()
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host.remote_authority_lock = MODULE.threading.RLock()
        host.remote_rebind_target = ""
        host.remote_rebind_deadline = 0.0
        host.workstack_webview = types.SimpleNamespace(CoreWebView2=core)

        host._handle_ssot_message(f"workstack-ssot-host|rebind-start|{workspace_id}")

        self.assertEqual(host.remote_rebind_target, workspace_id)
        payload = json.loads(core.PostWebMessageAsJson.call_args.args[0])
        self.assertEqual(payload, {
            "type": "workstack-ssot-rebind-ready",
            "workspace_id": workspace_id,
        })


if __name__ == "__main__":
    unittest.main()
