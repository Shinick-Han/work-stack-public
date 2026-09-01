from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "workstack_desktop.py"
SPEC = importlib.util.spec_from_file_location("workstack_desktop_registry_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    SPEC.loader.exec_module(MODULE)


PROFILE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"


def bare_host(root: Path):
    host = object.__new__(MODULE.WorkStackDesktopHost)
    host.state_root = root
    host.options = argparse.Namespace(url=None)
    host.local_startup_selection = None
    host._origin = mock.Mock(side_effect=lambda url: url.rstrip("/"))
    host._local_runtime_config = mock.Mock(return_value=(
        {"data_dir": str(root / "legacy"), "port": 8765}, root / "config.json"
    ))
    return host


class DesktopConnectionRegistryStartupTest(unittest.TestCase):
    def test_registry_startup_is_on_by_default_with_explicit_recovery_opt_out(self) -> None:
        self.assertTrue(MODULE.connection_registry_startup_enabled({}))
        self.assertTrue(MODULE.connection_registry_startup_enabled({"WORKSTACK_CONNECTION_REGISTRY_V1": "1"}))
        self.assertFalse(MODULE.connection_registry_startup_enabled({"WORKSTACK_CONNECTION_REGISTRY_V1": "0"}))

    def test_registry_startup_stays_dark_until_release_gate_is_enabled(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.startup_error = None
        host.startup_ready = mock.Mock()
        host.connection_registry_startup_enabled = False
        host._prepare_connection_registry_runtime = mock.Mock()
        host._ensure_server = mock.Mock()

        host._prepare_server()

        host._prepare_connection_registry_runtime.assert_not_called()
        host._ensure_server.assert_called_once()

    def test_prepare_server_selects_registry_before_starting_runtime(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.startup_error = None
        host.startup_ready = mock.Mock()
        host.connection_registry_startup_enabled = True
        host._prepare_connection_registry_runtime = mock.Mock()
        host._ensure_server = mock.Mock()
        host._confirm_pending_connection_registry_activation = mock.Mock()
        calls = mock.Mock()
        calls.attach_mock(host._prepare_connection_registry_runtime, "select")
        calls.attach_mock(host._ensure_server, "start")
        calls.attach_mock(
            host._confirm_pending_connection_registry_activation, "confirm"
        )

        host._prepare_server()

        self.assertEqual(
            calls.mock_calls,
            [mock.call.select(), mock.call.start(), mock.call.confirm()],
        )
        host.startup_ready.set.assert_called_once()

    def test_local_active_profile_overrides_data_and_backup_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = bare_host(root)
            selection = MODULE.LocalStartupSelection(
                profile_id=PROFILE_ID,
                label="Local",
                expected_workspace_id=WORKSPACE_ID,
                data_dir=root / "workspace-a",
                backup_dir=root / "workspace-backups" / PROFILE_ID,
                live_updates=True,
            )
            host._configured_url = mock.Mock(return_value="http://127.0.0.1:8765/")
            registry = mock.sentinel.registry
            with mock.patch.object(
                MODULE, "ensure_connection_registry", return_value=registry
            ) as ensure, mock.patch.object(
                MODULE, "select_active_profile_for_startup", return_value=selection
            ), mock.patch.object(
                MODULE, "registry_digest", return_value="sha256:" + "1" * 64
            ), mock.patch.object(
                MODULE,
                "current_registry_snapshot",
                return_value=(registry, "sha256:" + "1" * 64),
            ), mock.patch.object(MODULE, "export_active_legacy_mirror"):
                host._prepare_connection_registry_runtime()

            ensure.assert_called_once()
            self.assertIs(host.local_startup_selection, selection)
            self.assertIsNone(host.remote_profile)
            self.assertEqual(host.active_connection_draft, {"storage_mode": "local"})
            host._configured_url.assert_called_once()

    def test_remote_active_profile_drives_tunnel_fields_and_runtime_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = bare_host(root)
            selection = MODULE.SshStartupSelection(
                profile_id=PROFILE_ID,
                label="Remote",
                expected_workspace_id=WORKSPACE_ID,
                ssh_host_alias="work-linux",
                remote_app_dir="/srv/workstack/app",
                remote_data_dir="/srv/workstack/ssot",
                preferred_forward_port=18765,
                remote_port=8765,
                live_updates=True,
            )
            runtime = MODULE.RemoteConnectionProfile(
                "work-linux", "/srv/workstack/app", "/srv/workstack/ssot",
                29123, WORKSPACE_ID, 8765,
            )
            registry = mock.sentinel.registry
            with mock.patch.object(
                MODULE, "ensure_connection_registry", return_value=registry
            ), mock.patch.object(
                MODULE, "select_active_profile_for_startup", return_value=selection
            ), mock.patch.object(
                MODULE, "profile_with_runtime_forward_port", return_value=runtime
            ), mock.patch.object(
                MODULE, "registry_digest", return_value="sha256:" + "1" * 64
            ), mock.patch.object(
                MODULE,
                "current_registry_snapshot",
                return_value=(registry, "sha256:" + "1" * 64),
            ), mock.patch.object(MODULE, "export_active_legacy_mirror"):
                host._prepare_connection_registry_runtime()

            self.assertIsNone(host.local_startup_selection)
            self.assertIs(host.remote_profile, runtime)
            self.assertEqual(host.workstack_url, "http://127.0.0.1:29123/")
            self.assertEqual(host.active_connection_draft["workspace_id"], WORKSPACE_ID)

    def test_local_runtime_config_uses_profile_scoped_paths_without_rewriting_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(
                '{"port":8765,"data_dir":"C:/legacy","backup_dir":"C:/legacy-backup","backup_retention":7}',
                encoding="utf-8",
            )
            original = config_path.read_bytes()
            host = object.__new__(MODULE.WorkStackDesktopHost)
            host.state_root = root
            host.install_root = root
            host.local_startup_selection = MODULE.LocalStartupSelection(
                PROFILE_ID, "Local", WORKSPACE_ID, root / "ssot", root / "backups", True
            )

            config, loaded_path = host._local_runtime_config()

            self.assertEqual(loaded_path, config_path)
            self.assertEqual(config["data_dir"], str(root / "ssot"))
            self.assertEqual(config["backup_dir"], str(root / "backups"))
            self.assertEqual(config_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
