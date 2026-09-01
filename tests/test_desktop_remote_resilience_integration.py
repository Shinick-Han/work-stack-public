from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import types
import unittest
import time
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "workstack_desktop.py"
SPEC = importlib.util.spec_from_file_location("workstack_desktop_resilience_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    SPEC.loader.exec_module(MODULE)


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"


def write_remote_profile(root: Path) -> None:
    MODULE.save_connection_draft(root, {
        "storage_mode": "ssh-remote",
        "ssh_host_alias": "work-linux",
        "remote_app_dir": "/srv/workstack/app",
        "remote_data_dir": "/srv/workstack/ssot",
        "local_forward_port": 18765,
        "remote_port": 8765,
        "workspace_id": WORKSPACE_ID,
    })


class DesktopRemoteResilienceIntegrationTest(unittest.TestCase):
    def test_out_of_band_second_client_rebind_fails_closed_on_next_monitor_tick(self) -> None:
        other_workspace_id = "22222222-2222-4222-8222-222222222222"
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"data": {
            "workspace_id": other_workspace_id,
            "product_version": "1.0.6",
            "remote_protocol_version": 1,
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
        host.remote_shutdown_requested = MODULE.threading.Event()
        process = mock.Mock()
        process.poll.return_value = None
        process.pid = 42
        host.remote_ssh_process = process
        host.remote_ssh_log = None
        host._dispatch_ssot_status = mock.Mock()
        host.active_connection_draft = {
            "storage_mode": "ssh-remote", "ssh_host_alias": "work-linux",
            "remote_app_dir": "/app", "remote_data_dir": "/ssot",
            "local_forward_port": 18765, "remote_port": 8765,
            "workspace_id": WORKSPACE_ID,
        }
        reconnect = mock.Mock(return_value=True)
        states: list[str] = []
        monitor = MODULE.RemoteConnectionMonitor(
            is_healthy=host._is_remote_session_healthy,
            is_process_alive=host._is_remote_process_alive,
            reconnect_once=reconnect,
            publish_state=states.append,
            reload_view=mock.Mock(),
            is_recovery_required=host.remote_recovery_required.is_set,
            on_recovery_required=host._fail_closed_remote_authority,
            initial_grace=0,
            poll_interval=0.005,
            failure_threshold=1,
            reconnect_backoff=(0,),
            reconnect_grace=0,
        )

        with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response):
            response.__enter__.return_value = response
            monitor.start()
            deadline = time.monotonic() + 1
            while not host.remote_shutdown_requested.is_set() and time.monotonic() < deadline:
                time.sleep(0.005)
            monitor.stop()

        self.assertTrue(host.remote_recovery_required.is_set())
        self.assertTrue(host.remote_shutdown_requested.is_set())
        self.assertEqual(states, ["disconnected"])
        reconnect.assert_not_called()
        process.terminate.assert_called_once_with()

    def test_initiating_client_rebind_gap_is_serialized_until_profile_commit(self) -> None:
        next_workspace_id = "22222222-2222-4222-8222-222222222222"
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"data": {
            "workspace_id": next_workspace_id,
            "product_version": "1.0.6",
            "remote_protocol_version": 1,
        }}).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_remote_profile(root)
            host = object.__new__(MODULE.WorkStackDesktopHost)
            host.state_root = root
            host.active_connection_draft = MODULE.load_connection_draft(root)
            host.remote_profile = MODULE.connection_profile_from_draft(host.active_connection_draft)
            host.workstack_url = "http://127.0.0.1:18765/"
            host.remote_protocol_version = 1
            host.remote_authority_lock = MODULE.threading.RLock()
            host.remote_rebind_target = ""
            host.remote_rebind_deadline = 0.0
            host.remote_recovery_required = MODULE.threading.Event()
            host.remote_recovery_message = ""
            host._dispatch_ssot_status = mock.Mock()
            host._is_ready = mock.Mock(return_value=True)

            host._begin_remote_workspace_rebind(next_workspace_id)
            with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response) as urlopen:
                response.__enter__.return_value = response
                self.assertTrue(host._is_remote_session_healthy())
                urlopen.assert_not_called()
                host._coordinate_remote_workspace_rebind(next_workspace_id)

            saved = MODULE.load_connection_draft(root)

        self.assertEqual(saved["workspace_id"], next_workspace_id)
        self.assertFalse(host.remote_recovery_required.is_set())
        self.assertEqual(host.remote_rebind_target, "")

    def test_coordinated_rebind_survives_restart_and_reconnect_verification(self) -> None:
        next_workspace_id = "22222222-2222-4222-8222-222222222222"
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"data": {
            "workspace_id": next_workspace_id,
            "product_version": "1.0.5",
            "remote_protocol_version": 1,
        }}).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_remote_profile(root)
            first = object.__new__(MODULE.WorkStackDesktopHost)
            first.state_root = root
            first.active_connection_draft = MODULE.load_connection_draft(root)
            first.remote_profile = MODULE.connection_profile_from_draft(first.active_connection_draft)
            first.workstack_url = "http://127.0.0.1:18765/"
            first._dispatch_ssot_status = mock.Mock()
            with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response):
                response.__enter__.return_value = response
                first._coordinate_remote_workspace_rebind(next_workspace_id)

            options = argparse.Namespace(
                install_root=ROOT, state_root=root, url="", probe_provider="",
                probe_result=None, auto_close_seconds=0, check_remote_connection=False,
            )
            restarted = MODULE.WorkStackDesktopHost(options)
            with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response):
                response.__enter__.return_value = response
                restarted._verify_remote_workspace()

        self.assertEqual(restarted.remote_profile.workspace_id, next_workspace_id)
    def test_host_uses_runtime_port_without_rewriting_saved_preference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_remote_profile(root)
            runtime = MODULE.RemoteConnectionProfile(
                "work-linux", "/srv/workstack/app", "/srv/workstack/ssot",
                24567, WORKSPACE_ID, 8765,
            )
            options = argparse.Namespace(
                install_root=ROOT,
                state_root=root,
                url="",
                probe_provider="",
                probe_result=None,
                auto_close_seconds=0,
                check_remote_connection=False,
            )
            with mock.patch.object(MODULE, "profile_with_runtime_forward_port", return_value=runtime):
                host = MODULE.WorkStackDesktopHost(options)

            saved = MODULE.load_connection_draft(root)

        self.assertEqual(host.workstack_url, "http://127.0.0.1:24567/")
        self.assertEqual(saved["local_forward_port"], 18765)

    def test_remote_monitor_is_started_once_and_stopped_explicitly(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host.remote_monitor = None
        host.remote_shutdown_requested = MODULE.threading.Event()
        host._is_ready = mock.Mock(return_value=True)
        host._is_remote_process_alive = mock.Mock(return_value=True)
        host._reconnect_remote_once = mock.Mock(return_value=True)
        host._publish_remote_connection_state = mock.Mock()
        host._reload_workstack_after_reconnect = mock.Mock()

        with mock.patch.object(MODULE, "RemoteConnectionMonitor") as monitor_type:
            monitor = monitor_type.return_value
            monitor.is_running = True
            host._start_remote_monitor()
            host._start_remote_monitor()
            host._stop_remote_monitor()

        monitor_type.assert_called_once()
        monitor.start.assert_called_once_with()
        monitor.stop.assert_called_once_with(timeout=5)
        self.assertIsNone(host.remote_monitor)

    def test_reconnect_replaces_only_the_owned_tunnel(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host.remote_shutdown_requested = MODULE.threading.Event()
        host._stop_owned_remote_connection = mock.Mock()
        host._ensure_remote_server = mock.Mock()

        self.assertTrue(host._reconnect_remote_once())

        host._stop_owned_remote_connection.assert_called_once_with()
        host._ensure_remote_server.assert_called_once_with()

    def test_reconnect_failure_is_bounded_and_does_not_raise_to_monitor(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host.remote_shutdown_requested = MODULE.threading.Event()
        host._stop_owned_remote_connection = mock.Mock()
        host._ensure_remote_server = mock.Mock(side_effect=RuntimeError("bind failed"))
        host._trace = mock.Mock()

        self.assertFalse(host._reconnect_remote_once())
        host._trace.assert_called_once()

    def test_shutdown_request_prevents_a_late_tunnel_resurrection(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host.remote_shutdown_requested = MODULE.threading.Event()
        host.remote_shutdown_requested.set()
        host._stop_owned_remote_connection = mock.Mock()
        host._ensure_remote_server = mock.Mock()

        self.assertFalse(host._reconnect_remote_once())
        host._stop_owned_remote_connection.assert_not_called()
        host._ensure_remote_server.assert_not_called()

    def test_manual_reconnect_publishes_progress_then_ready_and_reloads(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host._publish_remote_connection_state = mock.Mock()
        host._reconnect_remote_once = mock.Mock(return_value=True)
        host._reload_workstack_after_reconnect = mock.Mock()

        host._run_manual_remote_reconnect()

        self.assertEqual(
            host._publish_remote_connection_state.call_args_list,
            [mock.call("reconnecting"), mock.call("ready")],
        )
        host._reload_workstack_after_reconnect.assert_called_once_with()
        host._reconnect_remote_once.assert_called_once_with(wait_for_active=True)

    def test_manual_reconnect_failure_ends_disconnected_without_reload(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host._publish_remote_connection_state = mock.Mock()
        host._reconnect_remote_once = mock.Mock(return_value=False)
        host._reload_workstack_after_reconnect = mock.Mock()

        host._run_manual_remote_reconnect()

        self.assertEqual(
            host._publish_remote_connection_state.call_args_list,
            [mock.call("reconnecting"), mock.call("disconnected")],
        )
        host._reload_workstack_after_reconnect.assert_not_called()
        host._reconnect_remote_once.assert_called_once_with(wait_for_active=True)

    def test_manual_reconnect_cannot_bypass_authority_recovery_block(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host.remote_reconnect_lock = MODULE.threading.Lock()
        host.remote_recovery_required = MODULE.threading.Event()
        host.remote_recovery_required.set()
        host.remote_shutdown_requested = MODULE.threading.Event()
        host._is_remote_process_alive = mock.Mock(return_value=True)
        host._is_remote_session_healthy = mock.Mock(return_value=False)
        host._stop_owned_remote_connection = mock.Mock()
        host._replace_remote_connection = mock.Mock()

        self.assertFalse(host._reconnect_remote_once(wait_for_active=True))

        host._replace_remote_connection.assert_not_called()
        host._stop_owned_remote_connection.assert_called_once_with()
        self.assertTrue(host.remote_shutdown_requested.is_set())

    def test_waiting_manual_reconnect_reuses_a_connection_restored_by_monitor(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.remote_reconnect_lock = MODULE.threading.Lock()
        host._is_remote_process_alive = mock.Mock(return_value=True)
        host._is_remote_session_healthy = mock.Mock(return_value=True)
        host._replace_remote_connection = mock.Mock()

        self.assertTrue(host._reconnect_remote_once(wait_for_active=True))

        host._replace_remote_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
